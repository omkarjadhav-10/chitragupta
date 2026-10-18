"""Past-day view: `/cg:dashboard day <date|today|yesterday|-N>` and the day data (send history, branch and PR events of
that day, the day's tickets' alerts)."""
import datetime as _dt
import json
import os
import unittest

from tests import test_dashboard as td

from chitragupta import common


class DayArg(td.Base):
    def test_forms(self):
        from chitragupta.dashboard.__main__ import day_arg
        today = _dt.date.fromisoformat(common.workday(common.now(), common.load_config()))
        self.assertEqual(day_arg("today"), (today.isoformat(), None))
        self.assertEqual(day_arg("yesterday"), ((today - _dt.timedelta(days=1)).isoformat(), None))
        self.assertEqual(day_arg("-3"), ((today - _dt.timedelta(days=3)).isoformat(), None))
        self.assertEqual(day_arg("2026-10-15"), ("2026-10-15", None))
        for bad in ("2026-13-01", "nope", "-99999", "+3"):
            day, err = day_arg(bad)
            self.assertIsNone(day)
            self.assertIn("YYYY-MM-DD, today, yesterday or -N", err)


class DayData(td.Base):
    def test_history_events_and_alerts(self):
        from chitragupta.dashboard import api
        day = self.days["day"]
        p = common.hp("data", "days", day[:4], day + ".json")
        with open(p, encoding="utf-8") as f:
            dd = json.load(f)
        dd["branch_activity"] = [{"ts": day + "T11:05:00+05:30", "repo_name": "shop-api", "text": "created fix/ABC-1001-total from qa"}]
        td._w(p, dd)
        tp = common.hp("data", "tickets", td.TICKET + ".json")
        with open(tp, encoding="utf-8") as f:
            tk = json.load(f)
        tk["git"] = {"repos": {"shop-api": {"prs": [{"number": 7, "source": "fix/ABC-1001-total", "target": "qa",
                                                     "created_at": day + "T12:00:00+05:30", "merged_at": None}]}},
                     "alerts": [{"kind": "gap", "text": "1 commit not in qa", "repo": "shop-api"}]}
        td._w(tp, tk)
        sp = common.hp("data", "timesheets", day + ".json")
        td._w(sp, {"status": "sent", "versions": [{"v": 1, "at": day + "T20:00:00+05:30", "kind": "message", "text": "x",
                                                   "slack": {"ok": True}},
                                                  {"v": 2, "at": day + "T21:00:00+05:30", "kind": "message", "why": "resend",
                                                   "text": "x", "slack": {"ok": False, "error": "timeout"}}],
                   "entries": []})
        api._cache.clear()
        d = api.day(day, preview=False)
        self.assertEqual([e["kind"] for e in d["branch_events"]], ["branch", "pr"])
        self.assertIn("PR #7 opened", d["branch_events"][1]["text"])
        self.assertEqual(d["alerts"], [{"ticket": td.TICKET, "kind": "gap", "text": "1 commit not in qa", "repo": "shop-api"}])
        h = d["worklog_saved"]["history"]
        self.assertEqual([(x["v"], x["sent"], x["why"]) for x in h], [(1, True, ""), (2, False, "resend")])
        self.assertEqual(h[1]["error"], "timeout")

    def test_empty_day(self):
        from chitragupta.dashboard import api
        d = api.day("2001-01-01", preview=False)
        self.assertEqual((d["tickets"], d["other"], d["branch_events"], d["alerts"], d["worklog_saved"]), ([], [], [], [], None))


if __name__ == "__main__":
    unittest.main()
