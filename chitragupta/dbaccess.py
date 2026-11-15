"""Database access: the approved scope, who is asking, the generations that invalidate cached views, and the one access
decision every reader applies (database flow D0, plan §2.3; DB-02, DB-03).

Approved scope
  mcp.database.scope (explicit, validated by scope_errors; set through /cg:setup or the config, not the dashboard):
      {"connections": {"qa-main": {"env": "qa", "engine": "postgres", "databases": ["shop"], "schemas": ["shop"],
                                   "case_sensitive": false, "aliases": []}}, "repos": ["<watched repo path>"]}
  Without it, a legacy scope is derived from mcp.database.connections / allowed (the env is the key there), types and
  schema_map, with provenance "derived from mcp.database.connections"; its schema list is unknown unless schema_map
  names the connection, and then no statement naming a schema is built for it.
  A connection is approved only when its entry is valid, it has an explicit environment, that environment is in
  qa_data.allowed_envs, no "prod" is in its name or environment, the guard allows it (mcpjobs.db_guard_connections, the
  same derivation as allow.json), you haven't revoked it, and the connector's own inventory doesn't report another
  environment or engine for it. Unknown environment -> blocked. The inventory (list_connections) is recorded apart and
  never adds a connection; an alias that resolves outside the approved scope is blocked. Production is never approved
  and qa_data.allowed_envs is never widened here.

Identity and generations
  principal_id(cfg)        "u_" + sha256(confirmed commit emails + confirmed account keys)[:16]; None without a
                           confirmed identity (personal investigations are then unavailable: missing_identity)
  profile_generation(cfg)  profile.signature(cfg)
  policy_generation(cfg)   sha256 of the approved scope, allowed environments, the guard's connection list and scopes
                           in allow.json, the tool and argument mapping, the row cap and the revocations. Any change
                           invalidates cached projections, and a pinned job sees it before its next call.

decide(ctx, rec) -> {allowed, category: technical | personal | shared | outside, reason[, inferred_only]}
  technical  objects, edges, evidence, claims, passports, flows: every connection the record names is approved now,
             nothing it names is quarantined, and code evidence comes from a selected repo (a repo outside the
             selection is shown only as inferred context, and only next to an approved connection)
  personal   investigations, plans, jobs: owner == principal_id and every connection still approved
  shared     an explicitly shared, redacted artifact whose connections are approved
  A denied record yields nothing: callers drop it without a title, count or snippet.
Quarantine: a permission-denied answer for (connection, object) hides it from normal views ("Access denied at last
check") until a successful read reconciles it. Authorization freshness: the last successful observation per
connection, labelled "access last confirmed <date>" (stale after mcp.database.authorization_max_age_days, default 7),
never "verified access". Database records never feed progress or activity metrics.
"""
import hashlib
import json
import os
import re

from . import common
from . import dbstore

ENGINES = ("postgres", "mssql", "mongodb", "other")
SCOPE_KEYS = {"connections", "repos"}
CONN_KEYS = {"env", "engine", "databases", "schemas", "case_sensitive", "aliases"}
CONN_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
ENV_RX = re.compile(r"^[a-z][a-z0-9_-]{0,19}$")
NAME_RX = re.compile(r"^[^\x00-\x1f\"'`\[\];]{1,128}$")
MAX_AGE_DEFAULT = 7
LEGACY = "derived from mcp.database.connections"
EXPLICIT = "mcp.database.scope"
DENIED = "Access denied at last check"


def _db(cfg):
    return ((cfg or {}).get("mcp") or {}).get("database") or {}


# --------------------------------------------------------------------------- validation

def _names(v, field, where):
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > 50:
        return ["%s: %s is a list of at most 50 names" % (where, field)]
    return ["%s: %s holds %r, not a catalog name" % (where, field, x) for x in v
            if not isinstance(x, str) or not NAME_RX.match(x) or x != x.strip()]


