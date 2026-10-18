"""Command guard: `run(argv, purpose)` is the only way Chitragupta starts a process.

Each purpose has its own allowlist; the executable, the subcommand, every flag and the working folder are checked
before anything starts. Anything not listed is blocked, logged to security.log and raises Blocked.

  git-work   read-only git in a work repo (rule 2), hardened against the repo's own config; `fetch` only into
             refs/remotes/<remote>/*, only in a watched repo, never while git holds a lock, git.allow_fetch
  git-home   the data-home repo's own sync: add (push paths only) / commit / fetch + rebase / push, never forced
  git-probe  git --version
  host       gh / glab / az: list and view only; `api` with GET only; az with --output json
  claude     `claude --version`, `claude mcp list`, and `claude -p` calls built by safety.claude (checked again here)
  self       this Python running Chitragupta's own launcher (maintenance, dashboard)
  task       schtasks for the `Chitragupta` task only
  launchd    launchctl print / bootout / bootstrap for Chitragupta's own LaunchAgent only (macOS)
  systemd    systemctl --user for Chitragupta's own units only (Linux)
  cron       crontab -l, and crontab - with a table whose other lines are exactly the current ones (Linux)
  probe      py -3 --version
"""
import json
import os
import re
import subprocess
import sys

from .. import common
from .log import block

LAYER = "command"
CREATE_NO_WINDOW = 0x08000000
TASK_NAME = "Chitragupta"

# ----------------------------------------------------------------------------------------------- git (work repos)

# Prepended to every work-repo git call: no pager, and none of the repo-config switches that make a read run a
# program (fsmonitor, external diff, ext:: transport, hooks, ssh command).
HARDEN = ["--no-pager", "-c", "core.pager=cat", "-c", "core.fsmonitor=false", "-c", "diff.external=",
          "-c", "protocol.ext.allow=never", "-c", "core.hooksPath=" + os.devnull,
          "-c", "core.sshCommand=ssh -o BatchMode=yes -o ConnectTimeout=15", "-c", "gc.auto=0",
          "-c", "maintenance.auto=false", "-c", "fetch.writeCommitGraph=false"]
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_PAGER": "cat",
           "PAGER": "cat", "GIT_EXTERNAL_DIFF": "", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=15"}
NO_DRIVERS = ["--no-ext-diff", "--no-textconv"]  # forced on log / show

WORK_READ = {"rev-parse", "for-each-ref", "log", "rev-list", "merge-base", "cherry", "patch-id", "show", "reflog",
             "branch", "ls-remote", "remote", "config", "fetch", "ls-files"}
_REFSPEC = re.compile(r"^\+?refs/heads/[^:\s]+:refs/remotes/[^/:\s]+/[^:\s]+$")
_FETCH_FLAGS = {"--no-tags", "--quiet", "-q", "--no-auto-gc", "--no-auto-maintenance", "--no-write-fetch-head",
                "--no-recurse-submodules"}
_PATCH_FLAGS = ("-p", "-u", "--patch", "--stat", "--numstat", "--name-only", "--name-status", "--raw", "--shortstat",
                "-U", "--unified")
_NEVER_FLAGS = ("--output", "--ext-diff", "--textconv", "--upload-pack", "--receive-pack", "--exec", "-c",
                "--config-env", "--git-dir", "--work-tree", "--open-files-in-pager", "-O")


