"""The ticket brief (FX-20): what a colleague needs to understand a ticket in one minute.

One model call per ticket (purpose "brief", model models.brief, else models.consolidate) over the ticket's current
items and its deterministic facts (commits, PRs, delivery stage, test runs, replay steps, queries, tables, the Jira
card and your notes; never raw events). The answer is validated claim by claim: a claim that cites nothing, cites an
id that is not a current item of this ticket or a deterministic fact, or names a path, table, endpoint or class the
ticket doesn't know is dropped and logged (brief.dropped). Status, "Fixes made" and whether the fix is verified come
from the data, never from model text. A note of yours ("ABC-1001: root cause: …" in inbox.md) replaces the model's
wording for that section and survives every regeneration.

Stored in tk["brief"] (schema v1, docs/architecture.md "The ticket brief"); rendered only through view(tk). Written at
day close when the inputs changed (source_hash), by `/cg:ticket <KEY> --brief` and by the dashboard's brief-refresh
action, within brief.daily_cap and budget.model_daily_usd. An Epic (jira.parent_issue_types) gets no brief: view()
returns its children instead.
"""
import hashlib
import json
import os
import re

from . import common
from . import store as S

V = 1
SECTIONS = ("root_cause", "findings", "before", "proposed", "fixes_made", "after", "cons")
HEADINGS = {"root_cause": "Root cause", "findings": "Helpful findings", "before": "How it works today",
            "proposed": "Proposed fix", "fixes_made": "Fixes made", "after": "How it works after the fix",
            "cons": "Possible cons"}
WHY = "Why this change"
NOT_RECORDED = "Not recorded yet"
STORY_TYPES = ("story", "task", "sub-task", "subtask", "improvement", "new feature", "feature", "spike",
               "technical task", "enhancement", "chore")
INPUT_CAP = 100_000  # ~25k tokens of input
WORD_CAP = 400
CONF_RANK = {"none": 0, "hypothesis": 1, "likely": 2, "confirmed": 3}
FACT_PREFIXES = ("commit:", "pr:", "jira:", "query:", "table:", "replay:", "test:")
VERIFIED_STATUSES = ["QA COMPLETED", "QA PASSED", "READY FOR RELEASE", "DONE", "CLOSED", "RESOLVED"]


def verified_statuses(cfg=None):
    """Jira statuses that mean QA verified the fix (brief.verified_statuses)."""
    from . import configstore
    v = configstore.get(cfg if cfg is not None else common.load_config(), "brief.verified_statuses", None)
    return [str(x) for x in v] if isinstance(v, list) else list(VERIFIED_STATUSES)


def qa_verified(status, cfg=None):
    """True when a Jira status is one of brief.verified_statuses (any case)."""
    s = " ".join(str(status or "").split()).upper()
    return bool(s) and s in {" ".join(x.split()).upper() for x in verified_statuses(cfg)}


# --------------------------------------------------------------------------- what is current

def is_current(it):
    """Not archived, not superseded or done (a TODO git data closed), not flagged for review (K3, K8, K12)."""
    return (isinstance(it, dict) and not it.get("archived") and it.get("status") not in ("superseded", "done")
            and not it.get("review"))


def current_items(tk):
    """The ticket's current items, newest first; of two with the same kind and title only the newer is kept."""
    out, seen = [], set()
    items = sorted((i for i in (tk.get("items") or {}).values() if is_current(i)),
                   key=lambda i: (i.get("date") or "", i.get("time") or "", i.get("id") or ""), reverse=True)
    for i in items:
        k = (i.get("kind"), " ".join(str(i.get("title") or "").lower().split()))
        if k in seen:
            continue
        seen.add(k)
        out.append(i)
    return out


def current_files(tk):
    return [f for f in (tk.get("files") or {}).values()
            if isinstance(f, dict) and not f.get("archived") and f.get("verified") is not False]


def current_queries(tk):
    return [q for q in (tk.get("queries") or {}).values()
            if isinstance(q, dict) and not q.get("archived") and q.get("sent") is not False and q.get("status") != "superseded"]


def kind_of(tk, cfg=None):
    """"epic" for a parent issue type (jira.parent_issue_types), "story" for stories and tasks, else "bug"."""
    t = str((tk.get("jira") or {}).get("type") or "").strip().lower() if isinstance(tk.get("jira"), dict) else ""
    if not t and tk.get("id"):
        t = str(common._card(tk["id"]).get("type") or "").strip().lower()  # a card not yet applied to the record
    if t and t in {x.lower() for x in common.parent_issue_types(cfg if cfg is not None else common.load_config())}:
        return "epic"
    return "story" if t in STORY_TYPES else "bug"


def jira_card(key):
    """data/jira/<KEY>.json card with its description and comments (taken from the raw result for older cards)."""
    rec = common.read_json(common.hp("data", "jira", key + ".json"), {}) or {}
    card = dict(rec.get("card") or {}) if isinstance(rec.get("card"), dict) else {}
    if card and rec.get("raw") and "comments" not in card:
        from . import mcpjobs
        card.update(mcpjobs.card_sources(rec["raw"]))
    return card


# --------------------------------------------------------------------------- deterministic facts

def _sha7(sha):
    return str(sha or "")[:7]


def _prs(tk):
    seen, out = set(), []
    for x in ((tk.get("git") or {}).get("repos") or {}).values():
        for p in x.get("prs") or []:
            if p.get("number") is not None and p["number"] not in seen:
                seen.add(p["number"])
                out.append(p)
    return out


