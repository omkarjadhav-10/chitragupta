"""Views of a ticket's stored branch-tracking block (tk["git"], written by delivery.py) — pure functions, no git, no I/O.

    gitgraph(repo_entry)  -> Mermaid `gitGraph` source, or None when there is nothing to draw or it is too large
    graph(repo_entry)     -> {"source", "legend"}: the same, plus {drawn name: real name} for every renamed branch
    check(source)         -> [problems]: replays the gitGraph statements with Mermaid's own rules (tests, self-check)

The graph is built only from what is stored: base branches have no history here (the dashboard never runs git), so
each base used by the ticket (a parent or a PR target) is one lane that starts with a single marker commit. Ticket
branches start from their parent, carry their own commits in date order, and merged PRs are merges into the target.
A commit whose patch also sits on another branch (cherry-pick / rebase copy, same delivery row) is drawn on every
branch it is on after the first, tagged "cherry-pick of <sha>". Commits that miss a target are drawn crossed (REVERSE).
"""
import datetime as _dt
import re

SYMBOL = {"in": "✓", "same": "✓", "squash": "✓", "open": "⏳", "missing": "✗", "reverted": "↩", "check": "?"}
LABEL = {"in": "in target", "same": "same change in target", "squash": "via squash PR", "open": "PR open",
         "missing": "missing", "reverted": "reverted", "check": "check by hand"}
GOOD = ("in", "same", "squash")
BAD = ("missing", "reverted")

MAX_COMMITS = 40
MAX_BRANCHES = 14
SUBJECT_LEN = 28
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


# --------------------------------------------------------------------------- small helpers

def _ts(s):
    """An aware datetime for an ISO timestamp (or date); None when unparseable."""
    if not isinstance(s, str) or not s.strip():
        return None
    t = s.strip().replace("Z", "+00:00")
    try:
        d = _dt.datetime.fromisoformat(t)
    except ValueError:
        try:
            d = _dt.datetime.fromisoformat(t[:10])
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d


def _label(text, limit=60):
    """A commit id / tag Mermaid accepts inside double quotes: a small safe character set, no quotes."""
    s = re.sub(r"[^\w .,:/()+#-]+", " ", str(text or ""), flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit - 1].rstrip() + "…"
    return s or "commit"


def safe_branch(name):
    """A branch name drawn as is when it only uses letters, digits and . _ / -; anything else becomes '-'."""
    s = re.sub(r"[^A-Za-z0-9_./-]+", "-", str(name or "").strip())
    s = re.sub(r"([./-])[./-]+", r"\1", s).strip("./-")
    return s or "branch"


class _Names:
    """Original branch name -> unique safe name, with a legend of the ones that changed."""

    def __init__(self):
        self.map, self.used, self.legend = {}, set(), {}

    def __call__(self, name):
        if name in self.map:
            return self.map[name]
        base = safe_branch(name)
        s, n = base, 2
        while s.lower() in self.used:
            s, n = "%s-%d" % (base, n), n + 1
        self.used.add(s.lower())
        self.map[name] = s
        if s != name:
            self.legend[s] = name
        return s


def _q(s):
    return '"%s"' % s.replace('"', "").replace("\\", "/")


# --------------------------------------------------------------------------- the graph

