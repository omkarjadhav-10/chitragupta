"""The database MCP as a connector: role -> tool mapping, argument mapping, capability records, engine adapters, typed
literal encoders, the constrained statement builder and the transport (database flow D0, plan §2.4; DB-01, DB-11).

Roles (mcp.database.tools, mcpjobs.db_tools): list_connections, list_schemas, list_tables, describe_table, query.
Arguments (mcp.database.arguments, new): {connection, schema, table, sql, limit, params, timeout} -> that server's
  argument names. Absent: the names the existing jobs use (connectionId, schema, table, sql, limit), provenance
  "default". Present but invalid (a connection key the guard doesn't recognise, an sql key it can't find, a limit key
  other than "limit", a schema / table key the guard reads as routing, two roles on one name) -> no mapping at all:
  every capability that needs it is `unsupported` with the reason. Never a guessed default.
Capabilities (data/db/capabilities.json): supported | unsupported | permission_denied | not_verified, each with
  observed_at, reason and provenance (tool_listing, configured_mapping, adapter, observed_call). A tool listing (the
  stream's init line, sanitized to this server's tools) makes a mapped tool `supported`; without one it is
  `not_verified`; an observed call outcome overrides (ok -> supported, denied -> permission_denied, error or timeout ->
  not_verified, never an empty list). Kinds: tables, views, foreign_keys, procedures, triggers, dependencies,
  indexes, audit_events; with no dependency / index / audit query here those stay `unsupported`.
Adapters: PostgresAdapter ("x""y", LIMIT n) and MssqlAdapter ([x]]y], TOP (n)); their catalog SQL is
  mcpjobs.CATALOG_SQL. adapter_for(engine) is None for mongodb and anything unknown: no row or catalog statement is
  built for them. The engine comes from the approved scope, never from a connection's name.
Literals: encoded by the column's declared type (integer, decimal, uuid, date, datetime, string); a value that
  doesn't fit is declined (mapping_unresolved), never coerced or stripped: '00123' stays a string for a char column
  and is declined for an integer one. Bound parameters are used when a params argument is mapped.
build_select: every identifier from a Metadata object (schema-card describe rows), predicates are (column, value)
  pairs encoded here, a row limit always; the statement then passes the query guard and, given the connection's
  permitted scope, statement_scope_reason. No free SQL text goes in.
Transport.execute(job, calls, cfg, health, pin, deadline_s, keep_stream) -> [{call_id, tool, connection, statement,
  status: ok | error | permission_denied | missing, rows, error}]. McpJobTransport runs one exact-call mcpjobs.run job
  and matches results back by tool, connection and exact statement (a call whose statement differs is ignored).
"""
import datetime as _dt
import json
import re

from . import common
from . import dbstore

ROLES = ("list_connections", "list_schemas", "list_tables", "describe_table", "query")
ARG_ROLES = ("connection", "schema", "table", "sql", "limit", "params", "timeout")
DEFAULT_ARGUMENTS = {"connection": "connectionId", "schema": "schema", "table": "table", "sql": "sql", "limit": "limit",
                     "params": "", "timeout": ""}
STATES = ("supported", "unsupported", "permission_denied", "not_verified")
STATE_LABEL = {"supported": "Supported", "unsupported": "Not available", "permission_denied": "Access denied",
               "not_verified": "Not verified"}
CAPABILITIES = ("connection_inventory", "schemas", "object_list", "describe", "catalog_read", "parameterized_query",
                "pagination", "cancellation", "timeout")
OBJECT_KINDS = ("tables", "views", "foreign_keys", "procedures", "triggers", "dependencies", "indexes", "audit_events")
CAP_ROLE = {"connection_inventory": "list_connections", "schemas": "list_schemas", "object_list": "list_tables",
            "describe": "describe_table", "catalog_read": "query", "parameterized_query": "query"}
ARG_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
TOOL_RX = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
MAX_ROWS = 20
MAX_IN = 20
DENIED_RX = re.compile(r"(?i)permission denied|access denied|not authori[sz]ed|forbidden|insufficient privilege|"
                       r"denied to user|lacks? (?:the )?permission")


