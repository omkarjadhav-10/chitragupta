"""Jira worklog allocator / renderer / calendar / schedule, description guard, read-only MCP guard, storage allowlist,
public redaction, QA-literal capture (stdlib unittest, deterministic, no model calls).

The goldens keep the legacy layout and numbers byte for byte: keys map to ABC-1001..1004 (same length, same sort order),
descriptions are neutral. Slack is patched; MCP servers and Slack are tripwired (tests.support).
"""
import datetime as dt
import glob
import json
import os
import random
import subprocess
import tempfile
import unittest

from tests.support import PY, RUN_PY, Isolated, install_launcher, rd, rmtree, run_hook, write_config, wr
from chitragupta import timesheet as T

GOLDEN_A = """Today's update (16 Oct):

```
┌───────┬──────────┬────────┐
│ Start │  Ticket  │  Time  │
├───────┼──────────┼────────┤
│ 10:30 │ ABC-1004 │ 2h     │
├───────┼──────────┼────────┤
│ 12:30 │ ABC-1001 │ 1h 30m │
├───────┼──────────┼────────┤
│ 15:00 │ ABC-1003 │ 3h 30m │
├───────┼──────────┼────────┤
│ 18:30 │ ABC-1001 │ 1h     │
└───────┴──────────┴────────┘
```

ABC-1004 · 10:30 · 2h
Retested the document upload fix on QA and checked the file shows up in the records system. Wrote the RCA comment.

ABC-1001 · 12:30 · 1h 30m
Found why document download fails, reproduced it on QA, and saved the steps to test it.

ABC-1003 · 15:00 · 3h 30m
Checked the fix for all portal access on/off cases, ran the tests, replied to the review comment and wrote the RCA.

ABC-1001 · 18:30 · 1h
Connected with Alex and investigated the databases."""

GOLDEN_B = """Yesterday's update (15 Oct):

```
┌───────┬──────────┬────────┐
│ Start │  Ticket  │  Time  │
├───────┼──────────┼────────┤
│ 10:30 │ ABC-1004 │ 2h 30m │
├───────┼──────────┼────────┤
│ 14:00 │ ABC-1003 │ 5h 30m │
└───────┴──────────┴────────┘
```

ABC-1004 · 10:30 · 2h 30m
Cleaned up the upload fix code, tested document upload on QA and checked it shows in the records system. Set up a new test order.

ABC-1003 · 14:00 · 5h 30m
Found the root cause of the wrong login message, fixed it so disabled users are set to inactive, added tests and checked the partner flow."""

GOLDEN_C = """Update for 14 Oct, the day before yesterday:

```
┌───────┬──────────┬────────┐
│ Start │  Ticket  │  Time  │
├───────┼──────────┼────────┤
│ 10:30 │ ABC-1002 │ 1h 30m │
├───────┼──────────┼────────┤
│ 12:00 │ ABC-1004 │ 6h 30m │
└───────┴──────────┴────────┘
```

ABC-1002 · 10:30 · 1h 30m
Looked into why documents uploaded in the portal don't show in the records system for orders that are not confirmed yet, and found the cause.

ABC-1004 · 12:00 · 6h 30m
Reproduced the upload bug on QA, built the fix so portal uploads are saved for the records system, and tested it end to end on QA.

Only about 5h 30m was recorded that day: 4h 26m on ABC-1004 and 1h 06m on ABC-1002. I stretched these to make 8h. The worklog doesn't say which ticket came first, so swap the start times if you did ABC-1004 earlier."""


def at(day, hh, mm):
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm)).astimezone()


def blk(t, start, minutes, lines=()):
    return {"ticket": t, "start": start, "end": None, "minutes": minutes, "lines": list(lines), "spans": []}


def mock_desc(table):
    def f(entries):
        seen = {}
        for e in entries:
            k = seen.get(e["ticket"], 0)
            seen[e["ticket"]] = k + 1
            e["desc"] = table.get((e["ticket"], k), table.get(e["ticket"], ""))
    return f


TS = T.settings({})


