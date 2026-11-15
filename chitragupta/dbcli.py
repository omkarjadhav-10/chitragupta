"""`cli db plan|run|status|cancel|investigations|investigation|note|handoff|context`: record investigations from the
command line (dbtrace + dbviews as text); `cli db impact|compare|reconcile|snapshots` (run_d3): change impact,
metadata comparison and drift from cached metadata (dbimpact: computed only when nothing is cached or --refresh is
given; never a query, never a model). Reading commands only read. `plan` saves a reviewed plan (and the flow it
starts from); with --run it is the explicit execution option of a fully scoped direct command, so no second
confirmation is asked. A run is executed by the agent (`agent db-trace <job>`, started detached through the safety
layer): this process itself never calls a model or the database MCP; --wait reads the job record until it ends.
Key values typed here are transient for Chitragupta (kept only until the run takes them), but your shell may keep
them in its history.
"""
import json

D3_ACTIONS = ("impact", "compare", "reconcile", "snapshots")
ACTIONS = ("plan", "run", "status", "cancel", "investigations", "investigation", "note", "handoff",
           "context") + D3_ACTIONS


def _key(pairs):
    out = {}
    for p in pairs or []:
        k, sep, v = str(p).partition("=")
        if not sep or not k:
            raise ValueError("give each key field as field=value")
        if k in out:
            raise ValueError("the key field %s is given twice" % k)
        out[k] = v  # as typed: never stripped or converted
    return out


def _flow_and_object(ctx, cfg, target, obj, env):
    """(saved flow ID, object ID) from `target` (f_ ID or a start) and --object (o_ ID, schema.table or
    connection:schema.table, matched among the flow's objects; several matches are refused, nothing is chosen)."""
    from . import dbflows, dbmodel, dbviews
    if dbmodel.valid_id(target, "f"):
        fid = target
    else:
        r = dbviews.resolve(ctx, target, None, cfg=cfg)
        start = r["data"]["start"]
        if r["data"]["state"] != "resolved" or not start:
            raise ValueError("%s does not name one flow start (cli db resolve %s)" % (target, target))
        fid = dbflows.build_flow(ctx, start, env=env, save=True, cfg=cfg)["id"]
    if dbmodel.valid_id(obj, "o"):
        return fid, obj
    vis = dbflows.Visible(ctx, cfg=cfg)
    rec = dbflows.saved_flow(fid) or {}
    objs = list((rec.get("current") or {}).get("objects") or [])
    if not obj:
        raise ValueError("name the start object with --object (one of: %s)" % ", ".join(vis.label(o) for o in objs))
    want = str(obj).strip().lower()
    hits = [o for o in objs if want in (vis.label(o, False).lower(), vis.label(o).lower(),
                                        "%s:%s" % ((vis.conn_of(o) or "").lower(), vis.label(o, False).lower()))]
    if len(hits) != 1:
        raise ValueError("--object %s matches %d of the flow's objects (%s): give its o_ ID" % (
            obj, len(hits), ", ".join("%s %s" % (vis.label(o), o) for o in objs)))
    return fid, hits[0]


