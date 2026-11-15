"""Change impact, metadata comparison, drift and reconciliation over the canonical graph (database flow D3, plan §3;
DB-13, DB-14, DB-16). Deterministic: no database query, no model call, no process. Results are cached as dbstore
records; a read (dashboard GET, MCP tool, `cli db impact` without --refresh) only projects the cache, never recomputes.

impact(ctx, target, change)  target: an object (o_) or column (c_) ID; change: {kind: rename | type | nullability |
    default | semantic | application_only, to?, note?} or None (every relation). Breadth-first over the VISIBLE typed
    edges (dbflows.Visible: dbaccess.decide on every record) in the directions the change kind needs: readers, writers,
    views / procedures / triggers, foreign-key dependents and references, column lineage (transforms / copies) and
    onward through events to their consumers and destination tables, jobs and reports, and the visible tickets whose
    recorded flows touch it. Each item: the node, its relation and path, basis (direct evidence apart from inferred
    association), stale / unavailable flags, why this change kind affects it, and a suggested validation with its
    rationale (text only: nothing is ever run). Limits (depth <= 3, <= 100 nodes) and unknown coverage are listed
    ("Writers incomplete on qa-billing", "Static analysis may miss dynamic SQL"). Nothing recorded -> "No recorded
    impact links" (never "no impact", never "safe to change"). A mapping that feeds the target also lists the
    component whose cited code implements it (relation `mapper`, same citation). headline(items) counts exactly the
    listed items (links and tickets apart); views recompute it from what the reader may see. resolve_target narrows a
    shared name only by an explicit env / connection.
compare(ctx, left, right)    two permitted connections ("qa-main") or two metadata snapshots of one connection
    ("qa-main@20261107T101500"). Objects pair only through an explicit mapping: the same object (two snapshots of one
    connection), a cross_environment_mapping you confirmed (cli db map-confirm) or a pair you confirmed for comparing
    (confirm_pair); the same qualified name on two connections is only a *candidate* until confirmed. Columns, types
    (engine-aware normalisation: int4 = integer, varchar(50) != varchar(100)), nullability, defaults, keys, definition
    availability and recorded code applicability; categories added / removed / changed / unchanged / not_comparable /
    unavailable. An object missing from a partial or denied inventory is unavailable, never removed. A side whose
    connection is not approved blocks the comparison with the reason; nothing widens qa_data.allowed_envs or relabels an
    environment. Metadata only: matching schema says nothing about data, configuration, deployed code or behaviour, and
    row-by-row comparison is not offered.
take_snapshot(ctx, conn)     the connection's visible metadata as data/db/snapshots/<conn>/<ts>.json (after a metadata
    refresh; an unchanged snapshot is not repeated, at most dbstore.SNAPSHOT_KEEP kept).
reconcile(ctx, cfg)          fingerprints (schema version, policy / profile generation, per-connection scope, mapping
    file, cited code files, source fingerprints) -> saved flows and investigations whose dependencies changed get
    review {state: needs_review, label "Requires revalidation", reasons} without losing a version, a note or a run;
    removed objects (a complete listing no longer shows them) stay historical and leave current lookups; cached
    impact / comparison records naming a connection no longer approved are deleted (the search index re-projects
    with today's access, the handoff copy re-checks access). Per-source last success / last attempt / failure, stable
    event IDs and a resumable checkpoint live in data/db/reconcile.json. Everything is published under one dbstore
    lock after re-checking the policy generation: a pass started under an older scope publishes nothing.
flow_drift(ctx, rec)         why a saved flow needs revalidation, from the per-source fingerprints it recorded (cheap:
    no rebuild on read).
"""
import hashlib
import os
import re
import time

from . import common
from . import dbaccess
from . import dbflows
from . import dbmodel
from . import dbstore

SCHEMA_VERSION = 1
DEPTH, MAX_NODES = 3, 100
CHANGE_KINDS = ("rename", "type", "nullability", "default", "semantic", "application_only")
CHANGE_LABEL = {"rename": "Rename", "type": "Type change", "nullability": "Nullability change",
                "default": "Default change", "semantic": "Meaning change", "application_only": "Application-only change",
                None: "Any change"}
NO_LINKS = "No recorded impact links"
NO_LINKS_NOTE = ("Nothing recorded depends on it. This is not evidence that the change is safe: dynamic SQL, other "
                 "services, reports and jobs outside the selected repositories are not seen.")
SUGGEST_NOTE = "Suggestions only: nothing was run (no tests, migrations or requests)."
REVIEW_LABEL = "Requires revalidation"
SCHEMA_ONLY = ("Schema comparison only: matching metadata says nothing about data, configuration, deployed code or "
               "behaviour.")
NO_ROWS = "Row-by-row comparison is not offered: it would need its own reviewed record plan (DB-10)."
NOT_SYNC = "The selected objects were compared; this never says the environments are synchronised."
CATEGORIES = ("added", "removed", "changed", "unchanged", "not_comparable", "unavailable")
CATEGORY_LABEL = {"added": "Added", "removed": "Removed", "changed": "Changed", "unchanged": "Unchanged",
                  "not_comparable": "Not comparable", "unavailable": "Unavailable (unknown)"}
DIRECT = ("declared_metadata", "code_supported", "user_confirmed", "observed_sample")
RECONCILE_BUDGET = 200  # flows + investigations per pass; the rest resumes from the checkpoint
EVENTS_KEEP = 200
CODE_FILES_MAX = 500


class ImpactError(ValueError):
    """A target or change that can't be analysed (unknown or invisible ID, unknown change kind)."""


class CompareError(ValueError):
    """A side that isn't a connection or snapshot reference."""


def _now():
    return common.now_iso()


def normalise_change(change):
    """{kind, to, note} with bounded strings, or None. Raises ImpactError for an unknown kind."""
    if change in (None, "", {}):
        return None
    if isinstance(change, str):
        change = {"kind": change}
    if not isinstance(change, dict) or change.get("kind") not in CHANGE_KINDS:
        raise ImpactError("change kind is one of %s" % ", ".join(CHANGE_KINDS))
    out = {"kind": change["kind"]}
    for k in ("to", "note"):
        v = change.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = " ".join(v.split())[:120]
    return out


def impact_id(target, change=None, depth=DEPTH, max_nodes=MAX_NODES):
    ch = normalise_change(change) or {}
    return dbmodel.new_id("x", "impact", target, ch.get("kind") or "", ch.get("to") or "", int(depth), int(max_nodes))


def compare_id(left, right, objects=None):
    return dbmodel.new_id("x", "compare", left, right, sorted(objects or []))


# --------------------------------------------------------------------------- dependency fingerprints

def sources_now():
    s = dbstore.read_file("sources").get("sources")
    return s if isinstance(s, dict) else {}


def scope_fp(ctx, conn):
    """Fingerprint of one connection's approved scope entry ("" when it is not approved now)."""
    e = ctx.connections.get(conn)
    return dbmodel.fingerprint(e)[:16] if e else ""


def dep_key(k):
    """A source key as a derived record keeps it: a ticket source is hashed (a record never carries a ticket key: it
    may be someone else's ticket), every other key is technical and kept as written."""
    return "ticket#" + dbmodel.fingerprint(k)[:16] if str(k).startswith("ticket:") else k


def deps_of(vis, node_ids, extra=()):
    """{source key: fingerprint} of the ingestion sources that contributed the given nodes (their attrs.from), plus
    `extra` keys: what a derived record depends on (ticket keys hashed, dep_key)."""
    keys = set(extra)
    for nid in node_ids:
        n = vis.nodes.get(nid) or vis.raw["nodes"].get(nid) or {}
        for k in ((n.get("attrs") or {}).get("from") or []):
            if isinstance(k, str):
                keys.add(k)
        if n.get("parent"):
            p = vis.nodes.get(n["parent"]) or {}
            keys.update(k for k in ((p.get("attrs") or {}).get("from") or []) if isinstance(k, str))
    srcs = sources_now()
    return {dep_key(k): (srcs.get(k) or {}).get("fingerprint") or "" for k in sorted(keys)}


