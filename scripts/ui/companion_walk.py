"""Recorded UX-04 and UX-06 walkthroughs of the Companion on synthetic homes (dev only; never a real home).

    python scripts/ui/companion_walk.py [OUT_DIR] [--only ux04-1,ux06-3]

Three synthetic homes, built and served from this checkout one at a time on a free port:
- `new`: nothing confirmed yet, plus one other developer's ticket (UX-04 1);
- `dash`: the dashboard's synthetic home of scripts/ui/run.py (releases, delivery, profile, activity) plus Companion
  tasks for one ticket on two repositories and one on a third (UX-04 2-7);
- `plan`: the planner home of scripts/ui/companion.py (every stage) plus the review-incomplete, three-step, switch and
  race tasks (UX-06).
scripts/ui/companion-walkthroughs.js drives Chrome or Edge (playwright-core) and asks this process over a line protocol
(its stdout `@@ctl {json}`, our answer on its stdin) to switch homes, restart the server (a new server instance after
clearing this process's caches and deleting the synthetic raw history, session state and search cache), land synthetic
changes, inspect records and report counters. Generate / Improve run through the real job code with the stub caller of
scripts/ui/companion.py. Every process start, MCP job, Slack call, Jev transport and dispatch start is counted and
refused. Writes walkthroughs.json, walkthroughs.md (the record table) and the screenshots into OUT_DIR. Needs
scripts/ui/node_modules; nothing is installed.
"""
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
_spec = importlib.util.spec_from_file_location("ui_companion", os.path.join(HERE, "companion.py"))
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)  # the planner Fixture and the stub caller (its CALLS counts every stub call)
from tests.support import Isolated, clear_caches, rmtree, write_config  # noqa: E402
from tests.test_companion_state import HEAD1, HEAD2, JIRA, THREE, plan_body  # noqa: E402
from chitragupta import (common, companion, companionevidence, companionexecution, companionjobs,  # noqa: E402
                         companionviews, jev, profile, store, workflowmodel as model, workflowstore)
from chitragupta.dashboard import api, server  # noqa: E402
from chitragupta.safety import commands  # noqa: E402
from chitragupta.safety.log import Blocked  # noqa: E402

HEAD0 = "9e8d7c6b5a4f"
DASH_JIRA = "acme.atlassian.net"
COUNT = {"job_spawns": 0, "job_runs": 0, "process_starts_refused": 0, "jev_evaluate_calls": 0,
         "jev_transport_calls": 0, "dispatch_starts": 0}
JOBS = {"hold": False, "interrupt": False, "held": [], "crashed": [], "timers": []}
NOW = [None]
REFUSED = []  # every refused process start, for the record


# ---------------------------------------------------------------- tripwires and counters (whole run)

def _install():
    def refuse(name):
        def trip(argv, *a, **k):
            COUNT["process_starts_refused"] += 1
            what = " ".join(os.path.basename(str(x)) if i == 0 else str(x) for i, x in enumerate(argv))[:160]
            REFUSED.append("%s(%s) from %s" % (name, what, k.get("purpose") or (a[0] if a else "")))
            print("tripwire refused commands.%s: %s" % (name, REFUSED[-1]), flush=True)
            raise Blocked("walkthrough", "process starts are tripwires here", " ".join(map(str, argv))[:80], name)
        return trip
    for name in ("run", "spawn"):
        mock.patch.object(commands, name, refuse(name)).start()
    mock.patch.object(companionjobs, "spawn_runner", _spawn).start()
    for mod, name, key in ((jev, "evaluate", "jev_evaluate_calls"), (jev, "blocked_transport", "jev_transport_calls"),
                           (companionexecution, "begin_dispatch", "dispatch_starts")):
        mock.patch.object(mod, name, _counted(getattr(mod, name), key)).start()


def _counted(fn, key):
    def wrapper(*a, **k):
        COUNT[key] += 1
        return fn(*a, **k)
    return wrapper


class _Crash(Exception):
    """A simulated runner crash: the plan commit is on disk, the evaluation commit never happens."""


def _spawn(jid, cfg=None):
    """companionjobs.spawn_runner: the runner in this process with the stub caller, never a process."""
    COUNT["job_spawns"] += 1
    if JOBS["hold"]:
        JOBS["held"].append(jid)
        return
    t = threading.Timer(0.8, _run_job, (jid,))
    t.daemon = True
    JOBS["timers"].append(t)
    t.start()


