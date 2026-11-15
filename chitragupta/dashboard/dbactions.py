"""The database workspace's protected actions (POST /api/actions/db-*, registered through writes.action: the same Host,
Origin, page token, JSON, size, rate and audit checks as every dashboard write). Each one does a single own thing
through dbtrace / dbflows / dbingest, re-checks access for this request, and records the action without any key value
(start keys travel only in the request body: never in a URL, a log line or the settings history).

  db-plan                  review a trace plan for one record: {flow | start, env, connections?, object, key, objective?,
                           investigation?} -> the plan view (a `start` saves the flow first)
  db-trace-run             {plan, fingerprint} -> queue the job and start the runner (`agent db-trace`), only when the
                           fingerprint is the reviewed plan's
  db-trace-cancel          {job} -> cancellation requested (no new call; an in-flight provider call is only no longer
                           awaited)
  db-note-save             {investigation, text, revision} -> your notes; a stale revision is a 409 that keeps your text
  db-investigation-attach  {investigation, ticket | null} -> link to one of YOUR tickets, or unlink
  db-handoff-save          {investigation, regenerate} | {investigation, text, revision} -> the local handoff draft
  db-mapping-confirm       {from, to, via?, transform?, note?} -> dbingest.confirm_mapping (self-reported evidence)
  db-metadata-refresh      {seeds} -> queue a bounded metadata discovery for explicit objects (describe and catalog
                           reads, never rows) and start `agent db-discover`
  db-impact-compute        {target, change?, env?, connection?} -> compute and cache the impact from cached metadata
                           (no query, no model, no process; nothing is run)
  db-compare-compute       {left, right, objects?, confirm?} -> record the pairs you confirm and compare cached metadata
                           (no query; a side that is not permitted answers with its reason, nothing is widened)
"""
from .. import common
from .writes import action, record_action

KEY_MAX = 4096
NOTE_CAP = 48 * 1024


def _ctx(cfg):
    from .. import dbaccess
    return dbaccess.AccessContext(cfg)


def _id(body, name, prefix):
    from .. import dbmodel
    v = body.get(name)
    return v if dbmodel.valid_id(v, prefix) else None


def _errors(fn):
    """The workspace actions' error answers: never a traceback, never a key value."""
    def wrap(body):
        from .. import dbflows, dbingest, dbstore, dbtrace, dbviews
        name = fn.__name__.replace("_", "-")
        try:
            return fn(body)
        except dbtrace.PlanError as e:
            record_action(name, False, "plan refused")
            return 422, {"error": str(e)[:400], "required": e.required, "field": e.field}
        except dbtrace.RunError as e:
            record_action(name, False, "run refused")
            return 409, {"error": str(e)[:300]}
        except dbstore.Conflict as e:
            record_action(name, False, "stale revision")
            cur = e.current or {}
            return 409, {"error": "Changed since you opened it: your text is kept here; save again to replace the "
                                  "newer version.", "conflict": True,
                         "current": {"text": cur.get("text") or "", "revision": cur.get("revision") or 0},
                         "yours": (e.unsaved or {}).get("text")}
        except dbstore.Busy as e:
            return 503, {"error": str(e)[:200]}
        except dbflows.NotFound:
            record_action(name, False, "not found")
            return 404, {"error": "Not found."}
        except (dbflows.FlowError, dbviews.ViewError, dbingest.MappingError, ValueError) as e:
            record_action(name, False, "refused")
            return 400, {"error": str(e)[:300]}
    wrap.__name__ = fn.__name__
    return wrap


@action("db-plan", cap=KEY_MAX)
@_errors
def db_plan(body):
    from .. import dbflows, dbtrace, dbviews
    cfg = common.load_config()
    ctx = _ctx(cfg)
    fid = _id(body, "flow", "f")
    if not fid:
        start = body.get("start")
        kind, _, ref = str(start or "").partition(":")
        st = {"kind": "ticket", "key": ref.upper()} if kind == "ticket" else {"kind": "node", "id": ref} \
            if kind == "node" else None
        if st is None:
            return 400, {"error": "give flow=<f_…> or start=ticket:<KEY> | node:<ID>"}
        fid = dbflows.build_flow(ctx, st, env=body.get("env") or None, save=True, cfg=cfg)["id"]
    key = body.get("key")
    if not isinstance(key, dict) or len(key) > 16:
        return 422, {"error": "give the key as {field: value}", "required": [], "field": None}
    conns = body.get("connections")
    conns = [str(c) for c in conns][:10] if isinstance(conns, list) and conns else None
    plan = dbtrace.make_plan(ctx, fid, str(body.get("env") or ""), conns,
                             {"object": body.get("object"), "key": key}, cfg=cfg,
                             objective=body.get("objective") if isinstance(body.get("objective"), str) else None,
                             investigation_id=_id(body, "investigation", "i"))
    record_action("db-plan", True, plan["id"])
    return 200, dbviews.plan_view(_ctx(cfg), plan["id"])


