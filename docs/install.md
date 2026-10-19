# Install

## Requirements

- Windows (v0.1.0 schedules its background work with Windows Task Scheduler)
- Claude Code: 2.1.292+ for the one-command install, 2.1.275+ for the in-session form
- Python 3.10+ with the `py` launcher on `PATH`. The plugin's hooks run `py -3`; plain `python` may be the Microsoft Store stub.
- Git

## The one command

```
claude plugin install cg --marketplace omkarjadhav-10/chitragupta
```

On Claude Code older than 2.1.292, run the two-step form instead:

```
claude plugin marketplace add omkarjadhav-10/chitragupta
claude plugin install cg@chitragupta
```

Inside a session (2.1.275+), use `/plugin install cg --marketplace omkarjadhav-10/chitragupta`.

A private repository installs with the git credentials you already have (Git Credential Manager, `gh`, or SSH). Nothing prompts.

## First run

Open a new Claude Code session. Until setup has run, the plugin's SessionStart hook adds exactly one line and does nothing else:

> Chitragupta isn't set up yet — run /cg:setup to finish.

No events are recorded and nothing is written. Run **`/cg:setup`**.

### What `/cg:setup` checks and detects

- **Requirements.** It checks Python 3.10+, git, Windows, the `claude` executable (and its version) and `py -3`. If any of these fail, it tells you how to fix it and stops.
- **Data home.** It shows the current one if set, otherwise suggests `%USERPROFILE%\Chitragupta`. Data already in the folder you pick is kept.
- **Ticket prefixes, repos and Bruno collections.** It finds these in the folders your recent Claude Code sessions ran in: the repos, their branch names (`feature/ABC-1001-…` gives prefix `ABC`) and any `bruno.json` collections. You confirm the list or change it.
- **MCP servers.** It shows Slack, Jira and a database MCP as connected, signed out, unreachable, missing or unsupported. An unreachable or signed-out server keeps the check's short error text and time (`error`, `error_at` in `.worklog\cache\mcp-status.json`; caches from older versions say `offline`, read as unreachable), and doctor adds what the last background job saw.

### What it asks, in one message

1. The data home folder.
2. Your ticket prefixes (for example `ABC, OPS`).
3. The repos to watch and the Bruno collections to read.
4. An optional GitHub remote for the daily notes. It must be a **private** repository (see [privacy.md](privacy.md)).
5. Whether to register the `Chitragupta` scheduled task. It runs the background tick at logon and every 5 minutes.

### What it installs

- **Home pointer.** `%USERPROFILE%\.chitragupta\home` records which data home you chose.
- **Data home layout and config.** It creates the layout and `<home>\.worklog\config.json`. An existing config is merged, never replaced: keys are kept, lists only grow, and a backup is written to `.worklog\backups\`.
- **Stable runtime.** It copies the engine to `%USERPROFILE%\.chitragupta\runtime\<version>\`, with the launcher at `runtime\run.py`. The scheduled task and git hooks call this copy, so plugin updates never break them.
- **The `Chitragupta` scheduled task.** If Task Scheduler refuses it, setup falls back to a Startup-folder launcher.
- **The `chitragupta` recall skill.** It goes in `~/.claude/skills/chitragupta/`.
- **The CLAUDE.md block.** It is added to `~/.claude/CLAUDE.md` between `<!-- chitragupta:start -->` and `<!-- chitragupta:end -->`. The file is backed up first.
- **A doctor run** at the end. It includes **Safety: enforced**: every process, file write and tool call goes through the safety layer ([safety.md](safety.md)). It needs Claude Code 2.1.295 or newer for the internal calls' fail-closed guard hook.

Setup is idempotent: re-running it repairs or upgrades an install and never changes recorded data. When it finishes, start a new session so the recall skill and the CLAUDE.md block load, then try `/cg:today`.

## The `claude` path the scheduled task needs

The background tick runs from Task Scheduler. It has no shell, so it can't see the per-session `PATH` of the terminal you work in. Setup records a `claude.exe` the task can always reach in `paths.claude`. It looks in this order:

1. `paths.claude` in config, if that file still exists. Setup replaces a recorded path that has disappeared, for example after Claude Code updated itself in place.
2. The native install: `%USERPROFILE%\.local\bin\claude.exe` (from `claude install`).
3. The fnm **default alias**: `%APPDATA%\fnm\aliases\default\…\claude.exe`, or the same under `%FNM_DIR%`.
4. nvm-windows' active-version link (`%NVM_SYMLINK%\…`).
5. The npm global install: `%APPDATA%\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe`.
6. `claude.exe` on `PATH`. A `fnm_multishells` path is resolved to the real Node folder behind it.

`.cmd` shims are never used. `/cg:doctor` fails the `claude` row when the only `claude` it finds is under `fnm_multishells`. That folder exists only while one shell is open, so the scheduled task can't use it.

To fix the `claude` row, do one of these:

- Run `claude install` to get the native binary.
- Set an fnm default alias (`fnm default <version>`).
- Repair the npm global install.
- Set `paths.claude` in config yourself.

## Connecting Slack (optional)

Slack is used for one thing: the 20:00 Jira-worklog message, sent to you as a direct message.

**Background jobs only.** Setup never adds Slack to your global Claude Code config, because that would load Slack's tools into every session. Instead `setup add-slack`:

1. Creates `<home>\.worklog\slack-sender\` and runs `git init` in it. That gives Claude Code a project folder to attach the server to.
2. Prints the one command that adds Slack's hosted MCP server (`https://mcp.slack.com/mcp`) in **local scope of that folder only**: `claude mcp add --transport http --scope local --client-id … --callback-port 3118 slack https://mcp.slack.com/mcp`. **You run it yourself**, in a terminal in that folder: Chitragupta never writes Claude Code's settings (`~/.claude.json`). Slack's hosted MCP has no dynamic client registration, so the command carries the client id Slack publishes for Claude, with callback port **3118**. You can override both with `jira_worklog.slack.server.client_id` and `callback_port`.
3. Prints the **one-time sign-in**: in the same terminal in `<home>\.worklog\slack-sender`, run `claude`, type `/mcp`, choose `slack`, select Authenticate, and finish the sign-in in the browser.

