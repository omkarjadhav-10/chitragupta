"""Durable workflow core: STATE-01, ARCH-05B, EXEC-01, FLOW-15/16, RESUME-01/02 (A46-A51, A62-A75, A78).

Synthetic data only (ABC-…, acme.test), frozen clocks, isolated data homes. Every process start is a
tripwire: no provider, model, Jira, git or database call can happen here.
"""
import datetime as dt
import errno
import io
import json
import os
import subprocess
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests.support import PY, ROOT, Isolated, rmtree, write_config
from chitragupta import (common, companion, companionevidence as evidence, companionexecution as execution,
                         companionviews, jev, store, testruns, workflowmodel as model, workflowstore)
from chitragupta.safety import commands, workflowio

T0 = dt.datetime(2026, 11, 8, 10, 0, tzinfo=dt.timezone.utc)
HEAD1, HEAD2 = "a1b2c3d4e5f6", "0f9e8d7c6b5a"
JIRA = "https://jira.acme.test"
THREE = [{"id": "one", "text": "Add order filter", "criteria": ["ac1"]},
         {"id": "two", "text": "Persist filter choice", "criteria": ["ac2"]},
         {"id": "three", "text": "Show filter badge", "criteria": ["ac3"]},
         {"id": "check", "text": "Verify all criteria", "kind": "verification", "criteria": ["ac1", "ac2", "ac3"],
          "depends_on": ["one", "two", "three"]}]


def plan_body(steps=None, text="Implement the agreed behavior and verify its result."):
    return {"text": text, **{s: "Recorded design detail" for s in model.REQUIRED_SECTIONS},
            "steps": steps or [{"id": "one", "text": "Implement behavior", "criteria": ["ac1"]},
                               {"id": "two", "text": "Check result", "kind": "verification", "criteria": ["ac1"],
                                "depends_on": ["one"]}]}


def criteria(n):
    return [{"id": "ac%d" % i, "text": "Expected behavior %d" % i} for i in range(1, n + 1)]


