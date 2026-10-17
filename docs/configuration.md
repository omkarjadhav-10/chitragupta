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
| `env_branches` | list of strings | `["qa","uat","staging","master","main","develop"]` | Environment branches checked for "is my branch merged into qa/uat/…". |
| `branch_fetch` | bool | `false` | Run `git fetch` hourly in watched repos, so environment status stays current. |
| `day_starts_at` | `"HH:MM"` | `"04:00"` | Work-day boundary. Work before this time counts as the previous day. |
| `push_interval_minutes` | int | `30` | Minimum gap between pushes. A push also happens when a session ends or a day closes. |
| `raw_retention_days` | int | `30` | Raw event files older than this are pruned, but only after they have been processed. |
| `extract_triggers.min_new_chars` | int | `1500` | A session is extracted once it has at least this much new text, **and** one of the next two triggers fires or the session has ended. |
| `extract_triggers.idle_minutes` | int | `10` | Extract after this many idle minutes. |
| `extract_triggers.max_interval_minutes` | int | `20` | Extract at least this often while a session is active. |
| `extra_sensitive_patterns` | list of regexes | `[]` | Extra patterns to mask everywhere, for example `["\\bINTERNAL-[0-9]+\\b"]`. |
| `allow_patterns` | list of regexes | `[]` | Patterns that are never masked, for example a public test host. |

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

## `render`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `render.example_ticket` | string | `"ABC-1234"` | Ticket key used in examples inside generated text, such as the "unfiled artifacts" hint and the data home's `.worklog\README.md`. |

## `artifacts`

Files you produce (Write/Edit output, untracked repo files, published links, plans, `_drop\`) are filed under `tickets\<KEY>\artifacts\`.

| Key | Type | Default | Meaning |
|---|---|---|---|
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
| `mcp.daily_call_budget` | int | `10` | Maximum background MCP jobs per day. Runs you ask for are counted separately. |
| `mcp.jira.enabled` | bool | `false` | Run the Jira jobs: ticket cards, and the evening facts used by the worklog. |
| `mcp.jira.server` | string | `"mcp-atlassian"` | Name of the Jira MCP server in your Claude Code user scope. |
| `mcp.jira.account_id` | string | `""` | Your Atlassian account id. It identifies your comments and worklogs, in addition to `git_author_emails`. |
| `mcp.database.enabled` | bool | `false` | Run the database jobs: schema cards, QA trace and link map. Set by `setup use-database`. |
| `mcp.database.server` | string | `""` | The database MCP server's name in your user scope. Nothing runs while this is empty. |
| `mcp.database.label` | string | `"database MCP"` | How rendered headings name it, for example "Schema facts (database MCP, …)". |
| `mcp.database.connections` | object env → connection | `{}` | Default connection per environment, for example `{"qa": "qa-main"}`. |
| `mcp.database.allowed` | object env → list | `{}` | More connections the guard allows per environment, for example `{"qa": ["qa-main", "qa-audit"]}`. Connections whose name contains `prod` are always dropped. |
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
| `storage.local_backup_dir` | path | `""` | If set, a zip of the data home is written there each night after 21:00 (`worklog-kb-<day>.zip`, last 7 kept). |

## `dbflow`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dbflow.sources_label` | string | `"entities, repositories, SQL, procs, integration channels"` | What a ticket's `db-flow.md` header says the map was built from. |

## `reminders`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `reminders.mcp` | bool | `true` | The weekly SessionStart line when Slack or Jira isn't connected. `setup reminders off` sets this to `false`. |

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
| `CHITRAGUPTA_MCP_OVERRIDE` | JSON file of stand-in MCP servers for jobs (tests). |
| `CHITRAGUPTA_MCP_MANUAL` | `1` counts MCP jobs as manual runs, outside the daily budget. Set by the CLI. |
| `CHITRAGUPTA_GUARD_ALLOW` | Alternative `allow.json` for the MCP guard. |
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
