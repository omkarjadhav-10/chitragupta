"""Canonical data under <home>/data: tickets/<T>.json, days/<date>.json, catalog.json.

Markdown is always rendered from here and never parsed back. Every write is atomic.
IDs are stable hashes, so merging the same extraction twice is a no-op.
"""
import json
import os
import re

from . import common
from . import kbnorm
from . import offticket
from . import sqlsent
from .redact import redact_obj

ITEM_KINDS = ["finding", "root_cause", "how_it_works", "fix", "decision", "config", "error",
              "repro_test", "reference", "todo", "question", "blocker", "note", "risk"]
PHASES = ("before", "after", "both")  # how_it_works items: before the fix, after it, unchanged by it
TICKET_RX = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,6}$")


def ticket_path(t):
    return common.hp("data", "tickets", t + ".json")


def day_path(d):
    return common.hp("data", "days", d[:4], d + ".json")


def catalog_path():
    return common.hp("data", "catalog.json")


def branches_path():
    return common.hp("data", "branches.json")


def new_ticket(t):
    return {"id": t, "title": "", "title_source": "", "title_hint": "", "repos": [], "branches": [], "first": "", "last": "",
            "status": "", "status_inferred": "", "tldr": "", "tldr_date": "", "problem": "", "items": {}, "queries": {},
            "tables": {}, "apis": {}, "integrations": {}, "files": {}, "commits": {}, "timeline": {},
            "bruno": {}, "api_calls": {}, "replay": {}, "data_flow": {}, "table_relations": {}, "dbflow_conflicts": [],
            "artifacts": {}, "tests": {}}


def new_day(d):
    return {"date": d, "updated_at": "", "closed": False, "closed_at": "", "sessions": {}, "activity": [],
            "tickets": {}, "other": {}, "fallback": {}, "outside": {"bruno": [], "sql": [], "inbox": []},
            "commits": [], "branch_activity": [], "summary": "", "summary_final": False, "standup": {}, "open_todos": [],
            "carried_todos": [], "consolidated": {}, "stats": {"cost_usd": 0.0, "errors": 0}, "unfiled_artifacts": []}


class Store:
    """Loads lazily, tracks dirty objects, saves atomically."""

    def __init__(self):
        self.tickets, self.days, self._catalog, self._branches = {}, {}, None, None
        self.dirty_tickets, self.dirty_days, self.catalog_dirty, self.branches_dirty = set(), set(), False, False
        self.delta = {}  # ticket-or-group -> {"findings": n, "queries": n, "apis": n}

    # ---- access
    def ticket(self, t):
        if t not in self.tickets:
            data = common.read_json(ticket_path(t), None)
            self.tickets[t] = data if data else new_ticket(t)
            for k, v in new_ticket(t).items():
                self.tickets[t].setdefault(k, v)
        return self.tickets[t]

    def day(self, d):
        if d not in self.days:
            data = common.read_json(day_path(d), None)
            self.days[d] = data if data else new_day(d)
            for k, v in new_day(d).items():
                self.days[d].setdefault(k, v)
        return self.days[d]

    def catalog(self):
        if self._catalog is None:
            self._catalog = common.read_json(catalog_path(), None) or {"tables": {}, "apis": {}, "integrations": {}}
        return self._catalog

    def branches(self):
        """{repo_name: {branch: record}} from data/git/<repo>/branches.json (branch tracking, schema 4).
        Before the migration has run, the single legacy data/branches.json."""
        if self._branches is None:
            root = common.hp("data", "git")
            out = {}
            if os.path.isdir(root):
                for name in sorted(os.listdir(root)):
                    recs = common.read_json(os.path.join(root, name, "branches.json"), None)
                    if isinstance(recs, dict):
                        out[name] = recs
                self._branches = out
            else:
                self._branches = common.read_json(branches_path(), None) or {}
        return self._branches

    def reload_branches(self):
        self._branches = None

    def touch_ticket(self, t):
        self.dirty_tickets.add(t)

    def touch_day(self, d):
        self.dirty_days.add(d)
        self.day(d)["updated_at"] = common.now_iso()

    def bump(self, key, what, n=1):
        self.delta.setdefault(key, {}).setdefault(what, 0)
        self.delta[key][what] += n

    def save(self):
        for t in self.dirty_tickets:
            self.tickets[t]["status"] = status_of(self.tickets[t])  # computed, for readers of the old single field
            common.write_json(ticket_path(t), redact_obj(self.tickets[t]))
        for d in self.dirty_days:
            common.write_json(day_path(d), redact_obj(self.days[d]))
        if self.catalog_dirty and self._catalog is not None:
            common.write_json(catalog_path(), redact_obj(self._catalog))
        # branch records are written by gittrack (data/git/<repo>/); branches_dirty only asks for a re-render

    # ---- listing
    @staticmethod
    def all_ticket_ids():
        d = common.hp("data", "tickets")
        try:
            return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))
        except OSError:
            return []

    @staticmethod
    def all_days():
        root = common.hp("data", "days")
        out = []
        for dirpath, _, files in os.walk(root):
            out += [f[:-5] for f in files if f.endswith(".json")]
        return sorted(out)


