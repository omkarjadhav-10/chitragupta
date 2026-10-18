# Search

One full-text index over your data home answers the dashboard's Search page and command palette, `/cg:search` and the MCP server's `search` tool. It lives in `<home>\.worklog\cache\search.sqlite`: a local cache, never pushed, never the source of truth. Delete it and nothing is lost; the next tick builds it again.

## What is indexed

| Type | One result per | Opens | File to read |
|---|---|---|---|
| `ticket` | ticket: key, title, TL;DR, problem, status, repos | `#/tickets/<KEY>` | `tickets/<KEY>/README.md` |
| `finding` | finding, root cause, decision, how it works, question … | the ticket's Overview | `tickets/<KEY>/README.md` |
| `query` | saved query: purpose, SQL, tables | the Queries tab | `tickets/<KEY>/queries.md` |
| `request` | saved replay step (else a step of `requests.md`), and each captured API call | the Requests tab | `tickets/<KEY>/requests.md` (captured calls: the ticket's JSON) |
| `artifact` | artifact entry: name, purpose, kind | the Artifacts tab | the stored file, else `artifacts/INDEX.md` |
| `branch` | branch linked to the ticket: name, repo, parent, status, commit subjects | the Branches & PRs tab | `tickets/<KEY>/README.md` |
| `pr` | pull request: number, title, source → target, state | the Branches & PRs tab | `tickets/<KEY>/README.md` |
| `api` | API in the catalog: method, path, host, purpose, fields | Knowledge → APIs | `knowledge/apis.md` |
| `table` | table: name, database, purpose, columns (from the QA schema cards) | Knowledge → DB map | `knowledge/db-tables.md` |
| `dblink` | DB link: a catalog relation, or a link in the QA link map | Knowledge → DB map | `knowledge/db-map.md` |
| `integration` | integration: system, artifact, notes | Knowledge → Integrations | `knowledge/integrations.md` |
| `day` | daily file | `#/day/<date>` | `daily/<yyyy>/<mm>/<date>.md` |

Headers, request bodies and tokens of captured calls are not indexed.

## Searching

- **Words:** lowercase, accents ignored, split on anything that isn't a letter or digit (`ABC-1001` → `abc` `1001`); at most 8 words. Every word must match; the last one also matches as a prefix (`checko` finds *checkout*). Quotes, `OR`, `*` and other search syntax are treated as plain text.
- **Ranking:** SQLite FTS5 with BM25, a match in the title counting ten times one in the body; ties go to the newest. A query that is exactly a ticket key puts that ticket first. Without FTS5 in your Python's SQLite (or with `CHITRAGUPTA_SEARCH_ENGINE=scan`) a plain scan scores the same way (title hits ×10 plus body hits); tests check both give the same top result.
- **Filters:** type (one or more), ticket (also catalog entries that list it), repo, and a date range (inclusive; results without a date drop out when you give one).
- **Results:** grouped by type in the order above, at most 8 per type; each with a snippet around the first match, its ticket, repo and date, the dashboard link and the file to read.

`/cg:search` (in Claude Code) or the engine directly:

```
py -3 <plugin>/hooks/run.py cli search order total --type finding,query --ticket ABC-1001 --from 2026-10-01
py -3 <plugin>/hooks/run.py cli search --rebuild
```

`--json` prints the same result as `/api/search`; `--limit N` (default 20) caps the results.

## Keeping it current

- **After every render** (the background tick, `/cg:check`, backfills) the index is updated: sources whose size or modification time changed are re-read, and only those whose content hash changed are re-indexed; removed ones are dropped. A source is a ticket's JSON with its `requests.md`, the catalog with the QA schema cards and link map, or one daily file. A failed update is logged to `.worklog\logs\search.log` and never stops the tick.
- **Readers never write.** The dashboard, `/cg:search` and the MCP server read the file directly while it matches the data home. When it is missing or a tick hasn't caught up, they copy it into memory and apply the same update there.
- **Rebuild:** `/cg:search --rebuild` starts over. A new version that changes the index's layout rebuilds it on the next update, and so does a damaged file.

Measured on a 20-source development home: full build 0.14 s, update after a changed ticket 27 ms, a query 3 ms (p50) / 6 ms (p95). On a synthetic home 25 times larger (770 sources, 14,600 results): full build 2.1 s, update 0.1 s, a query 21 ms / 37 ms.

## Safety

The index file is written only by the background tick and `--rebuild`, through the safety layer's path guard (`paths.sqlite_connect`: inside the data home only, `ATTACH` and `VACUUM INTO` refused). Readers open it read-only. See [safety.md](safety.md).