class BuildError(ValueError):
    """A statement or call that can't be built safely: nothing is sent. `gap` names the flow gap it stands for."""

    def __init__(self, msg, gap="mapping_unresolved"):
        super().__init__(msg)
        self.gap = gap


# --------------------------------------------------------------------------- tools and argument mapping

def role_tools(cfg):
    """{role: tool name} (mcp.database.tools over the defaults)."""
    from . import mcpjobs
    return mcpjobs.db_tools(cfg)


def mapping_errors(value):
    """Problems of an mcp.database.arguments value (an empty list: valid)."""
    from .safety import mcp
    if not isinstance(value, dict):
        return ["mcp.database.arguments maps connection, schema, table, sql, limit, params and timeout to argument names"]
    out = ["unknown role %s" % k for k in sorted(set(value) - set(ARG_ROLES))]
    m = dict(DEFAULT_ARGUMENTS, **{k: v for k, v in value.items() if k in ARG_ROLES})
    for k, v in m.items():
        if not isinstance(v, str) or (v and not ARG_RX.match(v)):
            out.append("%s: %r is not an argument name" % (k, v))
    if out:
        return out
    if not m["connection"] or not mcp._conn_key(m["connection"]):
        out.append("connection: %r is not an argument the guard reads as the connection" % m["connection"])
    if m["sql"] not in mcp.SQL_KEYS:
        out.append("sql: %r is not an argument the guard reads a statement from (%s)" % (m["sql"], ", ".join(mcp.SQL_KEYS)))
    if m["limit"] not in ("", "limit"):
        out.append("limit: only an argument named limit caps rows in the guard")
    for k in ("schema", "table", "params", "timeout"):
        v = m[k].lower()
        if v and (mcp._conn_key(v) or v in mcp.ROUTING_KEYS or v in mcp.SQL_KEYS):
            out.append("%s: %r is read by the guard as a connection, routing or statement argument" % (k, m[k]))
    used = [v for v in m.values() if v]
    if len(used) != len(set(u.lower() for u in used)):
        out.append("two roles map to the same argument name")
    if not m["schema"] or not m["table"]:
        out.append("schema and table need argument names")
    return out


def argument_mapping(cfg):
    """(mapping or None, errors, provenance)."""
    raw = ((cfg or {}).get("mcp") or {}).get("database") or {}
    if "arguments" not in raw:
        return dict(DEFAULT_ARGUMENTS), [], "default"
    errs = mapping_errors(raw.get("arguments"))
    if errs:
        return None, errs, "mcp.database.arguments"
    return dict(DEFAULT_ARGUMENTS, **raw["arguments"]), [], "mcp.database.arguments"


def build_call(mapping, tools, role, connection, schema=None, table=None, sql=None, limit=None, params=None,
               timeout=None):
    """{tool, input} for one call in the server's own argument names. Every call names its connection."""
    if mapping is None:
        raise BuildError("no valid argument mapping", gap="unknown_boundary")
    if role not in ROLES:
        raise BuildError("unknown role %r" % role)
    if not connection and role != "list_connections":
        raise BuildError("a call names its connection (never the server's default)")
    inp = {mapping["connection"]: connection} if connection else {}
    for k, v in (("schema", schema), ("table", table), ("sql", sql)):
        if v is not None:
            inp[mapping[k]] = v
    if limit is not None:
        if not mapping["limit"]:
            raise BuildError("no limit argument mapped")
        inp[mapping["limit"]] = int(limit)
    if params is not None:
        if not mapping["params"]:
            raise BuildError("no params argument mapped")
        inp[mapping["params"]] = list(params)
    if timeout is not None and mapping["timeout"]:
        inp[mapping["timeout"]] = int(timeout)
    return {"tool": tools[role], "input": inp}


