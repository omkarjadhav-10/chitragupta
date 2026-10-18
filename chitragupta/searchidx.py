"""The search index: one SQLite file over the data home, shared by the dashboard, `/cg:search` and the MCP server.

File: <home>/.worklog/cache/search.sqlite. A local, rebuildable cache, never the source of truth: deleting it loses
nothing. It is opened only through safety.paths.sqlite_connect (write roots checked, ATTACH refused).

What is indexed (one row per "unit"):
  ticket       a ticket's summary (title, TL;DR, problem, status, repos)
  finding      each finding / root cause / decision … of a ticket
  query        each saved query
  request      each saved replay step (replay.final, else requests.md) and each captured API call
  artifact     each artifact entry
  branch, pr   each branch and pull request that branch tracking linked to the ticket (tk["git"])
  api, table, integration, dblink   the knowledge catalogs: APIs, tables (with columns from the QA schema cards),
               integrations, DB links (catalog db_relations and the QA link map)
  day          each daily file
Every unit carries type, ref, title, body, ticket, repo, date, href (the dashboard deep link) and path (the
data-home-relative file to read for detail).

Sources: a ticket JSON (+ its requests.md), the catalog (+ db-schema cards and the link map), each daily file.
update() re-reads only sources whose size or mtime changed and re-indexes only those whose content hash changed;
it runs after every render (fail-open). rebuild() starts over; so does any open whose SCHEMA (or FTS5 support)
differs from the file's.

Readers never write. search() uses the file directly while it matches the data home; when it is missing or behind
(between ticks), it copies the file into memory and applies the same incremental update there.

Two engines answer the same way:
  fts5  SQLite FTS5 (unicode61 tokenizer, diacritics removed, prefix indexes), ranked by bm25 with the title
        weighted 10x the body
  scan  pure Python over the same units, for a SQLite built without FTS5 (or CHITRAGUPTA_SEARCH_ENGINE=scan)

Query rules (shared): lowercase, split on anything that is not a letter or digit ("ABC-1001" -> "abc", "1001"),
at most 8 terms and 200 characters; every term must match a whole token except the last, which matches as a
prefix. Terms are passed to FTS5 as quoted strings, so user input can never become FTS syntax. A query that is
exactly a ticket key puts that ticket first. Snippets are plain text with hit offsets; the page escapes them.
"""
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.parse

from . import common
from .safety import paths

SCHEMA = 1  # bump when units or tables change: every index is rebuilt on its next open
DB_NAME = "search.sqlite"
TYPES = [("ticket", "Tickets"), ("finding", "Findings"), ("query", "Queries"), ("request", "Requests"),
         ("artifact", "Artifacts"), ("branch", "Branches"), ("pr", "Pull requests"), ("api", "APIs"),
         ("table", "Tables"), ("dblink", "DB links"), ("integration", "Integrations"), ("day", "Days")]
TYPE_ORDER = [t for t, _ in TYPES]
LABELS = dict(TYPES)
MAX_TERMS = 8
MAX_QUERY = 200
PER_GROUP = 8
MAX_MATCHES = 2000
SNIPPET = 180
DAY_TEXT_MAX = 100_000
BODY_MAX = 20_000
TITLE_WEIGHT = 10.0
FRESH_S = 2.0  # readers re-check the data home at most this often
TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,6}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOKEN = re.compile(r"[^\W_]+")
_KEY = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]{1,9}-\d{1,6})\s*$")
_COLS = ("type", "ref", "title", "body", "ticket", "tickets", "repo", "date", "href", "path")


# --------------------------------------------------------------------------- text

def fold(s):
    """Lowercase without diacritics (what FTS5's unicode61 tokenizer with remove_diacritics 2 does)."""
    s = (s or "").lower()
    if s.isascii():
        return s
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def tokens(s):
    return [fold(m.group(0)) for m in _TOKEN.finditer(s or "")]


def parse_query(q):
    """-> list of terms (the last one is a prefix)."""
    return tokens((q or "")[:MAX_QUERY])[:MAX_TERMS]


def fts_expr(terms):
    """An FTS5 MATCH expression: each term a quoted string, the last one a prefix. Terms are letters and digits
    only, and quoting would neutralise anything else anyway."""
    quoted = ['"%s"' % t.replace('"', '""') for t in terms]
    quoted[-1] += "*"
    return " ".join(quoted)


def _matches(tok, term, prefix):
    return tok.startswith(term) if prefix else tok == term


def _u16(s, i):
    """A str index as a UTF-16 code-unit offset (what JavaScript string indices count)."""
    head = s[:i]
    return len(head) + sum(1 for c in head if ord(c) > 0xFFFF)