def git_work_reason(args):
    """None when `args` (after the hardening prefix) is a read-only work-repo call, else why not."""
    a = list(args)
    if not a:
        return "empty git call"
    sub, rest = a[0], a[1:]
    if sub not in WORK_READ:
        return "git %s is not allowed in a work repo" % sub
    for x in rest:
        if x.startswith(_NEVER_FLAGS) and not (sub == "log" and x == "-c"):  # log -c = combined diff format
            return "git flag %s is not allowed" % x
    if sub in ("log", "show") and not all(f in rest for f in NO_DRIVERS):
        return "git %s needs --no-ext-diff --no-textconv" % sub
    if sub == "show" and (not any(x in ("-s", "--no-patch") for x in rest) or any(x.startswith(_PATCH_FLAGS) for x in rest)):
        return "git show is allowed for metadata only (-s / --no-patch)"
    if sub == "reflog" and (any(x in ("expire", "delete", "drop", "write") for x in rest) or
                            (rest and not rest[0].startswith(("-", "refs/", "HEAD", "show")))):
        return "git reflog may only show"
    if sub == "log" and any(x in ("-g", "--walk-reflogs") for x in rest) and "--expire" in rest:
        return "git log --expire is not allowed"
    if sub == "branch":
        if not any(x in ("--list", "-l", "--contains", "--merged", "--no-merged", "--points-at") for x in rest):
            return "git branch needs --list / --contains"
        if any(x in ("-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force", "-u",
                     "--set-upstream-to", "--unset-upstream", "--edit-description", "-t", "--track") or
               x.startswith("--set-upstream") for x in rest):
            return "git branch may only list"
    if sub == "remote" and rest[:1] != ["get-url"]:
        return "git remote: only get-url"
    if sub == "config" and (not rest or rest[0] not in ("--get", "--get-all", "--get-regexp") or
                            any(x in ("--add", "--unset", "--unset-all", "--replace-all", "--rename-section",
                                      "--remove-section", "-e", "--edit", "--file", "-f", "--global", "--system")
                                for x in rest)):
        return "git config: only --get"
    if sub == "ls-files" and any(x in ("--with-tree",) for x in rest):
        return "git ls-files flag not allowed"
    if sub == "fetch":
        specs = [x for x in rest if not x.startswith("-")]
        bad = [f for f in rest if f.startswith("-") and f not in _FETCH_FLAGS]
        if bad:
            return "git fetch flag(s) not allowed: %s" % " ".join(bad)
        if "--no-tags" not in rest:
            return "git fetch must use --no-tags"
        if len(specs) < 2 or not all(_REFSPEC.match(r) for r in specs[1:]):
            return "git fetch needs <remote> and explicit refs/heads/*:refs/remotes/<remote>/* refspecs"
        if any(not r.split(":", 1)[1].startswith("refs/remotes/%s/" % specs[0]) for r in specs[1:]):
            return "git fetch may only update refs/remotes/%s/*" % specs[0]
    return None


def work_argv(cfg, repo, args):
    """The full argv for a work-repo read: git -C <repo> <HARDEN> <sub> [--no-ext-diff --no-textconv] args…"""
    a = list(args)
    if a and a[0] in ("log", "show"):
        a = [a[0]] + [f for f in NO_DRIVERS if f not in a] + a[1:]
    return [common.git_exe(cfg), "-C", repo] + HARDEN + a


def _norm(p):
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(p)))
    except (TypeError, ValueError):
        return ""


def watched(cfg, repo):
    return _norm(repo) in {_norm(p) for p in cfg.get("watched_repos") or [] if p}


def observed(cfg, repo):
    """A watched repo, or a repo root inside the capture scope (where your captured sessions worked)."""
    if watched(cfg, repo):
        return True
    return bool(repo) and os.path.exists(os.path.join(repo, ".git")) and common.scope_allows(repo, cfg) and \
        not common.repo_ignored(repo)


def git_dir(repo):
    return common._git_dir(repo)


def locked(repo):
    """True while git holds any lock in the repo (a commit, checkout, rebase or fetch is running)."""
    import glob
    gd = git_dir(repo)
    if not gd:
        return False
    pats = [os.path.join(gd, "*.lock"), os.path.join(gd, "refs", "remotes", "*", "*.lock")]
    cdir = common.read_text(os.path.join(gd, "commondir")).strip()
    if cdir:  # a worktree: the shared refs live in the main .git
        cd = cdir if os.path.isabs(cdir) else os.path.normpath(os.path.join(gd, cdir))
        pats.append(os.path.join(cd, "*.lock"))
    return any(glob.glob(p) for p in pats)


def allow_fetch(cfg):
    return (cfg.get("git") or {}).get("allow_fetch", True) is not False