class GoldenTests(unittest.TestCase):
    def test_a(self):
        d = "2026-10-16"
        r = T.compose(d, d, [blk("ABC-1004", at(d, 10, 30), 95), blk("ABC-1001", at(d, 12, 30), 70),
                             blk("ABC-1003", at(d, 15, 0), 160)],
                      [{"ticket": "ABC-1001", "start": "18:30", "minutes": 60,
                        "text": "Connected with Alex and investigated the databases."}], {}, TS,
                      describe=mock_desc({
                          "ABC-1004": "Retested the document upload fix on QA and checked the file shows up in the records system. "
                                      "Wrote the RCA comment.",
                          "ABC-1001": "Found why document download fails, reproduced it on QA, and saved the steps to test it.",
                          "ABC-1003": "Checked the fix for all portal access on/off cases, ran the tests, replied to the review "
                                      "comment and wrote the RCA."}))
        self.assertEqual(r["text"], GOLDEN_A)

    def test_b(self):
        d = "2026-10-15"
        r = T.compose(d, "2026-10-16", [blk("ABC-1004", at(d, 10, 0), 130), blk("ABC-1003", at(d, 13, 0), 290)], [], {}, TS,
                      describe=mock_desc({
                          "ABC-1004": "Cleaned up the upload fix code, tested document upload on QA and checked it shows in the "
                                      "records system. Set up a new test order.",
                          "ABC-1003": "Found the root cause of the wrong login message, fixed it so disabled users are set to "
                                      "inactive, added tests and checked the partner flow."}))
        self.assertEqual(r["text"], GOLDEN_B)

    def test_c(self):
        d = "2026-10-14"
        r = T.compose(d, "2026-10-16", [blk("ABC-1004", None, 266), blk("ABC-1002", None, 66)], [], {}, TS,
                      describe=mock_desc({
                          "ABC-1002": "Looked into why documents uploaded in the portal don't show in the records system for "
                                      "orders that are not confirmed yet, and found the cause.",
                          "ABC-1004": "Reproduced the upload bug on QA, built the fix so portal uploads are saved for the "
                                      "records system, and tested it end to end on QA."}))
        self.assertEqual(r["text"], GOLDEN_C)

    def test_headers(self):
        self.assertEqual(T.header("2026-10-13", "2026-10-17"), "Update for 13 Oct:")
        self.assertEqual(T.header("2026-10-16", "2026-10-16", correction=True), "Corrected update for 16 Oct:")
        self.assertEqual(T.header("2026-10-17", "2026-10-17", "half day"), "Today's update (17 Oct, half day):")
        self.assertEqual(T.header("2026-10-13", "2026-10-17", "weekend"), "Update for 13 Oct (weekend):")

    def test_already_logged_note(self):
        d = "2026-10-16"
        r = T.compose(d, d, [blk("ABC-1004", at(d, 10, 0), 200), blk("ABC-1003", at(d, 13, 0), 200)], [], {}, TS,
                      already=[("ABC-1004", 120)])
        self.assertTrue(r["note"].startswith("Already in Jira: ABC-1004 2h."))
        self.assertEqual(sum(e["minutes"] for e in r["entries"]), 360)