def _commits(tk):
    """One row per delivery patch group; old SHA citations remain valid aliases. No stash snapshots."""
    from . import sources
    raw = {str(c["sha"])[:7]: c for c in (tk.get("commits") or {}).values()
           if isinstance(c, dict) and c.get("sha") and not sources.stash_commit(c.get("ref"), c.get("subject"))}
    out, covered = [], set()
    for name, repo in ((tk.get("git") or {}).get("repos") or {}).items():
        for row in repo.get("rows") or []:
            shas = list(dict.fromkeys(_sha7(s) for s in list(row.get("shas") or []) + list(row.get("loose") or []) if s))
            if not shas or sources.stash_commit("", row.get("subject")):
                continue
            primary = shas[0]
            c = raw.get(primary) or {}
            out.append({"sha7": primary, "aliases": shas[1:], "subject": row.get("subject") or c.get("subject") or "",
                        "date": str(row.get("date") or c.get("date") or "")[:10], "repo": name, "ref": c.get("ref") or "",
                        "patch_id": row.get("pid") or row.get("id") or primary, "branches": list(row.get("branches") or []),
                        "reached": [target for target, cell in (row.get("cells") or {}).items() if cell.get("s") in ("in", "same", "squash")]})
            covered.update(shas)
    for sha, c in raw.items():
        if sha in covered:
            continue
        pid = c.get("patch_id") or c.get("pid")
        group = next((r for r in out if pid and r["patch_id"] == pid and r["repo"] == c.get("repo_name")), None)
        if group:
            group["aliases"].append(sha)
            continue
        out.append({"sha7": sha, "aliases": [], "subject": c.get("subject") or "", "date": str(c.get("date") or "")[:10],
                    "repo": c.get("repo_name") or "", "ref": c.get("ref") or "", "patch_id": pid or sha,
                    "branches": [c["ref"]] if c.get("ref") else [], "reached": []})
    return sorted(out, key=lambda c: (c["date"], c["sha7"]))


def _stage(tk):
    from . import delivery
    return delivery.stage(tk.get("git") or {})


def fixes_made(tk):
    """The "Fixes made" list, computed from git and PR data at render time (never stored, never model text):
    [{"id": "commit:<sha7>" | "pr:<n>" | "stage", "kind": commit|pr|stage, "text", "date"?, ...}]."""
    out = [{"id": "commit:" + c["sha7"], "kind": "commit", "text": c["subject"], "date": c["date"], "repo": c["repo"]}
           for c in _commits(tk)]
    for p in _prs(tk):
        out.append({"id": "pr:%s" % p["number"], "kind": "pr", "state": p.get("state") or "",
                    "text": "PR #%s %s → %s" % (p["number"], p.get("source") or "?", p.get("target") or "?"),
                    "date": str(p.get("merged_at") or "")[:10]})
    st = _stage(tk)
    if out and st and st.get("kind") != "not started":
        out.append({"id": "stage", "kind": "stage", "text": st.get("text") or st.get("kind")})
    return out


def facts(key, tk, card=None):
    """Deterministic facts with their ids, as fed to the model and used to check what it cites."""
    card = jira_card(key) if card is None else card
    f = {}
    if card.get("found"):
        f["jira"] = {"id": "jira:" + key, "type": card.get("type") or "", "status": card.get("status") or "",
                     "title": card.get("title") or "", "description": card.get("description") or "",
                     "comments": [{"at": c.get("at"), "text": c.get("text")} for c in card.get("comments") or []]}
    st = _stage(tk)
    if st:
        f["delivery_stage"] = st.get("text") or st.get("kind")
    f["commits"] = [{"id": "commit:" + c["sha7"], "aliases": ["commit:" + s for s in c["aliases"]],
                     "subject": c["subject"], "date": c["date"], "repo": c["repo"]}
                    for c in reversed(_commits(tk))][:40]
    f["prs"] = [{"id": "pr:%s" % p["number"], "source": p.get("source"), "target": p.get("target"),
                 "state": p.get("state"), "merged": str(p.get("merged_at") or "")[:10]} for p in _prs(tk)]
    f["tests"] = [{"id": "test:" + t["id"], "command": t.get("command"), "status": t.get("status"),
                   "counts": t.get("counts") or {}, "at": t.get("at")}
                  for t in sorted((tk.get("tests") or {}).values(), key=lambda t: t.get("at") or "", reverse=True)
                  if isinstance(t, dict) and t.get("id")][:15]
    f["replay"] = [{"id": "replay:%s" % s.get("n"), "name": s.get("name"), "method": s.get("method"),
                    "expected": s.get("expected") or "", "scenario": s.get("scenario") or ""}
                   for s in (tk.get("replay") or {}).get("final") or [] if isinstance(s, dict) and s.get("n") is not None]
    f["queries"] = [{"id": "query:" + q.get("id", ""), "purpose": q.get("purpose") or "",
                     "revealed": q.get("revealed") or "", "tables": q.get("tables") or []}
                    for q in sorted(current_queries(tk), key=lambda q: q.get("date") or "", reverse=True) if q.get("id")][:30]
    f["tables"] = [{"id": "table:" + k, "purpose": (v or {}).get("purpose") or ""}
                   for k, v in sorted((tk.get("tables") or {}).items()) if not (v or {}).get("archived")][:40]
    f["apis"] = sorted(k for k, v in (tk.get("apis") or {}).items() if not (v or {}).get("archived"))[:40]
    f["files"] = [{"path": x.get("path"), "changed": bool(x.get("changed")), "verified": x.get("verified")}
                  for x in sorted(current_files(tk), key=lambda x: x.get("path") or "")][:60]
    return {k: v for k, v in f.items() if v}


def _fact_ids(f):
    ids = set()
    if f.get("jira"):
        ids.add(f["jira"]["id"])
    for k in ("commits", "prs", "tests", "replay", "queries", "tables"):
        ids |= {x["id"] for x in f.get(k) or []}
        ids |= {alias for x in f.get(k) or [] for alias in x.get("aliases") or []}
    return ids


def _item_view(i):
    d = {"id": i["id"], "kind": i.get("kind"), "title": i.get("title") or "",
         "detail": common.truncate(i.get("detail") or "", 300), "confidence": i.get("confidence") or "",
         "date": i.get("date") or ""}
    for k in ("status", "phase", "state"):
        if i.get(k):
            d[k] = i[k]
    if i.get("session") == "inbox" or i.get("src") == "user":
        d["user_note"] = True
    ev = [common.truncate(str(e), 120) for e in i.get("evidence") or []][:2]
    if ev:
        d["evidence"] = ev
    return d


