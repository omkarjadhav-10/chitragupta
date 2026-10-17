"""Branch records for watched repos (no LLM): parent branch, creation time, upstream, last commit,
and whether the work has reached each environment branch (qa / uat / staging / master ...).

Sources, best first:
  parent    1. branch reflog "branch: Created from <ref>"        (exact, local, ~90 days)
            2. "Created from HEAD" -> HEAD reflog: the branch HEAD was on at that moment
            3. inferred: the candidate branch with the fewest commits not on this branch
  created   1. oldest branch-reflog entry  2. oldest own commit (approximate)
  merged    1. tip is an ancestor of the env branch                  -> "merged"
            2. every own commit has an equivalent patch there (git cherry) -> "cherry-picked"
            3. some do -> "partial n/m"
            4. a commit on the env branch mentions the ticket after creation -> "ticket commit" (squash merge?)
Env status reads origin/<env> when it exists, i.e. as of the last fetch (gittrack fetches; branch_fetch is retired).
Since v0.2.0 the tick uses gittrack.py; the helpers here (reflog, parent from HEAD, head activity) are shared.
"""
import os
import re

from . import common
from . import gitro
from .redact import redact

DEFAULT_ENVS = ["qa", "uat", "staging", "master", "main", "develop"]
ALGO_VERSION = 2  # bump when parent/created/env logic changes: stored records are recomputed
_SEL = re.compile(r"@\{(.+)\}$")


def _git(cfg, repo, args, timeout=60):
    return gitro.run(cfg, repo, args, timeout=timeout)


def env_names(cfg):
    return [e for e in (cfg.get("env_branches") or DEFAULT_ENVS) if e]


def env_refs(cfg, repo):
    """{env: ref} preferring origin/<env> (shared truth) over the local copy."""
    rc, so, _ = _git(cfg, repo, ["for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin"])
    have = set(so.split()) if rc == 0 else set()
    out = {}
    for e in env_names(cfg):
        if "origin/" + e in have:
            out[e] = "origin/" + e
        elif e in have:
            out[e] = e
    return out


def local_branches(cfg, repo):
    fmt = "%(refname:short)%1f%(objectname)%1f%(upstream:short)%1f%(committerdate:iso-strict)%1f%(contents:subject)"
    rc, so, _ = _git(cfg, repo, ["for-each-ref", "--format=" + fmt, "refs/heads"])
    out = {}
    if rc != 0:
        return out
    for line in so.splitlines():
        p = line.split("\x1f")
        if len(p) >= 5:
            out[p[0]] = {"tip": p[1], "upstream": p[2], "last_commit": p[3], "last_subject": redact(p[4])[:160]}
    return out


def reflog(cfg, repo, ref, limit=2000):
    """[(iso_time, message, sha)] newest first."""
    rc, so, _ = _git(cfg, repo, ["log", "-g", "-n", str(limit), "--date=iso-strict", "--format=%gd%x1f%gs%x1f%H", ref])
    out = []
    if rc != 0:
        return out
    for line in so.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 3:
            continue
        m = _SEL.search(parts[0])
        if m:
            out.append((m.group(1), parts[1], parts[2]))
    return out


def _parent_from_head(head_log, branch, created):
    """HEAD reflog: the checkout that moved onto `branch` at/after its creation tells where it came from."""
    t0 = common.parse_ts(created)
    best = None
    for ts, msg, _sha in reversed(head_log):  # oldest first, so same-second ties resolve to the creation checkout
        m = re.match(r"checkout: moving from (.+) to (.+)$", msg)
        if not m or m.group(2) != branch:
            continue
        t = common.parse_ts(ts)
        if t and t0 and (t - t0).total_seconds() >= -2:
            if best is None or t < best[0]:
                best = (t, m.group(1))
    if best and (best[0] - t0).total_seconds() < 600:
        return best[1]
    # otherwise: whatever HEAD was on just before creation
    prev = None
    for ts, msg, _sha in sorted(head_log):
        t = common.parse_ts(ts)
        if t and t0 and t > t0:
            break
        m = re.match(r"checkout: moving from (.+) to (.+)$", msg)
        if m:
            prev = m.group(2)
    return prev if prev and prev != branch else None


def _count(cfg, repo, rng):
    rc, so, _ = _git(cfg, repo, ["rev-list", "--count", rng])
    return int(so.strip()) if rc == 0 and so.strip().isdigit() else None


