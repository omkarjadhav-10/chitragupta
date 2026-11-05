"""K5: Jira cards for every ticket active in the last 7 days whose card is missing or older than its last active day."""
import datetime as dt
import types
import unittest

from tests.support import Isolated, local_iso, write_config

TODAY = "2026-09-10"


def day(n):
    return (dt.date.fromisoformat(TODAY) - dt.timedelta(days=n)).isoformat()


class JiraCards(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00"})
        from chitragupta import common, kbjobs, mcpjobs, store
        common.load_config()
        for n, keys in ((9, ["ABC-1009"]), (5, ["ABC-1001"]), (2, ["ABC-1002"]), (0, ["ABC-1003"])):
            common.write_json(store.day_path(day(n)), {"date": day(n), "tickets": {k: {"minutes": 10} for k in keys}})
        jira = lambda k, rec: common.write_json(common.hp("data", "jira", k + ".json"), rec)
        jira("ABC-1003", {"fetched_at": local_iso(TODAY, 9, 0), "card": {"found": True}})  # fresh
        jira("ABC-1002", {"fetched_at": local_iso(day(3), 9, 0), "card": {"found": True}})  # older than its last day
        jira("ABC-1001", {"evening": {"x": 1}})  # no card yet
        self.calls = []
        real = mcpjobs.jira_card
        self.addCleanup(setattr, mcpjobs, "jira_card", real)

        def fake(t, cfg, health):
            self.calls.append(t)
            rec = {"raw": "x", "fetched_at": local_iso(TODAY, 10, 0), "card": {"found": True, "title": t + " title"}}
            jira(t, rec)
            return rec
        mcpjobs.jira_card = fake
        self.st = store.Store()
        self.jobs = kbjobs.KBJobs(types.SimpleNamespace(cfg={"day_starts_at": "04:00"}, store=self.st,
                                                        health={"mcp": {"jira_auth_ok": True}}))

    def test_last_7_days_missing_first_then_oldest(self):
        self.assertEqual(self.jobs.jira_wanted(TODAY), ["ABC-1001", "ABC-1002"])
        self.assertEqual(self.jobs.jira_cards(TODAY, limit=2), 2)
        self.assertEqual(self.calls, ["ABC-1001", "ABC-1002"])
        self.assertEqual(self.st.ticket("ABC-1001")["title"], "ABC-1001 title")
        self.assertEqual(self.jobs.jira_cards(TODAY, limit=2), 0)  # all fresh now

    def test_limit_and_recent_missing_key_skipped(self):
        from chitragupta import common, store
        common.write_json(store.ticket_path("ABC-1001"), {"id": "ABC-1001", "jira_missing": local_iso(day(1), 10, 0)})
        self.assertEqual(self.jobs.jira_cards(TODAY, limit=1), 1)
        self.assertEqual(self.calls, ["ABC-1002"])


if __name__ == "__main__":
    unittest.main()
