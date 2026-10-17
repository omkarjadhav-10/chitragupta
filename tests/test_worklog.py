"""Core engine tests: redaction, tickets, work-day maths, merge/render, inbox, git, hooks, settings merge, branches.

Synthetic fixtures only. Fake secrets are assembled at runtime so this file never holds a literal token.
"""
import datetime as dt
import json
import os
import subprocess
import tempfile
import time
import unittest

from tests.support import PY, Isolated, rd, rdb, rmtree, run_hook, write_config, wr


def make_home(prefixes=("ABC", "DEMO")):
    h = tempfile.mkdtemp(prefix="cg-test-")
    write_config(h, {"ticket_prefixes": list(prefixes), "day_starts_at": "04:00",
                     "capture_scope": {"include_paths": [], "exclude_paths": []}, "paths": {"python": PY}})
    return h


class HomeCase(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate(make_home())

    def tearDown(self):
        self.restore()


# --------------------------------------------------------------------------- redaction

JWT = "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
GHP = "gh" + "p_abcdefghijklmnopqrstuvwxyz0123456789"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEAxyz\n-----END RSA PRIVATE KEY-----"

MUST_MASK = {
    "sqlserver-jdbc": ("jdbc:sqlserver://db01:1433;databaseName=APPDB;user=svc_app;password=S3cr3t!Pass;encrypt=true", "S3cr3t!Pass"),
    "postgres-url": ("postgresql://app_user:hunter2xyz@pg.example.internal:5432/shop", "hunter2xyz"),
    "bearer": ("curl -H 'Authorization: Bearer abcdefghijklmnop123456.qrs' https://x", "abcdefghijklmnop123456"),
    "basic": ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
    "jwt": (JWT, "eyJzdWIiOiIxMjM0NTY3ODkwIn0"),
    "aws": ("key %s here" % AWS, AWS),
    "github": ("token " + GHP, GHP),
    "private-key": (PEM, "MIIEpAIBAAKCAQEAxyz"),
    "hl7-pid": ("PID|1||00123456^^^MRN||Smith^John^A||19800102|M|||123 Main St^^Springfield^IL^62701", "Smith"),
    "fhir-patient": ('{"resourceType":"Patient","name":[{"family":"Smith","given":["John"]}],"birthDate":"1980-01-02",'
                     '"identifier":[{"value":"00123456"}]}', "Smith"),
    "sql-mrn": ("SELECT * FROM patient WHERE mrn = '00123456'", "00123456"),
    "ssn": ("ssn is 123-45-6789", "123-45-6789"),
    "email": ("mail john.smith@acme.test now", "john.smith@acme.test"),
    "phone": ("call (713) 555-0101", "555-0101"),
}
MUST_KEEP = [
    "SELECT config_key, value FROM shop.app_config WHERE config_key = 'SMS_QUIET_HOURS'",
    "XYZ-201",
    "PID-3",
    "C:/Users/dev/Work/acme-portal/services/notify/NotificationRetryJob.java:88",
    "C:\\Users\\dev\\Work\\acme\\order-service\\README.md",
    "Retry job re-sends SMS because retry_count is never persisted (NotificationRetryJob.java:88).",
    "GET /api/v2/orders/{orderId}/kits",
    "2026-10-14T11:05:00+05:30",
]


class RedactionTests(HomeCase):
    def test_must_mask(self):
        from chitragupta import redact
        for name, (text, secret) in MUST_MASK.items():
            out = redact.redact(text)
            self.assertNotIn(secret, out, name)
            self.assertEqual(redact.redact(out), out, "idempotent: " + name)

    def test_must_keep(self):
        from chitragupta import redact
        for text in MUST_KEEP:
            self.assertEqual(redact.redact(text), text)

    def test_hl7_keeps_structure(self):
        from chitragupta import redact
        out = redact.redact("PID|1||00123456^^^MRN||Smith^John")
        self.assertTrue(out.startswith("PID|1||"))
        self.assertEqual(out.count("|"), 5)

    def test_shape_preserving_phone(self):
        from chitragupta import redact
        self.assertIn("(XXX) XXX-XXXX", redact.redact("phone (713) 555-0101"))

    def test_fix_file_json(self):
        from chitragupta import redact
        p = os.path.join(self.home, "x.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"note": "password=hunter2xyz"}, f)
        self.assertTrue(redact.file_needs_fix(p))
        redact.fix_file(p)
        self.assertFalse(redact.file_needs_fix(p))
        self.assertNotIn("hunter2xyz", rd(p))

    def test_allow_patterns(self):
        from chitragupta import redact
        cfgp = os.path.join(self.home, ".worklog", "config.json")
        cfg = json.loads(rd(cfgp))
        cfg["allow_patterns"] = [r"support@acme\.test"]
        wr(cfgp, json.dumps(cfg))
        redact._cfg_cache.clear()
        self.assertIn("support@acme.test", redact.redact("write to support@acme.test"))


# --------------------------------------------------------------------------- tickets & day boundary

class TicketTests(unittest.TestCase):
    def test_find_tickets_lookalikes(self):
        from chitragupta import common
        pre = ["ABC", "XYZ", "UTF", "SHA"]
        txt = "ABC-4235 uses UTF-8 and SHA-256 per ISO-8601; see XYZ-201 and PID-3"
        self.assertEqual(common.find_tickets(txt, pre), ["ABC-4235", "XYZ-201"])

    def test_branch(self):
        from chitragupta import common
        self.assertEqual(common.ticket_from_branch("feat/ABC-4235/sms-flag", ["ABC"]), "ABC-4235")
        self.assertEqual(common.ticket_from_branch("feat/abc-4235-x", ["ABC"]), "ABC-4235")
        self.assertEqual(common.ticket_from_branch("feature/NEWP-12-x", ["ABC"]), "NEWP-12")
        self.assertIsNone(common.ticket_from_branch("fix/utf-8-encoding", ["ABC"]))
        self.assertIsNone(common.ticket_from_branch("fix/UTF-8-encoding", ["ABC"]))
        self.assertIsNone(common.ticket_from_branch("qa", ["ABC"]))
        self.assertEqual(common.new_branch_prefix("feature/NEWP-12-x", ["ABC"]), "NEWP")
        self.assertIsNone(common.new_branch_prefix("feat/ABC-1-x", ["ABC"]))

    def test_day_boundary(self):
        from chitragupta import common
        cfg = {"day_starts_at": "04:00"}
        tz = dt.datetime.now().astimezone().tzinfo
        self.assertEqual(common.workday(dt.datetime(2026, 10, 15, 1, 30, tzinfo=tz), cfg), "2026-10-14")
        self.assertEqual(common.workday(dt.datetime(2026, 10, 15, 3, 59, tzinfo=tz), cfg), "2026-10-14")
        self.assertEqual(common.workday(dt.datetime(2026, 10, 15, 4, 0, tzinfo=tz), cfg), "2026-10-15")
        s, e = common.day_bounds("2026-10-14", cfg)
        self.assertEqual((s.hour, e.day, e.hour), (4, 15, 4))

    def test_blocks(self):
        from chitragupta import agent
        tz = dt.datetime.now().astimezone().tzinfo
        b = dt.datetime(2026, 10, 14, 10, 0, tzinfo=tz)
        stamps = [b, b + dt.timedelta(minutes=10), b + dt.timedelta(minutes=25),  # block 1: 25m
                  b + dt.timedelta(hours=2), b + dt.timedelta(hours=2, minutes=5)]  # block 2: 5m
        self.assertEqual(agent.blocks_minutes(stamps), 25 + 1 + 5 + 1)


# --------------------------------------------------------------------------- merge / render

SAMPLE = {
    "activity_summary": "Traced duplicate SMS to the retry job.",
    "ticket_links": [{"ticket": "ABC-1234", "title_hint": "Duplicate SMS on retry", "evidence": "branch"}],
    "items": [{"kind": "root_cause", "ticket": "ABC-1234", "title": "Retry job never persists retry_count",
               "detail": "NotificationRetryJob re-sends because retry_count stays 0.", "evidence": ["NotificationRetryJob.java:88"],
               "confidence": "confirmed", "time": "11:05", "updates": None},
              {"kind": "todo", "ticket": "ABC-1234", "title": "Add unit test for retry", "detail": "", "evidence": [],
               "confidence": "likely", "time": "11:10", "updates": None}],
    "queries": [{"ticket": "ABC-1234", "dialect": "postgresql",
                 "text": "SELECT order_id, COUNT(*) FROM shop.notification_audit GROUP BY order_id HAVING COUNT(*) > 1",
                 "purpose": "duplicate sends per order", "tables": ["shop.notification_audit"], "kind": "read",
                 "revealed": "duplicates only after gateway timeout"}],
    "tables": [{"name": "shop.notification_audit", "db": "PostgreSQL", "purpose": "SMS send audit",
                "key_columns": ["order_id", "status"], "relations": [], "ticket": "ABC-1234"}],
    "apis": [{"method": "POST", "path": "/v1/sms/send", "host_or_service": "sms-gateway", "direction": "outbound",
              "purpose": "send SMS", "request_fields": ["to", "body"], "response_fields": ["id"], "auth_type": "api key",
              "ticket": "ABC-1234"}],
    "integrations": [], "files": [{"path": "services/notify/NotificationRetryJob.java", "role": "retry scheduler",
                                   "changed": True, "ticket": "ABC-1234"}],
}


class MergeRenderTests(HomeCase):
    def ctx(self):
        return {"day": "2026-10-14", "session": "abcdef123456", "time": "11:00", "repo": "C:/w/acme-portal",
                "branch": "feature/ABC-1234-retry", "candidates": ["ABC-1234"]}

    def test_merge_idempotent(self):
        from chitragupta import store
        st = store.Store()
        store.merge_extraction(st, SAMPLE, self.ctx(), ["ABC"])
        store.merge_extraction(st, json.loads(json.dumps(SAMPLE)), self.ctx(), ["ABC"])
        tk = st.ticket("ABC-1234")
        self.assertEqual(len(tk["items"]), 2)
        self.assertEqual(len(tk["queries"]), 1)
        self.assertEqual(len(st.day("2026-10-14")["activity"]), 1)
        self.assertEqual(tk["title"], "Duplicate SMS on retry")
        self.assertEqual(len(st.catalog()["tables"]), 1)

    def test_updates_replaces(self):
        from chitragupta import store
        st = store.Store()
        store.merge_extraction(st, SAMPLE, self.ctx(), ["ABC"])
        iid = [i for i in st.ticket("ABC-1234")["items"].values() if i["kind"] == "todo"][0]["id"]
        upd = {"items": [{"kind": "fix", "ticket": "ABC-1234", "title": "Added retry unit test", "detail": "done",
                          "evidence": ["RetryJobTest.java"], "confidence": "confirmed", "time": "12:00", "updates": iid}]}
        store.merge_extraction(st, upd, self.ctx(), ["ABC"])
        items = st.ticket("ABC-1234")["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[iid]["kind"], "fix")
        self.assertEqual(items[iid]["history"][0]["kind"], "todo")

    def test_unknown_ticket_goes_to_other(self):
        from chitragupta import store
        st = store.Store()
        res = {"items": [{"kind": "finding", "ticket": "UTF-8", "title": "Encoding is UTF-8", "detail": "", "evidence": [],
                          "confidence": "confirmed", "time": "", "updates": None}]}
        ctx = dict(self.ctx(), candidates=[])
        store.merge_extraction(st, res, ctx, ["ABC"])
        self.assertEqual(store.Store.all_ticket_ids(), [])
        self.assertEqual(len(st.day("2026-10-14")["other"]), 1)

    def test_render_idempotent_and_notes(self):
        from chitragupta import render, store
        st = store.Store()
        store.merge_extraction(st, SAMPLE, self.ctx(), ["ABC"])
        st.save()
        cfg = {"day_starts_at": "04:00", "models": {}}
        render.render_all(st, cfg, {}, days=["2026-10-14"], tickets=["ABC-1234"])
        daily = os.path.join(self.home, "daily", "2026", "10", "2026-10-14.md")
        readme = os.path.join(self.home, "tickets", "ABC-1234", "README.md")
        self.assertTrue(os.path.exists(daily) and os.path.exists(readme))
        first = rd(daily)
        self.assertIn("Retry job never persists retry_count", first)
        self.assertIn("[ABC-1234](../../../tickets/ABC-1234/README.md)", first)
        # my-notes survive
        notes = render.NOTES_START + "\nremember: ask QA about UAT\n" + render.NOTES_END
        txt = rd(readme).replace(render.NOTES_BLOCK, notes)
        wr(readme, txt)
        st2 = store.Store()
        changed = render.render_all(st2, cfg, {}, days=["2026-10-14"], tickets=["ABC-1234"])
        self.assertNotIn(daily, changed)
        self.assertIn("remember: ask QA about UAT", rd(readme))
        self.assertEqual(rd(daily), first)
        q = rd(os.path.join(self.home, "tickets", "ABC-1234", "queries.md"))
        self.assertIn("```sql", q)
        ctxf = rd(os.path.join(self.home, ".worklog", "cache", "context", "ABC-1234.md"))
        self.assertLessEqual(len(ctxf), 4000)
        self.assertIn("Retry job never persists retry_count", ctxf)

    def test_no_bom_lf(self):
        from chitragupta import common
        p = os.path.join(self.home, "a.md")
        common.write_text(p, "x\ny\n")
        raw = rdb(p)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", raw)


class InboxTests(HomeCase):
    def test_inbox_hashing(self):
        from chitragupta import sources
        p = os.path.join(self.home, "inbox.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# Inbox\n<!--\nABC-1: template line\n-->\nABC-1234: QA confirmed fix\nplain note\n")
        state = {}
        a = sources.scan_inbox({}, state)
        self.assertEqual([(x["ticket"], x["text"]) for x in a], [("ABC-1234", "QA confirmed fix"), (None, "plain note")])
        self.assertEqual(sources.scan_inbox({}, state), [])
        with open(p, "a", encoding="utf-8") as f:
            f.write("ABC-9: another\n")
        b = sources.scan_inbox({}, state)
        self.assertEqual(len(b), 1)
        self.assertEqual(rd(p).count("QA confirmed fix"), 1)  # never rewritten


class GitTests(HomeCase):
    def test_commit_message(self):
        from chitragupta import gitsync
        m = gitsync.commit_message("2026-10-14", {"ABC-1234": {"findings": 2, "queries": 1}, "XYZ-77": {"APIs": 1}})
        self.assertEqual(m, "worklog(2026-10-14): ABC-1234 +2 findings +1 query · XYZ-77 +1 API")

    def test_parse_github(self):
        from chitragupta import gitsync
        self.assertEqual(gitsync.parse_github("https://github.com/example-user/worklog"), ("example-user", "worklog"))
        self.assertEqual(gitsync.parse_github("git@github.com:a/b.git"), ("a", "b"))

    def test_public_guard_blocks_push(self):
        from chitragupta import gitsync, render
        os.environ["CHITRAGUPTA_PRIVACY_SIMULATE"] = "public"
        health = {}
        cfg = {"github_remote": "https://github.com/x/y", "paths": {}}
        self.assertEqual(gitsync.check_privacy(cfg, health, force=True), "public")
        self.assertIn("PUBLIC", render.health_footer({"sessions": {}, "stats": {}}, health))
        gitsync.ensure_repo(cfg)
        self.assertFalse(gitsync.push(cfg, health))
        self.assertEqual(health.get("push_blocked"), "public")

    def test_privacy_simulate_legacy_name(self):
        """The old WORKLOG_* name is still read as a fallback."""
        from chitragupta import gitsync
        os.environ["WORKLOG_PRIVACY_SIMULATE"] = "public"
        self.assertEqual(gitsync.privacy_status({"github_remote": "https://github.com/x/y"}), "public")


# --------------------------------------------------------------------------- hook behaviour

class HookTests(HomeCase):
    def run_hook(self, event, payload, env_extra=None, exe=None):
        return run_hook(event, payload, self.child_env(**(env_extra or {})), exe=exe)

    def raw_lines(self):
        out = []
        for dp, _, fs in os.walk(os.path.join(self.home, ".worklog", "raw")):
            for f in fs:
                out += rd(os.path.join(dp, f)).splitlines()
        return out

    def scratch_repo(self, branch="feature/DEMO-1-selftest"):
        d = tempfile.mkdtemp(prefix="cg-repo-")
        os.makedirs(os.path.join(d, ".git"))
        with open(os.path.join(d, ".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/%s\n" % branch)
        self.addCleanup(rmtree, d)
        return d

    def test_capture_and_redaction(self):
        repo = self.scratch_repo()
        p = self.run_hook("user-prompt", {"session_id": "s1", "cwd": repo, "user_prompt": "check password=hunter2xyz"})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")
        lines = self.raw_lines()
        self.assertEqual(len(lines), 1)
        ev = json.loads(lines[0])
        self.assertEqual(ev["ticket"], "DEMO-1")
        self.assertEqual(ev["branch"], "feature/DEMO-1-selftest")
        self.assertNotIn("hunter2xyz", lines[0])

    def test_tool_output_never_recorded(self):
        repo = self.scratch_repo()
        self.run_hook("post-tool", {"session_id": "s1", "cwd": repo, "tool_name": "Bash",
                                    "tool_input": {"command": "ls"}, "tool_response": {"stdout": "SECRET_OUTPUT"}})
        self.assertNotIn("SECRET_OUTPUT", "".join(self.raw_lines()))

    def test_recursion_guard(self):
        repo = self.scratch_repo()
        for marker in ("CHITRAGUPTA_INTERNAL", "WORKLOG_INTERNAL"):
            for ev in ("session-start", "user-prompt", "post-tool", "stop", "session-end"):
                p = self.run_hook(ev, {"session_id": "s1", "cwd": repo}, {marker: "1"})
                self.assertEqual((p.returncode, p.stdout), (0, b""), (marker, ev))
        self.assertEqual(self.raw_lines(), [])

    def test_pause(self):
        repo = self.scratch_repo()
        open(os.path.join(self.home, ".worklog", "PAUSED"), "w").close()
        self.run_hook("user-prompt", {"session_id": "s1", "cwd": repo, "user_prompt": "x"})
        self.assertEqual(self.raw_lines(), [])
        os.remove(os.path.join(self.home, ".worklog", "PAUSED"))
        self.run_hook("user-prompt", {"session_id": "s1", "cwd": repo, "user_prompt": "x"})
        self.assertEqual(len(self.raw_lines()), 1)

    def test_ignore_and_scope(self):
        repo = self.scratch_repo()
        open(os.path.join(repo, ".worklog-ignore"), "w").close()
        self.run_hook("user-prompt", {"session_id": "s1", "cwd": repo, "user_prompt": "x"})
        self.run_hook("user-prompt", {"session_id": "s1", "cwd": self.home, "user_prompt": "x"})
        # sessions inside the data home leave a time-only line (timestamp + ticket), never content
        lines = [json.loads(l) for l in self.raw_lines()]
        self.assertTrue(all(l.get("source") == "time-only" and "payload" not in l for l in lines), lines)
        self.assertEqual(len(lines), 1)

    def test_bad_input_exit0(self):
        p = run_hook("stop", None, self.child_env(), raw_input=b"not json{")
        self.assertEqual((p.returncode, p.stdout), (0, b""))

    def test_session_start_context_and_speed(self):
        repo = self.scratch_repo()
        ctxdir = os.path.join(self.home, ".worklog", "cache", "context")
        os.makedirs(ctxdir, exist_ok=True)
        with open(os.path.join(ctxdir, "DEMO-1.md"), "w", encoding="utf-8") as f:
            f.write("Worklog facts recorded automatically for DEMO-1.")
        times = []
        for i in range(20):
            t = time.perf_counter()
            p = self.run_hook("session-start", {"session_id": "s%d" % i, "cwd": repo, "source": "startup"})
            times.append((time.perf_counter() - t) * 1000)
        out = json.loads(p.stdout)
        self.assertIn("DEMO-1", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(out["hookSpecificOutput"]["sessionTitle"], "DEMO-1 · " + os.path.basename(repo))
        times.sort()
        p95 = times[int(len(times) * 0.95) - 1]
        print("\nSessionStart p95 = %.0f ms (median %.0f ms)" % (p95, times[len(times) // 2]))
        self.assertLess(p95, 500)

    def test_pythonw_receives_stdin(self):
        pyw = os.path.join(os.path.dirname(PY), "pythonw.exe")
        if not os.path.exists(pyw):
            self.skipTest("no pythonw")
        repo = self.scratch_repo()
        self.run_hook("stop", {"session_id": "s9", "cwd": repo, "last_assistant_message": "done"}, exe=pyw)
        self.assertIn('"done"', "".join(self.raw_lines()))


class SettingsMergeTests(HomeCase):
    def test_merge_keeps_everything(self):
        from chitragupta import install
        before = {"model": "opus", "statusLine": {"type": "command", "command": "x"},
                  "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "learn"}]}],
                            "Stop": [{"hooks": [{"type": "command", "command": "other"}]}]},
                  "permissions": {"allow": ["Bash(ls)"]}, "enabledPlugins": {"a": True}}
        cfgp = os.path.join(self.home, ".worklog", "config.json")
        cfg = json.loads(rd(cfgp))
        cfg["paths"] = {"python": "C:/p/python.exe", "pythonw": "C:/p/pythonw.exe"}
        wr(cfgp, json.dumps(cfg))
        after = install.merged_settings(before)
        again = install.merged_settings(after)
        self.assertEqual(after, again)  # idempotent
        for k in before:
            self.assertIn(k, after)
        self.assertEqual(after["hooks"]["SessionStart"][0], before["hooks"]["SessionStart"][0])
        self.assertEqual(after["hooks"]["Stop"][0], before["hooks"]["Stop"][0])
        self.assertEqual(len(after["hooks"]["SessionStart"]), 2)
        self.assertIn("Bash(ls)", after["permissions"]["allow"])
        self.assertTrue(after["hooks"]["Stop"][1]["hooks"][0]["async"])
        self.assertEqual(install.strip_ours(after), before)


# --------------------------------------------------------------------------- branches
# (in the legacy suite this class sat below `if __name__ == "__main__"` and only ran under discover)

class BranchTests(HomeCase):
    def g(self, *args):
        p = subprocess.run(["git", "-C", self.repo] + list(args), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def commit(self, name, msg):
        with open(os.path.join(self.repo, name), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
        self.g("add", "-A")
        self.g("commit", "-q", "-m", msg)

    def setUp(self):
        super().setUp()
        self.repo = tempfile.mkdtemp(prefix="cg-branches-")
        self.addCleanup(rmtree, self.repo)
        self.g("init", "-q", "-b", "qa")
        self.g("config", "user.email", "t@acme.test")
        self.g("config", "user.name", "t")
        self.g("config", "commit.gpgsign", "false")
        self.commit("a.txt", "base")
        self.g("branch", "uat")
        self.g("branch", "staging")
        # the workflow: feature from qa, env copy cut from uat with the commit cherry-picked
        self.g("checkout", "-q", "-b", "feat/ABC-1/sms-flag")           # reflog: Created from HEAD (on qa)
        self.commit("b.txt", "ABC-1: sms flag")
        self.g("checkout", "-q", "uat")
        self.commit("u.txt", "uat-only hotfix")                          # uat has moved on
        self.g("checkout", "-q", "feat/ABC-1/sms-flag")
        self.g("branch", "chore/ABC-1/sms-flag-uat", "uat")              # reflog: Created from uat
        self.g("checkout", "-q", "chore/ABC-1/sms-flag-uat")
        self.g("cherry-pick", self.g("rev-parse", "feat/ABC-1/sms-flag"))
        self.g("checkout", "-q", "qa")
        self.g("merge", "-q", "--no-ff", "-m", "Merged PR 1: ABC-1", "feat/ABC-1/sms-flag")
        # a branch whose reflog is gone -> parent must be inferred
        self.g("checkout", "-q", "-b", "fix/ABC-2/old", "staging")
        self.commit("c.txt", "ABC-2: old fix")
        self.g("checkout", "-q", "qa")
        os.remove(os.path.join(self.repo, ".git", "logs", "refs", "heads", "fix", "ABC-2", "old"))
        self.cfg = {"ticket_prefixes": ["ABC"], "env_branches": ["qa", "uat", "staging"], "paths": {}}

    def test_parent_created_and_env_status(self):
        from chitragupta import branches
        recs = branches.scan_repo(self.cfg, self.repo, {})
        self.assertEqual(set(recs), {"feat/ABC-1/sms-flag", "chore/ABC-1/sms-flag-uat", "fix/ABC-2/old"})
        feat, chore, old = recs["feat/ABC-1/sms-flag"], recs["chore/ABC-1/sms-flag-uat"], recs["fix/ABC-2/old"]
        self.assertEqual((feat["parent"], feat["parent_source"]), ("qa", "reflog (HEAD)"))
        self.assertEqual((chore["parent"], chore["parent_source"]), ("uat", "reflog"))
        self.assertEqual(old["parent_source"], "inferred")
        self.assertEqual(old["parent"], "staging")  # uat moved on, so staging is the unique nearest base
        self.assertEqual(feat["ahead"], 1)  # own commits stay countable after the merge into qa
        self.assertEqual(feat["created_source"], "reflog")
        self.assertEqual(old["created_source"], "first own commit")
        self.assertEqual(feat["ticket"], "ABC-1")
        self.assertEqual(feat["envs"]["qa"]["status"], "merged")
        self.assertEqual(feat["envs"]["uat"]["status"], "not merged")
        self.assertEqual(chore["envs"]["qa"]["status"], "cherry-picked")  # same patch reached qa via feat
        self.assertEqual(chore["envs"]["uat"]["status"], "not merged")
        self.assertEqual(old["envs"]["qa"]["status"], "not merged")
        # PR into uat merges the env copy -> both now show uat as done
        self.g("checkout", "-q", "uat")
        self.g("merge", "-q", "--no-ff", "-m", "Merged PR 2", "chore/ABC-1/sms-flag-uat")
        recs2 = branches.scan_repo(self.cfg, self.repo, recs)
        self.assertEqual(recs2["chore/ABC-1/sms-flag-uat"]["envs"]["uat"]["status"], "merged")
        self.assertEqual(recs2["feat/ABC-1/sms-flag"]["envs"]["uat"]["status"], "cherry-picked")
        self.assertEqual(recs2["feat/ABC-1/sms-flag"]["created_at"], feat["created_at"])  # cached, not recomputed

    def test_head_activity(self):
        from chitragupta import branches
        acts = branches.head_activity(self.cfg, self.repo, "2000-01-01T00:00:00+00:00")
        switches = [(a["from"], a["to"]) for a in acts if a["kind"] == "switch"]
        self.assertIn(("qa", "feat/ABC-1/sms-flag"), switches)
        self.assertIn(("feat/ABC-1/sms-flag", "chore/ABC-1/sms-flag-uat"), switches)
        self.assertTrue(any(a["kind"] == "cherry-pick" for a in acts))
        self.assertTrue(any(a["kind"] == "merge" for a in acts))
        self.assertFalse(any(a["kind"] == "commit" for a in acts))

    def test_render(self):
        from chitragupta import branches, render, store
        st = store.Store()
        st.branches()["repo"] = branches.scan_repo(self.cfg, self.repo, {})
        md = render.render_branches(st)
        self.assertIn("`feat/ABC-1/sms-flag`", md)
        self.assertIn("✅ merged", md)
        self.assertIn("✅ cherry-picked", md)
        self.assertIn("_(inferred)_", md)
        self.assertIn("Branches:", render.render_context(st, "ABC-1"))
        self.assertIn("## Branches", render.render_ticket(st, "ABC-1"))


if __name__ == "__main__":
    unittest.main()
