"""Today's preview on the dashboard is the sender's own builder, and its three actions: Refresh from Jira, Preview
exactly (both switchable off in Settings) and Send again (one inbox line per request id)."""
import datetime as _dt
import unittest
from unittest import mock

from tests import test_dashboard as td

from chitragupta import common


class PreviewActions(td.Base):
    def test_day_view_uses_the_sender_builder(self):
        from chitragupta import jiraworklog
        from chitragupta.dashboard import api
        fake = {"kind": "message", "text": "ABC-1001 10:00 1h x", "entries": [], "facts_as_of": "Jira data as of 19:40",
                "pending_condense": True, "blocks": []}
        with mock.patch.object(jiraworklog, "preview", return_value=dict(fake)) as pv:
            d = api.day(self.days["day"])
        pv.assert_called_once_with(self.days["day"], with_blocks=True)
        self.assertEqual(d["worklog_preview"]["facts_as_of"], "Jira data as of 19:40")
        self.assertNotIn("blocks", d["worklog_preview"])
        self.assertTrue(d["preview_buttons"])

    def test_refresh_and_exact(self):
        from chitragupta import jiraworklog, settings
        from chitragupta.dashboard import actions
        day = self.days["day"]
        with mock.patch.object(jiraworklog, "refresh_facts", return_value={"ok": True, "preview": {"kind": "message"}}) as rf:
            self.assertEqual(actions.worklog_refresh({"day": day}), (200, {"ok": True, "preview": {"kind": "message"}}))
        rf.assert_called_once_with(day, wait_s=3)
        with mock.patch.object(jiraworklog, "preview_exact", return_value={"ok": False, "skipped": "budget", "preview": {}}):
            st, body = actions.worklog_exact({"day": day})
        self.assertEqual((st, body["ok"], body["skipped"]), (200, False, "budget"))
        self.assertEqual(actions.worklog_refresh({"day": "16-10-2026"})[0], 400)
        settings.apply({"dashboard.preview_buttons": False})
        with mock.patch.object(jiraworklog, "refresh_facts") as rf:
            self.assertEqual(actions.worklog_refresh({"day": day})[0], 403)
            self.assertEqual(actions.worklog_exact({"day": day})[0], 403)
        rf.assert_not_called()

    def test_send_again(self):
        from chitragupta.dashboard import actions
        day = self.days["day"]
        self.assertEqual(actions.worklog_resend({"day": day})[0], 400)
        future = (_dt.date.fromisoformat(common.workday(common.now(), common.load_config())) + _dt.timedelta(days=2)).isoformat()
        self.assertEqual(actions.worklog_resend({"day": future, "request_id": "r0"})[0], 400)
        self.assertEqual(actions.worklog_resend({"day": day, "request_id": "r1"}), (200, {"ok": True, "duplicate": False, "day": day}))
        self.assertTrue(actions.worklog_resend({"day": day, "request_id": "r1"})[1]["duplicate"])  # a double click
        self.assertFalse(actions.worklog_resend({"day": day, "request_id": "r2"})[1]["duplicate"])
        lines = [l for l in common.read_text(common.hp("inbox.md")).splitlines() if l.strip() == "resend: " + day]
        self.assertEqual(len(lines), 2)


class CalendarMark(td.Base):
    def test_lines(self):
        from chitragupta.dashboard import actions
        day = self.days["day"]
        for body, line in (({"day": day, "kind": "leave"}, "leave: " + day),
                           ({"day": day, "kind": "holiday", "name": "  Founders\n day  "}, "holiday: %s Founders day" % day),
                           ({"day": day, "kind": "clear", "name": "ignored"}, "clear: " + day),
                           ({"day": day, "kind": "leave"}, "leave: " + day)):
            self.assertEqual(actions.calendar_mark(body), (200, {"ok": True, "line": line}))
        lines = common.read_text(common.hp("inbox.md")).splitlines()
        self.assertEqual(lines[-4:], ["leave: " + day, "holiday: %s Founders day" % day, "clear: " + day, "leave: " + day])
        for bad in ({"day": day, "kind": "time"}, {"day": day, "kind": "resend"}, {"day": "2026-13-01", "kind": "leave"},
                    {"day": day + "..2026-12-31", "kind": "leave"}, {"kind": "leave"}):
            self.assertEqual(actions.calendar_mark(bad)[0], 400, bad)


class SearchRebuild(td.Base):
    def test_rebuild(self):
        import os
        from chitragupta.dashboard import actions
        st, body = actions.search_rebuild({})
        self.assertEqual(st, 200)
        self.assertGreater(body["stats"]["docs"], 0)
        self.assertTrue(os.path.isfile(common.wl("cache", "search.sqlite")))


if __name__ == "__main__":
    unittest.main()
