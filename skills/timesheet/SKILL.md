---
name: timesheet
description: Hours per ticket for this week (by day) or a month (by ISO week), with leave and holidays, optionally as CSV.
disable-model-invocation: true
argument-hint: "[week|month] [YYYY-MM-DD] [csv]"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli hours *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli hours *)
---

# /cg:timesheet [week|month] [date] [csv]

This week so far:

!`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli hours --week`

Arguments: `$ARGUMENTS`

- No arguments: show the table above.
- `month`, a date or `csv`: run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli hours` with `--month` (instead of `--week`), `--date YYYY-MM-DD` (the week or month containing that day; resolve "last week" or "September" to a date yourself) and `--csv`, and show that output instead. CSV goes in a fenced block, unchanged.

Always say the hours are approximate (derived from Claude Code activity), and list the leave, half days and holidays the output names. Time logged by hand with `time:` lines is listed under the table, not added to it. Read-only.
