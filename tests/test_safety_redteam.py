"""Red-team and allowed-action tests for the safety layer (docs/safety.md, §4 of the plan).

Every attempt below must be blocked by the layer meant to catch it, and logged to security.log; every allowed
action must still work. Synthetic data only (temp repos, ABC-1001, shop schema, acme.test).
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import GIT_IDENTITY, PY, RUN_PY, Isolated, install_launcher, rmtree, write_config

GIT = "git"
SEND = "mcp__slack__slack_send_message"
ME = "U0TESTSELF1"
DB = "sqldb"

# ---------------------------------------------------------------------------------------------- attempt tables

GIT_WRITES = [
    ["commit", "-m", "x"], ["merge", "feature"], ["rebase", "main"], ["cherry-pick", "abc123"], ["revert", "HEAD"],
    ["reset", "--hard", "HEAD~1"], ["checkout", "-b", "x"], ["switch", "main"], ["restore", "file.txt"], ["stash"],
    ["pull"], ["push", "origin", "main"], ["tag", "v1"], ["branch", "-D", "feature"], ["clean", "-fdx"], ["gc"],
    ["prune"], ["worktree", "add", "../wt"], ["config", "user.name", "x"], ["config", "--add", "core.x", "y"],
    ["fetch", "--no-tags", "origin", "main:main"], ["fetch", "--no-tags", "origin", "+refs/heads/main:refs/heads/main"],
    ["fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"],  # no --no-tags
    ["log", "--output=C:/x.txt"], ["ls-remote", "--upload-pack=calc", "origin"],
    ["branch", "--list", "-m", "a", "b"], ["update-ref", "refs/heads/x", "HEAD"], ["am", "p.patch"], ["apply", "p.diff"],
    ["submodule", "update"], ["reflog", "expire", "--all"],
]
GH = [["pr", "create", "--title", "x"], ["pr", "merge", "1"], ["pr", "review", "1", "--approve"],
      ["pr", "comment", "1", "-b", "x"], ["pr", "close", "1"], ["api", "-X", "POST", "repos/o/r/pulls"],
      ["api", "-f", "title=x", "repos/o/r/pulls"], ["api", "--input", "body.json", "repos/o/r/pulls"],
      ["api", "--method=DELETE", "repos/o/r/git/refs/heads/x"], ["pr", "view", "1", "--web"]]
GLAB = [["mr", "merge", "1"], ["mr", "approve", "1"], ["mr", "note", "1", "-m", "x"], ["api", "-X", "PUT", "projects/1"]]
AZ = [["repos", "pr", "create", "--title", "x"], ["repos", "pr", "update", "--id", "1", "--title", "y"],
      ["repos", "pr", "set-vote", "--id", "1", "--vote", "approve"],
      ["repos", "pr", "update", "--id", "1", "--status", "completed"], ["repos", "ref", "delete", "--name", "heads/x"],
      ["boards", "work-item", "update", "--id", "1"], ["pipelines", "run", "--name", "x"], ["vm", "delete", "-n", "x"],
      ["sql", "db", "delete", "-n", "x"], ["rest", "--uri", "https://management.azure.com/x"],
      ["repos", "pr", "list"]]  # the last one: allowed command but without --output json
SQL = [
    "INSERT INTO shop.orders VALUES (1)", "UPDATE shop.orders SET a = 1", "DELETE FROM shop.orders",
    "MERGE INTO t USING s ON 1 = 1 WHEN MATCHED THEN DELETE", "TRUNCATE TABLE shop.orders",
    "DROP TABLE shop.orders", "ALTER TABLE shop.orders ADD c int", "CREATE TABLE t (a int)", "GRANT SELECT ON t TO u",
    "EXEC sp_who", "EXEC xp_cmdshell 'dir'", "SELECT * INTO t2 FROM shop.orders", "EXPLAIN ANALYZE DELETE FROM t",
    "EXPLAIN ANALYZE SELECT * FROM t LIMIT 1", "SELECT nextval('s')", "SELECT * FROM t LIMIT 1 FOR UPDATE",
    "SELECT * FROM t WITH (UPDLOCK)", "SELECT 1; SELECT 2", "SELECT 1 -- note\n; DELETE FROM t",
    "SELECT 1 /* x */; /**/ DELETE FROM t", "SeLeCt 1; dElEtE fRoM t",
    "WITH x AS (D\u0415LETE FROM t RETURNING *) SELECT * FROM x LIMIT 1",  # Cyrillic E
    "WITH x AS (\uff24\uff25\uff2c\uff25\uff34\uff25 FROM t RETURNING *) SELECT * FROM x LIMIT 1",  # fullwidth
    "WITH x AS (DEL\u200bETE FROM t RETURNING *) SELECT * FROM x LIMIT 1",  # zero-width space
    "SELECT pg_try_advisory_lock(1)", "SELECT pg_terminate_backend(1)", "SELECT dblink_exec('c', 'DROP TABLE t')",
    "SELECT lo_import('x')", "SELECT * FROM OPENROWSET('a', 'b', 'c')", "SELECT * FROM OPENQUERY(s, 'x')",
    "EXEC sp_executesql N'DELETE FROM t'", "SELECT setval('s', 1)", "SELECT 1\nGO\nDELETE FROM t",
    "WITH d AS (DELETE FROM t RETURNING id) SELECT id FROM d LIMIT 1", "SELECT * FROM t",  # no LIMIT
]
SQL_OK = ["SELECT TOP 5 * FROM shop.orders", "WITH x AS (SELECT id FROM shop.orders) SELECT * FROM x LIMIT 5",
          "SELECT column_name FROM information_schema.columns WHERE table_name = 'orders'",
          "EXPLAIN SELECT * FROM shop.orders LIMIT 1", "EXEC sp_help 'shop.orders'",
          "SELECT replace(name, 'a', 'b') FROM shop.customers LIMIT 3", "SELECT count(*) FROM shop.orders",
          "SELECT 'delete from x' AS s FROM shop.orders LIMIT 1", "SELECT id FROM shop.orders ORDER BY id DESC LIMIT 20"]
MONGO = [{"operation": op, "collection": "c"} for op in
         ("insertOne", "updateMany", "deleteMany", "findOneAndUpdate", "bulkWrite", "drop", "createIndex", "mapReduce")] + [
    {"operation": "aggregate", "collection": "c", "pipeline": [{"$match": {}}, {"$out": "x"}]},
    {"operation": "aggregate", "collection": "c", "pipeline": [{"$merge": {"into": "x"}}]},
    {"query": "db.orders.find({}).forEach(function (d) { db.orders.deleteOne(d) })"},
    {"operation": "find", "collection": "c", "filter": {"$where": "sleep(1)"}},
]
MONGO_OK = [{"operation": "find", "collection": "c", "filter": {"a": 1}, "limit": 5},
            {"operation": "aggregate", "collection": "c", "pipeline": [{"$match": {"a": 1}}, {"$limit": 5}]},
            {"operation": "countDocuments", "collection": "c"}, {"operation": "distinct", "collection": "c", "key": "a"},
            {"operation": "listCollections"}]
JIRA_WRITES = ["jira_create_issue", "jira_update_issue", "jira_transition_issue", "jira_add_comment", "jira_add_worklog",
               "jira_delete_issue", "jira_assign_issue"]


def _git(repo, *args, check=True):
    env = dict(os.environ, **GIT_IDENTITY)
    p = subprocess.run([GIT, "-C", repo] + list(args), capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise AssertionError("git %s: %s" % (args, p.stderr))
    return p.stdout


def _init_repo(path, remote=None):
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    with open(os.path.join(path, "a.txt"), "w") as f:
        f.write("one\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "first")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    return path


class SafetyCase(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.addCleanup(self.restore)
        self.scratch = tempfile.mkdtemp(prefix="cg-rt-")
        self.addCleanup(rmtree, self.scratch)
        self.repo = _init_repo(os.path.join(self.scratch, "work"))
        self.cfg_dict = {"watched_repos": [self.repo], "ticket_prefixes": ["ABC"],
                         "jira_worklog": {"slack": {"self_user_id": ME, "send_tool": SEND}},
                         "mcp": {"database": {"server": DB, "enabled": True}}}
        write_config(self.home, self.cfg_dict)
        os.makedirs(os.path.join(self.home, ".worklog", "mcp"), exist_ok=True)
        allow = {"max_rows": 20, "servers": {
            "mcp-atlassian": {"read": ["jira_get_issue", "jira_search"] + JIRA_WRITES[:2],  # floor beats a bad list
                              "write": JIRA_WRITES[2:]},
            DB: {"read": ["run_query", "list_tables"], "query_tools": ["run_query"], "connections": ["qa-main"]}}}
        with open(os.path.join(self.home, ".worklog", "mcp", "allow.json"), "w") as f:
            json.dump(allow, f)

    def cfg(self):
        from chitragupta import common
        return common.load_config()

    def log_count(self):
        from chitragupta.safety import log
        return len(log.entries(100000))

    def assertBlocked(self, fn, layer):
        """fn() raises Blocked from `layer`, and one more line lands in security.log with that layer."""
        from chitragupta.safety import log
        n = self.log_count()
        with self.assertRaises(log.Blocked) as cm:
            fn()
        self.assertEqual(cm.exception.layer, layer)
        es = log.entries(100000)
        self.assertEqual(len(es), n + 1)
        self.assertEqual(es[-1]["layer"], layer)
        return cm.exception

    def hook(self, mode, data, profile=None):
        """The real hook entry point, in-process: (exit code, decoded stdout JSON or None)."""
        from chitragupta.safety import hook
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
        sys.stdout = io.StringIO()
        try:
            rc = hook.internal(profile) if mode == "internal" else hook.session()
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        return rc, (json.loads(out) if out.strip() else None)

    def assertHookDenies(self, mode, data, profile=None):
        n = self.log_count()
        rc, out = self.hook(mode, data, profile)
        self.assertEqual(rc, 2, out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.log_count(), n + 1)
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def assertHookAllows(self, mode, data, profile=None):
        rc, out = self.hook(mode, data, profile)
        self.assertEqual((rc, out), (0, None))


# ---------------------------------------------------------------------------------------------- command guard

class CommandGuardTests(SafetyCase):
    def test_git_writes_in_a_work_repo_are_blocked(self):
        from chitragupta.safety import commands
        cfg = self.cfg()
        for args in GIT_WRITES:
            with self.subTest(args=args):
                self.assertBlocked(lambda: commands.check(commands.work_argv(cfg, self.repo, args), "git-work", cfg=cfg),
                                   "command")

    def test_gitro_refuses_before_a_process_starts(self):
        from chitragupta import gitro
        before = _git(self.repo, "rev-parse", "HEAD")
        self.assertBlocked(lambda: gitro.run(self.cfg(), self.repo, ["commit", "--allow-empty", "-m", "x"]), "command")
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), before)

    def test_git_without_hardening_or_outside_watched_repos(self):
        from chitragupta.safety import commands
        cfg = self.cfg()
        self.assertBlocked(lambda: commands.check([GIT, "-C", self.repo, "log"], "git-work", cfg=cfg), "command")
        other = _init_repo(os.path.join(self.scratch, "other"))
        self.assertBlocked(lambda: commands.check([GIT, "-C", other, "fetch", "--no-tags", "origin",
                                                   "+refs/heads/main:refs/remotes/origin/main"], "git-work", cfg=cfg), "command")

    def test_fetch_skipped_while_locked_and_when_off(self):
        from chitragupta import common, gitro
        open(os.path.join(self.repo, ".git", "index.lock"), "w").close()
        self.assertEqual(gitro.fetch(self.cfg(), self.repo, "origin", ["main"]), (False, "skipped: a git lock file exists"))
        os.remove(os.path.join(self.repo, ".git", "index.lock"))
        common.save_config(dict(self.cfg_dict, git={"allow_fetch": False}))
        self.assertIn("off", gitro.fetch(self.cfg(), self.repo, "origin", ["main"])[1])

    def test_hostile_repo_config_never_runs_a_program(self):
        """A repo whose own config sets a pager, an external diff, a textconv driver and fsmonitor to a program:
        plain git runs them; reads through the guard don't."""
        from chitragupta import gitro
        repo = self.repo
        marker = os.path.join(self.scratch, "PWNED").replace("\\", "/")
        script = os.path.join(self.scratch, "evil.sh").replace("\\", "/")
        with open(script, "w", newline="\n") as f:
            f.write("#!/bin/sh\necho ran >> '%s'\ncat \"$@\" 2>/dev/null\nexit 0\n" % marker)
        for k, v in (("core.pager", "sh '%s'" % script), ("diff.external", "sh '%s'" % script),
                     ("diff.evil.textconv", "sh '%s'" % script), ("core.fsmonitor", script)):
            _git(repo, "config", k, v)
        with open(os.path.join(repo, ".gitattributes"), "w") as f:
            f.write("*.txt diff=evil\n")
        with open(os.path.join(repo, "a.txt"), "a") as f:
            f.write("two\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "second")
        with open(os.path.join(repo, "new.txt"), "w") as f:
            f.write("untracked\n")
        # control: an unguarded `git log -p` runs the repo's programs
        subprocess.run([GIT, "-C", repo, "log", "-p", "-1"], capture_output=True)
        self.assertTrue(os.path.exists(marker), "control failed: the hostile config did not run")
        os.remove(marker)
        cfg = self.cfg()
        for args in (["log", "-p", "-1"], ["show", "-s", "HEAD"], ["ls-files", "--others", "--exclude-standard", "-z"],
                     ["log", "--name-status", "-1"], ["rev-list", "--count", "HEAD"]):
            rc, so, se = gitro.run(cfg, repo, args)
            self.assertEqual(rc, 0, se)
        self.assertFalse(os.path.exists(marker), "a guarded read ran a program from the repo's config")

    def test_git_hosts(self):
        from chitragupta.safety import commands
        for exe, cases in (("gh", GH), ("glab", GLAB), ("az", AZ)):
            for args in cases:
                with self.subTest(exe=exe, args=args):
                    argv = [exe] + args + (["--output", "json"] if exe == "az" and args != ["repos", "pr", "list"] else [])
                    self.assertBlocked(lambda: commands.check(argv, "host", cfg=self.cfg()), "command")

    def test_unknown_tools_are_blocked(self):
        from chitragupta.safety import commands
        for argv, purpose in ((["psql", "-c", "select 1"], "self"), (["sqlcmd", "-Q", "x"], "probe"),
                              (["mongosh", "--eval", "x"], "self"), (["kubectl", "delete", "pod", "x"], "host"),
                              (["docker", "rm", "x"], "self"), (["terraform", "apply"], "self"), (["ssh", "h"], "self"),
                              (["powershell", "-c", "Remove-Item x"], "self"), (["git", "--version"], "made-up"),
                              (["schtasks", "/delete", "/tn", "Other", "/f"], "task"),
                              (["schtasks", "/run", "/tn", "Chitragupta"], "task")):
            with self.subTest(argv=argv):
                self.assertBlocked(lambda: commands.check(argv, purpose, cfg=self.cfg()), "command")

    def test_data_home_repo_limits(self):
        from chitragupta import common
        from chitragupta.safety import commands
        h = common.home()
        cfg = self.cfg()
        for args in (["push", "--force", "origin", "main"], ["push", "-f", "origin", "main"],
                     ["push", "origin", "+main"], ["push", "other", "main"], ["add", "-A"], ["add", "--", "tickets/x.md"],
                     ["add", "--", "../escape.md"], ["commit", "--amend", "-m", "x"], ["remote", "set-url", "origin", "https://x"],
                     ["config", "core.hooksPath", "x"], ["reset", "--hard", "origin/main"], ["clean", "-fdx"]):
            with self.subTest(args=args):
                self.assertBlocked(lambda: commands.check([GIT, "-C", h] + args, "git-home", cfg=cfg), "command")
        self.assertBlocked(lambda: commands.check([GIT, "-C", self.repo, "status"], "git-home", cfg=cfg), "command")

    def test_internal_call_argv_is_rechecked(self):
        from chitragupta.safety import claude as builder, commands, paths
        cfg = self.cfg()
        sb = paths.sandbox()
        good = builder.build(["claude"], "x", "none", "haiku", cfg=cfg)
        self.assertIsNone(commands.reason(good, "claude", cwd=sb, cfg=cfg))

        def tamper(fn):
            a = list(good)
            fn(a)
            return a
        bad = [
            tamper(lambda a: a.append("--dangerously-skip-permissions")),
            tamper(lambda a: a.__setitem__(a.index("dontAsk"), "bypassPermissions")),
            tamper(lambda a: a.__setitem__(a.index("dontAsk"), "acceptEdits")),
            tamper(lambda a: a.extend(["--allowedTools", "Bash"])),
            tamper(lambda a: a.__setitem__(a.index("--tools") + 1, "default")),
            tamper(lambda a: a.__setitem__(a.index("--settings") + 1, json.dumps({"disableAllHooks": True}))),
            tamper(lambda a: a.extend(["--add-dir", "C:/"])),
            ["claude", "mcp", "add", "x", "https://x"],
        ]
        for argv in bad:
            with self.subTest(argv=argv[-3:]):
                self.assertBlocked(lambda: commands.check(argv, "claude", cwd=sb, cfg=cfg), "command")
        self.assertBlocked(lambda: commands.check(good, "claude", cwd=self.repo, cfg=cfg), "command")


# ---------------------------------------------------------------------------------------------- path guard

class PathGuardTests(SafetyCase):
    def test_writes_outside_the_roots(self):
        from chitragupta import common
        from chitragupta.safety import paths
        h = common.home()
        bad = [os.path.join(self.repo, "a.txt"), os.path.join(self.repo, "src", "new.py"),
               os.path.join(h, "..", "escape.txt"), h + "\\..\\escape.txt", "\\\\server\\share\\x.txt",
               "\\\\?\\C:\\x.txt", "\\\\.\\PhysicalDrive0", os.path.join(h, "inbox.md:evil"),
               os.path.join(self.repo.upper(), "A.TXT"), ""]
        for p in bad:
            with self.subTest(p=p):
                self.assertBlocked(lambda: paths.write_text(p, "x"), "path")
        self.assertBlocked(lambda: paths.delete(os.path.join(self.repo, "a.txt")), "path")
        self.assertBlocked(lambda: paths.replace(os.path.join(self.repo, "a.txt"), os.path.join(h, "a.txt")), "path")
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "a.txt")))

    def test_junction_out_of_the_data_home(self):
        import _winapi
        from chitragupta import common
        from chitragupta.safety import paths
        outside = os.path.join(self.scratch, "outside")
        os.makedirs(outside)
        j = os.path.join(common.home(), "j")
        _winapi.CreateJunction(outside, j)
        self.addCleanup(lambda: os.path.isdir(j) and os.rmdir(j))
        self.assertBlocked(lambda: paths.write_text(os.path.join(j, "x.txt"), "x"), "path")
        self.assertFalse(os.listdir(outside))

    def test_case_variant_of_an_own_root_is_still_ours(self):
        from chitragupta import common
        from chitragupta.safety import paths
        p = os.path.join(common.home().upper(), "inbox.md")
        self.assertTrue(paths.write_text(p, "x\n"))

    def test_sqlite_files_only_in_the_roots(self):
        import sqlite3
        from chitragupta import common
        from chitragupta.safety import paths
        outside = os.path.join(self.repo, "x.sqlite")
        for p in (outside, os.path.join(common.home(), "..", "x.sqlite"), "\\\\server\\share\\x.sqlite", ""):
            with self.subTest(p=p):
                self.assertBlocked(lambda: paths.sqlite_connect(p), "path")
        self.assertFalse(os.path.exists(outside))
        self.assertBlocked(lambda: paths.sqlite_connect("\\\\server\\share\\x.sqlite", readonly=True), "path")
        mine = common.wl("cache", "t.sqlite")
        con = paths.sqlite_connect(mine)
        try:
            con.execute("CREATE TABLE t(x)")
            con.commit()
            with self.assertRaises(sqlite3.DatabaseError):  # ATTACH would create a database file anywhere
                con.execute("ATTACH DATABASE ? AS o", (outside,))
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute("VACUUM INTO ?", (outside,))
        finally:
            con.close()
        self.assertFalse(os.path.exists(outside))
        ro = paths.sqlite_connect(mine, readonly=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("INSERT INTO t VALUES (1)")
        finally:
            ro.close()
        with self.assertRaises(sqlite3.OperationalError):  # read-only never creates the file
            paths.sqlite_connect(outside, readonly=True).execute("SELECT 1")
        self.assertFalse(os.path.exists(outside))

    def test_copies_read_from_outside_and_never_move(self):
        from chitragupta import common
        from chitragupta.safety import paths
        src = os.path.join(self.repo, "a.txt")
        paths.copy(src, os.path.join(common.home(), "copy.txt"))
        self.assertTrue(os.path.isfile(src))


# ---------------------------------------------------------------------------------------------- query + MCP guards

class QueryAndMcpTests(SafetyCase):
    def test_sql_writes_and_tricks(self):
        tool = "mcp__%s__run_query" % DB
        for sql in SQL:
            with self.subTest(sql=sql):
                self.assertHookDenies("internal", {"tool_name": tool, "tool_input": {"connection_id": "qa-main", "sql": sql}},
                                      "mcp")

    def test_sql_reads(self):
        from chitragupta.safety import query
        for sql in SQL_OK:
            with self.subTest(sql=sql):
                self.assertIsNone(query.sql_reason(sql))

    def test_mongo(self):
        tool = "mcp__%s__run_query" % DB
        for inp in MONGO:
            with self.subTest(inp=inp):
                self.assertHookDenies("internal", {"tool_name": tool, "tool_input": dict(inp, connection_id="qa-main")}, "mcp")
        from chitragupta.safety import query
        for inp in MONGO_OK:
            with self.subTest(inp=inp):
                self.assertIsNone(query.mongo_reason(inp))

    def test_production_connection(self):
        self.assertHookDenies("internal", {"tool_name": "mcp__%s__run_query" % DB,
                                           "tool_input": {"connection_id": "prod-main", "sql": "SELECT TOP 1 * FROM t"}}, "mcp")

    def test_jira_writes(self):
        for t in JIRA_WRITES:
            with self.subTest(tool=t):
                self.assertHookDenies("internal", {"tool_name": "mcp__mcp-atlassian__" + t, "tool_input": {"issue_key": "ABC-1001"}},
                                      "mcp")
        self.assertHookAllows("internal", {"tool_name": "mcp__mcp-atlassian__jira_get_issue",
                                           "tool_input": {"issue_key": "ABC-1001"}}, "mcp")

    def test_slack(self):
        bad = [(SEND, {"channel_id": "U0SOMEONE", "message": "hi"}), (SEND, {"channel_id": "C0CHANNEL", "message": "hi"}),
               (SEND, {"channel_id": ME, "message": "hi", "thread_ts": "1.2"}),
               (SEND, {"channel_id": ME, "message": "hi", "draft_id": "x"}), (SEND, {"channel_id": ME, "message": "x" * 9000}),
               ("mcp__slack__slack_upload_file", {"channel_id": ME}), ("mcp__slack__slack_schedule_message",
                                                                        {"channel_id": ME, "message": "hi", "post_at": 1}),
               ("mcp__slack__slack_add_reaction", {"channel_id": ME}), ("mcp__slack__slack_update_message", {"channel_id": ME})]
        for tool, inp in bad:
            with self.subTest(tool=tool, inp=list(inp)):
                self.assertHookDenies("internal", {"tool_name": tool, "tool_input": inp}, "slack-send")
        self.assertHookAllows("internal", {"tool_name": SEND, "tool_input": {"channel_id": ME, "message": "Worklog for today"}},
                              "slack-send")
        self.assertHookDenies("internal", {"tool_name": SEND, "tool_input": {"channel_id": ME, "message": "x"}}, "slack-discover")

    def test_internal_call_model_attempts(self):
        for profile in ("none", "mcp", "slack-send", "slack-discover"):
            for tool, inp in (("Bash", {"command": "git commit -am x"}), ("Edit", {"file_path": "C:/x", "old_string": "a"}),
                              ("Write", {"file_path": "C:/x", "content": "x"}), ("PowerShell", {"command": "rm x"}),
                              ("WebFetch", {"url": "https://acme.test"})):
                with self.subTest(profile=profile, tool=tool):
                    self.assertHookDenies("internal", {"tool_name": tool, "tool_input": inp}, profile)

    def test_internal_guard_fails_closed(self):
        self.assertHookDenies("internal", b"not json", "mcp")
        self.assertHookDenies("internal", {"tool_name": "StructuredOutput"}, "made-up-profile")

    def test_guard_runs_this_package_not_the_stable_launcher(self):
        """An older runtime copy behind the stable launcher may not know the guard: every call would fail closed."""
        from chitragupta import plugin
        from chitragupta.safety import claude as builder
        hook = json.loads(builder.guard_settings("none"))["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(hook["args"][:3], [plugin.own_launcher(), "safety", "internal"])
        self.assertEqual(hook["onFailure"], "block")
        p = subprocess.run([hook["command"]] + hook["args"], capture_output=True, timeout=30, env=self.child_env(),
                           input=json.dumps({"tool_name": "StructuredOutput", "tool_input": {}}).encode())
        self.assertEqual((p.returncode, p.stdout), (0, b""))

    def test_ignored_repos_are_skipped_not_blocked(self):
        from chitragupta import artifacts
        other = _init_repo(os.path.join(self.scratch, "ignored"))
        open(os.path.join(other, ".worklog-ignore"), "w").close()
        n = self.log_count()
        self.assertEqual(artifacts.untracked(self.cfg(), other), [])
        self.assertFalse(artifacts.tracked_before(self.cfg(), other, "a.txt", "2026-10-16T10:00:00+00:00"))
        self.assertEqual(self.log_count(), n)

    def test_hook_process_through_the_launcher(self):
        """As Claude Code runs it: exit 2 + deny JSON, and the block lands in security.log."""
        p = subprocess.run([PY, "-B", RUN_PY, "safety", "internal", "none"], capture_output=True, timeout=30,
                           input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "dir"}}).encode(),
                           env=self.child_env())
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout.decode())["hookSpecificOutput"]["permissionDecision"], "deny")


