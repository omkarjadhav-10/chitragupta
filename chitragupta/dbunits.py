"""Search units of the database-flow records (searchidx source "data/db"; DB-15 recall). Read-only: nothing here
queries a database, calls a model or writes a file (the index file itself is written by searchidx).

  dbobject         an object's passport summary: qualified name, kind, connection, environment, lifecycle, and its
                   column names and declared types
  dbflow           a saved flow: title, environment, label, step labels (a flow started from a ticket carries the
                   ticket key, so the personal-scope filter of every search applies to it)
  dbinvestigation  one of YOUR record investigations (owner = the confirmed identity when indexed): its flow,
                   environment, run state and the step objects with their statuses

Never indexed: row values, start keys, SQL text, objectives and notes (free text that may hold a key value).
Units are built from what the access projection lets you see when indexing (dbflows.Visible), and allowed() decides
again on every result at query time, so a revoked connection, a quarantined object or another principal's record
yields nothing (no title, count or snippet) on the very next search. A reconciliation rewrites data/db/reconcile.json,
one of this source's files, so the next index update re-projects the units with today's access.
"""
import os

from . import common

SOURCE = "data/db"
MAX_FILES = 1000
BODY_MAX = 4000


def source_files():
    """The files whose size / mtime / content decide when the database units are rebuilt ([] without data/db)."""
    root = common.hp("data", "db")
    if not os.path.isdir(root):
        return []
    files = [os.path.join(root, "graph", "nodes.json")] + [os.path.join(root, f + ".json") for f in (
        "reconcile", "revocations", "quarantine", "sources")] + [common.wl("config.json")]
    for kind in ("flows", "investigations"):
        d = os.path.join(root, kind)
        try:
            names = sorted(f for f in os.listdir(d) if f.endswith(".json"))[:MAX_FILES]
        except OSError:
            names = []
        files += [os.path.join(d, f) for f in names]
    return files


def _ctx():
    from . import dbaccess, dbflows
    cfg = common.load_config()
    ctx = dbaccess.AccessContext(cfg)
    return cfg, ctx, dbflows.Visible(ctx, cfg=cfg)


def units(unit):
    """[unit(...)] of every database record you may see now (module doc). `unit` is searchidx._unit."""
    from . import dbmodel, dbstore
    try:
        cfg, ctx, vis = _ctx()
    except Exception:  # noqa: BLE001 - no usable config: no database units rather than a failed index update
        return []
    out = []
    for nid, n in sorted(vis.nodes.items()):
        if n.get("kind") not in dbmodel.DB_KINDS:
            continue
        d = n.get("display") or {}
        cols = ["%s %s" % ((m.get("display") or {}).get("member"), (m.get("attrs") or {}).get("data_type") or "")
                for m in vis.members.get(nid) or []]
        body = " · ".join(x for x in (dbmodel.KIND_LABEL.get(n.get("kind")), str(d.get("environment") or "").upper(),
                                      d.get("connection"), n.get("lifecycle") if n.get("lifecycle") != "current" else "",
                                      "columns: " + ", ".join(cols) if cols else "") if x)
        out.append(unit("dbobject", nid, vis.label(nid), body[:BODY_MAX], href="#/db?start=node:" + nid,
                        path="data/db/graph/nodes.json"))
    for rec in dbstore.iter_records("flows", limit=MAX_FILES):
        cur = rec.get("current") or {}
        st = rec.get("start") or {}
        if not cur or not _flow_ok(ctx, vis, rec):
            continue
        key = st.get("key") if st.get("kind") == "ticket" else ""
        body = " · ".join(x for x in [cur.get("environment"), cur.get("label"), cur.get("scope_line")] +
                          [s.get("label") for s in (cur.get("steps") or [])[:60]] if x)
        out.append(unit("dbflow", rec["id"], cur.get("title") or rec["id"], body[:BODY_MAX], ticket=key or "",
                        date=str(cur.get("built_at") or "")[:10], href="#/db?flow=" + rec["id"],
                        path="data/db/flows/%s.json" % rec["id"]))
    if ctx.principal_id:
        from . import dbtrace
        for inv in dbstore.iter_records("investigations", limit=MAX_FILES):
            if not dbtrace._visible(ctx, inv):
                continue
            fl = inv.get("flow") or {}
            env = str((inv.get("scope") or {}).get("environment") or "").upper()
            run = (inv.get("runs") or [{}])[-1] if inv.get("runs") else {}
            steps = ["%s %s" % (o.get("object_label") or "", o.get("status") or "") for o in
                     (run.get("observations") or [])[:40] if isinstance(o, dict)]  # labels and statuses, no values
            title = "Investigation: %s%s" % (fl.get("title") or "a flow", " (%s)" % env if env else "")
            body = " · ".join(x for x in [run.get("state")] + steps if x)
            out.append(unit("dbinvestigation", inv["id"], title, body[:BODY_MAX],
                            date=str(inv.get("updated_at") or "")[:10],
                            href="#/db?flow=%s&tab=journey" % (fl.get("id") or ""),
                            path="data/db/investigations/%s.json" % inv["id"]))
    return out


def _flow_ok(ctx, vis, rec):
    from . import dbflows
    cur = rec.get("current") or {}
    return dbflows._flow_visible(vis, rec) and all(c in ctx.connections for c in cur.get("connections") or [])


def allowed(matches):
    """{unit row id: (type, ref)} -> the row ids you may see now (the query-time decision; module doc)."""
    if not matches:
        return set()
    from . import dbstore, dbtrace
    try:
        cfg, ctx, vis = _ctx()
    except Exception:  # noqa: BLE001 - no usable context: nothing database-related is shown
        return set()
    ok = set()
    for i, (typ, ref) in matches.items():
        if not isinstance(ref, str):
            continue
        if typ == "dbobject":
            good = ref in vis.nodes
        elif typ == "dbflow":
            rec = dbstore.load("flows", ref) if ref.startswith("f_") else None
            good = bool(rec and rec.get("current") and _flow_ok(ctx, vis, rec))
        elif typ == "dbinvestigation":
            try:
                dbtrace.load_visible(ctx, "investigations", ref)
                good = True
            except Exception:  # noqa: BLE001 - not yours, revoked or gone: not shown
                good = False
        else:
            good = False
        if good:
            ok.add(i)
    return ok
