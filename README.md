# Chitragupta

Named for Chitragupta, who keeps the record of every deed. `cg` is short for Chitragupta.

Chitragupta is a Claude Code plugin that writes your worklog for you. Hooks record what happens in your Claude Code sessions. A background task turns those records into:

- a **daily note** with a summary, a standup draft and a section for each ticket
- **ticket notes** with findings, root causes, queries, the APIs you called, saved requests you can replay, a DB-flow map and the files you produced
- a local **knowledge base** of tables, APIs, integrations and branches
- **branch tracking** per ticket: its branches and their parents, the PRs, and whether every commit reached every branch your branching strategy requires, with warnings on the ticket page, at session start, while you work and at 20:00 (see [docs/branch-tracking.md](docs/branch-tracking.md))
- an **evening Jira-worklog summary** (8 h laid out as Jira entries), sent to you on Slack at 20:00 if Slack is connected
- a **dashboard** on `127.0.0.1`: today (with the exact 20:00 message), any past day, tickets, timesheet, the DB map, knowledge, search, team notes and settings, with a command palette, live updates and keyboard shortcuts
- a read-only **MCP server**, so Claude asks the worklog through 8 tools (`search`, `get_ticket`, `get_day`, `get_timesheet`, `get_branch_status`, `get_db_table`, `list_tickets`, `get_alerts`) instead of reading files; about 140 tokens per session with tool search (see [docs/mcp.md](docs/mcp.md))

It runs on Windows (the background job also has macOS and Linux backends; see [scheduling](docs/scheduling.md)) and uses only the Python standard library. Slack, Jira and a database MCP are optional. Everything stays on your machine unless you configure a private GitHub remote for the daily notes.

## Install

You need Claude Code, Python 3.10+ (the `py` launcher), Git and Windows.

```
claude plugin install cg --marketplace omkarjadhav-10/chitragupta
```

This one-command form needs Claude Code 2.1.292 or later. On an older version, run:

```
claude plugin marketplace add omkarjadhav-10/chitragupta
claude plugin install cg@chitragupta
```

Inside a session (Claude Code 2.1.275 or later) you can use `/plugin install cg --marketplace omkarjadhav-10/chitragupta`. If the repository is private, the install uses the git credentials you already have.

Then open a new session and run **`/cg:setup`**. Until setup has run, Chitragupta records nothing; each session shows one line: *Chitragupta isn't set up yet — run /cg:setup to finish.*

Setup asks where to keep the data home (default `%USERPROFILE%\Chitragupta`). It detects your ticket prefixes, repos and Bruno collections, then installs the background task. Finally it offers to connect Slack, Jira and a database MCP. See [docs/install.md](docs/install.md).

## Commands

| Command | What it does |
|---|---|
| `/cg:today [YYYY-MM-DD]` | Time per ticket so far today, plus a preview of tonight's Jira-worklog message (nothing is sent) |
| `/cg:share <KEY> [sections] [--level team\|public]` | Share picked sections of a ticket with your team as a redacted bundle (preview first; written only when the scan passes); `--notes` lists your team's shared notes. See [docs/sharing.md](docs/sharing.md) |
| `/cg:ticket <KEY> [--export [--mode team\|shareable]]` | Everything recorded for a ticket: summary, root causes, time, saved requests, DB flow, artifacts, branches, file paths; `--export` saves the whole page as one HTML file |
| `/cg:branches <KEY>` | The ticket's branches, PRs, delivery check per target and alerts |
| `/cg:check [KEY]` | Fetch the watched repos and refresh branches, PRs and the delivery check now |
| `/cg:branch link\|unlink\|abandon\|restore\|strategy …` | Fix branch tracking by hand: a wrong link, an abandoned branch, a repo's or ticket's strategy |
| `/cg:timesheet [week\|month] [date] [csv]` | Hours per ticket by day (week) or by ISO week (month), with leave and holidays; optional CSV |
| `/cg:trace <KEY>` | The ticket's DB flow (tables and columns it changes, before → after) and its QA worked example; it can fetch a fresh example if a database MCP is connected |
| `/cg:search <words> [--rebuild]` | Search everything recorded (tickets, findings, queries, requests, branches, PRs, tables, APIs, DB links, daily notes) with filters by type, ticket, repo and date; shows the file to read for each result |
| `/cg:log <what>` | Change the evening worklog: extra time or a meeting, leave, a half day, a holiday, a working weekend day, or resend a day |
| `/cg:pause [resume\|status]` | Pause or resume capture |
| `/cg:dashboard [day DATE \| ticket KEY \| page]` | Open the local dashboard in your browser, starting it first if needed |
| `/cg:setup` | Set up, repair or upgrade (safe to re-run; never changes recorded data) |
| `/cg:doctor` | Check hooks, the background tick, runtime, scheduled task, recall skill, CLAUDE.md block, Slack/Jira/database MCP and recent errors. It only diagnoses and never changes anything. |

