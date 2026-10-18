"""Branches, commits and PRs per ticket, from git and the Git host's CLI (no model calls), or, without a CLI, the
host's read-only MCP server (one budgeted job per repo per refresh, hostmcp.py).

Per watched repo, under <home>/data/git/<repo>/:
  events.jsonl     append-only: branch creations, checkouts, commits, cherry-picks, merges, pushes, PR links.
                   From Claude Code sessions (the captured command) and from the reflog (anything outside Claude Code).
                   Kept for good, because git expires reflog entries.
  branches.json    {branch: record}  the ticket branches: parent (and how it was found), link (and how), own commits,
                   pushed / unpushed, ahead / behind, status (active · pr_open · merged · deleted_remote · abandoned)
  prs.json         {number: PR}  from gh / glab / az or the host's MCP, else from the PR merge commits on the bases
  patch-ids.json   {sha: patch-id}  never changes for a sha, so computed once
  repo.json        host, PR source, cursors, last fetch, manual links / abandons / strategy
Each ticket record gets `git` = {strategy, branches, prs, delivery, alerts} (delivery.py), recomputed when anything moves.

Work repos are read-only: every git call goes through gitro.
"""
import fnmatch
import json
import os
import re

from . import branches as legacy
from . import common
from . import gitro
from . import hosts
from .gitcmd import (  # noqa: F401 - settings and command parsing live in the light module (the guard uses it)
    DEFAULT_BASES, DEFAULT_STRATEGIES, DEFAULT_SUFFIXES, FETCH, MAX_OWN, PR_REFRESH_MINUTES, RELEASE_LOOKBACK_DAYS,
    STATUSES, URL_RX, _list, ignored, is_pattern_role, parse_command, pr_urls, repo_conf, repo_key, role_of, settings,
    suffix_role)
from .redact import redact

ALGO = 3


# --------------------------------------------------------------------------- storage

def gdir(name, *parts):
    return common.hp("data", "git", name, *parts)


class RepoData:
    """The four files of one repo; loaded lazily, saved only when changed."""

    def __init__(self, name):
        self.name = name
        self._b = self._p = self._pid = self._meta = None
        self.dirty = set()

    def _load(self, attr, fname, default):
        if getattr(self, attr) is None:
            setattr(self, attr, common.read_json(gdir(self.name, fname), None) or default)
        return getattr(self, attr)

    @property
    def branches(self):
        return self._load("_b", "branches.json", {})

    @property
    def prs(self):
        return self._load("_p", "prs.json", {})

    @property
    def patch_ids(self):
        return self._load("_pid", "patch-ids.json", {})

    @property
    def meta(self):
        return self._load("_meta", "repo.json", {})

    def touch(self, what):
        self.dirty.add(what)

    def save(self):
        files = {"branches": ("branches.json", self._b), "prs": ("prs.json", self._p),
                 "patch_ids": ("patch-ids.json", self._pid), "meta": ("repo.json", self._meta)}
        for what in sorted(self.dirty):
            fname, obj = files[what]
            if obj is not None:
                common.write_json(gdir(self.name, fname), obj)
        self.dirty.clear()

    # events: append-only
    def events(self):
        out = []
        for line in common.read_text(gdir(self.name, "events.jsonl")).splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def append_events(self, evs):
        if not evs:
            return 0
        known = {_ev_key(e) for e in self.events()}
        n = 0
        for e in evs:
            k = _ev_key(e)
            if k in known:
                continue
            known.add(k)
            common.append_line(gdir(self.name, "events.jsonl"), json.dumps(e, ensure_ascii=False, sort_keys=True))
            n += 1
        return n


def _ev_key(e):
    return (e.get("ts"), e.get("kind"), e.get("ref") or e.get("branch"), e.get("sha"), e.get("src"), e.get("text"),
            e.get("pr_url"))


def all_repo_names():
    root = common.hp("data", "git")
    try:
        return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return []


def session_events(ev, ticket=None):
    """Raw post-tool hook event -> repo events (src=session). Only the command and the PR URL are kept."""
    p = ev.get("payload") or {}
    if p.get("tool") not in ("Bash", "PowerShell"):
        return []
    cmd = ((p.get("input") or {}).get("command") or "")
    urls = ((p.get("outcome") or {}).get("pr_urls") or [])
    if "git" not in cmd and not urls and " pr " not in cmd and " mr " not in cmd:
        return []
    out = []
    for c in parse_command(cmd):
        rec = {"ts": ev.get("ts"), "src": "session", "session": (ev.get("session_id") or "")[:12], "ticket": ticket,
               "at_branch": ev.get("branch")}
        rec.update({k: v for k, v in c.items() if v not in (None, [], "")})
        if c["kind"] == "pr-create" and urls:
            rec["pr_url"] = urls[0]
        out.append(rec)
    return out


# --------------------------------------------------------------------------- reflog events

_REFLOG = [
    (re.compile(r"^branch: Created from (.+)$"), "create"),
    (re.compile(r"^checkout: moving from (.+) to (.+)$"), "checkout"),
    (re.compile(r"^cherry-pick: (.+)$"), "cherry-pick"),
    (re.compile(r"^(?:merge|pull)\b.*?:\s*(.*)$"), "merge"),
    (re.compile(r"^rebase.*\((?:finish|start)\)"), "rebase"),
    (re.compile(r"^commit(?: \((?:amend|merge|initial)\))?: (.+)$"), "commit"),
    (re.compile(r"^reset: moving to (.+)$"), "reset"),
    (re.compile(r"^update by push$"), "push"),
    (re.compile(r"^fetch\b|^pull\b.*fast-forward"), "fetch"),
]


