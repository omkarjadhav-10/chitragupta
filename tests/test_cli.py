"""`chitragupta cli` (the /cg: commands' engine), the /cg: skill files, the recall-skill and CLAUDE.md block templates.

Synthetic data only (ABC-1001…, schema `shop`). Every test runs in a throwaway CHITRAGUPTA_HOME / CHITRAGUPTA_USER_DIR.
"""
import contextlib
import datetime as dt
import glob
import io
import json
import os
import re
import subprocess
import unittest

from tests.support import PY, ROOT, RUN_PY, Isolated, rd, write_config, wr
from chitragupta import cli, common
from chitragupta import timesheet as T

SKILLS = ["today", "ticket", "timesheet", "trace", "log", "pause", "dashboard", "setup", "doctor", "branches", "check", "branch", "search", "share"]
LAUNCH = 'py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py"'


def run_cli(*argv):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, buf.getvalue(), err.getvalue()


def tree(root):
    out = {}
    for p in glob.glob(os.path.join(root, "**", "*"), recursive=True):
        if os.path.isfile(p):
            out[os.path.relpath(p, root)] = os.path.getmtime(p)
    return out


def frontmatter(path):
    text = rd(path)
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, path
    meta, key = {}, None
    for line in m.group(1).splitlines():
        if line.startswith("  - ") and key:
            meta.setdefault(key, []).append(line[4:].strip())
        elif ":" in line and not line.startswith(" "):
            key, v = line.split(":", 1)
            key, v = key.strip(), v.strip()
            meta[key] = v.strip('"') if v else []
    return meta, m.group(2)