def connection_errors(cid, e):
    """Problems of one mcp.database.scope connection entry (an empty list: valid)."""
    where = "connection %s" % cid
    if not isinstance(cid, str) or not CONN_RX.match(cid):
        return ["%r is not a connection ID (letters, digits, . _ : -)" % cid]
    if not isinstance(e, dict):
        return ["%s: give env and engine" % where]
    out = ["%s: unknown field %s" % (where, k) for k in sorted(set(e) - CONN_KEYS)]
    env = e.get("env")
    if not isinstance(env, str) or not ENV_RX.match(env):
        out.append("%s: env is an explicit environment name such as qa" % where)
    if "engine" in e and e["engine"] not in ENGINES:
        out.append("%s: engine is one of %s" % (where, ", ".join(ENGINES)))
    out += _names(e.get("databases"), "databases", where) + _names(e.get("schemas"), "schemas", where)
    if "case_sensitive" in e and not isinstance(e["case_sensitive"], bool):
        out.append("%s: case_sensitive is true or false" % where)
    al = e.get("aliases")
    if al is not None and (not isinstance(al, list) or any(not isinstance(a, str) or not CONN_RX.match(a) for a in al)):
        out.append("%s: aliases is a list of connection names" % where)
    return out


def scope_errors(value):
    """Problems of an mcp.database.scope value (an empty list: valid). Production can't be approved here."""
    if not isinstance(value, dict):
        return ["mcp.database.scope is an object with connections and repos"]
    out = ["unknown field %s" % k for k in sorted(set(value) - SCOPE_KEYS)]
    conns = value.get("connections")
    if not isinstance(conns, dict) or not conns:
        out.append("connections names at least one connection")
        conns = {}
    for cid, e in conns.items():
        out += connection_errors(cid, e)
        if isinstance(cid, str) and "prod" in cid.lower() or isinstance(e, dict) and "prod" in str(e.get("env")).lower():
            out.append("connection %s: production is never in scope" % cid)
    repos = value.get("repos")
    if repos is not None and (not isinstance(repos, list) or any(not isinstance(r, str) or not r.strip() for r in repos)):
        out.append("repos is a list of watched repository folders")
    return out


def max_age_errors(value):
    if value is None:
        return []
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 90:
        return ["mcp.database.authorization_max_age_days is a whole number from 1 to 90"]
    return []


def max_age_days(cfg):
    v = _db(cfg).get("authorization_max_age_days")
    return v if not max_age_errors(v) and v is not None else MAX_AGE_DEFAULT


# --------------------------------------------------------------------------- scope

def _entries(cfg):
    """(source, provenance, {conn: entry}, errors) as configured, before any approval decision."""
    db = _db(cfg)
    sc = db.get("scope")
    if sc is not None:
        errs = scope_errors(sc)
        conns = sc.get("connections") if isinstance(sc, dict) and isinstance(sc.get("connections"), dict) else {}
        out = {}
        for cid, e in conns.items():
            if not isinstance(cid, str):
                continue
            e = e if isinstance(e, dict) else {}
            out[cid] = {"env": e.get("env") if isinstance(e.get("env"), str) else "",
                        "engine": e.get("engine") if e.get("engine") in ENGINES else None,
                        "databases": [x for x in e.get("databases") or [] if isinstance(x, str)],
                        "schemas": [x for x in e.get("schemas") or [] if isinstance(x, str)],
                        "case_sensitive": e.get("case_sensitive") is True,
                        "aliases": [a for a in e.get("aliases") or [] if isinstance(a, str)],
                        "errors": connection_errors(cid, conns[cid]), "provenance": EXPLICIT}
        return "scope", EXPLICIT, out, errs
    out = {}
    types = db.get("types") if isinstance(db.get("types"), dict) else {}
    smap = db.get("schema_map") if isinstance(db.get("schema_map"), dict) else {}
    pairs = [(env, c) for env, c in (db.get("connections") or {}).items() if isinstance(c, str) and c]
    for env, lst in (db.get("allowed") or {}).items():
        pairs += [(env, c) for c in lst or [] if isinstance(c, str) and c]
    for env, c in pairs:
        if c in out and out[c]["env"] != str(env):
            out[c]["errors"].append("connection %s is listed under two environments" % c)
            continue
        schemas = sorted(s for s, conn in smap.items() if conn == c)
        eng = types.get(c)
        out.setdefault(c, {"env": str(env), "engine": eng if eng in ENGINES else None, "databases": [],
                           "schemas": schemas or None, "case_sensitive": False, "aliases": [], "errors": [],
                           "provenance": LEGACY})
    return "legacy", LEGACY, out, []