def reflog_events(repo, ref, since=None, limit=2000):
    """Reflog entries of `ref` newer than `since` (iso) as events, oldest first."""
    out = []
    for ts, msg, sha in sorted(repo.reflog(ref, limit)):
        if since and ts <= since:
            continue
        kind = None
        for rx, k in _REFLOG:
            m = rx.match(msg)
            if m:
                kind = k
                break
        if not kind or kind == "fetch":
            continue
        e = {"ts": ts, "kind": kind, "ref": ref, "sha": sha, "src": "reflog", "text": redact(msg)[:200]}
        if kind == "create":
            e["branch"], e["from"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref, m.group(1).strip()
        elif kind == "checkout":
            e["from"], e["branch"] = m.group(1), m.group(2)
        out.append(e)
    return out


# --------------------------------------------------------------------------- one repo, one tick

class Repo:
    """A watched repo during one refresh: refs read once, git answers memoised."""

    def __init__(self, cfg, path, data=None, now=None):
        self.cfg, self.path = cfg, path
        self.name = repo_key(path)
        self.conf = repo_conf(cfg, self.name)
        self.remote = self.conf["remote"]
        self.data = data or RepoData(self.name)
        self.now = now or common.now()
        self._anc, self._mb, self._log = {}, {}, {}
        self.refs = {}       # short name -> {sha, date, email, upstream, subject, kind: local|remote}
        self.load_refs()

    # ---- git
    def git(self, args, timeout=60):
        return gitro.ok(self.cfg, self.path, args, timeout=timeout)

    def load_refs(self):
        fmt = "%(refname)%1f%(objectname)%1f%(committerdate:iso-strict)%1f%(authoremail)%1f%(upstream:short)%1f%(contents:subject)"
        so = self.git(["for-each-ref", "--format=" + fmt, "refs/heads", "refs/remotes/" + self.remote])
        self.refs = {}
        for line in so.splitlines():
            p = line.split("\x1f")
            if len(p) < 6:
                continue
            ref = p[0]
            if ref.startswith("refs/heads/"):
                short, kind = ref[len("refs/heads/"):], "local"
            else:
                short, kind = ref[len("refs/remotes/"):], "remote"
                if short.endswith("/HEAD"):
                    continue
            self.refs[short] = {"sha": p[1], "date": p[2], "email": p[3].strip("<>").lower(), "upstream": p[4],
                                "subject": p[5], "kind": kind}

    def local(self):
        return {k: v for k, v in self.refs.items() if v["kind"] == "local"}

    def remote_name(self, ref):
        pre = self.remote + "/"
        return ref[len(pre):] if ref.startswith(pre) else None

    def ref_for(self, branch):
        """Prefer <remote>/<branch> (shared truth) over the local copy."""
        r = self.remote + "/" + branch
        if r in self.refs:
            return r
        return branch if branch in self.refs else None

    def sha(self, ref):
        r = self.refs.get(ref)
        return r["sha"] if r else gitro.rev(self.cfg, self.path, ref)

    def is_ancestor(self, a, b):
        k = (a, b)
        if k not in self._anc:
            self._anc[k] = gitro.is_ancestor(self.cfg, self.path, a, b)
        return self._anc[k]

    def merge_base(self, a, b):
        k = (a, b)
        if k not in self._mb:
            self._mb[k] = self.git(["merge-base", a, b]).strip() or None
        return self._mb[k]

    def containing(self, sha, refs):
        """Which of `refs` contain sha: one for-each-ref call per sha, however many refs."""
        key = (sha, tuple(refs))
        if key not in self._anc:
            full = ["refs/remotes/" + x if x.startswith(self.remote + "/") else "refs/heads/" + x for x in refs]
            so = self.git(["for-each-ref", "--contains", sha, "--format=%(refname)"] + full)
            got = set()
            for line in so.split():
                got.add(line[len("refs/remotes/"):] if line.startswith("refs/remotes/") else line[len("refs/heads/"):])
            self._anc[key] = got
        return self._anc[key]

    def count(self, rng):
        so = self.git(["rev-list", "--count", rng]).strip()
        return int(so) if so.isdigit() else None

    def reflog(self, ref, limit=2000):
        so = self.git(["log", "-g", "-n", str(limit), "--date=iso-strict", "--format=%gd%x1f%gs%x1f%H", ref])
        out = []
        for line in so.splitlines():
            parts = line.split("\x1f")
            m = re.search(r"@\{(.+)\}$", parts[0]) if len(parts) >= 3 else None
            if m:
                out.append((m.group(1), parts[1], parts[2]))
        return out

    def commits(self, rng, merges=False, limit=MAX_OWN):
        """[{sha, short, date, adate, email, subject, body, parents}] newest first."""
        key = (tuple(_list(rng)), merges, limit)
        if key in self._log:
            return self._log[key]
        args = ["log", "-n", str(limit), "--format=%H%x1f%P%x1f%cI%x1f%aI%x1f%ae%x1f%s%x1f%b%x1e"]
        if not merges:
            args.append("--no-merges")
        so = self.git(args + _list(rng), timeout=90)
        out = []
        for rec in so.split("\x1e"):
            p = rec.strip("\n").split("\x1f")
            if len(p) < 7:
                continue
            out.append({"sha": p[0].strip(), "short": p[0].strip()[:9], "parents": p[1].split(), "date": p[2], "adate": p[3],
                        "email": p[4].lower(), "subject": redact(p[5].strip())[:200], "body": p[6]})
        self._log[key] = out
        return out

    def patch_ids(self, shas):
        """{sha: patch-id} for non-merge commits; cached in patch-ids.json for good."""
        cache = self.data.patch_ids
        missing = [s for s in dict.fromkeys(shas) if s and s not in cache]
        for i in range(0, len(missing), 50):
            chunk = missing[i:i + 50]
            rc, so, _ = gitro.run(self.cfg, self.path, ["log", "--no-walk=unsorted", "-p", "--format=commit %H"] + chunk,
                                  timeout=120)
            if rc != 0:
                continue
            rc2, ids, _ = gitro.run(self.cfg, self.path, ["patch-id", "--stable"], input_text=so, timeout=120)
            got = {}
            for line in ids.splitlines() if rc2 == 0 else []:
                bits = line.split()
                if len(bits) == 2:
                    got[bits[1]] = bits[0]
            for s in chunk:
                cache[s] = got.get(s, "")  # "" = no patch (empty or merge): cached too
            self.data.touch("patch_ids")
        return {s: cache.get(s, "") for s in shas}

    # ---- bases
    def base_refs(self, role):
        """Refs for a base role, newest first. Fixed role: the one branch. Pattern role: recent matching branches."""
        pats = _list(self.conf["bases"].get(role))
        out = []
        for short, r in self.refs.items():
            name = self.remote_name(short) if r["kind"] == "remote" else short
            if name is None or not any(fnmatch.fnmatchcase(name, p) for p in pats):
                continue
            ref = self.ref_for(name)
            if ref and ref not in out:
                out.append(ref)
        out.sort(key=lambda x: self.refs[x]["date"], reverse=True)
        if is_pattern_role(self.conf["bases"], role):
            cutoff = (self.now - _days(RELEASE_LOOKBACK_DAYS)).isoformat()
            out = [r for r in out if self.refs[r]["date"] >= cutoff][:15]
        else:
            seen, keep = set(), []
            for r in out:  # one ref per name (main OR master)
                n = r.split("/", 1)[1] if r.startswith(self.remote + "/") else r
                if n not in seen:
                    seen.add(n)
                    keep.append(r)
            out = keep[:1] if out else []
        return out

    def all_base_refs(self):
        out = {}
        for role in self.conf["bases"]:
            for r in self.base_refs(role):
                out[r] = role
        return out

    def role(self, branch):
        return role_of(branch, self.conf["bases"])


def _days(n):
    import datetime as _dt
    return _dt.timedelta(days=n)


# --------------------------------------------------------------------------- refresh

class Tracker:
    """Runs inside the agent tick (the single writer)."""

    def __init__(self, cfg, state, store, now=None, pr_run=None, active_ticket=None, pr_fetch=None, force_fetch=False,
                 only_tickets=None, health=None):
        self.cfg, self.state, self.store = cfg, state, store
        self.health = health  # the agent's health.json: the MCP PR source needs it for the daily budget (None: no MCP)
        self.now = now or common.now()
        self.pr_run = pr_run or hosts._run
        self.pr_fetch = pr_fetch  # tests: (repo, branch) -> [PR], stands in for the host CLI
        self.force_fetch = force_fetch  # /cg:check: fetch now, whatever the schedule says
        self.only_tickets = set(only_tickets or [])  # /cg:check <KEY>: forced PR refresh for these tickets only
        self.active_ticket = active_ticket or (lambda ts: None)
        self.pending = {}  # repo name -> [session events]
        self.changed_tickets = set()
        self.notes = []

    # ---- session events from the hook stream
    def add_session_event(self, ev):
        repo = ev.get("repo_root")
        if not repo:
            return
        t = ev.get("ticket") or self.active_ticket(ev.get("ts"))
        evs = session_events(ev, t)
        if evs:
            self.pending.setdefault(repo_key(repo), []).extend(evs)

    def flush_session_events(self):
        for name, evs in self.pending.items():
            RepoData(name).append_events(evs)
        self.pending = {}

    # ---- main
    def refresh(self, repos=None, fetch=True, force_pr=False):
        self.flush_session_events()
        out = {}
        for path in repos if repos is not None else (self.cfg.get("watched_repos") or []):
            if not os.path.exists(os.path.join(path, ".git")):
                continue
            try:
                out[repo_key(path)] = self.refresh_repo(path, fetch=fetch, force_pr=force_pr)
            except gitro.NotAllowed:
                raise
            except Exception:
                import traceback
                common.log("agent", "branch tracking failed for %s: %s" % (path, traceback.format_exc()))
        return out

    def refresh_repo(self, path, fetch=True, force_pr=False):
        data = RepoData(repo_key(path))
        meta = data.meta
        if fetch:
            self.maybe_fetch(path, data)
        r = Repo(self.cfg, path, data, self.now)
        info = hosts.remote_info(self.cfg, path, r.remote)  # every refresh: the remote may change
        # meta["path"] is the local folder; a GitHub / GitLab "owner/repo" path is kept apart as remote_path
        host = {k: v for k, v in info.items() if k != "path"}
        if info.get("path"):
            host["remote_path"] = info["path"]
        if any(meta.get(k) != v for k, v in host.items()) or meta.get("path") != path.replace("\\", "/"):
            for k in ("host", "org", "project", "repo", "remote_path"):
                meta.pop(k, None)
            meta.update(host)
            meta["path"] = path.replace("\\", "/")
            data.touch("meta")
        self.read_reflogs(r)
        self.update_branches(r)
        self.refresh_prs(r, force=force_pr)
        data.save()
        return r

    # ---- fetch (network, tick only)
    def fetch_due(self, data, active):
        f = settings(self.cfg)["fetch"]
        if not f.get("enabled", True) or not active:
            return False
        last = common.parse_ts(data.meta.get("fetched_at"))
        if not last or (self.now - last).total_seconds() >= int(f.get("every_minutes") or 30) * 60:
            return True
        win = self.pre_send_window()
        return bool(win and win[0] <= self.now < win[1] and last < win[0])

    def pre_send_window(self):
        """(start, send time) today: one more fetch and PR check just before the 8 PM message."""
        send = (self.cfg.get("jira_worklog") or {}).get("send_at") or "20:00"
        try:
            h, m = map(int, send.split(":"))
        except ValueError:
            return None
        end = self.now.replace(hour=h, minute=m, second=0, microsecond=0)
        return end - _minutes(int(settings(self.cfg)["fetch"].get("before_send_minutes") or 20)), end

    def active_tickets(self, days=None):
        days = days or int(settings(self.cfg)["fetch"].get("active_days") or 14)
        cutoff = (self.now - _days(days)).date().isoformat()
        out = set()
        from .store import Store
        for t in Store.all_ticket_ids():
            tk = self.store.ticket(t)
            if (tk.get("last") or "") >= cutoff:
                out.add(t)
        return out

    def maybe_fetch(self, path, data):
        conf = repo_conf(self.cfg, repo_key(path))
        if not conf["fetch"]:
            return
        active = self.active_tickets()
        mine = {b.get("ticket") for b in data.branches.values()}
        if not self.force_fetch and not self.fetch_due(data, bool(active & mine) or (not data.branches and bool(active))):
            return
        remote = conf["remote"]
        if gitro.locked(path):
            data.meta["fetch_skipped"] = common.now_iso() + " a git lock file exists"
            data.touch("meta")
            return
        rc, so, se = gitro.run(self.cfg, path, ["ls-remote", "--heads", remote], timeout=60)
        if rc != 0:
            data.meta["fetch_error"] = common.now_iso() + " " + (se or so).strip()[:200]
            data.touch("meta")
            return
        heads = {}
        for line in so.splitlines():
            bits = line.split("\t")
            if len(bits) == 2 and bits[1].startswith("refs/heads/"):
                heads[bits[1][len("refs/heads/"):]] = bits[0]
        bases = conf["bases"]
        prefixes = self.cfg.get("ticket_prefixes") or []
        want = [b for b in heads if role_of(b, bases)]                                  # every base branch
        want += [b for b in heads if b in data.branches and (data.branches[b].get("ticket") in active
                                                              or data.branches[b].get("local"))]
        want += [b for b in heads if common.ticket_from_branch(b, prefixes) in active]  # my active tickets' branches
        want = [b for b in want if not ignored(self.cfg, common.ticket_from_branch(b, prefixes)) or role_of(b, bases)]
        okf, msg = gitro.fetch(self.cfg, path, remote, want)
        data.meta["fetched_at"] = common.now_iso()
        if okf:
            data.meta.pop("fetch_error", None)
        else:
            data.meta["fetch_error"] = common.now_iso() + " " + msg
        data.meta["_heads"] = sorted(heads)  # names only: tells "deleted on remote"
        data.touch("meta")

    # ---- events
    def read_reflogs(self, r):
        meta, data = r.data.meta, r.data
        cur = meta.setdefault("cursors", {})
        evs = []
        prefixes = self.cfg.get("ticket_prefixes") or []
        for ref in ["HEAD"] + ["refs/heads/" + b for b in r.local()
                               if not ignored(self.cfg, common.ticket_from_branch(b, prefixes))]:
            tip = r.refs.get(ref[len("refs/heads/"):], {}).get("sha") if ref != "HEAD" else None
            c = cur.get(ref)
            if ref != "HEAD" and c and c.get("tip") == tip:
                continue
            new = reflog_events(r, ref, (c or {}).get("ts"))
            evs += new
            last = max([e["ts"] for e in new] + [(c or {}).get("ts") or ""])
            cur[ref] = {"ts": last, "tip": tip}
        if data.append_events(evs):
            meta["events_at"] = common.now_iso()
        data.touch("meta")

    # ---- branches
    def candidates(self, r, known_tickets, active):
        """Work branches worth tracking: every local one, and remote ones that name one of my tickets or are mine."""
        emails = {e.lower() for e in self.cfg.get("git_author_emails") or [] if e}
        prefixes = self.cfg.get("ticket_prefixes") or []
        cutoff = (self.now - _days(60)).isoformat()
        out = {}
        for short, ref in r.refs.items():
            if ref["kind"] == "local":
                name = short
            else:
                name = r.remote_name(short)
                if name is None:
                    continue
            if r.role(name) or name in out:
                continue
            if ignored(self.cfg, common.ticket_from_branch(name, prefixes)):
                continue
            if ref["kind"] == "local" or name in r.data.branches:
                out[name] = True
                continue
            t = common.ticket_from_branch(name, prefixes)
            if (t and t in active) or (ref["email"] in emails and ref["date"] >= cutoff):
                out[name] = True
        return list(out)

    def update_branches(self, r):
        data = r.data
        prefixes = self.cfg.get("ticket_prefixes") or []
        known = set(_known_tickets())
        active = self.active_tickets(60)
        events = data.events()
        manual = data.meta.get("manual") or {}
        heads = set(data.meta.get("_heads") or [])
        names = self.candidates(r, known, active)
        old_all = data.branches
        for b in [b for b, rec in old_all.items() if ignored(self.cfg, rec.get("ticket"))]:
            del old_all[b]  # an ignored ticket's records go entirely
            data.touch("branches")
        base_refs = r.all_base_refs()
        for b in names:
            old = old_all.get(b) or {}
            rec = self.branch_record(r, b, old, events, manual, prefixes, base_refs, heads)
            if rec != old:
                old_all[b] = rec
                data.touch("branches")
                for t in {rec.get("ticket"), old.get("ticket")} - {None}:
                    self.changed_tickets.add(t)
        for b, old in list(old_all.items()):
            if b in names:
                continue
            gone = dict(old)
            if old.get("local") or old.get("on_remote"):
                gone.update(local=False, on_remote=False if heads else old.get("on_remote"))
                if not gone.get("deleted_at"):
                    gone["deleted_at"] = common.now_iso()
                gone["status"] = "abandoned" if b in (manual.get("abandoned") or {}) else (
                    "merged" if old.get("status") == "merged" else "deleted_remote")
                old_all[b] = gone
                data.touch("branches")
                if old.get("ticket"):
                    self.changed_tickets.add(old["ticket"])

    def branch_record(self, r, b, old, events, manual, prefixes, base_refs, heads):
        local = r.refs.get(b)
        local = local if local and local["kind"] == "local" else None
        rref = r.refs.get(r.remote + "/" + b)
        tip_ref = b if local else (r.remote + "/" + b)
        tip = (local or rref or {}).get("sha")
        if old.get("v") != ALGO:
            old = {k: v for k, v in old.items() if k in ("first_seen", "parent", "parent_ref", "parent_source", "fork_sha",
                                                     "created_at", "created_source", "link", "ticket", "deleted_at")}
        rec = dict(old, v=ALGO, branch=b, repo_name=r.name, local=bool(local), on_remote=bool(rref) and (not heads or b in heads),
                   tip=tip, remote_tip=(rref or {}).get("sha"))
        rec.setdefault("first_seen", common.now_iso())
        if rref and heads and b not in heads:
            rec["remote_gone"] = True
        else:
            rec.pop("remote_gone", None)
        info = local or rref
        rec["last_commit"], rec["last_subject"] = info["date"], redact(info["subject"])[:160]
        rec["upstream"] = (local or {}).get("upstream") or ""
        # parent
        creates = len([e for e in events if e.get("kind") == "create" and e.get("branch") == b])
        if not rec.get("parent") or (rec.get("parent_source") == "inferred" and creates != rec.get("_creates", 0)):
            rec["_creates"] = creates  # look again only when a new creation event turns up
            p, src, created, alts = self.find_parent(r, b, tip_ref, events, base_refs)
            if p and (not rec.get("parent") or src != "inferred"):
                rec.update(parent=p, parent_ref=r.ref_for(p) or p, parent_source=src, fork_sha=None)
                rec.pop("parent_alternatives", None)
                if alts:
                    rec["parent_alternatives"] = alts
                if created:
                    rec["created_at"], rec["created_source"] = created, src
        rec["parent_role"] = r.role(rec.get("parent"))
        rec.pop("role_guessed", None)
        if not rec["parent_role"] and rec.get("parent"):
            up = r.data.branches.get(rec["parent"]) or {}
            rec["parent_role"] = up.get("parent_role")  # cut from another work branch: it plays that branch's role
            if up.get("parent_source") == "inferred" or up.get("role_guessed"):
                rec["role_guessed"] = True
        sig = "%s#%s" % (tip, r.sha(rec["parent_ref"]) if rec.get("parent_ref") else "")
        if old.get("_sig") != sig or not old.get("own") and tip:
            self.commit_facts(r, rec, tip_ref, local, rref)
            rec["_sig"] = sig
            rec["checked_at"] = common.now_iso()
        # link
        t, how = self.link(r, b, rec, events, manual, prefixes)
        if ignored(self.cfg, t):
            t = None
        rec["ticket"], rec["link"] = t, ({"how": how} if t else None)
        if not rec.get("created_at"):
            own = rec.get("own") or []
            if own:
                rec["created_at"], rec["created_source"] = own[-1]["date"], "first own commit"
        # abandoned (manual) — status is finished by the ticket pass (needs PRs)
        if b in (manual.get("abandoned") or {}):
            rec["abandoned"] = manual["abandoned"][b]
        else:
            rec.pop("abandoned", None)
        return rec

    def find_parent(self, r, b, tip_ref, events, base_refs):
        """(parent, source, created_at, alternatives): captured command, then the reflog, then inferred."""
        creates = [e for e in events if e.get("kind") == "create" and e.get("branch") == b]
        for e in sorted(creates, key=lambda e: e.get("ts") or ""):
            if e.get("src") == "session" and e.get("from"):
                return _short_parent(e["from"], r.remote), "command", e.get("ts"), []
        for e in sorted(creates, key=lambda e: e.get("ts") or ""):
            if e.get("src") != "reflog":
                continue
            src = (e.get("from") or "").strip()
            if src in ("origin/" + b, "refs/remotes/%s/%s" % (r.remote, b), "%s/%s" % (r.remote, b)):
                break  # a checkout of someone's (or another machine's) branch: creation not recorded here
            if src == "HEAD":
                head = [(x["ts"], "checkout: moving from %s to %s" % (x.get("from"), x.get("branch")), x.get("sha"))
                        for x in events if x.get("kind") == "checkout" and x.get("src") == "reflog"]
                p = legacy._parent_from_head(sorted(head, reverse=True), b, e["ts"])
                if p and re.fullmatch(r"[0-9a-f]{7,40}", p) is None:
                    return _short_parent(p, r.remote), "reflog", e["ts"], []
                continue
            if re.fullmatch(r"[0-9a-f]{7,40}", src):
                continue
            return _short_parent(src, r.remote), "reflog", e["ts"], []
        # inferred: the base with the newest merge-base (bases that already contain the whole branch don't count)
        tip = r.sha(tip_ref)
        scored = []
        for ref in base_refs:
            mb = r.merge_base(ref, tip_ref) if tip else None
            if not mb:
                continue
            d = r.git(["log", "-1", "--format=%cI", mb]).strip()
            scored.append((mb == tip, _neg(d), ref))  # a base that already contains the whole branch ranks last
        if not scored:
            return None, None, None, []
        scored.sort()
        best = scored[0]
        ties = [x[2] for x in scored[1:] if x[:2] == best[:2]]
        return _short_parent(best[2], r.remote), "inferred", None, [_short_parent(x, r.remote) for x in ties][:4]

    def commit_facts(self, r, rec, tip_ref, local, rref):
        base = rec.get("parent_ref")
        if base and not r.sha(base):
            base = None
        fork = rec.get("fork_sha")
        tip = rec.get("tip")
        if not fork or not tip or not r.is_ancestor(fork, tip):
            fork = None
            if base and tip:
                mb = r.merge_base(base, tip_ref)
                if mb == tip:  # already merged into its parent: find the commit before the merge
                    mb = self.fork_before_merge(r, base, tip) or mb
                fork = mb
            rec["fork_sha"] = fork
        own = r.commits("%s..%s" % (fork, tip_ref)) if tip and fork else []
        emails = {e.lower() for e in self.cfg.get("git_author_emails") or [] if e}
        rec["own"] = [{"sha": c["sha"], "short": c["short"], "date": c["date"], "subject": c["subject"],
                       "mine": (c["email"] in emails) if emails else True,
                       "picked_from": _picked_from(c["body"])} for c in own]
        rec["ahead"] = len(own)
        rec["behind"] = r.count("%s..%s" % (tip_ref, base)) if base and tip else None
        if rref and local:
            rec["unpushed"] = r.count("%s/%s..%s" % (r.remote, rec["branch"], rec["branch"])) or 0
        elif local:
            rec["unpushed"] = len(own)
        else:
            rec["unpushed"] = 0
        rec["unpushed_shas"] = [c["sha"] for c in own][:rec["unpushed"]] if rec["unpushed"] else []

    def fork_before_merge(self, r, base, tip):
        """The oldest first-parent merge on `base` that descends from tip is the one that brought tip in;
        the fork is where tip met the base just before it."""
        so = r.git(["rev-list", "--first-parent", "--ancestry-path", "--merges", "--parents", "%s..%s" % (tip, base)])
        lines = so.strip().splitlines()
        if not lines:
            return None
        bits = lines[-1].split()
        return r.merge_base(bits[1], tip) if len(bits) >= 3 else None

    def link(self, r, b, rec, events, manual, prefixes):
        """(ticket, how): branch name, commit messages, created in a session on the ticket, patch-id, manual."""
        ml = (manual.get("links") or {}).get(b)
        unl = set((manual.get("unlinks") or {}).get(b) or [])
        if ml and ml not in unl:
            return ml, "manual"
        t = common.ticket_from_branch(b, prefixes)
        if t and t not in unl:
            return t, "branch name"
        counts = {}
        for c in rec.get("own") or []:
            for k in common.find_tickets(c["subject"], prefixes):
                counts[k] = counts.get(k, 0) + 1
        for k, _n in sorted(counts.items(), key=lambda x: -x[1]):
            if k not in unl:
                return k, "commit message"
        for e in events:
            if e.get("kind") == "create" and e.get("branch") == b and e.get("src") == "session" and e.get("ticket") \
                    and e["ticket"] not in unl:
                return e["ticket"], "session"
        pid = self.patch_link(r, rec, unl)
        if pid:
            return pid, "patch-id"
        return None, None

    def patch_link(self, r, rec, unl):
        own = [c["sha"] for c in rec.get("own") or []]
        if not own:
            return None
        mine = r.patch_ids(own)
        want = {v for v in mine.values() if v}
        if not want:
            return None
        for ob, orec in r.data.branches.items():
            t = orec.get("ticket")
            if not t or t in unl or ob == rec["branch"] or (orec.get("link") or {}).get("how") == "patch-id":
                continue
            theirs = r.patch_ids([c["sha"] for c in orec.get("own") or []])
            if want & {v for v in theirs.values() if v}:
                return t
        return None

    # ---- PRs
    def refresh_prs(self, r, force=False):
        data, meta = r.data, r.data.meta
        info = {k: meta.get(k) for k in ("host", "org", "project", "repo")}
        info["path"] = meta.get("remote_path")  # gh / glab --repo and the GitHub MCP take "owner/repo", not the folder
        src = "test" if self.pr_fetch else hosts.source_for(self.cfg, info, r.conf.get("pr_source"))
        meta["pr_source"] = src
        meta["pr_source_pref"] = hosts.pref_of(r.conf.get("pr_source"))
        prs = data.prs
        # merge commits on the base branches: always (cheap, git only); they fill in what the CLI can't see
        sig = "|".join("%s=%s" % (ref, r.sha(ref)) for ref in sorted(r.all_base_refs())) + "#" + \
            "|".join("%s=%s" % (b, x.get("remote_tip") or x.get("tip")) for b, x in sorted(data.branches.items()))
        if meta.get("merge_scan_sig") != sig:
            since = (self.now - _days(RELEASE_LOOKBACK_DAYS)).date().isoformat()
            for ref, role in r.all_base_refs().items():
                tname = r.remote_name(ref) or ref
                for p in hosts.merge_commits(self.cfg, r.path, ref, tname, since):
                    self._merge_pr(r, prs, p)
            meta["merge_scan_sig"] = sig
            data.touch("prs")
        if src == "merge-commit":
            meta.pop("pr_error", None)
            data.touch("meta")
            return
        every = settings(self.cfg)["pr_refresh_minutes"] * 60
        active = self.active_tickets()
        checked = meta.setdefault("pr_checked", {})
        win = self.pre_send_window()
        due = []
        for b, rec in data.branches.items():
            if not rec.get("on_remote") and not rec.get("remote_tip"):
                continue
            if self.only_tickets and rec.get("ticket") not in self.only_tickets:
                continue
            if rec.get("ticket") not in active and not force:
                continue
            ck = checked.get(b) or {}
            last = common.parse_ts(ck.get("at"))
            has_open = any(p.get("source") == b and p.get("state") in ("open", "draft") for p in prs.values())
            pre_send = bool(win and win[0] <= self.now < win[1] and last < win[0])
            if force or not last or ck.get("tip") != rec.get("remote_tip") or pre_send or \
                    ((has_open or not any(p.get("source") == b for p in prs.values()))
                     and (self.now - last).total_seconds() >= every):
                due.append(b)
        if src.startswith("mcp:"):
            self._prs_via_mcp(r, info, src[4:], due, force, win, every)
            data.touch("meta")
            return
        for b in due:
            rec = data.branches[b]
            try:
                got = self.pr_fetch(r, b) if self.pr_fetch else hosts.prs_for_branch(self.cfg, info, src, b, run=self.pr_run)
            except hosts.SourceError as e:
                meta["pr_error"] = common.now_iso() + " " + str(e)[:200]
                break
            except (OSError, ValueError) as e:
                meta["pr_error"] = common.now_iso() + " " + str(e)[:200]
                break
            meta.pop("pr_error", None)
            for p in got:
                self._merge_pr(r, prs, p)
            checked[b] = {"at": common.now_iso(), "tip": rec.get("remote_tip")}
            meta["pr_refreshed_at"] = common.now_iso()
            data.touch("prs")
        data.touch("meta")

    def _prs_via_mcp(self, r, info, provider, due, force, win, every):
        """All due branches of this repo in ONE read-only MCP job (hostmcp.fetch_prs), at most once per
        pr_refresh_minutes per repo (the pre-send window and /cg:check excepted); 2 h back-off after a failed job."""
        from . import hostmcp, mcpjobs
        from .kbjobs import OFFLINE_BACKOFF_S
        data, meta, prs = r.data, r.data.meta, r.data.prs
        m = meta.setdefault("pr_mcp", {})
        if not due:
            return
        last = common.parse_ts(m.get("at"))
        failed = common.parse_ts(m.get("failed_at"))
        pre_send = bool(win and win[0] <= self.now < win[1] and (not last or last < win[0]))
        if not force and not pre_send:
            if last and (self.now - last).total_seconds() < every:
                return
            if failed and (self.now - failed).total_seconds() < OFFLINE_BACKOFF_S:
                return
        if self.health is None:
            meta["pr_error"] = common.now_iso() + " PRs via MCP run only in the background tick"
            return
        known = sorted({int(k) for k, p in prs.items() if str(k).isdigit() and p.get("source") in due and (
            p.get("state") in ("open", "draft") or (p.get("state") == "merged" and not p.get("merge_commit")))})
        try:
            got = hostmcp.fetch_prs(self.cfg, info, provider, due, known, self.health)
        except hostmcp.Deferred as e:
            meta["pr_error"] = common.now_iso() + " " + str(e)[:200]
            return
        except mcpjobs.JobError as e:
            meta["pr_error"] = common.now_iso() + " " + str(e)[:200]
            m["failed_at"] = common.now_iso()
            common.log("agent", "PRs via MCP for %s: %s" % (r.name, e))
            return
        now = common.now_iso()
        meta.pop("pr_error", None)
        m.pop("failed_at", None)
        m.update(at=now, server=got["server"], branches=len(got["branches"]), calls=got["calls"], tools=got["tools"])
        for b in got["branches"]:
            for p in got["prs"].get(b) or []:
                self._merge_pr(r, prs, p)
            checked = meta.setdefault("pr_checked", {})
            checked[b] = {"at": now, "tip": (data.branches.get(b) or {}).get("remote_tip")}
        meta["pr_refreshed_at"] = now
        data.touch("prs")

    def _merge_pr(self, r, prs, p):
        key = str(p["number"])
        old = prs.get(key) or {}
        if p.get("src") == "merge-commit" and not old:
            p = dict(p)
            p["source"] = p.get("source") or self.source_of(r, p)
            if p["source"] not in r.data.branches:
                return  # someone else's PR: not kept
        if p.get("src") == "merge-commit" and old and old.get("src") != "merge-commit":
            for k in ("merge_commit", "head_at_merge", "merged_at"):
                if not old.get(k) and p.get(k):
                    old[k] = p[k]
            prs[key] = old
            return
        new = dict(old)
        new.update({k: v for k, v in p.items() if v not in (None, "")})
        new["title"] = redact(new.get("title") or "")[:200]
        if not new.get("source") and new.get("head"):
            new["source"] = self.source_of(r, new)
        new["checked_at"] = common.now_iso()
        prs[key] = new

    def source_of(self, r, p):
        """Merge-commit PR without a source name: the tracked branch whose commits make up the PR head."""
        head = p.get("head")
        if not head:
            return None
        keys = set(common.find_tickets(p.get("title") or "", self.cfg.get("ticket_prefixes") or []))
        for b, rec in r.data.branches.items():
            tip = rec.get("remote_tip") or rec.get("tip")
            if not tip:
                continue
            if tip == head:
                return b
            if rec.get("ticket") in keys and rec.get("own") and r.is_ancestor(tip, head) \
                    and not r.is_ancestor(tip, p["merge_commit"] + "^1"):
                return b
        return None


def pr_source_rows(cfg, repos=None):
    """Per watched repo: which PR source is in use and when PRs were last refreshed -> [{repo, host, pref, source,
    label, refreshed_at, error, mcp}] (health.json "pr_sources", for Health and Settings). `repos`: {name: Repo}."""
    from .gitview import pr_source_text
    out = []
    names = sorted(repos) if repos is not None else all_repo_names()
    for name in names:
        meta = (repos[name].data if repos is not None else RepoData(name)).meta
        src = meta.get("pr_source")
        m = meta.get("pr_mcp") or {}
        out.append({"repo": name, "host": meta.get("host"), "pref": meta.get("pr_source_pref") or hosts.pref_of(
            repo_conf(cfg, name).get("pr_source")), "source": src, "label": pr_source_text(src),
            "refreshed_at": meta.get("pr_refreshed_at") or (meta.get("pr_mcp") or {}).get("at"),
            "error": meta.get("pr_error"),
            "mcp": {k: m.get(k) for k in ("server", "at", "calls", "branches", "failed_at") if m.get(k)} or None})
    return out


def _neg(s):
    return "".join(chr(0x10FFFF - ord(c)) for c in (s or ""))


def _minutes(n):
    import datetime as _dt
    return _dt.timedelta(minutes=n)


def _short_parent(ref, remote):
    ref = re.sub(r"^refs/(heads|remotes)/", "", ref or "")
    return ref[len(remote) + 1:] if ref.startswith(remote + "/") else ref


def _picked_from(body):
    m = re.search(r"\(cherry picked from commit ([0-9a-f]{7,40})\)", body or "")
    return m.group(1) if m else None


def _known_tickets():
    from .store import Store
    return Store.all_ticket_ids()


# --------------------------------------------------------------------------- inbox directives

DIRECTIVE = re.compile(r"^(branch-link|branch-unlink|branch-abandon|branch-restore|strategy)\s*:\s*(.+)$", re.I)


def parse_directive(text):
    """inbox.md line -> dict or None.
      branch-link: <TICKET> <repo> <branch>     branch-unlink: <TICKET> <repo> <branch>
      branch-abandon: <repo> <branch>           branch-restore: <repo> <branch>
      strategy: <repo> <strategy-name>          strategy: <TICKET> <strategy-name>"""
    m = DIRECTIVE.match((text or "").strip().lstrip("-* ").strip())
    if not m:
        return None
    kind, args = m.group(1).lower(), m.group(2).split()
    if kind in ("branch-link", "branch-unlink") and len(args) == 3:
        return {"kind": kind, "ticket": args[0].upper(), "repo": args[1], "branch": args[2]}
    if kind in ("branch-abandon", "branch-restore") and len(args) == 2:
        return {"kind": kind, "repo": args[0], "branch": args[1]}
    if kind == "strategy" and len(args) == 2:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,9}-\d{1,6}", args[0]):
            return {"kind": "ticket-strategy", "ticket": args[0].upper(), "strategy": args[1]}
        return {"kind": "strategy", "repo": args[0], "strategy": args[1]}
    return {"kind": "invalid", "text": text}