def run(a, cfg, out, show):
    """One trace sub-command; returns the exit code."""
    if a.action in D3_ACTIONS:
        return run_d3(a, cfg, out, show)
    from . import dbaccess, dbstore, dbtrace, dbviews
    ctx = dbaccess.AccessContext(cfg)
    target = " ".join(a.target).strip()

    def start_and_wait(job):
        out("Job %s %s." % (job["id"], "queued: the agent runs it" if job.get("state") == "queued" else
                            "is " + str(job.get("state"))))
        if a.wait:
            job = dbtrace.wait_job(job["id"])
            show(dbviews.job_view(dbaccess.AccessContext(cfg), job["id"]))
            if job.get("investigation") and job.get("state") in dbtrace.TERMINAL:
                show(dbviews.investigation_view(dbaccess.AccessContext(cfg), job["investigation"]))
        else:
            out("Follow it: cli db status %s" % job["id"])
        return 0
    try:
        if a.action == "plan":
            if not target or not a.env:
                out("Give the flow and the environment: db plan <flow|ticket> --env qa --object shop.orders "
                    "--key tenant_id=7 --key id=1001 [--run [--wait]]")
                return 2
            fid, oid = _flow_and_object(ctx, cfg, target, a.object, a.env)
            plan = dbtrace.make_plan(ctx, fid, a.env, a.connection or None, {"object": oid, "key": _key(a.key)},
                                     cfg=cfg, objective=a.objective or None, investigation_id=a.investigation or None)
            show(dbviews.plan_view(dbaccess.AccessContext(cfg), plan["id"]))
            if not a.run:
                return 0
            job = dbtrace.request_run(dbaccess.AccessContext(cfg), plan["id"], plan["fingerprint"], "cli",
                                      start=lambda j: dbtrace.spawn_runner(j, cfg))
            return start_and_wait(job)
        if a.action == "run":
            if not target or not a.fingerprint:
                out("Give the reviewed plan and its fingerprint: db run <p_…> --fingerprint <F> [--wait]")
                return 2
            job = dbtrace.request_run(ctx, target, a.fingerprint, "cli", start=lambda j: dbtrace.spawn_runner(j, cfg))
            return start_and_wait(job)
        if a.action == "status":
            return show(dbviews.job_view(ctx, target))
        if a.action == "cancel":
            job = dbtrace.request_cancel(ctx, target)
            out("Cancellation requested for %s: no new call is made; %s." % (job["id"], dbtrace.WAIT_WORDS.lower()))
            return show(dbviews.job_view(ctx, job["id"]))
        if a.action == "investigations":
            return show(dbviews.investigations_list(ctx, target or None, cursor=a.cursor))
        if a.action == "investigation":
            return show(dbviews.investigation_view(ctx, target, cursor=a.cursor))
        if a.action == "note":
            if a.text is None or a.revision is None:
                out("Give your text and the notes' revision you edited: db note <i_…> --text \"…\" --revision N")
                return 2
            try:
                inv = dbtrace.note_save(ctx, target, a.text, a.revision)
            except dbstore.Conflict as e:
                out("Not saved: the notes changed since revision %s (now %s). Your text, kept here:\n%s" % (
                    a.revision, (e.current or {}).get("revision"), a.text))
                return 3
            out("Notes saved (revision %d)." % inv["notes"]["revision"])
            return 0
        if a.action == "handoff":
            if a.regenerate:
                dbtrace.handoff_save(ctx, target, regenerate=True, flow=_flow_of(ctx, cfg, target))
            if a.copy:
                out(dbtrace.handoff_copy(ctx, target).rstrip())
                return 0
            return show(dbviews.handoff_view(ctx, target))
        if a.action == "context":
            return show(dbviews.context_view(ctx, a.flow or None, a.investigation or None, cfg=cfg))
    except (dbtrace.PlanError, dbtrace.RunError, ValueError) as e:
        out("Not possible: %s" % e)
        if getattr(e, "required", None):
            out("The key is: %s" % ", ".join("%s=…" % f for f in e.required))
        return 2
    except dbtrace.NotFound as e:
        out("Not found: %s" % e)
        return 2
    return 2


def _flow_of(ctx, cfg, inv_id):
    from . import dbflows, dbtrace, dbviews
    inv = dbtrace.load_visible(ctx, "investigations", inv_id)
    rec = dbflows.saved_flow((inv.get("flow") or {}).get("id") or "")
    vis = dbflows.Visible(ctx, cfg=cfg)
    if rec and rec.get("current") and dbflows._flow_visible(vis, rec):
        return dbviews._flow_visible_filter(vis, rec["current"])[0]
    return None



# --------------------------------------------------------------------------- D3: impact, comparison, drift


