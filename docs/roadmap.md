# Roadmap

v0.1.0 ports the existing worklog features into the `cg` plugin, adds a read-only dashboard and a first run that works with or without Slack and Jira. Later:

- **Search index** over daily files, tickets and knowledge.
- **DB map page** in the dashboard.
- **Settings page** (edit config from the dashboard; v0.1.0 is read-only).
- **MCP server** so Claude can query the worklog directly.
- **Mac/Linux scheduling** (launchd / systemd timers); v0.1.0 schedules with Windows Task Scheduler only.
- **Team sharing** of selected, redacted ticket notes.
