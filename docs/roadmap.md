# Roadmap

v0.1.0 brings the existing worklog features into the `cg` plugin. It also adds a read-only dashboard, and a first run that works with or without Slack and Jira. Later:

- **Settings and actions in the dashboard**: edit the config with a preview, history and undo; mark leave and holidays; pause capture; send a test message; Fix buttons on Setup. They come with the write protections and the safety layer they need.
- **MCP server**, so Claude can query the worklog directly.
- **Mac/Linux scheduling** with launchd or systemd timers. v0.1.0 schedules with Windows Task Scheduler only.
- **Team sharing** of selected, redacted ticket notes.
- **Official Atlassian remote MCP support.** v0.1.0 Jira jobs need the `mcp-atlassian` server. The official remote server, and the claude.ai Atlassian connector, are detected but reported as unsupported.
- **A "Check again" for MCP status in the dashboard.** Health shows the last cached `claude mcp list` check; `/cg:doctor` runs a fresh one.
- **A condensed, Jira-subtracted worklog preview in the dashboard.** Today's preview uses the raw lines and doesn't subtract time already in Jira, so it can differ from the 20:00 message.
- **Better free-text name redaction.** Names in labelled fields are masked; names written into prose are not reliably masked yet.
- **A repeatable `resend:`.** Inbox lines are de-duplicated, so repeating the exact same `resend:` line is ignored.
- **PR data through a host's read-only MCP** for branch tracking, when no host CLI is installed (v0.2.0 falls back to the PR merge commits).
- **Base-branch relations in the branch graph.** The dashboard draws extra base branches forking from the first one; it doesn't know how qa, release and main relate.
