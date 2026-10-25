"""Six questions use current cited evidence and deterministic delivery/verification facts."""
import copy
import unittest
from tests.test_brief import BriefBase, T, it, ticket, c


class TicketSummary(BriefBase, unittest.TestCase):
    def test_generation_adds_six_sections_without_losing_old_brief(self):
        from chitragupta import brief
        self.output["summary"] = {"unknowns": [c("Partial cancels still need checking.", "rk00000001")],
                                  "next_action": c("Run the partial cancel check in QA.", "rk00000001")}
        self.gen()
        tk = self.saved()
        self.assertEqual(set(tk["brief"]["sections"]), set(brief.SECTIONS))
        self.assertTrue(tk["brief"]["summary"]["next_action"])
        view = brief.view(tk, T, self.cfg())["summary"]
        self.assertEqual([s["id"] for s in view["sections"]], ["problem", "root_cause", "changed", "verified", "unknowns", "next_action"])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(c["cites"] for s in view["sections"] for c in s["claims"]))
        self.assertIn("partial cancel", view["next_action"]["text"])

    def test_empty_summary_and_unexecuted_replay_are_honest(self):
        from chitragupta import summary
        tk = {"id": T, "items": {}, "replay": {"final": [{"n": 1, "name": "Cancel", "expected": "200", "executed": False}]}}
        view = summary.view(tk, T, self.cfg(), card={})
        for s in view["sections"]:
            self.assertEqual(s["text"], "Nothing verified yet" if s["id"] == "verified" else "Not recorded yet")
        self.assertIsNone(view["next_action"])

    def test_invalid_or_superseded_cites_drop_and_owner_cause_wins(self):
        from chitragupta import brief, common, store
        tk = self.saved()
        tk["items"]["owner"] = it("owner", "note", "root cause: The coupon cancel skips stock release", session="inbox")
        tk["items"]["extractor"] = it("extractor", "note", "root cause: An unrelated guess", session="session")
        common.write_json(store.ticket_path(T), tk)
        self.output["summary"] = {"unknowns": [c("A ghost result", "ghost")], "next_action": c("Run the old job", "rc00000001")}
        self.gen()
        v = brief.view(self.saved(),T,self.cfg())["summary"]
        root = next(s for s in v["sections"] if s["id"] == "root_cause")
        self.assertIn("coupon cancel", root["claims"][0]["text"])
        self.assertEqual(root["claims"][0]["cites"], ["owner"])
        self.assertIsNone(v["next_action"])
        self.assertTrue(self.saved()["brief"]["summary_dropped"])

    def test_jira_and_actual_tests_win_over_model_status_words(self):
        from chitragupta import brief
        self.output["summary"] = {"unknowns": [], "next_action": c("Run another cancel test", "fx00000001"),
                                 "problem": [c("Invented problem", "rc00000002")],
                                 "changed": [c("No commit exists", "fx00000001")], "verified": [c("Nothing ran", "fx00000001")]}
        self.gen()
        v = brief.view(self.saved(),T,self.cfg())["summary"]
        text = str(v)
        self.assertIn("Cancel an order: stock stays reserved", text)
        self.assertIn("passed", text)
        self.assertNotIn("Invented problem", text)
        self.assertNotIn("No commit exists", text)
        self.assertNotIn("Nothing ran", text)

    def test_migration_backs_up_brief_and_preserves_seven_sections(self):
        from chitragupta import common, kb51, migrate, store
        self.gen()
        tk = self.saved()
        tk["brief"].pop("summary",None)
        common.write_json(store.ticket_path(T),tk)
        before = copy.deepcopy(tk["brief"]["sections"])
        backups = []
        self.assertIn(("kb51-summary", "kb51:ticket_summary"), migrate.KB_STEPS)
        kb51.ticket_summary(False, [], backups.append)
        self.assertIn(store.ticket_path(T), backups[0])
        saved = self.saved()
        self.assertEqual(saved["brief"]["sections"],before)
        self.assertIn("next_action",saved["brief"]["summary"])

    def test_execution_flag_without_recorded_outcome_is_not_verification(self):
        from chitragupta import summary
        tk = {"id": T, "items": {}, "replay": {"final": [{"n": 1, "name": "Cancel", "expected": "200", "executed": True}]}}
        v = summary.view(tk,T,self.cfg(),card={})
        self.assertEqual(next(s for s in v["sections"] if s["id"] == "verified")["text"], "Nothing verified yet")

    def test_jira_description_beats_an_old_model_problem(self):
        from chitragupta import brief, common, store
        tk = self.saved()
        tk["problem"] = "Unrelated old model problem"
        common.write_json(store.ticket_path(T),tk)
        self.gen()
        v = brief.view(self.saved(),T,self.cfg())["summary"]
        problem = next(s for s in v["sections"] if s["id"] == "problem")
        self.assertIn("Cancel an order", problem["claims"][0]["text"])
        self.assertNotIn("Unrelated old model problem", str(v))

    def test_newest_three_changes_and_earlier_count(self):
        from chitragupta import summary
        commits = {str(n)*40: {"sha":str(n)*40,"subject":"change%d" % n,"date":"2026-09-0%d" % n,"repo_name":"shop-api"} for n in range(1,5)}
        v = summary.view({"id":T,"items":{},"commits":commits},T,self.cfg(),card={})
        changed = next(s for s in v["sections"] if s["id"] == "changed")
        self.assertEqual(len(changed["claims"]),3)
        self.assertEqual([c["cites"] for c in changed["claims"]], [["commit:"+str(n)*7] for n in (4,3,2)])
        self.assertIn("and 1 earlier change",changed["claims"][-1]["text"])

    def test_unknown_fallback_priority_and_current_only(self):
        from chitragupta import summary
        items = [it("risk","risk","risk",date="2026-09-09"), it("qnew","question","question",date="2026-09-08"),
                 it("bold","blocker","older blocker",date="2026-09-01"),it("bnew","blocker","new blocker",date="2026-09-03"),
                 it("archived","blocker","archived",archived={"reason":"archive"}),
                 it("superseded","blocker","superseded",status="superseded"),it("review","blocker","review",review={"why":"other"})]
        tk={"id":T,"items":{i["id"]:i for i in items}}
        def ids():
            return [c["cites"][0] for s in summary.view(tk,T,self.cfg(),card={})["sections"] if s["id"] == "unknowns" for c in s["claims"]]
        self.assertEqual(ids(),["bnew","bold","qnew"])
        tk["brief"]={"summary":{"sections":{"unknowns":{"claims":[c("Selected current risk","risk")]}}}}
        self.assertEqual(ids(),["risk","bnew","bold"])

    def test_same_date_changes_prefer_further_delivery_target(self):
        from chitragupta import summary
        rows = [{"shas":[str(n)*40],"subject":"change%d" % n,"date":"2026-09-04",
                 "cells":{target:{"s":"in"}}} for n,target in ((1,"main"),(2,"qa"))]
        tk = {"id":T,"items":{},"git":{"repos":{"shop-api":{"rows":rows,
              "targets":[{"name":"qa","role":"qa"},{"name":"main","role":"main"}]}}}}
        changed = next(s for s in summary.view(tk,T,self.cfg(),card={})["sections"] if s["id"] == "changed")
        self.assertEqual(changed["claims"][0]["cites"],["commit:1111111"])