# --------------------------------------------------------------------------- merge helpers

def _union(a, b):
    out = list(a or [])
    for x in b or []:
        if x and x not in out:
            out.append(x)
    return out


TITLE_SOURCES = ("jira", "user")  # the only sources of a ticket's title; anything else is a hint


def display_title(tk):
    """The ticket's title from its Jira card or the user; "" until one exists (show the key instead)."""
    return (tk.get("title") or "") if tk.get("title_source") in TITLE_SOURCES else ""


def jira_status(tk):
    return ((tk.get("jira") or {}).get("status") or "") if isinstance(tk.get("jira"), dict) else ""


def status_of(tk):
    """The single `status` older readers expect: the Jira status when a card exists, else the inferred one."""
    return jira_status(tk) or tk.get("status_inferred") or ""


def group_key(repo, branch):
    return "%s · %s" % (os.path.basename((repo or "").rstrip("/\\")) or "no-repo", branch or "-")


def valid_ticket(t, known):
    if not t or not isinstance(t, str):
        return None
    t = t.strip().upper()
    if not TICKET_RX.match(t):
        return None
    if known is not None and t.split("-")[0] not in known:
        return None
    if common.is_placeholder(t):  # a configured example key, e.g. from a hint line the model read
        return None
    return t


def touch_ticket_meta(store, t, day, repo=None, branch=None):
    tk = store.ticket(t)
    if repo:
        tk["repos"] = _union(tk["repos"], [os.path.basename(repo.rstrip("/\\"))])
    # a branch belongs to the ticket it names: a turn about another ticket on this branch doesn't take it
    if branch and not branch.startswith("detached@") and common.ticket_from_branch(branch, [t.split("-")[0]]) == t:
        tk["branches"] = _union(tk["branches"], [branch])
        if not tk.get("title_hint"):
            slug = re.sub(r"^.*?" + re.escape(t) + r"[-_/]?", "", branch, flags=re.I)
            slug = re.sub(r"[-_/]+", " ", slug).strip()
            if slug:  # a hint only: the title comes from the Jira card or the user
                tk["title_hint"], tk["title_hint_source"] = slug[:90], "branch"
    if not tk["first"] or day < tk["first"]:
        tk["first"] = day
    if not tk["last"] or day > tk["last"]:
        tk["last"] = day
    store.touch_ticket(t)
    return tk


_HHMM = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")


def hhmm_of(s):
    """"HH:MM" of an item's time as the model wrote it ("10:35", "2026-09-01 10:35", "10:35:12"), or ""."""
    m = _HHMM.search(str(s or ""))
    return "%02d:%s" % (int(m.group(1)), m.group(2)) if m and int(m.group(1)) < 24 else ""


def _turn_at(ctx, hhmm):
    """The chunk's turn (hhmm, ticket, src[, line]) an item at `hhmm` happened in: the last one started by then."""
    hit = None
    for tu in ctx.get("turns") or []:
        if tu and hhmm and tu[0] and tu[0] <= hhmm:
            hit = tu
    return hit


def turn_line(ctx, hhmm):
    """The raw line of the turn an item at `hhmm` happened in (its prompt line), or None."""
    tu = _turn_at(ctx, hhmm_of(hhmm))
    return tu[3] if tu and len(tu) > 3 and tu[3] else None


