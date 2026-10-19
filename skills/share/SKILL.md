---
name: share
description: Share a ticket's notes with your team - pick sections, see exactly what is removed (secrets, patient data, QA values, local paths, My notes), then write a redacted bundle to your team's share folder or Git repo. Also lists your team's shared notes.
disable-model-invocation: true
argument-hint: "<TICKET-KEY> [sections] [--level team|public] [--target NAME] | --notes"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli share *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli share *)
  - Read
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: py
          args: ["-3", "${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "safety", "session"]
          timeout: 10
---

# /cg:share <KEY>

If they asked for the team's notes (`--notes`, "what has the team shared"), run
`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli share --notes` and list them briefly (newest first: date, ticket, title, who). Stop.

If no ticket key was given, ask for one and stop.

1. Preview first, always. Run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli share <KEY>` with `--sections a,b,…` when they named sections (summary, findings, requests, queries, dbflow, branches, artifacts, timeline, notes, or `findings.<kind>` such as `findings.root_cause`) and `--level public` only when they asked for a public copy. Nothing is written.
2. Show them: the level, the sections, what was removed from each section, what is never included, and the bundle's first lines. If it says BLOCKED, show each hit (section, line, rule) and stop: nothing was written, and the fix is to leave that section out or remove the text from the ticket's notes.
3. Ask whether to share it. Only after a clear yes, run the same command with `--yes`, and report where it was written and whether it was committed and pushed.

Sharing writes only to the share target in `share.targets` (a folder, or a team Git repo where only `shares/` is committed and pushed, never forced). It never posts to Slack. If no target is configured, say so and point to docs/sharing.md; don't configure one yourself.