You type the `/cg:` commands yourself; Claude never runs them on its own. Setup also installs a **recall skill** (`chitragupta`) that Claude does use on its own. Ask it things like "what did I find on ABC-1001?", "standup for yesterday" or "I was on leave Friday", and it answers from the worklog or adds the right line to `inbox.md`.

## Dashboard

Run `/cg:dashboard`, use the **Chitragupta** shortcut setup adds to the Start menu, or install it as an app from Chrome or Edge.

![The Today page with synthetic data](docs/images/dashboard-today.png)

- **Today**: time against the day's target, the day lane by lane per ticket, delivery alerts, and tonight's Jira entries with a copy button per field
- **Tickets**: filter and sort every ticket; each ticket has tabs for its findings, Branches & PRs, DB flow, saved requests (copy as curl), queries, artifacts, timeline and notes, and exports as one HTML file
- **Timesheet**: a month calendar against your targets, leave and holidays marked, weekly bars, hours per ticket and a CSV export
- **Knowledge**: the DB map (tables, links, which ones QA verified), the API catalog and integrations
- **Search** across everything, **Health**, a **Setup** checklist, and **Settings** for appearance

<kbd>Ctrl</kbd> <kbd>K</kbd> opens a command palette from anywhere and <kbd>?</kbd> lists the keyboard shortcuts. The page updates live, works offline, is checked against WCAG 2.2 AA in both themes, and listens only on `127.0.0.1`. See [docs/dashboard.md](docs/dashboard.md).

## What Chitragupta will never do

Chitragupta only reads your work. It never edits a codebase, never runs a git command that changes history or branches in a work repo, never creates, approves, comments on, merges or closes a PR, never changes a branch, pipeline, Azure resource or Jira issue, and never writes to a database (one `SELECT` at a time, on QA). Its one Slack action is a DM to you. It writes only to its own folders, never to Claude Code's settings.

This is enforced in code, not just promised: one safety layer checks every process, file write, query and tool call against an allowlist, fails closed, logs every block to `security.log` (shown on Health and by `/cg:doctor`), and has no off switch. Your own sessions are never affected outside a `/cg:` command. See [docs/safety.md](docs/safety.md).

## Privacy

- **Local by default.** Raw events, ticket notes, the knowledge base and the config never leave the machine.
- **What can be pushed.** If you set a GitHub remote, only `daily/**`, `README.md`, `.gitignore` and `.gitattributes` are pushed. They go only after a check confirms the repository is private, and only after a public redaction pass.
- **Masking.** Secrets and personal data are masked when captured, before any model call and before any commit.
- **Model calls.** These run as `claude -p` with no built-in tools, nothing pre-approved but the job's read tools, and a fail-closed guard on every tool call. MCP jobs are read-only: only `SELECT`-style reads on QA connections.
- **Your work repos are only read.** Branch tracking runs hardened read-only git through the safety layer; its only network calls are `git fetch` into remote-tracking refs (never pruning) and your Git host's own CLI (`gh`, `glab`, `az`) with its own sign-in.
- **Known limit.** Names written in free text are not reliably masked. Review the daily notes before you push them anywhere shared.

Details are in [docs/privacy.md](docs/privacy.md).

## Uninstall

Stop the dashboard, then remove Chitragupta's pieces outside the data home. Run these in PowerShell:

```
py -3 "$env:USERPROFILE\.chitragupta\runtime\run.py" dashboard stop
py -3 "$env:USERPROFILE\.chitragupta\runtime\run.py" uninstall
claude plugin uninstall cg@chitragupta
```

`uninstall` removes the `Chitragupta` scheduled task, the stable runtime, the data home's git pre-commit hook (it calls that runtime), the recall skill and the CLAUDE.md block (backed up first), plus the home pointer. **It keeps all your data**: daily notes, tickets, the knowledge base, config and the data home's git repo.

## Docs

[Install](docs/install.md) · [Configuration](docs/configuration.md) · [Branch tracking](docs/branch-tracking.md) · [Safety](docs/safety.md) · [Privacy and data](docs/privacy.md) · [Dashboard](docs/dashboard.md) · [MCP server](docs/mcp.md) · [Search](docs/search.md) · [Sharing](docs/sharing.md) · [Scheduling](docs/scheduling.md) · [Architecture](docs/architecture.md) · [Development](docs/development.md) · [Roadmap](docs/roadmap.md) · [Changelog](CHANGELOG.md)

## License

MIT — see [LICENSE](LICENSE). Third-party components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
