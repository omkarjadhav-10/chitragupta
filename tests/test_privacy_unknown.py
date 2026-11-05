"""O6: an "unknown" privacy result is stored and blocks the push (the last known result is kept beside it), with the
same warning in health, doctor and the daily footer. O8: the other health warnings (push failing, hooks silent)."""
import datetime as dt
import os
import unittest

from tests.support import Isolated, write_config


class PrivacyUnknown(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.cfg = {"worklog_home": self.home, "github_remote": "https://github.com/x/y", "paths": {}}
        write_config(self.home, self.cfg)

    def test_private_then_unknown_blocks_push_and_warns(self):
        from chitragupta import agent, common, doctor, gitsync, render
        health = {}
        os.environ["CHITRAGUPTA_PRIVACY_SIMULATE"] = "private"
        self.assertEqual(gitsync.check_privacy(self.cfg, health, force=True), "private")
        self.assertEqual(health["privacy_last_known"], "private")
        known_at = health["privacy_last_known_at"]
        os.environ["CHITRAGUPTA_PRIVACY_SIMULATE"] = "unknown"
        self.assertEqual(gitsync.check_privacy(self.cfg, health, force=True), "unknown")
        self.assertEqual(health["privacy"], "unknown")
        self.assertEqual((health["privacy_last_known"], health["privacy_last_known_at"]), ("private", known_at))
        gitsync.ensure_repo(self.cfg)
        self.assertFalse(gitsync.push(self.cfg, health))
        self.assertEqual(health.get("push_blocked"), "unknown")
        want = "repo privacy unknown — pushes paused (last known: private, %s)" % common.parse_ts(known_at).strftime("%Y-%m-%d %H:%M")
        a = agent.Agent()
        a.health.update(health)
        a.update_health()
        self.assertTrue(any(want in w for w in a.health["warnings"]), a.health["warnings"])
        row = doctor.push_row(self.cfg, health)
        self.assertFalse(row["ok"])
        self.assertIn(want, row["detail"])
        foot = render.health_footer({"sessions": {}, "stats": {}}, {k: v for k, v in health.items() if k != "warnings"})
        self.assertIn(want, foot)
        self.assertEqual(render.health_footer({"sessions": {}, "stats": {}}, a.health).count(want), 1)
        # private again: the warning goes
        os.environ["CHITRAGUPTA_PRIVACY_SIMULATE"] = "private"
        self.assertEqual(gitsync.check_privacy(self.cfg, health, force=True), "private")
        a.health.update(health)
        a.update_health()
        self.assertFalse(any("privacy unknown" in w for w in a.health["warnings"]))

    def test_unknown_with_no_earlier_result(self):
        from chitragupta import gitsync
        os.environ["CHITRAGUPTA_PRIVACY_SIMULATE"] = "unknown"
        health = {}
        self.assertEqual(gitsync.check_privacy(self.cfg, health, force=True), "unknown")
        self.assertNotIn("privacy_last_known", health)
        self.assertIn("no earlier result", gitsync.privacy_warning(health))


class HealthWarnings(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"worklog_home": self.home})

    def test_push_failing_and_silent_hooks(self):
        from chitragupta import agent, common
        a = agent.Agent()
        a.health["push_failing_since"] = (common.now() - dt.timedelta(hours=7)).isoformat(timespec="seconds")
        a.health["last_hook_event"] = "2026-10-01T10:00:00+05:30"
        a.state["safety_net_days"] = ["2026-10-02", "2026-10-03", "2026-10-06"]
        a.update_health()
        w = " | ".join(a.health["warnings"])
        self.assertIn("pushes to GitHub have failed since", w)
        self.assertIn("hooks recorded nothing on 3 recent work days", w)
        a.health["push_failing_since"] = (common.now() - dt.timedelta(hours=1)).isoformat(timespec="seconds")
        a.state["safety_net_days"] = ["2026-10-02"]
        a.update_health()
        self.assertEqual(a.health["warnings"], [])


if __name__ == "__main__":
    unittest.main()
