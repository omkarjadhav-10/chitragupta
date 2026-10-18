# Branch tracking

For every ticket, Chitragupta keeps one picture of its git work:

- which branches belong to the ticket, and which parent each one was cut from
- which commits are on them
- which PRs you raised, into which branch, and whether they merged
- whether every commit has reached every branch your project's strategy requires, and if not, which commits are missing and why

It warns you early: on the ticket page, at the start of a session on that ticket, in the 8 PM Slack message and, while you work, when Claude Code is about to create a branch or a PR that doesn't fit.

Everything here comes from git and your Git host's own CLI. There are no model calls. Your work repos are only ever read.

## Strategies

A strategy says where branches start, where their PRs go, and which branches the work must reach. Strategies are config, not code. Three come built in:

| Strategy | Steps | Must reach |
|---|---|---|
| `release-then-qa` | 1. Branch from `release/*`, PR into that release.<br>2. Branch from `qa`, bring in the release-based branch (merge or cherry-pick), PR into `qa`. | the release branch **and** `qa` |
| `qa-only` | Branch from `qa`, PR into `qa`. | `qa` |
| `qa-cherry-pick` | Branch from `qa`, PR into `qa`. Then branch from `uat` and from `staging`, cherry-pick the commits onto each, PR into each. | `qa`, `uat` **and** `staging` |

`release`, `qa`, `uat`, `staging` and `main` are **roles**. Each repo maps its own branch names to the roles, so a project whose qa branch is called `develop-qa` maps `"qa": "develop-qa"`. A pattern role such as `release/*` stands for many branches. The release that counts for a ticket is the target of its PR into a release, else the parent of the ticket branch cut from one. With neither, the check looks at every release branch changed in the last 120 days, plus `main`, and says so.

The config lives under `"branching"` in `<home>/.worklog/config.json`:

```json
"branching": {
  "strategies": {
    "release-then-qa": { "targets": ["release", "qa"], "steps": [
      { "from": "release", "pr_into": "release" },
      { "from": "qa", "brings": "merge", "of_step": 1, "pr_into": "qa" } ] },
    "qa-only": { "targets": ["qa"], "steps": [ { "from": "qa", "pr_into": "qa" } ] }
  },
  "repos": {
    "shop-api": {
      "strategy": "release-then-qa",
      "remote": "origin",
      "bases": { "release": ["release/*", "*-hotfix"], "qa": "qa", "uat": "uat", "staging": "staging" },
      "rules": [ { "jira_parent": "OPS-*", "strategy": "qa-only" } ]
    }
  },
  "guard_mode": "warn",
  "stale_pr_days": 3,
  "ignore_tickets": []
}
```

