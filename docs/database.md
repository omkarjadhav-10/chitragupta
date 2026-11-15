# Database investigation

Chitragupta can explain how a business operation moves data through your databases, trace one explicitly chosen record
through that flow, and tell you what a schema change would touch, all from evidence it has already recorded. This page
describes what the workspace does, what each kind of evidence proves and doesn't prove, and the limits.

Everything here reads the knowledge base. Opening, rendering, paging, searching, recalling or polling never queries a
database, never calls a model and never starts a process. The only things that read a database are an explicit metadata
refresh (describe and catalog reads, never rows) and a record trace you review and run yourself.

The internal database tool is always "the database MCP" (`mcp.database` in your config). Chitragupta has been tested
only against a synthetic stand-in for it; see [Known limits](#known-limits).

## Before you start

1. Name the database MCP server and its tools in `/cg:setup` (`mcp.database.server`, `mcp.database.tools`).
2. Approve a scope: `mcp.database.scope` lists each connection with its environment, engine, databases and schemas, and
   the repositories whose code may be cited. A connection is used only when it is in that scope with an explicit
   environment, the environment is in `qa_data.allowed_envs`, its name contains no `prod`, and the safety guard allows
   it. Without a scope the connections are derived from `mcp.database.connections` / `allowed`, `types` and
   `schema_map` (provenance "derived from mcp.database.connections"). See [Configuration](configuration.md#database-scope).
3. Optionally map the server's argument names (`mcp.database.arguments`). A mapping the guard can't recognise makes the
   capability *unsupported*: nothing is guessed.

The background tick then turns schema cards, your tickets' recorded DB flows, the QA link map and mappings you
confirmed into one canonical graph under `data/db/`. `cli db ingest` does the same on demand.

## The workspace

Open **Database** in the dashboard (`#/db`). Every database view, including the older one-table pages
(`#/db/<schema.table>`), keeps a header with:

- the **environment label** of the approved scope (`QA`, or `DEV/QA` when two environments are approved) and
  **Change**, which explains the scope (approved and blocked connections with their reason, repositories, where the
  scope comes from). Editing the scope happens in `/cg:setup` or the config file, never in the workspace;
- a **coverage and freshness line**: connections, objects described, saved flows and when the graph was last ingested.

Below it are the states that apply now, each with its next step, and a start box. A ticket, API operation, code
symbol or file, table or words resolve to the candidates you may see. Several matches stay candidates: nothing is
chosen for you. A flow then opens in three views.

### States and their next step

No state is ever shown as an empty graph or a bare "No data".

| State | What it means | Next step shown |
|---|---|---|
| No database MCP configured | `mcp.database.server` is empty | Name the server in `/cg:setup` |
| No connection selected | No connection is approved | Add one with its environment to `mcp.database.scope` |
| Environment unknown | A connection has no explicit environment (it stays blocked) | Give it an `env` in the scope |
| Your identity is not confirmed | Record investigations are personal and need a confirmed identity | Confirm your commit email on Profile |
| Several objects match | A name is ambiguous (two environments both hold `shop.orders`, say) | Pick a candidate, or narrow with an environment / connection |
| No metadata yet | Nothing described on a connection | Refresh the schema cards or describe the table |
| Not available from this connector | A capability is unsupported | Use the fallback shown; nothing is assumed |
| Access denied at last check | A read was denied, or a saved flow has steps you can no longer see | It is checked again at the next refresh |
| Partial metadata | An object came from a partial page or hit a row limit | Run the metadata discovery again |
| The database MCP is not reachable | The server was offline at the last check | Reconnect it; cached metadata stays readable |
| Saved flow may be stale / Requires revalidation | Its evidence changed since the version you see | Rebuild the flow |
| Queued, running, cancellation requested | A trace job's state (read from the local job record) | Wait, or cancel |
| Cancelled, timed out, partial, failed, revoked | How a trace job ended | Answered steps are kept; review a new plan |
| Not observed within this query scope | The bounded query found no matching row | Check later (eventual consistency) or inspect the consumer's evidence |
| Mapping unresolved | The key can't be carried to a step | Confirm the column mapping, then review a new plan |
| Observed | A successful bounded observation (at most the plan's row limit) | Compare it with the other steps and their times |
| No cached result yet / computed before the evidence changed | An impact or comparison isn't computed, or is stale | Compute it from cached metadata |
| Comparison blocked | A side is not permitted | Approving it happens only in `/cg:setup` or the config file |

### Explain flow

The flow's steps as a list first: entry point, service, read, decision, write, transaction, publish, consume,
downstream effect and unknown boundary, each with its evidence basis in words and tags for async, path and
*unordered*. Steps are numbered by evidenced stage: steps with no order between them share a number, a separate path
starts again at 1, and steps no evidence orders are listed apart.

A **focus header** sits above the list: *Last observed* (from your newest trace of this flow, or the last cited step
before any trace), *Unresolved* (the largest gap) and *Next* (its next evidence), with **View evidence** and
**Review trace plan**.

**Expand relevant graph** draws only the page's step edges, never the whole catalog. Objects are grouped in boxes by
database connection, by services and code, and by events, so database and service boundaries are visible. Line style
carries meaning as well as colour, and every arrow has a text label with its stage, meaning and basis: solid = cited,
thick = cited and asynchronous (an event), dotted = inferred or not recorded (`?`). *Show as text* is an equivalent
table (stage, from, edge, to, basis, boundary) whose buttons open each step's detail by click or Enter; clicking a box
in the drawing opens its first step. A step's detail lists its citations (file and lines in a selected repository),
column lineage and gaps, and, for each database object it touches, the contextual actions **Known impacts** and
**Compare metadata**. Full names stay reachable when a name is long: they wrap, and the table and detail show them whole.

### Record journey

Trace one record through the flow; see [Record traces](#record-traces).

### Evidence

Passports of the flow's objects (columns, keys, readers and writers, owner unknown, routines, per-source state with
coverage and freshness, and the static-analysis disclaimer), the cited sources, applicability and unresolved claims.
A passport offers **Known impacts** and **Compare metadata** for the object, and **Known impacts** for each column.

## Evidence bases

Every step, edge, column mapping and impact item says what supports it. The result of an investigation, its coverage
and its freshness are separate fields: nothing is ever just "verified".

| Basis | Comes from | Supports | Does not prove |
|---|---|---|---|
| Declared in catalog | The database's own metadata (describe, keys, triggers, routine sources) | That the object, column or constraint is declared on that connection | That code uses it, or anything about the rows |
| Code-supported | A cited file and line range in a selected repository (repo, revision `worktree`, content hash) | That the code as read does this | That this code is what is deployed (*Code model; deployed revision unknown*) or that dynamic SQL elsewhere doesn't |
| Observed in a sample | The QA link map's sampled match counts (*Sample matched 95% (19 of 20, QA, date)*) | That the sampled values matched | A declared constraint, other environments, or the rows not sampled |
| You confirmed (self-reported) | A mapping or pair you recorded | That you said so | Anything you didn't check |
| Inferred | Names, legacy relations, model proposals, unselected repositories | A candidate to check | Anything: it is never promoted to fact by a model's confidence |

A model's explanation may only cite records that exist; a proposal is at most *Inferred*. Conflicting evidence stays
visible side by side. A citation whose file changed since it was recorded is marked *Requires revalidation*.

## Scope and approval

- **Approved** means: in `mcp.database.scope` with an explicit environment, that environment in
  `qa_data.allowed_envs`, no `prod` in the name, allowed by the guard, and not revoked. Unknown environment: blocked.
  The connector's own connection list (inventory) never adds scope.
- **Technical context** (objects, edges, evidence, flows, passports) shows only while every connection it names is
  approved now and the cited repository is selected. **Personal context** (record investigations, your ticket links)
  shows only to its owner, and only while every connection is still approved.
- **Revocation**: removing a connection from `mcp.database.scope`, or its environment from `qa_data.allowed_envs`,
  changes the policy generation (a connection listed in `data/db/revocations.json` is treated the same). Every view
  recomputes on the next request: the connection's steps leave saved flows (*N step(s) of this saved flow are outside
  your access now*), its passports and investigations answer 404 like records that never existed, search drops them,
  the DB map rail no longer lists its tables, and a running job sees the mismatch before its next phase and ends
  *revoked* without publishing. Changing your identity hides your investigations the same way.
- Access freshness: `mcp.database.authorization_max_age_days` (default 7). Older evidence says *access last confirmed
  <date>*, never "verified access".
- Database records never count as personal activity or progress.

## Capability states

Each connection's capabilities are recorded in `data/db/capabilities.json` from the tool listing, observed calls,
the argument mapping and the engine adapter: `connection_inventory`, `schemas`, `object_list`, `describe`,
`catalog_read`, `parameterized_query`, `pagination`, `cancellation`, `timeout`, `row_query` and the object kinds
`tables`, `views`, `foreign_keys`, `procedures`, `triggers`, `dependencies`, `indexes`, `audit_events`. A state is
*supported* (observed), *unsupported* (with the reason), *permission_denied* or *not_verified*. A failed metadata read
sets *not_verified* or *permission_denied*, never an empty list. Views, CLI text and MCP answers list every capability
that is not observed supported, with its reason, and the fallback used. Engine adapters exist for `postgres` and
`mssql`; any other engine fails closed for row and catalog queries.

## Record traces

**Record journey** traces one record you choose. Nothing about it is implicit.

1. **Key form.** Pick the environment (approved connections only; blocked ones are listed with their reason), the
   start object and every key field, tenant included. A value that doesn't fit its column type is refused with the
   reason; nothing is stripped, trimmed or coerced. Typed values stay in the form through errors and refreshes and
   travel only in the request body, never in a URL, a log line or the settings history.
2. **Review trace plan.** The plan lists every target (connection, object, columns, predicate shape with values
   masked, row limit), steps that can't be queried and why (*mapping unresolved* when the mappings don't carry the
   whole key), the limits (20 rows per step, 10 queries, 2 at a time, 30 s per statement, 120 s overall, 256 KiB of
   values; a lower configured value wins), the coverage estimate, which columns are kept as sanitized values and which
   are masked, the retention, the provider limits and the transport disclosure: rows pass through the constrained
   internal Claude transport (the read-only MCP job), so the raw tool output is visible to that model call and its
   provider. Planning queries nothing.
3. **Run trace** runs exactly the reviewed plan: the request must echo the plan's fingerprint under the same access
   policy. A job runs in phases; every call must match the plan pin (tool, connection, statement hash) and the guard
   still checks it first. A step that needs a value observed earlier runs only when the provider bounds server work (a
   statement timeout or cancellation observed supported); otherwise the plan says so and that step is *not queried*.
   Nothing is retried.
4. **Progress.** One poller reads the local job record (polling never queries or enqueues): queued, running,
   cancellation requested, then completed, partial, failed, cancelled, timed out or revoked. **Cancel** stops further
   reads; for a read already sent it says *Stopped waiting; the provider query may still finish* unless the connector
   reports cancellation as supported.
5. **Observations** per step and run: *Observed* (bounded), *Not observed within this query scope* (never a failure,
   a deletion or data loss: it comes with the next checks), *Not queried*, *Access denied* (the object is quarantined
   until a later read succeeds), *Failed*, *Truncated at the row limit*, *Mapping unresolved*. Observation times are
   recorded per connection and never used to order steps; a configured `mcp.database.lag_signals` threshold only adds a
   signal. Changes are shown only between two real observations of the same step.

**Stored:** the plan (no key values; a keyed fingerprint groups runs of the same record), the job record, your
investigation (runs appended, never overwritten; notes with their own revision; a local handoff draft) and sanitized
values of the retained columns. **Not stored:** raw rows (memory only), the start key (a transient file deleted when
the job ends in any state, or after an hour if never run), SQL text, and anything raw in logs, URLs, the search index,
backups or exports. There is no raw-retention option.

## Known impacts

**Known impacts** (from a flow step, a passport or a column; `cli db impact`; MCP `get_db_impact`) lists what recorded
evidence links to an object or column and how a kind of change would reach it: *Rename*, *Type change*, *Nullability
change*, *Default change*, *Meaning change* or *Application-only change* (or any change).

- It follows evidenced edges both ways, one hop from the target (readers, writers, callers, triggers, foreign-key
  dependents and references, column lineage in and out) and onward only along a carried value (event consumers,
  destination tables and their readers) or, for application-only changes, to entry points and published events. A
  rename stops at the first mapping. When a mapping feeds the column, the component whose cited code implements it
  (it consumes the mapping's source and writes the object, under the same citation) is listed as *Implements the
  mapping (writer)*.
- Each item says *Direct evidence* or *Inferred association*, why this kind of change affects it, and a suggested
  check as text. Stale or unavailable sources are flagged. Nothing is run: no test, migration or request.
- The headline counts exactly what is listed (*3 affected: 2 recorded impact links (2 with direct evidence, 0
  inferred) and 1 ticket whose recorded flow touches it*). Nothing recorded reads *No recorded impact links*, with the
  reminder that this is not evidence the change is safe.
- Coverage and limits are always shown: depth 3 and 100 items, *Writers incomplete on qa-main*, unreadable routine
  definitions, unresolved names that may also depend on it, *Static analysis may miss dynamic SQL*, deployed revision
  unknown.
- A name several objects share (two environments both hold `shop.orders`) answers with candidates; `--env` /
  `--connection` (dashboard and MCP: `env`, `connection`) narrow it explicitly.

Results are cached in `data/db/impact/`. A read (dashboard GET, MCP, `cli db impact` without `--refresh`) never
recomputes: it says *No cached result yet*, *Computed before the evidence changed* or *Computed under another access
scope* (items hidden) and offers to compute. Computing is the protected action `db-impact-compute` in the dashboard or
`cli db impact … --refresh`, from cached metadata only. Items are re-projected through your access on every read.

## Metadata comparison

**Compare metadata** (from a passport; `cli db compare`; MCP `get_db_comparison`) compares the recorded metadata of
two permitted connections, or two snapshots of one connection.

- Objects pair only through an explicit mapping: the same object in two snapshots, a cross-environment mapping you
  confirmed, or a pair you confirm for comparing (**Confirm … is the same object on both**, `--confirm`). The same
  name on two connections is only a candidate until you confirm it.
- Both sides show their snapshot or *metadata as of* time. Results fall into six categories: added, removed, changed,
  unchanged, not comparable, unavailable (unknown). An object missing from a partial or denied listing is unavailable,
  never removed. Types are compared engine-aware (`int4` = `integer`; `varchar(50)` ≠ `varchar(100)`).
- A side that is not permitted (a blocked environment, a revoked connection) answers *Comparison blocked* with the
  reason; nothing widens `qa_data.allowed_envs` or relabels an environment.
- Matching schema says nothing about data or deployed code, and row-by-row comparison is not offered.

Snapshots (`data/db/snapshots/<connection>/`) are taken after each metadata refresh (an unchanged one isn't repeated;
at most 20 are kept). Computing is the protected action `db-compare-compute` or `cli db compare … --refresh`.

## Drift and revalidation

A saved flow records the sources and scope it was built from. When a schema card, a cited file, a mapping or the
scope changes, the flow and the investigations that follow it are marked *Requires revalidation* with the reasons;
no version, run or note is lost, and old observations stay historical. An object a complete listing no longer shows
stays as *historical* and leaves current lookups. The background reconciliation pass (at most 200 flows and
investigations per pass, resumable) also deletes cached impact and comparison results that name a connection no
longer approved. `cli db reconcile` runs one pass on demand.

## Migration

The knowledge-base step `db1-canonical-graph` runs once (backup of `data/db/` first, forward only). Without an
approved connection it only records its marker. Otherwise it ingests everything, records explicit aliases only for a
legacy name that resolves to exactly one visible object, saves flows for the tickets with a DB flow, runs one
reconciliation pass and updates the search index. Existing `worklog-dbflow` blocks, ticket files, rendered DB notes,
schema cards and `/cg:trace` keep working. A legacy unqualified name that several connections could mean stays
*needs resolution* with its candidates; legacy relations stay *Inferred*. Re-running it duplicates nothing.

## CLI, MCP and recall

- `cli db table|flow|flows|resolve|plan|run|status|cancel|investigations|investigation|note|handoff|context|impact|compare|reconcile|snapshots|map-confirm|ingest`
  prints the same views as text; no command queries a database or calls a model, except that `plan --run` and `run`
  start the trace runner for a plan you reviewed.
- `/cg:db` tells Claude which `cli db …` view to read; a trace runs only on your explicit request.
- MCP tools (read-only, cached, paged): `get_db_table`, `get_db_flow`, `get_db_investigation`, `get_db_impact`,
  `get_db_comparison`. See [MCP server](mcp.md).
- Search finds database objects, saved flows and your own investigations, each decided again at query time. See
  [Search](search.md).

## Known limits

- **The live database MCP was never contacted while this was built.** Every test and browser check runs against a
  synthetic stand-in. Tool names, argument conventions, dialect details, whether the provider honours a statement
  timeout or a cancellation, and how much routine definition text an account can read are unverified until you run
  it against your own server; the capability states then record what was observed.
- Code relations come from the selected repositories' working trees: the deployed revision is unknown, and dynamic
  SQL, other services, reports and jobs outside them are not seen.
- No event or audit source exists, so whether an asynchronous consumer ran for a record is not observable; a refund
  that isn't there yet is *Not observed within this query scope*, never a failure.
- Engine adapters cover PostgreSQL and SQL Server; MongoDB and other engines fail closed for row and catalog queries.
- Pairing objects for comparison through the scope configuration is not offered; confirm pairs instead.
- The native companion's database view is not built yet; it will consume the same `database_context` projection.
