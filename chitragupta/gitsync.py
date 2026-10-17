"""Git sync for the Worklog repo: private-repo guard, redact-checked commits, safe pushes.
Fully non-interactive (GIT_TERMINAL_PROMPT=0, credentials from Git Credential Manager)."""
import json
import os
import re
import urllib.error
import urllib.request

from . import common
from . import redact

# Allowlist: only the daily work is pushed; everything else is a local knowledge base (upgrade 02).
PUSH_PATHS = ("daily/", "README.md", ".gitignore", ".gitattributes")
GITIGNORE = """/*
!/daily/
!/README.md
!/.gitignore
!/.gitattributes
*.tmp
__pycache__/
*.pyc
"""

GITATTRIBUTES = "* text=auto eol=lf\n"

PRE_COMMIT = """#!/bin/sh
# Worklog pre-commit: only daily work may be committed (allowlist), and it must pass the public redaction pass.
exec {command}
"""


def _sh_word(a):
    """One argv word for a POSIX sh line: plain words as-is, anything else double-quoted."""
    a = str(a).replace("\\", "/")
    if re.fullmatch(r"[A-Za-z0-9_.-]+", a):
        return a
    return '"%s"' % "".join("\\" + c if c in '"$`\\' else c for c in a)


def push_paths(cfg):
    return tuple((cfg.get("storage") or {}).get("push_paths") or PUSH_PATHS)


def allowed_path(rel, paths=PUSH_PATHS):
    r = rel.replace("\\", "/")
    return any(r == p or (p.endswith("/") and r.startswith(p)) for p in paths)


def git(args, cfg, timeout=120):
    return common.run([common.git_exe(cfg), "-C", common.home()] + args, timeout=timeout)


def parse_github(url):
    m = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$", (url or "").strip())
    return (m.group(1), m.group(2)) if m else None


def remote_url(cfg):
    url = (cfg.get("github_remote") or "").strip()
    if parse_github(url) and url.startswith("https://") and not url.endswith(".git"):
        url = url.rstrip("/") + ".git"
    return url


def ensure_repo(cfg):
    h = common.home()
    common.write_text(os.path.join(h, ".gitignore"), GITIGNORE)
    common.write_text(os.path.join(h, ".gitattributes"), GITATTRIBUTES)
    if not os.path.isdir(os.path.join(h, ".git")):
        rc, so, se = git(["init", "-b", "main"], cfg)
        if rc != 0:
            raise RuntimeError("git init failed: " + se)
    git(["config", "core.autocrlf", "false"], cfg)
    git(["config", "core.safecrlf", "false"], cfg)
    url = remote_url(cfg)
    if url:
        rc, cur, _ = git(["remote", "get-url", "origin"], cfg)
        if rc != 0:
            git(["remote", "add", "origin", url], cfg)
        elif cur.strip() != url:
            git(["remote", "set-url", "origin", url], cfg)
    install_pre_commit(cfg)


def install_pre_commit(cfg):
    command = " ".join(_sh_word(a) for a in common.self_command("redact", "--check-staged", cfg=cfg))
    path = os.path.join(common.home(), ".git", "hooks", "pre-commit")
    common.write_text(path, PRE_COMMIT.format(command=command))
    try:  # git skips hooks that aren't executable (no-op on Windows)
        os.chmod(path, 0o755)
    except OSError:
        pass


# --------------------------------------------------------------------------- privacy guard

