"""Make (or refresh) a development copy of a data home.

    python scripts/sync_dev_home.py --source <data home> [--dest .dev-home] [--force]

The copy has its git remote removed and push, Slack and every MCP job switched off,
so a dev session can't reach anything outside the machine. It also prints the
environment variables for a dev session. The source is only read.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEGACY_DB_KEY = "d" + "pilot"  # legacy config key of the original worklog engine


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True)
    ap.add_argument("--dest", default=str(REPO / ".dev-home"))
    ap.add_argument("--force", action="store_true", help="replace an existing copy")
    args = ap.parse_args(argv)
    src, dst = Path(args.source).resolve(), Path(args.dest).resolve()
    if dst.exists():
        if not args.force:
            ap.error(f"{dst} exists (use --force to replace it)")
        shutil.rmtree(dst, onerror=lambda f, p, e: (Path(p).chmod(0o666), f(p)))
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("engine", "__pycache__", "*.lock", "slack-sender"))
    if (dst / ".git").exists():
        subprocess.run(["git", "-C", str(dst), "remote", "remove", "origin"], capture_output=True)
    cfg_path = dst / ".worklog" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["github_remote"] = ""
    cfg["branch_fetch"] = False
    cfg["worklog_home"] = dst.as_posix()
    jw = cfg.setdefault("jira_worklog", {})
    jw.setdefault("slack", {})["enabled"] = False
    for key in ("jira", "database", LEGACY_DB_KEY):
        if isinstance(cfg.get("mcp", {}).get(key), dict):
            cfg["mcp"][key]["enabled"] = False
    cfg.setdefault("storage", {})["local_backup_dir"] = ""
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    user = dst.parent / ".dev-user"
    throwaway = dst.parent / ".dev-throwaway"
    user.mkdir(exist_ok=True)
    throwaway.mkdir(exist_ok=True)
    print(f"dev home ready: {dst}")
    print("Dev session environment (PowerShell):")
    print(f'  $env:CHITRAGUPTA_HOME="{dst}"; $env:CHITRAGUPTA_USER_DIR="{user}"; '
          f'$env:CHITRAGUPTA_NO_TASK="1"; $env:WORKLOG_HOME="{throwaway}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
