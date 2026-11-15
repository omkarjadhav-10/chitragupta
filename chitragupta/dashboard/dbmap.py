"""The DB map: databases → schemas → tables, and one table's columns, links, writers, tickets and saved queries.
Read-only, built from what the knowledge base already holds:

  data/db-schema/_linkmap.json     the QA link map: database/schema/tables hierarchy and links with sampled/matched counts
  data/db-schema/<conn>/<schema.table>.json   schema cards: columns with type, PK, FK and PHI flags
  data/catalog.json                tables, relations, writers (read from code and sessions)
  data/tickets/<KEY>.json          saved queries and the tables they use

A link's status says what was observed, never "verified": sample_matched (the QA link map's sample matched 80 % or
more), sample_partial (30 % or more), sample_weak (under 30 %) or not_sampled (seen in code or a session only). Each
link carries its label ("Sample matched 95% (19 of 20, QA, 2026-11-05)") and basis (observed_sample: Observed in a
sample; inferred otherwise). A sample supports only the sampled relationship, not a declared constraint.
With an explicit mcp.database.scope the map shows only the cards, link-map hierarchy and sampled links of connections
approved now: a connection removed from the scope, revoked or in a blocked environment disappears here as it does from
every database view (DB-02 / DB-03).
"""
import collections
import os

from .. import common
from .api import read_json_cached, _listdir, TICKET_RE

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
    from .. import dbmodel
    return dbmodel.sample_status(sampled, matched)


def _sample(sampled, matched, lm):
    from .. import dbmodel
    if not sampled:
        return {"label": "Not sampled: seen in code or a session", "basis": "inferred",
                "basis_label": dbmodel.BASIS_LABEL["inferred"]}
    return {"label": dbmodel.sample_label(sampled, matched, (lm or {}).get("env") or "qa", (lm or {}).get("fetched_at")),
            "basis": "observed_sample", "basis_label": dbmodel.BASIS_LABEL["observed_sample"]}


def _approved():
    """The connections the map may show: with an explicit mcp.database.scope, only those approved now (a connection
    removed from the scope, revoked or in a blocked environment shows nothing, like every database view). None without
    an explicit scope: the legacy map keeps showing every card it has (it has nothing to decide with)."""
    cfg = common.load_config()
    if not ((cfg.get("mcp") or {}).get("database") or {}).get("scope"):
        return None
    from .. import dbaccess
    return set(dbaccess.approved_scope(cfg)["connections"])


def _cards(ok=None):
    """{table key: (connection, card)} from the schema cards (of approved connections when `ok` is a set)."""
    out = {}
    root = common.hp("data", "db-schema")
    for conn in _listdir(root):
        d = os.path.join(root, conn)
        if conn.startswith("_") or not os.path.isdir(d) or (ok is not None and conn not in ok):
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
    from .. import relevance
    cat = relevance.catalog_view(read_json_cached(common.hp("data", "catalog.json")) or {},
        lambda t: read_json_cached(common.hp("data", "tickets", t + ".json")))
    lm, res = _linkmap()
    ok = _approved()
    cards = _cards(ok)
    if ok is not None:  # the link map's sampled links and hierarchy of a connection not approved now are hidden too
        res = dict(res, hierarchy=[h for h in res.get("hierarchy") or [] if str(h.get("database") or "") in ok],
                   links=[l for l in res.get("links") or [] if not l.get("connection") or l.get("connection") in ok])
    if cat.get("relevance_filtered"):
        good = {_key(k) for k in cat.get("tables") or {}}
        def allowed(k):
            return any(_same(_key(k), g) for g in good)
        cards = {k: v for k, v in cards.items() if allowed(k)}
        res = dict(res, hierarchy=[dict(h, tables=[t for t in h.get("tables") or [] if allowed(str(h.get("schema")) + "." + t)])
                                  for h in res.get("hierarchy") or []],
                   links=[r for r in res.get("links") or [] if allowed(_table_of(r.get("from"), good)) and allowed(_table_of(r.get("to"), good))])
    return cat, lm, res, cards, _links(cat, res, _known(cat, res, cards), lm)