def changed_deps(deps, srcs=None):
    """The dependency keys whose fingerprint differs from the one recorded (a removed source counts as changed)."""
    srcs = sources_now() if srcs is None else srcs
    if not deps:
        return []
    cur = {dep_key(k): (v or {}).get("fingerprint") or "" for k, v in srcs.items() if isinstance(v, dict)}
    return sorted(k for k, fp in deps.items() if cur.get(k, "") != fp)


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(dbmodel.CITE_MAX_BYTES + 1)).hexdigest()
    except OSError:
        return ""


# --------------------------------------------------------------------------- impact (DB-13)

# relation -> (the change kinds that affect it, why per kind; "*" is the fallback)
AFFECTS = {
    "reader": ({"rename", "type", "nullability", "semantic", "application_only"}, {
        "rename": "It reads the object by name: its query must use the new name.",
        "type": "It reads the value: parsing, comparisons and sizes may change with the type.",
        "nullability": "It reads the value: it may meet NULLs it did not expect.",
        "semantic": "It reads the value and acts on its meaning.",
        "application_only": "It is application code that would change.",
        "default": "A default only changes rows written without the column; readers see it only through new rows.",
        "*": "It reads the object."}),
    "writer": (set(CHANGE_KINDS), {
        "rename": "It writes the object by name: its statement must use the new name.",
        "type": "It writes the value: the value it supplies must fit the new type.",
        "nullability": "It writes the object: it must supply a value when the column becomes NOT NULL.",
        "default": "It writes the object: rows it writes without the column get the new default.",
        "semantic": "It produces the value: its meaning comes from here.",
        "application_only": "It is application code that would change.",
        "*": "It writes the object."}),
    "trigger": ({"rename", "type", "nullability", "default", "semantic"}, {
        "*": "A trigger on the object runs with every write: its definition names the object's columns.",
        "application_only": "An application-only change leaves the trigger's table as it is."}),
    "caller": ({"rename", "type", "nullability", "semantic", "application_only"}, {
        "*": "It calls the routine: its arguments and results follow the routine's signature."}),
    "fk_dependent": ({"rename", "type", "semantic"}, {
        "rename": "A foreign key references this column; joins written by name must change.",
        "type": "A foreign key references this column: the referencing column must keep a compatible type.",
        "semantic": "Rows elsewhere point at these values: a new meaning changes what they point at.",
        "*": "A foreign key references this object; this change kind leaves the reference as it is."}),
    "fk_reference": ({"rename", "type", "nullability"}, {
        "rename": "Its foreign key names this column.",
        "type": "Its foreign key needs a type compatible with the referenced key.",
        "nullability": "A nullable foreign key means rows without a parent.",
        "*": "This object references another one; this change kind leaves the reference as it is."}),
    "lineage": ({"rename", "type", "nullability", "semantic"}, {
        "rename": "A recorded mapping names this column: the mapping must follow the new name.",
        "type": "The value is carried here by a recorded mapping: the destination must accept the new type.",
        "nullability": "The value is carried here: the destination may receive NULLs.",
        "semantic": "The value is carried here: its meaning changes downstream too.",
        "*": "The value is carried here by a recorded mapping."}),
    "upstream": ({"rename", "type", "nullability"}, {
        "rename": "A recorded mapping writes into this column by name.",
        "type": "A recorded mapping feeds this column: the value it carries must fit the new type.",
        "nullability": "A recorded mapping feeds this column: NULLs it carries must be allowed.",
        "*": "A recorded mapping feeds this object; this change kind does not reach it."}),
    "consumer": ({"type", "nullability", "semantic", "application_only"}, {
        "*": "It consumes an event that carries the value.",
        "application_only": "It consumes an event the changed code publishes."}),
    "downstream_reader": ({"type", "nullability", "semantic"}, {
        "*": "It reads or writes a destination the value is carried to."}),
    "entry": ({"application_only"}, {
        "*": "It is how the changed code is reached (an entry point or caller)."}),
    "association": (set(CHANGE_KINDS) - {"application_only"}, {
        "*": "Only an inferred association (names or a sample), not a recorded dependency."}),
}
RELATION_LABEL = {"reader": "Reader", "writer": "Writer", "trigger": "Trigger", "caller": "Caller",
                  "fk_dependent": "Foreign-key dependent", "fk_reference": "Foreign-key reference",
                  "lineage": "Carries the value (lineage)", "upstream": "Feeds the value (lineage)",
                  "consumer": "Event consumer", "downstream_reader": "Downstream reader", "entry": "Entry point",
                  "association": "Inferred association", "ticket": "Ticket"}
VALIDATE = {
    "reader": ("Update and re-run the tests that cover %(label)s (search its code for %(what)s).",
               "%(label)s reads %(what)s%(cite)s."),
    "writer": ("Check every write path of %(label)s still succeeds after the change, then run its tests.",
               "%(label)s writes %(what)s%(cite)s."),
    "trigger": ("Review the definition of %(label)s for the changed columns.", "%(label)s runs on writes to %(what)s."),
    "caller": ("Re-run the tests of %(label)s that call the routine.", "%(label)s calls %(what)s%(cite)s."),
    "fk_dependent": ("Change %(label)s's referencing column in the same migration and validate the constraint on a "
                     "QA copy.", "%(label)s has a declared foreign key to %(what)s."),
    "fk_reference": ("Check the foreign key from %(what)s to %(label)s still validates.",
                     "%(what)s declares a foreign key to %(label)s."),
    "lineage": ("Compare the destination %(label)s with the change (type, NULLs, meaning) before releasing.",
                "A recorded mapping carries %(what)s to %(label)s%(cite)s."),
    "upstream": ("Check the mapping from %(label)s still produces values that fit.",
                 "A recorded mapping feeds %(what)s from %(label)s%(cite)s."),
    "consumer": ("Replay a recorded event through %(label)s (or ask its owner) after the change.",
                 "%(label)s consumes an event that carries %(what)s%(cite)s."),
    "downstream_reader": ("Re-run the tests of %(label)s.", "%(label)s uses where %(what)s ends up%(cite)s."),
    "entry": ("Exercise %(label)s end to end after the change.", "%(label)s reaches the changed code%(cite)s."),
    "association": ("Confirm whether %(label)s really depends on %(what)s before relying on it.",
                    "Only an inferred association links them%(cite)s."),
    "ticket": ("Replay %(label)s's saved requests and re-run its tests after the change (/cg:ticket %(label)s).",
               "%(label)s recorded a flow that touches %(what)s."),
}


def _relation(e, here, col, carried):
    """(relation, other end, columns carried onward | None, columns_unknown) of one visible edge seen from `here`, or
    None when the edge is not a dependency of the column(s) in question."""
    k, src, dst = e.get("kind"), e.get("source"), e.get("target")
    other = dst if src == here else src
    want = set(carried or ([col] if col else []))
    cols = e.get("columns") or {}
    used = set((cols.get("read") or []) + (cols.get("written") or []))
    maps = e.get("column_mapping") or []
    if k in ("reads", "writes", "calls") and dst == here:
        unknown = bool(want) and not used
        if want and used and not want & used:
            return None
        rel = "caller" if k == "calls" else "writer" if k == "writes" else "reader"
        return rel, other, None, unknown
    if k == "triggers" and src == here:
        return "trigger", other, None, bool(want)
    if k == "foreign_key":
        side = [set(m.get("to" if dst == here else "from") or []) for m in maps]
        if want and not any(want & s for s in side):
            return None
        return ("fk_dependent" if dst == here else "fk_reference"), other, None, False
    if k in ("transforms", "copies"):
        a, b = ("from", "to") if src == here else ("to", "from")
        hit = [m for m in maps if not want or want & set(m.get(a) or [])]
        if not hit:
            return None
        return ("lineage" if src == here else "upstream"), other, sorted({c for m in hit for c in m.get(b) or []}), False
    if k == "logical_association":
        if want and maps and not any(want & set((m.get("from") or []) + (m.get("to") or [])) for m in maps):
            return None
        return "association", other, None, False
    return None


AFFECTS["published"] = ({"application_only"}, {"*": "The changed code publishes it: its payload may change."})
RELATION_LABEL["published"] = "Published by the changed code"
VALIDATE["published"] = ("Check the payload of %(label)s against its consumers' expectations.",
                         "The changed code publishes %(label)s%(cite)s.")