class CliTest(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.cfg = {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00"}
        write_config(self.home, self.cfg)

    def tearDown(self):
        self.restore()

    # ---- fixtures
    def day(self, day, tickets, other=0):
        dd = {"date": day, "updated_at": day + "T12:00:00+00:00",
              "tickets": {t: {"minutes": m, "where": ["shop · main"]} for t, m in tickets.items()},
              "other": {"shop · main": {"minutes": other}} if other else {}}
        p = os.path.join(self.home, "data", "days", day[:4], day + ".json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        wr(p, json.dumps(dd))

    def ticket(self, key, **kw):
        tk = {"id": key, "title": "Upload fails for large files", "status": "in progress", "first": "2026-10-12",
              "last": "2026-10-14", "tldr": "Large uploads time out in the order service.",
              "items": {"i1": {"kind": "root_cause", "text": "The proxy limit is 10 MB.", "date": "2026-10-13"},
                        "i2": {"kind": "finding", "text": "Retries double the upload.", "date": "2026-10-13"},
                        "i3": {"kind": "question", "text": "Raise the limit?", "date": "2026-10-14"}},
              "timeline": {"2026-10-13": {"minutes": 90, "line": "worked in shop · main"},
                           "2026-10-14": {"minutes": 30, "line": "retested"}},
              "queries": {"q1": {}}, "replay": {"final": [{"n": 1}, {"n": 2}]}, "data_flow": {"d1": {}}}
        tk.update(kw)
        p = os.path.join(self.home, "data", "tickets", key + ".json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        wr(p, json.dumps(tk))

    def calendar(self, cal):
        p = os.path.join(self.home, "data", "calendar.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        wr(p, json.dumps(cal))

    def inbox(self):
        p = os.path.join(self.home, "inbox.md")
        return rd(p) if os.path.exists(p) else ""

    # ---- where / status / pause
    def test_where_reports_paths_and_setup_state(self):
        rc, out, _ = run_cli("where", "--json")
        self.assertEqual(rc, 0)
        info = json.loads(out)
        self.assertEqual(os.path.normcase(info["home"]), os.path.normcase(os.path.abspath(self.home)))
        self.assertEqual(info["home_source"], "CHITRAGUPTA_HOME")
        self.assertTrue(info["set_up"])  # CHITRAGUPTA_HOME set + config.json present
        self.assertFalse(info["runtime"]["launcher_exists"])
        os.remove(os.path.join(self.home, ".worklog", "config.json"))
        self.assertFalse(json.loads(run_cli("where", "--json")[1])["set_up"])
        self.assertIn("run /cg:setup", run_cli("where")[1])

    def test_pause_resume_status(self):
        flag = os.path.join(self.home, ".worklog", "PAUSED")
        self.assertIn("Running", run_cli("status")[1])
        self.assertEqual(run_cli("pause")[0], 0)
        self.assertTrue(os.path.exists(flag))
        self.assertIn("Already paused", run_cli("pause")[1])
        self.assertIn("Paused since", run_cli("status")[1])
        self.assertEqual(run_cli("resume")[0], 0)
        self.assertFalse(os.path.exists(flag))
        self.assertIn("Not paused", run_cli("resume")[1])

    # ---- log
    def test_log_appends_one_valid_line(self):
        cases = [(["leave:", "2026-10-20"], "leave: 2026-10-20"),
                 (["leave", "2026-10-20..2026-10-22"], "leave: 2026-10-20..2026-10-22"),
                 (["half-day:", "2026-10-24"], "half-day: 2026-10-24"),
                 (["holiday:", "2026-11-09", "Festival"], "holiday: 2026-11-09 Festival"),
                 (["workday:", "2026-10-25"], "workday: 2026-10-25"),
                 (["resend:", "2026-10-16"], "resend: 2026-10-16"),
                 (["time:", "2026-10-16", "18:30", "1h", "ABC-1001", "Connected with Alex."],
                  "time: 2026-10-16 18:30 1h ABC-1001 Connected with Alex."),
                 (["TIME:", "1h 30m abc-1002 Reviewed the fix."], "time: 1h 30m abc-1002 Reviewed the fix.")]
        for argv, line in cases:
            rc, out, err = run_cli("log", *argv)
            self.assertEqual(rc, 0, err)
            self.assertIn(line, out)
        lines = self.inbox().splitlines()
        self.assertEqual(lines, [l for _, l in cases])
        today = common.workday(common.now(), common.load_config())
        for l in lines:  # what the agent will read back
            d = T.parse_directive(l, today)
            self.assertNotEqual(d["kind"], "invalid", l)

    def test_log_rejects_bad_lines_and_skips_repeats(self):
        for argv in (["nonsense"], ["leave:", "2026-02-30"], ["leave:", "Monday"], ["time:", "2h", "ABC-1001"],
                     ["time:", "25:00", "1h", "ABC-1001", "Late call."], ["time:", "1h", "nope", "text"], []):
            rc, out, err = run_cli("log", *argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("Not added", err)
        self.assertEqual(self.inbox(), "")
        t = ("time:", "2026-10-20", "1h", "ABC-1001", "Late call.")
        run_cli("log", *t)
        rc, out, _ = run_cli("log", *t)  # a data line: a repeat is not added
        self.assertEqual(rc, 0)
        self.assertIn("Already in inbox.md", out)
        run_cli("log", "leave:", "2026-10-20")
        run_cli("log", "leave:", "2026-10-20")  # a calendar mark counts every copy (mark, clear, mark again)
        self.assertEqual(self.inbox(), "time: 2026-10-20 1h ABC-1001 Late call.\nleave: 2026-10-20\nleave: 2026-10-20\n")

    def test_log_keeps_existing_inbox_and_adds_newline(self):
        wr(os.path.join(self.home, "inbox.md"), "# Inbox\nABC-1001: remember the proxy limit")
        run_cli("log", "workday:", "2026-10-25")
        self.assertEqual(self.inbox(), "# Inbox\nABC-1001: remember the proxy limit\nworkday: 2026-10-25\n")

    def test_log_mentions_correction_for_a_sent_day(self):
        today = common.workday(common.now(), common.load_config())
        day = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
        p = os.path.join(self.home, "data", "timesheets", day + ".json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        wr(p, json.dumps({"day": day, "status": "sent"}))
        out = run_cli("log", "half-day:", day)[1]
        self.assertIn("Corrected update", out)

    def test_log_today(self):
        rc, out, _ = run_cli("log", "--today")
        self.assertEqual(rc, 0)
        self.assertIn(common.workday(common.now(), common.load_config()), out)
        self.assertEqual(self.inbox(), "")

    # ---- today / hours
    def test_today_table_and_preview_are_read_only(self):
        self.day("2026-10-14", {"ABC-1001": 95, "ABC-1002": 30}, other=10)
        self.ticket("ABC-1001")
        self.calendar({"time": {"2026-10-14": [{"ticket": "ABC-1002", "start": "18:30", "minutes": 60,
                                                "text": "Synced with Alex."}]}})
        before = tree(self.home)
        rc, out, _ = run_cli("today", "--date", "2026-10-14")
        self.assertEqual(rc, 0)
        self.assertIn("about 2h 15m tracked", out)
        self.assertIn("| ABC-1001 | Upload fails for large files | 1h 35m |", out)
        self.assertIn("| (no ticket) | | 10m |", out)
        self.assertIn("ABC-1002 1h at 18:30 Synced with Alex.", out)
        self.assertIn("Jira worklog preview", out)
        self.assertNotIn("preview unavailable", out)
        self.assertEqual(tree(self.home), before, "today must not write anything")
        self.assertNotIn("preview", run_cli("today", "--date", "2026-10-14", "--no-preview")[1])

    def test_today_marks_leave_and_bad_date(self):
        self.calendar({"leave": {"2026-10-15": True}})
        self.assertIn("Calendar: leave day.", run_cli("today", "--date", "2026-10-15", "--no-preview")[1])
        with self.assertRaises(SystemExit):
            run_cli("today", "--date", "15-10-2026")

    def test_hours_week_and_month(self):
        self.day("2026-10-12", {"ABC-1001": 60})
        self.day("2026-10-14", {"ABC-1001": 30, "ABC-1002": 120}, other=6)
        self.day("2026-10-20", {"ABC-1002": 45})
        self.ticket("ABC-1001")
        self.calendar({"leave": {"2026-10-16": True}, "holiday": {"2026-10-13": "Festival"}})
        rc, out, _ = run_cli("hours", "--week", "--date", "2026-10-14")
        self.assertEqual(rc, 0)
        self.assertIn("week 2026-W42 (2026-10-12 to 2026-10-18)", out)
        self.assertIn("| ABC-1002 |  |  |  | 2.0 |", out)
        self.assertIn("| ABC-1001 | Upload fails for large files | 1.0 |  | 0.5 |", out)
        self.assertIn("| **Total** |", out)
        self.assertIn("2026-10-13 holiday Festival; 2026-10-16 leave", out)
        rc, out, _ = run_cli("hours", "--month", "--date", "2026-10-01", "--csv")
        rows = out.strip().splitlines()
        self.assertEqual(rows[0], "ticket,title,2026-W40,2026-W41,2026-W42,2026-W43,2026-W44,total_hours")
        self.assertEqual(rows[1], "ABC-1002,,,,2.00,0.75,,2.75")
        self.assertEqual(rows[2], "ABC-1001,Upload fails for large files,,,1.50,,,1.50")
        self.assertEqual(rows[3], "(no ticket),,,,0.10,,,0.10")
        self.assertIn("Nothing tracked", run_cli("hours", "--date", "2025-01-01")[1])

    # ---- ticket / trace
    def test_ticket_summary_and_files(self):
        self.ticket("ABC-1001")
        tdir = os.path.join(self.home, "tickets", "ABC-1001")
        os.makedirs(tdir)
        wr(os.path.join(tdir, "README.md"), "# ABC-1001\n")
        wr(os.path.join(tdir, "db-flow.md"), "# DB flow\n")
        rc, out, _ = run_cli("ticket", "abc-1001")
        self.assertEqual(rc, 0)
        self.assertIn("# ABC-1001 - Upload fails for large files", out)
        self.assertIn("Root cause: The proxy limit is 10 MB.", out)
        self.assertIn("2 findings, 1 open items, 1 queries, 0 APIs, 2 saved requests, 1 DB-flow steps", out)
        self.assertIn("≈ 2h 00m in total", out)
        self.assertIn("- db-flow: " + os.path.join(tdir, "db-flow.md"), out)
        js = json.loads(run_cli("ticket", "ABC-1001", "--json")[1])
        self.assertEqual(js["requests"], 2)
        self.assertIn("README", js["files"])
        self.assertIn("Nothing recorded for ABC-1009", run_cli("ticket", "ABC-1009")[1])
        self.assertIn("Not a ticket key", run_cli("ticket", "rm -rf")[1])
        self.assertIn("Not a ticket key", run_cli("ticket")[1])

    def test_trace_shows_flow_example_and_database_status(self):
        tdir = os.path.join(self.home, "tickets", "ABC-1001")
        os.makedirs(tdir)
        wr(os.path.join(tdir, "db-flow.md"), "# DB flow — ABC-1001\n\n## Column changes\n- `shop.orders.status`\n")
        out = run_cli("trace", "ABC-1001")[1]
        self.assertIn("shop.orders.status", out)
        self.assertIn("No QA worked example", out)
        self.assertIn("database_mcp: not configured", out)
        qa = {"fetched_at": "2026-10-14T10:00:00+00:00", "env": "qa", "connection": "qa-shop",
              "trace": {"summary": "Order moved to PAID.", "steps": [{"table": "shop.orders", "key": "id = 7",
                                                                    "values": "status=PAID", "note": ""}],
                        "changes": [{"table": "shop.orders", "column": "status", "before": "NEW", "after": "PAID"}]}}
        p = os.path.join(self.home, "data", "qa-trace", "ABC-1001.json")
        os.makedirs(os.path.dirname(p))
        wr(p, json.dumps(qa))
        write_config(self.home, dict(self.cfg, mcp={"database": {"enabled": True, "server": "db-test"}}))
        out = run_cli("trace", "ABC-1001")[1]
        self.assertIn("Order moved to PAID.", out)
        self.assertIn("changed `shop.orders.status`: NEW -> PAID", out)
        self.assertIn("database_mcp: configured (db-test)", out)
        self.assertIn("No DB flow recorded for ABC-1002", run_cli("trace", "ABC-1002")[1])

    @unittest.skipUnless("cli" in __import__("chitragupta.__main__", fromlist=["x"]).ENTRY_MODULES,
                         "cli not yet in ENTRY_MODULES")
    def test_runs_through_the_plugin_launcher(self):
        r = subprocess.run([PY, "-B", RUN_PY, "cli", "log", "leave:", "2026-10-20"], capture_output=True,
                           env=self.child_env(), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.inbox(), "leave: 2026-10-20\n")


class SkillFilesTest(unittest.TestCase):
    def test_every_command_skill(self):
        subs = set(cli.parser()._subparsers._group_actions[0].choices)
        for name in SKILLS:
            path = os.path.join(ROOT, "skills", name, "SKILL.md")
            meta, body = frontmatter(path)
            self.assertEqual(meta.get("name"), name)
            self.assertTrue(meta.get("description"), name)
            self.assertEqual(meta.get("disable-model-invocation"), "true", name)
            allowed = meta.get("allowed-tools") or []
            self.assertTrue(allowed, name)
            for rule in allowed:
                if rule.startswith(("Bash(", "PowerShell(")):
                    self.assertIn(LAUNCH, rule, (name, rule))
            for cmd in re.findall(r"!`([^`]+)`", body):  # injected commands must be pre-approved
                pats = [re.match(r"^(?:Bash|PowerShell)\((.*)\)$", r).group(1) for r in allowed
                        if r.startswith("Bash(")]
                ok = any(cmd == p or (p.endswith(" *") and (cmd == p[:-2] or cmd.startswith(p[:-1]))) for p in pats)
                self.assertTrue(ok, (name, cmd))
            for sub in re.findall(re.escape(LAUNCH) + r" cli (\w+)", body + "\n".join(allowed)):
                self.assertIn(sub, subs, (name, sub))
        self.assertEqual(sorted(os.listdir(os.path.join(ROOT, "skills"))), sorted(SKILLS))

    def test_recall_skill_template(self):
        meta, body = frontmatter(os.path.join(ROOT, "user-skill", "chitragupta", "SKILL.md"))
        self.assertEqual(meta["name"], "chitragupta")
        self.assertNotIn("disable-model-invocation", meta)
        self.assertIn("{{HOME}}", meta["description"])
        for must in ("worklog-requests", "worklog-dbflow", "inbox.md", "_drop", "/cg:today", "the database MCP",
                     "resend:", "half-day:"):
            self.assertIn(must, body)
        placeholders = set(re.findall(r"\{\{([^}]+)\}\}", body)) - {"HOME", "LAUNCHER"}
        self.assertTrue(all(p.startswith("process.env.") for p in placeholders), placeholders)

    def test_claude_md_block_template(self):
        text = rd(os.path.join(ROOT, "chitragupta", "templates", "claude_md_block.md"))
        self.assertIn("{{HOME}}", text)
        self.assertIn("`Worklog note:", text)
        self.assertIn("chitragupta skill", text)
        self.assertNotIn("<!--", text)  # setup adds the chitragupta:start/end markers


if __name__ == "__main__":
    unittest.main()


class CheckHeadlineTest(unittest.TestCase):
    """/cg:check says "Fetched" only for repos it really fetched (fetching can be off or not due)."""

    def test_no_fetch_is_not_called_a_fetch(self):
        msg = cli.check_headline({"repos": ["shop-api"], "fetched": []})
        self.assertNotIn("Fetched", msg)
        self.assertIn("no fetch", msg)

    def test_fetched_and_unfetched_repos_are_told_apart(self):
        self.assertEqual(cli.check_headline({"repos": ["a", "b"], "fetched": ["a"]}),
                         "Fetched a; refreshed b from local git data.")
        self.assertEqual(cli.check_headline({"repos": ["a"], "fetched": ["a"]}), "Fetched and refreshed a.")

    def test_old_result_without_fetched_key(self):
        self.assertNotIn("Fetched", cli.check_headline({"repos": ["a"]}))
