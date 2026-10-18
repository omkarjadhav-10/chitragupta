# Sharing a ticket with your team

`/cg:share <KEY>` turns the sections of a ticket you pick into a redacted bundle your team can read: a Markdown `README.md` and the ticket page as one `ticket.html`. You always see a preview first (what each section loses, what is never included), and nothing is written until you say yes. Sharing never posts to Slack.

## Set up a target

Add one or more targets to `share.targets` in `<home>\.worklog\config.json`:

```json
"share": {
  "author": "your name as the team should see it",
  "default_level": "team",
  "targets": [
    {"name": "team-folder", "kind": "folder", "path": "D:/Synced/Team/notes"},
    {"name": "team-repo", "kind": "git", "url": "https://example.test/team/notes.git", "branch": "main"}
  ]
}
```

| Key | Meaning |
|---|---|
| `share.targets[].kind` | `folder`: an existing folder (for example one your team syncs); bundles go under `<path>/shares/` only. `git`: a team Git repo; Chitragupta clones it to `<home>/share/<name>` on first use. |
| `share.targets[].name` | Letters, digits, `-` and `_`. The first target is the default; `--target NAME` picks another. |
| `share.targets[].branch` | Git targets: the branch to commit to (default `main`). |
| `share.targets[].default_level` | Optional: this target's level. |
| `share.default_level` | `team` (default) or `public`. |
| `share.author` | Shown as "by" on your bundles, and how Team notes recognises yours. |
| `share.denylist` | The deny-list file the scan uses (default `<home>/.worklog/denylist.txt`). One name or `re:<regex>` per line; a line starting `internal:` is a name your team may see but the public may not. |

## Levels

| | team (default) | public |
|---|---|---|
| Secrets, patient data | removed | removed |
| QA values and rows (QA trace, worked example, query result samples, request setup, headers and bodies) | removed | removed |
| Local file paths, the local file list | removed | removed |
| Your "My notes" | removed | removed |
| SQL text, hosts, internal service names, branch names, PR links | kept | removed (`internal:` names become `<internal>`) |
| Needs a deny-list | no | yes |

Never included at any level: raw captures, `.env` files, the local file list, Jira fields and "My notes".

## The scan

Before anything is written, the README and the ticket data inside `ticket.html` are scanned with the deny-list and the secret rules. Any hit blocks the share: nothing is written, and the preview lists each hit (section, line, rule). Leave that section out, or change the text in the ticket's notes, and try again. At the `team` level, `internal:` names are allowed.

## Where bundles go

`shares/<KEY>/<YYYYMMDD-HHMM>-<level>/README.md` and `ticket.html`. For a git target, Chitragupta then commits only those files, brings in the team's new commits with a plain merge (never a rebase), and pushes; it never force-pushes and never rewrites history. A push that fails leaves the commit in the clone, and the next share pushes it.

## Team notes

`/cg:share --notes` (and `cli share --notes`) lists every bundle in your targets, yours and your teammates', newest first. It only reads: a git target is read as its clone is now.

## Commands

```
cli share ABC-1001                                   # preview: nothing written
cli share ABC-1001 --sections summary,findings.root_cause,branches --yes
cli share ABC-1001 --level public --target team-folder --yes
cli share --notes [--target NAME] [--json]
cli ticket ABC-1001 --export --mode team              # the team-level page as one HTML file, kept locally
```
