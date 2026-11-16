# Companion: plan, resume and verify a ticket

The Companion turns a Jira ticket into a deliberate, recoverable task: pick the task for today, save an implementation
plan as an immutable version, review it, approve the exact version you want to hand to Claude, and later resume from
recorded evidence instead of from memory or conversation history. It works in the dashboard (**Companion** page), as
text through `/cg:plan` and `/cg:resume`, and, optionally, as a native pane inside Claude Code (see
[Native panes](#native-panes-optional)).

Chitragupta still never writes code, runs tests, creates branches or PRs, or changes Jira. **Execute Plan saves your
approval and prepares a prompt for you to paste into an ordinary Claude turn** (*Prepared; not submitted*). That turn
runs under your normal Claude Code permissions; Chitragupta only notices its activity afterwards through capture.

## The workflow

1. **Daily Planner.** Lists your in-scope tickets and saved tasks, grouped as *New*, *Planned*, *Review incomplete*,
   *In development*, *Paused*, *Awaiting verification*, *PR review* and *Completed* (collapsed). Each row says why it is
   suggested (pinned, blocked, resumable, priority, recent activity). Jira status and the task stage are kept apart: a
   Done ticket or an open PR doesn't complete a task. Selecting a ticket changes only what you are looking at, never
   who did earlier work.
2. **Requirements.** A new task starts from a requirement baseline proposed from the cached Jira card: configured
   criteria fields first (`companion.criteria_fields`), then cited bullet lines under an *Acceptance criteria* heading.
   You confirm it before it counts. The card keeps at most 3,000 description characters and ten comments, so a
   criterion past the cut is listed as a gap and the baseline says *Requirements incomplete*; with no criteria at all
   coverage is *unavailable*, never 100%. Comments and your clarifications stay *proposed* until you accept them into a
   new local baseline; Jira is never edited. Criterion IDs survive rewording and reordering, removed criteria stay in
   the baseline history, and a split criterion shares its parent's weight.
3. **Plan.** Write a plan yourself, paste one Claude drafted, or press **Generate Plan** (a paid, explicit model call;
   see [Budgets](#budgets-and-paid-calls)). A plan has ordered steps with dependencies, proposed files and components
   (cited from recorded evidence, or labelled *proposed new file*), database considerations, the criterion-to-step
   mapping, risks and edge cases, tests with expected outcomes, and verification steps; assumptions and missing
   information are listed apart from confirmed requirements. Every save is a new immutable version with its parent; a
   generated improvement is only a *candidate* until you select it.
4. **Review.** **Jev Review** shows each criterion as *Covered*, *Partial*, *Missing* or *Unverified* and the coverage
   score with its numerator and denominator. It always offers both **Improve Plan** (one Claude revision plus one
   evaluation attempt, never a loop) and **Execute Plan**, with one of them recommended. See [Jev](#jev-evaluation).
5. **Execute.** Shows the exact version and digest, the baseline, the target repository and location, the findings and
   the unresolved risks; your click saves the approval and a checkpoint first, then prepares the copy packet. The
   approval expires after five minutes and is invalidated by any change to the plan, the baseline, the recorded
   context or your profile. Only one execution owner may hold a working tree at a time.
6. **Track.** Your ordinary Claude session does the work. The background tick (every five minutes) checkpoints new
   evidence for open tasks — commits, files, tests, PRs, decisions — without acknowledging it. Associate the captured
   session with the approval when you see it (*Started*); a copy that never shows up stays *Prepared*.
7. **Pause and resume.** **Pause** checkpoints the task and keeps capture running (`/cg:pause` is different: it stops
   capture globally). **Resume Task** rebuilds the situation from durable records only — no conversation history — and
   offers only the actions that fit: reopen the saved plan and latest evaluation; continue interrupted development
   with *completed*, *pending*, *uncertain* and *revalidate* steps; finish outstanding tests; or continue an existing
   PR (never a second one). **Resume Execution** prepares a continuation packet the same way as Execute.
   Pause, a checkpoint and **Mark changes reviewed** also note where the ticket's [Evidence Graph](evidence-graph.md)
   stood; the Resume tab and `/cg:resume` then add *Evidence graph since your checkpoint*: requirements completed
   since, evidence that no longer holds, new commits, tests and runs (with their outcome and whether they apply),
   pull request and Jira changes, what is still outstanding and the graph's one next action. It only adds to the
   view: when the graph is not built, rebuilding or behind it says *unavailable* and everything else is unchanged,
   and it never changes which resume candidate is offered.
8. **Verify.** **Verify Requirements** recomputes a requirement-by-requirement ledger: *Verified*, *Partial*,
   *Missing* or *Unverified*, with the evidence, the revision it applies to and why older evidence stopped applying.
   **Complete** is accepted only with an explicit decision and no open mandatory gap (failing or missing verification,
   an open required PR, UAT still required).

## What counts as evidence

| Measure | What it is | What it is not |
|---|---|---|
| Plan coverage | `100 × Σ(weight × c) / Σ weight` over the confirmed criteria, with Covered = 1, Partial = 0.5, Missing = Unverified = 0. Four equally weighted criteria classified Covered, Partial, Missing, Unverified score 37.5%. | Proof that the code works. Classifier confidence never changes it. |
| Implementation progress | Evidenced implementation steps / approved implementation steps (verification steps not counted). | A checked box, a model's report or lines changed. |
| Functional verification | Verified criterion weight / total weight, from current, applicable test, CI or recorded acceptance evidence. | An old passing run, a Jira Done status, a merged PR, Jev confidence or Claude saying it is done. |

Evidence applies to the current code only when the repository, location, HEAD, working-tree, configuration and
environment it was observed under are all known and equal the current observation. Captured test runs don't record
their working-tree state yet, so they show *Applicability unknown* and can't verify a criterion on their own. You can
record what you observed (**Record current context** on the Resume tab: HEAD commit, branch and working tree; labelled
*developer-recorded*) and record a scoped acceptance check (**Record an acceptance observation (self-reported)** on the
Verification tab) for the criteria it covers. Later
changes make earlier evidence *historical*; it is never erased. Cited evidence is pinned into the task so pruning old
test records can't remove it.

**CI checks** are read for GitHub pull requests you raised (your confirmed account only), while they are open, as part
of the existing PR refresh: `gh api` GET of the check runs of the PR's head commit (100 per page, at most 3 pages, at
most 5 PRs and 60 seconds per repository refresh; never on render). Each run keeps only its name, status, conclusion,
attempt and times — no logs, output or links. The state always belongs to the current head: *CI checks passed on head
…*, *need attention*, *still running*, *partly read* (a rate limit, a permission error or the page cap keeps the earlier
runs as stale), *missing* (a check that ran on an earlier head hasn't run on this one) or *unavailable* (not read yet,
or GitLab / Azure DevOps: *not implemented for this host*). A pass on an earlier head never clears the current one, and
a re-run stays a separate attempt. A CI run is evidence like a test run: it counts toward a criterion only when you map
it, and because GitHub doesn't report the configuration and environment it ran with, it shows *applicability unknown*
rather than verifying a criterion by itself.

**Since your last checkpoint** lists ingested evidence you haven't acknowledged. Background ingestion, saving or
reopening never marks it read; only **Mark changes reviewed** does.

## Decisions, impact, attention and drafts

- **Decisions** (Resume tab) come from the ticket's recorded knowledge items and their supersession: *Confirmed*,
  *Proposed*, *Needs review*, *Superseded* or *Recorded (captured; not confirmed)*, with history kept. Conflicting
  decisions are shown side by side for review, never merged. A failed approach recorded in another repository or
  environment is marked *context only, not a prohibition here*.
- **Impact** (Resume tab) lists what a change may touch, from recorded links only: the ticket's files, APIs, saved
  requests and tests, other tickets that share them, and the cached database impact results. Each suggestion says why,
  whether the link is *Explicit*, *Historical* or *Inferred*, and a *Proposed check (text only; nothing runs from
  here)*. With nothing recorded it says *No recorded impact links*, never "no impact" or "safe to change".
- **Attention** (Focus band) shows at most three items first (**Show all**). **Acknowledge** hides an item until its
  evidence materially changes (a new UAT revision reopens it); **Snooze…** hides it for 1 hour, 1 day or 1 week. Both
  change only what you see: a required item stays in the full list and its delivery or completion gate is unchanged.
  The state is per profile in `data/workflows/.attention.json`.
- **Handoff drafts** (Handoff tab → Drafts): **PR reviewer**, **QA tester** and **Next developer session** templates
  built from the task's own evidence, each with purpose, verified changes, evidence, risks and questions, next action
  and a **Missing information** section. They never say all tests pass, deployed or accepted without current evidence,
  and self-reported parts are labelled. **Generate draft** (no model) and **Save new version** keep every version;
  regenerating never overwrites your edit, and a draft whose sources changed keeps your text with *Source evidence
  changed*. **Copy draft** rechecks your access and redaction; nothing is sent anywhere. Text form:
  `cli companion draft <w_…> --audience pr|qa|session`.

## Jev evaluation

Jev (TypeSafe's structured decision model) would classify each criterion against the saved plan. **Live Jev is off by
the current safety policy**: Chitragupta's network guard allows only GET to two hosts, and an inference POST needs a
separately reviewed safety-contract change first. Until then Jev Review shows *Evaluation unavailable — Jev unavailable
by current safety policy*, never a 0% or a passing score. You can still review the plan yourself (**Record
assessment**: a *developer assessment*, labelled as such and never shown as a Jev score), improve it, and execute it
after acknowledging that it wasn't evaluated.

The adapter's Jev Review tab shows **Evaluation**, the developer assessment (**Classify the criteria yourself** →
**Record assessment**), the candidate versions, and **Improve or execute** with **Improve Plan** and **Execute Plan**,
one marked *Recommended*. Without an evaluation, Execute asks you to acknowledge "the unresolved risks, the unavailable
Jev evaluation and the unknown working-tree state" for that exact version.

The adapter is built and tested against synthetic stubs only: one choice question per criterion (at most 50 per
request, 256 KiB each way), separate feasibility and risk questions, answer validation (unknown IDs, values or
non-finite numbers become *Unverified*), at most two retries and only for 429/529, a 30-second request timeout and a
120-second deadline, cancellation, and a payload check that refuses secrets, absolute paths, URLs, tool requests and
raw database keys before anything would be sent. The credential would be an environment-variable name only
(`jev.credential_env`), and the first use would require acknowledging what is shared with the provider. The pinned
model name and the request shape are unverified against the live service.

## Budgets and paid calls

**Generate Plan** and **Improve Plan** are off until you set *Companion planning: spend ceiling per call*
(`budget.companion_call_usd`, Settings → Models & budget). Each explicit call first reserves that amount of today's
model spend cap (`budget.model_daily_usd`) under a lock shared by every session; a call whose reservation doesn't fit is
refused before anything runs. At most two paid Companion jobs run at a time, one per task; a second click shows the
running job instead of starting another. A call that times out after it may have been accepted keeps its reservation
as *outcome unknown* and is never retried on its own: **Retry** says it may cost twice. The Claude call runs in
Chitragupta's sandbox with no tools, the plugin disabled and a 180-second limit; its output is validated (unknown
criteria, invented existing files, paths, secrets and tool requests are refused) and saved as a new version before any
evaluation. Jev would add its own subcap ($0.50 a day, $0.10 per task a day).

Reading, refreshing, switching tabs, opening a pane and polling a job never call a model, a provider, Git or a
database, and never start a job.

## Storage and recovery

Each task is `data/workflows/<w_…>/` in the data home: content-addressed, hash-checked checkpoints
(`checkpoints/<sha256>.json`) and one `head.json` replaced atomically last, so a crash leaves either the previous or
the new complete checkpoint, never a mix. Saves carry the revision you edited: a concurrent save from another session
gets *changed elsewhere* with your text kept. A full disk or a busy lock answers *Not saved* with your draft kept, and
nothing is handed off. When a task reports *recovery required*, `python -m chitragupta.companion recover <w_…>` moves
incomplete or orphaned files to `quarantine/` and re-links the ticket, idempotently; it never picks a checkpoint for
you. Jobs live under `jobs/`, the spend
ledger in `data/workflows/.budget.json`. The ticket record keeps only a short `workflows` reference. Workflow folders
stay on this machine: they are not pushed, shared or indexed for search, and the nightly local backup
(`storage.local_backup_dir`, when set) includes them like the rest of the data home. Plans, approvals and checkpoints
are kept until you archive a task; archiving keeps its history.

## Native panes (optional)

The `companion/` folder is a separate, optional plugin (`cg-companion`) for Claude Code hosts with Mods support. See
[companion/README.md](../companion/README.md) for activation, the supported host versions and the tested surfaces. It
only renders projections it reads from its own read-only MCP server; it never writes, never calls a model or provider,
and never submits a prompt. Without it, everything above works in the dashboard and through `/cg:plan` and
`/cg:resume`.

## Known limits

- Live Jev is blocked by policy (above); the manual review path is not a Jev evaluation.
- Direct prompt submission into a Claude session is disabled: the host can't yet prove a queued prompt is revalidated
  or cancelled after a context switch, so execution always uses the copy packet.
- Working-tree state at test time isn't captured, so automated verification needs a test record with a captured
  context or your recorded context plus a scoped acceptance check.
- CI checks are read for GitHub only (`gh` on PATH as an executable; a `.cmd` shim is refused by the command guard),
  not for GitLab, Azure DevOps or GitHub Enterprise, and a CI run alone never verifies a criterion (its configuration
  and environment are unknown).
- Native panes are verified only with Claude Code's offline mod test kit; interactive terminal, Desktop and VS Code
  walkthroughs are not done (see the companion README).
