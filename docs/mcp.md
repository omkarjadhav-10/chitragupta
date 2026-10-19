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
| `get_ticket` | `key`, `section?` | Summary (title, status, dates, time, repos, TL;DR, counts, branches, alerts). With `section`, one part in full: `findings`, `requests`, `queries`, `dbflow`, `branches`, `artifacts`, `timeline` or `notes` (your My notes block) |
| `get_day` | `date` (`YYYY-MM-DD`, `today`, `yesterday`, `-N`) | Time per ticket, other work, commits, and the evening Jira worklog as sent (or a preview when it isn't sent yet) |
| `get_timesheet` | `period` (`week`, `month`, `day`), `from?`, `to?` | Hours per ticket per period, totals, and leave or holidays in the range. Without `from`, the current week, month or day |
| `get_branch_status` | `key`, `repo?` | Branches and their parents, PRs, delivery to each target branch, and alerts (the same view as `/cg:branches`) |
| `get_db_table` | `name` (`table` or `schema.table`) | Columns, relations, QA link-map matches (`m/s verified · likely · unverified`), the code that reads or writes it, tickets, and saved queries that use it |
| `list_tickets` | `q?`, `status?`, `repo?`, `since?` | One line per ticket, newest activity first |
| `get_alerts` | none | Branch and PR delivery alerts across tickets, and worklog health warnings (paused capture, a stale tick, the last error, safety blocks) |

Every tool also takes `page`. An answer is at most 8,000 characters. A longer one is cut at a line break and ends with
`… more: page=2 (of 3)`.

Examples of what Claude calls:

```text
search(q="discount total", ticket="ABC-1234")
get_ticket(key="ABC-1234", section="dbflow")
get_day(date="yesterday")
get_timesheet(period="week", from="2026-09-01")
get_db_table(name="shop.orders")
```

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
  8 names (~320 characters with their `mcp__plugin_cg_chitragupta__` prefix) plus 255 characters of instructions.
  That is about **140 tokens** per session. A tool's schema loads only when Claude looks it up.
- **When tools load upfront** (tool search off, or a `claude -p` run without the tool-search tool), the full
  `tools/list` result is 2,760 characters of compact JSON, about **700 tokens**. Claude Code's own wrapping
  brings it to roughly 800–900.
- **Answers** are capped at 8,000 characters (about 2,000 tokens) per call, well under Claude Code's 10,000-token
  warning for MCP output.