def _run_job(jid):
    COUNT["job_runs"] += 1
    crash, JOBS["interrupt"] = JOBS["interrupt"], False
    try:
        if crash:
            def die(*a, **k):
                raise _Crash()
            with mock.patch.object(companionjobs, "_evaluate", die):
                companionjobs.run(jid, common.load_config(), caller=base.stub_caller)
        else:
            companionjobs.run(jid, common.load_config(), caller=base.stub_caller)
    except _Crash:
        JOBS["crashed"].append(jid)
    except Exception:  # noqa: BLE001 - shown in the run log; the walkthrough records the visible state
        traceback.print_exc()


def counters(fx=None):
    out = dict(COUNT, stub_model_calls=len(base.CALLS), refused=list(REFUSED))
    out["process_starts_refused"] += len(getattr(fx, "processes", []) or [])  # the planner home's own tripwire
    forbidden = list(getattr(fx, "forbidden", []) or [])
    out["mcp_jobs_refused"] = sum(1 for f in forbidden if f.startswith("mcpjobs"))
    out["slack_refused"] = sum(1 for f in forbidden if f.startswith("slack"))
    return out


def clear_all():
    """Every in-process cache the dashboard and the projections keep (a restart starts from none)."""
    clear_caches()
    companionviews._SLIM.clear()
    companionviews._JIRA.clear()
    with api._cache_lock:
        api._cache.clear()
    api._SCOPE.clear()
    api._setup_cache.update(at=0.0, home=None, value=None)
    api._cursor_cache.update(at=0.0, value=None)


# ---------------------------------------------------------------- homes

class Home(Isolated, unittest.TestCase):
    """A temp data home and user folder (tests.support.Isolated) with a frozen, advanceable clock."""

    def runTest(self):  # instantiated directly, never collected
        pass

    def start(self, fixed):
        self.isolate()
        NOW[0] = fixed
        clock = mock.patch.object(common, "now", lambda: NOW[0])
        clock.start()
        self.addCleanup(clock.stop)

    def advance(self, **kw):
        NOW[0] = NOW[0] + dt.timedelta(**kw)

    def stamp(self, **kw):
        return (NOW[0] + dt.timedelta(**kw)).isoformat(timespec="seconds")


def _fixed():
    return dt.datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)


def _session(sid, repo, key, stamp, day):
    state = common.read_json(common.wl("state.json"), {}) or {}
    state.setdefault("sessions", {})[sid] = {"repo_root": repo, "last_event": stamp}
    common.write_json(common.wl("state.json"), state)
    tk = common.read_json(store.ticket_path(key))
    tk["items"]["it-" + sid] = {"id": "it-" + sid, "kind": "note", "title": "Captured work", "date": day,
                                "ref": "raw/%s/%s.jsonl:3" % (day, sid)}
    common.write_json(store.ticket_path(key), tk)


def build_new():
    """UX-04 1: nobody confirmed; one other developer's ticket is on disk and must never show."""
    fx = Home()
    fx.start(_fixed())
    repo = os.path.realpath(os.path.join(fx.home, "repos", "shop"))
    os.makedirs(repo)
    write_config(fx.home, {"watched_repos": [repo], "ticket_prefixes": ["ABC"]})
    tk = store.new_ticket("ABC-2001")
    tk.update(title_hint="Another developer: warehouse sync", repos=["shop"], first=fx.stamp()[:10],
              last=fx.stamp()[:10])
    tk["commits"] = {"c9": {"sha": "c9" * 20, "subject": "ABC-2001 warehouse sync", "author_email": "other@acme.test",
                            "date": fx.stamp(), "repo_name": "shop"}}
    common.write_json(store.ticket_path("ABC-2001"), tk)
    return fx, {"repo": "shop", "other_ticket": "ABC-2001", "email": "developer@acme.test"}


