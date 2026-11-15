"""Passports, bounded discovery and explainable business flows over the canonical graph (database flow D1, plan §3;
DB-06, DB-07, DB-08, DB-09). Everything here reads the graph through one access projection (Visible: dbaccess.decide
on every record); only discover_metadata talks to the database MCP, and only through a Transport, for metadata.

Visible(ctx)        the records the principal may see now. A record from a ticket (context.tickets, a session-record
                    evidence's provenance.ticket) shows that ticket only when it is inside the principal's personal
                    scope (api.scope_of); a technical claim with a validated code citation stays usable without it.
passport(ctx, id)   identity and environment, purpose (cited, else "Unconfirmed meaning"), columns (type, nullability,
                    default, keys, personal data, enum meanings only when evidenced), declared keys, relationships by
                    layer, readers / writers (the writing service is not an accountable owner: owner unknown unless
                    evidenced), routines and triggers, related saved flows, and per-source state (description,
                    constraints, writers: state, coverage, freshness, last success / attempt, error). Writers are
                    "incomplete" when a source failed, is stale or unsupported, or a routine's definition is hidden:
                    never "No writers". Static analysis may miss dynamic SQL. No query.
expand(ctx, seeds)  breadth-first over visible edges with a visited set (cycles end), depth <= 3, <= 100 nodes, pages of
                    50, `more_available` + a continuation cursor; structure and flow edges stay apart.
discover_metadata   the explicit / scheduled metadata job: <= 10 objects described per batch through a Transport
                    (describe, triggers and writer-routine catalog reads; never row data), cards written in
                    mcpjobs.card_record's format, a checkpoint per seed set (an interrupted run resumes without
                    describing an object twice), capability observations recorded (dbconnector.observe_call), a
                    permission-denied answer quarantines the object. A partial inventory page never removes an object;
                    only a complete listing of a schema marks one missing (lifecycle historical, history kept).
resolve_start       ticket, API operation, code symbol / file, table or free text -> candidates; one exact match
                    resolves, several stay candidates (never the first match).
build_flow          a bounded subgraph -> steps (entry, service, read, decision, write, transaction, publish, consume,
                    downstream_effect, unknown_boundary) with path, async flag and an order only where evidence orders
                    it (call -> handler, publish -> consume, recorded step numbers; cycles and unordered steps stay
                    unordered), edges, column lineage per transforms edge (source columns, transform or
                    "transformation unknown", transport, destination columns, evidence) and gaps, each with the next
                    useful evidence source. Same-named columns are never joined; an edge between environments is never
                    a flow edge. Label "Code model; deployed revision unknown" (no deployment evidence in D1).
                    save=True keeps a versioned record (data/db/flows/<f_id>.json): a new version only on a material
                    change, earlier versions kept, your notes untouched by regeneration.
explain_flow        an optional model explanation through the constrained transport (extract.call_claude style,
                    injectable); its output must cite only the flow's own step and evidence IDs, else the
                    deterministic explanation is used (also when the call fails or the budget is spent).
"""
import json

from . import common
from . import dbaccess
from . import dbmodel
from . import dbstore

DEPTH, MAX_NODES, PAGE, BATCH = 3, 100, 50, 10
FLOW_LABEL = "Code model; deployed revision unknown"
DISCLAIMER = "Static analysis may miss dynamic SQL."
OWNER_UNKNOWN = "Accountable owner unknown: the service that writes it is not its owner, and no owner is recorded."
STEP_KINDS = ("entry", "service", "read", "decision", "write", "transaction", "publish", "consume", "downstream_effect",
              "unknown_boundary")
STEP_LABEL = {"entry": "Entry point", "service": "Service", "read": "Read", "decision": "Decision", "write": "Write",
              "transaction": "Transaction", "publish": "Publish", "consume": "Consume",
              "downstream_effect": "Downstream effect", "unknown_boundary": "Unknown boundary"}
PATH_LABEL = {"success": "Success path", "cancel": "Cancellation path", "retry": "Retry path", "error": "Error path"}
NEXT = {
    "deployed_revision_unknown": "Record which revision is deployed in this environment (release or deployment "
                                 "evidence) for the services involved.",
    "dynamic_sql": "Look for string-built SQL or query builders in these services, or compare with a captured query "
                   "log.",
    "runtime_events_unavailable": "Check the consumer's processing log or the event broker's delivery records: the "
                                  "database MCP has no event or audit source.",
    "mapping_unresolved": "Confirm the mapping from the code or a definition (cli db map-confirm --from ... --to ...).",
    "definition_unavailable": "Ask for permission to read the routine's definition, or find its source in a "
                              "repository.",
    "permission_denied": "Access was denied at the last check; it is re-checked at the next metadata refresh.",
    "unknown_boundary": "Pick the object explicitly (cli db resolve <name>) or record where this step leads.",
}
KIND_RANK = {k: i for i, k in enumerate(STEP_KINDS)}


class FlowError(ValueError):
    """A start that can't be used (unknown, outside your scope, ambiguous): nothing is built or saved."""


class NotFound(LookupError):
    """No visible record with that ID (unknown, or not visible to you: indistinguishable on purpose)."""


# --------------------------------------------------------------------------- the visible graph

class Visible:
    """The graph records the principal may see now (module doc). Built once per request from the stored graph."""

    def __init__(self, ctx, graph=None, cfg=None):
        self.ctx = ctx
        g = graph if graph is not None else dbstore.load_graph()
        self.raw = g
        self._tickets = {}
        self.cfg = cfg
        nodes, self.inferred_only = {}, set()
        for k, n in g["nodes"].items():
            d = dbaccess.decide(ctx, n, g)
            if d["allowed"]:
                nodes[k] = n
                if d.get("inferred_only"):
                    self.inferred_only.add(k)
        self.nodes = {k: n for k, n in nodes.items() if not n.get("parent") or n["parent"] in nodes}
        self.edges = {k: e for k, e in g["edges"].items() if e.get("source") in self.nodes and
                      e.get("target") in self.nodes and dbaccess.decide(ctx, e, g)["allowed"]}
        self.hidden_edges = {k: e for k, e in g["edges"].items() if k not in self.edges}
        self.evidence = {k: v for k, v in g["evidence"].items() if self._ev_ok(v)}
        self.claims = {k: c for k, c in g["claims"].items() if self._claim_ok(c)}
        self.adj = {}
        for e in self.edges.values():
            self.adj.setdefault(e["source"], []).append(e["id"])
            self.adj.setdefault(e["target"], []).append(e["id"])
        self.members = {}
        for n in self.nodes.values():
            if n.get("parent"):
                self.members.setdefault(n["parent"], []).append(n)

    def repo_name(self, repo_id):
        """A selected repository's folder name (never its full path); "" for an unknown one."""
        root = self.ctx.repo_roots.get(repo_id) or ""
        return root.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if root else ""

    def ticket_ok(self, key):
        if key not in self._tickets:
            try:
                from .dashboard import api
                self._tickets[key] = bool(api.scope_of(key, self.cfg).get("visible"))
            except Exception:  # noqa: BLE001 - unknown scope: not shown
                self._tickets[key] = False
        return self._tickets[key]

    def tickets(self, rec):
        """The tickets a record names that you may see (others are left out without a count)."""
        return [t for t in ((rec or {}).get("context") or {}).get("tickets") or [] if t and self.ticket_ok(t)]

    def _ev_ok(self, ev):
        prov = ev.get("provenance") or {}
        if prov.get("ticket") and not self.ticket_ok(prov["ticket"]):
            return False
        if dbaccess.decide(self.ctx, ev, self.raw)["allowed"]:
            return True
        sc = ev.get("scope") or {}
        if sc.get("connection") or sc.get("repo"):
            return False  # its own scope is outside: no inheritance
        subs = [s for s in ev.get("subjects") or [] if s in self.raw["nodes"] or s in self.raw["edges"]]
        if not subs:
            return False
        if prov.get("ticket"):  # your own ticket's record: shown unless a subject is on a connection you can't see
            return not any(dbaccess._connections_of(self.raw["nodes"].get(s) or self.raw["edges"].get(s) or {}, self.raw)
                           - set(self.ctx.connections) for s in subs)
        return all(s in self.nodes or s in self.edges for s in subs)

    def _claim_ok(self, c):
        subj = c.get("subject")
        if (subj in self.nodes or subj in self.edges) and dbaccess.decide(self.ctx, c, self.raw)["allowed"]:
            return True
        anchors = c.get("anchors") or {}
        if any(x not in self.ctx.connections for x in anchors.get("connections") or []):
            return False
        return bool(self.tickets(c))  # personal context: shown to whoever's ticket it is

    # ------------------------------------------------------------------ labels
    def label(self, nid, with_conn=True):
        n = self.nodes.get(nid) or self.raw["nodes"].get(nid) or {}
        d = n.get("display") or {}
        k = n.get("kind")
        if k in dbmodel.DB_KINDS:
            s = "%s.%s" % (d.get("schema"), d.get("name")) if d.get("schema") else str(d.get("name") or "")
            return s + (" (%s)" % d.get("connection") if with_conn and d.get("connection") else "")
        if k in dbmodel.MEMBER_KINDS:
            parent = self.nodes.get(n.get("parent")) or self.raw["nodes"].get(n.get("parent")) or {}
            pd = parent.get("display") or {}
            base = "%s.%s" % (pd.get("schema"), pd.get("name")) if pd.get("schema") else str(pd.get("name") or "")
            return "%s.%s" % (base, d.get("member"))
        return str(d.get("name") or nid)

    def env_of(self, nid):
        n = self.nodes.get(nid) or {}
        if n.get("kind") in dbmodel.MEMBER_KINDS:
            n = self.nodes.get(n.get("parent")) or {}
        return (n.get("display") or {}).get("environment") if n.get("kind") in dbmodel.DB_KINDS else None

    def conn_of(self, nid):
        n = self.nodes.get(nid) or {}
        if n.get("kind") in dbmodel.MEMBER_KINDS:
            n = self.nodes.get(n.get("parent")) or {}
        return (n.get("display") or {}).get("connection") if n.get("kind") in dbmodel.DB_KINDS else None

    def evidence_view(self, vid):
        ev = self.evidence.get(vid)
        if not ev:
            return None
        p, q = ev.get("provenance") or {}, ev.get("quality") or {}
        out = {"id": vid, "kind": ev.get("kind"), "basis": ev.get("basis"),
               "basis_label": dbmodel.BASIS_LABEL.get(ev.get("basis"), ""), "validation": q.get("validation"),
               "coverage": q.get("coverage"), "freshness": q.get("freshness"), "as_of": q.get("as_of") or "",
               "lifecycle": ev.get("lifecycle")}
        if ev.get("kind") == "source" and p.get("type", "code") == "code":
            out.update(cite="%s:%s" % (p.get("path"), p.get("line_start") if p.get("line_start") == p.get("line_end")
                                       else "%s-%s" % (p.get("line_start"), p.get("line_end"))),
                       path=p.get("path"), line_start=p.get("line_start"), line_end=p.get("line_end"),
                       repo=self.repo_name(p.get("repo")),
                       revision=p.get("revision"), content_hash=str(p.get("content_hash") or "")[:12])
            out["text"] = "%s in %s (%s)" % (out["cite"], out["repo"] or "a selected repository", out["revision"])
        elif ev.get("kind") == "catalog":
            out["text"] = "Catalog %s of %s on %s%s" % (p.get("source"), p.get("object"), p.get("connection"),
                                                        ", read " + str(q.get("as_of"))[:10] if q.get("as_of") else "")
        elif ev.get("kind") == "observation":
            out["text"] = p.get("label") or "Observed in a sample"
        elif ev.get("kind") == "user_note":
            out["text"] = "You confirmed this mapping%s" % (": " + p["note"] if p.get("note") else "")
        elif ev.get("kind") == "model_proposal":
            out["text"] = "Proposed by the ticket's consolidated DB-flow summary (a model proposal)"
        else:
            out["text"] = "Session record%s: %s (%s)" % (" of " + p["ticket"] if p.get("ticket") else "",
                                                         p.get("text") or "", p.get("reason") or "not checked")
        return out


