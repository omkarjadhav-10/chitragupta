"""Gate 2: "Verified" also comes from QA's Jira status.

A ticket QA had passed showed "Not verified" because only a passing test: or replay: fact counted. Now, without one,
a Jira status in brief.verified_statuses (default QA COMPLETED, QA PASSED, READY FOR RELEASE, DONE, CLOSED, RESOLVED;
any case) makes it "Verified by QA (Jira: <status>)", cited jira:<KEY>. Synthetic data only (ABC-1001).
"""
import unittest

from tests import test_brief as tb


class VerifiedByQa(tb.BriefBase, unittest.TestCase):
    def status(self, s, tests=False):
        from chitragupta import common, store
        tk = self.saved()
        tk["jira"]["status"] = s
        tk["tests"], tk["replay"] = (tb.ticket()["tests"] if tests else {}), {}
        common.write_json(store.ticket_path(tb.T), tk)
        card = common.read_json(common.hp("data", "jira", tb.T + ".json"))
        card["card"]["status"] = s
        common.write_json(common.hp("data", "jira", tb.T + ".json"), card)

    def after(self):
        from chitragupta import brief
        return [s for s in brief.view(self.saved(), tb.T, self.cfg())["sections"] if s["id"] == "after"][0]["verified"]

    def test_qa_status_verifies_without_a_test(self):
        from chitragupta import render
        self.status("qa completed")
        self.output["after"]["verified"] = {"state": "not-verified", "text": "Run the cancel replay.", "cites": []}
        self.gen()
        v = self.after()
        self.assertEqual((v["state"], v["label"], v["cites"]),
                         ("verified", "Verified by QA (Jira: qa completed)", ["jira:" + tb.T]))
        md = "\n".join(render.render_brief(render.brief_view(self.saved(), tb.T), self.saved()["items"]))
        self.assertIn("**Verified by QA (Jira: qa completed)**", md)
        self.assertNotIn("Not verified", md)

    def test_validator_accepts_the_jira_cite(self):
        self.status("Ready for Release")
        self.output["after"]["verified"] = {"state": "verified", "text": "QA passed it.", "cites": ["jira:" + tb.T]}
        self.gen()
        stored = self.saved()["brief"]["sections"]["after"]["verified"]
        self.assertEqual((stored["state"], stored["cites"]), ("verified", ["jira:" + tb.T]))

    def test_other_statuses_and_a_passing_test_keep_their_label(self):
        self.status("In Progress")
        self.output["after"]["verified"] = {"state": "not-verified", "text": "", "cites": []}
        self.gen()
        self.assertEqual(self.after()["state"], "not-verified")
        self.status("Done", tests=True)
        self.output["after"]["verified"] = {"state": "verified", "text": "The cancel tests pass.",
                                            "cites": ["test:te00000001"]}
        self.gen(force=True)
        v = self.after()
        self.assertEqual((v["state"], v["label"], v["cites"]), ("verified", "Verified", ["test:te00000001"]))

    def test_configurable(self):
        from chitragupta import brief, common, settings
        self.assertIn("QA COMPLETED", brief.verified_statuses(self.cfg()))
        common.write_json(common.wl("config.json"), dict(common.read_json(common.wl("config.json")),
                                                         brief={"verified_statuses": ["Signed Off"]}))
        self.assertEqual(brief.verified_statuses(common.load_config()), ["Signed Off"])
        self.assertIn("brief.verified_statuses", [i["key"] for i in settings.schema(common.load_config())])


if __name__ == "__main__":
    unittest.main()


class JiraVerificationRegressions(VerifiedByQa):
    def test_reopened_jira_status_revokes_jira_only_verification(self):
        self.status("QA COMPLETED")
        self.output["after"]["verified"] = {"state": "verified", "text": "QA passed it.", "cites": ["jira:" + tb.T]}
        self.gen()
        self.status("In Progress")
        self.assertEqual(self.after()["state"], "not-verified")

    def test_removing_a_verified_status_revokes_jira_only_verification(self):
        from chitragupta import common
        self.status("QA COMPLETED")
        self.output["after"]["verified"] = {"state": "verified", "text": "QA passed it.", "cites": ["jira:" + tb.T]}
        self.gen()
        cfg = common.read_json(common.wl("config.json"))
        cfg["brief"]["verified_statuses"] = ["Signed Off"]
        common.write_json(common.wl("config.json"), cfg)
        self.assertEqual(self.after()["state"], "not-verified")

    def test_view_does_not_build_the_generation_context(self):
        from chitragupta import brief
        from unittest.mock import patch
        self.status("Done", tests=True)
        self.gen()
        with patch.object(brief, "_Ctx", side_effect=AssertionError("view built full context")):
            self.after()

    def test_passing_test_does_not_expire_after_fifteen_newer_tests(self):
        from chitragupta import brief
        tk = self.saved()
        tk["jira"]["status"] = "In Progress"
        tk["replay"] = {}
        tk["tests"] = {"old": {"id": "old", "status": "passed", "at": "2026-01-01"}}
        tk["tests"].update({str(n): {"id": str(n), "status": "failed", "at": "2026-10-%02d" % n} for n in range(1, 17)})
        tk["brief"] = {"sections": {"after": {"verified": {"state": "verified", "cites": ["test:old"]}}}}
        after = next(s for s in brief.view(tk, tb.T, self.cfg())["sections"] if s["id"] == "after")
        self.assertEqual(after["verified"]["cites"], ["test:old"])
