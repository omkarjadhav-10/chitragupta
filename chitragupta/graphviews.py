"""Evidence Graph query service: read-only, access-checked, bounded projections of the index (design section 13).

Every read: the ticket must be visible (else `PermissionError`); the index must be on the current profile generation
(else `state: "rebuilding"` and nothing else); the authorized scope set is computed once (the ticket level, each
workflow that passes `companionviews.visibility`, the locations the profile includes); every row, alias, count,
reason and next action is derived from what survives that filter, never from a stored aggregate. All lookups are on
`(ticket, id)`: `graphstore.ticket_rows` reads one ticket's rows only. No write, build, process, network or model
call. Envelopes reuse the Companion's (`companionviews._envelope` / `_finish`, 64 KiB, HMAC cursors).
"""
import json
import re

from . import common, graphmodel, graphstore, profile, workflowstore
from . import companionviews as cv

VIEWS = ("summary", "trace", "changes")
ARGS = {"view", "ticket", "requirement", "workflow", "since", "cursor", "basis", "history", "privacy"}
OPERATION = {"summary": "graph_summary", "trace": "graph_trace", "changes": "graph_changes"}
PAGE_SIZE = cv.PAGE_SIZE
TEXT = 200
CAP = 50   # ids listed beside a total
HIDDEN = "hidden (privacy mode)"
LOCAL = "local baseline"
_KEY = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
_REQ = re.compile(r"^(?:gr_[0-9a-f]{24}|ac\d{1,4}|R\d{1,6})$")
_SINCE = re.compile(r"^\d{1,9}$")
DIMENSIONS = ("plan_coverage", "implementation", "verification")
_COLUMN = {"plan_coverage": "plan", "implementation": "implementation", "verification": "verification"}
_BEST = {dim: graphmodel.ORDER[dim][-1] for dim in DIMENSIONS}


# ---------------------------------------------------------------- validation

def _validate(args):
    """Allowlisted keys, 8 KiB, every value checked. → the view."""
    if not isinstance(args, dict) or set(args) - ARGS:
        raise ValueError("invalid graph read arguments")
    if cv._size(args) > cv.INPUT_BYTES:
        raise ValueError("graph read arguments exceed 8 KiB")
    view = args.get("view", "summary")
    if view not in VIEWS:
        raise ValueError("unknown graph view")
    key = args.get("ticket")
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise ValueError("invalid ticket argument")
    for name, rx in (("requirement", _REQ), ("workflow", cv._WID)):
        value = args.get(name)
        if value is not None and (not isinstance(value, str) or not rx.fullmatch(value)):
            raise ValueError("invalid %s argument" % name)
    since = args.get("since")
    if since is not None and (isinstance(since, bool) or not (isinstance(since, int) and 0 <= since < 10 ** 9)
                              and not (isinstance(since, str) and _SINCE.fullmatch(since))):
        raise ValueError("invalid since argument")
    if args.get("cursor") is not None and (not isinstance(args["cursor"], str) or len(args["cursor"]) > 1024):
        raise ValueError("stale or invalid cursor; reload the view")
    if args.get("basis") is not None and args["basis"] not in graphmodel.BASIS:
        raise ValueError("basis must be one of: %s" % ", ".join(graphmodel.BASIS))
    for name in ("history", "privacy"):
        if args.get(name) is not None and not isinstance(args[name], bool):
            raise ValueError("%s must be true or false" % name)
    return view


# ---------------------------------------------------------------- access

def _workflow_ok(cfg, wids):
    """The workflows (by id) that pass companion.authorize's checks now; an unreadable one does not."""
    ok, out = cv.visibility(cfg), set()
    for wid in sorted(wids):
        try:
            rec = workflowstore.load(wid)
        except Exception:
            continue
        if rec and ok(rec.get("binding") or {}):
            out.add(wid)
    return out


def _scope(key, cfg):
    """→ the ticket's rows and its authorized scope set, or {state} when there is nothing to show yet.

    Raises PermissionError when the ticket is outside the personal projection: a hidden ticket reads as unknown."""
    from .dashboard import api
    if not api.scope_of(key, cfg).get("visible"):
        raise PermissionError("ticket is unavailable in the current profile")
    generation = profile.signature(cfg)
    con = graphstore.open_read()
    if con is None:
        return {"state": "not_built", "generation": generation}
    m = graphstore.meta(con)
    if m.get("profile_generation") != generation:
        return {"state": "rebuilding", "generation": generation}
    nodes, edges, statuses, source = graphstore.ticket_rows(con, key)
    if source is None:
        return {"state": "not_built", "generation": generation}
    if source.get("profile_generation") != generation:
        return {"state": "rebuilding", "generation": generation}
    wids = {r.get("workflow") for r in nodes + edges + statuses if r.get("workflow")}
    wids |= set((source.get("coverage") or {}).get("workflows") or [])
    pol = profile.Policy(cfg)
    places = {}
    for row in nodes:
        if row.get("repo_id") and row.get("location"):
            places.setdefault(row["repo_id"], set()).add(row["location"])
    return {"state": "ok", "generation": generation, "ticket": key, "meta": m, "source": source,
            "rows": (nodes, edges, statuses), "workflows": _workflow_ok(cfg, wids),
            "location_ok": lambda location: bool(pol.repo_in_scope(location)), "places": places}


