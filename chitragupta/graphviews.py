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
    text = HIDDEN if privacy else cv.safe(rev.get("text"), TEXT)
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


def read(args):
    """The one entry point: validate, then dispatch. Views other than `summary` arrive with subplan 17."""
    view = _validate(args)
    if view == "summary":
        return summary(args)
    raise ValueError("graph view %s is not available yet" % view)
