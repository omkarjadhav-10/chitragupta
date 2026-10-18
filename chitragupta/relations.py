"""How a repo's base branches relate: which is cut from which, which merges into which, which syncs from which.

Config (`branching.repos.<repo>.relations`, a list):
    {"from": "release/*", "rel": "cut_from", "to": "master"}
    {"from": "release/*", "rel": "merges_into", "to": ["qa", "uat", "staging", "master"]}
    {"from": "qa", "rel": "syncs_from", "to": "master"}
`from` and `to` are branch names or fnmatch patterns (as in `bases`). Relations only shape the graph and the wording
of delivery explanations; the delivery check itself always uses real reachability.

    normalize(raw)        -> (relations, warnings)   cleaned list; bad entries dropped with a reason
    cut_from(name, rels)  -> the base `name` was cut from, or None
    explain(have, target, rels) -> "reaches qa when release/x is merged into qa", or None
    detect(cfg, repo_path, bases) -> {"proposals": [...], "evidence": {...}}   read-only git through gitro
"""
import collections
import fnmatch

KINDS = ("cut_from", "merges_into", "syncs_from")
FLAT_DEPTH = 1500   # first-parent commits read per base
MERGE_DEPTH = 400   # merges read per flat base
RELEASE_SAMPLE = 6  # newest pattern branches examined for "cut from"


def _list(x):
    return [x] if isinstance(x, str) else [t for t in (x or []) if isinstance(t, str)]


def normalize(raw):
    """(clean relations, warnings) from the config value; never raises."""
    out, warnings = [], []
    if raw in (None, ""):
        return out, warnings
    if not isinstance(raw, list):
        return out, ["relations must be a list"]
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            warnings.append("relation %d is not an object" % (i + 1))
            continue
        frm, rel, to = r.get("from"), r.get("rel"), _list(r.get("to"))
        if not isinstance(frm, str) or not frm.strip():
            warnings.append("relation %d has no 'from'" % (i + 1))
        elif rel not in KINDS:
            warnings.append("relation %d: 'rel' must be one of %s" % (i + 1, ", ".join(KINDS)))
        elif not to:
            warnings.append("relation %d has no 'to'" % (i + 1))
        else:
            out.append({"from": frm.strip(), "rel": rel, "to": [t.strip() for t in to if t.strip()]})
    return out, warnings


def matches(pattern, name):
    return bool(name) and (pattern == name or fnmatch.fnmatchcase(name, pattern))


def cut_from(name, rels):
    for r in rels or []:
        if r["rel"] == "cut_from" and matches(r["from"], name):
            return r["to"][0]
    return None


def merges_into(name, rels):
    return [t for r in rels or [] if r["rel"] == "merges_into" and matches(r["from"], name) for t in r["to"]]


def syncs_from(name, rels):
    return [t for r in rels or [] if r["rel"] == "syncs_from" and matches(r["from"], name) for t in r["to"]]


def explain(have, target, rels):
    """Why a change that is already in `have` will reach `target` (a sentence fragment), or None."""
    for t in merges_into(have, rels):
        if matches(t, target):
            return "reaches %s when %s is merged into %s" % (target, have, target)
    for src in syncs_from(target, rels):
        if matches(src, have):
            return "reaches %s at its next sync from %s" % (target, have)
    return None


def order(bases, rels):
    """Bases sorted so every base comes after the base it was cut from (stable otherwise)."""
    names = list(bases)
    placed, out = set(), []

    def visit(n, depth=0):
        if n in placed or depth > len(names):
            return
        p = cut_from(n, rels)
        if p and p in names and p != n:
            visit(p, depth + 1)
        placed.add(n)
        out.append(n)
    for n in names:
        visit(n)
    return out


# --------------------------------------------------------------------------- detection (read-only)

def _short(ref, remote):
    return ref[len(remote) + 1:] if ref.startswith(remote + "/") else ref


def detect(cfg, repo, bases, remote="origin"):
    """Propose relations for `repo` from its remote-tracking refs only (no fetch). `bases`: role -> name/pattern(s)
    (gitcmd.repo_conf()["bases"]). Every git call goes through gitro (the hardened read-only allowlist)."""
    from . import gitro

    def git(*args):
        rc, out, _ = gitro.run(cfg, repo, list(args), timeout=120)
        return out if rc == 0 else ""

    pats = [p for v in bases.values() for p in _list(v)]
    fixed = [p for p in pats if not any(c in p for c in "*?[")]
    patterns = [p for p in pats if any(c in p for c in "*?[")]
    refs = [l.split("\t") for l in git("for-each-ref", "--sort=-committerdate", "--format=%(refname:short)\t%(committerdate:short)",
                                         "refs/remotes/%s" % remote).splitlines() if "\t" in l]
    names = [_short(r[0], remote) for r in refs if r[0].startswith(remote + "/")]
    flat = [n for n in fixed if n in names]
    rel_refs = [n for n in names if any(fnmatch.fnmatchcase(n, p) for p in patterns)][:RELEASE_SAMPLE]
    pattern_of = {n: next(p for p in patterns if fnmatch.fnmatchcase(n, p)) for n in rel_refs}
    fp = {n: git("rev-list", "--first-parent", "-n", str(FLAT_DEPTH), "%s/%s" % (remote, n)).split() for n in flat + rel_refs}
    fpset = {n: set(v) for n, v in fp.items()}

    def group(n):
        return pattern_of.get(n, n)

    flows, examples = collections.Counter(), {}
    for x in flat:
        for line in git("log", "--merges", "--first-parent", "-n", str(MERGE_DEPTH), "--format=%H %P", "%s/%s" % (remote, x)).splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            src = {group(y) for y in flat + rel_refs if y != x and parts[2] in fpset[y]}
            if len(src) != 1:
                continue  # on several bases' first-parent lines: not evidence of one direction
            key = (src.pop(), x)
            flows[key] += 1
            examples.setdefault(key, parts[0][:9])
    cuts = collections.defaultdict(collections.Counter)
    for r in rel_refs:
        for c in fp[r]:
            on = [b for b in flat if c in fpset[b]]
            if on:
                cuts[pattern_of[r]][on[0] if len(on) == 1 else tuple(sorted(on))] += 1
                break
    proposals = []
    for pat, cnt in cuts.items():
        clear = [(b, n) for b, n in cnt.most_common() if isinstance(b, str)]  # a commit on one base's line only
        if clear:
            best, n = clear[0]
            proposals.append({"from": pat, "rel": "cut_from", "to": best, "evidence": "%d of %d recent branches" % (n, sum(cnt.values()))})
    into = collections.defaultdict(list)
    for (src, dst), n in flows.most_common():
        back = flows.get((dst, src), 0)
        if src in flat and dst in flat and back and back >= n:
            continue  # the other direction is the main flow
        kind = "syncs_from" if src in flat and dst in flat and back == 0 and n <= 3 else "merges_into"
        if kind == "syncs_from":
            proposals.append({"from": dst, "rel": "syncs_from", "to": src, "evidence": "%d merges, e.g. %s" % (n, examples[(src, dst)])})
        else:
            into[src].append((dst, n, examples[(src, dst)]))
    for src, ts in into.items():
        proposals.append({"from": src, "rel": "merges_into", "to": [t for t, _, _ in ts],
                          "evidence": ", ".join("%s %d (e.g. %s)" % (t, n, ex) for t, n, ex in ts)})
    return {"repo": repo, "bases": flat, "pattern_sample": rel_refs, "proposals": proposals}
