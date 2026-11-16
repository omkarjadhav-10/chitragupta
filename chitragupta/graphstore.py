"""Evidence Graph index: a derived, rebuildable SQLite file at <home>/.worklog/cache/evidence-graph.sqlite.

The store keeps rows; it does not know how a graph is built. Storage is per ticket (`PRIMARY KEY (ticket, id)`), so
a node two tickets share is stored once under each and every join is on `(ticket, id)`. Readers never write.
"""
import copy
import json
import os
import re
import sqlite3
import time
import traceback

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


# --- canonical history: <home>/data/graph/<KEY>.json (design section 8.2) ---
# The file is the authority for history; the index is the queryable copy of the current state. `publish` and
# `recover` are the only writers of both.

HISTORY_SCHEMA = 1
MAX_JOURNAL = 200
MAX_SNAPSHOTS = 50
MAX_DELTA = 100
MAX_ITEMS = 20000
MAX_BYTES = 4 * 1024 * 1024
EVIDENCE_TYPES = ("commit", "test_case", "test_run", "observation", "pull_request", "release_evidence")
DIMENSIONS = ("plan_coverage", "implementation", "verification")   # status row columns plan / implementation / …
_KEY = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


def history_path(key):
    if not isinstance(key, str) or not _KEY.match(key):
        raise ValueError("not a ticket key: %r" % (key,))
    return common.hp("data", "graph", key + ".json")


def new_history(key, next_seq=1):
    history_path(key)
    return {"schema_version": HISTORY_SCHEMA, "ticket": key, "next_seq": next_seq, "requirements": {},
            "head": None, "journal": []}


def _valid(data, key):
    if not isinstance(data, dict) or data.get("schema_version") != HISTORY_SCHEMA or data.get("ticket") != key:
        return False
    seq, head, journal = data.get("next_seq"), data.get("head"), data.get("journal")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return False
    if not isinstance(data.get("requirements"), dict) or not isinstance(journal, list):
        return False
    if head is not None:
        if not isinstance(head, dict) or not isinstance(head.get("seq"), int) or not head["seq"] < seq:
            return False
        state = head.get("state")
        if not isinstance(head.get("content"), str) or not isinstance(state, dict):
            return False
        if not isinstance(state.get("requirements"), dict) or not isinstance(state.get("items"), dict):
            return False
    return all(isinstance(e, dict) and isinstance(e.get("seq"), int) and e["seq"] < seq
               and isinstance(e.get("delta"), dict) for e in journal)


def load_history(key):
    """→ ('missing', None) | ('ok', data) | ('corrupt', None). Unreadable is never reported as missing or empty."""
    p = history_path(key)
    if not os.path.lexists(p):
        return "missing", None
    try:
        raw = common.read_text(p, default=None)
        data = json.loads(raw) if raw is not None else None
    except ValueError:   # not JSON, or not UTF-8
        data = None
    return ("ok", data) if _valid(data, key) else ("corrupt", None)


def state_of(graph):
    """The compact state a later delta is computed against: status rows, and evidence nodes without code changes."""
    requirements = {"%s|%s" % (row["requirement"], row.get("workflow") or ""):
                    [row.get("revision") or "", row.get("plan") or "", row.get("implementation") or "",
                     row.get("verification") or ""] for row in graph.get("statuses") or []}
    nodes = [n for n in graph.get("nodes") or [] if n["type"] in EVIDENCE_TYPES]
    if len(nodes) > MAX_ITEMS:
        counts = {}
        for n in nodes:
            counts[n["type"]] = counts.get(n["type"], 0) + 1
        return {"requirements": requirements, "items": {}, "coarse": True, "counts": counts}
    return {"requirements": requirements,
            "items": {n["id"]: [n["type"], n["version"], n.get("workflow") or "", n.get("location") or ""]
                      for n in nodes}}


def _capped(out, name, rows):
    rows = sorted(rows, key=_dump)
    out[name] = rows[:MAX_DELTA]
    return max(0, len(rows) - MAX_DELTA)