def revoked():
    """{connection: {at, reason}} you revoked."""
    r = dbstore.read_file("revocations").get("connections")
    return r if isinstance(r, dict) else {}


def inventory():
    r = dbstore.read_file("inventory").get("connections")
    return r if isinstance(r, dict) else {}


_ENGINE_ALIASES = {"postgresql": "postgres", "pg": "postgres", "postgres": "postgres", "mssql": "mssql",
                   "sqlserver": "mssql", "sql server": "mssql", "mongo": "mongodb", "mongodb": "mongodb"}


def _engine_word(x):
    return _ENGINE_ALIASES.get(str(x or "").strip().lower(), "")


def _repo_id(path):
    from . import activity
    return activity.canonical_id(None, path)


def approved_scope(cfg):
    """{source, provenance, connections: {approved conn: entry}, blocked: {conn: reason}, aliases: {alias: conn},
    aliases_blocked: {alias: reason}, repos: [canonical ids], repo_roots: {id: folder}, repos_blocked, errors}."""
    from . import mcpjobs
    source, prov, entries, errors = _entries(cfg)
    envs = {str(e).lower() for e in ((cfg or {}).get("qa_data") or {}).get("allowed_envs") or ["qa"]}
    guard = set(mcpjobs.db_guard_connections(cfg or {}))
    rev, inv = revoked(), inventory()
    ok, blocked = {}, {}
    for c, e in sorted(entries.items()):
        env = (e["env"] or "").lower()
        i = inv.get(c) if isinstance(inv.get(c), dict) else {}
        if e["errors"]:
            why = "invalid scope entry: " + e["errors"][0]
        elif not env:
            why = "environment unknown"
        elif "prod" in c.lower() or "prod" in env:
            why = "production is never approved"
        elif env not in envs:
            why = "environment %s is not in qa_data.allowed_envs" % env
        elif c in rev:
            why = "revoked"
        elif c not in guard:
            why = "not on the guard's connection list"
        elif i.get("env") and str(i["env"]).strip().lower() != env:
            why = "the connector reports another environment for it"
        elif i.get("engine") and e["engine"] and _engine_word(i["engine"]) and _engine_word(i["engine"]) != e["engine"]:
            why = "the connector reports another engine for it"
        else:
            why = ""
        if why:
            blocked[c] = why
        else:
            ok[c] = {k: e[k] for k in ("env", "engine", "databases", "schemas", "case_sensitive", "aliases",
                                         "provenance")}
            ok[c]["env"] = env
    aliases, al_blocked = {}, {}
    for c, e in ok.items():
        for a in e["aliases"]:
            ai = inv.get(a) if isinstance(inv.get(a), dict) else {}
            if a in entries or a in aliases:
                al_blocked[a] = "names another connection"
            elif "prod" in a.lower():
                al_blocked[a] = "production is never approved"
            elif ai.get("env") and str(ai["env"]).strip().lower() != e["env"]:
                al_blocked[a] = "resolves outside the approved scope"
            else:
                aliases[a] = c
    watched = {common.norm_path(r): r for r in (cfg or {}).get("watched_repos") or [] if isinstance(r, str) and r}
    sel = ((_db(cfg).get("scope") or {}).get("repos") if source == "scope" else None)
    repos, roots, rblocked = [], {}, []
    for r in (sel if isinstance(sel, list) else list(watched.values())):
        if not isinstance(r, str) or common.norm_path(r) not in watched:
            rblocked.append(r)
            continue
        rid = _repo_id(watched[common.norm_path(r)])
        if rid not in roots:
            repos.append(rid)
            roots[rid] = watched[common.norm_path(r)]
    return {"source": source, "provenance": prov, "connections": ok, "blocked": blocked, "aliases": aliases,
            "aliases_blocked": al_blocked, "repos": repos, "repo_roots": roots, "repos_blocked": rblocked,
            "errors": errors}