def _filtered(rows, scope):
    """The rows the caller may see: nodes first, then edges whose endpoints both survived, status rows of authorized
    scopes, aliases and baseline references of authorized workflows only. → {nodes: {id: node}, edges, statuses}."""
    nodes, edges, statuses = rows
    allowed, located, places = scope["workflows"], scope["location_ok"], scope["places"]

    def visible(row):
        if row.get("workflow") and row["workflow"] not in allowed:
            return False
        if row.get("location"):
            return located(row["location"])
        known = places.get(row.get("repo_id") or "")
        return not known or any(located(p) for p in known)

    kept = {}
    for row in nodes:
        if not visible(row):
            continue
        row = json.loads(json.dumps(row))   # never hand out the stored row itself
        attrs = row.get("attrs") or {}
        if row["type"] == "requirement":
            aliases = {w: a for w, a in (attrs.get("aliases") or {}).items() if w in allowed}
            if attrs.get("source") == LOCAL and not aliases:
                continue   # wording only a hidden workflow's baseline introduced
            attrs["aliases"] = aliases
            attrs["alias"] = aliases[sorted(aliases)[0]] if aliases else (
                "R%d" % attrs["ordinal"] if attrs.get("ordinal") else "")
            if attrs.get("status") != "removed":
                attrs["status"] = "current" if aliases else "unconfirmed"
            if attrs.get("ambiguity") and len(set(aliases)) < 2:
                attrs["ambiguity"] = None
        elif row["type"] == "requirement_revision":
            attrs["confirmed_in"] = [c for c in attrs.get("confirmed_in") or [] if c.get("workflow") in allowed]
        kept[row["id"]] = row
    for row in list(kept.values()):   # a revision whose requirement is hidden goes with it
        if row["type"] == "requirement_revision" and (row.get("attrs") or {}).get("requirement") not in kept:
            del kept[row["id"]]
    out_edges = [json.loads(json.dumps(e)) for e in edges
                 if e["src"] in kept and e["dst"] in kept and (not e.get("workflow") or e["workflow"] in allowed)]
    out_statuses = [json.loads(json.dumps(s)) for s in statuses
                    if s["requirement"] in kept and (not s.get("workflow") or s["workflow"] in allowed)]
    return {"nodes": kept, "edges": out_edges, "statuses": out_statuses}


def _stale(key, cfg, source):
    """The inputs moved since the rows were built (a stat pass; nothing is rebuilt)."""
    from . import graphbuild
    try:
        return graphbuild.input_signature(key, cfg) != source.get("sig")
    except Exception:
        return True


def _graph_block(scope, key, cfg):
    if scope["state"] != "ok":
        return {"generation": None, "snapshot": None, "built_at": None, "state": scope["state"], "stale": None}
    source, m = scope["source"], scope["meta"]
    stale = _stale(key, cfg, source)
    try:
        snapshot = int(source.get("snapshot") or 0)
    except ValueError:
        snapshot = None
    return {"generation": int(m.get("generation") or 0), "snapshot": snapshot, "built_at": source.get("built_at"),
            "state": "behind" if stale else "ok", "stale": stale}


def _envelope(view, cfg, privacy, key, graph):
    out = cv._envelope(OPERATION[view], cfg, privacy)
    out.update(ticket=key, graph=graph)
    return out


def _cursor_ref(view, args):
    return [args.get("ticket"), args.get("requirement"), args.get("workflow"), args.get("basis"),
            bool(args.get("history")), str(args.get("since") or "")]


# ---------------------------------------------------------------- requirement statuses, combined at read time

def _plain(dim, revision, has_changes):
    if dim == "plan_coverage":
        return graphmodel.plan_coverage(False, False, None, False)
    if dim == "implementation":
        return graphmodel.implementation([], [], has_changes)
    return graphmodel.verification(None, None, revision)


def _brief(status):
    return {"state": status.get("state"), "basis": status.get("basis"), "reason": cv.safe(status.get("reason"), 300)}


def _requirements(view):
    """Each visible requirement with its scope rows and its combined statuses (authorized scopes only)."""
    nodes = view["nodes"]
    has_changes = any(n["type"] in ("commit", "code_change") for n in nodes.values())
    scoped = {}
    for row in view["statuses"]:
        if row.get("workflow"):
            scoped.setdefault(row["requirement"], []).append(row)
    out = []
    for rid, node in nodes.items():
        if node["type"] != "requirement":
            continue
        attrs = node["attrs"]
        revision = nodes.get(attrs.get("revision")) or {}
        rows = sorted(scoped.get(rid) or [], key=lambda r: r["workflow"])
        counted = [r for r in rows if not (r.get("detail") or {}).get("excluded")]
        combined = {}
        for dim in DIMENSIONS:
            if counted:
                states = [r["detail"][dim] for r in counted]
                state = graphmodel.combine(dim, [s["state"] for s in states])
                combined[dim] = _brief(next(s for s in states if s["state"] == state))
            else:
                combined[dim] = _brief(_plain(dim, (revision.get("attrs") or {}).get("digest"), has_changes))
        out.append({"id": rid, "node": node, "revision": revision, "rows": rows, "combined": combined,
                    "excluded": bool(rows) and not counted, "removed": attrs.get("status") == "removed"})
    return out


def _critical(req):
    c = req["combined"]
    return (c["verification"]["state"] == "Failed" or c["plan_coverage"]["reason"] == graphmodel.PLAN_CHANGED
            or c["verification"]["reason"] == graphmodel.VERIFY_CHANGED or c["plan_coverage"]["state"] == "Missing")


def resolve_requirement(key, value, workflow, view):
    """A requirement the caller may see, by `gr_` id, by alias (`ac2`, qualified by `workflow` when several
    workflows use it) or by `R<n>`. → its id; ValueError listing the choices when it is unknown or ambiguous."""
    reqs = {n["id"]: n for n in view["nodes"].values() if n["type"] == "requirement"}
    if value in reqs:
        return value
    if str(value).startswith("gr_"):
        raise ValueError("unknown requirement for %s" % key)
    if str(value).startswith("R"):
        pairs = [(rid, None) for rid, n in reqs.items() if "R%s" % n["attrs"].get("ordinal") == value]
    else:   # an alias belongs to one workflow's baseline: used by several, it must be qualified
        pairs = sorted((rid, w) for rid, n in reqs.items() for w, a in n["attrs"]["aliases"].items()
                       if a == value and (workflow is None or w == workflow))
    if len(pairs) == 1:
        return pairs[0][0]
    if not pairs:
        raise ValueError("unknown requirement %s for %s" % (value, key))
    choices = sorted("%s (%s in workflow %s)" % (rid, value, w) for rid, w in pairs)
    raise ValueError("%s is used by several workflows; pass workflow, or use one of: %s" % (value, "; ".join(choices)))


# ---------------------------------------------------------------- summary

