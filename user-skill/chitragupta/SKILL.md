---
name: chitragupta
description: Answer questions from the user's automatic worklog (Chitragupta) at {{HOME}} — what they did on a day or week, everything known about a ticket (findings, root cause, how it works, DB queries, tables, APIs, integrations, files, decisions), standups, timesheets and handover notes; the daily Jira worklog sent to Slack at 20:00 (log extra time or a meeting, leave, half days, holidays, working weekend days, resend or show a day's worklog); replay or run a ticket's saved requests, save requests to the worklog, show or map a ticket's DB flow (which tables and columns change, from what to what), and find a ticket's artifacts. Use whenever they mention a ticket ID, ask what they did or found, what we know about something, which tables, APIs or queries were involved, want to replay / save / pin a request, map a DB flow, find an analysis doc or export, or want a standup, timesheet, weekly summary or handover, say they were on leave / took a half day / worked extra time, or ask a read-only Jira or database question about a ticket — even if they don't say "worklog".
---

# Worklog recall (Chitragupta)

The worklog at `{{HOME}}` is written automatically by a background agent (the `Chitragupta` scheduled task) from
Claude Code sessions, git commits, Bruno requests, SQL scripts, `inbox.md` and `_drop/`. Read it; don't edit it
(two exceptions below). Quick views when the Chitragupta plugin is loaded: `/cg:today`, `/cg:ticket <KEY>`,
`/cg:timesheet`, `/cg:trace <KEY>`, `/cg:log`, `/cg:dashboard`, `/cg:doctor`.

## Where things live
- `tickets/INDEX.md` — every ticket: status, first/last worked, ≈ time, TL;DR
- `tickets/<TICKET>/README.md` — the ticket hub: quick links, Scenarios (trigger request · DB effect · verify query),
  replay steps, latest artifacts, TL;DR, problem, root cause & findings, how it works, fix, database, APIs,
  integrations, config, errors, repro, decisions, open questions, code map, timeline
- `tickets/<TICKET>/queries.md` — important queries, dated, with what each revealed
- `tickets/<TICKET>/requests.md` — the minimal replay set as curl for Git Bash (purpose + expected per step)
- `tickets/<TICKET>/replay.sh` — run it: `bash tickets/<TICKET>/replay.sh qa` (or `local`) from the worklog folder
- `bruno/<TICKET>/` — the same requests in the single Bruno collection `{{HOME}}\bruno` (envs in `bruno/environments`,
  secrets in `bruno/.env`, git-ignored)
- `tickets/<TICKET>/db-flow.md` — DB flow: Mermaid flow + sequence, table map, column changes (before → after,
  set by, condition, file:line), relationships, verify queries, unknowns & conflicts
- `knowledge/db-map.md` — all tables across tickets, relationships, which components write which columns
- `tickets/<TICKET>/artifacts/INDEX.md` — every artifact (analysis docs, HTML pages, diagrams, payloads, scripts,
  exports, screenshots, published links) with date, purpose, source and where it is stored (`local/` = this machine only)
- `knowledge/db-tables.md`, `knowledge/apis.md` (with a "Replay in" column), `knowledge/integrations.md` — catalogs
- `knowledge/branches.md` — every local branch: parent, created, env status (qa / uat / staging / master)
- `daily/YYYY/MM/YYYY-MM-DD.md` — per day: summary, per-ticket work with times (incl. requests saved, DB flow
  updates, new artifacts), other work, outside Claude Code, commits, standup. A work day starts at `day_starts_at`
  in `.worklog/config.json`.
- `data/` — canonical JSON behind the markdown (useful for exact lookups)

## How to answer
1. Search in this order: ticket README → requests.md / db-flow.md / artifacts/INDEX.md → queries.md → knowledge/ →
   daily files by date → grep across the folder (ticket keys look like ABC-1234; the prefixes are `ticket_prefixes`
   in `.worklog/config.json`).
2. Cite the file and date for every fact, e.g. "(tickets/ABC-1234/README.md, 2026-10-14)". Quote queries in
   fenced code blocks exactly as recorded.
3. Never claim more than the files say. If something isn't recorded, say so plainly.
4. Standups: use the "Standup for next working day" section of the latest daily file; if it is a draft, build
   Yesterday/Today/Blockers from that day's ticket sections, one line each with ticket refs.
5. Timesheets: sum the "≈ Time" column of each daily file's Tickets table per ticket and day (or run
   `py -3 "{{LAUNCHER}}" cli hours --week` / `--month`); label totals as approximate (derived from Claude Code activity).
6. Weekly summaries and handovers: build from daily files for the range and the ticket READMEs.
7. "Replay / run the requests for <TICKET>": point to `bash tickets/<TICKET>/replay.sh qa` (and Bruno folder
   `bruno/<TICKET>`); you may run replay.sh yourself when asked. If it stops on a missing variable, the name to fill is in
   `bruno/.env.example`; the user fills `bruno/.env` themselves — never write secrets anywhere.

## Saving requests (only when the user asks to save / pin requests to the worklog)
End your final reply with one fenced block; the agent files it exactly as written and pins the requests:

