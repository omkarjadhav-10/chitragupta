# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Companion** ([docs/companion.md](docs/companion.md)): a ticket becomes a recoverable task in `data/workflows/` (local only). Content-addressed, hash-checked checkpoints and an atomic head; every save carries the revision it edited (a concurrent save keeps your text as a conflict); *Not saved* on a full disk or busy lock with the draft kept; `python -m chitragupta.companion recover <w_…>` quarantines incomplete files. Stages follow one explicit table (new → planning → planned → review → approved → execution ready → in development → awaiting verification / PR review → completed, with pause, reopen, cancel and archive); blocked, stale, cancelled and execution-status-unknown are overlays that keep the stage. Jira status never drives the stage.
- Companion **Daily Planner** (dashboard **Companion** page, `/cg:plan`, `cli companion planner`): your in-scope tickets and tasks in eight stage groups with *why* each is suggested; several attempts or repositories give a chooser, never a guess. Requirement baselines are proposed from the cached Jira card (configured `companion.criteria_fields`, now also requested by the card read, then cited acceptance-criteria lines): the 3,000-character cut, failed reads and missing criteria are named gaps, comments stay proposals until you accept them (Jira is never edited), and criterion IDs survive rewording and reordering.
- Companion plans: manual or Claude-drafted versions are immutable with their parent and diff; **Generate Plan** / **Improve Plan** are explicit paid jobs (`agent companion-job`, tool-less constrained call, 180 s, one per task, two at most) that reserve their ceiling (`budget.companion_call_usd`, new in Settings → Models & budget, `0` = off) against the daily cap in a ledger every session shares; output is validated (unknown criteria, invented existing files, paths, secrets and tool requests refused) and saved before any evaluation; an improvement is only a candidate until selected; a timeout after possible acceptance stays *outcome unknown* and is never retried on its own.
- Companion **Jev Review**: per-criterion Covered / Partial / Missing / Unverified, weighted coverage with its numerator and denominator (37.5% for one of each; confidence never changes it), comparisons that keep regressions and non-comparable baselines visible, **Improve Plan** and **Execute Plan** with one recommended, and a separate *developer assessment*. Live Jev is blocked by the safety policy (*Evaluation unavailable*, never 0% or a pass); the adapter (request building, answer validation, retries only on 429/529, timeouts, cancellation, payload checks) is tested with stub transports only.
- Companion execution handoff: **Execute Plan** saves an exact, expiring approval (plan and context digests, target location, one execution owner per working tree) before preparing the text to paste into an ordinary Claude turn (*Prepared; not submitted*); dispatch states prepared / started / rejected / unknown / cancelled are recorded by you or by captured activity, never by a resend. Chitragupta still runs nothing.
- Companion resume and verification: the tick step `companion` checkpoints new ticket evidence for open tasks without acknowledging it (**Mark changes reviewed** does); **Resume Task** (`/cg:resume`, `cli companion resume`) rebuilds from durable records only and shows stage-specific actions (interrupted planning, development with completed / pending / uncertain / revalidate steps, outstanding tests, an existing PR, a ticket switch) and recovery states (moved or missing repository, deleted branch, changed requirements); **Verify Requirements** keeps plan coverage, evidenced implementation progress and functional verification apart and never verifies from an old pass, a Jira status, a merged PR or a model's report; cited evidence is pinned so pruning can't erase it.
- CI checks for GitHub PRs you raised: during the existing PR refresh, `gh api` GET of the head commit's check runs (≤ 3 pages, ≤ 5 PRs and 60 s per repository; names, states, attempts and times only), kept per head in the ticket's `git.repos.<repo>.ci`; the Companion shows the current head's state (passed / need attention / running / partly read / missing / unavailable) and never lets an earlier head's pass clear it. GitLab and Azure DevOps say *CI checks unavailable: not implemented for this host*. No new command-guard entry was needed.
- Companion handoff **Drafts** (PR reviewer, QA tester, next developer session; deterministic, with a *Missing information* section, versions that never overwrite your edit, *Source evidence changed*, `cli companion draft`), **Attention** acknowledge and snooze (1 hour / 1 day / 1 week; presentation only, `data/workflows/.attention.json`, reopened by materially new evidence), **Decisions** with supersession and conflicts shown, and **Impact** suggestions from recorded links and cached database impact (*No recorded impact links* when there are none; checks are text only).
- Optional native companion (`companion/`, separate plugin `cg-companion`): Focus / Evidence / Attention and workflow panes rendered from a read-only MCP bridge (`companion_read`); verified with Claude Code's offline mod test kit only. The dashboard's `GET /api/companion` and protected `companion` action serve the same projections.
- Database investigation, first part (cached records only; no query on any view): a canonical graph in `data/db/` built by the background step `dbingest` (only with an approved `mcp.database.scope` connection) or `cli db ingest` from schema cards, tickets' DB-flow records, the QA link map and mappings you confirm (`cli db map-confirm`). Table names resolve only through the one approved connection whose scope holds their schema; code citations are checked against the selected repositories (file, lines, content hash); session notes stay unvalidated and inferred; the consolidated summary is a proposal that can't invent tables or citations.
- Table passports (connection and environment, columns, keys, relationships by layer, readers and writers with the owner unknown, routines and triggers, per-source state, coverage and freshness; *Writers incomplete* instead of ever *No writers*), bounded metadata discovery through the database MCP (10 describes a batch, resumable, partial listings never remove an object) and business flows: ordered steps where evidence orders them, column lineage across connections and engines, and gaps with the next useful evidence, labelled *Code model; deployed revision unknown*. Saved flows keep versions and your notes.
- The `worklog-dbflow` block may carry `event` (with operation `publish` / `consume`), `mapping`, `repo`, `revision`, `path`, `path_conditions` and `async`; old blocks and records are read as before.
- Dashboard **Database investigation** (`#/db`): environment label and scope, start candidates, *Explain flow*, *Record journey* and *Evidence* tabs, and the `/api/db/workspace`, `/api/db/passport`, `/api/db/flows`, `/api/db/flow` and `/api/db/resolve` routes ([docs/dashboard.md](docs/dashboard.md)). MCP: `get_db_flow`, and `get_db_table` adds the qualified passport. CLI: `db table|flow|flows|resolve|ingest|map-confirm`.
- **Record investigations** (database flow D2): trace one record across approved connections through an explicit, reviewed, bounded plan (key fields from metadata incl. tenant / composite fields, typed values never stripped or converted; deterministic mappings only, an evidenced lookup as its own bounded step; 20 rows / step, 10 queries, 2 at a time, 30 s statement, 120 s overall, 256 KiB of sanitized values; a plan fingerprint the run must echo). Jobs run in phases, each with its own plan pin; derived phases only with a provider-side timeout or cancellation. Per-step observations (*Observed*, *Not observed within this query scope* — never a failure or data loss —, *Not queried*, *Access denied*, *Failed*, *Truncated*, *Mapping unresolved*), personal saved investigations with notes, corrections, a ticket link and a local handoff draft. Dashboard *Record journey* tab and the `db-plan`, `db-trace-run`, `db-trace-cancel`, `db-note-save`, `db-investigation-attach`, `db-handoff-save`, `db-mapping-confirm`, `db-metadata-refresh` actions; `/api/db/investigations|investigation|plan|job|handoff|context` reads. MCP `get_db_investigation` (11 tools). CLI `db plan|run|status|cancel|investigations|investigation|note|handoff|context`; agent `db-trace` / `db-discover`.
- **Change impact, comparison and drift** (database flow D3, cached metadata only: no query, no model): `cli db impact <object|column> [--change rename|type|nullability|default|semantic|application_only] [--refresh]` lists the recorded readers, writers, triggers, foreign-key dependents, carried values and their consumers, entry points and tickets with the relation path, *Direct evidence* vs *Inferred association*, stale / unavailable flags, why the change kind reaches each and a suggested check (text only; nothing runs), plus coverage limits; nothing recorded reads "No recorded impact links". `cli db compare <left> <right> [--objects …] [--confirm schema.table] [--refresh]` compares two permitted connections (objects pair only through a mapping you confirmed; the same name is a candidate) or two metadata snapshots (`conn@<snapshot>`, `cli db snapshots <conn>`; one is taken after each metadata refresh): added / removed / changed / unchanged / not comparable / unavailable, engine-aware types, both snapshot times; a missing object from a partial or denied listing is unavailable, never removed; a side that is not permitted is refused with the reason. `cli db reconcile` (and the tick, after ingestion) marks saved flows and investigations *Requires revalidation* when a source, a scope or a cited file they depend on changed (versions, runs and notes kept), drops cached results of revoked connections. MCP `get_db_impact` and `get_db_comparison` (13 tools; they read cached results only), `/api/db/impact|impacts|compare|comparisons|snapshots`, and the explicit command `/cg:db table|flow|impact|compare|investigation|plan`.
- Search indexes the database records you may see (`dbobject`, `dbflow`, your `dbinvestigation`; never a value, key, SQL text, objective or note), access-checked on every result.
- Database workspace, finished ([docs/database.md](docs/database.md)): the environment label and coverage line on every database view; a focus header (last observed, unresolved, next) with **View evidence** and **Review trace plan**; **Known impacts** and **Compare metadata** as contextual actions on a flow step, a passport and each column, opening the cached results with a protected compute over cached metadata (`db-impact-compute`, `db-compare-compute`; confirm a same-named pair from the dialog); a graph grouped by database connection, services and events, with line styles and labels for cited, asynchronous, inferred and unknown edges and an equivalent table with direction and boundary; a specific next step for every job state and observation status; status announcements; unsaved notes kept through refreshes.
- `docs/database.md`, and browser walkthroughs of the seven database tasks at 390–1440 px in both themes plus a keyboard pass (`scripts/ui/dbcheck.js`, run by `scripts/ui/run.py` against the synthetic scenario and the fake database MCP; dev only).
- Migration `db1-canonical-graph`: the existing DB flows, schema cards and link map become the canonical graph (backup first; legacy files untouched; ambiguous legacy names stay unresolved; re-running adds nothing).
- Release tracking (`releases.enabled`, off by default; [docs/releases.md](docs/releases.md)): each ticket's Jira release (fix version identity, planned date, the date in its name kept as a separate fact) resolved per repository to a release branch (your mapping, an unambiguous structured link, else suggestions to confirm) with source, confidence and verification time recorded. Ambiguous fix versions and unconfirmed branches stay visible and never yield a development base.
- Per affected repository: development branches and their recorded start, PRs per destination (follow-up, closed and superseded PRs kept; a shared QA integration PR linked to every ticket whose commits it carries), release / QA / UAT stages from the delivery check's evidence (present, partial, missing, unknown, not started, not applicable), wrong-base and wrong-target flags, a moved release kept in history with old targets flagged, and one next step following the repository's strategy.
- UAT as `required` / `not_required` / `unknown` from a configured Jira field or label or your choice; the UAT destination and promotion path per repository; branch inclusion, deployment and acceptance tracked separately (deployment and acceptance only from configured Jira statuses). Reminders are recomputed from evidence every tick and never cleared by being shown.
- A planning brief per repository in the session context (with the cached context's age), `/cg:branches <KEY>` and `get_branch_status`; the monthly release overview through `/cg:branches [YYYY-MM]`, `list_tickets(release=...)` and the dashboard's Releases page, with overdue and unresolved-date groups and per-repo detail. Release and UAT reminders also appear in `get_alerts`.
- Dashboard: a **Releases** page (`#/releases?month=YYYY-MM`) and, on a ticket, a *Release:* line in the header and a **Release plan** section at the top of Branches & PRs; a reminder count on the ticket list. Shareable exports drop the release plan's PR links like the other PR links.
- Built-in strategy `release-then-qa-sync`: a QA integration branch cut from `qa` merges the release branch, then PRs into `qa`.
- `/cg:branch release-map|release-unmap|release-pick|release-branch|release-repos|uat`, applied through `inbox.md`; a *Releases & UAT* group on the Settings page.
- Jira: the card job also reads fix versions, assignee and the configured release / UAT fields (same call); two budgeted read-only jobs read the scope search for the monthly overview and each project's versions catalog (`jira_get_project_versions`, added to the Jira read allowlist).
- Developer profile ([docs/progress.md](docs/progress.md)): display name, primary email, confirmed commit emails (`git_author_emails`), provider accounts scoped to their instance (`profile.accounts`: Jira account id, GitHub / GitLab / Azure DevOps accounts), repositories and Jira projects in scope, focus, role, technologies, time zone, history range and collaborative tickets with a reason. Edited on the dashboard's **Profile** page or with `/cg:setup`'s new profile step, always through the settings write path. Tokens, bot identities and look-alike addresses are refused.
- Identity suggestions with their provenance: each watched repo's `git config user.email` (daily, local), the account `gh` / `glab` / `az` is signed in as (on request), and the Jira account for your primary email (one read-only job, on request). A suggestion counts only after you confirm it (`profile confirm <id>`). Status per provider shows ok / unconfigured / partial / error and why.
- One ownership policy (`profile.Policy`) applied in collection, records, alerts, the dashboard, `/cg:` commands, MCP and exports. Jira tickets are yours when assigned to your confirmed account or included by you; commits when their author email is confirmed (never the committer's); PRs when your confirmed account authored them. A reassigned ticket keeps your earlier activity and its ownership history (you / other / unassigned, never the other person's identity), but stops being current.
- **My progress** (dashboard, `/cg:progress`, CLI `progress`, MCP `get_progress`). Today's authored commits, committed changes (+added / -deleted after visible exclusions), shared and co-authored commits apart, PRs raised (drafts labelled) and merged, and tickets completed from Jira status history. Each metric is known / zero / partial / stale / unknown with why. Evidence drill-down, a month calendar, a week view, personal records with ties and coverage, factual milestone messages, deterministic weekly wins with redacted copy text, a local self-reported daily reflection (`data/reflections/`), and up to three next steps from your PRs and current tickets (none on a day off; no streaks).
- Per-repository activity ledgers (`data/activity/`, tick step `activity`): authored commits from `git log --numstat --root --no-merges` with exact author recheck, co-author trailers, patch-id equivalence for rebases and cherry-picks, reachability after rewrites, squash-merge integration, and future-dated commits left unresolved. PRs come from author-filtered host requests (`gh pr list --author`, `glab mr list --author`, `az repos pr list --creator`), rechecked by account before they are stored.
- Session start adds one compact profile line (name, focus, scope, ownership rule, what isn't confirmed; no email or id). `list_tickets(mine=true)` and `get_progress` are new on the MCP server (9 tools).
- Settings: a *Developer profile* group (`profile.*`, `progress.exclude_globs`, `progress.done_statuses`).
- The Tickets page says why each ticket is yours or not and gains an *Only mine now* filter; `/cg:doctor` reports the profile per provider (an unresolvable time zone fails it; nothing confirmed yet doesn't).
- One personal projection for every ticket reader ([docs/progress.md](docs/progress.md#the-personal-projection)): a record is *current*, *history* (your own earlier or collaborative work), *context* (the Jira parent of one of your tickets) or *outside* (none of your work on it). Outside records aren't listed, returned by key (dashboard, `get_ticket`, `get_branch_status`, `/cg:ticket`, `/cg:branches`), searched, exported, shared or alerted on; they answer only that they are outside and why.
- Several confirmed accounts per provider instance (a work and an older account on `github.com`): PRs are asked for once per account and attributed to it, and the profile preview counts only the removed account's PRs.
- `/cg:branch uat-deployed|uat-accepted <KEY>`: your own confirmation of UAT deployment or acceptance for exactly the changes in the UAT branch now.
- Dashboard: a **History** page (`#/history`, `g y`) with weekly wins, the activity calendar and personal records; Today and My progress open with a source line, the next action and five metric buttons, each opening its own definition, counted events, what was left out and its sources; Health lists *Your sources* with the same connection words as Profile.
- **Evidence Graph** ([docs/evidence-graph.md](docs/evidence-graph.md)): each requirement read from a ticket's Jira card is traced to its plan steps, code changes, commits, tests and runs, pull requests and release evidence, every link labelled observed, declared, inferred or unknown, with three separate statuses (plan coverage, implementation, verification from the Companion's ledger). Built by the background run from cached records only (`<home>/.worklog/cache/evidence-graph.sqlite`, a per-ticket history in `data/graph/`); no process, repository, Jira or model call. Read it with `/cg:graph <KEY> [requirement]` or `cli graph summary|trace|changes|status`; `cli graph rebuild` replaces the index. Every read is checked against the current profile.
- Evidence Graph for Claude and the native Companion: the read-only MCP tool `get_graph(key, view?, requirement?, since?)` answers with the same text as `cli graph` (summary, a requirement's trace, changes), and the Companion bridge gains `graph_summary`, `graph_trace` and `graph_changes` (the same JSON envelope). The own-tool list the session guard allows gains `get_graph`; the `tools/list` result is now 3,783 characters (about 950 tokens when loaded upfront; the test cap moves from 3,500 to 3,800). See [docs/mcp.md](docs/mcp.md).
- Dashboard **Evidence graph** page (`#/graph?ticket=<KEY>`, More menu, `g` `v`): a ticket's requirements with plan coverage, implementation and verification as separate icon-and-word statuses, the source line, freshness and the one next action, with not-built, rebuilding, behind, no-requirements, all-verified and revoked states. Read through the new read-only `GET /api/graph` (the same envelope as `cli graph --json`; 403 with `revoke` outside your profile, 400 on bad input); the page refreshes when the index changes. See [docs/dashboard.md](docs/dashboard.md).

### Changed
- Companion evidence mapped to plan steps now records the plan it was mapped under (`plan` on the pinned evidence). Mapping the same source to steps of another plan version becomes its own pin, so a step id reused in a later version is never taken for the same step. Evidence mapped before this has no `plan` and is read, reconciled and verified exactly as before.
- A captured test run now records the commit HEAD pointed at when the command returned (`context.head`, with `head_basis: "observed after run"`, the location and, when an activity ledger already knows it, the repository id). It is read from the repository's `.git` files with no process, only for commands that neither change directory nor can move HEAD (`cd`, `-C <dir>`, `git checkout`, `git commit`, … record nothing), and it is an observation, not proof of the revision the test ran against. Older test records are unchanged.
- A saved flow's "may be stale" now comes from the sources and scopes it depends on (no rebuild when it is opened). An object a complete listing no longer shows leaves name lookups while a current one matches. The MCP tools no longer list `page` in their schemas (still accepted; a cut answer names it). A repeated metadata-refresh request queues one job.
- Flow steps are numbered by evidenced stage: steps with no order between them share a number, a separate path starts again at 1 (never a list position).
- Change impact: `--env` / `--connection` (MCP and dashboard: `env`, `connection`) narrow a name several environments share instead of being ignored; the headline counts exactly the listed items, links and tickets apart (*3 affected: 2 recorded impact links (…) and 1 ticket …*); a mapping into a column also lists the component whose cited code implements it (*Implements the mapping (writer)*, same citation).
- `/cg:trace`'s QA example: a start value with a quote is declined and reported (never stripped); a numeric-looking value is a number only for an integer column; the start table is labelled a name-similarity guess; a model before/after is kept as observed only when both values were read, else *model summary, unvalidated*. Its sampled rows stay in `data/qa-trace/<KEY>.json`.
- A QA link-map sample is no longer called *verified*: links say *Sample matched NN% (m of n, QA, date)* with the basis *Observed in a sample* (dashboard, DB map, Knowledge, `get_db_table`, `knowledge/db-map.md`'s *Sampled links*). API fields: `verified` → `sample_matched` (with `sample`), statuses `sample_matched` / `sample_partial` / `sample_weak` / `not_sampled`. The **DB map** navigation entry is now **Database**; `#/db/<table>` pages are unchanged.
- Navigation is Today, Tickets, Releases, History, then Profile and Health; My progress, Timesheet, Knowledge, DB map, Search, Team notes, Setup and Settings are under More (every address still works).
- Releases puts Overdue and Release date unresolved first, shows each ticket as one row (pending repositories, one next action) with stacked per-repository blocks on request instead of a wide table, and folds ready tickets. The ticket's release plan says the release once, then per repository the development base, PR target, Release, QA, UAT and next step; blockers once; evidence and provenance on request.
- Profile has three groups (Identity with the suggestions beside your confirmed identities, Scope, Preferences and tracking), one review button each, and keeps unsaved edits as page-level drafts.
- The Jira scope search is always narrowed to your confirmed Jira accounts and Jira projects (an `assignee = currentUser()` is replaced; no confirmed account, no search), read page by page (`releases.jira.scope_max_pages`, default 4, resumable), every row rechecked before it is kept. The default `scope_jql` no longer names the assignee.
- UAT deployment and acceptance count only for the revision (the exact changes) now in the UAT branch, from evidence observed after it got there; earlier changes' evidence is kept as history.
- Development branches and PRs are told apart from integration and promotion ones; a development branch from `main`, `qa` or another base, and a development PR into `main` or straight into `qa` / `uat`, are flagged with the resolved release branch as the target.
- The monthly release view, overdue releases and reminders use the profile's time zone and day boundary; a cached *Ready* older than `releases.stale_hours` is shown as not ready until refreshed.
- Personal records come only from completely collected days; milestones and weekly wins say "at least" for a partial or stale count and never compare it with a record. A recorded PR approval from a disconnected or stale source becomes "check its current state", and setup and recovery come first among next steps. Profile, My progress and Health report a confirmed identity apart from whether the last read worked.
- Activity ledgers are kept per repository location (`data/activity/<folder>-<hash>.json`); a folder-name ledger is read only for the same folder and carried over. Two clones of one repository count their commits and PRs once.
- Reconciling the profile (rules version 2, once, backed up) quarantines commit records whose author can't be verified, like other archived records.
- The delivery check uses the release branch confirmed for the ticket's Jira release as its release target when release tracking is on, and checks the UAT branch when UAT is required.
- The branch guard also warns when a new ticket branch is cut from, or a PR targets, another release branch than the one confirmed for the ticket's Jira release.
- `get_alerts`, release and UAT reminders and the monthly release overview leave out tickets Jira assigns to someone else (or reassigned away from you); `get_alerts` and the dashboard's *Needs you* strip say how many they left out. The release overview counts a "your development branch" ticket only from commits authored by a confirmed email.
- The commit scan reads only the profile's repositories and rechecks each commit's author email exactly; new day and ticket commit records keep that email (`ae`, local only). PR records gain their author (`author`: id / login, no name or email). The Jira card job also asks for the assignee whenever a Jira account is confirmed.
- After an identity or scope change the next tick backs up and moves commit records no longer authored by a confirmed email to `commits_excluded` (kept and labelled, restored when they are yours again), in ticket records and in day records whose evening worklog wasn't sent, and imports the newly confirmed email's commits.

### Fixed
- `agent db-trace` with an unknown job ID ends with "no such job" instead of failing (a missing record read as empty).
- A ticket or recall request by key returned another developer's cached ticket in full (dashboard API, `get_ticket`, search, export).
- After changing or removing the Jira account, release tracking's cached "assigned to you" kept the old account's tickets yours; assignee answers are now bound to the confirmed accounts and unknown otherwise.
- A Jira scope search that returned one page, or an error object, replaced the whole scope as if complete, so tickets vanished from the month view.
- An old Jira *Accepted* status (even from a failed read) could make later UAT changes ready.
- A confirmed commit email with `+` (or other regex characters) collected nothing: git's `--author` treated it as a pattern. Author and co-author filters are now literal (`--fixed-strings`).
- Two clones of the same repository doubled commits, lines and PRs; two repositories with the same folder name shared one activity ledger.
- Adding a second account on the same host replaced the first.
- Showing emails and ids on the Profile page discarded an unsaved edit.
- Releases and Profile scrolled sideways on narrow screens.
- Ticket completions near midnight could be missed when the profile's time zone and this machine's put them on different days.
- MCP jobs on macOS and Linux read a large tool result that Claude Code saved to a file (a POSIX path); only Windows drive paths were recognised before.
- Tests no longer assume the machine's clock is UTC+05:30 (fixtures use the local timezone), and the Windows-only path-guard tests are skipped elsewhere, so CI passes on its UTC runners.
- The commit scan's `--author` pattern also matched look-alike addresses (`dev@acme.test` matched `xdev@acme.test`); every result is now rechecked exactly.
- With no `git_author_emails`, branch tracking counted every commit as yours; it is now unknown and never counted as yours (the delivery check is unchanged).
- The database workspace's start box kept an earlier kind when the address changed, and a value typed right after the page opened could be reset.
- Flowchart diagrams (a ticket's DB flow, the database graph) drew at a 300 px default width, unreadable until zoomed; they now draw at their natural size inside the pan-and-zoom view.

### Security
- With an explicit `mcp.database.scope`, the older DB map (`/api/db`, `/api/db/table`, `#/db/<table>`) shows only connections approved now: a connection removed from the scope, revoked or in a blocked environment no longer lists its tables, columns or sampled links there.

## [0.5.1] - 2026-10-25

### Added
- Briefs select up to five key queries and three context queries. Default query views keep both groups visible; short dated SQL candidates help preserve early cause evidence.

- Gzip negotiation for dashboard JSON responses, including explicit identity and HEAD handling.
- Monotonic per-step tick timings and a Health warning for background runs over 60 seconds.
- A grounded six-question summary and concrete next action alongside the preserved full brief, with a backed migration and the existing single-call budget/triggers. Jira, grouped git changes and actual verification facts win over model wording; only owner provenance overrides the cause.
- Additive record/scenario relevance, explanations and literal-query variants retain all evidence while separating core work from supporting context and noise; a backed migration covers existing tickets.
- A learning guide maps captured events, ticket knowledge, time, delivery and the interface to the project's code and documentation.

### Changed
- Ticket pages, hubs, exports and SessionStart lead with six cited answers; Full brief preserves the previous explanation behind one disclosure. Epic child rows answer the same six questions, and verification chips open recorded proof.
- Migrated tickets show capped core lists with meaningful pre-pick ordering and honest not-yet-ranked labels. Full evidence stays behind All recorded and generated companions; the dashboard loads heavy tabs, archives and hub text on demand. Global DB maps filter noise and unresolved reviews.
- The existing brief call also selects validated core query, table, API and scenario ids. DB-flow consolidation receives the problem and current cause/fix without adding a relevance model call.
- The quiet Mix dashboard has primary navigation and More pages, a Day Line and worklog ledger, ticket evidence margins, a monthly timesheet, concise Health summaries and a sticky settings review bar. Night ink is the default; saved choices remain respected. Fonts and licenses are embedded in ticket exports.

### Fixed
- Focused Epic query files, SessionStart and core API lists keep retained child evidence under the children instead of presenting it as Epic knowledge.

- Summary citations open and focus supporting query evidence even when its lazy response takes longer than one animation cycle.

- Cache empty branch commit facts and invalidate them when the remote tip moves, avoiding repeated unchanged-ticket rewrites.
- Make path verification converge with fair ticket budgets, committed-file cache priority, and terminal unknown-repo results.
- Stagger background fetches across ticks, with at most two due repositories and fair rotation after failures.
- Batch host CLI pull-request refreshes once per repository and reuse open-PR checks until due.
- Reuse one off-ticket record profile per ticket during merges and migrations, retaining self-evidence exclusion.
- Six-question summaries show the newest logical changes and count earlier groups; fallback unknowns prioritize current blockers, questions and risks while preserving grounded model choices first.
- Ticket change facts group cherry-picked copies by delivery patch id and preserve old SHA citation aliases; stash snapshots are excluded from commit capture and summary changes.
- Focused hubs retain labelled title hints and core scenario effects; SessionStart retains the core DB summary and recorded query companions show sent/raw provenance.
- Ordinary owner notes beginning with keep/move and unresolved review commands remain inbox notes; only successfully resolved commands are consumed.
- Record review flags appear in Needs review with copyable inbox keep/move commands for query ids, table keys and API keys; moves copy additively and archive the original.
- K8 reviews queries, tables and APIs as well as findings, preserving records and owner decisions; existing tickets receive flags through a backed-up migration.
- Multi-ticket prompts prefer a named work-branch key, retaining first-own-words and last-breadcrumb behavior otherwise. Configured shared hotfix bases remain excluded from branch attribution.
- Follow-ups continue the immediately previous turn within thirty minutes: notifications inherit any source, while unnamed typed prompts of at most twelve words inherit only an own-words chain. Branch-decided questions end that chain; tools do not renew its window.
- Queries, tables, APIs and files follow their own turn and carry their own timestamp, with trusted raw/SQL times preferred over model or chunk times.
- Parallel tickets use separate Day Line tracks; Jira entries without a start time remain in the ledger with an explanation.
- Older media-query APIs keep appearance setup working, including saved density and motion choices.
- Finding evidence no longer creates repeated landmarks, More references an existing menu, and j/k opens the ticket switcher before moving visible focus.
- Printed dashboard pages and standalone exports force light colors so ink stays visible when backgrounds are omitted, including before the appearance script runs.
- Ticket Overview renders delivery alerts and their accessible alert region once.
- Supersession skips a final singleton when no comparison context fits, avoiding a model call that cannot supersede anything.
- Rebuild and interrupted migration retries do not duplicate archived history versions.
- Brief rendering reads passing evidence directly and retains older passing-test citations beyond the recent-facts window.
- Rebuild restoration unions prior and re-extracted histories, preserving both through compaction.
- Terse re-mentions preserve richer explanations and do not promote confidence. Identical updates are no-ops; brief freshness ignores whitespace and case.
- Item history uses content deduplication, chronological snapshots without evidence, and a five-version inline bound. Older versions remain in an additive archive; existing data is backed up before migration.
- Extracted notes accept corrections; only inbox/user provenance protects owner wording. Identical owner-note retries do not count as updates, and evidence is capped at ten.
- Recompute Jira-only verification in both directions when status or configuration changes.
- Resolve an exact current-item citation with an item: prefix while rejecting unknown or historical ids.
- Keep dashboard activity fixtures deterministic around midnight.
- Wrap long brief method names and owner corrections on phones, keep ticket actions below the heading, and lead with generated content without repeating the legacy summary.


### Security
- Allow guarded names-only `ls-tree` reads for path verification; reject output-file and repository-redirect options. Document existing `ls-files` reads alongside the safety red-team coverage.

### Migration
- Backed `kb51-record-review`, `kb51-relevance` and `kb51-summary` steps add review/relevance fields and six-section summaries while preserving canonical evidence and the previous brief.
- `kb51-record-provenance` backs up tickets/catalog before marking unknown historical origins as legacy, without moving records or inventing times.
- `kb51-history-compact` backs up canonical tickets and existing history archives, then retains the first and newest four snapshots inline. Older versions remain in additive, content-deduplicated `data/history/<KEY>.jsonl` files.

## [0.5.0] - 2026-10-24

Knowledge you can trust, and a one-minute brief per ticket. Work is filed under the ticket you name, old conclusions
are superseded instead of piling up, file paths and queries are checked against what really happened, time is counted
once, and every ticket page opens on a short, evidenced brief. Includes a data migration (backed up first).

### Added
- **Ticket brief.** Each ticket gets a short brief (`tk["brief"]`, `chitragupta/brief.py`): Root cause (or "Why this
  change" for a Story or Task), Helpful findings, How it works today, Proposed fix, Fixes made, How it works after the
  fix and Possible cons (*Recorded* or *Suggested*), about 400 words. One JSON-schema model call (`models.brief`, else
  `models.consolidate`) over the ticket's current items and deterministic facts, never raw events. Every claim must
  cite an evidence id (an item, `commit:`, `pr:`, `jira:`, `query:`, `table:`, `replay:`, `test:`); claims that cite
  nothing, a missing or superseded id, or a path, table, endpoint or class the ticket's verified records don't contain
  are dropped and listed. "Fixes made" and the statuses come from git, PR and Jira data, never from model text. Your
  inbox correction (`ABC-1001: root cause: …`, `fix: …`, `cons: …`) replaces the model's wording and survives
  regeneration. Written at day close when its inputs changed, by `/cg:ticket <KEY> --brief`, and by the dashboard's
  **Refresh brief** (new allowlisted action `brief-refresh`); "New findings since …" shows when it is out of date.
- The ticket page, the hub `README.md`, the single-file export, the SessionStart context and the MCP server's
  `get_ticket` (new section `brief`) lead with the brief; everything extracted stays one click away under
  *All findings*. An Epic (any `jira.parent_issue_types`) gets no brief: its page lists its child tickets.
  `docs/design.md` documents the dashboard's tokens and the brief's components.
- **`agent rebuild <KEY...> | --all`** rebuilds tickets' knowledge from the raw events on disk under the current rules.
  `--dry-run` prices it from the spend ledger and writes nothing; a real run takes the agent lock, refuses over
  `rebuild.cap_usd`, backs up first, archives the old extracted items (never deletes them; your notes stay current),
  re-extracts, recomputes minutes (keeping `minutes_before_rebuild`), runs supersession and the brief, and re-renders
  hubs and INDEX. Daily files are not touched.
- **Model spend ledger and daily cap.** Every internal model call records tokens and cost per day and purpose
  (`health.model_usage`). `budget.model_daily_usd` ($3) gates supersession and briefs (never extraction);
  `brief.daily_cap` (8). Health, doctor and the dashboard show today's spend against the cap. `models.*` accept
  explicit model ids.
- Extraction inputs for the brief: raw line numbers (items keep `ref: raw/<day>/<session>.jsonl:<line>`), how-it-works
  `phase` (before / after / both), fix `state` (proposed / applied when an edit or commit touches a file it names), a
  `risk` item kind, deterministic test-run records, and the Jira card's description and last 10 comments (redacted).
- `jira.parent_issue_types` (Epic, Initiative), `turn_tail_minutes`, `idle_minutes`, `rebuild.*` settings.

### Changed
- **Ticket attribution (K1, K2).** Each turn's ticket is decided once, for time and knowledge alike: the ticket named
  in the prompt (or a pasted Jira title) → the work branch of the session's own repo (never release/, environment or
  base branches) → the last ticket named in the session. Raw events record `ticket_src`. A pasted breadcrumb gives its
  last id; an id typed in your own words beats one in pasted text; a parent issue never receives time or items.
- **Time (K10).** Minutes are turn intervals (prompt → last event of the turn, + `turn_tail_minutes` 5, split at
  `idle_minutes` 15), counted once across parallel sessions and split between their tickets. Session start/end events
  no longer count; commits and Bruno runs count as the 15 minutes before them; Bruno file times count only today.
- **Supersession (K3).** At day close one per-ticket call (budget-gated) marks older root causes, decisions and
  findings on the same subject as superseded (kept, collapsed, linked to their replacement); text is never rewritten
  and your notes are never superseded. Git-action TODOs (rebase, push, open a PR, merge, cherry-pick) close from
  branch and PR data. The extractor's "already recorded" list is split per ticket and refreshed per chunk.
- **Titles and status (K4, K9).** A title comes only from the Jira card or you; guesses are kept as a labelled hint
  and the key is shown until then. The Jira status and the inferred status are stored and shown apart. A delivery
  stage computed from branch and PR data leads the hub, dashboard and SessionStart context; a TL;DR sentence that
  contradicts it is marked outdated.
- **Verified specifics (K6).** Changed files come only from edits and commits; other paths are checked with read-only
  `git rev-parse <sha>:<path>` and hidden until verified. `queries.md` lists only SQL that was actually sent, each
  with its raw line; SQL that was only mentioned stays in the data under *Not run*.
- **One record per thing (K7).** APIs (method + path template), tables (schema-qualified), queries (normalised SQL) and
  files are deduplicated; merged records keep every source and old ids as aliases; one Code map.
- **Off-ticket items (K8)** are flagged under *Needs review*, never moved or deleted; inbox `KEY: keep <id>` /
  `KEY: move <id> OTHER` resolve them (a move copies and archives).
- **Jira cards (K5)** are fetched for every ticket active in the last 7 days whose card is missing or stale.
- **Replay safety (K11).** Steps that change a shared record are flagged in `requests.md` and need `--yes` in
  `replay.sh` outside local; a restore step only when the before-state was captured; repeat steps get their own
  `.bru` so `bru run` matches `replay.sh`.
- Schema cards (O2): writer detection is one ordered, schema-aware catalog query per table; a routine is a writer only
  when its source writes the table; known writers are never dropped by a failed or empty run.
- Raw capture older than `raw_retention_days` is pruned once processed on every install, not only after a push (O7).

### Fixed
- The home folder itself is captured time-only (O1); raw events from folders that are now ignored are never
  extracted (O5); extraction state survives copying or moving the data home (O4).
- A privacy check that returns unknown now pauses the push and says so, as `docs/privacy.md` promised (O6).
- Doctor and Health show the security log's counts and layer breakdown from the same window, with other plugins'
  entries apart (O3).
- `docs/architecture.md` says what uninstall removes and keeps (O9).

### Migration
Forward-only steps, each backing up what it changes to `.worklog/backups/chitragupta-<step>-<time>/` first, run by
the next tick or `migrate`: `raw-relative-keys`, `kb5-db-schema-writers`, `kb5-dedupe-keys`, `kb5-title-status`,
`kb5-paths-verified`, `kb5-queries-sent`, `kb5-todo-objects`, `kb5-schema-version` (`data/meta.json` 4 → 5). Nothing
is deleted. Existing hubs keep their old content until you run `agent rebuild`.

## [0.4.1] - 2026-10-20

Fixes from the end-to-end audit of 19 Oct: the `/cg:` guard's scope, read-only gaps in the query and command guards,
capture leaks from dev folders, MCP budget and status, and run records.

### Security
- Query guard: comments and literals are read in one pass, and syntax that SQL dialects read differently is refused
  instead of guessed (MySQL `/*! */` and `/*M! */`, nested comments, a backslash before a closing quote, dollar
  quoting, quotes inside `[brackets]`, `#` or `--` without a space before a quote, bracket or `;` on the same line).
  `-- ` comments end at CR or LF.
- Query guard: catalog reads skip the row limit only when every FROM / JOIN / APPLY source is a catalog object.
- MCP guard: with `connections` configured, every database tool call names exactly one allowed connection (no fallback
  to the server's default); routing arguments (env, server, database …) must name an allowed one too. Exempt tools
  are the explicit `connectionless` list in allow.json (default whoami, list_connections); a query tool never is.
- Command guard: internal `claude` calls need `-p` / `--print` as the first argument.
- Slack: the sender always runs with `--strict-mcp-config` and only the Slack server, in `local` mode too (the
  local-scope definition is passed as is; no token is copied); the command guard no longer exempts the sender folder.

### Fixed
- `/cg:` session guard acts only for the prompt that ran the `/cg:` command. A synchronous `UserPromptExpansion` hook,
  matched to the `/cg:` command names only, records that prompt's `prompt_id`, and the guard compares it with each
  tool call's `prompt_id`. Your next prompt, an interrupted (Esc) command or a resumed session is no longer blocked.
  The transcript fallback and the UserPromptSubmit mark are gone. With no `prompt_id`, the guard does not act.
- The `/cg:` skills' guard hook no longer uses `onFailure: block`, and `run.py safety session` exits 0 when the
  package can't load, so a broken install never blocks your session. Internal calls' guard still fails closed.
- Other plugins' MCP tools called during a `/cg:` command are still denied ("not a Chitragupta tool; skip this call
  and don't retry it"), but logged with `"category": "other-plugin"` and not counted as blocks on Health, in
  `/cg:doctor` or by `get_alerts`.
- Query guard no longer over-blocks reads: a query tool's own `limit` (number or digit string, 1..max_rows) caps the
  rows so the statement needs no TOP / LIMIT; a SELECT without FROM needs no limit; a SQL call with a `filter`
  argument isn't sent to the MongoDB check; a select-list column named `copy` passes.
- Capture: `.worklog-ignore` now covers every folder below it, up to your home folder, so a repo nested inside an
  ignored checkout is no longer captured.
- Ticket prefixes and watched repos are learned only from captured work: never from folders outside the capture
  scope, ignored folders, the system temp folder or Chitragupta's own folders.
- The transcript safety net applies the hooks' capture rules to every message's folder (time-only in the data home,
  nothing in excluded, ignored or Chitragupta folders) and recovers full content only inside a git repo or an include
  path, so home-folder sessions no longer appear under "Other work" through it.
- Artifacts are never filed from ignored folders, the data home, the runtime folder or the plugin.
- The configured example ticket (`render.example_ticket`, `jira_worklog.example_ticket`) is never detected as a
  ticket, never gets time in the evening worklog, a hub or a Jira call.
- MCP jobs: a daily budget unit is counted only when a job made at least one MCP tool call; jobs that made none are
  counted apart under their own cap. While a server is known unreachable or signed out, background Jira/database jobs
  wait, with one probe job at most every 2 hours, and one failed job stops that server's other jobs in the same run.
- Evening worklog: a retry after a failed Slack send is recorded as `retry` and reuses the Jira facts fetched in the
  last 30 minutes instead of reading Jira again.
- MCP status: "unreachable" (was "offline") and "signed out" are told apart and keep the check's error text and
  time; health keeps `mcp.offline` per server only, adds `mcp.server_state` and `mcp.job_errors`, and the next job
  that reaches a server clears its offline/skip marks. Older status caches and health files still read.
- Jira allowlist: the entry is filed under `mcp.jira.server` and refreshed from the code daily, so a renamed Jira
  server works and new read tools reach existing installs. `get_write_request` is no longer listed as a database read.
- MCP jobs: a large tool result saved to a file is used even when its delete is refused.
- `/cg:check` says "Fetched" only for repos it actually fetched; the CLI help lists `check` among the commands that
  write (Chitragupta's own branch data). The data home's README says only `daily/` is pushed and QA values may stay
  local. Stale docs corrected (branch-tracking limits, dashboard write actions, install requirements).

### Added
- `placeholder_tickets` (default `[]`): more example ticket keys that are never treated as tickets.
- `/cg:` commands may use Chitragupta's own read-only MCP tools (`mcp__plugin_cg_chitragupta__*`) without an
  allow.json entry.
- Background-run records in `.worklog/logs/ticks.jsonl` (ok, error, skipped-busy, skipped-upgrade, duration; last
  50), and a warning on SessionStart, `/cg:doctor`, Health and `get_alerts` when hooks recorded events in the last
  hour but no background run succeeded for over 30 minutes.
- security.log: timestamps carry the local UTC offset (older lines still read), and the caller is taken from the
  stack when the code doesn't name one.

### Changed
- A Slack send that makes no call now says whether the Slack server connected and whether the send tool loaded.
- Contributor rule 1 has one documented exception: the synchronous `UserPromptExpansion` hook for `/cg:` commands.

### Internal
- tests/support.py: the restore runs as the last cleanup; the whole run gets a sandbox user folder; leftover temp
  folders fail the test.

## [0.4.0] - 2026-10-18

### Added
- **Settings page with a single config write path.** Every setting the dashboard may change, with validation shared by page and server, a review (now → after saving), history in `settings-history.jsonl` and undo; locked items are shown only. `configstore.py` is now the one way config changes (setup, the engine and the dashboard), with a lock, a backup and history; the engine saves only the keys it changed.
- **Live MCP status** on Health and Settings: Check now, an automatic check when the page opens (at most every 5 minutes), the same code as `/cg:doctor`, shown next to the last background job's view.
- **Past days:** `#/day/<date>` shows the message as sent with its send history, the day's branch and PR events and its tickets' alerts, with a date picker; `/cg:dashboard day today|yesterday|-N|YYYY-MM-DD`.
- **Dashboard write protection:** an allowlist of write routes, each checked for Host, Origin, a per-start page token, JSON, size and rate. First actions: pause/resume capture and the live MCP check.
- **More settings:** on/off switches for Jira ticket cards and evening facts, the QA trace, the weekly link map, the daily push, request curation and artifacts (all on unless switched off, so existing setups don't change); per repo, the base branch names, the base relations (with *Detect from the repo*) and the PR source.
- **Today shows the exact 20:00 message:** the preview is the sender's own builder, with "Jira data as of …", lines marked *condensed at 20:00*, and **Refresh from Jira** / **Preview exactly** (within the daily MCP budget; can be hidden in Settings). A sent day has **Send again**, and its send history says when Slack was off or not connected.
- **Health shows the search index** (entries, build and update times) with **Rebuild index**.
- **Search page filters:** ticket, repo and date range (kept in the address); repo chips on results; tables and DB links open in the DB map.
- **Mark a day from the Timesheet page** (leave, half day, holiday, extra workday, or remove the mark), and **`clear: <day>`** in `inbox.md` / `/cg:log` to undo a mark.
- **Share from the dashboard and read your team's notes:** a ticket's **Share…** dialog (preview, then share), the Team notes page (`#/team`), and Settings for share targets, level and your name. Settings and Health show the background job for this OS with **Repair**, and Health lists each repo's PR source and last refresh.
- **DB map page** (`#/db`): databases → schemas → tables in the side list, with search; per table its columns (type, keys, personal-data flag), its links with how well each was verified in QA (verified, likely or not verified, with the sampled-row counts), who reads and writes it, its tickets and saved queries, and a direct or two-hop neighbourhood diagram with a table version. The whole map is drawn only on request.
- **Search index** ([docs/search.md](docs/search.md)): one persistent SQLite index (`.worklog\cache\search.sqlite`) for the dashboard, `/cg:search` and the MCP server. It now also covers saved and captured requests, branches, PRs and DB links; each result names the data-home file to read. Updated incrementally after every render (by content hash), rebuilt on a schema change or with `/cg:search --rebuild`. Filters by type, ticket, repo and date range (`/api/search?q=&type=&ticket=&repo=&from=&to=&limit=`).
- **`/cg:search <words> [--rebuild]`**: the top results with the file to read for each.
- **Read-only MCP server** (`chitragupta`, started from `bin/cg-mcp`): 8 tools (`search`, `get_ticket`, `get_day`, `get_timesheet`, `get_branch_status`, `get_db_table`, `list_tickets`, `get_alerts`) answer in compact Markdown with file paths, at most 8,000 characters per page. The recall skill uses them before reading files. Chitragupta's own background calls switch the plugin off. See [docs/mcp.md](docs/mcp.md).
- **PR data through a Git host's read-only MCP server** (Azure DevOps, GitHub) when the host CLI isn't installed: `/cg:setup use-git-host <server>` records it and allows only that provider's classified PR and branch read tools; one budgeted job per repo per refresh; `branching.repos.<repo>.pr_source` takes `auto`, `cli`, `mcp` or `git`; Health and Settings show each repo's PR source and its last refresh.
- **The background run on macOS and Linux** ([docs/scheduling.md](docs/scheduling.md)): a launchd LaunchAgent on macOS, a systemd user timer on Linux (catch-up after sleep), and one marked crontab line where systemd user units aren't available, behind one interface with Windows' Task Scheduler task. Setup registers it, `setup scheduler [--repair]` shows and repairs it, doctor reports whether it is current, uninstall removes it. The command guard allows `launchctl`, `systemctl --user` and `crontab` for Chitragupta's own job only; a crontab change must leave every other line as it was. The hooks still start with `py -3` until the `bin/cg-hook` launcher is switched in.
- **Team sharing** ([docs/sharing.md](docs/sharing.md)): `/cg:share <KEY>` previews the sections you pick at the `team` (default) or `public` level, with what each section loses, then writes a bundle (`README.md` + `ticket.html`) to a share folder or a team Git repo. Nothing is written when the deny-list or secret scan finds anything. A git target commits and pushes `shares/` only, never forced, never rewriting history. `--notes` lists your team's bundles. `cli ticket --export --mode team`.

### Changed
- **The worklog preview is the 20:00 message.** One builder makes the message for the sender and every preview (`/cg:today`, `agent worklog-show`): Jira comments, time already in Jira subtracted, condensed descriptions, closing note, delivery check, correction header. A preview uses the last Jira data ("Jira data as of HH:MM") and cached condensed lines, marks lines still "condensed at 20:00", and calls neither Jira nor the model. `worklog-show --jira` refreshes the Jira data (one read job) and `--exact` condenses now, both within the daily MCP budget ([docs/configuration.md](docs/configuration.md)).
- **Calendar marks count every copy, like `resend:`** (mark, clear, mark again works), and a day keeps only its newest mark. `time:` lines are still read once.
- **`resend:` can be repeated.** Every new copy of a `resend:` line sends the day again (other inbox lines are still read once); `/cg:log resend: <day>` always adds one. Each send, resends included, is kept in the day's history. Migration `inbox-counts` converts the inbox state, backing up `state.json` first.

### Fixed
- The path guard could refuse a write in the data home on Windows when another thread was deleting a file there at the same moment (Windows resolves such a file into NTFS's `$Deleted` folder); it now judges the file by its folder.
- Ticket exports are smaller: pages an export never shows are left out (about 170 KB instead of 310 KB without diagrams).
- The date pickers (Today's "Go to a day", the Timesheet month) now show the focus ring on each part, including the browser's calendar button.
- `agent worklog-show` could make a model call while labelled read-only.
- GitHub and GitLab repos passed the local folder to `gh` / `glab --repo` instead of `owner/repo`; the host path is now kept apart (`remote_path` in `repo.json`).
- A shareable export kept the ticket README's "My notes"; every shared level now removes them.

### Security
- The path guard opens SQLite files only through `paths.sqlite_connect` (write roots checked, `ATTACH` and `VACUUM INTO` refused); the static check rejects any other `sqlite3.connect` of a file.

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
- **Base relations** (`branching.repos.<repo>.relations`: `cut_from`, `merges_into`, `syncs_from`): the branch graph draws bases from their real fork points and the merges between them; missing delivery cells say how the change will arrive (verdicts unchanged). `cli relations detect` proposes them from local refs (read-only), `cli relations set` saves them.

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