def snippet(title, body, terms):
    """(text, hits): ~180 characters of plain text around the first hit in the body (the title when the body has
    none) and [start, length] of every term occurrence inside it, in UTF-16 code units."""
    if not terms:
        return " ".join((body or title or "").split())[:SNIPPET], []
    last = len(terms) - 1

    def first_hit(text):
        for m in _TOKEN.finditer(text or ""):
            tok = fold(m.group(0))
            if any(_matches(tok, t, k == last) for k, t in enumerate(terms)):
                return m.start()
        return None
    src, at = body or "", first_hit(body)
    if at is None:
        src, at = title or "", first_hit(title) or 0
    start = max(0, at - SNIPPET // 3)
    if start:  # start on a word boundary
        sp = src.rfind(" ", max(0, start - 20), at)
        start = sp + 1 if sp >= 0 else start
    end = min(len(src), start + SNIPPET)
    if end < len(src):
        sp = src.rfind(" ", at, end)
        end = sp if sp > at + 20 else end
    text = " ".join(src[start:end].split())
    text = ("…" if start > 0 else "") + text + ("…" if end < len(src) else "")
    hits = []
    for m in _TOKEN.finditer(text):
        tok = fold(m.group(0))
        for k, t in enumerate(terms):
            if _matches(tok, t, k == last):
                n = len(t) if k == last and len(m.group(0)) == len(tok) else len(m.group(0))
                hits.append([_u16(text, m.start()), _u16(text, m.start() + n) - _u16(text, m.start())])
                break
    return text, hits


# --------------------------------------------------------------------------- units

def _j(*parts):
    return " ".join(str(p) for p in parts if p not in (None, "", [], {}))


def _flat(xs):
    return " ".join(str(x) for x in xs or [] if x) if isinstance(xs, list) else (str(xs) if xs else "")


def _first(xs):
    return str(xs[0]) if isinstance(xs, list) and xs else ""


def _strs(xs):
    return [str(x) for x in xs if x] if isinstance(xs, list) else []


def _unit(type_, ref, title, body, ticket="", date="", href="", path="", repo="", tickets=None):
    tickets = [t for t in (tickets or ([ticket] if ticket else [])) if t]
    return {"type": type_, "ref": str(ref), "title": (str(title or "")).strip()[:300], "body": (body or "")[:BODY_MAX],
            "ticket": ticket or "", "tickets": ",".join(tickets), "repo": repo or "", "date": str(date or "")[:10],
            "href": href, "path": path}


def _repo_name(r):
    return os.path.basename(str(r).rstrip("/\\"))


def _exists(*rel):
    return os.path.exists(common.hp(*rel))


def ticket_units(key, tk, requests_md=""):
    """Units of one ticket record (+ the text of its requests.md)."""
    out = []
    base = "#/tickets/" + key
    readme = "tickets/%s/README.md" % key if _exists("tickets", key, "README.md") else "data/tickets/%s.json" % key
    record = "data/tickets/%s.json" % key
    status = tk.get("status") or ((tk.get("jira") or {}).get("status") or "")
    git = tk.get("git") if isinstance(tk.get("git"), dict) else {}
    grepos = {n: e for n, e in sorted((git.get("repos") or {}).items()) if isinstance(e, dict)}
    repos = []
    for r in [_repo_name(r) for r in tk.get("repos") or []] + list(grepos):
        if r and r not in repos:
            repos.append(r)
    rs = ",".join(repos)
    last = tk.get("last") or tk.get("first") or ""
    out.append(_unit("ticket", key, _j(key, tk.get("title")), _j(tk.get("tldr"), tk.get("problem"), status, *repos),
                     key, last, base, readme, rs))
    for iid, it in sorted((tk.get("items") or {}).items()):
        if isinstance(it, dict):
            out.append(_unit("finding", "%s/%s" % (key, iid), it.get("title"),
                             _j(it.get("detail"), _flat(it.get("evidence")), it.get("kind"), key), key, it.get("date"),
                             base + "?tab=overview", readme, rs))
    qpath = "tickets/%s/queries.md" % key if _exists("tickets", key, "queries.md") else readme
    for qid, q in sorted((tk.get("queries") or {}).items()):
        if isinstance(q, dict):
            out.append(_unit("query", "%s/%s" % (key, qid), q.get("purpose") or "Query",
                             _j(q.get("text"), _flat(q.get("tables")), key), key, q.get("date"), base + "?tab=queries",
                             qpath, rs))
    out += _request_units(key, tk, requests_md, base + "?tab=requests", record, rs, last)
    for aid, a in sorted((tk.get("artifacts") or {}).items()):
        if isinstance(a, dict):
            stored = a.get("stored_name") or a.get("stored")
            apath = "tickets/%s/artifacts/INDEX.md" % key
            if isinstance(stored, str) and stored and _exists("tickets", key, "artifacts", os.path.basename(stored)):
                apath = "tickets/%s/artifacts/%s" % (key, os.path.basename(stored))
            out.append(_unit("artifact", "%s/%s" % (key, aid), a.get("name") or aid,
                             _j(a.get("purpose"), a.get("kind"), key), key, a.get("last") or a.get("date"),
                             base + "?tab=artifacts", apath, rs))
    out += _git_units(key, tk, grepos, base + "?tab=branches", readme)
    return out


def _request_units(key, tk, requests_md, href, record, rs, last):
    out = []
    rpath = "tickets/%s/requests.md" % key if requests_md else record
    final = [s for s in (tk.get("replay") or {}).get("final") or [] if isinstance(s, dict)]
    for i, s in enumerate(final, 1):
        n = s.get("n") or i
        out.append(_unit("request", "%s/step-%s" % (key, n), s.get("name") or _j((s.get("method") or "").upper(), s.get("url")),
                         _j((s.get("method") or "").upper(), s.get("url"), s.get("expected"), s.get("scenario"),
                            s.get("file"), _flat(s.get("critical_fields")), s.get("verify_query"), key),
                         key, last, href, rpath, rs))
    if not final and requests_md:
        from .dashboard import api
        for n, s in sorted(api.parse_requests_md(requests_md)["steps"].items()):
            out.append(_unit("request", "%s/step-%s" % (key, n), s.get("name"),
                             _j(s.get("curl", "")[:4000], s.get("expected"), key), key, last, href, rpath, rs))
    for cid, c in sorted((tk.get("api_calls") or {}).items()):
        if not isinstance(c, dict):
            continue
        method, url = (c.get("method") or "").upper(), c.get("url") or ""
        title = c.get("name") or _j(method, urllib.parse.urlsplit(url).path or url)
        out.append(_unit("request", "%s/call-%s" % (key, cid), title,
                         _j(method, url, c.get("purpose"), c.get("expected"), c.get("scenario"), c.get("role"),
                            c.get("outcome"), _flat(c.get("statuses")), c.get("evidence"), key),
                         key, c.get("day") or c.get("at") or last, href, record, rs))
    return out


def _git_units(key, tk, grepos, href, path):
    out = []
    for repo, e in grepos.items():
        for b in e.get("branches") or []:
            if not isinstance(b, dict) or not b.get("branch"):
                continue
            subjects = [c.get("subject") for c in (b.get("commits") or [])[:20] if isinstance(c, dict)]
            out.append(_unit("branch", "%s/%s/%s" % (key, repo, b["branch"]), b["branch"],
                             _j(repo, "from", b.get("parent"), b.get("status"), b.get("link"), *subjects, key),
                             key, b.get("last_commit") or b.get("created_at"), href, path, repo))
        for p in e.get("prs") or []:
            if not isinstance(p, dict):
                continue
            num = p.get("number") or p.get("id") or ""
            out.append(_unit("pr", "%s/%s/pr-%s" % (key, repo, num), _j("PR", "#%s" % num if num else "", p.get("title")),
                             _j(repo, p.get("source"), "into", p.get("target"), p.get("state"), p.get("review"),
                                p.get("url"), key),
                             key, p.get("merged_at") or p.get("created_at"), href, path, repo))
    if not grepos:  # before branch tracking ran: the branch names seen in sessions
        for name in tk.get("branches") or []:
            if isinstance(name, str) and name and not name.startswith("refs/"):
                out.append(_unit("branch", "%s//%s" % (key, name), name, key, key, tk.get("last"), href, path))
    return out


def _schema_columns(cards):
    """{lowercase "schema.table" and bare "table": [column names]} from the QA schema cards."""
    full, bare = {}, {}
    for rec in cards:
        if not isinstance(rec, dict):
            continue
        name = str(rec.get("table") or "").lower()
        cols = [c.get("name") for c in rec.get("describe") or [] if isinstance(c, dict) and c.get("name")]
        if not name or not cols:
            continue
        full.setdefault(name, cols)
        bare.setdefault(name.rsplit(".", 1)[-1], []).append(cols)
    return full, {k: v[0] for k, v in bare.items() if len(v) == 1}  # a bare name only when it is unambiguous


def _table_of(ref):
    """"schema.table.column" -> "schema.table" (a plain "table.column" -> "table")."""
    parts = str(ref or "").split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else str(ref or "")


def catalog_units(cat, linkmap, cards):
    out = []
    kn = lambda name: "knowledge/" + name if _exists("knowledge", name) else "data/catalog.json"  # noqa: E731
    for k, a in sorted((cat.get("apis") or {}).items()):
        if isinstance(a, dict):
            path = a.get("path") or ""
            out.append(_unit("api", k, _j((a.get("method") or "").upper(), path),
                             _j(a.get("host_or_service") or a.get("host"), a.get("purpose"), _flat(a.get("request_fields")),
                                _flat(a.get("response_fields"))),
                             _first(a.get("tickets")), a.get("last_seen"),
                             "#/knowledge?tab=apis&q=" + urllib.parse.quote(path, safe=""), kn("apis.md"),
                             tickets=_strs(a.get("tickets"))))
    cols, bare = _schema_columns(cards)
    for k, t in sorted((cat.get("tables") or {}).items()):
        if isinstance(t, dict):
            name = t.get("name") or k
            c = cols.get(name.lower()) or bare.get(name.lower().rsplit(".", 1)[-1]) or t.get("key_columns") or []
            out.append(_unit("table", name, name, _j(t.get("db"), t.get("purpose"), _flat(c)), _first(t.get("tickets")),
                             t.get("last_seen"), "#/knowledge?tab=db&table=" + urllib.parse.quote(name, safe=""),
                             kn("db-tables.md"), tickets=_strs(t.get("tickets"))))
    dbmap = kn("db-map.md")
    for rid, r in sorted((cat.get("db_relations") or {}).items()):
        if isinstance(r, dict) and r.get("from") and r.get("to"):
            a, b = str(r["from"]), str(r["to"])
            out.append(_unit("dblink", "%s -> %s" % (a, b), "%s → %s" % (a, b),
                             _j(r.get("kind"), str(r.get("evidence") or "")[:2000], _flat(r.get("tickets"))),
                             _first(r.get("tickets")), r.get("last_seen"),
                             "#/knowledge?tab=db&table=" + urllib.parse.quote(_table_of(a), safe=""), dbmap,
                             tickets=_strs(r.get("tickets"))))
    res = (linkmap or {}).get("result") if isinstance(linkmap, dict) else None
    for ln in (res or {}).get("links") or [] if isinstance(res, dict) else []:
        if not isinstance(ln, dict) or not ln.get("from") or not ln.get("to"):
            continue
        a, b = str(ln["from"]), str(ln["to"])
        out.append(_unit("dblink", "qa:%s -> %s" % (a, b), "%s → %s" % (a, b),
                         _j(ln.get("connection"), ln.get("evidence"),
                            "%s of %s sampled values matched" % (ln.get("matched"), ln.get("sampled"))
                            if ln.get("sampled") else ""),
                         "", linkmap.get("fetched_at"), "#/knowledge?tab=db&table=" + urllib.parse.quote(_table_of(a), safe=""),
                         dbmap))
    for k, i in sorted((cat.get("integrations") or {}).items()):
        if isinstance(i, dict):
            out.append(_unit("integration", k, i.get("system") or k, _j(i.get("artifact"), i.get("notes")),
                             _first(i.get("tickets")), i.get("last_seen"), "#/knowledge?tab=integrations",
                             kn("integrations.md"), tickets=_strs(i.get("tickets"))))
    return out


# --------------------------------------------------------------------------- sources

def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def sources():
    """{source (data-home-relative path): [absolute files]}: what the index is built from (nothing is read)."""
    h = common.home()
    out = {}
    tdir = os.path.join(h, "data", "tickets")
    for fn in _listdir(tdir):
        key = fn[:-5]
        if fn.endswith(".json") and TICKET_RE.match(key):
            out["data/tickets/" + fn] = [os.path.join(tdir, fn), os.path.join(h, "tickets", key, "requests.md")]
    sroot = os.path.join(h, "data", "db-schema")
    cards = [os.path.join(sroot, c, fn) for c in _listdir(sroot) if not c.startswith("_")
             for fn in _listdir(os.path.join(sroot, c)) if fn.endswith(".json") and not fn.startswith("_")]
    out["data/catalog.json"] = [os.path.join(h, "data", "catalog.json"), os.path.join(sroot, "_linkmap.json")] + cards
    droot = os.path.join(h, "daily")
    for y in _listdir(droot):
        for m in _listdir(os.path.join(droot, y)):
            for fn in _listdir(os.path.join(droot, y, m)):
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", fn):
                    out["daily/%s/%s/%s" % (y, m, fn)] = [os.path.join(droot, y, m, fn)]
    return out


def _sig(files):
    """Size and mtime of each file (cheap; a change means "read and hash it")."""
    parts = []
    for p in files:
        try:
            st = os.stat(p)
            parts.append("%s:%d:%d" % (os.path.basename(p), st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(os.path.basename(p) + ":-")
    return "|".join(parts)


def _read(path, limit=None):
    try:
        with open(path, "rb") as f:
            return f.read(limit) if limit else f.read()
    except OSError:
        return None


def _load(src, files):
    """(content hash, units) of one source."""
    h = hashlib.sha1()
    blobs = []
    for p in files:
        b = _read(p, DAY_TEXT_MAX if src.startswith("daily/") else None)
        h.update(b"\x00-" if b is None else b"\x00%d:" % len(b) + b)
        blobs.append(b)
    return h.hexdigest(), lambda: _units(src, files, blobs)


def _json(b):
    try:
        v = json.loads(b.decode("utf-8")) if b else None
    except (ValueError, UnicodeDecodeError):
        return None
    return v if isinstance(v, dict) else None


def _units(src, files, blobs):
    if src.startswith("data/tickets/"):
        tk = _json(blobs[0])
        if tk is None:
            return []
        md = blobs[1].decode("utf-8", "replace") if blobs[1] else ""
        return ticket_units(src[len("data/tickets/"):-5], tk, md)
    if src == "data/catalog.json":
        return catalog_units(_json(blobs[0]) or {}, _json(blobs[1]) or {}, [_json(b) for b in blobs[2:]])
    if src.startswith("daily/"):
        d = src.rsplit("/", 1)[-1][:-3]
        text = blobs[0].decode("utf-8", "replace") if blobs[0] else ""
        return [_unit("day", d, d, text, "", d, "#/day/" + d, src)]
    return []


# --------------------------------------------------------------------------- the database

_fts5 = {}


def fts5_available():
    if "ok" not in _fts5:
        try:
            con = sqlite3.connect(":memory:")
            try:
                con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
                _fts5["ok"] = True
            finally:
                con.close()
        except sqlite3.Error:
            _fts5["ok"] = False
    return _fts5["ok"]


def default_engine():
    forced = (common.env("SEARCH_ENGINE") or "").lower()
    if forced == "scan" or not fts5_available():
        return "scan"
    return "fts5"


def db_path():
    return common.wl("cache", DB_NAME)


def _create(con):
    """Drop whatever is there and create this SCHEMA's tables (FTS5 only where this SQLite has it)."""
    for virtual in (True, False):  # virtual tables first: dropping one drops its shadow tables
        for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                                     "AND name NOT LIKE 'sqlite%'").fetchall():
            if virtual == (sql or "").upper().startswith("CREATE VIRTUAL"):
                con.execute('DROP TABLE IF EXISTS "%s"' % name.replace('"', '""'))
    con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE sources(path TEXT PRIMARY KEY, sig TEXT, hash TEXT, units INTEGER)")
    con.execute("CREATE TABLE units(id INTEGER PRIMARY KEY, source TEXT, type TEXT, ref TEXT, title TEXT, body TEXT, "
                "ticket TEXT, tickets TEXT, repo TEXT, date TEXT, href TEXT, path TEXT)")
    con.execute("CREATE INDEX units_source ON units(source)")
    con.execute("CREATE INDEX units_ref ON units(type, ref)")
    fts = fts5_available()
    if fts:
        con.execute("CREATE VIRTUAL TABLE fts USING fts5(title, body, content = 'units', content_rowid = 'id', "
                    "tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3')")
    con.executemany("INSERT INTO meta(k, v) VALUES (?, ?)", [("schema", str(SCHEMA)), ("fts", "1" if fts else "0"),
                                                            ("generation", "0"), ("built_at", common.now_iso())])


def _meta(con):
    try:
        return dict(con.execute("SELECT k, v FROM meta").fetchall())
    except sqlite3.Error:
        return {}


def _schema_ok(con):
    m = _meta(con)
    return m.get("schema") == str(SCHEMA) and m.get("fts") == ("1" if fts5_available() else "0")


def _has_fts(con):
    return _meta(con).get("fts") == "1"


def _delete_source(con, src, fts):
    if fts:
        con.execute("INSERT INTO fts(fts, rowid, title, body) SELECT 'delete', id, title, body FROM units WHERE source = ?",
                    (src,))
    con.execute("DELETE FROM units WHERE source = ?", (src,))


def _insert(con, src, units, fts):
    for u in units:
        cur = con.execute("INSERT INTO units(source, %s) VALUES (?%s)" % (", ".join(_COLS), ", ?" * len(_COLS)),
                          [src] + [u[c] for c in _COLS])
        if fts:
            con.execute("INSERT INTO fts(rowid, title, body) VALUES (?, ?, ?)", (cur.lastrowid, u["title"], u["body"]))


def _sync(con, current=None, full=False):
    """Bring the index in `con` up to date with the data home. -> {"changed", "removed", "sources", "units", "ms"}."""
    t0 = time.perf_counter()
    current = sources() if current is None else current
    fts = _has_fts(con)
    known = {p: (s, h) for p, s, h in con.execute("SELECT path, sig, hash FROM sources")}
    changed = removed = 0
    with con:
        for src in set(known) - set(current):
            _delete_source(con, src, fts)
            con.execute("DELETE FROM sources WHERE path = ?", (src,))
            removed += 1
        for src, files in sorted(current.items()):
            sig = _sig(files)
            old = known.get(src)
            if old and old[0] == sig and not full:
                continue
            digest, units = _load(src, files)
            if old and old[1] == digest and not full:
                con.execute("UPDATE sources SET sig = ? WHERE path = ?", (sig, src))
                continue
            us = units()
            if old:
                _delete_source(con, src, fts)
            _insert(con, src, us, fts)
            con.execute("INSERT OR REPLACE INTO sources(path, sig, hash, units) VALUES (?, ?, ?, ?)",
                        (src, sig, digest, len(us)))
            changed += 1
        ms = int(round((time.perf_counter() - t0) * 1000))
        meta = {"updated_at": common.now_iso(), "update_ms": str(ms)}
        if changed or removed:
            meta["generation"] = str(int(_meta(con).get("generation") or 0) + 1)
        con.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", sorted(meta.items()))
    n = con.execute("SELECT COUNT(*) FROM units").fetchone()[0]
    return {"changed": changed, "removed": removed, "sources": len(current), "units": n, "ms": ms}


def _open_write():
    """The index file, ready for writing (created, or recreated when the schema differs or the file is damaged)."""
    p = db_path()
    for attempt in (1, 2):
        con = paths.sqlite_connect(p, caller="searchidx", timeout=10.0)
        try:
            con.execute("PRAGMA synchronous = NORMAL")
            if not _schema_ok(con):
                with con:
                    _create(con)
            return con
        except sqlite3.DatabaseError:
            con.close()
            if attempt == 2:
                raise
            for x in (p, p + "-journal", p + "-wal", p + "-shm"):  # a damaged cache: start over
                if os.path.exists(x):
                    paths.delete(x, caller="searchidx")


def update(full=False):
    """Re-index the sources that changed since the last update (a full rebuild with full=True). Writes only the
    index file. -> {"changed", "removed", "sources", "units", "ms", "rebuilt"}."""
    t0 = time.perf_counter()
    con = _open_write()
    try:
        rebuilt = full or not con.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        if full:
            with con:
                _create(con)
        res = _sync(con, full=full)
        if rebuilt:
            con.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)",
                            [("built_at", common.now_iso()), ("build_ms", str(res["ms"]))])
            con.commit()
    finally:
        con.close()
    invalidate()
    res["rebuilt"] = bool(rebuilt)
    res["ms"] = int(round((time.perf_counter() - t0) * 1000))
    return res