# The component whose cited code implements a recorded mapping into the object (it takes the mapping's source and
# writes the object, under the same citation): the mapping edge alone names only its source (an event, a table).
AFFECTS["mapper"] = (set(CHANGE_KINDS) - {"default"}, {
    "rename": "Its code writes this column through the recorded mapping: its statement must use the new name.",
    "type": "Its code writes the mapped value: the value must fit the new type.",
    "nullability": "Its code writes the mapped value: it must supply one when the column becomes NOT NULL.",
    "semantic": "Its code produces the value through the recorded mapping.",
    "application_only": "It is application code that would change.",
    "default": "It writes this column through the mapping; a default only applies to rows written without it.",
    "*": "Its code implements the recorded mapping into this object."})
RELATION_LABEL["mapper"] = "Implements the mapping (writer)"
VALIDATE["mapper"] = ("Check the statement and the mapping in %(label)s after the change, then run its tests.",
                      "%(label)s implements the recorded mapping into %(what)s%(cite)s.")


def _implementers(vis, e, here):
    """[(writes edge ID, component)] for a mapping edge `e` (transforms / copies) that feeds `here`: components with a
    visible `writes` edge into `here` citing the same evidence as the mapping, that also consume or read the mapping's
    source (or are that source). Evidence-backed only: a name or a shared event alone never makes an implementer."""
    src, cite = e.get("source"), set(e.get("evidence") or [])
    if not cite:
        return []
    out = []
    for wid in sorted(vis.adj.get(here, [])):
        w = vis.edges[wid]
        x = w.get("source")
        if w.get("kind") != "writes" or w.get("target") != here or not cite & set(w.get("evidence") or []) or \
                (vis.nodes.get(x) or {}).get("kind") in dbmodel.DB_KINDS:
            continue
        linked = x == src or any(vis.edges[l].get("kind") in ("consumes", "reads") and vis.edges[l].get("source") == x
                                 and vis.edges[l].get("target") == src for l in vis.adj.get(x, []))
        if linked:
            out.append((wid, x))
    return out


def headline(items):
    """The summary line of exactly the listed items: impact links (direct / inferred) and tickets counted apart, so
    the numbers add up to what "Affected" lists."""
    links = [it for it in items if it.get("relation") != "ticket"]
    tix = len(items) - len(links)
    if not links and not tix:
        return NO_LINKS
    direct = sum(1 for it in links if it.get("basis") == "direct")
    n, s = len(links), "" if len(links) == 1 else "s"
    if not tix:
        return "%d recorded impact link%s: %d with direct evidence, %d inferred" % (n, s, direct, n - direct)
    part = "%d recorded impact link%s (%d with direct evidence, %d inferred)" % (n, s, direct, n - direct) if links \
        else "no recorded impact links"
    return "%d affected: %s and %d ticket%s whose recorded flow touches it" % (len(items), part, tix,
                                                                               "" if tix == 1 else "s")


def _onward(e, here, came_as, carried, kind):
    """Relations one hop further from a node reached at depth >= 1: consumers and destinations of a carried value;
    entry points and published events of changed application code."""
    k, src, dst = e.get("kind"), e.get("source"), e.get("target")
    want = set(carried or [])
    if came_as in ("lineage", "published"):
        if k == "consumes" and dst == here:
            return "consumer", src, None, False
        if came_as == "lineage" and k in ("transforms", "copies") and src == here:
            hit = [m for m in e.get("column_mapping") or [] if not want or want & set(m.get("from") or [])]
            if hit:
                return "lineage", dst, sorted({c for m in hit for c in m.get("to") or []}), False
        if came_as == "lineage" and k in ("reads", "writes") and dst == here:
            cols = e.get("columns") or {}
            used = set((cols.get("read") or []) + (cols.get("written") or []))
            if not want or not used or want & used:
                return "downstream_reader", src, None, bool(want) and not used
        return None
    if came_as in ("reader", "writer", "caller", "mapper") and kind == "application_only":
        if k == "calls" and dst == here:
            return "entry", src, None, False
        if k == "publishes" and src == here:
            return "published", dst, None, False
    return None


def _flags(vis, e, nid):
    stale, unavailable = [], []
    for v in e.get("evidence") or []:
        ev = vis.evidence.get(v) or {}
        q = ev.get("quality") or {}
        if q.get("freshness") == "stale":
            stale.append("evidence stale%s" % (" (as of %s)" % str(q.get("as_of"))[:10] if q.get("as_of") else ""))
        if q.get("validation") == "needs_review" or ev.get("lifecycle") == "needs_review":
            stale.append("cited code changed since it was recorded")
    if e.get("lifecycle") not in (None, "current"):
        stale.append("relation %s" % e.get("lifecycle"))
    n = vis.nodes.get(nid) or {}
    if n.get("lifecycle") not in (None, "current"):
        stale.append("object %s" % n.get("lifecycle"))
    if nid in vis.inferred_only:
        unavailable.append("code from a repository that is not selected")
    if (n.get("attrs") or {}).get("definition") == "unavailable":
        unavailable.append("definition unavailable")
    if n.get("kind") in dbmodel.NODE_KINDS and n.get("kind") != "event":
        kinds = [(vis.evidence.get(v) or {}) for v in e.get("evidence") or []]
        if not any(x.get("kind") == "source" and (x.get("quality") or {}).get("validation") == "validated" and
                   (x.get("provenance") or {}).get("path") for x in kinds):
            unavailable.append("code not checked against a selected repository")
    return sorted(set(stale)), sorted(set(unavailable))


def _cite(vis, e):
    for v in e.get("evidence") or []:
        x = vis.evidence_view(v)
        if x:
            return " (%s)" % (x.get("cite") or x.get("text") or "")[:160]
    return ""


def _why(rel, kind):
    kinds, texts = AFFECTS[rel]
    return (kind is None or kind in kinds), texts.get(kind) or texts["*"]


def impact(ctx, target, change=None, depth=DEPTH, max_nodes=MAX_NODES, vis=None, cfg=None, save=True):
    """The impact result for one visible object or column (module doc). Raises ImpactError. save=True caches it."""
    vis = vis or dbflows.Visible(ctx, cfg=cfg)
    ch = normalise_change(change)
    kind = (ch or {}).get("kind")
    depth = max(1, min(int(depth), DEPTH))
    max_nodes = max(1, min(int(max_nodes), MAX_NODES))
    if not dbmodel.valid_id(target) or target[0] not in "oc" or target not in vis.nodes:
        raise ImpactError("no such object or column")
    col = target if target[0] == "c" else None
    obj = vis.nodes[target].get("parent") if col else target
    if obj not in vis.nodes or vis.nodes[obj].get("kind") not in dbmodel.DB_KINDS:
        raise ImpactError("impact is analysed for database objects and their columns")
    what = vis.label(target)
    items, excluded, order = {}, [], []
    visited = {obj, target}
    queue = [(obj, 0, [vis.label(obj)], [col] if col else None, "direct", None)]
    cut = False
    while queue and not cut:
        here, d, path, carried, pbasis, came_as = queue.pop(0)
        if d >= depth:
            continue
        found = []
        for eid in sorted(vis.adj.get(here, [])):
            e = vis.edges[eid]
            r = _relation(e, here, col, carried) if d == 0 else _onward(e, here, came_as, carried, kind)
            if r is None:
                continue
            found.append((eid, e, r))
            if d == 0 and r[0] == "upstream":  # who implements the mapping into it (same citation)
                found += [(wid, vis.edges[wid], ("mapper", x, None, False)) for wid, x in _implementers(vis, e, here)]
        for eid, e, r in found:
            rel, other, onward, unknown = r
            affected, why = _why(rel, kind)
            if other in visited:
                it = items.get(other)
                if affected and it is not None and it["depth"] == d + 1 and rel != it["relation"] and \
                        rel not in [a["relation"] for a in it["also"]]:
                    it["also"].append({"relation": rel, "relation_label": RELATION_LABEL[rel], "why": why,
                                       "edge": eid})  # the same code both reads and writes it, say
                continue
            if affected and kind == "rename" and rel == "lineage" and d >= 1:
                affected, why = False, "A rename stops at the first mapping: later hops carry other names."
            if not affected:
                if len(excluded) < 30:
                    excluded.append({"id": other, "label": vis.label(other), "relation": rel,
                                     "relation_label": RELATION_LABEL[rel], "reason": why})
                continue
            if len(order) >= max_nodes:
                cut = True
                break
            visited.add(other)
            eb = dbflows._basis(vis, e)
            basis = "direct" if pbasis == "direct" and eb in DIRECT and rel != "association" else "inferred"
            stale, unav = _flags(vis, e, other)
            n = vis.nodes.get(other) or {}
            fill = {"label": vis.label(other), "what": what, "cite": _cite(vis, e)}
            items[other] = {
                "id": other, "label": fill["label"], "kind": n.get("kind"),
                "kind_label": dbmodel.KIND_LABEL.get(n.get("kind")) or str(n.get("kind") or "").replace("_", " "),
                "relation": rel, "relation_label": RELATION_LABEL[rel], "depth": d + 1, "path": path + [fill["label"]],
                "edge": eid, "edge_kind": e.get("kind"), "basis": basis,
                "basis_label": "Direct evidence" if basis == "direct" else "Inferred association",
                "evidence_basis": eb, "evidence_label": dbmodel.BASIS_LABEL.get(eb, ""), "cite": fill["cite"].strip(" ()"),
                "stale": bool(stale), "stale_reasons": stale, "unavailable": bool(unav), "unavailable_reasons": unav,
                "columns_unknown": bool(unknown), "why": why + (" Its recorded columns are unknown: it may not use this "
                                                              "column." if unknown else ""),
                "validation": {"text": VALIDATE[rel][0] % fill, "rationale": VALIDATE[rel][1] % fill},
                "connection": vis.conn_of(other) or "", "environment": vis.env_of(other) or "",
                "tickets": vis.tickets(e), "also": []}
            order.append(other)
            queue.append((other, d + 1, path + [fill["label"]], onward, basis, rel))
    return _impact_result(ctx, vis, target, obj, col, ch, depth, max_nodes, items, order, excluded, cut, queue, save)