def _compact(req, view, out_edges, privacy):
    """What a one-screen trace needs: plan, commits, PR, run, the first unmet thing."""
    nodes = view["nodes"]
    plan = None
    for row in req["rows"]:
        pid = (row["detail"].get("plan_coverage") or {}).get("plan")
        if pid in nodes:
            plan = nodes[pid]["label"]
            break
    commits = []
    for row in req["rows"]:
        for eid in (row["detail"].get("implementation") or {}).get("edges") or []:
            edge = view["by_id"].get(eid)
            target = nodes.get(edge["dst"]) if edge else None
            if target is None:
                continue
            if target["type"] == "commit":
                commits.append(target)
            for e in out_edges.get(target["id"], []):
                if target["type"] == "code_change" and e["type"] == "PART_OF_COMMIT" and e["dst"] in nodes:
                    commits.append(nodes[e["dst"]])
    commits = sorted({c["id"]: c for c in commits}.values(), key=lambda c: c["id"])
    pr = None
    for c in commits:
        for e in out_edges.get(c["id"], []):
            if e["type"] == "INCLUDED_IN_PR" and e["dst"] in nodes:
                a = nodes[e["dst"]]["attrs"]
                pr = {"number": a.get("number"), "state": a.get("state"), "ci": (a.get("ci") or {}).get("state")}
                break
        if pr:
            break
    run = None
    current = req["revision"].get("id")
    for e in out_edges.get(current, []):
        target = nodes.get(e["dst"])
        if e["type"] == "SUPPORTED_BY" and target and target["type"] == "test_run":
            a = target["attrs"]
            run = "%s; observed at %s" % (a.get("status"), str(a.get("head") or "unknown")[:7] or "unknown")
            break
    c, gap = req["combined"], None
    if c["verification"]["state"] == "Failed":
        gap = c["verification"]["reason"]
    else:
        for dim in DIMENSIONS:
            if c[dim]["state"] != _BEST[dim]:
                gap = c[dim]["reason"]
                break
    return {"plan": plan, "commit": commits[0]["attrs"].get("short") if commits else None, "commits": len(commits),
            "pr": pr, "run": run, "gap": gap}


def _ambiguous(view):
    nodes, found = view["nodes"], {}

    def add(kind, rid):
        found.setdefault(kind, []).append(rid)

    for e in view["edges"]:
        if e["basis"] == "inferred":
            add("inferred link", e["id"])
        if (e.get("attrs") or {}).get("ambiguous"):
            add("plan-unbound legacy mapping", e["id"])
    for n in nodes.values():
        a = n.get("attrs") or {}
        if n["type"] == "requirement" and a.get("ambiguity"):
            add("baselines differ", n["id"])
        if a.get("identity_basis") in ("folder-name", "ambiguous"):
            add("weak repository identity", n["id"])
        if n["type"] == "ticket" and a.get("identity") == "key-only":
            add("key-only ticket identity", n["id"])
        if n["type"] == "test_run" and (a.get("applicability") or {}).get("state") == "ambiguous":
            add("run with conflicting references", n["id"])
    return {kind: {"total": len(ids), "ids": sorted(ids)[:CAP]} for kind, ids in sorted(found.items())}


def _stale_evidence(view):
    ids = []
    for n in view["nodes"].values():
        a = n.get("attrs") or {}
        if n["lifecycle"] == "source_missing" or (n["type"] == "test_run" and (
                n["lifecycle"] == "historical" or (a.get("applicability") or {}).get("state") == "different commit")):
            ids.append(n["id"])
    for e in view["edges"]:
        if e["type"] == "SUPPORTED_BY" and (e.get("attrs") or {}).get("applicability") == "historical":
            ids.append(e["id"])
    return {"total": len(ids), "ids": sorted(ids)[:CAP]}


def _row(req, view, out_edges, privacy):
    attrs, rev = req["node"]["attrs"], req["revision"].get("attrs") or {}
    text = _text(rev.get("text"), privacy, TEXT)
    scopes = [{"workflow": r["workflow"], "repository": cv._repo(r.get("location")), "alias": r.get("alias") or None,
               "excluded": bool(r["detail"].get("excluded")),
               **{dim: _brief(r["detail"][dim]) for dim in DIMENSIONS}} for r in req["rows"]]
    return {"id": req["id"], "alias": attrs.get("alias") or None, "aliases": dict(attrs.get("aliases") or {}),
            "ordinal": attrs.get("ordinal"), "status": attrs.get("status"), "source": attrs.get("source"),
            "text": text, "excluded": req["excluded"],
            "statuses": {"combined": req["combined"], "scopes": scopes},
            "compact": _compact(req, view, out_edges, privacy)}


def _requirement_source(key, coverage, privacy):
    """The current requirement snapshot as the history file records it (read only)."""
    found, data = graphstore.load_history(key)
    src = ((data or {}).get("requirements") or {}).get("source") or {} if found == "ok" else {}
    req = coverage.get("requirements") or {}
    gaps = [cv.safe(g, 300) for g in src.get("gaps") or []][:CAP]
    return {"state": req.get("state") or src.get("state") or "unknown",
            "completeness": req.get("completeness") or src.get("completeness") or "unknown",
            "fetched_at": req.get("fetched_at") or src.get("fetched_at"), "gaps": gaps}


