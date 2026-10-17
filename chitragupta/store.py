"""Canonical data under <home>/data: tickets/<T>.json, days/<date>.json, catalog.json.

Markdown is always rendered from here and never parsed back. Every write is atomic.
IDs are stable hashes, so merging the same extraction twice is a no-op.
"""
import os
import re

from . import common
from .redact import redact_obj

ITEM_KINDS = ["finding", "root_cause", "how_it_works", "fix", "decision", "config", "error",
              "repro_test", "reference", "todo", "question", "blocker", "note"]
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
    return {"id": t, "title": "", "title_source": "", "repos": [], "branches": [], "first": "", "last": "",
            "status": "", "tldr": "", "tldr_date": "", "problem": "", "items": {}, "queries": {},
            "tables": {}, "apis": {}, "integrations": {}, "files": {}, "commits": {}, "timeline": {},
            "bruno": {}, "api_calls": {}, "replay": {}, "data_flow": {}, "table_relations": {}, "dbflow_conflicts": [],
            "artifacts": {}}


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
    return t


def touch_ticket_meta(store, t, day, repo=None, branch=None):
    tk = store.ticket(t)
    if repo:
        tk["repos"] = _union(tk["repos"], [os.path.basename(repo.rstrip("/\\"))])
    if branch and not branch.startswith("detached@"):
        tk["branches"] = _union(tk["branches"], [branch])
        if not tk["title"]:
            slug = re.sub(r"^.*?" + re.escape(t) + r"[-_/]?", "", branch, flags=re.I)
            slug = re.sub(r"[-_/]+", " ", slug).strip()
            if slug:
                tk["title"], tk["title_source"] = slug[:90], "branch"
    if not tk["first"] or day < tk["first"]:
        tk["first"] = day
    if not tk["last"] or day > tk["last"]:
        tk["last"] = day
    store.touch_ticket(t)
    return tk


def merge_extraction(store, res, ctx, prefixes):
    """Merge one validated extraction result. ctx: day, session, time, repo, branch, candidates."""
    day, sid = ctx["day"], ctx["session"]
    known = {p.upper() for p in prefixes}
    dd = store.day(day)
    default_t = ctx["candidates"][0] if len(ctx.get("candidates") or []) == 1 else None
    gkey = group_key(ctx.get("repo"), ctx.get("branch"))

    def resolve(t):
        return valid_ticket(t, known) if t else None

    def tslot(t):
        if t:
            touch_ticket_meta(store, t, day, ctx.get("repo"), ctx.get("branch"))
            return dd["tickets"].setdefault(t, {"minutes": 0, "item_ids": [], "query_ids": [], "api_keys": [],
                                                 "table_keys": [], "integration_keys": [], "files_changed": [],
                                                 "what_i_did": []})
        return dd["other"].setdefault(gkey, {"minutes": 0, "items": {}, "queries": {}, "apis": {}, "tables": {},
                                             "integrations": {}, "files_changed": [], "what_i_did": []})

    # ticket titles
    for link in res.get("ticket_links") or []:
        t = resolve(link.get("ticket"))
        if t and link.get("title_hint"):
            tk = touch_ticket_meta(store, t, day, ctx.get("repo"), ctx.get("branch"))
            if tk["title_source"] in ("", "branch"):
                tk["title"], tk["title_source"] = link["title_hint"][:90], "hint"

    # activity line (what I did)
    summ = (res.get("activity_summary") or "").strip()
    if summ:
        t = default_t or (resolve((res.get("ticket_links") or [{}])[0].get("ticket")) if res.get("ticket_links") else None)
        entry = {"time": ctx.get("time", ""), "text": summ, "session": sid[:8]}
        slot = tslot(t)
        if entry["text"] not in [w["text"] for w in slot["what_i_did"]]:
            slot["what_i_did"].append(entry)
        if summ not in [a["text"] for a in dd["activity"]]:
            dd["activity"].append({"time": entry["time"], "ticket": t, "group": None if t else gkey, "text": summ})

    for it in res.get("items") or []:
        kind = it.get("kind") if it.get("kind") in ITEM_KINDS else "finding"
        title = (it.get("title") or "").strip()[:90]
        if not title:
            continue
        t = resolve(it.get("ticket")) or default_t
        rec = {"kind": kind, "title": title, "detail": (it.get("detail") or "").strip(),
               "evidence": [e for e in (it.get("evidence") or []) if e][:8],
               "confidence": it.get("confidence") or "likely", "date": day,
               "time": it.get("time") or ctx.get("time", ""), "session": sid[:8]}
        _merge_item(store, dd, t, gkey, rec, it.get("updates"), tslot)

    for q in res.get("queries") or []:
        text = (q.get("text") or "").strip()
        if not text:
            continue
        t = resolve(q.get("ticket")) or default_t
        qid = common.stable_id(t or gkey, "query", re.sub(r"\s+", " ", text))
        rec = {"id": qid, "date": day, "time": ctx.get("time", ""), "dialect": q.get("dialect") or "other",
               "text": text, "purpose": q.get("purpose") or "", "tables": q.get("tables") or [],
               "kind": q.get("kind") or "read", "revealed": q.get("revealed") or ""}
        slot = tslot(t)
        if t:
            tk = store.ticket(t)
            if qid not in tk["queries"]:
                tk["queries"][qid] = rec
                store.bump(t, "queries")
            elif rec["revealed"] and not tk["queries"][qid].get("revealed"):
                tk["queries"][qid]["revealed"] = rec["revealed"]
            if qid not in slot["query_ids"]:
                slot["query_ids"].append(qid)
        elif qid not in slot["queries"]:
            slot["queries"][qid] = rec
            store.bump(gkey, "queries")

    cat = store.catalog()
    for tb in res.get("tables") or []:
        name = (tb.get("name") or "").strip()
        if not name:
            continue
        t = resolve(tb.get("ticket")) or default_t
        key = name.lower()
        rec = {"name": name, "db": tb.get("db") or "", "purpose": tb.get("purpose") or "",
               "key_columns": tb.get("key_columns") or [], "relations": tb.get("relations") or [], "last": day}
        _merge_catalog(cat["tables"], key, rec, t, day)
        store.catalog_dirty = True
        slot = tslot(t)
        if t:
            tk = store.ticket(t)
            tk["tables"][key] = _merge_rec(tk["tables"].get(key), rec)
            if key not in slot["table_keys"]:
                slot["table_keys"].append(key)
        else:
            slot["tables"][key] = rec

    for a in res.get("apis") or []:
        path = (a.get("path") or "").strip()
        if not path:
            continue
        method = (a.get("method") or "").upper().strip() or "ANY"
        t = resolve(a.get("ticket")) or default_t
        key = "%s %s %s" % (method, path, (a.get("host_or_service") or "").strip().lower())
        rec = {"method": method, "path": path, "host_or_service": a.get("host_or_service") or "",
               "direction": a.get("direction") or "", "purpose": a.get("purpose") or "",
               "request_fields": a.get("request_fields") or [], "response_fields": a.get("response_fields") or [],
               "auth_type": a.get("auth_type") or "", "last": day}
        _merge_catalog(cat["apis"], key, rec, t, day)
        store.catalog_dirty = True
        slot = tslot(t)
        if t:
            tk = store.ticket(t)
            if key not in tk["apis"]:
                store.bump(t, "APIs")
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

    for fl in res.get("files") or []:
        p = (fl.get("path") or "").strip()
        if not p:
            continue
        t = resolve(fl.get("ticket")) or default_t
        slot = tslot(t)
        if t:
            tk = store.ticket(t)
            old = tk["files"].get(p) or {}
            tk["files"][p] = {"path": p, "role": fl.get("role") or old.get("role", ""),
                              "changed": bool(fl.get("changed")) or old.get("changed", False), "last": day}
        if fl.get("changed") and p not in slot["files_changed"]:
            slot["files_changed"].append(p)

    store.touch_day(day)