def infer_parent(cfg, repo, branch, candidates, envs):
    """The true parent leaves the branch with the fewest own commits; ties go to the candidate that moved on least,
    then to env branches. Feature branches that already contain this branch are its children, not its parent.
    Returns (parent, [equally good alternatives]) - ties happen when env branches point at the same commit."""
    scored = []
    for c in candidates:
        if c in (branch, "origin/" + branch):
            continue
        is_env = c in envs.values() or c in envs
        if not is_env and _is_ancestor(cfg, repo, branch, c):
            continue
        ahead = _count(cfg, repo, "%s..%s" % (c, branch))   # own commits of branch vs c
        behind = _count(cfg, repo, "%s..%s" % (branch, c))  # commits on c not on branch
        if behind is None or ahead is None:
            continue
        scored.append(((ahead, behind, 0 if is_env else 1), c))
    if not scored:
        return None, []
    scored.sort()
    best = scored[0][0]
    tied = [c for k, c in scored if k == best]
    return tied[0], [_short(c) for c in tied[1:]]


def _short(ref):
    return ref[len("origin/"):] if ref and ref.startswith("origin/") else ref


def _is_ancestor(cfg, repo, a, b):
    return _git(cfg, repo, ["merge-base", "--is-ancestor", a, b])[0] == 0


def find_fork(cfg, repo, branch_log, parent_ref, tip, from_reflog):
    """Commit the branch was cut from: the latest 'rebase ... onto', else the reflog's creation commit,
    else the merge-base with the parent. Stays fixed after the branch is merged, unlike the parent's tip."""
    fork = None
    for ts, msg, sha in branch_log or []:  # newest first
        m = re.search(r"rebase.*onto ([0-9a-f]{7,40})", msg)
        if m:
            fork = m.group(1)
            break
    if not fork and from_reflog and branch_log:
        fork = branch_log[-1][2]
    if fork and not _is_ancestor(cfg, repo, fork, tip):
        fork = None
    if not fork and parent_ref:
        rc, so, _ = _git(cfg, repo, ["merge-base", parent_ref, tip])
        fork = so.strip() if rc == 0 and so.strip() else None
    return fork


def env_status(cfg, repo, tip, fork, parent, ticket, created, envs):
    out = {}
    own = _count(cfg, repo, "%s..%s" % (fork, tip)) if fork else None
    for env, ref in envs.items():
        if parent == env and own == 0:
            out[env] = {"status": "base"}
            continue
        if _is_ancestor(cfg, repo, tip, ref):
            out[env] = {"status": "merged"} if own != 0 else {"status": "contains base"}
            continue
        rc, so, _ = _git(cfg, repo, ["cherry", ref, tip] + ([fork] if fork else []))
        lines = [l for l in so.splitlines() if l[:1] in "+-"] if rc == 0 else []
        picked = len([l for l in lines if l.startswith("-")])
        if lines and picked == len(lines):
            out[env] = {"status": "cherry-picked"}
            continue
        if picked:
            out[env] = {"status": "partial %d/%d" % (picked, len(lines))}
            continue
        if ticket:
            args = ["log", ref, "-1", "--format=%h %cI", "--grep=" + ticket, "--fixed-strings"]
            if created:
                args.insert(2, "--since=" + created)
            rc, so, _ = _git(cfg, repo, args)
            if rc == 0 and so.strip():
                out[env] = {"status": "ticket commit", "commit": so.split()[0]}
                continue
        out[env] = {"status": "not merged"}
    return out