def summary(args, cfg=None):
    cfg = cfg if cfg is not None else common.load_config()
    key = args["ticket"]
    privacy = cv._privacy(args, cfg)
    scope = _scope(key, cfg)
    graph = _graph_block(scope, key, cfg)
    out = _envelope("summary", cfg, privacy, key, graph)
    if scope["state"] != "ok":
        return cv._finish(out)
    view = _filtered(scope["rows"], scope)
    view["by_id"] = {e["id"]: e for e in view["edges"]}
    out_edges = {}
    for e in view["edges"]:
        out_edges.setdefault(e["src"], []).append(e)
    reqs = _requirements(view)
    live = [r for r in reqs if not r["removed"]]
    counted = [r for r in live if not r["excluded"]]
    states = {dim: {} for dim in DIMENSIONS}
    for r in counted:
        for dim in DIMENSIONS:
            states[dim][r["combined"][dim]["state"]] = states[dim].get(r["combined"][dim]["state"], 0) + 1
    critical = sorted(r["id"] for r in counted if _critical(r))
    order = sorted(reqs, key=lambda r: (r["id"] not in critical, r["removed"], r["node"]["attrs"].get("ordinal") is None,
                                        r["node"]["attrs"].get("ordinal") or 0, r["id"]))
    prs = [{"number": n["attrs"].get("number"), "state": n["attrs"].get("state")}
           for n in view["nodes"].values() if n["type"] == "pull_request" and not n["attrs"].get("shared")]
    coverage = scope["source"].get("coverage") or {}
    req_source = _requirement_source(key, coverage, privacy)
    action_rows = [{"id": r["id"], "aliases": [r["node"]["attrs"].get("alias") or r["id"]], "excluded": r["excluded"],
                    "statuses": r["combined"]} for r in sorted(live, key=lambda r: (
                        r["node"]["attrs"].get("ordinal") is None, r["node"]["attrs"].get("ordinal") or 0, r["id"]))]
    allowed = scope["workflows"]
    out.update({
        "snapshot": dict(req_source, seq=graph["snapshot"], at=graph["built_at"]),
        "requirements": {"total": len(reqs), "current": sum(1 for r in live if r["node"]["attrs"]["status"] == "current"),
                         "unconfirmed": sum(1 for r in live if r["node"]["attrs"]["status"] == "unconfirmed"),
                         "removed": len(reqs) - len(live), "excluded": sum(1 for r in live if r["excluded"])},
        "counts": states,
        "critical": {"total": len(critical), "ids": critical[:CAP]},
        "ambiguous": _ambiguous(view),
        "stale_evidence": _stale_evidence(view),
        "source_coverage": {"requirements": {k: req_source[k] for k in ("state", "completeness", "fetched_at")},
                            "workflows": len(allowed & set(coverage.get("workflows") or [])),
                            "gaps": [dict(g) for g in coverage.get("gaps") or [] if g.get("workflow") in allowed][:CAP],
                            "unprojected": sum(len(v) for w, v in (coverage.get("unprojected") or {}).items()
                                               if w in allowed)},
        "next_action": graphmodel.next_action(action_rows, prs, req_source["state"]),
    })
    ref = _cursor_ref("summary", args)
    offset = cv.read_cursor(args.get("cursor"), "summary", ref, scope["generation"], graph["snapshot"])
    rows = [_row(r, view, out_edges, privacy) for r in order]

    def build(page):
        return dict(out, rows=page, total=len(rows), offset=offset, next_cursor="x" * 120)

    page, n = cv._page(rows, offset, build)
    nxt = offset + n
    out.update(rows=page, total=len(rows), offset=offset,
               next_cursor=cv.make_cursor("summary", ref, nxt, scope["generation"], graph["snapshot"])
               if nxt < len(rows) else None)
    return cv._finish(out)


# ---------------------------------------------------------------- shared text and item helpers

_SCHEME = re.compile(r"(?i)\b(?:javascript|vbscript|data|file):\S*")
PRIVATE = {"commit", "pull_request", "plan_step", "requirement_revision", "requirement"}   # labels that are free text
KEY_ATTRS = {"plan_step": ("step", "kind", "plan"), "code_change": ("path", "state", "verified"),
             "commit": ("short", "mine", "date", "ref", "sha_basis", "identity_basis"),
             "test_case": ("test_type",), "test_run": ("status", "source", "head", "head_basis", "attempt"),
             "observation": ("kind", "outcome", "basis"), "pull_request": ("number", "state", "review", "head", "shared"),
             "release_evidence": ("fact", "environment", "state", "revision")}
ATTR_BYTES = 1024


def _text(value, privacy, limit=300):
    """Untrusted text made inert: redacted, control characters gone, no link of any scheme, no absolute path."""
    if privacy:
        return HIDDEN
    return _SCHEME.sub("[link]", cv.scrub(value, 2 * limit))[:limit]


def _label(node, privacy):
    if privacy and node["type"] in PRIVATE:
        a = node.get("attrs") or {}
        return {"commit": a.get("short") or "commit", "pull_request": "#%s" % a.get("number"),
                "plan_step": "step %s" % a.get("step")}.get(node["type"], HIDDEN)
    return _text(node.get("label"), False)


def _key_attrs(node):
    a = node.get("attrs") or {}
    out = {}
    for name in KEY_ATTRS.get(node["type"], ()):
        value = a.get(name)
        out[name] = _text(value, False, 120) if isinstance(value, str) else value
    if node["type"] == "test_run":
        app = a.get("applicability") or {}
        out["applicability"] = app if isinstance(app, str) else {"state": app.get("state"),
                                                                  "text": _text(app.get("text"), False)}
    if node["type"] == "pull_request":
        out["ci"] = (a.get("ci") or {}).get("state")
    if a.get("pinned"):
        out.update(pinned=True, note=_text(a.get("note"), False))
    return out if cv._size(out) <= ATTR_BYTES else {"truncated": True}


def _node(node, privacy):
    return {"id": node["id"], "type": node["type"], "label": _label(node, privacy), "lifecycle": node["lifecycle"],
            "attrs": _key_attrs(node), "source_ref": _text(node.get("source_ref"), False)}


def _edge(edge):
    if edge is None:
        return None
    a = edge.get("attrs") or {}
    out = {"id": edge["id"], "type": edge["type"], "basis": edge["basis"], "confidence": edge.get("confidence"),
           "scope": edge.get("scope") or "", "lifecycle": edge["lifecycle"],
           "evidence_ref": _text(edge.get("evidence_ref"), False),
           "declarations": [{"by": _text(d.get("by"), False, 80), "at": d.get("at"), "ref": _text(d.get("ref"), False)}
                            for d in a.get("declarations") or []][:10]}
    for name in ("applicability", "reason", "coverage", "outcome", "criterion", "ambiguous", "plan_binding"):
        if a.get(name) not in (None, ""):
            out[name] = _text(a[name], False) if isinstance(a[name], str) else a[name]
    return out


def _paged(out, view_name, args, scope, graph, items, name="items"):
    ref = _cursor_ref(view_name, args)
    offset = cv.read_cursor(args.get("cursor"), view_name, ref, scope["generation"], graph["snapshot"])

    def build(page):
        return dict(out, **{name: page, "total": len(items), "offset": offset, "next_cursor": "x" * 120})

    page, n = cv._page(items, offset, build)
    nxt = offset + n
    out.update({name: page, "total": len(items), "offset": offset,
                "next_cursor": cv.make_cursor(view_name, ref, nxt, scope["generation"], graph["snapshot"])
                if nxt < len(items) else None})
    return out


