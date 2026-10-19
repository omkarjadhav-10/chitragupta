# Development

Read [CLAUDE.md](../CLAUDE.md) first. It is the contributor contract: fixed names, the fail-open rule for hooks, the standard-library rule, and **no company or personal data anywhere in the repo**.

## Setup after cloning

```
python scripts/install_git_hooks.py     # pre-commit: deny-list + secret scan on staged files
```

The deny-list lives at `notes/denylist.txt`. `notes/` is git-ignored, so each contributor writes their own. Put one term per line, or use `re:<regex>`; lines starting with `#` are comments. List your employer, clients, internal hosts and tool names, real ticket prefixes and colleagues' names. `scan.py` refuses to pass without a deny-list, except with `--secrets-only`.

## Dev loop

Never point a dev session at a real data home. Develop against a copy.

1. **Make a dev home** (git-ignored, inside the repo):

   ```
   python scripts/sync_dev_home.py --source <a data home to copy> [--dest .dev-home] [--force]
   ```

   The copy's git remote is removed and `branch_fetch` is turned off. Slack, the Jira and database MCP jobs, and the nightly backup are switched off too, so nothing reaches outside the machine. The source is only read. The script prints the environment for the session. You can also start from an empty folder and run setup against it.

2. **Set the dev environment.** In PowerShell, from the repo root:

   ```
   $env:CHITRAGUPTA_HOME       = "$PWD\.dev-home"       # data home
   $env:CHITRAGUPTA_USER_DIR   = "$PWD\.dev-user"       # runtime + pointer, not ~/.chitragupta
   $env:CHITRAGUPTA_CLAUDE_DIR = "$PWD\.dev-claude"     # recall skill + CLAUDE.md block, not ~/.claude
   $env:CHITRAGUPTA_NO_TASK    = "1"                    # never touch the real Task Scheduler
   $env:CHITRAGUPTA_DEV        = "1"                    # lets setup accept a home inside the repo
   $env:WORKLOG_HOME           = "$PWD\.dev-throwaway"  # only if a legacy worklog engine's hooks are installed
   ```

   `CHITRAGUPTA_DASHBOARD_PORT` moves the dev dashboard off the default port. Use it if an installed copy is already running on 7419.

3. **Make a scratch repo** to work in, for example `.dev-scratch` with a branch like `feature/ABC-1001-demo`.

   **Always create `.worklog-ignore` in a scratch repo before its first session if another worklog is running on this machine.** That could be an installed Chitragupta or a legacy engine. Environment variables only redirect the hooks of the session you start, but the other worklog's background transcript scan reads Claude Code's transcripts directly. Without the file, that scan files your dev session into your **real** data home.

   The file also stops the dev copy from capturing that repo. To test capture end to end, run the dev session under its own Claude Code config folder (`$env:CLAUDE_CONFIG_DIR = "$PWD\.dev-config"`, which needs its own `/login`). Then no other worklog sees the transcripts at all.

