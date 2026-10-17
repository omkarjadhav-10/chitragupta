"""Per ticket: which strategy applies, where every commit has (and hasn't) arrived, and what needs attention.

For each required target of the repo's strategy and each of the ticket's commits, exactly one status:
  in       the commit is an ancestor of <remote>/<target>
  same     the same change is there (cherry-picked or rebased; equal patch-id)
  squash   a PR that carried it was squash-merged into the target (host says merged, merge commit in target, not reverted)
  open     a PR that carries it is open
  missing  no PR carries it, or it was pushed after its PR merged
  reverted a later commit in the target reverts it (or the merge that brought it)
  check    only the message or a "cherry picked from" trailer matches: the patch changed (conflict?), check by hand
Rows are the ticket's own commits grouped by patch-id, so a cherry-picked copy is the same row.
"""
import fnmatch
import hashlib
import json
import re

from . import common
from . import gitcmd
from . import gittrack as gt

SYMBOL = {"in": "✓", "same": "✓", "squash": "✓", "open": "⏳", "missing": "✗", "reverted": "↩", "check": "?"}
LABEL = {"in": "in target", "same": "same change in target", "squash": "via squash PR", "open": "PR open",
         "missing": "missing", "reverted": "reverted", "check": "check by hand"}
GOOD = ("in", "same", "squash")


# --------------------------------------------------------------------------- strategy

def jira_parent(tk, key):
    p = (tk.get("jira") or {}).get("parent")
    if not p:
        card = (common.read_json(common.hp("data", "jira", key + ".json"), {}) or {}).get("card") or {}
        p = card.get("parent")
    return (p or "").strip() or None


def strategy_for(cfg, r, key, tk):
    """(name, how) — ticket override, then a rule (e.g. by Jira parent), then the repo's setting, then the default."""
    return gitcmd.strategy_for(cfg, r.conf, r.data.meta.get("manual"), key, tk, jira_parent(tk, key))


# --------------------------------------------------------------------------- targets

def resolve_targets(r, strat, branches, prs):
    """[{role, name, refs, how}] for the strategy's targets. For a pattern role (release/*) the release that counts is
    the PR's target, else the parent of the ticket branch cut from it; with neither, every recent release branch
    (and main) is checked and the column says so."""
    bases = r.conf["bases"]
    out = []
    for role in strat["targets"]:
        if not gt.is_pattern_role(bases, role):
            refs = r.base_refs(role)
            out.append({"role": role, "name": (r.remote_name(refs[0]) or refs[0]) if refs else role, "refs": refs[:1],
                        "how": "base" if refs else "no %s branch found" % role})
            continue
        pats = gt._list(bases.get(role))
        named = []
        for p in sorted(prs, key=lambda p: p.get("merged_at") or p.get("created_at") or "", reverse=True):
            if p.get("target") and any(fnmatch.fnmatchcase(p["target"], x) for x in pats) and p.get("state") != "closed":
                named.append((p["target"], "PR #%s target" % p["number"]))
        for b in sorted(branches, key=lambda b: b.get("created_at") or "", reverse=True):
            if guessed(b):
                continue  # a guess can't pick the release that counts
            if b.get("parent_role") == role and b.get("parent") and any(fnmatch.fnmatchcase(b["parent"], x) for x in pats):
                named.append((b["parent"], "parent of %s" % b["branch"]))
        if named:
            name, how = named[0]
            ref = r.ref_for(name)
            out.append({"role": role, "name": name, "refs": [ref] if ref else [], "how": how})
            continue
        refs = r.base_refs(role) + r.base_refs("main")
        out.append({"role": role, "name": "any %s" % role, "refs": refs, "any": True,
                    "how": "no %s branch linked yet: checked every recent %s branch and main" % (role, role)})
    return out


# --------------------------------------------------------------------------- rows

def _subject_tickets(s, cfg):
    return set(common.find_tickets(s or "", cfg.get("ticket_prefixes") or []))