def sanitize_tools(tools_loaded, server):
    """This server's tool names from a stream's init listing (mcp__<server>__<tool>); everything else dropped."""
    pre = "mcp__%s__" % server
    out = set()
    for t in tools_loaded or []:
        if isinstance(t, str) and t.startswith(pre) and TOOL_RX.match(t[len(pre):]):
            out.add(t[len(pre):])
    return sorted(out)


# --------------------------------------------------------------------------- capability records

def cap(state, reason="", provenance="", observed_at=""):
    if state not in STATES:
        raise ValueError("unknown capability state %r" % state)
    return {"state": state, "reason": reason, "provenance": provenance, "observed_at": observed_at}


def _tool_cap(role, tools, mapping, map_errs, listed, at):
    if mapping is None:
        return cap("unsupported", "argument mapping invalid: %s" % map_errs[0], "configured_mapping")
    name = tools.get(role)
    if listed is None:
        return cap("not_verified", "no tool listing observed yet (%s)" % name, "none")
    if name not in listed:
        return cap("unsupported", "%s is not in the server's tool listing" % name, "tool_listing", at)
    return cap("supported", "%s listed" % name, "tool_listing", at)


def assess(cfg, tools_loaded=None, previous=None, now=None):
    """Capability record from the configured mapping, the adapters and (when given) a tool listing. Observations in
    `previous` (provenance observed_call) are kept: they are newer evidence than a listing."""
    from . import dbaccess, mcpjobs
    at = now or common.now_iso()
    srv = mcpjobs.server_name(cfg or {}, "database")
    tools = role_tools(cfg)
    mapping, map_errs, map_prov = argument_mapping(cfg)
    listed = None if tools_loaded is None else set(sanitize_tools(tools_loaded, srv))
    if not srv:
        none = cap("unsupported", "no database MCP configured (mcp.database.server)", "configured_mapping")
        conn_caps = {}
        caps = {c: dict(none) for c in CAPABILITIES}
    else:
        caps = {c: _tool_cap(CAP_ROLE[c], tools, mapping, map_errs, listed, at) for c in CAP_ROLE}
        q = caps["parameterized_query"]
        if q["state"] != "unsupported" and mapping is not None and not mapping["params"]:
            caps["parameterized_query"] = cap("unsupported", "no params argument mapped: typed literals only",
                                              "configured_mapping")
        caps["pagination"] = cap("unsupported", "no pagination argument mapped; reads are bounded by the row limit",
                                 "configured_mapping")
        caps["cancellation"] = cap("unsupported", "the connector has no cancel call: a stopped job only stops "
                                                  "waiting", "configured_mapping")
        caps["timeout"] = (cap("not_verified", "timeout argument mapped, not observed yet", "configured_mapping")
                           if mapping is not None and mapping["timeout"] else
                           cap("unsupported", "no timeout argument mapped: a client deadline only", "configured_mapping"))
        conn_caps = {}
        for c, e in dbaccess.approved_scope(cfg)["connections"].items():
            conn_caps[c] = {"engine": e["engine"] or "", "capabilities": _object_caps(e["engine"], caps)}
    rec = {"schema_version": dbstore.SCHEMA_VERSION, "assessed_at": at, "server_listed": listed is not None,
           "mapping": {"provenance": map_prov, "errors": map_errs},
           "connector": {"capabilities": caps}, "connections": conn_caps}
    return _keep_observed(rec, previous)


def _object_caps(engine, caps):
    from . import mcpjobs
    describe, query = caps["describe"], caps["catalog_read"]
    catalog = engine in mcpjobs.CATALOG_SQL

    def via(base, ok_reason, need_catalog=False):
        if need_catalog and not catalog:
            return cap("unsupported", "no catalog statements for engine %s" % (engine or "unknown"), "adapter")
        if base["state"] != "supported":
            return cap(base["state"], base["reason"], base["provenance"], base["observed_at"])
        return cap("supported", ok_reason, base["provenance"], base["observed_at"])
    out = {"tables": via(describe, "columns from describe_table"),
           "foreign_keys": via(describe, "isForeignKey / references in describe rows"),
           "views": via(describe, "described like tables"),
           "procedures": via(query, "routine sources from the catalog (definitions may be hidden)", True),
           "triggers": via(query, "trigger definitions from the catalog", True),
           "catalog_read": via(query, "catalog statements for %s" % engine, True)}
    for k in ("dependencies", "indexes", "audit_events"):
        out[k] = cap("unsupported", "no %s source in this connector contract" % k.replace("_", " "), "adapter")
    return out