def run_d3(a, cfg, out, show):
    """`db impact|compare|reconcile|snapshots`. impact / compare read the cached result and compute it only when
    none is cached or --refresh is given (from cached metadata: never a query, never a model). Returns the exit code."""
    from . import dbaccess, dbflows, dbimpact, dbviews
    ctx = dbaccess.AccessContext(cfg)
    target = " ".join(a.target).strip()
    try:
        if a.action == "reconcile":
            s = dbimpact.reconcile(ctx, cfg)
            try:
                from . import searchidx
                searchidx.update()  # the database units re-projected with today's access
            except Exception:  # noqa: BLE001 - the index is a rebuildable cache: a failure never fails reconcile
                pass
            if a.json:
                out(json.dumps(s, ensure_ascii=False, indent=1))
                return 0
            if s["state"] == "superseded":
                out("The access scope changed while reconciling: nothing was published. Run it again.")
                return 3
            out("Reconciled: %d flow(s) checked, %d marked %s, %d investigation(s) marked, %d cached result(s) "
                "removed, %d cited file(s) checked (%d changed)%s." % (
                    s["flows_checked"], s["flows_marked"], dbimpact.REVIEW_LABEL.lower(), s["investigations_marked"],
                    s["caches_removed"], s["code_files_checked"], s["code_changed"],
                    "; more remain: run it again" if s["more_available"] else ""))
            return 0
        if a.action == "snapshots":
            if not target:
                out("Name the connection: db snapshots qa-main")
                return 2
            return show(dbviews.snapshots_view(ctx, target, cursor=a.cursor))
        if a.action == "impact":
            if not target:
                return show(dbviews.impacts_list(ctx, cursor=a.cursor, cfg=cfg))
            change = dbimpact.normalise_change(a.change) if a.change else None
            vis = dbflows.Visible(ctx, cfg=cfg)
            state, tid, cands = dbimpact.resolve_target(ctx, vis, target, env=a.env, connection=a.connection)
            if state != "resolved":
                return show(dbviews.impact_view(ctx, target=target, change=change, cfg=cfg, env=a.env,
                                                connection=a.connection))
            if a.refresh or dbimpact.cached("impact", dbimpact.impact_id(tid, change)) is None:
                r = dbimpact.impact(ctx, tid, change, vis=vis, cfg=cfg)
                if not r.get("published"):
                    out("The access scope changed while computing: nothing was saved. Run it again.")
                    return 3
            return show(dbviews.impact_view(dbaccess.AccessContext(cfg), target=tid, change=change, cursor=a.cursor,
                                            cfg=cfg))
        if a.action == "compare":
            sides = list(a.target)
            if len(sides) != 2:
                if not sides:
                    return show(dbviews.comparisons_list(ctx, cursor=a.cursor, cfg=cfg))
                out("Give two sides: db compare qa-main dev-main | db compare qa-main@<snapshot> qa-main@<snapshot> "
                    "[--objects shop.orders,...] [--confirm shop.orders[=shop.orders_v2]] [--refresh]")
                return 2
            left, right = sides
            objs = [o.strip() for x in a.objects for o in str(x).split(",") if o.strip()] or None
            if a.confirm:
                lconn, _ = dbimpact.parse_side(left)
                rconn, _ = dbimpact.parse_side(right)
                for c in a.confirm:
                    ln, _, rn = str(c).partition("=")
                    dbimpact.confirm_pair(ctx, lconn, rconn, ln.strip(), rn.strip() or None)
                    out("Recorded: %s on %s is %s on %s for comparing (You confirmed: self-reported)." % (
                        ln.strip(), lconn, rn.strip() or ln.strip(), rconn))
            blocked = [w for w in (dbimpact.blocked_side(ctx, dbimpact.parse_side(s)[0]) for s in sides) if w]
            if not blocked and (a.refresh or a.confirm or
                                dbimpact.cached("compare", dbimpact.compare_id(left, right, objs)) is None):
                r = dbimpact.compare(ctx, left, right, objs, cfg=cfg)
                if r.get("state") == "ok" and not r.get("published"):
                    out("The access scope changed while comparing: nothing was saved. Run it again.")
                    return 3
            return show(dbviews.compare_view(dbaccess.AccessContext(cfg), left, right, objects=objs, cursor=a.cursor,
                                             cfg=cfg))
    except (dbimpact.ImpactError, dbimpact.CompareError, ValueError) as e:
        out("Not possible: %s" % e)
        return 2
    except dbflows.NotFound as e:
        out("Not found: %s" % e)
        return 2
    return 2