| Key | Meaning |
|---|---|
| `strategies` | Your own strategies, merged over the built-in three. A new project or way of working is a config edit. |
| `repos.<repo>` | One entry per watched repo, keyed by the repo folder's name. A watched repo without an entry is still tracked (branches, parents, PRs), but gets no delivery check. |
| `repos.<repo>.strategy` | The repo's default strategy. |
| `repos.<repo>.bases` | Role → branch name, pattern, or a list of them. Defaults: `release/*`, `qa`, `uat`, `staging`, and `main` or `master`. A branch that matches a base is never treated as a ticket branch, even when its name carries a ticket key. |
| `repos.<repo>.rules` | Per-ticket choices, checked in order. `jira_parent` matches the ticket's Jira parent (from the Jira card job, so no extra call) against a pattern. |
| `repos.<repo>.suffixes` | Branch-name endings that say where a branch is meant to go. Defaults: `-qa`, `-uat`, `-on-uat`, `-staging`, `-stg`, `-on-staging`, `-release`. The guard uses them. |
| `repos.<repo>.remote` | Default `origin`. |
| `repos.<repo>.pr_source` | `git` forces git-only PR data. Otherwise the host CLI is used when it's installed. |
| `guard_mode` | `warn` (default), `ask` or `off`. See [The guard](#the-guard). |
| `stale_pr_days` | An open PR older than this raises an alert. Default 3. |
| `ignore_tickets` | Tickets that get no branch tracking at all: no git or host calls, and their records are dropped. |
| `fetch` | `{"enabled": true, "every_minutes": 30, "active_days": 14, "before_send_minutes": 20}`. See [Refresh and fetch](#refresh-and-fetch). |
| `pr_refresh_minutes` | How often open PRs are re-read. Default 60. |

For one ticket, the strategy is chosen in this order: a manual choice (`/cg:branch strategy ABC-1001 qa-only`), a rule, the repo's setting from the inbox, then the repo default.

## Base relations

How a repo's base branches relate, in `branching.repos.<repo>.relations`:

```json
"relations": [
  {"from": "release/*", "rel": "cut_from",    "to": "main"},
  {"from": "release/*", "rel": "merges_into", "to": ["qa", "staging", "main"]},
  {"from": "uat",       "rel": "syncs_from",  "to": "main"}
]
```

- `cut_from`: where new branches matching `from` start. The graph draws each base lane from its real parent instead of the first base, and adds that parent as a lane.
- `merges_into`: where `from` is merged next. When the ticket's change reached a target that way, the graph draws that merge between the base lanes; a missing delivery cell explains how the change will arrive, for example *in release/1.0; reaches qa when release/1.0 is merged into qa*.
- `syncs_from`: the base is refreshed from another one now and then (*reaches uat at its next sync from main*).

Relations change only the drawing and the wording. The delivery check itself still asks git whether each commit is really in each target.

`… cli relations detect [REPO] [--json]` proposes relations from your local remote-tracking refs, with the evidence (how many merges went which way, where recent release branches start). It is read-only and fetches nothing. `… cli relations set REPO --json '[…]'` saves a list through the config write path (backup and history); `… cli relations show [REPO]` prints what is set, with a warning for any entry that was dropped as invalid.

## How it is worked out

### Events

Branch creations, checkouts, commits, cherry-picks, merges, pushes and PR links come from two places:

- **Claude Code sessions.** The git command as captured by the PostToolUse hook. For `gh pr create`, `glab mr create` and `az repos pr create`, only the PR's URL is kept from the output.
- **Everything else** (IDE, terminal). Each tick reads the new reflog entries of the watched repos.

They are stored for good in `data/git/<repo>/events.jsonl` (append-only), because git expires reflog entries after 90 days.

### Linking a branch to a ticket

Tried in order; the ticket page shows which one made each link:

1. the ticket key in the branch name
2. the ticket key in its commit messages
3. the branch was created during a Claude Code session on that ticket
4. it holds commits with the same patch as the ticket's commits (catches cherry-pick branches with other names)
5. a manual link (`/cg:branch link`)

### Where a branch came from

1. the captured command (`git checkout -b X origin/qa`)
2. the reflog: `branch: Created from …`, or for `Created from HEAD`, the checkout just before
3. inferred: the base branch with the newest merge-base, marked *inferred*

An inferred parent is shown, but never used to choose the release that counts, and never raises a "wrong parent" alert.

### Branch status

`active` · `pr_open` · `merged` · `deleted_remote` · `abandoned`. A branch is **suggested** as abandoned (superseded) when all three hold:

- its recorded parent doesn't fit the strategy
- the ticket later got a branch from a parent that fits
- it has no PR, and no commits since then

You confirm with `/cg:branch abandon <repo> <branch>`. Abandoned and superseded branches stay visible but don't count in the delivery check. If they hold the only copy of some commits, you get an alert.

For each branch it also tracks: commits not pushed, own commits / how far its parent moved on, and the last commit.

### PRs

The data comes from the host's CLI, signed in with its own login: `gh` (GitHub), `glab` (GitLab), `az repos` (Azure DevOps, with the `azure-devops` extension). No token is stored. Without a CLI, PRs are read from the PR merge commits on the base branches ("Merged PR 812: …", "Merge pull request #812 from …", "See merge request !812"). That shows merged PRs with their merge commit and the head at merge, but not open, closed or draft PRs, nor reviews. The ticket page then says *PR info from merge commits only*.

Recorded per PR: number, title, source → target, state (draft · open · merged · closed), review state, merged time, merge commit and the head at merge.

### The delivery check

Rows are the ticket's commits, grouped by patch (a cherry-picked copy is the same row). Columns are the strategy's targets. Each cell gets exactly one status:

| Status | Meaning |
|---|---|
| ✓ in target | The commit is an ancestor of `<remote>/<target>`. |
| ✓ same change in target | Cherry-picked or rebased: a commit there has the same patch-id. |
| ✓ via squash PR | The host says the PR merged into the target, its merge commit is in the target, it wasn't reverted, and the commit was part of the PR's final head. |
| ⏳ PR open | An open PR into the target carries it. |
| ✗ missing | No PR carries it, it was pushed after its PR merged, it isn't pushed, or the PR merged but hasn't been fetched yet. The reason is shown. |
| ↩ reverted | A later commit in the target reverts it, or reverts its merge. |
| ? check by hand | Only the message or a `cherry picked from` trailer matches: the patch changed, most likely while resolving a conflict. |

A commit that names another ticket and not this one is left out. So is someone else's commit that doesn't name this ticket (for example, a release's other changes brought in by a merge). Patch-ids never change for a commit, so each one is computed once (`data/git/<repo>/patch-ids.json`).

### Alerts

Each alert is one plain sentence plus the commits it's about:

- commits pushed after their PR merged, which never reached the target (the lost-commit case)
- commits in one required target but not in another (in `release-then-qa`: qa behind the release; in `qa-cherry-pick`: uat or staging behind qa)
- commits that exist only on an abandoned or superseded branch
- a branch created from a parent the strategy doesn't use (recorded parents only)
- a PR into an unexpected base, with the commits from other work it would carry
- commits not pushed
- a commit that reached a target only with a changed patch
- a PR open longer than `stale_pr_days`, or approved but not merged

## Refresh and fetch

- **Every tick** (5 minutes), with no network: reflogs, branches, parents, links, PR merge commits, and the delivery check for tickets whose branches moved or that were active in the last 30 days. After the first run, this takes a couple of seconds.
- **Fetch:**
  - Only repos with tickets active in the last 14 days, at most every 30 minutes, and once in the 20 minutes before the 8 PM message.
  - The command is `git fetch --no-tags --no-auto-gc --no-auto-maintenance <remote>` with explicit refspecs, for the base branches and the active tickets' branches only. It never prunes.
  - It is non-interactive and windowless, and it is skipped (then retried) while any `.git/*.lock` file exists.
  - A branch deleted on the remote is noticed through `git ls-remote --heads`, and its history stays.
- **PRs:**
  - For active tickets, open PRs are re-read every 60 minutes and before 8 PM.
  - Merged or closed PRs are re-read only when their branch gets new commits.
- **Now:** `/cg:check [KEY]` fetches and refreshes at once.

The very first tick after installing v0.2.0 computes everything once and can take a few minutes on a large repo.

## Commands

| Command | What it does |
|---|---|
| `/cg:branches <KEY>` | Branches, PRs, the delivery summary per target, and alerts, in the chat |
| `/cg:check [KEY]` | Fetch and refresh now, then report |
| `/cg:branch link\|unlink <repo> <branch> <KEY>` | Fix a wrong link |
| `/cg:branch abandon\|restore <repo> <branch>` | Mark a branch abandoned (or undo it) |
| `/cg:branch strategy <repo> <name>` | Set a repo's strategy |
| `/cg:branch strategy <KEY> <name>` | Set one ticket's strategy |
| `/cg:ticket <KEY> --export [--mode shareable]` | Save the whole ticket page, branches and PRs included, as one HTML file |

`/cg:branch` writes one line to `inbox.md`, keeping the background agent the only writer of your data. You can also write the lines yourself:

```text
branch-link: <TICKET> <repo> <branch>
branch-unlink: <TICKET> <repo> <branch>
branch-abandon: <repo> <branch>
branch-restore: <repo> <branch>
strategy: <repo> <strategy-name>
strategy: <TICKET> <strategy-name>
```

## Where it shows up

- **Ticket page (dashboard):** an alert banner at the top, and a *Branches & PRs* section with a graph of the bases and the ticket's branches, the delivery matrix, a branches table and a PRs table. The ticket's `README.md` has the same section as Markdown.
- **Session start:** when the session's ticket has alerts, one line comes first, e.g. `ABC-1001: 3 commits not in release/1.4 — pushed after PR #812 merged. (+1 more on the ticket page)`.
- **8 PM Slack message:** when any of the day's tickets has alerts, a `Delivery check` block follows the closing note, one line per alert. The Jira entries above it are unchanged, and it goes out in the same send with no extra model call. It gets the same public redaction as the rest of the message (PR numbers are kept).
- **Daily file (pushed):** one line per ticket, e.g. `Branches 2 · PRs 1 merged, 1 open · Delivery ⚠ 3 commits not in release/1.4`. No URLs or hostnames.
- **Recall skill:** answers "did ABC-1001 reach release?", "which PRs are still open?" or "show my branches for ABC-1001" from this data.

## The guard

While Claude Code works, a PreToolUse hook looks at branch creation (`git checkout -b`, `git switch -c`, `git branch <new> <start>`) and PR creation (`gh pr create`, `glab mr create`, `az repos pr create`):

- **Branch creation:** it warns when the new branch's parent doesn't fit the strategy for that ticket, or when the name's suffix says `-uat` but the branch is cut from qa.
- **PR creation:** it warns when a PR's base doesn't fit the branch's parent (a qa-based branch with a PR into a release, for example).

What happens depends on `guard_mode`:

| Mode | Behaviour |
|---|---|
| `warn` (default) | The command runs; Claude is told why it looks wrong and tells you straight away. Your normal permission prompts are unchanged. |
| `ask` | Claude Code asks you to confirm before it runs. |
| `off` | Nothing. |

The guard starts only for those commands, through the hooks' `if` filter. It reads `.git/HEAD` and a few small JSON files, runs no git process, finishes in well under 200 ms, and fails open: any error lets the command run. Branches you create in your IDE or terminal are picked up from the reflog within one tick, and the ticket page flags them.

## Read-only, always

- **One wrapper.** Every git call against a work repo goes through `chitragupta/gitro.py`, which allows only `rev-parse`, `for-each-ref`, `log`, `rev-list`, `merge-base`, `cherry`, `patch-id`, `show` (metadata only), `reflog`, `branch --list/--contains`, `ls-remote`, `ls-files`, `remote get-url`, `config --get` and `fetch` into remote-tracking refs. Anything else is refused before a process starts.
- **Tested.** A unit test scans the code for mutating git calls.
- **No index locks.** Every call runs with `GIT_OPTIONAL_LOCKS=0`, so a read never takes the index lock.

## Limits

- **Squash merges** are recognised only with PR data from a host CLI (git-only mode can't tell a squash commit from any other).
- **Conflict-resolved cherry-picks** have a different patch-id. They show as *? check by hand* (matched by message or `-x` trailer), never as ✓.
- **Git-only mode** sees merged PRs only, through their merge commits: no open, draft or closed PRs, and no reviews.
- **Inferred parents** (branches older than the reflog) are labelled and never judged.
- **Someone else's commits** on your branch that don't name the ticket aren't counted.
- **The MCP fallback** for PR data (a host's read-only MCP through the existing harness) is not in v0.2.0.

## Privacy

- **Network:** fetch and the host CLIs are the only network calls this feature makes. They run from the background tick and `/cg:check`, never from a hook.
- **Credentials:** the host CLIs use their own sign-in. Chitragupta never stores a token.
- **Local only:** branch names, commit subjects, PR titles and PR URLs stay in the local knowledge base (`data/git/`, `tickets/`, `knowledge/`).
- **Pushed:** the daily file gets only the one summary line per ticket.
- **Slack:** the block gets the public redaction pass.

## Files

| File | Holds |
|---|---|
| `data/git/<repo>/events.jsonl` | Every branch event, append-only |
| `data/git/<repo>/branches.json` | One record per tracked branch |
| `data/git/<repo>/prs.json` | PRs by number |
| `data/git/<repo>/patch-ids.json` | Patch-id per commit |
| `data/git/<repo>/repo.json` | Host, PR source, cursors, last fetch, your manual links, abandons and strategy |
| `data/tickets/<KEY>.json` → `git` | Per repo: strategy, branches, PRs, targets, rows (the matrix) and alerts, plus a summary |

The v0.2.0 migration (`branch-tracking`, `schema_version` 4) backs up first, then:
- moves the old `data/branches.json` records into `data/git/<repo>/branches.json` (the old file is left as it was)
- seeds `events.jsonl` from the current reflogs
- adds `"branching"` to the config

`branch_fetch` is retired: fetching now never prunes.
