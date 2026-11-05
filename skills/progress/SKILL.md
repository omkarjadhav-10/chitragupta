---
name: progress
description: Your recorded progress - today's authored commits, committed line changes, PRs raised and merged and tickets completed, the week, a month calendar, personal records and weekly wins; or save a short note on what moved forward today.
disable-model-invocation: true
argument-hint: "[today|week|month|records|wins] [YYYY-MM-DD] | reflect \"note\""
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli progress *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli progress *)
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: py
          args: ["-3", "${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "safety", "session"]
          timeout: 10
---

# /cg:progress

Today's progress, from Chitragupta's data home:

!`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli progress today`

Present it briefly and factually:

1. The numbers as they are. Keep "unknown", "partial" and "stale" apart from zero and say why in a few words; never turn an unknown into a zero.
2. "Committed changes" counts lines in your committed, non-merge commits after the listed exclusions. It is change volume, not code quality or productivity; say so if the user reads more into it.
3. Repeat a milestone sentence only if the output has one. Don't add praise of your own, and never compare with other people.
4. If nothing is recorded, say that design, debugging, reviews, learning and helping teammates often leave no commit, and mention `/cg:progress reflect "…"`.

With arguments (`$ARGUMENTS`):

- `week`, `month`, `records` or `wins` (optionally followed by a date): run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli progress <period> [--date <YYYY-MM-DD>]` and show that instead. `wins` is ready to copy: it has no email or account id.
- A date alone: run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli progress today --date <YYYY-MM-DD>`.
- `reflect "<note>"`: run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli progress reflect "<note>"` (add `--date <YYYY-MM-DD>` for another day). It saves the user's own note for the day on this machine only, labelled self-reported. Pass their words unchanged.

If the output says no commit email or account is confirmed, point to the Profile page of the dashboard or `/cg:setup`. If Chitragupta isn't set up, say so in one line and suggest `/cg:setup`. Don't edit any file.

## Safety

Chitragupta's /cg: commands only read; the one write here is the reflection note, made by Chitragupta's own helper inside its data home. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
