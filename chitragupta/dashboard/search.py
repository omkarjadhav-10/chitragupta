"""Full-text search over the data home for the dashboard: tickets, findings, queries, artifacts, APIs, tables,
integrations and daily files.

The index lives in memory only (an in-memory SQLite database, never a file) and is rebuilt when the dashboard's
change cursor moves. Two engines answer the same way:
  fts5  SQLite FTS5 (unicode61 tokenizer, diacritics removed, prefix indexes), ranked by bm25 with the title
        weighted 10x the body
  scan  pure Python over the same documents, for a SQLite built without FTS5 (or CHITRAGUPTA_SEARCH_ENGINE=scan)

Query rules (shared): lowercase, split on anything that is not a letter or digit ("ABC-1001" -> "abc", "1001"),
at most 8 terms and 200 characters; every term must match a whole token except the last, which matches as a
prefix. Terms are passed to FTS5 as quoted strings, so user input can never become FTS syntax. A query that is
exactly a ticket key puts that ticket first. Snippets are plain text with hit offsets; the page escapes them.
"""
import os
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.parse

from .. import common
from . import api

TYPES = [("ticket", "Tickets"), ("finding", "Findings"), ("query", "Queries"), ("artifact", "Artifacts"),
         ("api", "APIs"), ("table", "Tables"), ("integration", "Integrations"), ("day", "Days")]
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
_TOKEN = re.compile(r"[^\W_]+")
_KEY = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]{1,9}-\d{1,6})\s*$")


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


# --------------------------------------------------------------------------- documents

def _j(*parts):
    return " ".join(str(p) for p in parts if p not in (None, "", [], {}))


def _flat(xs):
    return " ".join(str(x) for x in xs or [] if x) if isinstance(xs, list) else (str(xs) if xs else "")


def _doc(type_, ref, title, body, ticket="", date="", href=""):
    return {"type": type_, "ref": ref, "title": (title or "").strip()[:300], "body": (body or "")[:BODY_MAX],
            "ticket": ticket or "", "date": (date or "")[:10], "href": href}


def documents():
    """Every searchable document in the data home (read-only)."""
    docs = []
    tdir = common.hp("data", "tickets")
    for fn in api._listdir(tdir):
        key = fn[:-5]
        if not fn.endswith(".json") or not api.TICKET_RE.match(key):
            continue
        tk = api.read_json_cached(os.path.join(tdir, fn))
        if not isinstance(tk, dict):
            continue
        status = tk.get("status") or ((tk.get("jira") or {}).get("status") or "")
        repos = [os.path.basename(str(r).rstrip("/\\")) for r in tk.get("repos") or []]
        docs.append(_doc("ticket", key, _j(key, tk.get("title")), _j(tk.get("tldr"), tk.get("problem"), status, *repos),
                         key, tk.get("last") or tk.get("first"), "#/tickets/" + key))
        for iid, it in sorted((tk.get("items") or {}).items()):
            if isinstance(it, dict):
                docs.append(_doc("finding", "%s/%s" % (key, iid), it.get("title"),
                                 _j(it.get("detail"), _flat(it.get("evidence")), it.get("kind"), key), key, it.get("date"),
                                 "#/tickets/%s?tab=overview" % key))
        for qid, q in sorted((tk.get("queries") or {}).items()):
            if isinstance(q, dict):
                docs.append(_doc("query", "%s/%s" % (key, qid), q.get("purpose") or "Query",
                                 _j(q.get("text"), _flat(q.get("tables")), key), key, q.get("date"),
                                 "#/tickets/%s?tab=queries" % key))
        for aid, a in sorted((tk.get("artifacts") or {}).items()):
            if isinstance(a, dict):
                docs.append(_doc("artifact", "%s/%s" % (key, aid), a.get("name") or aid,
                                 _j(a.get("purpose"), a.get("kind"), key), key, a.get("last") or a.get("date"),
                                 "#/tickets/%s?tab=artifacts" % key))
    cat = api.read_json_cached(common.hp("data", "catalog.json")) or {}
    for k, a in sorted((cat.get("apis") or {}).items()):
        if isinstance(a, dict):
            path = a.get("path") or ""
            docs.append(_doc("api", k, _j((a.get("method") or "").upper(), path),
                             _j(a.get("host_or_service") or a.get("host"), a.get("purpose"), _flat(a.get("request_fields")),
                                _flat(a.get("response_fields"))),
                             _first(a.get("tickets")), a.get("last_seen"),
                             "#/knowledge?tab=apis&q=" + urllib.parse.quote(path, safe="")))
    cols, bare = api._schema_columns()
    for k, t in sorted((cat.get("tables") or {}).items()):
        if isinstance(t, dict):
            name = t.get("name") or k
            c = cols.get(name.lower()) or bare.get(name.lower().rsplit(".", 1)[-1]) or t.get("key_columns") or []
            docs.append(_doc("table", name, name, _j(t.get("db"), t.get("purpose"), _flat(c)), _first(t.get("tickets")),
                             t.get("last_seen"), "#/knowledge?tab=db&table=" + urllib.parse.quote(name, safe="")))
    for k, i in sorted((cat.get("integrations") or {}).items()):
        if isinstance(i, dict):
            docs.append(_doc("integration", k, i.get("system") or k, _j(i.get("artifact"), i.get("notes")),
                             _first(i.get("tickets")), i.get("last_seen"), "#/knowledge?tab=integrations"))
    droot = common.hp("daily")
    for y in api._listdir(droot):
        for dp, _, fns in os.walk(os.path.join(droot, y)):
            for fn in fns:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", fn):
                    d = fn[:-3]
                    docs.append(_doc("day", d, d, _head(os.path.join(dp, fn)), "", d, "#/day/" + d))
    return docs


