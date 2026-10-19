"""Background MCP jobs: saved tool results, the Jira allowlist, server status, the daily budget, tick records and the
Slack sender's MCP config (stdlib unittest, synthetic data, no model / MCP / Slack calls)."""
import json
import os
import unittest
from unittest import mock

from tests.test_v010 import Case

JIRA = "mcp-atlassian"
DBS = "sqldb"


def stream(servers, calls=(), out=None):
    """A stream-json transcript: init with {server: status}, one tool_use + tool_result per (tool, result), a result."""
    ev = [{"type": "system", "subtype": "init", "tools": [], "mcp_servers": [
        dict({"name": n}, **(st if isinstance(st, dict) else {"status": st})) for n, st in servers.items()]}]
    for i, (tool, result) in enumerate(calls):
        ev.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t%d" % i, "name": tool,
                                                                 "input": {}}]}})
        ev.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t%d" % i,
                                                            "content": result}]}})
    ev.append({"type": "result", "subtype": "success", "result": json.dumps(out or {"done": True}),
               "usage": {"input_tokens": 10, "output_tokens": 2}, "total_cost_usd": 0.001})
    return "\n".join(json.dumps(e) for e in ev)


class Harness(Case):
    """mcpjobs.run for real; only the claude process is replaced at the command guard."""

    def setUp(self):
        super().setUp()
        from chitragupta import mcpjobs
        from chitragupta.safety import commands
        mcpjobs.run = self._real_mcp[0]  # restore() puts the tripwire back
        real_exe, real_run = mcpjobs.claude_exe, commands.run
        self.addCleanup(setattr, mcpjobs, "claude_exe", real_exe)
        self.addCleanup(setattr, commands, "run", real_run)
        mcpjobs.claude_exe = lambda cfg: ["claude"]
        mcpjobs.claude_json = lambda: {"mcpServers": {JIRA: {"type": "stdio", "command": "uvx"},
                                                      DBS: {"type": "http", "url": "https://db.acme.test/mcp"}}}
        self.runs, self.replies = [], []

        def fake_run(argv, purpose, cwd=None, input_text=None, timeout=120, env_extra=None, cfg=None):
            self.runs.append({"argv": list(argv), "reason": commands.reason(argv, purpose, cwd=cwd, cfg=cfg)})
            return self.replies.pop(0) if self.replies else (0, stream({}), "")
        commands.run = fake_run

    def jcfg(self, **mcp):
        from chitragupta import common
        cfg = json.loads(json.dumps(common.load_config()))
        cfg.setdefault("mcp", {}).setdefault("jira", {}).update(enabled=True)
        cfg["mcp"].setdefault("database", {}).update(enabled=True, server=DBS, connections={"qa": "qa-shop"})
        cfg["mcp"].update(mcp)
        return cfg

    def job(self, health, server=JIRA, reply=None, cfg=None):
        from chitragupta import mcpjobs
        if reply is not None:
            self.replies.append(reply)
        return mcpjobs.run("probe-job", {server: ["jira_get_issue"]}, [mcpjobs.mcp_tool(server, "jira_get_issue")], [],
                           "task", {"type": "object"}, cfg or self.jcfg(), health, day="2026-10-16")


