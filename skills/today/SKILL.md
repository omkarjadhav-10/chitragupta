---
name: today
description: Show today's tracked time per ticket and a preview of tonight's Jira worklog message (nothing is sent).
disable-model-invocation: true
argument-hint: "[YYYY-MM-DD]"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli today *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli today *)
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

# /cg:today

Today so far, from Chitragupta's data home:

!`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli today`

Present it briefly:

1. The time table as it is (times are approximate, from Claude Code activity; the background tick refreshes them every 5 minutes).
2. The Jira worklog preview: say it is what the 20:00 message would contain if the day ended now. It used no Jira call and no model, so descriptions are the recorded lines, not the condensed ones.
3. If they passed a date (`$ARGUMENTS`), run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli today --date <YYYY-MM-DD>` and show that day instead.
4. To change tonight's message (extra time, a meeting, leave, a half day), point to `/cg:log`.

If the output says Chitragupta isn't set up, say so in one line and suggest `/cg:setup`. Don't edit any file.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
