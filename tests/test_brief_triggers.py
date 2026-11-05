"""FX-20: when a ticket brief is written. At day close for the day's tickets whose inputs changed (never per tick;
within the cap and the spend budget, skips recorded in health), by `/cg:ticket <KEY> --brief` and by the dashboard's
brief-refresh action. A fake model stands in for claude. Synthetic data only."""
import unittest

from tests import test_brief as tb
from tests.test_cli import run_cli

DAY = tb.DAY
T = tb.T


class TriggerBase(tb.BriefBase):
    def setUp(self):
        super().setUp()
        from chitragupta import common, extract, modelbudget, store
        dd = store.new_day(DAY)
        dd["tickets"][T] = {"minutes": 30, "item_ids": ["rc00000002"], "query_ids": [], "api_keys": [], "table_keys": [],
                            "integration_keys": [], "files_changed": [], "what_i_did": []}
        common.write_json(store.day_path(DAY), dd)
        fake_brief = extract.call_claude

        def fake(kind, text, cfg, timeout=300):
            if kind == "consolidate":
                self.calls.append((kind, text))
                return {"day_summary": "x", "standup": {}, "open_todos": [], "per_ticket": []}, 0.0
            return fake_brief(kind, text, cfg, timeout)
        extract.call_claude = fake
        modelbudget._PENDING.clear()

    def kinds(self):
        return [k for k, _ in self.calls]


class DayClose(TriggerBase, unittest.TestCase):
    def test_day_close_writes_the_brief_once(self):
        from chitragupta import agent
        a = agent.Agent()
        a.close_days()
        a.store.save()
        self.assertEqual(self.kinds(), ["consolidate", "supersede", "brief"])  # supersession first: the brief reads current items
        self.assertIn("brief", self.saved())
        self.assertEqual(a.health["briefs"]["made"], [T])
        a = agent.Agent()  # the next tick: the day is closed, nothing runs
        a.close_days()
        self.assertEqual(self.kinds(), ["consolidate", "supersede", "brief"])  # supersession first: the brief reads current items

    def test_cap_reached_skip_is_recorded_and_retried_later(self):
        from chitragupta import agent, common
        cfg = common.read_json(common.wl("config.json"))
        cfg["brief"] = {"daily_cap": 0}
        common.write_json(common.wl("config.json"), cfg)
        a = agent.Agent()
        a.close_days()
        a.store.save()
        self.assertEqual(self.kinds(), ["consolidate", "supersede"])
        self.assertEqual(a.health["briefs"]["skipped"], {T: "budget"})
        self.assertEqual(a.state["brief_pending"], [T])
        self.assertNotIn("brief", self.saved())
        # a later day close (cap back) picks the pending ticket up
        cfg["brief"] = {"daily_cap": 8}
        common.write_json(common.wl("config.json"), cfg)
        a.cfg = common.load_config()
        a.refresh_briefs([])
        self.assertEqual(self.kinds(), ["consolidate", "supersede", "brief"])  # supersession first: the brief reads current items
        self.assertEqual(a.state["brief_pending"], [])


class Manual(TriggerBase, unittest.TestCase):
    def test_cli_ticket_brief(self):
        rc, out, _ = run_cli("ticket", T, "--brief")
        self.assertEqual(rc, 0)
        self.assertEqual(self.kinds(), ["brief"])
        self.assertIn("Root cause (confirmed)", out)
        self.assertIn("Cancel never releases the reservation. [rc00000002]", out)
        self.assertIn("Fixes made", out)
        self.assertIn("commit:a1b2c3d", out)
        self.assertIn("brief", self.saved())
        rc, out, _ = run_cli("ticket", T, "--brief")  # nothing changed: no second call
        self.assertEqual(self.kinds(), ["brief"])
        self.assertIn("unchanged", out)

    def test_dashboard_action(self):
        from chitragupta.dashboard import actions
        st, body = actions.brief_refresh({"key": T})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["view"]["sections"][0]["claims"][0]["cites"], ["rc00000002"])
        self.assertEqual(actions.brief_refresh({"key": "../etc"})[0], 400)
        self.assertEqual(actions.brief_refresh({"key": "ABC-9999"})[0], 404)
        self.assertEqual(self.kinds(), ["brief"])


if __name__ == "__main__":
    unittest.main()