Then run `/cg:setup` again, or this command:

```
py -3 "$env:USERPROFILE\.chitragupta\runtime\run.py" setup slack-user [you@acme.test]
```

It looks up your Slack user ID and the send tool, and stores both in config. Sends then reuse the token from that sign-in. Each send is one short `claude -p` call that is allowed to use only the send tool, and the safety guard checks the call itself: only to your own user ID, plain text, no threads, files or scheduling.

To check it end to end, send yourself one short test DM (nothing else changes):

```
py -3 "$env:USERPROFILE\.chitragupta\runtime\run.py" setup slack-test
```

The claude.ai Slack connector and any Slack server in your user scope are **not** used by background jobs.

## Connecting Jira (optional)

v0.1.0 jobs are written for the **`mcp-atlassian`** server, configured in your Claude Code user scope. Turn the jobs on with `mcp.jira.enabled: true`; the server name comes from `mcp.jira.server`. With Jira connected:

- Ticket titles and summaries come from the Jira card, fetched once per ticket and at most once a day.
- The evening summary subtracts time you already logged in Jira and mentions your comments and status changes.

The **official Atlassian remote MCP** (and the claude.ai Atlassian connector) is detected and reported as *present but not supported*. v0.1.0 jobs don't use it; support is on the [roadmap](roadmap.md).

## Connecting a database MCP (optional)

Any **read-only** database MCP that you have configured in Claude Code's user scope can supply:

- schema facts, which fill a DB flow's "Unknowns"
- QA worked examples: real before → after values for a ticket's flow, from QA only
- a verified table-link map in `knowledge/db-map.md`

Setup lists the user-scope servers that could be a database MCP and asks which one, if any, to use. It never picks for you. To choose one by hand:

```
py -3 "$env:USERPROFILE\.chitragupta\runtime\run.py" setup use-database <server>
```

This sets `mcp.database.server` and `mcp.database.enabled: true`. Then set `mcp.database.connections` (the QA connection) and, if your server's tools use other names, `mcp.database.tools`. See [configuration.md](configuration.md#mcp).

Every MCP job is read-only. Each job gets only the server it needs and its read tools; write tools are explicitly disallowed. A guard hook rejects anything that isn't a single read statement on an allowed (non-production) connection, with a row limit. See [privacy.md](privacy.md#mcp-jobs).

## What works without each

| Missing | What still works | What you lose |
|---|---|---|
| **Slack** | The evening message is still composed. It's saved in the daily note under `## Jira worklog` and in `data/timesheets/<day>.json`, and it's shown on the dashboard and in `/cg:today`. | Delivery. The daily note says *saved here, Slack isn't connected*, and Health says Slack isn't connected. Days composed before you connect Slack aren't sent later; use `/cg:log resend: <day>`. |
| **Jira** | Everything else. Ticket titles come from branch names. | Jira titles and summaries, and subtracting time already logged in Jira |
| **Database MCP** | DB flows built from what your sessions read, changed and ran | QA worked examples, schema facts and the verified link map |

Capture, ticket notes, daily notes, saved requests, DB flows, artifacts, timesheets and the dashboard need none of them.

## The weekly reminder

If Slack or Jira isn't connected, a SessionStart line reminds you at most once every 7 days. The line says what is missing and what that costs. To stop it:

```
py -3 "$env:USERPROFILE\.chitragupta\runtime\run.py" setup reminders off
```

`setup reminders on` turns it back on. `/cg:setup` offers this when you decline Slack or Jira. It sets `reminders.mcp` in config.

## Other setup commands

All of them run through the stable launcher, `py -3 "$env:USERPROFILE\.chitragupta\runtime\run.py" …`. In `cmd.exe`, use `%USERPROFILE%` instead of `$env:USERPROFILE`.

| Command | Purpose |
|---|---|
| `setup check [--json] [--no-mcp]` | Requirements, data home, detections and MCP status, without changing anything |
| `setup apply --home DIR [--prefixes A,B] [--repos P,…] [--bruno P,…] [--github-remote URL] [--no-task]` | What `/cg:setup` runs |
| `setup mcp-status [--json] [--cached]` | Slack / Jira / database MCP status |
| `setup add-slack [--client-id ID] [--callback-port N]` | Prepare Slack for background jobs and print the `claude mcp add` command to run (see above) |
| `doctor [--json] [--fast]` | Health checks, including **Safety: enforced** and the blocks in `security.log` ([safety.md](safety.md)); exits 1 if any check fails; never repairs |
| `runtime status` / `runtime install [--force]` | Inspect or refresh the stable runtime |
| `install --status` | Scheduled-task status |
| `uninstall` | Remove the task, runtime, recall skill, CLAUDE.md block and home pointer; keeps data |