class Base(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.repo = os.path.realpath(os.path.join(common.home(), "scratch"))
        self.repo_b = os.path.realpath(os.path.join(common.home(), "other"))
        os.makedirs(self.repo)
        os.makedirs(self.repo_b)
        self.cfg = {"profile": {"accounts": [{"provider": "jira", "instance": JIRA, "id": "dev-a"}],
                                "repos": [self.repo, self.repo_b],
                                "include_tickets": {"ABC-1001": "My task", "ABC-1002": "Other task"}},
                    "watched_repos": [self.repo, self.repo_b], "git_author_emails": ["dev@acme.test"]}
        write_config(common.home(), self.cfg)
        for key in ("ABC-1001", "ABC-1002"):
            common.write_json(store.ticket_path(key), store.new_ticket(key))
        self.now = T0
        clock = mock.patch.object(common, "now", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.processes = []

        def no_process(*args, **kwargs):
            self.processes.append(args[:1])
            raise AssertionError("workflow code started a process")
        for name in ("run", "spawn"):
            trip = mock.patch.object(commands, name, no_process)
            trip.start()
            self.addCleanup(trip.stop)
        self.addCleanup(lambda: self.assertEqual(self.processes, [], "no git, test, Jira or model process"))

    # ---- fixtures
    def advance(self, **kw):
        self.now = self.now + dt.timedelta(**kw)

    def stamp(self, **kw):
        return (self.now + dt.timedelta(**kw)).isoformat(timespec="seconds")

    def tk(self, key="ABC-1001"):
        return common.read_json(store.ticket_path(key))

    def put(self, key="ABC-1001", **fields):
        tk = self.tk(key)
        tk.update(fields)
        common.write_json(store.ticket_path(key), tk)

    def create(self, key="ABC-1001", repo=None, n=1):
        return companion.create(key, repo or self.repo, JIRA, criteria(n))

    def planned(self, key="ABC-1001", repo=None, steps=None, n=1):
        rec = self.create(key, repo, n)
        return companion.save_plan(rec["id"], rec["revision"], plan_body(steps))

    def approve(self, rec):
        digest = companion.resume(rec["id"])["context_digest"]
        return companion.approve(rec["id"], rec["revision"], rec["selected_plan"], digest, True, "reviewed-copy")

    def session(self, sid="sess-1", key="ABC-1001", repo=None):
        """Captured activity of a submitted copy: existing ingestion records, never a transcript."""
        state = common.read_json(common.wl("state.json"), {}) or {}
        state.setdefault("sessions", {})[sid] = {"repo_root": repo or self.repo, "last_event": self.stamp(minutes=1)}
        common.write_json(common.wl("state.json"), state)
        tk = self.tk(key)
        tk["items"]["it-" + sid] = {"id": "it-" + sid, "kind": "note", "title": "Captured work", "date": "2026-11-08",
                                    "ref": "raw/2026-11-08/%s.jsonl:3" % sid}
        common.write_json(store.ticket_path(key), tk)
        return sid

    def associate(self, rec, sid="sess-1", key="ABC-1001", repo=None):
        self.session(sid, key, repo)
        return execution.associate(rec["id"], rec["revision"], sid)

    def started(self, key="ABC-1001", repo=None, steps=None, n=1, sid="sess-1"):
        rec = self.approve(self.planned(key, repo, steps, n))
        return self.associate(rec, sid, key, repo)

    def observe(self, rec, head=HEAD1, worktree="clean", **extra):
        body = {"head": head, "worktree": worktree, "config": "cfg-1", "environment": "local", **extra}
        return companion.observe_context(rec["id"], rec["revision"], body)

    def captured_test(self, rec, tid, status="passed", head=HEAD1, context=True, at=None, environment="local"):
        tk = self.tk(rec["binding"]["ticket"])
        row = {"id": tid, "at": at or self.stamp(), "day": "2026-11-08", "session": "sess-1",
               "command": "python -m unittest tests.test_shop", "status": status}
        if context:
            row["context"] = {"repo_id": rec["binding"]["repo_id"], "location": rec["binding"]["location"],
                              "head": head, "worktree": "clean", "config": "cfg-1", "environment": environment}
        tk["tests"][tid] = row
        common.write_json(store.ticket_path(rec["binding"]["ticket"]), tk)

    def pr(self, number=7, state="open", review="", head=None, key="ABC-1001", repo="scratch"):
        tk = self.tk(key)
        git = tk.get("git") or {"repos": {}}
        prs = [p for p in git["repos"].get(repo, {}).get("prs", []) if p["number"] != number]
        prs.append({"number": number, "state": state, "review": review, "source": "feature/" + key,
                    "target": "main", "title": "Order filter", "head": head})
        git["repos"].setdefault(repo, {"branches": []})["prs"] = prs
        git["checked_at"] = self.stamp()
        tk["git"] = git
        common.write_json(store.ticket_path(key), tk)

    def tick(self, *keys):
        return evidence.tick(common.load_config(), set(keys or ("ABC-1001",)))

    def map(self, rec, **mapping):
        return companion.record_evidence(rec["id"], rec["revision"], mapping)

    def ledger(self, rec):
        return {r["criterion"]: r["status"] for r in rec["verifications"][-1]["rows"]}

    def files(self):
        out = set()
        for dp, _, fs in os.walk(common.hp("data", "workflows")):
            out |= {os.path.join(dp, f) for f in fs}
        return out


class A65Transitions(Base):
    def test_every_table_row_and_only_those(self):
        triggers = {r[1] for r in model.RULES}
        for frm, trigger, to in model.RULES:
            for stage in frm:
                rec = {"stage": stage}
                model.transition(rec, to, trigger)
                self.assertEqual(rec["stage"], to)
        allowed = {(s, t, to) for frm, t, to in model.RULES for s in frm}
        for stage in model.STAGES:
            for target in model.STAGES:
                for trigger in triggers:
                    if (stage, trigger, target) in allowed:
                        continue
                    with self.assertRaisesRegex(ValueError, "allowed next stages"):
                        model.transition({"stage": stage}, target, trigger)

    def test_invalid_jumps_fail(self):
        for start, target, trigger in (("new", "completed", None), ("new", "completed", "complete"),
                                       ("review_incomplete", "approved", "evaluation"),
                                       ("pr_review", "completed", "observed_pr"),
                                       ("approved", "in_development", None), ("paused", "in_development", "resume"),
                                       ("completed", "approved", None)):
            with self.assertRaises(ValueError):
                model.transition({"stage": start}, target, trigger)
        rec = self.create()
        with self.assertRaisesRegex(ValueError, "complete only from"):
            companion.complete(rec["id"], rec["revision"], "Done in Jira")
        self.assertEqual(companion.get(rec["id"])["stage"], "new")

    def test_failed_evaluation_never_approves_without_intent(self):
        rec = self.planned()
        rec["evaluations"]["e1"] = dict(jev.validate_decisions(rec["baselines"][rec["baseline"]], [], available=False),
                                        plan=rec["selected_plan"], state="failed")
        rec = workflowstore.save(rec, rec["revision"], companion.authorize)
        digest = companion.resume(rec["id"])["context_digest"]
        for risks in (False, None, "yes"):
            with self.assertRaisesRegex(ValueError, "acknowledge"):
                companion.approve(rec["id"], rec["revision"], rec["selected_plan"], digest, risks, "s1")
        self.assertEqual(companion.get(rec["id"])["stage"], "planned")
        # Below 100% (no coverage at all) execution is allowed only with explicit intent.
        rec = companion.approve(rec["id"], rec["revision"], rec["selected_plan"], digest, True, "s1")
        self.assertEqual((rec["stage"], rec["approvals"][-1]["evaluation"]), ("approved", "e1"))

    def test_restart_never_jumps_to_executing(self):
        rec = self.approve(self.planned())
        for _ in range(3):
            companion.resume(rec["id"])
        self.assertEqual(workflowstore.load(rec["id"])["stage"], "approved")
        with self.assertRaisesRegex(ValueError, "no captured work"):
            execution.associate(rec["id"], rec["revision"], "sess-unknown")
        self.assertEqual(workflowstore.load(rec["id"])["revision"], rec["revision"])

    def test_full_walk_through_the_table(self):
        rec = self.create()
        steps = []

        def did(new, *expect):
            nonlocal rec
            self.assertEqual(new["revision"], rec["revision"] + 1, "one checkpoint per action")
            self.assertEqual(new["stage"], expect[-1])
            got = [h["to"] for h in new["history"][len(rec["history"]):]]
            self.assertEqual(got, list(expect))
            steps.extend(got)
            rec = new
        did(companion.save_plan(rec["id"], rec["revision"], plan_body()), "planning", "planned")
        did(companion.review(rec["id"], rec["revision"], "Reviewed manually"), "review_incomplete", "plan_reviewed")
        did(companion.select_plan(rec["id"], rec["revision"], rec["selected_plan"]), "review_incomplete")
        did(self.approve(rec), "approved")
        did(execution.review_again(rec["id"], rec["revision"]), "review_incomplete")
        self.assertEqual(rec["approvals"][-1]["dispatch"], "cancelled")
        self.assertIsNone(rec["approved_plan"])
        did(self.approve(rec), "approved")
        did(execution.record_dispatch(rec["id"], rec["revision"], rec["approvals"][-1]["attempt"], "copied"),
            "execution_ready")
        self.assertEqual(rec["approvals"][-1]["dispatch"], "prepared")
        did(self.associate(rec), "in_development")
        did(companion.pause(rec["id"], rec["revision"], "Stopping for the day"), "paused")
        self.advance(minutes=1)
        cand = companion.resume(rec["id"])
        did(execution.approve_continuation(rec["id"], rec["revision"], cand["id"], True, "reviewed-copy"),
            "execution_ready")
        did(self.associate(rec, "sess-2"), "in_development")
        did(companion.request_verification(rec["id"], rec["revision"], ["Run the order filter tests"]),
            "awaiting_verification")
        self.pr(state="open")
        self.assertEqual(self.tick(), 1)
        new = workflowstore.load(rec["id"])
        self.assertEqual((new["stage"], new["history"][-1]["trigger"]), ("pr_review", "observed_pr"))
        self.assertTrue(new["obligations"], "unfinished work stays listed")
        steps.append("pr_review")
        rec = new
        self.advance(minutes=1)
        cand = companion.resume(rec["id"])
        did(execution.approve_continuation(rec["id"], rec["revision"], cand["id"], True, "reviewed-copy"),
            "execution_ready")
        did(self.associate(rec, "sess-3"), "pr_review")
        with self.assertRaisesRegex(ValueError, "Open mandatory gaps"):
            companion.complete(rec["id"], rec["revision"], "Looks done")
        rec = self.observe(rec)
        self.captured_test(rec, "t1")
        rec = self.map(rec, evidence="test:t1", criteria=["ac1"], coverage="complete")
        self.pr(state="merged", head=HEAD1)
        did(companion.complete(rec["id"], rec["revision"], "All criteria verified on the current revision"),
            "completed")
        self.assertEqual(len(rec["completions"]), 1)
        did(companion.reopen(rec["id"], rec["revision"], "awaiting_verification", "UAT reported a regression"),
            "awaiting_verification")
        self.assertEqual(len(rec["completions"]), 1, "completion history kept")
        self.assertEqual(steps[-3:], ["pr_review", "completed", "awaiting_verification"])

    def test_remaining_rows_and_overlays_keep_stage(self):
        rec = self.planned()
        rec = self.approve(rec)
        self.assertEqual(rec["history"][-1], dict(rec["history"][-1], **{"from": "planned", "to": "approved"}))
        rec = self.associate(rec)
        rec = companion.pause(rec["id"], rec["revision"], "Switching")
        # awaiting_verification -> paused and completed -> paused (development needed)
        rec = self.associate(execution.approve_continuation(
            rec["id"], rec["revision"], companion.resume(rec["id"])["id"], True, "copy",
            target_stage="awaiting_verification"), "sess-2")
        self.assertEqual(rec["stage"], "awaiting_verification")
        rec = companion.pause(rec["id"], rec["revision"], "Waiting for QA")
        self.assertEqual((rec["stage"], rec["resume_stage"]), ("paused", "awaiting_verification"))
        rec = companion.cancel(rec["id"], rec["revision"], "Not today")
        self.assertEqual(rec["stage"], "paused")
        self.assertEqual(rec["overlays"]["cancelled"]["stage"], "paused")
        self.assertIs(companion.cancel(rec["id"], rec["revision"] - 1, "again")["revision"], rec["revision"])
        # planning -> new on cancel (a generation job is the only way into planning)
        other = self.create("ABC-1002", self.repo_b)
        other["stage"] = "planning"
        other = workflowstore.save(other, other["revision"], companion.authorize)
        other = companion.cancel(other["id"], other["revision"], "Stop generating")
        self.assertEqual((other["stage"], other["history"][-1]["trigger"]), ("new", "planning_failed"))

    def test_completed_reopens_to_paused_development(self):
        rec = self.started()
        rec = companion.request_verification(rec["id"], rec["revision"], ["Manual QA"])
        rec = self.observe(rec)
        self.captured_test(rec, "t1")
        rec = self.map(rec, evidence="test:t1", criteria=["ac1"], coverage="complete")
        with self.assertRaisesRegex(ValueError, "record why a PR is not required"):
            companion.complete(rec["id"], rec["revision"], "Verified")
        rec = companion.complete(rec["id"], rec["revision"], "Verified", pr_not_required="Configuration-only repo")
        self.assertEqual(rec["completions"][-1]["pr"], {"not_required": "Configuration-only repo"})
        with self.assertRaisesRegex(ValueError, "explain"):
            companion.reopen(rec["id"], rec["revision"], "paused", "")
        rec = companion.reopen(rec["id"], rec["revision"], "paused", "Requirement reopened")
        self.assertEqual((rec["stage"], rec["resume_stage"]), ("paused", "in_development"))
        with self.assertRaises(ValueError):
            companion.reopen(rec["id"], rec["revision"], "pr_review", "again")

    def test_archive_retains_history_and_is_not_completion(self):
        rec = self.planned()
        before = len(self.files())
        rec = companion.archive(rec["id"], rec["revision"], "Superseded by ABC-1002")
        self.assertEqual(rec["stage"], "planned")
        self.assertTrue(rec["archived"])
        self.assertGreater(len(self.files()), before)
        with self.assertRaisesRegex(ValueError, "archived"):
            companion.review(rec["id"], rec["revision"], "late")
        self.assertFalse(companion.rollup("ABC-1001")["complete"])

    def test_actions_are_allowlisted(self):
        rec = self.create()
        for name in ("pin_ticket", "select", "begin_dispatch", "delete", "run_tests"):
            with self.assertRaisesRegex(ValueError, "unknown"):
                companion.action({"action": name, "workflow": rec["id"], "revision": rec["revision"]})
        out = companion.action({"action": "observe_context", "workflow": rec["id"], "revision": rec["revision"],
                                "observation": {"head": HEAD1, "worktree": "dirty"}})
        self.assertEqual(out["observations"][-1]["label"], "developer-recorded")


class A46Authorization(Base):
    def test_exact_version_and_confirmation_saved_before_any_handoff(self):
        rec = self.planned()
        digest = companion.resume(rec["id"])["context_digest"]
        with self.assertRaisesRegex(ValueError, "exact selected"):
            companion.approve(rec["id"], rec["revision"], "p_" + "0" * 64, digest, True, "s1")
        with self.assertRaisesRegex(ValueError, "context changed"):
            companion.approve(rec["id"], rec["revision"], rec["selected_plan"], "0" * 64, True, "s1")
        with self.assertRaisesRegex(ValueError, "no current approved handoff"):
            companion.packet(rec["id"])
        rec = companion.approve(rec["id"], rec["revision"], rec["selected_plan"], digest, True, "s1")
        intent = rec["approvals"][-1]
        self.assertEqual(intent["plan_digest"], model.digest(rec["plans"][rec["selected_plan"]]))
        self.assertEqual((intent["dispatch"], intent["checkpoint_revision"]), ("prepared", rec["revision"]))
        self.assertEqual(common.parse_ts(intent["expires_at"]) - common.parse_ts(intent["at"]), dt.timedelta(minutes=5))
        self.assertEqual(intent["target"]["location"], self.repo)
        self.assertEqual(intent["profile_generation"], rec["binding"]["profile_generation"])
        self.assertTrue(intent["attempt"].startswith("x_"))
        text = companion.packet(rec["id"])
        self.assertIn(plan_body()["text"], text)
        self.assertIn("Prepared; not submitted", text)
        self.assertIn(intent["attempt"], text)
        self.assertEqual(companion.get(rec["id"])["revision"], rec["revision"], "copy reads never write")


class A47InterruptedPlanning(Base):
    def test_restart_shows_saved_plan_and_only_improve_or_execute(self):
        rec = self.planned()
        rec = companion.review(rec["id"], rec["revision"], "Half reviewed")
        files = self.files()
        reads = [companion.resume(rec["id"]) for _ in range(3)]
        self.assertEqual(reads[0], reads[2])
        self.assertEqual(reads[0]["situation"], "A")
        self.assertEqual(reads[0]["available_actions"], ["View Previous Plan", "Improve Plan", "Execute Plan"])
        self.assertEqual(self.files(), files, "no regeneration, no new version")
        self.put(commits={"c1": {"sha": "c1", "subject": "Draft", "date": self.stamp(), "repo_name": "scratch"}})
        rec = companion.checkpoint(rec["id"], rec["revision"])
        self.assertIn("Review Changes", companion.resume(rec["id"])["available_actions"])
        self.assertNotIn("Generate Plan", companion.resume(rec["id"])["available_actions"])


class A48InterruptedDevelopment(Base):
    def test_continuation_states_and_explicit_resume(self):
        rec = self.started(steps=THREE, n=3)
        rec = self.observe(rec)
        rec = self.map(rec, manual={"kind": "code_inspection", "outcome": "passed", "text": "Filter implemented",
                                    "revision": HEAD1, "limits": "Read the diff only"}, steps=["one"])
        rec = self.map(rec, manual={"kind": "code_inspection", "outcome": "failed", "text": "Persistence missing",
                                    "revision": HEAD1, "limits": "Read the diff only"}, steps=["two"])
        self.put(commits={"c1": {"sha": "c1", "subject": "ABC-1001 filter", "date": self.stamp(), "repo_name": "scratch"}},
                 files={"src/filter.py": {"path": "src/filter.py", "changed": True, "last": "2026-11-08", "repo": "scratch"}})
        self.tick()
        rec = workflowstore.load(rec["id"])
        files = self.files()
        reads = [companion.resume(rec["id"]) for _ in range(3)]
        self.assertEqual(reads[0]["id"], reads[2]["id"])
        self.assertEqual(self.files(), files)
        cand = reads[0]
        self.assertEqual(cand["situation"], "B")
        self.assertIn("Resume Execution", cand["available_actions"])
        self.assertEqual([c["step"] for c in cand["continuation"]["carry_forward"]], ["one"])
        self.assertEqual(cand["continuation"]["remaining"], ["three", "two"])
        self.assertEqual(cand["progress"]["uncertain"], ["two"])
        self.assertEqual(cand["measures"]["implementation_progress"]["numerator"], 1)
        self.assertEqual(cand["measures"]["implementation_progress"]["denominator"], 3)
        self.assertIn("commit:c1", cand["unread"])
        self.assertEqual(cand["execution"], "not authorized by resume")
        verified_before = list(rec["verifications"])
        rec = execution.approve_continuation(rec["id"], rec["revision"], cand["id"], True, "reviewed-copy")
        self.assertEqual(rec["stage"], "execution_ready")
        self.assertEqual(rec["approvals"][-1]["continuation"]["id"], cand["id"])
        self.assertEqual(rec["approved_plan"], rec["approvals"][0]["plan"], "approved plan kept")
        self.assertEqual(rec["verifications"], verified_before)
        with self.assertRaises(workflowstore.Conflict):
            execution.approve_continuation(rec["id"], rec["revision"] - 1, cand["id"], True, "reviewed-copy")


class A49TestingPrSwitch(Base):
    def test_testing_resumes_as_verification(self):
        rec = self.started()
        rec = companion.request_verification(rec["id"], rec["revision"], ["Run UAT checklist"])
        rec = companion.pause(rec["id"], rec["revision"], "QA tomorrow")
        cand = companion.resume(rec["id"])
        self.assertEqual(cand["situation"], "C")
        self.assertEqual(cand["continuation"]["remaining"], [])
        self.assertIn("Verification work only", cand["continuation"]["scope"])
        self.assertEqual(cand["available_actions"][0], "Verify Requirements")
        self.assertNotIn("Generate Plan", cand["available_actions"])

    def test_existing_pr_resumes_review_without_another_pr(self):
        rec = self.started()
        self.pr(state="draft")
        self.tick()
        cand = companion.resume(rec["id"])
        self.assertEqual((cand["situation"], cand["next_action"]), ("D", "Review existing PR"))
        self.assertFalse([a for a in cand["available_actions"] if "Create" in a or "Generate" in a])
        self.assertEqual([p["id"] for p in cand["prs"]], ["pr:scratch:7"])

    def test_two_tickets_keep_independent_checkpoints_and_attribution(self):
        a = self.started()
        b = self.planned("ABC-1002", self.repo_b)
        cand = companion.resume(b["id"])
        self.assertEqual(cand["switch"], [{"workflow": a["id"], "ticket": "ABC-1001", "stage": "in_development"}])
        self.assertIn("Pause current task", cand["available_actions"])
        items = self.tk("ABC-1001")["items"]
        head_a = common.read_json(os.path.join(workflowstore.root(a["id"]), "head.json"))
        a = companion.pause(a["id"], a["revision"], "Switch to ABC-1002")
        b = self.associate(self.approve(b), "sess-b", "ABC-1002", self.repo_b)
        self.assertEqual(b["stage"], "in_development")
        self.assertNotEqual(common.read_json(os.path.join(workflowstore.root(a["id"]), "head.json")), head_a)
        head_a = common.read_json(os.path.join(workflowstore.root(a["id"]), "head.json"))
        b = companion.pause(b["id"], b["revision"], "Back to ABC-1001")
        self.assertEqual(common.read_json(os.path.join(workflowstore.root(a["id"]), "head.json")), head_a)
        self.assertEqual(self.tk("ABC-1001")["items"], items, "earlier attribution never relabelled")
        self.assertEqual(companion.resume(a["id"])["situation"], "B")


class A50Recovery(Base):
    def test_concurrent_edit_conflicts_recoverably(self):
        rec = self.planned()
        companion.review(rec["id"], rec["revision"], "Session one")
        with self.assertRaises(workflowstore.Conflict) as caught:
            companion.save_plan(rec["id"], rec["revision"], plan_body(text="Session two draft"))
        self.assertEqual(caught.exception.current["stage"], "plan_reviewed")
        self.assertEqual(caught.exception.unsaved, {"expected_revision": rec["revision"]})

    def test_specific_recovery_states(self):
        rec = self.started()
        cand = companion.resume(rec["id"])
        self.assertEqual(cand["handoff"]["direct"], False)
        self.assertIn("unsupported", cand["handoff"]["reason"])
        states = {r["state"] for r in cand["recovery"]}
        self.assertIn("requirements_unavailable", states)
        common.write_json(common.hp("data", "jira", "ABC-1001.json"),
                          {"key": "ABC-1001", "fetched_at": self.stamp(), "card": {"summary": "Changed", "status": "Done"}})
        self.assertIn("requirements_changed", {r["state"] for r in companion.resume(rec["id"])["recovery"]})
        os.rename(self.repo, self.repo + "-moved")
        try:
            cand = companion.resume(rec["id"])
            self.assertIn("repository_missing", {r["state"] for r in cand["recovery"]})
            self.assertIn("Map the moved clone", cand["next_action"])
        finally:
            os.rename(self.repo + "-moved", self.repo)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["repos"] = [self.repo_b]
        write_config(common.home(), cfg)
        with self.assertRaises(PermissionError):
            companion.resume(rec["id"])


class A62GuardedHandoff(Base):
    def test_failure_to_save_dispatches_nothing(self):
        rec = self.planned()
        digest = companion.resume(rec["id"])["context_digest"]
        with mock.patch.object(workflowio, "durable_json", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaisesRegex(workflowstore.NotSaved, "Not saved"):
                companion.approve(rec["id"], rec["revision"], rec["selected_plan"], digest, True, "s1")
        with self.assertRaisesRegex(ValueError, "no current approved handoff"):
            companion.packet(rec["id"])
        self.assertEqual(companion.get(rec["id"])["approvals"], [])


class A63DispatchAmbiguity(Base):
    def test_intent_survives_crash_and_is_never_resent(self):
        rec = self.approve(self.planned())
        intent = rec["approvals"][-1]
        for _ in range(3):
            companion.resume(rec["id"])
        again = workflowstore.load(rec["id"])
        self.assertEqual(again["approvals"], [intent])
        self.assertEqual(again["approvals"][-1]["dispatch"], "prepared")

    def test_double_click_yields_one_intent(self):
        rec = self.planned()
        digest = companion.resume(rec["id"])["context_digest"]
        first = companion.approve(rec["id"], rec["revision"], rec["selected_plan"], digest, True, "s1")
        with self.assertRaises(workflowstore.Conflict):
            companion.approve(rec["id"], rec["revision"], rec["selected_plan"], digest, True, "s1")
        with self.assertRaisesRegex(ValueError, "already prepared"):
            companion.approve(first["id"], first["revision"], rec["selected_plan"], digest, True, "s1")
        self.assertEqual(len(companion.get(rec["id"])["approvals"]), 1)

    def test_crash_after_dispatch_leaves_unknown_until_decided(self):
        rec = self.approve(self.planned())
        attempt = rec["approvals"][-1]["attempt"]
        rec = execution.begin_dispatch(rec["id"], rec["revision"], attempt)
        self.assertEqual((rec["stage"], rec["approvals"][-1]["dispatch"]), ("execution_ready", "dispatching"))
        self.advance(minutes=6)  # process died before any acknowledgement
        cand = companion.resume(rec["id"])
        self.assertEqual(cand["handoff"]["dispatch"], "unknown")
        self.assertEqual(cand["available_actions"], ["Record handoff outcome", "Associate captured session"])
        self.assertIn("nothing is resent", cand["next_action"])
        with self.assertRaisesRegex(ValueError, "Execution status unknown"):
            self.approve(rec)
        with self.assertRaisesRegex(ValueError, "handoff is unknown"):
            companion.packet(rec["id"])
        self.assertEqual(self.tick(), 1)
        rec = workflowstore.load(rec["id"])
        self.assertEqual(rec["approvals"][-1]["dispatch"], "unknown")
        self.assertTrue(rec["overlays"]["execution_status_unknown"])
        self.assertEqual(rec["stage"], "execution_ready", "overlay never replaces the stage")
        self.assertEqual(self.tick(), 0, "no repeated resend or checkpoint")
        rec = execution.record_dispatch(rec["id"], rec["revision"], attempt, "rejected", "Never reached the session")
        self.assertIsNone(rec["overlays"]["execution_status_unknown"])
        rec = self.approve(rec)
        self.assertNotEqual(rec["approvals"][-1]["attempt"], attempt)
        self.assertEqual([a["dispatch"] for a in rec["approvals"]], ["rejected", "prepared"])

    def test_copy_stays_prepared_and_copied_intent_needs_a_decision(self):
        rec = self.approve(self.planned())
        attempt = rec["approvals"][-1]["attempt"]
        rec = execution.record_dispatch(rec["id"], rec["revision"], attempt, "copied")
        self.assertEqual((rec["stage"], rec["approvals"][-1]["dispatch"]), ("execution_ready", "prepared"))
        self.assertIn("Prepared; not submitted", rec["next_action"])
        self.advance(minutes=6)
        with self.assertRaisesRegex(ValueError, "was copied"):
            self.approve(rec)
        with self.assertRaises(ValueError):
            execution.record_dispatch(rec["id"], rec["revision"], "x_other", "rejected")
        rec = execution.record_dispatch(rec["id"], rec["revision"], attempt, "unknown")
        self.assertTrue(rec["overlays"]["execution_status_unknown"])
        with self.assertRaisesRegex(ValueError, "reconcile the execution attempt"):
            companion.archive(rec["id"], rec["revision"], "x")


class A64ContextRace(Base):
    def setUp(self):
        super().setUp()
        self.rec = self.approve(self.planned())

    def test_frozen_clock_expiry(self):
        self.advance(minutes=4, seconds=59)
        self.assertIn("Prepared; not submitted", companion.packet(self.rec["id"]))
        self.advance(seconds=2)
        with self.assertRaisesRegex(ValueError, "expired"):
            companion.packet(self.rec["id"])
        with self.assertRaisesRegex(ValueError, "expired"):
            execution.record_dispatch(self.rec["id"], self.rec["revision"], self.rec["approvals"][-1]["attempt"], "copied")
        self.assertIn("expired", companion.resume(self.rec["id"])["next_action"])

    def test_changed_context_plan_baseline_and_profile_invalidate(self):
        rid = self.rec["id"]
        self.put(commits={"c9": {"sha": "c9", "subject": "Unrelated", "date": self.stamp(), "repo_name": "scratch"}})
        with self.assertRaisesRegex(ValueError, "context changed"):
            companion.packet(rid)
        rec = execution.review_again(rid, self.rec["revision"])
        self.assertEqual(rec["approvals"][-1]["dispatch"], "cancelled")
        with self.assertRaisesRegex(ValueError, "no current approved handoff"):
            companion.packet(rid)
        rec = self.approve(rec)
        rec = execution.requirements(rid, rec["revision"], criteria(2), "partial", "UAT added a criterion")
        self.assertEqual((rec["stage"], rec["approvals"][-1]["dispatch"]), ("review_incomplete", "cancelled"))
        rec = companion.save_plan(rid, rec["revision"], plan_body())
        rec = self.approve(rec)
        rec = self.observe(rec, HEAD2)
        with self.assertRaisesRegex(ValueError, "context changed"):
            companion.packet(rid)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["accounts"][0]["id"] = "dev-b"
        write_config(common.home(), cfg)
        with self.assertRaises(PermissionError):
            companion.packet(rid)

    def test_repository_removed_and_other_session_rejected(self):
        with self.assertRaisesRegex(ValueError, "no captured work"):
            self.associate(self.rec, "sess-elsewhere", repo=self.repo_b)
        os.rename(self.repo, self.repo + "-gone")
        try:
            with self.assertRaisesRegex(ValueError, "context changed"):
                companion.packet(self.rec["id"])
        finally:
            os.rename(self.repo + "-gone", self.repo)


class A66CrashConsistency(Base):
    def fail_on(self, which, exc=None):
        original = workflowio.durable_json

        def durable(path, value):
            if (which == "payload" and os.sep + "checkpoints" + os.sep in path) or \
                    (which == "head" and path.endswith("head.json")):
                raise exc or OSError(errno.ENOSPC, "No space left on device")
            original(path, value)
        return mock.patch.object(workflowio, "durable_json", durable)

    def test_before_payload_and_after_payload_keep_prior_checkpoint(self):
        rec = self.create()
        body = plan_body()
        draft = json.dumps(body, sort_keys=True)
        for boundary in ("payload", "head"):
            with self.fail_on(boundary):
                with self.assertRaisesRegex(workflowstore.NotSaved, "Not saved"):
                    companion.save_plan(rec["id"], rec["revision"], body)
            self.assertEqual(workflowstore.load(rec["id"]), rec)
            self.assertEqual(json.dumps(body, sort_keys=True), draft, "draft preserved")
        orphans = [f for f in os.listdir(os.path.join(workflowstore.root(rec["id"]), "checkpoints"))]
        self.assertEqual(len(orphans), 2, "one published checkpoint and one orphan payload")
        report = workflowstore.recover(rec["id"])
        self.assertEqual((report["state"], len(report["quarantined"])), ("ok", 1))
        self.assertEqual(workflowstore.recover(rec["id"])["quarantined"], [], "idempotent")
        saved = companion.save_plan(rec["id"], rec["revision"], body)
        self.assertEqual(saved["revision"], 2)

    def test_after_head_before_ticket_reference(self):
        with mock.patch.object(workflowstore, "_attach", side_effect=OSError(errno.EIO, "I/O error")):
            rec = self.create()
        self.assertNotIn("workflows", self.tk())
        self.assertEqual(workflowstore.load(rec["id"]), rec)
        self.assertEqual(workflowstore.recover(rec["id"])["ticket_reference"], "repaired")
        self.assertEqual(self.tk()["workflows"]["ids"], [rec["id"]])
        self.assertEqual(workflowstore.recover(rec["id"])["ticket_reference"], "present")
        with self.assertRaises(workflowstore.Conflict):
            self.create()  # same binding: no duplicate workflow ID
        self.assertEqual(workflowstore.ids(), [rec["id"]])

    def test_invalid_head_never_exposes_mixed_versions(self):
        rec = self.planned()
        good = common.read_json(os.path.join(workflowstore.root(rec["id"]), "head.json"))
        workflowio_path = os.path.join(workflowstore.root(rec["id"]), "head.json")
        with open(workflowio_path, "w", encoding="utf-8") as f:
            f.write('{"digest": "' + "f" * 64 + '", "revision": 9}')
        with self.assertRaisesRegex(ValueError, "recovery required.*%s.*revision 2" % good["digest"]):
            workflowstore.load(rec["id"])
        with self.assertRaises(ValueError):
            companion.resume(rec["id"])
        report = workflowstore.recover(rec["id"])
        self.assertEqual((report["state"], report["last_complete"]), ("recovery_required", good["digest"]))
        report = workflowstore.recover(rec["id"], restore=good["digest"])
        self.assertEqual(report["restored"], good["digest"])
        self.assertEqual(workflowstore.load(rec["id"]), rec)

    def test_lock_timeout_and_disk_full_report_not_saved(self):
        rec = self.planned()
        held, release = threading.Event(), threading.Event()

        def hold():
            with workflowio.lock():
                held.set()
                release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            held.wait(5)
            with self.assertRaisesRegex(TimeoutError, "Not saved"):
                workflowstore.save(rec, rec["revision"], lambda r: None, timeout=0.2)
        finally:
            release.set()
            thread.join(10)
        with mock.patch.object(workflowio, "lock", side_effect=TimeoutError(workflowio.BUSY)):
            with self.assertRaisesRegex(TimeoutError, "Not saved"):
                self.approve(rec)
        with self.assertRaisesRegex(ValueError, "no current approved handoff"):
            companion.packet(rec["id"])

    def test_migration_restart_keeps_exact_versions_and_no_duplicate_ids(self):
        rec = self.approve(self.planned())
        self.put(commits={"c1": {"sha": "c1", "subject": "Work", "date": self.stamp(), "repo_name": "scratch"}})
        legacy = {k: v for k, v in rec.items() if k not in model.DEFAULTS and k not in ("overlays", "imported")}
        tk = self.tk()
        legacy["ingested"] = sorted("commits:" + model.digest([k, v]) for k, v in tk["commits"].items())
        legacy["acknowledged"] = list(legacy["ingested"])
        legacy["ingested"].append("git:" + "e" * 64)
        legacy["revision"] = rec["revision"]
        folder = workflowstore.root(rec["id"])
        digest = model.digest(legacy)
        with workflowio.lock():
            workflowio.durable_json(os.path.join(folder, "checkpoints", digest + ".json"), legacy)
            workflowio.durable_json(os.path.join(folder, "head.json"), {"digest": digest, "revision": legacy["revision"]})
        loaded = workflowstore.load(rec["id"])
        self.assertEqual(loaded["acknowledged"], {legacy["acknowledged"][0]: ""})
        exact = json.dumps([rec["plans"], rec["approvals"]], sort_keys=True)
        for _ in range(2):
            loaded = companion.checkpoint(loaded["id"], loaded["revision"])
        self.assertEqual(json.dumps([loaded["plans"], loaded["approvals"]], sort_keys=True), exact)
        self.assertEqual(loaded["acknowledged"].get("commit:c1"), loaded["ingested"]["commit:c1"])
        self.assertEqual(loaded["legacy_ingested"], ["git:" + "e" * 64])
        self.assertFalse([x for x in loaded["ingested"] if x.startswith("commits:")])
        self.assertNotIn("commit:c1", companion.resume(rec["id"])["unread"])


RACE = r"""
import json, os, sys, time
from chitragupta import workflowstore
wid, folder, tag = sys.argv[1], sys.argv[2], sys.argv[3]
rec = workflowstore.load(wid)
base = rec["revision"]
rec["summary"] = "draft from " + tag
open(os.path.join(folder, "ready-" + tag), "w").close()
deadline = time.monotonic() + 60  # never outlive a failed parent
while not os.path.exists(os.path.join(folder, "go")):
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.005)
try:
    out = workflowstore.save(rec, base, lambda r: None, timeout=20)
    print(json.dumps({"ok": out["revision"]}))
except workflowstore.Conflict as exc:
    print(json.dumps({"conflict": exc.current["revision"], "unsaved": exc.unsaved["summary"]}))
"""


def kill(proc):
    """Terminate a test child for certain (Windows included) and reap it."""
    if proc.poll() is None:
        proc.kill()
    try:
        proc.communicate(timeout=10)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


class A67ConcurrentSessions(Base):
    def test_two_processes_save_from_the_same_revision(self):
        rec = self.planned()
        signals = os.path.join(self.user_dir, "race")
        os.makedirs(signals)
        procs = []
        self.addCleanup(lambda: [kill(p) for p in procs])  # hard deadline: no child outlives the test
        for tag in ("one", "two"):
            procs.append(subprocess.Popen([PY, "-c", RACE, rec["id"], signals, tag], cwd=ROOT, env=self.child_env(),
                                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        deadline = time.monotonic() + 60
        while not all(os.path.exists(os.path.join(signals, "ready-" + t)) for t in ("one", "two")):
            self.assertLess(time.monotonic(), deadline, "writers did not start")
            self.assertEqual([p.poll() for p in procs], [None, None], "a writer exited early")
            time.sleep(0.02)
        open(os.path.join(signals, "go"), "w").close()
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode())
            results.append(json.loads(out.decode().strip().splitlines()[-1]))
        oks = [r for r in results if "ok" in r]
        conflicts = [r for r in results if "conflict" in r]
        self.assertEqual((len(oks), len(conflicts)), (1, 1))
        self.assertEqual(oks[0]["ok"], rec["revision"] + 1)
        self.assertEqual(conflicts[0]["conflict"], rec["revision"] + 1, "current content returned")
        self.assertTrue(conflicts[0]["unsaved"].startswith("draft from "), "unsaved draft returned")
        self.assertEqual(workflowstore.load(rec["id"])["revision"], rec["revision"] + 1)

    def test_one_owner_per_working_tree_and_expired_is_unknown(self):
        a = self.started()
        b = self.planned("ABC-1002")  # same clone, different task
        with self.assertRaises(companion.OwnershipConflict) as caught:
            self.approve(b)
        self.assertIn("ABC-1001", str(caught.exception))
        self.assertIn("Observed execution activity", caught.exception.owners[0]["state"])
        self.advance(minutes=31)
        with self.assertRaises(companion.OwnershipConflict) as caught:
            self.approve(b)
        self.assertEqual(caught.exception.owners[0]["state"], "Execution status unknown")
        self.assertNotIn("stopped", str(caught.exception).replace("never stops", ""))
        self.assertEqual(companion.resume(a["id"])["ownership"]["label"], "Execution status unknown")
        companion.pause(a["id"], a["revision"], "Explicit reconciliation")
        self.assertEqual(self.approve(b)["stage"], "approved")


class A68NoHistoryRestart(Base):
    def run_cli(self, *args):
        # run() kills the child on its deadline; stdin is closed so nothing can wait on it.
        proc = subprocess.run([PY, "-m", "chitragupta.companion"] + list(args), cwd=ROOT, env=self.child_env(),
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        return json.loads(proc.stdout.decode())

    def test_rebuild_from_canonical_records_only(self):
        rec = self.started()
        rec = companion.pause(rec["id"], rec["revision"], "End of day")
        rec = workflowstore.load(rec["id"])
        exact = model.digest([rec["plans"], rec["approvals"], rec["decisions"], rec["evidence"]])
        expected = companion.resume(rec["id"])
        # Remove conversation history and every derived cache, then restart in a new process.
        derived = (common.wl("raw"), common.wl("cache"), common.hp("data", "search"), common.hp("tickets"))
        for path in derived:
            os.makedirs(path, exist_ok=True)
            open(os.path.join(path, "synthetic.jsonl"), "w").close()
        os.remove(common.wl("state.json"))
        for path in derived:
            rmtree(path)
        files = self.files()
        shown = self.run_cli("show", rec["id"])
        self.assertEqual(model.digest([shown["plans"], shown["approvals"], shown["decisions"], shown["evidence"]]), exact)
        reads = [self.run_cli("resume", rec["id"]) for _ in range(3)]
        self.assertEqual(reads[0], reads[1])
        self.assertEqual(reads[1], reads[2])
        self.assertEqual(reads[0]["id"], expected["id"])
        self.assertEqual(reads[0]["next_action"], expected["next_action"])
        self.assertEqual(self.files(), files, "zero generation, evaluation, dispatch or checkpoint")
        self.assertEqual(workflowstore.load(rec["id"])["approvals"], rec["approvals"])


class A69UnmarkedRevertedSteps(Base):
    def test_blank_checklist_and_revert(self):
        rec = self.started(steps=THREE, n=3)
        rec = self.observe(rec)
        for step in ("one", "two"):
            rec = self.map(rec, steps=[step], manual={"kind": "code_inspection", "outcome": "passed",
                                                      "text": "Step %s present in code" % step, "revision": HEAD1,
                                                      "limits": "Inspection only"})
        cand = companion.resume(rec["id"])
        self.assertEqual(cand["continuation"]["remaining"], ["three"])
        self.assertEqual([c["step"] for c in cand["continuation"]["carry_forward"]], ["one", "two"])
        rec = self.observe(rec, HEAD2)  # step one reverted in a later commit
        rec = self.map(rec, steps=["two"], manual={"kind": "code_inspection", "outcome": "passed",
                                                   "text": "Step two still present", "revision": HEAD2,
                                                   "limits": "Inspection only"})
        cand = companion.resume(rec["id"])
        self.assertEqual(cand["progress"]["completed"], ["two"])
        self.assertEqual(cand["continuation"]["revalidate"], ["one"])
        self.assertEqual(sorted(cand["continuation"]["remaining"]), ["one", "three"])
        self.assertEqual(len([e for e in rec["evidence"] if "one" in e["steps"]]), 1, "old completion kept as history")
        rec = self.observe(rec, HEAD2, worktree="dirty")
        self.assertEqual(model.applicability(rec["evidence"][-1], evidence.applicable(companion.context(rec))),
                         "unknown")
        self.assertEqual(companion.resume(rec["id"])["progress"]["completed"], [])


class A70CursorSeparation(Base):
    def test_unread_persists_until_marked_reviewed(self):
        common.write_json(common.hp("data", "jira", "ABC-1001.json"),
                          {"key": "ABC-1001", "fetched_at": self.stamp(), "card": {"summary": "Filter", "status": "Open"}})
        self.put(commits={"c0": {"sha": "c0", "subject": "Before the task", "date": self.stamp(), "repo_name": "scratch"}})
        rec = self.started()
        self.assertEqual(companion.resume(rec["id"])["unread"], [], "imported history is not a flood of new work")
        self.put(commits={"c0": self.tk()["commits"]["c0"],
                          "c1": {"sha": "c1", "subject": "Late", "date": "2026-11-01T09:00:00+00:00", "repo_name": "scratch"}})
        self.pr(state="open")
        self.captured_test(rec, "t1", context=False)
        common.write_json(common.hp("data", "jira", "ABC-1001.json"),
                          {"key": "ABC-1001", "fetched_at": self.stamp(), "card": {"summary": "Filter v2", "status": "Open"}})
        self.assertEqual(self.tick(), 1)
        rec = workflowstore.load(rec["id"])
        unread = companion.resume(rec["id"])["unread"]
        for sid in ("commit:c1", "pr:scratch:7", "test:t1", "requirements:ABC-1001"):
            self.assertIn(sid, unread)
        changed = {i["id"]: i["change"] for i in companion.resume(rec["id"])["delta"]["items"]}
        self.assertEqual(changed["requirements:ABC-1001"], "changed")
        self.assertEqual(changed["commit:c1"], "new", "late source time is still new by stable ID")
        self.assertTrue(rec["overlays"]["stale"])
        self.assertEqual(self.tick(), 0, "refresh without new source events")
        rec = companion.checkpoint(rec["id"], rec["revision"], "Saving progress")
        self.assertEqual(companion.resume(rec["id"])["unread"], unread)
        self.put(commits=dict(self.tk()["commits"]))  # replayed duplicate import
        self.assertEqual(self.tick(), 0)
        rec = workflowstore.load(rec["id"])
        rec = companion.acknowledge(rec["id"], rec["revision"], unread)
        self.assertEqual(companion.resume(rec["id"])["unread"], [])
        self.captured_test(rec, "t1", status="failed", context=False)  # materially changed source, same ID
        self.tick()
        self.assertEqual(companion.resume(rec["id"])["unread"], ["test:t1"])
        with self.assertRaisesRegex(ValueError, "displayed ingested"):
            companion.acknowledge(rec["id"], workflowstore.load(rec["id"])["revision"], ["commit:never"])


class A71VerificationPr(Base):
    def test_pr_on_old_head_requested_changes_and_reviewed_continuation(self):
        rec = self.started()
        rec = self.observe(rec, HEAD2)
        self.pr(state="draft", review="changes_requested", head=HEAD1)
        self.tick()
        rec = workflowstore.load(rec["id"])
        cand = companion.resume(rec["id"])
        self.assertEqual(cand["situation"], "D")
        self.assertEqual(cand["continuation"]["requested_changes"], ["pr:scratch:7"])
        self.assertEqual(cand["continuation"]["head_gaps"], ["pr:scratch:7"])
        # The PR has no check runs read for its head: unavailable, never passed (FLOW-03).
        self.assertTrue(cand["continuation"]["ci"])
        self.assertTrue(all(t.startswith("CI checks unavailable") for t in cand["continuation"]["ci"]))
        self.assertTrue(all(c["state"] == "unavailable" for c in cand["ci"]))
        self.assertIn("explicitly selected", cand["continuation"]["scope"])
        rec = execution.approve_continuation(rec["id"], rec["revision"], cand["id"], True, "reviewed-copy",
                                             target_stage="in_development")
        self.assertEqual((rec["stage"], rec["approvals"][-1]["resume_stage"]), ("execution_ready", "in_development"))
        self.assertEqual(len([p for p in self.tk()["git"]["repos"]["scratch"]["prs"]]), 1, "no duplicate PR")


class A72SwitchesRepositoryLoss(Base):
    def test_a_b_a_and_explicit_resolution(self):
        a = self.started()
        a = companion.pause(a["id"], a["revision"], "To B")
        b = self.started("ABC-1002", self.repo_b, sid="sess-b")
        b = companion.pause(b["id"], b["revision"], "Back to A")
        before = {w: os.listdir(os.path.join(workflowstore.root(w), "checkpoints")) for w in (a["id"], b["id"])}
        a = execution.approve_continuation(a["id"], a["revision"], companion.resume(a["id"])["id"], True, "copy")
        self.assertEqual(os.listdir(os.path.join(workflowstore.root(b["id"]), "checkpoints")), before[b["id"]])
        self.assertEqual(workflowstore.load(b["id"])["stage"], "paused")
        a = execution.record_dispatch(a["id"], a["revision"], a["approvals"][-1]["attempt"], "cancelled")
        branch = {"branch": "feature/ABC-1001", "status": "deleted_remote", "local": False}
        tk = self.tk()
        tk["git"] = {"repos": {"scratch": {"branches": [branch], "prs": []}}, "checked_at": self.stamp()}
        common.write_json(store.ticket_path("ABC-1001"), tk)
        cases = {"branch_deleted": {"branch": "feature/ABC-1001"},
                 "detached_head": {"detached": True},
                 "location_moved": {"location": self.repo + "-copy"},
                 "different_repository": {"repo_id": "github:github.com/acme/scratch"}}
        for state, extra in cases.items():
            a = self.observe(a, HEAD1, **extra)
            recovery = companion.resume(a["id"])["recovery"]
            found = [r for r in recovery if r["state"] == state]
            self.assertTrue(found, state)
            self.assertTrue(found[0]["blocking"])
            self.assertRegex(found[0]["action"], "(?i)never|not accepted|explicit|nothing|yourself")


class A73AccessRevocation(Base):
    def test_revocation_mid_action_and_tick(self):
        rec = self.planned()
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["repos"] = [self.repo_b]

        def revoke_mid_change(record):
            write_config(common.home(), cfg)
        with self.assertRaises(PermissionError):
            companion._change(rec["id"], rec["revision"], revoke_mid_change, "late")
        write_config(common.home(), self.cfg)
        self.assertEqual(workflowstore.load(rec["id"])["revision"], rec["revision"], "nothing published")
        write_config(common.home(), cfg)
        self.put(commits={"c1": {"sha": "c1", "subject": "Work", "date": self.stamp(), "repo_name": "scratch"}})
        self.assertEqual(self.tick(), 0, "revoked scope ingests nothing")
        self.assertEqual(companionviews.snapshot()["items"][0]["workflow"], None)
        write_config(common.home(), self.cfg)  # valid reconciliation restores access
        self.assertEqual(self.tick(), 1)


class A74Applicability(Base):
    def test_only_current_mapped_evidence_verifies(self):
        rec = self.started(n=4)
        rec = self.observe(rec)
        self.captured_test(rec, "t1")
        self.captured_test(rec, "t2", status="failed")
        self.captured_test(rec, "t3", environment="staging")
        rec = self.map(rec, evidence="test:t1", criteria=["ac1"], coverage="complete")
        rec = self.map(rec, evidence="test:t2", criteria=["ac2"], coverage="complete")
        rec = self.map(rec, evidence="test:t3", criteria=["ac3"], coverage="complete")
        # Jev confidence 1.0, a Claude completion note, Jira Done and a merged PR are not evidence.
        rec["evaluations"]["e1"] = dict(jev.validate_decisions(rec["baselines"][rec["baseline"]],
                                                               [{"id": "ac4", "status": "Covered", "confidence": 1.0}]),
                                        plan=rec["approved_plan"])
        rec = workflowstore.save(rec, rec["revision"], companion.authorize)
        self.put(items=dict(self.tk()["items"], d1={"id": "d1", "kind": "decision", "title": "Claude reports done",
                                                     "date": "2026-11-08"}))
        rec = self.map(rec, evidence="decision:d1", criteria=["ac4"], coverage="complete")
        common.write_json(common.hp("data", "jira", "ABC-1001.json"),
                          {"key": "ABC-1001", "fetched_at": self.stamp(), "card": {"status": "Done"}})
        self.pr(state="merged", head=HEAD1)
        rec = companion.verify(rec["id"], rec["revision"])
        ledger = self.ledger(rec)
        self.assertEqual(ledger, {"ac1": "Verified", "ac2": "Missing", "ac3": "Unverified", "ac4": "Unverified"})
        rows = {r["criterion"]: r for r in rec["verifications"][-1]["rows"]}
        self.assertTrue(rows["ac2"]["failing"])
        self.assertIn("environment", rows["ac3"]["invalidation"])
        measures = rec["verifications"][-1]["measures"]
        self.assertEqual(measures["functional_verification"]["percent"], 25.0)
        self.assertEqual(measures["plan_coverage"]["percent"], 25.0, "plan coverage is separate")
        self.assertEqual(measures["implementation_progress"]["label"],
                         "evidenced implementation steps / approved implementation steps")
        rec = companion.request_verification(rec["id"], rec["revision"], ["Fix ac2"])
        with self.assertRaisesRegex(ValueError, "ac2 Missing"):
            companion.complete(rec["id"], rec["revision"], "Jira says Done and the PR merged")
        rec = self.observe(rec, HEAD2)  # R2: the R1 pass no longer applies
        rec = companion.verify(rec["id"], rec["revision"])
        self.assertEqual(self.ledger(rec)["ac1"], "Unverified")
        self.assertIn("head", {r["criterion"]: r for r in rec["verifications"][-1]["rows"]}["ac1"]["invalidation"])
        self.assertEqual({r["criterion"]: r["status"] for r in rec["verifications"][0]["rows"]}["ac1"], "Verified",
                         "earlier result never erased")
        rec = self.observe(rec, HEAD1, worktree="dirty")
        rec = companion.verify(rec["id"], rec["revision"])
        self.assertEqual(self.ledger(rec)["ac1"], "Unverified")

    def test_scoped_self_reported_acceptance(self):
        rec = self.started()
        with self.assertRaisesRegex(ValueError, "limits"):
            self.map(rec, criteria=["ac1"], coverage="complete",
                     manual={"kind": "acceptance", "outcome": "passed", "text": "Works", "revision": HEAD1, "limits": ""})
        rec = self.observe(rec)
        rec = self.map(rec, criteria=["ac1"], coverage="complete",
                       manual={"kind": "acceptance", "outcome": "passed", "text": "Filter works in UAT",
                               "revision": HEAD1, "limits": "Chrome only; synthetic data"})
        rec = companion.verify(rec["id"], rec["revision"])
        row = rec["verifications"][-1]["rows"][0]
        self.assertEqual((row["status"], row["basis"]), ("Verified", ["self-reported"]))


class PinPlanBinding(Base):
    """A step mapping records the plan it was made under: the same step id in a later plan version is another step."""

    def pins(self, rec, sid="test:t1"):
        return [e for e in rec["evidence"] if e.get("id") == sid]

    def test_a_new_step_mapping_carries_the_plan(self):
        rec = self.observe(self.started())
        self.captured_test(rec, "t1")
        rec = self.map(rec, evidence="test:t1", criteria=["ac1"], steps=["one"], coverage="complete")
        self.assertEqual([e["plan"] for e in self.pins(rec)], [rec["approved_plan"]])
        rec = self.map(rec, steps=["two"], manual={"kind": "code_inspection", "outcome": "passed", "text": "Read it",
                                                   "revision": HEAD1, "limits": "Read the diff only"})
        self.assertEqual(rec["evidence"][-1]["plan"], rec["approved_plan"])

    def test_a_criteria_only_mapping_records_no_plan(self):
        rec = self.observe(self.started())
        self.captured_test(rec, "t1")
        rec = self.map(rec, evidence="test:t1", criteria=["ac1"], coverage="complete")
        rec = self.map(rec, criteria=["ac1"], manual={"kind": "acceptance", "outcome": "passed", "text": "Checked",
                                                      "revision": HEAD1, "limits": "One account"})
        self.assertEqual([("plan" in e) for e in rec["evidence"]], [False, False])
        rec = self.map(rec, evidence="test:t1", criteria=["ac1"], coverage="partial")   # amended in place, as before
        self.assertEqual([(e["coverage"], "plan" in e) for e in self.pins(rec)], [("partial", False)])

    def test_the_same_source_under_another_plan_is_a_second_pin(self):
        rec = self.planned()
        self.captured_test(rec, "t1", context=False)
        first = rec["selected_plan"]
        rec = self.map(rec, evidence="test:t1", steps=["one"])
        rec = self.map(rec, evidence="test:t1", steps=["two"])     # the same plan: amended in place
        self.assertEqual([(e["plan"], e["steps"]) for e in self.pins(rec)], [(first, ["one", "two"])])
        rec = companion.save_plan(rec["id"], rec["revision"], plan_body(text="Another way to reach the same result."))
        second = rec["selected_plan"]
        self.assertNotEqual(first, second)
        rec = self.map(rec, evidence="test:t1", steps=["one"])     # step "one" of the new version
        self.assertEqual([(e["plan"], e["steps"]) for e in self.pins(rec)],
                         [(first, ["one", "two"]), (second, ["one"])])
        rec = self.map(rec, evidence="test:t1", criteria=["ac1"])  # criteria-only: joins the current plan's pin
        self.assertEqual([(e["plan"], e["criteria"]) for e in self.pins(rec)], [(first, []), (second, ["ac1"])])

    def test_a_legacy_pin_loads_reconciles_and_verifies_as_before(self):
        rec = self.observe(self.started())
        self.captured_test(rec, "t1")
        rec = self.map(rec, evidence="test:t1", criteria=["ac1"], steps=["one"], coverage="complete")
        with_plan = companion.verify(rec["id"], rec["revision"])
        plan = with_plan["plans"][with_plan["approved_plan"]]
        base = with_plan["baselines"][with_plan["baseline"]]
        ctx = companion.context(with_plan)
        legacy = [{k: v for k, v in e.items() if k != "plan"} for e in with_plan["evidence"]]
        self.assertNotEqual(legacy, with_plan["evidence"])
        self.assertEqual(model.reconcile(plan, legacy, ctx), model.reconcile(plan, with_plan["evidence"], ctx))
        self.assertEqual(model.verification(base, legacy, ctx), model.verification(base, with_plan["evidence"], ctx))
        self.assertEqual(self.ledger(with_plan), {"ac1": "Verified"})
        # a record saved before pins recorded a plan
        with_plan["evidence"] = legacy
        saved = workflowstore.save(with_plan, with_plan["revision"], companion.authorize)
        loaded = workflowstore.load(saved["id"])
        self.assertEqual([("plan" in e) for e in loaded["evidence"]], [False])
        again = companion.verify(loaded["id"], loaded["revision"])
        self.assertEqual(self.ledger(again), {"ac1": "Verified"})
        self.assertEqual(model.reconcile(plan, again["evidence"], ctx)["completed"],
                         model.reconcile(plan, legacy, ctx)["completed"])
        again = self.map(again, evidence="test:t1", criteria=["ac1"], coverage="complete")
        self.assertEqual(len(self.pins(again)), 1)                 # criteria-only still amends the legacy pin
        again = self.map(again, evidence="test:t1", steps=["two"])
        self.assertEqual([(e.get("plan"), e["steps"]) for e in self.pins(again)],
                         [(None, ["one"]), (again["approved_plan"], ["two"])])   # a legacy pin is never given a plan


class A75EvidenceRetention(Base):
    def run_tests(self, n, start):
        st = store.Store()
        for i in range(start, start + n):
            ev = {"ts": (T0 + dt.timedelta(minutes=i)).isoformat(timespec="seconds"), "_ln": i + 1,
                  "payload": {"input": {"command": "python -m unittest tests.test_shop_%d" % i},
                              "outcome": {"test": {"status": "passed", "exit": 0, "counts": {"total": 3, "passed": 3}}}}}
            testruns.record(st, "ABC-1001", ev, "2026-11-08", "sess-1")
        st.save()

    def test_pinned_evidence_survives_pruning(self):
        rec = self.started()
        self.run_tests(10, 0)
        self.tick()
        rec = workflowstore.load(rec["id"])
        first = sorted((v["at"], k) for k, v in self.tk()["tests"].items())[0][1]
        rec = self.map(rec, evidence="test:" + first, criteria=["ac1"], coverage="partial")
        self.run_tests(55, 10)
        self.assertEqual(len(self.tk()["tests"]), testruns.KEEP)
        self.assertNotIn(first, self.tk()["tests"])
        self.tick()
        rec = workflowstore.load(rec["id"])
        pinned = [e for e in rec["evidence"] if e["id"] == "test:" + first][0]
        self.assertLessEqual(set(pinned), {"id", "kind", "version", "source_ref", "observed_at", "outcome", "label",
                                           "counts", "context", "repo", "review", "head", "origin", "criteria",
                                           "steps", "coverage", "baseline", "mapped_by", "mapped_at", "pinned"})
        _, rows = evidence.current_sources(rec)
        self.assertEqual(evidence.resolve(rec, rows, "test:" + first)["state"], "pinned copy; source no longer retained")
        pruned = [sid for sid in rec["ingested"] if sid.startswith("test:") and sid[5:] not in self.tk()["tests"]]
        self.assertEqual(len(pruned), 10)
        self.assertEqual({evidence.resolve(rec, rows, sid)["state"] for sid in pruned if sid != "test:" + first},
                         {"unavailable"})
        delta = companion.resume(rec["id"])["delta"]
        self.assertEqual((delta["total"], delta["more"], len(delta["items"])), (60, True, evidence.DELTA_LIMIT))
        ledger = companion.resume(rec["id"])
        self.assertIn("ac1", ledger["measures"]["functional_verification"]["gaps"])
        cited = companionviews.detail(rec["id"])["verification"]["rows"][0]["evidence"]
        self.assertEqual([c["id"] for c in cited], ["test:" + first])


class A78GlobalPauseUnchanged(Base):
    def test_task_pause_is_not_capture_pause(self):
        from chitragupta import cli
        rec = self.started()
        rec = companion.pause(rec["id"], rec["revision"], "Task pause")
        self.assertFalse(os.path.exists(common.wl("PAUSED")))
        with redirect_stdout(io.StringIO()):
            cli.cmd_pause(None)
            self.put(commits={"c1": {"sha": "c1", "subject": "Work", "date": self.stamp(), "repo_name": "scratch"}})
            self.assertEqual(self.tick(), 0, "the companion step skips while capture is paused")
            cli.cmd_resume(None)
        self.assertFalse(os.path.exists(common.wl("PAUSED")))
        self.assertEqual(workflowstore.load(rec["id"])["stage"], "paused")
        for mod in (companion, evidence, execution, workflowstore):
            self.assertNotIn('"PAUSED"', open(mod.__file__, encoding="utf-8").read().replace(
                'common.wl("PAUSED")) or', ""))


class StoreLockFallback(Base):
    def test_no_workflow_storage_means_no_lock_or_folder(self):
        st = store.Store()
        st.ticket("ABC-1001")["title"] = "Filter"
        st.touch_ticket("ABC-1001")
        with mock.patch.object(workflowio, "lock", side_effect=AssertionError("lock taken")):
            st.save()
        self.assertFalse(os.path.exists(common.hp("data", "workflows")))

    def test_busy_lock_falls_back_and_keeps_references(self):
        rec = self.create()
        st = store.Store()
        st.ticket("ABC-1001")["title"] = "Filter"
        st.tickets["ABC-1001"].pop("workflows", None)
        st.touch_ticket("ABC-1001")
        held, release = threading.Event(), threading.Event()

        def hold():
            with workflowio.lock():
                held.set()
                release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            held.wait(5)
            began = time.monotonic()
            st.save()
            self.assertLess(time.monotonic() - began, 3)
        finally:
            release.set()
            thread.join(10)
        tk = self.tk()
        self.assertEqual((tk["title"], tk["workflows"]["ids"]), ("Filter", [rec["id"]]))


class TickStep(Base):
    def test_bounded_no_workflow_folder_and_failure_isolated(self):
        self.assertEqual(evidence.tick(common.load_config(), {"ABC-1001"}), 0)
        self.assertFalse(os.path.exists(common.hp("data", "workflows")))
        recs = [self.started(), self.planned("ABC-1002", self.repo_b)]
        for key in ("ABC-1001", "ABC-1002"):
            self.put(key, commits={"c-" + key: {"sha": "c-" + key, "subject": "Work", "date": self.stamp(),
                                                "repo_name": "scratch" if key == "ABC-1001" else "other"}})
        self.assertEqual(evidence.tick(common.load_config(), {"ABC-1001", "ABC-1002"}, limit=1), 1)
        self.assertEqual(evidence.tick(common.load_config(), {"ABC-1001", "ABC-1002"}, limit=1), 1)
        self.assertEqual(evidence.tick(common.load_config(), {"ABC-1001", "ABC-1002"}), 0)
        for rec in recs:
            self.assertNotEqual(workflowstore.load(rec["id"])["acknowledged"].get("commit:c-" + rec["binding"]["ticket"]),
                                workflowstore.load(rec["id"])["ingested"].get("commit:c-" + rec["binding"]["ticket"]))


class LifecycleGates(Base):
    def test_dismissal_keeps_the_intent_prepared(self):
        rec = self.approve(self.planned())
        attempt = rec["approvals"][-1]["attempt"]
        rec = companion.action({"action": "dismiss_handoff", "workflow": rec["id"], "revision": rec["revision"],
                                "attempt": attempt})
        self.assertEqual((rec["stage"], rec["approvals"][-1]["dispatch"]), ("approved", "prepared"))
        self.assertTrue(rec["approvals"][-1]["dismissed_at"])
        rec = companion.cancel(rec["id"], rec["revision"], "Not now")
        self.assertEqual((rec["stage"], rec["approvals"][-1]["dispatch"]), ("approved", "cancelled"))
        self.assertIn("Nothing was rolled back", rec["overlays"]["cancelled"]["recovery"])

    def test_verification_request_claims_completion_only_with_evidence(self):
        rec = self.started(steps=THREE, n=3)
        with self.assertRaisesRegex(ValueError, "list the remaining checks"):
            companion.request_verification(rec["id"], rec["revision"], [])
        with self.assertRaisesRegex(ValueError, "does not support completed implementation"):
            companion.request_verification(rec["id"], rec["revision"], ["UAT"], implementation_complete=True)
        rec = self.observe(rec)
        for step in ("one", "two", "three"):
            rec = self.map(rec, steps=[step], manual={"kind": "code_inspection", "outcome": "passed",
                                                      "text": "Step present", "revision": HEAD1, "limits": "Read only"})
        rec = companion.request_verification(rec["id"], rec["revision"], ["UAT"], implementation_complete=True)
        self.assertEqual(rec["verification_request"]["implementation"], "evidenced")

    def test_open_uat_reminder_and_open_pr_block_completion(self):
        rec = self.started()
        rec = companion.request_verification(rec["id"], rec["revision"], ["UAT"])
        rec = self.observe(rec)
        self.captured_test(rec, "t1")
        rec = self.map(rec, evidence="test:t1", criteria=["ac1"], coverage="complete")
        self.put(release={"reminders": [{"id": "uat-verify:scratch", "kind": "uat-verify", "severity": "warn",
                                         "text": "Verify ABC-1001 on UAT"}]})
        self.pr(number=7, state="merged", head=HEAD1)
        self.pr(number=8, state="open")
        with self.assertRaisesRegex(ValueError, "open PR pr:scratch:8.*release: Verify ABC-1001 on UAT"):
            companion.complete(rec["id"], workflowstore.load(rec["id"])["revision"], "Done")

    def test_observed_pr_moves_once_and_continuation_is_not_bounced(self):
        rec = self.started()
        self.pr(state="open")
        self.tick()
        rec = workflowstore.load(rec["id"])
        self.assertEqual(rec["stage"], "pr_review")
        self.advance(minutes=1)
        rec = execution.approve_continuation(rec["id"], rec["revision"], companion.resume(rec["id"])["id"], True,
                                             "copy", target_stage="in_development")
        rec = self.associate(rec, "sess-2")
        self.assertEqual(rec["stage"], "in_development")
        self.put(commits={"c2": {"sha": "c2", "subject": "Address review", "date": self.stamp(), "repo_name": "scratch"}})
        self.assertEqual(self.tick(), 1)
        self.assertEqual(workflowstore.load(rec["id"])["stage"], "in_development")

    def test_multi_repository_rollup_never_hides_unfinished(self):
        a = self.started()
        b = self.create("ABC-1001", self.repo_b)
        a = companion.request_verification(a["id"], a["revision"], ["UAT"])
        a = self.observe(a)
        self.captured_test(a, "t1")
        a = self.map(a, evidence="test:t1", criteria=["ac1"], coverage="complete")
        companion.complete(a["id"], a["revision"], "Verified", pr_not_required="Hotfix branch only")
        out = companion.rollup("ABC-1001")
        self.assertEqual(out["label"], "1 of 2 repositories completed")
        self.assertEqual([r["workflow"] for r in out["unfinished"]], [b["id"]])
        self.assertEqual(sorted(self.tk()["workflows"]["ids"]), sorted([a["id"], b["id"]]))


from tests.test_v010 import Case as _V010  # noqa: E402  (agent fixture: isolated home, stubbed model and Slack)


class AgentTickStep(_V010):
    def test_step_runs_after_save_skips_when_paused_and_never_fails_the_tick(self):
        from chitragupta import gitsync
        self.clock("2026-10-16", 10, 0)
        a = self.agent()
        calls = []

        def failing(cfg, touched):
            calls.append(set(touched))
            raise RuntimeError("synthetic companion failure")
        with mock.patch.object(evidence, "tick", failing), mock.patch.object(gitsync, "commit", return_value=False), \
                mock.patch.object(gitsync, "ensure_repo"), mock.patch.object(gitsync, "check_privacy"), \
                mock.patch.object(gitsync, "unpushed", return_value=0):
            a.tick()
            self.assertEqual(len(calls), 1)
            self.assertIn("companion", a.steps)
            self.assertTrue(a.health.get("last_tick_ok"))
            common.write_text(common.wl("PAUSED"), "paused")
            a.tick()
            self.assertEqual(len(calls), 1, "skipped while capture is paused")


class Rollup(unittest.TestCase):
    def test_never_hides_an_unfinished_repository(self):
        rows = [{"id": "w%d" % i, "stage": s, "binding": {"repo_id": "r%d" % i, "location": "/r%d" % i}}
                for i, s in enumerate(["completed", "completed", "in_development"])]
        out = model.rollup(rows)
        self.assertEqual(out["label"], "2 of 3 repositories completed")
        self.assertEqual([r["repo_id"] for r in out["unfinished"]], ["r2"])
        self.assertFalse(out["complete"])
        self.assertTrue(model.rollup(rows[:2])["complete"])
        rows[1]["archived"] = {"reason": "x"}
        rows[1]["stage"] = "planned"
        self.assertFalse(model.rollup(rows[:2])["complete"])


if __name__ == "__main__":
    unittest.main()
