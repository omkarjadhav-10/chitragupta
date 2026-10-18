# Safety

Chitragupta only reads your work. It keeps a worklog *about* your repos, Git hosts, Jira and databases, and it never changes any of them. That holds for its own background processes, for its `/cg:` commands and recall skill inside your sessions, and for every model it calls. It never touches your own work: outside a `/cg:` command your sessions keep full control of your repos and databases.

Two layers make this hold:

- **Rules**, written below and in `CLAUDE.md`, the README and every skill, so people and Claude sessions know them.
- **Enforcement**: one package, `chitragupta/safety/`, is the only code that starts a process, changes a file or opens a network connection. It checks every action against an allowlist and blocks the rest, whoever asks for it. No setting, flag or environment variable turns it off.

## Safety rules — Chitragupta only reads

1. **No codebase changes.** Chitragupta never creates, edits, moves or deletes a file outside its own folders: the data home, `%USERPROFILE%\.chitragupta\`, its plugin folder and its temp/sandbox folder. Work repos and every other folder are read-only.
2. **Git is read-only in work repos.**
   - Allowed: `rev-parse`, `for-each-ref`, `log`, `rev-list`, `merge-base`, `cherry`, `patch-id`, `show` (metadata), `reflog`, `branch --list` / `--contains`, `ls-remote`, `remote get-url`, `config --get`, and `fetch` of remote-tracking refs only.
   - Never: `commit`, `merge`, `rebase`, `cherry-pick`, `revert`, `reset`, `checkout`, `switch`, `restore`, `stash`, `pull`, `push`, `tag`, `branch -d/-D/-m`, `clean`, `gc`, `prune`, `am`, `apply`, `worktree`, `submodule update`, or any config write.
   - The one exception: committing and pushing daily files in Chitragupta's own data-home repo.
3. **Git hosts are read-only.**
   - `gh`, `glab`, `az` and any Git-host MCP may list and view PRs, branches and work items.
   - Never create, edit, approve, comment on, merge, close or complete a PR.
   - Never create, delete or lock a branch or ref, run a pipeline, or call a raw API with anything but GET.
4. **Azure CLI: an allowlist, nothing else.** Only `az repos pr list|show`, `az repos ref list`, `az repos list|show`, `az boards work-item show` and `az account show`. Every other `az` command group is blocked, because it can change cloud resources.
5. **Databases are read-only.**
   - Allowed: one `SELECT` or `WITH … SELECT`, catalog reads, `EXPLAIN` without `ANALYZE`, and read-only metadata procedures.
   - Never: `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `UPSERT`, `TRUNCATE`, `DROP`, `ALTER`, `CREATE`, `GRANT`, `REVOKE`, `EXEC` of any other procedure, `SELECT … INTO`, locking hints, sequence changes, functions with side effects, transactions, or more than one statement.
   - MongoDB: only `find`, `aggregate` without `$out`/`$merge`, `count`, `distinct` and list operations.
6. **Jira is read-only. Slack has exactly one action:** a direct message to my own Slack user ID.
7. **No other tools.** No database CLIs (`psql`, `sqlcmd`, `mysql`, `mongosh`), no `kubectl`, `docker`, `helm`, `terraform`, no build or publish tools, no `ssh`/`scp`, no PowerShell commands that change anything.
8. **Internal Claude calls never get write tools,** never use a bypass or skip-permissions mode, and always run in Chitragupta's sandbox folder.
9. **When unsure, refuse.** Unknown commands, tools and statements count as writes. They're blocked, logged and shown on the Health page.
10. **No off switch.** No setting, flag or environment variable turns these rules off.

## Chitragupta's own writes — the complete list