def rebuild():
    """Throw the index away and build it again from the data home."""
    return update(full=True)


# --------------------------------------------------------------------------- reading (never writes)

class View:
    """One open index for queries: the file itself (kind "disk") or an in-memory copy brought up to date ("memory").
    Every use goes through `lock` (one connection, shared by the dashboard's threads)."""

    def __init__(self, con, kind, key, home):
        self.con, self.kind, self.key, self.home = con, kind, key, home
        self.fts = _has_fts(con)
        self.lock = threading.Lock()
        self.toks = None  # the scan's token lists, built on first use

    def close(self):
        with self.lock:
            try:
                self.con.close()
            except sqlite3.Error:
                pass


_lock = threading.Lock()
_state = {"view": None, "at": 0.0}


def _open_read():
    """The index file read-only, or None when it is missing, damaged or of another schema."""
    p = db_path()
    if not os.path.isfile(p):
        return None
    try:
        con = paths.sqlite_connect(p, readonly=True, caller="searchidx")
    except sqlite3.Error:
        return None
    try:
        if _schema_ok(con):
            return con
    except sqlite3.Error:
        pass
    con.close()
    return None


def view():
    """The current View: the file while it matches the data home, else an up-to-date copy in memory. Re-checked at
    most every FRESH_S seconds per process."""
    with _lock:
        v, now, home = _state["view"], time.monotonic(), common.home()
        if v is not None and v.home == home and now - _state["at"] < FRESH_S:
            return v
        current = sources()
        sigs = {s: _sig(f) for s, f in current.items()}
        try:
            st = os.stat(db_path())
            disk_key = (st.st_mtime_ns, st.st_size)
        except OSError:
            disk_key = None
        key = (home, disk_key, hashlib.sha1(json.dumps(sorted(sigs.items())).encode("utf-8")).hexdigest())
        if v is not None and v.key == key:
            _state["at"] = now
            return v
        disk = _open_read()
        if disk is not None and dict(disk.execute("SELECT path, sig FROM sources").fetchall()) == sigs:
            new = View(disk, "disk", key, home)
        else:
            mem = sqlite3.connect(":memory:", check_same_thread=False)
            if disk is not None:
                disk.backup(mem)
                disk.close()
            else:
                with mem:
                    _create(mem)
            _sync(mem, current)
            new = View(mem, "memory", key, home)
        if v is not None:
            v.close()
        _state.update(view=new, at=now)
        return new