class AllocatorProperties(unittest.TestCase):
    def test_random(self):
        rnd = random.Random(7)
        d = "2026-10-16"
        for _ in range(400):
            n = rnd.randint(1, 12)
            tickets = ["ABC-%d" % rnd.randint(1, 5) for _ in range(n)]
            known = rnd.random() < 0.8
            t0 = at(d, 8, 0)
            bl = []
            for t in tickets:
                t0 += dt.timedelta(minutes=rnd.randint(5, 60))
                bl.append(blk(t, t0 if known else None, rnd.randint(1, 120), ["did x"] if rnd.random() < 0.5 else []))
            manual = []
            if rnd.random() < 0.3:
                manual.append({"ticket": "ABC-9", "start": rnd.choice([None, "17:00"]), "minutes": rnd.choice([30, 60, 90]),
                               "text": "Meeting."})
            r = T.compose(d, d, bl, [dict(m) for m in manual], {}, TS)
            es = r["entries"]
            self.assertEqual(sum(e["minutes"] for e in es), 480)
            for e in es:
                self.assertEqual(e["minutes"] % 30 if not e.get("manual") else 0, 0)
            starts = [T.mins(e["start_hhmm"]) for e in es]
            ends = [s + e["minutes"] for s, e in zip(starts, es)]
            for k in range(1, len(es)):
                self.assertGreaterEqual(starts[k], ends[k - 1], "overlap")
                gap = starts[k] - ends[k - 1]
                self.assertIn(gap, (0, 60))
            gaps = [starts[k] - ends[k - 1] for k in range(1, len(es))]
            total_span = ends[-1] - starts[0]
            self.assertIn(total_span, (480, 540))  # one lunch, on a boundary or inside an entry
            self.assertLessEqual(gaps.count(60), 1)
            for m in manual:
                hit = [e for e in es if e.get("manual")]
                self.assertEqual(hit[0]["minutes"], m["minutes"])
                self.assertEqual(hit[0]["desc"], "Meeting.")
            if len(es) > 6:
                per = {}
                for e in es:
                    if not e.get("manual"):
                        per[e["ticket"]] = per.get(e["ticket"], 0) + 1
                self.assertTrue(all(v == 1 for v in per.values()), "merge allowed but not done")
            if not known and len([e for e in es if not e.get("manual")]) >= 2:
                unk = [e["ticket"] for e in es if not e.get("known", True) and not e.get("manual")]
                self.assertEqual(unk, sorted(unk))
                self.assertIn("The worklog doesn't say", r["note"])

    def test_lunch_inside_long_entry(self):
        d = "2026-10-14"
        es = T.compose(d, d, [blk("ABC-1", at(d, 9, 0), 60), blk("ABC-2", at(d, 10, 0), 420)], [], {}, TS)["entries"]
        self.assertEqual([e["start_hhmm"] for e in es], ["10:30", "11:30"])  # 11:30 + 7h + 1h lunch inside = 19:30


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.ts = T.settings({"jira_worklog": {"leave_ticket": None}})

    def test_kinds(self):
        d = "2026-10-16"  # Friday
        bl = [blk("ABC-1", at(d, 10, 0), 100)]
        self.assertEqual(sum(e["minutes"] for e in T.compose(d, d, bl, [], {"half_day": {d: True}}, self.ts)["entries"]), 240)
        self.assertIn("half day", T.compose(d, d, bl, [], {"half_day": {d: True}}, self.ts)["text"])
        sat = "2026-10-17"
        r = T.compose(sat, sat, [blk("ABC-1", at(sat, 11, 0), 100)], [], {}, self.ts)
        self.assertEqual(sum(e["minutes"] for e in r["entries"]), 90)  # actual time rounded, no padding
        self.assertIn("(17 Oct, weekend)", r["text"])
        self.assertEqual(T.compose(sat, sat, [], [], {}, self.ts)["kind"], "none")
        self.assertEqual(T.compose(d, d, [], [], {}, self.ts)["kind"], "nudge")
        self.assertEqual(T.compose(d, d, [], [], {"leave": {d: True}}, self.ts)["kind"], "none")
        lt = T.settings({"jira_worklog": {"leave_ticket": "HR-1"}})
        r = T.compose(d, d, [], [], {"leave": {d: True}}, lt)
        self.assertEqual([(e["ticket"], e["minutes"], e["desc"]) for e in r["entries"]], [("HR-1", 480, "Leave")])
        self.assertEqual(T.compose(sat, sat, [blk("ABC-1", at(sat, 11, 0), 100)], [], {"workday": {sat: True}},
                                   self.ts)["target"], 480)
        self.assertIn("holiday", T.compose(d, d, bl, [], {"holiday": {d: "Founders Day"}}, self.ts)["text"])

    def test_inbox_grammar(self):
        p = T.parse_directive
        self.assertEqual(p("time: 2026-10-16 18:30 1h ABC-1001 Connected with Alex.", "2026-10-17"),
                         {"kind": "time", "days": ["2026-10-16"], "start": "18:30", "minutes": 60, "ticket": "ABC-1001",
                          "text": "Connected with Alex."})
        self.assertEqual(p("time: 1h 30m ABC-1 x", "2026-10-17")["minutes"], 90)
        self.assertEqual(p("time: 45m ABC-1 x", "2026-10-17")["days"], ["2026-10-17"])
        self.assertEqual(p("time: 9:05 2h ABC-1 x", "2026-10-17")["start"], "09:05")
        self.assertEqual(p("leave: 2026-10-20..2026-10-22", "x")["days"], ["2026-10-20", "2026-10-21", "2026-10-22"])
        self.assertEqual(p("half-day: 2026-10-24", "x")["kind"], "half-day")
        self.assertEqual(p("holiday: 2026-11-09 Founders Day", "x")["name"], "Founders Day")
        self.assertEqual(p("workday: 2026-10-25", "x")["kind"], "workday")
        self.assertEqual(p("resend: 2026-10-16", "x")["kind"], "resend")
        self.assertEqual(p("leave: monday", "x")["kind"], "invalid")
        self.assertIsNone(p("ABC-1: a note", "x"))
        cal = {}
        T.apply_directive(cal, p("half-day: 2026-10-24", "x"))
        self.assertEqual(T.day_kind("2026-10-24", cal, TS), "half_day")


