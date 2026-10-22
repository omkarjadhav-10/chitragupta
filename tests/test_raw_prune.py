"""O7: fully processed raw capture older than raw_retention_days is pruned on every install (raw is never pushed),
not only after a push; raw that isn't fully read and extracted is never pruned."""
import datetime as dt
import os
import time
import unittest

from tests.support import Isolated, write_config


class RawPrune(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"worklog_home": self.home, "raw_retention_days": 30})  # no github_remote

    def raw(self, days_ago, sid, text='{"event": "prompt", "ticket": "ABC-1001"}\n'):
        from chitragupta import common
        day = (dt.date.today() - dt.timedelta(days=days_ago)).isoformat()
        p = os.path.join(common.wl("raw"), day, sid + ".jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        old = time.time() - days_ago * 86400
        os.utime(p, (old, old))
        return p

    def test_processed_old_raw_pruned_without_a_push(self):
        from chitragupta import agent
        a_p, b_p, c_p = self.raw(40, "aaa"), self.raw(40, "bbb"), self.raw(2, "ccc")
        a = agent.Agent()
        self.assertFalse(a.health.get("last_push"))
        size = os.path.getsize(a_p)
        a.state["sessions"] = {
            "aaa": {"files": {a_p: {"read": size, "extracted": size}}, "last_event": "2020-01-01"},
            "bbb": {"files": {b_p: {"read": size, "extracted": 0}}, "last_event": "2020-01-01"},
            "ccc": {"files": {c_p: {"read": size, "extracted": size}}, "last_event": "2020-01-01"}}
        a.prune()
        self.assertFalse(os.path.exists(a_p), "processed raw older than 30 days is pruned")
        self.assertTrue(os.path.exists(b_p), "unprocessed raw is never pruned")
        self.assertTrue(os.path.exists(c_p), "recent raw is kept")
        self.assertNotIn("aaa", a.state["sessions"])
        self.assertIn("bbb", a.state["sessions"])

    def test_pruned_even_when_newer_than_the_last_push(self):
        from chitragupta import agent, common
        p = self.raw(40, "ddd")
        a = agent.Agent()
        a.health["last_push"] = (common.now() - dt.timedelta(days=60)).isoformat(timespec="seconds")
        size = os.path.getsize(p)
        a.state["sessions"] = {"ddd": {"files": {p: {"read": size, "extracted": size}}, "last_event": "2020-01-01"}}
        a.prune()
        self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
