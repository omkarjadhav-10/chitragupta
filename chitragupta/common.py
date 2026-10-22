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

DEFAULT_EXAMPLE_TICKET = "ABC-1234"  # the built-in example key (a placeholder only when listed in placeholder_tickets)

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
    "render": {"example_ticket": DEFAULT_EXAMPLE_TICKET},
    "reminders": {"mcp": True},  # weekly SessionStart line when Slack/Jira isn't connected
    # MCP jobs are off until configured; the database MCP's server name comes only from the user's config.
    # Jira issue types that only group work: they never get time or items themselves, their children do
    "jira": {"parent_issue_types": ["Epic", "Initiative"]},
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
    set_placeholders(cfg)
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


_KEY_RX = re.compile(r"^([A-Za-z][A-Za-z0-9]{1,9})-(\d{1,6})$")
_PLACEHOLDERS = frozenset()  # set by load_config(): the example keys of the loaded config


def ticket_key(s):
    """Canonical form of a ticket key (ABC-0042 -> ABC-42), or None."""
    m = _KEY_RX.match(str(s or "").strip())
    return "%s-%s" % (m.group(1).upper(), int(m.group(2))) if m else None


def placeholder_tickets(cfg):
    """Ticket keys that are examples, never work: the configured example tickets (render.example_ticket,
    jira_worklog.example_ticket) and the optional `placeholder_tickets` list. The built-in default example is a
    placeholder only when it is listed there. They are never detected as tickets and never get time, a hub or a
    Jira call."""
    cfg = cfg or {}
    out = set()
    for v in ((cfg.get("render") or {}).get("example_ticket"), (cfg.get("jira_worklog") or {}).get("example_ticket")):
        k = ticket_key(v)
        if k and k != DEFAULT_EXAMPLE_TICKET:
            out.add(k)
    lst = cfg.get("placeholder_tickets") or []
    for v in lst if isinstance(lst, list) else []:
        k = ticket_key(v)
        if k:
            out.add(k)
    return frozenset(out)


def set_placeholders(cfg):
    global _PLACEHOLDERS
    _PLACEHOLDERS = placeholder_tickets(cfg)


def is_placeholder(t):
    """True for a configured example ticket key (see placeholder_tickets) of the last loaded config."""
    return bool(t) and bool(_PLACEHOLDERS) and ticket_key(t) in _PLACEHOLDERS


def find_tickets(text, prefixes):
    """Ticket keys in free text, limited to configured prefixes, in order of appearance (unique). Configured
    example keys (placeholder_tickets) are never tickets."""
    if not text or not prefixes:
        return []
    allowed = {p.upper() for p in prefixes}
    out = []
    for m in _TICKET_ANY.finditer(text):
        p = m.group(1)
        if p in allowed and p not in TICKET_DENY:
            key = "%s-%s" % (p, int(m.group(2)))
            if key not in out and key not in _PLACEHOLDERS:
                out.append(key)
    return out


DEFAULT_PARENT_TYPES = ["Epic", "Initiative"]
_PASTED = re.compile(r"<pasted_content[^>]*>(.*?)(?:</pasted_content>|\Z)", re.S | re.I)
_CRUMB_SEP = re.compile(r"\s+[/>»›]\s+")
_CARD_CACHE = {}


def _crumb_line(line, prefixes):
    """The ticket ids of a breadcrumb line ("ABC-1000 / ABC-1001", "Spaces / Shop / ABC-1000 > ABC-1001", or a line
    that is only an id), else None."""
    s = line.strip()
    if not s:
        return None
    ids = find_tickets(s, prefixes)
    if not ids:
        return None
    if ticket_key(s) and ids == [ticket_key(s)]:
        return ids
    parts = _CRUMB_SEP.split(s)
    if len(parts) > 1 and all(len(p.split()) <= 4 for p in parts) and ticket_key(parts[-1].strip()):
        return ids
    return None


def _card(key):
    """data/jira/<KEY>.json "card" (cached per file version; {} when there is none)."""
    p = hp("data", "jira", key + ".json")
    try:
        st = os.stat(p)
        mt = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    hit = _CARD_CACHE.get(p)
    if hit and hit[0] == mt:
        return hit[1]
    card = read_json(p, {}).get("card") or {}
    card = card if isinstance(card, dict) else {}
    _CARD_CACHE[p] = (mt, card)
    return card


