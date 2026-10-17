---
name: ticket
description: Everything Chitragupta has recorded for a ticket - summary, root causes, time, saved requests, DB flow, artifacts and file paths.
disable-model-invocation: true
argument-hint: "<TICKET-KEY>"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli ticket *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli ticket *)
  - Read
  - Grep
  - Glob
---

# /cg:ticket <KEY>

!`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli ticket '$0'`

If no key was given, ask for one and stop. Otherwise:

1. Give the summary above in a few lines: title, status, time, TL;DR, root causes, what is recorded.
2. Read the ticket's `README.md` from the file list and add what matters most for picking the work back up: problem, root cause & findings, fix, open questions. Cite the file and date for every fact; never claim more than the files say.
3. List the useful paths (README, requests, replay, Bruno folder, DB flow, artifacts) so they can open them.
4. Mention `/cg:trace <KEY>` when a DB flow exists.

Read-only: don't edit anything in the data home.
