---
name: branches
description: A ticket's branches, parents, PRs, delivery check (did every commit reach qa / release / uat / staging?) and alerts.
disable-model-invocation: true
argument-hint: "<TICKET-KEY>"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli branches *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli branches *)
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: py
          args: ["-3", "${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "safety", "session"]
          timeout: 10
---

# /cg:branches <KEY>

!`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli branches '$0'`

If no key was given, ask for one and stop. Otherwise give a compact status from the output above:

1. One line: the summary (branches, PRs, delivery).
2. The alerts first, if any, each as one plain sentence with its commits. A "pushed after PR #N merged" alert means commits that never reached the target: say so clearly.
3. Branches (parent, status, PRs) and the delivery per target, briefly.
4. If a branch is marked "abandon (superseded by …)?", offer `/cg:branch abandon <repo> <branch>`; if a branch is linked to the wrong ticket, offer `/cg:branch unlink …` / `link …`.
5. The data is as of the last background tick; `/cg:check <KEY>` fetches and refreshes now.

Read-only: never run git yourself for this, and never edit files in the data home.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