def build_dash():
    """UX-04 2-7: run.py's synthetic home plus Companion tasks (ABC-1001 on two repositories, ABC-1002 on a third)."""
    from tests.test_dashboard import build_home
    from tests.test_dashboard_progress import add_progress_fixtures
    from tests.test_dashboard_releases import add_release_fixtures
    from tests.test_dashboard_v2 import add_v2_fixtures
    fx = Home()
    fixed = _fixed()
    fx.start(fixed)
    days = build_home(fx.home, fixed)
    add_v2_fixtures(fx.home, days)
    rel = add_release_fixtures(fx.home, days)
    add_progress_fixtures(fx.home, days)
    repos = {n: os.path.realpath(os.path.join(fx.home, "repos", n)) for n in ("service-api", "service-ui", "shop-api")}
    for p in repos.values():
        os.makedirs(p)
    cpath = os.path.join(fx.home, ".worklog", "config.json")
    cfg = common.read_json(cpath, {})
    cfg["watched_repos"] = sorted(set((cfg.get("watched_repos") or []) + list(repos.values())))
    common.write_json(cpath, cfg)
    lpath = common.hp("data", "activity", "service-api.json")
    led = common.read_json(lpath, {})
    led["profile_sig"] = profile.signature(common.load_config())  # the ledger stays current under the new scope
    common.write_json(lpath, led)
    day = days["day"]
    crit = [{"id": "ac1", "text": "The order total applies the discount once"},
            {"id": "ac2", "text": "Totals round half up to whole cents"},
            {"id": "ac3", "text": "The total label shows the currency"}]
    w1 = companion.create("ABC-1001", repos["service-api"], DASH_JIRA, crit)
    w1 = companion.save_plan(w1["id"], w1["revision"], plan_body(THREE, "Apply the discount once, round half up and "
                                                                       "label the currency; verify all three."))
    w1 = companion.approve(w1["id"], w1["revision"], w1["selected_plan"], companion.resume(w1["id"])["context_digest"],
                           True, "reviewed-copy")
    _session("sess-dash-1", repos["service-api"], "ABC-1001", fx.stamp(minutes=1), day)
    w1 = companionexecution.associate(w1["id"], w1["revision"], "sess-dash-1")
    # verification history: R0 verified ac2 with a captured test; HEAD then moved to R1 (developer-recorded)
    w1 = companion.observe_context(w1["id"], w1["revision"], {"head": HEAD0, "worktree": "clean", "config": "cfg-1",
                                                               "environment": "local",
                                                               "branch": "feature/ABC-1001-order-total"})
    w1 = _test(w1, "t-r0", HEAD0, fx.stamp(minutes=2), day, ["ac2"])
    w1 = companion.verify(w1["id"], w1["revision"], "Before the rounding change")
    fx.advance(minutes=10)
    w1 = companion.observe_context(w1["id"], w1["revision"], {"head": HEAD1, "worktree": "clean", "config": "cfg-1",
                                                               "environment": "local",
                                                               "branch": "feature/ABC-1001-order-total"})
    w1 = companion.record_evidence(w1["id"], w1["revision"], {
        "criteria": ["ac1"], "coverage": "complete", "manual": {
            "kind": "acceptance", "outcome": "passed", "revision": HEAD1, "text": "Discount applied once on a 3-item order",
            "limits": "Synthetic data; local only"}})
    w1 = _test(w1, "t-nocontext", None, fx.stamp(minutes=1), day, ["ac3"])
    w1 = companion.verify(w1["id"], w1["revision"], "After the rounding change")
    w2 = companion.create("ABC-1001", repos["service-ui"], DASH_JIRA, crit)
    w2 = companion.save_plan(w2["id"], w2["revision"], plan_body(THREE, "Show the currency on the total label."))
    w3 = companion.create("ABC-1002", repos["shop-api"], DASH_JIRA,
                          [{"id": "ac1", "text": "Checkout times out after 30 seconds with a clear message"}])
    w3 = companion.save_plan(w3["id"], w3["revision"], plan_body(text="Raise the checkout timeout message."))
    # returning developer: two new commits land after the last review and the tick ingests them (never acknowledged)
    fx.advance(minutes=20)
    tk = common.read_json(store.ticket_path("ABC-1001"))
    for sha, subject in (("d1" * 20, "ABC-1001 Round totals half up"), ("d2" * 20, "ABC-1001 Label the currency")):
        tk.setdefault("commits", {})[sha] = {"sha": sha, "subject": subject, "date": fx.stamp(), "repo_name": "service-api"}
    tk.setdefault("files", {})["src/orders/total.py"] = {"path": "src/orders/total.py", "changed": True, "last": day,
                                                          "repo": "service-api"}
    common.write_json(store.ticket_path("ABC-1001"), tk)
    companionevidence.tick(common.load_config(), {"ABC-1001"})
    return fx, {"w1": w1["id"], "w2": w2["id"], "w3": w3["id"], "day": day, "release": rel,
                "repos": {k: os.path.basename(v) for k, v in repos.items()}}


