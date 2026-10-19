---
name: doctor
description: Check that Chitragupta is healthy - hooks firing, background tick, runtime, scheduled task, recall skill, CLAUDE.md block, Slack, Jira and database MCP status, recent errors. Diagnoses only; changes nothing.
disable-model-invocation: true
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" doctor *)
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli where *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" doctor *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli where *)
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: py
          args: ["-3", "${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "safety", "session"]
          timeout: 10
---

# /cg:doctor

!`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli where`

Then run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" doctor` (it exits 1 when a check fails; that is expected, read its output). Report in this order, briefly:

1. Overall: "all checks passed" or how many failed.
2. Each failing row: what it means and the one fix, usually `/cg:setup` (re-running setup repairs the runtime, task, skill and CLAUDE.md block without touching data). Slack, Jira or the database MCP not being connected is a choice, not a failure — say what it costs (no Slack DM; titles from branch names; no QA examples).
3. Where things live (data home, runtime), from the first block.

Don't run any repair yourself, and don't edit files.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