def turn_of(ctx, hhmm=None):
    """(ticket, src) an extracted item falls back to when the model gave it no ticket: the turn it happened in (the
    chunk's `turns`, by the item's HH:MM time), else the chunk's dominant turn (`default`). Without turns (older
    callers) the session-day's only candidate, as before."""
    turns = [t for t in ctx.get("turns") or [] if t]
    if turns:
        hit = _turn_at(ctx, hhmm_of(hhmm) if hhmm else None)
        if hit and hit[1]:
            return hit[1], hit[2]
        d = ctx.get("default")
        return (d, next((tu[2] for tu in turns if tu[1] == d), None)) if d else (None, None)
    if "default" in ctx:
        return ctx.get("default"), None
    c = ctx.get("candidates") or []
    return (c[0], None) if len(c) == 1 else (None, None)


def merge_extraction(store, res, ctx, prefixes, only=None):
    """Merge one validated extraction result. ctx: day, session, time, repo, branch, candidates. `only` (a rebuild of
    some tickets): anything filed to another ticket, or to none, is left out and counted in ctx["ignored"]
    ({ticket or "-": n})."""
    day, sid = ctx["day"], ctx["session"]
    known = {p.upper() for p in prefixes}
    dd = store.day(day)
    default_t = turn_of(ctx)[0]
    gkey = group_key(ctx.get("repo"), ctx.get("branch"))

    cfg = common.load_config()

    def resolve(t):
        t = valid_ticket(t, known) if t else None
        return None if t and common.is_parent_ticket(t, cfg) else t  # a parent issue never gets items

    def origin(row, sent=None):
        line = row.get("line") if type(row.get("line")) is int else None
        when = (sent or {}).get("time") or (ctx.get("line_times") or {}).get(line) or hhmm_of(row.get("time")) or ctx.get("time", "")
        tt, tsrc = turn_of(ctx, when)
        explicit = resolve(row.get("ticket"))
        ticket = explicit or tt
        meta = {"time": when, "src": (tsrc if ticket == tt else "model") or "model"}
        ref = (sent or {}).get("ref") or raw_ref(day, sid, line) or raw_ref(day, sid, turn_line(ctx, when))
        if ref:
            meta["ref"] = ref
        return ticket, meta

    record_profiles = {}
    def record_profile(t):
        if t not in record_profiles:
            record_profiles[t] = offticket.record_profile(store.ticket(t),t)
        return record_profiles[t]

    touched = set()

    def skip(t):
        if only is None or t in only:
            return False
        ign = ctx.setdefault("ignored", {})
        ign[t or "-"] = ign.get(t or "-", 0) + 1
        return True

    def tslot(t):
        if t:
            touched.add(t)
            touch_ticket_meta(store, t, day, ctx.get("repo"), ctx.get("branch"))
            return dd["tickets"].setdefault(t, {"minutes": 0, "item_ids": [], "query_ids": [], "api_keys": [],
                                                 "table_keys": [], "integration_keys": [], "files_changed": [],
                                                 "what_i_did": []})
        return dd["other"].setdefault(gkey, {"minutes": 0, "items": {}, "queries": {}, "apis": {}, "tables": {},
                                             "integrations": {}, "files_changed": [], "what_i_did": []})

    # ticket titles
    for link in res.get("ticket_links") or []:
        t = resolve(link.get("ticket"))
        if t and link.get("title_hint") and (only is None or t in only):
            tk = touch_ticket_meta(store, t, day, ctx.get("repo"), ctx.get("branch"))
            if tk.get("title_hint_source") in (None, "", "branch"):
                tk["title_hint"], tk["title_hint_source"] = link["title_hint"][:90], "model"

    # activity line (what I did)
    summ = (res.get("activity_summary") or "").strip()
    if summ:
        t = default_t or (resolve((res.get("ticket_links") or [{}])[0].get("ticket")) if res.get("ticket_links") else None)
        entry = {"time": ctx.get("time", ""), "text": summ, "session": sid[:8]}
        slot = tslot(t) if only is None or t in only else None
        if slot is not None and entry["text"] not in [w["text"] for w in slot["what_i_did"]]:
            slot["what_i_did"].append(entry)
        if slot is not None and summ not in [a["text"] for a in dd["activity"]]:
            dd["activity"].append({"time": entry["time"], "ticket": t, "group": None if t else gkey, "text": summ})

    for it in res.get("items") or []:
        kind = it.get("kind") if it.get("kind") in ITEM_KINDS else "finding"
        title = (it.get("title") or "").strip()[:90]
        if not title:
            continue
        tt, tsrc = turn_of(ctx, it.get("time"))
        t = resolve(it.get("ticket"))
        src = (tsrc if t == tt else "model") if t else tsrc
        t = t or tt
        if skip(t):
            continue
        rec = {"kind": kind, "title": title, "detail": (it.get("detail") or "").strip(),
               "evidence": [e for e in (it.get("evidence") or []) if e][:8],
               "confidence": it.get("confidence") or "likely", "date": day,
               "time": hhmm_of(it.get("time")) or it.get("time") or ctx.get("time", ""), "session": sid[:8]}
        if t and src:
            rec["src"] = src  # where the ticket came from: prompt / jira-title / branch / carry, or model
        if kind == "fix":
            rec["state"] = "proposed"  # "applied" once an edit or a commit touches a file it names (mark_applied_fixes)
        if kind == "how_it_works" and it.get("phase") in PHASES:
            rec["phase"] = it["phase"]
        # the raw line the model named, else the line of the turn the item happened in (its prompt line)
        ref = raw_ref(day, sid, it.get("line")) or raw_ref(day, sid, turn_line(ctx, it.get("time")))
        if ref:
            rec["ref"] = ref
        if t:  # K8: maybe another ticket's (or the worklog tool's own) note: flagged for review, never moved
            rv = offticket.check(store.ticket(t), t, rec, prefixes)
            if rv:
                rec["review"] = rv
        _merge_item(store, dd, t, gkey, rec, it.get("updates"), tslot)

    for q in res.get("queries") or []:
        text = (q.get("text") or "").strip()
        if not text:
            continue
        hit = sqlsent.find(text, ctx.get("sent") or [], line=q.get("line"))
        t, provenance = origin(q, hit)
        if skip(t):
            continue
        qid = query_id(t or gkey, text)
        rec = {"id": qid, "date": day, "time": ctx.get("time", ""), "dialect": q.get("dialect") or "other",
               "text": text, "purpose": q.get("purpose") or "", "tables": q.get("tables") or [],
               "kind": q.get("kind") or "read", "revealed": q.get("revealed") or "", "days": [day]}
        rec.update(provenance)
        if t:
            rv = offticket.record_check(store.ticket(t), t, "queries", qid, rec, prefixes, prepared=record_profile(t))
            if rv:
                rec["review"] = rv
        slot = tslot(t)
        qs = store.ticket(t)["queries"] if t else slot["queries"]
        if "sent" in ctx:  # K6b: only SQL a database MCP call or a SQL command line sent counts as run
            rec["sent"] = bool(hit)
            if hit and hit.get("ref"):
                rec["ref"] = hit["ref"]
        if qid not in qs:
            qs[qid] = rec
            store.bump(t or gkey, "queries")
        else:
            add_query_sighting(qs[qid], rec)
            if rec.get("review") and not qs[qid].get("review_kept"):
                qs[qid].setdefault("review", rec["review"])
        if t and qid not in slot["query_ids"]:
            slot["query_ids"].append(qid)

    cat = store.catalog()
    for tb in res.get("tables") or []:
        name = (tb.get("name") or "").strip()
        if not name:
            continue
        t, provenance = origin(tb)
        if skip(t):
            continue
        slot = tslot(t)
        own = store.ticket(t)["tables"] if t else slot["tables"]
        key = kbnorm.table_key(name, [k for k in own if "." in k])
        if "." not in key:
            key = kbnorm.table_key(name, [k for k in cat["tables"] if "." in k])
        rec = {"name": name, "db": tb.get("db") or "", "purpose": tb.get("purpose") or "",
               "key_columns": tb.get("key_columns") or [], "relations": tb.get("relations") or [], "last": day}
        rec.update(provenance)
        _merge_catalog(cat["tables"], key, rec, t, day)
        fold_bare(cat["tables"], ".", table_name)
        store.catalog_dirty = True
        if t:
            rv = offticket.record_check(store.ticket(t), t, "tables", key, rec, prefixes, prepared=record_profile(t))
            if rv:
                rec["review"] = rv
        own[key] = _merge_rec(own.get(key), rec)
        folded = fold_bare(own, ".", table_name)
        if t:
            slot["table_keys"] = _union([folded.get(k, k) for k in slot["table_keys"]], [key])

    for a in res.get("apis") or []:
        path = (a.get("path") or "").strip()
        if not path:
            continue
        method = (a.get("method") or "").upper().strip() or "ANY"
        t, provenance = origin(a)
        if skip(t):
            continue
        key = kbnorm.api_key(method, path)
        host = (a.get("host_or_service") or "").strip()
        rec = {"method": method, "path": path, "host_or_service": host,
               "direction": a.get("direction") or "", "purpose": a.get("purpose") or "",
               "request_fields": a.get("request_fields") or [], "response_fields": a.get("response_fields") or [],
               "auth_type": a.get("auth_type") or "", "hosts": [host] if host else [], "paths": [path], "last": day}
        rec.update(provenance)
        _merge_catalog(cat["apis"], key, rec, t, day)
        store.catalog_dirty = True
        slot = tslot(t)
        if t:
            tk = store.ticket(t)
            if key not in tk["apis"]:
                store.bump(t, "APIs")
            rv = offticket.record_check(tk, t, "apis", key, rec, prefixes, prepared=record_profile(t))
            if rv:
                rec["review"] = rv
            tk["apis"][key] = _merge_rec(tk["apis"].get(key), rec)
            if key not in slot["api_keys"]:
                slot["api_keys"].append(key)
        else:
            slot["apis"][key] = rec

    for ig in res.get("integrations") or []:
        art = (ig.get("artifact") or "").strip()
        if not art:
            continue
        t = resolve(ig.get("ticket")) or default_t
        if skip(t):
            continue
        key = "%s|%s" % ((ig.get("system") or "other").lower(), art.lower())
        rec = {"system": ig.get("system") or "other", "artifact": art, "notes": ig.get("notes") or "", "last": day}
        _merge_catalog(cat["integrations"], key, rec, t, day)
        store.catalog_dirty = True
        slot = tslot(t)
        if t:
            tk = store.ticket(t)
            tk["integrations"][key] = _merge_rec(tk["integrations"].get(key), rec)
            if key not in slot["integration_keys"]:
                slot["integration_keys"].append(key)
        else:
            slot["integrations"][key] = rec

    repo = os.path.basename((ctx.get("repo") or "").rstrip("/\\"))
    edited = [kbnorm.file_key(x, ctx.get("repo"), [repo]) for x in ctx.get("edited") or []]
    for fl in res.get("files") or []:
        p = (fl.get("path") or "").strip()
        if not p:
            continue
        t, provenance = origin(fl)
        if skip(t):
            continue
        slot = tslot(t)
        # the model's path is a claim: `changed` only when this session edited it (or a commit has it, agent.py);
        # an unedited path stays verified None until the pathcheck step finds it in git
        if t:
            tk = store.ticket(t)
            p = file_key_for(tk, p, ctx.get("repo"))
            did = p in edited
            old = tk["files"].get(p) or {}
            tk["files"][p] = dict(old, path=p, role=fl.get("role") or old.get("role", ""),
                                  changed=did or old.get("changed", False), last=day, **provenance)
            rec = tk["files"][p]
            if did:
                rec.update(verified=True, verified_by=old.get("verified_by") or "edit")
            elif "verified" not in old:
                rec["verified"] = None
            if fl.get("changed") and not did:
                rec["claimed_changed"] = True
            if repo and not old.get("repo"):
                rec["repo"], rec["repo_root"] = repo, ctx.get("repo")
            folded = fold_bare(tk["files"], "/")
            p = folded.get(p, p)
            slot["files_changed"] = [folded.get(x, x) for x in slot["files_changed"]]
        else:
            p = kbnorm.file_key(p, ctx.get("repo"), [repo])
            did = p in edited
        if did and p not in slot["files_changed"]:
            slot["files_changed"].append(p)

    from . import relevance
    for t in sorted(touched):
        relevance.classify(store.ticket(t))
        mark_applied_fixes(store.ticket(t), day)
    store.touch_day(day)


