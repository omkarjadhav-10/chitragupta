"""The DB map: databases → schemas → tables, and one table's columns, links, writers, tickets and saved queries.
Read-only, built from what the knowledge base already holds:

  data/db-schema/_linkmap.json     the QA link map: database/schema/tables hierarchy and links with sampled/matched counts
  data/db-schema/<conn>/<schema.table>.json   schema cards: columns with type, PK, FK and PHI flags
  data/catalog.json                tables, relations, writers (read from code and sessions)
  data/tickets/<KEY>.json          saved queries and the tables they use

A link's status: verified (matched/sampled ≥ 0.8 in QA), likely (≥ 0.3), unverified (seen in code or a session, or
under 0.3). The same thresholds as knowledge/db-map.md.
"""
import collections
import os

from .. import common
from .api import read_json_cached, _listdir, TICKET_RE

VERIFIED, LIKELY = 0.8, 0.3
NO_SCHEMA = "(no schema)"
UNPLACED = "Seen in code or sessions"


def _key(name):
    return str(name or "").strip().strip('"[]').lower()


def _table_of(ref, known=()):
    """'schema.table.column', 'schema.table', 'table.column' or 'table' -> the table key. Two parts are read as
    schema.table unless only the first part names a table we know."""
    parts = _key(ref).split(".")
    if len(parts) >= 3:
        return ".".join(parts[:-1])
    whole = ".".join(parts)
    if len(parts) == 2 and not any(_same(whole, k) for k in known) and any(_same(parts[0], k) for k in known):
        return parts[0]
    return whole


def _same(a, b):
    """Two table keys name the same table: equal, or one is the other without its schema."""
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _status(sampled, matched):
    if not sampled:
        return "unverified", None
    r = matched / float(sampled)
    return ("verified" if r >= VERIFIED else "likely" if r >= LIKELY else "unverified"), round(r, 2)


def _cards():
    """{table key: (connection, card)} from the schema cards."""
    out = {}
    root = common.hp("data", "db-schema")
    for conn in _listdir(root):
        d = os.path.join(root, conn)
        if conn.startswith("_") or not os.path.isdir(d):
            continue
        for fn in _listdir(d):
            if fn.endswith(".json"):
                card = read_json_cached(os.path.join(d, fn)) or {}
                out[_key(fn[:-5])] = (conn, card)
    return out


def _linkmap():
    lm = read_json_cached(common.hp("data", "db-schema", "_linkmap.json")) or {}
    return lm, (lm.get("result") or {}) if isinstance(lm, dict) else {}


def _known(cat, res, cards):
    out = {_key("%s.%s" % (h.get("schema"), t)) for h in res.get("hierarchy") or [] for t in h.get("tables") or []}
    out.update(cards)
    out.update(_key(t.get("name") or n) for n, t in (cat.get("tables") or {}).items())
    return out


def _load():
    cat = read_json_cached(common.hp("data", "catalog.json")) or {}
    lm, res = _linkmap()
    cards = _cards()
    return cat, lm, res, cards, _links(cat, res, _known(cat, res, cards))


def _links(cat, res, known=()):
    """Every known link as {from, to, from_col, to_col, kind, status, ratio, sampled, matched, evidence, tickets}."""
    out = {}
    for l in res.get("links") or []:
        a, b = _table_of(l.get("from"), known), _table_of(l.get("to"), known)
        st, ratio = _status(int(l.get("sampled") or 0), int(l.get("matched") or 0))
        out[(a, b)] = {"from": a, "to": b, "from_col": str(l.get("from") or ""), "to_col": str(l.get("to") or ""),
                       "kind": "QA link", "status": st, "ratio": ratio, "sampled": l.get("sampled"),
                       "matched": l.get("matched"), "evidence": str(l.get("evidence") or "")[:300], "tickets": []}
    for r in (cat.get("db_relations") or {}).values():
        a, b = _table_of(r.get("from"), known), _table_of(r.get("to"), known)
        hit = next((v for (x, y), v in out.items() if _same(x, a) and _same(y, b) or _same(x, b) and _same(y, a)), None)
        if hit:
            hit["tickets"] = sorted(set(hit["tickets"]) | set(r.get("tickets") or []))
            hit["kind"] = r.get("kind") or hit["kind"]
            continue
        out[(a, b)] = {"from": a, "to": b, "from_col": str(r.get("from") or ""), "to_col": str(r.get("to") or ""),
                       "kind": r.get("kind") or "link", "status": "unverified", "ratio": None, "sampled": None,
                       "matched": None, "evidence": str(r.get("evidence") or "")[:300], "tickets": list(r.get("tickets") or [])}
    return list(out.values())


