"""Companion plan jobs and budget reservations (PLAN-02, PLAN-03, FLOW-13, JEV-03; A43, A45, A58, A60, A61).

Synthetic data only, frozen clock, isolated data homes. Model calls are injected stubs, the Jev transport is a
stub or the blocked default, the runner spawn is recorded instead of started, and every real process start is a
tripwire. The two-process budget race uses real children with a hard deadline and stdin closed.
"""
import datetime as dt
import json
import os
import subprocess
import threading
import time
import unittest
from unittest import mock

from tests.support import PY, ROOT, Isolated, write_config
from tests.test_companion_planning import output
from chitragupta import (agent, common, companion, companionevidence as evidence, companionjobs as jobs,
                         extract, jev, modelbudget, store, workflowmodel as model, workflowstore)
from chitragupta.safety import commands

T0 = dt.datetime(2026, 11, 8, 10, 0, tzinfo=dt.timezone.utc)
JIRA = "https://jira.acme.test"
CRITERIA = [{"id": "ac1", "text": "Orders can be filtered by status"},
            {"id": "ac2", "text": "The chosen filter persists"}]


def manual():
    return {"text": "Manual plan: filter orders.", **{s: "Recorded design detail" for s in model.REQUIRED_SECTIONS},
            "steps": [{"id": "one", "text": "Add the filter", "criteria": ["ac1", "ac2"]}]}


class Caller:
    """Injected model caller: records each call; `fn` may raise or act during the call."""

    def __init__(self, result=None, cost=0.05, fn=None):
        self.result, self.cost, self.fn, self.calls = result, cost, fn, []

    def __call__(self, kind, text, cfg, timeout=None):
        self.calls.append({"kind": kind, "text": text, "timeout": timeout})
        if self.fn:
            self.fn()
        if isinstance(self.result, BaseException):
            raise self.result
        return (self.result if self.result is not None else output(("ac1", "ac2"))), self.cost