def _names_file(text, path):
    """True when `text` names the file at `path`: its repo path, or its file name as a whole word."""
    low = text.lower()
    p = path.replace("\\", "/").lower()
    if p in low:
        return True
    base = p.rsplit("/", 1)[-1]
    return len(base) >= 4 and "." in base and re.search(r"(?<![\w./-])" + re.escape(base) + r"(?![\w-])", low) is not None


def mark_applied_fixes(tk, day=None):
    """Fix items become state "applied" when a file they name was changed by an Edit/Write tool call or a commit
    (a file record with verified_by edit / commit). Deterministic, never undone. Returns how many changed."""
    files = [f for f in (tk.get("files") or {}).values()
             if isinstance(f, dict) and f.get("changed") and f.get("verified") is True and f.get("verified_by") in ("edit", "commit")]
    n = 0
    for it in (tk.get("items") or {}).values():
        if it.get("kind") != "fix" or it.get("state") == "applied" or not files:
            continue
        text = " ".join([it.get("title") or "", it.get("detail") or ""] + [str(e) for e in it.get("evidence") or []])
        hit = next((f for f in sorted(files, key=lambda f: f.get("verified_by") != "commit") if _names_file(text, f.get("path") or "")), None)
        if hit:
            it.update(state="applied", applied_by=hit.get("verified_by"), applied_file=hit.get("path"),
                      applied_at=day or hit.get("last") or "")
            n += 1
    return n