def _merge_rec(old, new):
    if not old:
        return dict(new)
    out = dict(old)
    for k, v in new.items():
        if isinstance(v, list):
            out[k] = _union(old.get(k), v)
        elif v and (not old.get(k) or k == "last"):
            out[k] = v
    return out


def _merge_catalog(table, key, rec, ticket, day):
    cur = _merge_rec(table.get(key), rec)
    cur["tickets"] = _union(cur.get("tickets"), [ticket] if ticket else [])
    cur["last_seen"] = max(cur.get("last_seen") or day, day)
    cur.pop("last", None)
    table[key] = cur


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
        if target:
            hist = old.get("history") or []
            hist.append({"date": old.get("date"), "kind": old.get("kind"), "title": old.get("title"),
                         "confidence": old.get("confidence")})
            new = dict(rec, id=iid, first_date=old.get("first_date") or old.get("date"), history=hist[-5:])
            new["evidence"] = _union(rec["evidence"], old.get("evidence"))[:10]
            items[iid] = new
            store.bump(t or gkey, "updates")
        else:
            old["evidence"] = _union(old.get("evidence"), rec["evidence"])[:10]
            if len(rec["detail"]) > len(old.get("detail") or ""):
                old["detail"] = rec["detail"]
            if old.get("confidence") != "confirmed" and rec["confidence"] == "confirmed":
                old["confidence"] = "confirmed"
    else:
        items[iid] = dict(rec, id=iid, first_date=rec["date"])
        store.bump(t or gkey, "findings" if rec["kind"] not in ("todo", "question", "blocker") else "todos")
    if t and iid not in slot["item_ids"]:
        slot["item_ids"].append(iid)


def already_recorded(store, tickets, limit=80):
    """Compact view of what's known for candidate tickets, passed to the extractor."""
    out = {}
    for t in tickets:
        tk = store.ticket(t)
        items = sorted(tk["items"].values(), key=lambda i: (i.get("date", ""), i.get("time", "")), reverse=True)[:limit]
        out[t] = {
            "title": tk.get("title", ""),
            "items": [{"id": i["id"], "kind": i["kind"], "title": i["title"], "confidence": i.get("confidence")}
                      for i in items],
            "queries": [q.get("purpose") or q["text"][:80] for q in list(tk["queries"].values())[-30:]],
            "tables": sorted(tk["tables"].keys())[:60],
            "apis": sorted(tk["apis"].keys())[:60],
        }
    return out
