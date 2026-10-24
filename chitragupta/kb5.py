"""Data migrations of the knowledge fixes (0.5.0), registered in migrate.KB_STEPS.

Each is fn(dry_run, notes, backup): it backs up every file it will change, then changes it in place. Forward-only and
additive: records are re-keyed and merged, never dropped; an old key stays on the merged record as an alias.
A migration that changes what the markdown shows asks the next tick to re-render everything (request_rerender).
"""
import json
import os

from . import common
from . import kbnorm
from . import store as S
from .safety import paths as sp


def rerender_marker():
    return common.wl("cache", "rerender-all")


def request_rerender(dry_run):
    """The next tick renders every day and ticket once (the marker is a cache file, removed after that render)."""
    if not dry_run:
        common.write_text(rerender_marker(), common.now_iso() + "\n")


def take_rerender():
    """True once after request_rerender(): removes the marker."""
    p = rerender_marker()
    if not os.path.exists(p):
        return False
    try:
        sp.delete(p)
    except OSError:
        pass
    return True


def _day_files():
    root = common.hp("data", "days")
    out = []
    for d, _, files in os.walk(root):
        out += [os.path.join(d, f) for f in sorted(files) if f.endswith(".json")]
    return sorted(out)


def _changed(a, b):
    return json.dumps(a, sort_keys=True, default=str) != json.dumps(b, sort_keys=True, default=str)


# --------------------------------------------------------------------------- kb5-dedupe-keys (K7)

def _rekey(d, keyfn, after=None):
    """Re-key a {key: record} dict, merging records that land on one key. Returns (new dict, {old: new})."""
    out, m = {}, {}
    for old, rec in (d or {}).items():
        if not isinstance(rec, dict):
            out[old], m[old] = rec, old
            continue
        k = keyfn(old, rec)
        m[old] = k
        if k in out:
            out[k] = S.merge_dup(out[k], rec, k, old)
        else:
            out[k] = dict(rec)
            if old != k:
                out[k]["aliases"] = S._union(rec.get("aliases"), [old])
        if after:
            after(k, out[k], rec)
    return out, m


def _api_rec(k, cur, rec):
    host = rec.get("host_or_service")
    cur["hosts"] = S._union(cur.get("hosts"), [host] if host else [])
    cur["paths"] = S._union(cur.get("paths"), [rec.get("path")] if rec.get("path") else [])


def rekey_apis(d):
    return _rekey(d, lambda old, r: kbnorm.api_key(r.get("method"), r.get("path") or old), _api_rec)


def rekey_tables(d, outer_known=()):
    norm = {old: kbnorm.table_key((r or {}).get("name") or old) if isinstance(r, dict) else old for old, r in (d or {}).items()}
    known = [n for n in norm.values() if "." in n]
    outer = [n for n in outer_known if "." in n]

    def key(old, _r):
        k = kbnorm.table_key(norm[old], known)
        return kbnorm.table_key(k, outer) if "." not in k else k
    out, m = _rekey(d, key)
    for k, rec in out.items():
        if isinstance(rec, dict):
            rec["name"] = S.table_name(k, [rec] + [r for o, r in d.items() if m.get(o) == k and isinstance(r, dict)])
    return out, m


def rekey_queries(d, owner):
    def key(old, r):
        return S.query_id(owner, r.get("text") or "") if r.get("text") else old

    def after(k, cur, rec):
        cur["id"] = k
        cur["days"] = sorted(x for x in S._union(cur.get("days"), [rec.get("date")]) if x)
    return _rekey(d, key, after)


def rekey_files(d, repos):
    out, m = _rekey(d, lambda old, r: kbnorm.file_key(r.get("path") or old, None, repos))
    moved = S.fold_bare(out, "/")
    for k, rec in out.items():
        if isinstance(rec, dict):
            rec["path"] = k
    return out, {o: moved.get(n, n) for o, n in m.items()}


def _remap(keys, m, fallback=None):
    out = []
    for k in keys or []:
        n = m.get(k) or (fallback(k) if fallback else k)
        if n not in out:
            out.append(n)
    return out


