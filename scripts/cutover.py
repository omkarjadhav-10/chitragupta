"""Retire a legacy worklog install so Chitragupta can take over the same data home — and undo it.

    python scripts/cutover.py apply    --home <data home> [--claude-dir ~/.claude] [--task WorklogAgent]
                                       [--backup-root ~/.chitragupta/backups] [--dry-run]
    python scripts/cutover.py rollback [--manifest <backup dir>/manifest.json]   (default: the latest cutover)

`apply` backs everything up first, into <backup-root>/cutover-<timestamp>/:
  1. data-home.zip            the whole data home (incl. .git)
  2. config.json              <home>/.worklog/config.json before Chitragupta migrates it
  3. <task>.xml               the legacy scheduled task, which is then DISABLED (not deleted)
  4. settings.json            ~/.claude/settings.json; hook entries that run <home>/.worklog/engine/hook.py are removed
  5. skills-worklog/          ~/.claude/skills/worklog, which is then removed
  6. CLAUDE.md                ~/.claude/CLAUDE.md; the block between <!-- worklog:start --> and <!-- worklog:end --> is removed
and writes manifest.json. The legacy engine folder (<home>/.worklog/engine) is never touched.

`rollback` undoes it from the manifest: uninstalls Chitragupta's own pieces (task, runtime, recall skill,
CLAUDE.md block, home pointer — data is kept), restores settings.json, CLAUDE.md, the skill and the
pre-migration config.json, and re-enables the legacy task. Remove the plugin itself with
`claude plugin uninstall cg@chitragupta` (printed at the end).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEGACY_START, LEGACY_END = "<!-- worklog:start -->", "<!-- worklog:end -->"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def claude_dir(arg: str | None) -> Path:
    return Path(arg or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def user_dir() -> Path:
    return Path(os.environ.get("CHITRAGUPTA_USER_DIR") or Path.home() / ".chitragupta")


def schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=NO_WINDOW)


def zip_dir(src: Path, dest: Path) -> int:
    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                p = Path(root) / f
                try:
                    z.write(p, p.relative_to(src).as_posix())
                    n += 1
                except OSError:
                    pass  # a file locked by a running tick; the rest of the backup still counts
    return n


def legacy_hook(entry: dict, engine: str) -> bool:
    text = json.dumps(entry).replace("\\\\", "/").replace("\\", "/").lower()
    return engine in text and "hook.py" in text


def strip_hooks(settings: dict, engine: str) -> int:
    removed = 0
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if not legacy_hook(h, engine)]
            removed += len(group.get("hooks", [])) - len(kept)
            if kept:
                groups.append(dict(group, hooks=kept))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    return removed


def strip_block(text: str) -> tuple[str, bool]:
    rx = re.compile(r"\n?" + re.escape(LEGACY_START) + r".*?" + re.escape(LEGACY_END) + r"\n?", re.S)
    new, n = rx.subn("\n", text, count=1)
    return (new.lstrip("\n") if n else text), bool(n)


def apply(args) -> int:
    home = Path(args.home).resolve()
    cdir = claude_dir(args.claude_dir)
    if not (home / ".worklog" / "config.json").is_file():
        print(f"{home} has no .worklog/config.json — not a worklog data home", file=sys.stderr)
        return 2
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.backup_root or user_dir() / "backups") / f"cutover-{ts}"
    engine = (home / ".worklog" / "engine").as_posix().lower()
    plan, man = [], {"created": ts, "home": str(home), "claude_dir": str(cdir), "task": args.task, "steps": []}

    settings_p, skill_p, md_p = cdir / "settings.json", cdir / "skills" / "worklog", cdir / "CLAUDE.md"
    settings = json.loads(settings_p.read_text(encoding="utf-8")) if settings_p.is_file() else None
    n_hooks = strip_hooks(json.loads(json.dumps(settings)), engine) if settings else 0
    md = md_p.read_text(encoding="utf-8") if md_p.is_file() else ""
    has_block = LEGACY_START in md and LEGACY_END in md
    task_q = schtasks("/query", "/tn", args.task, "/xml") if os.name == "nt" else None
    has_task = bool(task_q and task_q.returncode == 0)

    plan.append(f"backup dir: {out}")
    plan.append(f"zip data home {home} -> data-home.zip")
    plan.append(f"save {home / '.worklog' / 'config.json'} (pre-migration)")
    plan.append(f"task {args.task}: " + ("export XML, then disable" if has_task else "not found — skipped"))
    plan.append(f"{settings_p}: remove {n_hooks} legacy hook entr{'y' if n_hooks == 1 else 'ies'}")
    plan.append(f"{skill_p}: " + ("move to backup" if skill_p.is_dir() else "not found — skipped"))
    plan.append(f"{md_p}: " + ("remove the worklog:start/end block" if has_block else "no legacy block — skipped"))
    print("\n".join(("DRY RUN: " if args.dry_run else "") + p for p in plan))
    if args.dry_run:
        return 0

    out.mkdir(parents=True)
    n = zip_dir(home, out / "data-home.zip")
    man["steps"].append({"step": "zip", "files": n, "path": str(out / "data-home.zip")})
    shutil.copy2(home / ".worklog" / "config.json", out / "config.json")
    man["steps"].append({"step": "config", "path": str(out / "config.json")})
    if has_task:
        (out / f"{args.task}.xml").write_text(task_q.stdout, encoding="utf-8")
        r = schtasks("/change", "/tn", args.task, "/disable")
        man["steps"].append({"step": "task", "disabled": r.returncode == 0, "detail": (r.stdout or r.stderr).strip()})
    if settings is not None:
        shutil.copy2(settings_p, out / "settings.json")
        if n_hooks:
            strip_hooks(settings, engine)
            settings_p.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        man["steps"].append({"step": "settings", "removed_hooks": n_hooks})
    if skill_p.is_dir():
        shutil.copytree(skill_p, out / "skills-worklog")
        shutil.rmtree(skill_p)
        man["steps"].append({"step": "skill", "moved": True})
    if md_p.is_file():
        shutil.copy2(md_p, out / "CLAUDE.md")
        if has_block:
            md_p.write_text(strip_block(md)[0], encoding="utf-8")
        man["steps"].append({"step": "claude_md", "removed_block": has_block})
    (out / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    print(f"done. manifest: {out / 'manifest.json'}")
    print("next: install the plugin, run /cg:setup with this data home, apply your local overlay.")
    print(f"undo: python {REPO / 'scripts' / 'rollback_cutover.py'}")
    return 0


def latest_manifest() -> Path | None:
    root = user_dir() / "backups"
    found = sorted(root.glob("cutover-*/manifest.json")) if root.is_dir() else []
    return found[-1] if found else None


def rollback(args) -> int:
    mp = Path(args.manifest) if args.manifest else latest_manifest()
    if not mp or not mp.is_file():
        print("no cutover manifest found", file=sys.stderr)
        return 2
    man = json.loads(mp.read_text(encoding="utf-8"))
    out, home, cdir = mp.parent, Path(man["home"]), Path(man["claude_dir"])
    print(f"rolling back the cutover of {man['created']} ({out})")
    env = dict(os.environ, CHITRAGUPTA_HOME=str(home))
    if cdir.resolve() != (Path.home() / ".claude").resolve():
        env["CHITRAGUPTA_CLAUDE_DIR"] = str(cdir)  # Chitragupta's own override; never CLAUDE_CONFIG_DIR here
    r = subprocess.run([sys.executable, "-B", str(REPO / "hooks" / "run.py"), "uninstall"], env=env,
                       capture_output=True, text=True)
    print("chitragupta uninstall:", "ok" if r.returncode == 0 else "rc %d %s" % (r.returncode, r.stderr.strip()[:300]))
    for name, target in (("settings.json", cdir / "settings.json"), ("CLAUDE.md", cdir / "CLAUDE.md"),
                         ("config.json", home / ".worklog" / "config.json")):
        if (out / name).is_file():
            shutil.copy2(out / name, target)
            print("restored", target)
    if (out / "skills-worklog").is_dir():
        dst = cdir / "skills" / "worklog"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(out / "skills-worklog", dst)
        print("restored", dst)
    if (out / f"{man['task']}.xml").is_file() and os.name == "nt":
        q = schtasks("/query", "/tn", man["task"])
        r = schtasks("/change", "/tn", man["task"], "/enable") if q.returncode == 0 else \
            schtasks("/create", "/tn", man["task"], "/xml", str(out / f"{man['task']}.xml"), "/f")
        print(f"task {man['task']}:", "enabled" if r.returncode == 0 else (r.stderr or r.stdout).strip())
    print("data home zip kept at", out / "data-home.zip", "(not restored automatically)")
    print("finally: claude plugin uninstall cg@chitragupta")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--home", required=True)
    a.add_argument("--claude-dir")
    a.add_argument("--task", default="WorklogAgent")
    a.add_argument("--backup-root")
    a.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("rollback")
    r.add_argument("--manifest")
    args = ap.parse_args(argv)
    return apply(args) if args.cmd == "apply" else rollback(args)


if __name__ == "__main__":
    sys.exit(main())
