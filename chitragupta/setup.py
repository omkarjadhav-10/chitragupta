"""Set Chitragupta up (idempotent; re-running it is both repair and upgrade). Never modifies data files.

    python -m chitragupta setup check [--json] [--no-mcp]
        Python, git, Windows, Claude Code, `py -3`; the data home; detected repos, ticket prefixes and Bruno
        collections (from Claude Code session folders); MCP status; what is installed.
    python -m chitragupta setup apply --home DIR [--prefixes A,B] [--repos P,…] [--bruno P,…] [--no-task]
                                       [--github-remote URL] [--json]
        Pointer, home layout, config merge (keeps every key you have), migrate, runtime, scheduled task,
        recall skill, CLAUDE.md block; ends with doctor.
    python -m chitragupta setup mcp-status [--json] [--cached]
    python -m chitragupta setup add-slack [--client-id ID] [--callback-port N]
    python -m chitragupta setup slack-user [email]
    python -m chitragupta setup use-database <server>

Slack runs only for background jobs: the server is added in *local* scope of <home>/.worklog/slack-sender/ (its own
git root), never user scope. Jira jobs are written for the `mcp-atlassian` server. The database MCP is whichever
configured server you pick with `use-database`.
"""
import json
import os
import re
import shutil
import sys
import time

from . import __version__
from . import common
from . import plugin

MD_START, MD_END = "<!-- chitragupta:start -->", "<!-- chitragupta:end -->"
SKILL_NAME = "chitragupta"
SLACK_URL = "https://mcp.slack.com/mcp"
MIN_PYTHON = (3, 10)

PLUGIN_ROOT = os.path.dirname(common.PACKAGE_DIR)
# <plugin>/user-skill/... or runtime/<version>/user-skill/... (runtime.install copies it); placeholders {{HOME}},
# {{LAUNCHER}} (also {{HOME_POSIX}}, {{DAY_STARTS_AT}}, {{VERSION}}, {{PYTHON}}); anything else stays literal.
SKILL_TEMPLATES = [os.path.join(os.path.dirname(common.PACKAGE_DIR), "user-skill", "chitragupta", "SKILL.md")]
BLOCK_TEMPLATES = [os.path.join(common.PACKAGE_DIR, "templates", "claude_md_block.md")]

RUNNER = None  # tests: callable(args, cwd=None, timeout=…) -> (rc, stdout, stderr) for git / claude / py calls


def _run(args, cwd=None, timeout=60):
    if RUNNER is not None:
        return RUNNER(args, cwd=cwd, timeout=timeout)
    return common.run(args, cwd=cwd, timeout=timeout)


def _claude_exe(cfg=None):
    from . import extract
    try:
        return extract.resolve_claude(cfg or common.load_config())
    except Exception:  # noqa: BLE001
        return None


def _fwd(p):
    return os.path.abspath(p).replace("\\", "/")


# --------------------------------------------------------------------------- environment

def _version_of(args, cwd=None):
    try:
        rc, so, se = _run(args, cwd=cwd, timeout=30)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    text = (so or se or "").strip().splitlines()
    return rc == 0, (text[0] if text else "rc %s" % rc)


def environment(cfg=None):
    rows = []
    v = sys.version_info
    rows.append({"name": "python", "ok": v >= MIN_PYTHON, "detail": "%d.%d.%d (%s)" % (v[0], v[1], v[2], sys.executable)})
    git = (cfg or {}).get("paths", {}).get("git") or shutil.which("git") or "git"
    ok, d = _version_of([git, "--version"])
    rows.append({"name": "git", "ok": ok, "detail": d if ok else "git not found: %s" % d})
    rows.append({"name": "windows", "ok": os.name == "nt", "detail": "Windows" if os.name == "nt" else
                 "not Windows: the scheduled task is Windows-only"})
    from . import extract
    exe = _claude_exe(cfg)
    if exe:
        ok, d = _version_of(exe + ["--version"])
        d = "%s (%s)" % (d, exe[-1])
        if extract.unstable(exe[-1]):
            ok, d = False, d + " — this path only exists in the current shell; the scheduled task can't use it"
    else:
        ok, d = False, ("claude executable not found where the scheduled task can reach it. Fix one of: "
                        "run `claude install` (native, %%USERPROFILE%%\\.local\\bin), repair the npm global copy, "
                        "or set paths.claude in %s" % common.wl("config.json"))
    rows.append({"name": "claude", "ok": ok, "detail": d})
    py = shutil.which("py")
    if py:
        ok, d = _version_of([py, "-3", "--version"])
    else:
        ok, d = False, "`py` launcher not on PATH (the plugin's hooks run `py -3`)"
    rows.append({"name": "py -3", "ok": ok, "detail": d})
    return rows