def dedupe_keys(dry_run, notes, backup):
    """K7: one record per API (METHOD + path template), table (schema-qualified), query (normalised SQL) and file
    (repo-relative path) in tickets, catalog.json and the day slots; old keys kept as aliases."""
    cat_p = S.catalog_path()
    cat = common.read_json(cat_p, None) or {}
    cat_tables = list((cat.get("tables") or {}).keys())
    tickets, maps = {}, {}
    for t in S.Store.all_ticket_ids():
        tk = common.read_json(S.ticket_path(t), None)
        if not isinstance(tk, dict) or not tk:
            continue
        new = dict(tk)
        mm = {}
        new["apis"], mm["apis"] = rekey_apis(tk.get("apis"))
        new["tables"], mm["tables"] = rekey_tables(tk.get("tables"), [kbnorm.table_key(x) for x in cat_tables])
        new["queries"], mm["queries"] = rekey_queries(tk.get("queries"), t)
        new["files"], mm["files"] = rekey_files(tk.get("files"), list(tk.get("repos") or []))
        maps[t] = (mm, list(tk.get("repos") or []), new["files"])
        if _changed(tk, new):
            tickets[t] = new
            notes.append("%s: apis %d -> %d, tables %d -> %d, queries %d -> %d, files %d -> %d" % (
                t, len(tk.get("apis") or {}), len(new["apis"]), len(tk.get("tables") or {}), len(new["tables"]),
                len(tk.get("queries") or {}), len(new["queries"]), len(tk.get("files") or {}), len(new["files"])))
    new_cat = dict(cat)
    if cat:
        new_cat["apis"], _ = rekey_apis(cat.get("apis"))
        new_cat["tables"], _ = rekey_tables(cat.get("tables"))
    cat_changed = bool(cat) and _changed(cat, new_cat)
    if cat_changed:
        notes.append("catalog.json: apis %d -> %d, tables %d -> %d" % (len(cat.get("apis") or {}), len(new_cat["apis"]),
                                                                       len(cat.get("tables") or {}), len(new_cat["tables"])))
    days = {}
    for p in _day_files():
        dd = common.read_json(p, None)
        if not isinstance(dd, dict) or not dd:
            continue
        nd = json.loads(json.dumps(dd))
        for t, slot in (nd.get("tickets") or {}).items():
            if t not in maps or not isinstance(slot, dict):
                continue
            mm, repos, files = maps[t]
            slot["api_keys"] = _remap(slot.get("api_keys"), mm["apis"])
            slot["table_keys"] = _remap(slot.get("table_keys"), mm["tables"])
            slot["query_ids"] = _remap(slot.get("query_ids"), mm["queries"])
            slot["files_changed"] = _remap(slot.get("files_changed"), mm["files"],
                                           lambda f: S.file_key_for({"files": files, "repos": repos}, f))
        for g, slot in (nd.get("other") or {}).items():
            if not isinstance(slot, dict):
                continue
            slot["apis"], _ = rekey_apis(slot.get("apis"))
            slot["tables"], _ = rekey_tables(slot.get("tables"))
            slot["queries"], _ = rekey_queries(slot.get("queries"), g)
            repo = g.split(" · ")[0]
            slot["files_changed"] = _remap(slot.get("files_changed"), {}, lambda f: kbnorm.file_key(f, None, [repo]))
        if _changed(dd, nd):
            days[p] = nd
    if days:
        notes.append("%d day file(s): slot keys re-keyed" % len(days))
    if not (tickets or cat_changed or days):
        return
    backup([common.hp("data", "tickets"), cat_p, common.hp("data", "days")])
    if dry_run:
        return
    for t, tk in tickets.items():
        common.write_json(S.ticket_path(t), tk)
    if cat_changed:
        common.write_json(cat_p, new_cat)
    for p, dd in days.items():
        common.write_json(p, dd)
    request_rerender(dry_run)


# --------------------------------------------------------------------------- kb5-title-status (K4)

def title_status(dry_run, notes, backup):
    """K4: a title that came from a branch slug or the model moves to title_hint (title stays only from Jira or you);
    the one `status` field splits into jira.status (already in the card) and status_inferred (day close)."""
    changed = {}
    for t in S.Store.all_ticket_ids():
        tk = common.read_json(S.ticket_path(t), None)
        if not isinstance(tk, dict) or not tk:
            continue
        new = dict(tk)
        if new.get("title") and new.get("title_source") not in S.TITLE_SOURCES:
            if not new.get("title_hint"):
                new["title_hint"], new["title_hint_source"] = new["title"], {"hint": "model"}.get(new.get("title_source"),
                                                                                              new.get("title_source") or "")
            new["title"], new["title_source"] = "", ""
        st = new.get("status") or ""
        if st and not new.get("status_inferred") and st.lower() != S.jira_status(new).lower():
            new["status_inferred"] = st  # what day close wrote; a value equal to the card's status was the card's
        new["status"] = S.status_of(new)
        if _changed(tk, new):
            changed[t] = new
            notes.append("%s: title %r -> %r (hint %r), status %r -> jira %r, inferred %r" % (
                t, tk.get("title", ""), new.get("title", ""), new.get("title_hint", ""), st, S.jira_status(new),
                new.get("status_inferred", "")))
    if not changed:
        return
    backup([S.ticket_path(t) for t in changed])
    if dry_run:
        return
    for t, tk in changed.items():
        common.write_json(S.ticket_path(t), tk)
    request_rerender(dry_run)


# --------------------------------------------------------------------------- kb5-paths-verified (K6a)

