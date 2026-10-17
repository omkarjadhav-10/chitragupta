"""Plugin glue: set-up gating in the hooks, the maintenance spawn, the weekly MCP reminder."""
import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import time
import unittest

from tests.support import Isolated, rmtree, run_hook, write_config
from tests.support import wr as _wr

MSG = "Chitragupta isn't set up yet — run /cg:setup to finish."


def wr(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    _wr(p, text)


def files_under(root):
    return [os.path.join(dp, f) for dp, _, fs in os.walk(root) for f in fs]


class PluginCase(Isolated, unittest.TestCase):
    configured = True

    def setUp(self):
        self.isolate()
        if self.configured:
            write_config(self.home, {"ticket_prefixes": ["ABC"]})
        os.environ["CHITRAGUPTA_DASHBOARD"] = "0"
        from chitragupta import plugin
        self.plugin = plugin

    def tearDown(self):
        self.restore()

    def env(self, **extra):
        base = dict(CHITRAGUPTA_NO_MAINTENANCE="1", CHITRAGUPTA_DASHBOARD="0", CHITRAGUPTA_CLAUDE_DIR=self.claude_dir)
        base.update(extra)
        return self.child_env(**base)

    def repo(self, branch="feature/ABC-7-x"):
        d = tempfile.mkdtemp(prefix="cg-repo-")
        os.makedirs(os.path.join(d, ".git"))
        wr(os.path.join(d, ".git", "HEAD"), "ref: refs/heads/%s\n" % branch)
        self.addCleanup(rmtree, d)
        return d


class NotSetUpTests(PluginCase):
    configured = False

    def test_session_start_prints_one_line_and_nothing_else_runs(self):
        p = run_hook("session-start", {"session_id": "s1", "cwd": self.repo()}, self.env())
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout.decode())
        self.assertEqual(out, {"systemMessage": MSG,
                               "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": MSG}})
        self.assertEqual(files_under(self.home), [])

    def test_other_events_write_nothing(self):
        repo = self.repo()
        for ev, extra in (("user-prompt", {"user_prompt": "x"}), ("post-tool", {"tool_name": "Bash"}),
                          ("stop", {"last_assistant_message": "y"}), ("session-end", {})):
            p = run_hook(ev, dict({"session_id": "s1", "cwd": repo}, **extra), self.env())
            self.assertEqual((p.returncode, p.stdout), (0, b""), ev)
        p = run_hook("stop", None, self.env(), raw_input=b"not json{")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(files_under(self.home), [])

    def test_is_set_up_needs_a_chosen_home_and_its_config(self):
        self.assertFalse(self.plugin.is_set_up())  # CHITRAGUPTA_HOME set, no config
        write_config(self.home, {})
        self.assertTrue(self.plugin.is_set_up())
        del os.environ["CHITRAGUPTA_HOME"]  # no env, no pointer: the default home is never "set up"
        self.assertFalse(self.plugin.is_set_up())
        wr(os.path.join(self.user_dir, "home"), self.home + "\n")
        self.assertTrue(self.plugin.is_set_up())


class SetUpHookTests(PluginCase):
    def test_session_start_output_unchanged_when_set_up(self):
        p = run_hook("session-start", {"session_id": "s1", "cwd": self.repo()}, self.env())
        out = json.loads(p.stdout.decode())["hookSpecificOutput"]
        self.assertEqual(out["sessionTitle"].split(" ")[0], "ABC-7")
        self.assertNotIn("systemMessage", json.loads(p.stdout.decode()))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".worklog", "cache", "maintenance.stamp")))

    def test_reminder_reaches_the_context(self):
        wr(os.path.join(self.home, ".worklog", "cache", "mcp-status.json"),
           json.dumps({"slack": {"status": "missing"}, "jira": {"status": "connected"}}))
        p = run_hook("session-start", {"session_id": "s1", "cwd": self.repo()}, self.env())
        ctx = json.loads(p.stdout.decode())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Slack isn't connected", ctx)
        p = run_hook("session-start", {"session_id": "s2", "cwd": self.repo()}, self.env())
        self.assertNotIn("additionalContext", json.loads(p.stdout.decode())["hookSpecificOutput"])