# --------------------------------------------------------------------------- agent-level (temp home)

DB_SERVER = "sqldb"  # the database MCP's server name in these tests (configured, as in a real install)


def make_home():
    h = tempfile.mkdtemp(prefix="cg-up02-")
    write_config(h, {"ticket_prefixes": ["ABC"], "day_starts_at": "07:00", "github_remote": "",
                     "capture_scope": {"include_paths": [], "exclude_paths": []}, "paths": {"python": PY},
                     "git_author_emails": [], "watched_repos": [], "bruno_collections": [],
                     "artifacts": {"watch_dirs": [], "push_binary_types": []},
                     "mcp": {"daily_call_budget": 10, "jira": {"enabled": False},
                             "database": {"enabled": False, "server": DB_SERVER}},
                     "jira_worklog": {"slack": {"self_user_id": "U000TEST", "send_tool": "mcp__slack__slack_send_message",
                                                "mode": "strict"}}})
    return h


class AgentCase(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate(make_home())
        from chitragupta import common, extract, jiraworklog, slack
        self._call, self._send, self._now = extract.call_claude, slack.send, common.now
        self.calls, self.sent = [], []

        def fake_call(kind, stdin_text, cfg, timeout=300):
            self.calls.append(kind)
            return {"descriptions": []}, 0.0
        extract.call_claude = fake_call

        def fake_send(cfg, text, health):
            self.sent.append(text)
            if common.env("SLACK_FAIL"):
                return {"ok": False, "error": "forced"}
            return {"ok": True, "ts": "1760000000.000100", "tokens": {"in": 1200, "out": 20}}
        slack.send = fake_send
        jiraworklog.slack.send = fake_send

    def tearDown(self):
        from chitragupta import common, extract, jiraworklog, slack
        extract.call_claude, slack.send, common.now = self._call, self._send, self._now
        jiraworklog.slack.send = self._send
        self.restore()

    def clock(self, day, hh, mm):
        from chitragupta import common
        t = at(day, hh, mm)
        common.now = lambda: t
        return t

    def raw(self, day, ticket, hh, mm, n=10, step=5, sid="s1"):
        from chitragupta import common
        p = common.wl("raw", day, sid + ".jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            t = at(day, hh, mm)
            for i in range(n):
                f.write(json.dumps({"ts": (t + dt.timedelta(minutes=i * step)).isoformat(), "event": "post-tool",
                                    "session_id": sid, "branch": "feature/%s-x" % ticket, "payload": {}}) + "\n")

    def agent(self):
        from chitragupta import agent
        a = agent.Agent()
        a.health.setdefault("jira_worklog", {})["active_from"] = "2026-10-14"
        return a


class ScheduleTests(AgentCase):
    def test_send_once_late_and_no_double(self):
        from chitragupta import jiraworklog
        d = "2026-10-16"
        self.raw(d, "ABC-1", 11, 0, n=20)
        self.raw(d, "ABC-2", 15, 0, n=20, sid="s2")
        self.clock(d, 19, 0)
        a = self.agent()
        a.jwl.tick()
        self.assertEqual(self.sent, [])  # before 20:00
        self.clock("2026-10-17", 9, 30)  # laptop was off at 20:00: next morning it goes out, a day late
        a = self.agent()
        a.jwl.tick()
        a.jwl.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith("Yesterday's update (16 Oct):"))
        self.assertEqual(self.calls, [])  # lines missing -> fallback lines, no condense call needed for single notes
        sh = jiraworklog.load_sheet(d)
        self.assertEqual(sh["status"], "sent")
        self.assertEqual(sh["slack_ts"], "1760000000.000100")

    def test_skip_day_no_model_call(self):
        self.clock("2026-10-18", 21, 0)  # Sunday, nothing tracked
        a = self.agent()
        a.jwl.tick()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.calls, [])

    def test_correction_exactly_once(self):
        d = "2026-10-16"
        self.raw(d, "ABC-1", 11, 0, n=20)
        self.clock(d, 20, 5)
        a = self.agent()
        a.jwl.tick()
        self.assertEqual(len(self.sent), 1)
        a.jwl.handle_inbox({"text": "time: 2026-10-16 18:30 1h ABC-2 Met the team."})
        a.jwl.handle_inbox({"text": "half-day: 2026-10-16"})
        self.clock(d, 20, 10)
        a.jwl.tick()
        self.assertEqual(len(self.sent), 1)  # debounced
        self.clock(d, 20, 30)
        a.jwl.tick()
        a.jwl.tick()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[1].startswith("Corrected update for 16 Oct (half day):"))
        self.assertIn("Met the team.", self.sent[1])

    def test_failure_retries_then_warns_and_daily_holds_message(self):
        from chitragupta import agent, render
        d = "2026-10-16"
        self.raw(d, "ABC-1", 11, 0, n=20)
        os.environ["CHITRAGUPTA_SLACK_FAIL"] = "1"
        for k in range(6):
            self.clock(d, 20, 5 + k * 5)
            a = self.agent()
            a.jwl.tick()
            agent.save_state(a)
        self.assertEqual(len(self.sent), 4)  # first try + 3 retries
        self.assertIn("send_warning", a.health["jira_worklog"])
        txt = render.render_day(a.store, d, a.cfg, a.health)
        self.assertIn("## Jira worklog", txt)
        self.assertIn("Today's update (16 Oct):", txt)