class SavedToolResult(Case):
    """FX-14: Claude Code moved a large tool result to a file; a refused delete must not lose the text."""

    def test_text_survives_a_refused_delete(self):
        from chitragupta import mcpjobs
        from chitragupta.safety import paths
        from chitragupta.safety.log import Blocked
        p = os.path.join(self.home, "saved-result.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"issues": [{"key": "ABC-1001"}]}')
        content = "Output too large. Full output saved to: %s" % p
        with mock.patch.object(paths, "delete", side_effect=Blocked("path", "outside the write roots")):
            self.assertEqual(mcpjobs._persisted(content), '{"issues": [{"key": "ABC-1001"}]}')
        self.assertTrue(os.path.exists(p))
        self.assertEqual(mcpjobs._persisted(content), '{"issues": [{"key": "ABC-1001"}]}')  # deleted this time
        self.assertFalse(os.path.exists(p))


class JiraAllowlist(Case):
    """FX-13: the Jira entry is filed under the configured server name and refreshed from the code every day."""

    def cfg(self, server=None):
        from chitragupta import common
        cfg = json.loads(json.dumps(common.load_config()))
        cfg.setdefault("mcp", {}).setdefault("jira", {}).update(enabled=True)
        if server:
            cfg["mcp"]["jira"]["server"] = server
        return cfg

    def test_renamed_server_gets_the_reads(self):
        from chitragupta import common, mcpjobs
        common.write_json(common.wl("mcp", "allow.json"),
                          {"servers": {"mcp-atlassian": {"read": ["jira_get_issue"], "write": []}}})
        allow = mcpjobs.ensure_files(self.cfg("jira-acme"))
        self.assertEqual(allow["servers"]["jira-acme"]["read"], mcpjobs.JIRA_ALLOW_DEFAULT["read"])
        self.assertIn("jira_transition_issue", allow["servers"]["jira-acme"]["write"])
        self.assertIn("jira-acme", mcpjobs.allow_default(self.cfg("jira-acme"))["servers"])

    def test_existing_file_gets_new_reads_and_keeps_extra_writes(self):
        from chitragupta import common, mcpjobs
        common.write_json(common.wl("mcp", "allow.json"), {"servers": {"mcp-atlassian": {
            "read": ["jira_get_issue"], "write": ["jira_some_new_write"]}}})
        allow = mcpjobs.ensure_files(self.cfg())
        e = allow["servers"]["mcp-atlassian"]
        self.assertEqual(e["read"], mcpjobs.JIRA_ALLOW_DEFAULT["read"])
        self.assertIn("jira_some_new_write", e["write"])
        self.assertIn("jira_add_worklog", e["write"])

    def test_read_only_mode_under_the_configured_name(self):
        from chitragupta import common, mcpjobs
        mcpjobs.claude_json = lambda: {"mcpServers": {"jira-acme": {"type": "stdio", "command": "uvx",
                                                                    "env": {"JIRA_URL": "https://jira.acme.test"}}}}
        path, _ = mcpjobs.job_config("jira-acme", ["jira_get_issue"], "jira-card-ABC-1001", self.cfg("jira-acme"))
        env = common.read_json(path)["mcpServers"]["jira-acme"]["env"]
        self.assertEqual((env["READ_ONLY_MODE"], env["ENABLED_TOOLS"]), ("true", "jira_get_issue"))

    def test_get_write_request_is_not_a_read(self):
        from chitragupta import common, mcpjobs
        self.assertNotIn("get_write_request", mcpjobs.DATABASE_ALLOW_DEFAULT["read"])
        common.write_json(common.wl("mcp", "allow.json"), {"servers": {"sqldb": {
            "read": ["run_query", "get_write_request"], "write": [], "query_tools": ["run_query"]}}})
        cfg = self.cfg()
        cfg["mcp"].setdefault("database", {}).update(enabled=True, server="sqldb")
        allow = mcpjobs.ensure_files(cfg)
        self.assertEqual(allow["servers"]["sqldb"]["read"], ["run_query"])


class ServerStatus(Harness):
    """FX-10: unreachable / signed out / missing are told apart, keep their error text, and a later success clears them."""

    def test_unreachable_then_signed_out_then_cleared(self):
        from chitragupta import mcpjobs
        h = {"mcp": {"skip": {"jira": "2026-10-16T09:00:00+05:30"}, "last_skip_reason": {"jira": "x"},
                     "offline": {"jira-card-ABC-1001": "missing env JIRA_TOKEN"}}}
        with self.assertRaises(mcpjobs.JobError) as cm:
            self.job(h, reply=(1, stream({JIRA: "failed"}), "Error: connect ETIMEDOUT 10.0.0.1:443\n"))
        self.assertIn("unreachable", str(cm.exception))
        st = h["mcp"]["server_state"][JIRA]
        self.assertEqual(st["state"], "unreachable")
        self.assertIn("ETIMEDOUT", st["error"])
        self.assertTrue(st["at"])
        self.assertIn(JIRA, h["mcp"]["offline"])
        self.assertIn("probe-job", h["mcp"]["job_errors"])
        with self.assertRaises(mcpjobs.JobError) as cm:
            self.job(h, reply=(0, stream({JIRA: "needs-auth"}), ""))
        self.assertIn("signed-out", str(cm.exception))
        self.assertEqual(h["mcp"]["server_state"][JIRA]["state"], "signed-out")
        self.job(h, reply=(0, stream({JIRA: "connected"}, [("mcp__%s__jira_get_issue" % JIRA, "{}")]), ""))
        m = h["mcp"]
        self.assertNotIn(JIRA, m["offline"])
        self.assertNotIn("jira-card-ABC-1001", m["offline"])  # an older version's job-keyed entry goes too
        self.assertNotIn(JIRA, m["server_state"])
        self.assertNotIn("jira", m["skip"])
        self.assertNotIn("jira", m["last_skip_reason"])
        self.assertNotIn("probe-job", m["job_errors"])

    def test_missing_env_is_a_job_error_not_a_server_key(self):
        from chitragupta import mcpjobs
        mcpjobs.claude_json = lambda: {"mcpServers": {JIRA: {"type": "http", "url": "https://jira.acme.test/mcp",
                                                             "headers": {"Authorization": "Bearer ${ACME_JIRA_PAT}"}}}}
        os.environ.pop("ACME_JIRA_PAT", None)
        h = {}
        with mock.patch.object(mcpjobs, "user_env", return_value=None), self.assertRaises(mcpjobs.JobError):
            self.job(h)
        self.assertEqual(self.runs, [])
        self.assertEqual(list(h["mcp"]["offline"]), [JIRA])  # keyed by server only
        self.assertEqual(h["mcp"]["server_state"][JIRA]["state"], "signed-out")
        self.assertIn("ACME_JIRA_PAT", h["mcp"]["job_errors"]["probe-job"]["error"])

    def test_mcp_list_errors_and_old_cache(self):
        from chitragupta import doctor, setup
        from tests.test_setup import MCP_LIST
        self.assertEqual(setup.parse_mcp_list(MCP_LIST)["sqldb"], "unreachable")
        self.assertEqual(setup.parse_mcp_errors(MCP_LIST)["sqldb"], "Failed to connect")
        self.assertEqual(setup.parse_mcp_errors(MCP_LIST)["claude.ai Slack"], "Needs authentication")
        old = {"slack": {"status": "missing"}, "jira": {"status": "offline", "server": JIRA, "detail": "server (user scope)"},
               "database": {"status": "signed out", "server": DBS, "detail": "server (user scope)",
                            "error": "Needs authentication", "error_at": "2026-10-16T09:00:00+05:30"}}
        health = {"mcp": {"server_state": {JIRA: {"state": "unreachable", "error": "connect ETIMEDOUT",
                                                  "at": "2026-10-16T08:00:00+05:30"}}}}
        rows = {r["name"]: r for r in doctor.mcp_rows(old, self.jcfg(), health)}
        self.assertFalse(rows["Jira"]["ok"])
        self.assertTrue(rows["Jira"]["detail"].startswith("unreachable: "))
        self.assertIn("connect ETIMEDOUT", rows["Jira"]["detail"])
        self.assertIn("Needs authentication (09:00)", rows["database MCP"]["detail"])
        self.assertIn("/mcp", rows["database MCP"]["detail"])

    def test_status_cache_keeps_the_error(self):
        from chitragupta import plugin, setup
        from tests.test_setup import MCP_LIST
        os.makedirs(os.path.dirname(plugin.claude_json_path()), exist_ok=True)
        with open(plugin.claude_json_path(), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {DBS: {"type": "http", "url": "https://db.acme.test/mcp"}}}, f)
        with mock.patch.object(setup, "_claude_exe", return_value=["claude"]), \
                mock.patch.object(setup, "_run", return_value=(0, MCP_LIST, "")):
            st = setup.mcp_status({"mcp": {"database": {"server": DBS, "enabled": True}}}, probe=True)
        self.assertEqual((st["database"]["status"], st["database"]["error"]), ("unreachable", "Failed to connect"))
        self.assertEqual(st["database"]["error_at"], st["checked"])

    def test_dashboard_reads_old_and_new_health(self):
        from chitragupta.dashboard import api
        m = api._mcp({"mcp": {"offline": {"jira-card-ABC-1001": "missing env X", JIRA: "2026-10-16T09:00:00+05:30"},
                              "jobs": {"jira-card-ABC-1001": {"at": "2026-10-16T09:00:00+05:30",
                                                              "servers": {JIRA: "failed"}}},
                              "server_state": {JIRA: {"state": "unreachable", "error": "connect ETIMEDOUT",
                                                      "at": "2026-10-16T09:00:00+05:30"}}}}, self.jcfg(), "2026-10-16")
        self.assertEqual(list(m["offline"]), [JIRA])
        srv = m["servers"][0]
        self.assertEqual((srv["state"], srv["error"]), ("unreachable", "connect ETIMEDOUT"))
        self.assertEqual(api._mcp({"mcp": {"offline": {"a,b": "2026-10-16T09:00:00+05:30"}}}, self.jcfg(),
                                  "2026-10-16")["offline"], {"a,b": "2026-10-16T09:00:00+05:30"})


if __name__ == "__main__":
    unittest.main()
