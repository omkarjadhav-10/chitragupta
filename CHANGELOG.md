# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Settings page with a single config write path.** Every setting the dashboard may change, with validation shared by page and server, a review (now → after saving), history in `settings-history.jsonl` and undo; locked items are shown only. `configstore.py` is now the one way config changes (setup, the engine and the dashboard), with a lock, a backup and history; the engine saves only the keys it changed.
- **Live MCP status** on Health and Settings: Check now, an automatic check when the page opens (at most every 5 minutes), the same code as `/cg:doctor`, shown next to the last background job's view.
- **Past days:** `#/day/<date>` shows the message as sent with its send history, the day's branch and PR events and its tickets' alerts, with a date picker; `/cg:dashboard day today|yesterday|-N|YYYY-MM-DD`.
- **Dashboard write protection:** an allowlist of write routes, each checked for Host, Origin, a per-start page token, JSON, size and rate. First actions: pause/resume capture and the live MCP check.
- **More settings:** on/off switches for Jira ticket cards and evening facts, the QA trace, the weekly link map, the daily push, request curation and artifacts (all on unless switched off, so existing setups don't change); per repo, the base branch names, the base relations (with *Detect from the repo*) and the PR source.
- **DB map page** (`#/db`): databases → schemas → tables in the side list, with search; per table its columns (type, keys, personal-data flag), its links with how well each was verified in QA (verified, likely or not verified, with the sampled-row counts), who reads and writes it, its tickets and saved queries, and a direct or two-hop neighbourhood diagram with a table version. The whole map is drawn only on request.

## [0.3.0] - 2026-10-18

### Security
- **Chitragupta only reads, enforced in code** ([docs/safety.md](docs/safety.md)). A new package, `chitragupta/safety/`, is the only code that starts a process, changes a file or opens a network connection, and nothing turns it off:
  - **Command guard:** every process goes through `run(argv, purpose)` with an allowlist per purpose. Work-repo git is read-only (rule 2) and hardened so a repo's own config can't make a read run a program (pager, external diff, textconv, fsmonitor, `ext::`, hooks). `fetch` updates only `refs/remotes/<remote>/*`, writes no `FETCH_HEAD`, and is skipped while git holds a lock. `gh` / `glab` do list and view only, `api` with GET only. `az` gets a five-command allowlist with `--output json`. The data-home sync never forces a push and stages only the push paths.
  - **Path guard:** writes only under Chitragupta's own roots (the full list is in the docs). `..`, UNC and device paths, alternate data streams and junctions out of a root are refused.
  - **Query guard:** one classifier for every statement, with Unicode look-alikes and zero-width tricks normalised first. Newly refused: `EXPLAIN ANALYZE`, `pg_try_advisory_*`, `lo_*` and more side-effect functions.
  - **MCP tool guard:** read tools from `allow.json` only, plus a floor the allowlist can't lift (a tool named like a write is never a read). Slack can send only to your own user ID: no threads, drafts, files or scheduling, at most 8,000 characters.
  - **Internal calls:** one builder for every `claude -p`. No built-in tools, an explicit `--allowedTools`, `--permission-mode dontAsk --permission-prompts none`, only the job's MCP server, the sandbox folder, and a PreToolUse guard with `onFailure: block`, so a guard error denies the call. Needs Claude Code 2.1.295 or newer.
  - **`/cg:` commands:** each skill registers a fail-closed session guard. While a `/cg:` command runs, only read tools and Chitragupta's own helper pass. Outside one, your own work is never blocked.
  - **Violations:** every block goes to `%USERPROFILE%\.chitragupta\logs\security.log`. It is counted on Health and reported by `/cg:doctor`, which now shows **Safety: enforced**.
- The MCP guard used to fail open: a crash let the tool call through. It now denies.
- The Slack sender had no tool guard (hooks were off). It now has one, with the recipient checked in code.

### Added
- **Base relations** (`branching.repos.<repo>.relations`: `cut_from`, `merges_into`, `syncs_from`): the branch graph draws bases from their real fork points and the merges between them; missing delivery cells say how the change will arrive (verdicts unchanged). `cli relations detect` proposes them from local refs (read-only), `cli relations set` saves them.

### Added
- **Dashboard 2.0, read side** ([docs/dashboard.md](docs/dashboard.md)): a new look and layout (navigation, a side list per page, the page), live updates, and these pages:
  - **Today**: time against the day's target, the day lane by lane per ticket with commits, delivery alerts across all tickets, tonight's Jira entries with a copy button per field; **past days** at `#/day/<date>`
  - **Tickets**: filter by text, status, repo, alerts and last active; sort any column. **Ticket**: a header that stays in view and tabs (Overview, Branches & PRs, DB flow, Requests, Queries, Artifacts, Timeline, Notes)
  - **Timesheet**: a month calendar shaded against each day's target with leave, half days, holidays and extra workdays, weekly bars, a day panel, hours per ticket and CSV
  - **Knowledge**: the DB map (zoom, pan, links verified by the QA link map), the API catalog and integrations
  - **Search** over tickets, findings, queries, APIs, tables, artifacts and daily notes; **Setup** (the doctor checks as a checklist with the fix for each); **Settings** (theme, density, motion)
  - **Command palette** (Ctrl+K) and **keyboard shortcuts** (`?` lists them)
  - Health adds the scheduled task, MCP calls blocked by the read-only guard, each MCP server's state from the last `claude mcp list`, and *Copy diagnostics*
- **Open it like an app:** `/cg:dashboard [day|ticket|page]` now opens the page in your browser; setup adds a **Start menu** shortcut (`dashboard shortcut --desktop` adds one to the Desktop); Chrome and Edge can install it as an app with its own window (web app manifest; no offline copy of your data).
- **Accessibility:** WCAG 2.2 AA, checked with axe on every page in both themes; skip link, landmarks, visible focus, WAI-ARIA tabs, dialogs, combobox and live region; text or table versions of every chart and diagram; status never shown by colour alone.
- New read-only endpoints: `/api/day/<date>`, `/api/calendar`, `/api/knowledge`, `/api/search` (in-memory SQLite FTS5, or a plain scan), `/api/setup`, `/api/events` (Server-Sent Events). `/api/tickets` adds alerts and branches, `/api/tickets/<KEY>` the README, `/api/status` the cached MCP status and search stats.
- `scripts/ui/`: browser checks on a synthetic home (axe, keyboard, palette, layouts, no outgoing requests).

### Changed
- `setup add-slack` prints the `claude mcp add --scope local …` command for you to run. Chitragupta never writes Claude Code's settings (`~/.claude.json`).
- The recall skill no longer runs `replay.sh` itself (it sends real requests); it shows you the command.
- `replay.sh` and `bruenv` keep their temp files in `<home>/.worklog/tmp` (pruned after two days) instead of the system temp folder.
- New settings: `git.allow_fetch` (default `true`), `mcp.database.read_only_login` (Health warns until it's `true`).
- `mermaid_check` no longer runs `mmdc`.
- The page is now ES modules under `chitragupta/dashboard/app/` with a vendored Preact (MIT), and a stricter Content-Security-Policy: no inline script at all. `page.html` is gone.
- A ticket export is the new page with its scripts and styles inlined; it keeps the tabs, opens offline and carries its own CSP that blocks every request.
- The dashboard still writes nothing: settings and actions come in the next update.

### Removed
- `CHITRAGUPTA_GUARD_ALLOW` (it pointed the guard at another allowlist).

### Fixed
- Health read an MCP status field nothing wrote; it now reads the cached `claude mcp list` result.
- Ticket page: the DB-flow tables can be scrolled from the keyboard, and dark mode meets contrast everywhere.

## [0.2.1] - 2026-10-18

### Fixed
- The evening Jira worklog crashed while composing a weekend or holiday with only short stretches of ticket time: every block rounded below one 30-minute step, and the empty entry table failed. It now reports that there is nothing to log for that day.

## [0.2.0] - 2026-10-17

### Added
- **Branch tracking per ticket** ([docs/branch-tracking.md](docs/branch-tracking.md)). For every ticket: its branches and the parent each came from (captured command, reflog, or inferred and labelled), its commits, its PRs (source → target, state, review, merge), and a delivery check of every commit against every branch the repo's strategy requires (in target · same change · via squash PR · PR open · missing · reverted · check by hand). No model calls.
  - **Strategies are config:** `release-then-qa`, `qa-only` and `qa-cherry-pick` built in, per-repo base names, per-ticket rules (for example by Jira parent), and `ignore_tickets`.
  - **Alerts:**
    - commits pushed after their PR merged, or in one target but not another
    - commits only on an abandoned branch
    - a wrong parent, or a PR into an unexpected base (with the commits it would carry)
    - unpushed commits, changed-patch cherry-picks, stale or approved-but-unmerged PRs
  - **PR data:** from `gh`, `glab` or `az repos` with the CLI's own sign-in; without one, from the PR merge commits on the base branches.
  - **Work repos are read-only:** all git goes through one allowlisted wrapper (`gitro`), with a unit test that scans for mutating calls. `git fetch` only updates remote-tracking refs, never prunes, and waits out git lock files.
- **Where it shows:**
  - the dashboard ticket page (alert banner, graph, delivery matrix, branches and PRs tables) and the ticket README
  - one line at session start for a ticket with alerts
  - a `Delivery check` block after the closing note of the 20:00 Slack message (same send, Jira entries unchanged)
  - one summary line per ticket in the daily file
  - the recall skill
- **Guard:** a PreToolUse hook warns (or asks, with `branching.guard_mode: ask`) when Claude Code creates a branch from a parent, or a PR into a base, that doesn't fit the ticket's strategy. It fails open and finishes in well under 200 ms.
- **Commands:**
  - `/cg:branches <KEY>`
  - `/cg:check [KEY]` (fetch and refresh now)
  - `/cg:branch link|unlink|abandon|restore|strategy` (one `inbox.md` line each)
  - `/cg:ticket <KEY> --export [--mode shareable]`: the whole ticket page as one self-contained HTML file
- **Migration `branch-tracking`** (`schema_version` 4). It backs up first, seeds `data/git/<repo>/` from `data/branches.json` and the current reflogs, and adds `"branching"` to the config. The tick runs pending migrations itself.

### Changed
- `knowledge/branches.md` shows parent, unpushed commits and where each branch lives. Delivery moved to each ticket's page.
- Setup records the paths of `gh`, `glab` and `az` when found.

### Removed
- `branch_fetch` (it ran `git fetch --prune`). Fetching is now `branching.fetch`, and it never prunes.

## [0.1.0] - unreleased

The first release. It takes an existing standalone worklog engine and makes it the `cg` Claude Code plugin, with a read-only dashboard and a first run that works with or without Slack and Jira.

### Added
- **The `cg` plugin, Chitragupta.**
  - Manifests and a marketplace (`chitragupta`) for the one-command install: `claude plugin install cg --marketplace omkarjadhav-10/chitragupta`.
  - Hooks for SessionStart (sync), UserPromptSubmit, PostToolUse, Stop and SessionEnd (async). They run through `hooks/run.py`, and every hook fails open.
- **The engine, as the `chitragupta` package.** It is ported with parity: on a copy of a live data home, the old and new engines produced 397 of 404 files byte-identical, and the 7 differences were justified. The package covers:
  - capture
  - the background tick
  - extraction and consolidation
  - daily notes and ticket notes
  - the knowledge base
  - saved and replayable requests (Bruno, `requests.md`, `replay.sh`)
  - DB flows with Mermaid
  - artifacts
  - redaction (`local` / `secrets` / `public`)
  - allowlisted git push with a private-repo check
  - the 20:00 Jira worklog with Slack sending
  - read-only Jira and database-MCP jobs behind a guard
  - the nightly backup
- **Commands:** `/cg:today`, `/cg:ticket`, `/cg:timesheet`, `/cg:trace`, `/cg:log`, `/cg:pause`, `/cg:dashboard`, `/cg:setup` and `/cg:doctor`. They are explicit-only and backed by a read-only `cli` module; only `log` and `pause` write anything.
- **The `chitragupta` recall skill.** Claude invokes it on its own. Setup installs it to `~/.claude/skills/chitragupta/`, along with a CLAUDE.md block between `<!-- chitragupta:start -->` and `<!-- chitragupta:end -->`.
- **`/cg:setup`** (`setup check|apply|mcp-status|add-slack|slack-user|slack-test|use-database|reminders`).
  - It is idempotent and never changes recorded data.
  - It checks requirements and detects ticket prefixes, repos and Bruno collections from recent sessions.
  - It merges config without dropping keys.
  - It installs the runtime, the `Chitragupta` scheduled task (falling back to the Startup folder), the recall skill and the CLAUDE.md block.
  - It shows the Slack, Jira and database MCP status.
- **`/cg:doctor`.** It diagnoses only and never repairs. It checks Python, git, `claude`, the data home, config, hooks, the last tick, the last push, the runtime, the task, the recall skill, the CLAUDE.md block, the MCP status and recent errors.
- **A stable runtime** at `%USERPROFILE%\.chitragupta\runtime\<version>`, with `current` and `run.py`. It outlives plugin updates and is refreshed atomically. The newest two versions are kept, and it never downgrades.
- **SessionStart maintenance.** A detached, throttled process that refreshes the runtime, keeps the dashboard running and refreshes the MCP status cache.
- **First-run behaviour.** Until setup has run, SessionStart shows one line, *Chitragupta isn't set up yet — run /cg:setup to finish.*, and records nothing.
- **A read-only dashboard** on `127.0.0.1:7419` with Today, Tickets, Timesheet (with CSV) and Health pages.
  - Its security: Host and Origin checks, GET/HEAD only, no CORS, a strict CSP and a token-guarded shutdown.
  - Mermaid 11.17.2 is vendored. The dashboard autostarts and is replaced when the version changes.
- **Working without MCPs.**
  - **Without Slack**, the evening worklog is composed and saved in the daily note and `data/timesheets/` ("saved here, Slack isn't connected"), and Health reports Slack as not connected.
  - **Without Jira**, ticket titles come from branch names.
  - **Without a database MCP**, DB flows come only from sessions.
- **A weekly reminder** to connect Slack or Jira, with an off switch: `setup reminders off`, or `reminders.mcp: false`.
- **Database MCP wiring by config.** `setup use-database <server>` chooses the server, and `mcp.database.tools` maps its tool names. `mcp.database.label` and `dbflow.sources_label` set the label text in rendered notes.
- **Config keys:**
  - `jira_worklog.slack.enabled`
  - `jira_worklog.slack.server.{url,client_id,callback_port}`
  - `dashboard.{port,autostart}`
  - `render.example_ticket`
- **Environment variables:**
  - `CHITRAGUPTA_HOME`, `CHITRAGUPTA_INTERNAL` and `CHITRAGUPTA_USER_DIR`. The legacy `WORKLOG_*` names are read as a fallback.
  - `CHITRAGUPTA_CLAUDE_DIR`, `CHITRAGUPTA_DASHBOARD`, `CHITRAGUPTA_DASHBOARD_PORT`, `CHITRAGUPTA_NO_MAINTENANCE` and `CHITRAGUPTA_NO_TASK`.
- **Prompt overrides.** A file in `<home>/.worklog/prompts/` wins over the packaged prompt of the same name.
- **`uninstall`.** It removes the task, runtime, recall skill, CLAUDE.md block and home pointer, and keeps all data.
- **Developer tooling:**
  - `scripts/sync_dev_home.py` (dev data home)
  - `scripts/parity.py` (parity harness)
  - opt-in end-to-end scripts (`CHITRAGUPTA_E2E=1`)
  - a deny-list and secret scan, with a pre-commit hook
  - `scripts/bump_version.py`
  - `scripts/cutover.py`, which moves from a legacy worklog engine with a one-command rollback
  - CI on Windows and Ubuntu with Python 3.10–3.12
- **Docs:** install, configuration, privacy, dashboard, architecture, development and roadmap.

### Changed
- **The config key for the database MCP** is now `mcp.database`. A forward migration with a backup first moves the old key, the health keys and the job names.
- **MCP jobs are off until configured** (`mcp.jira.enabled`, `mcp.database.enabled` + `server`). Earlier they defaulted to on.
- **The guard files** (`.worklog/mcp/allow.json` and `readonly-settings.json`) are written only when an MCP job can run. A job never runs without them.
- **Persisted commands use the stable launcher.** The scheduled task, the data home's pre-commit hook, the MCP guard and `replay.sh` call `~/.chitragupta/runtime/run.py`, not a versioned plugin path.
- **The scheduled task** is now named `Chitragupta`. `agent loop` (the Startup-folder fallback) is single-instance.
- **`CHITRAGUPTA_NO_MCP_JOBS`** is now respected by forced ticks too. The nightly backup no longer depends on it.
- **`migrate` takes the agent lock.** It exits 3 ("busy, retry") instead of racing a running tick.
- **The data home's `.worklog/README.md`** now describes the plugin, its commands, and how it behaves without Slack or Jira.
- **No company-specific values in code.** Examples, labels, hosts and email filters now come from config.

### Fixed
- **Finding `claude` for the scheduled task.** The task now finds `claude` without a shell `PATH`. It checks the native install, the fnm default alias, nvm-windows and npm global. It resolves `fnm_multishells` paths, and setup records a stable `paths.claude`, replacing one that has vanished. Doctor flags a shell-only path.
- **Slack sign-in failing** with "does not support dynamic client registration". `setup add-slack` now always passes Slack's published client id for Claude, with callback port 3118.
- **Lock races** in `agent loop`, in `worklog-show --jira` (which now takes the agent lock) and in `migrate`.
- **Without Slack,** the evening worklog no longer retries and warns four times. It is saved locally, with a "not connected" status.
- **Shared nested defaults.** Config defaults are deep-copied, so changing one config no longer changes the defaults for the whole process.
- **The MCP guard's log** now goes to the data home's `logs/` folder instead of the package folder.
- **CI-only failures:**
  - a time-zone-dependent Jira-facts fixture
  - the pre-commit hook's file mode
  - one-word HTTP/0.9 requests on Python < 3.13
  - dashboard binds on POSIX (`SO_REUSEADDR` plus a 3-second bind retry, including in the test stub)
