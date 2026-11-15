"""Record investigations: reviewed trace plans, bounded jobs, per-step observations, saved investigations, handoff
drafts and the companion's database_context (database flow D2, plan §3; DB-10, DB-11, DB-12, DB-15, DB-17, DB-19).

Plan  make_plan(ctx, flow_id, env, connections, start={"object": <o_id>, "key": {field: value}})
  The start key is the object's declared primary key plus every column of it that the flow's mappings carry onward
  (tenant / composite fields); a missing or extra field is a PlanError listing the required fields (never a guess, no
  `id` chosen by name). Every value is checked by the typed encoder of its column (dbconnector.check_value): nothing
  is stripped, trimmed or coerced ('00123' stays text for a char column; '12;--' is declined for an integer one).
  Steps follow the flow's objects in evidence order; another object is reached only through evidenced column
  mappings (transforms / copies edges, declared foreign keys) whose transforms are deterministic: identity,
  to_string, concat of key fields with literal separators, prefix / suffix literals, and an evidenced lookup ("lookup
  <schema>.<table>.<column> by <column>"), which becomes its own bounded step on the lookup object (validated
  metadata, approved scope, <= 20 rows, counted in the budget) whose values feed the next step. A step is planned only
  when the mappings carry the whole start key to it (a partial key such as order_ref alone could match another
  tenant's record): otherwise, or for an unknown transform, it is `mapping_unresolved` with the next evidence source
  and is never queried. Each step names its connection, columns (key columns + the columns the flow's evidence names;
  never *, never a column the catalog flags as personal data), the predicate shape built by dbconnector.build_select
  from validated metadata (schema-card describe rows) and the approved scope, and a row limit.
  Limits (the stricter configured value wins: qa_data.max_rows_per_query, the guard's max_rows,
  mcp.database.trace_limits): 20 rows / step, 10 queries / plan, 2 concurrent calls, 30 s statement timeout, 120 s
  overall deadline (from the moment the job starts running), 256 KiB of sanitized values.
  The plan also states the coverage estimate, which columns are kept as sanitized values and which are masked, the
  retention, and the transport disclosure (TRANSPORT_DISCLOSURE). plan_fingerprint covers the environment, connection
  set, objects, columns, predicate shapes, mappings, limits and capability limits, never the key values. The plan
  record keeps the key's field names and a keyed fingerprint of its values (HMAC with this data home's secret: not
  anonymisation, used only to group runs); the values themselves wait in .worklog/cache/db-jobs/<plan>.key.json for
  at most dbstore.PLAN_KEY_TTL_S, and the run takes them (a later run asks for the key again).
Phases
  A step whose statement depends on values observed earlier (a lookup's result) can't be known in advance, so a job
  runs in phases: each phase is one Transport.execute of at most `concurrent` calls with its own plan pin (the job's
  pin file grows by that phase's calls; earlier entries stay claimed), built only from the start key and values
  observed in earlier phases (<= 20 per step, each checked again by the destination column's encoder). Phases never add
  an object, column, connection or call beyond the plan. Without a provider-side bound on server work (a statement
  timeout or a cancellation the connector reports `supported`, from an observed call), derived phases are disabled
  and the plan says so before it runs: only steps computable from the start key run, the rest are `not_queried`
  ("the provider has no query timeout; review a new plan for the next step").
Intent  request_run(ctx, plan_id, fingerprint, origin): runs only when the caller echoes the reviewed plan's
  fingerprint under the same access policy. Showing a plan, opening a result or polling a job never runs anything.
Jobs (data/db/jobs/<j_id>.json, never a raw value): owner, plan + fingerprint, policy generation, budget, deadline,
  state (queued, running, cancel_requested, cancelled, timed_out, completed, partial, failed, revoked), per-phase
  accounting reconciled with the pin's call ledger. One active job per plan (a second request returns it). The runner
  is `agent db-trace <job>` (started detached through safety.commands.spawn by spawn_runner; `cli db run --wait`
  waits on the job record) or run_job() with any dbconnector.Transport. Before every phase and again before
  publishing, the access context must still approve every connection with the job's policy generation, the cancel flag
  must be absent and the deadline not passed: otherwise revoked / cancelled / timed_out, and nothing from that phase
  is published (revoked: nothing from the job). Retries: none (v1 retries nothing; permission errors, guard refusals
  and invalid plans would never be retried anyway). A client deadline or a cancel only stops waiting: the job says
  "Stopped waiting; the provider query may still finish" unless the connector's cancellation is observed `supported`.
  Permission denied -> the step is permission_denied, (connection, object) is quarantined, the observation recorded.
Observations (DB-12): per planned step and run, status observed | not_observed_in_scope | not_queried |
  permission_denied | failed | truncated | mapping_unresolved, with connection, plan step, a hash of the statement
  template (values masked), observed_at (UTC, this machine), source timestamps as returned plus UTC only when their
  zone is known, coverage ("complete for the declared bounded query" below the limit, truncated at it) and sanitized
  values of the retained columns only (key columns masked; control characters, HTML and Markdown link / code / table
  syntax neutralised; <= 256 KiB per run). Rows of different connections are separate observations; observation
  times are never used to order steps. `not_observed_in_scope` is never a failure, a deletion or data loss: it comes
  with the next useful evidence checks from the flow's gaps. mcp.database.lag_signals ({"conn:schema.table":
  seconds}, explicit config only) raises an investigation signal, nothing more.
Investigations (data/db/investigations/<i_id>.json, personal: dbaccess.decide's owner + approved connections):
  objective, scope, flow id + version, plan refs, runs[] appended (never overwritten), unknowns, next action, notes
  (their own revision: a stale save is a dbstore.Conflict that keeps your text), corrections, an optional ticket link
  (only a ticket in your personal scope). Before / after only between two real single-row observations of the same
  step and column; otherwise "Before state unavailable". Identical repeated reads are not a change.
Handoff drafts (DB-15): environment, flow revision, observation window, last evidenced step, unknown boundaries,
  missing checks, next action; code-supported behaviour apart from observed record behaviour; no key, row value or
  SQL. Generated versions and your edited text are kept apart; regeneration adds a version and flags "Source evidence
  changed". Copy text passes the team redaction (redact + export.scrub) after an access check. Nothing is sent.
database_context(ctx, flow_id, investigation_id) -> {schema_version, database_context: {...} | None, reason}: the
  companion's bounded (<= 8 KiB) summary, no raw key, row or SQL.
Privacy (DB-17): raw rows stay in memory; the start key is in the plan's / job's transient key file, deleted at
  every job end (and stale plan keys after the TTL). Nothing raw reaches a record, a log line, a URL, the search index,
  a backup or an export. There is no raw-retention option. Row text and catalog comments are data, never instructions.
"""
import hashlib
import hmac
import json
import os
import re
import time

from . import common
from . import dbaccess
from . import dbconnector
from . import dbflows
from . import dbmodel
from . import dbstore

SCHEMA_VERSION = 1
DEFAULT_LIMITS = {"rows_per_step": 20, "queries": 10, "concurrent": 2, "statement_timeout_s": 30, "deadline_s": 120,
                  "value_bytes": 256 * 1024}
ACTIVE = ("queued", "running", "cancel_requested")
TERMINAL = ("cancelled", "timed_out", "completed", "partial", "failed", "revoked")
ANSWERED = ("observed", "not_observed_in_scope", "truncated")
MAX_EXTRACT = 20
QUEUE_MAX_S = 600
NOTE_MAX = 20000
VALUE_MAX = 200
CONTEXT_MAX = 8 * 1024
TRANSPORT_DISCLOSURE = ("Rows pass through the constrained internal Claude transport (the read-only MCP job): the raw "
                        "tool output is visible to that model call and its provider. Chitragupta keeps only the "
                        "sanitized observations shown here.")
RETENTION = ("Sanitized observations stay in this investigation until you delete it. Raw rows stay in memory and the "
             "start key in the job's transient file, deleted when the job ends in any state; raw retention is off and "
             "has no opt-in.")
KEY_NOTE = ("The key is stored only as a keyed fingerprint (HMAC with this data home's secret): not anonymisation, "
            "used only to group runs of the same record.")
WAIT_WORDS = "Stopped waiting; the provider query may still finish"
CLOCK_NOTE = ("Observed-at times are this machine's clock (UTC) when each answer arrived; source timestamps are each "
              "database's own and are shown as returned. Rows from different databases are separate observations, "
              "not one snapshot, and their times are never used to order steps.")
NOT_OBSERVED = ("The bounded authorized query did not return a matching row. This does not show the record is absent "
                "everywhere, deleted, lost, or that a consumer failed.")
NO_TIMEOUT = "the provider has no query timeout; review a new plan for the next step"
RERUN = "Run the trace again later: an asynchronous consumer may not have written yet."
DUMMY = {"integer": "0", "decimal": "0", "uuid": "00000000-0000-0000-0000-000000000000", "date": "2000-01-01",
         "datetime": "2000-01-01T00:00:00", "string": "x"}

_clock = time.time  # tests move it to check deadlines; the pin's own deadline always uses the real clock


class PlanError(ValueError):
    """A plan that can't be made: nothing is saved. `required` lists the key fields, `field` the one to fix."""

    def __init__(self, msg, required=None, field=None):
        super().__init__(msg)
        self.required, self.field = list(required or []), field


class RunError(ValueError):
    """A run request refused before anything was queued (fingerprint, policy, missing key)."""


class NotFound(dbflows.NotFound):
    """No visible record with that ID: unknown and not yours are the same answer."""


# --------------------------------------------------------------------------- limits and capabilities

