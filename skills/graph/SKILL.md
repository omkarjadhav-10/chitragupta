---
name: graph
description: Show a ticket's Evidence Graph — each requirement read from Jira traced to its plan steps, code, tests and runs, pull requests and release evidence, with three separate statuses (plan coverage, implementation, verification), what changed, and the one next action. Reads cached records only; never inspects a repository or calls Jira or a model.
disable-model-invocation: true
argument-hint: "<ticket> [requirement]"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli graph *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli graph *)
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: py
          args: ["-3", "${CLAUDE_PLUGIN_ROOT}/hooks/run.py", "safety", "session"]
          timeout: 10
---

# /cg:graph <ticket> [requirement]

The user typed: `$ARGUMENTS`

With a ticket only, run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli graph summary <TICKET>` and answer from its
output. With a requirement as well (a `gr_` id, an alias such as `ac2`, or `R3`), run
`cli graph trace <TICKET> --requirement <R>`; add `--workflow <w_ID>` when the output says the alias is used by several
tasks. For "what changed", run `cli graph changes <TICKET>` (`--since <N>` for an earlier point). `cli graph status`
says what the index holds. When an answer ends with "More: pass cursor …", rerun the same command with `--cursor`.

Rules:

1. **Three measures, kept apart.** Plan coverage, implementation and verification are separate statuses. Never merge
   them, and never call a requirement implemented or verified beyond what the output says. A passing run applies
   only where the output says it applies (its commit and context); "Unverified" is not "Failed".
2. **Declared, observed and inferred are different.** A link a developer declared, a fact Chitragupta observed and an
   inferred suggestion are labelled as such in the output; repeat the label. Inferred links are never counted.
3. If the output says "Not available", the ticket is outside the current profile: say so and nothing more. If the
   graph is "rebuilding" or "not built", say that the background run builds it; do not run anything else.
4. Run `cli graph rebuild` only when the user explicitly asks for a rebuild. It replaces Chitragupta's own index from
   cached records and is skipped while a background run is in progress.
5. Never start coding, edit files, run tests, or create branches or PRs because of a gap the graph shows; report the
   next action it gives.

Never edit files in the data home.

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