def _keep_observed(rec, previous):
    prev = previous if isinstance(previous, dict) else {}
    for k, v in ((prev.get("connector") or {}).get("capabilities") or {}).items():
        if isinstance(v, dict) and v.get("provenance") == "observed_call" and k in rec["connector"]["capabilities"]:
            rec["connector"]["capabilities"][k] = v
    for c, pc in (prev.get("connections") or {}).items():
        if c not in rec["connections"] or not isinstance(pc, dict):
            continue
        for k, v in (pc.get("capabilities") or {}).items():
            if isinstance(v, dict) and v.get("provenance") == "observed_call":
                rec["connections"][c]["capabilities"][k] = v
        for k in ("last_authorized", "last_denied"):
            if pc.get(k):
                rec["connections"][c][k] = pc[k]
    return rec


OUTCOMES = {"ok": "supported", "permission_denied": "permission_denied", "error": "not_verified",
            "timeout": "not_verified", "missing": "not_verified"}


def observe_call(record, capability, outcome, connection=None, at=None, reason=""):
    """Record what a real call showed: ok -> supported, permission_denied -> permission_denied, error / timeout /
    missing -> not_verified (a failed metadata read is never an empty list). An ok call on a connection is also its
    last authorization observation."""
    if outcome not in OUTCOMES:
        raise ValueError("unknown outcome %r" % outcome)
    at = at or common.now_iso()
    c = cap(OUTCOMES[outcome], reason or outcome.replace("_", " "), "observed_call", at)
    if connection is None:
        record.setdefault("connector", {}).setdefault("capabilities", {})[capability] = c
        return record
    ent = record.setdefault("connections", {}).setdefault(connection, {"engine": "", "capabilities": {}})
    ent.setdefault("capabilities", {})[capability] = c
    if outcome == "ok":
        ent["last_authorized"] = at
    elif outcome == "permission_denied":
        ent["last_denied"] = at
    return record


def load_capabilities():
    return dbstore.read_file("capabilities")


def save_capabilities(record):
    with dbstore.lock():
        dbstore.write_file("capabilities", record)
    return record


# --------------------------------------------------------------------------- metadata

INTEGER_TYPES = {"int", "integer", "int2", "int4", "int8", "smallint", "bigint", "tinyint", "serial", "bigserial",
                 "smallserial"}
DECIMAL_TYPES = {"numeric", "decimal"}
UUID_TYPES = {"uuid", "uniqueidentifier"}
DATE_TYPES = {"date"}
DATETIME_TYPES = {"timestamp", "timestamptz", "timestamp with time zone", "timestamp without time zone", "datetime",
                  "datetime2", "datetimeoffset", "smalldatetime"}
STRING_TYPES = {"char", "character", "varchar", "character varying", "nvarchar", "nchar", "text", "citext", "bpchar"}
_TYPE = re.compile(r"^\s*([a-z][a-z0-9 ]*?)\s*(?:\(\s*(\d+|max)\s*(?:,\s*\d+\s*)?\))?\s*(?:\[\])?\s*$")


def column_class(data_type):
    """(class, max length or None) of a declared column type; class None = no encoder (fail closed)."""
    m = _TYPE.match(str(data_type or "").lower())
    if not m or str(data_type or "").strip().endswith("[]"):
        return None, None
    base, size = m.group(1).strip(), m.group(2)
    for cls, names in (("integer", INTEGER_TYPES), ("decimal", DECIMAL_TYPES), ("uuid", UUID_TYPES),
                       ("date", DATE_TYPES), ("datetime", DATETIME_TYPES), ("string", STRING_TYPES)):
        if base in names:
            n = int(size) if cls == "string" and size and size.isdigit() else None
            return cls, n
    return None, None


