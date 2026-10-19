# Privacy and data

Chitragupta records your work sessions, so it holds sensitive material by design. This page covers four things: what it keeps, what can leave the machine, how values are masked, and where the protection stops.

## What is captured

The hooks record five Claude Code events into `<home>\.worklog\raw\<work day>\<session>.jsonl`:

| Event | What is kept |
|---|---|
| SessionStart | The session's folder, repo, branch, ticket and model |
| UserPromptSubmit | Your prompt, up to 4,000 characters |
| PostToolUse | The tool's **input**, not its output. For a shell command: the command and its description. For HTTP commands such as curl or `Invoke-RestMethod`: the HTTP status and the *names* of the response's JSON keys, never the values. For files: the path. For MCP tools: the arguments. |
| Stop | Claude's last message, up to 12,000 characters, plus any `worklog-requests` / `worklog-dbflow` blocks |
| SessionEnd | The fact that the session ended |

All of it passes through redaction (see [Redaction](#redaction)) before it is written.

The hooks never call a model and never touch the network. Only two hooks are waited for: SessionStart (a 10-second budget) and, when you type a `/cg:` command, a UserPromptExpansion hook that stores only that prompt's ID and the session ID in `<home>\.worklog\cache\cg-active\<session>` for the safety guard ([safety.md](safety.md)); it records no prompt text.

### What is not captured

- **Sessions inside the data home**, such as asking Claude about your own worklog. Only a timestamp and the ticket you mention are recorded, so that the time counts, never the content.
- **A repo that contains a `.worklog-ignore` file.** Nothing is captured there, and the background transcript scan skips it too.
- **Anything while capture is paused**, from `/cg:pause` until `/cg:pause resume`.
- **Folders outside `capture_scope.include_paths`, or inside `capture_scope.exclude_paths`.**
- **The engine's own `claude -p` and git processes.** They carry `CHITRAGUPTA_INTERNAL=1`, and the hooks ignore them.

A **transcript safety net** reads Claude Code's own session transcripts from the last 48 hours. It catches sessions whose hooks didn't fire, and it obeys the same rules.

## What stays local

Everything in the data home stays on your machine except the push allowlist below:

- raw events and the engine state (`.worklog\`)
- ticket notes, `queries.md`, saved requests, `db-flow.md` and artifacts (`tickets\`)
- the knowledge base (`knowledge\`)
- structured data, including Jira and database caches (`data\`)
- the Bruno collection (`bruno\`)
- `inbox.md` and `_drop\`

The same goes for the settings folder `%USERPROFILE%\.chitragupta\`.

## What is pushed

Nothing is pushed unless `github_remote` is set. If it is set:

- **Only these paths** may be committed: `daily/**`, `README.md`, `.gitignore` and `.gitattributes` (`storage.push_paths`). The data home's `.gitignore` ignores everything else. A pre-commit hook in the data home refuses any other path. It also refuses a file that still needs masking under the `public` redaction pass; the engine then masks the file and tries again.
- **Only to a private repository.** Before pushing, and again every 24 hours, Chitragupta sends one unauthenticated request to the GitHub API (`api.github.com/repos/<owner>/<repo>`):
  - **404** (not visible to the public) counts as private, and the push goes ahead.
  - **200** (public) blocks every push.
  - Any other result is treated as unknown, which also blocks pushes.

  A local folder or `file://` remote counts as private.
- **Only to the configured remote.** If `origin` doesn't match `github_remote`, the push is refused. Pushes fetch and integrate the remote first, and never force.

## Redaction

Masks keep the shape of the value where shape is useful (digits become `X`, letters become `x`, punctuation stays). That way a finding can still say "phones arrive as `(XXX) XXX-XXXX`". Secrets become fixed tokens: `****`, `[JWT]`, `[PRIVATE KEY]`. Redaction runs at capture, before every model call, before rendering and before every commit.

| Mode | Masks | Used for |
|---|---|---|
| `local` (default) | Secrets (tokens, keys, passwords, auth headers, connection strings) and personal data in labelled fields (names, dates of birth, phone numbers, emails, record and member numbers, addresses) | Everything captured or written locally |
| `secrets` | Secrets only | Requests, payloads, responses and query samples from an allowed QA environment, where the values themselves are the point |
| `public` | Everything `local` masks, plus known QA values and ids, QA hosts, long digit runs and UUIDs. SQL text and JSON payload blocks are removed. | Daily notes, the Slack message: anything that leaves the machine |

These references are never masked: `{{var}}`, `${VAR}`, `$VAR`, `$(…)`. You can add your own patterns with `extra_sensitive_patterns`, and exempt known-safe ones with `allow_patterns`.

**Saved requests.** Credentials in saved requests become `{{process.env.NAME}}` references. The values go in `bruno\.env`, which is never read into the worklog and never committed. Personal values in request bodies become ticket-scoped variables with synthetic defaults.

## QA data rules

- An environment is QA only when it's listed in `qa_data.allowed_envs` (default `["qa"]`). A host's environment comes from your Bruno environment files and `qa_data.hosts`. **An unknown host counts as production.**
- Values from allowed environments are kept literally, in local files only. Credentials are still turned into references.
- Production values are masked at capture.
- A QA response body is kept only up to 50 KB, as the expected result for replay.
- Database samples are capped at `qa_data.max_rows_per_query` rows (default 20).
- Database MCP jobs read only connections listed for an allowed environment, and never a connection whose name contains `prod`.
- QA values never go into anything pushed or sent: the `public` pass masks known QA values and hosts.

## What goes to Slack

If Slack is connected, it receives only the evening Jira-worklog message, sent as a direct message to **you**:

- **Plain lines.** Each entry is a ticket key, a start time, a duration and a short description. The descriptions are checked first: a line containing a file path, an extension, SQL, an email or a long digit run is rejected. The whole message then goes through the `public` redaction pass.
- **One send call.** Each send is one `claude -p` call with only the send tool allowed and every other Slack tool disallowed, and with `--strict-mcp-config` plus a config holding only the Slack server, so none of your other MCP servers load. The OAuth sign-in stays in Claude Code's own store: the config names the same definition (URL and client id) and carries no token; a literal credential header would be passed as a `${CHITRAGUPTA_RT_…}` reference with its value only in the call's environment. The text the model actually sent is compared with the intended message.
- **Saved either way.** The message is also saved in the daily note and in `data\timesheets\<day>.json`, whether or not Slack is connected.

## Model calls

Every model call is a headless `claude -p`:

- **Isolated.** It runs in an empty sandbox folder (`.worklog\sandbox`) with no tools (`--tools ""`), nothing pre-approved (`--allowedTools ""`), `--permission-mode dontAsk`, no MCP servers (`--strict-mcp-config`), project settings only, no session persistence, slash commands off, and the fail-closed safety guard on every tool call. The internal-process marker is set. See [safety.md](safety.md).
- **Redacted input.** It sends a digest of the session that has already been redacted, up to about 120,000 characters, along with the packaged prompt.
- **Logged.** It records the cost in `health.json`.
- **Kept out of the hooks.** Hooks never call a model; only the background tick and the commands you run do.

## MCP jobs

MCP jobs run only in the background tick or when you ask for them, never from a hook. They are limited to `mcp.daily_call_budget` jobs a day (default 10); a job counts only when it made at least one MCP tool call, and jobs that made none (a server down) have their own cap of the same size. While a server is known to be unreachable or signed out (a job found it so, or the last `claude mcp list` check did after the last job that reached it), its background Jira and database jobs wait, apart from one probe job at most every 2 hours; one failed job also stops that server's other jobs in the same run. Each job is one `claude -p --model haiku` call that gets:

- **Only its own server.** `--strict-mcp-config --mcp-config <a file with only that server>`.
- **Only read tools.** `--allowedTools` lists the job's read tools, and `--disallowedTools` lists the server's write tools.
- **A guard hook.** The inline `--settings` install the safety guard as a PreToolUse hook with `"onFailure": "block"` (profile `mcp`; `.worklog\mcp\readonly-settings.json` keeps a copy for inspection). If the guard errors or times out, the call is denied. The guard denies:
  - any tool that isn't on the read allowlist (`.worklog\mcp\allow.json`), and any tool whose name says it writes, whatever the allowlist says
  - any statement that isn't a single read. `SELECT`, `WITH … SELECT`, `EXPLAIN`, `SHOW`, `DESCRIBE`, catalog reads and Mongo `find`/`count`/`aggregate` (without `$out`/`$merge`) are allowed. `SELECT INTO`, `EXPLAIN ANALYZE`, side-effect functions, locking hints, other procedure calls, multiple statements and look-alike Unicode tricks are denied.
  - row reads without a `TOP`/`LIMIT`/`FETCH FIRST` within the row limit
  - connections that aren't allowed

  Every block is logged in `%USERPROFILE%\.chitragupta\logs\security.log` and shown on Health.
- **No credentials in its files.** Literal header values from your Claude Code MCP config, such as an auth header, are written into the per-job file as `${CHITRAGUPTA_RT_…}` references. The values reach only that job's process environment. If a value is missing, for example because you're signed out, the job is skipped without a model call.

**Server state.** `health.json` keeps, per server, when a job last found it unreachable or signed out (`mcp.offline`, keyed by server) and why (`mcp.server_state`: `unreachable`, `signed-out` or `missing`, the short error text and the time); a job's own failure (missing env, a server down, the `claude` exit code) goes to `mcp.job_errors`. The next job that reaches the server clears its entries.

The guard files are written only when an MCP job can actually run: Jira is enabled, or a database server is chosen and enabled.
Once a day the Jira, database and Git-host entries of `allow.json` are refreshed from the code under the configured server names (`mcp.jira.server`, `mcp.database.server`, `mcp.git_host.*`): their read tools come only from the code (so new read tools reach an existing install and a renamed server isn't blocked), and write names you added to an entry stay on its deny list.

Jira results are cached locally in `data\jira\`. They can include colleagues' names from comments and worklogs. That cache is never pushed.

## The worklog MCP server

The plugin's own MCP server (`chitragupta`, see [mcp.md](mcp.md)) runs inside your Claude Code sessions. It reads the data home and returns what a tool asks for to that session, so that text reaches the model the same way it would if Claude opened the files itself. It writes nothing, opens no connection and is never started by Chitragupta's background calls.

## Network calls, all of them

| Call | When |
|---|---|
| `claude -p` (through Claude Code) | Extraction, consolidation, curation, DB-flow view, worklog condense, Slack send and MCP jobs, all in the background tick |
| `api.github.com/repos/<owner>/<repo>` (unauthenticated GET) | Privacy check before pushing, at most daily, only when a GitHub remote is set |
| `git fetch` / `git push` | Push: to `github_remote` only. Fetch: in watched repos with tickets active in the last 14 days, at most every 30 minutes and before 20:00, base and ticket branches only, into remote-tracking refs, never pruning (branch tracking; `branching.fetch.enabled: false` turns it off) |
| `git ls-remote --heads` | With each fetch, to notice branches deleted on the remote |
| `gh` / `glab` / `az repos` | PR data for active tickets' branches (hourly for open PRs, and before 20:00), with the CLI's own sign-in; never a token stored |
| Slack, Jira and database MCP servers | Only through the MCP jobs above, and only when you've connected them |

The dashboard makes no external requests. Mermaid is served from the plugin itself, not from a CDN.

## Known limits

- **Names in free text are not reliably masked.** Redaction masks values in labelled fields (`"firstName": …`, `name=…`, SQL columns and similar structured fields), but not a name written into a sentence. A finding such as "the order for Jane Doe failed" can reach a daily note. **Review the daily notes before you push them anywhere shared.** Better free-text name redaction is on the [roadmap](roadmap.md).
- **Local files hold real QA values and Jira people data by design.** Treat the data home like the rest of your work files: keep it out of shared or synced folders you don't control.

## Opting out

- **Pause capture:** `/cg:pause`.
- **Stop capture in one repo:** add a `.worklog-ignore` file to it.
- **Stop capture in a folder:** add it to `capture_scope.exclude_paths`.
- **Stop pushing:** clear `github_remote`.
- **Stop Slack:** set `jira_worklog.slack.enabled: false`.
- **Stop the MCP jobs:** set `mcp.jira.enabled` and `mcp.database.enabled` to `false`.
- **Remove everything outside the data home:** run `uninstall` (see the [README](../README.md#uninstall)).