class DescriptionTests(AgentCase):
    def test_guard(self):
        from chitragupta.jiraworklog import line_ok
        self.assertIsNone(line_ok("Fixed the login message and tested it on QA.", "ABC-1"))
        for bad in ("Changed src/main/App.java to fix it.", "Ran SELECT id FROM users on QA.", "Mailed jane@acme.test.",
                    "Checked customer 1000417 on QA.", "Updated CustomerService on QA.", "Set is_locked on QA.",
                    "Also fixed ABC-2 on QA."):
            self.assertIsNotNone(line_ok(bad, "ABC-1"), bad)

    def test_lines_used_without_model_call(self):
        d = "2026-10-16"
        self.raw(d, "ABC-1", 11, 0, n=20)
        self.clock(d, 20, 5)
        a = self.agent()
        a.jwl.merge_extraction(d, {"worklog_lines": [{"ticket": "ABC-1", "line": "Found the cause and tested the fix on QA."},
                                                     {"ticket": "ABC-1", "line": "Bad line with C:/x/y.java"}]},
                               [dict(e, _t="ABC-1") for e in a.day_events(d)])
        a.jwl.tick()
        self.assertEqual(self.calls, [])
        self.assertIn("Found the cause and tested the fix on QA.", self.sent[0])
        dd = a.store.day(d)
        self.assertEqual(dd["worklog_lines"]["ABC-1"][-1]["line"], "Found the cause and tested the fix on QA.")

    def test_public_pass_before_send(self):
        d = "2026-10-16"
        self.raw(d, "ABC-1", 11, 0, n=20)
        self.clock(d, 20, 5)
        a = self.agent()
        a.jwl.handle_inbox({"text": "time: 2026-10-16 1h ABC-1 Called jane@acme.test about it."})
        a.jwl.tick()
        self.assertNotIn("jane@acme.test", self.sent[0])


class JiraFactsTests(AgentCase):
    def test_facts_shape_descriptions_and_subtract(self):
        from chitragupta import extract, mcpjobs
        d = "2026-10-16"
        cfg = {"git_author_emails": ["me@acme.test"], "day_starts_at": "07:00"}
        me = {"email": "me@acme.test"}
        search = {"issues": [{"id": "1", "key": "ABC-1", "comments": [
            {"body": "RCA Summary: the lock was set because ...", "author": me, "created": "2026-10-16T12:16:55.000+0530"},
            {"body": "old", "author": me, "created": "2026-10-10T12:00:00.000+0530"},
            {"body": "theirs", "author": {"email": "x@acme.test"}, "created": "2026-10-16T13:00:00.000+0530"}],
            "worklog": {"worklogs": [{"author": {"emailAddress": "me@acme.test"}, "started": "2026-10-16T11:00:00.000+0530",
                                      "timeSpentSeconds": 3600}]}}]}
        changes = [{"issue_id": "1", "changelogs": [{"author": me, "created": "2026-10-16T18:49:03+05:30",
                                                     "items": [{"field": "status", "from_string": "Dev", "to_string": "READY"}]}]}]
        f = mcpjobs.evening_facts(d, search, changes, cfg)
        self.assertEqual([c["ticket"] for c in f["comments"]], ["ABC-1"])
        self.assertEqual(f["worklogs"][0]["minutes"], 60)
        self.assertEqual(f["status_changes"][0]["to"], "READY")
        self.assertEqual(mcpjobs.unwrap('{"result": "{\\"a\\": 1}"}'), {"a": 1})
        # compose with those facts: subtracted once, comment reaches the description call
        self.raw(d, "ABC-1", 11, 0, n=20)
        self.clock(d, 20, 5)
        a = self.agent()
        a.cfg["mcp"]["jira"]["enabled"] = True
        seen = []
        orig = extract.call_claude

        def fake(kind, stdin_text, cfg, timeout=300):
            seen.append(stdin_text)
            return {"descriptions": [{"i": 0, "text": "Found the cause, tested the fix on QA and wrote the RCA comment."}]}, 0.0
        extract.call_claude = fake
        real = mcpjobs.jira_evening
        mcpjobs.jira_evening = lambda day, tickets, cfg, health: f
        try:
            res = a.jwl.compose(d, d)
        finally:
            mcpjobs.jira_evening = real
            extract.call_claude = orig
        self.assertEqual(res["text"].count("Already in Jira: ABC-1 1h."), 1)
        self.assertEqual(sum(e["minutes"] for e in res["entries"]), 420)
        self.assertIn("RCA Summary", seen[0])
        self.assertIn("wrote the RCA comment", res["text"])
        self.assertNotIn("the lock was set because", res["text"])


