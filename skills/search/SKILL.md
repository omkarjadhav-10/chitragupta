---
name: search
description: Search everything Chitragupta recorded - tickets, findings, queries, requests, artifacts, branches, PRs, tables, APIs, DB links, integrations and daily notes - and show the top results with the file to read for each; --rebuild rebuilds the search index.
disable-model-invocation: true
argument-hint: "<words> [--type ticket,finding,…] [--ticket KEY] [--repo NAME] [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--rebuild]"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli search *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli search *)
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

# /cg:search <words>

Request: `$ARGUMENTS`

If nothing was asked, ask what to search for and stop. Otherwise run exactly one command with the request as its arguments, each search word or option value as a separate, quoted argument, for example:
`py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" cli search 'order total' --type finding,query --ticket ABC-1234`

Options: `--type` (ticket, finding, query, request, artifact, branch, pr, api, table, dblink, integration, day; comma-separated), `--ticket KEY`, `--repo NAME`, `--from` / `--to` (YYYY-MM-DD, inclusive), `--limit N` (default 20), `--rebuild` (rebuild the index first; alone, it only rebuilds).

Then present the results briefly, grouped as printed: the title, the ticket and date, and the matching snippet. Keep each result's `file:` path: it is the file to read for detail. Read one only when they ask for more about a result. If nothing matched, say so and suggest fewer or different words.

The index is a local cache that the background tick refreshes after every render; `--rebuild` rewrites it from the data home (nothing else is changed).

## Safety

Chitragupta's /cg: commands only read. While this command runs, Chitragupta's safety guard allows only read tools and Chitragupta's own helper (`run.py`): no file edits, no git, no `gh` / `glab` / `az` writes, no Jira or database writes, nothing else. If a step needs one of those, tell the user instead of trying.
