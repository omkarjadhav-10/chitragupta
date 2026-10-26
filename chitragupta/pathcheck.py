"""The `pathcheck` tick step: model-mentioned file paths are checked against git before the hub shows them.

A file record's `verified` is True (edited by an Edit/Write tool call, in a commit, or found in git), False (not in the
repo at the heads checked, or not a path git can check) or None (pending). Each pending path is checked with the
read-only `git rev-parse --verify -q <sha>:<path>` (through gitro, so the safety layer's work-repo guard applies)
against one preferred head per ticket branch (remote, else local) in the repo the record belongs to, or the repo's HEAD when no branch is tracked.
Whether a path exists at a commit never changes, so answers are cached per (repo, sha, path) in
.worklog/cache/pathcheck.json. At most `budget` git calls run per tick; the rest waits for the next tick.

The same step tells committed files from files only edited in a session: a changed file whose only proof is an
Edit/Write tool call gets `committed` True or False from the file lists of the ticket's commits (recorded by the
commit scan, or for older commits read with `git log --no-walk --name-only`, cached per (repo, sha)).
"""
import os

from . import common
from . import gitcmd
from . import gitro

BUDGET = 40
CACHE_MAX = 20000
COMMIT_CACHE_MAX = 3000
_SEP = "\x1e"


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
        self.commit_files = cache.get("commit_files") or {}
        self.cache = cache
        self.waiting = False
        self.pending_paths = ()

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
                    sha = b.get("remote_tip") or b.get("tip")
                    if sha and sha not in tips:
                        tips.append(sha)
            if not tips:
                if self.calls >= self.budget:
                    return None
                self.calls += 1
                head = gitro.rev(self.cfg, root, "HEAD")
                tips = [head] if head else []
            self.heads_memo[key] = tips
        return self.heads_memo[key]

    def check(self, t, tk, rec, path):
        """True / False when answered, None only when out of budget; missing local repositories are terminal unknown-repo."""
        waiting, unknown = False, []
        for name in [rec["repo"]] if rec.get("repo") else list(tk.get("repos") or []):
            root = repo_root(self.cfg, name, rec)
            if not root:
                unknown.append(name)
                continue
            heads = self.heads(t, name, root)
            if heads is None:
                waiting = True
                continue
            for sha in heads:
                ck = "%s|%s|%s" % (gitcmd.repo_key(name), sha, path)
                if ck not in self.known:
                    if self.calls >= self.budget:
                        waiting = True
                        break
                    if len(self.pending_paths) >= 8:
                        self.calls += 1
                        rc, so, _ = gitro.run(self.cfg,root,["ls-tree","-r","-z","--name-only",sha])
                        if rc not in (0,128):
                            waiting = True
                            break
                        paths = set(so.split("\0")) if rc == 0 else set()
                        for pending in self.pending_paths:
                            self.known["%s|%s|%s" % (gitcmd.repo_key(name),sha,pending)] = pending in paths
                    else:
                        self.known[ck] = self.git_ok(root, ["rev-parse", "--verify", "-q", "%s:%s" % (sha, path)])
                if self.known[ck]:
                    rec["repo"] = name
                    return True
        if not waiting and unknown:
            rec["verified_by"] = "unknown-repo"
            rec["unknown_repos"] = unknown
        return None if waiting else False


    def files_of(self, name, shas):
        """{sha: [paths]} of the commits in repo `name` whose file lists can be read now (cached, else git)."""
        out, todo = {}, []
        for sha in shas:
            ck = "%s|%s" % (gitcmd.repo_key(name), sha)
            if ck in self.commit_files:
                out[sha] = self.commit_files[ck]
            else:
                todo.append(sha)
        root = repo_root(self.cfg, name) if todo else None
        if todo and not root:
            for sha in todo:
                self.commit_files["%s|%s" % (gitcmd.repo_key(name),sha)] = None
            return out
        batches = [todo[i:i+50] for i in range(0,len(todo),50)]
        while batches:
            if self.calls >= self.budget:
                self.waiting = True
                break
            part = batches.pop(0)
            self.calls += 1
            rc, so, _ = gitro.run(self.cfg, root, ["log", "--no-walk", "--name-only", "--format=" + _SEP + "%H"] + part)
            if rc == 128:  # a rewritten/missing object must not poison the whole batch forever
                if len(part) > 1:
                    mid = len(part)//2
                    batches[:0] = [part[:mid],part[mid:]]
                else:
                    self.commit_files["%s|%s" % (gitcmd.repo_key(name),part[0])] = None
                    out[part[0]] = None  # unknown proof, never a negative committed verdict
                continue
            if rc != 0:
                self.waiting = True  # timeout or other transient failure: retry next tick
                break
            for rec in so.split(_SEP):
                lines = [x.strip() for x in rec.strip().splitlines() if x.strip()]
                if not lines:
                    continue
                full = lines[0]
                sha = next((s for s in part if full.startswith(s)), full)
                out[sha] = self.commit_files["%s|%s" % (gitcmd.repo_key(name), sha)] = lines[1:200]
        return out

    def mark_committed(self, tk):
        """Set `committed` on the ticket's files changed only by Edit/Write calls. Returns True when one changed."""
        edits = [f for f in (tk.get("files") or {}).values()
                 if isinstance(f, dict) and f.get("changed") and f.get("verified_by") == "edit"]
        if not edits:
            return False
        shas = {}  # repo key -> {sha: recorded files or None}
        for c in (tk.get("commits") or {}).values():
            if isinstance(c, dict) and c.get("sha") and c.get("repo_name"):
                shas.setdefault(c["repo_name"], {})[c["sha"]] = c.get("files") if isinstance(c.get("files"), list) else None
        for name, x in ((tk.get("git") or {}).get("repos") or {}).items():
            for row in (x or {}).get("rows") or []:
                for sha in row.get("shas") or []:
                    have = shas.setdefault(name, {})
                    if not any(s.startswith(sha) or sha.startswith(s) for s in have):
                        have[sha] = None
        files, unknown = {}, set()
        for name, m in shas.items():
            got = self.files_of(name, [s for s, fl in m.items() if fl is None])
            key = gitcmd.repo_key(name)
            for sha, fl in m.items():
                fl = fl if fl is not None else got.get(sha)
                if fl is None:
                    unknown.add(key)
                else:
                    files.setdefault(key, set()).update(fl)
        changed = False
        for f in edits:
            repos = [gitcmd.repo_key(f["repo"])] if f.get("repo") else list(files) + list(unknown)
            path = (f.get("path") or "").replace("\\", "/")
            if any(path in files.get(k, ()) for k in repos):
                val = True
            elif any(k in unknown for k in repos):
                continue  # a commit whose files aren't known yet: ask again on a later tick
            else:
                val = False
            if f.get("committed") is not val:
                f["committed"] = val
                changed = True
        return changed


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
    tickets = sorted(todo)
    cursor = r.cache.get("cursor")
    if cursor in tickets:
        n = tickets.index(cursor) + 1
        tickets = tickets[n:] + tickets[:n]
    for n,t in enumerate(tickets):
        # Give each remaining ticket its share; a tiny budget rotates on the next tick.
        quota = max(0,(budget-r.calls + len(tickets)-n-1) // (len(tickets)-n))
        r.budget = min(budget,r.calls+quota)
        before = r.calls
        tk = store.ticket(t)
        r.pending_paths = [(rec.get("path") or k).replace("\\","/") for k,rec in (tk.get("files") or {}).items() if _pending(rec)]
        r.waiting = False
        changed = r.mark_committed(tk)  # cached/recorded commit proof settles before path lookups
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
        if r.calls > before:
            cursor = t
        if changed:
            store.touch_ticket(t)
        if r.waiting or any(_pending(x) for x in (tk.get("files") or {}).values()):
            left.add(t)
    known = r.known
    if len(known) > CACHE_MAX:
        known = dict(list(known.items())[-CACHE_MAX:])
    cf = r.commit_files
    if len(cf) > COMMIT_CACHE_MAX:
        cf = dict(list(cf.items())[-COMMIT_CACHE_MAX:])
    common.write_json(cache_path(), {"seeded": True, "pending": sorted(left), "paths": known, "commit_files": cf, "cursor": cursor})
    if r.calls:
        common.log("agent", "pathcheck: %d git call(s), %d ticket(s) still pending" % (r.calls, len(left)))
    return r.calls
