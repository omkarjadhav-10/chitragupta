"""Evidence Graph index: a derived, rebuildable SQLite file at <home>/.worklog/cache/evidence-graph.sqlite.

The store keeps rows; it does not know how a graph is built. Storage is per ticket (`PRIMARY KEY (ticket, id)`), so
a node two tickets share is stored once under each and every join is on `(ticket, id)`. Readers never write.
"""
import json
import os
import sqlite3

from . import common, graphmodel
from .safety import paths

SCHEMA = 1    # the tables below
BUILDER = 1   # what the builder puts in them; a bump rebuilds every ticket
DB_NAME = "evidence-graph.sqlite"
CALLER = "graphstore"

NODE_COLS = ("id", "type", "label", "lifecycle", "version", "repo_id", "location", "workflow", "observed_at",
             "source_ref", "attrs")
EDGE_COLS = ("id", "type", "src", "dst", "basis", "lifecycle", "scope", "confidence", "evidence_ref", "observed_at",
             "version", "workflow", "attrs", "generation")
STATUS_COLS = ("requirement", "workflow", "location", "revision", "alias", "ordinal", "status", "plan",
               "implementation", "verification", "detail")
SOURCE_COLS = ("sig", "content", "generation", "built_at", "profile_generation", "snapshot", "coverage")
_TABLES = (
    "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE sources(ticket TEXT PRIMARY KEY, sig TEXT, content TEXT, generation INTEGER, built_at TEXT, "
    "profile_generation TEXT, snapshot TEXT, coverage TEXT)",
    "CREATE TABLE nodes(ticket TEXT, id TEXT, type TEXT, label TEXT, lifecycle TEXT, version TEXT, repo_id TEXT, "
    "location TEXT, workflow TEXT, observed_at TEXT, source_ref TEXT, attrs TEXT, generation INTEGER, "
    "PRIMARY KEY (ticket, id))",
    "CREATE TABLE edges(ticket TEXT, id TEXT, type TEXT, src TEXT, dst TEXT, basis TEXT, lifecycle TEXT, scope TEXT, "
    "confidence REAL, evidence_ref TEXT, observed_at TEXT, version TEXT, workflow TEXT, attrs TEXT, "
    "generation INTEGER, PRIMARY KEY (ticket, id))",
    "CREATE TABLE requirements(ticket TEXT, requirement TEXT, workflow TEXT, location TEXT, revision TEXT, "
    "alias TEXT, ordinal INTEGER, status TEXT, plan TEXT, implementation TEXT, verification TEXT, detail TEXT, "
    "PRIMARY KEY (ticket, requirement, workflow))",
    "CREATE INDEX nodes_type ON nodes(ticket, type)",
    "CREATE INDEX edges_type ON edges(ticket, type)",
    "CREATE INDEX edges_src ON edges(src)",
    "CREATE INDEX edges_dst ON edges(dst)",
)
_READ = {}   # db path -> the cached read-only connection


def db_path():
    return common.wl("cache", DB_NAME)


def reset():
    """Close the cached read connection (tests' cleanup; the writer before it replaces or deletes the file)."""
    for con in list(_READ.values()):
        try:
            con.close()
        except sqlite3.Error:
            pass
    _READ.clear()


def _dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _create(con):
    """Drop whatever is there and create this SCHEMA's tables."""
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                               "AND name NOT LIKE 'sqlite%'").fetchall():
        con.execute('DROP TABLE IF EXISTS "%s"' % name.replace('"', '""'))
    for sql in _TABLES:
        con.execute(sql)
    con.executemany("INSERT INTO meta(k, v) VALUES (?, ?)", [("schema", str(SCHEMA)), ("builder", str(BUILDER)),
                                                            ("generation", "0"), ("built_at", common.now_iso())])


def meta(con):
    try:
        return dict(con.execute("SELECT k, v FROM meta").fetchall())
    except sqlite3.Error:
        return {}


def set_meta(con, **values):
    con.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", [(k, str(v)) for k, v in values.items()])