# ---------------------------------------------------------------- trace

LAYERS = ("plan steps", "code changes", "commits", "tests and runs", "pull requests", "release evidence")
LAYER_OF = {"plan_step": "plan steps", "code_change": "code changes", "commit": "commits",
            "test_case": "tests and runs", "test_run": "tests and runs", "observation": "tests and runs",
            "pull_request": "pull requests", "release_evidence": "release evidence"}
LINKS = ("ADDRESSED_BY", "IMPLEMENTED_BY", "COVERED_BY_TEST", "SUPPORTED_BY")
WALK = {"requirement_revision": LINKS, "plan_step": ("IMPLEMENTED_BY",), "code_change": ("PART_OF_COMMIT",),
        "test_case": ("EXECUTED_IN",), "pull_request": ("CHECKED_BY",),
        "commit": ("INCLUDED_IN_PR", "HAS_RELEASE_EVIDENCE", "EQUIVALENT_TO", "CHERRY_PICKED_FROM", "SQUASHED_INTO")}
OLD = ("superseded", "historical", "removed")
TICKET_LEVEL = "ticket-level evidence; not linked to this requirement"


def _occurrences(key, rid):
    found, data = graphstore.load_history(key)
    entry = (((data or {}).get("requirements") or {}).get("logical") or {}).get(rid) if found == "ok" else None
    from .graphbuild import revision_id
    return [{"seq": h.get("seq"), "at": h.get("at"), "event": h.get("event"), "revision": revision_id(rid, h["revision"])}
            for h in (entry or {}).get("history") or [] if isinstance(h, dict) and h.get("revision")][-CAP:]


def trace(args, cfg=None):
    """One requirement: its occurrences and revisions, then what reaches it, layer by layer, each item with the edge
    that reached it; then the ticket-level evidence not linked to it. Paged over all items."""
    cfg = cfg if cfg is not None else common.load_config()
    key = args["ticket"]
    privacy = cv._privacy(args, cfg)
    scope = _scope(key, cfg)
    graph = _graph_block(scope, key, cfg)
    out = _envelope("trace", cfg, privacy, key, graph)
    if scope["state"] != "ok":
        return cv._finish(out)
    if not args.get("requirement"):
        raise ValueError("trace needs a requirement (gr_ id, alias or R<n>)")
    view = _filtered(scope["rows"], scope)
    nodes = view["nodes"]
    rid = resolve_requirement(key, args["requirement"], args.get("workflow"), view)
    history, basis = bool(args.get("history")), args.get("basis")
    out_edges = {}
    for e in sorted(view["edges"], key=lambda e: e["id"]):
        out_edges.setdefault(e["src"], []).append(e)
    req = nodes[rid]
    revisions = sorted((n for n in nodes.values() if n["type"] == "requirement_revision"
                        and n["attrs"].get("requirement") == rid),
                       key=lambda n: (n["id"] != req["attrs"].get("revision"), n["id"]))
    starts = [n["id"] for n in revisions if history or n["id"] == req["attrs"].get("revision")]

    def edge_ok(e):
        return (history or e["lifecycle"] not in OLD) and not (basis and e["type"] in LINKS and e["basis"] != basis)

    def node_ok(n):
        return history or n["lifecycle"] not in OLD

    reached, queue = {}, list(starts)
    while queue:
        src = queue.pop(0)
        for e in out_edges.get(src, []):
            dst = nodes.get(e["dst"])
            if e["type"] not in WALK.get(nodes[src]["type"], ()) or not edge_ok(e) or dst is None \
                    or dst["id"] in reached or dst["type"] not in LAYER_OF or not node_ok(dst):
                continue
            reached[dst["id"]] = e   # both ends are in the filtered view: no edge to a hidden endpoint
            queue.append(dst["id"])
    items = sorted(({"layer": LAYER_OF[nodes[n]["type"]], "node": _node(nodes[n], privacy), "edge": _edge(e)}
                    for n, e in reached.items()), key=lambda i: (LAYERS.index(i["layer"]), i["node"]["id"]))
    loose = sorted((n for n in nodes.values() if n["type"] in ("commit", "test_run", "pull_request")
                    and n["id"] not in reached and node_ok(n)), key=lambda n: (n["type"], n["id"]))
    items += [{"layer": "ticket evidence", "node": _node(n, privacy), "edge": None, "note": TICKET_LEVEL} for n in loose]
    totals = {layer: sum(1 for i in items if i["layer"] == layer) for layer in LAYERS + ("ticket evidence",)}
    a = req["attrs"]
    supersedes = [_edge(e) for r in revisions for e in out_edges.get(r["id"], [])
                  if e["type"] == "SUPERSEDES" and edge_ok(e)]
    out.update(requirement={
        "id": rid, "alias": a.get("alias") or None, "aliases": dict(a.get("aliases") or {}), "status": a.get("status"),
        "source": a.get("source"), "text": _text((nodes.get(a.get("revision")) or {}).get("attrs", {}).get("text"),
                                                privacy, TEXT),
        "revisions": [{"id": r["id"], "lifecycle": r["lifecycle"], "digest": r["attrs"].get("digest"),
                       "text": _text(r["attrs"].get("text"), privacy, TEXT), "first_seen": r["attrs"].get("first_seen"),
                       "confirmed_in": r["attrs"].get("confirmed_in") or []}
                      for r in revisions if history or r["id"] == a.get("revision")][:CAP],
        "occurrences": _occurrences(key, rid) if history else _occurrences(key, rid)[-5:],
        "supersedes": supersedes[:CAP]},
        totals=totals, filters={"basis": basis, "history": history})
    return cv._finish(_paged(out, "trace", args, scope, graph, items))


# ---------------------------------------------------------------- changes

EVENTS = ("added", "modified", "removed", "restored")
CATEGORIES = ("requirements", "implementation", "tests", "delivery", "invalidated", "updated", "gaps", "status",
              "source")


def _since(journal, value):
    """→ (since sequence, None) or (None, reason)."""
    head = journal[-1]
    since = int(value) if value is not None else (head.get("prev") or 0)
    if value is None and head.get("baseline"):
        return None, "reset by recovery"
    seqs = {e["seq"] for e in journal}
    if since == 0:
        first = journal[0]
        return (0, None) if first["seq"] == 1 and first.get("prev") is None and not first.get("baseline") \
            else (None, "dropped by retention")
    if since in seqs:
        return since, None
    return None, ("dropped by retention" if since < journal[0]["seq"] else "unknown sequence")