def _links(cat, res, known=(), lm=None):
    """Every known link as {from, to, from_col, to_col, kind, status, ratio, sampled, matched, label, basis,
    basis_label, evidence, tickets}."""
    out = {}
    for l in res.get("links") or []:
        a, b = _table_of(l.get("from"), known), _table_of(l.get("to"), known)
        s, m = int(l.get("sampled") or 0), int(l.get("matched") or 0)
        st, ratio = _status(s, m)
        out[(a, b)] = dict({"from": a, "to": b, "from_col": str(l.get("from") or ""), "to_col": str(l.get("to") or ""),
                            "kind": "QA link", "status": st, "ratio": ratio, "sampled": l.get("sampled"),
                            "matched": l.get("matched"), "evidence": str(l.get("evidence") or "")[:300], "tickets": []},
                           **_sample(s, m, lm))
    for r in (cat.get("db_relations") or {}).values():
        a, b = _table_of(r.get("from"), known), _table_of(r.get("to"), known)
        hit = next((v for (x, y), v in out.items() if _same(x, a) and _same(y, b) or _same(x, b) and _same(y, a)), None)
        if hit:
            hit["tickets"] = sorted(set(hit["tickets"]) | set(r.get("tickets") or []))
            hit["kind"] = r.get("kind") or hit["kind"]
            continue
        out[(a, b)] = dict({"from": a, "to": b, "from_col": str(r.get("from") or ""), "to_col": str(r.get("to") or ""),
                            "kind": r.get("kind") or "link", "status": "not_sampled", "ratio": None, "sampled": None,
                            "matched": None, "evidence": str(r.get("evidence") or "")[:300],
                            "tickets": list(r.get("tickets") or [])}, **_sample(0, 0, None))
    return list(out.values())