def invalidate():
    """Make the next search re-check the data home (after an update, or in tests)."""
    with _lock:
        _state["at"] = 0.0


def reset():
    """Close and forget the open view (tests; a data home switch)."""
    with _lock:
        v = _state["view"]
        _state.update(view=None, at=0.0)
    if v is not None:
        v.close()


def _day_arg(s, name):
    if not s:
        return None
    s = str(s).strip()
    if not DAY_RE.match(s):
        raise ValueError("bad %s date %r (use YYYY-MM-DD)" % (name, s[:20]))
    _dt.date.fromisoformat(s)  # ValueError for 2026-02-30
    return s


def _where(types, ticket, repo, frm, to):
    conds, args = ["u.type IN (%s)" % ", ".join("?" * len(types))], list(types)
    if ticket:
        conds.append("(u.ticket = ? OR instr(',' || u.tickets || ',', ',' || ? || ',') > 0)")
        args += [ticket, ticket]
    if repo:
        conds.append("instr(lower(',' || u.repo || ','), lower(',' || ? || ',')) > 0")
        args.append(repo)
    if frm:
        conds.append("u.date != '' AND u.date >= ?")
        args.append(frm)
    if to:
        conds.append("u.date != '' AND u.date <= ?")
        args.append(to)
    return " AND ".join(conds), args