@action("db-trace-run", cap=512)
@_errors
def db_trace_run(body):
    from .. import dbtrace, dbviews
    cfg = common.load_config()
    pid = _id(body, "plan", "p")
    if not pid:
        return 400, {"error": "give the plan ID"}
    job = dbtrace.request_run(_ctx(cfg), pid, body.get("fingerprint"), "dashboard",
                              start=lambda j: dbtrace.spawn_runner(j, cfg))
    record_action("db-trace-run", True, job["id"])
    return 200, dbviews.job_view(_ctx(cfg), job["id"])


@action("db-trace-cancel", cap=256)
@_errors
def db_trace_cancel(body):
    from .. import dbtrace, dbviews
    cfg = common.load_config()
    jid = _id(body, "job", "j")
    if not jid:
        return 400, {"error": "give the job ID"}
    dbtrace.request_cancel(_ctx(cfg), jid)
    record_action("db-trace-cancel", True, jid)
    return 200, dbviews.job_view(_ctx(cfg), jid)


@action("db-note-save", cap=NOTE_CAP)
@_errors
def db_note_save(body):
    from .. import dbtrace
    iid = _id(body, "investigation", "i")
    rev = body.get("revision")
    if not iid or not isinstance(body.get("text"), str) or isinstance(rev, bool) or not isinstance(rev, int):
        return 400, {"error": "give the investigation, your text and the revision you edited"}
    inv = dbtrace.note_save(_ctx(common.load_config()), iid, body["text"], rev)
    record_action("db-note-save", True, iid)
    return 200, {"ok": True, "notes": {k: (inv.get("notes") or {}).get(k) for k in ("text", "revision", "at")}}


@action("db-investigation-attach", cap=512)
@_errors
def db_investigation_attach(body):
    from .. import dbtrace
    cfg = common.load_config()
    iid = _id(body, "investigation", "i")
    if not iid or (body.get("ticket") is not None and not isinstance(body.get("ticket"), str)):
        return 400, {"error": "give the investigation and a ticket key (or null to unlink)"}
    inv = dbtrace.attach(_ctx(cfg), iid, body.get("ticket"), cfg=cfg)
    record_action("db-investigation-attach", True, iid)
    return 200, {"ok": True, "ticket": inv.get("ticket")}


@action("db-handoff-save", cap=NOTE_CAP)
@_errors
def db_handoff_save(body):
    from .. import dbaccess, dbflows, dbtrace, dbviews
    cfg = common.load_config()
    ctx = _ctx(cfg)
    iid = _id(body, "investigation", "i")
    if not iid:
        return 400, {"error": "give the investigation"}
    if body.get("regenerate") is True:
        inv = dbtrace.load_visible(ctx, "investigations", iid)
        flow = None
        rec = dbflows.saved_flow((inv.get("flow") or {}).get("id") or "")
        vis = dbflows.Visible(ctx, cfg=cfg)
        if rec and rec.get("current") and dbflows._flow_visible(vis, rec):
            flow = dbviews._flow_visible_filter(vis, rec["current"])[0]
        dbtrace.handoff_save(ctx, iid, regenerate=True, flow=flow)
    else:
        rev = body.get("revision")
        if not isinstance(body.get("text"), str) or isinstance(rev, bool) or not isinstance(rev, int):
            return 400, {"error": "give your edited text and the revision you edited"}
        dbtrace.handoff_save(ctx, iid, text=body["text"], revision=rev)
    record_action("db-handoff-save", True, iid)
    return 200, dbviews.handoff_view(dbaccess.AccessContext(cfg), iid)


@action("db-mapping-confirm", cap=2048)
@_errors
def db_mapping_confirm(body):
    from .. import dbingest
    cfg = common.load_config()
    frm, to = body.get("from"), body.get("to")
    rec = dbingest.confirm_mapping(cfg, frm, to, via=str(body.get("via") or ""),
                                   transform=str(body.get("transform") or ""), note=str(body.get("note") or ""))
    dbingest.run(cfg, only={"mappings"})
    record_action("db-mapping-confirm", True, "%d -> %d column(s)" % (len(rec["from"]), len(rec["to"])))
    return 200, {"ok": True, "from": rec["from"], "to": rec["to"], "via": rec["via"],
                 "basis": "You confirmed (self-reported)"}


