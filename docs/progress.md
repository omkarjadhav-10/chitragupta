# Developer profile and personal progress

Chitragupta keeps one developer profile: who you are, which identities are yours on each provider, and which repositories and Jira projects you work in. Every surface that says "yours" asks the same ownership policy (`chitragupta/profile.py`, `Policy`) and reads tickets through one personal projection (below): the background tick, the worklog, ticket pages, release reminders, the dashboard, search, the `/cg:` commands, the MCP tools, sharing and exports. **Today** opens with it (the next useful action, the day's numbers and your active tickets), **My progress** shows one day in full, and **History** holds weekly wins, the calendar and personal records.

This is a personal companion. It has no team view, no ranking and no score. The numbers describe observed, committed work in your configured scope. They do not measure quality or productivity.

## Set up your profile

Open the dashboard's **Profile** page (`g i`), or run `/cg:setup`, which now has a profile step. Everything here changes only through the settings write path (`configstore`: lock, backup, history, undo).

| What | Where it is kept | Used for |
|---|---|---|
| Display name | `profile.display_name` | Greetings and summaries. Never used to match work. |
| Primary work email | `profile.email` | Shown to you only. The Jira account lookup uses it. It isn't proof of authorship by itself. |
| Confirmed commit emails | `git_author_emails` | Commits whose **author** email is exactly one of these are yours. |
| Provider accounts | `profile.accounts` | Your Jira account ids and your Git host accounts, each scoped to its instance (`github.com`, `dev.azure.com/<org>`, a GitLab host). Several accounts of yours may share an instance (a work and an older account); each is identified by provider, instance and its stable id (else its login). |
| Repositories in scope | `profile.repos` | A subset of `watched_repos`; empty means every watched repo. |
| Jira projects in scope | `profile.jira_projects` | Empty means `ticket_prefixes`. |
| Focus, role, technologies | `profile.focus`, `profile.role`, `profile.technologies` | Planning context for Claude. Never ownership evidence. |
| Time zone | `profile.timezone` | Day boundaries and reminders. |
| Working week | `jira_worklog.workdays` and `data/calendar.json` | Days off: no suggestions, never a streak. |
| History range | `profile.tracking_start`, `profile.backfill_days` | How far back commits and PRs are imported. |
| Collaborative tickets | `profile.include_tickets` | Tickets assigned to someone else that you count as yours, each with your reason. |

An example, all synthetic:

```json
"git_author_emails": ["developer@acme.test", "12345+developer@users.noreply.github.com"],
"profile": {
  "display_name": "Dev Example",
  "email": "developer@acme.test",
  "accounts": [
    {"provider": "github", "instance": "github.com", "id": "12345", "login": "dev-example"},
    {"provider": "jira", "instance": "acme.atlassian.net", "id": "712020:00000000-0000-4000-8000-000000001001"}
  ],
  "repos": ["C:/work/service-api"],
  "focus": "Payments API migration",
  "timezone": "+05:30",
  "include_tickets": {"ABC-1003": "pairing on the API contract"}
}
```

### Suggestions and confirmation

Chitragupta suggests identities, and shows where each suggestion came from:

- `git config --get user.email` in each watched repository (local, refreshed daily by the tick)
- the account each Git host CLI is signed in as: `gh api user`, `glab api user`, `az account show`. These run only when you press **Find my Git host accounts** or run `profile discover --hosts`.
- the Jira account for your primary email: one read-only `jira_get_user_profile` job, only when you press **Look up my Jira account** or run `profile discover --jira`

A suggestion is never active. It counts only after you confirm it (the Confirm button, or `profile confirm <id>`). A repository's configured email may belong to anyone who set it up, and a host CLI may be signed in to a different account than yours. Chitragupta never matches by display name, email domain or look-alike address. It doesn't strip dots or `+tags` from addresses, and it doesn't guess. Bot and service identities (`profile.bot_patterns`, for example `dependabot[bot]` or `noreply@github.com`) can't be confirmed at all.

You can also type an account in by hand when discovery isn't available. The profile never holds a token or password: a value that looks like a credential is refused.

### Status per provider: identity and connection

Two separate answers per provider, the same on Profile, Today / My progress and Health:

- **Identity**: *confirmed* (ok), *not set up* (nothing confirmed, so that provider's personal data isn't counted), *partly set up* (for example the Jira account is confirmed but the Jira MCP is off, or a selected repository isn't watched), or a problem (for example a time zone this machine has no data for).
- **Connection**: how the last read went, from the collection itself: *last read worked*, *partly read* (a capped page), *not read recently* (more than 3 hours), *last read failed* (with the error), *not read yet*, or *switched off*. A confirmed identity is never shown as "connected" on its own.

One provider's gap never blocks the others. It never falls back to "everyone's" data either. A failing source of yours is also listed on Health, and Today's status line names it with the step that fixes it.

### Changing the profile

Before a change is saved, the page previews its effect: commits, PRs and current tickets that would leave or join your view. Removing one of two accounts on a host counts only the PRs attributed to that account (each stored PR records which of your accounts it came from). Adding an account adds it next to the others; nothing is replaced unless it is the very same account. After the change, the next tick reconciles everything (`profile.reconcile`):

1. It backs up every file it is about to change to `.worklog/backups/chitragupta-profile-<time>/`.
2. In ticket records, and in day records for today and days whose evening worklog wasn't sent yet, commits no longer authored by a confirmed email move to a `commits_excluded` list. They are kept and labelled with the reason, not deleted. Commits that are yours again move back. An older record whose author can't be verified from git (no recorded author, and the repository can't answer) is quarantined the same way, labelled "couldn't be verified", and moves back once git confirms a confirmed email wrote it. A new rules version (`profile.POLICY_V`) runs this once, backed up, on existing installs.
3. When you add an email, its commits are imported again (within the history range, at most 30 days for day records).
4. The activity ledgers rebuild from scratch. Until then the progress page shows **updating for your changed profile**, never the old numbers.

Reconciling twice changes nothing the second time. An evening worklog that was already sent is history and stays as sent.

Unsaved edits on the Profile page are drafts the page keeps per group (and for the browser tab in `sessionStorage`): showing emails and ids, a live refresh, a discovery, saving another group, or leaving and returning to the page doesn't reset them. Only saving a group or *Discard* clears that group's drafts; a refused save keeps them, with the reason.

## What counts as yours

| Kind | Yours when | Not yours |
|---|---|---|
| Jira ticket | Jira assigns it to your confirmed account, or you included it with a reason | Unassigned tickets, and tickets assigned to someone else, even on your board. A ticket reassigned away from you stops being current. Your own earlier activity on it is kept, with the ownership history (`you` / `other` / `unassigned` and when it changed; the other person is never named). |
| Commit | Its **author** email is a confirmed email | A commit you only rebased, merged or cherry-picked for someone else (you are the committer, not the author) |
| Co-authored commit | Your confirmed email is an exact `Co-authored-by:` trailer | Counted apart as co-authored. Its diff is never split per person. |
| PR | Its author is your confirmed account on that host instance | PRs you reviewed or merged. A shared integration PR someone else raised never raises your "PRs raised". |

The provider narrows the request first: `git log --fixed-strings --author` (a confirmed address is literal text, so `developer+work@acme.test` matches, in any letter case), `gh pr list --author <login>` once per confirmed account, `glab mr list --author`, `az repos pr list --creator`, and the Jira scope search's `assignee in (<your confirmed ids>) AND project in (<your projects>)`. Every result is then rechecked exactly before it is stored: a look-alike address, another person's PR or a row of another project or assignee is dropped and counted, never kept. Nothing collects everyone's PRs or tickets and hides them in the page, and an unconfirmed identity never widens the search.

Shared Git history and PR or ticket documents necessarily mention other people. Chitragupta keeps only what establishes your own contribution: no committer emails, no other person's account, and of a ticket's assignee only whether it was you.

Release and UAT reminders, delivery alerts in `get_alerts`, and next steps follow the same rule: a ticket assigned to someone else isn't yours to act on. `get_alerts` and the dashboard's *Needs you* strip say how many tickets they left out; the Tickets page shows each ticket's reason and has an *Only mine now* filter, and `/cg:doctor` reports the profile per provider.

A ticket assigned to you in a Jira project outside `profile.jira_projects` is *outside* your scope (not current) unless you include it.

### The personal projection

Ticket records can hold more than your own work: a branch tracked by name, an older commit record, a Jira parent. Every reader of a ticket goes through one projection (`profile.projection`, served by `dashboard.api.scope_index`):

| Basis | When | Shown |
|---|---|---|
| current | assigned to one of your confirmed Jira accounts (in your projects), or included by you | everywhere |
| history | not yours now, but your own work is recorded on it: time from your sessions; findings, queries, requests, saved Bruno requests, artifacts, edited files, tables, APIs or data flow from your sessions or notes; branch names seen in your sessions; commits whose recorded author is a confirmed email, or your own commits on a ticket branch; a day you worked on it (unless every commit recorded on it is someone else's or unverifiable); or an earlier assignment to you | everywhere, labelled; only your own activity counts |
| context | the Jira parent (an Epic) of one of your visible tickets, with none of your work on it | listed as context, never counted |
| outside | none of the above | nowhere: not listed, not returned by key (the ticket page, `get_ticket`, `get_branch_status`, `/cg:ticket`, `/cg:branches` say only that it is outside and why), not searchable, not exported or shared, not in alerts |

The projection is computed on read, so a profile change (an account, an included ticket, a project) takes effect on the next request without a rebuild. Redaction is a separate safeguard; it isn't what decides ownership.

**Supporting context.** Release tracking reads a little shared metadata to verify your own ticket's promotion: the release / QA / UAT branches, and pull requests on the ticket's tracked branches, including a shared QA integration PR someone else raised. Branch tracking lists a repository's PRs in one read per repository (the cheaper call) but keeps only the PRs whose source is a tracked branch of one of your tickets, and it runs only in your profile's repositories. Such a PR is labelled *shared* on the ticket and never counts as a PR you raised: your PR counts come only from the author-filtered activity ledgers.

## Metrics of a day

Days are work days in the profile time zone, from `day_starts_at` (default 04:00, so a commit at 01:30 belongs to the day before). A daylight-saving day is 23 or 25 hours long.

| Metric | Definition |
|---|---|
| Authored commits | Unique non-merge commits you authored, by author time. A commit seen on several refs, or through two clones or worktrees of the same repository, counts once (canonical repository identity + commit id). A rebased or cherry-picked copy with the same patch-id and author time counts once. |
| Committed changes | `+added / -deleted` lines of those commits that have no co-authors, from per-commit `git log --numstat --root`. Binary files count 0 lines. Paths matching `progress.exclude_globs` (lockfiles, vendored, build output, generated files) are shown separately, never silently dropped. |
| Shared commits | Commits you authored that list co-authors: their whole diff, labelled shared, never split per person |
| Co-authored only | Commits where you are only a `Co-authored-by`. Kept out of the headline numbers. |
| PRs raised | Your distinct PRs by creation time. Drafts are counted and labelled. Reopening a PR or marking a draft ready is the same PR. |
| PRs merged | Your PRs merged that day, whoever pressed merge. Separate from raised. |
| Tickets completed | Distinct tickets moved into `progress.done_statuses` that day while they were yours (the evening Jira check's status history). A ticket completed, reopened and completed again the same day counts once. |

The headline is labelled **Committed changes: +420 / -180 lines**. It measures change volume, tests and documentation included. It isn't typing, it isn't code quality and it isn't productivity. Repeated edits to the same lines count each time they are committed. Uncommitted work is never part of it.

### Zero is not unknown

Each metric carries a status and a reason:

- **known** or **zero**: every source covering that day is configured, current and was read
- **partial**: for example, one repository's scan failed, a host returned its maximum page, or a host's MCP server only lists PRs per branch
- **stale**: the source was last read more than 3 hours ago, or before that day
- **unknown**: for example, before the imported history, no confirmed identity, a source that can't tell authors apart, or no Jira status history for that day

A commit or PR whose time is more than an hour in the future is excluded and listed as unresolved. It is never moved into today.

The status travels with the number: a partial or stale count is said as "at least N" in milestones and weekly wins, is never compared with your records, and never becomes a record (below). When no source is set up the page says so and offers setup instead of "nothing to do".

### History rewrites, merges and squashes

- Merge commits never count. Their diff repeats changes already made in the commits they bring in, often by other people.
- A commit that is no longer on any ref after a successful full scan (amended, rewritten, or on a deleted branch) leaves the counts, and its replacement counts instead. This means work on a branch deleted after a squash merge counts only through the squash commit. Copies are recognised by patch-id, never by commit message. Commits whose patch-ids differ stay distinct: an amend that changed the content, a conflict-resolving rebase, or a squash of several commits.
- The squash or rebase-merge commit of one of your merged PRs, whose head commit is already counted, is the same work and isn't counted again. When its head commits are gone, the squash commit is the one that counts.
- PR diff totals are never added to commit line totals.

## Personal records

Records are computed over the imported history: most authored commits, PRs raised, PRs merged and tickets completed in a day (the last only with status history), and the **most active day**. That is the largest number of distinct activity events in one day: authored commits + PRs raised + PRs merged + tickets completed, shown with its breakdown. It is a count, not a weighted score.

Each record shows its date, value, breakdown and coverage: "since tracking began 2026-09-01" when the history really starts there, else "within imported history since …". A record is never called all-time. On a tie the earliest day wins, and the record says how many other days tied. Records are recalculated whenever the identity, scope, time zone or history changes.

Only a completely collected day (known or zero: every source configured, current and read) can hold a record. A partly collected or stale day that shows at least as much is listed with the record as a qualification ("Mon 2 Nov shows at least 5 but was partly collected"), never as the best day.

There is no "most worked day". Tracked time comes from Claude Code activity and is approximate, so hours aren't inferred from first and last commit times.

## Milestones, weekly wins, reflection, next steps

- **Milestone messages** (`profile.celebrations`) are short factual sentences, for example "You opened 3 PRs today and merged 2. Your highest PR-opening day within imported history since 2026-09-01 was 4." The same evidence always gives the same text, so a refresh never repeats or inflates one. Animations follow `profile.animations` and the system's reduced-motion setting.
- **Weekly wins** summarise the week deterministically, with no model call: merged PRs with links, PRs raised, completed tickets, commits and committed changes, records set, the day summaries already written at day close, and your notes. The Copy text passes the public redaction and contains no email or account id.
- **Daily reflection** (`profile.reflection`): an optional note for what moved forward that commits don't show, such as investigation, design, reviews or helping a teammate. It is stored locally in `data/reflections/<YYYY-MM>.json`, never in the pushed `daily/` files, and always labelled self-reported.
- **Next steps** (at most three) come first: setting up an unconfigured source, then recovering a failing one (named, with the step), then your own work: your open PRs with their recorded review state, and the release / UAT reminders and delivery alerts of tickets that are yours now. When a PR source is disconnected or wasn't read in the last 3 hours, a recorded approval becomes "was approved when last read …: check its current state", never "merge it". With nothing to do, the page says what it checked ("checked 12:00"), or that not every source is connected. A blocker is never inferred from inactivity. A day off (your workdays, leave and holidays) suggests nothing. There are no streaks.

## Where to see it

- Dashboard: **Today** (`g t`) opens with the source line, the next action, the day's numbers (each opens what it counts, the counted events, what was left out and the sources) and your active tickets; **My progress** (`g p`) shows any day in full with milestones and the reflection; **History** (`g y`) holds weekly wins, the calendar and records; **Profile** (`g i`) has three groups: Identity (with the suggestions beside your confirmed identities), Scope, and Preferences and tracking. See [dashboard.md](dashboard.md).
- `/cg:progress [today|week|month|records|wins]` and `/cg:progress reflect "…"`.
- CLI: `progress …`, `profile show|candidates|discover|confirm|check`. Emails and ids are masked unless `--reveal`.
- MCP: `get_progress(period, date?, include_ids?)`. `list_tickets(mine=true)` lists only your current tickets. `get_alerts` is scoped to your tickets. See [mcp.md](mcp.md).
- Session start adds one compact line (at most 600 characters): your name, focus, scope, the ownership rule and what isn't confirmed. It never contains an email, an id or your activity history.

## Privacy

Identity, ledgers and reflections stay in the data home (`data/activity/`, `data/reflections/`, `.worklog/cache/profile-*`). None of them are pushed. Emails and account ids are masked on the dashboard and in CLI output unless you reveal them, are left out of every progress answer, and are left out of exports and copied summaries. MCP shows them only with `include_ids=true`. See [privacy.md](privacy.md).

## Known limits

- Tickets completed covers only the tickets the evening Jira check reads (those you worked on that day). Other days show unknown.
- GitLab has no date filter on its author list. A full page of 100 merge requests is marked partial.
- A host whose PRs come only from its MCP server lists PRs per tracked branch, so it is partial. PRs found only through merge commits carry no author, so they are unavailable.
- On Windows, Python has no time zone database unless the `tzdata` package is installed. Use this machine's zone (leave it empty) or a UTC offset such as `+05:30`. A zone name this machine can't resolve is refused with that explanation.
- Progress, the monthly release view, overdue releases and reminders use the profile's zone and `day_starts_at`. The rest of the worklog (the daily note, the evening Jira worklog) still uses this machine's time zone; ticket completions read the evening files around the day, so a completion near midnight is counted on the right profile day, once.
- Two clones count once only when the repository's identity comes from its remote (GitHub, GitLab, Azure DevOps). Two folders without a known remote are different repositories as far as Chitragupta can tell.
- Activity ledgers are kept per repository location (`data/activity/<folder>-<hash>.json`). An older ledger named after the folder alone is read only when it records the same folder, and is carried over by the next refresh; it isn't deleted. Branch tracking's own files (`data/git/<folder>/`) are still keyed by folder name, so two watched repositories with the same folder name share them: give them different folder names.
- With many repositories and a long history, Today shows its page first and the action band a moment later (about a second for 12 repositories with 365 days each, on a synthetic home).