def _check_git_work(argv, cfg):
    if len(argv) < 4 or argv[1] != "-C":
        return "work-repo git must be git -C <repo> …"
    repo, rest = argv[2], argv[3:]
    if rest[:len(HARDEN)] != HARDEN:
        return "work-repo git without the hardening prefix"
    sub = rest[len(HARDEN):]
    why = git_work_reason(sub)
    if why:
        return why
    if sub[0] == "fetch":
        if not watched(cfg, repo):
            return "fetch only in a watched repo"
        if not allow_fetch(cfg):
            return "fetch is off (git.allow_fetch)"
        if locked(repo):
            return "a git lock file exists"
    elif not observed(cfg, repo):
        return "not a watched repo"
    return None


# ----------------------------------------------------------------------------------------------- git (data home)

def _home_remote(cfg):
    from .. import gitsync
    return gitsync.remote_url(cfg)


def _push_paths(cfg):
    from .. import gitsync
    return gitsync.push_paths(cfg)


def _rel_ok(p):
    return p and not p.startswith(("-", "/", "\\")) and ".." not in re.split(r"[\\/]", p) and ":" not in p


def git_home_reason(args, cfg):
    a = list(args)
    if not a:
        return "empty git call"
    sub, r = a[0], a[1:]
    msg_ok = lambda: len(r) >= 2  # noqa: E731
    if sub == "init":
        return None if r == ["-b", "main"] else "data-home git init: only -b main"
    if sub == "config":
        return None if len(r) == 2 and r[0] in ("core.autocrlf", "core.safecrlf") and r[1] in ("true", "false", "input") \
            else "data-home git config: only core.autocrlf / core.safecrlf"
    if sub == "remote":
        if r == ["get-url", "origin"]:
            return None
        if len(r) == 3 and r[0] in ("add", "set-url") and r[1] == "origin" and r[2] == _home_remote(cfg):
            return None
        return "data-home git remote: only get-url, or origin set to the configured github_remote"
    if sub == "status":
        return None if set(r) <= {"--porcelain", "-z", "--untracked-files=all", "--short"} else "status flag not allowed"
    if sub == "add":
        if r[:1] != ["--"] or len(r) < 2:
            return "data-home git add: only `add -- <files>`"
        from .. import gitsync
        bad = [p for p in r[1:] if not _rel_ok(p) or not gitsync.allowed_path(p, _push_paths(cfg))]
        return "data-home git add outside the push paths: %s" % bad[:3] if bad else None
    if sub == "reset":
        return None if r[:2] == ["-q", "--"] and len(r) > 2 and all(_rel_ok(p) for p in r[2:]) else \
            "data-home git reset: only `reset -q -- <files>`"
    if sub == "commit":
        return None if r[:2] == ["-q", "-m"] and len(r) == 3 else "data-home git commit: only -q -m <message>"
    if sub in ("rev-list", "merge-base", "rev-parse"):
        return None if not any(x.startswith(("--output", "--exec")) for x in r) else "flag not allowed"
    if sub == "diff":
        return None if "--cached" in r and "--name-only" in r and all(
            x in ("--cached", "--name-only", "-z") or x.startswith("--diff-filter=") for x in r) else "diff: only --cached --name-only"
    if sub == "ls-remote":
        return None if r == ["--heads", "origin", "main"] else "ls-remote: only --heads origin main"
    if sub == "fetch":
        return None if r == ["-q", "origin", "main"] else "data-home fetch: only -q origin main"
    if sub == "rebase":
        return None if r in (["-q", "origin/main"], ["--abort"]) else "rebase: only onto origin/main, or --abort"
    if sub == "merge":
        if r == ["--abort"]:
            return None
        if r[-1:] != ["origin/main"]:
            return "merge: only origin/main"
        i, body = 0, r[:-1]
        while i < len(body):
            x = body[i]
            if x in ("-q", "--allow-unrelated-histories"):
                i += 1
            elif x == "-X" and i + 1 < len(body) and body[i + 1] == "ours":
                i += 2
            elif x == "-m" and i + 1 < len(body):
                i += 2
            else:
                return "merge flag %s not allowed" % x
        return None
    if sub == "push":
        return None if r in (["-q", "-u", "origin", "main"], ["-q", "origin", "main"]) else \
            "data-home push: only `push -q [-u] origin main` (never forced)"
    return "git %s is not allowed in the data-home repo" % sub


