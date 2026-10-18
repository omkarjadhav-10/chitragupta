"""Deny-list and secret scanner for the Chitragupta repo.

Usage:
    python scripts/scan.py --staged      # pre-commit: staged blobs only
    python scripts/scan.py --all         # every tracked + untracked-not-ignored file
    python scripts/scan.py --history     # every commit: patches, messages, author/committer
    python scripts/scan.py --all --secrets-only   # CI (the deny-list is local only)

The deny-list is read from $CHITRAGUPTA_DENYLIST, else notes/denylist.txt.
Plain lines match as case-insensitive substrings; lines starting with "re:"
are Python regexes. Lines starting with "#" are comments. Exit 1 on any hit.
The rules and the matching live in chitragupta/scanlib.py (shared with the share gate).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT))
from chitragupta.scanlib import SECRET_RULES, scan_text, secret_rules  # noqa: E402,F401  (one implementation)
from chitragupta import scanlib  # noqa: E402


def load_denylist(path: Path | None):
    return scanlib.load_denylist(str(path) if path else "")


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                          stdout=subprocess.PIPE).stdout


def decode(blob: bytes) -> str | None:
    if b"\0" in blob[:8192]:
        return None
    return blob.decode("utf-8", errors="replace")


def scan_paths(paths, read, deny, secrets) -> list[str]:
    hits = []
    for p in paths:
        hits += scan_text(f"<path> {p}", p, deny, [])
        text = decode(read(p))
        if text is not None:
            hits += scan_text(p, text, deny, secrets)
    return hits


def staged(deny, secrets) -> list[str]:
    names = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode().split("\0")
    return scan_paths([n for n in names if n], lambda p: git("show", f":{p}"), deny, secrets)


def all_files(deny, secrets) -> list[str]:
    names = git("ls-files", "-co", "--exclude-standard", "-z").decode().split("\0")
    names = [n for n in names if n and (ROOT / n).is_file()]
    return scan_paths(names, lambda p: (ROOT / p).read_bytes(), deny, secrets)


def history(deny, secrets) -> list[str]:
    hits = []
    shas = git("rev-list", "--all").decode().split()
    for sha in shas:
        meta = git("show", "-s", "--format=%an <%ae>%n%cn <%ce>%n%B", sha).decode("utf-8", "replace")
        hits += scan_text(f"{sha[:10]} meta", meta, deny, secrets)
        patch = git("show", "--format=", "-p", "--no-color", "--no-ext-diff", sha)
        text = decode(patch) or ""
        added = "\n".join(l[1:] for l in text.splitlines() if l.startswith("+") and not l.startswith("+++"))
        hits += scan_text(f"{sha[:10]} diff", added, deny, secrets)
        files = git("show", "--format=", "--name-only", sha).decode().split()
        for f in files:
            hits += scan_text(f"{sha[:10]} <path> {f}", f, deny, [])
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--history", action="store_true")
    ap.add_argument("--secrets-only", action="store_true", help="skip the deny-list (CI)")
    args = ap.parse_args(argv)

    deny = []
    if not args.secrets_only:
        env = os.environ.get("CHITRAGUPTA_DENYLIST")
        path = Path(env) if env else ROOT / "notes" / "denylist.txt"
        deny = load_denylist(path)
        if not deny:
            print(f"scan: no deny-list at {path}; refusing to pass without it "
                  "(use --secrets-only in CI)", file=sys.stderr)
            return 1
    secrets = secret_rules()

    fn = staged if args.staged else all_files if args.all else history
    hits = fn(deny, secrets)
    for h in hits:
        print(h)
    if hits:
        print(f"scan: {len(hits)} hit(s); nothing may be committed until they are gone.", file=sys.stderr)
        return 1
    print(f"scan: clean ({len(deny)} deny rules, {len(secrets)} secret rules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