def privacy_status(cfg):
    """'private' | 'public' | 'unknown'. Local (non-GitHub) remotes count as private."""
    sim = common.env("PRIVACY_SIMULATE")
    if sim in ("public", "private", "unknown"):
        return sim
    url = cfg.get("github_remote") or ""
    gh = parse_github(url)
    if not gh:
        if url and (os.path.isdir(url) or url.startswith("file://")):
            return "private"
        return "unknown"
    req = urllib.request.Request("https://api.github.com/repos/%s/%s" % gh,
                                 headers={"User-Agent": "worklog-privacy-check", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return "public" if r.status == 200 else "unknown"
    except urllib.error.HTTPError as e:
        return "private" if e.code == 404 else "unknown"
    except Exception:
        return "unknown"


def check_privacy(cfg, health, force=False):
    last = common.parse_ts(health.get("privacy_checked"))
    due = force or not last or (common.now() - last).total_seconds() > 24 * 3600 or health.get("privacy") != "private"
    if not due:
        return health.get("privacy")
    st = privacy_status(cfg)
    if st != "unknown" or not health.get("privacy"):
        health["privacy"] = st
    health["privacy_checked"] = common.now_iso()
    if st == "public":
        common.log("agent", "PRIVACY: repo is public - pushes blocked")
    return health.get("privacy")


# --------------------------------------------------------------------------- commit / push

def changed_files(cfg):
    rc, so, _ = git(["status", "--porcelain", "-z", "--untracked-files=all"], cfg)
    if rc != 0:
        return []
    out, parts, i = [], so.split("\0"), 0
    while i < len(parts):
        p = parts[i]
        if len(p) > 3:
            status, path = p[:2], p[3:]
            if status.startswith("R"):
                i += 1  # skip rename source
            out.append(path)
        i += 1
    return out


HELD_PREFIXES = ("bruno/",)


def held_files(paths, health):
    """Your own Bruno requests are never rewritten by the scanner: a .bru holding a literal secret or person value
    is left out of the commit (and reported) until it uses {{process.env.NAME}} instead."""
    held = []
    for rel in paths:
        p = os.path.join(common.home(), rel)
        if rel.startswith(HELD_PREFIXES) and os.path.isfile(p) and redact.should_scan(rel) and redact.file_needs_fix(p):
            held.append(rel)
    health["held_back"] = held
    if held:
        common.log("agent", "held back from commit (literal sensitive values): %s" % held)
    return held


def scan_and_mask(paths, health):
    """Mask sensitive text in files about to be committed. Returns list of masked files."""
    masked = []
    for rel in paths:
        if rel.startswith(".worklog/engine/tests/") or rel.startswith(HELD_PREFIXES):
            continue  # test fixtures intentionally contain fake secrets; bruno/ files are held, not rewritten
        p = os.path.join(common.home(), rel)
        if not os.path.isfile(p) or not redact.should_scan(rel):
            continue
        stats = {}
        mode = redact.mode_for(rel)
        if redact.file_needs_fix(p, mode) and redact.fix_file(p, stats, mode):
            masked.append(rel)
            common.log("agent", "masked before commit: %s %s" % (rel, stats))
    if masked:
        health["masked_before_commit"] = int(health.get("masked_before_commit") or 0) + len(masked)
    return masked


def commit(cfg, message, health):
    files = [f for f in changed_files(cfg) if allowed_path(f, push_paths(cfg))]
    if not files:
        return False
    held = held_files(files, health)
    scan_and_mask(files, health)
    git(["add", "-A"], cfg)
    if held:
        git(["reset", "-q", "--"] + held, cfg)
    for attempt in range(2):
        rc, so, se = git(["commit", "-q", "-m", message], cfg)
        if rc == 0:
            health["last_commit"] = common.now_iso()
            return True
        if "nothing to commit" in (so + se):
            return False
        # pre-commit blocked: mask whatever it named, re-stage, retry
        bad = [l.strip() for l in se.splitlines() if l.startswith("  ")]
        scan_and_mask(bad or files, health)
        git(["add", "-A"], cfg)
        held = held_files(changed_files(cfg), health)
        if held:
            git(["reset", "-q", "--"] + held, cfg)
    common.log("agent", "commit failed: " + (se or so).strip()[:400])
    return False


def unpushed(cfg):
    rc, so, _ = git(["rev-list", "--count", "origin/main..main"], cfg)
    if rc == 0:
        return int(so.strip() or 0)
    rc, so, _ = git(["rev-list", "--count", "main"], cfg)  # never pushed
    return int(so.strip() or 0) if rc == 0 else 0


def push(cfg, health):
    """Pull --rebase then push to the configured remote only. Never force."""
    url = remote_url(cfg)
    if not url:
        return False
    rc, cur, _ = git(["remote", "get-url", "origin"], cfg)
    if rc != 0 or cur.strip() != url:
        common.log("agent", "push refused: origin is %r, configured %r" % (cur.strip(), url))
        return False
    if check_privacy(cfg, health) != "private":
        health["push_blocked"] = health.get("privacy")
        return False
    health.pop("push_blocked", None)
    rc, so, se = git(["ls-remote", "--heads", "origin", "main"], cfg, timeout=60)
    if rc != 0:
        return _push_failed(health, "ls-remote: " + se)
    if so.strip():
        rc, so2, se2 = git(["fetch", "-q", "origin", "main"], cfg, timeout=120)
        if rc != 0:
            return _push_failed(health, "fetch: " + se2)
        rc, base, _ = git(["merge-base", "main", "origin/main"], cfg)
        if rc != 0:  # unrelated history (remote created with README/licence)
            rc, _, se3 = git(["merge", "-q", "--allow-unrelated-histories", "-X", "ours", "-m",
                              "worklog: join existing remote history", "origin/main"], cfg)
        else:
            rc, _, se3 = git(["rebase", "-q", "origin/main"], cfg, timeout=120)
            if rc != 0:
                git(["rebase", "--abort"], cfg)
                rc, _, se3 = git(["merge", "-q", "-X", "ours", "-m", "worklog: merge remote", "origin/main"], cfg)
        if rc != 0:
            git(["merge", "--abort"], cfg)
            return _push_failed(health, "integrate remote: " + se3)
    rc, so, se = git(["push", "-q", "-u", "origin", "main"], cfg, timeout=180)
    if rc != 0:
        return _push_failed(health, "push: " + se)
    health["last_push"] = common.now_iso()
    health.pop("push_failing_since", None)
    health.pop("last_push_error", None)
    return True


def _push_failed(health, msg):
    common.log("agent", "push failed: " + msg.strip()[:400])
    health.setdefault("push_failing_since", common.now_iso())
    health["last_push_error"] = msg.strip()[:300]
    return False


def commit_message(day, delta, extra=""):
    parts = []
    for key in sorted(delta):
        d = delta[key]
        bits = []
        for what, label1, labeln in (("findings", "finding", "findings"), ("updates", "update", "updates"),
                                     ("queries", "query", "queries"), ("APIs", "API", "APIs"), ("todos", "todo", "todos")):
            n = d.get(what) or 0
            if n:
                bits.append("+%d %s" % (n, label1 if n == 1 else labeln))
        if bits:
            parts.append("%s %s" % (key, " ".join(bits)))
    msg = "worklog(%s): %s" % (day, " · ".join(parts[:6]) if parts else (extra or "update"))
    if parts and extra:
        msg += " · " + extra
    return msg[:200]
