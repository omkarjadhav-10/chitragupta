"""Branch-tracking settings and command parsing: light enough for the PreToolUse guard (no git, no redaction).

Strategies, base roles and suffix hints are config, not code (config.json "branching"); see docs/branch-tracking.md.
"""
import copy
import fnmatch
import os
import re
import shlex

DEFAULT_STRATEGIES = {
    "release-then-qa": {"targets": ["release", "qa"], "steps": [
        {"from": "release", "pr_into": "release"},
        {"from": "qa", "brings": "merge", "of_step": 1, "pr_into": "qa"}]},
    "qa-only": {"targets": ["qa"], "steps": [{"from": "qa", "pr_into": "qa"}]},
    "qa-cherry-pick": {"targets": ["qa", "uat", "staging"], "steps": [
        {"from": "qa", "pr_into": "qa"},
        {"from": "uat", "brings": "cherry-pick", "pr_into": "uat"},
        {"from": "staging", "brings": "cherry-pick", "pr_into": "staging"}]},
}
DEFAULT_BASES = {"release": "release/*", "qa": "qa", "uat": "uat", "staging": "staging", "main": ["main", "master"]}
# a branch-name suffix that names where the branch is meant to go (used by the guard and as a hint)
DEFAULT_SUFFIXES = {"-qa": "qa", "-uat": "uat", "-on-uat": "uat", "-staging": "staging", "-stg": "staging",
                    "-on-staging": "staging", "-release": "release"}
FETCH = {"enabled": True, "every_minutes": 30, "active_days": 14, "before_send_minutes": 20}
PR_REFRESH_MINUTES = 60
MAX_OWN = 200
RELEASE_LOOKBACK_DAYS = 120
STATUSES = ("active", "pr_open", "merged", "deleted_remote", "abandoned")

# --------------------------------------------------------------------------- config

def _list(x):
    return [x] if isinstance(x, str) else list(x or [])


def settings(cfg):
    b = cfg.get("branching") if isinstance(cfg.get("branching"), dict) else {}
    st = copy.deepcopy(DEFAULT_STRATEGIES)
    st.update(b.get("strategies") or {})
    fetch = dict(FETCH)
    fetch.update(b.get("fetch") or {})
    mode = (b.get("guard_mode") or "warn").lower()
    return {"strategies": st, "repos": b.get("repos") or {}, "guard_mode": mode if mode in ("warn", "ask", "off") else "warn",
            "stale_pr_days": int(b.get("stale_pr_days") or 3), "fetch": fetch,
            "ignore_tickets": {str(t).upper() for t in b.get("ignore_tickets") or []},
            "pr_refresh_minutes": int(b.get("pr_refresh_minutes") or PR_REFRESH_MINUTES)}


def ignored(cfg, ticket):
    """Tickets listed in branching.ignore_tickets get no branch tracking at all (no git or host calls for them)."""
    return bool(ticket) and ticket.upper() in settings(cfg)["ignore_tickets"]


def repo_conf(cfg, name):
    """Settings for one repo. Repos not listed under branching.repos are still tracked (branches, parents, PRs),
    with the default base names, but get no delivery check (no strategy)."""
    s = settings(cfg)
    rc = s["repos"].get(name)
    configured = isinstance(rc, dict)
    rc = rc if configured else {}
    bases = copy.deepcopy(DEFAULT_BASES)
    if not configured:
        for e in cfg.get("env_branches") or []:
            if e not in [p for v in bases.values() for p in _list(v)]:
                bases[e] = e
    bases.update(rc.get("bases") or {})
    suffixes = dict(DEFAULT_SUFFIXES)
    suffixes.update(rc.get("suffixes") or {})
    return {"configured": configured, "strategy": rc.get("strategy") if configured else None,
            "remote": rc.get("remote") or "origin", "bases": bases, "rules": rc.get("rules") or [],
            "pr_source": rc.get("pr_source"), "suffixes": suffixes, "fetch": rc.get("fetch", True)}


def role_of(name, bases):
    """The base role a branch name plays (qa, release, ...) or None for a work branch."""
    if not name:
        return None
    for role, pats in bases.items():
        for p in _list(pats):
            if fnmatch.fnmatchcase(name, p):
                return role
    return None


def is_pattern_role(bases, role):
    return any(any(c in p for c in "*?[") for p in _list(bases.get(role)))


def suffix_role(name, suffixes):
    best = None
    for suf, role in suffixes.items():
        if name and name.endswith(suf) and (best is None or len(suf) > len(best[0])):
            best = (suf, role)
    return best[1] if best else None


def strategy_for(cfg, conf, manual, key, tk, jira_parent=None):
    """(name, how) for a ticket: ticket override, then a rule (e.g. by Jira parent), then the repo setting from the
    inbox, then the repo default. `conf` = repo_conf(), `manual` = the repo's manual decisions (repo.json)."""
    strategies = settings(cfg)["strategies"]
    ov = ((tk or {}).get("git_override") or {}).get("strategy")
    if ov in strategies:
        return ov, "set for this ticket"
    parent = jira_parent or ((tk or {}).get("jira") or {}).get("parent")
    for rule in conf.get("rules") or []:
        pat = rule.get("jira_parent")
        if pat and parent and fnmatch.fnmatchcase(parent, pat) and rule.get("strategy") in strategies:
            return rule["strategy"], "Jira parent %s" % parent
    man = (manual or {}).get("strategy")
    if man in strategies:
        return man, "repo setting (inbox)"
    if conf.get("strategy") in strategies:
        return conf["strategy"], "repo default"
    return None, None


