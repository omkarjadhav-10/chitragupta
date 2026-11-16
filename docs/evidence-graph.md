# Evidence Graph

The Evidence Graph answers one question per requirement: **what supports it, and how strongly?** Each requirement read
from a ticket's Jira card is traced to the plan steps that address it, the code and commits that implement it, the
tests and runs that check it, the pull requests that carry it and the release evidence that ships it. Every link
says where it came from.

It is built from records Chitragupta already keeps: the ticket record, the cached Jira card, Companion task records
(baselines, plans, evidence pins, the verification ledger), branch, PR and CI records, and the activity ledgers.
Nothing new is collected. Building it starts no process, never inspects a repository, and never calls Jira, a Git host
or a model.

## What you can ask

| Command | Answer |
|---|---|
| `/cg:graph ABC-1001` or `cli graph summary ABC-1001` | Every requirement with its three statuses, counts, what is critical or ambiguous, stale evidence, how complete the Jira source was, and one next action |
| `/cg:graph ABC-1001 ac2` or `cli graph trace ABC-1001 --requirement ac2` | One requirement: its wording over time, then plan steps, code changes, commits, tests and runs, pull requests and release evidence, each with the link that reached it; then the ticket's evidence not linked to it |
| `cli graph changes ABC-1001 [--since N]` | What changed since the last change (or since sequence `N`): requirements added, reworded, removed or restored, new code and test evidence, evidence that no longer applies, delivery changes, status changes, gaps opened or closed |
| `cli graph status` | What the index holds and how many of your tickets it is behind on |
| `cli graph rebuild` | Replace the index from cached records (see [Rebuild and recovery](#rebuild-and-recovery)) |

Claude can ask the same three questions through the read-only MCP tool `get_graph(key, view?, requirement?, since?)`
(the same text, see [mcp.md](mcp.md)); the optional native Companion reads them as `graph_summary`, `graph_trace` and
`graph_changes` through its bridge (the same JSON as `--json`). On the dashboard, **Evidence graph** (More menu,
<kbd>g</kbd> <kbd>v</kbd>, `#/graph?ticket=ABC-1001`) lists the requirements with their three statuses and the next
action; `/api/graph` serves the same envelope (see [dashboard.md](dashboard.md)).

A requirement is named by its id (`gr_…`), by the alias a task's baseline gives it (`ac2`), or by its order on the
card (`R3`). When two tasks on the ticket both use `ac1`, add `--workflow w_…`. Long answers end with
`More: pass cursor …`; rerun the same command with `--cursor`. `--json` prints the full envelope, `--privacy` hides
requirement text, commit subjects, PR titles and plan step text (ids, states, counts and short shas remain).

## The evidence model

Every link carries one **basis**:

| Basis | Meaning | Example |
|---|---|---|
| observed | Chitragupta saw it in a record | a commit's files, a PR's commits, a CI run on a PR head |
| declared | a developer stated it | a plan step that addresses `ac2`, an evidence pin mapping a test run to `ac1` |
| inferred | a suggestion with a confidence | a commit that touches exactly the file a plan names |
| unknown | not enough to say | a run whose commit was not captured |

Only declared links count towards implementation, and only the Companion's verification ledger decides
verification. Inferred links are listed and never counted.

## The three statuses

Each requirement has three separate statuses. They are never merged into one score.

- **Plan coverage**: Covered, Partial, Missing or Unverified. Whether the plan the task is judged by (the approved
  plan, else the selected one) addresses the requirement, from the task's evaluation or its step mapping. A
  requirement reworded after the plan was made is *Unverified (requirement changed after this plan)*.
- **Implementation**: Linked, Partial, Unverified association or None. Linked means every implementation step for
  the requirement has a declared link to a commit or change. *Unverified association* means the ticket has changes,
  but none is linked to this requirement.
- **Verification**: Verified within recorded scope, Partial, Failed, Historical only or Unverified. This is the
  Companion's verification ledger, shown on the wording its baseline confirmed. The graph never shows more than the
  ledger. If Jira's wording moved on, the current wording is *Unverified* until a baseline confirms it.

When several tasks cover one requirement, the ticket's line shows the least favourable state among the tasks you can
see, and each task's own line is listed under it.

## What is not proof

- A passing test run counts only where it applies: the run records the commit it ran on, and the graph compares it
  with the pull request heads and branch tips it knows. A newer head is shown as a gap; it is never assumed tested.
- A pinned copy of a run or commit that is no longer kept ("pinned copy; source no longer retained") keeps the weight
  the ledger gives it and is never shown as new.
- Self-reported evidence (a manual acceptance or code inspection you recorded) is labelled self-reported.
- Requirements come from the cached card. A card cut at 3,000 characters or a failed read is a named gap, and a
  partial read never removes a requirement.

## Access and privacy

Every read checks the ticket against your current profile first; a ticket outside it reads as "Not available". Tasks
in repositories your profile no longer includes are left out entirely: their links, aliases, statuses, counts and
next action. After a profile change the graph answers "rebuilding" until the next background run, and changes from
before the change are "unavailable". Text from Jira, commits and PRs is shown as plain text: secrets are redacted and
links, paths and control characters are removed.

## Storage

- `<home>/.worklog/cache/evidence-graph.sqlite`: the index. It is derived; deleting it loses nothing.
- `<home>/data/graph/<KEY>.json`: per ticket, the requirement wording over time and a journal of what changed (at most
  200 entries and 4 MiB).

The background run updates both after the Companion step: dirty tickets first, then any ticket whose inputs changed,
at most 25 tickets or about 2 seconds per run. A ticket whose inputs did not change writes nothing.

## Rebuild and recovery

`cli graph rebuild` replaces the index from cached records. It waits for a running background tick to finish (the
same lock other commands use) and prints `skipped: a background run is in progress` if it cannot get it, changing
nothing. A history file is never rewritten: an interrupted update is completed at its own sequence number, and a file
that cannot be read is kept aside and restarted.

## Limits

- Jira identity is the key; the Jira instance is added when a task or a single configured instance supplies it.
- A repository is identified by its activity ledger, a task binding or its `repo.json`; otherwise by its folder name,
  marked as weak.
- Only links a developer declared reach a requirement. Commits that mention the ticket but were not mapped stay
  ticket-level evidence.