def _match(v, engine, terms, where, args):
    """[(unit id, type)] best first (at most MAX_MATCHES)."""
    with v.lock:
        if engine == "fts5":
            rows = v.con.execute("SELECT u.id, u.type FROM fts JOIN units u ON u.id = fts.rowid WHERE fts MATCH ? AND %s "
                                 "ORDER BY bm25(fts, ?, 1.0), u.date DESC LIMIT ?" % where,
                                 [fts_expr(terms)] + args + [TITLE_WEIGHT, MAX_MATCHES]).fetchall()
            return rows
        if v.toks is None:
            v.toks = [(i, d or "", tokens(t), tokens(b)) for i, d, t, b in
                      v.con.execute("SELECT id, date, title, body FROM units ORDER BY id")]
        allowed = dict(v.con.execute("SELECT u.id, u.type FROM units u WHERE %s" % where, args).fetchall())
    last = len(terms) - 1
    out = []
    for i, date, tt, bt in v.toks:
        if i not in allowed:
            continue
        score = 0
        for k, term in enumerate(terms):
            p = k == last
            th = sum(1 for x in tt if _matches(x, term, p))
            bh = sum(1 for x in bt if _matches(x, term, p))
            if not th and not bh:
                break
            score += TITLE_WEIGHT * th + bh
        else:
            out.append((i, score, date))
    out.sort(key=lambda r: r[2], reverse=True)  # ties: newest first (the sort is stable)
    out.sort(key=lambda r: -r[1])
    return [(i, allowed[i]) for i, _, _ in out[:MAX_MATCHES]]