def raw_ref(day, sid, line):
    """"raw/<day>/<sid>.jsonl:<line>": the raw line an item came from (the digest's [HH:MM L<n>] marker), or None."""
    if isinstance(line, bool) or not isinstance(line, int) or line < 1 or not day or not sid:
        return None
    return "raw/%s/%s.jsonl:%d" % (day, sid, line)


def _merge_rec(old, new):
    if not old:
        return dict(new)
    out = dict(old)
    for k, v in new.items():
        if isinstance(v, list):
            out[k] = _union(old.get(k), v)
        elif k in ("time", "src", "ref") and (new.get("last", ""), new.get("time", "")) >= (old.get("last", ""), old.get("time", "")):
            out[k] = v
        elif v and (not old.get(k) or k == "last"):
            out[k] = v
    return out


# --------------------------------------------------------------------------- one record per thing (K7)

def query_id(owner, text):
    """A query's id: the same SQL up to spacing, case outside literals, a session SET prefix or a trailing ';'."""
    return common.stable_id(owner, "query", kbnorm.sql_key(text))


def add_query_sighting(cur, rec):
    """The same query seen again: keep the first record, add the day and any differently written text."""
    cur["days"] = _union(cur.get("days") or [cur.get("date")], rec.get("days") or [rec.get("date")])
    if rec.get("text") and rec["text"].strip() != (cur.get("text") or "").strip():
        cur["variants"] = _union(cur.get("variants"), [rec["text"]])[:5]
    for k in ("revealed", "purpose"):
        if rec.get(k) and not cur.get(k):
            cur[k] = rec[k]
    if rec.get("sent") and cur.get("sent") is not True:  # sent once is sent; never back to false
        cur["sent"] = True
        if rec.get("ref"):
            cur["ref"] = rec["ref"]
    elif rec.get("sent") is False and "sent" not in cur:
        cur["sent"] = False


