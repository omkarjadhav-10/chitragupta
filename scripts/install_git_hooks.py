"""Install the repo's pre-commit hook (deny-list + secret scan).

Run once after cloning:  python scripts/install_git_hooks.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    hooks = Path(subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--git-path", "hooks"],
                                check=True, capture_output=True, text=True).stdout.strip())
    if not hooks.is_absolute():
        hooks = ROOT / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    py = Path(sys.executable).as_posix()
    hook = hooks / "pre-commit"
    hook.write_text(f'#!/bin/sh\nexec "{py}" "$(git rev-parse --show-toplevel)/scripts/scan.py" --staged\n',
                    encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    print(f"installed {hook}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