def _source_gap(vis, nid):
    """Coverage notes of one DB object's writers / description sources (never "no writers")."""
    n = vis.nodes.get(nid) or {}
    conn = vis.conn_of(nid) or "?"
    out = []
    srcs = (n.get("attrs") or {}).get("sources") or {}
    w = srcs.get("writers") or {}
    st, cov = w.get("state") or "not_checked", w.get("coverage") or "not_checked"
    if st == "failed":
        out.append("Writers incomplete on %s: the last writers refresh of %s failed (earlier data kept, stale)" % (
            conn, vis.label(nid, False)))
    elif st == "unsupported":
        out.append("Writers not available on %s for this engine (%s)" % (conn, vis.label(nid, False)))
    elif st == "not_checked":
        out.append("Writers not checked yet on %s (%s)" % (conn, vis.label(nid, False)))
    elif cov == "partial" or w.get("truncated"):
        out.append("Writers incomplete on %s: %s was read partially (a hidden routine or a row limit)" % (
            conn, vis.label(nid, False)))
    if w.get("freshness") == "stale" and st == "ok":
        out.append("Writers of %s on %s are stale (last read %s)" % (vis.label(nid, False), conn,
                                                                   str(w.get("last_success"))[:10]))
    return out


def _coverage(vis, obj, items):
    notes = _source_gap(vis, obj)
    for it in items:
        if it.get("kind") in ("table", "view") and it.get("id"):
            notes += _source_gap(vis, it["id"])
    for c in vis.claims.values():
        if c.get("predicate") == "routine_unreadable" and c.get("subject") == obj:
            notes.append("Definition of %s unavailable on %s: what it reads and writes is unknown" % (
                vis.label(c.get("value"), False), vis.conn_of(obj) or "?"))
        if c.get("predicate") == "unresolved_table":
            v = c.get("value") or {}
            cands = [x.get("id") if isinstance(x, dict) else x for x in v.get("candidates") or []]
            if obj in cands:
                notes.append("A recorded entry names %r without its schema: it may also depend on this object "
                             "(needs resolution)" % str(v.get("ref") or "")[:80])
    unav = sum(1 for it in items if it.get("unavailable"))
    if unav:
        notes.append("%d item(s) cite code that is unavailable or not checked against a selected repository" % unav)
    notes.append(dbflows.DISCLAIMER)
    notes.append("Deployed revision unknown: code relations come from the selected repositories' working trees.")
    return list(dict.fromkeys(notes))


def _impact_result(ctx, vis, target, obj, col, ch, depth, max_nodes, items, order, excluded, cut, queue, save):
    kind = (ch or {}).get("kind")
    out = [items[i] for i in order]
    tickets = {}
    for it in out:
        for t in it.get("tickets") or []:
            tickets.setdefault(t, []).append(it["basis"])
    what = vis.label(target)
    tix = [{"id": None, "key": t, "label": t, "kind": "ticket", "kind_label": "Ticket", "relation": "ticket",
            "relation_label": RELATION_LABEL["ticket"], "depth": None, "path": [], "edge": None, "basis":
                "direct" if "direct" in b else "inferred",
            "basis_label": "Direct evidence" if "direct" in b else "Inferred association", "stale": False,
            "stale_reasons": [], "unavailable": False, "unavailable_reasons": [], "columns_unknown": False,
            "why": "Its recorded flow touches this object; its saved requests and tests may exercise the change.",
            "validation": {"text": VALIDATE["ticket"][0] % {"label": t, "what": what, "cite": ""},
                           "rationale": VALIDATE["ticket"][1] % {"label": t, "what": what, "cite": ""}},
            "connection": "", "environment": "", "tickets": [t], "also": []} for t, b in sorted(tickets.items())]
    direct = sum(1 for it in out if it["basis"] == "direct")
    by_rel = {}
    for it in out:
        by_rel[it["relation"]] = by_rel.get(it["relation"], 0) + 1
    head = headline(out + tix)
    conns = sorted({c for c in [vis.conn_of(obj)] + [it.get("connection") for it in out] if c})
    srcs = sources_now()
    rec = {
        "id": impact_id(target, ch, depth, max_nodes), "kind": "impact", "schema_version": SCHEMA_VERSION,
        "target": {"id": target, "label": what, "object": obj, "column": col, "kind": (vis.nodes.get(target) or {})
                   .get("kind"), "connection": vis.conn_of(obj) or "", "environment": vis.env_of(obj) or ""},
        "change": ch, "change_label": CHANGE_LABEL.get(kind), "computed_at": _now(), "headline": head,
        "note": NO_LINKS_NOTE if not out else "", "suggest_note": SUGGEST_NOTE, "executed": False,
        "summary": {"items": len(out), "direct": direct, "inferred": len(out) - direct, "by_relation": by_rel,
                    "stale": sum(1 for it in out if it["stale"]), "unavailable": sum(1 for it in out if it["unavailable"]),
                    "tickets": len(tix), "excluded": len(excluded)},
        "items": out + tix, "excluded": excluded, "coverage": _coverage(vis, obj, out),
        "limits": {"depth": depth, "max_nodes": max_nodes, "visited": len(out), "cut": cut,
                   "text": "Traversal limited to depth %d and %d items%s" % (
                       depth, max_nodes, "; stopped at the limit, more may be affected" if cut else "")},
        "connections": conns,
        "deps": deps_of(vis, [obj, target] + order, extra=[k for k in ("mappings", "linkmap") if k in srcs]),
        "policy_generation": ctx.policy_generation, "profile_generation": ctx.profile_generation,
        "scope": {c: scope_fp(ctx, c) for c in conns}, "published": False,
    }
    if save:
        rec = publish_cached("impact", rec, ctx)
    return rec


# --------------------------------------------------------------------------- the cache

def generation_now():
    """The policy generation of the config and guard files as they are now (re-read, not the caller's context)."""
    return dbaccess.policy_generation(common.load_config())


def publish_cached(kind, rec, ctx):
    """Save a computed impact / comparison under the store lock, only while the access policy it was computed under
    is still the current one (a result from an older scope publishes nothing: `published` False, `superseded`)."""
    with dbstore.lock():
        if generation_now() != ctx.policy_generation:
            return dict(rec, published=False, superseded=True)
        cur = dbstore.load(kind, rec["id"]) or {}
        return dbstore.save_locked(kind, dict(rec, published=True, revision=cur.get("revision") or 0))