def _window(journal, since, generation):
    """→ (the entries after `since`, None), or (None, reason) when they are not one continuous window of this
    profile generation."""
    by_seq = {e["seq"]: e for e in journal}
    entries = [e for e in journal if e["seq"] > since]
    if since and by_seq[since].get("profile_generation") != generation:
        return None, "from another profile generation (profile changed)"
    previous = since or None
    for e in entries:
        if e.get("profile_generation") != generation:
            return None, "profile changed"
        if e.get("baseline"):
            return None, "reset by recovery"
        if e.get("prev") != previous:
            return None, "history is not continuous after %s" % since
        previous = e["seq"]
    return entries, None


def _fold(entries):
    reqs, ev, status, source, more, coarse = {}, {}, {}, [], 0, False
    for e in entries:
        d, seq = e.get("delta") or {}, e["seq"]
        more += int(d.get("more") or 0)
        coarse = coarse or bool(d.get("coarse"))
        for event in EVENTS:
            for rid in (d.get("requirements") or {}).get(event) or []:
                reqs[rid] = (event, seq)
        for name in ("added", "removed", "changed"):
            for row in (d.get("evidence") or {}).get(name) or []:
                rid, typ, workflow, location = (list(row) + ["", "", "", ""])[:4]
                prev = (ev.get(rid) or {}).get("state")
                if name == "removed" and prev == "added":
                    del ev[rid]   # came and went inside the window
                    continue
                state = ("changed" if prev == "removed" else "added") if name == "added" else \
                    "removed" if name == "removed" else ("added" if prev == "added" else "changed")
                ev[rid] = {"state": state, "type": typ, "workflow": workflow, "location": location, "seq": seq}
        for t in d.get("status") or []:
            k = (t.get("requirement"), t.get("workflow") or "", t.get("dimension"))
            if k in status:
                status[k][1:] = [t.get("to"), seq]
            else:
                status[k] = [t.get("from"), t.get("to"), seq]
        if d.get("source"):
            source.append(dict(d["source"], seq=seq))
    return reqs, ev, status, source, more, coarse


def _current_state(view):
    reqs = _requirements(view)
    live = [r for r in reqs if not r["removed"] and not r["excluded"]]
    counts = {dim: {} for dim in DIMENSIONS}
    for r in live:
        for dim in DIMENSIONS:
            counts[dim][r["combined"][dim]["state"]] = counts[dim].get(r["combined"][dim]["state"], 0) + 1
    return {"requirements": len(reqs), "counts": counts}


def changes(args, cfg=None):
    """What changed after journal sequence `since` (default: the entry before the head), folded, for authorized
    scopes only. Any doubt about continuity or the profile generation → `baseline: "unavailable"` with the reason."""
    cfg = cfg if cfg is not None else common.load_config()
    key = args["ticket"]
    privacy = cv._privacy(args, cfg)
    scope = _scope(key, cfg)
    graph = _graph_block(scope, key, cfg)
    out = _envelope("changes", cfg, privacy, key, graph)
    if scope["state"] != "ok":
        return cv._finish(out)
    view = _filtered(scope["rows"], scope)
    nodes, generation, allowed = view["nodes"], scope["generation"], scope["workflows"]

    def unavailable(reason):
        out.update(baseline="unavailable", reason=reason, current=_current_state(view), items=[], total=0,
                   totals={}, offset=0, next_cursor=None)
        return cv._finish(out)

    found, data = graphstore.load_history(key)
    if found != "ok" or not data.get("journal"):
        return unavailable("history unavailable")
    journal = data["journal"]
    since, why = _since(journal, args.get("since"))
    if why:
        return unavailable(why)
    entries, why = _window(journal, since, generation)
    if why:
        return unavailable(why)
    reqs, ev, status, source, more, coarse = _fold(entries)
    stored = {n["id"] for n in scope["rows"][0]}
    hidden = stored - set(nodes)   # present in the index, not for this caller
    every = {r.get("workflow") for r in scope["rows"][0] + scope["rows"][1] + scope["rows"][2] if r.get("workflow")}
    every |= set((scope["source"].get("coverage") or {}).get("workflows") or [])
    ticket_level_ok = every <= allowed   # a ticket-level transition mixes every workflow; safe only if all are seen

    def label(rid):
        node = nodes.get(rid)
        return _label(node, privacy) if node else None

    items = []
    for rid, (event, seq) in reqs.items():
        if rid in hidden:
            continue
        node = nodes.get(rid)
        items.append({"category": "requirements", "kind": event, "id": rid, "seq": seq,
                      "label": (node["attrs"].get("alias") or None) if node else None})
    for rid, e in ev.items():
        if (e["workflow"] and e["workflow"] not in allowed) or (e["location"] and not scope["location_ok"](e["location"])) \
                or rid in hidden:
            continue
        node = nodes.get(rid)
        if e["state"] == "removed" or (e["state"] == "changed" and node and node["lifecycle"] in OLD + ("source_missing",)):
            category = "invalidated"
        elif e["type"] in ("pull_request", "release_evidence"):
            category = "delivery"
        elif e["state"] == "added" and e["type"] in ("commit", "code_change"):
            category = "implementation"
        elif e["state"] == "added" and e["type"] in ("test_case", "test_run", "observation"):
            category = "tests"
        else:
            category = "updated"
        items.append({"category": category, "kind": e["type"], "state": e["state"], "id": rid, "seq": e["seq"],
                      "label": label(rid)})
    for (rid, workflow, dim), (before, after, seq) in status.items():
        if before == after or rid in hidden or (workflow and workflow not in allowed) or \
                (not workflow and not ticket_level_ok):
            continue
        node = nodes.get(rid)
        row = {"kind": dim, "id": rid, "workflow": workflow or None, "from": before, "to": after, "seq": seq,
               "label": (node["attrs"].get("alias") or None) if node else None}
        items.append(dict(row, category="status"))
        best = _BEST.get(dim)
        if before is not None and after is not None and (after == best) != (before == best):
            items.append(dict(row, category="gaps", state="closed" if after == best else "opened"))
    for s in source:
        items.append({"category": "source", "kind": "requirements source", "from": s.get("from"), "to": s.get("to"),
                      "seq": s["seq"], "id": None, "label": None})
    items.sort(key=lambda i: (-i["seq"], CATEGORIES.index(i["category"]), str(i.get("id") or ""),
                              str(i.get("kind") or "")))
    totals = {c: sum(1 for i in items if i["category"] == c) for c in CATEGORIES}
    out.update(baseline="ok", reason=None, entries=len(entries), totals=totals, more=more, coarse=coarse,
               current=_current_state(view),
               **{"from": since, "to": journal[-1]["seq"]})
    return cv._finish(_paged(out, "changes", args, scope, graph, items))


