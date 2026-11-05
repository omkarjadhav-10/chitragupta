# Release tracking

Release tracking answers five questions per ticket, with the evidence behind each answer:

1. Which Jira release is the ticket scheduled for, and on what date?
2. Which release branch should development start from, in each repository the ticket touches?
3. Where should each PR go, and what is the next step?
4. Are the ticket's changes in the release branch, in QA and, when UAT is required, in UAT?
5. Which of my tickets are scheduled this month, and what is still missing?

It builds on [branch tracking](branch-tracking.md): the same branches, PRs and delivery check, pinned to the branch that ships the ticket's Jira release. There are no model calls and nothing is ever changed in a repository, Git host or Jira. Branch names and commands it shows are suggestions for you to review and run yourself.

It is off until you turn it on: `"releases": {"enabled": true}` in `<home>/.worklog/config.json`, or **Track releases** on the dashboard's Settings page (group *Releases & UAT*).

## Separate facts

These are kept apart and never derived from one another:

| Fact | Comes from |
|---|---|
| Release identity | The ticket's Jira fix version: project + version id (the name only while no id is known). |
| Planned date | The version's release date in Jira, else a configured Jira date field (`releases.jira.planned_date_field`). |
| Date in the release's name | Shown ("name says December 14th") and used to rank suggestions. Never a planned date. A fiscal label such as `FY99` is never turned into a calendar year. |
| Release branch | Per repository, resolved as below. A release named for one date can ship from a branch named for another. |

## Which release counts

- **One fix version:** that one.
- **Several fix versions:** none is chosen. The ticket shows *ambiguous* with the candidates, no development base is recommended, and a reminder asks you to choose: `/cg:branch release-pick ABC-1001 10001` (version id or exact name; `-` clears).
- **None, or Jira not read yet:** *unresolved* or *unknown*, never "no release" when Jira simply hasn't been read.
- **The ticket moves to another release:** the previous assignment is kept under `history` with the branch each repository used. Development branches started from the previous release branch and PRs still targeting it are flagged (`old-release-base`, `old-release-pr`). Earlier PRs stay on record; nothing is retargeted for you.

## Which branch ships it, per repository

Resolved separately for every repository, in this order:

1. **Your choice for one ticket:** `/cg:branch release-branch ABC-1001 service-ui <branch>`.
2. **Your mapping:** `releases.repos.<repo>.branches`, keyed by the Jira version id or its exact name. A version renamed in Jira keeps its id, and the versions catalog remembers its old names, so a mapping by the old name still matches. Set it with `/cg:branch release-map service-api "FY99 December 14th Release" release/ABC-9000/7th-dec-release`.
3. **An unambiguous structured link:** the Jira version's description names an existing release branch, or names a ticket key that exactly one release branch carries (a release coordination ticket such as `ABC-9000` in `release/ABC-9000/7th-dec-release`).
4. **Suggestions to confirm:** release branches this ticket or other tickets of the same release already used, and branch names whose date or month matches. A suggestion is never treated as certain.

A mapping alone doesn't prove the branch exists. Each result records `state`, `source`, `confidence`, whether it exists (remote or only local) and `verified_at` (the time the refs were last fetched, by Chitragupta or anyone else):

| State | Meaning | Used as the release that counts? |
|---|---|---|
| `confirmed` | Your choice or mapping, and the branch exists | yes |
| `linked` | A structured link, and the branch exists | yes |
| `mapped-missing` | Mapped, but no such branch in the fetched refs | no |
| `mapped-unverified` | Mapped, but this machine has no refs for the repository | no |
| `suggested` | Candidates only; you confirm with `release-map` | no |
| `unresolved` | Nothing matches; the missing decision is named | no |