def build_input(key, tk, cfg=None, card=None):
    """The model's input: current items with ids (newest first, then the most evidenced), the deterministic facts and
    your notes; capped at INPUT_CAP chars. Never raw events."""
    kind = kind_of(tk, cfg)
    from . import relevance
    relevance.classify(tk)
    f = facts(key, tk, card)
    data = {"ticket": key, "title": S.display_title(tk) or tk.get("title_hint") or "",
            "kind": kind, "heading_1": WHY if kind == "story" else HEADINGS["root_cause"],
            "status_inferred": tk.get("status_inferred") or "", "problem": tk.get("problem") or "",
            "facts": f, "relevance_candidates": relevance.candidates(tk), "items": []}
    from .redact import redact
    used = len(json.dumps(data, ensure_ascii=False))
    items = sorted(current_items(tk), key=lambda i: (i.get("date") or "", len(i.get("evidence") or [])), reverse=True)
    for i in items:
        v = _item_view(i)
        n = len(json.dumps(v, ensure_ascii=False)) + 2
        if used + n > INPUT_CAP:
            break
        data["items"].append(v)
        used += n
    return redact(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def source_hash(key, tk, cfg=None, card=None):
    """sha256 over everything the brief is made from: the current items (id, kind, title, detail, confidence, status,
    phase, state) and the deterministic facts. A brief whose source_hash differs is stale."""
    def wording(text):
        return " ".join(str(text or "").lower().split())
    items = sorted([i.get("id"), i.get("kind"), wording(i.get("title")), wording(i.get("detail")), i.get("confidence"), i.get("status"),
                    i.get("phase"), i.get("state")] for i in current_items(tk))
    from . import relevance
    blob = json.dumps({"v": V, "summary_v": 1, "kind": kind_of(tk, cfg), "items": items, "facts": facts(key, tk, card),
                       "records": relevance.source_records(tk)},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stale(tk, key=None, cfg=None):
    """{"since": <when the brief was written>} when its inputs changed since ("New findings since …"), else None."""
    b = tk.get("brief") if isinstance(tk.get("brief"), dict) else None
    if not b or b.get("kind") == "epic":
        return None
    key = key or tk.get("id")
    if b.get("source_hash") == source_hash(key, tk, cfg):
        return None
    return {"since": b.get("generated_at") or ""}


# --------------------------------------------------------------------------- the validator

_EXT = (r"java|kt|kts|py|js|mjs|ts|tsx|jsx|cs|go|rb|php|sql|xml|json|ya?ml|properties|bru|html?|vue|scala|sh|ps1|"
        r"cfg|ini|gradle|tf|cpp|c|h|hpp|swift|rs|md|csv|toml|conf|env")
_PATH_RX = re.compile(r"(?<![\w@./-])(?:[\w.-]+/)*[\w-]+\.(?:%s)\b" % _EXT, re.I)
_ENDPOINT_RX = re.compile(r"(?<![\w./:])/[A-Za-z{:][\w{}:.-]*(?:/[\w{}:.-]+)*")
_DOTTED_RX = re.compile(r"(?<![\w./])[a-z_][a-z0-9_]+(?:\.[a-z_][a-z0-9_]+){1,2}(?![\w(/])")
_CLASS_RX = re.compile(r"\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b")
_COMMON_WORDS = {"javascript", "typescript", "postgresql", "mysql", "mongodb", "github", "gitlab", "powershell",
                 "junit", "oauth", "jira", "nodejs", "webhook", "datetime", "localdate", "uuid", "sqlserver", "pytest",
                 "testng", "springboot", "openapi", "graphql", "nullpointerexception", "iphone", "devops"}


def _words(s):
    return len(str(s or "").split())


def _passing(tk):
    """Read verification ids directly, without Jira, facts caps or the generation corpus."""
    tests = {"test:" + t["id"] for t in (tk.get("tests") or {}).values()
             if isinstance(t, dict) and t.get("id") and t.get("status") == "passed" and not t.get("archived")}
    replay = {"replay:%s" % r["n"] for r in (tk.get("replay") or {}).get("final") or []
              if isinstance(r, dict) and r.get("n") is not None}
    return tests | replay


class _Ctx:
    """What a claim may cite and name: this ticket's current item ids and deterministic facts, its verified files
    and the words its current records use."""

    def __init__(self, key, tk, card):
        self.items = {i["id"]: i for i in current_items(tk)}
        self.all_items = tk.get("items") or {}
        f = facts(key, tk, card)
        self.facts = f
        self.known = set(self.items) | _fact_ids(f)
        self.passing = _passing(tk)
        self.fix_ids = {i for i, x in self.items.items() if x.get("kind") == "fix"}
        files = [x for x in (tk.get("files") or {}).values() if isinstance(x, dict)]
        self.good_paths = {str(x.get("path") or "").lower() for x in files if x.get("verified") is not False}
        self.bad_paths = {str(x.get("path") or "").lower() for x in files if x.get("verified") is False}
        corpus = [json.dumps(f, ensure_ascii=False)]
        for i in self.items.values():
            corpus += [i.get("title") or "", i.get("detail") or ""] + [str(e) for e in i.get("evidence") or []]
        for q in current_queries(tk):
            corpus += [q.get("text") or "", q.get("purpose") or ""]
        for k, a in (tk.get("apis") or {}).items():
            corpus += [k, str((a or {}).get("path") or "")] + [str(p) for p in (a or {}).get("paths") or []]
        for c in (tk.get("api_calls") or {}).values():
            corpus.append(str((c or {}).get("url") or ""))
        corpus += [k + " " + str((v or {}).get("name") or "") for k, v in (tk.get("tables") or {}).items()]
        self.corpus = "\n".join(corpus).lower()

    def _path_ok(self, p):
        p = p.lower().replace("\\", "/")
        def hit(paths):
            return any(x == p or x.endswith("/" + p) or p.endswith("/" + x) or x.rsplit("/", 1)[-1] == p for x in paths)
        if hit(self.bad_paths) and not hit(self.good_paths):
            return "unverified-path"
        if hit(self.good_paths) or (p.rsplit("/", 1)[-1] in self.corpus and not hit(self.bad_paths)):
            return None
        return "unverified-path" if "/" in p else "unknown-name"

    def names(self, text):
        """None when every path, table, endpoint and class the text names is known; else the reason to drop it."""
        paths = [m.group(0) for m in _PATH_RX.finditer(text)]
        for p in paths:
            why = self._path_ok(p)
            if why:
                return why
        rest = _PATH_RX.sub(" ", text)
        for m in _ENDPOINT_RX.finditer(rest):
            e = m.group(0).rstrip(".,;:)").lower()
            if len(e) > 2 and e not in self.corpus:
                return "unknown-name"
        for m in _DOTTED_RX.finditer(rest):
            t = m.group(0).lower()
            parts = t.split(".")
            if t in self.corpus or ".".join(parts[:2]) in self.corpus or ".".join(parts[-2:]) in self.corpus:
                continue
            return "unknown-name"
        for m in _CLASS_RX.finditer(rest):
            w = m.group(0).lower()
            if w not in self.corpus and w not in _COMMON_WORDS:
                return "unknown-name"
        return None

    def cite_problem(self, cites):
        if not cites:
            return "no-cite"
        for c in cites:
            if c in self.known:
                continue
            old = self.all_items.get(c)
            if isinstance(old, dict):
                return "superseded" if old.get("status") == "superseded" or old.get("archived") else "not-this-ticket"
            return "unknown-id"
        return None


def _norm_cites(cites, items=None):
    out = []
    for c in cites or []:
        c = str(c or "").strip()
        if c.startswith("item:") and c[5:] in (items or {}):
            c = c[5:]  # an exact current item, never a guessed or historical id
        if c.startswith("commit:") and len(c) > 14:
            c = c[:14]  # commit:<sha7>
        if c and c not in out:
            out.append(c)
    return out


def validate(out, key, tk, kind, card=None):
    """The model's answer -> (sections, dropped). Rules: docs/architecture.md "The ticket brief"."""
    ctx = _Ctx(key, tk, card)
    dropped = []

    def drop(section, text, why):
        dropped.append({"section": section, "text": common.truncate(str(text or ""), 200), "why": why})

    def claim(section, c, max_words=None):
        if not isinstance(c, dict):
            return None
        text = " ".join(str(c.get("text") or "").split())
        if not text:
            return None
        cites = _norm_cites(c.get("cites"), ctx.items)
        why = ctx.cite_problem(cites) or ctx.names(text)
        if not why and max_words and _words(text) > max_words:
            why = "too-long"
        if why:
            drop(section, text, why)
            return None
        return {"text": text, "cites": cites}

    def claims(section, lst, per=None, total=None, most=None):
        """Valid claims in order; a claim past `most` or past `total` words is dropped whole, never cut."""
        kept, words = [], 0
        for c in lst or []:
            k = claim(section, c, per)
            if not k:
                continue
            n = _words(k["text"])
            if most and len(kept) >= most:
                drop(section, k["text"], "too-many")
                continue
            if total and words + n > total:
                drop(section, k["text"], "word-limit" if kept else "too-long")
                continue
            if isinstance(c, dict) and "label" in c:
                k["label"] = c.get("label") or ""
            kept.append(k)
            words += _words(k["text"])
        return kept

    o = out if isinstance(out, dict) else {}
    sec = {}
    rc = o.get("root_cause") or {}
    rcl = claims("root_cause", rc.get("claims"), total=60)
    # confidence: what the model says, never more than the cited items support ("none": not found yet, the claims
    # are the leading hypothesis)
    support = [ctx.items[c] for k in rcl for c in k["cites"] if c in ctx.items]
    data_rank = max([CONF_RANK.get(i.get("status") if i.get("status") in CONF_RANK else i.get("confidence"), 1)
                     for i in support] or [CONF_RANK["likely"]])  # facts only (commits, tests, Jira): likely at most
    rank = min(CONF_RANK.get(rc.get("confidence"), 0), data_rank) if rcl else 0
    sec["root_cause"] = {"confidence": [k for k, v in CONF_RANK.items() if v == rank][0], "claims": rcl}

    fl = claims("findings", (o.get("findings") or {}).get("claims"), per=25, most=7)
    ruled, kept = 0, []
    for k in fl:
        if k.get("label") == "ruled-out" or k["text"].lower().startswith("ruled out"):
            ruled += 1
            if ruled > 1:
                drop("findings", k["text"], "second-ruled-out")
                continue
            k["label"] = "ruled-out"
        else:
            k["label"] = ""
        kept.append(k)
    sec["findings"] = {"claims": kept}
    sec["before"] = {"steps": claims("before", (o.get("before") or {}).get("steps"), total=80, most=6)}
    pr = o.get("proposed") or {}
    sec["proposed"] = {"claims": claims("proposed", pr.get("claims"), total=60),
                       "rejected": claim("proposed", pr.get("rejected"), 25)}
    summary = claim("fixes_made", (o.get("fixes_made") or {}).get("summary"), 40)
    if summary and not fixes_made(tk):
        drop("fixes_made", summary["text"], "no-fixes")
        summary = None
    sec["fixes_made"] = {"summary": summary}
    af = o.get("after") or {}
    ver = af.get("verified") if isinstance(af.get("verified"), dict) else {}
    vtext = claim("after", {"text": ver.get("text"), "cites": ver.get("cites")}, 40) if ver.get("text") else None
    vcites = _norm_cites(ver.get("cites"))
    proven = [c for c in vcites if c in ctx.passing]
    if not proven and "jira:" + key in vcites and \
            qa_verified(card.get("status") if card and card.get("status") else S.jira_status(tk)):
        proven = ["jira:" + key]  # no passing test or replay: QA's Jira status is the proof
    state = "verified" if ver.get("state") == "verified" and proven else "not-verified"
    sec["after"] = {"steps": claims("after", af.get("steps"), total=80, most=6),
                    "verified": {"state": state, "text": vtext["text"] if vtext else "",
                                 "cites": (proven if state == "verified" else (vtext or {}).get("cites", []))}}
    cons, n_sugg = [], 0
    for c in (o.get("cons") or {}).get("claims") or []:
        k = claim("cons", c, 20)
        if not k:
            continue
        label = "suggested" if (c.get("label") == "suggested" or set(k["cites"]) <= ctx.fix_ids) else "recorded"
        if label == "suggested":
            if not set(k["cites"]) & ctx.fix_ids:
                drop("cons", k["text"], "suggested-without-fix")
                continue
            if n_sugg >= 2:
                drop("cons", k["text"], "too-many-suggested")
                continue
            n_sugg += 1
        if len(cons) >= 5:
            drop("cons", k["text"], "too-many")
            continue
        k["label"] = label
        cons.append(k)
    sec["cons"] = {"claims": cons}
    _trim(sec, drop)
    return sec, dropped


def _all_texts(sec):
    out = []
    for s in sec.values():
        for k in ("claims", "steps"):
            out += [c["text"] for c in s.get(k) or []]
        for k in ("rejected", "summary"):
            if s.get(k):
                out.append(s[k]["text"])
        if s.get("verified"):
            out.append(s["verified"].get("text") or "")
    return out


def _trim(sec, drop):
    """Keep the whole brief near WORD_CAP: drop whole claims from the end (findings down to 3, then suggested cons,
    then steps down to 3), never cutting a sentence."""
    order = [("findings", "claims", 3), ("cons", "claims", 0), ("before", "steps", 3), ("after", "steps", 3)]
    for s, k, keep in order:
        lst = sec[s][k]
        while sum(_words(t) for t in _all_texts(sec)) > WORD_CAP and len(lst) > keep:
            if s == "cons" and not any(c.get("label") == "suggested" for c in lst):
                break
            idx = max(i for i, c in enumerate(lst) if s != "cons" or c.get("label") == "suggested")
            drop(s, lst.pop(idx)["text"], "brief-length")


# --------------------------------------------------------------------------- your corrections

_OVERRIDE_RX = [("root_cause", r"root[\s_-]*cause|rca|cause|why(?:\s+this\s+change)?"),
                ("proposed", r"proposed[\s_-]*fix|fix"),
                ("cons", r"possible[\s_-]*cons|cons?|risks?")]


def overrides(tk):
    """{section: (note item, text)} from your notes (inbox "KEY: root cause: …", "KEY: fix: …", "KEY: cons: …"):
    the newest note per section."""
    out = {}
    notes = sorted((i for i in current_items(tk) if i.get("kind") == "note" and (i.get("session") == "inbox" or i.get("src") == "user")),
                   key=lambda i: (i.get("date") or "", i.get("time") or ""))
    for n in notes:
        text = " ".join(str(n.get("detail") or n.get("title") or "").split())
        for sid, rx in _OVERRIDE_RX:
            m = re.match(r"^(?:correction\s*:\s*)?(?:%s)\s*[:–—-]\s*(.+)$" % rx, text, re.I)
            if m and m.group(1).strip():
                out[sid] = (n, m.group(1).strip())
                break
    return out


def apply_overrides(sec, tk):
    """Your notes replace the model's wording for their section. Returns {section: "note:<id>"}."""
    ov = {}
    for sid, (n, text) in overrides(tk).items():
        c = {"text": text, "cites": [n["id"]]}
        if sid == "root_cause":
            sec["root_cause"] = {"confidence": "confirmed", "claims": [c]}
        elif sid == "proposed":
            sec["proposed"] = {"claims": [c], "rejected": (sec.get("proposed") or {}).get("rejected")}
        elif sid == "cons":
            sec["cons"] = {"claims": [dict(c, label="recorded")]}
        ov[sid] = "note:" + n["id"]
    return ov


# --------------------------------------------------------------------------- generate

def generate(key, store=None, cfg=None, health=None, force=False):
    """Write tk["brief"] for one ticket when its inputs changed (or `force`), within brief.daily_cap and
    budget.model_daily_usd. -> {"ok": bool, "skipped"?: why, "dropped"?: n}. The caller saves the store."""
    from . import extract, modelbudget
    cfg = cfg if cfg is not None else common.load_config()
    health = health if health is not None else {}
    st = store or S.Store()
    if not S.TICKET_RX.match(key or "") or (key not in st.tickets and not os.path.exists(S.ticket_path(key))):
        return {"ok": False, "skipped": "no such ticket"}
    tk = st.ticket(key)
    kind = kind_of(tk, cfg)
    if kind == "epic":
        return {"ok": True, "epic": True, "skipped": "epic: a children summary, no brief"}
    from . import relevance
    relevance.classify(tk)
    card = jira_card(key)
    h = source_hash(key, tk, cfg, card)
    old = tk.get("brief") if isinstance(tk.get("brief"), dict) else None
    if old and old.get("source_hash") == h and not force:
        return {"ok": True, "skipped": "unchanged"}
    if not current_items(tk) and not tk.get("commits") and not tk.get("tests") and not card.get("found"):
        return {"ok": False, "skipped": "nothing recorded yet"}
    if modelbudget.over_budget(health, cfg, "brief"):
        return {"ok": False, "skipped": "budget"}
    try:
        out, cost = extract.call_claude("brief", build_input(key, tk, cfg, card), cfg)
    except extract.ExtractError as e:
        common.log("agent", "brief %s failed: %s" % (key, e))
        return {"ok": False, "skipped": "model call failed: %s" % str(e)[:200]}
    tin, tout, usd = modelbudget.last_call("brief")
    sec, dropped = validate(out, key, tk, kind, card)
    ov = apply_overrides(sec, tk)
    from . import relevance
    relevance.classify(tk)
    relevance_dropped = relevance.apply_picks(tk, (out or {}).get("relevance"))
    tk["brief"] = {"v": V, "kind": kind, "generated_at": common.now_iso(), "model": extract.model_for("brief", cfg),
                   "tokens": {"in": tin, "out": tout}, "cost_usd": round(float(cost or usd or 0), 4), "source_hash": h,
                   "sections": sec, "overrides": ov, "dropped": dropped, "relevance_dropped": relevance_dropped}
    from . import summary
    tk["brief"]["summary"], tk["brief"]["summary_dropped"] = summary.build(tk,key,cfg,sec,(out or {}).get("summary"),card)
    st.touch_ticket(key)
    if dropped:
        common.log("agent", "brief %s: %d claims dropped (%s)" % (
            key, len(dropped), ", ".join(sorted({"%s %s" % (d["section"], d["why"]) for d in dropped}))))
    common.log("agent", "brief %s written (%d in / %d out tokens, $%.4f)" % (key, tin, tout, float(cost or 0)))
    return {"ok": True, "dropped": len(dropped)}


def refresh(key, wait_s=None, force=False):
    """Regenerate one ticket's brief now (`/cg:ticket <KEY> --brief`, the dashboard's brief-refresh), under the agent
    lock like a tick, then re-render its hub. Same rules as day close: no call when the inputs are unchanged, within the
    cap and the budget. -> generate()'s result plus "view"."""
    from . import agent as agentmod
    from . import render
    lock = agentmod.Lock()
    if not lock.acquire_wait(wait_s):
        return {"ok": False, "skipped": "the agent is busy; try again in a minute"}
    a = None
    try:
        a = agentmod.Agent()
        res = generate(key, store=a.store, cfg=a.cfg, health=a.health, force=force)
        if res.get("skipped") == "no such ticket":
            return res
        if res.get("ok") and not res.get("skipped"):
            a.store.save()
            render.render_all(a.store, a.cfg, a.health, tickets={key})
        res["view"] = view(a.store.ticket(key), key, a.cfg)
        return res
    finally:
        if a:
            try:
                agentmod.save_state(a)
            except Exception:  # noqa: BLE001 - the brief is saved; health is best effort here
                common.log("agent", "state save failed after brief %s" % key)
        lock.release()


# --------------------------------------------------------------------------- the view

def _header(key, tk, b=None):
    st = _stage(tk)
    h = {"key": key, "title": S.display_title(tk), "title_hint": tk.get("title_hint") or "",
            "jira_type": (tk.get("jira") or {}).get("type") or "", "jira_status": S.jira_status(tk),
            "status_inferred": tk.get("status_inferred") or "",
            "stage": {"kind": st["kind"], "text": st["text"]} if st else None,
            "repos": list(tk.get("repos") or []), "branches": list(tk.get("branches") or []),
            "prs": [{"number": p["number"], "target": p.get("target") or "", "state": p.get("state") or ""} for p in _prs(tk)],
            "updated": (b or {}).get("generated_at") or tk.get("last") or ""}
    # the one repo / branch / PR the header line names: the ticket's own (its key in the branch name; another
    # ticket's hotfix branch that carried the fix stays in the lists only), an open PR, else a merged one, else any, the newest
    own = re.compile(r"(?<![A-Z0-9])%s(?![0-9])" % re.escape(key.upper()))
    h["repo"] = (h["repos"] or [""])[0]
    h["branch"] = ([x for x in h["branches"] if own.search(x.upper())] or [""])[-1]
    mine = [p for p, raw in zip(h["prs"], _prs(tk)) if not raw.get("source") or own.search(raw["source"].upper())]
    opn = [p for p in mine if p["state"] == "open"]
    merged = [p for p in mine if p["state"] == "merged"]
    h["pr"] = (opn or merged or mine or [None])[-1]
    return h


def _fallback(tk):
    from . import render
    rcs = [i for i in current_items(tk) if i.get("kind") == "root_cause"]
    rcs.sort(key=lambda i: (i.get("status") == "confirmed", CONF_RANK.get(i.get("confidence"), 0),
                            i.get("date") or "", i.get("time") or ""), reverse=True)
    st = _stage(tk)
    rc = rcs[0] if rcs else None
    return {"root_cause": {"id": rc["id"], "title": rc.get("title") or "", "text": rc.get("title") or "",
                           "confidence": rc.get("confidence") or "", "cites": [rc["id"]]} if rc else None,
            "stage": (st or {}).get("text") or "",
            "tldr": render.tldr_text(tk, st, md=False) if tk.get("tldr") else "",
            "problem": tk.get("problem") or ""}


def _evidence(tk, cited, fx):
    out = {}
    items = tk.get("items") or {}
    labels = {x["id"]: x["text"] for x in fx}
    for t in (tk.get("tests") or {}).values():
        if isinstance(t, dict) and t.get("id"):
            labels["test:" + t["id"]] = "%s (%s)" % (t.get("command") or "test", t.get("status") or "")
    for c in cited:
        if c in items:
            i = items[c]
            out[c] = {"kind": i.get("kind"), "title": i.get("title") or "", "date": i.get("date") or "",
                      "ref": i.get("ref")}
        else:
            out[c] = {"kind": c.split(":", 1)[0] if ":" in c else "fact", "title": labels.get(c, c)}
    return out


_LABEL_WORDS = {"ruled-out": r"ruled[\s-]*out", "recorded": "recorded", "suggested": "suggested",
                "rejected": "rejected", "verified": r"(?:not\s+)?verified(?:\s+yet)?"}


def strip_label(text, label):
    """A claim's text without a leading "<Label>:" that its label already shows ("Ruled out: Ruled out: x" once)."""
    rx = _LABEL_WORDS.get(label or "")
    text = str(text or "")
    if not rx:
        return text
    out = re.sub(r"^\s*(?:\*\*)?(?:%s)\s*(?:\*\*)?\s*[:–—-]\s*(?:\*\*)?\s*" % rx, "", text, count=1, flags=re.I)
    return out[:1].upper() + out[1:] if out and out != text else (out or text)


def _fix_rows(fx):
    """The "Fixes made" list as the renderers show it: one row per commit and PR (cited by its own id), then the stage."""
    rows = []
    for x in fx:
        if x["kind"] == "commit":
            text = "%s %s%s" % (x["id"].split(":", 1)[1], x.get("text") or "", " (%s)" % x["date"] if x.get("date") else "")
        elif x["kind"] == "pr":
            st = " ".join(y for y in (x.get("state") or "", x.get("date") or "") if y)
            text = x.get("text") or x["id"] + (" (%s)" % st if st else "")
        else:
            text = "Reached: %s" % (x.get("text") or "")
        rows.append({"text": text.strip(), "cites": [x["id"]] if x["kind"] in ("commit", "pr") else []})
    return rows


def view(tk, key=None, cfg=None):
    """Everything the brief surfaces render, ready to show (D2 renders only from this):
    {"key", "kind", "epic": False, "header", "has_brief", "stale", "generated_at", "model", "tokens", "cost_usd",
     "sections": [{"id", "heading", "claims", "empty", "text" ("Not recorded yet" | "Not found yet" | "Not started" |
                   None), "override"?, plus "confidence" (root_cause), "rejected" (proposed), "items" + "summary"
                   (fixes_made), "steps" + "verified" (before / after)}],
     "fixes_made", "fallback" (when there is no brief), "evidence" {cited id: {kind, title, …}}, "dropped": n}
    An Epic: {"key", "kind": "epic", "epic": True, "header", "children": [{key, title, status, line}]}."""
    key = key or tk.get("id")
    cfg = cfg if cfg is not None else common.load_config()
    kind = kind_of(tk, cfg)
    b = tk.get("brief") if isinstance(tk.get("brief"), dict) else None
    if kind == "epic":
        return {"key": key, "kind": "epic", "epic": True, "header": _header(key, tk, b), "children": children(key)}
    sec = json.loads(json.dumps((b or {}).get("sections") or {}))
    current = {i["id"] for i in current_items(tk)}

    def still(c):  # an item cited when the brief was written may have been superseded since: never shown
        return all(x in current or ":" in x for x in c.get("cites") or [])
    for s in sec.values():
        for k in ("claims", "steps"):
            if isinstance(s.get(k), list):
                s[k] = [c for c in s[k] if still(c)]
        for k in ("rejected", "summary"):
            if isinstance(s.get(k), dict) and not still(s[k]):
                s[k] = None
    ov = apply_overrides(sec, tk)
    for s in sec.values():  # a label the renderers add is never printed twice ("Ruled out: Ruled out: …")
        for c in s.get("claims") or []:
            if isinstance(c, dict) and c.get("label"):
                c["text"] = strip_label(c.get("text"), c["label"])
        if isinstance(s.get("rejected"), dict):
            s["rejected"]["text"] = strip_label(s["rejected"].get("text"), "rejected")
        if isinstance(s.get("verified"), dict):
            s["verified"]["text"] = strip_label(s["verified"].get("text"), "verified")
    fx = fixes_made(tk)
    out_secs, cited = [], []
    for sid in SECTIONS:
        s = sec.get(sid) or {}
        d = {"id": sid, "heading": WHY if (sid == "root_cause" and kind == "story") else HEADINGS[sid],
             "claims": [], "empty": True, "text": NOT_RECORDED}
        if sid in ov:
            d["override"] = ov[sid]
        if sid == "root_cause":
            d["claims"] = s.get("claims") or []
            d["confidence"] = s.get("confidence") or "none"
            if d["confidence"] == "none" and kind != "story":
                d["text"], d["empty"] = "Not found yet", True
                d["leading_hypothesis"] = bool(d["claims"])
            else:
                d["empty"] = not d["claims"]
                d["text"] = None if d["claims"] else NOT_RECORDED
        elif sid == "fixes_made":
            d["items"] = fx
            d["list"] = _fix_rows(fx)  # what the renderers print, each row with its evidence id
            d["summary"] = s.get("summary") if fx and s.get("summary") else None
            d["empty"] = not fx
            d["text"] = None if fx else "Not started"
        elif sid in ("before", "after"):
            d["steps"] = s.get("steps") or []  # steps only: a renderer that read them as claims too printed each twice
            d["empty"] = not d["steps"]
            d["text"] = None if d["steps"] else NOT_RECORDED
            if sid == "after":
                v = dict(s.get("verified") or {"state": "not-verified", "text": "", "cites": []})
                js = S.jira_status(tk)
                proof = [c for c in v.get("cites") or [] if c in _passing(tk)]
                by_test = v.get("state") == "verified" and bool(proof)
                if by_test:
                    v["label"], v["cites"] = "Verified", proof
                elif qa_verified(js, cfg):
                    v = {"state": "verified", "label": "Verified by QA (Jira: %s)" % js, "cites": ["jira:" + key],
                         "text": v.get("text") if v.get("state") == "verified" else ""}
                else:
                    if v.get("state") == "verified":
                        v["text"], v["cites"] = "", []
                    v["state"], v["label"] = "not-verified", "Not verified yet"
                d["verified"] = v
                cited += v.get("cites") or []
        else:
            d["claims"] = s.get("claims") or []
            d["empty"] = not d["claims"]
            d["text"] = None if d["claims"] else NOT_RECORDED
            if sid == "proposed":
                d["rejected"] = s.get("rejected")
                if d["rejected"]:
                    cited += d["rejected"].get("cites") or []
        for c in d["claims"] + d.get("steps", []) + ([d["summary"]] if d.get("summary") else []):
            cited += c.get("cites") or []
        out_secs.append(d)
    view_ = {"key": key, "kind": kind, "epic": False, "header": _header(key, tk, b), "has_brief": bool(b),
             "stale": stale(tk, key, cfg) if b else None, "sections": out_secs, "fixes_made": fx,
             "evidence": _evidence(tk, list(dict.fromkeys(cited)), fx), "dropped": len((b or {}).get("dropped") or [])}
    for k in ("generated_at", "model", "tokens", "cost_usd"):
        view_[k] = (b or {}).get(k)
    if not b:
        view_["fallback"] = _fallback(tk)
    from . import summary
    view_["summary"] = summary.view(tk,key,cfg,sections=sec)
    summary_cites = [cid for s in view_["summary"]["sections"] for c in s["claims"] for cid in c["cites"]]
    view_["evidence"].update(_evidence(tk,list(dict.fromkeys(summary_cites)),fx))
    return view_


# --------------------------------------------------------------------------- Epics

_KEY_AT_START = re.compile(r"^\s*([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")


def _first_sentence(s):
    s = " ".join(str(s or "").split())
    m = re.match(r"^(.+?[.!?])(?:\s|$)", s)
    return common.truncate(m.group(1) if m else s, 200)


def child_line(tk):
    """A child's one line: your root-cause note, else its brief's root cause (or why, for a story), else its current
    root cause, else its goal (the first sentence of the problem or TL;DR)."""
    ov = overrides(tk).get("root_cause")
    if ov:
        return ov[1]
    b = tk.get("brief") if isinstance(tk.get("brief"), dict) else {}
    items = current_items(tk)
    cur = {i["id"] for i in items}
    for c in ((b.get("sections") or {}).get("root_cause") or {}).get("claims") or []:
        if all(x in cur or ":" in x for x in c.get("cites") or []):
            return c["text"]
    if any(i.get("kind") == "root_cause" for i in items):
        return _fallback(tk)["root_cause"]["title"]
    return _first_sentence(tk.get("problem") or tk.get("tldr") or "")


def children(key):
    """An Epic's children: tickets whose Jira card names it as parent, or that a pasted breadcrumb put under it
    (tk.crumb_parents). [{key, title, status, line}], by key."""
    out = []
    for t in S.Store.all_ticket_ids():
        if t == key:
            continue
        tk = common.read_json(S.ticket_path(t), None)
        if not isinstance(tk, dict):
            continue
        par = str((tk.get("jira") or {}).get("parent") or "") if isinstance(tk.get("jira"), dict) else ""
        m = _KEY_AT_START.match(par.upper())
        if not ((m and m.group(1) == key) or key in (tk.get("crumb_parents") or [])):
            continue
        from . import summary
        sv = summary.view(tk,t)
        out.append({"key": t, "title": S.display_title(tk) or tk.get("title_hint") or "",
                    "status": S.jira_status(tk) or tk.get("status_inferred") or "", "line": child_line(tk),
                    "summary":sv,"summary_line":summary.child_line(sv)})
    return out


# --------------------------------------------------------------------------- plain text (CLI)

def _cite(c):
    return " [%s]" % ", ".join(c.get("cites") or []) if c.get("cites") else ""


_TEXT_LABEL = {"ruled-out": "Ruled out", "recorded": "Recorded", "suggested": "Suggested"}


def text_lines(v):
    """A view as plain text, for `/cg:ticket <KEY> --brief`."""
    h = v.get("header") or {}
    L = ["# %s%s" % (v.get("key"), (" - " + h["title"]) if h.get("title") else "")]
    status = " · ".join(x for x in ("Jira: " + h["jira_status"] if h.get("jira_status") else "",
                                    "Inferred: " + h["status_inferred"] if h.get("status_inferred") else "",
                                    (h.get("stage") or {}).get("text") or "") if x)
    if status:
        L.append(status)
    if v.get("epic"):
        L += ["", "Epic: no brief. Its children:"]
        L += ["- %s %s · %s · %s" % (c["key"], c.get("title") or "", c.get("status") or "-", c.get("summary_line") or c.get("line") or "")
              for c in v.get("children") or []] or ["- none recorded"]
        return L
    if not v.get("has_brief"):
        fb = v.get("fallback") or {}
        L += ["", "No brief yet."]
        if (fb.get("root_cause") or {}).get("title"):
            L.append("Current root cause: %s [%s]" % (fb["root_cause"]["title"], fb["root_cause"]["id"]))
        if fb.get("tldr"):
            L.append("TL;DR: " + fb["tldr"])
        return L
    if v.get("stale"):
        L.append("New findings since %s." % (v["stale"].get("since") or "the last brief")[:16])
    L.append("Written %s by %s." % ((v.get("generated_at") or "")[:16], v.get("model") or "?"))
    for s in v.get("sections") or []:
        head = s["heading"] + (" (%s)" % s["confidence"] if s["id"] == "root_cause" and s.get("confidence") not in
                               (None, "none") else "")
        L += ["", "## " + head]
        if s.get("override"):
            L.append("(your note: %s)" % s["override"])
        if s["id"] == "fixes_made":
            L += [("- Reached: %s" % x["text"]) if x["kind"] == "stage" else
                  "- %s %s%s" % (x["id"], x["text"], " (%s%s)" % ((x.get("state") + " ") if x.get("state") else "", x["date"])
                                 if x.get("date") else "") for x in s["items"]]
            if s.get("summary"):
                L.append(s["summary"]["text"] + _cite(s["summary"]))
        if s.get("text"):
            L.append(s["text"] + (": leading hypothesis" if s.get("leading_hypothesis") else ""))
        for n, c in enumerate(s.get("steps") or [], 1):
            L.append("%d. " % n + c["text"] + _cite(c))
        for c in s.get("claims") or []:
            lab = "[%s] " % _TEXT_LABEL.get(c.get("label"), c.get("label")) if c.get("label") else ""
            L.append("- " + lab + c["text"] + _cite(c))
        if s.get("rejected"):
            L.append("Rejected: " + s["rejected"]["text"] + _cite(s["rejected"]))
        if s.get("verified"):
            ver = s["verified"]
            L.append("%s%s%s" % (ver.get("label"), ": " + ver["text"] if ver.get("text") else "", _cite(ver)))
    if v.get("dropped"):
        L += ["", "(%d unsupported claim%s left out)" % (v["dropped"], "" if v["dropped"] == 1 else "s")]
    return L