def _test(rec, tid, head, at, day, crit):
    """A captured test run on the ticket (with its context when `head` is given), mapped to `crit`."""
    key = rec["binding"]["ticket"]
    tk = common.read_json(store.ticket_path(key))
    row = {"id": tid, "at": at, "day": day, "session": "sess-dash-1", "command": "python -m unittest tests.test_orders",
           "status": "passed"}
    if head:
        row["context"] = {"repo_id": rec["binding"]["repo_id"], "location": rec["binding"]["location"], "head": head,
                          "worktree": "clean", "config": "cfg-1", "environment": "local"}
    tk.setdefault("tests", {})[tid] = row
    common.write_json(store.ticket_path(key), tk)
    return companion.record_evidence(rec["id"], rec["revision"], {"evidence": "test:" + tid, "criteria": crit,
                                                                  "coverage": "complete"})


class PlanHome(base.Fixture):
    def runTest(self):
        pass


def build_plan():
    """UX-06: every planner stage (companion.py's fixture) plus the walkthroughs' own tasks."""
    fx = PlanHome()
    fx.build()
    extra = {}
    for name in ("repo-h", "repo-i", "repo-j", "repo-k"):
        extra[name] = os.path.realpath(os.path.join(common.home(), name))
        os.makedirs(extra[name])
    titles = {"ABC-1009": "Review incomplete: coupon limits", "ABC-1010": "Three steps: order filter",
              "ABC-1011": "Other repository: tax export", "ABC-1012": "Race: discount rounding"}
    fx.cfg["profile"]["include_tickets"].update(titles)
    fx.cfg["profile"]["repos"] = fx.cfg["profile"]["repos"] + list(extra.values())
    fx.cfg["watched_repos"] = fx.cfg["watched_repos"] + list(extra.values())
    write_config(common.home(), fx.cfg)
    for key, title in titles.items():
        tk = store.new_ticket(key)
        tk["title_hint"] = title
        common.write_json(store.ticket_path(key), tk)
    ids = {}
    # every workflow of the base fixture was bound to the old profile: rebuild them under this one
    for wid in list(workflowstore.ids()):
        rmtree(workflowstore.root(wid))
    fx.cfg_snapshot = json.loads(json.dumps(fx.cfg))
    _plan_tasks(fx, extra, ids)
    return fx, ids


def _plan_tasks(fx, extra, ids):
    """The base fixture's stages (rebuilt under the final profile) plus UX-06's own tasks."""
    from chitragupta import companionjobs as jobs
    for key in [k for k in os.listdir(common.hp("data", "tickets")) if k.endswith(".json")]:
        tk = common.read_json(common.hp("data", "tickets", key))
        tk.pop("workflows", None)
        common.write_json(common.hp("data", "tickets", key), tk)
    home = common.home()
    r = {n: os.path.realpath(os.path.join(home, n)) for n in ("repo-c", "repo-d", "repo-e", "repo-f", "repo-g")}
    ids["ABC-1001"] = fx.planned("ABC-1001", n=2)["id"]
    ids["ABC-1002"] = fx.create("ABC-1002", fx.repo_b, n=2)["id"]
    ids["ABC-1004"] = fx.started("ABC-1004", r["repo-c"], sid="sess-4")["id"]
    p = fx.started("ABC-1005", r["repo-d"], sid="sess-5")
    ids["ABC-1005"] = companion.pause(p["id"], p["revision"], "Stopped after the rounding helper")["id"]
    t = fx.started("ABC-1006", r["repo-e"], sid="sess-6")
    ids["ABC-1006"] = companion.request_verification(t["id"], t["revision"], ["Run the shipping estimate tests"])["id"]
    ids["ABC-1007"] = fx.started("ABC-1007", r["repo-f"], sid="sess-7")["id"]
    fx.tick("ABC-1007")
    done = fx.planned("ABC-1008", r["repo-g"])
    rec = workflowstore.load(done["id"])
    rec.update(stage="completed", next_action="View completion evidence")  # fixture shortcut: a finished task
    workflowstore.save(rec, rec["revision"], lambda x: None)
    ids["ABC-1008"] = done["id"]
    rv = fx.planned("ABC-1009", extra["repo-k"], n=2)
    ids["ABC-1009"] = jobs.evaluate_plan(rv["id"], rv["revision"])["id"]  # Jev blocked: review incomplete
    three = fx.started("ABC-1010", extra["repo-h"], steps=THREE, n=3, sid="sess-10")
    three = fx.observe(three)
    for step in ("one", "two"):
        three = fx.map(three, manual={"kind": "code_inspection", "outcome": "passed", "text": "Step %s in the diff" % step,
                                      "revision": HEAD1, "limits": "Read the diff only"}, steps=[step])
    ids["ABC-1010"] = three["id"]
    ids["ABC-1011"] = fx.started("ABC-1011", extra["repo-i"], sid="sess-11")["id"]
    ids["ABC-1012"] = fx.planned("ABC-1012", extra["repo-j"], n=2)["id"]
    ids["repos"] = {k: os.path.basename(v) for k, v in dict(r, **extra).items()}