# ---------------------------------------------------------------- resume: called by companionviews.detail and companioncli

RESUME_CAP = 10
NEW = {"commits": ("commit",), "tests": ("test_run", "observation"), "prs": ("pull_request", "release_evidence")}
NOT_YET = {"not_built": "the evidence graph is not built yet",
           "rebuilding": "the evidence graph is rebuilding for the current profile"}


def checkpoint(ticket, cfg=None):
    """The ticket's journal position now, for a workflow's `graph_checkpoint`: {seq, at, profile_generation}, or None
    when there is nothing to record (no index, an index or head of another profile generation, no history). Reads
    only. A graph behind its inputs records the older position, so what follows is reported as new, never lost."""
    cfg = cfg if cfg is not None else common.load_config()
    generation = profile.signature(cfg)
    con = graphstore.open_read()
    if con is None or graphstore.meta(con).get("profile_generation") != generation:
        return None
    found, data = graphstore.load_history(ticket)
    if found != "ok" or not data.get("journal") or data["journal"][-1].get("profile_generation") != generation:
        return None
    return {"seq": data["journal"][-1]["seq"], "at": common.now_iso(), "profile_generation": generation}


def _applies(node):
    """A delta item's applicability now: a run's own state, else the node's lifecycle; gone from the graph → removed."""
    if node is None:
        return "removed"
    app = (node.get("attrs") or {}).get("applicability")
    if isinstance(app, dict):
        return app.get("state") or "unknown"
    return app if isinstance(app, str) and app else node.get("lifecycle")


def _listed(items):
    items = sorted(items, key=lambda i: (-(i.get("seq") or 0), str(i.get("id") or ""), str(i.get("kind") or "")))
    return {"total": len(items), "items": items[:RESUME_CAP]}


def _since_mark(journal, mark, generation):
    """→ (entries after the checkpoint, None) or (None, why the checkpoint counts as absent)."""
    if not isinstance(mark, dict) or not mark:
        return None, "no graph checkpoint recorded yet"
    if mark.get("profile_generation") != generation:
        return None, "the checkpoint is from another profile generation"
    seq = mark.get("seq")
    if not journal or not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        return None, "history unavailable" if not journal else "the checkpoint is unreadable"
    since, why = _since(journal, seq)
    if why:
        return None, {"unknown sequence": "the checkpoint is not in the graph history"}.get(
            why, "the checkpoint's history was " + why)
    return _window(journal, since, generation)


