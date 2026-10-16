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
9. **Dashboard is read-only and local**: 127.0.0.1 only, Host header checked, GET/HEAD only, no CORS, no external requests.
10. **Never write to a user's real data home in tests or dev.** Tests use temp dirs via `CHITRAGUPTA_HOME`; dev uses `.dev-home/`.

## Layout
- `.claude-plugin/` manifests · `hooks/` hooks.json + run.py launcher · `skills/` one folder per `/cg:` command (explicit-only) · `user-skill/chitragupta/` model-invoked recall skill (installed by setup)
- `chitragupta/` engine package (+ `prompts/`, `dashboard/`) · `tests/` unittest · `docs/` · `examples/` · `third_party/` · `scripts/`
- `notes/` is git-ignored working memory (inventory, deny-list, progress).

## Commands
- Tests: `python -m unittest discover -s tests -t .`
- Scan: `python scripts/scan.py --all` (local) · `--history` before any push to a new remote
- Validate plugin: `claude plugin validate .`
- Dev session: see `docs/development.md`
