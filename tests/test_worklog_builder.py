"""One builder for the 20:00 message and its previews; repeatable `resend:` (stdlib unittest, synthetic data).

The golden checks: from the same inputs (cached Jira facts and condensed lines) the preview is the text the sender
sends, byte for byte. Slack, Jira and the model are stubs; MCP servers and Slack are tripwired (tests.support).
"""
import io
import json
import os
import re
import unittest
from contextlib import redirect_stdout

from tests.support import clear_caches, rd, wr
from tests.test_upgrade02 import AgentCase

D = "2026-10-16"
FACTS = {"comments": [{"ticket": "ABC-1", "time": "2026-10-16T16:16:55+05:30", "text": "RCA Summary: the cause and the fix."}],
         "status_changes": [], "worklogs": [{"ticket": "ABC-1", "minutes": 60}]}
LONG = ["Reproduced the upload problem on QA with a fresh order and noted every step that fails along the way.",
        "Tested the fix for the upload problem on QA again and confirmed the file now shows up in the portal."]


class BuilderCase(AgentCase):
    def setUp(self):
        super().setUp()
        from chitragupta import common, extract, jiraworklog, mcpjobs
        self._real = (extract.call_claude, mcpjobs.jira_evening, jiraworklog.delivery_block)
        self.jira, self.stdins = [], []

        def fake_call(kind, stdin_text, cfg, timeout=300):
            self.calls.append(kind)
            self.stdins.append(stdin_text)
            ids = [int(m) for m in re.findall(r"(?m)^(\d+)\. ", stdin_text)]
            return {"descriptions": [{"i": i, "text": "Condensed entry %d on QA." % i} for i in ids]}, 0.01

        def fake_evening(day, tickets, cfg, health):
            self.jira.append((day, tuple(tickets)))
            common.write_json(common.hp("data", "jira", "_evening", day + ".json"),
                              {"day": day, "fetched_at": common.now_iso(), "facts": FACTS})
            return FACTS
        extract.call_claude = fake_call
        mcpjobs.jira_evening = fake_evening
        jiraworklog.delivery_block = lambda store, tickets: "Delivery check\n• ABC-1: not in qa yet."
        cfg = common.read_json(common.wl("config.json"))
        cfg["mcp"]["jira"]["enabled"] = True
        common.write_json(common.wl("config.json"), cfg)
        clear_caches()

    def tearDown(self):
        from chitragupta import extract, jiraworklog, mcpjobs
        extract.call_claude, mcpjobs.jira_evening, jiraworklog.delivery_block = self._real
        super().tearDown()

    def day_with_lines(self):
        self.raw(D, "ABC-1", 11, 0, n=20)
        self.raw(D, "ABC-2", 15, 0, n=20, sid="s2")
        self.clock(D, 19, 30)
        a = self.agent()
        evs = a.day_events(D)
        a.jwl.merge_extraction(D, {"worklog_lines": [{"ticket": "ABC-1", "line": "Found the cause and tested the fix on QA."}]},
                               [dict(e, _t="ABC-1") for e in evs if e.get("session_id") == "s1"])
        a.jwl.merge_extraction(D, {"worklog_lines": [{"ticket": "ABC-2", "line": LONG[0]}]},
                               [dict(e, _t="ABC-2") for e in evs if e.get("session_id") == "s2"][:5])
        a.jwl.merge_extraction(D, {"worklog_lines": [{"ticket": "ABC-2", "line": LONG[1]}]},
                               [dict(e, _t="ABC-2") for e in evs if e.get("session_id") == "s2"][-5:])
        a.store.save()
        from chitragupta import agent
        agent.save_state(a)


