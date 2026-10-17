"""Parity check: legacy worklog engine vs the chitragupta package on copies of one data home.

    python scripts/parity.py --old-engine <legacy .worklog/engine> --source <data home> --work <empty dir>

Both copies get: the git remote removed, MCP jobs and Slack switched off, an empty
transcripts dir, a simulated private repo, a frozen clock, and every model call
replaced by the same fixed recorded output per kind (taken from the source's
LLM answer cache, else a minimal schema-valid stub). Any other `claude` spawn
is blocked. Then each engine runs `tick --force`, renders the Jira worklog for
recent days and redacts a text corpus; the trees and outputs are compared byte
for byte and the differences are written to <work>/parity-report.md.
Nothing is written outside <work>.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "logs", "engine", "__pycache__", "sandbox", "slack-sender"}
LEGACY_DB_KEY = "d" + "pilot"  # legacy config key of the original worklog engine


def copy_home(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("engine", "__pycache__", "*.tmp", "*.lock"))
    if (dst / ".git").exists():
        subprocess.run(["git", "-C", str(dst), "remote", "remove", "origin"], capture_output=True)
    cfg_path = dst / ".worklog" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["github_remote"] = ""
    cfg["branch_fetch"] = False
    for key in ("jira", "database", LEGACY_DB_KEY):
        if isinstance(cfg.get("mcp", {}).get(key), dict):
            cfg["mcp"][key]["enabled"] = False
    cfg.setdefault("storage", {})["local_backup_dir"] = ""
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def stub_from_schema(schema: dict):
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), t[0])
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        props = schema.get("properties", {})
        return {k: stub_from_schema(props[k]) for k in schema.get("required", []) if k in props}
    return {"array": [], "string": "", "integer": 0, "number": 0, "boolean": False}.get(t)


def recorded_outputs(source: Path, work: Path) -> Path:
    kinds = sorted(p.stem for p in (source / ".worklog" / "prompts" / "schemas").glob("*.json"))
    out = {}
    for p in sorted((source / ".worklog" / "cache" / "llm").glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        out.setdefault(rec.get("kind"), rec.get("out"))
    for k in kinds:
        if out.get(k) is None:
            schema = json.loads((source / ".worklog" / "prompts" / "schemas" / f"{k}.json").read_text(encoding="utf-8"))
            out[k] = stub_from_schema(schema)
    path = work / "recorded.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


RUNNER = r'''
import copy, datetime as dt, hashlib, io, json, os, sys, contextlib
which, engine, recorded, clock, out_dir, corpus = sys.argv[1:7]
if which == "old":
    sys.path.insert(0, engine)
    import common, extract, agent, redact, jiraworklog
else:
    sys.path.insert(0, engine)
    from chitragupta import common, extract, agent, redact, jiraworklog
REC = json.load(open(recorded, encoding="utf-8"))
FIXED = dt.datetime.fromisoformat(clock)
common.now = lambda: FIXED
calls = open(os.path.join(out_dir, "calls.jsonl"), "w", encoding="utf-8")
def fake(kind, stdin_text, cfg, timeout=300, **kw):
    calls.write(json.dumps({"kind": kind, "stdin_sha1": hashlib.sha1(stdin_text.encode("utf-8")).hexdigest()}) + "\n")
    return copy.deepcopy(REC.get(kind) or {}), 0.0
extract.call_claude = fake
real_run = common.run
def guarded(args, *a, **k):
    if args and os.path.basename(str(args[0])).lower().startswith("claude"):
        return 127, "", "blocked by parity harness"
    return real_run(args, *a, **k)
common.run = guarded
rc = agent.main(["tick", "--force"])
calls.flush()
res = {"tick_rc": rc, "worklog": {}, "redact": {}}
cfg = common.load_config()
for back in range(0, 4):
    day = (FIXED - dt.timedelta(days=back)).date().isoformat()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            agent.main(["worklog-show", day])
        except SystemExit:
            pass
        except Exception as e:
            print("ERROR", type(e).__name__, e)
    res["worklog"][day] = buf.getvalue()
corpus_root = os.path.dirname(corpus)
for p in sorted(open(corpus, encoding="utf-8").read().splitlines()):
    try:
        text = open(p, encoding="utf-8").read()
    except OSError:
        continue
    for mode in ("local", "secrets", "public"):
        res["redact"]["%s|%s" % (mode, os.path.relpath(p, corpus_root))] = hashlib.sha1(
            redact.redact(text, mode=mode).encode("utf-8")).hexdigest()
json.dump(res, open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8"), indent=1, sort_keys=True)
'''


def run_engine(which: str, engine: str, home: Path, work: Path, recorded: Path, clock: str, corpus: Path) -> Path:
    """Run one engine with its copy moved to the shared path <work>/home, so absolute
    paths (and ids hashed from them) are the same for both engines; then move it back."""
    shared = work / "home"
    home.rename(shared)
    try:
        return _run_engine(which, engine, shared, work, recorded, clock, corpus)
    finally:
        shared.rename(home)


def _run_engine(which: str, engine: str, home: Path, work: Path, recorded: Path, clock: str, corpus: Path) -> Path:
    out = work / f"out-{which}"
    out.mkdir()
    (work / "transcripts").mkdir(exist_ok=True)
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("WORKLOG_", "CHITRAGUPTA_")):
            del env[k]
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "CHITRAGUPTA_INTERNAL": "1", "WORKLOG_INTERNAL": "1",
        "WORKLOG_TRANSCRIPTS_DIR": str(work / "transcripts"),
        "WORKLOG_PRIVACY_SIMULATE": "private",
        "WORKLOG_NO_MCP_JOBS": "1", "WORKLOG_SLACK_FAIL": "1",
        "CHITRAGUPTA_USER_DIR": str(work / f"user-{which}"),
    })
    env["WORKLOG_HOME" if which == "old" else "CHITRAGUPTA_HOME"] = str(home)
    runner = work / "runner.py"
    runner.write_text(RUNNER, encoding="utf-8")
    # -B: never write __pycache__ next to the legacy engine (it lives in the real data home).
    p = subprocess.run([sys.executable, "-B", "-s", str(runner), which, engine,
                        str(recorded), clock, str(out), str(corpus)],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    (out / "stdout.txt").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr, encoding="utf-8")
    if p.returncode != 0:
        print(f"[{which}] runner exited {p.returncode}; see {out / 'stdout.txt'}", file=sys.stderr)
    return out


def tree(root: Path) -> dict[str, str]:
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith("chitragupta-migrate-")]
        for f in filenames:
            p = Path(dirpath) / f
            files[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return files


def diff_text(a: Path, b: Path, limit=40) -> str:
    try:
        la = a.read_text(encoding="utf-8").splitlines()
        lb = b.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return "(binary or unreadable)"
    d = list(difflib.unified_diff(la, lb, "old", "new", n=1, lineterm=""))
    return "\n".join(d[:limit]) + ("\n…" if len(d) > limit else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--old-engine", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--clock", help="frozen ISO time (default: now, to the minute)")
    ap.add_argument("--overlay", help="JSON merged into the new copy's config after migrate")
    args = ap.parse_args(argv)
    source, work = Path(args.source).resolve(), Path(args.work).resolve()
    if work.exists() and any(work.iterdir()):
        ap.error(f"{work} must be empty or absent")
    work.mkdir(parents=True, exist_ok=True)
    clock = args.clock or dt.datetime.now().astimezone().replace(second=0, microsecond=0).isoformat()

    old_home, new_home = work / "home-old", work / "home-new"
    copy_home(source, old_home)
    copy_home(source, new_home)
    recorded = recorded_outputs(source, work)
    corpus = work / "corpus.txt"
    texts = [p for sub in ("daily", "tickets", "knowledge", "bruno", "_drop") for p in sorted((source / sub).rglob("*"))
             if p.is_file() and p.suffix.lower() in {".md", ".bru", ".sh", ".json", ".txt", ".sql", ".html"}]
    corpus.write_text("\n".join(str(p) for p in texts), encoding="utf-8")

    mig = subprocess.run([sys.executable, "-B", "-m", "chitragupta", "migrate"], cwd=REPO,
                         env={**os.environ, "CHITRAGUPTA_HOME": str(new_home),
                              "CHITRAGUPTA_USER_DIR": str(work / "user-new")},
                         capture_output=True, text=True)
    # A stable runtime for the new engine, as setup installs it (git pre-commit hook, replay.sh use it).
    runtime = work / "user-new" / "runtime"
    shutil.copytree(REPO / "chitragupta", runtime / "dev" / "chitragupta",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(REPO / "hooks" / "run.py", runtime / "run.py")
    (runtime / "current").write_text("dev", encoding="utf-8")
    if args.overlay:  # the user's local values for keys that replaced hardcoded literals
        cfg_path = new_home / ".worklog" / "config.json"
        cfg = deep_merge(json.loads(cfg_path.read_text(encoding="utf-8")),
                         json.loads(Path(args.overlay).read_text(encoding="utf-8")))
        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    out_old = run_engine("old", str(Path(args.old_engine).resolve()), old_home, work, recorded, clock, corpus)
    out_new = run_engine("new", str(REPO), new_home, work, recorded, clock, corpus)

    lines = [f"# Parity report", "", f"- clock: `{clock}`", f"- migrate rc: {mig.returncode}"]
    ta, tb = tree(old_home), tree(new_home)
    only_old = sorted(set(ta) - set(tb))
    only_new = sorted(set(tb) - set(ta))
    changed = sorted(k for k in set(ta) & set(tb) if ta[k] != tb[k])
    same = len(set(ta) & set(tb)) - len(changed)
    lines += [f"- files identical: {same}", f"- differ: {len(changed)}", f"- only old: {len(only_old)}",
              f"- only new: {len(only_new)}", ""]
    for title, items in (("Only in old", only_old), ("Only in new", only_new)):
        if items:
            lines += [f"## {title}", *[f"- `{k}`" for k in items], ""]
    if changed:
        lines.append("## Differences")
        for k in changed:
            lines += ["", f"### `{k}`", "```diff", diff_text(old_home / k, new_home / k), "```"]
    trees = []
    for h in (old_home, new_home):
        r = subprocess.run(["git", "-C", str(h), "ls-tree", "-r", "HEAD"], capture_output=True, text=True)
        trees.append(r.stdout)
    lines.append("- committed tree (`git ls-tree -r HEAD`): " + ("identical" if trees[0] == trees[1] else "DIFFERENT"))
    if trees[0] != trees[1]:
        lines += ["```diff", "\n".join(list(difflib.unified_diff(trees[0].splitlines(), trees[1].splitlines(),
                                                                 "old", "new", lineterm=""))[:40]), "```"]
    for name in ("calls.jsonl", "result.json"):
        a, b = out_old / name, out_new / name
        if a.exists() and b.exists() and a.read_bytes() == b.read_bytes():
            lines.append(f"- `{name}`: identical")
        else:
            lines += [f"- `{name}`: DIFFERENT", "```diff", diff_text(a, b), "```"]
    report = work / "parity-report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"identical {same}, differ {len(changed)}, only-old {len(only_old)}, only-new {len(only_new)} -> {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
