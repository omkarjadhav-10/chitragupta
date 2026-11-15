"""Companion fixes found by the recorded UX-04 / UX-06 walkthroughs (scripts/ui/companion_walk.py).

Each class pins one interaction a walkthrough found broken or stated falsely. Synthetic data only (ABC-…, acme.test),
frozen clocks, isolated data homes; every process start is a tripwire (tests.test_companion_state.Base).
"""
import json
import os
import unittest

from tests.support import write_config
from tests.test_companion_state import HEAD1, HEAD2, Base
from chitragupta import common, companion, companionviews as views, store, workflowstore
from chitragupta.dashboard import companion_api


class UxBase(Base):
    def setUp(self):
        super().setUp()
        views._SLIM.clear()
        views._JIRA.clear()

    def read(self, operation, **args):
        return views.read(dict(args, operation=operation))


class PlannerBeforeIdentity(UxBase):
    """UX-04 1: the planner of a home with nothing confirmed crashed the page (`groups` was an object)."""

    def test_unconfirmed_planner_has_the_confirmed_shape(self):
        write_config(common.home(), {"watched_repos": [self.repo]})
        out = self.read("planner")
        self.assertEqual(out["state"], "identity_unconfirmed")
        self.assertEqual((out["groups"], out["items"], out["total"], out["more"]), ([], [], 0, False))
        self.assertIsInstance(out["scope"], str)
        self.assertEqual(out["completed"], {"collapsed": True, "count": 0})
        write_config(common.home(), self.cfg)
        ready = self.read("planner")
        self.assertIsInstance(ready["groups"], list)
        self.assertLessEqual({"groups", "items", "scope", "completed", "empty"}, set(out))


class DeliveryLinks(UxBase):
    """UX-04 3: delivery details pointed at a ticket tab that does not exist (`tab=delivery` opened Overview), and
    `Resolve release mapping` led nowhere."""

    def test_delivery_links_open_branches_and_the_mapping_action_has_a_target(self):
        rec = self.planned()
        self.put(release={"reminders": [{"id": "map:scratch", "kind": "mapping", "repo": "scratch", "severity": "warn",
                                         "text": "release branch for the release not confirmed"}]})
        snap = self.read("snapshot", workflow=rec["id"])
        self.assertEqual(snap["next_action"]["label"], "Resolve release mapping")
        self.assertEqual(snap["next_action"]["target"], "#/tickets/ABC-1001?tab=branches")
        self.assertEqual(snap["focus"]["links"]["delivery"], "#/tickets/ABC-1001?tab=branches")
        self.assertEqual(self.read("detail", workflow=rec["id"])["links"]["delivery"], "#/tickets/ABC-1001?tab=branches")
        tickets_js = os.path.join(os.path.dirname(views.__file__), "dashboard", "app", "pages", "tickets.js")
        with open(tickets_js, encoding="utf-8") as f:
            self.assertIn("'branches'", f.read().split("const TABS = [", 1)[1].split("]", 1)[0])


class ApplicabilityWords(UxBase):
    """UX-04 5: a test whose context was never captured read "earlier evidence no longer applies", the words for
    historical evidence. Unknown and historical are now told apart, and each source event carries what the ledger
    row's details show (revision, basis, applicability and why)."""

    def test_unknown_is_not_called_historical(self):
        rec = self.started(n=3)
        rec = self.observe(rec)
        self.captured_test(rec, "t-old", head=HEAD2)
        self.captured_test(rec, "t-blind", context=False)
        rec = self.map(rec, evidence="test:t-old", criteria=["ac1"], coverage="complete")
        rec = self.map(rec, evidence="test:t-blind", criteria=["ac2"], coverage="complete")
        rows = {r["criterion"]: r for r in self.read("verification", workflow=rec["id"])["rows"]}
        self.assertRegex(rows["ac1"]["applicability"], r"^Unverified: earlier evidence no longer applies \(.*head")
        self.assertRegex(rows["ac2"]["applicability"], r"^Unverified: applicability unknown \(not captured: ")
        self.assertNotIn("earlier evidence", rows["ac2"]["applicability"])
        self.assertEqual(rows["ac3"]["applicability"], "Unverified: no current applicable evidence")
        old = rows["ac1"]["evidence"][0]
        self.assertEqual((old["applicability"], old["revision"], old["basis"]), ("historical", HEAD2, "captured"))
        self.assertIn("head", old["reason"])
        self.assertEqual(rows["ac2"]["evidence"][0]["applicability"], "unknown")

    def test_both_kinds_are_named(self):
        row = {"criterion": "ac1", "status": "Unverified", "excluded": False, "failing": False, "invalidation": "x",
               "evidence": [{"applicability": "historical", "reason": "changed since observation: head"},
                            {"applicability": "unknown", "reason": "not captured: worktree"}]}
        self.assertEqual(views._applicability_text(row), "Unverified: earlier evidence no longer applies (changed "
                                                         "since observation: head); applicability unknown (not "
                                                         "captured: worktree)")