class Base(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.repo = os.path.realpath(os.path.join(common.home(), "scratch"))
        os.makedirs(self.repo)
        self.cfg = {"profile": {"accounts": [{"provider": "jira", "instance": JIRA, "id": "dev-a"}],
                                "repos": [self.repo],
                                "include_tickets": {"ABC-1001": "Mine", "ABC-1002": "Mine", "ABC-1004": "Mine"}},
                    "watched_repos": [self.repo], "git_author_emails": ["dev@acme.test"],
                    "budget": {"model_daily_usd": 3.0, "companion_call_usd": 0.2}}
        write_config(common.home(), self.cfg)
        for key in ("ABC-1001", "ABC-1002", "ABC-1004"):
            tk = store.new_ticket(key)
            tk["files"] = {"src/orders/list.py": {"path": "src/orders/list.py", "last": "2026-11-07", "changed": True}}
            common.write_json(store.ticket_path(key), tk)
        self.now = T0
        clock = mock.patch.object(common, "now", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.spawned = []
        spawn = mock.patch.object(jobs, "spawn_runner", lambda jid, cfg=None: self.spawned.append(jid))
        spawn.start()
        self.addCleanup(spawn.stop)
        self.processes = []

        def no_process(*args, **kwargs):
            self.processes.append(args[:1])
            raise AssertionError("job code started a real process")
        for name in ("run", "spawn"):
            trip = mock.patch.object(commands, name, no_process)
            trip.start()
            self.addCleanup(trip.stop)
        self.addCleanup(lambda: self.assertEqual(self.processes, [], "no model, git or Jira process"))

    def configure(self, **over):
        cfg = json.loads(json.dumps(self.cfg))
        for key, value in over.items():
            node = cfg
            parts = key.split(".")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = value
        write_config(common.home(), cfg)
        return cfg

    def create(self, key="ABC-1001"):
        return companion.create(key, self.repo, JIRA, CRITERIA)

    def act(self, rec, name, **body):
        return companion.action(dict(body, action=name, workflow=rec["id"], revision=rec["revision"]))

    def planned(self, approve=False):
        rec = self.create()
        rec = companion.save_plan(rec["id"], rec["revision"], manual())
        if approve:
            digest = companion.resume(rec["id"])["context_digest"]
            rec = companion.approve(rec["id"], rec["revision"], rec["selected_plan"], digest, True, "reviewed-copy")
        return rec

    def ledger(self):
        return modelbudget._ledger()["entries"]

    def frozen(self, rec):
        rec = workflowstore.load(rec["id"])
        return json.dumps({k: rec[k] for k in ("plans", "approvals", "selected_plan", "approved_plan", "stage")},
                          sort_keys=True)


class GenerateAndImprove(Base):
    def test_generate_saves_the_plan_before_an_unavailable_evaluation(self):
        rec = self.create()
        out = self.act(rec, "generate_plan")
        job = out["job"]
        self.assertEqual((out["stage"], job["state"], job["kind"], self.spawned), ("planning", "queued", "generate",
                                                                                     [job["id"]]))
        self.assertNotIn("packet_text", job)
        reservation = self.ledger()[job["reservation"]]
        self.assertEqual((reservation["state"], reservation["usd"], reservation["purpose"]),
                         ("reserved", 0.2, "companion_plan"))
        caller = Caller()
        done = jobs.run(job["id"], caller=caller)
        self.assertEqual((done["state"], len(caller.calls)), ("succeeded", 1))
        self.assertIn("BEGIN UNTRUSTED SOURCE DATA", caller.calls[0]["text"])
        self.assertEqual(caller.calls[0]["timeout"], 180)
        rec = companion.get(rec["id"])
        plan = rec["plans"][rec["selected_plan"]]
        self.assertEqual((rec["stage"], plan["source"], plan["details"]["origin"]["job"]), ("planned", "claude", job["id"]))
        self.assertEqual(plan["details"]["files"][0]["basis"], "evidence")  # recorded file evidence is cited
        evaluation = rec["evaluations"][done["result"]["evaluation"]]
        self.assertEqual((evaluation["state"], evaluation["coverage"]["percent"], evaluation["coverage"]["reason"]),
                         ("unavailable", None, jev.UNAVAILABLE))
        self.assertEqual(evidence.plan_coverage(rec, plan["id"])["label"], "Evaluation unavailable")
        settled = self.ledger()[job["reservation"]]
        self.assertEqual((settled["state"], settled["actual"]), ("settled", 0.05))
        self.assertNotIn("packet_text", jobs.load(rec["id"], job["id"]))  # transient buffers expire on job end
        with self.assertRaisesRegex(ValueError, "use Improve Plan"):
            self.act(rec, "generate_plan")

    def test_paid_action_is_disabled_without_a_per_call_ceiling(self):
        self.configure(**{"budget.companion_call_usd": None})
        rec = self.create()
        with self.assertRaisesRegex(ValueError, "budget.companion_call_usd"):
            self.act(rec, "generate_plan")
        self.assertEqual((jobs.jobs(rec["id"]), self.spawned, self.ledger()), ([], [], {}))
        self.assertEqual(companion.get(rec["id"])["stage"], "new")


class A58Improve(Base):
    def test_one_improve_is_one_call_plus_one_evaluation_and_a_candidate_needs_selection(self):
        rec = self.planned(approve=True)
        before = self.frozen(rec)
        manual_id = rec["selected_plan"]
        out = self.act(rec, "improve_plan")
        again = self.act(rec, "improve_plan")  # duplicate click with the same (now old) revision
        self.assertEqual((again["job"]["id"], again["coalesced"]), (out["job"]["id"], True))
        self.assertEqual(self.spawned, [out["job"]["id"]])
        self.assertEqual(len(jobs.jobs(rec["id"])), 1)
        caller = Caller(output(("ac1",)))
        done = jobs.run(out["job"]["id"], caller=caller)
        self.assertEqual((done["state"], len(caller.calls)), ("succeeded", 1))
        self.assertIn('"previous_plan"', caller.calls[0]["text"])
        rec = companion.get(rec["id"])
        cand_id = done["result"]["plan"]
        self.assertEqual((rec["selected_plan"], rec["stage"]), (manual_id, "approved"))  # never selected by itself
        frozen = json.loads(before)
        self.assertEqual(rec["plans"][manual_id], frozen["plans"][manual_id])
        self.assertEqual(rec["approvals"], frozen["approvals"])
        cand = rec["candidates"][cand_id]
        self.assertEqual((cand["parent"], cand["state"]), (manual_id, "candidate"))
        self.assertIn("Manual plan: filter orders.", cand["diff"]["text"])
        self.assertEqual(len([e for e in rec["evaluations"].values() if e.get("job") == done["id"]]), 1)
        rec = self.act(rec, "select_candidate", plan_id=cand_id)
        self.assertEqual((rec["selected_plan"], rec["stage"], rec["approved_plan"]), (cand_id, "review_incomplete", None))
        self.assertEqual(len(rec["approvals"]), 1)  # the old approval is history, not inherited

    def test_failure_cancel_and_late_response_keep_prior_versions_byte_identical(self):
        rec = self.planned(approve=True)
        before = self.frozen(rec)
        failed = self.act(companion.get(rec["id"]), "improve_plan")["job"]
        done = jobs.run(failed["id"], caller=Caller(extract.ExtractError("claude rc=1: provider said no")))
        self.assertEqual((done["state"], done["reason"]), ("failed", "provider failure"))
        self.assertEqual(self.ledger()[failed["reservation"]]["actual"], 0.0)
        self.assertEqual(self.frozen(rec), before)
        late = self.act(companion.get(rec["id"]), "improve_plan")["job"]

        def cancel_meanwhile():
            cur = companion.get(rec["id"])
            jobs.cancel_job(cur["id"], cur["revision"], late["id"])
        caller = Caller(fn=cancel_meanwhile)
        done = jobs.run(late["id"], caller=caller)
        self.assertEqual((done["state"], len(caller.calls)), ("cancelled", 1))
        self.assertEqual(self.ledger()[late["reservation"]]["state"], "settled")  # spent, never "free"
        self.assertEqual(self.frozen(rec), before)
        self.assertFalse(companion.get(rec["id"]).get("candidates"))
        queued = self.act(companion.get(rec["id"]), "improve_plan")["job"]
        cur = companion.get(rec["id"])
        out = self.act(cur, "cancel_job", job=queued["id"])
        self.assertEqual(out["job"]["state"], "cancelled")
        self.assertEqual(self.ledger()[queued["reservation"]]["state"], "released")
        never = Caller()
        self.assertEqual(jobs.run(queued["id"], caller=never)["state"], "cancelled")
        self.assertEqual(never.calls, [])  # cancellation prevents the call
        self.assertEqual(self.frozen(rec), before)


class A45Recovery(Base):
    def test_timeout_is_outcome_unknown_keeps_its_reservation_and_needs_explicit_retry(self):
        rec = self.create()
        job = self.act(rec, "generate_plan")["job"]
        done = jobs.run(job["id"], caller=Caller(extract.ExtractError("claude rc=124: timeout after 180s")))
        self.assertEqual((done["state"], done["reason"]), ("outcome_unknown", "timeout after possible acceptance"))
        self.assertEqual(self.ledger()[job["reservation"]]["state"], "unknown")
        self.assertIn(job["reservation"], modelbudget.companion_summary(common.load_config())["unknown"])
        with self.assertRaises(ValueError):
            modelbudget.release(job["reservation"])  # never released as known-free
        rec = companion.get(rec["id"])
        self.assertEqual((rec["stage"], rec["plans"]), ("new", {}))
        with self.assertRaisesRegex(ValueError, "Outcome unknown"):
            self.act(rec, "generate_plan")
        retry = self.act(companion.get(rec["id"]), "generate_plan", retry=True)["job"]
        self.assertEqual(retry["retry_of"], job["id"])
        self.assertNotEqual(retry["attempt"], job["attempt"])

    def test_rate_limit_and_provider_failure_leave_a_recoverable_task(self):
        rec = self.create()
        job = self.act(rec, "generate_plan")["job"]
        done = jobs.run(job["id"], caller=Caller(extract.ExtractError("usage limit reached", limit=True)))
        self.assertEqual((done["state"], done["reason"]), ("failed", "rate limited or quota reached"))
        rec = companion.get(rec["id"])
        self.assertEqual((rec["stage"], rec["evaluations"]), ("new", {}))  # no false score
        again = self.act(rec, "generate_plan")["job"]
        self.assertEqual(jobs.run(again["id"], caller=Caller())["state"], "succeeded")

    def test_changed_requirements_mid_job_keep_a_historical_version(self):
        rec = self.planned()
        job = self.act(rec, "improve_plan")["job"]

        def rebaseline():
            cur = companion.get(rec["id"])
            companion.action({"action": "accept_requirement", "workflow": cur["id"], "revision": cur["revision"],
                              "clarification": "Archived orders are included", "rationale": "Agreed"})
        done = jobs.run(job["id"], caller=Caller(fn=rebaseline))
        self.assertEqual(done["state"], "superseded")
        rec = companion.get(rec["id"])
        old = rec["plans"][done["result"]["plan"]]
        self.assertNotEqual(old["baseline"], rec["baseline"])
        self.assertNotEqual(rec["selected_plan"], old["id"])
        self.assertFalse(rec.get("candidates"))
        with self.assertRaises(ValueError):
            companion.select_plan(rec["id"], rec["revision"], old["id"])  # historical, never current

    def test_concurrent_clicks_create_one_job(self):
        rec = self.planned()
        results, errors = [], []

        def click():
            try:
                results.append(jobs.enqueue(rec["id"], rec["revision"], "improve")["job"]["id"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=click) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(jobs.jobs(rec["id"])), 1)
        self.assertEqual(len(self.ledger()), 1)

    def test_incomplete_output_is_an_unselectable_draft(self):
        rec = self.create()
        job = self.act(rec, "generate_plan")["job"]
        done = jobs.run(job["id"], caller=Caller(output(steps=[])))
        self.assertEqual(done["state"], "partial")
        rec = companion.get(rec["id"])
        draft = done["result"]["draft"]
        self.assertEqual((rec["stage"], rec["plans"], rec["drafts"][draft]["state"]), ("new", {}, "incomplete"))
        with self.assertRaises((ValueError, KeyError)):
            companion.select_plan(rec["id"], rec["revision"], draft)
        bad = self.act(rec, "generate_plan")["job"]
        done = jobs.run(bad["id"], caller=Caller(output(criteria=("ac7",))))
        self.assertEqual(done["state"], "partial")
        self.assertIn("unknown criterion", done["reason"])

    def test_revoked_access_mid_job_publishes_nothing(self):
        rec = self.create()
        job = self.act(rec, "generate_plan")["job"]

        def revoke():
            self.configure(**{"profile.repos": [], "watched_repos": []})
        done = jobs.run(job["id"], caller=Caller(fn=revoke))
        self.assertEqual(done["state"], "superseded")
        self.assertEqual(workflowstore.load(rec["id"])["plans"], {})
        with self.assertRaises(PermissionError):
            companion.get(rec["id"])

    def test_restart_reads_never_retry_and_only_explicit_retry_restarts(self):
        rec = self.create()
        job = self.act(rec, "generate_plan")["job"]
        folder = os.path.join(workflowstore.root(rec["id"]), "jobs")
        snap = {n: open(os.path.join(folder, n), "rb").read() for n in os.listdir(folder)}
        for _ in range(3):  # status reads after a restart
            jobs.jobs(rec["id"])
            companion.resume(rec["id"])
            from chitragupta import companionplanning
            companionplanning.planner()
        self.assertEqual(snap, {n: open(os.path.join(folder, n), "rb").read() for n in os.listdir(folder)})
        self.assertEqual(self.spawned, [job["id"]])
        self.act(rec, "generate_plan")  # a repeated click focuses the job
        self.assertEqual(self.spawned, [job["id"]])
        self.act(rec, "generate_plan", retry=True)  # explicit retry of an undispatched job
        self.assertEqual(self.spawned, [job["id"], job["id"]])
        with mock.patch.object(jobs, "clock", lambda: time.time() + 3600):
            out = self.act(companion.get(rec["id"]), "cancel_job", job=job["id"])  # explicit action reconciles
        self.assertEqual(out["job"]["state"], "failed")  # its reservation expired before any dispatch
        self.assertEqual(self.ledger()[job["reservation"]]["state"], "released")
        self.assertEqual(companion.get(rec["id"])["stage"], "new")
        running = self.act(companion.get(rec["id"]), "generate_plan")["job"]
        with workflowstore.workflowio.lock():  # the runner claimed it, then the machine restarted mid-call
            j = jobs.load(rec["id"], running["id"])
            j.update(state="running", dispatched_at=common.now_iso(), deadline_epoch=time.time() + 300)
            jobs._write(j)
        self.assertEqual(jobs.run(running["id"], caller=Caller())["state"], "running")  # not re-dispatched
        with mock.patch.object(jobs, "clock", lambda: time.time() + 3600):
            with self.assertRaisesRegex(ValueError, "Outcome unknown"):
                self.act(companion.get(rec["id"]), "generate_plan")
        self.assertEqual(jobs.load(rec["id"], running["id"])["state"], "outcome_unknown")
        self.assertEqual(self.ledger()[running["reservation"]]["state"], "unknown")
        self.assertEqual(companion.get(rec["id"])["stage"], "new")

    def test_cancel_is_idempotent_and_the_task_cancel_reaches_queued_jobs(self):
        rec = self.create()
        job = self.act(rec, "generate_plan")["job"]
        stale = rec["revision"]
        out = jobs.cancel_job(rec["id"], stale + 1, job["id"])
        self.assertEqual((out["job"]["state"], out["stage"]), ("cancelled", "new"))
        again = jobs.cancel_job(rec["id"], stale, job["id"])  # a repeated click: no conflict, no change
        self.assertEqual((again["job"]["state"], again["revision"]), ("cancelled", out["revision"]))
        second = self.act(companion.get(rec["id"]), "generate_plan")["job"]
        cur = companion.get(rec["id"])
        cur = companion.cancel(cur["id"], cur["revision"], "changed my mind")  # Stage 1 task cancel
        self.assertEqual(cur["stage"], "new")
        never = Caller()
        self.assertEqual(jobs.run(second["id"], caller=never)["state"], "cancelled")
        self.assertEqual(never.calls, [])
        self.assertEqual(self.ledger()[second["reservation"]]["state"], "released")


class Evaluations(Base):
    def test_manual_assessment_is_a_developer_assessment_never_jev(self):
        rec = self.planned()
        rec = self.act(rec, "manual_assessment", plan_id=rec["selected_plan"],
                       classifications={"ac1": "Covered", "ac2": "Partial"}, note="Reviewed with the tester")
        ev = list(rec["evaluations"].values())[-1]
        self.assertEqual((ev["origin"], ev["provider"], ev["coverage"]["percent"]), ("developer assessment", None, 75.0))
        self.assertTrue(ev["coverage"]["label"].startswith("Developer assessment"))
        self.assertNotIn("Jev", json.dumps(ev))
        self.assertEqual(evidence.plan_coverage(rec, rec["selected_plan"])["percent"], 75.0)
        with self.assertRaises(ValueError):
            self.act(rec, "manual_assessment", plan_id=rec["selected_plan"], classifications={"ac9": "Covered"})

    def test_evaluate_with_jev_off_records_unavailable_without_a_job(self):
        rec = self.planned()
        rec = self.act(rec, "evaluate_plan")
        ev = list(rec["evaluations"].values())[-1]
        self.assertEqual((rec["stage"], ev["state"], ev["coverage"]["percent"], ev["reason"]),
                         ("review_incomplete", "unavailable", None, jev.UNAVAILABLE))
        self.assertEqual((jobs.jobs(rec["id"]), self.ledger(), self.spawned), ([], {}, []))
        rec = companion.review(rec["id"], rec["revision"], "Manual review; evaluation unavailable")
        self.assertEqual(rec["stage"], "plan_reviewed")  # unavailable Jev never blocks manual review

    def test_enabled_jev_uses_the_blocked_transport_and_stub_failures_keep_the_plan(self):
        cfg = self.configure(**{"jev.enabled": True, "jev.credential_env": "TYPESAFE_API_KEY", "jev.call_usd": 0.03})
        with workflowstore.workflowio.lock():
            workflowstore.workflowio.durable_json(common.hp("data", "workflows", ".jev-ack.json"),
                                                  {"digest": jev.disclosure(cfg)["digest"]})
        rec = self.planned()
        before = self.frozen(rec)
        job = self.act(rec, "evaluate_plan")["job"]
        self.assertEqual(self.ledger()[job["reservation"]]["purpose"], "jev")
        done = jobs.run(job["id"])  # default transport: blocked
        rec = companion.get(rec["id"])
        ev = rec["evaluations"][done["result"]["evaluation"]]
        self.assertEqual((ev["state"], ev["coverage"]["percent"], ev["reason"]), ("unavailable", None, jev.UNAVAILABLE))
        self.assertEqual(self.ledger()[job["reservation"]]["state"], "released")  # nothing was sent
        replies = {"401": (401, {}, b""), "429": (429, {"Retry-After": "0"}, b"")}
        for name, reply in replies.items():
            cur = companion.get(rec["id"])
            job = self.act(cur, "evaluate_plan")["job"]
            calls = []

            def stub(body, timeout, reply=reply):
                calls.append(timeout)
                return reply
            done = jobs.run(job["id"], transport=stub)
            ev = companion.get(rec["id"])["evaluations"][done["result"]["evaluation"]]
            self.assertEqual((ev["state"], ev["coverage"]["percent"]), ("unavailable", None), name)
            self.assertEqual(len(calls), 3 if name == "429" else 1)
        cur = companion.get(rec["id"])
        job = self.act(cur, "evaluate_plan")["job"]

        def good(body, timeout):
            req = json.loads(body)
            rows = [{"name": q["name"], "choice": "Covered" if q["name"].startswith("criterion:") else q["choices"][0],
                     "confidence": 0.9} for q in req["questions"]]
            return 200, {}, json.dumps({"model": jev.MODEL, "answers": rows, "usage": {"cost_usd": 0.02}}).encode()
        done = jobs.run(job["id"], transport=good)
        rec = companion.get(rec["id"])
        ev = rec["evaluations"][done["result"]["evaluation"]]
        self.assertEqual((ev["state"], ev["coverage"]["percent"], ev["origin"]), ("available", 100.0, "Jev classification"))
        self.assertEqual(self.ledger()[job["reservation"]]["actual"], 0.02)
        frozen = json.loads(before)
        self.assertEqual(rec["plans"], frozen["plans"])  # the exact plan is preserved throughout


RACE = r"""
import json, os, sys, time
from chitragupta import common, modelbudget
folder, tag = sys.argv[1], sys.argv[2]
cfg = common.load_config()
open(os.path.join(folder, "ready-" + tag), "w").close()
deadline = time.monotonic() + 60  # never outlive a failed parent
while not os.path.exists(os.path.join(folder, "go")):
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.005)
try:
    r = modelbudget.reserve("companion_plan", 0.3, cfg, task="ABC-1001")
    print(json.dumps({"ok": r["id"]}))
except modelbudget.Refused as exc:
    print(json.dumps({"refused": str(exc)}))
"""


def kill(proc):
    if proc.poll() is None:
        proc.kill()
    try:
        proc.communicate(timeout=10)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


class A61Budget(Base):
    def health(self, day, usd, by_purpose):
        common.write_json(common.wl("health.json"), {"model_usage": {day: {"usd": usd, "calls": 3, "by_purpose": by_purpose}}})

    def test_two_processes_race_the_last_budget(self):
        self.configure(**{"budget.model_daily_usd": 1.0})
        real_day = common.workday(dt.datetime.now().astimezone(), {})
        self.health(real_day, 0.6, {"extract": {"usd": 0.6}})
        signals = os.path.join(self.user_dir, "race")
        os.makedirs(signals)
        procs = []
        self.addCleanup(lambda: [kill(p) for p in procs])
        for tag in ("one", "two"):
            procs.append(subprocess.Popen([PY, "-c", RACE, signals, tag], cwd=ROOT, env=self.child_env(),
                                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        deadline = time.monotonic() + 60
        while not all(os.path.exists(os.path.join(signals, "ready-" + t)) for t in ("one", "two")):
            self.assertLess(time.monotonic(), deadline, "children did not start")
            self.assertEqual([p.poll() for p in procs], [None, None], "a child exited early")
            time.sleep(0.02)
        open(os.path.join(signals, "go"), "w").close()
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode())
            results.append(json.loads(out.decode().strip().splitlines()[-1]))
        self.assertEqual(sorted("ok" in r for r in results), [False, True])
        entries = modelbudget._ledger()["entries"]
        reserved = sum(e["usd"] for e in entries.values() if e["state"] == "reserved")
        self.assertLessEqual(0.6 + reserved, 1.0 + 1e-9)  # reserved + spent never exceeds the cap

    def test_cap_counts_each_spend_once_and_jev_subcaps(self):
        cfg = self.configure(**{"budget.model_daily_usd": 2.0})
        day = common.workday(common.now(), cfg)
        first = modelbudget.reserve("companion_plan", 0.5, cfg, task="ABC-1001")
        modelbudget.settle(first["id"], 0.5)
        # health also recorded that call under its purpose: the ledger owns it, so it is not counted twice
        self.health(day, 1.5, {"extract": {"usd": 1.0}, "companion_plan": {"usd": 0.5}})
        self.assertEqual(modelbudget.other_spend(cfg), 1.0)
        modelbudget.reserve("companion_improve", 0.4, cfg, task="ABC-1001")
        with self.assertRaisesRegex(modelbudget.Refused, "cap"):
            modelbudget.reserve("companion_improve", 0.2, cfg, task="ABC-1001")
        cfg = self.configure(**{"budget.model_daily_usd": 10.0})
        a = modelbudget.reserve("jev", 0.1, cfg, task="ABC-1001")
        with self.assertRaisesRegex(modelbudget.Refused, "per-task"):
            modelbudget.reserve("jev", 0.05, cfg, task="ABC-1001")
        for key in ("ABC-1002", "ABC-1004", "ABC-1005", "ABC-1006"):
            modelbudget.reserve("jev", 0.1, cfg, task=key)
        with self.assertRaisesRegex(modelbudget.Refused, "daily subcap"):
            modelbudget.reserve("jev", 0.01, cfg, task="ABC-1007")
        modelbudget.mark_unknown(a["id"])
        summary = modelbudget.companion_summary(cfg)
        self.assertEqual((summary["jev"]["used"], summary["jev"]["daily"], summary["unknown"]), (0.5, 0.5, [a["id"]]))
        self.assertEqual(summary["by_purpose"]["jev"]["unknown"], 0.1)
        self.assertEqual(modelbudget.call_ceiling({}, "companion_plan")[0], None)
        self.assertIn("jev.call_usd", modelbudget.call_ceiling({}, "jev")[1])

    def test_two_paid_jobs_at_most_across_the_profile(self):
        recs = [self.create(k) for k in ("ABC-1001", "ABC-1002", "ABC-1004")]
        self.act(recs[0], "generate_plan")
        self.act(recs[1], "generate_plan")
        with self.assertRaisesRegex(modelbudget.Refused, "Busy"):
            self.act(recs[2], "generate_plan")
        self.assertEqual(companion.get(recs[2]["id"])["stage"], "new")
        self.assertEqual(jobs.jobs(recs[2]["id"]), [])


class AgentDispatch(Base):
    def test_agent_runs_one_job_and_the_spawn_is_a_guarded_self_call(self):
        self.assertEqual(agent.main(["companion-job", "cj_" + "0" * 32]), 2)
        rec = self.create()
        job = self.act(rec, "generate_plan")["job"]
        with mock.patch.object(jobs, "_claude", Caller()):
            self.assertEqual(agent.main(["companion-job", job["id"]]), 0)
        self.assertEqual(jobs.load(rec["id"], job["id"])["state"], "succeeded")
        seen = []
        with mock.patch.object(commands, "spawn", lambda argv, **kw: seen.append(argv)):
            REAL_SPAWN(job["id"])
        self.assertEqual(seen[0][-3:], ["agent", "companion-job", job["id"]])
        self.assertIsNone(commands.reason(seen[0], "self", cfg=common.load_config()))


REAL_SPAWN = jobs.spawn_runner


if __name__ == "__main__":
    unittest.main()