def _basis(vis, rec):
    b = rec.get("basis") or "inferred"
    if rec.get("source") in vis.inferred_only or rec.get("target") in vis.inferred_only:
        return "inferred"
    return b


# --------------------------------------------------------------------------- passport (DB-06)

def _source_view(st, label):
    st = st or {}
    state = st.get("state") or "not_checked"
    txt = {"ok": "Read", "failed": "Last refresh failed: earlier data kept, marked stale",
           "unsupported": "Not available for this engine", "not_checked": "Not checked yet"}.get(state, state)
    return {"what": label, "state": state, "text": txt, "coverage": st.get("coverage") or "not_checked",
            "coverage_label": dbmodel.COVERAGE_LABEL.get(st.get("coverage") or "not_checked", ""),
            "freshness": st.get("freshness") or "unknown",
            "freshness_label": dbmodel.FRESHNESS_LABEL.get(st.get("freshness") or "unknown", ""),
            "last_success": st.get("last_success") or "", "last_attempt": st.get("last_attempt") or "",
            "error": st.get("error") or "", "truncated": bool(st.get("truncated")), "page": st.get("page") or ""}


def _edge_summary(vis, e, focus=None):
    other = e["target"] if e["source"] == focus else e["source"]
    return {"id": e["id"], "layer": e.get("layer"), "kind": e.get("kind"), "label": dbmodel.EDGE_LABEL.get(e.get("kind")),
            "source": e["source"], "target": e["target"], "source_label": vis.label(e["source"]),
            "target_label": vis.label(e["target"]), "other": other, "other_label": vis.label(other),
            "basis": _basis(vis, e), "basis_label": dbmodel.BASIS_LABEL.get(_basis(vis, e)),
            "lifecycle": e.get("lifecycle"), "transport": e.get("transport"),
            "columns": [{"from": [vis.label(c) for c in m.get("from") or []], "to": [vis.label(c) for c in m.get("to") or []],
                         "transform": m.get("transform") or ("" if m.get("transform_known") else "transformation unknown")}
                        for m in e.get("column_mapping") or []],
            "evidence": [x for x in (vis.evidence_view(v) for v in e.get("evidence") or []) if x]}