def delta(old_state, new_state, requirements_change=None, source_change=None):
    """The journal entry's `delta`. `old_state` None: the first entry, where everything present is new."""
    first = old_state is None
    old = old_state or {"requirements": {}, "items": {}}
    more, out = 0, {"requirements": {}, "evidence": {}, "status": [], "source": source_change or None}
    for name in ("added", "modified", "removed", "restored"):
        more += _capped(out["requirements"], name, (requirements_change or {}).get(name) or [])
    before, after = old.get("items") or {}, new_state.get("items") or {}
    coarse = bool(old.get("coarse") or new_state.get("coarse"))
    if coarse:   # too many items to list: counts only
        before, after = {}, {}
        out["coarse"] = True
        out["counts"] = {"from": old.get("counts"), "to": new_state.get("counts")}

    def item(rid, value):
        return [rid, value[0], value[2], value[3]]

    more += _capped(out["evidence"], "added", [item(i, after[i]) for i in set(after) - set(before)])
    more += _capped(out["evidence"], "removed", [item(i, before[i]) for i in set(before) - set(after)])
    more += _capped(out["evidence"], "changed",
                    [item(i, after[i]) for i in set(after) & set(before) if after[i] != before[i]])
    transitions = []
    if not first:
        was, now = old.get("requirements") or {}, new_state.get("requirements") or {}
        for key in set(was) | set(now):
            requirement, _, workflow = key.partition("|")
            for index, dimension in enumerate(DIMENSIONS, 1):
                a = was[key][index] if key in was else None
                b = now[key][index] if key in now else None
                if a != b:
                    transitions.append({"requirement": requirement, "workflow": workflow, "dimension": dimension,
                                        "from": a, "to": b})
    more += _capped(out, "status", transitions)
    out["more"] = more
    return out


