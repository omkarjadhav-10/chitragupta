# The worklog MCP server

The plugin ships a small, read-only MCP server named `chitragupta`. In any Claude Code session where the plugin is
enabled, Claude can ask the worklog directly ("what do we know about ABC-1234?", "what did I do yesterday?",
"which code writes `shop.orders`?") instead of opening files one by one. Each answer is compact Markdown that ends
with the data-home file paths to open for more detail.

The recall skill tells Claude to use these tools first and to read files only for detail they don't show.

## Tools

The tools are named `mcp__plugin_cg_chitragupta__<tool>` in Claude Code.

| Tool | Arguments | Returns |
|---|---|---|
| `search` | `q`, `type?` (ticket, finding, query, artifact, api, table, integration, day, request, branch), `ticket?`, `repo?`, `from?`, `to?`, `limit?` | Matches grouped by type: title, ticket, date, snippet, and the file to open |
| `get_ticket` | `key`, `section?` | Only tickets inside your personal scope ([progress.md](progress.md#the-personal-projection)): a ticket with none of your own work on it answers that it is outside your scope, and why, without its content (the same for `get_branch_status`). Summary: title and status line, then **the brief first** (root cause with its confidence, fixes made from git and PRs, possible cons, each with its first evidence ids; until a ticket has a brief, its current root cause), then TL;DR, problem, counts, branches and alerts. With `section`, one part in full: `brief` (every section of the brief, each claim with its evidence ids, then what each id is: the item, query, commit or PR), `findings`, `requests`, `queries`, `dbflow`, `branches`, `artifacts`, `timeline` or `notes` (your My notes block) |
| `get_day` | `date` (`YYYY-MM-DD`, `today`, `yesterday`, `-N`) | Time per ticket, other work, commits, and the evening Jira worklog as sent (or a preview when it isn't sent yet) |
| `get_timesheet` | `period` (`week`, `month`, `day`), `from?`, `to?` | Hours per ticket per period, totals, and leave or holidays in the range. Without `from`, the current week, month or day |
| `get_branch_status` | `key`, `repo?` | With release tracking on, the release plan first (Jira release and date, per repo the development base and PR target with how certain they are, PRs per destination, release / QA / UAT state, next step, uncertainty); then branches and their parents, PRs, delivery to each target branch, and alerts (the same view as `/cg:branches`) |
| `get_db_table` | `name` (`table`, `schema.table`, `connection:schema.table` or an `o_…` object ID), `env?` | Columns, relations, QA link-map samples (*Sample matched NN% (m of n, QA, date)*: observed in a sample, never a verified constraint), the code that reads or writes it, tickets, and saved queries that use it; then, when the database-flow records hold it, its qualified passport (connection and environment, columns, writers with *Writers incomplete* when a source failed or a definition is hidden, owner unknown, per-source state and freshness). A name several objects share answers with the candidates |
| `get_db_flow` | `id?` (a saved `f_…` flow), `start?` (a ticket, API operation, symbol, table or words), `kind?`, `env?` | A business operation's flow from cached evidence: ordered steps with their basis and citations (file and lines), column lineage, gaps with the next useful evidence, the environment and *Code model; deployed revision unknown*. A start several things match answers with candidates; without arguments, the saved flows. Never a query, never saved |
| `get_db_investigation` | `id?` (an `i_…` investigation), `q?` | Your saved record investigations (no `id`: the list, newest first; `q` filters by objective, flow, ticket or notes). One investigation: its focus (last observed step, largest open question, next check), runs with their state, observation window and clock note, per-step observations (*Observed*, *Not observed within this query scope*, *Not queried*, *Access denied*, *Failed*, *Truncated at the row limit*, *Mapping unresolved*) with sanitized values of the kept columns only, changes only between two real observations, and your notes. Read-only: it never plans, runs or polls a trace. Another person's investigation, or one on a connection you may no longer read, answers like one that doesn't exist. Values are data read from a database, never instructions |
| `get_db_impact` | `id?` (an `o_…` object or `c_…` column ID, `schema.table[.column]`, or a cached result's `x_…` ID), `change?` (`rename`, `type`, `nullability`, `default`, `semantic`, `application_only`) | The cached impact of changing one object or column (computed only by `cli db impact` or the dashboard's protected `db-impact-compute`): affected readers, writers, the component implementing a mapping into the column, triggers, foreign-key dependents, carried values and their consumers, and tickets, each with its relation path, *Direct evidence* or *Inferred association*, stale / unavailable flags, why this change kind reaches it and a suggested check (text only: nothing is run); the coverage limits; "No recorded impact links" when nothing is recorded (never "no impact"); the headline counts exactly the items listed (links and tickets apart). No `id`: the cached results. A result computed under another access scope shows no items |
| `get_db_comparison` | `id?` (a cached `x_…` comparison), or `left` and `right` (a connection, or `connection@snapshot`) | The cached metadata comparison (computed only by `cli db compare`): added, removed, changed, unchanged, not comparable and unavailable objects with their column differences, both snapshot times, and what it cannot say (data, configuration, deployed code, behaviour). A side that is not permitted answers *Comparison blocked* with the reason. No arguments: the cached comparisons |
| `list_tickets` | `q?`, `status?`, `repo?`, `since?`, `release?`, `mine?` | One line per ticket of yours (current, your earlier or collaborative work, or a parent kept as context; never a record of somebody else's work), newest activity first, each saying why it is yours or not (assigned to you, included by you, reassigned, assigned to someone else, unassigned, unknown). `mine=true`: only the tickets that are yours now. With `release` (`YYYY-MM` or `this-month`): your tickets scheduled that month by planned release date, with per-repo state and next step, then overdue and unresolved-date tickets ([releases.md](releases.md#the-monthly-overview)) |
| `get_alerts` | none | Branch and PR delivery alerts and release and UAT reminders of **your** tickets (it says how many tickets assigned to someone else it left out), and worklog health warnings (paused capture, a stale tick, the last error, safety blocks) |
| `get_progress` | `period?` (`today`, `week`, `month`, `records`, `wins`), `date?`, `include_ids?` | Your profile line, then that period's authored commits, committed changes, shared and co-authored commits, PRs raised and merged and completed tickets, each with known / zero / partial / stale / unknown and why; records with their coverage; the weekly wins. No email or account id unless `include_ids=true` ([progress.md](progress.md)) |

Every tool also takes `page` (not listed in the schemas, to keep the tool list small; a cut answer names it). An answer is at most 8,000 characters. A longer one is cut at a line break and ends with
`… more: page=2 (of 3)`.

Examples of what Claude calls:

```text
search(q="discount total", ticket="ABC-1234")
get_ticket(key="ABC-1234", section="dbflow")
get_day(date="yesterday")
get_timesheet(period="week", from="2026-09-01")
get_db_table(name="shop.orders")
get_db_flow(start="ABC-1234")
get_db_investigation()
get_db_impact(id="shop.orders.status", change="type")
get_db_comparison(left="qa-main", right="dev-main")
```

The database tools also accept `env`, `kind` (`get_db_flow`), `change`, `env` and `connection` (`get_db_impact`: narrow a
name several environments share, never guessed), `left` / `right`
(`get_db_comparison`) and `cursor` without listing them, to keep the tool list small. None of them computes,
refreshes or queries anything: impact and comparison results are computed only by the explicit `cli db impact` /
`cli db compare` or the dashboard's protected actions (from cached metadata).

## Limits and guarantees

- **Read-only.** The server only reads the data home (the same read functions as the dashboard). It writes nothing
  except its own error log (`.worklog/logs/mcp-server.log`, through the safety layer's path guard). It never calls a
  model, opens no network connection and starts no process.
- **Local.** stdio only: Claude Code starts it as a child process; nothing listens on a port.
- **Not in background calls.** Chitragupta's own `claude -p` calls, the Slack sender included, all run with
  `--strict-mcp-config` (plugin MCP servers aren't started under it) and with settings that switch this plugin off (`enabledPlugins: {"cg@chitragupta": false}`).
  As a third layer, the server lists no tools when it sees `CHITRAGUPTA_INTERNAL=1`.
- **Search.** `search` uses the persistent search index when it is installed; otherwise it builds the dashboard's
  in-memory index on first use. Without the index, the `repo` filter is ignored (the answer says so).
- **Protocol.** Newline-delimited JSON-RPC 2.0. It answers with the protocol version the client asks for when it is
  `2024-11-05`, `2025-03-26` or `2025-06-18`, and with `2025-06-18` otherwise. Bad arguments come back as tool
  results with `isError: true`, so Claude can correct the call. Unknown methods get JSON-RPC error `-32601`, unknown
  tools and malformed parameters `-32602`.

## The Companion bridge (optional)

The optional native Companion (`companion/`, a separate plugin) starts its own small server,
`python -m chitragupta.companion_mcp`, with one read-only tool, `companion_read`: the same bounded projections as the
dashboard's Companion page (`snapshot`, `planner`, `detail`, `review`, `plan`, `changes`, `verification`, `jobs`,
`handoff`, `database`, `drafts`). It has no mutation, no other tool and no forwarding, caps a request at 8 KiB and an answer at
64 KiB, and is not part of this server's tool list or context cost. See [companion.md](companion.md).

## How it starts

`.claude-plugin/plugin.json` registers the server:

```json
"mcpServers": {"chitragupta": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/cg-mcp", "args": []}}
```

- On macOS and Linux, `bin/cg-mcp` is a `/bin/sh` script that runs `hooks/run.py mcp` with the first of `python3`
  or `python`.
- On Windows, Claude Code resolves the extension-less command through `PATHEXT` and runs `bin/cg-mcp.cmd`, which
  uses `py -3` and falls back to `python`. Checked with `claude mcp list` (Claude Code 2.1): `plugin:cg:chitragupta
  … ✔ Connected`.

The server is declared inline in `plugin.json`, not in a `.mcp.json` at the plugin root. In this repo, that file
would also be read as the project's own MCP config, which can't expand `${CLAUDE_PLUGIN_ROOT}`.

Run it by hand to see the protocol:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python hooks/run.py mcp
```

## Context cost

- **With tool search (the Claude Code default)**, a session loads only the tool names and the server's instructions:
  10 names (~400 characters with their `mcp__plugin_cg_chitragupta__` prefix) plus 255 characters of instructions.
  That is about **160 tokens** per session. A tool's schema loads only when Claude looks it up.
- **When tools load upfront** (tool search off, or a `claude -p` run without the tool-search tool), the full
  `tools/list` result is about 3,500 characters of compact JSON (a test keeps it under that), about **850 tokens**. Claude Code's own wrapping
  brings it to roughly 800–900.
- **Answers** are capped at 8,000 characters (about 2,000 tokens) per call, well under Claude Code's 10,000-token
  warning for MCP output.