def passport(ctx, object_id, vis=None, cfg=None):
    """The passport of one visible DB object (module doc). Raises NotFound."""
    vis = vis or Visible(ctx, cfg=cfg)
    n = vis.nodes.get(object_id) if dbmodel.valid_id(object_id, "o") else None
    if not n or n.get("kind") not in dbmodel.DB_KINDS:
        raise NotFound("no such object")
    d, a = n.get("display") or {}, n.get("attrs") or {}
    srcs = a.get("sources") or {}
    cols = []
    claims_by = {}
    for c in vis.claims.values():
        claims_by.setdefault(c.get("subject"), []).append(c)
    for c in sorted(vis.members.get(object_id, []), key=lambda m: ((m.get("attrs") or {}).get("position") or 10 ** 6,
                                                                   str((m.get("display") or {}).get("member")))):
        ca = c.get("attrs") or {}
        enum = [cl for cl in claims_by.get(c["id"], []) if cl.get("predicate") == "enum_values" and
                cl.get("basis") != "inferred" and cl.get("evidence")]
        cols.append({"id": c["id"], "name": (c.get("display") or {}).get("member"), "type": ca.get("data_type") or "",
                     "nullable": ca.get("nullable"), "default": ca.get("default"),
                     "primary_key": bool(ca.get("primary_key")), "foreign_key": bool(ca.get("foreign_key")),
                     "sensitive": bool(ca.get("sensitive")), "described": not ca.get("placeholder"),
                     "basis": "declared_metadata" if not ca.get("placeholder") else "inferred",
                     "enum": [{"values": cl.get("value"), "basis": cl.get("basis"),
                               "evidence": cl.get("evidence")} for cl in enum]})
    edges = [vis.edges[k] for k in vis.adj.get(object_id, [])]
    for m in vis.members.get(object_id, []):
        edges += [vis.edges[k] for k in vis.adj.get(m["id"], []) if vis.edges[k] not in edges]
    rel = {"structure": [], "flow": []}
    readers, writers, routines, triggers = [], [], [], []
    for e in sorted(edges, key=lambda e: (e.get("layer"), e.get("kind"), e["id"])):
        s = _edge_summary(vis, e, object_id)
        rel.setdefault(e.get("layer") or "flow", []).append(s)
        other = vis.nodes.get(s["other"]) or {}
        who = {"id": other.get("id"), "kind": other.get("kind"), "name": vis.label(s["other"]),
               "service": vis.repo_name((other.get("identity") or {}).get("repo")),
               "basis": s["basis"], "basis_label": s["basis_label"], "lifecycle": e.get("lifecycle"),
               "ops": e.get("ops") or [], "columns": [vis.label(c) for c in (e.get("columns") or {}).get(
                   "written" if e.get("kind") == "writes" else "read") or []],
               "owner": "unknown", "evidence": s["evidence"]}
        if e.get("kind") == "writes" and e["target"] == object_id:
            writers.append(who)
        elif e.get("kind") == "reads" and e["target"] == object_id:
            readers.append(who)
        if other.get("kind") in ("procedure", "function"):
            routines.append(dict(who, definition=(other.get("attrs") or {}).get("definition") or "available"))
        if other.get("kind") == "trigger":
            triggers.append(dict(who, event=(other.get("attrs") or {}).get("event") or ""))
    hidden_defs = []
    for cl in claims_by.get(object_id, []):
        if cl.get("predicate") == "routine_unreadable":
            rn = cl.get("value")
            hidden_defs.append(vis.label(rn) if rn in vis.nodes else "a routine")
            routines.append({"id": rn if rn in vis.nodes else None, "kind": "procedure",
                             "name": vis.label(rn) if rn in vis.nodes else "a routine", "definition": "unavailable",
                             "basis": cl.get("basis"), "basis_label": dbmodel.BASIS_LABEL.get(cl.get("basis")),
                             "owner": "unknown", "evidence": [], "ops": [], "columns": []})
    wst = srcs.get("writers") or {}
    reasons = []
    if (wst.get("state") or "not_checked") != "ok":
        reasons.append({"failed": "the last writers refresh failed (%s)" % (wst.get("error") or "no result"),
                        "unsupported": "writer routines can't be read for this engine",
                        "not_checked": "writer routines were not checked yet"}.get(wst.get("state") or "not_checked",
                                                                                    "writers source %s" % wst.get("state")))
    elif wst.get("freshness") == "stale":
        reasons.append("the writers source is stale (last read %s)" % str(wst.get("last_success"))[:10])
    if wst.get("truncated"):
        reasons.append("the writers query hit its row limit")
    if hidden_defs:
        reasons.append("definition unavailable: %s" % ", ".join(hidden_defs))
    if reasons:
        wtext = "Writers incomplete: " + "; ".join(reasons) + ". " + DISCLAIMER
        wstate = "incomplete"
    elif writers:
        wtext = "%d known writer(s) from the catalog and recorded code. %s" % (len(writers), DISCLAIMER)
        wstate = "known"
    else:
        wtext = "No writer found in the catalog query or recorded code. " + DISCLAIMER
        wstate = "none_found"
    purpose = [c for c in claims_by.get(object_id, []) if c.get("predicate") == "purpose"]
    cited = [c for c in purpose if c.get("basis") != "inferred" and c.get("evidence")]
    pk = [c["name"] for c in cols if c["primary_key"]]
    fks = [s for s in rel["structure"] if s["kind"] == "foreign_key"]
    return {
        "id": object_id, "kind": n.get("kind"), "kind_label": dbmodel.KIND_LABEL.get(n.get("kind")),
        "name": vis.label(object_id, with_conn=False), "display": {k: d.get(k) for k in (
            "connection", "environment", "database", "schema", "name", "engine")},
        "environment": d.get("environment") or "", "lifecycle": n.get("lifecycle"),
        "missing_since": a.get("missing_since") or "",
        "described": bool(a.get("described")), "placeholder": bool(a.get("placeholder")),
        "purpose": ({"text": cited[0].get("value"), "basis": cited[0].get("basis"), "evidence": cited[0].get("evidence"),
                     "label": dbmodel.BASIS_LABEL.get(cited[0].get("basis"))} if cited else
                    {"text": purpose[0].get("value") if purpose else "", "basis": "inferred",
                     "label": "Unconfirmed meaning", "evidence": []}),
        "columns": cols, "keys": {"primary": pk, "foreign": fks},
        "relationships": rel, "readers": readers, "writers": writers, "writers_state": wstate,
        "writers_text": wtext, "owner": {"state": "unknown", "text": OWNER_UNKNOWN},
        "routines": routines, "triggers": triggers, "flows": related_flows(ctx, object_id, vis),
        "sources": {"description": _source_view(srcs.get("describe"), "Description (columns)"),
                    "constraints": _source_view(srcs.get("describe"), "Constraints (keys)"),
                    "writers": _source_view(srcs.get("writers"), "Writers (routines and triggers)"),
                    "triggers": _source_view(srcs.get("triggers"), "Triggers")},
        "disclaimer": DISCLAIMER, "authorization": dbaccess.authorization(ctx, d.get("connection")) if
        d.get("connection") else None,
    }


def related_flows(ctx, object_id, vis=None, limit=20):
    out = []
    for rec in dbstore.iter_records("flows", limit=1000):
        cur = rec.get("current") or {}
        if object_id in (cur.get("objects") or []) and _flow_visible(vis, rec):
            out.append({"id": rec["id"], "title": cur.get("title"), "version": cur.get("version")})
        if len(out) >= limit:
            break
    return out


def _flow_visible(vis, rec):
    st = rec.get("start") or {}
    if st.get("kind") == "ticket" and vis is not None and not vis.ticket_ok(st.get("key")):
        return False
    return True


# --------------------------------------------------------------------------- expansion (DB-07)

def _bfs(vis, start, depth, max_nodes, want, exclude=()):
    """Breadth-first from `start` ([id] or [[id, depth]] to continue) over visible edges of the wanted layers:
    (order, depth by node, cut, frontier). The visited set ends cycles; `exclude` (nodes already returned) are walked
    through but not returned again; `cut` says max_nodes stopped it, `frontier` ([[id, depth]]) is where to go on."""
    order, dist, queue = [], {}, []
    exclude = set(exclude)
    for s in start:
        sid, d0 = (s[0], int(s[1])) if isinstance(s, (list, tuple)) else (s, 0)
        if sid in dist:
            continue
        dist[sid] = d0
        queue.append(sid)
        if sid not in exclude:
            order.append(sid)
    cut, i, frontier = False, 0, []
    while i < len(queue):
        nid = queue[i]
        i += 1
        if dist[nid] >= depth:
            continue
        for eid in sorted(vis.adj.get(nid, [])):
            e = vis.edges[eid]
            if e.get("layer") not in want:
                continue
            nb = e["target"] if e["source"] == nid else e["source"]
            if nb in dist or nb in exclude:
                continue  # visited: a cycle ends here
            if len(order) >= max_nodes:
                cut = True
                break
            dist[nb] = dist[nid] + 1
            order.append(nb)
            queue.append(nb)
        if cut:  # the node being expanded and everything still queued: where a continuation starts
            frontier = [[n, dist[n]] for n in queue[i - 1:] if dist[n] < depth]
            break
    return order, dist, cut, frontier


def expand(ctx, seeds, depth=DEPTH, max_nodes=MAX_NODES, page=PAGE, cursor=None, vis=None, layers=None):
    """Bounded breadth-first neighbourhood (module doc) -> {nodes, edges: {structure, flow}, more_available, cursor,
    depth, limits}. `cursor` is the dict a previous call returned (dbviews signs it for callers)."""
    vis = vis or Visible(ctx)
    depth = max(1, min(int(depth), DEPTH))
    max_nodes = max(1, min(int(max_nodes), MAX_NODES))
    page = max(1, min(int(page), PAGE))
    cur = cursor if isinstance(cursor, dict) else {}
    exclude = set(x for x in cur.get("exclude") or [] if isinstance(x, str))
    start = [s for s in (cur.get("frontier") or cur.get("seeds") or seeds or [])
             if (s[0] if isinstance(s, list) else s) in vis.nodes]
    offset = max(0, int(cur.get("offset") or 0))
    want = set(layers or ("structure", "flow"))
    order, dist, cut, frontier = _bfs(vis, start, depth, max_nodes, want, exclude)
    chunk = order[offset:offset + page]
    seen = set(order[:offset + page])
    nxt = None
    if offset + page < len(order):
        nxt = {"seeds" if not cur.get("frontier") else "frontier": start, "exclude": sorted(exclude),
               "offset": offset + page}
    elif cut:
        nxt = {"frontier": frontier, "exclude": sorted(exclude | set(order)), "offset": 0}
    es = {"structure": [], "flow": []}
    for eid in sorted({x for n in chunk for x in vis.adj.get(n, [])}):
        e = vis.edges[eid]
        if e.get("layer") in want and e["source"] in seen and e["target"] in seen:
            es.setdefault(e["layer"], []).append(_edge_summary(vis, e))
    return {"nodes": [{"id": n, "kind": vis.nodes[n].get("kind"), "label": vis.label(n), "depth": dist[n],
                       "lifecycle": vis.nodes[n].get("lifecycle")} for n in chunk],
            "edges": es, "more_available": nxt is not None, "cursor": nxt, "visited": len(order),
            "depth": max(dist.values()) if dist else 0,
            "limits": {"depth": depth, "max_nodes": max_nodes, "page": page}}


# --------------------------------------------------------------------------- discovery (DB-07)