class VersionOrder(UxBase):
    """UX-06 2 / 3: checkpoints are stored with sorted keys, so "Version N" followed the plan IDs' hash order: a
    manual edit could read Version 1 and the generated plan before it Version 2. Numbers now follow creation."""

    def test_version_numbers_follow_creation_not_plan_ids(self):
        from tests.test_companion_state import plan_body
        rec = self.create(n=1)
        made = []
        for text in ("First saved plan.", "Second: keep the filter in the URL.", "Third: persist per user.",
                     "Fourth: verify both."):
            rec = companion.save_plan(rec["id"], rec["revision"], plan_body(text=text))
            made.append(rec["selected_plan"])
        self.assertNotEqual(sorted(made), made, "precondition: hash order differs from creation order")
        loaded = workflowstore.load(rec["id"])
        self.assertEqual(companion.plan_order(loaded), made)
        versions = self.read("detail", workflow=rec["id"])["plan_versions"]
        self.assertEqual([(v["id"], v["number"]) for v in versions], [(p, i + 1) for i, p in enumerate(made)])
        self.assertEqual([v["number"] for v in self.read("review", workflow=rec["id"])["versions"]], [1, 2, 3, 4])
        self.assertEqual([v["id"] for v in self.read("review", workflow=rec["id"])["versions"]], made)

    def test_records_without_an_order_list_put_parents_first(self):
        from tests.test_companion_state import plan_body
        rec = self.create(n=1)
        made = []
        for text in ("Root plan.", "Child plan.", "Grandchild plan."):
            rec = companion.save_plan(rec["id"], rec["revision"], plan_body(text=text))
            made.append(rec["selected_plan"])
        legacy = workflowstore.load(rec["id"])
        legacy.pop("plan_order")
        self.assertEqual(companion.plan_order(legacy), made, "parent chain gives the order of an older record")
        companion.add_plan(legacy, dict(legacy["plans"][made[0]], id=made[0]))
        self.assertEqual(legacy["plan_order"], made, "an identical re-save keeps its first place")


class PlannerOnlyYourWork(UxBase):
    """UX-04 1 / 6: a ticket reassigned to someone else was offered as New: Generate Plan."""

    def test_reassigned_ticket_without_a_task_is_not_offered(self):
        tk = store.new_ticket("ABC-1003")
        tk.update(title_hint="Reassigned away", first="2026-11-01", last="2026-11-02",
                  timeline={"2026-11-01": {"minutes": 50}},
                  ownership={"state": "other", "account": "dev-a", "checked_at": self.stamp(),
                             "history": [{"at": "2026-10-20T09:00:00+00:00", "state": "you", "account": "dev-a"},
                                         {"at": "2026-11-05T09:00:00+00:00", "state": "other", "account": "dev-a"}]})
        common.write_json(store.ticket_path("ABC-1003"), tk)
        from chitragupta.dashboard import api
        rows = {t["key"]: t for t in api.tickets(cfg=common.load_config())["tickets"]}
        self.assertEqual(rows["ABC-1003"]["ownership"]["state"], "reassigned", "precondition: still listed as history")
        self.planned()
        cards = [c["ticket"] for c in self.read("planner")["items"]]
        self.assertNotIn("ABC-1003", cards)
        self.assertIn("ABC-1001", cards)
        self.assertNotIn("ABC-1003", [c["ticket"] for c in self.read("snapshot")["items"]])


class ProfileReconciliation(UxBase):
    """UX-04 6: after any profile change every task disappeared for good (there was no reconciliation, so a task
    still in scope showed as New and could only be started again). Revoke first, then an explicit reconciliation
    re-binds what the profile still authorizes; anything outside it stays hidden and unchanged (ARCH-05A, A73)."""

    def test_reconcile_rebinds_only_what_still_fits(self):
        a = self.approve(self.planned())
        b = self.planned("ABC-1002", self.repo_b)
        head_b = common.read_json(os.path.join(workflowstore.root(b["id"]), "head.json"))
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["repos"] = [self.repo]
        write_config(common.home(), cfg)
        for wid in (a["id"], b["id"]):
            with self.assertRaises(PermissionError):
                companion.get(wid)
        snap = self.read("snapshot")
        self.assertEqual(snap["reconcile"]["count"], 1)
        self.assertNotIn(b["id"], json.dumps(snap))
        status, out = companion_api.mutate({"action": "reconcile_profile"})
        self.assertEqual((status, out["reconciled"], out["left_out"]), (200, 1, 1))
        self.assertNotIn("ABC-1002", json.dumps(out))
        again = companion.get(a["id"])
        self.assertEqual((again["revision"], again["plans"], again["approved_plan"]),
                         (a["revision"] + 1, a["plans"], a["approved_plan"]))
        self.assertEqual(again["reconciled"][-1]["to"], again["binding"]["profile_generation"])
        self.assertEqual(companion.intent_problem(again, again["approvals"][-1]),
                         "profile changed after approval; review again")
        with self.assertRaises(PermissionError):
            companion.get(b["id"])
        self.assertEqual(common.read_json(os.path.join(workflowstore.root(b["id"]), "head.json")), head_b)
        self.assertIsNone(self.read("snapshot")["reconcile"])
        self.assertEqual(companion.action({"action": "reconcile_profile"})["reconciled"], 0, "idempotent")

    def test_an_identity_only_change_reconciles_every_task(self):
        a, b = self.planned(), self.planned("ABC-1002", self.repo_b)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["git_author_emails"] = ["dev@acme.test", "dev.second@acme.test"]
        write_config(common.home(), cfg)
        self.assertEqual(self.read("snapshot")["reconcile"]["count"], 2)
        self.assertEqual(self.read("planner")["items"][0]["workflow"], None, "revoked until reconciled")
        self.assertEqual(companion.action({"action": "reconcile_profile"})["reconciled"], 2)
        self.assertEqual({c["ticket"]: c["workflow"] for c in self.read("planner")["items"]},
                         {"ABC-1001": a["id"], "ABC-1002": b["id"]})


if __name__ == "__main__":
    unittest.main()