def _check_git_home(argv, cwd, cfg):
    home = _norm(common.home())
    if len(argv) >= 3 and argv[1] == "-C":
        if _norm(argv[2]) != home:
            return "data-home git outside the data home"
        return git_home_reason(argv[3:], cfg)
    if argv[1:3] == ["init", "-q"] and len(argv) == 4:  # the Slack sender folder's own repo
        tgt = _norm(argv[3])
        return None if tgt.startswith(home + os.sep) else "git init outside the data home"
    if cwd and _norm(cwd) == home:
        return git_home_reason(argv[1:], cfg)
    return "data-home git needs -C <data home>"


# ----------------------------------------------------------------------------------------------- Git hosts

_API_BODY = ("-f", "-F", "--field", "--raw-field", "--input")
_CMD_META = re.compile(r"[&|<>^%\"\r\n]")


def _api_reason(r):
    i = 0
    while i < len(r):
        x = r[i]
        if x in ("-X", "--method"):
            if i + 1 >= len(r) or r[i + 1].upper() != "GET":
                return "api: only GET"
            i += 2
            continue
        if x.startswith(("--method=", "-X")) and x.split("=", 1)[-1].lstrip("-X").upper() != "GET":
            return "api: only GET"
        if x in _API_BODY or x.startswith(tuple(f + "=" for f in _API_BODY if f.startswith("--"))) or \
                re.match(r"^-[fF].", x):
            return "api: request bodies (-f / -F / --input) turn the call into a write"
        i += 1
    return None


def host_reason(exe, args):
    a = list(args)
    if exe == "gh":
        if a[:2] in (["pr", "list"], ["pr", "view"], ["pr", "status"], ["pr", "checks"], ["repo", "view"],
                     ["auth", "status"]):
            return "gh --web is not allowed" if any(x in ("-w", "--web") for x in a) else None
        if a[:1] == ["api"]:
            return _api_reason(a[1:])
        return "gh %s is not allowed (read-only: pr list/view/status/checks, repo view, auth status, api GET)" % " ".join(a[:2])
    if exe == "glab":
        if a[:2] in (["mr", "list"], ["mr", "view"], ["repo", "view"], ["auth", "status"]):
            return "glab --web is not allowed" if any(x in ("-w", "--web") for x in a) else None
        if a[:1] == ["api"]:
            return _api_reason(a[1:])
        return "glab %s is not allowed (read-only: mr list/view, repo view, auth status, api GET)" % " ".join(a[:2])
    if exe == "az":
        ok = a[:3] in (["repos", "pr", "list"], ["repos", "pr", "show"], ["repos", "ref", "list"],
                       ["boards", "work-item", "show"]) or a[:2] in (["repos", "list"], ["repos", "show"],
                                                                    ["account", "show"])
        if not ok:
            return "az %s is not allowed (allowlist: repos pr list|show, repos ref list, repos list|show, " \
                   "boards work-item show, account show)" % " ".join(a[:3])
        out = [a[i + 1] for i, x in enumerate(a[:-1]) if x in ("--output", "-o")]
        if out != ["json"]:
            return "az needs --output json"
        return None
    return "unknown host CLI"


# ----------------------------------------------------------------------------------------------- claude

BUILTIN_WRITES = ("Bash", "PowerShell", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
                  "Agent", "Task", "Skill")
_CLAUDE_NEVER = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--permission-prompt-tool",
                 "--add-dir", "--plugin-dir", "--agents", "--agent", "--ide", "--chrome")


def _opt(a, name):
    """Values of `name` (space or = form)."""
    out = []
    for i, x in enumerate(a):
        if x == name and i + 1 < len(a):
            out.append(a[i + 1])
        elif x.startswith(name + "="):
            out.append(x.split("=", 1)[1])
    return out


