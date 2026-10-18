---
name: setup
description: Set up or repair Chitragupta - pick the data home, confirm ticket prefixes, repos and Bruno collections, install the runtime, scheduled task, recall skill and CLAUDE.md block, connect Slack, Jira and a database MCP, then run doctor.
disable-model-invocation: true
argument-hint: "[--home <folder>]"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" setup check *)
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" setup mcp-status *)
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" doctor *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" setup check *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" setup mcp-status *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" doctor *)
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: py
          args: ["-3", "${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "safety", "session"]
          timeout: 10
          onFailure: block
---

# /cg:setup

Setup is idempotent: re-running it repairs or upgrades an install and never changes recorded data. Keep it to a few short exchanges. Write every command with the literal path below; shell variables don't survive between commands.

Launcher: `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py"`

## 1. What is already known

!`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" setup check --json`

Summarise in a short list: requirements (Python 3.10+, git, Windows, Claude Code version, `py -3`) with anything failing first; the current data home if one is set; detected ticket prefixes, repos and Bruno collections; Slack / Jira / database / Git-host MCP status; what is already installed. If a requirement fails, say how to fix it and stop.

## 2. Ask only what can't be detected

In ONE message, ask them to confirm or change:
- **Data home** — the folder that holds the worklog. Default: the current home if there is one, else `%USERPROFILE%\Chitragupta` (or `$ARGUMENTS` if they passed `--home`). Existing data there is kept.
- **Ticket prefixes** (e.g. `ABC, OPS`) — offer the detected ones.
- **Repos** to watch and **Bruno collections** — offer the detected ones.
- **GitHub remote** for the daily notes (optional; a private repo URL, or none).
- **Scheduled task** `Chitragupta` (runs the background tick at logon and every 5 minutes) — yes unless they say no.

## 3. Apply

Run, with their answers (lists comma-separated):

`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" setup apply --home "<folder>" --prefixes <A,B> --repos "<path>,<path>" --bruno "<path>"` plus `--github-remote <url>` if given and `--no-task` if they declined the task.

Report what it did in a few lines. If it fails, show the error and stop.

## 4. MCP servers (each optional)

`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" setup mcp-status --json`

- **Slack** (the 20:00 Jira-worklog DM). If missing, offer `setup add-slack`: it prepares a folder for background jobs only and prints the one `claude mcp add --scope local …` command plus a one-time browser sign-in. Chitragupta never writes Claude Code's settings itself, so the user runs that command (in a terminal in the printed folder) — walk them through it; don't run it for them. When signed in, run `setup slack-user` to store their Slack user ID, then offer `setup slack-test` (one short DM to themselves) to confirm it works. Without Slack the evening worklog is still composed and saved in the daily note, just not sent.
- **Jira** (`mcp-atlassian`): used for ticket titles and to subtract time already logged in Jira. If it reports the official Atlassian remote server, say it is present but not supported by v0.1.0 jobs. Without Jira, titles come from branch names.
- **Database MCP** (QA worked examples in DB flows, schema facts): if candidate servers are listed, ask which one (or none), then `setup use-database <server>`. Never pick one for them.
- **Git host MCP** (`git_host`: an Azure DevOps or GitHub MCP server): only needed for repos whose host CLI (`az`, `gh`) isn't installed; then PRs are read through that server's read tools only, one budgeted job per repo per refresh. If the row says `detected`, ask whether to use it (or not), then `setup use-git-host <server>`; it reports the read tools it allows (every other tool of that server stays blocked). Never pick one for them.

Run each of these only after they agree.

If they decline Slack or Jira for good, ask whether to stop the weekly "connect Slack/Jira" reminder; if yes, run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" setup reminders off` (`on` turns it back on).

## 5. Check

`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" doctor`

Show the failing rows with their fix, or "all checks passed". Finish with: start a new Claude Code session so the `chitragupta` recall skill and the CLAUDE.md block load; try `/cg:today`.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
