---
name: ticket
description: Everything Chitragupta has recorded for a ticket - summary, root causes, time, saved requests, DB flow, artifacts, branches and file paths; --export saves the whole page as one HTML file.
disable-model-invocation: true
argument-hint: "<TICKET-KEY> [--export [--mode shareable]]"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli ticket *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli ticket *)
  - Read
  - Grep
  - Glob
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

# /cg:ticket <KEY>

!`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli ticket '$0'`

If no key was given, ask for one and stop.

If they asked to export (`--export`, "export", "save as HTML"), run
`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli ticket <KEY> --export --mode full` (or `--mode shareable` for a copy to share:
public redaction, no QA values, no internal URLs or paths) and report the file path it printed. Otherwise:

1. Give the summary above in a few lines: title, status, time, TL;DR, root causes, what is recorded.
2. Read the ticket's `README.md` from the file list and add what matters most for picking the work back up: problem, root cause & findings, fix, open questions. Cite the file and date for every fact; never claim more than the files say.
3. List the useful paths (README, requests, replay, Bruno folder, DB flow, artifacts) so they can open them.
4. Mention `/cg:trace <KEY>` when a DB flow exists, and `/cg:branches <KEY>` for branches, PRs and the delivery check.

Read-only: don't edit anything in the data home.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