def home_state():
    ptr = common.read_text(common.home_pointer()).strip() or None
    return {"resolved": common.home(), "env": os.environ.get("CHITRAGUPTA_HOME") or None, "pointer": ptr,
            "pointer_file": common.home_pointer(), "default": common.DEFAULT_HOME, "set_up": plugin.is_set_up(),
            "config_exists": os.path.isfile(common.wl("config.json"))}


# --------------------------------------------------------------------------- detections

_BRANCH_TICKET = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,9})-(\d{1,6})(?![0-9])")
NOT_PREFIX = set(common.TICKET_DENY) | {
    "FEATURE", "FEAT", "BUGFIX", "BUG", "FIX", "HOTFIX", "RELEASE", "REL", "CHORE", "DEPENDABOT", "RENOVATE",
    "SPRINT", "VERSION", "BUILD", "MAIN", "MASTER", "DEVELOP", "DEV", "QA", "UAT", "STAGING", "PROD", "TASK",
    "STORY", "EPIC", "ISSUE", "WIP", "TMP", "TEMP", "BACKUP", "WORKTREE", "AGENT", "REVERT", "MERGE", "SPIKE",
    "DOCS", "REFACTOR", "PERF", "CI", "BUMP", "UPDATE", "PATCH", "MINOR", "MAJOR",
}
_PRUNE = {".git", "node_modules", "target", "build", "dist", "bin", "obj", ".venv", "venv", "__pycache__", ".idea",
          ".gradle", ".vs", ".vscode", "out", ".next", "coverage"}


def session_cwds(limit_dirs=300, files_per_dir=5, lines_per_file=40):
    """Working folders of recent Claude Code sessions (from <claude dir>/projects/*/*.jsonl)."""
    root = os.path.join(plugin.claude_dir(), "projects")
    seen, out = set(), []
    try:
        dirs = sorted((os.path.join(root, d) for d in os.listdir(root)), key=os.path.getmtime, reverse=True)
    except OSError:
        return []
    for d in dirs[:limit_dirs]:
        try:
            files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")),
                           key=os.path.getmtime, reverse=True)[:files_per_dir]
        except OSError:
            continue
        for fp in files:
            try:
                with open(fp, encoding="utf-8", errors="replace") as f:
                    for _ in range(lines_per_file):
                        line = f.readline()
                        if not line:
                            break
                        if '"cwd"' not in line:
                            continue
                        try:
                            cwd = json.loads(line).get("cwd")
                        except ValueError:
                            continue
                        if cwd:
                            key = common.norm_path(cwd)
                            if key not in seen:
                                seen.add(key)
                                out.append(cwd)
                            break
            except OSError:
                continue
    return out


def local_branches(repo, cap=500):
    """Local branch names from .git (refs/heads + packed-refs), without running git."""
    gd = common._git_dir(repo)
    if not gd:
        return []
    cd = common.read_text(os.path.join(gd, "commondir")).strip()
    if cd:
        gd = os.path.normpath(os.path.join(gd, cd))
    names = set()
    heads = os.path.join(gd, "refs", "heads")
    for dp, _, fns in os.walk(heads):
        for fn in fns:
            names.add(os.path.relpath(os.path.join(dp, fn), heads).replace("\\", "/"))
            if len(names) >= cap:
                break
    for line in common.read_text(os.path.join(gd, "packed-refs")).splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            names.add(parts[1][len("refs/heads/"):].strip())
    cur = common.read_branch(repo)
    if cur and not cur.startswith("detached@"):
        names.add(cur)
    return sorted(names)[:cap]


def prefixes_from_branches(branches):
    """{PREFIX: set(ticket keys)} for ticket-like names in branch names."""
    found = {}
    for b in branches:
        for m in _BRANCH_TICKET.finditer(b):
            p = m.group(1).upper()
            if p in NOT_PREFIX or p[0].isdigit():
                continue
            found.setdefault(p, set()).add("%s-%d" % (p, int(m.group(2))))
    return found


def bruno_collections(repo, max_depth=4, max_entries=20000):
    out, n = [], 0
    base = repo.rstrip("\\/").count(os.sep)
    for dp, dns, fns in os.walk(repo):
        n += len(dns) + len(fns)
        if "bruno.json" in fns:
            out.append(dp)
        if dp.count(os.sep) - base >= max_depth or n > max_entries:
            dns[:] = []
        else:
            dns[:] = [d for d in dns if d not in _PRUNE and not d.startswith(".")]
    return out