# --------------------------------------------------------------------------- read-only guard

class GuardTests(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        from chitragupta import mcpjobs
        self.allow = mcpjobs.allow_default({"mcp": {"database": {"server": DB_SERVER}}})
        self.allow["servers"][DB_SERVER]["connections"] = {"qa": "qa-main"}

    def tearDown(self):
        self.restore()

    def c(self, tool, inp=None):
        from chitragupta import mcpguard
        return mcpguard.check(tool, inp or {}, self.allow)

    def test_write_tools_denied(self):
        for t in ("mcp__mcp-atlassian__jira_add_comment", "mcp__mcp-atlassian__jira_add_worklog",
                  "mcp__mcp-atlassian__jira_transition_issue", "mcp__mcp-atlassian__confluence_update_page",
                  "mcp__sqldb__create_write_request", "mcp__sqldb__update_artifact", "mcp__mcp-atlassian__jira_new_thing",
                  "mcp__slack__slack_send_message", "Bash", "Write"):
            self.assertIsNotNone(self.c(t), t)
        self.assertIsNone(self.c("mcp__mcp-atlassian__jira_get_issue", {"issue_key": "ABC-1"}))
        self.assertIsNone(self.c("StructuredOutput"))

    def test_statements(self):
        q = lambda sql, conn="qa-main": self.c("mcp__sqldb__run_query", {"connection_id": conn, "sql": sql})  # noqa: E731
        for bad in ("UPDATE t SET a = 1", "DELETE FROM t", "SELECT * INTO bak FROM t", "EXEC dbo.usp_fix 1",
                    "SELECT TOP 5 * FROM t; DROP TABLE t", "SELECT TOP 5 * FROM t SELECT TOP 5 * FROM u",
                    "SELECT TOP 5 * FROM t WITH (UPDLOCK)", "SELECT * FROM t", "SELECT TOP 500 * FROM t",
                    "with x as (delete from t returning *) select * from x limit 1", "MERGE t USING u ON 1=1",
                    "TRUNCATE TABLE t", "select nextval('s')"):
            self.assertIsNotNone(q(bad), bad)
        for ok in ("SELECT TOP 20 id, is_locked FROM dbo.customers WHERE id = 5",
                   "SELECT column_name FROM INFORMATION_SCHEMA.COLUMNS WHERE table_name = 'customers'",
                   "select tgname from pg_catalog.pg_trigger", "SELECT name FROM sys.triggers",
                   "EXEC sp_helptext 'dbo.usp_lock'", "SELECT COUNT(*) FROM t WHERE x IN (1,2)",
                   "WITH a AS (SELECT id FROM t) SELECT TOP 5 * FROM a", "select id, last_update from t limit 5",
                   "SELECT 'delete me' AS x FROM t LIMIT 1"):
            self.assertIsNone(q(ok), ok)
        self.assertIsNotNone(q("SELECT TOP 5 id FROM t", "prod-main"))
        m = lambda inp: self.c("mcp__sqldb__run_query", dict(inp, connection_id="qa-main"))  # noqa: E731
        self.assertIsNone(m({"collection": "orders", "operation": "find", "filter": {}}))
        self.assertIsNotNone(m({"collection": "orders", "operation": "aggregate", "pipeline": [{"$out": "x"}]}))
        self.assertIsNotNone(m({"collection": "orders", "operation": "deleteMany", "filter": {}}))

    def test_hook_process_denies(self):
        """The guard as Claude Code runs it (launcher -> chitragupta.mcpguard); its log lands in the temp home."""
        ap = os.path.join(self.home, "allow.json")
        with open(ap, "w") as f:
            json.dump(self.allow, f)
        p = subprocess.run([PY, "-B", RUN_PY, "mcpguard"], capture_output=True, timeout=30,
                           input=json.dumps({"tool_name": "mcp__sqldb__run_query",
                                             "tool_input": {"connection_id": "qa-main", "sql": "UPDATE t SET a=1"}}).encode(),
                           env=self.child_env(CHITRAGUPTA_GUARD_ALLOW=ap))
        out = json.loads(p.stdout.decode())
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        log = os.path.join(self.home, ".worklog", "logs", "mcp-guard.log")
        self.assertIn("DENY mcp__sqldb__run_query", rd(log))

    def test_parse_stream_keeps_raw_results(self):
        from chitragupta import mcpjobs
        big = "x" * 200000
        lines = [json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "1", "name": "mcp__a__b", "input": {}}]}}),
                 json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "1",
                                                                      "content": [{"type": "text", "text": big}]}]}}),
                 json.dumps({"type": "result", "structured_output": {"ok": True}, "usage": {"input_tokens": 9, "output_tokens": 3},
                             "total_cost_usd": 0.001})]
        r = mcpjobs.parse_stream("\n".join(lines))
        self.assertEqual(len(r["calls"][0]["result"]), 200000)
        self.assertEqual(r["out"], {"ok": True})
        self.assertEqual(r["output_tokens"], 3)