def _first(xs):
    return str(xs[0]) if isinstance(xs, list) and xs else ""


def _head(path, limit=DAY_TEXT_MAX):
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- engines

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


class Index:
    """One built index: the documents plus either an FTS5 table or token lists for the scan."""

    def __init__(self, engine, docs, cursor):
        self.engine, self.docs, self.cursor = engine, docs, cursor
        self.built_at = common.now_iso()
        self.con = None
        self.toks = None
        t0 = time.perf_counter()
        if engine == "fts5":
            self.con = sqlite3.connect(":memory:", check_same_thread=False)  # only used under Index lock
            self.con.execute("CREATE VIRTUAL TABLE docs USING fts5(title, body, "
                             "tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3')")
            self.con.executemany("INSERT INTO docs(rowid, title, body) VALUES (?, ?, ?)",
                                 ((i, d["title"], d["body"]) for i, d in enumerate(docs)))
        else:
            self.toks = [(tokens(d["title"]), tokens(d["body"])) for d in docs]
        self.build_ms = int(round((time.perf_counter() - t0) * 1000))
        self.lock = threading.Lock()

    def close(self):
        if self.con is not None:
            self.con.close()

    def match(self, terms):
        """[(doc index, score)] best first; score: higher is better."""
        if self.engine == "fts5":
            with self.lock:
                rows = self.con.execute("SELECT rowid, bm25(docs, ?, 1.0) FROM docs WHERE docs MATCH ? "
                                        "ORDER BY bm25(docs, ?, 1.0) LIMIT ?",
                                        (TITLE_WEIGHT, fts_expr(terms), TITLE_WEIGHT, MAX_MATCHES)).fetchall()
            return [(i, -s) for i, s in rows]
        last = len(terms) - 1
        out = []
        for i, (tt, bt) in enumerate(self.toks):
            score = 0
            for k, term in enumerate(terms):
                p = k == last
                th = sum(1 for x in tt if _matches(x, term, p))
                bh = sum(1 for x in bt if _matches(x, term, p))
                if not th and not bh:
                    break
                score += TITLE_WEIGHT * th + bh
            else:
                out.append((i, score))
        out.sort(key=lambda r: self.docs[r[0]]["date"], reverse=True)  # ties: newest first (the sort is stable)
        out.sort(key=lambda r: -r[1])
        return out[:MAX_MATCHES]