def search(q, types=None, ticket=None, repo=None, frm=None, to=None, limit=40, engine=None):
    """-> {"q", "engine", "took_ms", "total", "groups": [{"type", "label", "count", "results": [...]}]}.
    Filters: types (list), ticket (key), repo (name), frm / to (YYYY-MM-DD, inclusive; undated units drop out).
    Groups come in a fixed type order with at most 8 results each; `limit` caps the results over all groups (each
    group's best match is kept first, then the rest by rank; a group left without results is left out); `count`
    is the group's number of matches before those caps. A bad date raises ValueError."""
    t0 = time.perf_counter()
    q = (q or "")[:MAX_QUERY]
    terms = parse_query(q)
    wanted = [t for t in TYPE_ORDER if not types or t in types]
    limit = max(1, min(200, int(limit or 40)))
    frm, to = _day_arg(frm, "from"), _day_arg(to, "to")
    ticket = (ticket or "").strip().upper()[:20] or None
    repo = (repo or "").strip()[:200] or None
    engine = engine or default_engine()
    if engine == "fts5" and not fts5_available():
        engine = "scan"
    out = {"q": q, "engine": engine, "took_ms": 0, "total": 0, "groups": []}
    if not terms or not wanted:
        out["took_ms"] = int(round((time.perf_counter() - t0) * 1000))
        return out
    try:
        return _search(out, view(), q, terms, wanted, ticket, repo, frm, to, limit, engine, t0)
    except sqlite3.Error:  # the file changed under an open view (a rebuild): reopen once
        reset()
        return _search(out, view(), q, terms, wanted, ticket, repo, frm, to, limit, engine, t0)