def graph(repo_entry, max_commits=MAX_COMMITS, max_branches=MAX_BRANCHES):
    """{"source": gitGraph text or None, "legend": {drawn: real}, "why": reason when None}."""
    e = repo_entry if isinstance(repo_entry, dict) else {}
    branches = [b for b in e.get("branches") or [] if isinstance(b, dict) and b.get("branch")]
    prs = [p for p in e.get("prs") or [] if isinstance(p, dict)]
    if not branches:
        return {"source": None, "legend": {}, "why": "no branches"}
    if len(branches) > max_branches:
        return {"source": None, "legend": {}, "why": "too many branches to draw (%d)" % len(branches)}
    tnames = {b["branch"] for b in branches}

    # copies: a delivery row lists every sha (and changed copy) of one change
    row_of = {}
    for row in e.get("rows") or []:
        for s in (row.get("shas") or []) + (row.get("loose") or []):
            row_of.setdefault(str(s)[:9], row)

    # every commit once, at its own date, on the first branch that has it
    seen, commits = set(), []
    for bi, b in enumerate(branches):
        cs = [c for c in b.get("commits") or [] if isinstance(c, dict) and c.get("sha")]
        n = len(cs)
        for ci, c in enumerate(cs):  # stored newest first
            sha = str(c["sha"])[:9]
            if sha in seen:
                continue
            seen.add(sha)
            commits.append({"branch": b["branch"], "sha": sha[:7], "key": sha, "subject": c.get("subject") or "",
                            "at": _ts(c.get("date")) or _EPOCH, "tie": (bi, n - ci)})
    if not commits:
        return {"source": None, "legend": {}, "why": "no commits recorded"}
    if len(commits) > max_commits:
        return {"source": None, "legend": {}, "why": "too many commits to draw (%d)" % len(commits)}
    commits.sort(key=lambda c: (c["at"], c["tie"]))
    first_of_row = {}
    for c in commits:
        row = row_of.get(c["key"])
        if row is not None:
            rid = id(row)
            if rid in first_of_row:
                c["copy_of"] = first_of_row[rid]
            else:
                first_of_row[rid] = c["sha"]
            c["bad"] = any((cell or {}).get("s") in BAD for cell in (row.get("cells") or {}).values())

    # lanes for the bases this ticket uses (parents and PR targets that aren't ticket branches)
    bases = []
    for b in branches:
        p = b.get("parent")
        if p and p not in tnames and p not in bases:
            bases.append(p)
    for p in prs:
        t = p.get("target")
        if t and t not in tnames and t not in bases:
            bases.append(t)
    if not bases:
        bases = ["base"]
    names = _Names()
    for x in bases:
        names(x)
    for b in branches:
        names(b["branch"])

    # events in time order: branch creation, commits, merges of merged PRs
    first_commit = {}
    for c in commits:
        first_commit.setdefault(c["branch"], c["at"])
    events = []
    for bi, b in enumerate(branches):
        at = _ts(b.get("created_at"))
        fc = first_commit.get(b["branch"])
        if at is None or (fc is not None and fc < at):
            at = fc or _EPOCH
        events.append((at, 0, bi, 0, ("branch", b)))
    for c in commits:
        events.append((c["at"], 1, c["tie"][0], c["tie"][1], ("commit", c)))
    for i, p in enumerate(prs):
        if p.get("state") == "merged" and p.get("source") in tnames and p.get("target"):
            events.append((_ts(p.get("merged_at")) or _EPOCH, 2, i, 0, ("merge", p)))
    events.sort(key=lambda ev: ev[:4])

    lines = ["gitGraph"]
    cur = [None]
    heads = {}       # drawn branch -> (head id, branch the head commit was made on)
    ids = set()
    created = set()
    merged = {}      # (source, target) -> source head id already merged

    def checkout(name):
        if cur[0] != name:
            lines.append("  checkout %s" % _q(name))
            cur[0] = name

    def uid(text):
        s, n = text, 2
        while s in ids:
            s, n = "%s ~%d" % (text, n), n + 1
        ids.add(s)
        return s

    # base lanes: the first is Mermaid's main branch (renamed by the init directive), the others fork from its marker
    main = names(bases[0])
    lines.insert(0, '%%%%{init: {"gitGraph": {"mainBranchName": %s, "showCommitLabel": true}} }%%%%' % _q(main))
    cur[0] = main
    for i, x in enumerate(bases):
        nm = names(x)
        if i:
            checkout(main)
            lines.append("  branch %s" % _q(nm))
            cur[0] = nm
        cid = uid(_label(x + " (base)"))
        lines.append("  commit id: %s type: HIGHLIGHT" % _q(cid))
        heads[nm] = (cid, nm)
        created.add(nm)

    for _, kind, _, _, (what, obj) in events:
        if what == "branch":
            b = obj
            nm = names(b["branch"])
            if nm in created:
                continue
            parent = b.get("parent")
            pn = names(parent) if parent and (parent in tnames or parent in bases) else main
            if pn not in created or pn == nm:
                pn = main
            checkout(pn)
            lines.append("  branch %s" % _q(nm))
            cur[0] = nm
            heads[nm] = heads[pn]
            created.add(nm)
        elif what == "commit":
            c = obj
            nm = names(c["branch"])
            if nm not in created:
                continue
            checkout(nm)
            cid = uid(_label("%s %s" % (c["sha"], _label(c["subject"], SUBJECT_LEN)), 60))
            st = "  commit id: %s" % _q(cid)
            if c.get("copy_of"):
                st += " tag: %s" % _q(_label("cherry-pick of %s" % c["copy_of"]))
            if c.get("bad"):
                st += " type: REVERSE"
            lines.append(st)
            heads[nm] = (cid, nm)
        else:
            p = obj
            src, tgt = names(p["source"]), names(p["target"])
            if src == tgt or src not in created or tgt not in created:
                continue
            sh, th = heads.get(src), heads.get(tgt)
            if not sh or not th or sh[0] == th[0] or th[1] == src or sh[1] != src:
                continue  # nothing of its own to merge, or Mermaid would call it a merge into itself
            if merged.get((src, tgt)) == sh[0]:
                continue
            checkout(tgt)
            mid = uid(_label("PR %s into %s" % (p.get("number") if p.get("number") is not None else "?", p["target"])))
            lines.append("  merge %s id: %s tag: %s" % (_q(src), _q(mid), _q(_label("PR #%s" % p.get("number")))))
            heads[tgt] = (mid, tgt)
            merged[(src, tgt)] = sh[0]
    src = "\n".join(lines) + "\n"
    problems = check(src)
    if problems:
        return {"source": None, "legend": names.legend, "why": "graph self-check failed: " + problems[0]}
    return {"source": src, "legend": names.legend, "why": None}