def from_roles(strat):
    return {s.get("from") for s in (strat or {}).get("steps") or []}


def repo_key(path):
    name = os.path.basename((path or "").replace("\\", "/").rstrip("/")) or "repo"
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)



# --------------------------------------------------------------------------- session commands (Claude Code)

_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\n|\|)\s*")
URL_RX = re.compile(r"https?://[^\s'\"<>)]+/(?:pull/\d+|-/merge_requests/\d+|merge_requests/\d+|pullrequest/\d+)")


def _tokens(part):
    try:
        return shlex.split(part, posix=True)
    except ValueError:
        return part.split()


def _git_args(tok):
    """Strip `git` and its global options (-C dir, -c k=v, --no-pager)."""
    if not tok or os.path.basename(tok[0]).lower() not in ("git", "git.exe"):
        return None
    i = 1
    while i < len(tok) and tok[i].startswith("-"):
        i += 2 if tok[i] in ("-C", "-c") else 1
    return tok[i:]


def parse_command(cmd):
    """Git and PR commands in one Bash / PowerShell command line -> [{kind, branch?, from?, base?, head?, ...}]."""
    out = []
    for part in _SPLIT.split(cmd or ""):
        tok = _tokens(part.strip())
        if not tok:
            continue
        g = _git_args(tok)
        if g:
            out += _parse_git(g)
            continue
        exe = os.path.basename(tok[0]).lower()
        if exe in ("gh", "gh.exe") and tok[1:3] == ["pr", "create"]:
            out.append({"kind": "pr-create", "tool": "gh", "base": _opt(tok, ("--base", "-B")), "head": _opt(tok, ("--head", "-H"))})
        elif exe in ("glab", "glab.exe") and tok[1:3] == ["mr", "create"]:
            out.append({"kind": "pr-create", "tool": "glab", "base": _opt(tok, ("--target-branch", "-b")),
                        "head": _opt(tok, ("--source-branch", "-s"))})
        elif exe in ("az", "az.cmd") and tok[1:4] == ["repos", "pr", "create"]:
            out.append({"kind": "pr-create", "tool": "az", "base": _strip_ref(_opt(tok, ("--target-branch", "-t"))),
                        "head": _strip_ref(_opt(tok, ("--source-branch", "-s")))})
    return out


def _strip_ref(b):
    return re.sub(r"^refs/heads/", "", b) if b else b


def _opt(tok, names):
    for i, t in enumerate(tok):
        for n in names:
            if t == n and i + 1 < len(tok):
                return tok[i + 1]
            if t.startswith(n + "=") and n.startswith("--"):
                return t[len(n) + 1:]
    return None


def _positional(args, takes_value=()):
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out += args[i + 1:]
            break
        if a.startswith("-"):
            i += 2 if a in takes_value else 1
            continue
        out.append(a)
        i += 1
    return out


def _parse_git(g):
    if not g:
        return []
    sub, rest = g[0], g[1:]
    if sub == "checkout":
        for flag in ("-b", "-B"):
            if flag in rest:
                i = rest.index(flag)
                if i + 1 < len(rest):
                    pos = _positional(rest[i + 2:], ("--track",))
                    return [{"kind": "create", "branch": rest[i + 1], "from": pos[0] if pos else None}]
        pos = _positional(rest)
        return [{"kind": "checkout", "branch": pos[0]}] if pos else []
    if sub == "switch":
        for flag in ("-c", "-C", "--create", "--force-create"):
            if flag in rest:
                i = rest.index(flag)
                if i + 1 < len(rest):
                    pos = _positional(rest[i + 2:])
                    return [{"kind": "create", "branch": rest[i + 1], "from": pos[0] if pos else None}]
        pos = _positional(rest)
        return [{"kind": "checkout", "branch": pos[0]}] if pos else []
    if sub == "branch":
        if any(x in rest for x in ("-d", "-D", "--delete", "-m", "-M", "--list", "-l", "-a", "-r", "--contains", "-v", "-vv",
                                   "--show-current", "-u", "--set-upstream-to", "--unset-upstream")):
            return []
        pos = _positional(rest)
        if pos:
            return [{"kind": "create", "branch": pos[0], "from": pos[1] if len(pos) > 1 else None}]
        return []
    if sub == "cherry-pick":
        pos = [p for p in _positional(rest, ("-m", "--mainline", "-X", "--strategy-option")) if p not in ("--continue", "--abort")]
        return [{"kind": "cherry-pick", "commits": pos}] if pos else []
    if sub == "merge":
        pos = _positional(rest, ("-m", "-X", "-s", "--strategy", "--strategy-option", "-F"))
        return [{"kind": "merge", "from": pos[0]}] if pos else []
    if sub == "push":
        pos = _positional(rest, ("-o", "--push-option", "--repo"))
        return [{"kind": "push", "remote": pos[0] if pos else None, "branch": pos[1] if len(pos) > 1 else None}]
    if sub == "rebase":
        return [{"kind": "rebase", "onto": _opt(rest, ("--onto",)) or (_positional(rest) or [None])[0]}]
    return []


def pr_urls(text):
    return sorted(set(URL_RX.findall(text or "")))