def _settings_guarded(val):
    try:
        s = json.loads(val)
    except (TypeError, ValueError):
        return False
    if s.get("disableAllHooks"):
        return False
    for grp in ((s.get("hooks") or {}).get("PreToolUse") or []):
        if grp.get("matcher") in ("*", ".*", "", None):
            for h in grp.get("hooks") or []:
                if h.get("onFailure") == "block" and "safety" in [str(x) for x in h.get("args") or []]:
                    return True
    return False


def claude_reason(args, cwd):
    a = list(args)
    if a == ["--version"] or a == ["mcp", "list"]:
        return None
    if a[:1] == ["mcp"]:
        return "claude mcp %s is not allowed (it writes Claude Code's settings)" % " ".join(a[1:2])
    for x in a:
        if x.startswith(_CLAUDE_NEVER) or "bypassPermissions" in x:
            return "claude flag %s is never allowed" % x
    if "-p" not in a and "--print" not in a:
        return "claude without -p"
    if _opt(a, "--permission-mode") != ["dontAsk"]:
        return "internal claude calls run with --permission-mode dontAsk"
    if _opt(a, "--permission-prompts") != ["none"]:
        return "internal claude calls run with --permission-prompts none"
    if _opt(a, "--tools") != [""]:
        return "internal claude calls get no built-in tools (--tools \"\")"
    st = _opt(a, "--settings")
    if len(st) != 1 or not _settings_guarded(st[0]):
        return "internal claude calls load the fail-closed guard settings"
    if not _opt(a, "--allowedTools"):
        return "internal claude calls pass an explicit --allowedTools"
    allowed = [t for v in _opt(a, "--allowedTools") for t in v.split(",") if t]
    if any(t.split("(")[0] in BUILTIN_WRITES for t in allowed):
        return "a write tool on --allowedTools"
    denied = {t for v in _opt(a, "--disallowedTools") for t in v.split(",") if t}
    if not set(BUILTIN_WRITES) <= denied:
        return "internal claude calls deny %s" % ", ".join(BUILTIN_WRITES)
    from .paths import sandbox
    from .. import slack
    places = {_norm(sandbox()), _norm(slack.sender_dir())}
    if not cwd or _norm(cwd) not in places:
        return "internal claude calls run in the sandbox folder"
    if "--strict-mcp-config" not in a and _norm(cwd) != _norm(slack.sender_dir()):
        return "internal claude calls use --strict-mcp-config"
    return None


def _claude_args(argv):
    """argv after the claude executable (native exe, or node + the npm cli*.js), or None."""
    e = exe_name(argv[0])
    if e == "claude":
        return argv[1:]
    if e == "node" and len(argv) > 1 and re.search(r"claude-code[\\/]cli[^\\/]*\.js$", argv[1].replace("\\", "/")):
        return argv[2:]
    return None


# ----------------------------------------------------------------------------------------------- self / task / probe

SELF_MODULES = {"plugin", "dashboard", "agent", "safety", "redact", "bruenv", "cli"}


def _self_reason(argv):
    e = exe_name(argv[0])
    if e not in ("python", "pythonw", "python3", "py"):
        return "not a Python interpreter"
    if len(argv) < 3:
        return "self call without a module"
    if argv[1:3] == ["-m", "chitragupta"]:  # the dashboard straight from the plugin folder (PYTHONPATH)
        return None if len(argv) > 3 and argv[3] in SELF_MODULES else "self call to module %s" % argv[3:4]
    from .. import plugin
    launchers = {_norm(common.launcher()), _norm(os.path.join(os.path.dirname(common.PACKAGE_DIR), "hooks", "run.py")),
                 _norm(plugin.own_launcher())}
    if _norm(argv[1]) not in launchers:
        return "self call through something other than Chitragupta's launcher"
    if argv[2] not in SELF_MODULES:
        return "self call to module %s" % argv[2]
    return None