def tree():
    """{"databases": [{"name", "schemas": [{"name", "tables": [{"key","name","links","verified","columns"}]}]}],
    "linkmap": {...} | None, "counts": {...}}"""
    cat, lm, res, cards, links = _load()
    place = collections.OrderedDict()  # table key -> (database, schema, display name)
    for h in res.get("hierarchy") or []:
        db, sc = str(h.get("database") or "?"), str(h.get("schema") or NO_SCHEMA)
        for t in h.get("tables") or []:
            place.setdefault(_key("%s.%s" % (sc, t)), (db, sc, str(t)))
    for k, (conn, card) in cards.items():
        if not any(_same(k, p) for p in place):
            sc, _, t = k.rpartition(".")
            place[k] = (conn, sc or NO_SCHEMA, t)
    seen = collections.defaultdict(set)
    for db, sc, _ in place.values():
        seen[sc.lower()].add(db)
    home = {sc: next(iter(d)) for sc, d in seen.items() if len(d) == 1}  # schema -> the one database it was seen in
    for name, t in (cat.get("tables") or {}).items():
        k = _key(t.get("name") or name)
        if any(_same(k, p) for p in place):
            continue
        sc, _, tn = k.rpartition(".")
        # the catalog's own db label is free text from sessions; a schema we already placed decides the database
        place[k] = (home.get(sc) or UNPLACED, sc or NO_SCHEMA, tn)
    dbs = collections.OrderedDict()
    for k, (db, sc, tn) in place.items():
        mine = [l for l in links if _same(l["from"], k) or _same(l["to"], k)]
        dbs.setdefault(db, collections.OrderedDict()).setdefault(sc, []).append({
            "key": k, "name": tn, "links": len(mine), "verified": sum(1 for l in mine if l["status"] == "verified"),
            "columns": any(_same(k, c) for c in cards)})
    out = [{"name": db, "schemas": [{"name": sc, "tables": sorted(ts, key=lambda t: t["name"].lower())}
                                    for sc, ts in sorted(scs.items())]} for db, scs in sorted(dbs.items(), key=lambda x: (x[0] == UNPLACED, x[0].lower()))]
    return {"databases": out, "counts": {"tables": len(place), "links": len(links),
                                         "verified": sum(1 for l in links if l["status"] == "verified")},
            "linkmap": {"env": lm.get("env"), "fetched_at": lm.get("fetched_at")} if res else None}


def graph():
    """Every recorded link, for the whole-map diagram."""
    links = _load()[4]
    return {"graph": {"nodes": sorted({n for l in links for n in (l["from"], l["to"])}), "edges": links, "hops": None}}


def _queries(k):
    out = []
    d = common.hp("data", "tickets")
    for fn in _listdir(d):
        key = fn[:-5]
        if not fn.endswith(".json") or not TICKET_RE.match(key):
            continue
        tk = read_json_cached(os.path.join(d, fn)) or {}
        for qid, q in (tk.get("queries") or {}).items():
            if any(_same(_key(t), k) for t in q.get("tables") or []):
                out.append({"ticket": key, "id": qid, "purpose": q.get("purpose") or "", "dialect": q.get("dialect") or "",
                            "date": q.get("date") or "", "text": str(q.get("text") or "")[:400]})
    return sorted(out, key=lambda q: q["date"], reverse=True)[:30]


def table(key, hops=1):
    """One table's panel plus its neighbourhood graph (1 or 2 hops), or None when unknown."""
    k = _key(key)
    t = tree()
    every = [(db["name"], sc["name"], tb) for db in t["databases"] for sc in db["schemas"] for tb in sc["tables"]]
    hits = [x for x in every if x[2]["key"] == k] or [x for x in every if _same(x[2]["key"], k)]
    if len(hits) != 1:  # unknown, or a bare table name that more than one schema has
        return None
    found = hits[0]
    k = found[2]["key"]
    cat, _, _, cards, links = _load()
    card = next((c for ck, c in cards.items() if _same(ck, k)), None)
    columns = []
    if card:
        for c in (card[1].get("describe") or []):
            if any(x["name"] == c.get("name") for x in columns):  # some describes list a column once per constraint
                continue
            columns.append({"name": c.get("name"), "type": c.get("dataType"), "pk": bool(c.get("isPrimaryKey")),
                            "fk": bool(c.get("isForeignKey")), "phi": bool(c.get("isPhiField")), "nullable": c.get("nullable")})
    ct = next((v for n, v in (cat.get("tables") or {}).items() if _same(_key(v.get("name") or n), k)), {}) or {}
    writers = {"reads": [], "writes": []}
    for n, w in (cat.get("db_writers") or {}).items():
        if _same(_key(w.get("table") or n), k):
            writers["reads"] += [{"component": r.get("component"), "tickets": r.get("tickets") or []} for r in (w.get("reads") or {}).values()]
            writers["writes"] += [{"component": x.get("component"), "column": x.get("column"), "change": x.get("change"),
                                   "tickets": x.get("tickets") or []} for x in (w.get("writes") or {}).values()]
    mine = [l for l in links if _same(l["from"], k) or _same(l["to"], k)]
    nodes, edges = {k}, list(mine)
    if hops >= 2:
        for l in mine:
            other = l["to"] if _same(l["from"], k) else l["from"]
            for l2 in links:
                if l2 not in edges and (_same(l2["from"], other) or _same(l2["to"], other)):
                    edges.append(l2)
    for e in edges:
        nodes.update([e["from"], e["to"]])
    tickets = sorted(set(ct.get("tickets") or []) | {x for l in mine for x in l["tickets"]})
    return {"key": k, "database": found[0], "schema": found[1], "name": found[2]["name"], "purpose": ct.get("purpose") or "",
            "connection": card[0] if card else None, "fetched_at": card[1].get("fetched_at") if card else None,
            "columns": columns, "key_columns": ct.get("key_columns") or [], "links": mine, "writers": writers,
            "tickets": tickets, "queries": _queries(k), "graph": {"nodes": sorted(nodes), "edges": edges, "hops": hops}}