def _schema_ok(con):
    m = meta(con)
    return m.get("schema") == str(SCHEMA) and m.get("builder") == str(BUILDER)


def open_write(timeout=2.0):
    """The index file, ready for writing: created, recreated on a schema or builder change, replaced when damaged."""
    p = db_path()
    for attempt in (1, 2):
        con = paths.sqlite_connect(p, caller=CALLER, timeout=timeout)
        try:
            con.execute("PRAGMA synchronous = NORMAL")
            if not _schema_ok(con):
                reset()
                with con:
                    _create(con)
            return con
        except sqlite3.OperationalError:   # locked or busy: another process holds it; never forced
            con.close()
            raise
        except sqlite3.DatabaseError:
            con.close()
            if attempt == 2:
                raise
            reset()
            for x in (p, p + "-journal"):   # a damaged cache: start over
                if os.path.exists(x):
                    paths.delete(x, caller=CALLER)


def open_read():
    """The index read-only (cached per home), or None when it is missing, damaged or on another schema."""
    p = db_path()
    con = _READ.pop(p, None)
    if not os.path.isfile(p):
        if con is not None:
            con.close()
        return None
    while True:
        fresh = con is None
        if fresh:
            try:
                con = paths.sqlite_connect(p, readonly=True, caller=CALLER)
            except sqlite3.Error:
                return None
        try:
            if _schema_ok(con):
                _READ[p] = con
                return con
        except sqlite3.Error:
            pass
        con.close()
        con = None
        if fresh:   # a cached connection gets one reopen: the writer may have replaced the file
            return None


def _generation(con):
    return int(meta(con).get("generation") or 0)


def _stored(con, table, key):
    return dict(con.execute("SELECT id, version FROM %s WHERE ticket = ?" % table, (key,)).fetchall())


def _sync(con, table, cols, key, rows, generation):
    """Delete this ticket's rows that are gone, write those that are new or whose version changed."""
    have, changed = _stored(con, table, key), 0
    want = {}
    for row in rows:
        if row.get("ticket") != key:
            raise ValueError("row %s belongs to ticket %r, not %r" % (row.get("id"), row.get("ticket"), key))
        want[row["id"]] = row
    for gone in sorted(set(have) - set(want)):
        con.execute("DELETE FROM %s WHERE ticket = ? AND id = ?" % table, (key, gone))
        changed += 1
    names = [c for c in cols if c != "generation"]
    sql = "INSERT OR REPLACE INTO %s(ticket, %s, generation) VALUES (%s)" % (
        table, ", ".join(names), ", ".join("?" * (len(names) + 2)))
    for rid in sorted(want):
        row = want[rid]
        if have.get(rid) == row["version"]:
            continue
        values = [_dump(row.get(c) or {}) if c == "attrs" else row.get(c) for c in names]
        con.execute(sql, [key] + values + [generation])
        changed += 1
    return changed


def _status_values(row):
    detail = row.get("detail")
    return tuple([row["requirement"], row.get("workflow") or "", row.get("location") or "", row.get("revision") or "",
                  row.get("alias") or "", row.get("ordinal"), row.get("status") or "", row.get("plan") or "",
                  row.get("implementation") or "", row.get("verification") or "", _dump(detail if detail else {})])


def _statuses(con, key):
    return con.execute("SELECT %s FROM requirements WHERE ticket = ? ORDER BY requirement, workflow"
                       % ", ".join(STATUS_COLS), (key,)).fetchall()