def _task_reason(argv):
    a = [x.lower() for x in argv[1:]]
    if len(a) < 3 or a[1] != "/tn" or argv[3] != TASK_NAME:
        return "schtasks only for the %s task" % TASK_NAME
    if a[0] == "/create" and len(a) == 6 and a[3] == "/xml" and a[5] == "/f":
        from .paths import allowed
        return None if allowed(argv[5]) is None else "task XML outside the write roots"
    if a[0] == "/delete" and a[3:] == ["/f"]:
        return None
    if a[0] == "/query" and set(a[3:]) <= {"/fo", "list", "/v", "csv", "/nh"}:
        return None
    return "schtasks %s is not allowed" % " ".join(argv[1:2])


def _launchd_reason(argv):
    """launchctl only for the LaunchAgent scheduler.py writes: print / bootout gui/<uid>/<label>, bootstrap gui/<uid>
    <its plist>."""
    from .. import scheduler
    from .paths import allowed
    a, svc, dom = argv[1:], "gui/%s/%s" % (scheduler.uid(), scheduler.LABEL), "gui/%s" % scheduler.uid()
    if len(a) == 2 and a[0] in ("print", "bootout") and a[1] == svc:
        return None
    if len(a) == 3 and a[0] == "bootstrap" and a[1] == dom and _norm(a[2]) == _norm(scheduler.plist_path()):
        return None if allowed(a[2]) is None else "LaunchAgent outside the write roots"
    return "launchctl only for the %s LaunchAgent" % scheduler.LABEL


def _systemd_reason(argv):
    """systemctl --user only for Chitragupta's units (and daemon-reload after writing them); never system units."""
    from .. import scheduler
    a = argv[1:]
    if a[:1] != ["--user"]:
        return "systemctl only with --user"
    a = a[1:]
    if a == ["daemon-reload"]:
        return None
    if len(a) == 2 and a[0] in ("enable", "start", "stop", "disable", "is-enabled", "is-active", "status") and \
            a[1] in scheduler.UNITS:
        return None
    return "systemctl --user only for %s" % ", ".join(scheduler.UNITS)


def _cron_reason(argv):
    a = argv[1:]
    if a in (["-l"], ["-"]):
        return None
    return "crontab: only -l, and - for Chitragupta's own line"


def cron_input_reason(table):
    """`crontab -` replaces the whole table: allowed only when every line that isn't Chitragupta's is exactly the
    current table's (read here, from crontab -l) and at most one marked line remains."""
    from .. import scheduler
    try:
        p = subprocess.run(["crontab", "-l"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=30, env=_env(None))
    except (OSError, subprocess.TimeoutExpired) as e:
        return "crontab -l failed before a change: %s" % e
    if p.returncode != 0 and "no crontab" not in ((p.stderr or "") + (p.stdout or "")).lower():
        return "crontab -l failed before a change"
    current = p.stdout if p.returncode == 0 else ""
    if scheduler.others(table or "") != scheduler.others(current):
        return "the new crontab changes lines that aren't Chitragupta's"
    if sum(1 for ln in (table or "").splitlines() if scheduler.is_ours(ln)) > 1:
        return "more than one Chitragupta crontab line"
    return None


def _probe_reason(argv):
    e, a = exe_name(argv[0]), argv[1:]
    if (e in ("git", "claude") and a == ["--version"]) or (e == "py" and a == ["-3", "--version"]):
        return None
    if _claude_args(argv) == ["--version"]:
        return None
    return "only version probes"


# ----------------------------------------------------------------------------------------------- the check

def exe_name(p):
    b = os.path.basename(str(p or "")).lower()
    for ext in (".exe", ".cmd", ".bat", ".com"):
        if b.endswith(ext):
            return b[:-len(ext)]
    return b


def reason(argv, purpose, cwd=None, cfg=None):
    """None when allowed, else why not."""
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(x, str) for x in argv):
        return "argv must be a non-empty list of strings"
    argv = list(argv)
    e = exe_name(argv[0])
    if argv[0].lower().endswith((".cmd", ".bat")) and any(_CMD_META.search(x) for x in argv[1:]):
        return "shell metacharacters in arguments to a .cmd / .bat"
    cfg = cfg if cfg is not None else common.load_config()
    if purpose == "git-work":
        return _check_git_work(argv, cfg) if e == "git" else "git-work runs git only"
    if purpose == "git-home":
        return _check_git_home(argv, cwd, cfg) if e == "git" else "git-home runs git only"
    if purpose == "git-probe":
        return None if e == "git" and argv[1:] == ["--version"] else "git-probe: only git --version"
    if purpose == "host":
        return host_reason(e, argv[1:]) if e in ("gh", "glab", "az") else "host: only gh, glab, az"
    if purpose == "claude":
        a = _claude_args(argv)
        return claude_reason(a, cwd) if a is not None else "claude: not the claude executable"
    if purpose == "self":
        return _self_reason(argv)
    if purpose == "task":
        return _task_reason(argv) if e == "schtasks" else "task: only schtasks"
    if purpose == "probe":
        return _probe_reason(argv)
    if purpose == "launchd":
        return _launchd_reason(argv) if e == "launchctl" else "launchd: only launchctl"
    if purpose == "systemd":
        return _systemd_reason(argv) if e == "systemctl" else "systemd: only systemctl"
    if purpose == "cron":
        return _cron_reason(argv) if e == "crontab" else "cron: only crontab"
    return "unknown purpose %r" % purpose