def cached(kind, rec_id):
    """The cached record, or None (dbstore.load reads a missing file as {})."""
    return (dbstore.load(kind, rec_id) or None) if dbmodel.valid_id(rec_id) else None


def cache_state(ctx, rec):
    """How a cached record reads now (no recompute): {state: current | stale | scope_changed, reasons}."""
    if rec.get("policy_generation") != ctx.policy_generation and \
            any(scope_fp(ctx, c) != fp for c, fp in (rec.get("scope") or {}).items()):
        return {"state": "scope_changed", "reasons": ["the approved scope of a connection it names changed"]}
    reasons = ["source changed: %s" % k for k in changed_deps(rec.get("deps"))][:10]
    return {"state": "stale" if reasons else "current", "reasons": reasons}


# --------------------------------------------------------------------------- comparison (DB-14)


_TYPE_ALIASES = {
    "int": "integer", "int4": "integer", "serial": "integer", "serial4": "integer", "int8": "bigint",
    "bigserial": "bigint", "serial8": "bigint", "int2": "smallint", "bool": "boolean", "float8": "double precision",
    "float4": "real", "decimal": "numeric", "character varying": "varchar", "character": "char", "bpchar": "char",
    "timestamp without time zone": "timestamp", "timestamp with time zone": "timestamptz",
    "time without time zone": "time", "time with time zone": "timetz", "uniqueidentifier": "uuid",
}
_DEFAULT_PRECISION = {"datetime2": "7", "timestamp": "6", "timestamptz": "6", "time": "6", "datetimeoffset": "7"}
_TYPE_RX = re.compile(r"^([a-z][a-z0-9_ ]*?)\s*(?:\(\s*([^)]*?)\s*\))?\s*(\[\])?$")


def norm_type(engine, t):
    """Comparable form of a declared type: aliases folded (int4 = integer = int, character varying = varchar),
    a default precision dropped (datetime2(7) = datetime2), lengths / precision / scale kept (varchar(50) !=
    varchar(100), numeric(10,2) != numeric(12,2)). Unknown spellings are kept as written (lower case)."""
    s = " ".join(str(t or "").lower().split())
    if not s:
        return ""
    m = _TYPE_RX.match(s)
    if not m:
        return s
    base, args, arr = m.group(1).strip(), (m.group(2) or "").replace(" ", ""), m.group(3) or ""
    if engine == "mssql" and base == "timestamp":
        base = "rowversion"  # SQL Server's timestamp is a row version, not a date
    base = _TYPE_ALIASES.get(base, base)
    if base == "float" and args in ("", "53"):
        base, args = "double precision", ""
    if args and _DEFAULT_PRECISION.get(base) == args:
        args = ""
    if base == "numeric" and args and "," not in args:
        args += ",0"
    return base + ("(%s)" % args if args else "") + arr


def norm_default(v):
    if v is None:
        return None
    s = str(v).strip()
    while len(s) >= 2 and s[0] == "(" and s[-1] == ")" and s.count("(") == s.count(")"):
        s = s[1:-1].strip()  # SQL Server keeps defaults as ((0))
    s = re.sub(r"::[a-z][a-z0-9_ ]*(\(\d+\))?$", "", s, flags=re.I)  # PostgreSQL's 'x'::character varying
    return s


def _qn(d):
    return "%s.%s" % (d.get("schema"), d.get("name")) if d.get("schema") else str(d.get("name") or "")


def side_from_graph(ctx, vis, conn):
    """One connection's visible metadata now: {objects: {qualified name casefolded: entry}, schemas, taken_at}."""
    entry = ctx.connections.get(conn) or {}
    objects, newest = {}, ""
    for nid, n in vis.nodes.items():
        d = n.get("display") or {}
        if n.get("kind") not in dbmodel.DB_KINDS or d.get("connection") != conn:
            continue
        a = n.get("attrs") or {}
        cols = {}
        for m in vis.members.get(nid) or []:
            ma = m.get("attrs") or {}
            name = (m.get("display") or {}).get("member")
            if not name or "data_type" not in ma:
                continue  # a placeholder column (named by a ticket): nothing declared to compare
            cols[str(name).casefold()] = {"name": name, "type": ma.get("data_type") or "", "nullable": ma.get("nullable"),
                                          "default": ma.get("default"), "pk": bool(ma.get("primary_key")),
                                          "fk": ma.get("references") or ""}
        newest = max(newest, str(a.get("fetched_at") or ""))
        objects[_qn(d).casefold()] = {"id": nid, "name": _qn(d), "kind": n.get("kind"),
                                      "lifecycle": n.get("lifecycle") or "current",
                                      "described": bool(a.get("described")) or bool(cols),
                                      "definition": a.get("definition") or "", "columns": cols,
                                      "code": sum(1 for eid in vis.adj.get(nid, [])
                                                  if (vis.edges.get(eid) or {}).get("layer") == "flow")}
    schemas = {}
    for sch in entry.get("schemas") or []:
        cp = dbstore.load_checkpoint("inventory-%s" % dbmodel.fingerprint([conn, sch])[:16])
        schemas[str(sch).casefold()] = {"complete": bool(cp.get("done")) and bool(cp.get("at")),
                                        "listed_at": cp.get("at") or ""}
    denied = any(isinstance(q, dict) and q.get("connection") == conn for q in ctx.quarantine.values())
    return {"connection": conn, "env": entry.get("env") or "", "engine": entry.get("engine") or "", "source": "live",
            "snapshot": None, "taken_at": newest, "schemas": schemas, "denied": denied, "objects": objects}


def take_snapshot(ctx, conn, vis=None, now=None):
    """Write the connection's metadata snapshot (module doc). Returns {id, connection, taken_at, unchanged} or None
    when the connection is not approved."""
    if conn not in ctx.connections:
        return None
    vis = vis or dbflows.Visible(ctx)
    side = side_from_graph(ctx, vis, conn)
    body = {k: side[k] for k in ("connection", "env", "engine", "schemas", "denied", "objects")}
    fp = dbmodel.fingerprint(body)
    with dbstore.lock():
        ids = dbstore.list_snapshots(conn)
        last = common.read_json(dbstore.snapshot_path(conn, ids[-1]), None) if ids else None
        if isinstance(last, dict) and last.get("fingerprint") == fp:
            return {"id": ids[-1], "connection": conn, "taken_at": last.get("taken_at"), "unchanged": True}
        sid, n = time.strftime("%Y%m%dT%H%M%S"), 2
        base = sid
        while sid in ids:
            sid, n = "%s-%d" % (base, n), n + 1
        rec = dict(body, schema_version=SCHEMA_VERSION, id=sid, fingerprint=fp, taken_at=now or _now(),
                   metadata_at=side["taken_at"], policy_generation=ctx.policy_generation)
        common.write_json(dbstore.snapshot_path(conn, sid), rec)
        from .safety import paths
        for old in (ids + [sid])[:-dbstore.SNAPSHOT_KEEP]:
            paths.delete(dbstore.snapshot_path(conn, old), caller="dbimpact.snapshot")
    return {"id": sid, "connection": conn, "taken_at": rec["taken_at"], "unchanged": False}


def snapshots(ctx, conn):
    """[{id, taken_at, metadata_at, objects}] of an approved connection, newest first (no query)."""
    if conn not in ctx.connections:
        return []
    out = []
    for sid in reversed(dbstore.list_snapshots(conn)):
        s = common.read_json(dbstore.snapshot_path(conn, sid), None)
        if isinstance(s, dict):
            out.append({"id": sid, "ref": "%s@%s" % (conn, sid), "taken_at": s.get("taken_at") or "",
                        "metadata_at": s.get("metadata_at") or "", "objects": len(s.get("objects") or {})})
    return out


SIDE_RX = re.compile(r"^(?P<conn>[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}?)(?:@(?P<snap>\d{8}T\d{6}(?:-\d{1,3})?))?$")


def parse_side(ref):
    """"qa-main" -> (qa-main, None); "qa-main@20261107T101500" -> (qa-main, that snapshot). CompareError else."""
    m = SIDE_RX.match(str(ref or "").strip())
    if not m:
        raise CompareError("a side is a connection (qa-main) or one of its snapshots (qa-main@YYYYMMDDTHHMMSS)")
    return m.group("conn"), m.group("snap")