def _search(out, v, q, terms, wanted, ticket, repo, frm, to, limit, engine, t0):
    if engine == "fts5" and not v.fts:
        out["engine"] = engine = "scan"
    where, args = _where(wanted, ticket, repo, frm, to)
    matched = _match(v, engine, terms, where, args)
    info = dict(matched)
    ranked = [i for i, _ in matched]
    m = _KEY.match(q)
    if m and "ticket" in wanted:  # an exact ticket key: that ticket first
        with v.lock:
            exact = [r[0] for r in v.con.execute("SELECT u.id FROM units u WHERE u.type = 'ticket' AND u.ref = ? AND %s"
                                                 % where, [m.group(1).upper()] + args)]
        info.update((i, "ticket") for i in exact)
        ranked = exact + [i for i in ranked if i not in exact]
    counts, cands = {}, {}
    for i in ranked:
        t = info.get(i)
        if t is None:
            continue
        counts[t] = counts.get(t, 0) + 1
        if len(cands.setdefault(t, [])) < PER_GROUP:
            cands[t].append(i)
    # the limit: every group's best match first (best first), then the rest in rank order, so many findings
    # can't crowd out the one matching ticket
    pos = {i: n for n, i in enumerate(ranked)}
    chosen = set(sorted((c[0] for c in cands.values()), key=pos.get)[:limit])
    for i in sorted((i for c in cands.values() for i in c[1:]), key=pos.get)[:max(0, limit - len(chosen))]:
        chosen.add(i)
    rows = {}
    if chosen:
        ids = sorted(chosen)
        with v.lock:
            for r in v.con.execute("SELECT id, %s FROM units WHERE id IN (%s)" % (", ".join(_COLS), ", ".join("?" * len(ids))),
                                   ids):
                rows[r[0]] = dict(zip(_COLS, r[1:]))
    out["total"] = sum(counts.values())
    out["groups"] = [{"type": t, "label": LABELS[t], "count": counts[t],
                      "results": [_result(rows[i], terms) for i in cands[t] if i in chosen and i in rows]}
                     for t in TYPE_ORDER if any(i in chosen for i in cands.get(t) or [])]
    out["took_ms"] = int(round((time.perf_counter() - t0) * 1000))
    return out