class Metadata:
    """One object's columns from schema-card describe rows ({name, dataType, nullable, isPrimaryKey, …}): the only
    source of identifiers for build_select. Lookups follow the engine's identifier rules."""

    def __init__(self, engine, schema, name, columns, database="", case_sensitive=False, object_id=""):
        from . import dbmodel
        self.engine, self.schema, self.name, self.database = engine, schema, name, database
        self.case_sensitive, self.object_id = bool(case_sensitive), object_id
        self._norm = lambda n: dbmodel.norm_ident(engine, n, True, self.case_sensitive)  # noqa: E731
        self.columns = {}
        for c in columns:
            self.columns[self._norm(c["name"])] = c

    @classmethod
    def from_describe(cls, engine, schema, name, rows, database="", case_sensitive=False, object_id=""):
        cols = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict) or not isinstance(r.get("name"), str) or not r["name"]:
                continue
            dt = r.get("dataType") or r.get("data_type") or r.get("type") or ""
            kls, size = column_class(dt)
            cols.append({"name": r["name"], "data_type": str(dt), "class": kls, "max_length": size,
                         "nullable": r.get("nullable"), "primary_key": bool(r.get("isPrimaryKey")),
                         "foreign_key": bool(r.get("isForeignKey")), "references": r.get("references"),
                         "sensitive": bool(r.get("isPhiField"))})
        if not cols:
            raise BuildError("no column metadata for %s.%s" % (schema, name), gap="definition_unavailable")
        return cls(engine, schema, name, cols, database, case_sensitive, object_id)

    def column(self, name):
        c = self.columns.get(self._norm(name)) if isinstance(name, str) else None
        if c is None:
            raise BuildError("column %r is not in the metadata of %s.%s" % (name, self.schema, self.name))
        return c


# --------------------------------------------------------------------------- literals

class Literal:
    """An encoded SQL literal: built only by encode_literal, for one engine and column class."""
    __slots__ = ("sql", "engine", "cls")

    def __init__(self, sql, engine, cls):
        self.sql, self.engine, self.cls = sql, engine, cls


