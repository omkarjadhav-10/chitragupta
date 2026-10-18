---
name: log
description: Change the evening Jira worklog - log extra time or a meeting, leave, a half day, a holiday, a working weekend day, or resend a day's message.
disable-model-invocation: true
argument-hint: "<what to log, e.g. 1h on ABC-1234 at 18:30: sync with the team>"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli log *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli log *)
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

# /cg:log <what>

Request: `$ARGUMENTS`

Today's work day: !`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli log --today`

Turn the request into ONE directive line. Resolve relative dates ("Monday", "yesterday", "on Friday") against the work day above:

| They say | Directive |
|---|---|
| log 1h on ABC-1234 at 18:30: synced with the team | `time: 2026-10-16 18:30 1h ABC-1234 Synced with the team on the rollout.` |
| I'm on leave Monday / Monday to Wednesday | `leave: 2026-10-20` / `leave: 2026-10-20..2026-10-22` |
| half day on Friday | `half-day: 2026-10-24` |
| 9 Nov is a holiday (Festival) | `holiday: 2026-11-09 Festival` |
| Saturday 25 Oct is a working day | `workday: 2026-10-25` |
| resend today's worklog (each request sends again) | `resend: 2026-10-17` |

`time:` details: duration `2h`, `1h 30m` or `45m`; the start time is optional; the text is their words as a short past-tense worklog line (no file paths, SQL or code identifiers).

Then run exactly one command with the directive as the arguments, for example:
`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli log leave: 2026-10-20`

The command checks the line and appends it to `inbox.md`; report what it printed. If it says "Not added", fix the line from the grammar it prints and try once more, or ask. If the request is ambiguous (which ticket, which day, how long), ask instead of guessing. Never edit `inbox.md` or any other file yourself.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