def rows_for(r, key, counted, cfg):
    """The ticket's commits, grouped by patch-id. A commit whose subject names another ticket (and not this one), or
    that isn't mine and doesn't name this ticket (for example a release delta brought in by a merge), is left out."""
    picked = []
    for b in counted:
        for c in b.get("own") or []:
            ts = _subject_tickets(c["subject"], cfg)
            if key in ts or (not ts and c.get("mine", True)):
                picked.append((b["branch"], c))
    pids = r.patch_ids([c["sha"] for _, c in picked])
    rows = {}
    for bname, c in picked:
        rid = pids.get(c["sha"]) or c["sha"]
        row = rows.setdefault(rid, {"id": rid, "pid": pids.get(c["sha"]) or "", "shas": [], "subject": c["subject"],
                                    "date": c["date"], "branches": [], "picked_from": []})
        if c["sha"] not in row["shas"]:
            row["shas"].append(c["sha"])
        if bname not in row["branches"]:
            row["branches"].append(bname)
        if c.get("picked_from"):
            row["picked_from"].append(c["picked_from"])
        if c["date"] < row["date"]:
            row["date"], row["subject"] = c["date"], c["subject"]
    # a copy whose patch changed (conflict resolved while cherry-picking, with a -x trailer) joins its original as a
    # loose copy: reaching a target only through it reads "check by hand"
    by_sha = {s: rid for rid, row in rows.items() for s in row["shas"]}
    for rid, row in list(rows.items()):
        if len(row["shas"]) != 1 or not row["picked_from"]:
            continue
        src = next((by_sha[s] for s in by_sha for p in row["picked_from"] if s.startswith(p) and by_sha[s] != rid), None)
        if src and src in rows:
            rows[src].setdefault("loose", []).append(row["shas"][0])
            rows[src]["branches"] += [b for b in row["branches"] if b not in rows[src]["branches"]]
            del rows[rid]
    return sorted(rows.values(), key=lambda x: x["date"])


# --------------------------------------------------------------------------- the check