# ---------------------------------------------------------------- mutations the walkthroughs ask for

def _put(key, **fields):
    tk = common.read_json(store.ticket_path(key))
    tk.update(fields)
    common.write_json(store.ticket_path(key), tk)
    return tk


def m_disconnect(run, args):
    """UX-04 7: the GitHub PR read is denied, Jira answered three days ago; commits stay current."""
    lpath = common.hp("data", "activity", "service-api.json")
    led = common.read_json(lpath, {})
    run.saved["ledger"] = json.loads(json.dumps(led))
    led["prs"].update(status="error", error="HTTP 403: Resource not accessible (permission denied; synthetic)")
    common.write_json(lpath, led)
    cpath = os.path.join(common.home(), ".worklog", "config.json")
    cfg = common.read_json(cpath, {})
    run.saved["config"] = json.loads(json.dumps(cfg))
    cfg.setdefault("mcp", {}).setdefault("jira", {})["enabled"] = True
    common.write_json(cpath, cfg)
    health = common.read_json(common.wl("health.json"), {}) or {}
    run.saved["health"] = json.loads(json.dumps(health))
    health.setdefault("mcp", {})["jira_auth_ok"] = (common.now() - dt.timedelta(days=3)).isoformat(timespec="seconds")
    common.write_json(common.wl("health.json"), health)
    return {"github": "permission denied", "jira": "last answered 3 days ago", "git": "current"}


def m_reconnect(run, args):
    common.write_json(common.hp("data", "activity", "service-api.json"), run.saved["ledger"])
    health = common.read_json(common.wl("health.json"), {}) or {}
    health.setdefault("mcp", {})["jira_auth_ok"] = common.now().isoformat(timespec="seconds")
    common.write_json(common.wl("health.json"), health)
    return {"github": "read again", "jira": "answered now"}


def m_land_change(run, args):
    """UX-06 4: another relevant change lands on the ticket record; the tick ingests it (never acknowledged)."""
    key = args.get("key", "ABC-1010")
    run.fx.advance(minutes=30)
    tk = common.read_json(store.ticket_path(key))
    repo = run.info["repos"]["repo-h"]
    tk.setdefault("commits", {})["e1" * 20] = {"sha": "e1" * 20, "subject": key + " Change the filter persistence",
                                               "date": run.fx.stamp(), "repo_name": repo}
    tk.setdefault("files", {})["src/orders/filter_store.py"] = {"path": "src/orders/filter_store.py", "changed": True,
                                                                "last": run.fx.stamp()[:10], "repo": repo}
    common.write_json(store.ticket_path(key), tk)
    return {"ticked": companionevidence.tick(common.load_config(), {key})}