```worklog-requests
{"ticket": "ABC-1234", "scenario": "null role locks user",
 "requests": [
  {"name": "Login", "method": "POST", "url": "https://api.qa.acme.test/api/v1/auth/login",
   "headers": {"Content-Type": "application/json"},
   "body": {"email": "{{process.env.ABC1234_EMAIL}}", "password": "{{process.env.ABC1234_PASSWORD}}"},
   "expected": "200 with access_token", "purpose": "get a token"},
  {"name": "Partner update - null role", "method": "PUT", "url": "https://api.qa.acme.test/api/v1/customers/{id}",
   "headers": {"authorization": "api-key {{process.env.PARTNER_API_KEY}}"},
   "body": {"firstName": "{{process.env.ABC1234_FIRST_NAME}}", "role": null},
   "expected": "200; is_locked -> true", "critical_fields": ["role = null"]}
 ]}
```
Strict JSON. Credentials, tokens, API keys and person values (names, dates of birth, phone, email, address,
customer / account IDs) are always `{{process.env.NAME}}` (ticket-scoped names like ABC1234_FIRST_NAME); everything
else literal — keep JSON null as null. Use the real captured URL and body; never invent fields.

## Mapping a DB flow (only when the user asks, e.g. "map the DB flow for ABC-1234")
Trace the code path: entry point (controller / listener / integration channel / job) → services → repositories / SQL /
procs → tables and columns. Answer in prose, then end the final reply with one fenced block:

```worklog-dbflow
{"ticket": "ABC-1234", "overview": "2-4 sentences",
 "data_flow": [
  {"scenario": "null role locks user", "step": 1, "trigger": "PUT /api/v1/customers/{id}",
   "component": "CustomerService.update (CustomerService.java:409)", "operation": "update",
   "table": "shop.customers", "columns_read": ["role"],
   "columns_written": [{"column": "is_locked", "change": "false -> true", "condition": "isBlank(payload.role)"}],
   "via": "JPA/EF entity", "evidence": ["CustomerService.java:409"], "confidence": "confirmed"}
 ],
 "table_relations": [{"from": "shop.login_history.customer_id", "to": "shop.customers.id", "kind": "fk",
   "evidence": "LoginHistory.java:22"}]}
```
operation: select | insert | update | delete | upsert | merge | proc. confidence: confirmed (read in code / seen in
data) | likely | hypothesis. Every entry carries evidence (file:line, query, log line). Emit these blocks only when
asked to save or map something, and only in the final reply.

## Jira worklog (Slack DM at 20:00) — time, leave, holidays
Every workday at 20:00 the agent DMs the user their day as Jira worklog entries (by default 8h, 30-min steps, from
10:30, 1h lunch near 14:00 — see `jira_worklog` in `.worklog/config.json`; saved in `data/timesheets/<day>.json` and the
daily file's "Jira worklog" section, even when Slack isn't connected). They change it only by telling you; resolve
relative dates against today (mind `day_starts_at`) and append ONE line to `inbox.md` (or run
`py -3 "{{LAUNCHER}}" cli log <line>`, which checks the line first):
- "log 1h on ABC-1234 at 18:30: connected with Alex" → `time: 2026-10-16 18:30 1h ABC-1234 Connected with Alex.`
  (duration `2h`, `1h 30m`, `45m`; start optional; the text is their words as a past-tense worklog line)
- "I'm on leave Monday" → `leave: 2026-10-20` · a range → `leave: 2026-10-20..2026-10-22`
- "half day on Friday" → `half-day: 2026-10-24` · "holiday on 9 Nov" → `holiday: 2026-11-09 Festival`
- "Saturday 25 Oct is a working day" → `workday: 2026-10-25`
- "resend today's worklog" → `resend: <day>`
A line for a day already messaged (up to 3 days back) makes the agent send one "Corrected update" 15 min later.
"Show today's worklog" is a read-only preview (nothing sent, no Jira call): `/cg:today`, or
`py -3 "{{LAUNCHER}}" agent worklog-show [YYYY-MM-DD]`

## Read-only MCP use (Jira, the database MCP)
When asked ("status of ABC-1234?", "which proc sets is_locked?") you may use the Jira MCP read tools (`jira_get_issue`,
`jira_search`, `jira_get_worklog`, `jira_batch_get_changelogs`) and the database MCP's read tools (describe a table,
list schemas / tables / connections, and run one SELECT on a QA connection, TOP/LIMIT <= 20). Never a write tool
(comment, transition, log work, create / update / delete, write requests), never a non-SELECT statement, never
production. Cached copies: `data/jira/<KEY>.json`, `data/db-schema/<connection>/<table>.json`, `data/qa-trace/<KEY>.json`.

## Storage
Only `daily/**` and `README.md` go to GitHub (when a remote is configured). Everything else (tickets/, knowledge/,
bruno/, data/, .worklog/) is a local knowledge base on this machine; QA values may appear in it literally, production
values never.

## The only writes allowed (only when the user explicitly asks)
- Append one line to `{{HOME}}\inbox.md` (prefix `TICKET-123:` to attach it; `ABC-1234: pin curl ...` pins a request;
  `time:` / `leave:` / `half-day:` / `holiday:` / `workday:` / `resend:` lines for the Jira worklog, see above).
- Create a file in `{{HOME}}\_drop\` whose name starts with the ticket ID, e.g. `ABC-1234_lock-analysis.md`
  ("save this analysis to the worklog for ABC-1234"); the agent files it under the ticket's artifacts.
Never edit any other worklog file.
