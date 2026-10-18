---
name: branch
description: Fix branch tracking by hand - link or unlink a branch and a ticket, mark a branch abandoned or restore it, or set a repo's or ticket's branching strategy.
disable-model-invocation: true
argument-hint: "link|unlink <repo> <branch> <KEY> · abandon|restore <repo> <branch> · strategy <repo|KEY> <name>"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli branch *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli branch *)
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

# /cg:branch <action> …

Request: `$ARGUMENTS`

Turn the request into exactly one of these and run it:

| They want | Command |
|---|---|
| this branch belongs to ABC-1234 | `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli branch link <repo> <branch> ABC-1234` |
| this branch is not ABC-1234's | `… cli branch unlink <repo> <branch> ABC-1234` |
| I abandoned this branch (wrong parent, started again) | `… cli branch abandon <repo> <branch>` |
| undo that | `… cli branch restore <repo> <branch>` |
| this repo uses qa-only | `… cli branch strategy <repo> qa-only` |
| this ticket follows release-then-qa | `… cli branch strategy ABC-1234 release-then-qa` |

`<repo>` is the repo folder's name as shown by `/cg:branches`. The command appends one line to `inbox.md`; the background agent applies it on its next tick (`/cg:check` applies it at once). Report what it printed. If it says "Not added", fix the arguments from its message or ask. Never edit `inbox.md` or any other file yourself.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