def table_name(key, recs):
    """The display name of a merged table: a written name that is this key, else the key itself."""
    for r in recs:
        if kbnorm.table_key(r.get("name")) == key:
            return r["name"]
    return key


def file_key_for(tk, path, repo_root=None):
    """A file's key in a ticket: repo-relative posix; a bare name takes the one known full path with that name."""
    names = [os.path.basename(str(repo_root or "").rstrip("/\\"))] + list(tk.get("repos") or [])
    k = kbnorm.file_key(path, repo_root, [n for n in names if n])
    if "/" not in k:
        hits = [x for x in tk.get("files") or {} if x.endswith("/" + k)]
        if len(hits) == 1:
            return hits[0]
    return k


def merge_dup(keep, other, keep_key, other_key):
    """Two records of one thing: every list is united, a value is filled only where `keep` has none, the newest
    last / last_seen wins, `changed` is either, and the other key becomes an alias. Nothing is dropped."""
    out = dict(keep)
    for k, v in other.items():
        if k in ("last", "last_seen"):
            out[k] = max(str(out.get(k) or ""), str(v or ""))
        elif k == "changed":
            out[k] = bool(out.get(k)) or bool(v)
        elif k in ("aliases", "id"):
            continue
        elif isinstance(v, list):
            out[k] = _union(out.get(k), v)
        elif v not in (None, "", {}) and out.get(k) in (None, "", {}, []):
            out[k] = v
    if "text" in other and (other.get("text") or "").strip() != (out.get("text") or "").strip():
        out["variants"] = _union(out.get("variants"), [other["text"]])[:5]
    if "host_or_service" in other:
        out["hosts"] = _union(out.get("hosts"), [x for x in (keep.get("host_or_service"), other.get("host_or_service")) if x])
    if "text" in other and "date" in other:  # a query: every day it was seen
        days = _union(out.get("days") or [keep.get("date")], other.get("days") or [other.get("date")])
        out["days"] = sorted(d for d in days if d)
    al = _union(out.get("aliases"), [other_key] + list(other.get("aliases") or []))
    out["aliases"] = [a for a in al if a != keep_key]
    if not out["aliases"]:
        out.pop("aliases")
    return out