def check(argv, purpose, cwd=None, cfg=None, caller=""):
    why = reason(argv, purpose, cwd, cfg)
    if why:
        block(LAYER, why, " ".join(str(x) for x in argv) if isinstance(argv, (list, tuple)) else repr(argv),
              purpose, caller or _caller())
    return argv


def _caller():
    f = sys._getframe(3) if hasattr(sys, "_getframe") else None
    return (f.f_globals.get("__name__", "") if f else "")


def _env(env_extra):
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    env.update(common.INTERNAL_ENV)
    if env_extra:
        env.update(env_extra)
    for k in [k for k, v in env.items() if v == "" and k == "GIT_EXTERNAL_DIFF"]:
        del env[k]
    return env


def run(argv, purpose, cwd=None, input_text=None, timeout=120, env_extra=None, cfg=None):
    """Check, then run hidden, never through a shell. Returns (rc, stdout, stderr). Raises Blocked."""
    check(argv, purpose, cwd, cfg)
    if purpose == "cron" and list(argv[1:]) == ["-"]:
        why = cron_input_reason(input_text)
        if why:
            block(LAYER, why, "crontab -", purpose, _caller())
    if purpose == "git-work":
        env_extra = dict(GIT_ENV, **(env_extra or {}))
    try:
        p = subprocess.run(list(argv), cwd=cwd, input=input_text, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=_env(env_extra),
                           creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %ss" % timeout
    except OSError as e:
        return 127, "", str(e)


_spawned = []


def spawn(argv, cwd=None, env_extra=None, log_path=None, internal=True, cfg=None, popen=None):
    """Start one of Chitragupta's own processes detached and windowless (purpose `self`); never waits.
    `log_path` gets stdout/stderr (appended). Returns the Popen object; raises OSError when it can't start."""
    check(argv, "self", cwd, cfg)
    from . import paths
    logf = None
    if log_path:
        try:
            logf = paths.open_write(log_path, "a", encoding="utf-8")
        except OSError:
            logf = None
    env = _env(env_extra)
    if not internal:  # a long-lived server: its own children are not "internal"
        for k in common.INTERNAL_ENV:
            env.pop(k, None)
    kw = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=logf or subprocess.DEVNULL,
              stderr=subprocess.STDOUT if logf else subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW | 0x00000008 | 0x00000200  # no window, DETACHED, NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        proc = (popen or subprocess.Popen)(list(argv), **kw)
        _spawned.append(proc)  # never waited on; kept so Popen.__del__ does not warn
        return proc
    finally:
        if logf:
            logf.close()


def terminate(pid):
    """Stop one of our own processes (the dashboard, identified through its /api/health)."""
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x0001, False, int(pid))  # PROCESS_TERMINATE
            if not h:
                return False
            try:
                return bool(k.TerminateProcess(h, 1))
            finally:
                k.CloseHandle(h)
        import signal
        os.kill(int(pid), signal.SIGTERM)
        return True
    except Exception:  # noqa: BLE001
        return False