def _target(ctx, vis, s):
    """(connection, schema, name) of a seed, or (None, reason)."""
    if isinstance(s, str) and dbmodel.valid_id(s, "o"):
        n = (vis.nodes if vis else {}).get(s)
        if not n:
            return None, "unknown object"
        d = n.get("display") or {}
        return (d.get("connection"), d.get("schema"), d.get("name")), ""
    if isinstance(s, str) and ":" in s and "." in s:
        conn, rest = s.split(":", 1)
        sch, _, name = rest.rpartition(".")
        s = {"connection": conn, "schema": sch, "name": name}
    if isinstance(s, dict):
        c, sch, name = s.get("connection"), s.get("schema"), s.get("name")
        if c not in ctx.connections:
            return None, "connection not approved"
        if not sch or not name:
            return None, "name the schema and the table"
        return (c, sch, name), ""
    return None, "not an object"


def discover_metadata(ctx, seeds, transport, cfg, health=None, batch=BATCH, inventory=None, now=None):
    """The bounded metadata job (module doc). Returns {described, failed, skipped, remaining, more_available,
    checkpoint, inventory, candidates, calls}."""
    from . import dbconnector, dbingest, mcpjobs
    now = now or common.now_iso()
    batch = max(1, min(int(batch), BATCH))
    vis = Visible(ctx, cfg=cfg)
    targets, skipped = [], []
    for s in seeds or []:
        t, why = _target(ctx, vis, s)
        if t is None:
            skipped.append({"seed": s if isinstance(s, str) else json.dumps(s)[:120], "why": why})
            continue
        ent = ctx.connections.get(t[0])
        if not ent or not ent.get("schemas") or dbmodel.norm_ident(ent.get("engine"), t[1]) not in \
                {dbmodel.norm_ident(ent.get("engine"), x) for x in ent["schemas"]}:
            skipped.append({"seed": "%s:%s.%s" % t, "why": "schema outside the approved scope"})
            continue
        if t not in targets:
            targets.append(t)
    keys = ["%s:%s.%s" % t for t in targets]
    src = "discover-" + dbmodel.fingerprint(sorted(keys))[:16]
    cp = dbstore.load_checkpoint(src)
    seen = [] if cp.get("done") else list(cp.get("seen") or [])
    todo = [t for t, k in zip(targets, keys) if k not in seen][:batch]
    caps = dbconnector.load_capabilities() or dbconnector.assess(cfg)
    calls, plan = [], {}
    by_schema = {}
    for t in todo:
        by_schema.setdefault((t[0], t[1]), []).append(t)
    n = 0
    for (conn, sch), ts in sorted(by_schema.items()):
        ad = dbconnector.adapter_for(ctx.connections[conn].get("engine"))
        tid = None
        if ad is not None and all(dbconnector_plain(x) for x in [sch] + [t[2] for t in ts]):
            n += 1
            tid = "t%d" % n
            calls.append({"call_id": tid, "role": "query", "connection": conn, "limit": 20,
                          "sql": ad.catalog("triggers", sch, [t[2] for t in ts])})
        for t in ts:
            n += 1
            did, wid = "d%d" % n, None
            calls.append({"call_id": did, "role": "describe_table", "connection": conn, "schema": sch, "table": t[2]})
            if ad is not None and dbconnector_plain(sch) and dbconnector_plain(t[2]):
                wid = "w%d" % n
                calls.append({"call_id": wid, "role": "query", "connection": conn, "limit": 20,
                              "sql": ad.catalog("writers", sch, [t[2]])})
            plan[t] = (did, tid, wid)
    for (conn, sch) in inventory or []:
        if conn in ctx.connections:
            n += 1
            calls.append({"call_id": "l%d" % n, "role": "list_tables", "connection": conn, "schema": sch})
    res = {r["call_id"]: r for r in (transport.execute("database-discovery", calls, cfg, health or {}) if calls else [])}
    out = {"described": [], "failed": [], "skipped": skipped, "candidates": [], "inventory": [], "calls": len(calls),
           "checkpoint": src}
    touched = []
    outcome = {"ok": "ok", "permission_denied": "permission_denied", "error": "error", "missing": "missing"}
    for t, (did, tid, wid) in plan.items():
        conn, sch, name = t
        ent = ctx.connections[conn]
        d, tr, wr = res.get(did), res.get(tid) if tid else None, res.get(wid) if wid else None
        oid = dbmodel.object_id(mcpjobs.server_name(cfg, "database"), conn, ent["env"],
                                ent["databases"][0] if len(ent.get("databases") or []) == 1 else "", sch, "table",
                                name, ent.get("engine") or "", quoted=True, case_sensitive=ent.get("case_sensitive"))
        for capname, r in (("tables", d), ("triggers", tr), ("procedures", wr)):
            if r is not None:
                dbconnector.observe_call(caps, capname, outcome.get(r["status"], "error"), connection=conn, at=now,
                                         reason=(r.get("error") or "")[:120])
        if d is not None:
            dbconnector.observe_call(caps, "describe", outcome.get(d["status"], "error"), at=now)
        if d is not None and d["status"] == "permission_denied":
            dbaccess.quarantine(conn, oid, "describe denied", at=now)
            out["failed"].append({"object": "%s:%s.%s" % t, "why": "permission denied"})
        else:
            if d is not None and d["status"] == "ok" and "%s|%s" % (conn, oid) in dbaccess.quarantine_entries():
                dbaccess.reconcile(conn, oid)  # a successful read lifts an earlier denial
            table = "%s.%s" % (sch, name)
            p = mcpjobs.card_path(conn, table)
            old = common.read_json(p, {}) or {}
            desc = d["rows"] if d is not None and d["status"] == "ok" and isinstance(d.get("rows"), list) and d["rows"] \
                else mcpjobs.NO_RESULT
            rec = mcpjobs.card_record(table, conn, ent["env"], ent.get("engine") or "", old, desc,
                                      tr["rows"] if tr is not None and tr["status"] == "ok" else None,
                                      wr["rows"] if wr is not None and wr["status"] == "ok" else None, now,
                                      "" if dbconnector.adapter_for(ent.get("engine")) else
                                      "not supported for %s" % (ent.get("engine") or "this engine"), mark_hidden=True)
            if old.get("missing_since"):
                rec["missing_since"] = old["missing_since"]
            mcpjobs.save_card(p, rec)
            touched.append("card:%s/%s" % (mcpjobs._safe(conn), mcpjobs._safe(table)))
            (out["described"] if desc is not mcpjobs.NO_RESULT else out["failed"]).append(
                {"object": "%s:%s" % (conn, table), "why": "" if desc is not mcpjobs.NO_RESULT else
                 (d or {}).get("error") or "no describe result"})
        seen.append("%s:%s.%s" % t)
    for c in calls:
        if c["role"] != "list_tables":
            continue
        r = res.get(c["call_id"]) or {}
        conn, sch = c["connection"], c["schema"]
        if r.get("status") != "ok":
            dbconnector.observe_call(caps, "object_list", outcome.get(r.get("status"), "error"), at=now)
            out["inventory"].append({"connection": conn, "schema": sch, "complete": False,
                                     "why": r.get("error") or "no result"})
            continue
        dbconnector.observe_call(caps, "object_list", "ok", at=now)
        rows = [x for x in r.get("rows") or [] if isinstance(x, dict)]
        partial = any(x.get("more_available") for x in rows)
        names = sorted({str(x.get("name")).rsplit(".", 1)[-1] for x in rows if x.get("name")})
        known = {}
        for nd in vis.nodes.values():
            dd = nd.get("display") or {}
            if nd.get("kind") in ("table", "view") and dd.get("connection") == conn and \
                    str(dd.get("schema")).casefold() == str(sch).casefold():
                known[str(dd.get("name"))] = nd
        out["candidates"] += ["%s:%s.%s" % (conn, sch, x) for x in names if x not in known]
        cpi = "inventory-%s" % dbmodel.fingerprint([conn, sch])[:16]
        dbstore.save_checkpoint(cpi, (rows[-1] or {}).get("next") if partial else None, names, done=not partial)
        missing = []
        if not partial:  # only a complete listing of the schema may mark a known object missing
            for nm, nd in known.items():
                p = mcpjobs.card_path(conn, "%s.%s" % ((nd.get("display") or {}).get("schema"), nm))
                card = common.read_json(p, None)
                if not isinstance(card, dict):
                    continue
                if nm not in names and not card.get("missing_since"):
                    card["missing_since"] = now
                elif nm in names and card.get("missing_since"):
                    card.pop("missing_since")
                else:
                    continue
                mcpjobs.save_card(p, card)
                if card.get("missing_since"):
                    missing.append(nm)
                touched.append("card:%s/%s" % (mcpjobs._safe(conn), mcpjobs._safe(card.get("table") or "")))
        out["inventory"].append({"connection": conn, "schema": sch, "complete": not partial, "listed": len(names),
                                 "missing": missing})
    remaining = len([k for k in keys if k not in seen])
    dbstore.save_checkpoint(src, len(seen), seen, done=remaining == 0)
    dbconnector.save_capabilities(caps)
    if touched:
        dbingest.run(cfg, only=set(touched), force=True)
        from . import dbimpact  # a metadata snapshot per connection read (DB-14 comparisons of two snapshots)
        conns = sorted({t.split(":", 1)[1].split("/", 1)[0] for t in touched})
        out["snapshots"] = [s for s in (dbimpact.take_snapshot(ctx, c) for c in conns if c in ctx.connections) if s]
    out.update(remaining=remaining, more_available=remaining > 0)
    return out