def _posint(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def limits(cfg):
    """The plan limits: the defaults, lowered (never raised) by qa_data.max_rows_per_query, the guard's max_rows and
    mcp.database.trace_limits."""
    from .safety import mcp as guard
    cfg = cfg or {}
    out = dict(DEFAULT_LIMITS)
    for v in ((cfg.get("qa_data") or {}).get("max_rows_per_query"), guard.load_allow().get("max_rows")):
        if _posint(v):
            out["rows_per_step"] = min(out["rows_per_step"], v)
    tl = ((cfg.get("mcp") or {}).get("database") or {}).get("trace_limits")
    for k, v in (tl.items() if isinstance(tl, dict) else ()):
        if k in out and _posint(v):
            out[k] = min(out[k], v)
    return out


def capability_limits(cfg, connections):
    """Whether derived phases may run: only with a provider-side bound on server work (a statement timeout or a
    cancellation observed `supported`). A client deadline alone never counts."""
    rec = dbconnector.load_capabilities()
    conn_caps = rec.get("connections") or {}
    base = (rec.get("connector") or {}).get("capabilities") or {}
    out = {}
    for name in ("timeout", "cancellation"):
        states = []
        for c in connections:
            v = ((conn_caps.get(c) or {}).get("capabilities") or {}).get(name) or base.get(name) or {}
            states.append(v.get("state") if isinstance(v, dict) else None)
        out[name] = "supported" if states and all(s == "supported" for s in states) else \
            next((s for s in states if s and s != "supported"), "not_verified")
    mapping, _, _ = dbconnector.argument_mapping(cfg)
    timeout_arg = bool(mapping and mapping.get("timeout"))
    derived = out["cancellation"] == "supported" or (out["timeout"] == "supported" and timeout_arg)
    return {"timeout": out["timeout"], "cancellation": out["cancellation"], "timeout_argument": timeout_arg,
            "derived_phases": derived,
            "text": ("A provider-side bound on server work is available: steps that need values observed earlier may "
                     "run in later phases." if derived else
                     "No provider query timeout or cancellation is available: only steps computable from the start "
                     "key run; " + NO_TIMEOUT + ".")}


# --------------------------------------------------------------------------- transforms

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_IDENTITY = {"", "as is", "as-is", "identity", "same", "same value", "unchanged", "copy", "copied"}


def parse_transform(text, from_names=()):
    """A deterministic transform from its recorded text, or None (unknown -> mapping_unresolved)."""
    t = " ".join(str(text or "").split())
    low = t.lower()
    if low in _IDENTITY:
        return {"kind": "identity"}
    if low in ("to_string", "tostring", "to string", "str", "string") or \
            re.fullmatch(r"(?:str|string|to_?string|tostring)\(\s*%s(?:\.%s)*\s*\)" % (_IDENT, _IDENT), low) or \
            re.fullmatch(r"%s(?:\.%s)*\.to_?string\(\)" % (_IDENT, _IDENT), low):
        return {"kind": "to_string"}
    m = re.fullmatch(r"lookup\s+(%s)\.(%s)\.(%s)\s+by\s+(%s)" % (_IDENT, _IDENT, _IDENT, _IDENT), t, re.I)
    if m:
        return {"kind": "lookup", "schema": m.group(1), "table": m.group(2), "out": m.group(3), "by": m.group(4)}
    m = re.fullmatch(r"(prefix|suffix)\s+'([^'\\\x00-\x1f]{1,40})'", t, re.I)
    if m:
        return {"kind": m.group(1).lower(), "literal": m.group(2)}
    m = re.fullmatch(r"concat\((.*)\)", t, re.I)
    if m:
        parts, names = [], {str(n).lower(): n for n in from_names}
        for tok in re.findall(r"\s*('(?:[^'\\\x00-\x1f]{0,40})'|%s(?:\.%s)*)\s*(?:,|$)" % (_IDENT, _IDENT), m.group(1)):
            if tok.startswith("'"):
                parts.append({"literal": tok[1:-1]})
            elif tok.split(".")[-1].lower() in names:
                parts.append({"field": names[tok.split(".")[-1].lower()]})
            else:
                return None
        rebuilt = ", ".join("'%s'" % p["literal"] if "literal" in p else p["field"] for p in parts)
        if len([p for p in parts if "field" in p]) < 1 or \
                re.sub(r"\s+", "", rebuilt.lower()) != re.sub(r"\s+", "", re.sub(r"\b%s\." % _IDENT, "",
                                                                                     m.group(1).lower())):
            return None
        return {"kind": "concat", "parts": parts}
    return None


def _apply(tr, args):
    """Text values of one transform applied to its arguments' value lists (concat: the combinations, bounded)."""
    k = tr["kind"]
    if k in ("identity", "to_string"):
        return list(args[0])
    if k == "prefix":
        return [tr["literal"] + v for v in args[0]]
    if k == "suffix":
        return [v + tr["literal"] for v in args[0]]
    if k == "concat":
        out = [""]
        it = iter(args)
        for p in tr["parts"]:
            vals = [p["literal"]] if "literal" in p else next(it)
            out = [a + b for a in out for b in vals][:MAX_EXTRACT]
        return out
    raise ValueError("not a value transform")


# --------------------------------------------------------------------------- the key's fingerprint

def key_fingerprint(object_id, key):
    """HMAC of the start key with this data home's secret: groups runs of the same record; not anonymisation."""
    from . import dbviews
    dbviews.ensure_secret()
    body = json.dumps({"object": object_id, "key": {k: str(v) for k, v in sorted(key.items())}}, sort_keys=True)
    return hmac.new(dbviews._secret(), body.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


# --------------------------------------------------------------------------- metadata of one object

def _card_rows(conn, schema, table):
    from . import mcpjobs
    rec = common.read_json(mcpjobs.card_path(conn, "%s.%s" % (schema, table)), None)
    rows = rec.get("describe") if isinstance(rec, dict) else None
    return rows if isinstance(rows, list) else None


def _metadata(ctx, vis, oid):
    """(Metadata, node) of a visible, described object on an approved connection, from its schema card; PlanError
    otherwise."""
    n = vis.nodes.get(oid)
    if not n or n.get("kind") not in ("table", "view") or n.get("lifecycle") not in (None, "current"):
        raise PlanError("%s is not a current table or view you may read" % vis.label(oid))
    d = n.get("display") or {}
    c = d.get("connection")
    e = ctx.connections.get(c)
    if not e:
        raise PlanError("%s is not on an approved connection" % vis.label(oid))
    if not e.get("schemas"):
        raise PlanError("the approved schemas of %s are unknown (set mcp.database.scope): no record trace" % c)
    rows = _card_rows(c, d.get("schema"), d.get("name"))
    if not rows or not (n.get("attrs") or {}).get("described"):
        raise PlanError("%s has no validated column metadata yet (describe it first)" % vis.label(oid))
    try:
        meta = dbconnector.Metadata.from_describe(e.get("engine"), d.get("schema"), d.get("name"), rows,
                                                  d.get("database") or "", e.get("case_sensitive"), oid)
    except dbconnector.BuildError as x:
        raise PlanError("%s: %s" % (vis.label(oid), x))
    return meta, n


def _permitted(ctx, conn):
    e = ctx.connections[conn]
    return {"engine": e.get("engine") or "", "databases": list(e.get("databases") or []),
            "schemas": list(e.get("schemas") or []), "case_sensitive": bool(e.get("case_sensitive"))}


def _col_meta(meta, name):
    c = meta.column(name)
    return {k: c.get(k) for k in ("name", "data_type", "class", "max_length", "primary_key", "sensitive")}


def _meta_of(step, ctx=None):
    e = (ctx.connections.get(step["connection"]) if ctx else None) or {}
    return dbconnector.Metadata(step["engine"], step["schema"], step["table"], step["meta"], step.get("database") or "",
                                e.get("case_sensitive", step.get("case_sensitive", False)), step["object"])


def _check(value, col):
    """The value as text when it fits the column's declared type; a PlanError that never repeats the value."""
    try:
        return dbconnector.check_value(value, col)
    except dbconnector.BuildError:
        raise PlanError("%s is a %s column (%s): the value doesn't fit that type; nothing is stripped, trimmed or "
                        "converted" % (col.get("name"), col.get("class") or "unsupported", col.get("data_type")),
                        field=col.get("name"))


# --------------------------------------------------------------------------- the mapping graph

def _member(vis, cid):
    n = vis.nodes.get(cid) or {}
    return n.get("parent"), (n.get("display") or {}).get("member")


def _mappings(vis, flow):
    """Column-level mapping entries among the flow's nodes: [{from: [col ids], to: col id, transform (parsed or None),
    text, basis, edge}] from transforms / copies edges, and both directions of declared foreign keys between the
    flow's objects (identity)."""
    nodes = {n["id"] for n in flow.get("nodes") or []} | set(flow.get("objects") or [])
    out = []
    for e in vis.edges.values():
        if e.get("source") not in nodes or e.get("target") not in nodes:
            continue
        b = dbflows._basis(vis, e)
        if e.get("kind") in ("transforms", "copies"):
            for m in e.get("column_mapping") or []:
                frm = [c for c in m.get("from") or [] if c in vis.nodes]
                if not frm:
                    continue
                names = [_member(vis, c)[1] for c in frm]
                tr = parse_transform(m.get("transform"), names) if m.get("transform_known", True) else None
                for to in m.get("to") or []:
                    if to in vis.nodes:
                        out.append({"from": frm, "to": to, "transform": tr, "text": m.get("transform") or "as is",
                                    "basis": b, "edge": e["id"]})
        elif e.get("kind") == "foreign_key":
            for m in e.get("column_mapping") or []:
                for a, z in zip(m.get("from") or [], m.get("to") or []):
                    if a in vis.nodes and z in vis.nodes:
                        for x, y in ((z, a), (a, z)):
                            out.append({"from": [x], "to": y, "transform": {"kind": "identity"}, "text": "declared "
                                        "foreign key", "basis": b, "edge": e["id"], "fk": True})
    return out


def _resolve(start_cols, mappings):
    """{col id: expression}: what each column's value is in terms of the start key. An expression: {"start": field} |
    {"op": transform, "args": [expr]} | {"unresolved": why}. Two different mappings into one column -> unresolved."""
    expr = {c: {"start": name} for c, name in start_cols.items()}
    for _ in range(6):
        changed = False
        for m in mappings:
            if m["to"] in start_cols or not all(f in expr and "unresolved" not in expr[f] for f in m["from"]):
                continue
            if m["transform"] is None:
                new = {"unresolved": "transformation unknown: %s" % str(m["text"])[:80], "basis": m["basis"]}
            else:
                new = {"op": m["transform"], "args": [expr[f] for f in m["from"]], "basis": m["basis"],
                       "text": m["text"]}
            old = expr.get(m["to"])
            if old is None or ("unresolved" in old and "unresolved" not in new):
                expr[m["to"]] = new
                changed = True
            elif "unresolved" not in old and "unresolved" not in new and _shape(old) != _shape(new):
                expr[m["to"]] = {"unresolved": "two different mappings lead to this column"}
                changed = True
        if not changed:
            break
    return expr


def _shape(e):
    if "start" in e:
        return ["start", e["start"]]
    if "unresolved" in e:
        return ["unresolved"]
    return [e["op"], [_shape(a) for a in e["args"]]]


def _fields(e):
    if "start" in e:
        return {e["start"]}
    if "unresolved" in e:
        return set()
    return set().union(*[_fields(a) for a in e["args"]]) if e["args"] else set()


def _lookups(e):
    if "op" not in e:
        return []
    own = [e] if e["op"]["kind"] == "lookup" else []
    return own + [x for a in e["args"] for x in _lookups(a)]


def _weakest(e):
    bs = []

    def walk(x):
        if x.get("basis"):
            bs.append(x["basis"])
        for a in x.get("args") or []:
            walk(a)
    walk(e)
    return min(bs, key=lambda b: dbmodel.BASIS_RANK.get(b, 0)) if bs else "declared_metadata"


def _describe_expr(vis, e):
    if "start" in e:
        return "start key %s" % e["start"]
    if "unresolved" in e:
        return e["unresolved"]
    op = e["op"]
    inner = ", ".join(_describe_expr(vis, a) for a in e["args"])
    if op["kind"] == "identity":
        return inner
    if op["kind"] == "lookup":
        return "lookup %s.%s.%s by %s (%s)" % (op["schema"], op["table"], op["out"], op["by"], inner)
    if op["kind"] in ("prefix", "suffix"):
        return "%s '%s' (%s)" % (op["kind"], op["literal"], inner)
    return "%s(%s)" % (op["kind"], inner)


# --------------------------------------------------------------------------- the plan

def _flow_for(ctx, vis, flow_id):
    from . import dbviews
    if not dbmodel.valid_id(flow_id, "f"):
        raise PlanError("give a saved flow ID (f_...)")
    rec = dbflows.saved_flow(flow_id)
    if not rec or not rec.get("current") or not dbflows._flow_visible(vis, rec):
        raise NotFound("no such flow")
    flow, _ = dbviews._flow_visible_filter(vis, rec["current"])
    return rec, flow


def _evidence_columns(vis, flow, oid):
    names = []
    for s in flow.get("edges") or []:
        e = vis.edges.get(s["id"]) or {}
        if e.get("target") != oid and e.get("source") != oid:
            continue
        cols = e.get("columns") or {}
        for cid in list(cols.get("read") or []) + list(cols.get("written") or []) + \
                [ch.get("column") for ch in e.get("changes") or [] if isinstance(ch, dict)]:
            parent, name = _member(vis, cid)
            if parent == oid and name:
                names.append(name)
    return list(dict.fromkeys(names))


def _async_objects(flow):
    return {s["nodes"][-1] for s in flow.get("steps") or [] if s.get("kind") == "downstream_effect" and s.get("nodes")}


def _next_checks(flow, oid, async_):
    sids = {s["id"] for s in flow.get("steps") or [] if oid in (s.get("nodes") or [])}
    out = []
    for g in flow.get("gaps") or []:
        touches = sids & set(g.get("steps") or [])
        if g.get("kind") == "runtime_events_unavailable" and async_ or touches and g.get("kind") in (
                "unknown_boundary", "mapping_unresolved", "definition_unavailable", "permission_denied"):
            out.append(g.get("next"))
    if async_:
        out.append(RERUN)
    return list(dict.fromkeys(x for x in out if x))[:4]


def _step_key(oid, role, extra=""):
    return dbmodel.new_id("x", "trace-step", oid, role, extra)


def _template(adapter, meta, cols, pred_cols, limit, permitted):
    """The statement with placeholders for the values (built and checked like the real one): its shape and hash."""
    preds = [(c, dbconnector.Param(DUMMY.get(meta.column(c)["class"] or "", "x"))) for c in pred_cols]
    sql, _ = dbconnector.build_select(adapter, meta, cols, preds, limit=limit, params=True, permitted=permitted,
                                      max_rows=limit)
    return sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _base_step(ctx, vis, meta, node, role, cols, preds, lim, masked, n):
    d = node.get("display") or {}
    conn = d.get("connection")
    adapter = dbconnector.adapter_for(ctx.connections[conn].get("engine"))
    if adapter is None:
        raise PlanError("no statement adapter for %s's engine: unsupported dialects fail closed" % conn)
    sens = [c["name"] for c in meta.columns.values() if c.get("sensitive")]
    cols = [c for c in dict.fromkeys(cols) if c not in sens]
    try:
        _, h = _template(adapter, meta, cols, [p["column"] for p in preds], lim, _permitted(ctx, conn))
    except dbconnector.BuildError as x:
        raise PlanError("%s: %s" % (vis.label(node["id"]), x))
    shape = " AND ".join("%s IN (…)" % p["column"] if p.get("many") else "%s = ?" % p["column"] for p in preds)
    return {"n": n, "role": role, "object": node["id"], "object_label": vis.label(node["id"]), "connection": conn,
            "environment": d.get("environment") or "", "engine": ctx.connections[conn].get("engine"),
            "database": d.get("database") or "", "schema": d.get("schema"), "table": d.get("name"),
            "columns": cols, "key_columns": [p["column"] for p in preds],
            "retained": [c for c in cols if c not in masked and c not in [p["column"] for p in preds]],
            "masked": [c for c in cols if c in masked or c in [p["column"] for p in preds]],
            "never_selected": sens, "meta": [_col_meta(meta, c) for c in dict.fromkeys(cols + [p["column"] for p in
                                                                                             preds])],
            "predicate": preds, "predicate_shape": shape, "template_sha256": h, "limit": lim, "status": "planned", "reason": "", "next": [],
            "depends_on": [], "stage": 0}


def _unresolved_step(vis, oid, n, why, nxt, flow):
    node = vis.nodes.get(oid) or {}
    d = node.get("display") or {}
    return {"n": n, "role": "record", "object": oid, "object_label": vis.label(oid), "connection": d.get("connection"),
            "environment": d.get("environment") or "", "columns": [], "key_columns": [], "retained": [], "masked": [],
            "predicate": [], "predicate_shape": "", "template_sha256": "", "limit": 0,
            "status": "mapping_unresolved", "reason": why,
            "next": list(dict.fromkeys([nxt] + _next_checks(flow, oid, False)))[:4], "depends_on": [], "stage": 0,
            "key": _step_key(oid, "record")}


def make_plan(ctx, flow_id, env, connections, start, cfg=None, objective=None, investigation_id=None):
    """A reviewed, bounded trace plan for one record (module doc). Returns the plan record (no raw value in it)."""
    cfg = common.load_config() if cfg is None else cfg
    if not ctx.principal_id:
        raise PlanError("your identity is not confirmed (missing_identity): personal investigations need it")
    reap_stale()
    vis = dbflows.Visible(ctx, cfg=cfg)
    rec, flow = _flow_for(ctx, vis, flow_id)
    env = str(env or "").strip().lower()
    envs = sorted({e["env"] for e in ctx.connections.values()})
    if env not in envs:
        raise PlanError("environment %s has no approved connection (approved: %s)" % (env or "?", ", ".join(envs) or
                                                                                      "none"))
    used = sorted({vis.conn_of(o) for o in flow.get("objects") or [] if vis.conn_of(o)})
    if connections in (None, [], ()):
        conns = [c for c in used if ctx.connections.get(c, {}).get("env") == env]
    else:
        conns = []
        for c in connections:
            cid, why = dbaccess.resolve_connection(ctx, str(c), env)
            if not cid:
                raise PlanError("connection %s: %s" % (c, why))
            conns.append(cid)
        conns = sorted(set(conns))
    if not conns:
        raise PlanError("the flow touches no approved connection in %s" % env.upper())
    if not isinstance(start, dict) or not dbmodel.valid_id(start.get("object"), "o"):
        raise PlanError("name the start object (an o_ ID from the flow) and its key")
    oid = start["object"]
    objs = [o for o in flow.get("objects") or [] if vis.conn_of(o) in conns]
    if oid not in objs:
        raise PlanError("start from one of the flow's objects on %s: %s" % (", ".join(conns), ", ".join(
            vis.label(o) for o in objs) or "none"))
    meta, node = _metadata(ctx, vis, oid)
    mappings = _mappings(vis, flow)
    required = _required(vis, meta, oid, mappings)
    key = start.get("key") if isinstance(start.get("key"), dict) else {}
    have = {str(k) for k in key}
    if have != set(required):
        missing, extra = [r for r in required if r not in have], sorted(have - set(required))
        raise PlanError("the key of %s is %s%s%s" % (vis.label(oid), ", ".join(required),
                                                     "; missing " + ", ".join(missing) if missing else "",
                                                     "; not part of it: " + ", ".join(extra) if extra else ""),
                        required=required, field=(missing or extra or [None])[0])
    for f in required:
        _check(key[f], meta.column(f))
    lim = limits(cfg)
    caps = capability_limits(cfg, conns)
    colid = {(oid, _member(vis, cid)[1]): cid for cid, n in vis.nodes.items() if n.get("parent") == oid}
    start_cols = {colid[(oid, f)]: f for f in required if (oid, f) in colid}
    expr = _resolve(start_cols, mappings)
    asyncs = _async_objects(flow)
    steps = []
    s0 = _base_step(ctx, vis, meta, node, "start", required + _evidence_columns(vis, flow, oid),
                    [{"column": f, "from": {"start": f}, "text": "start key"} for f in required], lim["rows_per_step"],
                    set(required), 1)
    s0.update(key=_step_key(oid, "start"), mapping="the start key", basis=None, async_boundary=False)
    steps.append(s0)
    lookups = {}
    for o in objs:
        if o == oid:
            continue
        mine = {cid: e for cid, e in expr.items() if _member(vis, cid)[0] == o}
        reached = {cid: e for cid, e in mine.items() if "unresolved" not in e and _fields(e)}
        bad = [e["unresolved"] for e in mine.values() if "unresolved" in e]
        cover = set().union(*[_fields(e) for e in reached.values()]) if reached else set()
        n = len(steps) + 1
        if bad and cover != set(required):
            steps.append(_unresolved_step(vis, o, n, "mapping unresolved: %s" % bad[0], dbflows.NEXT["mapping_unresolved"],
                                          flow))
            continue
        if not reached:
            steps.append(_unresolved_step(vis, o, n, "no evidenced mapping carries %s's key to it" % vis.label(oid),
                                          dbflows.NEXT["mapping_unresolved"], flow))
            continue
        if cover != set(required):
            steps.append(_unresolved_step(
                vis, o, n, "the mappings carry only %s of the key (%s): a partial key could match another record"
                % (", ".join(sorted(cover)), ", ".join(required)), dbflows.NEXT["mapping_unresolved"], flow))
            continue
        try:
            tmeta, tnode = _metadata(ctx, vis, o)
        except PlanError as x:
            steps.append(_unresolved_step(vis, o, n, str(x), dbflows.NEXT["definition_unavailable"], flow))
            continue
        tconn = (tnode.get("display") or {}).get("connection")
        preds, deps, why = [], [], ""
        for cid, e in sorted(reached.items(), key=lambda x: _member(vis, x[0])[1]):
            cname = _member(vis, cid)[1]
            for lk in _lookups(e):
                if _lookups(lk["args"][0]):
                    why = "a lookup that needs another lookup's result is not supported"
                    break
                lid, lwhy = _lookup_step(ctx, vis, tconn, lk, lookups, steps, lim, flow)
                if lwhy:
                    why = lwhy
                    break
                lk["step"] = lid  # the lookup's values come from that step's result
                deps.append(lid)
            if why:
                break
            preds.append({"column": cname, "from": e, "text": _describe_expr(vis, e), "many": bool(_lookups(e))})
        if why:
            steps.append(_unresolved_step(vis, o, len(steps) + 1, why, dbflows.NEXT["mapping_unresolved"], flow))
            continue
        try:
            st = _base_step(ctx, vis, tmeta, tnode, "record", [p["column"] for p in preds] +
                            _evidence_columns(vis, flow, o), preds, lim["rows_per_step"],
                            {p["column"] for p in preds}, len(steps) + 1)
        except PlanError as x:
            steps.append(_unresolved_step(vis, o, len(steps) + 1, str(x), dbflows.NEXT["unknown_boundary"], flow))
            continue
        a = o in asyncs
        st.update(key=_step_key(o, "record"), depends_on=list(dict.fromkeys(deps)), stage=1 if deps else 0,
                  basis=min((_weakest(e) for e in reached.values()), key=lambda b: dbmodel.BASIS_RANK.get(b, 0)),
                  mapping="; ".join("%s ← %s" % (p["column"], p["text"]) for p in preds), async_boundary=a,
                  next=_next_checks(flow, o, a))
        steps.append(st)
    _gate_plan(steps, caps, lim)
    for s in steps:
        s["id"] = s["key"]
        if s.get("basis"):
            s["basis_label"] = dbmodel.BASIS_LABEL.get(s["basis"], "")
    runnable = [s for s in steps if s["status"] == "planned"]
    shape = {"env": env, "connections": conns, "start": {"object": oid, "fields": required}, "limits": lim,
             "derived_phases": caps["derived_phases"],
             "steps": [{k: s.get(k) for k in ("key", "role", "object", "connection", "columns", "predicate_shape",
                                              "mapping", "limit", "stage", "status", "depends_on", "template_sha256")}
                       for s in steps]}
    fp = dbmodel.fingerprint(shape)
    kf = key_fingerprint(oid, {f: key[f] for f in required})
    now = common.now_iso()
    pid = dbmodel.new_id("p")
    plan = {"id": pid, "kind": "plan", "owner": ctx.principal_id, "created_at": now,
            "flow": {"id": rec["id"], "version": flow.get("version"), "title": flow.get("title"),
                     "fingerprint": flow.get("fingerprint"), "label": flow.get("label")},
            "environment": env, "connections": conns, "policy_generation": ctx.policy_generation,
            "start": {"object": oid, "object_label": vis.label(oid), "fields": required}, "key_fingerprint": kf,
            "key_note": KEY_NOTE, "steps": steps, "limits": lim, "capabilities": caps,
            "coverage_estimate": _coverage_estimate(steps, lim, conns), "data_handling": _handling(steps),
            "retention": RETENTION, "transport_disclosure": TRANSPORT_DISCLOSURE, "fingerprint": fp,
            "queries_planned": len(runnable), "jobs": [], "active_job": None,
            "key_kept_until": _iso_epoch(time.time() + dbstore.PLAN_KEY_TTL_S), "revision": 0}
    inv = _investigation_for(ctx, plan, objective, investigation_id)
    plan["investigation"] = inv["id"]
    plan = dbstore.save("plans", plan)
    common.write_json(dbstore.plan_key_file(pid), {"plan": pid, "at": time.time(),
                                                   "key": {f: key[f] for f in required}})
    _link_plan(inv["id"], plan)
    return plan


def _required(vis, meta, oid, mappings):
    """The start key's fields: the declared primary key plus every column of the object a mapping carries onward."""
    onward = {_member(vis, c)[1] for m in mappings for c in m["from"] if _member(vis, c)[0] == oid}
    required = [c["name"] for c in meta.columns.values() if c.get("primary_key") or c["name"] in onward]
    if not required:
        raise PlanError("%s declares no key and no mapping carries one of its columns: nothing is guessed" %
                        vis.label(oid))
    return required


def key_spec(ctx, flow_id=None, start=None, env=None, cfg=None):
    """What a plan for this flow would ask for (a read: nothing is saved, nothing queried): per environment its
    approved connections, and per object of the flow the key fields with their declared types, or why it can't start
    a trace. A flow not saved yet is computed in memory from its start (a plan saves it first)."""
    vis = dbflows.Visible(ctx, cfg=cfg)
    if flow_id is not None:
        rec, flow = _flow_for(ctx, vis, flow_id)
    else:
        try:
            flow = dbflows.build_flow(ctx, start, env=env, vis=vis, cfg=cfg)
        except dbflows.FlowError as x:
            raise PlanError(str(x))
        rec = {"id": flow["id"]}
        saved = dbflows.saved_flow(flow["id"])
        flow["saved"] = bool(saved and saved.get("current"))
    mappings = _mappings(vis, flow)
    objs = []
    for o in flow.get("objects") or []:
        item = {"id": o, "label": vis.label(o), "connection": vis.conn_of(o), "environment": vis.env_of(o)}
        try:
            meta, _ = _metadata(ctx, vis, o)
            req = _required(vis, meta, o, mappings)
            item["fields"] = [{"name": f, "type": meta.column(f).get("data_type"),
                               "class": meta.column(f).get("class") or "unsupported"} for f in req]
        except PlanError as x:
            item["fields"], item["reason"] = [], str(x)
        objs.append(item)
    envs = {}
    for c, e in sorted(ctx.connections.items()):
        if c in {vis.conn_of(o) for o in flow.get("objects") or []}:
            envs.setdefault(e["env"], []).append(c)
    return {"flow": {"id": rec["id"], "title": flow.get("title"), "version": flow.get("version"),
                     "saved": flow_id is not None or bool(flow.get("saved"))},
            "environments": [{"env": k, "connections": v} for k, v in sorted(envs.items())], "objects": objs,
            "blocked": [{"id": c, "reason": why} for c, why in sorted(ctx.scope["blocked"].items())],
            "capabilities": capability_limits(cfg or common.load_config(), sorted({c for v in envs.values() for c in v})),
            "limits": limits(cfg or common.load_config()), "transport_disclosure": TRANSPORT_DISCLOSURE,
            "history_note": "Key values are sent in the request body only; they are never put in a URL or a log."}


def _lookup_step(ctx, vis, conn, lk, lookups, steps, lim, flow):
    """(step key, "") of the bounded lookup step for one lookup expression (shared by every step that needs it), or
    (None, why) when the lookup object or its columns aren't in validated metadata and approved scope."""
    op = lk["op"]
    sig = json.dumps([conn, op, _shape(lk["args"][0])], sort_keys=True)
    if sig in lookups:
        return lookups[sig], ""
    e = ctx.connections.get(conn) or {}
    want = (dbmodel.norm_ident(e.get("engine"), op["schema"], False, e.get("case_sensitive")),
            dbmodel.norm_ident(e.get("engine"), op["table"], False, e.get("case_sensitive")))
    hits = [nid for nid, n in vis.nodes.items() if n.get("kind") in ("table", "view") and
            (n.get("display") or {}).get("connection") == conn and
            (dbmodel.norm_ident(e.get("engine"), (n.get("display") or {}).get("schema"), True, e.get("case_sensitive")),
             dbmodel.norm_ident(e.get("engine"), (n.get("display") or {}).get("name"), True,
                                e.get("case_sensitive"))) == want]
    if len(hits) != 1:
        return None, "lookup object %s.%s is not in validated metadata on %s" % (op["schema"], op["table"], conn)
    try:
        meta, node = _metadata(ctx, vis, hits[0])
        meta.column(op["by"])
        meta.column(op["out"])
    except (PlanError, dbconnector.BuildError) as x:
        return None, "lookup %s.%s.%s: %s" % (op["schema"], op["table"], op["out"], x)
    pred = [{"column": meta.column(op["by"])["name"], "from": lk["args"][0], "text": _describe_expr(vis, lk["args"][0])}]
    try:
        st = _base_step(ctx, vis, meta, node, "lookup", [meta.column(op["by"])["name"], meta.column(op["out"])["name"]],
                        pred, lim["rows_per_step"], {meta.column(op["out"])["name"]}, len(steps) + 1)
    except PlanError as x:
        return None, str(x)
    k = _step_key(hits[0], "lookup", sig)
    st.update(key=k, output=meta.column(op["out"])["name"], basis=lk.get("basis"),
              mapping="lookup %s by %s ← %s" % (op["out"], op["by"], pred[0]["text"]), async_boundary=False,
              next=[])
    steps.append(st)
    lookups[sig] = k
    return k, ""


def _gate_plan(steps, caps, lim):
    """Derived phases off without a provider bound (their lookups too when nothing else needs them); the query budget
    holds: steps beyond it are not queried (visible in the plan, never silently cut)."""
    by = {s["key"]: s for s in steps}
    if not caps["derived_phases"]:
        for s in steps:
            if s["status"] == "planned" and s.get("depends_on"):
                s.update(status="not_queried", reason=NO_TIMEOUT)
        for s in steps:
            if s["role"] == "lookup" and s["status"] == "planned":
                users = [x for x in steps if s["key"] in (x.get("depends_on") or [])]
                if users and all(u["status"] != "planned" for u in users):
                    s.update(status="not_queried", reason="its value only feeds a step that can't run: " + NO_TIMEOUT)
    n = 0
    for s in sorted(steps, key=lambda x: (x.get("stage", 0), x["n"])):
        if s["status"] != "planned":
            continue
        n += 1
        if n > lim["queries"]:
            s.update(status="not_queried", reason="over the plan's budget of %d queries: review a narrower plan" %
                     lim["queries"])
    for s in steps:
        if s["status"] == "planned" and any(by[d]["status"] != "planned" for d in s.get("depends_on") or [] if d in by):
            s.update(status="not_queried", reason="a step it needs is not queried")


def _coverage_estimate(steps, lim, conns):
    run = [s for s in steps if s["status"] == "planned"]
    other = [s for s in steps if s["status"] != "planned"]
    return ("%d bounded read%s on %d connection%s, at most %d row%s each for the mapped key; %s. A result is complete "
            "only for its own bounded query." % (
                len(run), "" if len(run) == 1 else "s", len(conns), "" if len(conns) == 1 else "s",
                lim["rows_per_step"], "" if lim["rows_per_step"] == 1 else "s",
                ("%d step%s can't be queried (shown with the reason)" % (len(other), "" if len(other) == 1 else "s"))
                if other else "every object of the flow on these connections is covered"))


def _handling(steps):
    return {"retained": [{"step": s["object_label"], "columns": s["retained"]} for s in steps if s.get("retained")],
            "masked": [{"step": s["object_label"], "columns": s["masked"]} for s in steps if s.get("masked")],
            "never_selected": [{"step": s["object_label"], "columns": s["never_selected"]} for s in steps
                               if s.get("never_selected")],
            "text": "Kept: the listed columns, sanitized. Masked: key columns and lookup results (never stored). Never "
                    "selected: columns the catalog flags as personal data."}


# --------------------------------------------------------------------------- investigations

def _visible(ctx, rec):
    return isinstance(rec, dict) and dbaccess.decide(ctx, rec)["allowed"]


def load_visible(ctx, kind, rid):
    """A personal record (plan, job, investigation) when it is yours and its connections are approved now; NotFound
    otherwise (a record of someone else is indistinguishable from one that doesn't exist)."""
    if not dbmodel.valid_id(rid, {"plans": "p", "jobs": "j", "investigations": "i"}[kind]):
        raise NotFound("no such %s" % kind[:-1])
    rec = dbstore.load(kind, rid)
    if not _visible(ctx, rec):
        raise NotFound("no such %s" % kind[:-1])
    return rec


def _investigation_for(ctx, plan, objective, inv_id):
    if inv_id:
        inv = load_visible(ctx, "investigations", inv_id)
        if inv["flow"]["id"] != plan["flow"]["id"] or inv.get("key_fingerprint") != plan["key_fingerprint"]:
            raise PlanError("that investigation follows another flow or record")
        return inv
    for rec in dbstore.iter_records("investigations", limit=1000):
        if rec.get("owner") == ctx.principal_id and (rec.get("flow") or {}).get("id") == plan["flow"]["id"] and \
                rec.get("key_fingerprint") == plan["key_fingerprint"] and \
                (rec.get("scope") or {}).get("environment") == plan["environment"] and not rec.get("superseded_by"):
            return rec
    inv = {"id": dbmodel.new_id("i"), "kind": "investigation", "owner": ctx.principal_id,
           "created_at": common.now_iso(),
           "objective": _clean_text(objective, 300) or "Trace one %s record through %s" % (
               plan["start"]["object_label"], plan["flow"]["title"] or "the flow"),
           "scope": {"environment": plan["environment"], "connections": list(plan["connections"])},
           "flow": dict(plan["flow"]), "start": dict(plan["start"]), "key_fingerprint": plan["key_fingerprint"],
           "key_note": KEY_NOTE, "plans": [], "runs": [], "unknowns": [], "next_action": "",
           "notes": {"text": "", "revision": 0, "at": ""}, "corrections": [], "superseded_by": None, "ticket": None,
           "handoff": {"generated": [], "edited": None, "source_changed": False}, "revision": 0}
    return dbstore.save("investigations", inv)


def _update(kind, rid, fn, tries=5):
    """Load, change (fn(rec) -> rec), save; a concurrent save is retried on the newer record (fn must be idempotent
    over the newer record: it appends or sets, never replaces what someone else wrote)."""
    for _ in range(tries):
        rec = dbstore.load(kind, rid)
        if not rec:  # dbstore.load reads a missing file as {}
            raise NotFound("no such %s" % kind[:-1])
        new = fn(dict(rec))
        if new is None:
            return rec
        try:
            return dbstore.save(kind, new, expected_revision=rec.get("revision") or 0)
        except dbstore.Conflict:
            continue
    raise dbstore.Busy("the record kept changing; try again")


def _link_plan(inv_id, plan):
    def fn(inv):
        if plan["id"] in inv.get("plans") or []:
            return None
        sc = dict(inv.get("scope") or {})
        sc["connections"] = sorted(set(sc.get("connections") or []) | set(plan["connections"]))
        return dict(inv, plans=list(inv.get("plans") or []) + [plan["id"]], scope=sc,
                    flow=dict(plan["flow"]))
    _update("investigations", inv_id, fn)


def _clean_text(s, n):
    s = " ".join(str(s or "").split())
    return s[:n]


def note_save(ctx, inv_id, text, revision):
    """Save your notes when `revision` is the notes' revision you read; else dbstore.Conflict (current, your text)."""
    inv = load_visible(ctx, "investigations", inv_id)
    text = str(text or "")[:NOTE_MAX]
    cur = inv.get("notes") or {"text": "", "revision": 0}
    if int(revision if revision is not None else -1) != int(cur.get("revision") or 0):
        raise dbstore.Conflict(cur, {"text": text, "revision": revision})

    def fn(rec):
        n = rec.get("notes") or {}
        if int(n.get("revision") or 0) != int(cur.get("revision") or 0):
            raise dbstore.Conflict(n, {"text": text, "revision": revision})
        return dict(rec, notes={"text": text, "revision": int(n.get("revision") or 0) + 1, "at": common.now_iso()})
    return _update("investigations", inv_id, fn)


def add_correction(ctx, inv_id, text, supersedes=None):
    """A correction you record: kept beside what it corrects (nothing earlier is overwritten)."""
    load_visible(ctx, "investigations", inv_id)
    entry = {"at": common.now_iso(), "text": str(text or "")[:2000],
             "supersedes": supersedes if isinstance(supersedes, str) and dbmodel.valid_id(supersedes) else None}
    return _update("investigations", inv_id, lambda r: dict(r, corrections=list(r.get("corrections") or []) + [entry]))


def attach(ctx, inv_id, ticket, cfg=None):
    """Link the investigation to one of YOUR tickets (api.scope_of: visible to you), or unlink it (ticket None)."""
    from .dashboard import api
    load_visible(ctx, "investigations", inv_id)
    if ticket is not None:
        key = str(ticket).strip().upper()
        if not api.TICKET_RE.match(key) or not api.scope_of(key, cfg).get("visible"):
            raise PlanError("%s is not a ticket in your personal scope; an investigation is attached only to your own "
                            "tickets" % key[:20])
        ticket = key
    return _update("investigations", inv_id, lambda r: dict(r, ticket=ticket))


# --------------------------------------------------------------------------- run intent and jobs

def request_run(ctx, plan_id, fingerprint, origin="cli", start=None):
    """Queue a job for a reviewed plan when the caller echoes its fingerprint (module doc). Returns the job record (the
    active one when the plan already has one). `start(job_id)` launches the runner."""
    reap_stale()
    plan = load_visible(ctx, "plans", plan_id)
    if not isinstance(fingerprint, str) or not hmac.compare_digest(fingerprint, plan["fingerprint"]):
        raise RunError("that fingerprint is not this plan's: review the plan again (nothing ran)")
    if plan["policy_generation"] != ctx.policy_generation:
        raise RunError("the access policy changed since this plan was reviewed: review a new plan (nothing ran)")
    act = plan.get("active_job")
    if act:
        job = dbstore.load("jobs", act)
        if not job or job.get("state") in ACTIVE:
            return job or {"id": act, "state": "queued", "plan": plan_id}
    if not [s for s in plan["steps"] if s["status"] == "planned"]:
        raise RunError("nothing in this plan can be queried (see each step's reason)")
    kp = dbstore.plan_key_file(plan_id)
    key = common.read_json(kp, None)
    if not isinstance(key, dict) or not isinstance(key.get("key"), dict):
        raise RunError("the start key is not kept after a run or after an hour: review the plan again with the key")
    jid = dbmodel.new_id("j")

    def claim(p):
        a = p.get("active_job")
        if a and (dbstore.load("jobs", a) or {"state": "queued"}).get("state") in ACTIVE:
            return None
        return dict(p, active_job=jid, jobs=(list(p.get("jobs") or []) + [jid])[-50:])
    plan = _update("plans", plan_id, claim)
    if plan.get("active_job") != jid:
        return dbstore.load("jobs", plan["active_job"]) or {"id": plan["active_job"], "state": "queued", "plan": plan_id}
    lim = plan["limits"]
    job = {"id": jid, "kind": "job", "owner": ctx.principal_id, "plan": plan_id, "plan_fingerprint": plan["fingerprint"],
           "investigation": plan.get("investigation"), "connections": list(plan["connections"]),
           "environment": plan["environment"], "policy_generation": plan["policy_generation"],
           "origin": str(origin or "")[:20], "created_at": common.now_iso(), "queued_epoch": time.time(),
           "started_at": "", "ended_at": "", "deadline_epoch": None, "state": "queued",
           "budget": {"queries_allowed": min(lim["queries"], plan["queries_planned"]), "queries_used": 0,
                      "bytes_allowed": lim["value_bytes"], "bytes_used": 0, "rows_per_step": lim["rows_per_step"],
                      "concurrent": lim["concurrent"], "deadline_s": lim["deadline_s"]},
           "phases": [], "accounting": {"planned": plan["queries_planned"], "sent": 0, "answered": 0, "ledger": 0,
                                        "reconciled": True},
           "retries": 0, "retry_policy": "v1 retries nothing", "in_flight": None, "error": "", "revision": 0}
    job = dbstore.save("jobs", job)
    common.write_json(dbstore.job_file(jid, ".key.json"), key)
    _delete(kp)
    if start is not None:
        try:
            start(jid)
        except Exception as x:  # noqa: BLE001 - a runner that can't start ends the job, honestly
            _finish(jid, "failed", error="the runner could not start: %s" % str(x)[:160])
            raise RunError("the trace could not start: %s" % str(x)[:160])
    return dbstore.load("jobs", jid) or job


def request_cancel(ctx, job_id):
    """Stop a job: no new call is made (the pin refuses them) and nothing from an unfinished phase is published. The
    in-flight provider call, if any, is only no longer awaited."""
    job = load_visible(ctx, "jobs", job_id)
    if job["state"] not in ACTIVE:
        return job
    common.write_text(dbstore.job_file(job_id, ".cancel"), common.now_iso() + "\n")

    def fn(j):
        if j["state"] not in ACTIVE or j["state"] == "cancel_requested":
            return None
        return dict(j, state="cancel_requested", cancel_requested_at=common.now_iso())
    return _update("jobs", job_id, fn)


def spawn_agent(cmd, *args, cfg=None):
    """Start `agent <cmd> <args>` (db-trace, db-discover) detached and windowless through safety.commands.spawn
    (purpose self); never waits."""
    from .safety import commands
    if cmd not in ("db-trace", "db-discover"):
        raise ValueError("not a database agent command")
    cfg = common.load_config() if cfg is None else cfg
    argv = common.self_command("agent", cmd, *args, cfg=cfg, windowless=True)
    cwd = common.home() if os.path.isdir(common.home()) else None
    commands.spawn(argv, cwd=cwd, cfg=cfg)


def spawn_runner(job_id, cfg=None):
    """Start `agent db-trace <job>` detached (the runner of one queued job)."""
    spawn_agent("db-trace", job_id, cfg=cfg)


def wait_job(job_id, timeout_s=None, poll_s=1.0, sleep=time.sleep):
    """Read the job record until it ends (a read: no query, no enqueue). Returns the last record."""
    end = time.time() + (timeout_s if timeout_s is not None else DEFAULT_LIMITS["deadline_s"] + QUEUE_MAX_S)
    while True:
        job = dbstore.load("jobs", job_id) if dbmodel.valid_id(job_id, "j") else None
        if not job or job.get("state") in TERMINAL or time.time() >= end:
            return job
        sleep(poll_s)


def agent_main(cmd, argv):
    """`agent db-trace <job>`: run one queued job with the database MCP; `agent db-discover`: run the queued metadata
    discovery requests. Both under the agent lock, as explicit runs (counted as manual MCP calls), then the MCP budget
    counters are saved."""
    from . import modelbudget
    from .agent import Lock
    job_id = argv[0] if argv else ""
    if cmd == "db-trace" and (not dbmodel.valid_id(job_id, "j") or not dbstore.load("jobs", job_id)):
        print("no such job")
        return 2
    os.environ["CHITRAGUPTA_MCP_MANUAL"] = os.environ["WORKLOG_MCP_MANUAL"] = "1"
    lock = Lock()
    if not lock.acquire_wait():
        if cmd == "db-trace":
            _finish(job_id, "failed", error="the agent stayed busy; request the run again")
        print("agent busy")
        return 1
    try:
        cfg = common.load_config()
        health = common.read_json(common.wl("health.json"), {}) or {}
        if cmd == "db-trace":
            job = run_job(job_id, dbconnector.McpJobTransport(), cfg, health)
            print("job %s: %s" % (job_id, (job or {}).get("state")))
        else:
            res = dbflows.run_refresh(dbconnector.McpJobTransport(), cfg, health)
            print("%d discovery request(s) run" % len(res))
        modelbudget.drain(health, cfg)
        common.write_json(common.wl("health.json"), health)
        return 0
    finally:
        lock.release()


def reap_stale(now=None):
    """Jobs whose runner never started or stopped without finishing end as failed (their transient files deleted);
    plan keys older than PLAN_KEY_TTL_S are deleted. Runs on write paths only (plan, run), never on a read."""
    now = time.time() if now is None else now
    d = dbstore.jobs_dir()
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for f in names:
        if re.fullmatch(r"p_[0-9a-f]{8,40}\.key\.json", f):
            p = os.path.join(d, f)
            try:
                if now - os.path.getmtime(p) > dbstore.PLAN_KEY_TTL_S:
                    _delete(p)
            except OSError:
                pass
    for job in dbstore.iter_records("jobs", limit=1000):
        st = job.get("state")
        if st == "queued" and now - float(job.get("queued_epoch") or now) > QUEUE_MAX_S or \
                st in ("running", "cancel_requested") and job.get("deadline_epoch") and \
                now > float(job["deadline_epoch"]) + 300:
            _finish(job["id"], "failed", error="the runner stopped without finishing; nothing more is published")


def _delete(p):
    from .safety import paths
    try:
        if os.path.exists(p):
            paths.delete(p, caller="dbtrace")
    except OSError:
        pass


def _finish(job_id, state, error="", **extra):
    def fn(j):
        if j["state"] in TERMINAL:
            return None
        return dict(j, state=state, ended_at=common.now_iso(), error=error or j.get("error") or "", **extra)
    try:
        job = _update("jobs", job_id, fn)
    finally:
        dbstore.delete_job_files(job_id)
    plan_id = (job or {}).get("plan")
    if plan_id:
        try:
            _update("plans", plan_id, lambda p: dict(p, active_job=None) if p.get("active_job") == job_id else None)
        except NotFound:
            pass
    return job


# --------------------------------------------------------------------------- the runner

def _gate(job, plan, deadline):
    """None, or (state, reason) when the job must stop: access, policy generation, cancel flag, deadline."""
    cfg = common.load_config()
    ctx = dbaccess.AccessContext(cfg)
    if ctx.principal_id != job["owner"]:
        return "revoked", "your identity changed"
    lost = [c for c in plan["connections"] if c not in ctx.connections]
    if lost:
        return "revoked", "access to %s changed" % ", ".join(lost)
    if ctx.policy_generation != job["policy_generation"]:
        return "revoked", "the access policy changed during the job"
    if os.path.exists(dbstore.job_file(job["id"], ".cancel")):
        return "cancelled", "you cancelled the job"
    if deadline is not None and _clock() > deadline:
        return "timed_out", "the job's %d s deadline passed" % plan["limits"]["deadline_s"]
    return None


def run_job(job_id, transport, cfg=None, health=None):
    """Run one queued job through `transport` (module doc). Never runs a job twice; its transient files are deleted
    when it ends, in every state. Returns the job record."""
    cfg = common.load_config() if cfg is None else cfg
    health = {} if health is None else health
    job = dbstore.load("jobs", job_id) if dbmodel.valid_id(job_id, "j") else None
    if not job:  # dbstore.load reads a missing file as {}
        return None
    if job["state"] not in ("queued", "cancel_requested"):
        return job
    try:
        return _run(job, transport, cfg, health)
    except Exception as x:  # noqa: BLE001 - a crashed runner ends the job; nothing half-done is published
        common.log("dbtrace", "job %s failed: %s" % (job_id, x.__class__.__name__))
        return _finish(job_id, "failed", error="the runner failed (%s); nothing from it was published" %
                       x.__class__.__name__)
    finally:
        dbstore.delete_job_files(job_id)


def _run(job, transport, cfg, health):
    from . import mcpjobs
    from .safety import mcp as guard
    jid = job["id"]
    plan = dbstore.load("plans", job["plan"])
    keyrec = common.read_json(dbstore.job_file(jid, ".key.json"), None)
    if not plan or not isinstance(keyrec, dict) or not isinstance(keyrec.get("key"), dict):
        return _finish(jid, "failed", error="the plan or its transient key is gone: review the plan again")
    key = keyrec["key"]
    g = _gate(job, plan, None)
    if g:  # stopped before any call: a cancelled run is still kept (nothing read); a revoked one publishes nothing
        run = None
        if g[0] != "revoked":
            run = _publish(job, plan, _observations(job, plan, {}, g, None, cfg), g[0], None)
        return _finish(jid, g[0], error=g[1], in_flight=None, run=run["n"] if run else None)
    lim = plan["limits"]
    deadline = _clock() + lim["deadline_s"]
    job = _update("jobs", jid, lambda j: dict(j, state="running" if j["state"] == "queued" else j["state"],
                                              started_at=common.now_iso(), deadline_epoch=deadline))
    srv = mcpjobs.server_name(cfg, "database")
    steps = {s["key"]: s for s in plan["steps"]}
    results, extracted, pin_calls, phases = {}, {}, [], []
    stop, in_flight, used, broken = None, None, 0, ""
    order = sorted((s for s in plan["steps"] if s["status"] == "planned"), key=lambda s: (s.get("stage", 0), s["n"]))
    for stage in sorted({s.get("stage", 0) for s in order}):
        todo = []
        for s in (x for x in order if x.get("stage", 0) == stage):
            calls = _call_for(s, steps, key, extracted, results, plan, cfg)
            if calls is not None:
                todo.append((s, calls))
        for i in range(0, len(todo), max(1, lim["concurrent"])):
            chunk = todo[i:i + lim["concurrent"]]
            g = _gate(job, plan, deadline)
            if g:
                stop = g
                break
            if used + len(chunk) > job["budget"]["queries_allowed"] or broken:
                for s, _ in chunk:
                    results[s["key"]] = {"status": "not_queried", "reason": "the transport failed earlier in this job "
                                         "(nothing is retried)" if broken else "over the plan's query budget"}
                continue
            calls = [c for _, c in chunk]
            pin_calls += [{"tool": c["tool"], "connection": c["connection"], "sha256": c["sha256"]} for c in calls]
            common.write_json(dbstore.job_file(jid, ".pin.json"), {
                "schema_version": 1, "job": jid, "server": srv, "policy_generation": job["policy_generation"],
                "deadline_epoch": time.time() + max(1.0, deadline - _clock()), "calls": pin_calls})
            phase = {"n": len(phases) + 1, "stage": stage, "calls": len(calls), "at": common.now_iso()}
            out, err = [], ""
            try:
                out = transport.execute("db-trace-" + jid, [c["call"] for c in calls], cfg, health,
                                        pin=dbstore.job_file(jid, ".pin.json"),
                                        deadline_s=max(1, int(deadline - _clock())), keep_stream=False)
            except Exception as x:  # noqa: BLE001 - the transport failed: its calls failed, nothing is retried
                err = broken = x.__class__.__name__  # never the message: it may echo a statement or a value
            used += len(calls)
            answered_at = _utc_now()
            ledger = _ledger_lines(jid)
            got = {r.get("call_id"): r for r in out or [] if isinstance(r, dict)}
            phase.update(answered=sum(1 for r in got.values() if r.get("status") in ("ok", "permission_denied")),
                         ledger=ledger)
            phases.append(phase)
            g = _gate(job, plan, deadline)
            if g:  # nothing from this phase is published
                stop = g
                in_flight = {"phase": phase["n"], "calls": len(calls), "cancelled_at_provider": False,
                             "text": _stop_words(g[0], plan)}
                for s, _ in chunk:
                    results[s["key"]] = {"status": "not_queried", "reason": "%s: %s" % (g[1], WAIT_WORDS),
                                         "discarded": True}
                break
            for s, c in chunk:
                r = got.get(c["call"]["call_id"])
                results[s["key"]] = _result(s, r, err, answered_at, cfg)
                if s["role"] == "lookup" and results[s["key"]]["status"] in ("observed", "truncated"):
                    extracted[s["key"]] = results[s["key"]].pop("_values", [])
            _account(jid, used, phases)
        if stop:
            break
    if stop is None:  # a late result from an older policy generation is never published
        g = _gate(job, plan, None)
        if g and g[0] == "revoked":
            stop = g
    if stop and stop[0] == "revoked":
        return _finish(jid, "revoked", error=stop[1], phases=phases, in_flight=in_flight,
                       accounting=_accounting(jid, plan, used, phases))
    obs = _observations(job, plan, results, stop, in_flight, cfg)
    state = stop[0] if stop else _state(plan, obs)
    run = _publish(job, plan, obs, state, in_flight)
    return _finish(jid, state, error=stop[1] if stop else "", phases=phases, in_flight=in_flight,
                   accounting=_accounting(jid, plan, used, phases), run=run["n"] if run else None,
                   observations=len(obs))


def _utc_now():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_epoch(e):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(e).astimezone().isoformat(timespec="seconds")


def _stop_words(state, plan):
    if state == "cancelled":
        return "Cancellation requested: no further call was made. " + WAIT_WORDS + "."
    if state == "timed_out":
        return "Deadline reached: " + WAIT_WORDS + " (a client deadline is not a server cancellation)."
    return "Access changed: nothing from this job is published. " + WAIT_WORDS + "."


def _ledger_lines(jid):
    try:
        return len([x for x in common.read_text(dbstore.job_file(jid, ".calls.jsonl")).splitlines() if x.strip()])
    except (OSError, ValueError):
        return 0


def _account(jid, used, phases):
    _update("jobs", jid, lambda j: dict(j, phases=phases, budget=dict(j["budget"], queries_used=used)))


def _accounting(jid, plan, used, phases):
    ledger = phases[-1]["ledger"] if phases else 0
    answered = sum(p.get("answered", 0) for p in phases)
    return {"planned": plan["queries_planned"], "sent": used, "answered": answered, "ledger": ledger,
            "reconciled": answered <= ledger <= used,
            "text": "%d of %d planned call(s) sent, %d answered, %d in the guard's ledger" % (
                used, plan["queries_planned"], answered, ledger)}


def _values_for(expr, key, extracted):
    if "start" in expr:
        v = key.get(expr["start"])
        return [] if v is None or isinstance(v, bool) else [str(v)]
    op = expr["op"]
    if op["kind"] == "lookup":
        return list(extracted.get(expr.get("step"), []))
    return _apply(op, [_values_for(a, key, extracted) for a in expr["args"]])


def _call_for(s, steps, key, extracted, results, plan, cfg):
    """The call of one step from the start key and earlier phases' validated values, or None (its result is then
    recorded: a value that doesn't fit, a lookup that found nothing)."""
    for d in s.get("depends_on") or []:
        r = results.get(d) or {}
        if r.get("status") not in ("observed", "truncated") or not extracted.get(d):
            results[s["key"]] = {"status": "not_queried", "reason": "the lookup it needs %s" % (
                "found no value" if r.get("status") == "not_observed_in_scope" else "has no result")}
            return None
    meta = _meta_of(s)
    preds = []
    try:
        for p in s["predicate"]:
            col = meta.column(p["column"])
            vals = list(dict.fromkeys(_values_for(p["from"], key, extracted)))[:MAX_EXTRACT]
            if not vals:
                raise dbconnector.BuildError("no value for %s" % col["name"])
            vals = [dbconnector.check_value(v, col) for v in vals]
            preds.append((col["name"], vals if len(vals) > 1 or p.get("many") else vals[0]))
        mapping, errs, _ = dbconnector.argument_mapping(cfg)
        if mapping is None:
            raise dbconnector.BuildError("argument mapping invalid: %s" % errs[0])
        use_params = bool(mapping.get("params"))
        ctx_conn = dbaccess.AccessContext(cfg).connections.get(s["connection"])
        if not ctx_conn:
            raise dbconnector.BuildError("connection not approved now")
        permitted = {"engine": ctx_conn.get("engine") or "", "databases": list(ctx_conn.get("databases") or []),
                     "schemas": list(ctx_conn.get("schemas") or []), "case_sensitive": bool(ctx_conn.get("case_sensitive"))}
        sql, bound = dbconnector.build_select(dbconnector.adapter_for(s["engine"]), meta, s["columns"], preds,
                                              limit=s["limit"], params=use_params, permitted=permitted,
                                              max_rows=s["limit"])
        call = {"call_id": s["key"], "role": "query", "connection": s["connection"], "sql": sql, "limit": s["limit"],
                "timeout": plan["limits"]["statement_timeout_s"]}
        if bound is not None:
            call["params"] = bound
        ci = dbconnector.call_input(cfg, call)
    except dbconnector.BuildError as x:
        results[s["key"]] = {"status": "mapping_unresolved", "reason": "a mapped value doesn't fit or the statement "
                                                                       "can't be built safely (%s)" % x.gap}
        return None
    from .safety import mcp as guard
    return {"call": call, "tool": ci["tool"], "connection": s["connection"], "sha256": guard.call_digest(ci["input"])}


def _result(s, r, err, at, cfg):
    """One step's outcome from its call result (raw rows stay here, in memory)."""
    if r is None:
        return {"status": "failed", "observed_at": at,
                "reason": ("the transport failed: %s" % err) if err else "no result: %s" % WAIT_WORDS}
    st = r.get("status")
    if st == "permission_denied":
        dbaccess.quarantine(s["connection"], s["object"], "permission denied on a record trace")
        _observe(s["connection"], "permission_denied")
        return {"status": "permission_denied", "observed_at": at, "reason": "access denied by the database"}
    if st == "missing":
        return {"status": "failed", "observed_at": at, "reason": "no result: %s" % WAIT_WORDS}
    if st != "ok":
        why = str(r.get("error") or "error")
        return {"status": "failed", "observed_at": at,
                "reason": "refused by the safety guard (never retried)" if "safety guard" in why else
                "the provider answered with an error (not retried)"}
    rows = [x for x in r.get("rows") or [] if isinstance(x, dict)]
    _observe(s["connection"], "ok")
    dbaccess.reconcile(s["connection"], s["object"])
    out = {"observed_at": at, "rows": len(rows), "_rows": rows}
    if not rows:
        out["status"] = "not_observed_in_scope"
    elif len(rows) >= s["limit"]:
        out["status"] = "truncated"
    else:
        out["status"] = "observed"
    if s["role"] == "lookup":
        vals = []
        for x in rows:
            v = x.get(s["output"]) if s["output"] in x else next((x[k] for k in x if k.lower() == s["output"].lower()),
                                                                   None)
            if isinstance(v, (str, int)) and not isinstance(v, bool):
                vals.append(str(v))
        out["_values"] = list(dict.fromkeys(vals))[:MAX_EXTRACT]
    return out


def _observe(conn, outcome):
    try:
        with dbstore.lock():
            rec = dbconnector.load_capabilities()
            dbconnector.observe_call(rec, "row_query", outcome, connection=conn,
                                     reason="record trace read" if outcome == "ok" else "record trace: access denied")
            dbstore.write_file("capabilities", rec)
    except dbstore.Busy:
        pass


# --------------------------------------------------------------------------- observations

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sanitize(v):
    """One row value as inert text: control characters spelled out (\\xNN), HTML angle brackets and the Markdown
    characters that make links, images, code and tables escaped, at most VALUE_MAX characters. Numbers stay numbers."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    cut = len(s) > VALUE_MAX
    s = s[:VALUE_MAX]
    s = re.sub(r"([\\`\[\]|])", r"\\\1", s)
    s = _CTRL.sub(lambda m: "\\x%02x" % ord(m.group(0)), s)
    s = s.replace("<", "‹").replace(">", "›")
    return s + ("…" if cut else "")


def _source_times(s, rows):
    out = []
    dt = {c["name"] for c in s.get("meta") or [] if c.get("class") in ("datetime", "date")}
    for c in s.get("retained") or []:
        if c not in dt:
            continue
        for x in rows[:1]:
            v = x.get(c)
            if v is None:
                continue
            txt = sanitize(str(v))
            ts = common.parse_ts(str(v)) if re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", str(v)) else None
            if ts is not None:
                import datetime as _dt
                utc = ts.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                out.append({"column": c, "as_returned": txt, "utc": utc, "zone": "known"})
            else:
                out.append({"column": c, "as_returned": txt, "utc": "", "zone": "zone unknown"})
    return out


def _lag_signal(cfg, s, start_obs, observed_at):
    sig = (((cfg or {}).get("mcp") or {}).get("database") or {}).get("lag_signals")
    if not isinstance(sig, dict):
        return []
    lag = sig.get("%s:%s.%s" % (s["connection"], s.get("schema"), s.get("table")))
    if not _posint(lag):
        return []
    known = [t for t in (start_obs or {}).get("source_times") or [] if t.get("utc")]
    if not known:
        return ["A lag of %d s is configured, but no source time with a known zone was observed: no signal." % lag]
    since = common.parse_ts(known[0]["utc"])
    now = common.parse_ts(observed_at)
    if since and now and (now - since).total_seconds() > lag:
        return ["Investigation signal: not observed %d min after %s; the configured lag is %d s. This compares a "
                "database's timestamp with this machine's clock and is not evidence of a failure." % (
                    int((now - since).total_seconds() // 60), known[0]["column"], lag)]
    return []


def _observations(job, plan, results, stop, in_flight, cfg):
    budget, used, cut = plan["limits"]["value_bytes"], 0, False
    out, start_obs = [], None
    for s in plan["steps"]:
        r = results.get(s["key"])
        if s["status"] != "planned":
            r = {"status": s["status"], "reason": s["reason"]}
        elif r is None:
            r = {"status": "not_queried", "reason": ("the job stopped before this step (%s)" % stop[1]) if stop else
                 "not reached"}
        rows = r.pop("_rows", None) or []
        vals = []
        for x in rows[:s.get("limit") or 0]:
            item = {c: sanitize(x.get(c)) for c in s.get("retained") or [] if c in x}
            sz = len(json.dumps(item, ensure_ascii=False))
            if used + sz > budget:
                cut = True
                break
            used += sz
            if item:
                vals.append(item)
        st = r["status"]
        o = {"id": dbmodel.new_id("x", "obs", job["id"], s["key"]), "step": s["key"], "n": s["n"], "role": s["role"],
             "object_label": s["object_label"], "connection": s.get("connection"), "environment": s.get("environment"),
             "status": st, "status_label": dbmodel.STEP_LABEL.get(st, st), "plan": plan["id"],
             "template_sha256": s.get("template_sha256") or "", "observed_at": r.get("observed_at") or "",
             "rows": r.get("rows"), "limit": s.get("limit"), "values": vals, "masked": list(s.get("masked") or []),
             "source_times": _source_times(s, rows), "reason": r.get("reason") or "",
             "async_boundary": bool(s.get("async_boundary")), "next": [], "signals": []}
        o["coverage"] = {"observed": "complete", "not_observed_in_scope": "complete", "truncated": "partial"}.get(
            st, "not_checked")
        o["coverage_label"] = {"complete": dbmodel.COVERAGE_LABEL["complete"],
                               "partial": "Truncated: the row limit (%s) was hit" % s.get("limit")}.get(
            o["coverage"], "Not checked")
        if st == "not_observed_in_scope":
            o["reason"] = NOT_OBSERVED
            o["next"] = list(s.get("next") or [])
            o["signals"] = _lag_signal(cfg, s, start_obs, o["observed_at"]) if s.get("async_boundary") else []
        elif st in ("mapping_unresolved", "not_queried", "permission_denied", "failed"):
            o["next"] = list(s.get("next") or [])[:3]
        if s["role"] == "start":
            start_obs = o
        out.append(o)
    if cut:
        for o in out:
            o["values_cut"] = True
    return out


def _state(plan, obs):
    runnable = [o for o in obs if any(s["key"] == o["step"] and s["status"] == "planned" for s in plan["steps"])]
    if runnable and all(o["status"] in ANSWERED for o in runnable):
        return "completed"
    if runnable and not any(o["status"] in ANSWERED + ("permission_denied",) for o in runnable):
        return "failed"
    return "partial"


def _publish(job, plan, obs, state, in_flight):
    """Append the run to the investigation (earlier runs untouched)."""
    times = sorted(o["observed_at"] for o in obs if o.get("observed_at"))
    unknowns = list(dict.fromkeys("%s: %s" % (o["object_label"], o["reason"]) for o in obs
                                  if o["status"] in ("mapping_unresolved", "not_queried", "failed",
                                                     "permission_denied")))[:10]
    first = [o for o in obs if o["status"] == "not_observed_in_scope"] + \
        [o for o in obs if o["status"] != "not_observed_in_scope"]  # the open record question leads
    checks = list(dict.fromkeys(x for o in first for x in o.get("next") or []))[:6]
    if state in ("cancelled", "timed_out"):
        checks.insert(0, "Run a new trace when you are ready: earlier observations are kept.")

    def fn(inv):
        n = len(inv.get("runs") or []) + 1
        if any(r.get("job") == job["id"] for r in inv.get("runs") or []):
            return None
        run = {"n": n, "job": job["id"], "plan": plan["id"], "plan_fingerprint": plan["fingerprint"], "state": state,
               "state_label": dbmodel.JOB_LABEL.get(state, state), "started_at": job.get("started_at") or "",
               "ended_at": common.now_iso(),
               "window": {"from": times[0] if times else "", "to": times[-1] if times else "", "note": CLOCK_NOTE},
               "observations": obs, "unknowns": unknowns, "next_checks": checks, "in_flight": in_flight,
               "limits": dict(plan["limits"]), "connections": list(plan["connections"])}
        return dict(inv, runs=list(inv.get("runs") or []) + [run], unknowns=unknowns,
                    next_action=checks[0] if checks else "", last_run_at=run["ended_at"])
    if not plan.get("investigation"):
        return None
    inv = _update("investigations", plan["investigation"], fn)
    return next((r for r in inv.get("runs") or [] if r.get("job") == job["id"]), None)


# --------------------------------------------------------------------------- reading investigations

def changes(inv):
    """Before / after per step and column only between two real single-row observations; first observations and
    unchanged repeats are said as such, never as transitions."""
    out = []
    seq = {}
    for run in inv.get("runs") or []:
        for o in run.get("observations") or []:
            seq.setdefault(o["step"], []).append((run["n"], o))
    for step, lst in seq.items():
        prev = None
        for n, o in lst:
            if o["status"] not in ("observed", "truncated"):
                if o["status"] == "not_observed_in_scope":
                    prev = (n, o)
                continue
            if prev is None:
                prev = (n, o)
                continue
            pn, po = prev
            if po["status"] == "not_observed_in_scope":
                out.append({"step": step, "object_label": o["object_label"], "kind": "first_observed", "run": n,
                            "text": "First observed in run %d; run %d's bounded query did not return it. Before "
                                    "state unavailable." % (n, pn)})
            elif len(po.get("values") or []) == 1 and len(o.get("values") or []) == 1:
                a, b = po["values"][0], o["values"][0]
                for col in sorted(set(a) & set(b)):
                    if a[col] != b[col]:
                        out.append({"step": step, "object_label": o["object_label"], "kind": "changed", "column": col,
                                    "before": {"value": a[col], "observed_at": po["observed_at"], "run": pn},
                                    "after": {"value": b[col], "observed_at": o["observed_at"], "run": n}})
            prev = (n, o)
    return out


def latest(inv):
    runs = inv.get("runs") or []
    return runs[-1] if runs else None


def last_evidenced(run):
    """The last observed step of the record in plan order (a lookup only when nothing else was observed)."""
    best = aux = None
    for o in (run or {}).get("observations") or []:
        if o["status"] in ("observed", "truncated"):
            if o.get("role") == "lookup":
                aux = o
            else:
                best = o
    return best or aux


def largest_gap(run, flow_gaps=()):
    for o in (run or {}).get("observations") or []:
        if o["status"] == "not_observed_in_scope":
            return "%s: not observed within this query scope" % o["object_label"]
    for o in (run or {}).get("observations") or []:
        if o["status"] in ("mapping_unresolved", "permission_denied", "failed", "not_queried"):
            return "%s: %s" % (o["object_label"], dbmodel.STEP_LABEL.get(o["status"], o["status"]).lower())
    for g in flow_gaps or []:
        if g.get("kind") in ("runtime_events_unavailable", "unknown_boundary", "mapping_unresolved"):
            return g.get("text")
    return ""


# --------------------------------------------------------------------------- handoff drafts

def _handoff_source(inv):
    run = latest(inv)
    return dbmodel.fingerprint({"flow": (inv.get("flow") or {}).get("fingerprint"), "runs": len(inv.get("runs") or []),
                                "last": [(o["step"], o["status"]) for o in (run or {}).get("observations") or []]})


def handoff_text(inv, flow=None):
    """The generated draft (module doc): no key, row value or SQL."""
    run = latest(inv)
    f = inv.get("flow") or {}
    L = ["Handoff: %s" % inv.get("objective"), "",
         "Environment: %s (%s)" % (str((inv.get("scope") or {}).get("environment") or "?").upper(),
                                   ", ".join((inv.get("scope") or {}).get("connections") or [])),
         "Flow: %s, version %s (%s)" % (f.get("title") or "?", f.get("version") or "?", f.get("label") or
                                        dbflows.FLOW_LABEL)]
    if run:
        w = run.get("window") or {}
        L.append("Observation window: %s to %s (this machine's clock, UTC); run %d of %d, %s" % (
            w.get("from") or "?", w.get("to") or "?", run["n"], len(inv.get("runs") or []), run["state_label"]))
        le = last_evidenced(run)
        L.append("Last evidenced step: %s" % ("%s, observed %s" % (le["object_label"], le["observed_at"]) if le else
                                              "none observed yet"))
    else:
        L.append("No run yet: nothing has been observed.")
    if flow:
        code = [s for s in flow.get("steps") or [] if s.get("basis") == "code_supported"][:8]
        if code:
            L += ["", "Code-supported behaviour (from the flow, not from this record):"]
            L += ["- %s" % s["label"] for s in code]
    if run:
        L += ["", "Observed record behaviour (this record, bounded reads):"]
        L += ["- %s: %s%s" % (o["object_label"], o["status_label"], " (%s)" % o["observed_at"] if o.get("observed_at")
                                                                    else "") for o in run.get("observations") or []]
        gaps = [u for u in run.get("unknowns") or []]
        if gaps:
            L += ["", "Unknown boundaries and missing checks:"] + ["- %s" % u for u in gaps]
    nxt = (run or {}).get("next_checks") or []
    L += ["", "Next action: %s" % (nxt[0] if nxt else inv.get("next_action") or "Review a trace plan for this record.")]
    if len(nxt) > 1:
        L += ["- also: %s" % x for x in nxt[1:4]]
    L += ["", "Not observed means the bounded query found no matching row: not a failure, deletion or data loss."]
    return "\n".join(L) + "\n"


def handoff_save(ctx, inv_id, text=None, revision=None, regenerate=False, flow=None):
    """Regenerate the draft (a new generated version only when its sources changed; your edit is kept and flagged
    "Source evidence changed") or save your edited text against its revision (stale: dbstore.Conflict)."""
    inv = load_visible(ctx, "investigations", inv_id)
    if regenerate:
        fp = _handoff_source(inv)
        txt = handoff_text(inv, flow)

        def fn(rec):
            h = dict(rec.get("handoff") or {"generated": [], "edited": None, "source_changed": False})
            gen = list(h.get("generated") or [])
            if gen and gen[-1].get("source") == fp:
                return None
            gen.append({"version": len(gen) + 1, "text": txt, "source": fp, "at": common.now_iso()})
            h.update(generated=gen[-10:], source_changed=bool(h.get("edited")) and len(gen) > 1)
            return dict(rec, handoff=h)
        return _update("investigations", inv_id, fn)
    h = inv.get("handoff") or {}
    ed = h.get("edited") or {"revision": 0}
    if int(revision if revision is not None else -1) != int(ed.get("revision") or 0):
        raise dbstore.Conflict(ed, {"text": text, "revision": revision})

    def fn2(rec):
        hh = dict(rec.get("handoff") or {"generated": [], "edited": None, "source_changed": False})
        cur = hh.get("edited") or {"revision": 0}
        if int(cur.get("revision") or 0) != int(ed.get("revision") or 0):
            raise dbstore.Conflict(cur, {"text": text, "revision": revision})
        gen = hh.get("generated") or []
        hh.update(edited={"text": str(text or "")[:NOTE_MAX], "revision": int(cur.get("revision") or 0) + 1,
                          "based_on": gen[-1]["version"] if gen else None, "at": common.now_iso()},
                  source_changed=False)
        return dict(rec, handoff=hh)
    return _update("investigations", inv_id, fn2)


def handoff_copy(ctx, inv_id):
    """The draft's copy text: access checked now, then the team redaction (secrets, personal data, paths). Nothing is
    sent anywhere."""
    from . import export
    from .redact import redact
    inv = load_visible(ctx, "investigations", inv_id)
    h = inv.get("handoff") or {}
    txt = (h.get("edited") or {}).get("text") or ((h.get("generated") or [{}])[-1]).get("text") or \
        handoff_text(inv)
    return export.scrub(redact(txt, mode="team"), hosts=True)


# --------------------------------------------------------------------------- the companion's database_context

def database_context(ctx, flow_id=None, investigation_id=None, cfg=None):
    """{schema_version, database_context: {...} | None, reason}: the bounded summary the native companion shows
    (DB-19 / DB-22). Absent or not-yours selections give None with a reason, never an error."""
    from . import dbviews
    out = {"schema_version": SCHEMA_VERSION, "database_context": None, "reason": ""}
    if not flow_id and not investigation_id:
        out["reason"] = "no flow or investigation selected"
        return out
    inv, flow = None, None
    if investigation_id:
        try:
            inv = load_visible(ctx, "investigations", investigation_id)
        except NotFound:
            out["reason"] = "no such investigation"
            return out
        flow_id = flow_id or (inv.get("flow") or {}).get("id")
    if flow_id:
        vis = dbflows.Visible(ctx, cfg=cfg)
        rec = dbflows.saved_flow(flow_id) if dbmodel.valid_id(flow_id, "f") else None
        if not rec or not rec.get("current") or not dbflows._flow_visible(vis, rec):
            if inv is None:
                out["reason"] = "no such flow"
                return out
        else:
            flow, _ = dbviews._flow_visible_filter(vis, rec["current"])
    run = latest(inv) if inv else None
    le = last_evidenced(run)
    if le is None and flow:
        steps = [s for s in flow.get("steps") or [] if s.get("basis") != "inferred" and s.get("stage") is not None]
        le = {"object_label": steps[-1]["label"], "status_label": steps[-1]["basis_label"]} if steps else None
    gap = largest_gap(run, (flow or {}).get("gaps") or [])
    nxt = (run or {}).get("next_checks") or []
    nxt = nxt[0] if nxt else (next((g.get("next") for g in (flow or {}).get("gaps") or []
                                    if g.get("kind") in ("runtime_events_unavailable", "unknown_boundary")), "")
                              if flow else "") or (inv or {}).get("next_action") or ""
    obs = [{"step": o["object_label"], "status": o["status"], "status_label": o["status_label"],
            "observed_at": o.get("observed_at") or ""} for o in (run or {}).get("observations") or []][:3]
    ctxd = {"schema_version": SCHEMA_VERSION, "flow_id": flow_id or None,
            "investigation_id": inv["id"] if inv else None,
            "operation_label": _clean_text((flow or {}).get("title") or (inv or {}).get("flow", {}).get("title") or
                                           "", 120),
            "environment": str((inv or {}).get("scope", {}).get("environment") or (flow or {}).get("environment") or
                               "").upper(),
            "last_evidenced_step": _clean_text("%s: %s" % (le["object_label"], le.get("status_label") or "observed")
                                               if le else "", 200),
            "largest_gap": _clean_text(gap, 240), "next_action": _clean_text(nxt, 300),
            "coverage": ((flow or {}).get("scope_line") or "") if not run else "%d of %d planned step(s) answered" % (
                sum(1 for o in run["observations"] if o["status"] in ANSWERED), len(run["observations"])),
            "freshness": (flow or {}).get("freshness") or "unknown",
            "updated_at": (run or {}).get("ended_at") or (inv or {}).get("updated_at") or (flow or {}).get("built_at")
            or "", "observations": obs}
    while len(json.dumps(ctxd, ensure_ascii=False).encode("utf-8")) > CONTEXT_MAX and ctxd["observations"]:
        ctxd["observations"].pop()
    out["database_context"] = ctxd
    return out