def m_pr_update(run, args):
    """UX-06 5: PR #41 gets a new review request (changes requested) and a new head; CI passed on the old head."""
    from tests.test_ci_checks import INFO, Recorded, check, page
    from chitragupta import hosts
    key, repo = "ABC-1007", run.info["repos"]["repo-f"]
    run.fx.advance(minutes=30)
    tk = common.read_json(store.ticket_path(key))
    r1, r2 = "a1" * 20, "b2" * 20
    pr = {"number": 41, "state": "open", "review": "", "source": "feature/" + key, "target": "main",
          "title": "Order search", "head": r1, "src": "gh"}
    hosts.ci_record(pr, INFO, hosts.gh_check_runs({}, INFO, r1, run=Recorded({(r1, 1): page([check(1), check(2, "lint")])})),
                    (common.now() - dt.timedelta(minutes=20)).isoformat(timespec="seconds"))
    pr.update(head=r2, review="changes_requested", updated_at=run.fx.stamp())
    git = tk.get("git") or {"repos": {}}
    entry = git["repos"].setdefault(repo, {"branches": []})
    entry["prs"] = [p for p in entry.get("prs", []) if p.get("number") != 41] + [{k: v for k, v in pr.items() if k != "ci"}]
    entry["ci"] = {"41": pr["ci"]}
    git["checked_at"] = run.fx.stamp()
    tk["git"] = git
    tk.setdefault("commits", {})["f2" * 20] = {"sha": "f2" * 20, "subject": key + " Address review comments",
                                               "date": run.fx.stamp(), "repo_name": repo}
    common.write_json(store.ticket_path(key), tk)
    return {"ticked": companionevidence.tick(common.load_config(), {key}), "old_head": r1[:7], "new_head": r2[:7]}


def m_work_on_b(run, args):
    """UX-06 6: work lands on B (ABC-1011, another repository) while A is paused."""
    key, repo = "ABC-1011", run.info["repos"]["repo-i"]
    run.fx.advance(minutes=30)
    tk = common.read_json(store.ticket_path(key))
    tk.setdefault("commits", {})["b1" * 20] = {"sha": "b1" * 20, "subject": key + " Export the tax lines",
                                               "date": run.fx.stamp(), "repo_name": repo}
    common.write_json(store.ticket_path(key), tk)
    state = common.read_json(common.wl("state.json"), {}) or {}
    state.setdefault("sessions", {}).setdefault("sess-11", {})["last_event"] = run.fx.stamp()
    common.write_json(common.wl("state.json"), state)
    return {"ticked": companionevidence.tick(common.load_config(), {key})}


def m_revoke(run, args):
    """UX-06 7: the race task's repository leaves the profile (written outside the dashboard)."""
    cfg = json.loads(json.dumps(run.fx.cfg_snapshot))
    gone = os.path.realpath(os.path.join(common.home(), "repo-j"))
    cfg["profile"]["repos"] = [p for p in cfg["profile"]["repos"] if os.path.realpath(p) != gone]
    write_config(common.home(), cfg)
    return {"removed": "repo-j"}


def m_restore(run, args):
    write_config(common.home(), run.fx.cfg_snapshot)
    return {"restored": True}


def m_jobs(run, args):
    for k in ("hold", "interrupt"):
        if k in args:
            JOBS[k] = bool(args[k])
    if args.get("release"):
        held, JOBS["held"] = JOBS["held"], []
        for jid in held:
            _spawn(jid)
    return {k: (len(v) if isinstance(v, list) else v) for k, v in JOBS.items() if k != "timers"}


MUTATIONS = {"disconnect": m_disconnect, "reconnect": m_reconnect, "land_change": m_land_change,
             "pr_update": m_pr_update, "work_on_b": m_work_on_b, "revoke": m_revoke, "restore": m_restore,
             "jobs": m_jobs}
BUILD = {"new": build_new, "dash": build_dash, "plan": build_plan}


# ---------------------------------------------------------------- the runner and its control protocol

