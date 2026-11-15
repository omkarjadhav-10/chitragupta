# Development Context

This is persistent project context requested by the repository owner. Use it alongside `CLAUDE.md` when developing Chitragupta. The contributor contract defines the rules; current code and `CHANGELOG.md` establish implementation details and release status.

## Why We Build It

Engineering knowledge becomes scattered across Claude Code sessions, tickets, branches, SQL queries, API requests, and investigation artifacts. Reconstructing that work wastes time and loses context between sessions.

Chitragupta preserves a persistent memory of engineering work: what happened, what was discovered, how to reproduce it, and whether changes reached their required delivery branches. Automatic daily notes, standups, and Jira-worklog drafts make that memory useful immediately. Future Claude sessions should retrieve recorded evidence and build on it.

## What We Build

The `cg` Claude Code plugin produces daily notes, ticket histories, reusable requests, database flows, artifacts, timesheets, and a knowledge base. Users access these through explicit `/cg:` commands, a local dashboard, and an automatic recall skill backed by read-only MCP tools.

Slack, Jira, database MCPs, and Git hosts are optional integrations. The core works without them. Evening worklogs are composed locally and optionally sent as a Slack DM to the user; Jira access remains read-only.

## How It Works

1. Fast hooks capture redacted session events without network or model calls.
2. A scheduled background tick normally runs every five minutes, also ingesting eligible transcripts, commits, requests, SQL scripts, and manual notes.
3. Constrained `claude -p` calls extract structured findings. Code validates results, merges stable identifiers, and renders documents while preserving personal notes. Extraction failures have a deterministic fallback.
4. JSON holds canonical records, Markdown provides readable outputs, and SQLite indexes search. The dashboard and MCP server expose this knowledge.

Database investigation ([database.md](database.md)) is one evidence-graph backend under `data/db/` for the dashboard's workspace, the CLI, MCP tools, search and recall: flows are explained from catalog, code and sample evidence with their basis labelled, a record is traced only through a reviewed plan the safety guard pins, and impact and comparison are computed from cached metadata. Every read is access-checked against the approved scope and never queries, and the live database MCP remains unverified (synthetic fixtures only).

The Companion ([companion.md](companion.md)) turns a ticket into a recoverable task under `data/workflows/`: requirement baselines, immutable plan versions, an explicit STATE-01 stage machine, hash-checked checkpoints published atomically with expected revisions, evidence drawn from canonical ticket records (ingest watermark apart from the acknowledged cursor), and one bounded projection service shared by the dashboard, `cli companion`, `/cg:plan`, `/cg:resume` and an optional native mod (`companion/`, read-only MCP bridge). Execution is never Chitragupta's: an approval prepares text the developer pastes into an ordinary Claude turn. Paid planning calls are explicit, reserved against the daily cap and off until a per-call ceiling is set; live Jev evaluation is blocked by the GET-only network guard until a reviewed safety amendment, and native panes are verified only with Claude Code's offline mod test kit.

Branch delivery checks use Git evidence. Activity-derived time is approximate. A versioned stable runtime survives plugin path changes. Runtime code uses Python's standard library; the dashboard uses JavaScript modules and vendored Preact without a frontend build step.

The dashboard follows the quiet Mix design: top navigation, a Day Line and worklog ledger, evidence in ticket margins, and locally bundled Source Serif 4 / IBM Plex Sans fonts. Night ink is the default theme; explicit browser preferences remain respected. Standalone ticket exports embed both fonts and their license notices. Browser checks select each theme explicitly and cover both at 1440, 1280 and 900 pixels.

## Development Priorities

- Preserve privacy, capture boundaries, and local ownership of data.
- Route side effects through `chitragupta/safety/`; keep work repositories read-only.
- Keep capture hooks fail-open and internal tool guards fail-closed.
- Preserve existing data formats through backed-up forward migrations.
- Test with isolated homes and synthetic data.
- Keep retrieved claims grounded in recorded evidence.

## Evolution

The standalone engine became the plugin in 0.1. Subsequent releases added branch delivery tracking (0.2), centralized safety and an expanded dashboard (0.3), shared search, MCP recall, protected actions, sharing, and scheduler backends (0.4). Version 0.4.1 hardened guards, capture scope, MCP budgets, and diagnostics. Consult the current roadmap before treating platform support or redaction limitations as resolved.

Unreleased: release tracking (`release.py`) resolves each ticket's Jira release to a release branch per repo with recorded confidence, pins it in the delivery check, evaluates release / QA / UAT stages with freshness kept apart from status, and surfaces a planning brief, reminders and a monthly Releases overview; Jira release data comes from the existing card job plus two budgeted read-only jobs.

Unreleased: the developer profile (`profile.py`) is the single answer to "whose work is this". `Policy` decides ownership from confirmed identities only (commit author emails, provider accounts scoped to their instance, the Jira assignee compared with the confirmed account), and the commit scan, branch tracking, release scope, Jira facts, alerts, MCP tools, dashboard and progress all ask it. Suggestions (git config, host CLI self-accounts, a Jira lookup) carry their provenance and never activate on their own. A profile change reconciles records under a signature, backed up and archived rather than deleted. `activity.py` keeps per-repository ledgers of authored commits (numstat, patch-id equivalence, reachability) and author-filtered PRs. `progress.py` derives day metrics with known / zero / partial / stale / unknown, records, milestones, weekly wins and next steps, deterministically and without a model.

Unreleased (review fixes): one personal projection (`profile.projection`, served by `dashboard.api.scope_index`) decides which ticket records any reader may return: current, your own history, a parent as context, or outside (never listed, returned by key, searched, exported or shared). Several accounts per provider instance; Jira assignee answers are bound to the confirmed account set (`Policy.jira_binding`), so an old account's answer is unknown. The Jira scope search is narrowed to confirmed accounts and projects, paged, rechecked row by row and merged without false absence (`release.merge_scope`). UAT deployment and acceptance are bound to the revision in UAT (`release.uat_revision` / `uat_evidence`). Development versus integration intent decides base and PR-target flags (`release.branch_intent`). Activity ledgers are keyed per location and progress deduplicates by canonical repository identity; git author filters are literal (`--fixed-strings`). Source status travels into records, milestones, next steps and weekly wins, and identity is reported apart from connection (`profile.connections`). The dashboard is action-first: Today and My progress open with the source line, the next action and five metric drilldowns (`lib/focus.js`); History holds wins, calendar and records; Releases and the release plan are compact with evidence on request; Profile has three groups with page-level drafts.

Version 0.5.0 introduced ticket briefs backed by current evidence. Version 0.5.1 leads with six cited questions and a next action, with capped core records and full retained evidence on demand. History compaction is additive and backed up; attribution follows the previous turn for narrow continuations. Background work batches repository PR reads, rotates fetches and path budgets, and records monotonic step timings.
