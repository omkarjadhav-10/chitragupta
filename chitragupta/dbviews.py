"""Shared bounded read projections of the database-flow records (database flow D1, plan §3 "Projections"; DB-19): one
backend for the dashboard's GET routes, the MCP tools and the CLI. Reading never queries a database, never ingests,
never builds or saves anything: it projects what data/db/ already holds.

Envelope (every view):
  {schema_version, kind, id, profile_generation, policy_generation, scope_generation, environment, snapshot_at,
   source_revision, coverage, freshness, capabilities, state, states, data, items, more_available, cursor, links}
  `data` is the view's bounded summary, `items` its paged list (at most PAGE items and MAX_BYTES of JSON for the
  whole envelope; anything left is `more_available` with a `cursor`, never cut silently: an item too large on its own
  is replaced by a stub that says so).
Cursors are opaque: base64url(JSON {view, ref, offset, snapshot}) + "." + HMAC-SHA256 over it, the principal and the
  policy generation, keyed by a per-home secret (.worklog/db-view.key, written by ingestion, never by a read; without
  it a per-process key is used). A malformed, foreign (another principal or policy) or stale (the data changed)
  cursor is refused with CursorError: start again from the first page.
Access: every page is projected through dbflows.Visible (dbaccess.decide on every record, every time). Arguments are
  record IDs checked against dbmodel.ID_RX, or names checked against NAME_RX; paths, SQL and tool names are refused.
Required states (DB-23), each with a next step: no_database_mcp, no_selected_connection, unknown_environment,
  missing_identity, ambiguous_object, missing_metadata, unsupported_capability, permission_denied, partial_pages,
  disconnected_source, stale_flow.
Record investigations (D2): plan_view, job_view (a read of the job record: polling never enqueues), investigation_view,
  investigations_list (yours only: another principal's investigation has no title, count or snippet), handoff_view,
  context_view (the companion's database_context) and key_spec_view; personal records go through
  dbtrace.load_visible on every request.
render_text(envelope) gives the CLI / MCP text of the same view.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time

from . import common
from . import dbaccess
from . import dbflows
from . import dbmodel
from . import dbstore

SCHEMA_VERSION = 1
MAX_BYTES = 64 * 1024
PAGE = 50
LIST_CAP = 50
NAME_RX = re.compile(r"^[A-Za-z0-9_$#@][A-Za-z0-9_$#@ .:-]{0,199}$")
SECRET_FILE = "db-view.key"
_PROCESS_KEY = secrets.token_bytes(32)
STATES = {
    "no_database_mcp": ("No database MCP configured", "Name the database MCP server in /cg:setup (mcp.database.server)."),
    "no_selected_connection": ("No connection selected", "Add a connection with its environment to mcp.database.scope "
                                                         "(/cg:setup or the config file); the dashboard only shows it."),
    "unknown_environment": ("Environment unknown", "Give the connection an explicit env in mcp.database.scope; a "
                                                   "connection without one stays blocked."),
    "missing_identity": ("Your identity is not confirmed", "Confirm your commit email on the Profile page; personal "
                                                           "investigations need it."),
    "ambiguous_object": ("Several objects match", "Pick one of the candidates; nothing is chosen for you."),
    "missing_metadata": ("No metadata yet", "Refresh the schema cards (the background job) or describe the table, then "
                                            "run cli db ingest."),
    "unsupported_capability": ("Not available from this connector", "Use the fallback shown; nothing is assumed."),
    "permission_denied": ("Access denied at last check", "It is checked again at the next metadata refresh; ask for "
                                                         "read access if you need it."),
    "partial_pages": ("Partial metadata", "More objects are available: run the metadata discovery again to continue."),
    "disconnected_source": ("The database MCP is not reachable", "Reconnect it (claude mcp list, /cg:doctor); cached "
                                                                 "metadata stays readable."),
    "stale_flow": ("Saved flow may be stale", "The evidence changed after this version: rebuild it with cli db flow."),
}
# D3: the states of a cached impact / comparison result (a read never recomputes it); apart from the 11 required ones
RESULT_STATES = {
    "not_computed": ("No cached result yet", "Compute it with cli db impact <target> or cli db compare <left> <right> "
                                             "(cached metadata only, no query)."),
    "stale_result": ("Computed before the evidence changed", "Recompute it with --refresh (cached metadata only, no "
                                                             "query)."),
    "scope_changed": ("Computed under another access scope", "Its items are hidden; recompute it with --refresh."),
    "blocked_comparison": ("Comparison blocked", "Approving a connection or environment happens only in /cg:setup or "
                                                 "the config file; nothing here widens them."),
}


class CursorError(ValueError):
    """A cursor that is malformed, issued to someone else or for another policy, or for data that changed."""


class ViewError(ValueError):
    """A bad view argument (not an ID or a name): refused before anything is read."""


# --------------------------------------------------------------------------- secret, snapshot, generations

def secret_path():
    return common.wl(SECRET_FILE)


def ensure_secret():
    """The per-home cursor key, written once by ingestion (a write path), never by a read."""
    p = secret_path()
    if not os.path.isfile(p):
        common.write_text(p, secrets.token_hex(32) + "\n")


def _secret():
    v = common.read_text(secret_path()).strip()
    return bytes.fromhex(v) if re.fullmatch(r"[0-9a-f]{64}", v or "") else _PROCESS_KEY


def snapshot():
    """(source revision, snapshot_at): what the stored records look like now (graph files, flow records, sources,
    quarantine, capabilities). A cursor taken from another snapshot is stale."""
    parts, newest = [], 0
    paths = [dbstore.graph_path(c) for c in dbstore.GRAPH] + [dbstore.file_path(f) for f in ("sources", "quarantine",
                                                                                              "capabilities")]
    fd = os.path.join(dbstore.root(), "flows")
    try:
        paths += [os.path.join(fd, f) for f in sorted(os.listdir(fd))[:1000]]
    except OSError:
        pass
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        parts.append([os.path.basename(p), st.st_mtime_ns, st.st_size])
        newest = max(newest, st.st_mtime_ns)
    rev = hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]
    import datetime as _dt
    at = _dt.datetime.fromtimestamp(newest / 1e9).astimezone().isoformat(timespec="seconds") if newest else ""
    return rev, at


def scope_generation(ctx):
    return dbmodel.fingerprint({"connections": ctx.connections, "repos": ctx.repos, "aliases": ctx.aliases})[:16]


# --------------------------------------------------------------------------- cursors

def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _mac(ctx, body):
    msg = "%s|%s|%s" % (body, ctx.principal_id or "", ctx.policy_generation)
    return hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def make_cursor(ctx, view, ref, offset, snap, extra=None):
    body = _b64(json.dumps({"v": view, "r": ref, "o": offset, "s": snap, "x": extra or {}}, separators=(",", ":"),
                           sort_keys=True).encode())
    return body + "." + _mac(ctx, body)


def read_cursor(ctx, token, view, ref, snap):
    """The cursor's state {offset, extra}, or CursorError."""
    if token in (None, ""):
        return {"offset": 0, "extra": {}}
    if not isinstance(token, str) or len(token) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[0-9a-f]{32}", token):
        raise CursorError("malformed cursor")
    body, mac = token.rsplit(".", 1)
    if not hmac.compare_digest(mac, _mac(ctx, body)):
        raise CursorError("this cursor was not issued to you under the current access policy; start again")
    try:
        st = json.loads(_unb64(body))
    except (ValueError, TypeError):
        raise CursorError("malformed cursor")
    if not isinstance(st, dict) or st.get("v") != view or st.get("r") != ref:
        raise CursorError("this cursor belongs to another view")
    if st.get("s") != snap:
        raise CursorError("stale cursor: the records changed since the first page; start again")
    off = st.get("o")
    if isinstance(off, bool) or not isinstance(off, int) or off < 0:
        raise CursorError("malformed cursor")
    return {"offset": off, "extra": st.get("x") if isinstance(st.get("x"), dict) else {}}


# --------------------------------------------------------------------------- arguments

def check_id(value, prefix=None):
    if not dbmodel.valid_id(value, prefix):
        raise ViewError("not a record ID")
    return value


def check_name(value):
    v = " ".join(str(value or "").split())
    if not v or not NAME_RX.match(v) or "--" in v or re.search(r"[/\\;'\"`*=(){}<>|]", v) or \
            v.lower().startswith("mcp__") or re.match(r"(?i)^(select|with|exec|insert|update|delete)\s", v):
        raise ViewError("give a ticket key, a name like schema.table or an ID (no paths, SQL or tool names)")
    return v


# --------------------------------------------------------------------------- the envelope and pages