def _size(history):
    return len((json.dumps(history, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def _bounded(history):
    """Apply the bounds in place; → how many old entries the file-size check dropped."""
    history["journal"] = history["journal"][-MAX_JOURNAL:]
    snapshots = history["requirements"].get("snapshots")
    if isinstance(snapshots, list):
        history["requirements"]["snapshots"] = snapshots = snapshots[-MAX_SNAPSHOTS:]
    else:
        snapshots = []
    dropped = 0
    while _size(history) > MAX_BYTES and (len(history["journal"]) > 1 or snapshots):
        for rows, keep in ((history["journal"], 1), (snapshots, 0)):
            cut = min(len(rows) - keep, max(1, len(rows) // 4))
            if cut > 0:
                del rows[:cut]
                dropped += cut
    return dropped


def _index(con, key, graph, seq, sig, profile_generation, at, coverage=None, note=None):
    """Publish 2: one transaction that makes the index hold these rows for journal sequence `seq`."""
    with con:
        res = write_ticket(con, key, graph, {"sig": sig, "profile_generation": profile_generation, "snapshot": seq,
                                             "coverage": coverage, "built_at": at})
        if note:
            set_meta(con, last_error=note)
    return res


def publish(con, key, graph, history, sig, profile_generation, at, requirements_change=None, source_change=None,
            coverage=None, baseline=None):
    """The two-step publication. (1) one atomic write of the history file: next sequence, head, journal entry and
    whatever `history['requirements']` holds; (2) one transaction replacing the ticket's index rows. `history` is
    the loaded (or new) file content and is not modified; the published one is returned."""
    path = history_path(key)
    new = copy.deepcopy(history)
    head = new.get("head")
    state = state_of(graph)
    content = graphmodel.content(graph.get("nodes") or [], graph.get("edges") or [], graph.get("statuses") or [])
    seq = new["next_seq"]
    entry = {"seq": seq, "prev": None if baseline or not head else head["seq"], "at": at, "content": content,
             "profile_generation": profile_generation,
             "delta": delta(state if baseline else head["state"] if head else None, state, requirements_change,
                            source_change)}
    if baseline:   # history before this entry is not continuous with it
        entry["baseline"] = baseline
    new["journal"].append(entry)
    new["head"] = {"seq": seq, "content": content, "profile_generation": profile_generation, "state": state}
    new["next_seq"] = seq + 1
    dropped = _bounded(new)
    common.write_json(path, new)                                                              # publish 1
    note = "history of %s exceeded its size bound; %d oldest entries dropped" % (key, dropped) if dropped else None
    res = _index(con, key, graph, seq, sig, profile_generation, at, coverage, note)           # publish 2
    return {"seq": seq, "content": content, "history": new, "changed": res["changed"], "dropped": dropped}


def _source(con, key):
    row = con.execute("SELECT snapshot, content FROM sources WHERE ticket = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return int(row[0]), row[1]
    except (TypeError, ValueError):
        return -1, row[1]


def _moved_aside(path):
    folder, name = os.path.split(path)
    try:
        return any(n.startswith(name + ".corrupt-") for n in os.listdir(folder))
    except OSError:
        return False


def recover(con, key, rebuild):
    """Bring the history file and the index back in step before anything else looks at this ticket. Idempotent.

    `rebuild(history)` builds from the current inputs against that history and returns {graph, sig,
    profile_generation, at} plus, optionally, coverage, requirements (the new `history['requirements']`),
    requirements_change and source_change. It is called only when something has to be repaired.
    → {action: 'none' | 'index' | 'publish' | 'reset', reason, seq}"""
    path = history_path(key)
    found, history = load_history(key)
    source = _source(con, key)
    head = history["head"] if history else None
    if found == "ok" and (source == (head["seq"], head["content"]) if head else source is None):
        return {"action": "none", "reason": "consistent", "seq": head["seq"] if head else None}
    if found == "missing" and source is None and not _moved_aside(path):
        return {"action": "none", "reason": "not built", "seq": None}

    def build(against):
        built = rebuild(against)
        if "requirements" in built:
            against = dict(against, requirements=built["requirements"])
        return built, against

    def full(against, built, baseline=None):
        return publish(con, key, built["graph"], against, built["sig"], built["profile_generation"], built["at"],
                       built.get("requirements_change"), built.get("source_change"), built.get("coverage"), baseline)

    if found == "ok" and head and (source is None or source[0] < head["seq"]):
        # the file is ahead: a crash after publish 1, or the index was deleted
        built, against = build(history)
        g = built["graph"]
        if graphmodel.content(g.get("nodes") or [], g.get("edges") or [], g.get("statuses") or []) == head["content"]:
            _index(con, key, g, head["seq"], built["sig"], built["profile_generation"], built["at"],
                   built.get("coverage"))
            return {"action": "index", "reason": "file ahead of the index", "seq": head["seq"]}
        return {"action": "publish", "reason": "file ahead of the index; inputs moved on",
                "seq": full(against, built)["seq"]}

    # The file cannot vouch for what the index holds: start again from the current state, said plainly.
    after = max(source[0] if source else 0, 0) + 1   # a sequence is never reused
    reason, note = "index ahead of the file", None
    if found == "missing":
        reason, history = "history file missing", new_history(key, after)
    elif found == "corrupt":
        stamp = re.sub(r"[^0-9A-Za-z]", "", common.now_iso())
        aside, n = "%s.corrupt-%s" % (path, stamp), 1
        while os.path.lexists(aside):
            n += 1
            aside = "%s.corrupt-%s-%d" % (path, stamp, n)
        paths.replace(path, aside, caller=CALLER)
        reason = "history file unreadable"
        note = "history of %s was unreadable; kept as %s and restarted" % (key, os.path.basename(aside))
        with con:
            set_meta(con, last_error=note)
        history = new_history(key, after)
    else:
        history = dict(history, next_seq=max(history["next_seq"], after))
    built, against = build(history)
    out = {"action": "reset", "reason": reason, "seq": full(against, built, baseline="reset")["seq"]}
    if note:
        out["error"] = note
    return out


def _behind(cfg, stored, profile_ok):
    """Visible tickets whose rows are missing or older than their inputs (reads only, as the sync's own check)."""
    from . import graphbuild
    from .dashboard import api
    scope = api.scope_index(cfg)
    count = 0
    for key in _ticket_keys():
        if not (scope.get(key) or {}).get("visible"):
            continue
        if not profile_ok or key not in stored or stored[key] != graphbuild.input_signature(key, cfg):
            count += 1
    return count


def stats(cfg=None):
    """What the index holds, from the read connection. Never builds and never writes. With `cfg`, also how many
    visible tickets the index is behind on (`behind`; it stats every input, so only on request)."""
    con = open_read()
    if con is None:
        out = {"state": "not_built", "schema": SCHEMA, "builder": BUILDER}
        if cfg is not None:
            from .dashboard import api
            out["behind"] = sum(1 for key in _ticket_keys() if (api.scope_index(cfg).get(key) or {}).get("visible"))
        return out
    try:
        m = meta(con)
        count = {table: con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                 for table in ("sources", "nodes", "edges", "requirements")}
        stored = dict(con.execute("SELECT ticket, sig FROM sources").fetchall()) if cfg is not None else {}
    except sqlite3.Error as exc:
        return {"state": "unavailable", "reason": type(exc).__name__, "schema": SCHEMA, "builder": BUILDER}
    out = {"state": "ok", "schema": SCHEMA, "builder": BUILDER, "generation": int(m.get("generation") or 0),
           "built_at": m.get("built_at") or "", "full_at": m.get("full_at") or "",
           "last_error": m.get("last_error") or "", "tickets": count["sources"], "nodes": count["nodes"],
           "edges": count["edges"], "requirements": count["requirements"],
           "rows": count["nodes"] + count["edges"] + count["requirements"], "bytes": os.path.getsize(db_path())}
    if cfg is not None:
        from . import profile
        out["behind"] = _behind(cfg, stored, m.get("profile_generation") == profile.signature(cfg))
    return out


# --- incremental sync and the tick step (design section 12) ---
# No process, network or model call: the builder reads cached records only. An unchanged ticket stops at its input
# signature and writes nothing at all.

LIMIT = 25        # tickets rebuilt per run
BUDGET_S = 2.0    # checked between tickets, so one ticket can overrun it
GRAPH_LOCK = "evidence-graph.lock"   # one writer of the history files and the index at a time
SKIPPED = "a background run is in progress"
_TABLES_DATA = ("sources", "nodes", "edges", "requirements")


def _ticket_keys():
    try:
        names = os.listdir(common.hp("data", "tickets"))
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json") and _KEY.match(n[:-5]))


def purge(con, profile_generation):
    """The profile changed: every row goes before anything is rebuilt; the history files stay."""
    with con:
        for table in _TABLES_DATA:
            con.execute("DELETE FROM %s" % table)
        set_meta(con, profile_generation=profile_generation, generation=_generation(con) + 1)
    reset()


def _built(built, sig, profile_generation, at):
    """`graphbuild.ticket_graph`'s result as `recover`'s rebuild contract and `publish`'s arguments take it."""
    change = built.get("change") or {}
    return {"graph": {name: built[name] for name in ("nodes", "edges", "statuses")}, "sig": sig,
            "profile_generation": profile_generation, "at": at, "coverage": built.get("coverage"),
            "requirements": built["requirements"],
            "requirements_change": {name: list(change.get(name) or []) for name in ("added", "modified", "removed",
                                                                                   "restored")},
            "source_change": change.get("source")}


def _order(everyone, first, cursor):
    """Dirty tickets first, then the rest starting after the cursor and wrapping round."""
    rest = [k for k in everyone if k not in first]
    if cursor:
        rest = [k for k in rest if k > cursor] + [k for k in rest if k <= cursor]
    return list(first) + rest


def _note(con, text):
    if meta(con).get("last_error") != text:
        with con:
            set_meta(con, last_error=text)


def _one(con, cfg, key, profile_generation, dirty, old_sig):
    """One ticket: → None (nothing to do), 'deleted', 'recovered', 'unchanged' (signature updated only) or 'built'."""
    from . import graphbuild
    from .dashboard import api
    if not api.scope_of(key, cfg).get("visible"):
        if old_sig is None:
            return None
        with con:
            delete_ticket(con, key)
        return "deleted"
    sig = graphbuild.input_signature(key, cfg)
    if sig == old_sig and not dirty:
        return None

    def rebuild(history):
        inputs = graphbuild.load_inputs(key, cfg, history)
        return _built(graphbuild.ticket_graph(inputs), sig, profile_generation, inputs["at"])

    if recover(con, key, rebuild)["action"] != "none":
        return "recovered"   # current as of that rebuild, with this signature
    row = con.execute("SELECT sig FROM sources WHERE ticket = ?", (key,)).fetchone()
    if row and row[0] == sig:
        return None
    found, history = load_history(key)
    if found != "ok":   # recover found nothing to repair: this is the first build
        history = new_history(key)
    inputs = graphbuild.load_inputs(key, cfg, history)
    built = graphbuild.ticket_graph(inputs)
    head = history.get("head")
    if row and head and built["content"] == head["content"] and not (built.get("change") or {}).get("changed"):
        with con:
            con.execute("UPDATE sources SET sig = ?, coverage = ? WHERE ticket = ?",
                        (sig, _dump(built.get("coverage") or {}), key))
        return "unchanged"
    args = _built(built, sig, profile_generation, inputs["at"])
    publish(con, key, args["graph"], dict(history, requirements=built["requirements"]), sig, profile_generation,
            inputs["at"], args["requirements_change"], args["source_change"], args["coverage"])
    if built.get("dropped"):
        _note(con, "%d edges of %s left out: an endpoint is missing" % (built["dropped"], key))
    return "built"


def sync(cfg, keys=None, limit=LIMIT, budget_s=BUDGET_S):
    """Bring the index up to date, a bounded amount per call. `keys` are this run's dirty tickets: they come first
    and are always checked by `recover`; the others only when their input signature changed. A busy index (another
    writer) skips the run. → a report of what happened to which ticket."""
    from .agent import Lock
    began = time.monotonic()
    report = {"busy": False, "purged": False, "built": [], "recovered": [], "unchanged": [], "deleted": [],
              "errors": [], "more": False}
    lock = Lock(GRAPH_LOCK)
    if not lock.acquire():   # another sync or a rebuild is writing: skip this run, the next one catches up
        report["busy"] = True
        return report
    try:
        try:
            con = open_write(timeout=2.0)
        except sqlite3.OperationalError:
            report["busy"] = True
            return report
        return _sync_run(con, cfg, keys, limit, budget_s, began, report)
    finally:
        lock.release()


def _sync_run(con, cfg, keys, limit, budget_s, began, report):
    from . import profile
    try:
        generation = profile.signature(cfg)
        if meta(con).get("profile_generation") != generation:
            purge(con, generation)
            report["purged"] = True
        stored = dict(con.execute("SELECT ticket, sig FROM sources").fetchall())
        first = sorted({str(k).upper() for k in keys or () if _KEY.match(str(k).upper())})
        everyone = sorted(set(_ticket_keys()) | set(stored) | set(first))
        cursor = meta(con).get("cursor") or ""
        done, last = 0, ""
        for key in _order(everyone, first, cursor):
            if done >= limit or time.monotonic() - began >= budget_s:
                report["more"] = True
                break
            last = key
            try:
                outcome = _one(con, cfg, key, generation, key in first, stored.get(key))
            except Exception:   # one ticket never stops the others; the next run tries it again
                common.log(CALLER, "evidence graph %s failed: %s" % (key, traceback.format_exc()))
                report["errors"].append(key)
                done += 1
                continue
            if outcome:
                report[outcome].append(key)
                done += outcome in ("built", "recovered")
        wanted = last if report["more"] else ""
        if (meta(con).get("cursor") or "") != wanted:
            with con:
                set_meta(con, cursor=wanted)
        return report
    finally:
        con.close()


def rebuild(cfg, wait_s=None):
    """The explicit full rebuild: replace the tables, then rebuild every visible ticket in its own transaction,
    `recover` first. Journals are never rewritten (a history file that is ahead re-indexes at its own sequence).
    Takes the agent lock like every CLI command that changes state; not obtained → skipped, nothing written.
    → {skipped, built, recovered, unchanged, deleted, errors}"""
    from . import profile
    from .agent import Lock
    report = {"skipped": None, "built": [], "recovered": [], "unchanged": [], "deleted": [], "errors": []}
    lock = Lock()
    if not lock.acquire_wait(wait_s):
        report["skipped"] = SKIPPED
        return report
    try:
        graph_lock = Lock(GRAPH_LOCK)
        if not graph_lock.acquire():
            report["skipped"] = SKIPPED
            return report
        try:
            reset()   # this process's cached reader goes before the tables are replaced
            try:
                con = open_write(timeout=10.0)
            except sqlite3.OperationalError:
                report["skipped"] = "the index is in use; try again in a moment"
                return report
            try:
                generation = profile.signature(cfg)
                with con:
                    before = _generation(con)
                    _create(con)
                    set_meta(con, generation=before + 1, profile_generation=generation)
                for key in _ticket_keys():
                    lock.refresh()
                    graph_lock.refresh()
                    try:
                        outcome = _one(con, cfg, key, generation, True, None)
                    except Exception:
                        common.log(CALLER, "evidence graph rebuild of %s failed: %s" % (key, traceback.format_exc()))
                        report["errors"].append(key)
                        continue
                    if outcome:
                        report[outcome].append(key)
                with con:
                    set_meta(con, full_at=common.now_iso())
                return report
            finally:
                con.close()
        finally:
            graph_lock.release()
    finally:
        lock.release()


def tick(cfg, touched=()):
    """The background step: nothing while paused."""
    if os.path.exists(common.wl("PAUSED")):
        return {"paused": True}
    return sync(cfg, keys=touched)