def apply_directive(d, store, cfg, now_iso=None):
    """Record a manual decision. Returns (ok, message)."""
    now_iso = now_iso or common.now_iso()
    if d["kind"] == "invalid":
        return False, "not understood: " + d["text"]
    if d["kind"] == "ticket-strategy":
        if d["strategy"] not in settings(cfg)["strategies"]:
            return False, "unknown strategy " + d["strategy"]
        store.ticket(d["ticket"]).setdefault("git_override", {})["strategy"] = d["strategy"]
        store.touch_ticket(d["ticket"])
        return True, "%s uses %s" % (d["ticket"], d["strategy"])
    data = RepoData(_match_repo(cfg, d["repo"]))
    man = data.meta.setdefault("manual", {})
    if d["kind"] == "strategy":
        if d["strategy"] not in settings(cfg)["strategies"]:
            return False, "unknown strategy " + d["strategy"]
        man["strategy"] = d["strategy"]
    elif d["kind"] == "branch-link":
        man.setdefault("links", {})[d["branch"]] = d["ticket"]
        lst = man.setdefault("unlinks", {}).get(d["branch"]) or []
        if d["ticket"] in lst:
            lst.remove(d["ticket"])
    elif d["kind"] == "branch-unlink":
        if (man.get("links") or {}).get(d["branch"]) == d["ticket"]:
            del man["links"][d["branch"]]
        lst = man.setdefault("unlinks", {}).setdefault(d["branch"], [])
        if d["ticket"] not in lst:
            lst.append(d["ticket"])
    elif d["kind"] == "branch-abandon":
        man.setdefault("abandoned", {})[d["branch"]] = now_iso
    elif d["kind"] == "branch-restore":
        (man.get("abandoned") or {}).pop(d["branch"], None)
        man.setdefault("restored", {})[d["branch"]] = now_iso
    data.touch("meta")
    data.save()
    rec = data.branches.get(d.get("branch") or "") or {}
    for t in {rec.get("ticket"), d.get("ticket")} - {None}:
        tk = store.ticket(t)
        tk.setdefault("git", {})["sig"] = None  # recompute
        store.touch_ticket(t)
    return True, "%s %s" % (d["kind"], d.get("branch") or d.get("strategy"))


def _match_repo(cfg, name):
    for p in cfg.get("watched_repos") or []:
        if repo_key(p).lower() == name.lower():
            return repo_key(p)
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)