# ---------------------------------------------------------------------------------------------- /cg: commands in my sessions

class SessionGuardTests(SafetyCase):
    """The session guard acts only for the prompt a /cg: command expanded (UserPromptExpansion records its prompt_id);
    every PreToolUse input carries the prompt_id of the prompt being processed."""

    def expand(self, prompt_id, sid="s-1", name="cg:today", prompt=None, **extra):
        """What the synchronous UserPromptExpansion hook (`hook cg-expand`) does for a typed /cg: command."""
        from chitragupta.safety import hook
        d = {"session_id": sid, "hook_event_name": "UserPromptExpansion", "expansion_type": "slash_command",
             "command_name": name, "command_args": "", "command_source": "plugin", "prompt": prompt or "/" + name}
        if prompt_id:
            d["prompt_id"] = prompt_id
        d.update(extra)
        hook.expand(d)

    def data(self, tool, inp, prompt_id="P1", sid="s-1", transcript=None):
        d = {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inp}
        if prompt_id:
            d["prompt_id"] = prompt_id
        if transcript:
            d["transcript_path"] = transcript
        return d

    def transcript(self, prompt):
        p = os.path.join(self.scratch, "t-%d.jsonl" % (len(os.listdir(self.scratch))))
        lines = [{"type": "user", "message": {"role": "user", "content": "earlier work"}},
                 {"type": "user", "message": {"role": "user", "content": prompt}}]
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(l) for l in lines) + "\n")
        return p

    def test_writes_blocked_during_a_cg_command(self):
        self.expand("P1")
        for tool, inp in (("Bash", {"command": "git commit -am x"}), ("PowerShell", {"command": "git push"}),
                          ("Bash", {"command": 'py -3 "%s" cli today; git commit -am x' % RUN_PY}),
                          ("Edit", {"file_path": os.path.join(self.repo, "a.txt"), "old_string": "one", "new_string": "x"}),
                          ("Write", {"file_path": os.path.join(self.repo, "b.txt"), "content": "x"}),
                          ("mcp__%s__run_query" % DB, {"connection_id": "qa-main", "sql": "UPDATE shop.orders SET a = 1"}),
                          ("mcp__mcp-atlassian__jira_transition_issue", {"issue_key": "ABC-1001"}),
                          (SEND, {"channel_id": ME, "message": "x"}), ("Agent", {"prompt": "x"}),
                          ("WebFetch", {"url": "https://acme.test"})):
            with self.subTest(tool=tool, inp=str(inp)[:50]):
                self.assertHookDenies("session", self.data(tool, inp))
        self.expand("P2", name="cg:check", prompt="/cg:check ABC-1001")
        self.assertHookDenies("session", self.data("Bash", {"command": "git merge x"}, prompt_id="P2"))

    def test_reads_and_the_helper_pass_during_a_cg_command(self):
        self.expand("P1", name="cg:ticket")
        for tool, inp in (("Read", {"file_path": os.path.join(self.home, "inbox.md")}), ("Grep", {"pattern": "x"}),
                          ("Bash", {"command": 'py -3 "%s" cli ticket ABC-1001' % RUN_PY}),
                          ("mcp__%s__run_query" % DB, {"connection_id": "qa-main", "sql": "SELECT TOP 5 * FROM shop.orders"})):
            with self.subTest(tool=tool):
                self.assertHookAllows("session", self.data(tool, inp))

    def test_my_own_work_is_untouched_outside_cg_commands(self):
        for tool, inp in (("Bash", {"command": "git commit -am x"}), ("Bash", {"command": "git push --force"}),
                          ("Edit", {"file_path": os.path.join(self.repo, "a.txt")}),
                          ("mcp__%s__run_query" % DB, {"sql": "UPDATE t SET a = 1"})):
            with self.subTest(tool=tool):
                n = self.log_count()
                self.assertHookAllows("session", self.data(tool, inp))
                self.assertEqual(self.log_count(), n)

    def test_a_after_a_cg_prompt_the_next_prompt_is_not_guarded(self):
        """Audit scenario: /cg: prompt P1 marked; the next normal prompt P2 (mark still there, transcript lagging)."""
        self.expand("P1")
        lag = self.transcript("<command-name>/cg:today</command-name>")  # the transcript still ends with the /cg: prompt
        for tool, inp in (("Bash", {"command": "git commit -am x"}), ("Edit", {"file_path": os.path.join(self.repo, "a.txt")}),
                          ("mcp__plugin_other_tool__panel_sync", {"x": 1})):
            with self.subTest(tool=tool):
                n = self.log_count()
                self.assertHookAllows("session", self.data(tool, inp, prompt_id="P2", transcript=lag))
                self.assertEqual(self.log_count(), n)

    def test_b_same_prompt_is_guarded(self):
        self.expand("P1")
        self.assertHookDenies("session", self.data("Bash", {"command": "git commit -am x"}, prompt_id="P1"))
        self.assertHookAllows("session", self.data("Grep", {"pattern": "x"}, prompt_id="P1"))

    def test_c_interrupted_cg_command_then_a_new_prompt(self):
        """Esc during a /cg: command: no Stop, the mark stays; the next prompt has a new prompt_id."""
        self.expand("P1")
        self.assertHookAllows("session", self.data("Bash", {"command": "git commit -am x"}, prompt_id="P2"))

    def test_d_resumed_session(self):
        self.expand("P1", sid="s-r")
        self.assertHookAllows("session", self.data("Bash", {"command": "git push"}, prompt_id="P7", sid="s-r"))
        self.assertHookAllows("session", self.data("Bash", {"command": "git push"}, prompt_id="P1", sid="s-other"))

    def test_e_no_prompt_id_is_not_guarded(self):
        from chitragupta.safety import hook
        self.expand("P1")
        self.assertHookAllows("session", self.data("Bash", {"command": "git commit -am x"}, prompt_id=None))
        # an expansion without a prompt_id leaves nothing that could match later
        self.expand(None, sid="s-2")
        self.assertFalse(os.path.exists(hook.mark_path("s-2")))
        self.assertHookAllows("session", self.data("Bash", {"command": "git commit -am x"}, prompt_id=None, sid="s-2"))

    def test_transcript_alone_no_longer_activates(self):
        t = self.transcript("<command-name>/cg:today</command-name>")
        self.assertHookAllows("session", self.data("Bash", {"command": "git commit -am x"}, transcript=t))

    def test_only_cg_commands_mark(self):
        from chitragupta.safety import hook
        for name, prompt in (("other:today", "/other:today"), ("today", "/today"), ("review", "/review")):
            with self.subTest(name=name):
                self.expand("P1", name=name, prompt=prompt)
                self.assertFalse(os.path.exists(hook.mark_path("s-1")))
        self.expand("P1", name="today", prompt="/cg:today")  # a plain command name with the plugin prefix in the prompt
        self.assertHookDenies("session", self.data("Bash", {"command": "git commit -am x"}))

    def test_stop_clears_only_its_own_prompt(self):
        from chitragupta.safety import hook
        self.expand("P2")
        hook.clear("s-1", "P1")  # a late Stop of the previous turn must not lift the guard of this one
        self.assertHookDenies("session", self.data("Bash", {"command": "git commit -am x"}, prompt_id="P2"))
        hook.clear("s-1", "P2")
        self.assertFalse(os.path.exists(hook.mark_path("s-1")))
        self.assertHookAllows("session", self.data("Bash", {"command": "git commit -am x"}, prompt_id="P2"))

    def test_hooks_json_registers_a_synchronous_expansion_hook_for_cg_commands_only(self):
        import re
        from tests.support import ROOT
        with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as f:
            hooks = json.load(f)["hooks"]
        groups = hooks["UserPromptExpansion"]
        self.assertEqual(len(groups), 1)
        m = re.compile(groups[0]["matcher"])
        for name in os.listdir(os.path.join(ROOT, "skills")):
            with self.subTest(skill=name):
                self.assertTrue(m.search("cg:" + name))
        for other in ("other:today", "deploy", "cg:unknown", "xcg:today", "today-extra"):
            self.assertFalse(m.search(other), other)
        h = groups[0]["hooks"]
        self.assertEqual(len(h), 1)
        self.assertNotIn("async", h[0])
        self.assertEqual(h[0]["args"][-2:], ["hook", "cg-expand"])
        self.assertLessEqual(h[0]["timeout"], 5)
        # nothing marks /cg: activity on UserPromptSubmit any more
        self.assertNotIn("safety", json.dumps(hooks["UserPromptSubmit"]))

    def test_through_the_launcher_and_fast(self):
        """As Claude Code runs it: `hook cg-expand` records the prompt, then `safety session` guards only that prompt."""
        from tests.support import run_hook
        env = self.child_env()
        exp = {"session_id": "s-l", "prompt_id": "P1", "hook_event_name": "UserPromptExpansion",
               "expansion_type": "slash_command", "command_name": "cg:today", "command_source": "plugin",
               "prompt": "/cg:today"}
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            p = run_hook("cg-expand", exp, env)
            times.append((time.perf_counter() - t0) * 1000)
            self.assertEqual((p.returncode, p.stdout), (0, b""))
        times.sort()
        print("\ncg-expand hook median = %.0f ms" % times[len(times) // 2])
        self.assertLess(times[len(times) // 2], 400)
        bad = json.dumps(self.data("Bash", {"command": "git commit -am x"}, prompt_id="P1", sid="s-l")).encode()
        p = subprocess.run([PY, "-B", RUN_PY, "safety", "session"], input=bad, capture_output=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 2)
        nxt = json.dumps(self.data("Bash", {"command": "git commit -am x"}, prompt_id="P2", sid="s-l")).encode()
        p = subprocess.run([PY, "-B", RUN_PY, "safety", "session"], input=nxt, capture_output=True, env=env, timeout=30)
        self.assertEqual((p.returncode, p.stdout), (0, b""))

    def test_session_guard_is_fast_when_idle(self):
        """Outside /cg: commands the hook process returns at once (spawn + import + one small file check)."""
        env = self.child_env()
        data = json.dumps(self.data("Bash", {"command": "git status"})).encode()
        times = []
        for _ in range(7):
            t0 = time.perf_counter()
            p = subprocess.run([PY, "-B", RUN_PY, "safety", "session"], input=data, capture_output=True, env=env, timeout=30)
            times.append((time.perf_counter() - t0) * 1000)
            self.assertEqual((p.returncode, p.stdout), (0, b""))
        times.sort()
        print("\nsession guard (idle) median = %.0f ms" % times[len(times) // 2])
        self.assertLess(times[len(times) // 2], 400)


class SessionLauncherFailOpenTests(SafetyCase):
    """The session guard must not block my session when the launcher itself can't load the package: the skills'
    guard hook has no onFailure: block, and `run.py safety session` exits 0 silently. (The internal guard keeps
    onFailure: block and fails closed.)"""

    BAD = {"session_id": "s-1", "prompt_id": "P1", "tool_name": "Bash", "tool_input": {"command": "git commit -am x"}}

    def launcher_copy(self, broken_package):
        import shutil
        root = tempfile.mkdtemp(prefix="plugin-copy-", dir=self.scratch)
        os.makedirs(os.path.join(root, "hooks"))
        shutil.copy(RUN_PY, os.path.join(root, "hooks", "run.py"))
        if broken_package:
            os.makedirs(os.path.join(root, "chitragupta"))
            with open(os.path.join(root, "chitragupta", "__init__.py"), "w") as f:
                f.write("raise ImportError('simulated broken install')\n")
        return os.path.join(root, "hooks", "run.py")

    def test_session_guard_exits_0_silently_when_the_package_cannot_load(self):
        for broken in (False, True):  # no package next to the launcher / a package that fails to import
            with self.subTest(broken_package=broken):
                run = self.launcher_copy(broken)
                p = subprocess.run([PY, "-B", run, "safety", "session"], input=json.dumps(self.BAD).encode(),
                                   capture_output=True, timeout=30, env=self.child_env())
                self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))

    def test_other_launcher_commands_still_report_a_missing_package(self):
        run = self.launcher_copy(False)
        p = subprocess.run([PY, "-B", run, "safety", "internal", "none"], input=b"{}", capture_output=True, timeout=30,
                           env=self.child_env())
        self.assertNotEqual(p.returncode, 0)  # internal calls also carry onFailure: block

    def test_skills_register_the_session_guard_without_on_failure_block(self):
        import glob
        from tests.support import ROOT
        from chitragupta.safety import status
        files = sorted(glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md")))
        self.assertTrue(files)
        for p in files:
            with self.subTest(skill=p):
                with open(p, encoding="utf-8") as f:
                    text = f.read()
                self.assertIn('"safety", "session"]', text)
                self.assertFalse("onFailure" in text, "the session guard must not block when its launcher fails")
        self.assertEqual(status.unguarded_skills(), [])


class OwnMcpServerTests(SafetyCase):
    """During a /cg: command Chitragupta's own read-only MCP server (plugin `cg`, server `chitragupta`) needs no
    allow.json entry; nothing else about the session profile changes."""

    OWN = "mcp__plugin_cg_chitragupta__"

    def setUp(self):
        super().setUp()
        from chitragupta.safety import hook
        hook.expand({"session_id": "s-1", "prompt_id": "P1", "command_name": "cg:ticket", "prompt": "/cg:ticket ABC-1001"})

    def data(self, tool, inp=None, server=None):
        d = {"session_id": "s-1", "prompt_id": "P1", "tool_name": tool, "tool_input": inp or {}}
        if server is not None:
            d["mcp_server"] = server
        return d

    def test_every_own_read_tool_passes_without_allow_json(self):
        from chitragupta import mcpserver
        from chitragupta.safety import mcp
        self.assertNotIn("plugin_cg_chitragupta", (mcp.load_allow().get("servers") or {}))
        self.assertTrue(mcpserver.TOOL_NAMES)
        for name in mcpserver.TOOL_NAMES:
            for server in (None, {"name": "plugin:cg:chitragupta", "source": "plugin"}):
                with self.subTest(tool=name, server=server):
                    self.assertHookAllows("session", self.data(self.OWN + name, {"key": "ABC-1001"}, server))

    def test_own_tool_list_is_read_only_and_pinned(self):
        """The session allowlist is exactly the server's tool list, and none of those names says it writes."""
        from chitragupta import mcpserver
        from chitragupta.safety import mcp
        self.assertEqual(sorted(mcp.OWN_MCP_TOOLS), sorted(mcpserver.TOOL_NAMES))
        for name in mcpserver.TOOL_NAMES:
            self.assertFalse(mcp.writes(name), name)

    def test_unknown_or_spoofed_own_tools_are_denied(self):
        self.assertHookDenies("session", self.data(self.OWN + "delete_ticket"))
        self.assertHookDenies("session", self.data(self.OWN + "run_anything"))
        # a server that only borrows the name: Claude Code says it isn't a plugin server
        self.assertHookDenies("session", self.data(self.OWN + "get_ticket", {"key": "ABC-1001"},
                                                   {"name": "plugin_cg_chitragupta", "source": "user"}))

    def test_internal_profiles_still_refuse_it(self):
        from chitragupta.safety import mcp
        for profile in ("none", "mcp", "slack-send", "slack-discover"):
            with self.subTest(profile=profile):
                self.assertIsNotNone(mcp.reason(profile, self.OWN + "get_ticket", {"key": "ABC-1001"}, cfg=self.cfg()))


class OtherPluginToolTests(SafetyCase):
    """Policy A: during a /cg: command another plugin's MCP tool is still denied, but it is logged as category
    `other-plugin` and not counted as a violation on Health / doctor."""

    OTHER = "mcp__plugin_other_tool__panel_sync"
    SKIP = "not a Chitragupta tool; skip this call and don't retry it"

    def setUp(self):
        super().setUp()
        from chitragupta.safety import hook
        hook.expand({"session_id": "s-1", "prompt_id": "P1", "command_name": "cg:today", "prompt": "/cg:today"})

    def data(self, tool, server=None, prompt_id="P1"):
        d = {"session_id": "s-1", "prompt_id": prompt_id, "tool_name": tool, "tool_input": {"x": 1}}
        if server is not None:
            d["mcp_server"] = server
        return d

    def last(self):
        from chitragupta.safety import log
        return log.entries(1)[-1]

    def test_denied_with_a_skip_reason_and_logged_as_other_plugin(self):
        for server in ({"name": "plugin:other:tool", "source": "plugin"}, None):  # by provenance, else by name
            with self.subTest(server=server):
                why = self.assertHookDenies("session", self.data(self.OTHER, server))
                self.assertIn(self.SKIP, why)
                e = self.last()
                self.assertEqual((e["layer"], e.get("category")), ("tool", "other-plugin"))
                self.assertIn(self.OTHER, e["what"])

    def test_other_servers_are_ordinary_violations(self):
        for tool, server in (("mcp__mcp-atlassian__jira_transition_issue", {"name": "mcp-atlassian", "source": "user"}),
                             ("mcp__plugin_other_tool__panel_sync", {"name": "plugin_other_tool", "source": "project"}),
                             ("Bash", None)):
            with self.subTest(tool=tool):
                why = self.assertHookDenies("session", dict(self.data(tool, server), tool_input={"command": "git push"}))
                self.assertNotIn(self.SKIP, why)
                self.assertNotIn("category", self.last())

    def test_outside_a_cg_command_other_plugins_are_untouched(self):
        n = self.log_count()
        self.assertHookAllows("session", self.data(self.OTHER, {"name": "plugin:other:tool", "source": "plugin"}, "P2"))
        self.assertEqual(self.log_count(), n)

    def test_not_counted_on_health_or_doctor(self):
        from chitragupta.safety import log, status
        self.assertHookDenies("session", self.data(self.OTHER, {"name": "plugin:other:tool", "source": "plugin"}))
        s = log.summary(7)
        self.assertEqual((s["total"], s["recent"], s["by_layer"], s["last"]), (0, 0, {}, []))
        self.assertEqual(s["other_plugin"], 1)
        st = status.status(self.cfg())
        self.assertEqual((st["blocks_24h"], st["blocks_7d"]), (0, 0))
        rows = {r["name"]: r for r in status.doctor_rows(self.cfg())}
        self.assertTrue(rows["security log"]["ok"], rows["security log"])
        # a real violation still counts
        self.assertHookDenies("session", self.data("Bash"))
        s = log.summary(7)
        self.assertEqual((s["recent"], s["by_layer"], len(s["last"])), (1, {"tool": 1}, 1))
        self.assertFalse({r["name"]: r for r in status.doctor_rows(self.cfg())}["security log"]["ok"])


class SecurityLogFormatTests(SafetyCase):
    def test_timestamp_carries_the_local_utc_offset(self):
        from chitragupta.safety import log
        log.record("path", "test", "write C:/x", "write", "tests.caller")
        self.assertRegex(log.entries(1)[-1]["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")

    def test_caller_comes_from_the_stack_when_not_given(self):
        from chitragupta.safety import log, paths
        log.record("path", "test", "write C:/x")
        self.assertEqual(log.entries(1)[-1]["caller"], __name__)
        outside = os.path.join(os.path.dirname(self.scratch), "elsewhere-%d.txt" % os.getpid())
        self.assertBlocked(lambda: paths.write_text(outside, "x"), "path")  # through the guard: its own frames skipped
        self.assertEqual(log.entries(1)[-1]["caller"], __name__)
        log.record("path", "test", "write C:/x", caller="tests.explicit")
        self.assertEqual(log.entries(1)[-1]["caller"], "tests.explicit")

    def test_summary_counts_old_and_new_timestamps(self):
        import datetime as dt
        from chitragupta.safety import log
        now = dt.datetime.now().astimezone()
        other = dt.timezone(now.utcoffset() - dt.timedelta(hours=6))  # reads 6 h earlier than local time
        ats = [(now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),          # old format, recent
               (now - dt.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S"),           # old format, 3 days
               (now - dt.timedelta(hours=2)).isoformat(timespec="seconds"),          # new format, recent
               (now - dt.timedelta(hours=20)).astimezone(other).isoformat(timespec="seconds"),  # recent, other offset
               (now - dt.timedelta(days=3)).astimezone(other).isoformat(timespec="seconds")]
        os.makedirs(os.path.dirname(log.path()), exist_ok=True)
        with open(log.path(), "w", encoding="utf-8") as f:
            for at in ats:
                f.write(json.dumps({"at": at, "layer": "path", "caller": "", "purpose": "", "what": "x", "reason": "r"}) + "\n")
        self.assertEqual((log.summary(7)["total"], log.summary(7)["recent"]), (5, 5))
        self.assertEqual(log.summary(1)["recent"], 3)


class LateCleanupIsolationTests(unittest.TestCase):
    """tests/support.py: restore() runs as the last cleanup, after every cleanup a test registered, and the whole run
    has one sandbox user folder, so a late cleanup can never reach the real one."""

    def test_the_run_has_a_sandbox_user_folder(self):
        real = os.path.join(os.path.expanduser("~"), ".chitragupta")
        for k in ("CHITRAGUPTA_USER_DIR", "CHITRAGUPTA_HOME"):
            self.assertTrue(os.environ.get(k), k)
        self.assertNotEqual(os.path.normcase(os.path.abspath(os.environ["CHITRAGUPTA_USER_DIR"])), os.path.normcase(real))

    def test_a_guarded_cleanup_registered_the_old_way_stays_in_the_test_sandbox(self):
        from chitragupta.safety import log
        outer = log.path()
        size = os.path.getsize(outer) if os.path.exists(outer) else 0
        seen = {}

        class Inner(Isolated, unittest.TestCase):
            def setUp(self):
                self.isolate()

            def tearDown(self):
                self.restore()  # the old way: before the test's own cleanups

            def test_late(self):
                seen["log"], seen["user"] = log.path(), self.user_dir
                self.addCleanup(log.record, "path", "late cleanup", "write C:/x", "write", "tests.late")

        res = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Inner).run(res)
        self.assertEqual((res.errors, res.failures), ([], []))
        self.assertNotEqual(seen["log"], outer)
        self.assertEqual(os.path.getsize(outer) if os.path.exists(outer) else 0, size, "the late cleanup reached outside")
        self.assertFalse(os.path.exists(seen["user"]), "the temp user folder was not removed")


# ---------------------------------------------------------------------------------------------- allowed actions

class AllowedTests(SafetyCase):
    def test_work_repo_reads_and_fetch(self):
        from chitragupta import gitro
        remote = os.path.join(self.scratch, "remote.git")
        subprocess.run([GIT, "clone", "-q", "--bare", self.repo, remote], check=True, capture_output=True)
        _git(self.repo, "remote", "add", "origin", remote)
        cfg = self.cfg()
        head = _git(self.repo, "rev-parse", "HEAD").strip()
        for args in (["rev-parse", "HEAD"], ["for-each-ref", "refs/heads"], ["log", "-1", "--format=%H"],
                     ["rev-list", "--count", "HEAD"], ["merge-base", "HEAD", "HEAD"], ["cherry", "HEAD", "HEAD"],
                     ["show", "-s", "--format=%s", "HEAD"], ["reflog", "-n", "1"], ["branch", "--list"],
                     ["branch", "--contains", head], ["ls-remote", "--heads", "origin"], ["remote", "get-url", "origin"],
                     ["config", "--get", "remote.origin.url"], ["ls-files", "--others", "--exclude-standard"]):
            with self.subTest(args=args):
                rc, so, se = gitro.run(cfg, self.repo, args)
                self.assertEqual(rc, 0, se)
        ok, msg = gitro.fetch(cfg, self.repo, "origin", ["main"])
        self.assertTrue(ok, msg)
        self.assertEqual(_git(self.repo, "rev-parse", "refs/remotes/origin/main").strip(), head)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "FETCH_HEAD")))

    def test_data_home_commit_and_push(self):
        from chitragupta import common, gitsync
        remote = os.path.join(self.scratch, "daily.git")
        subprocess.run([GIT, "init", "-q", "--bare", "-b", "main", remote], check=True, capture_output=True)
        common.save_config(dict(self.cfg_dict, github_remote=remote))
        install_launcher(self.user_dir)  # the data home's pre-commit hook runs the stable launcher
        cfg = self.cfg()
        gitsync.ensure_repo(cfg)
        common.write_text(common.hp("daily", "2026", "10", "2026-10-16.md"), "# 2026-10-16\n\nWorked on ABC-1001.\n")
        common.write_text(common.hp("tickets", "ABC-1001", "README.md"), "local only\n")
        health = {}
        self.assertTrue(gitsync.commit(cfg, "worklog(2026-10-16): test", health))
        self.assertTrue(gitsync.push(cfg, health), health)
        files = subprocess.run([GIT, "-C", remote, "ls-tree", "-r", "--name-only", "main"], capture_output=True,
                               text=True).stdout.split()
        self.assertIn("daily/2026/10/2026-10-16.md", files)
        self.assertNotIn("tickets/ABC-1001/README.md", files)

    def test_host_reads_and_task_repair(self):
        from chitragupta import common
        from chitragupta.safety import commands
        cfg = self.cfg()
        for argv in (["gh", "pr", "list", "--repo", "o/r", "--json", "number"], ["gh", "pr", "view", "1"],
                     ["gh", "pr", "checks", "1"], ["gh", "repo", "view"], ["gh", "auth", "status"],
                     ["gh", "api", "repos/o/r/pulls"], ["gh", "api", "-X", "GET", "repos/o/r"],
                     ["glab", "mr", "list", "--output", "json"], ["glab", "mr", "view", "1"],
                     ["az", "repos", "pr", "list", "--output", "json"], ["az", "repos", "pr", "show", "--id", "1", "-o", "json"],
                     ["az", "repos", "ref", "list", "--output", "json"], ["az", "boards", "work-item", "show", "--id", "1",
                                                                          "--output", "json"],
                     ["az", "account", "show", "--output", "json"]):
            with self.subTest(argv=argv):
                self.assertIsNone(commands.reason(argv, "host", cfg=cfg))
        xml = common.wl("cache", "Chitragupta.xml")
        self.assertIsNone(commands.reason(["schtasks", "/create", "/tn", "Chitragupta", "/xml", xml, "/f"], "task", cfg=cfg))
        self.assertIsNone(commands.reason(["schtasks", "/query", "/tn", "Chitragupta", "/fo", "LIST", "/v"], "task", cfg=cfg))

    def test_slack_dm_to_me_and_builder_shape(self):
        from chitragupta import mcpjobs, slack
        from chitragupta.safety import commands
        real = mcpjobs.claude_exe
        mcpjobs.claude_exe = lambda cfg: ["claude"]
        self.addCleanup(setattr, mcpjobs, "claude_exe", real)
        a = slack._args(self.cfg(), "Send", [SEND], "strict", disallowed=["mcp__slack__slack_read_channel"])
        self.assertIsNone(commands.reason(a, "claude", cwd=slack._cwd("strict"), cfg=self.cfg()))
        self.assertIn('"slack-send"', a[a.index("--settings") + 1])

    def test_doctor_says_enforced(self):
        from chitragupta.safety import status
        s = status.status(self.cfg(), "2.1.302 (Claude Code)")
        self.assertTrue(s["enforced"], s["problems"])
        self.assertTrue(s["skills_checked"])
        self.assertFalse(status.status(self.cfg(), "2.1.200 (Claude Code)")["enforced"])


if __name__ == "__main__":
    unittest.main()