def gitgraph(repo_entry, max_commits=MAX_COMMITS):
    """Mermaid gitGraph source for one repo of tk["git"]["repos"], or None (then the page shows the tables only)."""
    return graph(repo_entry, max_commits)["source"]


# --------------------------------------------------------------------------- checker (Mermaid's gitGraph rules)

_NAME = r'"[^"\\]*"|\w(?:[-./\w]*[-\w])?'
_RX = {
    "commit": re.compile(r'^commit(?:\s+id:\s*"([^"]*)")?(?:\s+tag:\s*"[^"]*")*(?:\s+type:\s*(NORMAL|REVERSE|HIGHLIGHT))?$'),
    "branch": re.compile(r"^branch\s+(%s)(?:\s+order:\s*\d+)?$" % _NAME),
    "checkout": re.compile(r"^(?:checkout|switch)\s+(%s)$" % _NAME),
    "merge": re.compile(r'^merge\s+(%s)(?:\s+id:\s*"([^"]*)")?(?:\s+tag:\s*"[^"]*")*(?:\s+type:\s*(?:NORMAL|REVERSE|HIGHLIGHT))?$' % _NAME),
}


def _unq(s):
    return s[1:-1] if s.startswith('"') else s


def check(src):
    """Problems Mermaid would raise for this gitGraph (syntax of the statements we emit + its branch/merge rules)."""
    errs = []
    lines = [l.strip() for l in (src or "").split("\n")]
    lines = [l for l in lines if l and not l.startswith("%%")]
    if not lines or lines[0] != "gitGraph":
        return ["not a gitGraph"]
    main = "main"
    m = re.search(r'"mainBranchName":\s*"([^"]*)"', src or "")
    if m:
        main = m.group(1)
    branches = {main: None}   # name -> head commit id
    commit_branch = {}
    cur = main
    seq = 0
    for l in lines[1:]:
        if l.startswith("commit"):
            mm = _RX["commit"].match(l)
            if not mm:
                errs.append("bad commit: " + l)
                continue
            cid = mm.group(1) or "auto-%d" % seq
            seq += 1
            if cid in commit_branch:
                errs.append("duplicate commit id: " + cid)
            commit_branch[cid] = cur
            branches[cur] = cid
        elif l.startswith("branch"):
            mm = _RX["branch"].match(l)
            if not mm:
                errs.append("bad branch: " + l)
                continue
            nm = _unq(mm.group(1))
            if nm in branches:
                errs.append("branch exists: " + nm)
                continue
            branches[nm] = branches[cur]
            cur = nm
        elif l.startswith(("checkout", "switch")):
            mm = _RX["checkout"].match(l)
            if not mm:
                errs.append("bad checkout: " + l)
                continue
            nm = _unq(mm.group(1))
            if nm not in branches:
                errs.append("checkout of unknown branch: " + nm)
                continue
            cur = nm
        elif l.startswith("merge"):
            mm = _RX["merge"].match(l)
            if not mm:
                errs.append("bad merge: " + l)
                continue
            other, mid = _unq(mm.group(1)), mm.group(2)
            ch, oh = branches.get(cur), branches.get(other)
            if other == cur:
                errs.append("merge of a branch into itself: " + other)
            elif other not in branches:
                errs.append("merge of unknown branch: " + other)
            elif ch is None:
                errs.append("merge into a branch without commits: " + cur)
            elif oh is None:
                errs.append("merge of a branch without commits: " + other)
            elif ch == oh:
                errs.append("merge with the same head: " + other)
            elif commit_branch.get(ch) == other:
                errs.append("merge into itself (target head is on %s)" % other)
            else:
                cid = mid or "auto-%d" % seq
                seq += 1
                if cid in commit_branch:
                    errs.append("duplicate commit id: " + cid)
                commit_branch[cid] = cur
                branches[cur] = cid
        else:
            errs.append("unknown statement: " + l)
    return errs


# --------------------------------------------------------------------------- small text helpers (export, CLI)

def pr_source_text(src):
    return {"gh": "PR info from GitHub (gh)", "glab": "PR info from GitLab (glab)", "az": "PR info from Azure DevOps (az)",
            "merge-commit": "PR info from merge commits (open PRs and reviews unavailable)",
            "test": "PR info from test data"}.get(src or "", "PR info unavailable")


def cell_text(cell):
    s = (cell or {}).get("s") or "missing"
    return "%s %s" % (SYMBOL.get(s, "?"), LABEL.get(s, s))
