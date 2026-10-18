"""Background MCP jobs: saved tool results, the Jira allowlist, server status, the daily budget, tick records and the
Slack sender's MCP config (stdlib unittest, synthetic data, no model / MCP / Slack calls)."""
import json
import os
import unittest
from unittest import mock

from tests.test_v010 import Case


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


if __name__ == "__main__":
    unittest.main()
