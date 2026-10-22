"""The `pathcheck` tick step: model-mentioned file paths are checked against git before the hub shows them.

A file record's `verified` is True (edited by an Edit/Write tool call, in a commit, or found in git), False (not in the
repo at the heads checked, or not a path git can check) or None (pending). Each pending path is checked with the
read-only `git rev-parse --verify -q <sha>:<path>` (through gitro, so the safety layer's work-repo guard applies)
against the ticket's branch heads in the repo the record belongs to, or the repo's HEAD when no branch is tracked.
Whether a path exists at a commit never changes, so answers are cached per (repo, sha, path) in
.worklog/cache/pathcheck.json. At most `budget` git calls run per tick; the rest waits for the next tick.
"""
import os

from . import common
from . import gitcmd
from . import gitro

BUDGET = 40
CACHE_MAX = 20000


def cache_path():
    return common.wl("cache", "pathcheck.json")


def repo_root(cfg, name, rec=None):
    """The local folder of repo `name` (a basename): the record's repo_root, the branch-tracking repo.json path, or a
    watched repo with that name. None when unknown."""
    meta = common.read_json(common.hp("data", "git", gitcmd.repo_key(name), "repo.json"), {}) or {}
    for c in [(rec or {}).get("repo_root"), meta.get("path")] + list(cfg.get("watched_repos") or []):
        if c and gitcmd.repo_key(c) == gitcmd.repo_key(name) and os.path.exists(os.path.join(c, ".git")):
            return c
    return None


def checkable(path):
    """A repo-relative path git can look up (not absolute, not a bare word)."""
    return bool(path) and not path.startswith("/") and ":" not in path and ("/" in path or "." in path)


class _Run:
    def __init__(self, store, cfg, budget):
        self.store, self.cfg, self.budget = store, cfg, budget
        self.calls, self.heads_memo = 0, {}
        cache = common.read_json(cache_path(), {}) or {}
        self.known = cache.get("paths") or {}
        self.cache = cache

    def git_ok(self, root, args):
        self.calls += 1
        return gitro.run(self.cfg, root, args)[0] == 0

    def heads(self, t, name, root):
        """The ticket's branch tips in this repo (remote tip first), else the repo's HEAD; None when out of budget."""
        key = (t, name)
        if key not in self.heads_memo:
            tips = []
            for b in (self.store.branches().get(gitcmd.repo_key(name)) or {}).values():
                if isinstance(b, dict) and b.get("ticket") == t and not b.get("deleted_at"):
                    tips += [s for s in (b.get("remote_tip"), b.get("tip")) if s and s not in tips]
            if not tips:
                if self.calls >= self.budget:
                    return None
                self.calls += 1
                head = gitro.rev(self.cfg, root, "HEAD")
                tips = [head] if head else []
            self.heads_memo[key] = tips
        return self.heads_memo[key]

    def check(self, t, tk, rec, path):
        """True / False when answered, None when it has to wait (no repo folder known, or out of budget)."""
        waiting = False
        for name in [rec["repo"]] if rec.get("repo") else list(tk.get("repos") or []):
            root = repo_root(self.cfg, name, rec)
            heads = self.heads(t, name, root) if root else None
            if heads is None:
                waiting = True
                continue
            for sha in heads:
                ck = "%s|%s|%s" % (gitcmd.repo_key(name), sha, path)
                if ck not in self.known:
                    if self.calls >= self.budget:
                        waiting = True
                        break
                    self.known[ck] = self.git_ok(root, ["rev-parse", "--verify", "-q", "%s:%s" % (sha, path)])
                if self.known[ck]:
                    rec["repo"] = name
                    return True
        return None if waiting else False


def _pending(rec):
    return isinstance(rec, dict) and rec.get("verified") is None


def run(store, cfg, budget=BUDGET):
    """Check the pending paths of the tickets that may have some. Returns the number of git calls made."""
    from .store import Store
    r = _Run(store, cfg, budget)
    todo = set(store.dirty_tickets) | set(r.cache.get("pending") or [])
    if not r.cache.get("seeded"):
        todo |= set(Store.all_ticket_ids())  # once: records from before this step existed
    left = set()
    for t in sorted(todo):
        tk = store.ticket(t)
        changed = False
        for k, rec in (tk.get("files") or {}).items():
            if not _pending(rec):
                continue
            path = (rec.get("path") or k).replace("\\", "/")
            ok = r.check(t, tk, rec, path) if checkable(path) else False
            if ok is not None:
                rec.update(verified=ok, checked_at=common.now_iso())
                if ok:
                    rec["verified_by"] = "git"
                changed = True
        if changed:
            store.touch_ticket(t)
        if any(_pending(x) for x in (tk.get("files") or {}).values()):
            left.add(t)
    known = r.known
    if len(known) > CACHE_MAX:
        known = dict(list(known.items())[-CACHE_MAX:])
    common.write_json(cache_path(), {"seeded": True, "pending": sorted(left), "paths": known})
    if r.calls:
        common.log("agent", "pathcheck: %d git call(s), %d ticket(s) still pending" % (r.calls, len(left)))
    return r.calls