def parent_issue_types(cfg):
    lst = ((cfg or {}).get("jira") or {}).get("parent_issue_types")
    return [str(x) for x in (lst if isinstance(lst, list) else DEFAULT_PARENT_TYPES)]


def is_parent_ticket(key, cfg, others=()):
    """True when the ticket is a parent issue (an Epic, an Initiative…): its Jira card's type is one of
    jira.parent_issue_types, or another candidate's card names it as its parent. Parents never get time or items."""
    if not key:
        return False
    types = {t.lower() for t in parent_issue_types(cfg)}
    if str(_card(key).get("type") or "").lower() in types:
        return True
    for o in others:
        par = str(_card(o).get("parent") or "").strip().upper() if o != key else ""
        if par == key or par.startswith(key + " "):
            return True
    return False


def prompt_ticket(text, prefixes, cfg=None):
    """(ticket, src) a prompt is about, or (None, None). An id typed in your own words wins (src "prompt") over one in
    pasted text (`<pasted_content>` blocks and Jira header / breadcrumb lines; src "jira-title"). A breadcrumb gives its
    LAST id (the issue itself; the ones before it are its parents). Parent issue types never win (is_parent_ticket):
    the next candidate does, or nobody, so the branch or the carried ticket decides."""
    if not text or not prefixes:
        return None, None
    text = str(text)
    own, crumbs, pasted, parents = [], [], [], []

    def lines_of(block, is_pasted):
        run = []  # consecutive breadcrumb lines (blank lines between them allowed)

        def flush():
            if len(run) == 1 and not is_pasted:  # one id alone on a line of your own text: your words
                own.append(run[0])
            elif run:
                crumbs.append(run[-1])
                parents.extend(run[:-1])
            del run[:]
        for line in block.splitlines():
            ids = _crumb_line(line, prefixes)
            if ids:
                run.extend(i for i in ids if i not in run)
                continue
            if line.strip():
                flush()
                (pasted if is_pasted else own).extend(find_tickets(line, prefixes))
        flush()
    pos = 0
    for m in _PASTED.finditer(text):
        lines_of(text[pos:m.start()], False)
        lines_of(m.group(1), True)
        pos = m.end()
    lines_of(text[pos:], False)
    cands = []
    for k, src in [(k, "prompt") for k in own] + [(k, "jira-title") for k in crumbs + pasted + parents]:
        if k not in [c[0] for c in cands]:
            cands.append((k, src))
    keys = [c[0] for c in cands]
    for k, src in cands:
        if cfg is not None and is_parent_ticket(k, cfg, keys):
            continue
        return k, src
    return None, None


def breadcrumb_parents(text, prefixes):
    """{child: [parents]} from the breadcrumb runs of a prompt ("Shop / ABC-1000 / ABC-1001": ABC-1000 is the parent
    of ABC-1001, the run's last id), in your own text or pasted. A lone id is no breadcrumb."""
    out = {}
    if not text or not prefixes:
        return out
    run = []

    def flush():
        if len(run) > 1:
            out.setdefault(run[-1], [])
            out[run[-1]] += [p for p in run[:-1] if p not in out[run[-1]]]
        del run[:]
    for line in _PASTED.sub(lambda m: "\n" + m.group(1) + "\n", str(text)).splitlines():
        ids = _crumb_line(line, prefixes)
        if ids:
            run.extend(i for i in ids if i not in run)
        elif line.strip():
            flush()
    flush()
    return out


def work_branch_ticket(branch, cfg, repo=None):
    """A work branch's ticket. Base and environment branches (release/…, qa, uat, main…: the repo's branching bases,
    gitcmd.repo_conf) name no ticket of their own, and neither does a parent issue (is_parent_ticket)."""
    if not branch or branch.startswith("detached@"):
        return None
    if branch.split("/")[0].lower() == "release":
        return None
    from . import gitcmd
    bases = gitcmd.repo_conf(cfg, gitcmd.repo_key(repo) if repo else "")["bases"]
    if gitcmd.role_of(branch, bases):
        return None
    t = ticket_from_branch(branch, cfg.get("ticket_prefixes") or [])
    return None if t and is_parent_ticket(t, cfg) else t


