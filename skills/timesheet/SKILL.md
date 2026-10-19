---
name: timesheet
description: Hours per ticket for this week (by day) or a month (by ISO week), with leave and holidays, optionally as CSV.
disable-model-invocation: true
argument-hint: "[week|month] [YYYY-MM-DD] [csv]"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli hours *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli hours *)
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: py
          args: ["-3", "${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "safety", "session"]
          timeout: 10
---

# /cg:timesheet [week|month] [date] [csv]

This week so far:

!`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli hours --week`

Arguments: `$ARGUMENTS`

- No arguments: show the table above.
- `month`, a date or `csv`: run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli hours` with `--month` (instead of `--week`), `--date YYYY-MM-DD` (the week or month containing that day; resolve "last week" or "September" to a date yourself) and `--csv`, and show that output instead. CSV goes in a fenced block, unchanged.

Always say the hours are approximate (derived from Claude Code activity), and list the leave, half days and holidays the output names. Time logged by hand with `time:` lines is listed under the table, not added to it. Read-only.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