# --------------------------------------------------------------------------- storage + redaction + QA capture

class StorageTests(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate(make_home())
        install_launcher(self.user_dir)  # the pre-commit hook runs the stable launcher

    def tearDown(self):
        self.restore()

    def git(self, *a):
        return subprocess.run(["git", "-C", self.home] + list(a), capture_output=True, text=True)

    def test_allowlist_and_precommit(self):
        from chitragupta import common, gitsync
        cfg = common.load_config()
        gitsync.ensure_repo(cfg)
        self.git("config", "user.email", "t@acme.test")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        for rel, txt in (("daily/2026/10/2026-10-16.md", "# day\nRan `SELECT id FROM customers WHERE id = 1000417` on QA.\n"),
                         ("tickets/ABC-1/README.md", "# ticket\n"), ("data/x.json", "{}"), ("bruno/ABC-1/a.bru", "x")):
            p = os.path.join(self.home, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            wr(p, txt)
        h = {}
        self.assertTrue(gitsync.commit(cfg, "t", h))
        files = self.git("ls-files").stdout.split()
        self.assertEqual(sorted(files), sorted([".gitattributes", ".gitignore", "daily/2026/10/2026-10-16.md"]))
        daily = rd(os.path.join(self.home, "daily", "2026", "10", "2026-10-16.md"))
        self.assertNotIn("SELECT", daily)
        self.assertNotIn("1000417", daily)
        self.assertTrue(os.path.exists(os.path.join(self.home, "tickets", "ABC-1", "README.md")))
        self.git("add", "-f", "tickets/ABC-1/README.md")
        r = self.git("commit", "-m", "sneak")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stay local", r.stderr)

    def test_public_pass(self):
        from chitragupta.redact import redact
        t = ("Found it.\n\n```sql\nSELECT * FROM shop.customers WHERE id = 1000417\n```\n\n```json\n{\"email\": \"a@acme.test\"}\n```\n"
             "Called https://api-orders-qa-v2.acme.test/api/x with id 1000417 and `UPDATE t SET a = 1`.\n")
        p = redact(t, mode="public")
        for bad in ("SELECT", "UPDATE", "1000417", "api-orders-qa-v2", "a@acme.test"):
            self.assertNotIn(bad, p)
        self.assertEqual(redact(p, mode="public"), p)
        self.assertIn("Found it.", p)
        s = redact('{"email": "qa.user@acme.test", "token": "Bearer abcdefghijklmnop1234"}', mode="secrets")
        self.assertIn("qa.user@acme.test", s)
        self.assertNotIn("abcdefghijklmnop1234", s)


QA_HOST = "https://api-orders-qa-v2.acme.test"
PROD_HOST = "https://api-prod.acme.test"
QA_BEARER = "abcdefghijklmnop" + "qrstuvwxyz123456"


class QaCaptureTests(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate(make_home())
        os.makedirs(os.path.join(self.home, "bruno", "environments"))
        with open(os.path.join(self.home, "bruno", "environments", "QA.bru"), "w") as f:
            f.write("vars {\n  base: %s\n}\n" % QA_HOST)
        from chitragupta import common, envmap
        envmap.build(common.load_config())
        self.repo = tempfile.mkdtemp(prefix="cg-qa-repo-")
        self.addCleanup(rmtree, self.repo)

    def tearDown(self):
        self.restore()

    def hook(self, event, **inp):
        inp.setdefault("session_id", "s-qa")
        inp.setdefault("cwd", self.repo)
        run_hook(event, inp, self.child_env())

    def raw(self):
        return "\n".join(rd(p) for p in glob.glob(os.path.join(self.home, ".worklog", "raw", "*", "*.jsonl")))

    def test_qa_literal_prod_masked_no_credentials(self):
        body = '{"email": "qa.user@acme.test", "role": null}'
        qa = "curl -s -X PUT %s/api/v1/x -H 'Authorization: Bearer %s' -d '%s'" % (QA_HOST, QA_BEARER, body)
        prod = "curl -s -X PUT %s/api/v1/x -d '%s'" % (PROD_HOST, body.replace("qa.user", "real.person"))
        self.hook("post-tool", tool_name="Bash", tool_input={"command": qa},
                  tool_response={"stdout": '{"id": 1000417, "email": "qa.user@acme.test"}', "stderr": ""})
        self.hook("post-tool", tool_name="Bash", tool_input={"command": prod}, tool_response={"stdout": "{}", "stderr": ""})
        raw = self.raw()
        self.assertIn("qa.user@acme.test", raw)        # QA value kept literally (local)
        self.assertIn("1000417", raw)                  # QA response body kept as the expected result
        self.assertNotIn("real.person@acme.test", raw)  # production value masked at capture
        self.assertNotIn(QA_BEARER, raw)                # credentials never stored
        self.assertIn('"env": "qa"', raw)

    def test_worklog_folder_time_only(self):
        self.hook("user-prompt", cwd=self.home, prompt="save the ABC-1001 requests; secret text here")
        self.hook("post-tool", cwd=self.home, tool_name="Bash", tool_input={"command": "echo hi"}, tool_response={})
        files = glob.glob(os.path.join(self.home, ".worklog", "raw", "*", "time", "*.jsonl"))
        self.assertEqual(len(files), 1)
        txt = rd(files[0])
        self.assertIn("ABC-1001", txt)
        self.assertNotIn("secret text", txt)
        self.assertNotIn("echo hi", txt)


# --------------------------------------------------------------------------- the stub database MCP (used by scripts/e2e)

class StubMcpTests(unittest.TestCase):
    """New (the legacy stub was only exercised by the hand-run e2e): a JSON-RPC round trip, no model involved."""

    def test_jsonrpc_roundtrip(self):
        from tests.support import STUB_MCP
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "stub.log")
            msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "describe_table", "arguments": {"connectionId": "qa-main", "table": "shop.orders"}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "run_query", "arguments": {
                        "connectionId": "qa-main", "sql": "SELECT id, is_locked FROM shop.customers WHERE id = 1000417 LIMIT 5"}}},
                    {"jsonrpc": "2.0", "id": 5, "method": "nope"}]
            p = subprocess.run([PY, "-B", STUB_MCP], input="\n".join(json.dumps(m) for m in msgs) + "\n", text=True,
                               capture_output=True, timeout=30, env=dict(os.environ, STUB_MCP_LOG=log))
            out = {r["id"]: r for r in map(json.loads, p.stdout.splitlines())}
            self.assertEqual(sorted(out), [1, 2, 3, 4, 5])  # the notification got no reply
            self.assertEqual(out[1]["result"]["serverInfo"]["name"], "stub-sqldb")
            self.assertIn("run_query", [t["name"] for t in out[2]["result"]["tools"]])
            cols = json.loads(out[3]["result"]["content"][0]["text"])
            self.assertEqual({c["name"]: c["references"] for c in cols}["customer_id"], "customers.id")
            txt = out[4]["result"]["content"][0]["text"]
            self.assertTrue(txt.startswith("1 row(s) in 1ms"), txt)
            self.assertEqual(json.loads(txt.split("\n\n", 1)[1]), [{"id": 1000417, "is_locked": 1}])
            self.assertEqual(out[5]["error"]["code"], -32601)
            self.assertEqual([json.loads(l)["tool"] for l in rd(log).splitlines()], ["describe_table", "run_query"])


if __name__ == "__main__":
    unittest.main()