REFRESH_MAX = 20


def queue_refresh(ctx, seeds, cfg=None):
    """Queue a bounded metadata discovery for explicit seeds (o_ IDs or "conn:schema.table", at most BATCH), each
    checked against the approved scope now. The agent's `db-discover` runs the queue (describe and catalog reads only,
    never rows). Returns {queued, skipped}."""
    vis = Visible(ctx, cfg=cfg)
    ok, skipped = [], []
    for s in list(seeds or [])[:BATCH]:
        t, why = _target(ctx, vis, s)
        if t is None:
            skipped.append({"seed": str(s)[:120], "why": why})
        elif "%s:%s.%s" % t not in ok:
            ok.append("%s:%s.%s" % t)
    if ok:
        with dbstore.lock():
            reqs = list(dbstore.read_file("refresh").get("requests") or [])
            if any(sorted(r.get("seeds") or []) == sorted(ok) for r in reqs):
                return {"queued": ok, "skipped": skipped, "already_queued": True}  # a repeated request: one job
            reqs.append({"seeds": ok, "at": common.now_iso()})
            dbstore.write_file("refresh", {"schema_version": dbstore.SCHEMA_VERSION, "requests": reqs[-REFRESH_MAX:]})
    return {"queued": ok, "skipped": skipped}


def run_refresh(transport, cfg, health=None):
    """Run the queued metadata discovery requests (the agent's `db-discover`), each still checked against today's
    scope by discover_metadata. Returns their results."""
    with dbstore.lock():
        reqs = list(dbstore.read_file("refresh").get("requests") or [])
        dbstore.write_file("refresh", {"schema_version": dbstore.SCHEMA_VERSION, "requests": []})
    ctx = dbaccess.AccessContext(cfg)
    return [discover_metadata(ctx, r.get("seeds") or [], transport, cfg, health) for r in reqs]


def dbconnector_plain(x):
    """A name the catalog statements take as written (letters, digits, underscore): others are never stripped to fit."""
    import re
    return isinstance(x, str) and bool(re.fullmatch(r"[A-Za-z0-9_]{1,128}", x))


# --------------------------------------------------------------------------- starts (DB-08)

START_KINDS = ("ticket", "api", "symbol", "table", "text")


def _cand(vis, nid, kind=None):
    n = vis.nodes[nid]
    d = n.get("display") or {}
    return {"kind": kind or n.get("kind"), "id": nid, "label": vis.label(nid),
            "detail": " · ".join(x for x in (dbmodel.KIND_LABEL.get(n.get("kind")), d.get("environment") and
                                            str(d.get("environment")).upper()) if x),
            "environment": str(d.get("environment") or ""), "connection": str(d.get("connection") or "")}


def resolve_start(ctx, kind, text, vis=None, cfg=None, limit=20):
    """-> {state: resolved | ambiguous | none, start, candidates, more}. Never picks the first of several."""
    from .dashboard import api
    vis = vis or Visible(ctx, cfg=cfg)
    text = " ".join(str(text or "").split())[:200]
    kind = kind if kind in START_KINDS else None
    if not text:
        return {"state": "none", "start": None, "candidates": [], "more": 0, "reason": "type a ticket, operation, "
                                                                                       "symbol, file or table"}
    if kind in (None, "ticket") and api.TICKET_RE.match(text.upper()):
        key = text.upper()
        if not vis.ticket_ok(key):
            return {"state": "none", "start": None, "candidates": [], "more": 0,
                    "reason": "%s is not a ticket in your personal scope" % key}
        steps = sum(1 for e in vis.edges.values() if key in ((e.get("context") or {}).get("tickets") or []))
        c = {"kind": "ticket", "key": key, "id": None, "label": key, "detail": "%d recorded flow edge(s)" % steps}
        return {"state": "resolved", "start": {"kind": "ticket", "key": key}, "candidates": [c], "more": 0}
    if kind == "ticket":
        return {"state": "none", "start": None, "candidates": [], "more": 0, "reason": "not a ticket key"}
    low = text.casefold()
    want = {"api": ("api_operation",), "symbol": ("code_symbol", "service", "job"), "table": ("table", "view"),
            None: ("api_operation", "code_symbol", "service", "event", "job", "table", "view", "procedure"),
            "text": ("api_operation", "code_symbol", "service", "event", "job", "table", "view", "procedure")}[kind]
    exact, part = [], []
    for nid, n in vis.nodes.items():
        if n.get("kind") not in want:
            continue
        names = {vis.label(nid, False).casefold(), str((n.get("display") or {}).get("name") or "").casefold(),
                 vis.label(nid).casefold()}
        d = n.get("display") or {}
        if d.get("connection"):
            names.add(("%s:%s.%s" % (d.get("connection"), d.get("schema"), d.get("name"))).casefold())
        if low in names:
            exact.append(nid)
        elif all(tok in " ".join(names) for tok in low.split()):
            part.append(nid)
    if kind == "symbol" and not exact and ("/" in text or "." in text):  # a file: code nodes its citations name
        for e in vis.edges.values():
            for v in e.get("evidence") or []:
                p = ((vis.evidence.get(v) or {}).get("provenance") or {}).get("path") or ""
                if p and (p.casefold() == low or p.casefold().endswith("/" + low)) and \
                        vis.nodes.get(e["source"], {}).get("kind") == "code_symbol" and e["source"] not in part:
                    part.append(e["source"])
    if not exact:  # a legacy name the migration mapped to exactly one object (an explicit alias)
        al = (dbstore.read_file("aliases").get("aliases") or {}).get(low)
        if isinstance(al, dict) and al.get("object") in vis.nodes and vis.nodes[al["object"]].get("kind") in want:
            exact = [al["object"]]
    # an object a complete listing no longer shows (historical) leaves current lookups while a current one matches;
    # opened by its ID it still shows, with its history
    exact = [h for h in exact if vis.nodes[h].get("lifecycle") != "historical"] or exact
    part = [h for h in part if vis.nodes[h].get("lifecycle") != "historical"] or part
    hits = sorted(exact) or sorted(part, key=lambda x: vis.label(x))
    cands = [_cand(vis, h) for h in hits[:limit]]
    state = "resolved" if len(hits) == 1 else "ambiguous" if hits else "none"
    return {"state": state, "start": {"kind": "node", "id": hits[0]} if state == "resolved" else None,
            "candidates": cands, "more": max(0, len(hits) - limit),
            "reason": "" if hits else "nothing recorded matches %r" % text[:60]}


# --------------------------------------------------------------------------- the flow (DB-08, DB-09)

def flow_id(start, env=None):
    return dbmodel.new_id("f", "flow", start.get("kind"), start.get("key") or start.get("id") or "", env or "")


def _step_id(kind, ref):
    return dbmodel.new_id("x", "step", kind, ref)


def build_flow(ctx, start, env=None, repos=None, vis=None, save=False, cfg=None):
    """The flow for a start {kind: ticket, key} | {kind: node, id} (module doc). Raises FlowError."""
    vis = vis or Visible(ctx, cfg=cfg)
    if not isinstance(start, dict) or start.get("kind") not in ("ticket", "node"):
        raise FlowError("start with a ticket or an object / operation ID")
    env = str(env).lower() if env else None
    if env and env not in {c["env"] for c in ctx.connections.values()}:
        raise FlowError("environment %s has no approved connection" % env)
    title, claims, hidden = "", [], 0
    if start["kind"] == "ticket":
        key = str(start.get("key") or "").upper()
        if not vis.ticket_ok(key):
            raise FlowError("%s is not a ticket in your personal scope" % key)
        start = {"kind": "ticket", "key": key}
        title = key
        sel = {k: e for k, e in vis.edges.items() if key in ((e.get("context") or {}).get("tickets") or [])
               and e.get("layer") == "flow"}
        hidden = sum(1 for e in vis.hidden_edges.values() if key in ((e.get("context") or {}).get("tickets") or []))
        claims = [c for c in vis.claims.values() if key in ((c.get("context") or {}).get("tickets") or [])]
    else:
        nid = start.get("id")
        if not dbmodel.valid_id(nid) or nid not in vis.nodes:
            raise FlowError("no such object or operation")
        start = {"kind": "node", "id": nid}
        title = vis.label(nid)
        reach = set(_bfs(vis, [nid], DEPTH, MAX_NODES, {"flow"})[0])
        sel = {k: e for k, e in vis.edges.items() if e.get("layer") == "flow" and e["source"] in reach and
               e["target"] in reach}
        claims = [c for c in vis.claims.values() if c.get("subject") in reach]
    edges = {}
    for k, e in sel.items():
        envs = {x for x in (vis.env_of(e["source"]), vis.env_of(e["target"])) if x}
        if len(envs) > 1:
            continue  # an edge between environments is a comparison, never a flow edge
        if env and envs and env not in envs:
            continue
        if repos and (e.get("applicability") or {}).get("repo") and e["applicability"]["repo"] not in repos:
            continue
        edges[k] = e
    return _assemble(ctx, vis, start, env, title, edges, claims, hidden, save, cfg)