class Checker:
    def __init__(self, r, key, rows, prs, branches, cfg):
        self.r, self.key, self.rows, self.prs, self.branches, self.cfg = r, key, rows, prs, branches, cfg
        since = min([x["date"] for x in rows] or [common.now_iso()])
        d = common.parse_ts(since)
        self.since = (d - gt._days(3)).isoformat() if d else since
        self._tlog = {}
        self.all_refs = []

    def target_log(self, ref):
        if ref not in self._tlog:
            cs = self.r.commits(["--since=" + self.since, ref], merges=True, limit=3000)
            reverts = {}
            for c in cs:
                for sha in re.findall(r"This reverts commit ([0-9a-f]{7,40})", c["body"] or ""):
                    reverts[sha] = c["sha"]
                m = re.match(r'^Revert "(.+)"$', c["subject"] or "")
                if m:
                    reverts["subject:" + m.group(1)] = c["sha"]
            self._tlog[ref] = ([c for c in cs if len(c["parents"]) < 2], reverts, {c["sha"] for c in cs})
        return self._tlog[ref]

    def has(self, ref, sha, exact=False):
        """sha is in ref: first from the target's recent history (one git log per target), exactly only when asked."""
        return sha in self.target_log(ref)[2] or (exact and ref in self.r.containing(sha, self.all_refs))

    def _reverted(self, ref, shas, subject):
        rv = self.target_log(ref)[1]
        for s in shas:
            if not s or len(s) < 7:
                continue
            for k, v in rv.items():
                if not k.startswith("subject:") and (s.startswith(k) or k.startswith(s)):
                    return v
        return rv.get("subject:" + (subject or ""))

    def same_change(self, ref, row):
        """('same', sha) if a target commit has the row's patch-id; ('check', sha) if only subject/trailer match."""
        cands = []
        log = self.target_log(ref)[0]
        for c in log:
            body_pick = re.search(r"cherry picked from commit ([0-9a-f]{7,40})", c["body"] or "")
            if c["subject"] == row["subject"] or (body_pick and any(s.startswith(body_pick.group(1)) for s in row["shas"])) \
                    or self.key in _subject_tickets(c["subject"], self.cfg):
                cands.append(c)
        if not cands:
            return None
        pids = self.r.patch_ids([c["sha"] for c in cands])
        for c in cands:
            if row["pid"] and pids.get(c["sha"]) == row["pid"]:
                return "same", c["sha"]
        for c in cands:
            body_pick = re.search(r"cherry picked from commit ([0-9a-f]{7,40})", c["body"] or "")
            if c["subject"] == row["subject"] or (body_pick and any(s.startswith(body_pick.group(1)) for s in row["shas"])):
                return "check", c["sha"]
        return None

    def cell(self, row, tgt):
        """{s: status, ref?, via?, pr?, why?}"""
        res = self._cell(row, tgt, exact=False)
        if res["s"] in GOOD or res["s"] == "reverted":
            return res
        return self._cell(row, tgt, exact=True) or res  # a miss is confirmed with exact ancestry (older than the window)

    def _hit(self, ref, row, hit):
        rv = self._reverted(ref, row["shas"] + [hit.get("via") or ""] +
                            [p.get("merge_commit") or "" for p in self.prs if p.get("number") == hit.get("pr")],
                            row["subject"])
        if rv:
            return {"s": "reverted", "ref": ref, "via": rv[:9], "why": "reverted by %s" % rv[:9]}
        return hit

    def _cell(self, row, tgt, exact):
        names = {tgt["name"]} | {self.r.remote_name(x) or x for x in tgt["refs"]}
        loose = None
        for ref in tgt["refs"]:
            hit = None
            for sha in row["shas"]:
                if self.has(ref, sha, exact):
                    hit = {"s": "in", "ref": ref, "via": sha[:9]}
                    break
            if exact:
                if hit:
                    return self._hit(ref, row, hit)
                continue  # the exact pass only re-checks ancestry; the rest was answered by the first pass
            if not hit and not loose:
                for sha in row.get("loose") or []:
                    if self.has(ref, sha):
                        loose = {"s": "check", "ref": ref, "via": sha[:9],
                                 "why": "cherry-picked with changes (conflict resolved?)"}
                        break
            if not hit:
                sc = self.same_change(ref, row)
                if sc and sc[0] == "same":
                    hit = {"s": "same", "ref": ref, "via": sc[1][:9]}
                elif sc and not loose:
                    loose = {"s": "check", "ref": ref, "via": sc[1][:9],
                             "why": "same message, different patch (conflict-resolved cherry-pick?)"}
            if not hit:
                for p in self.prs:
                    if p.get("state") == "merged" and p.get("target") == (self.r.remote_name(ref) or ref) and \
                            p.get("merge_commit") and p.get("head_at_merge") and \
                            self.r.is_ancestor(p["merge_commit"], ref) and \
                            any(self.r.is_ancestor(sha, p["head_at_merge"]) for sha in row["shas"]):
                        hit = {"s": "squash", "ref": ref, "pr": p["number"], "via": p["merge_commit"][:9]}
                        break
            if hit:
                return self._hit(ref, row, hit)
        if exact:
            return None
        if loose:
            return loose
        for p in self.prs:
            if p.get("state") in ("open", "draft") and p.get("target") in names:
                head = self._branch_tip(p.get("source")) or p.get("head")
                if head and any(self.r.is_ancestor(sha, head) for sha in row["shas"]):
                    return {"s": "open", "pr": p["number"]}
        return {"s": "missing", "why": self.why_missing(row, names)}

    def _branch_tip(self, b):
        rec = self.r.data.branches.get(b or "") or {}
        return rec.get("remote_tip") or rec.get("tip")

    def why_missing(self, row, names):
        for b in self.branches:
            if set(row["shas"]) & set(b.get("unpushed_shas") or []) and not b.get("remote_tip"):
                return "not pushed"
        for p in sorted(self.prs, key=lambda p: p.get("merged_at") or ""):
            if p.get("state") != "merged" or p.get("target") not in names or not p.get("head_at_merge"):
                continue
            tip = self._branch_tip(p.get("source"))
            carries = tip and any(self.r.is_ancestor(sha, tip) for sha in row["shas"])
            if carries and not any(self.r.is_ancestor(sha, p["head_at_merge"]) for sha in row["shas"]):
                return "pushed after PR #%s merged" % p["number"]
        for b in self.branches:
            if set(row["shas"]) & set(b.get("unpushed_shas") or []):
                return "not pushed"
        for p in self.prs:  # the host says merged, but the merge isn't in the local refs yet
            if p.get("state") == "merged" and p.get("target") in names and p.get("head_at_merge") and \
                    any(s == p["head_at_merge"] or self.r.is_ancestor(s, p["head_at_merge"]) for s in row["shas"]) and \
                    p.get("merge_commit") and not self.r.sha(p["merge_commit"]):
                return "PR #%s merged; fetch pending" % p["number"]
        return "no PR carries it"