class ReminderTests(PluginCase):
    def cache(self, slack, jira):
        wr(os.path.join(self.home, ".worklog", "cache", "mcp-status.json"),
           json.dumps({"slack": {"status": slack}, "jira": {"status": jira}}))

    def test_weekly(self):
        self.assertEqual(self.plugin.mcp_reminder(), "")  # no cache: never probes, says nothing
        self.cache("missing", "signed out")
        line = self.plugin.mcp_reminder()
        self.assertIn("Slack and Jira aren't connected", line)
        self.assertNotIn("\n", line)
        self.assertEqual(self.plugin.mcp_reminder(), "")
        later = self.plugin.common.now() + dt.timedelta(days=8)
        self.assertIn("Slack and Jira", self.plugin.mcp_reminder(now=later))

    def test_quiet_when_connected(self):
        self.cache("connected", "connected")
        self.assertEqual(self.plugin.mcp_reminder(), "")

    def test_switched_off_in_config(self):
        self.cache("missing", "missing")
        from chitragupta import setup
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(["reminders", "off"]), 0)
        self.assertEqual(self.plugin.mcp_reminder(), "")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(["reminders", "on"]), 0)
        self.assertIn("Slack and Jira", self.plugin.mcp_reminder())


class MaintenanceTests(PluginCase):
    def test_spawn_is_detached_windowless_and_throttled(self):
        calls = []
        self.assertTrue(self.plugin.spawn_maintenance(popen=lambda argv, **kw: calls.append((argv, kw))))
        argv, kw = calls[0]
        self.assertEqual(argv[-2:], ["plugin", "maintain"])
        self.assertTrue(argv[1].endswith("run.py"))
        self.assertEqual(kw["cwd"], self.home)
        self.assertEqual(kw["env"]["CHITRAGUPTA_INTERNAL"], "1")
        if os.name == "nt":
            self.assertEqual(kw["creationflags"] & 0x08000008, 0x08000008)  # CREATE_NO_WINDOW | DETACHED_PROCESS
        self.assertFalse(self.plugin.spawn_maintenance(popen=lambda argv, **kw: calls.append(argv)))  # stamp is fresh
        self.assertEqual(len(calls), 1)
        os.environ["CHITRAGUPTA_NO_MAINTENANCE"] = "1"
        os.remove(os.path.join(self.home, ".worklog", "cache", "maintenance.stamp"))
        self.assertFalse(self.plugin.spawn_maintenance(popen=lambda argv, **kw: calls.append(argv)))

    def test_maintain_refreshes_runtime_and_mcp_cache(self):
        cache = os.path.join(self.home, ".worklog", "cache", "mcp-status.json")
        wr(cache, "{}")
        old = time.time() - 2 * 86400
        os.utime(cache, (old, old))
        refreshed = []

        def refresh():
            refreshed.append(1)
            wr(cache, "{}")  # what setup.mcp_status does
        done = self.plugin.maintain(mcp_refresh=refresh)
        self.assertTrue(any(d.startswith("runtime") for d in done), done)
        self.assertIn("dashboard", " ".join(done))  # ensure() called, autostart off in tests
        self.assertTrue(self.plugin.runtime_current())
        self.assertEqual(refreshed, [1])
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "maintenance.lock")))
        again = self.plugin.maintain(mcp_refresh=refresh)  # runtime current, cache fresh
        self.assertFalse([d for d in again if d.startswith(("runtime", "mcp"))], again)
        self.assertEqual(refreshed, [1])

    def test_lock_keeps_one_run(self):
        os.makedirs(self.user_dir, exist_ok=True)
        wr(os.path.join(self.user_dir, "maintenance.lock"), "1 now\n")
        self.assertEqual(self.plugin.maintain(), ["another maintenance run is active"])


class PathTests(PluginCase):
    def test_claude_dir_precedence(self):
        self.assertEqual(self.plugin.claude_dir(), os.path.abspath(self.claude_dir))
        self.assertEqual(self.plugin.claude_json_path(), os.path.join(os.path.abspath(self.claude_dir), ".claude.json"))
        del os.environ["CHITRAGUPTA_CLAUDE_DIR"]
        saved = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.user_dir
        try:
            self.assertEqual(self.plugin.skill_path(),
                             os.path.join(os.path.abspath(self.user_dir), "skills", "chitragupta", "SKILL.md"))
        finally:
            if saved is None:
                del os.environ["CLAUDE_CONFIG_DIR"]
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = saved


if __name__ == "__main__":
    unittest.main()