def scan_repo(cfg, repo, known):
    """Return {branch: record} for local branches of one repo; `known` = previous records (for caching)."""
    envs = env_refs(cfg, repo)
    env_local = set(env_names(cfg))
    branches = local_branches(cfg, repo)
    head_log = reflog(cfg, repo, "HEAD")
    prefixes = cfg.get("ticket_prefixes") or []
    env_sig = "|".join("%s=%s" % (e, _rev(cfg, repo, r)) for e, r in sorted(envs.items()))
    out = {}
    for b, info in branches.items():
        if b in env_local:
            continue
        old = known.get(b) or {}
        if old.get("v") != ALGO_VERSION:
            old = {k: v for k, v in old.items() if k in ("first_seen",)}
        rec = dict(old, v=ALGO_VERSION)
        rec.update({"repo": repo.replace("\\", "/"), "repo_name": os.path.basename(repo.rstrip("/\\")), "branch": b,
                    "ticket": common.ticket_from_branch(b, prefixes), "upstream": info["upstream"],
                    "last_commit": info["last_commit"], "last_subject": info["last_subject"]})
        rec.pop("deleted_at", None)
        blog = None
        if not old.get("parent"):
            blog = reflog(cfg, repo, "refs/heads/" + b)
            created, csrc, parent, psrc, local_since = None, None, None, None, None
            if blog:
                first_ts, msg, _sha = blog[-1]
                m = re.match(r"branch: Created from (.+)$", msg)
                if m and m.group(1).strip() in ("origin/" + b, "refs/remotes/origin/" + b):
                    rec["checked_out_from_remote"] = True  # someone (or another machine) created it; this is our checkout
                    local_since = first_ts
                elif m:
                    created, csrc = first_ts, "reflog"
                    src = m.group(1).strip()
                    if src == "HEAD":
                        parent, psrc = _parent_from_head(head_log, b, created), "reflog (HEAD)"
                    else:
                        parent, psrc = src, "reflog"
                else:
                    local_since = first_ts  # reflog truncated / cloned: creation not recorded
            if not parent:
                cands = list(envs.values()) + [x for x in branches if x != b and x not in env_local]
                parent, alts = infer_parent(cfg, repo, b, cands, envs)
                psrc = "inferred"
                if alts:
                    rec["parent_alternatives"] = alts
            fork = find_fork(cfg, repo, blog, parent, info["tip"], psrc == "reflog" or psrc == "reflog (HEAD)")
            if not created:
                first = None
                if fork:
                    rc, so, _ = _git(cfg, repo, ["log", "--reverse", "--format=%cI", "%s..%s" % (fork, b)])
                    first = so.split()[0] if rc == 0 and so.split() else None
                if first and (not local_since or first < local_since):
                    created, csrc = first, "first own commit"
                elif local_since:
                    created, csrc = local_since, "local checkout"
            rec.update({"parent": _short(parent) if parent else None, "parent_ref": parent, "fork_sha": fork,
                        "parent_source": psrc if parent else None, "created_at": created, "created_source": csrc,
                        "local_since": local_since})
        sig = info["tip"] + "#" + env_sig
        if old.get("_sig") != sig:
            base = rec.get("parent_ref")
            if base and _git(cfg, repo, ["rev-parse", "--verify", "-q", base])[0] != 0:
                base = None
            fork = rec.get("fork_sha")
            if not fork or not _is_ancestor(cfg, repo, fork, info["tip"]):  # rebased or reset since last scan
                fork = find_fork(cfg, repo, blog if blog is not None else reflog(cfg, repo, "refs/heads/" + b), base,
                                 info["tip"], (rec.get("parent_source") or "").startswith("reflog"))
                rec["fork_sha"] = fork
            rec["ahead"] = _count(cfg, repo, "%s..%s" % (fork, info["tip"])) if fork else None   # own commits
            rec["behind"] = _count(cfg, repo, "%s..%s" % (info["tip"], base)) if base else None  # parent moved on
            rec["envs"] = env_status(cfg, repo, info["tip"], fork, rec.get("parent"), rec.get("ticket"),
                                     rec.get("created_at"), envs)
            rec["_sig"] = sig
            rec["checked_at"] = common.now_iso()
        out[b] = rec
    return out


def _rev(cfg, repo, ref):
    rc, so, _ = _git(cfg, repo, ["rev-parse", "-q", "--verify", ref])
    return so.strip()[:12] if rc == 0 else ""


_ACT = [
    (re.compile(r"^checkout: moving from (.+) to (.+)$"), "switch"),
    (re.compile(r"^(pull|merge)\b.*?:\s*(.+)$"), "merge"),
    (re.compile(r"^rebase.*\(finish\)"), "rebase"),
    (re.compile(r"^cherry-pick: (.+)$"), "cherry-pick"),
    (re.compile(r"^commit( \(amend\)| \(merge\))?: (.+)$"), "commit"),
    (re.compile(r"^reset: moving to (.+)$"), "reset"),
]


def head_activity(cfg, repo, since_iso):
    """Branch activity from HEAD reflog newer than since_iso: switches, merges/pulls, rebases, cherry-picks, resets.
    Commits are left to the commit scanner. Returns oldest-first list of dicts."""
    since = common.parse_ts(since_iso)
    out, cur = [], None
    for ts, msg, _sha in sorted(reflog(cfg, repo, "HEAD", 500)):
        t = common.parse_ts(ts)
        m = re.match(r"checkout: moving from (.+) to (.+)$", msg)
        if m:
            cur = m.group(2)
        if not t or (since and t <= since):
            continue
        kind = None
        for rx, k in _ACT:
            if rx.match(msg):
                kind = k
                break
        if not kind or kind == "commit":
            continue
        rec = {"ts": t.isoformat(timespec="seconds"), "kind": kind, "repo_name": os.path.basename(repo.rstrip("/\\")),
               "text": redact(msg)[:200], "branch": cur}
        if m:
            rec["from"], rec["to"] = m.group(1), m.group(2)
        out.append(rec)
    return out