- files in the data home and in `%USERPROFILE%\.chitragupta\`
- `~/.claude/skills/chitragupta/` and the `chitragupta` block in `~/.claude/CLAUDE.md` (plus its one backup, `CLAUDE.md.chitragupta.bak`)
- the `Chitragupta` scheduled task (setup and doctor repair only); when Task Scheduler refuses it, the one hidden launcher `Chitragupta.vbs` in your Startup folder
- the dashboard's `Chitragupta.url` shortcut in the Start menu (and on the Desktop when you ask for it)
- the nightly backup `worklog-kb-<day>.zip` in `storage.local_backup_dir`, when you set one (and pruning the older ones there)
- deleting the large tool-result copies Claude Code saves under `~/.claude/projects/<sandbox folder>/` for Chitragupta's own MCP jobs, so QA data lives only in the knowledge base
- commit and push of daily files in the data-home repo
- the Slack DM to my own ID
- `fetch` of remote-tracking refs in work repos

Chitragupta never edits `~/.claude/settings.json`, `~/.claude.json` or any other Claude Code settings file. Connecting Slack prints the one `claude mcp add --scope local …` command for you to run yourself.

## How it is enforced

| Layer | Module | What it checks |
|---|---|---|
| Command guard | `safety/commands.py` | `run(argv, purpose)` is the only way to start a process. Each purpose has its allowlist: `git-work` (rule 2, only in a watched or captured repo), `git-home` (the data-home sync), `host` (`gh` / `glab` / `az`), `claude`, `self` (Chitragupta's own launcher), `task` (schtasks for `Chitragupta` only), `probe` (version checks). Executable, subcommand, every flag and the working folder are checked before anything starts. |
| Path guard | `safety/paths.py` | `write_text`, `append_text`, `replace`, `copy`, `delete`, `rmtree` … are the only file-changing functions. The real path (junctions and symlinks followed, case-insensitive) must be under a write root above. `..`, UNC and device paths (`\\server`, `\\?\`, `\\.\`) and alternate data streams are refused outright. Sources of copies are only read. A SQLite file (the search index) is opened only through `sqlite_connect`: the same root check, opened read-only where nothing is written, and `ATTACH` / `VACUUM INTO` refused so a statement can't write a database file elsewhere. |
| Query guard | `safety/query.py` | One classifier for every database statement: Unicode NFKC, look-alike letters mapped, zero-width characters dropped, comments and literals stripped, then exactly one statement of an allowed form, with a row limit. Side-effect functions (`nextval`, `pg_advisory_lock`, `dblink_exec`, `xp_cmdshell`, `sp_executesql`, `OPENROWSET` …), locking hints and `EXPLAIN ANALYZE` are refused. MongoDB: read operations only, no write operator, stage or option. |
| MCP tool guard | `safety/mcp.py` | Per server, only the read tools in `.worklog/mcp/allow.json`. A floor the allowlist can't lift: a tool whose name says it writes (create, update, delete, send, comment, transition …) is never a read. Database tools: every statement through the query guard, QA/dev connections only. Slack: only the send tool, only to your own user ID, no threads, drafts, files or scheduling, at most 8,000 characters. |
| Internal calls | `safety/claude.py` | One builder for every `claude -p`: no built-in tools (`--tools ""`), an explicit `--allowedTools` (empty for extraction), every write tool on `--disallowedTools`, `--permission-mode dontAsk --permission-prompts none`, `--strict-mcp-config` with only the job's server, this plugin switched off (`enabledPlugins: {"cg@chitragupta": false}`, so its own MCP server never starts inside a call), the sandbox folder, and a PreToolUse hook that runs the guard with `"onFailure": "block"`: if the guard errors or times out, the call is denied. The command guard re-checks every call. |
| `/cg:` commands | `safety/hook.py` + each skill's `hooks:` | Every `/cg:` skill registers the session guard (fail-closed). It acts only while a `/cg:` command is running (your last prompt was one, or the UserPromptSubmit hook marked it; Stop clears the mark): then only read tools and Chitragupta's own helper (`run.py`) pass. Outside a `/cg:` command it returns at once, so your own work is never blocked; a session that never ran a `/cg:` command doesn't start it at all. |
| Network | `safety/net.py` | Direct HTTP is GET only: `api.github.com/repos/<owner>/<repo>` (the data-home privacy check) and the local dashboard. Everything else happens inside git / `gh` / `glab` / `az` / `claude`, under the command guard. |

Skills' `allowed-tools` only pre-approve their helper; they don't restrict other tools (that's what the session guard is for). The recall skill (`chitragupta`) states the rules and pre-approves only reads and `run.py cli`; it never runs `replay.sh` — that sends real requests, so you run it yourself.

Every git read in a work repo starts with `--no-pager -c core.pager=cat -c core.fsmonitor=false -c diff.external= -c protocol.ext.allow=never -c core.hooksPath=NUL …`, and `log` / `show` add `--no-ext-diff --no-textconv`, with `GIT_OPTIONAL_LOCKS=0` and `GIT_TERMINAL_PROMPT=0`, so a repo's own config can't make a read run a program.

### `fetch`

Branch tracking fetches remote-tracking refs only (`+refs/heads/<b>:refs/remotes/<remote>/<b>`, `--no-tags --no-write-fetch-head`), never local branches, never the working tree, never while any `.git/*.lock` exists, and only in repos you listed. `git.allow_fetch` (default `true`) set to `false` means zero writes inside `.git`, at the cost of less precise merge status.

## Violations

Each block is written to `%USERPROFILE%\.chitragupta\logs\security.log` (one JSON line: time, layer, caller, purpose, the blocked command, path, statement or tool, and why), counted on the dashboard's **Health** page and reported by `/cg:doctor`, which also shows **Safety: enforced** when the canned guard checks hold, every `/cg:` skill registers the guard and Claude Code is new enough for fail-closed hooks (2.1.295). A blocked action is never retried. After a normal day the log is empty.

## What you can do yourself

- **Give the database MCP a read-only login**, then set `mcp.database.read_only_login` to `true`. Until then Health and doctor show a standing warning: the query guard is the only thing between a model and a write.
- Keep `git.allow_fetch` on unless you want no writes at all inside your repos' `.git` folders.

## For contributors

`tests/test_safety_static.py` fails the build if any module outside `chitragupta/safety/` calls `subprocess`, `os.system`, `os.popen`, `open()` in a write mode, `shutil` move/copy/delete, `os.remove` / `rename` / `replace`, pathlib writes, `sqlite3.connect` of anything but `:memory:` or `urllib` / `http.client` / `socket` (the dashboard server's own 127.0.0.1 listener is the one exception), and if the builder can ever emit a bypass. `tests/test_safety_redteam.py` holds the red-team table: every attempt must be blocked by its layer and logged, every allowed action must still work.