Only `confirmed` and `linked` branches are pinned as the release target of the [delivery check](branch-tracking.md#the-delivery-check). The PreToolUse guard also warns when a new ticket branch is cut from, or a PR targets, another release branch than the pinned one.

### Which repositories a ticket touches

A repository counts when it has branches or PRs for the ticket. Other release repositories (those under `releases.repos`, or whose strategy starts from the release role) are shown as *candidates* ("If ABC-1001 changes service-ui: start from …"), never assumed. `/cg:branch release-repos ABC-1001 service-api` sets the list by hand (`-` clears).

## The workflow it follows

The steps come from the repository's branching strategy. For the release-then-QA-integration workflow use the built-in strategy `release-then-qa-sync`:

1. Create the development branch from the release branch, develop, PR it into the release branch, merge.
2. Create a new QA integration branch **from `qa`**, merge the **release branch** into it, resolve conflicts there, PR it into `qa`, merge.
3. UAT, when required, by the path you configure (below).

`release-then-qa` (the QA branch brings in your release-based branch) and the other strategies keep working; the next step is worded from each strategy's steps.

## Stages per repository

For each affected repository, from the delivery check's evidence (ancestry, same patch, squash PRs, reverts):

| Stage state | Meaning |
|---|---|
| `present` | Every commit of the ticket is in the destination. |
| `partial` | Some are (for example later commits not yet promoted). The detail says how many, and when the stage was complete before. |
| `missing` | None are, and the refs are fresh. |
| `unknown` | Can't be confirmed: refs stale or the last fetch failed, the release target isn't the resolved branch, or the source wasn't read. Never reported as missing. |
| `not_started` | No commits yet. |
| `not_applicable` | UAT not required. |

A merged PR proves that merge, not that later commits arrived or that anything was deployed. Commits pushed after a PR merged, reverted commits and conflict-resolved cherry-picks keep their own status.

**PRs** are listed per destination (release, QA, UAT) with state, dates and notes such as *closed unmerged; superseded by #12* or *followed by #14*. A shared QA integration PR is linked to every ticket whose commits its head carries (same commit or same patch), marked *shared, carries 1/1*; it is never credited as ownership of those tickets and never invented for a ticket it doesn't carry.

**Flags:** a development branch cut from another release branch (`wrong-base`, only for recorded starts: shared history alone never proves where a branch started), a release PR into another release branch (`wrong-target`), and the previous-release variants above.

**Freshness** is kept apart from status: refs fetched time, a failed fetch, a failed PR source, and whether open PRs are visible at all (with PR data from merge commits only they aren't, so the next step says "skip this if it's already open").

## UAT

**Requirement** is `required`, `not_required` or `unknown`, from (in order) your choice (`/cg:branch uat ABC-1001 required`), a configured Jira label (`releases.jira.uat_labels`), or the configured Jira field (`releases.jira.uat_field`, values mapped by `releases.jira.uat_values`). An empty or absent field is `unknown`, never "not required".

**Destination and path** are set per repository in `releases.repos.<repo>.uat`, or taken from a strategy step into the `uat` role:

```json
"uat": {"branch": "uat", "promotion": {"kind": "pr", "from": "uat", "brings": "cherry-pick"}}
```

`kind` is `pr` (a promotion branch from `from`, then a PR into the UAT branch), `sync` (the UAT branch receives `sync_from` at its next sync) or `unspecified` (your team's process; tracking still checks the branch). Without any of these, the UAT destination is reported as not configured.

When UAT is required and a destination branch is set, it becomes one more column of the delivery check. Four facts stay separate: required; changes in the UAT branch; **deployed** to UAT; **accepted**. Deployment and acceptance count only with explicit evidence: Jira statuses you list in `releases.jira.uat_deployed_statuses` / `uat_accepted_statuses`. Otherwise they stay *unverified*.

## Reminders

Recomputed from the evidence on every tick; showing one never clears it. A reminder goes away only when the evidence changes:

- UAT required but the changes aren't in the UAT branch (again after later commits) — with the next step.
- UAT branch has the changes but deployment or acceptance is unverified.
- UAT requirement unknown — asks you to settle it.
- Several fix versions, an unconfirmed release branch, a wrong base or target.

They appear on the ticket page, the Releases page, `get_alerts`, and in the session brief. Nothing is added to your prompts; the session brief comes once per session start (and after a resume, clear or compaction).

## The planning brief

When a session starts on a ticket's branch, the session context (written by the background tick, read by the SessionStart hook with its age) includes:

```text
Release plan for ABC-1001 (computed 2026-12-02 10:15; refresh with /cg:check ABC-1001):
Release: FY99 December 14th Release (planned 2026-12-14, Jira version release date); name says December 14th (shown only, never the planned date)
Repository service-api:
  Development base: release/ABC-9000/7th-dec-release (confirmed; your mapping …; refs fetched 2026-12-02 09:40)
  Development PR target: release/ABC-9000/7th-dec-release
  Observed: feature/ABC-1001-x ← release/ABC-9000/7th-dec-release [ok]; PR #51 feature/ABC-1001-x→release/ABC-9000/7th-dec-release merged
  Release: present (all 2 commits in release/ABC-9000/7th-dec-release) · QA: missing (none of 2 commits in qa)
  UAT: required (Jira customfield_10100 = Yes) → uat (PR from a branch cut from uat); missing (none of 2 commits in uat)
  Next: QA next: create a QA integration branch from qa (`git switch -c feature/ABC-1001-x-qa origin/qa`), merge release/ABC-9000/7th-dec-release into it (`git merge origin/release/ABC-9000/7th-dec-release`), resolve conflicts there, push, then PR feature/ABC-1001-x-qa → qa.
Jira read 2026-12-02 09:00.
```

Before development it names the base and the PR target; during development it flags mismatches; after the release merge it explains the QA integration step; after QA it lists what UAT still needs. When the base isn't certain it says so and gives no command. The same brief starts `/cg:branches <KEY>` and `get_branch_status`.

## The monthly overview

`/cg:branches` with no key (or `/cg:branches 2026-12`), `list_tickets(release="2026-12")` in recall, and the dashboard's **Releases** page show your tickets grouped by the planned release date of the month (default: this month, by your `day_starts_at` day boundary), then **Overdue** (planned before today, not ready; listed when you look at the current month or a later one, so late work stays in sight while planning ahead) and **Release date unresolved**. Each ticket keeps its per-repository detail; a ticket is *ready* only when every affected repository has every required stage present on fresh refs, its release branches are confirmed, and UAT is settled (required UAT also needs deployment and acceptance evidence).

"My tickets" (`releases.scope`): assigned to you in Jira, a development branch named for the ticket with your commits, or Claude Code work you did on it. Authoring a shared integration PR never makes a ticket yours. Tickets in `branching.ignore_tickets`, placeholders, keys Jira says don't exist and tickets in `releases.jira.exclude_statuses` (cancelled and the like) are left out.

## Jira reads

All through the read-only Jira MCP harness, in the background tick, within `mcp.daily_call_budget` and never from the evening reserve:

- **Ticket cards** (existing job, same single call) also read `fixVersions`, `assignee` and the configured fields.
- **Scope search** (`releases.jira.scope_jql`, default your tickets with a fix version updated in 120 days): one `jira_search` every `scope_refresh_hours` (12). Kept as `data/jira/_releases/scope.json`, release facts only.
- **Versions catalog:** one `jira_get_project_versions` per project at most every `versions_refresh_hours` (24), for projects whose tickets have fix versions; `data/jira/_versions/<PROJECT>.json`. It supplies ids and release dates when the MCP returns version names only.

At most two release jobs per tick. A failed job is recorded with its error and retried after the usual back-off; the release then shows its Jira read time and error rather than a guess.

## Configuration

`releases` in `config.json` (all optional; see [configuration.md](configuration.md#releases)):

```json
"releases": {
  "enabled": true,
  "jira": {"uat_field": "customfield_10100", "uat_values": {"required": ["yes"], "not_required": ["no"]},
           "uat_accepted_statuses": ["UAT Done"]},
  "repos": {
    "service-api": {"branches": {"10001": "release/ABC-9000/7th-dec-release"},
                    "uat": {"branch": "uat", "promotion": {"kind": "pr", "from": "uat", "brings": "cherry-pick"}}},
    "service-ui": {"branches": {"FY99 December 14th Release": "release/ABC-9000/7th-dec-release"}}
  }
}
```

with `"branching": {"repos": {"service-api": {"strategy": "release-then-qa-sync"}, …}}`.

## Inbox lines

`/cg:branch` writes one of these to `inbox.md`; the tick applies it (`/cg:check` at once). Mappings go to the config through the config write path (backup, history); per-ticket choices go to the ticket's `release_override`.

```text
release-map: <repo> "<release id or name>" <branch>
release-unmap: <repo> "<release id or name>"
release-pick: <TICKET> "<release id or name>"|-
release-branch: <TICKET> <repo> <branch>|-
release-repos: <TICKET> <repo> [<repo> …]|-
uat: <TICKET> required|not_required|unknown
```

## Files

| File | Holds |
|---|---|
| `data/tickets/<KEY>.json` → `release` | Releases, selection, history, per-repo branch resolution, dev branches, PRs per destination, shared PRs, stages, flags, next step, freshness; UAT facts, reminders, readiness |
| `data/tickets/<KEY>.json` → `release_override` | Your per-ticket choices |
| `data/jira/_releases/scope.json` | The scope search: release facts per issue |
| `data/jira/_versions/<PROJECT>.json` | Versions catalog with previous names |

The record is additive: existing data needs no migration, and switching tracking off removes only the `release` record (your overrides stay).

## Limits

- Where a branch was originally created can't be proven from ancestry; only a recorded start (command or reflog) is judged.
- Deployment and acceptance are known only through the Jira statuses you list.
- With PR data from merge commits only, open, draft and closed PRs are invisible; QA and release steps are worded accordingly.
- Suggestions from branch-name dates are hints; a holiday-shifted release always needs your mapping.