# --------------------------------------------------------------------------- statuses and suggestions

def branch_status(b, prs):
    mine = [p for p in prs if p.get("source") == b["branch"]]
    if b.get("abandoned"):
        return "abandoned"
    if any(p.get("state") in ("open", "draft") for p in mine):
        return "pr_open"
    merged = [p for p in mine if p.get("state") == "merged"]
    if merged:
        last = max(merged, key=lambda p: p.get("merged_at") or "")
        head = last.get("head_at_merge")
        tip = b.get("remote_tip") or b.get("tip")
        if not head or tip == head or not b.get("own") or b["own"][0]["sha"] == head:
            return "merged"
    if b.get("deleted_at") and not b.get("local") or b.get("remote_gone") and not b.get("local"):
        return "merged" if merged else "deleted_remote"
    return "merged" if merged and not b.get("own") else "active"


def guessed(b):
    """The parent (or the role it passes on) was inferred, not recorded by a command or the reflog."""
    return b.get("parent_source") == "inferred" or bool(b.get("role_guessed"))


from_roles = gitcmd.from_roles


def superseded(b, branches, strat, prs):
    """Wrong parent, and the ticket later got a branch from a parent that fits, and this one has no PR and no newer
    commits -> the replacement's name."""
    roles = from_roles(strat)
    if not b.get("parent_role") or b["parent_role"] in roles or guessed(b):
        return None
    if any(p.get("source") == b["branch"] for p in prs):
        return None
    t0 = b.get("created_at") or b.get("first_seen") or ""
    for o in branches:
        if o is b or o.get("parent_role") not in roles:
            continue
        t1 = o.get("created_at") or o.get("first_seen") or ""
        if t1 > t0 and (not b.get("own") or (b["own"][0]["date"] <= t1)):
            return o["branch"]
    return None


# --------------------------------------------------------------------------- one ticket in one repo