def _order_of(e, key=None):
    return [o for o in e.get("orders") or [] if isinstance(o, dict) and isinstance(o.get("step"), int)
            and (key is None or o.get("ticket") == key)]


def _assemble(ctx, vis, start, env, title, edges, claims, hidden, save, cfg):
    key = start.get("key")
    steps, after = {}, set()

    def ev_list(e):
        return [x for x in (vis.evidence_view(v) for v in (e or {}).get("evidence") or []) if x]

    def add(kind, ref, label, edge=None, nodes=(), basis=None, evidence=None, path=None, async_=False, extra=None):
        sid = _step_id(kind, ref)
        if sid in steps:
            return sid
        e = edge or {}
        b = basis or (_basis(vis, e) if e else "inferred")
        scen = next((o.get("scenario") for o in _order_of(e, key) if o.get("scenario")), "") if e else ""
        steps[sid] = dict({"id": sid, "kind": kind, "kind_label": STEP_LABEL[kind], "label": label,
                           "scenario": scen, "edge": e.get("id"), "nodes": list(nodes), "basis": b,
                           "basis_label": dbmodel.BASIS_LABEL.get(b, ""),
                           "path": path or e.get("path") or "success",
                           "path_known": bool(path or e.get("path")), "async": bool(async_ or e.get("async")),
                           "evidence": evidence if evidence is not None else ev_list(e), "lineage": [],
                           "gaps": []}, **(extra or {}))
        return sid
    consumers = {e["source"] for e in edges.values() if e.get("kind") == "consumes"}
    by_comp = {}
    for e in sorted(edges.values(), key=lambda e: e["id"]):
        by_comp.setdefault(e["source"], []).append(e)
    svc = {}
    for comp, es in by_comp.items():
        node = vis.nodes.get(comp) or {}
        if node.get("kind") == "api_operation":
            continue
        if any(x.get("kind") in ("reads", "writes", "publishes", "consumes", "calls") for x in es):
            calls_in = [x for x in edges.values() if x.get("kind") == "calls" and x["target"] == comp]
            src = calls_in[0] if calls_in else es[0]
            svc[comp] = add("service", comp, "%s handles it" % vis.label(comp), nodes=[comp], basis=_basis(vis, src),
                            evidence=ev_list(src), path=src.get("path") or None,
                            extra={"scenario": next((o.get("scenario") for x in es for o in _order_of(x, key)
                                                     if o.get("scenario")), "")})
    cons, entry = [], {}
    tx = {}
    for e in sorted(edges.values(), key=lambda e: e["id"]):
        k, s, t = e.get("kind"), e["source"], e["target"]
        sl, tl = vis.label(s), vis.label(t)
        if k == "calls" and (vis.nodes.get(s) or {}).get("kind") == "api_operation":
            sid = add("entry", e["id"], "%s calls %s" % (sl, tl), e, [s, t])
            entry[e["id"]] = sid
            if t in svc:
                cons.append((sid, svc[t], "the call reaches its handler"))
        elif k == "calls":
            sid = add("service", e["id"], "%s calls %s" % (sl, tl), e, [s, t])
            if s in svc:
                cons.append((svc[s], sid, "the handler makes the call"))
        elif k == "reads":
            sid = add("read", e["id"], "%s reads %s" % (sl, tl), e, [s, t],
                      extra={"columns": [vis.label(c) for c in (e.get("columns") or {}).get("read") or []]})
            if s in svc:
                cons.append((svc[s], sid, "the handler reads it"))
        elif k == "writes":
            down = s in consumers
            ops = ", ".join(e.get("ops") or []) or "writes"
            sid = add("downstream_effect" if down else "write", e["id"], "%s %s %s" % (sl, ops if ops != "writes" else
                                                                                       "writes", tl), e, [s, t],
                      extra={"columns": [vis.label(c) for c in (e.get("columns") or {}).get("written") or []],
                             "changes": [{"column": vis.label(ch.get("column")), "change": ch.get("change"),
                                          "condition": ch.get("condition")} for ch in e.get("changes") or []]})
            if s in svc:
                cons.append((svc[s], sid, "the handler writes it"))
            conds = [c for c in e.get("condition") or [] if isinstance(c, str) and c]
            if conds:
                did = add("decision", "dec:" + e["id"], "when " + "; ".join(conds), e, [s])
                cons.append((did, sid, "the condition guards the write"))
                if s in svc:
                    cons.append((svc[s], did, "the handler checks it"))
            if e.get("transaction"):
                tx.setdefault((s, e["transaction"]), []).append(sid)
        elif k == "publishes":
            sid = add("publish", e["id"], "%s publishes %s" % (sl, tl), e, [s, t], async_=True)
            if s in svc:
                cons.append((svc[s], sid, "the handler publishes it"))
        elif k == "consumes":
            sid = add("consume", e["id"], "%s consumed by %s" % (tl, sl), e, [t, s], async_=True)
            if s in svc:
                cons.append((sid, svc[s], "the consumer handles the event"))
    for (comp, name), sids in tx.items():
        if len(sids) > 1:
            tid = add("transaction", "tx:%s:%s" % (comp, name), "%s writes %d objects in one transaction (%s)" % (
                vis.label(comp), len(sids), name), basis=steps[sids[0]]["basis"], nodes=[comp],
                evidence=steps[sids[0]]["evidence"])
            for x in sids:
                cons.append((tid, x, "inside the transaction"))
            if comp in svc:
                cons.append((svc[comp], tid, "the handler opens it"))
            steps[tid]["scenario"] = steps[sids[0]].get("scenario") or ""
    # publish -> consume of the same event; recorded step numbers within one ticket's scenario
    pubs = {}
    for e in edges.values():
        if e.get("kind") == "publishes":
            pubs.setdefault(e["target"], []).append(_step_id("publish", e["id"]))
    for e in edges.values():
        if e.get("kind") == "consumes":
            for p in pubs.get(e["target"], []):
                cons.append((p, _step_id("consume", e["id"]), "an event is published before it is consumed"))
    numbered = {}
    for e in edges.values():
        kind = {"reads": "read", "writes": "downstream_effect" if e["source"] in consumers else "write",
                "publishes": "publish", "consumes": "consume"}.get(e.get("kind"))
        if e.get("kind") == "calls":
            kind = "entry" if (vis.nodes.get(e["source"]) or {}).get("kind") == "api_operation" else "service"
        if not kind:
            continue
        for o in _order_of(e, key if start.get("kind") == "ticket" else None):
            numbered.setdefault((o.get("ticket"), o.get("scenario")), []).append((o["step"], _step_id(kind, e["id"])))
    same_tx = {x: k for k, sids in tx.items() if len(sids) > 1 for x in sids}
    for (tk, scen), lst in numbered.items():
        lst = sorted(set(lst))
        for (a, sa) in lst:
            for (b, sb) in lst:
                # writes of one transaction commit together: one stage, whatever order the session listed them in
                if a < b and sa != sb and not (sa in same_tx and same_tx.get(sa) == same_tx.get(sb)):
                    cons.append((sa, sb, "recorded step order"))
    # lineage per transforms edge
    lineage = []
    for e in sorted(edges.values(), key=lambda e: e["id"]):
        if e.get("kind") not in ("transforms", "copies"):
            continue
        for m in e.get("column_mapping") or []:
            item = {"edge": e["id"], "from": [vis.label(c) for c in m.get("from") or []],
                    "to": [vis.label(c) for c in m.get("to") or []],
                    "transform": m.get("transform") if m.get("transform_known") else
                    (m.get("transform") or "") + (" " if m.get("transform") else "") + "(transformation unknown)",
                    "transform_known": bool(m.get("transform_known")),
                    "transport": dbmodel.TRANSPORT_LABEL.get(e.get("transport"), ""), "basis": _basis(vis, e),
                    "basis_label": dbmodel.BASIS_LABEL.get(_basis(vis, e)), "evidence": ev_list(e)}
            lineage.append(item)
            # where the values go: the publish of the event, else the write into the destination, else the consume
            dst_event = (vis.nodes.get(e["target"]) or {}).get("kind") == "event"
            hit = [s for s in steps.values() if s["kind"] == "publish" and e["target"] in s["nodes"]] if dst_event \
                else [s for s in steps.values() if s["kind"] in ("downstream_effect", "write") and
                      s["nodes"][-1:] == [e["target"]]]
            if not hit and (vis.nodes.get(e["source"]) or {}).get("kind") == "event":
                hit = [s for s in steps.values() if s["kind"] == "consume" and e["source"] in s["nodes"]]
            for s in hit:
                s["lineage"].append(item)
    # gaps
    gaps = []

    def gap(kind, text, refs=(), nxt=None):
        gid = dbmodel.new_id("x", "gap", kind, text)
        if any(g["id"] == gid for g in gaps):
            return
        gaps.append({"id": gid, "kind": kind, "label": dbmodel.GAP_LABEL[kind], "text": text,
                     "next": nxt or NEXT[kind], "steps": [r for r in refs if r in steps]})
        for r in refs:
            if r in steps and gid not in steps[r]["gaps"]:
                steps[r]["gaps"].append(gid)
    envs = sorted({vis.env_of(n) for s in steps.values() for n in s["nodes"] if vis.env_of(n)})
    envlab = "/".join(x.upper() for x in envs) or (env or "").upper() or "the environment"
    gap("deployed_revision_unknown", "The steps come from source code as read (worktree), not from what is deployed "
                                     "in %s." % envlab)
    code_steps = [s["id"] for s in steps.values() if s["kind"] in ("read", "write", "downstream_effect")]
    if code_steps:
        gap("dynamic_sql", "Reads and writes built as strings at run time are not listed.", code_steps)
    for s in steps.values():
        if s["kind"] == "consume":
            gap("runtime_events_unavailable", "Runtime events are unavailable: whether %s ran for a given record is "
                                              "not recorded." % s["label"], [s["id"]])
    for it in lineage:
        if not it["transform_known"]:
            refs = [s["id"] for s in steps.values() if it in s["lineage"]]
            gap("mapping_unresolved", "%s -> %s: transformation unknown." % (", ".join(it["from"]),
                                                                            ", ".join(it["to"])), refs)
    objs = {n for s in steps.values() for n in s["nodes"] if (vis.nodes.get(n) or {}).get("kind") in dbmodel.DB_KINDS}
    for c in claims:
        p, v = c.get("predicate"), c.get("value") or {}
        if p == "mapping_unresolved":
            gap("mapping_unresolved", "Mapping %s -> %s unresolved: %s." % (
                ", ".join(v.get("from") or []), ", ".join(v.get("to") or []), v.get("reason") or "unknown"),
                [svc.get(c.get("subject"))])
        elif p == "unresolved_table":
            cands = [vis.label(x) for x in v.get("candidates") or [] if x in vis.nodes]
            cands += [x.split(":", 1)[1] for x in v.get("candidates") or [] if str(x).startswith("connection:")]
            text = "%s %s: %s" % (v.get("operation") or "uses", v.get("ref"), (
                "could be %s" % ", ".join(cands) if cands else v.get("reason") or "no approved connection holds it"))
            sid = add("unknown_boundary", c["id"], text, nodes=[c.get("subject")] if c.get("subject") in vis.nodes else [],
                      evidence=[x for x in (vis.evidence_view(v) for v in c.get("evidence") or []) if x])
            if c.get("subject") in svc:
                cons.append((svc[c["subject"]], sid, "the handler uses it"))
            gap("unknown_boundary", "The object %s is not resolved: %s." % (v.get("ref"), (
                "candidates " + ", ".join(cands)) if cands else (v.get("reason") or "no candidate")), [sid],
                "Pick the object explicitly: cli db resolve %s --kind table" % v.get("ref"))
    for c in vis.claims.values():
        if c.get("predicate") == "routine_unreadable" and c.get("subject") in objs:
            refs = [s["id"] for s in steps.values() if c["subject"] in s["nodes"]]
            gap("definition_unavailable", "Definition of %s unavailable: writers of %s are incomplete." % (
                vis.label(c.get("value")) if c.get("value") in vis.nodes else "a routine", vis.label(c["subject"])), refs)
    quarantined = dbaccess.quarantine_entries()
    for conn in sorted({vis.conn_of(n) for n in objs if vis.conn_of(n)}):
        if any(isinstance(q, dict) and q.get("connection") == conn for q in quarantined.values()):
            gap("permission_denied", "Access to part of %s was denied at the last check; those objects are hidden."
                % conn)
    if hidden:
        gap("unknown_boundary", "%d recorded step(s) lead outside what you may see now (an unapproved or revoked "
                                "connection, or access denied)." % hidden)
    for comp in consumers:
        if not any(e.get("kind") == "writes" and e["source"] == comp for e in edges.values()):
            sid = add("unknown_boundary", "after:" + comp, "What %s does after consuming is not recorded" %
                      vis.label(comp), nodes=[comp])
            if comp in svc:
                cons.append((svc[comp], sid, "after the consumer runs"))
            gap("unknown_boundary", "No recorded effect of %s after it consumes the event." % vis.label(comp), [sid],
                "Record what the consumer writes or calls (map the DB flow for its code).")
    _rank(steps, cons)
    _groups(steps, cons)
    ordered = sorted(steps.values(), key=lambda s: (s["group"], s["order"] is None,
                                                    s["order"] if s["order"] is not None else 0,
                                                    KIND_RANK[s["kind"]], s["label"]))
    for i, s in enumerate(ordered):
        s["position"] = i + 1  # a stable list index only: never shown as a sequence (the stage is)
    conns = sorted({vis.conn_of(n) for n in objs if vis.conn_of(n)})
    checked = sum(1 for s in ordered if any(v.get("validation") == "validated" and v.get("kind") in
                                            ("source", "catalog", "observation", "user_note") for v in s["evidence"]))
    unknown = sum(1 for g in gaps if g["kind"] == "unknown_boundary")
    nodes = sorted({n for s in ordered for n in s["nodes"]} | {e["source"] for e in edges.values()} |
                   {e["target"] for e in edges.values()})
    flow = {
        "kind": "flow", "start": dict(start, label=title), "env": env or "", "title": title, "label": FLOW_LABEL,
        "environment": envlab if envs else (env or "").upper(), "connections": conns,
        "steps": ordered, "edges": [_edge_summary(vis, e) for e in sorted(edges.values(), key=lambda e: e["id"])],
        "nodes": [{"id": n, "kind": (vis.nodes.get(n) or {}).get("kind"), "label": vis.label(n),
                   "connection": vis.conn_of(n) or "", "environment": vis.env_of(n) or ""} for n in nodes
                  if n in vis.nodes],
        "objects": sorted(objs), "lineage": lineage, "gaps": gaps,
        "scope_line": "%d connection%s · %d step%s checked · %d boundar%s unknown" % (
            len(conns), "" if len(conns) == 1 else "s", checked, "" if checked == 1 else "s", unknown,
            "y" if unknown == 1 else "ies"),
        "coverage": {"steps": len(ordered), "checked": checked, "inferred": sum(1 for s in ordered if s["basis"] ==
                                                                                 "inferred"),
                     "gaps": len(gaps), "state": "partial"},
        "freshness": _freshness(vis, edges),
    }
    flow["fingerprint"] = dbmodel.fingerprint({k: flow[k] for k in ("steps", "edges", "gaps", "lineage", "start",
                                                                     "env")})
    flow["id"] = flow_id(start, env)
    flow["built_at"] = common.now_iso()
    flow["sources_rev"] = dbstore.sources_revision()  # what ingestion had taken in: a later change makes it stale
    from . import dbimpact
    flow.update(dbimpact.flow_deps(ctx, vis, flow))  # the sources and scopes it depends on (DB-16: cheap staleness)
    if save:
        return save_flow(flow, ctx)
    return flow