def tree():
    """{"databases": [{"name", "schemas": [{"name", "tables": [{"key","name","links","sample_matched","columns"}]}]}],
    "linkmap": {...} | None, "counts": {tables, links, sample_matched}}"""
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
            "key": k, "name": tn, "links": len(mine), "sample_matched": sum(1 for l in mine if l["status"] == "sample_matched"),
            "columns": any(_same(k, c) for c in cards)})
    out = [{"name": db, "schemas": [{"name": sc, "tables": sorted(ts, key=lambda t: t["name"].lower())}
                                    for sc, ts in sorted(scs.items())]} for db, scs in sorted(dbs.items(), key=lambda x: (x[0] == UNPLACED, x[0].lower()))]
    return {"databases": out, "counts": {"tables": len(place), "links": len(links),
                                         "sample_matched": sum(1 for l in links if l["status"] == "sample_matched")},
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


# --------------------------------------------------------------------------- database investigation (database flow D1)

VIEW_ROUTES = ("/api/db/workspace", "/api/db/passport", "/api/db/flows", "/api/db/flow", "/api/db/resolve",
               "/api/db/investigations", "/api/db/investigation", "/api/db/plan", "/api/db/job", "/api/db/handoff",
               "/api/db/context", "/api/db/impact", "/api/db/impacts", "/api/db/compare", "/api/db/comparisons",
               "/api/db/snapshots")


def _start_arg(start):
    kind, _, ref = str(start or "").partition(":")
    if kind == "ticket":
        return {"kind": "ticket", "key": ref.upper()}
    if kind == "node":
        return {"kind": "node", "id": ref}
    return None


def _d3_errors():
    from .. import dbimpact
    return dbimpact.ImpactError, dbimpact.CompareError


def view(path, q, cfg=None):
    """(status, body) of one database-investigation GET route, from dbviews (cached records only: no query, no
    ingestion, nothing written). `q(name)` reads a query parameter. Bad arguments and cursors answer 400, an unknown
    or hidden record 404 (the same answer: a record you may not see is not confirmed to exist)."""
    from .. import dbaccess, dbflows, dbtrace, dbviews
    try:
        ctx = dbaccess.AccessContext(cfg)
        cur = q("cursor")[:4096] or None
        if path == "/api/db/workspace":
            return 200, dbviews.workspace_summary(ctx, cfg)
        if path == "/api/db/passport":
            return 200, dbviews.table_view(ctx, q("id")[:200], env=q("env")[:20] or None, cursor=cur, cfg=cfg)
        if path == "/api/db/flows":
            return 200, dbviews.flows_list(ctx, cursor=cur, cfg=cfg)
        if path == "/api/db/flow":
            if q("id"):
                return 200, dbviews.flow_view(ctx, fid=q("id")[:64], cursor=cur, cfg=cfg)
            start = q("start")[:240]
            kind, _, ref = start.partition(":")
            if kind == "ticket":
                return 200, dbviews.flow_view(ctx, start={"kind": "ticket", "key": ref.upper()},
                                              env=q("env")[:20] or None, cursor=cur, cfg=cfg)
            if kind == "node":
                return 200, dbviews.flow_view(ctx, start={"kind": "node", "id": ref}, env=q("env")[:20] or None,
                                              cursor=cur, cfg=cfg)
            return 400, {"error": "give id=<f_…> or start=ticket:<KEY> | node:<ID>"}
        if path == "/api/db/resolve":
            return 200, dbviews.resolve(ctx, q("q")[:200], q("kind")[:10] or None, cursor=cur, cfg=cfg)
        # record investigations: personal, re-checked on every request; nothing here plans, runs or polls a provider
        if path == "/api/db/investigations":
            return 200, dbviews.investigations_list(ctx, q("q")[:200] or None, cursor=cur)
        if path == "/api/db/investigation":
            return 200, dbviews.investigation_view(ctx, q("id")[:64], cursor=cur)
        if path == "/api/db/plan":
            if q("id"):
                return 200, dbviews.plan_view(ctx, q("id")[:64], cursor=cur)
            if q("flow"):
                return 200, dbviews.key_spec_view(ctx, flow_id=q("flow")[:64], cfg=cfg)
            start = _start_arg(q("start")[:240])
            if start:
                return 200, dbviews.key_spec_view(ctx, start=start, env=q("env")[:20] or None, cfg=cfg)
            return 400, {"error": "give id=<p_…>, flow=<f_…> or start=ticket:<KEY> | node:<ID>"}
        if path == "/api/db/job":
            return 200, dbviews.job_view(ctx, q("id")[:64])
        if path == "/api/db/handoff":
            return 200, dbviews.handoff_view(ctx, q("id")[:64])
        if path == "/api/db/context":
            return 200, dbviews.context_view(ctx, q("flow")[:64] or None, q("investigation")[:64] or None, cfg=cfg)
        # D3: cached impact / comparison results (computed only by cli db impact|compare; never on a read)
        if path == "/api/db/impacts":
            return 200, dbviews.impacts_list(ctx, cursor=cur, cfg=cfg)
        if path == "/api/db/impact":
            if q("id"):
                return 200, dbviews.impact_view(ctx, rid=q("id")[:64], cursor=cur, cfg=cfg)
            if not q("target"):
                return 400, {"error": "give id=<x_…> or target=<o_…|c_…|schema.table[.column]> [&change=kind]"}
            return 200, dbviews.impact_view(ctx, target=q("target")[:200], change=q("change")[:20] or None,
                                            cursor=cur, cfg=cfg, env=q("env")[:20] or None,
                                            connection=q("connection")[:80] or None)
        if path == "/api/db/comparisons":
            return 200, dbviews.comparisons_list(ctx, cursor=cur, cfg=cfg)
        if path == "/api/db/compare":
            if q("id"):
                return 200, dbviews.compare_view(ctx, rid=q("id")[:64], cursor=cur, cfg=cfg)
            if not q("left") or not q("right"):
                return 400, {"error": "give id=<x_…> or left=<connection[@snapshot]>&right=<…>"}
            objs = [o for o in q("objects")[:2000].split(",") if o.strip()] or None
            return 200, dbviews.compare_view(ctx, q("left")[:100], q("right")[:100], objects=objs, cursor=cur, cfg=cfg)
        if path == "/api/db/snapshots":
            return 200, dbviews.snapshots_view(ctx, q("connection")[:80], cursor=cur)
    except (dbviews.ViewError, dbviews.CursorError, dbflows.FlowError, dbtrace.PlanError) + _d3_errors() as e:
        return 400, {"error": str(e)[:300]}
    except dbflows.NotFound as e:
        return 404, {"error": str(e)[:300]}
    return 404, {"error": "not found"}