def fold_bare(d, sep, namefn=None):
    """Merge each bare key ("orders", "Order.java") into the one qualified key ending in it ("shop.orders",
    "src/Order.java"), in place. Ambiguous names stay as they are. Returns {old key: new key}."""
    moved = {}
    for k in [k for k in d if sep not in k]:
        hits = [x for x in d if x != k and x.endswith(sep + k)]
        if len(hits) != 1:
            continue
        old = d.pop(k)
        d[hits[0]] = merge_dup(d[hits[0]], old, hits[0], k)
        if namefn:
            d[hits[0]]["name"] = namefn(hits[0], [d[hits[0]], old])
        if sep == "/":
            d[hits[0]]["path"] = hits[0]
        moved[k] = hits[0]
    return moved


def lookup(d, key):
    """d[key], or the record that took `key` over as an alias."""
    if key in d:
        return d[key]
    for rec in d.values():
        if isinstance(rec, dict) and key in (rec.get("aliases") or ()):
            return rec
    return None


def _merge_catalog(table, key, rec, ticket, day):
    cur = _merge_rec(table.get(key), rec)
    cur["tickets"] = _union(cur.get("tickets"), [ticket] if ticket else [])
    cur["last_seen"] = max(cur.get("last_seen") or day, day)
    cur.pop("last", None)
    table[key] = cur


def _when(it):
    return (str(it.get("date") or ""), hhmm_of(it.get("time")))


def _hist(it):
    return json.loads(json.dumps({k: v for k, v in it.items() if k not in ("history", "evidence")}))


def history_key(it):
    """Provenance changes alone do not create a new version."""
    return tuple(it.get(k) or "" for k in ("title", "detail", "confidence"))


def union_history(*groups):
    """Linear content dedupe, followed by chronological ordering (previous version last)."""
    seen, out = set(), []
    for group in groups:
        for version in group or []:
            key = history_key(version)
            if key not in seen:
                seen.add(key)
                out.append(_hist(version))
    return sorted(out, key=_when)


