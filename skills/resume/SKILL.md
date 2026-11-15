---
name: resume
description: Read a task's resume brief — its stage, approved plan, evidence-backed completed / pending / uncertain steps, what changed since you last reviewed, and the one next action. Reads cached records only. It does not resume capture (that is /cg:pause resume) and never starts coding.
disable-model-invocation: true
argument-hint: "<ticket or w_ workflow ID>"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli companion *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli companion *)
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: py
          args: ["-3", "${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "safety", "session"]
          timeout: 10
---

# /cg:resume [ticket]

The user typed: `$ARGUMENTS`

Run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli companion resume <TICKET or w_ID>` and answer from its output.
Without an argument, run `cli companion planner` and ask which task to resume. Related read-only views:
`cli companion review <w_ID>` (Jev Review choices), `cli companion verification <w_ID>` (requirement ledger),
`cli companion handoff <w_ID>` (the approved copy packet, "Prepared; not submitted").

Rules:

1. **This is a task resume brief, not capture.** It does not resume capture: that is `/cg:pause resume`. Never run
   `cli resume` or `cli pause` for this command.
2. **Never start coding.** Do not edit files, run tests, create branches or PRs, or continue the plan's steps. Resume
   Execution and Execute Plan are explicit actions in the dashboard Companion page; implementation happens only in a
   separate ordinary turn the user starts with the copied handoff.
3. When the ticket has several task attempts, the output lists them: show the list and ask which one. Never pick.
4. Report what the output says: the next action, completed / pending / uncertain steps, and the changes since the last
   review with their provenance. Plan coverage, implementation progress and functional verification are three
   different measures; keep them apart, and never call a step done or a criterion verified beyond the output.
5. Reading never marks changes reviewed: say that "Mark changes reviewed" is in the dashboard Companion page.

Never edit files in the data home.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