def guard_scopes(cfg):
    """(scopes, scope_args) for allow.json's database entry: {conn: {engine, databases, schemas, case_sensitive}} of
    the approved connections when mcp.database.scope is set, else (None, None) (the guard then checks no statement
    scope, as before)."""
    if _db(cfg).get("scope") is None:
        return None, None
    sc = approved_scope(cfg)
    if sc["source"] != "scope":
        return None, None
    scopes = {c: {"engine": e["engine"] or "", "databases": list(e["databases"]), "schemas": list(e["schemas"] or []),
                  "case_sensitive": bool(e["case_sensitive"])} for c, e in sc["connections"].items()}
    args = (_db(cfg).get("arguments") or {}) if isinstance(_db(cfg).get("arguments"), dict) else {}
    scope_args = {"schema": [args["schema"]] if isinstance(args.get("schema"), str) and args["schema"] else [],
                  "table": [args["table"]] if isinstance(args.get("table"), str) and args["table"] else []}
    return scopes, scope_args


# --------------------------------------------------------------------------- identity and generations

def principal_id(cfg):
    from . import profile
    p = profile.load(cfg or {})
    keys = sorted(p["aliases"]) + sorted("|".join(profile.account_key(a)) for a in p["accounts"])
    if not keys:
        return None
    return "u_" + hashlib.sha256(json.dumps(keys).encode("utf-8")).hexdigest()[:16]


def profile_generation(cfg):
    from . import profile
    return profile.signature(cfg or {})


