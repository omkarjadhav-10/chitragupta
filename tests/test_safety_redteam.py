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
    def transcript(self, prompt):
        p = os.path.join(self.scratch, "t-%d.jsonl" % (len(os.listdir(self.scratch))))
        lines = [{"type": "user", "message": {"role": "user", "content": "earlier work"}},
                 {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
                 {"type": "user", "message": {"role": "user", "content": prompt}},
                 {"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "skill body"}]}},
                 {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}}]
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(l) for l in lines) + "\n")
        return p

    def data(self, prompt, tool, inp, sid="s-1"):
        return {"session_id": sid, "transcript_path": self.transcript(prompt), "tool_name": tool, "tool_input": inp}

    def test_writes_blocked_during_a_cg_command(self):
        cg = "<command-message>cg:today</command-message>\n<command-name>/cg:today</command-name>"
        for tool, inp in (("Bash", {"command": "git commit -am x"}), ("PowerShell", {"command": "git push"}),
                          ("Bash", {"command": 'py -3 "%s" cli today; git commit -am x' % RUN_PY}),
                          ("Edit", {"file_path": os.path.join(self.repo, "a.txt"), "old_string": "one", "new_string": "x"}),
                          ("Write", {"file_path": os.path.join(self.repo, "b.txt"), "content": "x"}),
                          ("mcp__%s__run_query" % DB, {"connection_id": "qa-main", "sql": "UPDATE shop.orders SET a = 1"}),
                          ("mcp__mcp-atlassian__jira_transition_issue", {"issue_key": "ABC-1001"}),
                          (SEND, {"channel_id": ME, "message": "x"}), ("Agent", {"prompt": "x"}),
                          ("WebFetch", {"url": "https://acme.test"})):
            with self.subTest(tool=tool, inp=str(inp)[:50]):
                self.assertHookDenies("session", self.data(cg, tool, inp))
        self.assertHookDenies("session", self.data("/cg:check ABC-1001", "Bash", {"command": "git merge x"}))

    def test_reads_and_the_helper_pass_during_a_cg_command(self):
        cg = "<command-name>/cg:ticket</command-name>"
        for tool, inp in (("Read", {"file_path": os.path.join(self.home, "inbox.md")}), ("Grep", {"pattern": "x"}),
                          ("Bash", {"command": 'py -3 "%s" cli ticket ABC-1001' % RUN_PY}),
                          ("mcp__%s__run_query" % DB, {"connection_id": "qa-main", "sql": "SELECT TOP 5 * FROM shop.orders"})):
            with self.subTest(tool=tool):
                self.assertHookAllows("session", self.data(cg, tool, inp))

    def test_my_own_work_is_untouched_outside_cg_commands(self):
        for prompt in ("commit and push this", "please run /cg:today later", "fix the bug in a.txt"):
            for tool, inp in (("Bash", {"command": "git commit -am x"}), ("Bash", {"command": "git push --force"}),
                              ("Edit", {"file_path": os.path.join(self.repo, "a.txt")}),
                              ("mcp__%s__run_query" % DB, {"sql": "UPDATE t SET a = 1"})):
                with self.subTest(prompt=prompt, tool=tool):
                    n = self.log_count()
                    self.assertHookAllows("session", self.data(prompt, tool, inp))
                    self.assertEqual(self.log_count(), n)

    def test_user_prompt_submit_mark_and_stop_clear(self):
        from chitragupta.safety import hook
        hook.mark("s-9", "/cg:today")
        d = self.data("normal prompt", "Bash", {"command": "git commit -am x"}, sid="s-9")
        self.assertHookDenies("session", d)  # the mark alone is enough (transcript not flushed yet)
        hook.clear("s-9")
        self.assertHookAllows("session", self.data("normal prompt", "Bash", {"command": "git commit -am x"}, sid="s-9"))
        hook.mark("s-9", "/cg:today")
        hook.mark("s-9", "now something else")  # the next normal prompt clears it too
        self.assertHookAllows("session", self.data("normal prompt", "Bash", {"command": "git commit"}, sid="s-9"))

    def test_session_guard_is_fast_when_idle(self):
        """Outside /cg: commands the hook process returns at once (spawn + import + one transcript tail)."""
        env = self.child_env()
        data = json.dumps(self.data("ordinary work", "Bash", {"command": "git status"})).encode()
        times = []
        for _ in range(7):
            t0 = time.perf_counter()
            p = subprocess.run([PY, "-B", RUN_PY, "safety", "session"], input=data, capture_output=True, env=env, timeout=30)
            times.append((time.perf_counter() - t0) * 1000)
            self.assertEqual((p.returncode, p.stdout), (0, b""))
        times.sort()
        print("\nsession guard (idle) median = %.0f ms" % times[len(times) // 2])
        self.assertLess(times[len(times) // 2], 400)


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
