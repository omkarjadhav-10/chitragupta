"""The database scenario of the browser checks (dev only): tests/dbfixture.py's synthetic scenario merged into the UI
check home, and the fake database MCP wired into the server process exactly as the unit tests do it.

    ctx = dbhome.add(home)       after the other fixtures; returns the info dbcheck.js reads (ids, files)
    dbhome.patch(ctx)            in the server process: mcpjobs.run is a tripwire (counted, raises), a trace runner
                                 runs the queued job in-process through dbfixture.FakeTransport (never `claude -p`,
                                 never a provider or model), processes and spawns are counted (not run)
    dbhome.unpatch(ctx)

Scenario: two qa connections (qa-main postgres, qa-billing mssql), a second permitted environment (dev-main, with the
status column-type difference and a hidden procedure definition), a blocked one (uat-main), a connection whose calls
are denied (qa-reporting, never in scope), a delayed refund (the refund row is not visible yet), and one saved
investigation with a run, for the ticket ABC-1101 (the cancellation flow; entries qualified by connection because two
environments hold shop.orders). Every count the checks assert on is written to <home>/ui-db-calls.json after each
call: transport calls, mcpjobs.run attempts, process runs and spawns.
"""
import copy
import json
import os
import threading

from tests import dbfixture
from tests.dbfixture import FLOW, FakeTransport, Scenario

TICKET = "ABC-1101"


def qualify(flow):
    """FLOW with its table and mapping references qualified by connection (qa-main / qa-billing): with dev approved
    too, `shop.orders` names two objects and an unqualified entry stays needs_resolution (DB-24)."""
    def q(x):
        if not isinstance(x, str) or not x.startswith(("shop.", "billing.")):
            return x
        return ("qa-billing:" if x.startswith("billing.") else "qa-main:") + x
    out = copy.deepcopy(flow)
    for e in out:
        if e.get("table") and "." in e["table"]:
            e["table"] = q(e["table"])
        for m in e.get("mapping") or []:
            for k in ("from", "to"):
                m[k] = [q(v) for v in m[k]] if isinstance(m[k], list) else q(m[k])
    return out


class UIScenario(Scenario):
    """dbfixture.Scenario whose config is merged into the UI home's config instead of replacing it."""

    def __init__(self, home, base):
        super().__init__(home)
        self.base = base

    def write(self):
        from chitragupta import mcpjobs
        from tests.support import write_config
        cfg = dict(self.base)
        cfg.update(self.config())
        write_config(self.home, cfg)
        mcpjobs.ensure_files(self.cfg())
        return self


def _counts_path(home):
    return os.path.join(home, "ui-db-calls.json")


def add(home):
    """Build the database scenario into `home` (after the other UI fixtures). Returns the info for dbcheck.js."""
    from chitragupta import common, dbaccess, dbconnector, dbflows, dbingest, dbstore, dbtrace, searchidx
    with open(os.path.join(home, ".worklog", "config.json"), encoding="utf-8") as f:
        base = json.load(f)
    sc = UIScenario(home, base)
    sc.dev, sc.timeout_arg = True, True
    sc.build(dev=True)
    sc.timeout_arg = True
    sc.write()
    rec = dbconnector.assess(sc.cfg(), tools_loaded=["mcp__%s__%s" % (dbfixture.SERVER, t) for t in dbfixture.TOOLS.values()])
    for c in ("qa-main", "qa-billing"):
        dbconnector.observe_call(rec, "timeout", "ok", connection=c, reason="statement timeout honoured")
    dbconnector.save_capabilities(rec)
    dbfixture.write_ticket(home, key=TICKET, flow=qualify(FLOW), relations=[])
    tk = os.path.join(home, "data", "tickets", TICKET + ".json")
    t = common.read_json(tk, {})
    t.update({"id": TICKET, "title": "Cancelling an order refunds it", "status": "In Progress"})
    common.write_json(tk, t)
    cfg = sc.cfg()
    dbingest.run(cfg)
    ctx = dbaccess.AccessContext(cfg)
    fake = FakeTransport(sc)
    flow = dbflows.build_flow(ctx, {"kind": "ticket", "key": TICKET}, env="qa", save=True, cfg=cfg)
    orders = [n for n in flow["nodes"] if n.get("label") == "shop.orders (qa-main)"][0]["id"]
    plan = dbtrace.make_plan(ctx, flow["id"], "qa", None, {"object": orders, "key": {"tenant_id": "7", "id": "1001"}},
                             cfg=cfg, objective="Why was order 1001 not refunded?")
    job = dbtrace.request_run(ctx, plan["id"], plan["fingerprint"], "dashboard", start=lambda j: None)
    dbtrace.run_job(job["id"], fake, cfg, {})
    inv = (dbstore.load("jobs", job["id"]) or {}).get("investigation")
    searchidx.update(full=True)
    info = {"home": home, "config": os.path.join(home, ".worklog", "config.json"), "calls": _counts_path(home),
            "ticket": TICKET, "flow": flow["id"], "orders": orders, "investigation": inv,
            "refunds": [n for n in flow["nodes"] if n.get("label") == "billing.refunds (qa-billing)"][0]["id"]}
    common.write_json(_counts_path(home), {"transport": 0, "mcpjobs_run": 0, "processes": 0, "jobs": []})
    return {"sc": sc, "fake": fake, "info": info}


def _bump(path, key, extra=None):
    lock = _bump.lock
    with lock:
        try:
            with open(path, encoding="utf-8") as f:
                cur = json.load(f)
        except (OSError, ValueError):
            cur = {"transport": 0, "mcpjobs_run": 0, "processes": 0, "jobs": []}
        cur[key] = cur.get(key, 0) + 1
        if extra:
            cur.setdefault("jobs", []).append(extra)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cur, f)


_bump.lock = threading.Lock()


def patch(state):
    """Install the fakes in this (the server's) process. Nothing here can reach a real database, provider or model."""
    from unittest import mock
    from chitragupta import dbtrace, mcpjobs
    from chitragupta.safety import commands
    path = state["info"]["calls"]
    fake, cfg_of = state["fake"], state["sc"].cfg
    real_execute = fake.execute

    def counted_execute(job, calls, cfg, health, **kw):
        for c in calls:
            _bump(path, "transport", {"job": job, "connection": c.get("connection") or ""})
        return real_execute(job, calls, cfg, health, **kw)

    def no_model(job, *a, **k):
        _bump(path, "mcpjobs_run", {"job": str(job)})
        raise RuntimeError("the UI checks never run an MCP job or a model (%s)" % job)

    def runner(job_id, cfg=None):  # the trace runner, in-process, through the fake transport only
        threading.Thread(target=lambda: dbtrace.run_job(job_id, fake, cfg_of(), {}), daemon=True).start()

    def no_process(*a, **k):
        _bump(path, "processes", {"job": "process: " + " ".join(str(x) for x in (a[0] if a else [])[:3])})
        return None

    fake.execute = counted_execute
    state["patches"] = [mock.patch.object(mcpjobs, "run", no_model), mock.patch.object(dbtrace, "spawn_runner", runner),
                        mock.patch.object(dbtrace, "spawn_agent", lambda *a, **k: _bump(path, "processes", {"job": "agent"})),
                        mock.patch.object(commands, "spawn", no_process)]
    for p in state["patches"]:
        p.start()
    return state


def unpatch(state):
    for p in reversed(state.get("patches") or []):
        p.stop()