def write_ticket(con, key, graph, meta=None):
    """Make the index hold exactly `graph` = {nodes, edges, statuses} for this ticket, inside the caller's
    transaction. Unchanged rows are not rewritten; the generation moves only when a row did."""
    meta = meta or {}
    nodes, edges, statuses = graph.get("nodes") or [], graph.get("edges") or [], graph.get("statuses") or []
    generation = _generation(con) + 1
    changed = _sync(con, "nodes", NODE_COLS, key, nodes, generation)
    changed += _sync(con, "edges", EDGE_COLS, key, edges, generation)
    want = sorted(_status_values(row) for row in statuses)
    if len({row[:2] for row in want}) != len(want):
        raise ValueError("two status rows for one requirement and workflow")
    if want != [tuple(row) for row in _statuses(con, key)]:
        con.execute("DELETE FROM requirements WHERE ticket = ?", (key,))
        con.executemany("INSERT INTO requirements(ticket, %s) VALUES (%s)"
                        % (", ".join(STATUS_COLS), ", ".join("?" * (len(STATUS_COLS) + 1))),
                        [(key,) + row for row in want])
        changed += 1
    old = con.execute("SELECT %s FROM sources WHERE ticket = ?" % ", ".join(SOURCE_COLS), (key,)).fetchone()
    if changed:
        set_meta(con, generation=generation, built_at=meta.get("built_at") or common.now_iso())
    source = (meta.get("sig") or "", graphmodel.content(nodes, edges, statuses),
              generation if changed or old is None else old[2],
              old[3] if old and not changed else meta.get("built_at") or common.now_iso(),
              meta.get("profile_generation") or "", str(meta.get("snapshot") or ""), _dump(meta.get("coverage") or {}))
    if old is None or tuple(old) != source:
        con.execute("INSERT OR REPLACE INTO sources(ticket, %s) VALUES (%s)"
                    % (", ".join(SOURCE_COLS), ", ".join("?" * (len(SOURCE_COLS) + 1))), (key,) + source)
    return {"changed": bool(changed), "rows": changed, "generation": source[2], "content": source[1]}


def delete_ticket(con, key):
    """Remove one ticket's subgraph (inside the caller's transaction); other tickets' copies of a node stay."""
    removed = sum(con.execute("DELETE FROM %s WHERE ticket = ?" % table, (key,)).rowcount
                  for table in ("nodes", "edges", "requirements", "sources"))
    if removed:
        set_meta(con, generation=_generation(con) + 1)
    return removed


def ticket_rows(con, key):
    """→ (nodes, edges, statuses, source row or None) for one ticket, each sorted by id."""
    nodes = []
    for values in con.execute("SELECT %s FROM nodes WHERE ticket = ? ORDER BY id" % ", ".join(NODE_COLS), (key,)):
        row = dict(zip(NODE_COLS, values), ticket=key)
        row["attrs"] = json.loads(row["attrs"] or "{}")
        nodes.append(row)
    edges = []
    for values in con.execute("SELECT %s FROM edges WHERE ticket = ? ORDER BY id" % ", ".join(EDGE_COLS), (key,)):
        row = dict(zip(EDGE_COLS, values), ticket=key)
        row["attrs"] = json.loads(row["attrs"] or "{}")
        edges.append(row)
    statuses = []
    for values in _statuses(con, key):
        row = dict(zip(STATUS_COLS, values))
        row["detail"] = json.loads(row["detail"] or "{}")
        statuses.append(row)
    found = con.execute("SELECT %s FROM sources WHERE ticket = ?" % ", ".join(SOURCE_COLS), (key,)).fetchone()
    source = None
    if found:
        source = dict(zip(SOURCE_COLS, found), ticket=key)
        source["coverage"] = json.loads(source["coverage"] or "{}")
    return nodes, edges, statuses, source


def stats():
    """What the index holds, from the read connection. Never builds and never writes."""
    con = open_read()
    if con is None:
        return {"state": "not_built", "schema": SCHEMA, "builder": BUILDER}
    try:
        m = meta(con)
        count = {table: con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                 for table in ("sources", "nodes", "edges", "requirements")}
    except sqlite3.Error as exc:
        return {"state": "unavailable", "reason": type(exc).__name__, "schema": SCHEMA, "builder": BUILDER}
    return {"state": "ok", "schema": SCHEMA, "builder": BUILDER, "generation": int(m.get("generation") or 0),
            "built_at": m.get("built_at") or "", "tickets": count["sources"], "nodes": count["nodes"],
            "edges": count["edges"], "requirements": count["requirements"], "bytes": os.path.getsize(db_path())}