def compute(r, key, tk, cfg, now=None):
    now = now or common.now()
    s = gt.settings(cfg)
    branches = sorted([b for b in r.data.branches.values() if b.get("ticket") == key],
                      key=lambda b: b.get("created_at") or b.get("first_seen") or "")
    names = {b["branch"] for b in branches}
    prs = sorted([p for p in r.data.prs.values() if p.get("source") in names],
                 key=lambda p: p.get("created_at") or p.get("merged_at") or "")
    name, how = strategy_for(cfg, r, key, tk)
    strat = s["strategies"].get(name) if name else None
    out = {"repo": r.name, "strategy": name, "strategy_how": how, "pr_source": r.data.meta.get("pr_source"),
           "branches": [], "prs": [], "targets": [], "rows": [], "alerts": []}
    for b in branches:
        st = branch_status(b, prs)
        sup = superseded(b, branches, strat, prs) if strat else None
        out["branches"].append({
            "branch": b["branch"], "parent": b.get("parent"), "parent_role": b.get("parent_role"),
            "parent_source": b.get("parent_source"), "role_guessed": b.get("role_guessed"), "created_at": b.get("created_at"), "link": (b.get("link") or {}).get("how"),
            "status": st, "suggested": "abandon (superseded by %s)" % sup if sup and st != "abandoned" else None,
            "prs": [p["number"] for p in prs if p.get("source") == b["branch"]], "unpushed": b.get("unpushed") or 0,
            "ahead": b.get("ahead"), "behind": b.get("behind"), "last_commit": b.get("last_commit"),
            "local": b.get("local"), "on_remote": b.get("on_remote"), "own": len(b.get("own") or []),
            "commits": [{"sha": c["sha"][:9], "subject": c["subject"], "date": c["date"]} for c in (b.get("own") or [])[:40]]})
    for p in prs:
        out["prs"].append({k: p.get(k) for k in ("number", "title", "source", "target", "state", "review", "created_at",
                                                 "merged_at", "merge_commit", "head_at_merge", "url", "src")})
    if not strat:
        out["note"] = "no branching strategy set for this repo"
        return out
    excluded = {x["branch"] for x in out["branches"] if x["status"] == "abandoned" or x["suggested"]}
    counted = [b for b in branches if b["branch"] not in excluded]
    targets = resolve_targets(r, strat, counted or branches, prs)
    rows = rows_for(r, key, counted, cfg)
    ck = Checker(r, key, rows, prs, branches, cfg)
    ck.all_refs = sorted({ref for t in targets for ref in t["refs"]})
    for row in rows:
        row["cells"] = {t["role"]: ck.cell(row, t) if t["refs"] else {"s": "missing", "why": t["how"]} for t in targets}
    out["targets"] = [{k: t.get(k) for k in ("role", "name", "how", "any")} for t in targets]
    out["rows"] = [{"id": x["id"][:12], "shas": [s_[:9] for s_ in x["shas"]], "loose": [s_[:9] for s_ in x.get("loose") or []],
                    "subject": x["subject"], "date": x["date"], "branches": x["branches"], "cells": x["cells"]} for x in rows]
    out["alerts"] = alerts(r, key, out, strat, branches, counted, rows, prs, targets, s, now)
    return out


