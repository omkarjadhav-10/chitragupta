# Roadmap

v0.1.0 brings the existing worklog features into the `cg` plugin. It also adds a read-only dashboard, and a first run that works with or without Slack and Jira. Later:

- **Search index** over daily files, tickets and knowledge.
- **DB map page** in the dashboard.
- **Settings page**, to edit config from the dashboard (v0.1.0 is read-only).
- **MCP server**, so Claude can query the worklog directly.
- **Mac/Linux scheduling** with launchd or systemd timers. v0.1.0 schedules with Windows Task Scheduler only.
- **Team sharing** of selected, redacted ticket notes.
- **Official Atlassian remote MCP support.** v0.1.0 Jira jobs need the `mcp-atlassian` server. The official remote server, and the claude.ai Atlassian connector, are detected but reported as unsupported.
- **Live MCP status checks in the dashboard.** Health currently shows what the last background job saw. `/cg:doctor` already does a live check.
- **A condensed, Jira-subtracted worklog preview in the dashboard.** Today's preview uses the raw lines and doesn't subtract time already in Jira, so it can differ from the 20:00 message.
- **Better free-text name redaction.** Names in labelled fields are masked; names written into prose are not reliably masked yet.
- **A past-day view in the dashboard**, so `/cg:dashboard day <date>` opens that day instead of Today.
- **A repeatable `resend:`.** Inbox lines are de-duplicated, so repeating the exact same `resend:` line is ignored.