def _result(d, terms):
    text, hits = snippet(d["title"], d["body"], terms)
    return {"type": d["type"], "ref": d["ref"], "title": d["title"], "ticket": d["ticket"], "repo": d["repo"],
            "date": d["date"], "href": d["href"], "path": d["path"], "snippet": text, "hits": hits}


def stats():
    """{"engine", "docs", "built_at", "build_ms", "updated_at", "update_ms", "sources", "file", "view"}: the index
    file's numbers; "view" is how this process last read it (disk, memory or None) and "docs" counts what that view
    holds (else the file). Nothing is built or written here."""
    out = {"engine": default_engine(), "docs": 0, "built_at": None, "build_ms": None, "updated_at": None,
           "update_ms": None, "sources": 0, "file": ".worklog/cache/" + DB_NAME, "view": None}
    with _lock:
        v = _state["view"]
        v = v if v is not None and v.home == common.home() else None
    con = _open_read()
    if con is not None:
        try:
            m = _meta(con)
            out.update(docs=con.execute("SELECT COUNT(*) FROM units").fetchone()[0],
                       sources=con.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                       built_at=m.get("built_at"), build_ms=_int(m.get("build_ms")), updated_at=m.get("updated_at"),
                       update_ms=_int(m.get("update_ms")))
            if m.get("fts") != "1":
                out["engine"] = "scan"
        except sqlite3.Error:
            pass
        finally:
            con.close()
    if v is not None:
        try:
            with v.lock:
                out.update(view=v.kind, docs=v.con.execute("SELECT COUNT(*) FROM units").fetchone()[0])
        except sqlite3.Error:
            pass
    return out


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None