def _freshness(vis, edges):
    fr = [(vis.evidence.get(v) or {}).get("quality", {}).get("freshness") for e in edges.values()
          for v in e.get("evidence") or [] if (vis.evidence.get(v) or {}).get("kind") in ("catalog", "observation")]
    if "stale" in fr:
        return "stale"
    return "current" if fr else "unknown"


def _rank(steps, cons):
    """Order only where a constraint says so: longest path over the constraint DAG; steps in a cycle or without any
    constraint stay unordered."""
    out = {s: set() for s in steps}
    inn = {s: set() for s in steps}
    why = {}
    for a, b, reason in cons:
        if a in steps and b in steps and a != b:
            out[a].add(b)
            inn[b].add(a)
            why.setdefault(b, reason)
    indeg = {s: len(inn[s]) for s in steps}
    queue = sorted(s for s in steps if indeg[s] == 0)
    rank, done = {s: 0 for s in steps}, set()
    while queue:
        s = queue.pop(0)
        done.add(s)
        for t in sorted(out[s]):
            rank[t] = max(rank[t], rank[s] + 1)
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    for s, st in steps.items():
        linked = bool(out[s] or inn[s])
        if s not in done:
            st.update(order=None, ordered=False, order_basis="in a cycle: no order")
        elif not linked:
            st.update(order=None, ordered=False, order_basis="no evidence orders this step")
        else:
            st.update(order=rank[s], ordered=True, order_basis=why.get(s, "starts the flow"))