class PreviewIsTheMessage(BuilderCase):
    def test_preview_equals_sent_text_after_refresh_and_exact(self):
        from chitragupta import jiraworklog
        self.day_with_lines()
        r = jiraworklog.refresh_facts(D)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(self.jira), 1)
        pv = r["preview"]
        self.assertEqual(pv["facts_as_of"], "Jira data as of 19:30")
        self.assertTrue(pv["pending_condense"])
        self.assertTrue(any(e["pending_condense"] for e in pv["entries"]))
        self.assertEqual(self.calls, [])  # a preview never calls the model
        self.assertIn("Already in Jira: ABC-1 1h.", pv["text"])
        ex = jiraworklog.preview_exact(D)
        self.assertEqual((ex["ok"], ex["model_calls"]), (True, 1))
        self.assertFalse(ex["preview"]["pending_condense"])
        self.clock(D, 20, 5)
        shown = jiraworklog.preview(D)["text"]
        self.assertEqual(len(self.calls), 1)
        a = self.agent()
        sh = a.jwl.run_day(D, "first")
        self.assertEqual(sh["status"], "sent")
        self.assertEqual(self.sent[-1], shown)  # byte for byte, delivery block included
        self.assertEqual(sh["versions"][-1]["text"], shown)
        self.assertEqual(len(self.calls), 1)  # the sender used the condensed lines the exact preview cached
        self.assertEqual(len(self.jira), 2)  # the sender still reads Jira live
        self.assertTrue(shown.endswith("Delivery check\n• ABC-1: not in qa yet."))
        self.assertIn("Condensed entry", shown)

    def test_sender_fills_the_cache_and_the_preview_follows(self):
        from chitragupta import jiraworklog
        self.day_with_lines()
        self.clock(D, 20, 5)
        a = self.agent()
        a.jwl.run_day(D, "first")
        self.assertEqual(len(self.calls), 1)
        pv = jiraworklog.preview(D)
        self.assertEqual(pv["text"], self.sent[-1])
        self.assertFalse(pv["pending_condense"])
        self.assertEqual(pv["facts_as_of"], "Jira data as of 20:05")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(os.path.isfile(jiraworklog.condense_path(D)))

    def test_build_modes_and_headers(self):
        self.day_with_lines()
        a = self.agent()
        nofacts = a.jwl.build(D, facts=None, condense=False)
        self.assertNotIn("Already in Jira", nofacts["text"])
        self.assertIsNone(nofacts["facts_at"])
        self.assertEqual(self.calls, [])
        cached = a.jwl.build(D, facts="cached", condense="cache")  # no cache yet: like no Jira data
        self.assertFalse(cached["facts"])
        corr = a.jwl.build(D, facts=None, condense="cache", correction=True)
        self.assertTrue(corr["text"].startswith("Corrected update for 16 Oct"))
        self.assertTrue(corr["pending_condense"])
        self.assertEqual(self.jira, [])
        self.clock("2026-10-17", 20, 5)  # a past day with nothing tracked: no late nudge, in the preview as at send
        self.assertEqual(self.agent().jwl.build("2026-10-15", facts=None, condense="cache")["kind"], "none")

    def test_refresh_respects_budget_and_switch(self):
        from chitragupta import agent, common, jiraworklog, kbjobs
        self.day_with_lines()
        a = self.agent()
        a.health.setdefault("mcp", {})["calls"] = {D: 10 - kbjobs.EVENING_RESERVE}
        agent.save_state(a)
        r = jiraworklog.refresh_facts(D)
        self.assertFalse(r["ok"])
        self.assertIn("budget", r["skipped"])
        self.assertIn("text", r["preview"])
        r = jiraworklog.preview_exact(D)
        self.assertIn("budget", r["skipped"])
        self.assertEqual((self.jira, self.calls), ([], []))
        cfg = common.read_json(common.wl("config.json"))
        cfg["mcp"]["jira"]["enabled"] = False
        common.write_json(common.wl("config.json"), cfg)
        clear_caches()
        self.assertIn("Jira is off", jiraworklog.refresh_facts(D)["skipped"])
        self.assertFalse(os.path.exists(common.wl("locks", "agent.lock")))

    def test_worklog_show_is_read_only(self):
        from chitragupta import agent
        self.day_with_lines()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(agent.main(["worklog-show", D]), 0)
        self.assertEqual((self.calls, self.jira), ([], []))  # the old bug: a condense call labelled read-only
        self.assertIn("condensed at 20:00", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(agent.main(["worklog-show", D, "--exact"]), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("condensed at 20:00", out.getvalue())


# --------------------------------------------------------------------------- item 10: repeatable resend

class InboxCounting(BuilderCase):
    def inbox(self, text, mode="a"):
        from chitragupta import common
        with open(common.hp("inbox.md"), mode, encoding="utf-8") as f:
            f.write(text)

    def test_action_lines_count_data_lines_dedupe(self):
        from chitragupta import sources
        st = {}
        self.inbox("leave: 2026-10-20\nleave: 2026-10-20\nresend: 2026-10-16\nresend: 2026-10-16\nABC-1: a note\n", "w")
        got = [n["text"] for n in sources.scan_inbox({}, st)]
        self.assertEqual(got, ["leave: 2026-10-20", "resend: 2026-10-16", "resend: 2026-10-16", "a note"])
        self.assertEqual(sources.scan_inbox({}, st), [])
        self.inbox("leave: 2026-10-20\nresend: 2026-10-16\nABC-1: a note\n")
        self.assertEqual([n["text"] for n in sources.scan_inbox({}, st)], ["resend: 2026-10-16"])
        self.assertTrue(all(isinstance(v, int) for v in st["inbox_hashes"].values()))
        self.inbox("", "w")  # a file caught mid-save: counts never go down, so nothing replays afterwards
        self.assertEqual(sources.scan_inbox({}, st), [])
        self.inbox("resend: 2026-10-16\nresend: 2026-10-16\n")
        self.assertEqual(sources.scan_inbox({}, st), [])

    def test_legacy_list_state_replays_nothing(self):
        import hashlib
        from chitragupta import sources
        self.inbox("resend: 2026-10-16\nresend: 2026-10-16\n", "w")
        st = {"inbox_hashes": [hashlib.sha1(b"resend: 2026-10-16").hexdigest()[:16]]}
        self.assertEqual(sources.scan_inbox({}, st), [])
        self.inbox("resend: 2026-10-16\n")
        self.assertEqual(len(sources.scan_inbox({}, st)), 1)

    def test_migration_backs_up_and_converts(self):
        import hashlib
        from chitragupta import common, migrate
        h = hashlib.sha1(b"resend: 2026-10-16").hexdigest()[:16]
        self.inbox("resend: 2026-10-16\nresend: 2026-10-16\n", "w")
        cfg = common.read_json(common.wl("config.json"))
        cfg["chitragupta_migrations"] = ["database-mcp-rename", "branch-tracking"]
        common.write_json(common.wl("config.json"), cfg)
        common.write_json(common.wl("state.json"), {"inbox_hashes": [h, "0123456789abcdef"], "other": 1})
        res = migrate.run(wait_s=0)
        self.assertEqual(res["applied"], ["inbox-counts"])
        self.assertTrue(res["state_changed"])
        self.assertEqual(json.loads(rd(os.path.join(res["backup"], "state.json")))["inbox_hashes"],
                         [h, "0123456789abcdef"])
        st = common.read_json(common.wl("state.json"))
        self.assertEqual(st["inbox_hashes"], {h: 2, "0123456789abcdef": 1})
        self.assertEqual(st["other"], 1)
        self.assertTrue(migrate.run(wait_s=0)["already"])

    def test_request_id_window(self):
        from chitragupta import common, jiraworklog
        self.clock(D, 20, 30)
        r1 = jiraworklog.request_resend(D, "req-1")
        r2 = jiraworklog.request_resend(D, "req-1")  # a double click
        self.assertEqual((r1["duplicate"], r2["duplicate"]), (False, True))
        jiraworklog.request_resend(D, "req-2")
        self.clock(D, 20, 32)  # 2 minutes later the same id is a new request
        self.assertFalse(jiraworklog.request_resend(D, "req-1")["duplicate"])
        self.assertEqual(rd(common.hp("inbox.md")), "resend: %s\n" % D * 3)
        with self.assertRaises(ValueError):
            jiraworklog.request_resend("2026-02-30", "x")
        with self.assertRaises(ValueError):
            jiraworklog.request_resend(D, "")

    def test_cli_repeats_resend_only(self):
        from chitragupta import cli, common
        for argv in (["resend:", D], ["resend:", D], ["leave:", "2026-10-20"], ["leave:", "2026-10-20"]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["log"] + argv), 0)
        self.assertEqual(rd(common.hp("inbox.md")),
                         "resend: %s\nresend: %s\nleave: 2026-10-20\n" % (D, D))

    def test_every_send_is_in_the_history(self):
        from chitragupta import jiraworklog, sources
        self.raw(D, "ABC-1", 11, 0, n=20)
        self.clock(D, 20, 5)
        a = self.agent()
        a.jwl.tick()
        st = {}
        for k, hhmm in enumerate(((20, 40), (21, 10))):
            self.inbox("resend: %s\n" % D)
            for n in sources.scan_inbox({}, st):
                self.assertTrue(a.jwl.handle_inbox(n))
            self.clock(D, *hhmm)
            a.jwl.tick()
        self.assertEqual(len(self.sent), 3)  # the same line twice: sent twice
        hist = jiraworklog.send_history(D)
        self.assertEqual([h["why"] for h in hist], ["first", "resend", "resend"])
        self.assertTrue(all(h["sent"] and h["slack_ts"] for h in hist))
        self.assertEqual([h["v"] for h in hist], [1, 2, 3])
        self.assertEqual(hist[2]["at"][11:16], "21:10")
        wr(jiraworklog.sheet_path("2026-10-15"), json.dumps({"versions": [{"v": 1, "at": "x", "kind": "message", "why": "first",
                                                                           "text": "t", "slack": {"ok": False, "state": "off"}}]}))
        self.assertEqual(jiraworklog.send_history("2026-10-15")[0]["slack_state"], "off")
        self.assertEqual(jiraworklog.send_history("2001-01-01"), [])


if __name__ == "__main__":
    unittest.main()