_lock = threading.Lock()
_indexes = {}  # engine -> Index
_last = {"engine": None}


def index(engine=None):
    """The current index for an engine, rebuilt when the dashboard's change cursor (or the data home) moved."""
    engine = engine or default_engine()
    if engine == "fts5" and not fts5_available():
        engine = "scan"
    cursor = "%s|%s" % (common.home(), api.changes()["cursor"])
    with _lock:
        ix = _indexes.get(engine)
        if ix is None or ix.cursor != cursor:
            t0 = time.perf_counter()
            new = Index(engine, documents(), cursor)
            new.build_ms = int(round((time.perf_counter() - t0) * 1000))  # reading the documents included
            if ix is not None:
                ix.close()
            _indexes[engine] = ix = new
        _last["engine"] = engine
        return ix


def reset():
    """Drop every built index (tests)."""
    with _lock:
        for ix in _indexes.values():
            ix.close()
        _indexes.clear()


def stats():
    """{"engine", "docs", "built_at", "build_ms"} of the last index used (nothing is built here)."""
    with _lock:
        engine = _last["engine"] or default_engine()
        ix = _indexes.get(engine)
    if ix is None:
        return {"engine": engine, "docs": 0, "built_at": None, "build_ms": None}
    return {"engine": ix.engine, "docs": len(ix.docs), "built_at": ix.built_at, "build_ms": ix.build_ms}


# --------------------------------------------------------------------------- search

def search(q, types=None, limit=40, engine=None):
    """-> {"q", "engine", "took_ms", "total", "groups": [{"type", "label", "count", "results": [...]}]}.
    Groups come in a fixed type order with at most 8 results each; `limit` caps the results over all groups (each
    group's best match is kept first, then the rest by rank; a group left without results is left out); `count`
    is the group's number of matches before those caps."""
    t0 = time.perf_counter()
    q = (q or "")[:MAX_QUERY]
    terms = parse_query(q)
    wanted = [t for t in TYPE_ORDER if not types or t in types]
    limit = max(1, min(200, int(limit or 40)))
    ix = index(engine)
    out = {"q": q, "engine": ix.engine, "took_ms": 0, "total": 0, "groups": []}
    if not terms or not wanted:
        out["took_ms"] = int(round((time.perf_counter() - t0) * 1000))
        return out
    ranked = [i for i, _ in ix.match(terms) if ix.docs[i]["type"] in wanted]
    m = _KEY.match(q)
    if m:  # an exact ticket key: that ticket first
        key = m.group(1).upper()
        exact = [i for i, d in enumerate(ix.docs) if d["type"] == "ticket" and d["ref"] == key and "ticket" in wanted]
        ranked = exact + [i for i in ranked if i not in exact]
    counts, cands = {}, {}
    for i in ranked:
        t = ix.docs[i]["type"]
        counts[t] = counts.get(t, 0) + 1
        if len(cands.setdefault(t, [])) < PER_GROUP:
            cands[t].append(i)
    # the limit: every group's best match first (best first), then the rest in rank order, so many findings
    # can't crowd out the one matching ticket
    pos = {i: n for n, i in enumerate(ranked)}
    chosen = set(sorted((c[0] for c in cands.values()), key=pos.get)[:limit])
    for i in sorted((i for c in cands.values() for i in c[1:]), key=pos.get)[:max(0, limit - len(chosen))]:
        chosen.add(i)
    out["total"] = len(ranked)
    out["groups"] = [{"type": t, "label": LABELS[t], "count": counts[t],
                      "results": [_result(ix.docs[i], terms) for i in cands[t] if i in chosen]}
                     for t in TYPE_ORDER if any(i in chosen for i in cands.get(t) or [])]
    out["took_ms"] = int(round((time.perf_counter() - t0) * 1000))
    return out


def _result(d, terms):
    text, hits = snippet(d["title"], d["body"], terms)
    return {"type": d["type"], "ref": d["ref"], "title": d["title"], "ticket": d["ticket"], "date": d["date"],
            "href": d["href"], "snippet": text, "hits": hits}