def blocked_side(ctx, conn):
    """The reason a side can't be compared, or "" when its connection is approved now. Nothing here widens a scope."""
    if conn in ctx.connections:
        return ""
    why = ctx.scope["blocked"].get(conn)
    return why or "not in the approved scope (mcp.database.scope)"


def load_side(ctx, vis, ref):
    conn, snap = parse_side(ref)
    if not snap:
        return side_from_graph(ctx, vis, conn)
    s = common.read_json(dbstore.snapshot_path(conn, snap), None)
    if not isinstance(s, dict):
        raise CompareError("no snapshot %s of %s (cli db snapshots %s)" % (snap, conn, conn))
    return dict(s, source="snapshot", snapshot=snap, taken_at=s.get("taken_at") or "")


def confirmed_pairs(vis, lconn, rconn):
    """{left object id: right object id} confirmed for comparing two connections: cross_environment_mapping claims
    (cli db map-confirm across environments) and pairs you confirmed with confirm_pair."""
    out = {}

    def add(a, b):
        ca, cb = vis.conn_of(a), vis.conn_of(b)
        if (ca, cb) == (lconn, rconn):
            out[a] = b
        elif (ca, cb) == (rconn, lconn):
            out[b] = a
    for c in vis.claims.values():
        if c.get("predicate") == "cross_environment_mapping":
            add(c.get("subject"), (c.get("value") or {}).get("to"))
    pairs = dbstore.read_file("comparisons").get("pairs")
    for p in (pairs.values() if isinstance(pairs, dict) else []):
        if isinstance(p, dict):
            add(p.get("left"), p.get("right"))
    return out


def _cmp_cols(lo, ro, le, re_):
    diffs = []
    for k in sorted(set(lo["columns"]) | set(ro["columns"])):
        a, b = lo["columns"].get(k), ro["columns"].get(k)
        name = (a or b)["name"]
        if a is None or b is None:
            diffs.append({"column": name, "field": "column", "left": "present" if a else "absent",
                          "right": "present" if b else "absent"})
            continue
        for field, va, vb in (("type", norm_type(le, a["type"]), norm_type(re_, b["type"])),
                              ("nullable", a["nullable"], b["nullable"]),
                              ("default", norm_default(a["default"]), norm_default(b["default"])),
                              ("primary key", a["pk"], b["pk"]), ("references", a["fk"], b["fk"])):
            if field == "nullable" and (va is None or vb is None):
                continue  # not declared on one side: unknown, not a difference
            if va != vb:
                diffs.append({"column": name, "field": field,
                              "left": a["type"] if field == "type" else va, "right": b["type"] if field == "type" else vb})
    return diffs


def _pair_item(key, lo, ro, L, R):
    it = {"key": key, "left": _brief(lo), "right": _brief(ro), "differences": [], "candidate": False,
          "code": {"left": lo.get("code", 0), "right": ro.get("code", 0)}}
    for side, o, s in (("left", lo, L), ("right", ro, R)):
        if not o["described"]:
            return dict(it, category="not_comparable", reason="%s is not described on %s yet" % (o["name"],
                                                                                                s["connection"]))
        if o["kind"] in ("procedure", "function", "trigger", "view") and o.get("definition") == "unavailable":
            return dict(it, category="not_comparable", reason="the definition of %s is unavailable on %s (no "
                                                              "permission to read it)" % (o["name"], s["connection"]))
    it["differences"] = _cmp_cols(lo, ro, L.get("engine"), R.get("engine"))
    if lo["kind"] != ro["kind"]:
        it["differences"].insert(0, {"column": "", "field": "kind", "left": lo["kind"], "right": ro["kind"]})
    if not lo["columns"] and not ro["columns"] and lo["kind"] not in ("table", "view", "collection"):
        return dict(it, category="not_comparable", reason="only routine names are recorded; their definitions are "
                                                          "not compared")
    return dict(it, category="changed" if it["differences"] else "unchanged",
                reason="%d difference(s)" % len(it["differences"]) if it["differences"] else
                "same columns, types, nullability, defaults and keys")


def _brief(o):
    return None if o is None else {"id": o.get("id"), "name": o.get("name"), "kind": o.get("kind"),
                                   "described": o.get("described"), "definition": o.get("definition") or "",
                                   "lifecycle": o.get("lifecycle")}


def _missing(side, key):
    """Why an object is absent from a side: (category-on-absence-is-known, reason)."""
    sch = key.rsplit(".", 1)[0] if "." in key else ""
    st = (side.get("schemas") or {}).get(sch)
    if st is None:
        return False, "schema %s is outside %s's approved scope" % (sch, side["connection"])
    if side.get("denied"):
        return False, "access was denied at the last check on part of %s" % side["connection"]
    if not st.get("complete"):
        return False, "%s's listing of %s is partial or was never taken" % (side["connection"], sch)
    return True, "not in %s's complete listing of %s (%s)" % (side["connection"], sch, str(st.get("listed_at"))[:10])


def _side_meta(s, ref):
    return {"ref": ref, "connection": s["connection"], "env": s.get("env") or "", "engine": s.get("engine") or "",
            "source": s.get("source"), "snapshot": s.get("snapshot"), "taken_at": s.get("taken_at") or "",
            "objects": len(s.get("objects") or {}), "denied": bool(s.get("denied")),
            "complete_schemas": sorted(k for k, v in (s.get("schemas") or {}).items() if v.get("complete"))}


def _one_sided(key, o, present, absent, L_is_present):
    known, why = _missing(absent, key)
    if o["kind"] not in ("table", "view", "collection"):
        known, why = False, "routines are not listed completely: absence on %s is unknown" % absent["connection"]
    if o.get("definition") == "unavailable":
        cat, why = "not_comparable", "the definition of %s is unavailable on %s (no permission to read it)" % (
            o["name"], present["connection"])
    else:
        cat = ("removed" if L_is_present else "added") if known else "unavailable"
    it = {"key": key, "left": _brief(o) if L_is_present else None, "right": None if L_is_present else _brief(o),
          "differences": [], "candidate": False, "category": cat, "reason": why,
          "code": {"left": o.get("code", 0) if L_is_present else 0, "right": 0 if L_is_present else o.get("code", 0)}}
    return it