def resume(ticket, workflow, since, cfg=None, privacy=False):
    """Resume's bounded graph delta for one workflow since its `graph_checkpoint` (`since`): completed since, newly
    invalidated, outstanding requirements, new commits / tests / pull requests / Jira changes, and the one next
    action; each list at most RESUME_CAP items beside its total. A checkpoint that is missing, of another profile
    generation or no longer continuous with the journal counts as absent: no delta, the reason, and still the
    outstanding requirements and next action. Not built, rebuilding or behind → {state: "unavailable", reason}.
    One way only: companionviews.detail and companioncli call this; it never calls companion.resume or them."""
    cfg = cfg if cfg is not None else common.load_config()
    try:
        scope = _scope(ticket, cfg)
    except PermissionError as exc:
        return {"state": "unavailable", "reason": str(exc)}
    if scope["state"] != "ok":
        return {"state": "unavailable", "reason": NOT_YET[scope["state"]]}
    if _stale(ticket, cfg, scope["source"]):
        return {"state": "unavailable", "reason": "the evidence graph is behind; it catches up on the next background run"}
    if workflow not in scope["workflows"]:
        return {"state": "unavailable", "reason": "this task is not in the evidence graph yet"}
    view, generation = _filtered(scope["rows"], scope), scope["generation"]
    nodes = view["nodes"]

    def alias(r):
        a = r["node"]["attrs"]
        return _text((a.get("aliases") or {}).get(workflow) or a.get("alias") or r["id"], False, 40)

    reqs = sorted((r for r in _requirements(view) if not r["removed"]), key=lambda r: (
        r["node"]["attrs"].get("ordinal") is None, r["node"]["attrs"].get("ordinal") or 0, r["id"]))
    own = {r["id"]: next((row for row in r["rows"] if row["workflow"] == workflow), None) for r in reqs}
    mine = [r for r in reqs if own[r["id"]]] or reqs   # this task's requirements; all of them before it has a baseline

    def statuses(r):
        row = own[r["id"]]
        if row and row.get("detail"):
            return {dim: _brief(row["detail"][dim]) for dim in DIMENSIONS}, bool(row["detail"].get("excluded"))
        return r["combined"], r["excluded"]

    action_rows, outstanding = [], []
    for r in mine:
        st, excluded = statuses(r)
        action_rows.append({"id": r["id"], "aliases": [alias(r)], "excluded": excluded, "statuses": st})
        if not excluded and any(st[dim]["state"] != _BEST[dim] for dim in DIMENSIONS):
            outstanding.append({"id": r["id"], "label": alias(r), "statuses": {dim: st[dim]["state"] for dim in DIMENSIONS}})
    prs = [{"number": n["attrs"].get("number"), "state": n["attrs"].get("state")}
           for n in nodes.values() if n["type"] == "pull_request" and not n["attrs"].get("shared")]
    source_state = _requirement_source(ticket, scope["source"].get("coverage") or {}, privacy)["state"]
    found, data = graphstore.load_history(ticket)
    journal = data.get("journal") if found == "ok" else None
    entries, why = _since_mark(journal, since, generation)
    out = {"state": "ok", "checkpoint": {"state": "absent" if why else "ok", "reason": why,
                                         "seq": (since or {}).get("seq") if not why else None,
                                         "at": (since or {}).get("at") if not why else None},
           "to": journal[-1]["seq"] if journal else None,
           "outstanding": {"total": len(outstanding), "items": outstanding[:RESUME_CAP]},
           "next_action": graphmodel.next_action(action_rows, prs, source_state)}
    completed, invalidated, new = [], [], {name: [] for name in ("commits", "tests", "prs", "jira")}
    if entries is not None:
        reqs_seen, ev, status, source, more, coarse = _fold(entries)
        hidden = {n["id"] for n in scope["rows"][0]} - set(nodes)
        labels = {r["id"]: alias(r) for r in reqs}
        for rid, (event, seq) in reqs_seen.items():
            if rid not in hidden:
                new["jira"].append({"kind": "requirement " + event, "id": rid, "label": labels.get(rid), "seq": seq})
        for s in source:
            new["jira"].append({"kind": "requirements source", "id": None, "label": "%s → %s" % (s.get("from"), s.get("to")),
                                "seq": s["seq"]})
        for rid, e in ev.items():
            if rid in hidden or (e["workflow"] and e["workflow"] != workflow) or \
                    (e["location"] and not scope["location_ok"](e["location"])):
                continue
            node = nodes.get(rid)
            item = {"kind": e["type"], "state": e["state"], "id": rid, "seq": e["seq"],
                    "label": _label(node, privacy) if node else None, "applicability": _applies(node)}
            if e["type"] == "test_run" and node:
                item["outcome"] = _text((node.get("attrs") or {}).get("status"), False, 40)
            if e["state"] == "removed" or (e["state"] == "changed" and node and node["lifecycle"] in OLD + ("source_missing",)):
                invalidated.append(item)
                continue
            for name, kinds in NEW.items():
                if e["type"] in kinds and (e["state"] == "added" or name == "prs"):
                    new[name].append(item)
        rows = scope["rows"][0] + scope["rows"][1] + scope["rows"][2]
        every = {r.get("workflow") for r in rows if r.get("workflow")}
        every |= set((scope["source"].get("coverage") or {}).get("workflows") or [])
        ticket_level_ok = every <= scope["workflows"]   # a ticket-level transition mixes every workflow
        for (rid, w, dim), (before, after, seq) in status.items():
            # this task's own rows; a ticket-level one only for a requirement this task has no row for
            skip = w != workflow if w else (not ticket_level_ok or rid not in own or own[rid] is not None)
            if before == after or rid in hidden or skip:
                continue
            row = {"kind": dim, "id": rid, "label": labels.get(rid), "from": before, "to": after, "seq": seq}
            if after == _BEST[dim]:
                completed.append(row)
            elif before == _BEST[dim]:
                invalidated.append(dict(row, kind=dim + " no longer complete"))
        out.update(more=more, coarse=coarse)
    out.update(completed=_listed(completed), invalidated=_listed(invalidated),
               new={name: _listed(items) for name, items in new.items()})
    return out


def shrink_resume(env):
    """A shrink step for an envelope holding a resume delta as `graph`: three items per list."""
    g = env.get("graph") or {}
    for part in [g.get(k) for k in ("completed", "invalidated", "outstanding")] + list((g.get("new") or {}).values()):
        if isinstance(part, dict):
            part["items"] = part["items"][:3]


# ---------------------------------------------------------------- dispatch and text

def read(args):
    """The one entry point: validate, then dispatch."""
    view = _validate(args)
    return {"summary": summary, "trace": trace, "changes": changes}[view](args)


def _line(value):
    return " ".join(str(value if value is not None else "").split())


def render_text(env):
    """Plain text for the CLI and MCP: every value is already inert; nothing here makes a link or markup."""
    op = env.get("operation", "")
    g = env.get("graph") or {}
    lines = ["%s %s: graph %s%s" % (env.get("ticket"), op.replace("graph_", ""), g.get("state"),
                                    " (inputs moved since the last build)" if g.get("stale") else "")]
    if g.get("state") not in ("ok", "behind"):
        lines.append({"rebuilding": "The index is being rebuilt for the current profile; try again shortly.",
                      "not_built": "Not built yet; the background run builds it."}.get(g.get("state"), ""))
        return "\n".join(l for l in lines if l)
    if op == "graph_summary":
        r = env.get("requirements") or {}
        lines.append("Next: %s" % _line(env.get("next_action")))
        lines.append("Requirements: %s total, %s current, %s unconfirmed, %s removed, %s excluded" % tuple(
            r.get(k, 0) for k in ("total", "current", "unconfirmed", "removed", "excluded")))
        for row in env.get("rows") or []:
            c = row["statuses"]["combined"]
            lines.append("- %s [%s | %s | %s] %s" % (_line(row.get("alias") or row["id"]), c["plan_coverage"]["state"],
                                                     c["implementation"]["state"], c["verification"]["state"],
                                                     _line(row.get("text"))))
    elif op == "graph_trace":
        req = env.get("requirement") or {}
        lines.append("Requirement %s (%s): %s" % (_line(req.get("alias") or req.get("id")), req.get("status"),
                                                 _line(req.get("text"))))
        for item in env.get("items") or []:
            e = item.get("edge") or {}
            lines.append("- %s: %s %s%s" % (item["layer"], item["node"]["type"], _line(item["node"]["label"]),
                                            " (%s, %s)" % (e.get("type"), e.get("basis")) if e else ""))
    elif op == "graph_changes":
        if env.get("baseline") != "ok":
            lines.append("Changes unavailable: %s" % _line(env.get("reason")))
        else:
            lines.append("Changes after %s up to %s (%s entries)" % (env.get("from"), env.get("to"), env.get("entries")))
            for item in env.get("items") or []:
                detail = "%s -> %s" % (item.get("from"), item.get("to")) if "from" in item else item.get("state") or ""
                lines.append("- %s: %s %s %s" % (item["category"], item.get("kind"),
                                                 _line(item.get("label") or item.get("id") or ""), _line(detail)))
    if env.get("next_cursor"):
        lines.append("More: pass cursor %s" % env["next_cursor"])
    return "\n".join(lines)
