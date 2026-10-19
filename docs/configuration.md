# Configuration

Chitragupta's settings live in one file, `<data home>\.worklog\config.json`. Setup writes it. After that you can edit it by hand: the background tick and the hooks read it again each time they run.

Any key you leave out takes its default. Setup only merges into the file: it never removes a key you added, lists only grow, and a value changes only when you give a new one. It saves a backup to `.worklog\backups\` each time.

Keep secrets out of this file. Passwords and API keys go in environment variables or `bruno\.env`, never in `config.json`.

A minimal config after setup:

```json
{
  "worklog_home": "C:/Users/you/Chitragupta",
  "ticket_prefixes": ["ABC"],
  "watched_repos": ["C:/src/shop-api"],
  "bruno_collections": ["C:/src/shop-api/bruno"],
  "github_remote": "",
  "paths": {"python": "C:/Python312/python.exe", "pythonw": "C:/Python312/pythonw.exe",
            "claude": "C:/Users/you/.local/bin/claude.exe", "git": "C:/Program Files/Git/cmd/git.exe"}
}
```

## Core

| Key | Type | Default | Meaning / example |
|---|---|---|---|
| `worklog_home` | string | the resolved data home | Absolute path of the data home. Informational; the home itself is resolved as described [below](#data-home-resolution). |
| `github_remote` | string | `""` | The only push target, for example `https://github.com/you/worklog`. Empty means nothing is pushed. A local folder or `file://` remote also works. It must be a **private** GitHub repo; see [privacy.md](privacy.md). |
| `ticket_prefixes` | list of strings | `[]` | Jira project keys, for example `["ABC", "OPS"]`. Grows by itself when a branch names a new UPPERCASE prefix. |
| `capture_scope.include_paths` | list of paths | `[]` | If not empty, capture happens only under these folders. Empty means everywhere. |
| `capture_scope.exclude_paths` | list of paths | `[]` | Never capture under these folders. The data home itself is always time-only (see [privacy.md](privacy.md)). |
| `watched_repos` | list of paths | `[]` | Repos scanned for your commits and branch activity. Grows by itself from sessions. |
| `git_author_emails` | list of strings | `[]` | Your commit emails, for example `["you@acme.test"]`. They filter "my commits" and identify you in Jira facts. The last one is also the default Slack lookup email. |
| `bruno_collections` | list of paths | `[]` | Bruno collections outside the data home. Request edits there show up under "Outside Claude Code". |
| `sql_script_dirs` | list of paths | `[]` | Folders of `.sql` scripts to watch. |
| `sql_script_globs` | list of globs | `[]` | Glob patterns for more SQL script folders, for example `["C:/Users/you/AppData/Roaming/SomeIDE/consoles/db/*"]`. |
| `env_branches` | list of strings | `["qa","uat","staging","master","main","develop"]` | Base branches of watched repos that have no `branching.repos` entry. |
| `branch_fetch` | bool | — | Retired in v0.2.0 (it pruned). Fetching is `branching.fetch`. |
| `day_starts_at` | `"HH:MM"` | `"04:00"` | Work-day boundary. Work before this time counts as the previous day. |
| `push_enabled` | bool | `true` | `false` keeps the daily commits on this machine; they are pushed once it's `true` again. |
| `push_interval_minutes` | int | `30` | Minimum gap between pushes. A push also happens when a session ends or a day closes. |
| `raw_retention_days` | int | `30` | Raw event files older than this are pruned, but only after they have been processed. |
| `extract_triggers.min_new_chars` | int | `1500` | A session is extracted once it has at least this much new text, **and** one of the next two triggers fires or the session has ended. |
| `extract_triggers.idle_minutes` | int | `10` | Extract after this many idle minutes. |
| `extract_triggers.max_interval_minutes` | int | `20` | Extract at least this often while a session is active. |
| `extra_sensitive_patterns` | list of regexes | `[]` | Extra patterns to mask everywhere, for example `["\\bINTERNAL-[0-9]+\\b"]`. |
| `allow_patterns` | list of regexes | `[]` | Patterns that are never masked, for example a public test host. |

## `branching`

