"""setup / doctor / uninstall / task XML / MCP status. Every schtasks, git and claude call is faked; Claude Code's
config folder and .claude.json live in the temp user folder (CHITRAGUPTA_CLAUDE_DIR)."""
import json
import os
import tempfile
import unittest

from tests.support import Isolated, rd, rmtree
from tests.support import wr as _wr

MCP_LIST = """Checking MCP server health\u2026

claude.ai Slack: https://mcp.example.test/slack - ! Needs authentication
mcp-atlassian: uvx mcp-atlassian - \u2714 Connected
sqldb: https://db.acme.test/mcp (HTTP) - \u2718 Failed to connect
slack: https://mcp.slack.com/mcp (HTTP) - \u2714 Connected
plugin:x:y: node C:/x/run.mjs server - \u2714 Connected
"""

SKILL_TPL = "---\nname: chitragupta\ndescription: recall from {{HOME}}\n---\n\nRun `{{LAUNCHER}}`; keep {{process.env.X}}.\n"
BLOCK_TPL = "## Worklog (Chitragupta)\nSessions are recorded into {{HOME}}.\n"


def wr(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    _wr(p, text)


class SetupCase(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate(tempfile.mkdtemp(prefix="cg-test-"))
        os.environ.update(CHITRAGUPTA_STARTUP_DIR=os.path.join(self.user_dir, "startup"), CHITRAGUPTA_DASHBOARD="0",
                          CHITRAGUPTA_NO_MAINTENANCE="1")
        from chitragupta import install, setup
        self.setup_mod, self.install = setup, install
        self.calls, self.task_calls = [], []
        self.schtasks_rc = 0
        saved = (install.RUNNER, setup.RUNNER, setup._claude_exe, list(setup.SKILL_TEMPLATES),
                 list(setup.BLOCK_TEMPLATES))

        def undo():
            install.RUNNER, setup.RUNNER, setup._claude_exe, setup.SKILL_TEMPLATES[:], setup.BLOCK_TEMPLATES[:] = saved
        self.addCleanup(undo)
        install.RUNNER = self.fake_schtasks
        setup.RUNNER = self.fake_run
        setup._claude_exe = lambda cfg=None: ["claude"]
        tpl = os.path.join(self.user_dir, "tpl")
        wr(os.path.join(tpl, "SKILL.md"), SKILL_TPL)
        wr(os.path.join(tpl, "block.md"), BLOCK_TPL)
        setup.SKILL_TEMPLATES[:] = [os.path.join(tpl, "SKILL.md")]
        setup.BLOCK_TEMPLATES[:] = [os.path.join(tpl, "block.md")]

    def tearDown(self):
        self.restore()

    def fake_schtasks(self, args, timeout=60):
        self.task_calls.append(list(args))
        if args[1] == "/query":
            return (0, "Status: Ready\nLast Result: 0\n", "") if self.task_registered() else (1, "", "not found")
        if args[1] == "/create":
            if self.schtasks_rc == 0:
                self._registered = True
            return self.schtasks_rc, "", "" if self.schtasks_rc == 0 else "Access is denied."
        if args[1] == "/delete":
            self._registered = False
        return 0, "", ""

    def task_registered(self):
        return getattr(self, "_registered", False)

    def fake_run(self, args, cwd=None, timeout=60):
        self.calls.append((list(args), cwd))
        if args[-2:] == ["mcp", "list"]:
            return 0, MCP_LIST, ""
        if args[1:2] == ["init"] or (len(args) > 2 and args[1] == "init"):
            os.makedirs(os.path.join(args[-1], ".git"), exist_ok=True)
        return 0, "tool 1.0", ""

    def cj(self, obj):
        wr(os.path.join(self.claude_dir, ".claude.json"), json.dumps(obj))

    def apply(self, **kw):
        return self.setup_mod.apply(self.home, **kw)


class ApplyTests(SetupCase):
    def test_apply_end_to_end(self):
        md = os.path.join(self.claude_dir, "CLAUDE.md")
        wr(md, "# Mine\nkeep this\n")
        repo = os.path.join(self.home + "-repo")
        res = self.apply(prefixes=["abc", "DEMO"], repos=[repo], github_remote="https://github.com/acme/wl")
        self.assertTrue(res["ok"], json.dumps(res["steps"], indent=1))
        self.assertEqual(rd(os.path.join(self.user_dir, "home")).strip(), self.home)
        cfg = json.loads(rd(os.path.join(self.home, ".worklog", "config.json")))
        self.assertEqual(cfg["ticket_prefixes"], ["ABC", "DEMO"])
        self.assertEqual(cfg["github_remote"], "https://github.com/acme/wl")
        self.assertIn("database-mcp-rename", cfg["chitragupta_migrations"])
        self.assertTrue(os.path.isdir(os.path.join(self.home, "_drop")))
        from chitragupta import __version__
        self.assertEqual(rd(os.path.join(self.user_dir, "runtime", "current")).strip(), __version__)
        skill = rd(os.path.join(self.claude_dir, "skills", "chitragupta", "SKILL.md"))
        self.assertIn("recall from %s" % self.home, skill)
        self.assertIn(os.path.join(self.user_dir, "runtime", "run.py"), skill)
        self.assertIn("{{process.env.X}}", skill)
        text = rd(md)
        self.assertTrue(text.startswith("# Mine\nkeep this\n\n<!-- chitragupta:start -->\n## Worklog (Chitragupta)"))
        self.assertTrue(text.endswith("<!-- chitragupta:end -->\n"))
        self.assertTrue(os.path.isfile(md + ".chitragupta.bak"))
        create = next(c for c in self.task_calls if c[1] == "/create")
        self.assertEqual(create[:4], ["schtasks", "/create", "/tn", "Chitragupta"])
        names = {r["name"]: r for r in res["doctor"]}
        for n in ("runtime", "task", "recall skill", "CLAUDE.md block", "config", "home"):
            self.assertTrue(names[n]["ok"], names[n])
        self.assertEqual(self.forbidden, [])

    def test_idempotent_and_keeps_user_keys(self):
        wr(os.path.join(self.home, ".worklog", "config.json"),
           json.dumps({"ticket_prefixes": ["XYZ"], "my_key": {"a": 1}, "github_remote": "keep",
                       "paths": {"python": "C:/p/python.exe"}}))
        self.apply(prefixes=["ABC"], task=False, run_doctor=False)
        cfgp = os.path.join(self.home, ".worklog", "config.json")
        cfg = json.loads(rd(cfgp))
        self.assertEqual(cfg["ticket_prefixes"], ["XYZ", "ABC"])
        self.assertEqual(cfg["my_key"], {"a": 1})
        self.assertEqual(cfg["github_remote"], "keep")
        self.assertEqual(cfg["paths"]["python"], "C:/p/python.exe")
        self.assertTrue(os.listdir(os.path.join(self.home, ".worklog", "backups")))
        first = rd(cfgp)
        md = os.path.join(self.claude_dir, "CLAUDE.md")
        res = self.apply(prefixes=["abc"], task=False, run_doctor=False)
        self.assertTrue(res["ok"])
        self.assertEqual(rd(cfgp), first)
        self.assertEqual(rd(md).count("<!-- chitragupta:start -->"), 1)
        self.assertFalse(os.path.exists(md + ".chitragupta.bak"))  # file was new, then unchanged
        steps = {s["step"]: s["detail"] for s in res["steps"]}
        self.assertIn("unchanged", steps["config"])
        self.assertIn("unchanged", steps["skill"])
        self.assertIn("unchanged", steps["claude_md"])
        self.assertFalse([c for c in self.task_calls if c[1] == "/create"])  # --no-task

    def test_refuses_unreadable_config_before_writing(self):
        wr(os.path.join(self.home, ".worklog", "config.json"), "{not json")
        res = self.apply(run_doctor=False)
        self.assertFalse(res["ok"])
        self.assertEqual([s["step"] for s in res["steps"]], ["validate"])
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "home")))
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "runtime")))

    def test_refuses_broken_markers(self):
        wr(os.path.join(self.claude_dir, "CLAUDE.md"), "<!-- chitragupta:start -->\nx\n<!-- chitragupta:start -->\n")
        res = self.apply(run_doctor=False)
        self.assertFalse(res["ok"])
        self.assertIn("markers", res["steps"][0]["detail"])
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "home")))

    def test_busy_migrate_is_reported(self):
        from chitragupta import migrate
        real = migrate.run
        migrate.run = lambda dry_run=False, wait_s=None: {"skipped": "the agent is busy"}
        self.addCleanup(setattr, migrate, "run", real)
        res = self.apply(task=False, run_doctor=False)
        step = next(s for s in res["steps"] if s["step"] == "migrate")
        self.assertFalse(step["ok"])
        self.assertIn("re-run", step["detail"])
        self.assertTrue(os.path.isfile(os.path.join(self.claude_dir, "skills", "chitragupta", "SKILL.md")))

    def test_foreign_skill_is_not_overwritten(self):
        p = os.path.join(self.claude_dir, "skills", "chitragupta", "SKILL.md")
        wr(p, "---\nname: something-else\n---\nmine\n")
        res = self.apply(task=False, run_doctor=False)
        step = next(s for s in res["steps"] if s["step"] == "skill")
        self.assertFalse(step["ok"])
        self.assertEqual(rd(p), "---\nname: something-else\n---\nmine\n")

    def test_schtasks_failure_falls_back_to_startup_launcher(self):
        self.schtasks_rc = 1
        res = self.apply(run_doctor=False)
        step = next(s for s in res["steps"] if s["step"] == "task")
        self.assertTrue(step["ok"])
        vbs = rd(os.path.join(self.user_dir, "startup", "Chitragupta.vbs"))
        self.assertIn("agent", vbs)
        self.assertIn("loop", vbs)
        self.assertEqual(self.install.task_status()["method"], "startup")

    def test_task_xml(self):
        from chitragupta import common
        xml = self.install.task_xml(common.load_config(), user="HOST\\alex", start="2026-10-17T09:00:00")
        for want in ("<URI>\\Chitragupta</URI>", "<Delay>PT1M</Delay>", "<Interval>PT5M</Interval>",
                     "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", "<Hidden>true</Hidden>",
                     "<ExecutionTimeLimit>PT20M</ExecutionTimeLimit>", "<LogonType>InteractiveToken</LogonType>",
                     "<RunLevel>LeastPrivilege</RunLevel>", "<WorkingDirectory>%s</WorkingDirectory>" % self.home,
                     "agent tick"):
            self.assertIn(want, xml)
        self.assertIn(os.path.join(self.user_dir, "runtime", "run.py"), xml)
        self.assertNotIn("WorklogAgent", xml)


