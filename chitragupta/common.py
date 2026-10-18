"""Shared helpers: paths, config, atomic I/O, work-day maths, ticket detection, git HEAD.

Kept import-light because hook.py loads it on every Claude Code event.
"""
import datetime as _dt
import hashlib
import json
import os
import re
import time

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = PACKAGE_DIR  # legacy name, kept for callers
PROMPTS_DIR = os.path.join(PACKAGE_DIR, "prompts")


def user_dir():
    """Per-user runtime and settings folder (%USERPROFILE%\\.chitragupta)."""
    return os.path.abspath(os.environ.get("CHITRAGUPTA_USER_DIR")
                           or os.path.join(os.path.expanduser("~"), ".chitragupta"))


DEFAULT_HOME = os.path.join(os.path.expanduser("~"), "Chitragupta")


def home_pointer():
    return os.path.join(user_dir(), "home")


def home():
    """Data home: $CHITRAGUPTA_HOME, else the pointer file, else ~/Chitragupta."""
    h = os.environ.get("CHITRAGUPTA_HOME")
    if not h:
        try:
            with open(home_pointer(), encoding="utf-8-sig") as f:
                h = f.read().strip()
        except OSError:
            h = ""
    return os.path.abspath(h or DEFAULT_HOME)


def env(name, default=None):
    """CHITRAGUPTA_<name>, falling back to the legacy WORKLOG_<name>."""
    v = os.environ.get("CHITRAGUPTA_" + name)
    if v is None:
        v = os.environ.get("WORKLOG_" + name)
    return default if v is None else v


def internal():
    """True inside the engine's own child processes (claude -p, git): hooks must ignore them."""
    return os.environ.get("CHITRAGUPTA_INTERNAL") == "1" or os.environ.get("WORKLOG_INTERNAL") == "1"


# Set on every child process. Both names until the old worklog hooks are retired.
INTERNAL_ENV = {"CHITRAGUPTA_INTERNAL": "1", "WORKLOG_INTERNAL": "1"}


def prompt_path(name):
    """A prompt or schema file: a user override in <home>/.worklog/prompts wins over the packaged one.
    `name` is relative to the prompts folder, e.g. "extract.md" or "schemas/extract.json"."""
    override = wl("prompts", *name.split("/"))
    if os.path.isfile(override):
        return override
    return os.path.join(PROMPTS_DIR, *name.split("/"))


def wl(*parts):
    """Path inside <home>/.worklog."""
    return os.path.join(home(), ".worklog", *parts)


def hp(*parts):
    """Path inside <home>."""
    return os.path.join(home(), *parts)


def norm_path(p):
    """Comparable form of a path: forward slashes, lower case, no trailing slash."""
    if not p:
        return ""
    p = os.path.abspath(p).replace("\\", "/").rstrip("/")
    return p.lower()


def path_within(child, parent):
    c, p = norm_path(child), norm_path(parent)
    return bool(p) and (c == p or c.startswith(p + "/"))


# --------------------------------------------------------------------------- config