4. **Load the plugin from the checkout:**

   ```
   claude --plugin-dir <path to this repo>
   ```

   Use `/reload-plugins` after changing skills or hooks. Check two things: events land in `.dev-home\.worklog\raw\`, and nothing lands in your real data home.

5. **Run engine commands** straight from the checkout, with the same environment:

   ```
   py -3 -m chitragupta agent tick --force
   py -3 -m chitragupta doctor
   py -3 -m chitragupta dashboard ensure
   py -3 hooks/run.py cli today
   ```

`.dev-home/`, `.dev-user/`, `.dev-claude/`, `.dev-scratch/` and `.dev-throwaway/` are git-ignored.

## Tests

```
python -m unittest discover -s tests -t .
```

The tests are standard-library `unittest`. Each test runs in its own temp `CHITRAGUPTA_HOME` and `CHITRAGUPTA_USER_DIR`, with maintenance, dashboard autostart and the real Task Scheduler all switched off (`tests/support.py`). `Isolated.isolate()` registers the restore as the test's first cleanup, so it runs last, after `tearDown()` and every cleanup the test adds; `restore()` is kept for old `tearDown()`s and does nothing itself. The restore fails the test when a temp folder can't be removed (usually a file left open). Importing `tests/support.py` also points `CHITRAGUPTA_HOME`, `CHITRAGUPTA_USER_DIR` and `CHITRAGUPTA_CLAUDE_DIR` at one temp folder for the whole run when they aren't set, so nothing that runs between tests can reach your real folders. Every model, Slack and MCP call is stubbed, and tripwires fail a test that tries a real one. The data is synthetic: `ABC-1001…1004`, `acme.test`, a `shop` schema. The golden Jira-worklog layouts are compared byte for byte.

CI (`.github/workflows/test.yml`) runs on Windows and Ubuntu with Python 3.10, 3.11 and 3.12. It runs the version check, the secret scan (`--secrets-only`, since the deny-list is local) and the unit suite.

### UI checks

Browser checks for the dashboard, on the same synthetic home the unit tests use (never a real one):

```
cd scripts/ui; npm ci; cd ../..                  # once: playwright-core, which drives your installed Chrome or Edge
python scripts/ui/run.py [--shots <folder>]      # every page, both themes, 1440/1280/900 px
```

It fails on a serious or critical axe violation (WCAG 2.2 AA; axe-core is vendored in `scripts/ui/vendor/`, dev only), a console error, any request leaving `127.0.0.1`, a first view slower than 1 s, horizontal scrolling at 900 px, or a keyboard check: skip link first, focus visible and targets of at least 24 px on every stop, the palette, tabs, `?` and `g` shortcuts, and the live event stream. `--shots` saves a screenshot of every page.

The page is plain ES modules with no build step: `chitragupta/dashboard/app/` (`main.js`, `lib/`, `pages/`, `app.css`) and the vendored Preact in `third_party/preact/`. Components use a small tagged-template helper (`lib/html.js`) instead of JSX.

### End-to-end (opt-in, real model calls)

These scripts make real `claude -p` calls. That costs money and needs a signed-in Claude Code. They do nothing unless `CHITRAGUPTA_E2E=1`. Each runs in a throwaway home with synthetic data and never reaches Slack or Jira. `CHITRAGUPTA_E2E_CLAUDE` picks the `claude` executable.

```
$env:CHITRAGUPTA_E2E = "1"
py -3 scripts/e2e/e2e_selftest.py --simulated   # hook launcher fed directly, then a real tick
py -3 scripts/e2e/e2e_selftest.py --live        # a real claude -p session with only Chitragupta's hooks
py -3 scripts/e2e/e2e_upgrade01.py              # saved requests + DB flow with real models
py -3 scripts/e2e/e2e_upgrade02.py guard        # read-only guard against the stub database MCP
py -3 scripts/e2e/e2e_upgrade02.py jobs         # schema card, QA trace, link map against the stub
```

Add `--keep` (or `KEEP=1` for `e2e_upgrade01`) to keep the temp folder.

### Parity harness

`scripts/parity.py` runs the legacy worklog engine and this package side by side on two copies of one data home. Both copies get a frozen clock, recorded model answers, MCP and Slack off, and a simulated private repo. The harness then compares the trees and outputs byte for byte:

```
python scripts/parity.py --old-engine <legacy .worklog/engine> --source <data home> --work <empty dir>
```

It writes `<work>/parity-report.md` and nothing outside `<work>`. Use it whenever you touch rendering, merging or the Jira-worklog layout.

## Scans

```
python scripts/scan.py --all          # every tracked + untracked-not-ignored file
python scripts/scan.py --staged       # what the pre-commit hook runs
python scripts/scan.py --history      # every commit: patches, messages, author/committer — before any push to a new remote
python scripts/scan.py --all --secrets-only   # what CI runs
```

`CHITRAGUPTA_DENYLIST` points the scans at another deny-list file. Never bypass the pre-commit hook with `--no-verify`.

## Versioning

The version lives in three places: `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` and `chitragupta/__init__.py`. They must agree, so change them only with the script:

```
python scripts/bump_version.py 0.2.0
python scripts/bump_version.py --check    # exit 1 if they disagree (CI runs this)
```

The project uses Semantic Versioning, and `CHANGELOG.md` follows Keep a Changelog. Every user-visible change updates `docs/` and `CHANGELOG.md` in the same commit.

The data home's formats are shared with existing installs. Any format change needs a forward-only migration in `chitragupta/migrate.py` that backs up first.

## Release

1. Move the `[Unreleased]` entries in `CHANGELOG.md` under the new version, with today's date.
2. Run `python scripts/bump_version.py X.Y.Z`.
3. Run `python -m unittest discover -s tests -t .` and check it is green.
4. Run `python scripts/scan.py --all` and `python scripts/scan.py --history`, and check both are clean.
5. Run `claude plugin validate .`. It passes with one expected warning: the root `CLAUDE.md` is a contributor doc.
6. Check a fresh install. Use a temporary `CLAUDE_CONFIG_DIR` plus `CHITRAGUPTA_USER_DIR`, and set `CHITRAGUPTA_NO_TASK=1`. Install from the marketplace, confirm the "isn't set up yet" line, run `/cg:setup`, then check capture, a tick, the daily note and the dashboard.
7. Commit, tag `vX.Y.Z` and push. Check that CI is green.

Users get the update through Claude Code's plugin update. Their next session's maintenance step refreshes the stable runtime, and the dashboard is replaced by the new version.

## Moving from a legacy worklog engine

`scripts/cutover.py apply --home <data home>` retires a standalone install of the legacy worklog engine. It backs everything up first into `~/.chitragupta/backups/cutover-<timestamp>/`, with a zip of the data home and a manifest. Then it:

- disables the old scheduled task
- removes its hook entries, skill and CLAUDE.md block

`--dry-run` shows the plan without changing anything. `scripts/cutover.py rollback` restores everything from the manifest. Data is never deleted.
