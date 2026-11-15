---
name: plan
description: Show the Companion's Daily Planner — your tickets grouped by task stage (new, planned, review incomplete, in development, paused, testing, PR review, completed) with why each is suggested and its next action. Reads cached records only; generating, improving or approving a plan happens in the dashboard Companion page.
disable-model-invocation: true
argument-hint: "[ticket]"
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

# /cg:plan [ticket]

The user typed: `$ARGUMENTS`

Run exactly one of these with `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli companion …` and answer from its
output. Both read cached records: no model call, no Jira, Git or database call, nothing written.

| They asked for | Run |
|---|---|
| nothing, or the planner | `cli companion planner` (`--cursor C` for the next page, `--group <stage>` for one stage) |
| `<ticket>` | `cli companion resume <TICKET>` — that task's plan state and next action |

Rules:

1. Show the planner as it is: the stage groups, each ticket's next action and its "Why" line. Unknown priority,
   due date or requirements stay "Unavailable"; never guess them.
2. When a ticket has several task attempts the output lists them: show the list and ask which one. Never pick one.
3. **Generate Plan, Improve Plan, Execute Plan and every other change are separate, explicit actions in the
   dashboard Companion page** (`/cg:dashboard`, then Companion). Say so; never generate, improve, evaluate or
   approve a plan from this command, and never write a plan yourself.
4. Selecting a ticket here changes nothing; it is only what you look at.

Never edit files in the data home.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