DEFAULT_CONFIG = {
    "worklog_home": "",
    "github_remote": "",
    "ticket_prefixes": [],
    "capture_scope": {"include_paths": [], "exclude_paths": []},
    "watched_repos": [],
    "git_author_emails": [],
    "bruno_collections": [],
    "sql_script_dirs": [],
    "sql_script_globs": [],
    "env_branches": ["qa", "uat", "staging", "master", "main", "develop"],
    "branch_fetch": False,
    "day_starts_at": "04:00",
    "models": {"extract": "haiku", "consolidate": "sonnet", "curate": "sonnet", "dbflow": "sonnet"},
    "artifacts": {"watch_dirs": [], "push_binary_types": [], "text_max_bytes": 2000000, "binary_max_bytes": 25000000},
    "extract_triggers": {"min_new_chars": 1500, "idle_minutes": 10, "max_interval_minutes": 20},
    "push_interval_minutes": 30,
    "raw_retention_days": 30,
    "paths": {"python": "", "pythonw": "", "claude": "", "git": ""},
    # fetch of remote-tracking refs in watched repos (precise merge status); false = no writes inside .git at all
    "git": {"allow_fetch": True},
    "extra_sensitive_patterns": [],
    "allow_patterns": [],
    # Example ticket key shown in generated text (daily "unfiled artifacts" hint, README examples).
    "render": {"example_ticket": "ABC-1234"},
    "reminders": {"mcp": True},  # weekly SessionStart line when Slack/Jira isn't connected
    # MCP jobs are off until configured; the database MCP's server name comes only from the user's config.
    "mcp": {"daily_call_budget": 10, "jira": {"enabled": False}, "database": {"enabled": False, "label": "database MCP"}},
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    try:
        with open(wl("config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg = _merge(json.loads(json.dumps(DEFAULT_CONFIG)), cfg)  # deep copy: callers may mutate cfg
    if not cfg.get("worklog_home"):
        cfg["worklog_home"] = home().replace("\\", "/")
    return cfg


def save_config(cfg, source="engine", base=None):
    """Save config changes through the one config write path (configstore: lock, backup, history). With `base` (the
    config as loaded) only the keys that changed since are written, onto the current file, so a change made
    elsewhere in the meantime (the dashboard, setup) is never overwritten."""
    from . import configstore
    if base is None:
        raw, _ = configstore.read_raw()
        return configstore.write(cfg, raw, source)
    diff = configstore.flat_diff(base, cfg)
    return configstore.update({d["key"]: d["new"] for d in diff}, source) if diff else None


# --------------------------------------------------------------------------- atomic I/O

def ensure_dir(p):
    from .safety import paths
    paths.makedirs(p)


def write_text(path, text):
    """Atomic UTF-8 (no BOM) write with LF newlines, through the path guard. Returns True if content changed."""
    from .safety import paths
    return paths.write_text(path, text)


def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def write_json(path, obj):
    return write_text(path, json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def append_line(path, text):
    from .safety import paths
    paths.append_text(path, text.rstrip("\n") + "\n")


def log(name, msg, max_bytes=1_000_000, keep=5):
    """Append to .worklog/logs/<name>.log with simple size rotation."""
    path = wl("logs", name + ".log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            from .safety import paths
            for i in range(keep - 1, 0, -1):
                src = "%s.%d" % (path, i)
                if os.path.exists(src):
                    paths.replace(src, "%s.%d" % (path, i + 1))
            paths.replace(path, path + ".1")
        append_line(path, "%s %s" % (now_iso(), msg))
    except OSError:
        pass


# --------------------------------------------------------------------------- time

def now():
    return _dt.datetime.now().astimezone()


def now_iso():
    return now().isoformat(timespec="seconds")


def parse_ts(s):
    """ISO timestamp -> aware local datetime (None on failure)."""
    if not s:
        return None
    try:
        s = str(s).replace("Z", "+00:00")
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.astimezone()
        return d.astimezone()
    except ValueError:
        return None


def day_offset(cfg):
    try:
        hh, mm = (cfg.get("day_starts_at") or "04:00").split(":")
        return _dt.timedelta(hours=int(hh), minutes=int(mm))
    except ValueError:
        return _dt.timedelta(hours=4)


def workday(dt, cfg):
    """Work day (YYYY-MM-DD) a moment belongs to; work before day_starts_at counts as the previous day."""
    if isinstance(dt, str):
        dt = parse_ts(dt)
    if dt is None:
        dt = now()
    return (dt.astimezone() - day_offset(cfg)).date().isoformat()


def day_bounds(day, cfg):
    """[start, end) of a work day as aware datetimes."""
    d = _dt.date.fromisoformat(day)
    tz = now().tzinfo
    start = _dt.datetime.combine(d, _dt.time(), tzinfo=tz) + day_offset(cfg)
    return start, start + _dt.timedelta(days=1)


def hhmm(ts):
    d = parse_ts(ts) if isinstance(ts, str) else ts
    return d.strftime("%H:%M") if d else ""


def fmt_minutes(m):
    m = int(round(m or 0))
    if m < 60:
        return "%dm" % m
    return "%dh %02dm" % (m // 60, m % 60)


# --------------------------------------------------------------------------- tickets

# Uppercase look-alikes that are never tickets.
TICKET_DENY = {
    "UTF", "SHA", "ISO", "HTTP", "HTTPS", "TLS", "SSL", "RFC", "AES", "RSA", "MD", "UTC", "GMT", "CVE",
    "X", "V", "HL", "PID", "OBX", "OBR", "MSH", "PV", "ORC", "NK", "IN", "GT", "AL", "DG", "NTE", "EVN",
    "SPM", "ES", "ECMA", "WIN", "JDK", "JRE", "JAVA", "PY", "NODE", "IPV", "TCP", "UDP", "PORT", "COVID",
    "SQL", "API", "LTS", "RC", "GPT", "CLAUDE", "OPUS", "SONNET", "HAIKU", "FABLE", "TODO", "STEP", "PHASE",
    "GATE", "TEST", "ROUND", "PR", "ID", "IDS", "WEEK", "DAY", "Q", "FY", "H", "LEVEL", "TIER",
}
_TICKET_ANY = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9})-(\d{1,6})(?![A-Za-z0-9])")
_TICKET_ANY_I = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,9})-(\d{1,6})(?![A-Za-z0-9])")


def find_tickets(text, prefixes):
    """Ticket keys in free text, limited to configured prefixes, in order of appearance (unique)."""
    if not text or not prefixes:
        return []
    allowed = {p.upper() for p in prefixes}
    out = []
    for m in _TICKET_ANY.finditer(text):
        p = m.group(1)
        if p in allowed and p not in TICKET_DENY:
            key = "%s-%s" % (p, int(m.group(2)))
            if key not in out:
                out.append(key)
    return out


def ticket_from_branch(branch, prefixes):
    """Ticket key from a branch name. Configured prefixes match case-insensitively;
    other UPPERCASE prefixes are accepted too (and later auto-added to config)."""
    if not branch:
        return None
    allowed = {p.upper() for p in prefixes or []}
    for m in _TICKET_ANY_I.finditer(branch):
        p = m.group(1).upper()
        if p in TICKET_DENY or p[0].isdigit():
            continue
        if p in allowed or m.group(1).isupper():
            return "%s-%s" % (p, int(m.group(2)))
    return None


def new_branch_prefix(branch, prefixes):
    t = ticket_from_branch(branch, prefixes)
    if t:
        p = t.split("-")[0]
        if p not in {x.upper() for x in prefixes or []}:
            return p
    return None


# --------------------------------------------------------------------------- git (no subprocess)

def find_repo_root(cwd):
    """Walk up from cwd to the directory containing .git (file or folder)."""
    try:
        d = os.path.abspath(cwd)
    except (TypeError, ValueError):
        return None
    for _ in range(40):
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def _git_dir(repo_root):
    g = os.path.join(repo_root, ".git")
    if os.path.isdir(g):
        return g
    txt = read_text(g).strip()
    if txt.startswith("gitdir:"):
        p = txt[7:].strip()
        if not os.path.isabs(p):
            p = os.path.join(repo_root, p)
        return os.path.normpath(p)
    return None


def read_branch(repo_root):
    """Current branch from .git/HEAD (follows worktree gitdir). Detached -> short sha."""
    if not repo_root:
        return None
    gd = _git_dir(repo_root)
    if not gd:
        return None
    head = read_text(os.path.join(gd, "HEAD")).strip()
    if head.startswith("ref:"):
        ref = head[4:].strip()
        return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    return ("detached@" + head[:8]) if head else None


# --------------------------------------------------------------------------- processes

# Processes start only through chitragupta.safety.commands.run(argv, purpose).

def launcher():
    """Stable launcher (~/.chitragupta/runtime/run.py). Anything persisted outside the engine
    (scheduled task, git pre-commit hook, MCP guard hook, replay.sh) calls this, never a
    versioned plugin path, because the plugin folder moves on every update."""
    return os.path.join(user_dir(), "runtime", "run.py")


def python_exe(cfg=None, windowless=False):
    import sys
    paths = (cfg or load_config()).get("paths") or {}
    exe = paths.get("pythonw" if windowless else "python") or sys.executable
    if windowless and not paths.get("pythonw") and os.name == "nt":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = w if os.path.exists(w) else exe
    return exe


def self_command(module, *args, cfg=None, windowless=False):
    """argv that runs `chitragupta.<module>` through the stable launcher."""
    return [python_exe(cfg, windowless), launcher(), module, *[str(a) for a in args]]


def git_exe(cfg=None):
    cfg = cfg or load_config()
    return (cfg.get("paths") or {}).get("git") or "git"


# --------------------------------------------------------------------------- misc

def stable_id(*parts):
    s = "|".join(normalize_title(str(p)) for p in parts)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]


def normalize_title(s):
    s = (s or "").lower()
    s = re.sub(r"[`*_\"'()\[\]{}.,:;!?]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def truncate(s, n):
    s = s or ""
    return s if len(s) <= n else s[: max(0, n - 20)] + " …[truncated]"


def scope_allows(cwd, cfg):
    """Capture-scope check shared by hooks and the transcript safety net."""
    if not cwd:
        return False
    if path_within(cwd, home()):
        return False
    scope = cfg.get("capture_scope") or {}
    for p in scope.get("exclude_paths") or []:
        if path_within(cwd, p):
            return False
    inc = scope.get("include_paths") or []
    if inc and not any(path_within(cwd, p) for p in inc):
        return False
    return True


def repo_ignored(repo_root):
    return bool(repo_root) and os.path.exists(os.path.join(repo_root, ".worklog-ignore"))