def policy_generation(cfg, scope=None):
    """Changes whenever what may be read could change (see the module doc). Computed the same way by a job when it
    plans and by the guard before each pinned call."""
    from . import mcpjobs
    from .safety import mcp as guard
    cfg = cfg or {}
    sc = scope or approved_scope(cfg)
    srv = mcpjobs.server_name(cfg, "database")
    entry = ((guard.load_allow().get("servers") or {}).get(srv) or {}) if srv else {}
    basis = {
        "server": srv, "connections": sc["connections"], "aliases": sc["aliases"], "repos": sc["repos"],
        "envs": sorted({str(e).lower() for e in (cfg.get("qa_data") or {}).get("allowed_envs") or ["qa"]}),
        "guard": {"connections": sorted(str(c) for c in (entry.get("connections") or [])
                                        if isinstance(entry.get("connections"), list)),
                  "scopes": entry.get("scopes"), "max_rows": guard.load_allow().get("max_rows")},
        "tools": mcpjobs.db_tools(cfg), "arguments": _db(cfg).get("arguments"),
        "max_rows": (cfg.get("qa_data") or {}).get("max_rows_per_query"), "revoked": sorted(revoked()),
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _authorized_at(conns):
    caps = dbstore.read_file("capabilities").get("connections")
    caps = caps if isinstance(caps, dict) else {}
    return {c: str((caps.get(c) or {}).get("last_authorized") or "") for c in conns}


class AccessContext:
    """Everything decide() needs, computed once per request from the config and data/db (no query, no process)."""

    def __init__(self, cfg=None):
        cfg = common.load_config() if cfg is None else cfg
        sc = approved_scope(cfg)
        self.scope = sc
        self.principal_id = principal_id(cfg)
        self.profile_generation = profile_generation(cfg)
        self.policy_generation = policy_generation(cfg, sc)
        self.connections = {c: {k: e[k] for k in ("env", "engine", "databases", "schemas", "case_sensitive")}
                            for c, e in sc["connections"].items()}
        self.aliases = dict(sc["aliases"])
        self.repos = list(sc["repos"])
        self.repo_roots = dict(sc["repo_roots"])
        self.authorized_at = _authorized_at(self.connections)
        self.quarantine = quarantine_entries()
        self.max_age_days = max_age_days(cfg)

    def as_dict(self):
        return {"principal_id": self.principal_id, "profile_generation": self.profile_generation,
                "policy_generation": self.policy_generation, "connections": self.connections, "repos": self.repos,
                "authorized_at": self.authorized_at}


# --------------------------------------------------------------------------- routing

def resolve_connection(ctx, name, env=None):
    """(connection, None) for an explicitly named approved connection (or an approved alias of one), else (None,
    reason). No name -> refused (never the server's default connection); an env that disagrees -> refused."""
    if not name:
        return None, "name a connection (the server's default connection is never used)"
    c = name if name in ctx.connections else ctx.aliases.get(name)
    if not c:
        return None, "connection not approved"
    if env is not None and str(env).lower() != ctx.connections[c]["env"]:
        return None, "conflicting routing: that connection is not in environment %s" % env
    return c, None


# --------------------------------------------------------------------------- the decision

PERSONAL_PREFIXES = ("i", "p", "j")
TECHNICAL_PREFIXES = ("o", "c", "n", "e", "v", "k", "f")


def _category(rec):
    if rec.get("shared") is True:
        return "shared"
    rid = str(rec.get("id") or "")
    if rec.get("owner") is not None or rid[:1] in PERSONAL_PREFIXES and rid[1:2] == "_":
        return "personal"
    if rid[:1] in TECHNICAL_PREFIXES and rid[1:2] == "_":
        return "technical"
    return "outside"


def _connections_of(rec, graph=None):
    out = set()

    def add(v):
        if isinstance(v, str) and v:
            out.add(v)
        elif isinstance(v, (list, tuple)):
            for x in v:
                add(x)
    add(rec.get("connection"))
    add(rec.get("connections"))
    for k in ("scope", "identity", "applicability"):
        d = rec.get(k)
        if isinstance(d, dict):
            add(d.get("connection"))
            add(d.get("connections"))
    for k in ("source", "target", "parent", "subject"):  # an edge / member / claim is only as visible as its ends
        n = ((graph or {}).get("nodes") or {}).get(rec.get(k)) if graph else None
        if isinstance(n, dict):
            out |= _connections_of(n)
    return out


def _repos_of(rec, ctx=None):
    """The repos a record's code context comes from. A `repos` list (a node cited from several repos) counts by its
    selected members: one selected repo is enough for the node itself (each evidence record is checked on its own);
    a list with no selected repo counts as outside."""
    out = set()
    for d in (rec.get("provenance"), rec.get("scope"), rec.get("identity"), rec.get("applicability")):
        if isinstance(d, dict) and isinstance(d.get("repo"), str) and d.get("repo"):
            out.add(d["repo"])
        many = d.get("repos") if isinstance(d, dict) else None
        if isinstance(many, list) and many:
            many = [r for r in many if isinstance(r, str) and r]
            chosen = [r for r in many if ctx is not None and r in ctx.repos]
            out |= set(chosen or many)
    return out


def _ids_of(rec):
    ids = {rec.get("id"), rec.get("source"), rec.get("target"), rec.get("parent"), rec.get("subject")}
    ids |= set(rec.get("subjects") or [])
    return {i for i in ids if isinstance(i, str) and i}


def _deny(category, reason):
    return {"allowed": False, "category": category, "reason": reason}


def decide(ctx, rec, graph=None):
    """The one access decision (module doc). `graph` ({nodes: …}) lets an edge, member or claim inherit its ends'
    connections."""
    if not isinstance(rec, dict):
        return _deny("outside", "not a record")
    cat = _category(rec)
    conns = _connections_of(rec, graph)
    if cat == "outside":
        return _deny("outside", "not a database-flow record")
    if any(c not in ctx.connections for c in conns):
        return _deny("outside", "a connection it names is not approved now")
    if is_quarantined(ctx.quarantine, conns, _ids_of(rec)):
        return _deny("outside", DENIED)
    if cat == "personal":
        if not ctx.principal_id:
            return _deny("personal", "missing_identity")
        if rec.get("owner") != ctx.principal_id:
            return _deny("outside", "not yours")
        return {"allowed": True, "category": "personal", "reason": ""}
    if cat == "shared":
        if not rec.get("redacted"):
            return _deny("outside", "a shared artifact must be redacted")
        return {"allowed": True, "category": "shared", "reason": ""}
    repos = _repos_of(rec, ctx)
    outside = [r for r in repos if r not in ctx.repos]
    if outside:
        if conns:  # code from an unselected repo, next to an approved connection: inferred context only
            return {"allowed": True, "category": "technical", "reason": "repository not selected",
                    "inferred_only": True}
        return _deny("outside", "repository not selected")
    if not conns and not repos:
        return _deny("outside", "names no approved connection or selected repository")
    return {"allowed": True, "category": "technical", "reason": ""}


def visible(ctx, records, graph=None):
    """The records decide() allows, in order. Denied ones are dropped without a trace (no count, no title)."""
    return [r for r in records if decide(ctx, r, graph)["allowed"]]


# --------------------------------------------------------------------------- quarantine and revocation

def _qkey(connection, object_id):
    return "%s|%s" % (connection, object_id or "*")


def quarantine_entries():
    q = dbstore.read_file("quarantine").get("entries")
    return q if isinstance(q, dict) else {}


def is_quarantined(entries, conns, ids):
    for c in conns:
        if _qkey(c, "*") in entries:
            return True
        for i in ids:
            if _qkey(c, i) in entries:
                return True
    return any(e.get("object") in ids for e in entries.values() if isinstance(e, dict) and not conns)


def quarantine(connection, object_id=None, reason="permission denied", at=None):
    """A permission-denied answer on (connection, object) — object None: the whole connection."""
    with dbstore.lock():
        q = dbstore.read_file("quarantine")
        ents = q.get("entries") if isinstance(q.get("entries"), dict) else {}
        ents[_qkey(connection, object_id)] = {"connection": connection, "object": object_id or "*",
                                              "reason": str(reason)[:200], "at": at or common.now_iso()}
        dbstore.write_file("quarantine", {"schema_version": dbstore.SCHEMA_VERSION, "entries": ents})


def reconcile(connection, object_id=None):
    """A successful read of (connection, object): its quarantine entry (and a connection-wide one when the read was
    of the connection itself) is lifted. Returns True when something was lifted."""
    with dbstore.lock():
        q = dbstore.read_file("quarantine")
        ents = q.get("entries") if isinstance(q.get("entries"), dict) else {}
        hit = [k for k in {_qkey(connection, object_id)} if k in ents]
        for k in hit:
            del ents[k]
        if hit:
            dbstore.write_file("quarantine", {"schema_version": dbstore.SCHEMA_VERSION, "entries": ents})
        return bool(hit)


def revoke(connection, reason="", cfg=None):
    """Stop using a connection now: the policy generation changes (views recompute, pinned jobs stop) and the guard
    file is regenerated without its scope. Only an explicit restore() brings it back."""
    with dbstore.lock():
        r = dbstore.read_file("revocations")
        cs = r.get("connections") if isinstance(r.get("connections"), dict) else {}
        cs[connection] = {"at": common.now_iso(), "reason": str(reason)[:200]}
        dbstore.write_file("revocations", {"schema_version": dbstore.SCHEMA_VERSION, "connections": cs})
    _refresh_guard(cfg)


def restore(connection, cfg=None):
    with dbstore.lock():
        r = dbstore.read_file("revocations")
        cs = r.get("connections") if isinstance(r.get("connections"), dict) else {}
        cs.pop(connection, None)
        dbstore.write_file("revocations", {"schema_version": dbstore.SCHEMA_VERSION, "connections": cs})
    _refresh_guard(cfg)


def _refresh_guard(cfg):
    from . import mcpjobs
    cfg = common.load_config() if cfg is None else cfg
    if os.path.isfile(os.path.join(mcpjobs.mcp_dir(), "allow.json")) or mcpjobs.configured(cfg):
        mcpjobs.ensure_files(cfg)


# --------------------------------------------------------------------------- inventory and authorization

def record_inventory(rows, at=None):
    """The connector's connection listing (list_connections), sanitized to id / env / engine. Recorded for display
    and conflict checks only: it never adds a connection to the scope."""
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        cid = r.get("id") or r.get("connectionId") or r.get("name")
        if not isinstance(cid, str) or not CONN_RX.match(cid):
            continue
        out[cid] = {"env": str(r.get("env") or r.get("environment") or "")[:20],
                    "engine": str(r.get("type") or r.get("engine") or "")[:20]}
    dbstore.write_file("inventory", {"schema_version": dbstore.SCHEMA_VERSION, "at": at or common.now_iso(),
                                     "connections": out})
    return out


def authorization(ctx, connection, now=None):
    """{state: current | stale | unknown, at, label}: the last time a read on `connection` succeeded. Remote
    permission changes are only seen at the next read, so this is a last observation, never "verified access"."""
    at = ctx.authorized_at.get(connection) or ""
    ts = common.parse_ts(at)
    if not ts:
        return {"state": "unknown", "at": "", "label": "access not confirmed yet"}
    now = now or common.now()
    day = ts.date().isoformat()
    if (now - ts).total_seconds() > ctx.max_age_days * 86400:
        return {"state": "stale", "at": at, "label": "access last confirmed %s (older than %d days)" % (
            day, ctx.max_age_days)}
    return {"state": "current", "at": at, "label": "access last confirmed %s" % day}