def paths_verified(dry_run, notes, backup):
    """K6a: every file record gets `verified`. Old records can't tell a model's claim from an edit or a commit, so
    they become None (pending): the pathcheck tick step confirms each against git. `changed` is kept as it was."""
    changed = {}
    for t in S.Store.all_ticket_ids():
        tk = common.read_json(S.ticket_path(t), None)
        if not isinstance(tk, dict) or not tk:
            continue
        n = 0
        for rec in (tk.get("files") or {}).values():
            if isinstance(rec, dict) and "verified" not in rec:
                rec["verified"] = None
                n += 1
        if n:
            changed[t] = tk
            notes.append("%s: %d file path(s) pending a check against git" % (t, n))
    if not changed:
        return
    backup([S.ticket_path(t) for t in changed])
    if dry_run:
        return
    for t, tk in changed.items():
        common.write_json(S.ticket_path(t), tk)
    request_rerender(dry_run)


# --------------------------------------------------------------------------- kb5-queries-sent (K6b)

def _sent_on(day, memo):
    """The SQL sent on a work day, from the raw files still on disk (None when that day's raw is gone)."""
    if day not in memo:
        from . import agent, sqlsent
        root = common.wl("raw", day)
        names = sorted(f for f in os.listdir(root) if f.endswith(".jsonl")) if os.path.isdir(root) else []
        if not names:
            memo[day] = None
        else:
            evs = []
            for fn in names:
                for e in agent.read_lines(os.path.join(root, fn), 0)[0]:
                    e["_ref"] = "raw/%s/%s:%d" % (day, fn, e["_ln"])
                    evs.append(e)
            memo[day] = sqlsent.sent_statements(evs)
    return memo[day]


def queries_sent(dry_run, notes, backup):
    """K6b: every ticket query gets `sent`. A saved SQL script stays unknown (None, its file as evidence); a model
    query is matched against the SQL its days' raw files sent: true with the raw line as `ref`, else false (kept in
    the data, not listed in queries.md); unknown (None) when that raw is no longer on disk."""
    from . import sqlsent
    memo, changed = {}, {}
    for t in S.Store.all_ticket_ids():
        tk = common.read_json(S.ticket_path(t), None)
        if not isinstance(tk, dict) or not tk:
            continue
        n = {"true": 0, "false": 0, "unknown": 0}
        for q in (tk.get("queries") or {}).values():
            if not isinstance(q, dict) or "sent" in q:
                continue
            if (q.get("purpose") or "").startswith("SQL script "):
                q["sent"] = None
                n["unknown"] += 1
                continue
            stmts = [s for d in sorted(set((q.get("days") or []) + [q.get("date")]) - {None, ""})
                     for s in (_sent_on(d, memo) or [])]
            have_raw = any(_sent_on(d, memo) is not None for d in set((q.get("days") or []) + [q.get("date")]) if d)
            hit = sqlsent.find(q.get("text") or "", stmts, q.get("variants")) if stmts else None
            if hit:
                q["sent"] = True
                if hit.get("ref"):
                    q["ref"] = hit["ref"]
                n["true"] += 1
            elif have_raw:
                q["sent"] = False
                n["false"] += 1
            else:
                q["sent"] = None
                n["unknown"] += 1
        if any(n.values()):
            changed[t] = tk
            notes.append("%s: queries sent %d, never sent %d, unknown %d" % (t, n["true"], n["false"], n["unknown"]))
    if not changed:
        return
    backup([S.ticket_path(t) for t in changed])
    if dry_run:
        return
    for t, tk in changed.items():
        common.write_json(S.ticket_path(t), tk)
    request_rerender(dry_run)


# --------------------------------------------------------------------------- kb5-todo-objects (K3)

def todo_objects(dry_run, notes, backup):
    """K3: a day's open_todos and carried_todos (and the state's carry_todos) become {text, ticket, closed_by} objects,
    the ticket read from the key in the text; nothing is dropped. Closing them is left to the next day close."""
    from . import supersede
    cfg = common.read_json(common.wl("config.json"), {}) or {}
    prefixes = cfg.get("ticket_prefixes") or None

    def conv(lst):
        return [supersede.todo_obj(x, prefixes) for x in lst or []]
    days = {}
    for p in _day_files():
        dd = common.read_json(p, None)
        if not isinstance(dd, dict) or not dd:
            continue
        n = sum(1 for k in ("open_todos", "carried_todos") for x in dd.get(k) or [] if not isinstance(x, dict))
        if n:
            dd["open_todos"], dd["carried_todos"] = conv(dd.get("open_todos")), conv(dd.get("carried_todos"))
            days[p] = dd
            notes.append("%s: %d TODO line(s) -> objects" % (os.path.basename(p)[:-5], n))
    spath = common.wl("state.json")
    st = common.read_json(spath, {}) or {}
    carry = st.get("carry_todos") if isinstance(st, dict) else None
    st_changed = isinstance(carry, dict) and any(not isinstance(x, dict) for x in carry.get("todos") or [])
    if st_changed:
        carry["todos"] = conv(carry.get("todos"))
        notes.append("state.json: carried TODOs -> objects")
    if not (days or st_changed):
        return
    backup([common.hp("data", "days"), spath])
    if dry_run:
        return
    for p, dd in days.items():
        common.write_json(p, dd)
    if st_changed:
        common.write_json(spath, st)
    request_rerender(dry_run)
