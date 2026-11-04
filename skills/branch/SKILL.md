---
name: branch
description: Fix branch tracking by hand - link or unlink a branch and a ticket, mark a branch abandoned or restore it, set a repo's or ticket's branching strategy, or record release decisions (which branch ships a Jira release, which fix version counts, which repos a ticket touches, whether it needs UAT).
disable-model-invocation: true
argument-hint: "link|unlink <repo> <branch> <KEY> · abandon|restore <repo> <branch> · strategy <repo|KEY> <name> · release-map <repo> <release> <branch> · release-pick|release-branch|release-repos|uat <KEY> …"
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
| the "FY99 December 14th Release" ships from release/ABC-9000/7th-dec-release in service-api | `… cli branch release-map service-api "FY99 December 14th Release" release/ABC-9000/7th-dec-release` (the Jira version id works too) |
| forget that mapping | `… cli branch release-unmap service-api "FY99 December 14th Release"` |
| of its two fix versions, ABC-1234 ships in 10001 | `… cli branch release-pick ABC-1234 10001` (`-` clears) |
| only for ABC-1234, service-ui uses another branch | `… cli branch release-branch ABC-1234 service-ui <branch>` (`-` clears) |
| ABC-1234 only touches service-api | `… cli branch release-repos ABC-1234 service-api` (`-` clears) |
| ABC-1234 needs UAT / doesn't | `… cli branch uat ABC-1234 required` / `not_required` / `unknown` |

`<repo>` is the repo folder's name as shown by `/cg:branches`. Keep a release name with spaces in double quotes. The command appends one line to `inbox.md`; the background agent applies it on its next tick (`/cg:check` applies it at once). Report what it printed. If it says "Not added", fix the arguments from its message or ask. Never edit `inbox.md` or any other file yourself.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
