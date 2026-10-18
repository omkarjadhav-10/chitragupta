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


if __name__ == "__main__":
    unittest.main()