def compact_history(it, owner):
    """Keep the first and newest four snapshots; append every older version before removing it inline."""
    from .safety import paths as sp
    hist = union_history(it.get("history"))
    if len(hist) > 5:
        name = owner if valid_ticket(owner, None) else common.stable_id(owner)
        path = common.hp("data", "history", name + ".jsonl")
        existing = common.read_text(path)
        seen = set()
        for line in existing.splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict) and isinstance(row.get("version"), dict):
                    seen.add((row.get("item"), history_key(row["version"])))
            except (ValueError, TypeError):
                continue  # retain a partial old row; later complete rows remain independently readable
        rows = []
        for version in hist[1:-4]:
            row = redact_obj({"item": it.get("id"), "version": version})
            key = (row["item"], history_key(row["version"]))
            if key not in seen:
                seen.add(key)
                rows.append(row)
        if rows:
            sp.makedirs(os.path.dirname(path))
            prefix = "\n" if existing and not existing.endswith("\n") else ""
            sp.append_text(path, prefix + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        hist = hist[:1] + hist[-4:]
    it["history"] = hist


def _remember(it, version, owner):
    it["history"] = union_history(it.get("history"), [_hist(version)])
    compact_history(it, owner)


def _merge_item(store, dd, t, gkey, rec, updates, tslot):
    slot = tslot(t)
    if t:
        tk = store.ticket(t)
        items = tk["items"]
    else:
        items = slot["items"]
    target = updates if (updates and updates in items) else None
    iid = target or common.stable_id(t or gkey, rec["kind"], rec["title"])
    if iid in items:
        old = items[iid]
        older = _when(rec) < _when(old)  # a re-extracted earlier session: its text never replaces a newer one
        if old.get("session") == "inbox" or old.get("src") == "user":
            if any(old.get(k) != rec.get(k) for k in ("kind", "title", "detail", "confidence")):
                _remember(old, rec, t or gkey)
                store.bump(t or gkey, "updates")
            old["evidence"] = _union(old.get("evidence"), rec["evidence"])[:10]
        elif history_key(old) == history_key(rec) and old.get("kind") == rec.get("kind"):
            old["evidence"] = _union(old.get("evidence"), rec["evidence"])[:10]
            if not older:
                for k in ("date", "time", "session", "ref"):
                    if rec.get(k) is not None:
                        old[k] = rec[k]
        elif target and older:
            _remember(old, rec, t or gkey)
            old["evidence"] = _union(old.get("evidence"), rec["evidence"])[:10]
            store.bump(t or gkey, "updates")
        elif target:
            _remember(old, old, t or gkey)
            hist = old["history"]
            new = dict(rec, id=iid, first_date=old.get("first_date") or old.get("date"), history=hist)
            new["evidence"] = _union(rec["evidence"], old.get("evidence"))[:10]
            for k in ("archived", "review_kept"):  # your decisions survive an update; new text is current again
                if old.get(k):
                    new[k] = old[k]
            if old.get("review_kept") or (old.get("review") and not new.get("review")):
                new["review"] = None if old.get("review_kept") else old.get("review")
            items[iid] = new
            store.bump(t or gkey, "updates")
        else:
            if older and rec.get("detail") != old.get("detail"):
                _remember(old, rec, t or gkey)
            old["evidence"] = _union(old.get("evidence"), rec["evidence"])[:10]
            for k in ("phase", "ref"):
                if rec.get(k) and not old.get(k):
                    old[k] = rec[k]
            if not older:
                detail = rec.get("detail") or ""
                if detail and len(detail) > len(old.get("detail") or ""):
                    if " ".join(detail.lower().split()) != " ".join((old.get("detail") or "").lower().split()):
                        _remember(old, old, t or gkey)
                        old["detail"] = detail
                for k in ("date", "time", "session", "ref"):
                    if rec.get(k) is not None:
                        old[k] = rec[k]
                # A re-mention is not new confirmation; an explicit updates correction carries its own confidence.

    else:
        items[iid] = dict(rec, id=iid, first_date=rec["date"])
        store.bump(t or gkey, "findings" if rec["kind"] not in ("todo", "question", "blocker") else "todos")
    if t and iid not in slot["item_ids"]:
        slot["item_ids"].append(iid)


ALREADY_BUDGET = 20000  # chars of ALREADY_RECORDED in an extraction header (extract.build_digests)


def _jlen(x):
    return len(json.dumps(x, ensure_ascii=False))


def already_recorded(store, tickets, limit=80, budget=ALREADY_BUDGET):
    """Compact view of what's known for candidate tickets, passed to the extractor. Each candidate gets an equal share
    of `budget` chars, filled with its newest items first; superseded and archived items are left out."""
    out = {}
    share = budget // max(1, len(tickets)) - 4  # the key and separators
    for t in tickets:
        tk = store.ticket(t)
        live = [i for i in tk["items"].values() if isinstance(i, dict) and not i.get("archived")
                and i.get("status") != "superseded"]
        items = sorted(live, key=lambda i: (i.get("date", ""), i.get("time", "")), reverse=True)[:limit]
        entry = {
            "title": tk.get("title") or tk.get("title_hint") or "",
            "items": [],
            "queries": [q.get("purpose") or (q.get("text") or "")[:80] for q in list(tk["queries"].values())[-30:]],
            "tables": sorted(tk["tables"].keys())[:60],
            "apis": sorted(tk["apis"].keys())[:60],
        }
        size = _jlen(entry) + len(t)
        for k in ("apis", "tables", "queries"):  # the lists give way to items when a share is small
            while size > share // 2 and entry[k]:
                size -= _jlen(entry[k].pop()) + 2
        for i in items:
            rec = {"id": i["id"], "kind": i["kind"], "title": i["title"], "confidence": i.get("confidence")}
            if i.get("status") and i["status"] != "current":
                rec["status"] = i["status"]
            n = _jlen(rec) + 2
            if size + n > share:
                break
            entry["items"].append(rec)
            size += n
        out[t] = entry
    return out