def _size(x):
    return len(json.dumps(x, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def envelope(ctx, kind, vid, data=None, items=(), more=False, cursor=None, environment="", coverage=None,
             freshness=None, capabilities=None, state=None, states=None, links=None, snap=None):
    rev, at = snap or snapshot()
    return {"schema_version": SCHEMA_VERSION, "kind": kind, "id": vid, "profile_generation": ctx.profile_generation,
            "policy_generation": ctx.policy_generation, "scope_generation": scope_generation(ctx),
            "environment": environment, "snapshot_at": at, "source_revision": rev, "coverage": coverage,
            "freshness": freshness, "capabilities": capabilities or {}, "state": state, "states": states or [],
            "data": data, "items": list(items), "more_available": bool(more), "cursor": cursor, "links": links or {}}


def paged(ctx, env, view, ref, all_items, token, snap, page=PAGE):
    """Fill env["items"] from all_items at the cursor's offset: at most `page` items and MAX_BYTES for the whole
    envelope; the rest gets a cursor. An item larger than the budget alone becomes an explicit stub."""
    st = read_cursor(ctx, token, view, ref, snap[0])
    off = st["offset"]
    if off > len(all_items):
        raise CursorError("stale cursor: the list is shorter now; start again")
    base = _size(env) + 400  # room for the cursor and the flags
    budget = MAX_BYTES - base
    items, used, i = [], 0, off
    while i < len(all_items) and len(items) < page:
        it = all_items[i]
        sz = _size(it) + 1
        if sz > budget and not items:
            it = {"id": it.get("id") if isinstance(it, dict) else None, "truncated": True,
                  "reason": "this item is larger than one page (%d KiB); open it by its ID" % (sz // 1024)}
            sz = _size(it) + 1
        if used + sz > budget:
            break
        items.append(it)
        used += sz
        i += 1
    env["items"] = items
    env["more_available"] = i < len(all_items)
    env["cursor"] = make_cursor(ctx, view, ref, i, snap[0]) if i < len(all_items) else None
    env["page"] = {"offset": off, "count": len(items), "total": len(all_items)}
    return env


def _cap(lst, n=LIST_CAP):
    lst = list(lst or [])
    return lst[:n], max(0, len(lst) - n)


def _state(sid, detail=""):
    title, nxt = STATES[sid] if sid in STATES else RESULT_STATES[sid]
    return {"id": sid, "title": title, "text": detail or title, "next": nxt}


# --------------------------------------------------------------------------- views

def _caps_for(conns):
    from . import dbconnector
    rec = dbconnector.load_capabilities()
    out = {}
    for c in conns:
        caps = ((rec.get("connections") or {}).get(c) or {}).get("capabilities") or {}
        out[c] = {k: {"state": v.get("state"), "label": dbconnector.STATE_LABEL.get(v.get("state"), ""),
                      "reason": v.get("reason") or ""} for k, v in caps.items() if isinstance(v, dict)}
    return out


def table_view(ctx, ref, env=None, cursor=None, cfg=None):
    """The passport of one object by ID or explicit name; a name several objects share answers with candidates."""
    snap = snapshot()
    vis = dbflows.Visible(ctx, cfg=cfg)
    if isinstance(ref, str) and dbmodel.valid_id(ref):
        oid = check_id(ref, "o")
    else:
        name = check_name(ref)
        r = dbflows.resolve_start(ctx, "table", name, vis=vis, cfg=cfg)
        cands = r["candidates"]
        if env:
            cands = [c for c in cands if str(c.get("environment") or "").lower() == str(env).lower()]
        if len(cands) != 1:
            st = _state("ambiguous_object", "%d objects match %r" % (len(cands), name)) if cands else \
                _state("missing_metadata", "Nothing recorded matches %r" % name)
            e = envelope(ctx, "candidates", None, data={"query": name, "kind": "table"}, state=st["id"],
                         states=[st], snap=snap)
            return paged(ctx, e, "candidates", "table:" + name, cands, cursor, snap)
        oid = cands[0]["id"]
    try:
        p = dbflows.passport(ctx, oid, vis=vis, cfg=cfg)
    except dbflows.NotFound:
        raise dbflows.NotFound("no such object")
    cols = p.pop("columns")
    data = dict(p)
    for k in ("readers", "writers", "routines", "triggers", "flows"):
        data[k], data[k + "_more"] = _cap(data[k])
    for lay in ("structure", "flow"):
        data["relationships"][lay], more = _cap(data["relationships"][lay])
        data["relationships"][lay + "_more"] = more
    data["columns_count"] = len(cols)
    states = []
    if p["sources"]["description"]["page"] == "partial":
        states.append(_state("partial_pages", "This object was described from a partial page."))
    if not p["described"]:
        states.append(_state("missing_metadata", "No describe result for this object yet."))
    conn = p["display"]["connection"]
    e = envelope(ctx, "table", oid, data=data, environment=str(p["environment"] or "").upper(),
                 coverage={k: v["coverage"] for k, v in p["sources"].items()},
                 freshness={k: v["freshness"] for k, v in p["sources"].items()},
                 capabilities=_caps_for([conn]).get(conn, {}), state=states[0]["id"] if states else "ok",
                 states=states, links={"self": "/api/db/passport?id=%s" % oid}, snap=snap)
    return paged(ctx, e, "table", oid, cols, cursor, snap)


def _flow_visible_filter(vis, flow):
    """A saved flow re-projected through today's access: steps whose objects you may no longer see are dropped (and
    counted, without names)."""
    keep, dropped = [], 0
    for s in flow.get("steps") or []:
        if all(n in vis.nodes for n in s.get("nodes") or []):
            keep.append(s)
        else:
            dropped += 1
    ids = {s.get("edge") for s in keep}
    edges = [e for e in flow.get("edges") or [] if e["id"] in ids and e["id"] in vis.edges]
    return dict(flow, steps=keep, edges=edges, nodes=[n for n in flow.get("nodes") or [] if n["id"] in vis.nodes],
                lineage=[x for x in flow.get("lineage") or [] if x.get("edge") in vis.edges]), dropped


def _graph_for(steps, flow):
    """The page's graph: exactly the edges its steps stand on, and their nodes (the list and the graph tell the same
    story, DBT-33)."""
    eids = [s["edge"] for s in steps if s.get("edge")]
    edges = [e for e in flow.get("edges") or [] if e["id"] in eids]
    nids = {n for s in steps for n in s.get("nodes") or []} | {e["source"] for e in edges} | {e["target"] for e in edges}
    nodes = [n for n in flow.get("nodes") or [] if n["id"] in nids]
    return {"nodes": [{k: n.get(k) for k in ("id", "kind", "label", "connection", "environment")} for n in nodes],
            "edges": [{"id": e["id"], "kind": e["kind"], "label": e.get("label"), "source": e["source"],
                       "target": e["target"], "basis": e.get("basis"), "basis_label": e.get("basis_label"),
                       "transport": e.get("transport"), "step": next(s["id"] for s in steps if s.get("edge") == e["id"]),
                       "stage": next(s.get("stage") for s in steps if s.get("edge") == e["id"]),
                       "group_label": next(s.get("group_label") for s in steps if s.get("edge") == e["id"])}
                      for e in edges]}


def flow_view(ctx, fid=None, start=None, env=None, cursor=None, cfg=None):
    """A saved flow by ID (re-projected through today's access; marked stale when the evidence changed since), or a
    flow computed in memory from a start (nothing saved)."""
    snap = snapshot()
    vis = dbflows.Visible(ctx, cfg=cfg)
    states, saved = [], None
    if fid is not None:
        check_id(fid, "f")
        rec = dbflows.saved_flow(fid)
        if not rec or not rec.get("current") or not dbflows._flow_visible(vis, rec):
            raise dbflows.NotFound("no such flow")
        flow, dropped = _flow_visible_filter(vis, rec["current"])
        if not flow["steps"] and (rec["current"].get("steps") or []):
            raise dbflows.NotFound("no such flow")
        saved = rec
        if dropped:
            states.append(_state("permission_denied", "%d step(s) of this saved flow are outside your access now."
                                 % dropped))
        from . import dbimpact
        drift = dbimpact.flow_drift(ctx, rec)  # the sources it recorded vs today's: no rebuild on a read (DB-16)
        if drift["state"] == "needs_review":
            states.append(_state("stale_flow", "%s since version %s: %s." % (
                dbimpact.REVIEW_LABEL, rec["current"].get("version"), "; ".join(drift["reasons"][:3]))))
    else:
        if not isinstance(start, dict):
            raise ViewError("give a flow ID or a start")
        if start.get("kind") == "ticket":
            from .dashboard import api
            if not api.TICKET_RE.match(str(start.get("key") or "").upper()):
                raise ViewError("not a ticket key")
        else:
            check_id(start.get("id"))
        flow = dbflows.build_flow(ctx, start, env=env, vis=vis, cfg=cfg)
    ref = flow["id"]
    steps = flow["steps"]
    data = {k: flow.get(k) for k in ("title", "label", "start", "environment", "connections", "scope_line", "coverage",
                                     "freshness", "version", "built_at")}
    data["saved"] = saved is not None
    data["review"] = drift if saved is not None else None
    data["notes"] = (saved or {}).get("notes") or ""
    data["versions"] = [{"version": v.get("version"), "built_at": v.get("built_at")} for v in
                        (saved or {}).get("versions") or []][-10:]
    data["gaps"], data["gaps_more"] = _cap(flow.get("gaps"))
    data["lineage"], data["lineage_more"] = _cap(flow.get("lineage"), 30)
    data["steps_total"] = len(steps)
    e = envelope(ctx, "flow", ref, data=data, environment=flow.get("environment") or "",
                 coverage=flow.get("coverage"), freshness=flow.get("freshness"),
                 capabilities=_caps_for(flow.get("connections") or []), state=states[0]["id"] if states else "ok",
                 states=states, links={"self": "/api/db/flow?id=%s" % ref}, snap=snap)
    e = paged(ctx, e, "flow", ref, steps, cursor, snap)
    e["data"]["graph"] = _graph_for(e["items"], flow)
    if _size(e) > MAX_BYTES:  # the graph is per page; a page this large is cut to fewer steps, never silently
        e = paged(ctx, dict(e, data=dict(data)), "flow", ref, steps, cursor, snap, page=max(1, len(e["items"]) // 2))
        e["data"]["graph"] = _graph_for(e["items"], flow)
    return e


def flows_list(ctx, cursor=None, cfg=None):
    from . import dbimpact
    snap = snapshot()
    vis = dbflows.Visible(ctx, cfg=cfg)
    rev = dbstore.sources_revision()
    srcs = dbimpact.sources_now()
    items = []
    for rec in dbstore.iter_records("flows", limit=1000):
        cur = rec.get("current") or {}
        if not cur or not dbflows._flow_visible(vis, rec):
            continue
        if any(c not in ctx.connections for c in cur.get("connections") or []):
            continue  # a connection it names is no longer approved: the flow is not listed
        st = rec.get("start") or {}
        items.append({"id": rec["id"], "title": cur.get("title"), "start": {"kind": st.get("kind"),
                                                                             "key": st.get("key"), "id": st.get("id")},
                      "version": cur.get("version"), "environment": cur.get("environment"),
                      "scope_line": cur.get("scope_line"), "built_at": cur.get("built_at"),
                      "updated_at": rec.get("updated_at"),
                      "stale": dbimpact.flow_drift(ctx, rec, srcs, rev)["state"] == "needs_review"})
    items.sort(key=lambda x: (str(x.get("updated_at") or ""), x["id"]), reverse=True)
    stale = [i for i in items if i["stale"]]
    states = [_state("stale_flow", "%d saved flow(s) may be stale" % len(stale))] if stale else []
    e = envelope(ctx, "flows", None, data={"count": len(items)}, states=states,
                 state=states[0]["id"] if states else "ok", snap=snap)
    return paged(ctx, e, "flows", "all", items, cursor, snap)


def resolve(ctx, text, kind=None, cursor=None, cfg=None):
    snap = snapshot()
    name = check_name(text)
    if kind not in (None, "") and kind not in dbflows.START_KINDS:
        raise ViewError("kind is one of %s" % ", ".join(dbflows.START_KINDS))
    r = dbflows.resolve_start(ctx, kind or None, name, cfg=cfg, limit=200)
    states = []
    if r["state"] == "ambiguous":
        states.append(_state("ambiguous_object", "%d candidates for %r" % (len(r["candidates"]) + r["more"], name)))
    elif r["state"] == "none":
        states.append(_state("missing_metadata", r.get("reason") or "Nothing recorded matches"))
    e = envelope(ctx, "candidates", None, data={"query": name, "kind": kind or "", "state": r["state"],
                                                "start": r["start"], "reason": r.get("reason") or ""},
                 state=r["state"], states=states, snap=snap)
    return paged(ctx, e, "resolve", "%s:%s" % (kind or "", name), r["candidates"], cursor, snap)


def workspace_summary(ctx, cfg=None, health=None):
    """The workspace header: environment label, connections and their states, coverage / freshness line, and every
    required state that applies now with its next step."""
    from . import dbconnector, mcpjobs
    cfg = common.load_config() if cfg is None else cfg
    snap = snapshot()
    srv = mcpjobs.server_name(cfg, "database")
    sc = ctx.scope
    states = []
    if not srv:
        states.append(_state("no_database_mcp"))
    if not ctx.connections:
        states.append(_state("no_selected_connection"))
    unknown = [c for c, why in sc["blocked"].items() if why == "environment unknown" or "explicit environment" in why]
    if unknown:
        states.append(_state("unknown_environment", "No environment for %s" % ", ".join(sorted(unknown))))
    if not ctx.principal_id:
        states.append(_state("missing_identity"))
    g = dbstore.load_graph()
    described = {}
    partial = 0
    for n in g["nodes"].values():
        d, a = n.get("display") or {}, n.get("attrs") or {}
        if n.get("kind") in ("table", "view") and d.get("connection") in ctx.connections and a.get("described"):
            described[d["connection"]] = described.get(d["connection"], 0) + 1
            if ((a.get("sources") or {}).get("describe") or {}).get("page") == "partial" or \
                    ((a.get("sources") or {}).get("writers") or {}).get("truncated"):
                partial += 1
    bare = [c for c in ctx.connections if not described.get(c)]
    if bare:
        states.append(_state("missing_metadata", "No described objects yet on %s" % ", ".join(sorted(bare))))
    caps = dbconnector.load_capabilities()
    uns = sorted({k for c in ctx.connections for k, v in (((caps.get("connections") or {}).get(c) or {}).get(
        "capabilities") or {}).items() if isinstance(v, dict) and v.get("state") == "unsupported"})
    if uns:
        states.append(_state("unsupported_capability", "Not available: %s" % ", ".join(x.replace("_", " ") for x in uns)))
    denied = [c for c in ctx.connections if any(isinstance(q, dict) and q.get("connection") == c for q in
                                                 ctx.quarantine.values())]
    if denied:
        states.append(_state("permission_denied", "Access denied at the last check on part of %s" %
                             ", ".join(sorted(denied))))
    if partial:
        states.append(_state("partial_pages", "%d object(s) were read from a partial page or hit a row limit" % partial))
    health = health if health is not None else (common.read_json(common.wl("health.json"), {}) or {})
    if srv and ((health.get("mcp") or {}).get("offline") or {}).get(srv):
        states.append(_state("disconnected_source", "The database MCP was unreachable at %s" %
                             str(health["mcp"]["offline"][srv])[:16]))
    fl = flows_list(ctx, cfg=cfg)
    if fl["states"]:
        states += fl["states"]
    envs = sorted({e["env"] for e in ctx.connections.values()})
    conns = [{"id": c, "env": e["env"], "engine": e.get("engine") or "", "schemas": list(e.get("schemas") or []),
              "described": described.get(c, 0), "authorization": dbaccess.authorization(ctx, c)["label"]}
             for c, e in sorted(ctx.connections.items())]
    srcs = dbstore.read_file("sources").get("sources") or {}
    last = max([str(v.get("at") or "") for v in srcs.values() if isinstance(v, dict)] or [""])
    line = "%d connection%s · %d object%s described · %d saved flow%s%s" % (
        len(conns), "" if len(conns) == 1 else "s", sum(described.values()), "" if sum(described.values()) == 1 else "s",
        fl["data"]["count"], "" if fl["data"]["count"] == 1 else "s", " · last ingested %s" % last[:16] if last else "")
    data = {"environment": "/".join(x.upper() for x in envs) or "No environment", "connections": conns,
            "blocked": [{"id": c, "reason": why} for c, why in sorted(sc["blocked"].items())],
            "repos": [os.path.basename(str(r).rstrip("/\\")) for r in sc["repo_roots"].values()],
            "scope_provenance": sc["provenance"], "coverage_line": line, "server_configured": bool(srv),
            "identity": bool(ctx.principal_id), "flows": fl["items"][:10],
            "scope_note": "Scope is edited in /cg:setup or the config file (mcp.database.scope); the dashboard shows it."}
    return envelope(ctx, "workspace", None, data=data, environment=data["environment"], states=states,
                    capabilities=_caps_for(sorted(ctx.connections)),
                    state=states[0]["id"] if states else "ok", snap=snap)


# --------------------------------------------------------------------------- record investigations (D2)
# Personal records: dbtrace.load_visible decides on every request (yours, connections approved now); a record of
# someone else answers exactly like one that doesn't exist. These views only read: none of them queries, enqueues,
# reaps or writes anything.

PLAN_STEP_FIELDS = ("id", "n", "role", "object_label", "connection", "environment", "columns", "key_columns", "retained",
                    "masked", "never_selected", "predicate_shape", "mapping", "basis_label", "limit", "stage", "status",
                    "reason", "next", "async_boundary", "depends_on")


def _plan_step(s):
    out = {k: s.get(k) for k in PLAN_STEP_FIELDS}
    out["status_label"] = "Planned" if s.get("status") == "planned" else dbmodel.STEP_LABEL.get(s.get("status"), "")
    return out


def plan_view(ctx, plan_id, cursor=None):
    """A reviewed plan: targets, columns, predicate shapes (values masked), limits, coverage estimate, data handling,
    retention, the transport disclosure and the capability limits; the fingerprint a run must echo."""
    from . import dbtrace
    check_id(plan_id, "p")
    snap = snapshot()
    p = dbtrace.load_visible(ctx, "plans", plan_id)
    keep = os.path.isfile(dbstore.plan_key_file(plan_id))
    data = {k: p.get(k) for k in ("flow", "environment", "connections", "start", "key_note", "limits", "capabilities",
                                  "coverage_estimate", "data_handling", "retention", "transport_disclosure",
                                  "fingerprint", "queries_planned", "investigation", "active_job", "created_at")}
    data.update(key_kept=keep, key_kept_until=p.get("key_kept_until") if keep else "",
                runnable=keep and bool(p.get("queries_planned")))
    e = envelope(ctx, "plan", plan_id, data=data, environment=str(p.get("environment") or "").upper(),
                 coverage={"estimate": p.get("coverage_estimate")}, capabilities=p.get("capabilities") or {},
                 state="ok" if keep else "key_not_kept", links={"self": "/api/db/plan?id=%s" % plan_id}, snap=snap)
    return paged(ctx, e, "plan", plan_id, [_plan_step(s) for s in p.get("steps") or []], cursor, snap)


def job_view(ctx, job_id):
    """A job's local state (a read of its record: never enqueues, never runs, never queries)."""
    from . import dbtrace
    check_id(job_id, "j")
    j = dbtrace.load_visible(ctx, "jobs", job_id)
    st = j.get("state")
    words = {"queued": "Queued: waiting for the runner", "running": "Running",
             "cancel_requested": "Cancellation requested: no new call will be made"}
    data = {k: j.get(k) for k in ("plan", "investigation", "environment", "connections", "created_at", "started_at",
                                  "ended_at", "budget", "phases", "accounting", "in_flight", "error", "retry_policy",
                                  "retries", "run", "observations")}
    data.update(state=st, state_label=dbmodel.JOB_LABEL.get(st, st), terminal=st in dbtrace.TERMINAL,
                poll=st in dbtrace.ACTIVE, text=words.get(st) or dbmodel.JOB_LABEL.get(st, st))
    if st == "queued" and j.get("queued_epoch") and time.time() - float(j["queued_epoch"]) > 60:
        data["text"] += " (the runner hasn't started yet; it ends as failed after %d minutes)" % (
            dbtrace.QUEUE_MAX_S // 60)
    return envelope(ctx, "job", job_id, data=data, environment=str(j.get("environment") or "").upper(), state=st,
                    links={"self": "/api/db/job?id=%s" % job_id})


def _focus(inv, flow_gaps=()):
    from . import dbtrace
    run = dbtrace.latest(inv)
    le = dbtrace.last_evidenced(run)
    nxt = (run or {}).get("next_checks") or []
    return {"last_observed": "%s (%s)" % (le["object_label"], le["observed_at"]) if le else "Nothing observed yet",
            "unresolved": dbtrace.largest_gap(run, flow_gaps) or "None recorded",
            "next": nxt[0] if nxt else (inv.get("next_action") or "Review a trace plan for this record")}


def investigation_view(ctx, inv_id, cursor=None):
    """One investigation: objective, scope, flow, runs (each with its state, window, unknowns and next checks) and
    their observations (items, newest run first), changes between real observations, notes and the focus line."""
    from . import dbtrace
    check_id(inv_id, "i")
    snap = snapshot()
    inv = dbtrace.load_visible(ctx, "investigations", inv_id)
    runs = list(inv.get("runs") or [])
    data = {k: inv.get(k) for k in ("objective", "scope", "flow", "start", "key_note", "ticket", "corrections",
                                    "superseded_by", "plans", "unknowns", "next_action", "created_at", "updated_at")}
    data["notes"] = {k: (inv.get("notes") or {}).get(k) for k in ("text", "revision", "at")}
    data["runs"] = [{k: r.get(k) for k in ("n", "job", "plan", "state", "state_label", "started_at", "ended_at",
                                           "window", "unknowns", "next_checks", "in_flight", "connections")}
                    for r in runs[-20:]]
    for r, full in zip(data["runs"], runs[-20:]):
        r["counts"] = {}
        for o in full.get("observations") or []:
            r["counts"][o["status"]] = r["counts"].get(o["status"], 0) + 1
    data["runs_total"] = len(runs)
    data["changes"] = dbtrace.changes(inv)[-30:]
    data["focus"] = _focus(inv)
    data["handoff"] = {"versions": len((inv.get("handoff") or {}).get("generated") or []),
                       "edited": bool((inv.get("handoff") or {}).get("edited")),
                       "source_changed": bool((inv.get("handoff") or {}).get("source_changed"))}
    items = [dict(o, run=r["n"]) for r in reversed(runs) for o in r.get("observations") or []]
    e = envelope(ctx, "investigation", inv_id, data=data, environment=str((inv.get("scope") or {}).get(
        "environment") or "").upper(), coverage={"runs": len(runs)}, state="ok",
        links={"self": "/api/db/investigation?id=%s" % inv_id, "handoff": "/api/db/handoff?id=%s" % inv_id}, snap=snap)
    return paged(ctx, e, "investigation", inv_id, items, cursor, snap)


def investigations_list(ctx, q=None, cursor=None):
    """Your investigations, newest first; `q` filters by objective, flow, ticket or your notes. Another person's are
    not listed, counted or hinted at."""
    from . import dbtrace
    snap = snapshot()
    q = " ".join(str(q or "").split()).casefold()[:200]
    items = []
    for rec in dbstore.iter_records("investigations", limit=1000):
        if not dbaccess.decide(ctx, rec)["allowed"]:
            continue
        hay = " ".join(str(x or "") for x in (rec.get("objective"), (rec.get("flow") or {}).get("title"),
                                              rec.get("ticket"), (rec.get("notes") or {}).get("text"))).casefold()
        if q and q not in hay:
            continue
        run = dbtrace.latest(rec)
        items.append({"id": rec["id"], "objective": rec.get("objective"), "flow": (rec.get("flow") or {}).get("title"),
                      "environment": str((rec.get("scope") or {}).get("environment") or "").upper(),
                      "connections": (rec.get("scope") or {}).get("connections") or [], "ticket": rec.get("ticket"),
                      "runs": len(rec.get("runs") or []), "last_state": (run or {}).get("state_label") or "No run yet",
                      "updated_at": rec.get("updated_at") or "", "next_action": rec.get("next_action") or ""})
    items.sort(key=lambda x: (x["updated_at"], x["id"]), reverse=True)
    e = envelope(ctx, "investigations", None, data={"count": len(items), "query": q}, state="ok" if ctx.principal_id
                 else "missing_identity", states=[] if ctx.principal_id else [_state("missing_identity")], snap=snap)
    return paged(ctx, e, "investigations", "all:" + q, items, cursor, snap)


def handoff_view(ctx, inv_id):
    """The local handoff draft: the latest generated version, your edited text (kept apart), whether the source
    evidence changed since your edit, and the redacted copy text. Nothing is sent anywhere."""
    from . import dbtrace
    check_id(inv_id, "i")
    inv = dbtrace.load_visible(ctx, "investigations", inv_id)
    h = inv.get("handoff") or {}
    gen = h.get("generated") or []
    data = {"generated": ({k: gen[-1].get(k) for k in ("version", "text", "at")} if gen else None),
            "versions": len(gen), "edited": h.get("edited"), "source_changed": bool(h.get("source_changed")),
            "source_changed_text": "Source evidence changed after your edit" if h.get("source_changed") else "",
            "preview": dbtrace.handoff_text(inv) if not gen else "", "copy_text": dbtrace.handoff_copy(ctx, inv_id),
            "notice": "A local draft: copying it sends nothing anywhere; the copy text is redacted for a team."}
    return envelope(ctx, "handoff", inv_id, data=data, environment=str((inv.get("scope") or {}).get("environment") or
                                                                       "").upper(),
                    links={"investigation": "/api/db/investigation?id=%s" % inv_id})


def context_view(ctx, flow_id=None, investigation_id=None, cfg=None):
    """The companion's database_context (dbtrace.database_context) in the envelope: data.database_context is the
    bounded object or None with data.reason."""
    from . import dbtrace
    if flow_id:
        check_id(flow_id, "f")
    if investigation_id:
        check_id(investigation_id, "i")
    c = dbtrace.database_context(ctx, flow_id, investigation_id, cfg=cfg)
    dc = c.get("database_context") or {}
    return envelope(ctx, "database_context", investigation_id or flow_id, data=c, environment=dc.get("environment")
                    or "", freshness=dc.get("freshness"), state="ok" if c.get("database_context") else "none")


def key_spec_view(ctx, flow_id=None, start=None, env=None, cfg=None):
    """What a trace plan for a flow (saved, or computed in memory from a start) would ask for (dbtrace.key_spec):
    nothing saved, nothing queried."""
    from . import dbtrace
    if flow_id is not None:
        check_id(flow_id, "f")
    elif not isinstance(start, dict) or (start.get("kind") != "ticket" and not dbmodel.valid_id(start.get("id"))):
        raise ViewError("give a flow ID or a start")
    data = dbtrace.key_spec(ctx, flow_id, start=start, env=env, cfg=cfg)
    return envelope(ctx, "key_spec", data["flow"]["id"], data=data, capabilities=data.get("capabilities") or {},
                    states=[] if ctx.principal_id else [_state("missing_identity")],
                    state="ok" if ctx.principal_id else "missing_identity")


# --------------------------------------------------------------------------- text (CLI and MCP)

def _cell(s, n=160):
    s = " ".join(str(s if s is not None else "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _states_text(env):
    return ["- %s: %s Next: %s" % (s["title"], s["text"], s["next"]) for s in env.get("states") or []]


def _caps_text(env):
    """The capabilities that are not observed as supported, with their reasons (DBT-38: never a silent "works")."""
    L = []
    caps_by = env.get("capabilities") or {}
    if caps_by and all(isinstance(v, dict) and "state" in v for v in caps_by.values()):  # one connection's (table)
        caps_by = {((env.get("data") or {}).get("display") or {}).get("connection") or "this connection": caps_by}
    for conn, caps in sorted(caps_by.items()):
        if not isinstance(caps, dict):
            continue
        for k, v in sorted(caps.items()):
            if isinstance(v, dict) and v.get("state") != "supported":
                L.append("- %s on %s: %s%s" % (k.replace("_", " "), conn, v.get("label") or v.get("state"),
                                              " (%s)" % v["reason"] if v.get("reason") else ""))
    return ["", "## Capabilities not verified or not available"] + L[:20] if L else []


def render_text(env):
    """Plain Markdown of a view, the same content the dashboard shows."""
    k, d = env.get("kind"), env.get("data") or {}
    L = []
    if k == "table":
        L.append("# %s · %s (%s, %s)" % (d["name"], d.get("kind_label"), d["display"].get("connection"),
                                         env.get("environment") or "?"))
        pu = d.get("purpose") or {}
        L.append("Purpose: %s" % ((pu.get("text") or "") + " (" + pu.get("label") + ")" if pu.get("text") else
                                  "Unconfirmed meaning"))
        if d.get("lifecycle") != "current":
            L.append("Lifecycle: %s%s" % (d.get("lifecycle"), " since %s" % d["missing_since"][:10]
                                          if d.get("missing_since") else ""))
        L += _states_text(env)
        L += _caps_text(env)
        L += ["", "## Columns (%d)" % d.get("columns_count", 0)]
        for c in env.get("items") or []:
            if c.get("truncated"):
                L.append("- (%s)" % c.get("reason"))
                continue
            flags = ", ".join(x for x in ("primary key" if c.get("primary_key") else "",
                                          "foreign key" if c.get("foreign_key") else "",
                                          "personal data" if c.get("sensitive") else "") if x)
            L.append("- %s %s%s%s%s" % (c["name"], c.get("type") or "?", "" if c.get("nullable") is not False else
                                        " not null", " default " + _cell(c["default"], 60) if c.get("default") else "",
                                        " · " + flags if flags else ""))
        if env.get("more_available"):
            L.append("… more columns: cursor=%s" % env.get("cursor"))
        L += ["", "## Readers and writers", d.get("writers_text") or ""]
        for w in d.get("writers") or []:
            L.append("- writes: %s (%s)%s" % (w["name"], w["basis_label"], " columns " + ", ".join(w["columns"])
                                               if w.get("columns") else ""))
        for r in d.get("readers") or []:
            L.append("- reads: %s (%s)" % (r["name"], r["basis_label"]))
        L.append(d.get("owner", {}).get("text") or "")
        if d.get("routines") or d.get("triggers"):
            L += ["", "## Routines and triggers"]
            L += ["- %s%s" % (r["name"], " (definition unavailable)" if r.get("definition") == "unavailable" else "")
                  for r in d.get("routines") or []]
            L += ["- trigger %s %s" % (t["name"], t.get("event") or "") for t in d.get("triggers") or []]
        for lay, title in (("structure", "Structure"), ("flow", "Flow")):
            rel = (d.get("relationships") or {}).get(lay) or []
            if rel:
                L += ["", "## Relationships: %s" % title]
                L += ["- %s %s → %s (%s)" % (r["label"], r["source_label"], r["target_label"], r["basis_label"])
                      for r in rel]
        L += ["", "## Sources"]
        for s in (d.get("sources") or {}).values():
            L.append("- %s: %s · %s · %s%s%s" % (s["what"], s["text"], s["coverage_label"], s["freshness_label"],
                                                 " · last success %s" % s["last_success"][:16] if s["last_success"]
                                                 else "", " · error: %s" % s["error"] if s["error"] else ""))
        if d.get("flows"):
            L += ["", "Related flows: " + ", ".join("%s (%s)" % (f["title"], f["id"]) for f in d["flows"])]
        if d.get("authorization"):
            L.append("Access: %s" % d["authorization"]["label"])
        L.append(d.get("disclaimer") or "")
    elif k == "flow":
        L.append("# %s" % (d.get("title") or "Flow"))
        L.append("%s · %s · %s" % (d.get("label"), env.get("environment") or "?", d.get("scope_line")))
        if d.get("saved"):
            L.append("Saved flow version %s (%s)" % (d.get("version"), env.get("id")))
        L += _states_text(env)
        L += ["", "## Steps", "Numbers are stages ordered by evidence; a separate path starts again at 1."]
        group = None
        for s in env.get("items") or []:
            if s.get("truncated"):
                L.append("- (%s)" % s.get("reason"))
                continue
            if s.get("group") != group:
                group = s.get("group")
                L += ["", "### %s" % (s.get("group_label") or "Steps")]
            n = "%d.%s" % (s["stage"], " (same stage, no order between them)" if s.get("stage_shared") else "") \
                if s.get("stage") is not None else "-"
            cite = "; ".join(v.get("cite") or v.get("text") or "" for v in s.get("evidence") or [])[:200]
            L.append("%s [%s] %s · %s%s%s%s" % (n, s["kind_label"], _cell(s["label"], 140), s["basis_label"],
                                               " · async" if s.get("async") else "",
                                               " · %s" % PATH_TEXT.get(s.get("path"), s.get("path")) if s.get("path_known")
                                               else "", " · " + cite if cite else ""))
            for it in s.get("lineage") or []:
                L.append("    lineage: %s → %s (%s; %s)" % (", ".join(it["from"]), ", ".join(it["to"]), it["transform"]
                                                            or "as is", it["transport"]))
            if s.get("stage") is None:
                L.append("    (unordered: %s)" % s.get("order_basis"))
        if env.get("more_available"):
            L.append("… more steps: cursor=%s" % env.get("cursor"))
        if d.get("gaps"):
            L += ["", "## Gaps"]
            L += ["- %s: %s Next: %s" % (g["label"], g["text"], g["next"]) for g in d["gaps"]]
    elif k == "flows":
        L.append("# Saved flows (%d)" % (env.get("page") or {}).get("total", 0))
        L += _states_text(env)
        L += ["- %s · v%s · %s · %s%s" % (f["title"], f.get("version"), f.get("environment") or "?", f["id"],
                                          " · may be stale" if f.get("stale") else "") for f in env.get("items") or []]
        if not env.get("items"):
            L.append("No saved flows yet: cli db flow <ticket> builds and saves one.")
    elif k == "candidates":
        L.append("# Candidates for %r" % d.get("query"))
        L += _states_text(env)
        L += ["- %s · %s · %s" % (c["label"], c.get("detail") or c["kind"], c.get("id") or c.get("key") or "")
              for c in env.get("items") or []]
        if not env.get("items"):
            L.append(d.get("reason") or "Nothing recorded matches.")
    elif k == "workspace":
        L.append("# Database investigation · %s" % d.get("environment"))
        L.append(d.get("coverage_line") or "")
        for c in d.get("connections") or []:
            L.append("- %s (%s, %s): %d described · %s" % (c["id"], c["env"].upper(), c["engine"] or "engine unknown",
                                                           c["described"], c["authorization"]))
        for b in d.get("blocked") or []:
            L.append("- blocked %s: %s" % (b["id"], b["reason"]))
        L += _states_text(env)
        L += _caps_text(env)
        L.append(d.get("scope_note") or "")
    elif k in TRACE_TEXT:
        L += TRACE_TEXT[k](env, d)
    return "\n".join(x for x in L if x is not None).rstrip() + "\n"


def _plan_text(env, d):
    L = ["# Trace plan %s · %s" % (env.get("id"), env.get("environment")),
         "Flow: %s (version %s) · connections %s" % ((d.get("flow") or {}).get("title"),
                                                    (d.get("flow") or {}).get("version"),
                                                    ", ".join(d.get("connections") or [])),
         "Start: %s by %s (values not shown; %s)" % ((d.get("start") or {}).get("object_label"),
                                                    ", ".join((d.get("start") or {}).get("fields") or []),
                                                    d.get("key_note")),
         "Limits: %(rows_per_step)d rows/step · %(queries)d queries · %(concurrent)d concurrent · "
         "%(statement_timeout_s)d s statement timeout · %(deadline_s)d s overall · %(value_bytes)d bytes of values"
         % d["limits"], "Coverage estimate: %s" % d.get("coverage_estimate"),
         "Provider limits: %s" % (d.get("capabilities") or {}).get("text"), "", "## Steps"]
    for s in env.get("items") or []:
        if s.get("truncated"):
            L.append("- (%s)" % s.get("reason"))
            continue
        L.append("%d. %s · %s%s" % (s["n"], s["object_label"], s["status_label"], " (stage %d)" % (s["stage"] + 1)
                                    if s.get("status") == "planned" else ""))
        if s.get("status") == "planned":
            L.append("    columns %s · where %s · limit %s" % (", ".join(s.get("columns") or []), s.get(
                "predicate_shape"), s.get("limit")))
            L.append("    mapping: %s%s" % (s.get("mapping") or "", " (%s)" % s["basis_label"] if s.get("basis_label")
                                            else ""))
            L.append("    kept: %s · masked: %s%s" % (", ".join(s.get("retained") or []) or "nothing",
                                                       ", ".join(s.get("masked") or []) or "nothing",
                                                       " · never selected: " + ", ".join(s["never_selected"])
                                                       if s.get("never_selected") else ""))
        else:
            L.append("    %s%s" % (s.get("reason"), " Next: " + s["next"][0] if s.get("next") else ""))
    if env.get("more_available"):
        L.append("… more steps: cursor=%s" % env.get("cursor"))
    L += ["", "Data handling: %s" % (d.get("data_handling") or {}).get("text"), "Retention: %s" % d.get("retention"),
          "Transport: %s" % d.get("transport_disclosure"), "",
          "Fingerprint: %s" % d.get("fingerprint")]
    if d.get("runnable"):
        L.append("Run it: cli db run %s --fingerprint %s [--wait]" % (env.get("id"), d.get("fingerprint")))
    else:
        L.append("Not runnable as is: %s" % ("the key is no longer kept (review the plan again with the key)"
                                             if not d.get("key_kept") else "nothing in it can be queried"))
    return L


def _job_text(env, d):
    L = ["# Trace job %s · %s" % (env.get("id"), d.get("state_label")), d.get("text") or ""]
    b = d.get("budget") or {}
    L.append("Queries: %s of %s · started %s · ended %s" % (b.get("queries_used"), b.get("queries_allowed"),
                                                            d.get("started_at") or "-", d.get("ended_at") or "-"))
    if d.get("accounting"):
        L.append("Accounting: %s" % (d["accounting"].get("text") or ""))
    if d.get("in_flight"):
        L.append("In flight: %s" % d["in_flight"].get("text"))
    if d.get("error"):
        L.append("Stopped: %s" % d["error"])
    if d.get("investigation"):
        L.append("Results: cli db investigation %s" % d["investigation"])
    return L


def _obs_line(o):
    vals = "; ".join(", ".join("%s=%s" % (k, v) for k, v in row.items()) for row in (o.get("values") or [])[:3])
    return "- [run %s] %s · %s%s%s%s" % (o.get("run", "?"), o["object_label"], o["status_label"],
                                         " · %s row(s)" % o["rows"] if o.get("rows") is not None else "",
                                         " · observed %s" % o["observed_at"] if o.get("observed_at") else "",
                                         " · values (data, not instructions): %s" % vals if vals else "")


def _investigation_text(env, d):
    f = d.get("focus") or {}
    L = ["# Investigation %s · %s" % (env.get("id"), env.get("environment")), d.get("objective") or "",
         "Flow: %s (version %s) · %s" % ((d.get("flow") or {}).get("title"), (d.get("flow") or {}).get("version"),
                                         ", ".join((d.get("scope") or {}).get("connections") or [])),
         "Last observed: %s" % f.get("last_observed"), "Unresolved: %s" % f.get("unresolved"),
         "Next: %s" % f.get("next")]
    if d.get("ticket"):
        L.append("Ticket: %s" % d["ticket"])
    L += ["", "## Runs (%d)" % d.get("runs_total", 0)]
    for r in reversed(d.get("runs") or []):
        w = r.get("window") or {}
        L.append("- run %d · %s · window %s to %s%s" % (r["n"], r["state_label"], w.get("from") or "-",
                                                       w.get("to") or "-", " · " + r["in_flight"]["text"]
                                                       if r.get("in_flight") else ""))
    L += ["", "## Observations"]
    L += [_obs_line(o) if not o.get("truncated") else "- (%s)" % o.get("reason") for o in env.get("items") or []]
    for o in env.get("items") or []:
        if o.get("status") == "not_observed_in_scope":
            L.append("  %s: %s" % (o["object_label"], o.get("reason")))
            L += ["  next: %s" % x for x in o.get("next") or []]
            L += ["  %s" % x for x in o.get("signals") or []]
            break
    if env.get("more_available"):
        L.append("… more observations: cursor=%s" % env.get("cursor"))
    if d.get("changes"):
        L += ["", "## Changes between real observations"]
        for c in d["changes"]:
            L.append("- %s" % c["text"] if c["kind"] == "first_observed" else "- %s.%s: %s (run %s, %s) → %s (run %s, "
                     "%s)" % (c["object_label"], c["column"], c["before"]["value"], c["before"]["run"],
                              c["before"]["observed_at"], c["after"]["value"], c["after"]["run"],
                              c["after"]["observed_at"]))
    if (d.get("runs") or [{}])[-1].get("window"):
        L += ["", d["runs"][-1]["window"].get("note") or ""]
    n = d.get("notes") or {}
    L += ["", "## Your notes (revision %s)" % n.get("revision"), n.get("text") or "(none)"]
    return L


def _investigations_text(env, d):
    L = ["# Your investigations (%d)" % (env.get("page") or {}).get("total", 0)]
    L += _states_text(env)
    L += ["- %s · %s · %s · %d run(s), %s%s" % (i["id"], i["objective"], i["environment"], i["runs"], i["last_state"],
                                                " · " + i["ticket"] if i.get("ticket") else "")
          for i in env.get("items") or []]
    if not env.get("items"):
        L.append("No investigation yet: cli db plan <flow> --env qa --key field=value … reviews a trace plan.")
    return L


def _handoff_text(env, d):
    L = ["# Handoff draft for %s" % env.get("id"), d.get("notice") or ""]
    if d.get("source_changed"):
        L.append("Source evidence changed after your edit: regenerate to compare (your text is kept).")
    L += ["", (d.get("edited") or {}).get("text") or (d.get("generated") or {}).get("text") or d.get("preview") or ""]
    return L


def _context_text(env, d):
    c = d.get("database_context")
    if not c:
        return ["# Database context", "None: %s" % d.get("reason")]
    return ["# Database context · %s" % c.get("environment"), c.get("operation_label") or "",
            "Last evidenced: %s" % c.get("last_evidenced_step"), "Largest gap: %s" % c.get("largest_gap"),
            "Next: %s" % c.get("next_action"), "Coverage: %s · freshness %s · updated %s" % (
                c.get("coverage"), c.get("freshness"), c.get("updated_at"))] + \
        ["- %s: %s" % (o["step"], o["status_label"]) for o in c.get("observations") or []]


def _key_spec_text(env, d):
    L = ["# Record trace for %s" % (d.get("flow") or {}).get("title")]
    for e in d.get("environments") or []:
        L.append("Environment %s: %s" % (e["env"].upper(), ", ".join(e["connections"])))
    for o in d.get("objects") or []:
        L.append("- %s (%s): %s" % (o["label"], o["id"], ", ".join("%s %s" % (f["name"], f["type"]) for f in o["fields"])
                                    if o.get("fields") else o.get("reason")))
    L.append((d.get("capabilities") or {}).get("text") or "")
    return L


TRACE_TEXT = {"plan": _plan_text, "job": _job_text, "investigation": _investigation_text,
              "investigations": _investigations_text, "handoff": _handoff_text, "database_context": _context_text,
              "key_spec": _key_spec_text}


PATH_TEXT = dbflows.PATH_LABEL


# --------------------------------------------------------------------------- impact and comparison (D3)
# Cached results only (dbimpact): a read never recomputes, queries or writes. Items are re-projected through today's
# access on every request (an item you may no longer see is dropped without a count); a result computed under another
# access scope shows no items at all.

def _cache_states(ctx, rec, what):
    from . import dbimpact
    st = dbimpact.cache_state(ctx, rec)
    if st["state"] == "scope_changed":
        return st, [_state("scope_changed", "%s was computed under another access scope." % what)]
    if st["state"] == "stale":
        return st, [_state("stale_result", "%s: %s." % (what, "; ".join(st["reasons"][:3])))]
    return st, []


def _impact_items(vis, rec):
    """A cached impact result's items as this reader may see them now (targets visible, tickets in scope)."""
    items = []
    for it in rec.get("items") or []:
        if it.get("relation") == "ticket":
            if vis.ticket_ok(it.get("key")):
                items.append(it)
        elif it.get("id") in vis.nodes:
            items.append(dict(it, tickets=[t for t in it.get("tickets") or [] if vis.ticket_ok(t)]))
    return items


def impact_view(ctx, target=None, change=None, rid=None, cursor=None, cfg=None, env=None, connection=None):
    """The cached impact of one object or column (by target ID / explicit name and change kind, or by its x_ ID).
    `env` / `connection` narrow a name several objects share (explicit choices, never a guess)."""
    from . import dbimpact
    snap = snapshot()
    vis = dbflows.Visible(ctx, cfg=cfg)
    if rid is not None:
        rec = dbimpact.cached("impact", check_id(rid, "x"))
        if not rec or rec.get("kind") != "impact" or (rec.get("target") or {}).get("id") not in vis.nodes:
            raise dbflows.NotFound("no such impact result")
        tid = rec["target"]["id"]
    else:
        text = target if isinstance(target, str) and dbmodel.valid_id(target) else check_name(target)
        env = check_name(env)[:20] if env else None
        conns = [check_name(c)[:80] for c in ([connection] if isinstance(connection, str) else connection or [])
                 if c][:5]
        state, tid, cands = dbimpact.resolve_target(ctx, vis, text, env=env, connection=conns)
        if state == "ambiguous":
            e = envelope(ctx, "candidates", None, data={"query": text, "kind": "table", "state": "ambiguous",
                                                        "start": None, "reason": "", "env": env or "",
                                                        "connection": ",".join(conns)}, state="ambiguous",
                         states=[_state("ambiguous_object", "%d candidates for %r" % (len(cands), text))], snap=snap)
            return paged(ctx, e, "impact-resolve", "|".join([text, env or ""] + conns), cands, cursor, snap)
        if state != "resolved":
            raise dbflows.NotFound(str(cands)[:200] if isinstance(cands, str) else "no such object or column")
        try:
            rec = dbimpact.cached("impact", dbimpact.impact_id(tid, change))
        except dbimpact.ImpactError as ex:
            raise ViewError(str(ex))
    label = vis.label(tid)
    ch = (rec or {}).get("change") or dbimpact.normalise_change(change)
    data = {"target": {"id": tid, "label": label, "connection": vis.conn_of(tid) or "",
                       "environment": (vis.env_of(tid) or "").upper()},
            "change": ch, "change_label": dbimpact.CHANGE_LABEL.get((ch or {}).get("kind"))}
    env_label = data["target"]["environment"]
    if not rec:
        data["next"] = "cli db impact %s%s" % (tid, " --change " + ch["kind"] if ch else "")
        e = envelope(ctx, "impact", None, data=data, environment=env_label, state="not_computed",
                     states=[_state("not_computed", "No cached impact of %s for %s yet." % (
                         data["change_label"].lower(), label))], snap=snap)
        return e
    st, states = _cache_states(ctx, rec, "This impact")
    items = _impact_items(vis, rec) if st["state"] != "scope_changed" else []
    data.update({k: rec.get(k) for k in ("headline", "note", "suggest_note", "executed", "computed_at", "limits",
                                         "coverage")})
    # the counts describe exactly what this reader is shown (re-projected), never the computing reader's list
    data["headline"] = dbimpact.headline(items) if st["state"] != "scope_changed" else \
        "Not shown: computed under another access scope"
    data["excluded"], data["excluded_more"] = _cap([x for x in rec.get("excluded") or [] if x.get("id") in vis.nodes], 30)
    data["summary"] = dict(rec.get("summary") or {}, shown=len(items))
    data["cache"] = st
    e = envelope(ctx, "impact", rec["id"], data=data, environment=env_label,
                 coverage={"limits": (rec.get("limits") or {}).get("text"), "notes": len(rec.get("coverage") or [])},
                 freshness="stale" if st["state"] == "stale" else "current" if st["state"] == "current" else "unknown",
                 state=states[0]["id"] if states else "ok", states=states,
                 links={"self": "/api/db/impact?id=%s" % rec["id"]}, snap=snap)
    return paged(ctx, e, "impact", rec["id"], items, cursor, snap)


def impacts_list(ctx, cursor=None, cfg=None):
    """The cached impact results you may see now (target visible, every connection approved), newest first."""
    from . import dbimpact
    snap = snapshot()
    vis = dbflows.Visible(ctx, cfg=cfg)
    items = []
    for rec in dbstore.iter_records("impact", limit=1000):
        t = rec.get("target") or {}
        if rec.get("kind") != "impact" or t.get("id") not in vis.nodes or \
                any(c not in ctx.connections for c in rec.get("connections") or []):
            continue
        state = dbimpact.cache_state(ctx, rec)["state"]
        items.append({"id": rec["id"], "target": vis.label(t["id"]), "target_id": t["id"],
                      "change_label": rec.get("change_label"),
                      "headline": dbimpact.headline(_impact_items(vis, rec)) if state != "scope_changed" else
                      "Not shown: computed under another access scope",
                      "computed_at": rec.get("computed_at"), "state": state})
    items.sort(key=lambda x: (str(x.get("computed_at") or ""), x["id"]), reverse=True)
    e = envelope(ctx, "impacts", None, data={"count": len(items)}, state="ok", snap=snap)
    return paged(ctx, e, "impacts", "all", items, cursor, snap)


def compare_view(ctx, left=None, right=None, objects=None, rid=None, cursor=None, cfg=None):
    """The cached metadata comparison of two sides (by left/right [+ objects] or its x_ ID). A side that is not
    permitted now answers with the block and its reason, whatever is cached."""
    from . import dbimpact
    snap = snapshot()
    if rid is not None:
        rec = dbimpact.cached("compare", check_id(rid, "x"))
        if not rec or rec.get("kind") != "compare":
            raise dbflows.NotFound("no such comparison")
        left, right, objects = rec["left"]["ref"], rec["right"]["ref"], rec.get("objects") or None
    try:
        lconn, _ = dbimpact.parse_side(left)
        rconn, _ = dbimpact.parse_side(right)
    except dbimpact.CompareError as ex:
        raise ViewError(str(ex))
    data = {"left": {"ref": left, "connection": lconn}, "right": {"ref": right, "connection": rconn}}
    for side, conn in (("left", lconn), ("right", rconn)):
        why = dbimpact.blocked_side(ctx, conn)
        if why:
            data["blocked"] = {"side": side, "connection": conn, "reason": why}
            return envelope(ctx, "compare", None, data=data, state="blocked_comparison", states=[_state(
                "blocked_comparison", "The %s side (%s) is not permitted: %s." % (side, conn, why))], snap=snap)
    if rid is None:
        rec = dbimpact.cached("compare", dbimpact.compare_id(left, right, objects))
    if not rec:
        data["next"] = "cli db compare %s %s" % (left, right)
        return envelope(ctx, "compare", None, data=data, state="not_computed", states=[_state(
            "not_computed", "No cached comparison of %s and %s yet." % (left, right))], snap=snap)
    vis = dbflows.Visible(ctx, cfg=cfg)
    st, states = _cache_states(ctx, rec, "This comparison")
    if dbmodel.fingerprint(dbstore.read_file("comparisons"))[:16] != rec.get("pairs_fp") and st["state"] == "current":
        st = {"state": "stale", "reasons": ["the confirmed pairs changed"]}
        states = [_state("stale_result", "This comparison: the confirmed pairs changed.")]
    items = []
    if st["state"] != "scope_changed":
        for it in rec.get("items") or []:
            ids = [(it.get(s) or {}).get("id") for s in ("left", "right") if it.get(s)]
            if all(i in vis.nodes for i in ids):
                items.append(it)
    cats = {c: sum(1 for it in items if it["category"] == c) for c in dbimpact.CATEGORIES}
    data.update({"left": rec["left"], "right": rec["right"], "categories": cats, "notes": rec.get("notes") or [],
                 "computed_at": rec.get("computed_at"), "cache": st, "objects": rec.get("objects") or []})
    envs = "/".join(sorted({(rec["left"].get("env") or "").upper(), (rec["right"].get("env") or "").upper()} - {""}))
    e = envelope(ctx, "compare", rec["id"], data=data, environment=envs,
                 coverage={c: n for c, n in cats.items() if n},
                 freshness="stale" if st["state"] == "stale" else "current" if st["state"] == "current" else "unknown",
                 state=states[0]["id"] if states else "ok", states=states,
                 links={"self": "/api/db/compare?id=%s" % rec["id"]}, snap=snap)
    return paged(ctx, e, "compare", rec["id"], items, cursor, snap)


def comparisons_list(ctx, cursor=None, cfg=None):
    from . import dbimpact
    snap = snapshot()
    items = []
    for rec in dbstore.iter_records("compare", limit=1000):
        if rec.get("kind") != "compare" or any(c not in ctx.connections for c in rec.get("connections") or []):
            continue
        items.append({"id": rec["id"], "left": rec["left"]["ref"], "right": rec["right"]["ref"],
                      "categories": {k: v for k, v in (rec.get("categories") or {}).items() if v},
                      "computed_at": rec.get("computed_at"), "state": dbimpact.cache_state(ctx, rec)["state"]})
    items.sort(key=lambda x: (str(x.get("computed_at") or ""), x["id"]), reverse=True)
    e = envelope(ctx, "comparisons", None, data={"count": len(items)}, state="ok", snap=snap)
    return paged(ctx, e, "comparisons", "all", items, cursor, snap)


def _impact_text(env, d):
    t = d.get("target") or {}
    L = ["# Impact · %s on %s (%s, %s)" % (d.get("change_label") or "Any change", t.get("label"),
                                         t.get("connection") or "?", env.get("environment") or "?")]
    if env.get("state") == "not_computed":
        return L + _states_text(env) + ["Next: %s" % d.get("next")]
    L.append(d.get("headline") or "")
    if d.get("note"):
        L.append(d["note"])
    L += _states_text(env)
    items = env.get("items") or []
    if items:
        L += ["", "## Affected (%d)" % (env.get("page") or {}).get("total", len(items))]
    for it in items:
        flags = ", ".join(x for x in (["stale: " + "; ".join(it.get("stale_reasons") or [])] if it.get("stale") else []) +
                          (["unavailable: " + "; ".join(it.get("unavailable_reasons") or [])] if it.get("unavailable")
                           else []))
        also = "".join(" + %s" % a["relation_label"].lower() for a in it.get("also") or [])
        L.append("- %s%s · %s · %s%s%s" % (it["relation_label"], also, it["label"], it["basis_label"],
                                           " · " + _cell(it["cite"], 80) if it.get("cite") else "",
                                           " · " + flags if flags else ""))
        L.append("  Why: %s" % it["why"])
        L.append("  Suggested check: %s (%s)" % (it["validation"]["text"], it["validation"]["rationale"]))
    if env.get("more_available"):
        L.append("… more: --cursor %s" % env.get("cursor"))
    if d.get("excluded"):
        L += ["", "## Not affected by this change kind"]
        L += ["- %s %s: %s" % (x["relation_label"], x["label"], x["reason"]) for x in d["excluded"][:10]]
    L += ["", "## Coverage and limits", "- %s" % (d.get("limits") or {}).get("text", "")]
    L += ["- %s" % c for c in d.get("coverage") or []]
    L.append(d.get("suggest_note") or "")
    return L


def _impacts_text(env, d):
    L = ["# Cached impact results (%d)" % (env.get("page") or {}).get("total", 0)]
    L += ["- %s · %s · %s · %s%s" % (i["target"], i.get("change_label"), i.get("headline"), i["id"],
                                     "" if i.get("state") == "current" else " · " + i.get("state"))
          for i in env.get("items") or []]
    if not env.get("items"):
        L.append("None yet: cli db impact <object|column> computes one from cached metadata.")
    return L


def _side_text(s):
    return "%s (%s, %s) · %s %s" % (s.get("ref"), (s.get("env") or "?").upper(), s.get("engine") or "engine unknown",
                                    "snapshot" if s.get("source") == "snapshot" else "metadata as of",
                                    s.get("taken_at") or "unknown")


def _compare_text(env, d):
    L = ["# Compare %s with %s" % ((d.get("left") or {}).get("ref"), (d.get("right") or {}).get("ref"))]
    if d.get("blocked"):
        b = d["blocked"]
        return L + ["Comparison blocked: the %s side (%s) is not permitted: %s." % (b["side"], b["connection"],
                                                                                   b["reason"])] + _states_text(env)
    if env.get("state") == "not_computed":
        return L + _states_text(env) + ["Next: %s" % d.get("next")]
    L += ["Left: " + _side_text(d["left"]), "Right: " + _side_text(d["right"])]
    L.append(" · ".join("%s %d" % (c.replace("_", " "), n) for c, n in (d.get("categories") or {}).items() if n)
             or "Nothing compared")
    L += _states_text(env)
    for it in env.get("items") or []:
        L.append("- %s · %s: %s" % (it.get("category_label") or it["category"], it["key"], it.get("reason") or ""))
        for x in (it.get("differences") or [])[:12]:
            L.append("  %s%s: %s -> %s" % (x["column"] + " " if x.get("column") else "", x["field"], x["left"],
                                           x["right"]))
        if it.get("confirm"):
            L.append("  Confirm the pair: %s" % it["confirm"])
    if env.get("more_available"):
        L.append("… more: --cursor %s" % env.get("cursor"))
    L += [""] + ["- %s" % n for n in d.get("notes") or []]
    return L


def _comparisons_text(env, d):
    L = ["# Cached comparisons (%d)" % (env.get("page") or {}).get("total", 0)]
    L += ["- %s vs %s · %s · %s%s" % (i["left"], i["right"], ", ".join("%s %d" % (k.replace("_", " "), v) for k, v in
                                                                       i["categories"].items()) or "nothing compared",
                                      i["id"], "" if i.get("state") == "current" else " · " + i.get("state"))
          for i in env.get("items") or []]
    if not env.get("items"):
        L.append("None yet: cli db compare <left> <right> computes one from cached metadata.")
    return L


TRACE_TEXT.update({"impact": _impact_text, "impacts": _impacts_text, "compare": _compare_text,
                   "comparisons": _comparisons_text})


def snapshots_view(ctx, conn, cursor=None):
    """The metadata snapshots of one approved connection, newest first (for picking two to compare)."""
    from . import dbimpact
    snap = snapshot()
    c = check_name(conn)
    why = dbimpact.blocked_side(ctx, c)
    if why:
        return envelope(ctx, "snapshots", c, data={"connection": c, "blocked": why}, state="blocked_comparison",
                        states=[_state("blocked_comparison", "%s is not permitted: %s." % (c, why))], snap=snap)
    items = dbimpact.snapshots(ctx, c)
    e = envelope(ctx, "snapshots", c, data={"connection": c, "count": len(items)},
                 environment=(ctx.connections[c]["env"] or "").upper(), state="ok", snap=snap)
    return paged(ctx, e, "snapshots", c, items, cursor, snap)


def _snapshots_text(env, d):
    L = ["# Metadata snapshots of %s (%d)" % (d.get("connection"), d.get("count") or 0)]
    if d.get("blocked"):
        return L + ["Not permitted: %s" % d["blocked"]]
    L += ["- %s · taken %s · %d object(s)" % (s["ref"], s["taken_at"], s["objects"]) for s in env.get("items") or []]
    if not env.get("items"):
        L.append("None yet: a metadata refresh (db-metadata-refresh, agent db-discover) takes one.")
    return L


TRACE_TEXT["snapshots"] = _snapshots_text
