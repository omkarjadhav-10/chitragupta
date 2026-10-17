---
name: pause
description: Pause or resume Chitragupta's capture, or show whether it is paused.
disable-model-invocation: true
argument-hint: "[resume|status]"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli pause)
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli resume)
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli status)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli pause)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli resume)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli status)
---

# /cg:pause [resume|status]

Argument: `$ARGUMENTS`

Run exactly one command and report its output in one or two lines:

- no argument, or `on` → `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli pause`
- `resume`, `off` or `unpause` → `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli resume`
- `status` → `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli status`

While paused, the hooks record nothing and the background tick skips ingesting new work (housekeeping still runs). Already-recorded data is untouched. Remind them to resume with `/cg:pause resume`.