def compare(ctx, left, right, objects=None, vis=None, cfg=None, save=True):
    """Metadata comparison of two sides (module doc). A blocked side returns {state: blocked, side, connection,
    reason} and nothing is computed or saved. Raises CompareError for a malformed side."""
    lconn, _ = parse_side(left)
    rconn, _ = parse_side(right)
    if str(left).strip() == str(right).strip():
        raise CompareError("compare two different connections or two different snapshots")
    for side, conn in (("left", lconn), ("right", rconn)):
        why = blocked_side(ctx, conn)
        if why:
            return {"id": compare_id(left, right, objects), "kind": "compare", "state": "blocked", "side": side,
                    "connection": conn, "reason": why, "published": False,
                    "text": "Comparison blocked: the %s side (%s) is not permitted: %s. Approving a connection or an "
                            "environment happens only in /cg:setup or the config file (mcp.database.scope, "
                            "qa_data.allowed_envs); nothing here widens or relabels them." % (side, conn, why)}
    vis = vis or dbflows.Visible(ctx, cfg=cfg)
    L, R = load_side(ctx, vis, left), load_side(ctx, vis, right)
    want = {str(o).strip().casefold() for o in objects or [] if str(o).strip()}
    same = lconn == rconn
    pairs = {} if same else confirmed_pairs(vis, lconn, rconn)
    rid = {o["id"]: k for k, o in R["objects"].items()}
    items, done_r = [], set()
    for key, lo in sorted(L["objects"].items()):
        if want and key not in want:
            continue
        if same and key in R["objects"]:
            ro, rk = R["objects"][key], key
        elif lo["id"] in pairs and pairs[lo["id"]] in rid:
            rk = rid[pairs[lo["id"]]]
            ro = R["objects"][rk]
        else:
            ro, rk = None, None
        if ro is not None:
            done_r.add(rk)
            it = _pair_item(key if rk == key else "%s -> %s" % (lo["name"], ro["name"]), lo, ro, L, R)
            it["mapping"] = "same object" if same else "confirmed"
            items.append(it)
        elif key in R["objects"]:
            done_r.add(key)
            items.append({"key": key, "left": _brief(lo), "right": _brief(R["objects"][key]), "differences": [],
                          "candidate": True, "category": "not_comparable", "mapping": "candidate",
                          "reason": "same name on both connections: a candidate mapping, not confirmed",
                          "confirm": "cli db compare %s %s --confirm %s" % (left, right, lo["name"]),
                          "code": {"left": lo.get("code", 0), "right": R["objects"][key].get("code", 0)}})
        else:
            items.append(_one_sided(key, lo, L, R, True))
    for key, ro in sorted(R["objects"].items()):
        if key in done_r or (want and key not in want):
            continue
        items.append(_one_sided(key, ro, R, L, False))
    for it in items:
        it["category_label"] = CATEGORY_LABEL[it["category"]]
    cats = {c: sum(1 for it in items if it["category"] == c) for c in CATEGORIES}
    times = "Snapshot times: %s %s (%s), %s %s (%s)." % (
        left, L.get("taken_at") or "unknown", L.get("source"), right, R.get("taken_at") or "unknown", R.get("source"))
    notes = [times, SCHEMA_ONLY, NO_ROWS, NOT_SYNC,
             "Code relations are counted from the selected repositories; the deployed revision is unknown."]
    if not same:
        notes.insert(1, "Objects pair only through a mapping you confirmed; the same name is a candidate.")
    rec = {"id": compare_id(left, right, objects), "kind": "compare", "schema_version": SCHEMA_VERSION, "state": "ok",
           "left": _side_meta(L, left), "right": _side_meta(R, right), "objects": sorted(want), "computed_at": _now(),
           "categories": cats, "items": items, "notes": notes, "connections": sorted({lconn, rconn}),
           "deps": deps_of(vis, [o["id"] for o in list(L["objects"].values()) + list(R["objects"].values())],
                           extra=[k for k in ("mappings",) if k in sources_now()]),
           "pairs_fp": dbmodel.fingerprint(dbstore.read_file("comparisons"))[:16],
           "policy_generation": ctx.policy_generation, "profile_generation": ctx.profile_generation,
           "scope": {c: scope_fp(ctx, c) for c in sorted({lconn, rconn})}, "published": False}
    if save:
        rec = publish_cached("compare", rec, ctx)
    return rec


def confirm_pair(ctx, left_conn, right_conn, left_name, right_name=None, vis=None):
    """Record that `left_name` on one connection and `right_name` (default: the same name) on the other are the same
    logical object for comparing. Each name must resolve to exactly one visible object; returns the pair record."""
    vis = vis or dbflows.Visible(ctx)
    ids = []
    for conn, name in ((left_conn, left_name), (right_conn, right_name or left_name)):
        if conn not in ctx.connections:
            raise CompareError("%s is not an approved connection" % conn)
        want = str(name or "").strip().casefold()
        hits = [nid for nid, n in vis.nodes.items() if n.get("kind") in dbmodel.DB_KINDS and
                (n.get("display") or {}).get("connection") == conn and _qn(n.get("display") or {}).casefold() == want]
        if len(hits) != 1:
            raise CompareError("%s names %d objects on %s (give schema.name)" % (name, len(hits), conn))
        ids.append(hits[0])
    rec = {"left": ids[0], "right": ids[1], "at": _now(), "principal": ctx.principal_id or ""}
    pid = dbmodel.new_id("x", "pair", sorted(ids))
    with dbstore.lock():
        cur = dbstore.read_file("comparisons")
        pairs = cur.get("pairs") if isinstance(cur.get("pairs"), dict) else {}
        pairs[pid] = rec
        dbstore.write_file("comparisons", {"schema_version": dbstore.SCHEMA_VERSION, "pairs": pairs})
    return dict(rec, id=pid)


# --------------------------------------------------------------------------- drift and reconciliation (DB-16)

def _src_label(key):
    if str(key).startswith("ticket#"):
        return "a recorded ticket flow"  # hashed: never the key, it may be someone else's ticket
    kind, _, rest = str(key).partition(":")
    if kind == "card":
        conn, _, table = rest.partition("/")
        return "metadata of %s (%s)" % (table, conn)
    if kind == "ticket":
        return "a recorded ticket flow"  # never the key: it may be someone else's ticket
    return {"linkmap": "the sampled link map", "mappings": "your confirmed mappings"}.get(kind, kind)


def flow_deps(ctx, vis, flow):
    """What a built flow depends on: {deps: {source: fingerprint}, scope: {connection: scope fingerprint}}."""
    st = flow.get("start") or {}
    extra = ["ticket:%s" % st["key"]] if st.get("kind") == "ticket" and st.get("key") else []
    srcs = sources_now()
    extra += [k for k in ("mappings", "linkmap") if k in srcs]
    nodes = [n.get("id") for n in flow.get("nodes") or []] + list(flow.get("objects") or [])
    return {"deps": deps_of(vis, [n for n in nodes if n], extra=extra),
            "scope": {c: scope_fp(ctx, c) for c in flow.get("connections") or []}}


def flow_drift(ctx, rec, srcs=None, rev=None):
    """{state: current | needs_review, label, reasons} of a saved flow, from the per-source fingerprints it recorded,
    its connections' scope and a reconciliation mark. Cheap: no rebuild, no graph load."""
    cur = (rec or {}).get("current") or {}
    reasons = []
    if cur.get("deps") is None:  # saved before dependencies were recorded: the coarse signal
        if cur.get("sources_rev") != (dbstore.sources_revision() if rev is None else rev):
            reasons.append("evidence was ingested after it was built")
    else:
        reasons += ["%s changed" % _src_label(k) for k in changed_deps(cur["deps"], srcs)]
    reasons += ["the approved scope of %s changed" % c for c, fp in sorted((cur.get("scope") or {}).items())
                if scope_fp(ctx, c) != fp and c in ctx.connections]
    rv = (rec or {}).get("review") or {}
    if rv.get("state") == "needs_review":
        reasons += [r for r in rv.get("reasons") or [] if r not in reasons]
    reasons = list(dict.fromkeys(reasons))[:10]
    return {"state": "needs_review" if reasons else "current", "label": REVIEW_LABEL if reasons else "",
            "reasons": reasons, "since": rv.get("since") or ""}


def _code_changes(ctx, raw, limit=CODE_FILES_MAX):
    """{evidence id: reason} for code citations whose file changed or vanished in a selected repo (bounded)."""
    files = {}
    for vid, ev in raw["evidence"].items():
        p = ev.get("provenance") or {}
        if ev.get("kind") == "source" and p.get("repo") and p.get("path") and p.get("content_hash"):
            files.setdefault((p["repo"], p["path"]), []).append((vid, p["content_hash"]))
    out, checked = {}, 0
    for (repo, rel), evs in sorted(files.items())[:limit]:
        root = ctx.repo_roots.get(repo)
        if not root:
            continue  # repository not selected now: unknown, nothing concluded
        checked += 1
        h = file_hash(os.path.join(root, *rel.split("/")))
        for vid, rh in evs:
            if h != rh:
                out[vid] = "cited code %s %s" % (rel, "no longer exists" if not h else "changed since it was recorded")
    return out, checked


def _flow_evidence(cur):
    return {v.get("id") for e in cur.get("edges") or [] for v in e.get("evidence") or [] if isinstance(v, dict)} | \
        {v.get("id") for s in cur.get("steps") or [] for v in s.get("evidence") or [] if isinstance(v, dict)}


def _mark(rec, reasons, now, gen):
    rv = rec.get("review") or {}
    if rv.get("state") == "needs_review" and rv.get("reasons") == reasons:
        return None
    return dict(rec, review={"state": "needs_review", "label": REVIEW_LABEL, "reasons": reasons,
                             "since": rv.get("since") or now, "generation": gen})


def _event(kind, subject, reasons, gen, now):
    eid = dbmodel.new_id("x", "drift", kind, subject, sorted(reasons), gen)  # stable: a re-run adds nothing
    return eid, {"at": now, "kind": kind, "subject": subject, "reasons": reasons[:5]}


