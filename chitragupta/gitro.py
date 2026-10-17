"""Read-only git for work repos. Every git call against a watched repo goes through `run()` here.

Allowed: rev-parse, for-each-ref, log, rev-list, merge-base, cherry, patch-id, show (metadata only), reflog (show),
branch --list / --contains, ls-remote, remote get-url, config --get, fetch (remote-tracking refs only, never prune),
ls-files (untracked files, for artifacts).
Anything else raises NotAllowed before a process starts. The data home's own repo (gitsync.py) is not a work repo
and does not use this module.

Every call: no window, no terminal prompt, GIT_OPTIONAL_LOCKS=0 (read commands never take the index lock).
"""
import glob
import os
import re

from . import common

READ_ONLY = {"rev-parse", "for-each-ref", "log", "rev-list", "merge-base", "cherry", "patch-id", "show", "reflog",
             "branch", "ls-remote", "remote", "config", "fetch", "ls-files"}
# git's global options that may come before the subcommand
_GLOBAL_OK = {"--no-pager", "--literal-pathspecs", "--no-replace-objects"}
_REFSPEC = re.compile(r"^\+?refs/heads/[^:\s]+:refs/remotes/[^/:\s]+/[^:\s]+$")
_SAFE_FETCH_FLAGS = {"--no-tags", "--quiet", "-q", "--no-auto-gc", "--no-auto-maintenance", "--no-write-fetch-head",
                     "--no-recurse-submodules"}
_PATCH_FLAGS = ("-p", "-u", "--patch", "--stat", "--numstat", "--name-only", "--name-status", "--raw", "--shortstat",
                "-U", "--unified")
ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
       "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=15"}


class NotAllowed(Exception):
    pass


def check(args):
    """Raise NotAllowed unless `args` (everything after `git -C <repo>`) is a read-only call."""
    a = list(args)
    while a and a[0] in _GLOBAL_OK:
        a.pop(0)
    if not a:
        raise NotAllowed("empty git call")
    sub, rest = a[0], a[1:]
    if sub not in READ_ONLY:
        raise NotAllowed("git %s is not allowed on a work repo" % sub)
    if sub == "show" and (not any(x in ("-s", "--no-patch") for x in rest) or any(x.startswith(_PATCH_FLAGS) for x in rest)):
        raise NotAllowed("git show is allowed for metadata only (-s / --no-patch)")
    if sub == "reflog" and rest and rest[0] not in ("show",) and not rest[0].startswith(("-", "refs/", "HEAD")):
        raise NotAllowed("git reflog %s is not allowed" % rest[0])
    if sub == "reflog" and any(x in ("expire", "delete") for x in rest):
        raise NotAllowed("git reflog expire/delete is not allowed")
    if sub == "branch":
        if not rest or not any(x in ("--list", "-l", "--contains", "--merged", "--no-merged", "--points-at") for x in rest):
            raise NotAllowed("git branch needs --list / --contains")
        if any(x in ("-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force", "-u",
                     "--set-upstream-to", "--unset-upstream", "--edit-description") or x.startswith("--set-upstream")
               for x in rest):
            raise NotAllowed("git branch may only list")
    if sub == "remote" and (not rest or rest[0] != "get-url"):
        raise NotAllowed("git remote: only get-url")
    if sub == "config" and (not rest or rest[0] not in ("--get", "--get-all", "--get-regexp")):
        raise NotAllowed("git config: only --get")
    if sub == "fetch":
        refspecs = [x for x in rest if not x.startswith("-")]
        flags = [x for x in rest if x.startswith("-")]
        bad = [f for f in flags if f not in _SAFE_FETCH_FLAGS]
        if bad:
            raise NotAllowed("git fetch flag(s) not allowed: %s" % " ".join(bad))
        if "--no-tags" not in flags:
            raise NotAllowed("git fetch must use --no-tags")
        if len(refspecs) < 2 or not all(_REFSPEC.match(r) for r in refspecs[1:]):
            raise NotAllowed("git fetch needs <remote> and explicit refs/heads/*:refs/remotes/<remote>/* refspecs")
        remote = refspecs[0]
        if any(not r.split(":", 1)[1].startswith("refs/remotes/%s/" % remote) for r in refspecs[1:]):
            raise NotAllowed("git fetch may only update refs/remotes/%s/*" % remote)
    if sub == "log" and any(x in ("-g", "--walk-reflogs") for x in rest) and any(x == "--expire" for x in rest):
        raise NotAllowed("not allowed")
    return True


def run(cfg, repo, args, timeout=60, input_text=None):
    """(rc, stdout, stderr) for a checked read-only call."""
    check(args)
    return common.run([common.git_exe(cfg), "-C", repo] + list(args), timeout=timeout, input_text=input_text, env_extra=ENV)


def ok(cfg, repo, args, timeout=60):
    """stdout when rc == 0, else ''."""
    rc, so, _ = run(cfg, repo, args, timeout=timeout)
    return so if rc == 0 else ""


def is_ancestor(cfg, repo, a, b):
    return run(cfg, repo, ["merge-base", "--is-ancestor", a, b])[0] == 0


def rev(cfg, repo, ref):
    so = ok(cfg, repo, ["rev-parse", "-q", "--verify", ref + "^{commit}"])
    return so.strip() or None


def git_dir(repo):
    return common._git_dir(repo)


def locked(repo):
    """True while git holds any lock in the repo (a commit, checkout, rebase or fetch is running)."""
    gd = git_dir(repo)
    if not gd:
        return False
    pats = [os.path.join(gd, "*.lock"), os.path.join(gd, "refs", "remotes", "*", "*.lock")]
    common_dir = common.read_text(os.path.join(gd, "commondir")).strip()
    if common_dir:  # a worktree: the shared refs live in the main .git
        cd = common_dir if os.path.isabs(common_dir) else os.path.normpath(os.path.join(gd, common_dir))
        pats += [os.path.join(cd, "*.lock")]
    return any(glob.glob(p) for p in pats)


def fetch(cfg, repo, remote, branches, timeout=180):
    """git fetch --no-tags <remote> +refs/heads/<b>:refs/remotes/<remote>/<b> ... ; never prunes, never while locked.
    Returns (ok, message)."""
    if locked(repo):
        return False, "skipped: a git lock file exists"
    specs = ["+refs/heads/%s:refs/remotes/%s/%s" % (b, remote, b) for b in sorted(set(branches)) if _safe_name(b)]
    if not specs:
        return False, "nothing to fetch"
    rc, so, se = run(cfg, repo, ["fetch", "--no-tags", "--quiet", "--no-auto-gc", "--no-auto-maintenance",
                                 "--no-recurse-submodules", remote] + specs, timeout=timeout)
    return rc == 0, (se or so).strip()[:300]


def _safe_name(b):
    return bool(b) and not b.startswith("-") and ".." not in b and not re.search(r"[\s~^:?*\[\\]", b)
