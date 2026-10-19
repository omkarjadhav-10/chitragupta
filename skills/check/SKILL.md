---
name: check
description: Fetch the watched repos and refresh branches, PRs and the delivery check now, for one ticket or all active ones.
disable-model-invocation: true
argument-hint: "[TICKET-KEY]"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli check*)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli check*)
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: py
          args: ["-3", "${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "safety", "session"]
          timeout: 10
---

# /cg:check [KEY]

Argument: `$ARGUMENTS`

Run exactly one command (it can take a minute: it runs `git fetch` for base and ticket branches, and asks the Git host's CLI for PRs; use a 10-minute timeout):

- with a key → `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli check <KEY>`
- without → `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli check`

Then report like `/cg:branches`: the alerts first (plain sentences, with commits), then branches, PRs and delivery per target, briefly. Mention any error lines, the ones starting with "!" (a failed fetch or a host CLI that isn't signed in: `gh auth login`, `glab auth login`, `az login`). If it says "Not checked: the background agent is busy", say so and suggest trying again in a few minutes.

The fetch only updates remote-tracking refs (`refs/remotes/<remote>/…`, never pruned); nothing in the work repos changes. Never run git yourself for this.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