def _source_states(prev, srcs):
    out = dict(prev or {})
    for k, v in srcs.items():
        if not isinstance(v, dict):
            continue
        st = dict(out.get(k) or {})
        at = str(v.get("at") or "")
        if at and at != st.get("last_attempt"):
            st["last_attempt"] = at
            if v.get("ok", True):
                st["last_success"] = at
            else:
                st["failure"] = {"at": at, "why": str(v.get("why") or "")[:200]}
        out[k] = st
    return out


def reconcile(ctx=None, cfg=None, budget=RECONCILE_BUDGET, now=None, before_publish=None):
    """One bounded reconciliation pass (module doc): no query, no model. Returns a summary; `state` superseded means
    the access policy changed while it ran and nothing was published. `before_publish` (tests) runs just before the
    publish step."""
    cfg = common.load_config() if cfg is None else cfg
    ctx = ctx or dbaccess.AccessContext(cfg)
    gen0, now = ctx.policy_generation, now or _now()
    vis = dbflows.Visible(ctx, cfg=cfg)
    srcs = sources_now()
    prev = dbstore.read_file("reconcile")
    code, checked = _code_changes(ctx, vis.raw)
    cp = dbstore.load_checkpoint("reconcile")
    after = cp.get("cursor") if not cp.get("done") else None
    fids = dbstore.list_ids("flows", limit=budget, after=after)
    flows, marked, events = {}, {}, {}
    for fid in fids:
        rec = dbstore.load("flows", fid)
        if not rec or not rec.get("current"):
            continue
        cur = rec["current"]
        dr = flow_drift(ctx, dict(rec, review=None), srcs)
        reasons = list(dr["reasons"])
        reasons += sorted({code[v] for v in _flow_evidence(cur) if v in code})
        hist = [o for o in cur.get("objects") or [] if (vis.raw["nodes"].get(o) or {}).get("lifecycle") == "historical"]
        reasons += ["%s is no longer listed (kept as history)" % vis.label(o) for o in hist]
        reasons = list(dict.fromkeys(reasons))[:10]
        if reasons:
            marked[fid] = reasons
            new = _mark(rec, reasons, now, gen0)
            if new is not None:
                flows[fid] = new
                k, v = _event("flow_needs_review", fid, reasons, gen0, now)
                events[k] = v
        elif (rec.get("review") or {}).get("state") == "needs_review":
            marked[fid] = (rec.get("review") or {}).get("reasons") or []
    invs = {}
    for inv in dbstore.iter_records("investigations", limit=1000):  # only those of a flow marked now
        fid = (inv.get("flow") or {}).get("id")
        if fid not in marked:
            continue
        new = _mark(inv, ["its flow requires revalidation: %s" % r for r in marked[fid]][:10], now, gen0)
        if new is not None:
            invs[inv["id"]] = new
            k, v = _event("investigation_needs_review", inv["id"], new["review"]["reasons"], gen0, now)
            events[k] = v
    drop = []
    for kind in ("impact", "compare"):
        for r in dbstore.iter_records(kind, limit=1000):
            if any(c not in ctx.connections for c in r.get("connections") or []):
                drop.append((kind, r["id"]))
    done = len(fids) < budget
    summary = {"state": "published", "at": now, "flows_checked": len(fids), "flows_marked": len(flows),
               "investigations_marked": len(invs), "caches_removed": len(drop), "code_files_checked": checked,
               "code_changed": len(code), "events": len(events), "more_available": not done,
               "policy_generation": gen0}
    if before_publish is not None:
        before_publish()
    with dbstore.lock():
        if generation_now() != gen0:  # an older scope: nothing it computed may publish now
            return dict(summary, state="superseded", flows_marked=0, investigations_marked=0, caches_removed=0,
                        events=0)
        for kind, recs in (("flows", flows), ("investigations", invs)):
            for rid, rec in recs.items():
                try:
                    dbstore.save_locked(kind, rec)
                except dbstore.Conflict:
                    summary["more_available"] = True  # changed meanwhile: the next pass sees the newer record
        for kind, rid in drop:
            dbstore.delete_record(kind, rid)
        evs = dict(prev.get("events") or {})
        evs.update({k: v for k, v in events.items() if k not in evs})
        keep = sorted(evs.items(), key=lambda kv: (kv[1].get("at") or "", kv[0]))[-EVENTS_KEEP:]
        dbstore.write_file("reconcile", {
            "schema_version": dbstore.SCHEMA_VERSION,
            "generations": {"schema_version": dbstore.SCHEMA_VERSION, "policy": gen0, "profile": ctx.profile_generation,
                            "scope": {c: scope_fp(ctx, c) for c in sorted(ctx.connections)},
                            "mappings": (srcs.get("mappings") or {}).get("fingerprint") or "",
                            "sources": dbstore.sources_revision()},
            "sources": _source_states(prev.get("sources"), srcs), "events": dict(keep), "last_run": summary})
        common.write_json(dbstore.checkpoint_path("reconcile"), {
            "source": "reconcile", "cursor": None if done else (fids[-1] if fids else None), "seen": [],
            "done": done, "at": now})
    return summary


def tick(cfg, state=None):
    """The background step (after ingestion): one bounded reconciliation pass. Fail-open (the caller logs)."""
    if not dbaccess.approved_scope(cfg)["connections"]:
        return None
    s = reconcile(cfg=cfg)
    if isinstance(state, dict):
        state["db_reconcile"] = {k: s.get(k) for k in ("at", "state", "flows_marked", "investigations_marked",
                                                         "caches_removed", "more_available")}
    return s


def _narrowed(r, env=None, connection=None):
    """resolve_start's answer narrowed to one environment and / or connection(s) the caller named explicitly (never a
    guess): (state, candidates). Without a narrowing argument the answer is unchanged."""
    conns = {str(c).casefold() for c in ([connection] if isinstance(connection, str) else connection or []) if c}
    if not env and not conns:
        return r["state"], r["candidates"]
    cands = [c for c in r["candidates"]
             if (not env or str(c.get("environment") or "").casefold() == str(env).casefold()) and
             (not conns or str(c.get("connection") or "").casefold() in conns)]
    return ("resolved" if len(cands) == 1 else "ambiguous" if cands else "none"), cands


def resolve_target(ctx, vis, text, env=None, connection=None):
    """An impact target from an explicit ID or name: ("resolved", id, []) | ("ambiguous", None, candidates) |
    ("none", None, reason). "schema.table" names an object, "schema.table.column" (optionally "connection:...") a
    column; `env` / `connection` (given explicitly: --env, --connection) narrow a name several objects share. Several
    matches are never narrowed down for you."""
    t = str(text or "").strip()
    if dbmodel.valid_id(t):
        if t[0] in "oc" and t in vis.nodes:
            return "resolved", t, []
        return "none", None, "no such object or column"
    where = " in %s" % " / ".join(x for x in (str(env or "").upper(), ", ".join(
        [connection] if isinstance(connection, str) else connection or [])) if x) if env or connection else ""
    r = dbflows.resolve_start(ctx, "table", t, vis=vis, limit=200)
    st, cands = _narrowed(r, env, connection)
    if st == "resolved":
        return "resolved", cands[0]["id"], []
    if st == "ambiguous" and "." in t.split(":")[-1]:
        return "ambiguous", None, cands[:20]
    head, _, col = t.rpartition(".")
    matched = r["state"] != "none"  # something matched before the explicit narrowing
    if head and col:
        r2 = dbflows.resolve_start(ctx, "table", head, vis=vis, limit=200)
        matched = matched or r2["state"] != "none"
        st2, cands2 = _narrowed(r2, env, connection)
        if st2 == "resolved":
            oid = cands2[0]["id"]
            hits = [m["id"] for m in vis.members.get(oid) or []
                    if str((m.get("display") or {}).get("member") or "").casefold() == col.casefold()]
            if len(hits) == 1:
                return "resolved", hits[0], []
            return "none", None, "%s has no recorded column %s" % (vis.label(oid), col[:60])
        if st2 == "ambiguous":
            return "ambiguous", None, cands2[:20]
    if st == "ambiguous":
        return "ambiguous", None, cands[:20]
    if where and matched:
        return "none", None, "nothing recorded matches %r%s" % (t[:60], where)
    return "none", None, r.get("reason") or "nothing recorded matches %r" % t[:60]