class Param:
    """A value to send as a bound parameter (only when the connector maps a params argument)."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


_INT = re.compile(r"^-?(?:0|[1-9]\d{0,17})$")
_DEC = re.compile(r"^-?(?:0|[1-9]\d{0,17})(?:\.\d{1,18})?$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?$")
STRING_MAX = 256


def check_value(value, column):
    """The value as text when it fits the column's declared type, else BuildError (mapping_unresolved). Nothing is
    converted, stripped or padded."""
    cls, size = column.get("class"), column.get("max_length")
    name = column.get("name")
    if cls is None:
        raise BuildError("no safe encoder for %s's type %s" % (name, column.get("data_type")))
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int)):
        raise BuildError("%s: a %s key must be given as text or a whole number" % (name, cls))
    if cls == "integer":
        txt = str(value)
        if not _INT.match(txt):
            raise BuildError("%s is an integer column: %r is not a whole number as written" % (name, value))
        return txt
    if not isinstance(value, str):
        raise BuildError("%s is a %s column: give the value as written, not a number" % (name, cls))
    if cls == "decimal":
        if not _DEC.match(value):
            raise BuildError("%s is a decimal column: %r is not a plain decimal" % (name, value))
        return value
    if cls == "uuid":
        if not _UUID.match(value):
            raise BuildError("%s is a uuid column: %r is not a uuid" % (name, value))
        return value
    if cls == "date":
        try:
            ok = bool(_DATE.match(value)) and bool(_dt.date.fromisoformat(value))
        except ValueError:
            ok = False
        if not ok:
            raise BuildError("%s is a date column: %r is not a date" % (name, value))
        return value
    if cls == "datetime":
        m = _DATETIME.match(value)
        try:
            ok = bool(m) and bool(_dt.date.fromisoformat(m.group(1))) and int(m.group(2)) < 24 and int(m.group(3)) < 60
        except ValueError:
            ok = False
        if not ok:
            raise BuildError("%s is a date-time column: %r is not an ISO date-time" % (name, value))
        return value
    # string
    if not value or len(value) > STRING_MAX:
        raise BuildError("%s: a text key of 1 to %d characters" % (name, STRING_MAX))
    if size and len(value) > size:
        raise BuildError("%s holds at most %d characters: %r can't match" % (name, size, value))
    if any(ord(ch) < 32 or ord(ch) == 127 or 0x80 <= ord(ch) < 0xA0 for ch in value) or "\\" in value:
        raise BuildError("%s: control characters and backslashes are not encoded" % name)
    return value


class Adapter:
    engine = ""
    forbidden = ("\\", ";", "'", "`", ".", "--", "/*", "*/")

    def quote(self, ident):
        """A quoted identifier, or BuildError for one that can't be quoted safely for this engine and the guard."""
        if not isinstance(ident, str) or not ident or len(ident) > 128 or ident != ident.strip() or \
                any(ord(c) < 32 or ord(c) == 127 for c in ident) or any(f in ident for f in self.forbidden):
            raise BuildError("identifier %r can't be quoted safely" % (ident,), gap="unknown_boundary")
        return self._quote(ident)

    def _quote(self, ident):
        raise NotImplementedError

    def literal(self, value, column):
        txt = check_value(value, column)
        if column["class"] in ("integer", "decimal"):
            return Literal(txt, self.engine, column["class"])
        return Literal(self._string(txt), self.engine, column["class"])

    def _string(self, txt):
        return "'%s'" % txt.replace("'", "''")

    def placeholder(self, n):
        raise NotImplementedError

    def select(self, cols, source, where, order, limit):
        raise NotImplementedError

    def catalog(self, kind, schema, names):
        """A catalog statement of mcpjobs.CATALOG_SQL ("triggers": names = tables; "writers": names = [table]).
        Names must already be plain identifiers: they are never stripped to fit."""
        from . import mcpjobs
        for x in [schema] + list(names):
            if not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,128}", x):
                raise BuildError("catalog lookups need plain names (got %r)" % (x,), gap="definition_unavailable")
        if kind == "triggers":
            return mcpjobs.triggers_sql(self.engine, schema, list(names))
        if kind == "writers" and len(names) == 1:
            return mcpjobs.writers_sql(self.engine, schema, names[0])
        raise BuildError("unknown catalog statement %r" % kind)


class PostgresAdapter(Adapter):
    engine = "postgres"

    def _quote(self, ident):
        return '"%s"' % ident.replace('"', '""')

    def placeholder(self, n):
        return "$%d" % n

    def select(self, cols, source, where, order, limit):
        return "SELECT %s FROM %s%s%s LIMIT %d" % (cols, source, where, order, limit)


class MssqlAdapter(Adapter):
    engine = "mssql"
    forbidden = Adapter.forbidden + ('"', "[")

    def _quote(self, ident):
        return "[%s]" % ident.replace("]", "]]")

    def _string(self, txt):
        return "N'%s'" % txt.replace("'", "''")

    def placeholder(self, n):
        return "@p%d" % n

    def select(self, cols, source, where, order, limit):
        return "SELECT TOP (%d) %s FROM %s%s%s" % (limit, cols, source, where, order)


ADAPTERS = {"postgres": PostgresAdapter, "mssql": MssqlAdapter}


def adapter_for(engine):
    """The adapter of a trusted engine name; None for mongodb, unknown or missing (no statement is built)."""
    k = ADAPTERS.get(str(engine or "").strip().lower())
    return k() if k else None


def encode_literal(adapter, value, column):
    if adapter is None:
        raise BuildError("no adapter for this engine", gap="unknown_boundary")
    return adapter.literal(value, column)