def ticket_for_turn(cfg, prompt_t=None, prompt_src="prompt", branch=None, repo=None, own_repo=None, carry=None):
    """(ticket, src) for one turn (a prompt and every event until the next one), the single rule for time and
    knowledge: the ticket the turn's prompt named (src "prompt", or "jira-title" for pasted Jira text; see
    prompt_ticket) -> the branch's ticket (src "branch"), only when the branch belongs to the session's own repo and
    is a work branch (work_branch_ticket) -> the last ticket named earlier in the session (src "carry")."""
    if prompt_t:
        return prompt_t, prompt_src or "prompt"
    own = not (repo and own_repo) or norm_path(repo) == norm_path(own_repo)
    bt = work_branch_ticket(branch, cfg, own_repo or repo) if own else None
    if bt:
        return bt, "branch"
    if carry:
        return carry, "carry"
    return None, None


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
        if (p in allowed or m.group(1).isupper()) and "%s-%s" % (p, int(m.group(2))) not in _PLACEHOLDERS:
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


IGNORE_FILE = ".worklog-ignore"
_IGNORE_WALK_MAX = 40


def path_ignored(path):
    """True when a `.worklog-ignore` file sits in `path` or in any folder above it, up to and including the user's
    home folder (never above it), or up to the drive root for paths outside the home folder. So a nested repo
    inside an ignored checkout (its own git repo, a dev data home…) is ignored too. A few stat calls and no cache:
    cheap enough for a hook, and a marker added while the tick runs counts at once."""
    if not path:
        return False
    try:
        d = os.path.abspath(path)
    except (TypeError, ValueError):
        return False
    stop = norm_path(os.path.expanduser("~"))
    for _ in range(_IGNORE_WALK_MAX):
        if os.path.exists(os.path.join(d, IGNORE_FILE)):
            return True
        if norm_path(d) == stop:
            return False
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent
    return False


def repo_ignored(repo_root):
    """A repo (or any folder) is ignored when it, or a folder above it, has `.worklog-ignore` (see path_ignored)."""
    return bool(repo_root) and path_ignored(repo_root)


def is_home_folder(path):
    """True for the user's home folder itself (not a folder below it); case-insensitive on Windows."""
    if not path:
        return False
    try:
        a = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    except (TypeError, ValueError):
        return False
    return a == os.path.normcase(os.path.abspath(os.path.expanduser("~"))).rstrip("\\/")


def capture_mode(cwd, cfg):
    """What capture does for a session in `cwd`, one rule set for the hooks and the transcript safety net:
    "time" inside the data home and in the home folder itself (timestamp + ticket mentioned, never content), None
    when not captured (outside the capture scope, `.worklog-ignore` here or above, Chitragupta's runtime and settings
    folder), else "full". Folders below the home folder (repos, notes) stay "full"."""
    if not cwd:
        return None
    if path_within(cwd, home()):
        return "time"
    if not scope_allows(cwd, cfg) or path_within(cwd, user_dir()) or path_ignored(cwd):
        return None
    if is_home_folder(cwd):
        return "time"
    return "full"


def own_folders():
    """Chitragupta's own folders: the data home, the runtime and settings folder, and the plugin (or runtime copy)
    this code runs from."""
    return [home(), user_dir(), os.path.dirname(PACKAGE_DIR)]


def in_own_folder(path):
    return bool(path) and any(path_within(path, p) for p in own_folders())


def temp_dirs():
    """The system temp folder(s): scratch and test folders live there, never work to learn from. The same places
    tempfile.gettempdir() looks (that module is only for chitragupta/safety)."""
    cands = [os.environ.get(k) for k in ("TMPDIR", "TEMP", "TMP")]
    if os.name == "nt":
        cands += [os.path.join(os.environ.get("LOCALAPPDATA") or "", "Temp") if os.environ.get("LOCALAPPDATA") else "",
                  os.path.join(os.environ.get("SystemRoot") or "C:\\Windows", "Temp")]
    else:
        cands += ["/tmp", "/var/tmp", "/usr/tmp"]
    out = []
    for p in cands:
        if p and norm_path(p) not in [norm_path(x) for x in out]:
            out.append(p)
    return out


def learnable_path(path, cfg):
    """May the engine learn settings (a new ticket prefix, a repo to watch) from work in this folder? Only from
    captured work: inside the capture scope, not ignored (.worklog-ignore here or above), not under the system temp
    folder, and not inside Chitragupta's own folders (data home, runtime, plugin)."""
    if not path or not scope_allows(path, cfg) or in_own_folder(path):
        return False
    if any(path_within(path, t) for t in temp_dirs()):
        return False
    return not path_ignored(path)