def _n(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def alerts(r, key, out, strat, branches, counted, rows, prs, targets, s, now):
    A = []

    def add(kind, text, commits=None, **kw):
        a = {"kind": kind, "text": text, "commits": [c[:9] for c in (commits or [])]}
        a.update(kw)
        A.append(a)

    tname = {t["role"]: t["name"] for t in targets}
    done = set()
    # 1. pushed after the PR merged (the lost-commit case)
    lost = {}
    for row in rows:
        for role, c in row["cells"].items():
            if c["s"] == "missing" and (c.get("why") or "").startswith("pushed after PR #"):
                lost.setdefault((role, c["why"]), []).append(row)
    for (role, why), rs in sorted(lost.items()):
        pr = why.split("#", 1)[1].split()[0]
        add("lost", "%s not in %s — pushed after PR #%s merged." % (_n(len(rs), "commit"), tname[role], pr),
            [x["shas"][0] for x in rs], target=role)
        done |= {(x["id"], role) for x in rs}
    # 2. in one target but not another (release-then-qa: qa behind release; qa-cherry-pick: uat/staging behind qa)
    gaps = {}
    checks = {}
    for row in rows:
        for ro, c in row["cells"].items():
            if c["s"] == "check":
                checks.setdefault(ro, []).append(row)
        have = [ro for ro, c in row["cells"].items() if c["s"] in GOOD]
        if not have:
            continue
        for ro, c in row["cells"].items():
            if c["s"] == "missing" and (row["id"], ro) not in done:
                gaps.setdefault((have[0], ro), []).append(row)
    for (h, m), rs in sorted(gaps.items()):
        add("gap", "%s in %s but not in %s." % (_n(len(rs), "commit"), tname[h], tname[m]), [x["shas"][0] for x in rs], target=m)
    for ro, rs in sorted(checks.items()):
        add("check", "%s reached %s only with a changed patch — check it by hand." % (_n(len(rs), "commit"), tname[ro]),
            [x["shas"][0] for x in rs], target=ro)
    # 3. only on an abandoned / superseded branch
    counted_ids = {x["id"] for x in rows}
    excluded = [b for b in branches if b not in counted]
    for b in excluded:
        own = b.get("own") or []
        if not own:
            continue
        pids = r.patch_ids([c["sha"] for c in own])
        only = []
        for c in own:
            pid = pids.get(c["sha"]) or c["sha"]
            if pid in counted_ids:
                continue
            if any(r.is_ancestor(c["sha"], ref) for t in targets for ref in t["refs"]):
                continue
            only.append(c["sha"])
        if only:
            add("only-copy", "%s exist only on %s, which is %s." % (
                _n(len(only), "commit"), b["branch"], "abandoned" if b.get("abandoned") else "superseded"), only, branch=b["branch"])
    # 4. created from a parent the strategy doesn't use
    roles = from_roles(strat)
    for b in out["branches"]:
        if b["status"] == "abandoned" or not b.get("parent_role") or b["parent_role"] in roles or guessed(b):
            continue  # only a recorded parent (command / reflog) is judged
        txt = "%s was created from %s, which %s doesn't use (%s)." % (
            b["branch"], b["parent"], out["strategy"], ", ".join(sorted(x for x in roles if x)))
        if b.get("suggested"):
            txt += " It looks superseded: /cg:branch abandon %s %s" % (r.name, b["branch"])
        add("wrong-parent", txt, branch=b["branch"])
    # 5. PR into an unexpected base / carrying other tickets' commits
    by_branch = {b["branch"]: b for b in branches}
    for p in prs:
        if p.get("state") == "closed":
            continue
        src = by_branch.get(p.get("source")) or {}
        prole = src.get("parent_role")
        trole = r.role(p.get("target") or "")
        expect = [st.get("pr_into") for st in strat["steps"] if st.get("from") == prole]
        if prole and trole and expect and trole not in expect:
            foreign = []
            if p.get("state") in ("open", "draft"):
                tref = r.ref_for(p["target"])
                head = src.get("remote_tip") or src.get("tip")
                if tref and head:
                    for c in r.commits("%s..%s" % (tref, head), limit=300):
                        if key not in _subject_tickets(c["subject"], r.cfg) and not any(c["sha"] in x["shas"] for x in rows):
                            foreign.append(c)
            txt = "PR #%s goes from a %s-based branch into %s; %s expects %s." % (
                p["number"], prole, p["target"], out["strategy"], " or ".join(expect))
            if foreign:
                txt += " It would carry %s from other work." % _n(len(foreign), "commit")
            add("pr-base", txt, [c["sha"] for c in foreign[:20]], pr=p["number"])
    # 6. not pushed
    for b in counted:
        if b.get("unpushed"):
            add("unpushed", "%s on %s not pushed." % (_n(b["unpushed"], "commit"), b["branch"]),
                b.get("unpushed_shas") or [], branch=b["branch"])
    # 7. stale PRs
    for p in prs:
        if p.get("state") not in ("open", "draft"):
            continue
        made = common.parse_ts(p.get("created_at"))
        age = (now - made).days if made else None
        if p.get("review") == "approved":
            add("pr-stale", "PR #%s into %s is approved but not merged." % (p["number"], p.get("target")), pr=p["number"])
        elif age is not None and age > s["stale_pr_days"]:
            add("pr-stale", "PR #%s into %s has been open %d days." % (p["number"], p.get("target"), age), pr=p["number"])
    return A


# --------------------------------------------------------------------------- ticket record

def sig_of(r, key, tk, cfg):
    branches = sorted((b["branch"], b.get("tip"), b.get("remote_tip"), b.get("parent"), b.get("abandoned"), b.get("unpushed"),
                       b.get("deleted_at"), b.get("remote_gone")) for b in r.data.branches.values() if b.get("ticket") == key)
    names = {b[0] for b in branches}
    prs = sorted((p["number"], p.get("state"), p.get("review"), p.get("head"), p.get("merge_commit"))
                 for p in r.data.prs.values() if p.get("source") in names)
    bases = sorted((ref, r.sha(ref)) for ref in r.all_base_refs())
    blob = json.dumps([branches, prs, bases, tk.get("git_override"), (tk.get("jira") or {}).get("parent"),
                       r.data.meta.get("manual"), gt.settings(cfg), r.conf, gt.ALGO], sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def summarize(git):
    """One line for daily files / Slack: 'Branches 2 · PRs 1 merged, 1 open · Delivery ⚠ 3 commits not in release'."""
    repos = git.get("repos") or {}
    nb = sum(len([b for b in x["branches"] if b["status"] != "abandoned"]) for x in repos.values())
    states = {}
    for x in repos.values():
        for p in x["prs"]:
            st = "open" if p["state"] in ("open", "draft") else p["state"]
            states[st] = states.get(st, 0) + 1
    prs = ", ".join("%d %s" % (states[k], k) for k in ("merged", "open", "closed") if states.get(k))
    missing = {}
    for x in repos.values():
        tn = {t["role"]: t["name"] for t in x.get("targets") or []}
        for row in x.get("rows") or []:
            for role, c in row["cells"].items():
                if c["s"] not in GOOD and c["s"] != "open":
                    missing.setdefault(tn.get(role, role), set()).add(row["id"])
    if not any(x.get("strategy") for x in repos.values()):
        dl = "Delivery not checked (no strategy)"
    elif missing:
        worst = max(missing.items(), key=lambda kv: len(kv[1]))
        dl = "Delivery ⚠ %s not in %s" % (_n(len(worst[1]), "commit"), worst[0].replace("any ", ""))
        if len(missing) > 1:
            dl += " (+%d more target%s)" % (len(missing) - 1, "" if len(missing) == 2 else "s")
    elif any(x.get("rows") for x in repos.values()):
        dl = "Delivery ✓"
    else:
        dl = "Delivery: no commits yet"
    return "Branches %d · PRs %s · %s" % (nb, prs or "none", dl)


def update_ticket(store, key, rx, cfg, now=None, force=False):
    """Recompute tk['git'] for every repo in `rx` ({name: Repo}) that has branches for the ticket. Returns True if changed."""
    tk = store.ticket(key)
    if gt.ignored(cfg, key):
        if tk.pop("git", None) is not None:
            store.touch_ticket(key)
            return True
        return False
    g = tk.get("git") if isinstance(tk.get("git"), dict) else {}
    repos = dict(g.get("repos") or {})
    sigs = dict(g.get("sigs") or {})
    changed = False
    for name, r in rx.items():
        has = any(b.get("ticket") == key for b in r.data.branches.values())
        if not has:
            if name in repos:
                repos.pop(name)
                sigs.pop(name, None)
                changed = True
            continue
        sg = sig_of(r, key, tk, cfg)
        if not force and sigs.get(name) == sg and name in repos and g.get("sig") is not None:
            continue
        repos[name] = compute(r, key, tk, cfg, now)
        sigs[name] = sg
        changed = True
    if not changed:
        return False
    alerts_all = [dict(a, repo=n) for n, x in sorted(repos.items()) for a in x.get("alerts") or []]
    new = {"repos": repos, "sigs": sigs, "sig": "ok", "alerts": alerts_all, "checked_at": common.now_iso()}
    new["summary"] = summarize(new)
    tk["git"] = new
    store.touch_ticket(key)
    return True