GROUP_MAIN, GROUP_SEPARATE, GROUP_UNORDERED = "main", "separate", "unordered"


def _groups(steps, cons):
    """Numbering only by evidence (DB-08: no invented sequence). The constraint graph's connected parts are groups:
    the part holding the entry point (else the largest) is the main path, every other part a separate path, and the
    steps no evidence orders form one unordered group. A step's `stage` is its rank within its group + 1: steps that
    share a rank share the number (same stage, no order between them, `stage_shared`), a separate path restarts at 1,
    and a step in a cycle or without constraint has no stage."""
    parent = {s: s for s in steps}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b, _ in cons:
        if a in steps and b in steps and a != b:
            parent[find(a)] = find(b)
    parts = {}
    for s in sorted(steps):
        parts.setdefault(find(s), []).append(s)
    linked = [p for p in parts.values() if len(p) > 1]
    alone = [s for p in parts.values() if len(p) == 1 for s in p]

    def first(p):
        return min((steps[s]["order"] if steps[s]["order"] is not None else 1 << 30, KIND_RANK[steps[s]["kind"]],
                    steps[s]["label"], s) for s in p)
    main = [p for p in linked if any(steps[s]["kind"] == "entry" for s in p)]
    main = main[0] if main else (sorted(linked, key=lambda p: (-len(p), first(p)))[0] if linked else None)
    others = sorted((p for p in linked if p is not main), key=first)
    groups = ([(GROUP_MAIN, "Main path", main)] if main else []) + \
        [(GROUP_SEPARATE, "Separate path: %s" % steps[first(p)[3]]["label"], p) for p in others] + \
        ([(GROUP_UNORDERED, "Not ordered by evidence", alone)] if alone else [])
    for gi, (kind, label, members) in enumerate(groups):
        ranks = [steps[s]["order"] for s in members if steps[s]["order"] is not None]
        low = min(ranks) if ranks else 0
        for s in members:
            st = steps[s]
            stage = st["order"] - low + 1 if st["order"] is not None and kind != GROUP_UNORDERED else None
            st.update(group=gi, group_kind=kind, group_label=label, stage=stage,
                      stage_shared=stage is not None and sum(1 for x in members if steps[x]["order"] == st["order"])
                      > 1)


def stage_text(s):
    """How a step's place reads: "3" / "3 (same stage, no order between them)" / "unordered: <why>"."""
    if s.get("stage") is None:
        return "unordered: %s" % (s.get("order_basis") or "no evidence orders this step")
    return "%d%s" % (s["stage"], " (same stage, no order between them)" if s.get("stage_shared") else "")


def save_flow(flow, ctx=None):
    """Keep the flow as a versioned record: a new version only when its fingerprint changed (earlier versions kept,
    at most 20), your notes never touched. A concurrent save is retried once on the newer record."""
    fid = flow["id"]
    for _ in range(2):
        rec = dbstore.load("flows", fid) or {"id": fid, "kind": "flow", "start": flow["start"], "env": flow["env"],
                                             "versions": [], "notes": "", "revision": 0}
        cur = rec.get("current") or {}
        if cur.get("fingerprint") == flow["fingerprint"]:
            if cur.get("sources_rev") != flow.get("sources_rev") or cur.get("deps") != flow.get("deps") or \
                    cur.get("scope") != flow.get("scope") or rec.get("review"):
                # same flow, newer sources: no new version; rebuilt against today's evidence = revalidated
                rec["current"] = dict(cur, sources_rev=flow.get("sources_rev"), checked_at=flow.get("built_at"),
                                      deps=flow.get("deps"), scope=flow.get("scope"))
                rec.pop("review", None)
                try:
                    rec = dbstore.save("flows", rec)
                except dbstore.Conflict:
                    continue
            return dict(rec["current"], saved=True, version=cur.get("version"), notes=rec.get("notes") or "")
        n = int(cur.get("version") or 0)
        if cur:
            old = dict(cur, review=rec["review"]) if rec.get("review") else cur
            rec["versions"] = (list(rec.get("versions") or []) + [old])[-20:]
        rec["current"] = dict(flow, version=n + 1)
        rec["start"], rec["env"] = flow["start"], flow["env"]
        rec.pop("review", None)  # the new version is built from today's evidence; earlier versions keep theirs
        try:
            saved = dbstore.save("flows", rec)
            return dict(saved["current"], saved=True, notes=saved.get("notes") or "", revision=saved["revision"])
        except dbstore.Conflict:
            continue
    raise FlowError("the flow record changed while saving; try again")


def saved_flow(fid):
    return dbstore.load("flows", fid) if dbmodel.valid_id(fid, "f") else None


# --------------------------------------------------------------------------- explanation

def deterministic_explanation(flow):
    items = []
    for s in flow.get("steps") or []:
        n = "%d. " % s["position"] if s.get("order") is not None else "- "
        cite = next((v.get("cite") or v.get("text") for v in s.get("evidence") or []), "no citation")
        items.append({"step": s["id"], "text": "%s%s (%s; %s)" % (n, s["label"], s["basis_label"], cite),
                      "evidence": [v["id"] for v in s.get("evidence") or []][:3]})
    gaps = ["%s: %s Next: %s" % (g["label"], g["text"], g["next"]) for g in flow.get("gaps") or []]
    return {"source": "deterministic", "summary": "%s. %s" % (flow.get("label"), flow.get("scope_line")),
            "items": items, "gaps": gaps}


EXPLAIN_MAX = 400


def explain_flow(flow, caller=None, cfg=None, health=None):
    """A model explanation when one is asked for and allowed, validated against the flow's own IDs; otherwise (no
    caller, budget spent, call failed, output rejected) the deterministic explanation, with the reason."""
    base = deterministic_explanation(flow)
    if caller is None and cfg is None:
        return dict(base, reason="no model call requested")
    from . import extract, modelbudget
    if health is not None and cfg is not None and modelbudget.budget_left(health, cfg) <= 0:
        return dict(base, reason="today's model budget is spent")
    inp = {"label": flow.get("label"), "title": flow.get("title"), "environment": flow.get("environment"),
           "steps": [{"id": s["id"], "kind": s["kind"], "label": s["label"], "basis": s["basis"], "path": s["path"],
                      "async": s["async"], "order": s["order"],
                      "evidence": [{"id": v["id"], "text": v.get("text")} for v in s.get("evidence") or []][:4]}
                     for s in flow.get("steps") or []][:60],
           "gaps": [{"kind": g["kind"], "text": g["text"]} for g in flow.get("gaps") or []]}
    try:
        res, _cost = (caller or extract.call_claude)("dbflow_explain", json.dumps(inp, ensure_ascii=False), cfg)
    except Exception as e:  # noqa: BLE001 - a failed call leaves the deterministic explanation
        return dict(base, reason="model call failed: %s" % str(e)[:120])
    why = validate_explanation(res, flow)
    if why:
        return dict(base, reason="model output rejected: %s" % why)
    return {"source": "model", "summary": str(res.get("summary") or "")[:EXPLAIN_MAX],
            "items": [{"step": i["step"], "text": str(i["text"])[:EXPLAIN_MAX], "evidence": list(i.get("evidence") or [])}
                      for i in res.get("steps") or []], "gaps": base["gaps"], "reason": ""}


def validate_explanation(res, flow):
    """'' when every step and evidence ID the model used is the flow's own and claims no basis; else why not."""
    if not isinstance(res, dict) or not isinstance(res.get("steps"), list):
        return "not a step list"
    steps = {s["id"]: s for s in flow.get("steps") or []}
    for i in res["steps"]:
        if not isinstance(i, dict) or i.get("step") not in steps:
            return "unknown step %r" % (i.get("step") if isinstance(i, dict) else i)
        if not isinstance(i.get("text"), str) or not i["text"].strip():
            return "an empty explanation"
        own = {v["id"] for v in steps[i["step"]].get("evidence") or []}
        bad = [v for v in i.get("evidence") or [] if v not in own]
        if bad:
            return "evidence %s is not this step's" % bad[0]
        if "verified" in i["text"].lower():
            return "it calls a step verified"
    return ""
