# Repository Guidelines

## Project Structure & Module Organization
Chitragupta is a Claude Code worklog plugin with a Python 3.10+ standard-library runtime. Read `CLAUDE.md` for the contributor contract and `docs/development.md` for detailed workflows.

- `chitragupta/`: engine, prompts, safety layer, and dashboard. Dashboard ES modules and CSS live in `dashboard/app/`; no frontend build is required.
- `hooks/`, `bin/`, `.claude-plugin/`: launchers, hook configuration, and plugin manifests.
- `skills/`: explicit `/cg:` commands; `user-skill/chitragupta/`: recall skill.
- `tests/`: unit tests and synthetic fixtures; `scripts/`: development, validation, and browser checks.
- `docs/`, `examples/`, `third_party/`: documentation, examples, and vendored assets.

## Build, Test, and Development Commands
Run from the repository root unless specified:

- `python scripts/install_git_hooks.py`: install the required pre-commit scanner; configure your local `notes/denylist.txt` first.
- `python -m unittest discover -s tests -t .`: run the unit suite.
- `python scripts/bump_version.py --check`: verify manifest/package version agreement.
- `python scripts/scan.py --all`: scan for secrets and local deny-list terms. Never bypass hooks with `--no-verify`.
- `claude plugin validate .`: validate plugin metadata.
- `claude --plugin-dir <repo-path>`: load the checkout for development after configuring the isolated environment described in `docs/development.md`.
- `python scripts/ui/run.py`: run browser checks; first run `npm ci` inside `scripts/ui/`.

## Coding Style & Naming Conventions
Follow existing style: four-space Python indentation, `snake_case` functions/modules, and `PascalCase` classes. Use plain JavaScript ES modules and the existing template helper rather than JSX. No repository-wide formatter or linter is configured. Runtime imports must remain standard-library-only. Route side effects through `chitragupta/safety/`; hooks must fail open, exit successfully, and avoid network/model calls.

## Testing Guidelines
Use `unittest`, `tests/test_*.py`, and `test_*` methods. Reuse `tests/support.py` isolation helpers and synthetic values such as `ABC-1001` and `acme.test`. Never target real data homes or services. CI covers Windows/Ubuntu and Python 3.10–3.12; no numeric coverage threshold is configured. Run browser checks for dashboard changes.

## Commit & Pull Request Guidelines
History uses descriptive subjects with optional issue prefixes such as `FX-12:`; no strict Conventional Commits format is evident. Keep commits focused. PRs should explain behavior changes, link relevant issues, report validation, and include screenshots for visual changes. Update `docs/` and `CHANGELOG.md` alongside user-visible changes. Keep company data and credentials out of code, fixtures, documentation, and commits.
