"""Placeholder tickets: the configured example ticket (and `placeholder_tickets`) is never a ticket, never gets time,
a hub or a Jira call. Synthetic keys only: ABC-0000 is the example, ABC-1001 real work.
"""
import json
import os
import tempfile
import unittest

from tests.support import PY, Isolated, rd, run_hook, write_config

DAY = "2026-10-14"  # a Wednesday


class PlaceholderCase(Isolated, unittest.TestCase):
    config = {"ticket_prefixes": ["ABC"], "capture_scope": {"include_paths": [], "exclude_paths": []},
              "paths": {"python": PY}, "render": {"example_ticket": "ABC-0000"},
              "jira_worklog": {"example_ticket": "ABC-0000"}, "placeholder_tickets": ["ABC-0002"]}

    def setUp(self):
        h = tempfile.mkdtemp(prefix="cg-test-")
        write_config(h, self.config)
        self.isolate(h)

    def tearDown(self):
        from chitragupta import common
        self.restore()
        common.set_placeholders({})

    def test_detection_skips_placeholders(self):
        from chitragupta import common, store
        cfg = common.load_config()
        self.assertEqual(common.placeholder_tickets(cfg), {"ABC-0", "ABC-2"})
        self.assertEqual(common.find_tickets("e.g. ABC-0000_notes.md, see ABC-1001 and ABC-0002", ["ABC"]), ["ABC-1001"])
        self.assertIsNone(common.ticket_from_branch("feature/ABC-0000-example", ["ABC"]))
        self.assertEqual(common.ticket_from_branch("feature/ABC-1001-demo", ["ABC"]), "ABC-1001")
        self.assertIsNone(store.valid_ticket("ABC-0000", {"ABC"}))
        self.assertEqual(store.valid_ticket("ABC-1001", {"ABC"}), "ABC-1001")

    def test_builtin_default_example_is_not_a_placeholder(self):
        from chitragupta import common
        self.assertEqual(common.placeholder_tickets({"render": {"example_ticket": common.DEFAULT_EXAMPLE_TICKET}}),
                         frozenset())
        self.assertEqual(common.placeholder_tickets({"placeholder_tickets": [common.DEFAULT_EXAMPLE_TICKET]}),
                         {common.DEFAULT_EXAMPLE_TICKET})

    def test_hook_time_only_line_never_carries_the_example(self):
        run_hook("user-prompt", {"session_id": "s1", "cwd": self.home,
                                 "user_prompt": "the hint says e.g. ABC-0000_name.md, where do I put it?"}, self.child_env())
        rows = []
        for dp, _, fs in os.walk(os.path.join(self.home, ".worklog", "raw")):
            rows += [json.loads(l) for f in fs for l in rd(os.path.join(dp, f)).splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["ticket"])

    def test_example_gets_no_time_in_the_evening_worklog(self):
        """Raw time-only lines written before the fix still carry the example key: it gets no time."""
        from chitragupta import agent, common
        tdir = os.path.join(self.home, ".worklog", "raw", DAY, "time")
        os.makedirs(tdir)
        rows = [("s1", "11:00", "ABC-0000"), ("s1", "11:10", None), ("s1", "11:25", None),
                ("s2", "15:00", "ABC-1001"), ("s2", "15:20", None), ("s2", "15:40", None)]
        for sid, hm, t in rows:
            with open(os.path.join(tdir, sid + ".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": "%sT%s:00+05:30" % (DAY, hm), "event": "user-prompt", "session_id": sid,
                                    "source": "time-only", "ticket": t}) + "\n")
        common.load_config()
        a = agent.Agent()
        cal = os.path.join(self.home, "data", "calendar.json")
        os.makedirs(os.path.dirname(cal), exist_ok=True)
        with open(cal, "w", encoding="utf-8") as f:
            json.dump({"time": {DAY: [{"ticket": "ABC-0002", "minutes": 60, "text": "example"}]}}, f)
        res = a.jwl.build(DAY, "2026-10-15", until=None, facts=None, condense=False, delivery=False)
        self.assertEqual(res["kind"], "message")
        self.assertIn("ABC-1001", res["tracked"])
        self.assertNotIn("ABC-0000", res["tracked"])
        self.assertNotIn("ABC-2", res["tracked"])
        self.assertNotIn("ABC-0002", res["tracked"])
        self.assertNotIn("ABC-0000", res["text"])

    def test_compose_drops_placeholder_entries(self):
        from chitragupta import timesheet as T
        ts = T.settings({})
        manual = [{"ticket": "ABC-0000", "minutes": 120, "text": "x"}, {"ticket": "ABC-1001", "minutes": 60, "text": "y"}]
        res = T.compose(DAY, "2026-10-15", [], manual, {}, ts, placeholders=["ABC-0000"])
        self.assertEqual(set(res["tracked"]), {"ABC-1001"})

    def test_no_jira_card_for_the_example(self):
        from chitragupta import agent, common
        common.load_config()
        a = agent.Agent()
        a.store.day(DAY)["tickets"]["ABC-0000"] = {"minutes": 5}
        self.assertEqual(a.jobs.jira_cards(DAY), 0)  # the MCP tripwire fails the test if a call is made


if __name__ == "__main__":
    unittest.main()