class UninstallTests(SetupCase):
    def test_removes_footprint_keeps_data(self):
        md = os.path.join(self.claude_dir, "CLAUDE.md")
        wr(md, "# Mine\n")
        self.apply(run_doctor=False)
        from chitragupta import uninstall
        out = uninstall.run()
        self.assertTrue(any("removed" in o for o in out))
        self.assertTrue(["schtasks", "/delete", "/tn", "Chitragupta", "/f"] in self.task_calls)
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "runtime")))
        self.assertFalse(os.path.exists(os.path.join(self.user_dir, "home")))
        self.assertFalse(os.path.exists(os.path.join(self.claude_dir, "skills", "chitragupta")))
        self.assertEqual(rd(md), "# Mine\n")
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".worklog", "config.json")))


class McpTests(SetupCase):
    def sender(self):
        return os.path.join(self.home, ".worklog", "slack-sender").replace("\\", "/")

    def test_status_from_claude_json_and_list(self):
        os.makedirs(self.sender(), exist_ok=True)
        self.cj({"mcpServers": {"mcp-atlassian": {"type": "stdio", "command": "uvx"},
                                "sqldb": {"type": "http", "url": "https://db.acme.test/mcp"},
                                "other": {"type": "stdio", "command": "x"}},
                 "projects": {self.sender(): {"mcpServers": {"slack": {"type": "http", "url": "https://mcp.slack.com/mcp"}}}}})
        st = self.setup_mod.mcp_status({"mcp": {"database": {"server": "sqldb", "enabled": True}}})
        self.assertEqual(st["slack"]["status"], "connected")
        self.assertIn("not used by background jobs", st["slack"]["detail"])
        self.assertEqual(st["jira"]["status"], "connected")
        self.assertEqual(st["database"]["status"], "unreachable")
        self.assertEqual(st["database"]["candidates"], ["other", "sqldb"])
        cwd = next(c for a, c in self.calls if a[-2:] == ["mcp", "list"])
        self.assertEqual(cwd.replace("\\", "/"), self.sender())

    def test_missing_and_unsupported(self):
        self.cj({"mcpServers": {"atl": {"type": "http", "url": "https://mcp.atlassian.com/v1/sse"},
                                "slack": {"type": "http", "url": "https://mcp.slack.com/mcp"}}})
        st = self.setup_mod.mcp_status({}, listing={})
        self.assertEqual(st["slack"]["status"], "missing")
        self.assertIn("user-scope Slack", st["slack"]["detail"])
        self.assertEqual(st["jira"]["status"], "unsupported")
        self.assertIn("not supported by v0.1.0", st["jira"]["detail"])
        self.assertEqual(st["database"]["candidates"], [])

    def test_parse_mcp_list(self):
        got = self.setup_mod.parse_mcp_list(MCP_LIST)
        self.assertEqual(got["claude.ai Slack"], "signed out")
        self.assertEqual(got["sqldb"], "unreachable")
        self.assertEqual(got["plugin:x:y"], "connected")

    def test_use_database(self):
        self.apply(task=False, run_doctor=False)
        self.cj({"mcpServers": {"sqldb": {"type": "stdio"}}})
        self.assertFalse(self.setup_mod.use_database("nope")["ok"])
        self.assertTrue(self.setup_mod.use_database("sqldb")["ok"])
        cfg = json.loads(rd(os.path.join(self.home, ".worklog", "config.json")))
        self.assertEqual(cfg["mcp"]["database"], {"server": "sqldb", "enabled": True})

    def test_add_slack_is_local_scope_only(self):
        self.apply(task=False, run_doctor=False)
        res = self.setup_mod.add_slack()
        self.assertTrue(res["ok"], res)
        # Chitragupta never writes Claude Code's settings: it prints the local-scope command for you to run
        self.assertFalse([a for a, c in self.calls if "mcp" in a and "add" in a])
        self.assertIn("--scope local", res["command"])
        self.assertNotIn("--scope user", res["command"])
        self.assertIn(res["command"], " ".join(res["sign_in"]))
        self.assertTrue(os.path.isdir(os.path.join(self.sender(), ".git")))


