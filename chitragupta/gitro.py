"""Read-only git for work repos. Every git call against a work repo goes through `run()` here, which goes through
the safety layer's command guard (chitragupta/safety/commands.py: purpose `git-work`).

Allowed: rev-parse, for-each-ref, log, rev-list, merge-base, cherry, patch-id, show (metadata only), reflog (show),
branch --list / --contains, ls-remote, remote get-url, config --get, fetch (remote-tracking refs only, never prune,
only in a watched repo, git.allow_fetch), ls-files (untracked files, for artifacts).
Every call is hardened against the repo's own config (no pager, fsmonitor, external diff, textconv, ext:: transport
or hooks) and runs with GIT_OPTIONAL_LOCKS=0, no window and no terminal prompt. Anything else raises NotAllowed
(safety Blocked) before a process starts. The data home's own repo (gitsync.py) is not a work repo.
"""
import re

from .safety import commands
from .safety.log import Blocked as NotAllowed

READ_ONLY = commands.WORK_READ
ENV = commands.GIT_ENV
_GLOBAL_OK = {"--no-pager", "--literal-pathspecs", "--no-replace-objects"}


def check(args):
    """Raise NotAllowed unless `args` (everything after `git -C <repo>`) is a read-only call. Pure: nothing is logged."""
    a = list(args)
    while a and a[0] in _GLOBAL_OK:
        a.pop(0)
    if a and a[0] in ("log", "show"):
        a = [a[0]] + [f for f in commands.NO_DRIVERS if f not in a] + a[1:]
    why = commands.git_work_reason(a)
    if why:
        raise NotAllowed(commands.LAYER, why, "git " + " ".join(a), "git-work")
    return True


def run(cfg, repo, args, timeout=60, input_text=None):
    """(rc, stdout, stderr) for a checked read-only call."""
    a = list(args)
    while a and a[0] in _GLOBAL_OK:
        a.pop(0)
    return commands.run(commands.work_argv(cfg, repo, a), "git-work", timeout=timeout, input_text=input_text, cfg=cfg)


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
    return commands.git_dir(repo)


def locked(repo):
    """True while git holds any lock in the repo (a commit, checkout, rebase or fetch is running)."""
    return commands.locked(repo)


def fetch(cfg, repo, remote, branches, timeout=180):
    """git fetch --no-tags <remote> +refs/heads/<b>:refs/remotes/<remote>/<b> ... ; never prunes, never while locked.
    Returns (ok, message)."""
    if not commands.allow_fetch(cfg):
        return False, "skipped: fetch is off (git.allow_fetch)"
    if locked(repo):
        return False, "skipped: a git lock file exists"
    specs = ["+refs/heads/%s:refs/remotes/%s/%s" % (b, remote, b) for b in sorted(set(branches)) if _safe_name(b)]
    if not specs:
        return False, "nothing to fetch"
    rc, so, se = run(cfg, repo, ["fetch", "--no-tags", "--quiet", "--no-auto-gc", "--no-auto-maintenance",
                                 "--no-recurse-submodules", "--no-write-fetch-head", remote] + specs, timeout=timeout)
    return rc == 0, (se or so).strip()[:300]


def _safe_name(b):
    return bool(b) and not b.startswith("-") and ".." not in b and not re.search(r"[\s~^:?*\[\\]", b)