def build_select(adapter, meta, columns, predicates, order_by=None, limit=MAX_ROWS, params=False, permitted=None,
                 max_rows=MAX_ROWS):
    """-> (sql, bound values or None). `predicates`: [(column, value | [values] | Literal | Param)] joined with AND
    (a composite or tenant key is several predicates); `order_by`: [(column, "ASC" | "DESC")]. Raises BuildError.
    The statement passes the query guard, and statement_scope_reason when `permitted` (the connection's approved
    scope) is given."""
    from .safety import query
    if adapter is None:
        raise BuildError("no adapter for this engine", gap="unknown_boundary")
    if not isinstance(meta, Metadata) or meta.engine != adapter.engine:
        raise BuildError("metadata of another engine")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= min(max_rows, MAX_ROWS):
        raise BuildError("row limit must be 1 to %d" % min(max_rows, MAX_ROWS))
    if not columns:
        raise BuildError("name the columns to read (no SELECT *)")
    if not predicates:
        raise BuildError("a read needs a selective predicate (no unbounded scan)")
    q = adapter.quote
    cols = ", ".join(q(meta.column(c)["name"]) for c in dict.fromkeys(columns))
    source = "%s.%s" % (q(meta.schema), q(meta.name))  # schema-qualified, always; never the default schema
    bound, conds = [], []
    for item in predicates:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise BuildError("a predicate is a (column, value) pair")
        col = meta.column(item[0])
        lhs = q(col["name"])
        val = item[1]
        if isinstance(val, list):
            if not val or len(val) > MAX_IN:
                raise BuildError("1 to %d values per key list" % MAX_IN)
            conds.append("%s IN (%s)" % (lhs, ", ".join(_operand(adapter, v, col, params, bound) for v in val)))
        else:
            conds.append("%s = %s" % (lhs, _operand(adapter, val, col, params, bound)))
    order = ""
    if order_by:
        parts = []
        for c, d in order_by:
            if str(d).upper() not in ("ASC", "DESC"):
                raise BuildError("order direction is ASC or DESC")
            parts.append("%s %s" % (q(meta.column(c)["name"]), str(d).upper()))
        order = " ORDER BY " + ", ".join(parts)
    sql = adapter.select(cols, source, " WHERE " + " AND ".join(conds), order, limit)
    why = query.sql_reason(sql, max_rows)
    if why:
        raise BuildError("the guard would refuse it: %s" % why, gap="unknown_boundary")
    if permitted is not None:
        why = query.statement_scope_reason(sql, adapter.engine, permitted)
        if why:
            raise BuildError("outside the approved scope: %s" % why, gap="permission_denied")
    return sql, (bound if params else None)


def _operand(adapter, val, col, params, bound):
    if isinstance(val, Literal):
        if val.engine != adapter.engine or val.cls != col["class"]:
            raise BuildError("literal encoded for another engine or column type")
        return val.sql
    if isinstance(val, Param) or params:
        raw = val.value if isinstance(val, Param) else val
        if not params:
            raise BuildError("bound parameters are not supported by this connector", gap="unknown_boundary")
        bound.append(check_value(raw, col))
        return adapter.placeholder(len(bound))
    return adapter.literal(val, col).sql


# --------------------------------------------------------------------------- transport

def result(call_id, tool, connection, statement, status, rows=None, error=""):
    if status not in ("ok", "error", "permission_denied", "missing"):
        raise ValueError("unknown call status %r" % status)
    return {"call_id": call_id, "tool": tool, "connection": connection, "statement": statement, "status": status,
            "rows": rows, "error": error}


def call_input(cfg, call):
    """{tool, input} of one planned call {call_id, role, connection, schema?, table?, sql?, limit?, params?}."""
    mapping, errs, _ = argument_mapping(cfg)
    if mapping is None:
        raise BuildError("argument mapping invalid: %s" % errs[0], gap="unknown_boundary")
    return build_call(mapping, role_tools(cfg), call.get("role"), call.get("connection"), schema=call.get("schema"),
                      table=call.get("table"), sql=call.get("sql"), limit=call.get("limit"), params=call.get("params"),
                      timeout=call.get("timeout"))


class Transport:
    """How planned calls reach the database MCP. Implementations never retry and never add a call."""

    def execute(self, job, calls, cfg, health, pin=None, deadline_s=120, keep_stream=False):
        raise NotImplementedError


