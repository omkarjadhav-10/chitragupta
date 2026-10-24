"""Model spend: every internal model call is recorded per day (tokens and dollars, by purpose), and the daily cap stops
the optional purposes (supersession, briefs) but never extraction."""
import json
import os
import unittest

from tests import test_audit_jobs as aj
from tests.support import Isolated, write_config


def _claude_reply(cost=0.25, tin=1200, tout=300):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost,
                       "usage": {"input_tokens": tin, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0,
                                 "output_tokens": tout},
                       "structured_output": {"sessions": [], "items": [], "queries": [], "time_segments": []}})


class ModelBudgetTests(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"worklog_home": self.home, "ticket_prefixes": ["ABC"]})
        from chitragupta import extract, modelbudget
        from chitragupta.safety import commands
        self._real = (extract.resolve_claude, commands.run)
        modelbudget._PENDING.clear()

        def restore():
            extract.resolve_claude, commands.run = self._real
            modelbudget._PENDING.clear()
        self.addCleanup(restore)

    def cfg(self, **over):
        from chitragupta import common
        c = common.load_config()
        c.update(over)
        return c

    def today(self, cfg):
        from chitragupta import common
        return common.workday(common.now(), cfg)

    def test_call_records_tokens_and_cost(self):
        from chitragupta import extract, modelbudget
        from chitragupta.safety import commands
        extract.resolve_claude = lambda cfg=None: ["claude.exe"]
        commands.run = lambda *a, **k: (0, _claude_reply(), "")
        cfg = self.cfg()
        out, cost = extract.call_claude("extract", "digest", cfg)
        self.assertEqual(cost, 0.25)
        health = {}
        modelbudget.drain(health, cfg)
        day = health["model_usage"][self.today(cfg)]
        self.assertEqual((day["calls"], day["in"], day["out"], day["usd"]), (1, 1300, 300, 0.25))
        self.assertEqual(day["by_purpose"]["extract"], {"calls": 1, "in": 1300, "out": 300, "usd": 0.25})
        self.assertEqual(modelbudget._PENDING, [])  # drained once

    def test_extraction_timeout_is_configurable_and_longer_by_default(self):
        """Large extraction chunks can take more than 300 s: extraction gets extract_timeout_s (default 600), other
        kinds keep their own timeouts."""
        from chitragupta import extract
        from chitragupta.safety import commands
        extract.resolve_claude = lambda cfg=None: ["claude.exe"]
        seen = []

        def run(*a, **k):
            seen.append(k.get("timeout"))
            return 0, _claude_reply(), ""
        commands.run = run
        extract.call_claude("extract", "digest", self.cfg())
        extract.call_claude("extract_delta", "digest", self.cfg())
        extract.call_claude("extract", "digest", self.cfg(extract_timeout_s=900))
        extract.call_claude("consolidate", "facts", self.cfg())
        extract.call_claude("worklog_condense", "lines", self.cfg(), timeout=180)
        self.assertEqual(seen, [600, 600, 900, 300, 180])

    def test_failed_call_still_counts_its_cost(self):
        from chitragupta import extract, modelbudget
        from chitragupta.safety import commands
        extract.resolve_claude = lambda cfg=None: ["claude.exe"]
        bad = json.loads(_claude_reply(cost=0.1))
        bad.update(subtype="error_max_turns", is_error=True)
        commands.run = lambda *a, **k: (0, json.dumps(bad), "")
        cfg = self.cfg()
        with self.assertRaises(extract.ExtractError):
            extract.call_claude("consolidate", "x", cfg)
        health = {}
        modelbudget.drain(health, cfg)
        self.assertEqual(health["model_usage"][self.today(cfg)]["by_purpose"]["consolidate"]["usd"], 0.1)

    def test_spend_at_cap_gates_brief_and_supersede_not_extract(self):
        from chitragupta import common, modelbudget
        cfg = self.cfg(budget={"model_daily_usd": 3.0})
        health = {}
        modelbudget.record(health, self.today(cfg), "extract", 50000, 4000, 3.0)
        self.assertEqual(modelbudget.budget_left(health, cfg), 0)
        self.assertTrue(modelbudget.over_budget(health, cfg, "brief"))
        self.assertTrue(modelbudget.over_budget(health, cfg, "supersede"))
        self.assertFalse(modelbudget.over_budget(health, cfg, "extract"))  # extraction is never gated
        self.assertFalse(modelbudget.over_budget(health, cfg, "consolidate"))
        self.assertEqual(health["model_usage"][self.today(cfg)]["skipped"], {"brief": 1, "supersede": 1})
        log = common.read_text(common.wl("logs", "agent.log"))
        self.assertIn("model budget: brief skipped", log)

    def test_under_cap_allows_gated_purposes(self):
        from chitragupta import modelbudget
        cfg = self.cfg()  # default cap $3
        health = {}
        modelbudget.record(health, self.today(cfg), "extract", 1000, 100, 2.99)
        self.assertFalse(modelbudget.over_budget(health, cfg, "brief"))
        self.assertAlmostEqual(modelbudget.budget_left(health, cfg), 0.01, places=4)

    def test_brief_daily_cap(self):
        from chitragupta import modelbudget
        cfg = self.cfg(brief={"daily_cap": 2})
        health = {}
        for _ in range(2):
            modelbudget.record(health, self.today(cfg), "brief", 100, 10, 0.01)
        self.assertTrue(modelbudget.over_budget(health, cfg, "brief"))
        self.assertFalse(modelbudget.over_budget(health, cfg, "supersede"))

    def test_ledger_keeps_30_days(self):
        from chitragupta import modelbudget
        health = {"model_usage": {"2020-01-%02d" % d: {"usd": 1} for d in range(1, 32)}}
        modelbudget.record(health, "2026-10-22", "extract", 1, 1, 0.0)
        self.assertEqual(len(health["model_usage"]), 30)
        self.assertIn("2026-10-22", health["model_usage"])

    def test_brief_model_defaults_to_consolidate_and_pins_accepted(self):
        from chitragupta import extract
        cfg = self.cfg(models={"extract": "claude-haiku-4-5", "consolidate": "opus"})
        self.assertEqual(extract.model_for("brief", cfg), "opus")
        self.assertEqual(extract.model_for("supersede", cfg), "opus")
        self.assertEqual(extract.model_for("extract_delta", cfg), "claude-haiku-4-5")
        cfg["models"]["brief"] = "sonnet"
        self.assertEqual(extract.model_for("brief", cfg), "sonnet")

    def test_settings_accept_model_ids_and_budget(self):
        from chitragupta import settings
        clean, errors = settings.validate({"models.extract": "claude-haiku-4-5", "budget.model_daily_usd": 5,
                                           "brief.daily_cap": 4, "models.brief": "sonnet"})
        self.assertEqual(errors, {})
        self.assertEqual(clean["models.extract"], "claude-haiku-4-5")
        _, errors = settings.validate({"models.extract": "haiku --dangerously", "budget.model_daily_usd": -1})
        self.assertEqual(sorted(errors), ["budget.model_daily_usd", "models.extract"])

    def test_health_doctor_and_dashboard_show_spend(self):
        from chitragupta import common, doctor, modelbudget
        from chitragupta.dashboard import api
        cfg = self.cfg()
        health = {}
        modelbudget.record(health, self.today(cfg), "extract", 1000, 100, 1.25)
        common.write_json(common.wl("health.json"), health)
        row = doctor.model_row(health, cfg)
        self.assertTrue(row["ok"])
        self.assertIn("$1.25 of $3.00", row["detail"])
        modelbudget.record(health, self.today(cfg), "consolidate", 1000, 100, 2.0)
        self.assertFalse(doctor.model_row(health, cfg)["ok"])
        s = api.status(cfg)
        self.assertEqual(s["models"]["usd"], 1.25)
        self.assertEqual(s["models"]["cap"], 3.0)
        self.assertEqual(s["models"]["by_purpose"]["extract"]["calls"], 1)


class McpLedger(aj.Harness):
    def test_mcp_job_spend_lands_in_the_ledger(self):
        from chitragupta import modelbudget
        health = {}
        self.job(health, reply=(0, aj.stream({aj.JIRA: "connected"}), ""))
        day = health["model_usage"]["2026-10-16"]
        self.assertEqual(day["by_purpose"]["mcp"], {"calls": 1, "in": 10, "out": 2, "usd": 0.001})
        self.assertEqual(modelbudget.summary(health, self.jcfg(), day="2026-10-16")["usd"], 0.001)


if __name__ == "__main__":
    unittest.main()
