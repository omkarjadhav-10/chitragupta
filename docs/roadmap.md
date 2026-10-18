# Roadmap

v0.4.0 brings settings and actions to the dashboard, the DB map, past days, a persistent search index, a read-only MCP server, the exact 20:00 preview, team sharing, PR data through a Git host's MCP, base-branch relations and the background job on macOS and Linux. Later:

- **Hooks on macOS and Linux.** The scheduler backends and the `bin/cg-hook` launcher are in place; the hooks and `/cg:` commands still start with `py -3` until the switch to the launcher is checked in a live Claude Code session on each OS.
- **Official Atlassian remote MCP support.** Jira jobs need the `mcp-atlassian` server. The official remote server, and the claude.ai Atlassian connector, are detected but reported as unsupported.
- **Better free-text name redaction.** Names in labelled fields are masked; names written into prose are not reliably masked yet.
- **Per-server MCP response times.** `claude mcp list` reports one time for the whole check.
- **Fix buttons on Setup** and a Slack test message from the dashboard.