def detect(cfg=None):
    cfg = cfg or {}
    home = common.home()
    repos, seen = [], set()
    for cwd in session_cwds() + list(cfg.get("watched_repos") or []):
        root = common.find_repo_root(cwd) if os.path.isdir(cwd) else None
        if not root or common.path_within(root, home) or common.norm_path(root) in seen:
            continue
        seen.add(common.norm_path(root))
        repos.append(root)
    pref, info = {}, []
    for r in repos:
        br = local_branches(r)
        for p, keys in prefixes_from_branches(br).items():
            pref.setdefault(p, set()).update(keys)
        info.append({"path": _fwd(r), "branches": len(br), "ignored": common.repo_ignored(r)})
    prefixes = [{"prefix": p, "tickets": len(k), "examples": sorted(k)[:3]}
                for p, k in sorted(pref.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    bruno = []
    for r in repos:
        bruno += [_fwd(b) for b in bruno_collections(r)]
    return {"repos": info, "prefixes": prefixes, "suggested_prefixes": [x["prefix"] for x in prefixes if x["tickets"] >= 2],
            "bruno": bruno, "configured_prefixes": list(cfg.get("ticket_prefixes") or [])}


# --------------------------------------------------------------------------- MCP status

STATUS_WORDS = (("Connected", "connected"), ("Needs authentication", "signed out"), ("Failed", "offline"))


def parse_mcp_list(text):
    """`claude mcp list` lines -> {server name: connected | signed out | offline | <raw>}."""
    out = {}
    for line in (text or "").splitlines():
        if ": " not in line or " - " not in line:
            continue
        name, rest = line.split(": ", 1)
        state = rest.rsplit(" - ", 1)[-1]
        status = next((v for k, v in STATUS_WORDS if k in state), state.strip()[:40])
        out[name.strip()] = status
    return out


def _servers(cj):
    return {k: v for k, v in (cj.get("mcpServers") or {}).items() if isinstance(v, dict)}


def local_servers(cj, folder):
    """Servers in Claude Code's local scope for `folder` (projects[<path>].mcpServers)."""
    key = common.norm_path(folder)
    for p, pv in (cj.get("projects") or {}).items():
        if common.norm_path(p) == key and isinstance(pv, dict):
            return {k: v for k, v in (pv.get("mcpServers") or {}).items() if isinstance(v, dict)}
    return {}


def _url(s):
    return str(s.get("url") or "")


def _is_slack(name, s):
    return "slack.com" in _url(s) or name.lower() == "slack"


def _is_atlassian_remote(name, s):
    return "atlassian.com" in _url(s) or "atlassian.net" in _url(s)


def mcp_list(folder, cfg=None, timeout=90):
    """Run `claude mcp list` in `folder` (its local scope included). None when claude can't be run."""
    exe = _claude_exe(cfg)
    if not exe:
        return None
    rc, so, se = _run(exe + ["mcp", "list"], cwd=folder if os.path.isdir(folder) else common.home(), timeout=timeout)
    if rc != 0 and not so:
        return None
    return parse_mcp_list(so)


def mcp_status(cfg=None, probe=True, listing=None):
    """Slack, Jira and database MCP status for background jobs; written to <home>/.worklog/cache/mcp-status.json.
    Reads Claude Code's .claude.json (never writes it); `probe` runs `claude mcp list` for live state."""
    from . import mcpjobs, slack
    cfg = cfg or common.load_config()
    cj = common.read_json(plugin.claude_json_path(), {}) or {}
    user = _servers(cj)
    sender = slack.sender_dir()
    local = local_servers(cj, sender)
    if listing is None and probe:
        listing = mcp_list(sender, cfg)
    probed = listing is not None
    listing = listing or {}

    def live(name):
        return listing.get(name) or ("configured" if not probed else "not listed")

    # Slack: Slack's hosted MCP in local scope of the sender folder (background only).
    sl = next((n for n, s in local.items() if _is_slack(n, s)), None)
    scfg = ((cfg.get("jira_worklog") or {}).get("slack") or {})
    if sl:
        st = live(sl)
        detail = "server %r in local scope of %s" % (sl, sender)
        if st == "connected" and not scfg.get("self_user_id"):
            detail += "; run `setup slack-user` to store your Slack user ID"
        slack_row = {"status": st, "server": sl, "detail": detail}
    else:
        slack_row = {"status": "missing", "server": None,
                     "detail": "no background Slack server (run `setup add-slack`)"}
        if any(_is_slack(n, s) for n, s in user.items()):
            slack_row["detail"] += "; a user-scope Slack server exists but background jobs don't use it"
    if any(n.lower().endswith(" slack") and n.lower().startswith("claude.ai") for n in listing):
        slack_row["detail"] += "; the claude.ai Slack connector is not used by background jobs"
    slack_row["user_id_stored"] = bool(scfg.get("self_user_id"))

    # Jira: the mcp-atlassian server the v0.1.0 jobs are written for (user scope).
    jname = mcpjobs.server_name(cfg, "jira")
    if jname in user:
        jira_row = {"status": live(jname), "server": jname, "detail": "server %r (user scope)" % jname}
    else:
        remote = [n for n, s in user.items() if _is_atlassian_remote(n, s)] + \
                 [n for n in listing if n.lower().startswith("claude.ai") and "atlassian" in n.lower()]
        if remote:
            jira_row = {"status": "unsupported", "server": remote[0],
                        "detail": "the official Atlassian remote MCP (%s) is present but not supported by v0.1.0 jobs; "
                                  "they need the %r server" % (remote[0], jname)}
        else:
            jira_row = {"status": "missing", "server": None, "detail": "no %r server in user scope" % jname}
    jira_row["enabled"] = bool(((cfg.get("mcp") or {}).get("jira") or {}).get("enabled"))

    # Database: whichever configured server the user picked (mcp.database.server).
    dcfg = ((cfg.get("mcp") or {}).get("database") or {})
    skip = {sl, jname} | {n for n, s in user.items() if _is_slack(n, s) or _is_atlassian_remote(n, s)}
    candidates = sorted(n for n in user if n not in skip)
    dname = dcfg.get("server") or ""
    if dname and dname in user:
        db_row = {"status": live(dname), "server": dname, "detail": "server %r (user scope)" % dname}
    elif dname:
        db_row = {"status": "missing", "server": dname, "detail": "mcp.database.server %r is not configured in Claude "
                                                                  "Code" % dname}
    else:
        db_row = {"status": "missing", "server": None,
                  "detail": ("not chosen; pick one with `setup use-database <server>`: %s" % ", ".join(candidates))
                  if candidates else "no candidate server configured"}
    db_row.update(enabled=bool(dcfg.get("enabled")), candidates=candidates)

    slack_row["state"] = slack.state(cfg)  # ready | off | not_connected (user id + send tool stored?)
    res = {"checked": common.now_iso(), "probed": probed, "slack": slack_row, "jira": jira_row, "database": db_row,
           "jobs_configured": mcpjobs.configured(cfg)}
    if plugin.is_set_up():
        try:
            common.write_json(plugin.mcp_status_cache(), res)
        except OSError:
            pass
    return res


def cached_mcp_status():
    return common.read_json(plugin.mcp_status_cache()) or None


# --------------------------------------------------------------------------- installed pieces

def frontmatter_name(text):
    head = (text or "")[:2048]
    if not head.lstrip("﻿").startswith("---"):
        return None
    m = re.search(r"^name:\s*['\"]?([^'\"\r\n]+?)['\"]?\s*$", head, re.M)
    return m.group(1).strip() if m else None


def skill_installed():
    p = plugin.skill_path()
    if not os.path.isfile(p):
        return {"installed": False, "ours": False, "path": p}
    name = frontmatter_name(common.read_text(p))
    return {"installed": name == SKILL_NAME, "ours": name == SKILL_NAME, "name": name, "path": p}


def check_markers(text):
    """None when the markers are usable (absent, or one start before one end); else the problem."""
    a, b = text.count(MD_START), text.count(MD_END)
    if a == 0 and b == 0:
        return None
    if a != 1 or b != 1:
        return "found %d start and %d end markers" % (a, b)
    if text.find(MD_START) > text.find(MD_END):
        return "end marker comes before the start marker"
    return None


def block_installed():
    p = plugin.claude_md_path()
    t = common.read_text(p)
    return {"installed": MD_START in t and MD_END in t and check_markers(t) is None, "path": p,
            "problem": check_markers(t)}


def installed(cfg=None):
    from . import install, runtime
    return {"runtime": runtime.status(), "task": install.task_status(), "skill": skill_installed(),
            "claude_md_block": block_installed()}


# --------------------------------------------------------------------------- templates

def template(candidates):
    for p in candidates:
        if os.path.isfile(p):
            return p, common.read_text(p)
    return None, None


def render_template(text, cfg=None):
    cfg = cfg or common.load_config()
    home = common.home()
    values = {"HOME": home, "HOME_POSIX": home.replace("\\", "/"), "DAY_STARTS_AT": cfg.get("day_starts_at") or "04:00",
              "VERSION": __version__, "LAUNCHER": common.launcher(), "PYTHON": common.python_exe(cfg)}
    for k, v in values.items():
        text = text.replace("{{%s}}" % k, v)
    return text


def install_skill(cfg=None):
    """Write the recall skill to <claude dir>/skills/chitragupta/SKILL.md; an existing file is replaced only when
    its frontmatter name is ours."""
    src, text = template(SKILL_TEMPLATES)
    if not text:
        return {"ok": False, "detail": "recall skill template not found (%s)" % ", ".join(SKILL_TEMPLATES)}
    body = render_template(text, cfg)
    if frontmatter_name(body) != SKILL_NAME:
        return {"ok": False, "detail": "template %s does not declare `name: %s`" % (src, SKILL_NAME)}
    dest = plugin.skill_path()
    if os.path.exists(dest):
        name = frontmatter_name(common.read_text(dest))
        if name != SKILL_NAME:
            return {"ok": False, "detail": "%s belongs to another skill (name: %s); not overwritten. Move it away and "
                                           "re-run setup." % (dest, name)}
    changed = common.write_text(dest, body)
    return {"ok": True, "detail": "%s %s" % (dest, "written" if changed else "unchanged")}


def block_text(cfg=None):
    src, text = template(BLOCK_TEMPLATES)
    if not text:
        return None
    body = render_template(text, cfg)
    if MD_START in body and MD_END in body:
        body = body[body.find(MD_START) + len(MD_START):body.find(MD_END)]
    return "%s\n%s\n%s" % (MD_START, body.strip("\n"), MD_END)


def upsert_block(text, block):
    i, j = text.find(MD_START), text.find(MD_END)
    if i >= 0 and j > i:
        return text[:i] + block + text[j + len(MD_END):]
    return (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"


def remove_block(text):
    i, j = text.find(MD_START), text.find(MD_END)
    if i < 0 or j < i:
        return text
    before, after = text[:i].rstrip("\n"), text[j + len(MD_END):].lstrip("\n")
    return (before + "\n\n" + after if before and after else before + after).strip("\n") + "\n"


def backup_once(path, run_id):
    """<file>.chitragupta.bak, written at most once per setup run."""
    if not os.path.isfile(path) or _BACKED_UP.get(path) == run_id:
        return None
    bak = path + ".chitragupta.bak"
    shutil.copy2(path, bak)
    _BACKED_UP[path] = run_id
    return bak


_BACKED_UP = {}


def install_block(cfg=None, run_id=None):
    block = block_text(cfg)
    if not block:
        return {"ok": False, "detail": "CLAUDE.md block template not found (%s)" % ", ".join(BLOCK_TEMPLATES)}
    path = plugin.claude_md_path()
    cur = common.read_text(path)
    problem = check_markers(cur)
    if problem:
        return {"ok": False, "detail": "%s: %s; fix the chitragupta markers by hand" % (path, problem)}
    new = upsert_block(cur, block)
    if new == cur:
        return {"ok": True, "detail": "%s block unchanged" % path}
    bak = backup_once(path, run_id or time.time())
    common.write_text(path, new)
    return {"ok": True, "detail": "%s block written%s" % (path, " (backup %s)" % bak if bak else "")}


# --------------------------------------------------------------------------- config

def _split(values):
    out = []
    for v in values or []:
        out += [x.strip() for x in re.split(r"[,;]", v) if x.strip()]
    return out


def _union(old, new, key=lambda x: x):
    out, seen = list(old or []), {key(x) for x in old or []}
    for x in new:
        if key(x) not in seen:
            seen.add(key(x))
            out.append(x)
    return out


def merged_config(existing, home, prefixes=(), repos=(), bruno=(), github_remote=None):
    """User keys are never dropped; lists only grow; scalars change only when given explicitly (or are empty)."""
    cfg = json.loads(json.dumps(existing or {}))
    if not cfg.get("worklog_home"):
        cfg["worklog_home"] = home.replace("\\", "/")
    cfg["ticket_prefixes"] = _union(cfg.get("ticket_prefixes"), [p.upper() for p in prefixes], key=str.upper)
    cfg["watched_repos"] = _union(cfg.get("watched_repos"), [_fwd(r) for r in repos], key=common.norm_path)
    cfg["bruno_collections"] = _union(cfg.get("bruno_collections"), [_fwd(b) for b in bruno], key=common.norm_path)
    if github_remote is not None:
        cfg["github_remote"] = github_remote
    paths = cfg.setdefault("paths", {})
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    found = {"python": py, "pythonw": pyw if os.path.isfile(pyw) else "", "git": shutil.which("git") or ""}
    for k, v in found.items():
        if not paths.get(k) and v:
            paths[k] = v.replace("\\", "/")
    # claude: record a path the scheduled task can reach; replace one that no longer exists
    # (an in-place Claude Code update can move it).
    if not (paths.get("claude") and os.path.isfile(paths["claude"])):
        from . import extract
        exe = extract.resolve_claude({"paths": {}})
        if exe and len(exe) == 1 and not extract.unstable(exe[0]):
            paths["claude"] = exe[0].replace("\\", "/")
    return cfg


def read_raw_config():
    """(cfg or None, error). Missing file -> ({}, None)."""
    p = common.wl("config.json")
    if not os.path.exists(p):
        return {}, None
    try:
        with open(p, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        return None, "%s does not parse: %s" % (p, e)
    if not isinstance(cfg, dict):
        return None, "%s is not a JSON object" % p
    return cfg, None


def write_raw_config(cfg, before):
    p = common.wl("config.json")
    if before == cfg and os.path.exists(p):
        return None
    backup = None
    if os.path.exists(p):
        dest = common.wl("backups", "chitragupta-setup-" + time.strftime("%Y%m%d-%H%M%S"))
        common.ensure_dir(dest)
        backup = os.path.join(dest, "config.json")
        shutil.copy2(p, backup)
    common.write_json(p, cfg)
    return backup or "new"


# --------------------------------------------------------------------------- apply

MIGRATE_WAIT_S = 120  # how long setup waits for a running tick to release the agent lock
LAYOUT = (".worklog", os.path.join(".worklog", "logs"), os.path.join(".worklog", "cache"), "_drop")


def apply(home, prefixes=(), repos=(), bruno=(), task=True, github_remote=None, run_doctor=True):
    """-> {ok, home, steps: [{step, ok, detail}], doctor: [...]}. Validates first; stops before writing anything when
    config.json or the CLAUDE.md markers are unreadable."""
    home = os.path.abspath(os.path.expanduser(home))
    run_id = "%d-%f" % (os.getpid(), time.time())
    steps = []
    res = {"ok": False, "home": home, "steps": steps}

    def step(name, ok, detail):
        steps.append({"step": name, "ok": bool(ok), "detail": detail})
        return ok

    in_plugin = common.path_within(home, PLUGIN_ROOT) and not common.env("DEV")  # DEV: git-ignored .dev-home
    if in_plugin or common.path_within(home, common.user_dir()):
        step("validate", False, "the data home can't be inside the plugin or %s" % common.user_dir())
        return res
    saved_env = os.environ.get("CHITRAGUPTA_HOME")
    env_note = ""
    if saved_env and common.norm_path(saved_env) != common.norm_path(home):
        env_note = " (CHITRAGUPTA_HOME=%s is set and overrides the pointer in this environment)" % saved_env
    os.environ["CHITRAGUPTA_HOME"] = home
    try:
        before, err = read_raw_config()
        problem = check_markers(common.read_text(plugin.claude_md_path()))
        if err or problem:
            step("validate", False, err or "%s: %s" % (plugin.claude_md_path(), problem))
            return res
        step("validate", True, "config.json and CLAUDE.md readable")

        common.write_text(common.home_pointer(), home + "\n")
        step("pointer", True, "%s -> %s%s" % (common.home_pointer(), home, env_note))

        for d in LAYOUT:
            common.ensure_dir(os.path.join(home, d))
        step("layout", True, "%s (+ .worklog, _drop)" % home)

        cfg = merged_config(before, home, prefixes, repos, bruno, github_remote)
        b = write_raw_config(cfg, before)
        step("config", True, "config.json %s" % ("unchanged" if b is None else "created" if b == "new"
                                                 else "updated (backup %s)" % b))

        from . import migrate
        m = migrate.run(wait_s=MIGRATE_WAIT_S)  # waits for a running tick; "skipped" = busy, nothing changed
        if m.get("skipped"):
            step("migrate", False, "%s; re-run `setup apply` in a few minutes" % m["skipped"])
        else:
            step("migrate", not m.get("error"), m.get("error") or ("already done" if m.get("already") else
                                                                    "; ".join(m.get("changes") or [])))

        from . import runtime
        try:
            r = runtime.install()
            step("runtime", True, "%s %s" % (r["path"], "kept (newer)" if r.get("kept_newer") else
                                             "updated" if r["changed"] else "unchanged"))
        except Exception as e:  # noqa: BLE001
            step("runtime", False, "runtime install failed: %s" % e)

        if task:
            from . import install
            t = install.register_task(common.load_config())
            step("task", t["ok"], t["detail"])
        else:
            step("task", True, "skipped (--no-task)")

        s = install_skill()
        step("skill", s["ok"], s["detail"])
        bl = install_block(run_id=run_id)
        step("claude_md", bl["ok"], bl["detail"])

        if run_doctor:
            from . import doctor
            if saved_env is None:
                os.environ.pop("CHITRAGUPTA_HOME", None)  # the pointer resolves to `home` now
            res["doctor"] = doctor.run(probe=True, task_expected=task)
        res["ok"] = all(s["ok"] for s in steps)
        return res
    finally:
        if saved_env is None:
            os.environ.pop("CHITRAGUPTA_HOME", None)
        else:
            os.environ["CHITRAGUPTA_HOME"] = saved_env


# --------------------------------------------------------------------------- MCP actions

def add_slack(client_id=None, callback_port=None):
    """Add Slack's hosted MCP in local scope of <home>/.worklog/slack-sender/ (its own git root), for background
    jobs only. Prints the one-time browser sign-in steps."""
    from . import slack
    cfg = common.load_config()
    sender = slack.sender_dir()
    common.ensure_dir(sender)
    out = {"ok": False, "folder": sender, "steps": []}
    if not os.path.exists(os.path.join(sender, ".git")):
        rc, so, se = _run([common.git_exe(cfg), "init", "-q", sender], timeout=60)
        if rc != 0:
            out["error"] = "git init failed: %s" % (se or so).strip()
            return out
        out["steps"].append("git init %s (so Claude Code keys the local scope to this folder)" % sender)
    cj = common.read_json(plugin.claude_json_path(), {}) or {}
    existing = next((n for n, s in local_servers(cj, sender).items() if _is_slack(n, s)), None)
    srv = ((cfg.get("jira_worklog") or {}).get("slack") or {}).get("server") or {}
    client_id = client_id or srv.get("client_id")
    callback_port = callback_port or srv.get("callback_port")
    if existing:
        out["steps"].append("Slack server %r already in local scope of %s" % (existing, sender))
    else:
        exe = _claude_exe(cfg)
        if not exe:
            out["error"] = "claude executable not found"
            return out
        args = exe + ["mcp", "add", "--transport", "http", "--scope", "local"]
        from . import slack as _slack  # Slack's MCP has no dynamic client registration: always send a client id
        args += ["--client-id", str(client_id or _slack.DEFAULT_CLIENT_ID),
                 "--callback-port", str(callback_port or _slack.DEFAULT_CALLBACK_PORT)]
        args += ["slack", srv.get("url") or SLACK_URL]
        rc, so, se = _run(args, cwd=sender, timeout=60)
        if rc != 0:
            out["error"] = "claude mcp add failed: %s" % (se or so).strip()[:300]
            return out
        out["steps"].append("added Slack's hosted MCP as 'slack' in local scope of %s" % sender)
    if client_id or callback_port:
        raw, err = read_raw_config()
        if raw is not None:
            before = json.loads(json.dumps(raw))
            s = raw.setdefault("jira_worklog", {}).setdefault("slack", {}).setdefault("server", {})
            if client_id:
                s["client_id"] = str(client_id)
            if callback_port:
                s["callback_port"] = int(callback_port)
            write_raw_config(raw, before)
    out["ok"] = True
    out["sign_in"] = [
        "Open a terminal in %s" % sender,
        "Run `claude`, type /mcp, choose slack, then Authenticate and finish the sign-in in the browser",
        "Exit that session, then run `/cg:setup` again (or `setup slack-user`) to store your Slack user ID",
    ]
    return out


def slack_user(email=None):
    """Store my Slack user ID and the send tool (the existing `agent slack-discover`)."""
    from . import agent
    return agent.main(["slack-discover"] + ([email] if email else []))


def use_database(server, force=False):
    cj = common.read_json(plugin.claude_json_path(), {}) or {}
    if server not in _servers(cj) and not force:
        return {"ok": False, "detail": "%r is not a user-scope MCP server in %s (use --force to set it anyway)"
                                       % (server, plugin.claude_json_path())}
    raw, err = read_raw_config()
    if raw is None:
        return {"ok": False, "detail": err}
    before = json.loads(json.dumps(raw))
    db = raw.setdefault("mcp", {}).setdefault("database", {})
    db.update(server=server, enabled=True)
    write_raw_config(raw, before)
    return {"ok": True, "detail": "mcp.database.server = %s (enabled)" % server}


# --------------------------------------------------------------------------- CLI

def check(probe_mcp=True):
    cfg = common.load_config()
    out = {"version": __version__, "environment": environment(cfg), "home": home_state(), "detected": detect(cfg),
           "installed": installed(cfg)}
    out["mcp"] = mcp_status(cfg, probe=probe_mcp) if probe_mcp else cached_mcp_status()
    return out


def _opt(argv, name, many=False):
    vals, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == name and i + 1 < len(argv):
            vals.append(argv[i + 1])
            i += 2
            continue
        if a.startswith(name + "="):
            vals.append(a.split("=", 1)[1])
        i += 1
    return vals if many else (vals[-1] if vals else None)


def _print_rows(rows, key="name"):
    for r in rows:
        print("  [%s] %-14s %s" % ("ok" if r.get("ok") else "!!", r.get(key), r.get("detail", "")))


def _human_check(c):
    print("Chitragupta %s setup check" % c["version"])
    _print_rows(c["environment"])
    h = c["home"]
    print("home: %s (%s)" % (h["resolved"], "set up" if h["set_up"] else "not set up"))
    d = c["detected"]
    print("repos: %s" % (", ".join(r["path"] for r in d["repos"]) or "none found"))
    print("ticket prefixes: %s" % (", ".join("%s (%d)" % (p["prefix"], p["tickets"]) for p in d["prefixes"]) or "none"))
    print("bruno collections: %s" % (", ".join(d["bruno"]) or "none"))
    for k, v in (c.get("mcp") or {}).items():
        if isinstance(v, dict):
            print("mcp %-8s %s: %s" % (k, v.get("status"), v.get("detail")))


def main(argv):
    cmd = argv[0] if argv else "check"
    rest = argv[1:]
    as_json = "--json" in rest
    if cmd == "check":
        c = check(probe_mcp="--no-mcp" not in rest)
        print(json.dumps(c, indent=1)) if as_json else _human_check(c)
        return 0
    if cmd == "apply":
        home = _opt(rest, "--home")
        if not home:
            print("setup apply needs --home DIR", file=sys.stderr)
            return 2
        res = apply(home, prefixes=_split(_opt(rest, "--prefixes", True)), repos=_split(_opt(rest, "--repos", True)),
                    bruno=_split(_opt(rest, "--bruno", True)), task="--no-task" not in rest,
                    github_remote=_opt(rest, "--github-remote"))
        if as_json:
            print(json.dumps(res, indent=1))
        else:
            print("Chitragupta setup: %s" % res["home"])
            _print_rows(res["steps"], key="step")
            if res.get("doctor"):
                print("doctor:")
                _print_rows(res["doctor"])
        return 0 if res["ok"] else 1
    if cmd == "mcp-status":
        st = cached_mcp_status() if "--cached" in rest else mcp_status(probe=True)
        if as_json or not st:
            print(json.dumps(st, indent=1))
        else:
            for k in ("slack", "jira", "database"):
                print("%-8s %s: %s" % (k, st[k]["status"], st[k]["detail"]))
        return 0
    if cmd == "add-slack":
        res = add_slack(_opt(rest, "--client-id"), _opt(rest, "--callback-port"))
        if as_json:
            print(json.dumps(res, indent=1))
        else:
            print("\n".join(res["steps"] + ([res["error"]] if res.get("error") else [])))
            if res.get("sign_in"):
                print("One-time sign-in:\n" + "\n".join("  %d. %s" % (i + 1, s) for i, s in enumerate(res["sign_in"])))
        return 0 if res["ok"] else 1
    if cmd == "slack-user":
        pos = [a for a in rest if not a.startswith("--")]
        return slack_user(pos[0] if pos else None)
    if cmd == "use-database":
        pos = [a for a in rest if not a.startswith("--")]
        if not pos:
            print("setup use-database <server>", file=sys.stderr)
            return 2
        res = use_database(pos[0], force="--force" in rest)
        print(json.dumps(res, indent=1) if as_json else res["detail"])
        return 0 if res["ok"] else 1
    if cmd == "reminders":
        pos = [a for a in rest if not a.startswith("--")]
        if not pos or pos[0] not in ("on", "off"):
            print("setup reminders on|off", file=sys.stderr)
            return 2
        path = common.wl("config.json")
        if not os.path.isfile(path):
            print("Chitragupta isn't set up yet (no %s)" % path, file=sys.stderr)
            return 1
        cfg = common.read_json(path)
        cfg.setdefault("reminders", {})["mcp"] = pos[0] == "on"
        common.write_json(path, cfg)
        print("weekly reminder to connect Slack/Jira: %s" % pos[0])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # pythonw has no console
    sys.exit(main(sys.argv[1:]))
