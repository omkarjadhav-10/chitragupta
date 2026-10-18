# Chitragupta — contributor contract

Chitragupta (`cg`) is a Claude Code plugin that keeps a worklog of your Claude Code sessions: hooks capture events, a background tick (Windows scheduled task) turns them into daily files, ticket notes, a knowledge base and an evening Jira-worklog summary, and a read-only local dashboard shows it all. Python, standard library only.

## Fixed names (never rename)
| What | Name |
|---|---|
| Tool / displayName | Chitragupta |
| Plugin `name` (command prefix) | `cg` → `/cg:today` |
| Repo, marketplace, Python package | `chitragupta` |
| Runtime + settings folder | `%USERPROFILE%\.chitragupta\` (`runtime\<version>`, `current`, `home` pointer) |
| Env vars | `CHITRAGUPTA_HOME`, `CHITRAGUPTA_INTERNAL` (legacy `WORKLOG_*` read as fallback) |
| Scheduled task | `Chitragupta` |
| Recall skill | `~/.claude/skills/chitragupta/` (source: `user-skill/chitragupta/`) |
| CLAUDE.md block markers | `<!-- chitragupta:start -->` … `<!-- chitragupta:end -->` |
| Kept for compatibility | `Worklog note:` lines, `worklog-requests` / `worklog-dbflow` blocks, data-home file formats |

## Rules
1. **Hooks fail open.** `hooks/run.py` and every hook path catch everything, log to the data home, and exit 0. Hooks never call a model, never touch the network, never block (only SessionStart is synchronous, and it has a 10 s budget; aim < 500 ms).
2. **Standard library only** at runtime. Dev tools are fine, but nothing the plugin imports may come from pip.
3. **No data, no company specifics in the repo** — code, tests, docs, examples, commit messages. No company names or domains, real ticket prefixes, internal system/service/tool/host/DB-connection names, Slack/Jira IDs, people's names, or real examples. They belong in the user's local config (`<home>/.worklog/config.json`). The internal database tool is only ever "the database MCP" (`mcp.database` in config).
4. **Synthetic test data only** (`ABC-1001`, `acme.test`, `shop` schema…). Real examples live in git-ignored `notes/local-tests/`.
5. **The pre-commit scan must pass**: `python scripts/install_git_hooks.py` once; `scripts/scan.py --staged` runs on every commit against the local deny-list `notes/denylist.txt`. Never bypass it with `--no-verify`.
6. **Docs move with features.** Any user-visible change updates `docs/` and `CHANGELOG.md` in the same commit.
7. **Versions agree.** `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` and `chitragupta/__init__.py` carry the same version; change it only with `scripts/bump_version.py`.
8. **Data format is stable.** The data home layout and file formats are shared with existing installs. Any change needs a forward-only migration that backs up first.
9. **Dashboard is local and reads by default**: 127.0.0.1 only, Host header checked, no CORS, no external requests. Reads are GET/HEAD; the only writes are the allowlisted routes in `chitragupta/dashboard/writes.py` (Origin and per-start token checked, JSON only, size and rate limits, every write recorded). Settings change only through `configstore`; see "The dashboard's writes" in `docs/safety.md`.
10. **Never write to a user's real data home in tests or dev.** Tests use temp dirs via `CHITRAGUPTA_HOME`; dev uses `.dev-home/`.

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
   - PR data may be read through a Git host's MCP server only with the read tools `/cg:setup` classified as read, through the same read-only harness and budget as the other MCP jobs.
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

### Chitragupta's own writes — the complete list

- files in the data home and in `%USERPROFILE%\.chitragupta\`
- `~/.claude/skills/chitragupta/` and the `chitragupta` block in `~/.claude/CLAUDE.md` (plus its one backup, `CLAUDE.md.chitragupta.bak`)
- Setup, doctor and uninstall may create, repair or remove Chitragupta's own scheduled job only: on Windows the `Chitragupta` task (when Task Scheduler refuses it, the one hidden launcher `Chitragupta.vbs` in your Startup folder), on macOS the LaunchAgent `dev.chitragupta.tick` in `~/Library/LaunchAgents/`, on Linux the user units `chitragupta.service`/`chitragupta.timer` in `~/.config/systemd/user/`, or one crontab line marked `# chitragupta`. `launchctl`, `systemctl --user` and `crontab` may be run only for those. See `docs/scheduling.md`.
- the dashboard's `Chitragupta.url` shortcut in the Start menu (and on the Desktop when you ask for it)
- the nightly backup `worklog-kb-<day>.zip` in `storage.local_backup_dir`, when you set one (and pruning the older ones there)
- deleting the large tool-result copies Claude Code saves under `~/.claude/projects/<sandbox folder>/` for Chitragupta's own MCP jobs, so QA data lives only in the knowledge base
- commit and push of daily files in the data-home repo
- the Slack DM to my own ID
- Sharing writes redacted bundles to my configured share target, only when I ask. A bundle that fails the deny-list or secret scan is not written. If the target is a team Git repo, Chitragupta commits and pushes only files inside its share folder, never force-pushes and never rewrites history. Sharing never posts to Slack.
- `fetch` of remote-tracking refs in work repos
- the dashboard's allowlisted writes: settings through the `/cg:setup` write path, and the actions listed in `docs/safety.md` (none of them writes outside Chitragupta's own folders)

Chitragupta never edits `~/.claude/settings.json`, `~/.claude.json` or any other Claude Code settings file. Connecting Slack prints the one `claude mcp add --scope local …` command for you to run yourself.

Enforced by `chitragupta/safety/` (see `docs/safety.md`): it is the only code that starts a process, changes a file or opens a connection; `tests/test_safety_static.py` keeps it that way and `tests/test_safety_redteam.py` proves the blocks. New code goes through `safety.commands.run(argv, purpose)`, `safety.paths.*` and `safety.net.*`, never around them.

## Layout
- `.claude-plugin/` manifests · `hooks/` hooks.json + run.py launcher · `skills/` one folder per `/cg:` command (explicit-only) · `user-skill/chitragupta/` model-invoked recall skill (installed by setup)
- `chitragupta/` engine package (+ `safety/` the only side-effect code, `prompts/`, `dashboard/`) · `tests/` unittest · `docs/` · `examples/` · `third_party/` · `scripts/`
- `notes/` is git-ignored working memory (inventory, deny-list, progress).

## Commands
- Tests: `python -m unittest discover -s tests -t .`
- Scan: `python scripts/scan.py --all` (local) · `--history` before any push to a new remote
- Validate plugin: `claude plugin validate .`
- Dev session: see `docs/development.md`
