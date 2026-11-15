"""Synthetic workflow restart, conflict, publication, scope and evidence tests."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from tests.support import Isolated, write_config
from chitragupta import common, companion, companionviews, jev, profile, store, workflowmodel as model, workflowstore
from chitragupta.safety import workflowio


def plan_body():
    return {"text": "Implement the agreed behavior and verify its result.",
            **{s: "Recorded design detail" for s in model.REQUIRED_SECTIONS},
            "steps": [{"id": "one", "text": "Implement behavior", "criteria": ["ac1"]},
                      {"id": "two", "text": "Check result", "kind": "verification", "criteria": ["ac1"],
                       "depends_on": ["one"]}]}


class CompanionTest(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.repo = os.path.join(common.home(), "scratch")
        os.makedirs(self.repo)
        self.cfg = {"profile": {"accounts": [{"provider": "jira", "instance": "https://jira.acme.test", "id": "dev-a"}],
                                "repos": [self.repo], "include_tickets": {"ABC-1001": "My task"}},
                    "watched_repos": [self.repo], "git_author_emails": ["dev@acme.test"]}
        write_config(common.home(), self.cfg)
        common.write_json(store.ticket_path("ABC-1001"), store.new_ticket("ABC-1001"))

    def create(self):
        return companion.create("ABC-1001", self.repo, "https://jira.acme.test", [{"id": "ac1", "text": "Expected behavior"}])

    def test_restart_restores_exact_manual_plan_and_resume_is_read_only(self):
        rec = self.create()
        rec = companion.save_plan(rec["id"], rec["revision"], plan_body())
        before = json.dumps(workflowstore.load(rec["id"]), sort_keys=True)
        reads = [companion.resume(rec["id"]) for _ in range(3)]
        self.assertEqual(reads[0], reads[2])
        self.assertEqual(before, json.dumps(workflowstore.load(rec["id"]), sort_keys=True))
        self.assertEqual(companion.get(rec["id"])["plans"][rec["selected_plan"]]["text"], plan_body()["text"])

    def test_conflict_preserves_manual_version(self):
        rec = self.create()
        saved = companion.save_plan(rec["id"], rec["revision"], plan_body())
        with self.assertRaises(workflowstore.Conflict):
            companion.save_plan(rec["id"], rec["revision"], dict(plan_body(), text="Other session draft"))
        self.assertEqual(saved, companion.get(rec["id"]))

    def test_crash_before_head_keeps_prior_complete_checkpoint(self):
        rec = self.create()
        original = workflowio.durable_json
        def fail_head(path, value):
            if path.endswith("head.json"):
                raise OSError("disk full")
            original(path, value)
        with patch.object(workflowio, "durable_json", fail_head):
            with self.assertRaises(OSError):
                companion.save_plan(rec["id"], rec["revision"], plan_body())
        self.assertEqual(rec, workflowstore.load(rec["id"]))

    def test_approval_is_exact_and_copy_does_not_start_execution(self):
        rec = self.create()
        rec = companion.save_plan(rec["id"], rec["revision"], plan_body())
        ctx = companion.resume(rec["id"])["context_digest"]
        with self.assertRaises(ValueError):
            companion.approve(rec["id"], rec["revision"], "wrong", ctx, True, "s1")
        rec = companion.approve(rec["id"], rec["revision"], rec["selected_plan"], ctx, True, "s1")
        self.assertEqual(rec["stage"], "approved")
        self.assertIn(plan_body()["text"], companion.packet(rec["id"]))
        self.assertIn("not submitted", companion.packet(rec["id"]))
        with self.assertRaises(ValueError):
            companion.approve(rec["id"], rec["revision"], rec["selected_plan"], ctx, True, "s2")

    def test_context_change_blocks_copy(self):
        rec = self.create()
        rec = companion.save_plan(rec["id"], rec["revision"], plan_body())
        rec = companion.approve(rec["id"], rec["revision"], rec["selected_plan"],
                                companion.resume(rec["id"])["context_digest"], True, "s1")
        tk = common.read_json(store.ticket_path("ABC-1001"))
        tk["commits"] = {"new": {"sha": "r2"}}
        common.write_json(store.ticket_path("ABC-1001"), tk)
        with self.assertRaisesRegex(ValueError, "context changed"):
            companion.packet(rec["id"])

    def test_revoked_profile_hides_reads_actions_and_cursor(self):
        rec = self.create()
        cfg = copy.deepcopy(self.cfg)
        cfg["profile"]["repos"] = []
        cfg["watched_repos"] = []
        write_config(common.home(), cfg)
        with self.assertRaises(PermissionError):
            companion.get(rec["id"])
        self.assertTrue(all(x["workflow"] is None for x in companionviews.snapshot()["items"]))

    def test_checkpoint_does_not_acknowledge_changes(self):
        rec = self.create()
        tk = common.read_json(store.ticket_path("ABC-1001"))
        tk["commits"] = {"one": {"sha": "r1"}}
        common.write_json(store.ticket_path("ABC-1001"), tk)
        rec = companion.checkpoint(rec["id"], rec["revision"])
        delta = companion.resume(rec["id"])["unread"]
        self.assertTrue(delta)
        rec = companion.checkpoint(rec["id"], rec["revision"])
        self.assertEqual(delta, companion.resume(rec["id"])["unread"])
        companion.acknowledge(rec["id"], rec["revision"], delta)
        self.assertEqual([], companion.resume(rec["id"])["unread"])

    def test_other_ticket_and_path_injection_denied(self):
        common.write_json(store.ticket_path("ABC-1002"), store.new_ticket("ABC-1002"))
        with self.assertRaises(PermissionError):
            companion.ticket("ABC-1002")
        with self.assertRaises(ValueError):
            workflowstore.load("../../private")
        with self.assertRaises(ValueError):
            companionviews.read({"operation": "save"})

    def test_bound_summary_and_no_plan_leak(self):
        rec = self.create()
        companion.save_plan(rec["id"], rec["revision"], plan_body())
        view = companionviews.snapshot()
        self.assertLessEqual(len(json.dumps(view).encode()), 65536)
        self.assertNotIn(plan_body()["text"], json.dumps(view))
        self.assertEqual(view["items"][0]["action"], "Resume Task")


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.baseline = model.baseline([{"id": "ac" + str(i), "text": "Behavior " + str(i)} for i in range(1, 5)])
        self.context = {k: "r1" for k in ("repo_id", "location", "head", "worktree", "config", "environment")}

    def test_numeric_coverage_confidence_independent(self):
        answers = [{"id": "ac" + str(i + 1), "status": s, "confidence": .2} for i, s in enumerate(model.COVERAGE)]
        a = jev.validate_decisions(self.baseline, answers)
        self.assertEqual(a["coverage"]["percent"], 37.5)
        for row in answers:
            row["confidence"] = .99
        self.assertEqual(a["coverage"], jev.validate_decisions(self.baseline, answers)["coverage"])
        self.baseline["criteria"][0]["weight"] = 2
        self.assertEqual(jev.validate_decisions(self.baseline, answers)["coverage"]["percent"], 50.0)
        self.assertIsNone(jev.validate_decisions(self.baseline, [], available=False)["coverage"]["percent"])

    def test_bad_weights_missing_and_invalid_answers(self):
        for weight in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                model.baseline([{"id": "ac1", "text": "Expected", "weight": weight}])
        result = jev.validate_decisions(self.baseline, [{"id": "ac1", "status": "Covered", "confidence": float("nan")}])
        self.assertEqual(result["coverage"]["counts"]["Unverified"], 4)
        with self.assertRaises(PermissionError):
            jev.evaluate()

    def evidence(self):
        return {"id": "test1", "criteria": ["ac1"], "steps": ["one"], "kind": "test", "outcome": "passed",
                "coverage": "complete", "context": self.context, "source_ref": "tests:test1",
                "observed_at": "2026-11-07T10:00:00Z", "baseline": self.baseline["id"]}

    def test_later_changes_and_unknown_dirty_tree_invalidate_pass(self):
        evidence = self.evidence()
        self.assertEqual(model.verification(self.baseline, [evidence], self.context)["rows"][0]["status"], "Verified")
        for current in (dict(self.context, head="r2"), dict(self.context, worktree=None)):
            self.assertEqual(model.verification(self.baseline, [evidence], current)["rows"][0]["status"], "Unverified")
        evidence["kind"] = "model_report"
        self.assertEqual(model.verification(self.baseline, [evidence], self.context)["rows"][0]["status"], "Unverified")

    def test_blank_checklist_does_not_repeat_evidenced_work(self):
        plan = model.plan(plan_body(), self.baseline)
        evidence = self.evidence()
        progress = model.reconcile(plan, [evidence], self.context)
        self.assertEqual(progress["completed"], ["one"])
        self.assertEqual(progress["verification"], ["two"])
        self.assertEqual(model.reconcile(plan, [evidence], dict(self.context, head="r2"))["uncertain"], ["one"])

    def test_impossible_state_jumps_rejected(self):
        for start, target in (("new", "completed"), ("review_incomplete", "in_development"), ("completed", "approved")):
            with self.assertRaises(ValueError):
                model.transition({"stage": start}, target)


if __name__ == "__main__":
    unittest.main()
