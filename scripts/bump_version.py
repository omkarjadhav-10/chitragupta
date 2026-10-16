"""Set one version everywhere: plugin.json, marketplace.json, chitragupta/__init__.py.

Usage:  python scripts/bump_version.py 0.2.0
        python scripts/bump_version.py --check      # exit 1 if they disagree
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
MARKET = ROOT / ".claude-plugin" / "marketplace.json"
INIT = ROOT / "chitragupta" / "__init__.py"
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
INIT_RX = re.compile(r'^__version__ = "([^"]+)"$', re.M)


def current() -> dict[str, str]:
    plugin = json.loads(PLUGIN.read_text(encoding="utf-8"))
    market = json.loads(MARKET.read_text(encoding="utf-8"))
    entry = next(p for p in market["plugins"] if p["name"] == plugin["name"])
    m = INIT_RX.search(INIT.read_text(encoding="utf-8"))
    return {"plugin.json": plugin["version"], "marketplace.json": entry["version"],
            "__init__.py": m.group(1) if m else ""}


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def bump(version: str) -> None:
    plugin = json.loads(PLUGIN.read_text(encoding="utf-8"))
    plugin["version"] = version
    write_json(PLUGIN, plugin)
    market = json.loads(MARKET.read_text(encoding="utf-8"))
    for p in market["plugins"]:
        if p["name"] == plugin["name"]:
            p["version"] = version
    write_json(MARKET, market)
    INIT.write_text(INIT_RX.sub(f'__version__ = "{version}"', INIT.read_text(encoding="utf-8")),
                    encoding="utf-8", newline="\n")


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        versions = current()
        ok = len(set(versions.values())) == 1
        print(("ok: " if ok else "MISMATCH: ") + ", ".join(f"{k}={v}" for k, v in versions.items()))
        return 0 if ok else 1
    if len(argv) != 1 or not SEMVER.match(argv[0]):
        print(__doc__, file=sys.stderr)
        return 2
    bump(argv[0])
    print("set", argv[0], "->", ", ".join(current()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