class Runner:
    def __init__(self):
        self.fx, self.srv, self.info, self.name, self.saved = None, None, {}, None, {}

    def _start(self):
        self.srv, _t = server.start_in_thread(0)
        return "http://127.0.0.1:%d/" % self.srv.port

    def _stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    def teardown(self):
        for t in JOBS["timers"]:
            t.cancel()
        JOBS.update(hold=False, interrupt=False, held=[], timers=[])
        time.sleep(0.2)
        self._stop()
        if self.fx:
            ok = self.fx.doCleanups()
            if ok is False:
                print("cleanup of the %s home reported a problem" % self.name)
            self.fx = None
        clear_all()

    def phase(self, args):
        self.teardown()
        self.name = args["name"]
        self.fx, self.info = BUILD[self.name]()
        self.saved = {}
        clear_all()
        return {"base": self._start(), "info": self.info, "home": self.name}

    def restart(self, args):
        """Stop the server, drop this process's caches, delete raw history / session state / search cache, start a
        new server instance. Canonical records (config, tickets, workflows) are all that is left."""
        self._stop()
        clear_all()
        wiped = []
        if args.get("wipe", True):
            for path in (common.wl("raw"), common.wl("cache"), common.hp("data", "search"), common.hp("tickets"),
                         common.hp("raw")):
                if os.path.exists(path):
                    rmtree(path)
                    wiped.append(os.path.relpath(path, common.home()).replace("\\", "/"))
            if os.path.exists(common.wl("state.json")):
                os.remove(common.wl("state.json"))
                wiped.append(".worklog/state.json")
        return {"base": self._start(), "wiped": wiped}

    def inspect(self, args):
        wid = args["workflow"]
        rec = workflowstore.load(wid)
        sel = rec.get("selected_plan")
        head = common.read_json(os.path.join(workflowstore.root(wid), "head.json"), {}) or {}
        tk = common.read_json(store.ticket_path(rec["binding"]["ticket"]), {})
        return {"revision": rec["revision"], "stage": rec["stage"], "resume_stage": rec.get("resume_stage"),
                "selected_plan": sel, "selected_digest": model.digest(rec["plans"][sel]) if sel else None,
                "approved_plan": rec.get("approved_plan"), "plans": len(rec["plans"]),
                "evaluations": len(rec.get("evaluations") or {}),
                "approvals": [{"attempt": companion.attempt_id(a), "dispatch": a.get("dispatch"),
                               "copied": bool(a.get("copied_at")), "continuation": bool(a.get("continuation"))}
                              for a in rec.get("approvals") or []],
                "jobs": [{"id": j["id"], "kind": j["kind"], "state": j["state"], "phase": j.get("phase")}
                         for j in companionjobs.jobs(wid)],
                "candidates": {k: v.get("state") for k, v in (rec.get("candidates") or {}).items()},
                "checkpoints": len(os.listdir(os.path.join(workflowstore.root(wid), "checkpoints"))),
                "head": head.get("digest"), "acknowledged": len(rec.get("acknowledged") or {}),
                "unread": len(companionevidence.unread(rec)), "location": os.path.basename(rec["binding"]["location"]),
                "ticket_items": model.digest(tk.get("items") or {})[:16], "session": rec.get("session"),
                "profile_generation": rec["binding"]["profile_generation"],
                "current_generation": profile.signature(common.load_config())}

    def cli(self, args):
        """The text fallback in a fresh process against the same home (read-only by contract)."""
        argv = [sys.executable, "-B", "-m", "chitragupta.cli"] + [str(a) for a in args["argv"]]
        proc = subprocess.run(argv, cwd=ROOT, env=self.fx.child_env(), stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=120)
        return {"rc": proc.returncode, "out": proc.stdout.decode("utf-8", "replace")[:6000],
                "err": proc.stderr.decode("utf-8", "replace")[-1500:]}

    def handle(self, msg):
        op, args = msg.get("op"), msg.get("args") or {}
        if op == "phase":
            return self.phase(args)
        if op == "restart":
            return self.restart(args)
        if op == "counters":
            return counters(self.fx)
        if op == "inspect":
            return self.inspect(args)
        if op == "cli":
            return self.cli(args)
        if op == "mutate":
            return MUTATIONS[args["kind"]](self, args)
        if op == "advance":
            self.fx.advance(**{k: v for k, v in args.items() if k in ("minutes", "hours", "days")})
            return {"now": common.now().isoformat()}
        raise ValueError("unknown op %r" % op)