class DetectTests(SetupCase):
    def test_repos_prefixes_and_bruno(self):
        repo = tempfile.mkdtemp(prefix="cg-repo-")
        self.addCleanup(rmtree, repo)
        wr(os.path.join(repo, ".git", "HEAD"), "ref: refs/heads/feature/abc-1001-retry\n")
        wr(os.path.join(repo, ".git", "refs", "heads", "feature", "abc-1001-retry"), "0" * 40)
        wr(os.path.join(repo, ".git", "refs", "heads", "release-2"), "0" * 40)
        wr(os.path.join(repo, ".git", "packed-refs"), "%s refs/heads/bugfix/ABC-1002\n" % ("1" * 40))
        wr(os.path.join(repo, "api", "collection", "bruno.json"), "{}")
        wr(os.path.join(self.claude_dir, "projects", "C--x", "s.jsonl"),
           json.dumps({"type": "summary"}) + "\n" + json.dumps({"cwd": os.path.join(repo, "api")}) + "\n")
        d = self.setup_mod.detect({})
        self.assertEqual([r["path"] for r in d["repos"]], [repo.replace("\\", "/")])
        self.assertEqual(d["prefixes"][0], {"prefix": "ABC", "tickets": 2, "examples": ["ABC-1001", "ABC-1002"]})
        self.assertNotIn("RELEASE", [p["prefix"] for p in d["prefixes"]])
        self.assertEqual(d["suggested_prefixes"], ["ABC"])
        self.assertEqual(d["bruno"], [os.path.join(repo, "api", "collection").replace("\\", "/")])


class DoctorTests(SetupCase):
    def test_rows_and_failures(self):
        from chitragupta import doctor
        self.apply(run_doctor=False)
        mcp = {"slack": {"status": "missing"}, "jira": {"status": "signed out"},
               "database": {"status": "missing", "server": None}}
        rows = {r["name"]: r for r in doctor.run(mcp=mcp)}
        self.assertTrue(rows["Slack"]["ok"])
        self.assertIn("Slack isn't connected", rows["Slack"]["detail"])
        self.assertFalse(rows["Jira"]["ok"])
        self.assertTrue(rows["runtime"]["ok"])
        for r in rows.values():
            self.assertEqual(set(r), {"name", "ok", "detail"})
        wr(os.path.join(self.user_dir, "runtime", "current"), "0.0.1\n")
        rows = {r["name"]: r for r in doctor.run(mcp=mcp)}
        self.assertFalse(rows["runtime"]["ok"])

    def test_never_repairs(self):
        from chitragupta import doctor
        before = sorted(os.listdir(self.user_dir))
        doctor.run(mcp={})
        self.assertEqual(sorted(os.listdir(self.user_dir)), before)
        self.assertFalse([c for c in self.task_calls if c[1] != "/query"])


if __name__ == "__main__":
    unittest.main()
