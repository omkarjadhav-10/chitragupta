---
name: trace
description: Show a ticket's DB flow (tables and columns it changes, before and after) and its QA worked example; can fetch a fresh example when a database MCP is configured.
disable-model-invocation: true
argument-hint: "<TICKET-KEY>"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli trace *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli trace *)
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: py
          args: ["-3", "${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "safety", "session"]
          timeout: 10
---

# /cg:trace <KEY>

!`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli trace '$0'`

If no key was given, ask for one and stop. Otherwise:

1. Summarise the DB flow: the trigger, then table by table what is read and written, each column change as `before -> after` with its condition and `file:line`. Keep the Mermaid blocks only if they asked for the diagram.
2. Show the QA worked example (the actual values seen on QA) if one is saved.
3. If there is no DB flow yet, offer to map it: trace the code path and end the reply with a `worklog-dbflow` block, as the chitragupta skill describes.
4. Only when the last line says `database_mcp: configured`, offer to fetch a fresh QA example with
   `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" agent qa-trace <KEY>` (read-only SELECTs on QA through the database MCP; takes a minute or two). Run it only after they say yes, then run `cli trace` again. When it says `not configured`, don't offer it.

Never edit files in the data home.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