def render(results, out, previous):
    """walkthroughs.md: one row per walkthrough plus the detail of each, and the stability line."""
    rows = results["walkthroughs"]
    lines = ["# Companion walkthroughs (UX-04 and UX-06), synthetic data", "",
             "Generated by `python scripts/ui/companion_walk.py %s` on %s. Dashboard only (plus the CLI text fallback "
             "where a step is text); the native panes are not covered here." % (
                 os.path.relpath(out, ROOT).replace("\\", "/"), results["finished_at"]), "",
             "| ID | Walkthrough | Result | Checks | Model / job / process calls | Dispatch / submit | Limitation |",
             "|---|---|---|---|---|---|---|"]
    for w in rows:
        c = w.get("counts") or {}
        calls = "stub model %s · jobs %s · process %s · MCP %s · Jev %s" % (
            c.get("stub_model_calls", 0), c.get("job_runs", 0), c.get("process_starts_refused", 0),
            c.get("mcp_jobs_refused", 0), c.get("jev_transport_calls", 0) + c.get("jev_evaluate_calls", 0))
        lim = "; ".join(w.get("limitations") or []) or "None"
        lines.append("| %s | %s | **%s** | %d/%d | %s | %s | %s |" % (
            w["id"], w["title"], w["status"], sum(1 for x in w["checks"] if x["ok"]), len(w["checks"]), calls,
            "dispatch starts %s · submits %s" % (c.get("dispatch_starts", 0), w.get("submits", 0)), lim))
    if previous:
        same = [w["id"] for w in rows if previous.get(w["id"]) == w["status"]]
        lines += ["", "Stability: %d of %d statuses equal the previous run's (%s)." % (
            len(same), len(rows), ", ".join("%s %s" % (k, v) for k, v in sorted(previous.items())))]
    for w in rows:
        lines += ["", "## %s: %s (%s)" % (w["id"], w["title"], w["status"]), ""]
        for label, key in (("Home", "home"), ("Expected", "expected"), ("Actual", "actual")):
            if w.get(key):
                lines.append("- **%s:** %s" % (label, w[key]))
        lines.append("- **Path:** " + " → ".join(w.get("path") or []))
        lines.append("- **State before:** `%s`" % json.dumps(w.get("before") or {}, ensure_ascii=False)[:900])
        lines.append("- **State after:** `%s`" % json.dumps(w.get("after") or {}, ensure_ascii=False)[:900])
        lines.append("- **Actions:** " + "; ".join(w.get("actions") or []))
        lines.append("- **Keyboard:** " + ("; ".join(w.get("keyboard") or []) or "not exercised"))
        lines.append("- **Counts (this walkthrough):** `%s`; submits %s" % (
            json.dumps(w.get("counts") or {}), w.get("submits", 0)))
        lines.append("- **Final view:** " + ", ".join(w.get("shots") or []))
        if w.get("limitations"):
            lines.append("- **Limitations:** " + "; ".join(w["limitations"]))
        if w.get("error"):
            lines.append("- **Error:** `%s`" % w["error"][:600])
        lines.append("- **Checks:**")
        lines += ["  - %s %s" % ("ok" if x["ok"] else "FAIL", x["what"]) for x in w["checks"]]
    with open(os.path.join(out, "walkthroughs.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def main(argv):
    out = os.path.abspath(argv[0]) if argv and not argv[0].startswith("--") else os.path.join(
        ROOT, "notes", "local-tests", "companion-review-assets", "walkthroughs")
    only = argv[argv.index("--only") + 1] if "--only" in argv else ""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.makedirs(out, exist_ok=True)
    results_path = os.path.join(out, "walkthroughs.json")
    previous = {}
    if os.path.exists(results_path) and not only:
        try:
            previous = {w["id"]: w["status"] for w in common.read_json(results_path, {}).get("walkthroughs", [])}
        except (AttributeError, TypeError, KeyError):
            previous = {}
    _install()
    run = Runner()
    node = subprocess.Popen(["node", os.path.join(HERE, "companion-walkthroughs.js"), out, results_path, only],
                            cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 1800
    watchdog = threading.Timer(1800, lambda: (print("walkthroughs exceeded 30 minutes; stopping", flush=True),
                                              node.kill()))
    watchdog.daemon = True
    watchdog.start()
    try:
        for raw in iter(node.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("@@ctl "):
                print(line, flush=True)
                continue
            msg = json.loads(line[6:])
            try:
                reply = {"id": msg["id"], "ok": True, "result": run.handle(msg)}
            except Exception as exc:  # noqa: BLE001 - the walkthrough records the failure
                traceback.print_exc()
                reply = {"id": msg["id"], "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            node.stdin.write((json.dumps(reply, default=str) + "\n").encode("utf-8"))
            node.stdin.flush()
            if time.monotonic() > deadline:
                print("walkthroughs exceeded 30 minutes; stopping")
                node.kill()
                break
        code = node.wait(timeout=60)
    finally:
        watchdog.cancel()
        if node.poll() is None:
            node.kill()
        run.teardown()
    results = common.read_json(results_path, {}) or {}
    if results.get("walkthroughs"):
        results["finished_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        render(results, out, previous if not only else {})
        print("walkthroughs.md written: %s" % os.path.join(out, "walkthroughs.md"))
    print("counters: %s" % json.dumps(counters()))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