@action("db-metadata-refresh", cap=2048)
@_errors
def db_metadata_refresh(body):
    from .. import dbflows, dbtrace
    cfg = common.load_config()
    seeds = body.get("seeds")
    if not isinstance(seeds, list) or not seeds or not all(isinstance(s, str) and len(s) <= 200 for s in seeds):
        return 400, {"error": "give 1 to 10 objects (o_ IDs or connection:schema.table)"}
    res = dbflows.queue_refresh(_ctx(cfg), seeds, cfg=cfg)
    if res["queued"]:
        dbtrace.spawn_agent("db-discover", cfg=cfg)
    record_action("db-metadata-refresh", bool(res["queued"]), "%d queued" % len(res["queued"]))
    return 200, {"ok": bool(res["queued"]), "queued": res["queued"], "skipped": res["skipped"],
                 "note": "Describe and catalog reads only, never rows; the background agent runs them."}


# --------------------------------------------------------------------------- D3: impact and comparison (cached metadata)

def _d3_errors(fn):
    """Impact / comparison inputs that can't be analysed answer 400 with the reason (never a traceback)."""
    def wrap(body):
        from .. import dbimpact
        try:
            return fn(body)
        except (dbimpact.ImpactError, dbimpact.CompareError) as e:
            record_action(fn.__name__.replace("_", "-"), False, "refused")
            return 400, {"error": str(e)[:300]}
    wrap.__name__ = fn.__name__
    return wrap


def _short(v, n):
    return v.strip()[:n] if isinstance(v, str) and v.strip() else None


@action("db-impact-compute", cap=1024)
@_errors
@_d3_errors
def db_impact_compute(body):
    """{target (o_ / c_ ID or schema.table[.column]), change?, env?, connection?} -> the impact computed from cached
    metadata (dbimpact.impact: no query, no model, nothing run) and saved; answers with the impact view."""
    from .. import dbflows, dbimpact, dbviews
    cfg = common.load_config()
    ctx = _ctx(cfg)
    target, env, conn = _short(body.get("target"), 200), _short(body.get("env"), 20), _short(body.get("connection"), 80)
    if not target:
        return 400, {"error": "give the target: an o_ / c_ ID or schema.table[.column]"}
    change = dbimpact.normalise_change(_short(body.get("change"), 20))
    vis = dbflows.Visible(ctx, cfg=cfg)
    state, tid, cands = dbimpact.resolve_target(ctx, vis, target, env=env, connection=conn)
    if state != "resolved":
        record_action("db-impact-compute", False, state)
        return (409 if state == "ambiguous" else 404), {
            "error": "Several objects match: pick one (nothing is chosen for you)." if state == "ambiguous" else
            "Not found: %s" % (cands if isinstance(cands, str) else "no such object or column")}
    r = dbimpact.impact(ctx, tid, change, vis=vis, cfg=cfg)
    if not r.get("published"):
        record_action("db-impact-compute", False, "scope changed")
        return 409, {"error": "The access scope changed while computing: nothing was saved. Try again."}
    record_action("db-impact-compute", True, r["id"])
    return 200, dbviews.impact_view(_ctx(cfg), rid=r["id"], cfg=cfg)


@action("db-compare-compute", cap=2048)
@_errors
@_d3_errors
def db_compare_compute(body):
    """{left, right (connection or connection@snapshot), objects?, confirm?: [schema.name[=other]]} -> record the
    pairs you confirm (self-reported), compare cached metadata (dbimpact.compare: no query, nothing widened) and
    answer with the comparison view; a side that is not permitted answers with the block and its reason."""
    from .. import dbimpact, dbviews
    cfg = common.load_config()
    ctx = _ctx(cfg)
    left, right = _short(body.get("left"), 100), _short(body.get("right"), 100)
    if not left or not right:
        return 400, {"error": "give left and right: connections (qa-main) or snapshots (qa-main@YYYYMMDDTHHMMSS)"}
    objs = body.get("objects")
    objs = [str(o).strip()[:200] for o in objs if str(o).strip()][:50] if isinstance(objs, list) and objs else None
    confirm = body.get("confirm")
    confirm = [str(c).strip()[:200] for c in confirm if str(c).strip()][:10] if isinstance(confirm, list) else []
    blocked = [w for w in (dbimpact.blocked_side(ctx, dbimpact.parse_side(s)[0]) for s in (left, right)) if w]
    if not blocked:
        lconn, rconn = dbimpact.parse_side(left)[0], dbimpact.parse_side(right)[0]
        for c in confirm:
            ln, _, rn = c.partition("=")
            dbimpact.confirm_pair(ctx, lconn, rconn, ln.strip(), rn.strip() or None)
        r = dbimpact.compare(ctx, left, right, objs, cfg=cfg)
        if r.get("state") == "ok" and not r.get("published"):
            record_action("db-compare-compute", False, "scope changed")
            return 409, {"error": "The access scope changed while comparing: nothing was saved. Try again."}
    record_action("db-compare-compute", not blocked, "blocked" if blocked else "%d pair(s) confirmed" % len(confirm))
    return 200, dbviews.compare_view(_ctx(cfg), left, right, objects=objs, cfg=cfg)