Branch tracking: strategies, per-repo base names, the guard and fetching. Full description in [branch-tracking.md](branch-tracking.md).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `branching.strategies` | object | `release-then-qa`, `qa-only`, `qa-cherry-pick` | `{name: {"targets": [roles], "steps": [{"from", "pr_into", "brings"?}]}}`, merged over the built-in three. |
| `branching.repos.<repo>.strategy` | string | none | The repo's default strategy. Repos without an entry are tracked but get no delivery check. |
| `branching.repos.<repo>.bases` | object role → name / pattern / list | `release/*`, `qa`, `uat`, `staging`, `main`/`master` | This repo's branch names for each role, e.g. `{"qa": "develop-qa", "release": ["release/*", "*-hotfix"]}`. |
| `branching.repos.<repo>.rules` | list | `[]` | `[{"jira_parent": "OPS-*", "strategy": "qa-only"}]`: a ticket whose Jira parent matches uses that strategy. |
| `branching.repos.<repo>.suffixes` | object | `-qa`, `-uat`, `-on-uat`, `-staging`, `-stg`, `-on-staging`, `-release` | Branch-name endings that name the target (the guard checks them). |
| `branching.repos.<repo>.remote` | string | `"origin"` | The remote to read and fetch. |
| `branching.repos.<repo>.relations` | list | `[]` | How the bases relate, each `{"from", "rel": "cut_from" \| "merges_into" \| "syncs_from", "to"}`; see [branch-tracking.md](branch-tracking.md#base-relations). |
| `branching.repos.<repo>.pr_source` | string | `"auto"` | `auto`: the host CLI if installed, else the host's MCP server (`mcp.git_host`), else merge commits. `"cli"` or `"mcp"`: only that source, else merge commits. `"git"`: merge commits only. |
| `branching.guard_mode` | `"warn"` \| `"ask"` \| `"off"` | `"warn"` | What the PreToolUse guard does when a branch or PR doesn't fit. |
| `branching.stale_pr_days` | int | `3` | An open PR older than this raises an alert. |
| `branching.ignore_tickets` | list | `[]` | Tickets with no branch tracking at all. |
| `branching.fetch` | object | `{"enabled": true, "every_minutes": 30, "active_days": 14, "before_send_minutes": 20}` | When the tick fetches. |
| `branching.pr_refresh_minutes` | int | `60` | How often open PRs are re-read. |

## `models`

Model aliases passed to `claude -p --model`.

| Key | Default | Used for |
|---|---|---|
| `models.extract` | `"haiku"` | Session extraction (also the follow-up "delta" extraction) |
| `models.consolidate` | `"sonnet"` | Day summary, standup and per-ticket TL;DR when a day closes |
| `models.curate` | `"sonnet"` | Picking the minimal replayable request set for a ticket |
| `models.dbflow` | `"sonnet"` | The DB-flow view (scenarios, conflicts, unknowns) |
| `models.worklog_condense` | not set, so `"sonnet"` | Condensing the evening-worklog descriptions in one batched call |

Slack sends and MCP jobs always use `haiku`.

## `paths`

Absolute paths to executables. Setup fills in any that are empty and replaces a recorded `claude` path that no longer exists.

| Key | Default | Meaning |
|---|---|---|
| `paths.python` | `""`, which means the Python running setup | Python used by the task, git hook and MCP guard |
| `paths.pythonw` | `""`, which means `pythonw.exe` next to `python` | Windowless Python for the scheduled task |
| `paths.claude` | `""`, which means looked up (see [install.md](install.md#the-claude-path-the-scheduled-task-needs)) | A real `claude.exe` (never a `.cmd` shim) |
| `paths.git` | `""`, which means `git` on `PATH` | Git |
| `paths.gh`, `paths.glab`, `paths.az` | found on `PATH` by setup | The Git host CLIs that branch tracking reads PRs with |

## `render`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `render.example_ticket` | string | `"ABC-1234"` | Ticket key used in examples inside generated text, such as the "unfiled artifacts" hint and the data home's `.worklog\README.md`. |

## `artifacts`

Files you produce (Write/Edit output, untracked repo files, published links, plans, `_drop\`) are filed under `tickets\<KEY>\artifacts\`.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `artifacts.enabled` | bool | `true` | `false` stops copying files into tickets; files already queued wait until it's `true` again. |
| `requests.enabled` | bool | `true` | `false` stops curating API calls into Bruno requests (no model call, nothing written to the Bruno folder). The DB flow map is not affected. |
| `artifacts.watch_dirs` | list of paths | `[]` | Extra folders whose new files are copied in. Files dropped into `<home>\_drop\` are moved, not copied. |
| `artifacts.push_binary_types` | list of extensions | `[]` | Binary types, for example `["pdf"]`, that are also copied next to the ticket's artifacts. Other binaries stay in `artifacts\local\` with 3 versions. |
| `artifacts.text_max_bytes` | int | `2000000` | Text files above this size are recorded as metadata only. |
| `artifacts.binary_max_bytes` | int | `25000000` | Binary files above this size are recorded as metadata only. |

## `jira_worklog`

These settings control the 20:00 message: your day laid out as Jira worklog entries that add up to your target hours.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `jira_worklog.enabled` | bool | `true` | Compose the evening worklog at all. |
| `jira_worklog.send_at` | `"HH:MM"` | `"20:00"` | First tick at or after this time composes (and, with Slack, sends) the day. |
| `jira_worklog.day_start` | `"HH:MM"` | `"10:30"` | Time of the first entry. |
| `jira_worklog.hours_per_day` | number | `8` | Target hours for a full day (half days are half this). |
| `jira_worklog.lunch.minutes` | int | `60` | Length of the lunch gap. |
| `jira_worklog.lunch.preferred` | `"HH:MM"` | `"14:00"` | Preferred lunch start. |
| `jira_worklog.lunch.window` | `["HH:MM","HH:MM"]` | `["12:30","15:00"]` | Lunch must start inside this window. |
| `jira_worklog.step_minutes` | int | `30` | Entries are rounded to this unit. |
| `jira_worklog.max_entries` | int | `6` | Small entries are merged until no more than this many remain. |
| `jira_worklog.stretch_note_below_minutes` | int | `360` | Below this much real tracked time, the message adds a note that the entries were stretched to the target. |
| `jira_worklog.workdays` | list | `["Mon","Tue","Wed","Thu","Fri"]` | Normal working days. |
| `jira_worklog.unassigned_time` | string | `"distribute"` | Time with no ticket: `"distribute"` spreads it across the day's tickets; a ticket key such as `"ABC-1000"` books it there. |
| `jira_worklog.leave_ticket` | string or null | `null` | On a leave day, book the full day here (`null` means nothing is sent for leave). |
| `jira_worklog.block_gap_minutes` | int | `20` | Activity less than this far apart counts as one block. |
| `jira_worklog.point_minutes` | int | `15` | Time credited to a single point event, such as one of your Jira comments. |
| `jira_worklog.late_days` | int | `3` | Unsent days up to this many days back are still sent late, with an age header. |
| `jira_worklog.correction_debounce_minutes` | int | `15` | Wait this long after a change to an already-sent day before sending one "Corrected update". |
| `jira_worklog.example_ticket` | string | `"ABC-1234"` | Ticket key in the "nothing tracked today" nudge. |

### The message and its preview

One builder makes the 20:00 message: blocks of activity (plus your Jira comments of the day), the allocation to your target, time already logged in Jira subtracted, condensed descriptions, the closing note, the delivery check and, for a changed day, the "Corrected update" header. The sender and every preview (`/cg:today`, `agent worklog-show`) call it, so a preview made from the same inputs is the message, character for character.

- **Jira data.** At 20:00 the sender reads Jira once (one read-only job). When the Slack send fails, the next tries (up to 4 attempts, `why: retry` in the day's history) reuse that Jira data if it was fetched within the last 30 minutes, instead of reading Jira again. A preview uses the latest copy of that data (`data\jira\_evening\<day>.json`) and says "Jira data as of HH:MM". It never calls Jira by itself; *Refresh from Jira* (`agent worklog-show --jira`) runs the one read job within `mcp.daily_call_budget`, keeping the 20:00 calls in reserve.
- **Condensed descriptions.** Lines that need condensing go to one batched model call, and each result is cached (`.worklog\cache\condense\<day>.json`, kept 14 days, keyed by a hash of that entry's notes and Jira facts). A preview uses the cached lines and marks the others "condensed at 20:00". *Preview exactly* (`agent worklog-show --exact`) condenses them now, within the same budget; the sender then reuses them.

### Inbox lines for the worklog

`time:` lines and notes are data: each distinct line is read once, however often it appears in `inbox.md`. `resend:` and the calendar marks (`leave:`, `half-day:`, `holiday:`, `workday:`, `clear:`) are actions: every new copy of the same line counts, in file order, so you can mark a day, clear it and mark it again, and `/cg:log` always adds the line. A day has one mark: the newest line wins. `clear: <day>[..<day>]` removes the leave, half-day, holiday or extra-workday mark of those days; hours you logged with `time:` stay. `resend:` sends the day again. Requests that arrive before the next tick go out as one send. Each send, resends included, is kept in the day's history in `data\timesheets\<day>.json` (`versions`: time, why (`first`, `retry` after a failed send, `resend`, `correction`), Slack's answer).

### `jira_worklog.slack`

Setup and `setup slack-user` fill in most of these.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `jira_worklog.slack.enabled` | bool | not set: on when Slack is configured | `false` turns sending off. The message is still saved locally. |
| `jira_worklog.slack.self_user_id` | string | `""` | Your Slack user ID (looked up by `setup slack-user`). |
| `jira_worklog.slack.send_tool` | string | `""` | Full name of the Slack MCP send tool (discovered). |
| `jira_worklog.slack.tools` | list | `[]` | All Slack tools seen at discovery. Every tool except the send tool is disallowed on a send. |
| `jira_worklog.slack.mode` | `"strict"` \| `"local"` | `"strict"` | `strict` passes only `.worklog\mcp\slack.json` to the send call. `local` runs in the sender folder's local scope. Discovery picks the one that works. |
| `jira_worklog.slack.email` | string | `""` | Email to look up in Slack; defaults to the last `git_author_emails` entry. |
| `jira_worklog.slack.server.url` | string | `"https://mcp.slack.com/mcp"` | Slack MCP endpoint. |
| `jira_worklog.slack.server.client_id` | string | Slack's published client id for Claude | OAuth client id. |
| `jira_worklog.slack.server.callback_port` | int | `3118` | OAuth callback port. |

## `mcp`

MCP jobs stay off until you configure them. Each job is a read-only `claude -p` call that runs in the background tick. Hooks never call an MCP.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `mcp.daily_call_budget` | int | `10` | Maximum background MCP jobs per day. Only a job that made at least one MCP tool call counts; jobs that made none (a server down) are counted apart under a cap of the same size. Runs you ask for are counted separately. |
| `mcp.jira.enabled` | bool | `false` | Run the Jira jobs: ticket cards, and the evening facts used by the worklog. |
| `mcp.jira.cards` | bool | `true` | With Jira on, read the day's tickets (title, status, summary) for the ticket pages. |
| `mcp.jira.evening` | bool | `true` | With Jira on, read what is already logged in Jira before the evening message, so it isn't logged twice. |
| `mcp.jira.server` | string | `"mcp-atlassian"` | Name of the Jira MCP server in your Claude Code user scope. The read allowlist entry and the server's own read-only mode (`READ_ONLY_MODE`, `ENABLED_TOOLS`) follow this name. |
| `mcp.jira.account_id` | string | `""` | Your Atlassian account id. It identifies your comments and worklogs, in addition to `git_author_emails`. |
| `mcp.git_host.<provider>` | object | — | `{"server", "enabled"}` for `azure-devops` or `github`: the user-scope MCP server PRs are read through when the host CLI isn't installed. Set by `setup use-git-host <server>`, which allows only that provider's classified PR and branch read tools. |
| `mcp.database.enabled` | bool | `false` | Run the database jobs: schema cards, QA trace and link map. Set by `setup use-database`. |
| `mcp.database.trace` | bool | `true` | With the database MCP on, run the QA trace of a ticket's records. |
| `mcp.database.linkmap` | bool | `true` | With the database MCP on, rebuild the QA link map once a week. |
| `mcp.database.server` | string | `""` | The database MCP server's name in your user scope. Nothing runs while this is empty. |
| `mcp.database.label` | string | `"database MCP"` | How rendered headings name it, for example "Schema facts (database MCP, …)". |
| `mcp.database.connections` | object env → connection | `{}` | Default connection per environment, for example `{"qa": "qa-main"}`. |
| `mcp.database.allowed` | object env → list | `{}` | More connections the guard allows per environment, for example `{"qa": ["qa-main", "qa-audit"]}`. Connections whose name contains `prod` are always dropped. |
| `mcp.database.read_only_login` | bool | `false` | Set to `true` once the database MCP connects with a read-only login. Until then Health and `/cg:doctor` show a standing warning. It changes nothing else: every statement still goes through the query guard. |
| `mcp.database.schema_map` | object schema → connection | `{}` | Which connection holds a schema, for example `{"shop": "qa-main"}`. When this is set, tables in unmapped schemas are skipped instead of guessed. |
| `mcp.database.types` | object connection → dialect | `{}` | `postgres`, `mssql`, `mongodb` or `elasticsearch`. If a connection isn't listed, the dialect is guessed from its name, falling back to `postgres`. |
| `mcp.database.entity_prefixes` | list | `[]` | Prefixes stripped when an id column names its table, so that with `["shop"]` the column `shopOrderId` points to `orders`. |
| `mcp.database.tools` | object role → tool | see below | Your server's tool names, if they differ from the defaults. |

`mcp.database.tools` defaults:

```json
{"query": "run_query", "describe_table": "describe_table", "list_tables": "list_tables",
 "list_schemas": "list_schemas", "list_connections": "list_connections"}
```

The tool mapped to `query` is SQL-checked by the guard. The guard reads the statement from an argument named `query`, `sql`, `statement`, `text`, `command` or `q`, and the connection from any argument whose name contains `connection` (or is `conn`, `datasource` or `database_id`). A query tool whose statement it can't find is denied: the guard fails closed. A database example:

```json
"mcp": {
  "database": {
    "enabled": true, "server": "shopdb", "label": "database MCP",
    "connections": {"qa": "qa-main"}, "schema_map": {"shop": "qa-main"},
    "types": {"qa-main": "postgres"}, "tools": {"query": "execute_sql"}
  }
}
```

## `qa_data`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `qa_data.allowed_envs` | list | `["qa"]` | Environments whose data may be kept literally, and whose connections MCP jobs may read. Anything unknown is treated as production. |
| `qa_data.hosts` | object host → env | `{}` | Maps a host to an environment, for example `{"api.qa.acme.test": "qa"}`. Bruno environment files are read too; `localhost` counts as `local`. |
| `qa_data.max_rows_per_query` | int | `20` | Row limit the guard enforces on database reads. |

## `storage`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `storage.push_paths` | list | `["daily/","README.md",".gitignore",".gitattributes"]` | What may be committed and pushed. The pre-commit hook refuses everything else. Widening this pushes more of your data, so think twice. |
| `storage.local_backup_dir` | path | `""` | If set, a zip of the data home is written there each night after 21:00 (`worklog-kb-<day>.zip`, last 7 kept). Only files with that name may be written there. |

## `git`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `git.allow_fetch` | bool | `true` | Branch tracking may `git fetch` remote-tracking refs (`refs/remotes/<remote>/*` only, `--no-tags`, never while a lock exists) in watched repos, for precise merge status. `false` = no writes inside your repos' `.git` folders at all. See [safety.md](safety.md#fetch). |

## `dbflow`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dbflow.sources_label` | string | `"entities, repositories, SQL, procs, integration channels"` | What a ticket's `db-flow.md` header says the map was built from. |

## `reminders`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `reminders.mcp` | bool | `true` | The weekly SessionStart line when Slack or Jira isn't connected. `setup reminders off` sets this to `false`. |

## `share`

Team sharing: targets, level, author and deny-list. See [sharing.md](sharing.md) for every key.

## `dashboard`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dashboard.port` | int | `7419` | Port on `127.0.0.1`. `CHITRAGUPTA_DASHBOARD_PORT` overrides it. |
| `dashboard.autostart` | bool | `true` | Start the dashboard from SessionStart maintenance. `false` (or `CHITRAGUPTA_DASHBOARD=0`) means it starts only through `/cg:dashboard` or `dashboard ensure`. |

## Reserved keys

These keys are accepted but nothing reads them in v0.1.0:

- `jira_worklog.model`: the condense step uses `models.worklog_condense` instead.
- `jira_worklog.slack.mcp_config`
- `qa_data.max_response_kb`: QA response bodies are capped at a fixed 50 KB.

## Environment variables

Each `CHITRAGUPTA_<NAME>` variable falls back to the legacy `WORKLOG_<NAME>` if it isn't set.

| Variable | Meaning |
|---|---|
| `CHITRAGUPTA_HOME` | Data home; overrides the pointer file. |
| `CHITRAGUPTA_INTERNAL` | Set to `1` on every child process the engine starts (`claude -p`, git). Hooks ignore sessions that carry it, so the engine never records itself. Don't set it yourself. |
| `CHITRAGUPTA_USER_DIR` | Runtime and settings folder (default `%USERPROFILE%\.chitragupta`). |
| `CHITRAGUPTA_CLAUDE_DIR` | Where setup puts the recall skill and the CLAUDE.md block. Default: `CLAUDE_CONFIG_DIR`, else `~/.claude`. |
| `CLAUDE_CONFIG_DIR` | Claude Code's own config folder. It's respected for `.claude.json` (MCP servers), transcripts and the skill/CLAUDE.md location. |
| `CHITRAGUPTA_DASHBOARD_PORT` | Dashboard port. |
| `CHITRAGUPTA_DASHBOARD` | `0` / `off` / `false` / `no` turns dashboard autostart off. |
| `CHITRAGUPTA_NO_MAINTENANCE` | `1` stops SessionStart from spawning maintenance (runtime refresh, dashboard, MCP status). |
| `CHITRAGUPTA_NO_TASK` | `1` makes setup and doctor skip the real Task Scheduler. |
| `CHITRAGUPTA_NO_MCP_JOBS` | `1` skips all MCP jobs, including in forced ticks. The nightly backup still runs. |
| `CHITRAGUPTA_BRUNO_DIR` | The data home's Bruno collection folder (default `<home>\bruno`). |

Development and test only:

| Variable | Meaning |
|---|---|
| `CHITRAGUPTA_DEV` | `1` lets setup accept a data home inside the plugin folder (the git-ignored `.dev-home`). |
| `CHITRAGUPTA_STARTUP_DIR` | Startup folder used for the task fallback. |
| `CHITRAGUPTA_TRANSCRIPTS_DIR` | Folder of Claude Code transcripts for the safety-net scan (default `<claude config>\projects`). |
| `CHITRAGUPTA_LLM_CACHE` | Folder that caches model answers by input, so the same input gets the same answer. |
| `CHITRAGUPTA_MCP_OVERRIDE` | JSON file of stand-in MCP servers for jobs (tests). Every call still goes through the full safety guard; nothing turns the guard off. |
| `CHITRAGUPTA_MCP_MANUAL` | `1` counts MCP jobs as manual runs, outside the daily budget. Set by the CLI. |
| `CHITRAGUPTA_PRIVACY_SIMULATE` | `public` / `private` / `unknown`: skips the GitHub visibility check. |
| `CHITRAGUPTA_SLACK_FAIL` | Forces Slack sends to fail. |
| `CHITRAGUPTA_HOOK_TIMING` | Logs each hook's duration to `logs\hook-timing.log`. |
| `CHITRAGUPTA_E2E`, `CHITRAGUPTA_E2E_CLAUDE` | Opt in to the end-to-end scripts, and the `claude` they use. |
| `CHITRAGUPTA_DENYLIST` | Deny-list file for `scripts/scan.py`. |

## Data home resolution

1. `CHITRAGUPTA_HOME`, if set.
2. Otherwise, the path in the pointer file `%USERPROFILE%\.chitragupta\home` (or `<CHITRAGUPTA_USER_DIR>\home`). Setup writes it.
3. Otherwise, `%USERPROFILE%\Chitragupta`.

Chitragupta counts as set up only when the resolved home has a `.worklog\config.json` and either `CHITRAGUPTA_HOME` is set or the pointer file exists.

## Prompt overrides

The prompts for model calls ship with the plugin (`chitragupta/prompts/*.md`, with JSON schemas in `prompts/schemas/`). To change one, put a file with the same name in `<home>\.worklog\prompts\`, for example `<home>\.worklog\prompts\extract.md` or `<home>\.worklog\prompts\schemas\extract.json`, and it wins over the packaged copy. The prompt files are:

- `extract`
- `extract_delta`
- `consolidate`
- `curate`
- `dbflow`
- `mcp_job`
- `slack_send`
- `worklog_condense`

If you change a schema, keep it compatible with the packaged one; the engine reads the same fields.