DONE_SCHEMA = {"type": "object", "properties": {"done": {"type": "boolean"}}, "required": ["done"],
               "additionalProperties": False}


class McpJobTransport(Transport):
    """One exact-call `claude -p` job (mcpjobs.run, the read-only harness): the task lists every call with its exact
    arguments; results are matched back by tool, connection and exact statement, so a changed call is ignored."""

    def execute(self, job, calls, cfg, health, pin=None, deadline_s=120, keep_stream=False):
        from . import mcpjobs
        from .safety import mcp as guard
        srv = mcpjobs.server_name(cfg or {}, "database")
        planned, out = [], {}
        for c in calls:
            try:
                ci = call_input(cfg, c)
            except BuildError as e:
                out[c.get("call_id")] = result(c.get("call_id"), "", c.get("connection") or "", "", "error",
                                               error=str(e))
                continue
            planned.append((c, ci, guard.call_statement(ci["input"])))
        if not srv:
            for c, ci, st in planned:
                out[c["call_id"]] = result(c["call_id"], ci["tool"], c.get("connection") or "", st, "missing",
                                           error="no database MCP configured")
            return [out[c.get("call_id")] for c in calls]
        if planned:
            tools = sorted({ci["tool"] for _, ci, _ in planned})
            task = ("Make ALL of these tool calls in a single turn (parallel), with exactly these arguments (JSON), then "
                    "return {\"done\": true}. Do not make any other call, do not retry, do not read saved outputs.\n" +
                    "\n".join("%d. %s %s" % (i + 1, ci["tool"], json.dumps(ci["input"], ensure_ascii=False))
                              for i, (_, ci, _) in enumerate(planned)))
            res = mcpjobs.run(job, {srv: tools}, [mcpjobs.mcp_tool(srv, t) for t in tools], mcpjobs._writes(srv, cfg),
                              task, DONE_SCHEMA, cfg, health, max_turns=3, timeout=max(10, int(deadline_s)),
                              keep_stream=keep_stream, pin=pin)
            want = {(ci["tool"], str(c.get("connection") or ""), st): c["call_id"] for c, ci, st in planned}
            for rc in res.get("calls") or []:
                _, tool = guard.split_tool(str(rc.get("name") or ""))
                inp = rc.get("input") if isinstance(rc.get("input"), dict) else {}
                key = (tool, guard._call_connection(inp), guard.call_statement(inp))
                cid = want.get(key)
                if cid is None or cid in out:
                    continue  # not one of ours, or a repeat: ignored
                out[cid] = _from_call(cid, key, rc)
        for c, ci, st in planned:
            if c["call_id"] not in out:
                out[c["call_id"]] = result(c["call_id"], ci["tool"], c.get("connection") or "", st, "missing",
                                           error="no result (not called, timed out or changed)")
        return [out[c.get("call_id")] for c in calls]


def _from_call(cid, key, rc):
    from . import mcpjobs
    tool, conn, st = key
    text = str(rc.get("result") or "")
    if rc.get("result") is None:
        return result(cid, tool, conn, st, "missing", error="no result")
    if rc.get("is_error"):
        if "safety guard" in text or "read-only guard" in text:
            return result(cid, tool, conn, st, "error", error="refused by the safety guard")
        if DENIED_RX.search(text):
            return result(cid, tool, conn, st, "permission_denied", error="permission denied")
        return result(cid, tool, conn, st, "error", error=text[:200])
    if DENIED_RX.search(text[:300]) and not text.lstrip().startswith(("[", "{")):
        return result(cid, tool, conn, st, "permission_denied", error="permission denied")
    rows = mcpjobs._query_rows(rc)
    if rows is None:
        val = mcpjobs.unwrap(rc.get("result"))
        rows = val if isinstance(val, list) else None
        if rows is None:
            return result(cid, tool, conn, st, "error", error="result is not rows")
    return result(cid, tool, conn, st, "ok", rows=rows)
