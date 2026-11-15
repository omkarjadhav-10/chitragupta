"""Canonical database-flow records: identities, enums with their labels, node / edge / evidence / claim records and the
checks that keep a claim from saying more than its evidence (database flow D0, plan §2.1-§2.2).

Identities (DB-04)
  object     (connector, connection, environment, database, schema, kind, name) -> "o_" + sha256[:24]
  member     the object's identity + (member_kind, member_name)               -> "c_" (columns, parameters)
  field      a non-DB node's ID + ("field", name)                             -> "c_" (an event's or API's fields)
  non-DB     (repo canonical id | "", kind, name)                             -> "n_" (service, api_operation, event,
                                                                                  job, code_symbol, report)
  edge "e_", evidence "v_", claim "k_", flow "f_", investigation "i_", plan "p_", job "j_", principal "u_".
  IDs are opaque and stable: never a path, never a display name (the original spelling is kept in `display`).
  norm_ident(engine, name, quoted): PostgreSQL folds an unquoted name to lower case and keeps a quoted one (a name read
  from the catalog is the stored name: pass quoted=True); SQL Server compares case-insensitively (collation
  "assumed_ci") unless the connection is case_sensitive; any other engine keeps the name exactly as written.
  A rename or alias links two IDs only through an explicit alias record; otherwise they stay two objects.

Evidence and claims (DB-05)
  basis, strongest first: declared_metadata > code_supported > user_confirmed > observed_sample > inferred, counted
  per what it can support: an observed sample supports only the sampled relationship, never a structural constraint
  (a foreign key, a cardinality, uniqueness, nullability, a type), and a user note stays self-reported.
  An evidence kind decides its basis: catalog -> declared_metadata, source -> code_supported, user_note ->
  user_confirmed, observation -> observed_sample, model_proposal -> inferred (any of them may be lowered to inferred,
  never relabelled). A model's `confidence` is kept for display and never used for a basis.
  validate_claim / validate_edge reject invented IDs, unrelated evidence (its subjects don't name the claim's subject),
  evidence from another scope (a claim wider than its evidence), code citations to a file that isn't in a selected
  repo or lines out of range, and a basis stronger than the validated evidence; a cited file whose content hash
  changed makes the claim needs_review. record_conflict keeps both claims and links them.
  Domain result, coverage and freshness are separate fields; nothing here says "verified".
"""
import hashlib
import json
import os
import re
import secrets

from . import common

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- enums and labels

DB_KINDS = ("table", "view", "procedure", "function", "trigger", "sequence", "collection")
NODE_KINDS = ("service", "api_operation", "event", "job", "code_symbol", "report")
MEMBER_KINDS = ("column", "parameter", "field")  # a field belongs to a non-DB node (an event's payload field)
KIND_LABEL = {"table": "Table", "view": "View", "procedure": "Procedure", "function": "Function", "trigger": "Trigger",
              "sequence": "Sequence", "collection": "Collection", "service": "Service", "api_operation": "API operation",
              "event": "Event", "job": "Job", "code_symbol": "Code symbol", "report": "Report", "column": "Column",
              "parameter": "Parameter", "field": "Field"}

EDGE_LAYERS = {"structure": ("foreign_key", "logical_association"),
               "flow": ("reads", "writes", "calls", "publishes", "consumes", "transforms", "copies", "triggers")}
EDGE_KINDS = tuple(k for ks in EDGE_LAYERS.values() for k in ks)
EDGE_LABEL = {"foreign_key": "Foreign key", "logical_association": "Logical association", "reads": "Reads",
              "writes": "Writes", "calls": "Calls", "publishes": "Publishes", "consumes": "Consumes",
              "transforms": "Transforms", "copies": "Copies", "triggers": "Triggers"}
TRANSPORTS = ("direct_sql", "event", "http", "job", "trigger", "unknown")
TRANSPORT_LABEL = {"direct_sql": "Direct SQL", "event": "Event", "http": "HTTP call", "job": "Job", "trigger": "Trigger",
                   "unknown": "Unknown transport"}
CARDINALITIES = ("one_to_one", "one_to_many", "many_to_one", "many_to_many")

BASIS = ("declared_metadata", "code_supported", "user_confirmed", "observed_sample", "inferred")  # strongest first
BASIS_RANK = {b: len(BASIS) - i for i, b in enumerate(BASIS)}
BASIS_LABEL = {"declared_metadata": "Declared in catalog", "code_supported": "Code-supported",
               "observed_sample": "Observed in a sample", "user_confirmed": "You confirmed (self-reported)",
               "inferred": "Inferred"}
EVIDENCE_KINDS = ("catalog", "source", "observation", "user_note", "model_proposal")
KIND_BASIS = {"catalog": "declared_metadata", "source": "code_supported", "observation": "observed_sample",
              "user_note": "user_confirmed", "model_proposal": "inferred"}
COVERAGE = ("complete", "partial", "not_checked", "unavailable")
COVERAGE_LABEL = {"complete": "Complete for the declared bounded query", "partial": "Partial",
                  "not_checked": "Not checked", "unavailable": "Unavailable"}
FRESHNESS = ("current", "stale", "unknown")
FRESHNESS_LABEL = {"current": "Current", "stale": "Stale", "unknown": "Freshness unknown"}
LIFECYCLE = ("current", "historical", "superseded", "inaccessible", "needs_review")
LIFECYCLE_LABEL = {"current": "Current", "historical": "Historical", "superseded": "Superseded",
                   "inaccessible": "Inaccessible", "needs_review": "Needs review"}
VALIDATION = ("validated", "needs_review", "rejected", "unvalidated")
VALIDATION_LABEL = {"validated": "References checked", "needs_review": "Needs review", "rejected": "Rejected",
                    "unvalidated": "Not checked yet"}
STEP_STATUSES = ("observed", "not_observed_in_scope", "not_queried", "permission_denied", "failed", "truncated",
                 "mapping_unresolved")
STEP_LABEL = {"observed": "Observed", "not_observed_in_scope": "Not observed within this query scope",
              "not_queried": "Not queried", "permission_denied": "Access denied", "failed": "Failed",
              "truncated": "Truncated at the row limit", "mapping_unresolved": "Mapping unresolved"}
JOB_STATES = ("queued", "running", "cancel_requested", "cancelled", "timed_out", "completed", "partial", "failed",
              "revoked")
JOB_LABEL = {"queued": "Queued", "running": "Running", "cancel_requested": "Cancel requested", "cancelled": "Cancelled",
             "timed_out": "Timed out", "completed": "All planned reads completed", "partial": "Partly completed",
             "failed": "Failed", "revoked": "Stopped: access changed"}
GAP_KINDS = ("deployed_revision_unknown", "dynamic_sql", "runtime_events_unavailable", "mapping_unresolved",
             "definition_unavailable", "permission_denied", "unknown_boundary")
GAP_LABEL = {"deployed_revision_unknown": "Code model; deployed revision unknown",
             "dynamic_sql": "Dynamic SQL: static analysis may miss queries",
             "runtime_events_unavailable": "Runtime events unavailable", "mapping_unresolved": "Mapping unresolved",
             "definition_unavailable": "Definition unavailable", "permission_denied": "Access denied",
             "unknown_boundary": "Unknown boundary"}
# Predicates (and edge kinds) that assert a structural constraint: an observed sample never supports them.
STRUCTURAL_PREDICATES = frozenset({"foreign_key", "cardinality", "unique", "primary_key", "nullable", "type", "default",
                                   "constraint"})

# --------------------------------------------------------------------------- IDs

ID_PREFIXES = {"o": "object", "c": "member", "n": "node", "e": "edge", "v": "evidence", "k": "claim", "f": "flow",
               "i": "investigation", "p": "plan", "j": "job", "u": "principal", "x": "other"}
ID_RX = re.compile(r"^[a-z]_[0-9a-f]{8,40}$")


def _h(parts, n=24):
    return hashlib.sha256(json.dumps(list(parts), sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()[:n]


def new_id(prefix, *parts):
    """`<prefix>_` + 24 hex: a hash of `parts` (stable), or random when no parts are given."""
    if prefix not in ID_PREFIXES:
        raise ValueError("unknown id prefix %r" % prefix)
    return "%s_%s" % (prefix, _h(parts) if parts else secrets.token_hex(12))


def valid_id(value, prefix=None):
    """True for an opaque record ID (optionally of one prefix). API and CLI arguments are checked with this."""
    return isinstance(value, str) and bool(ID_RX.match(value)) and (prefix is None or value[0] == prefix)


def norm_ident(engine, name, quoted=False, case_sensitive=None):
    """Comparable form of one identifier (see the module doc). Never strips or rewrites characters."""
    name = "" if name is None else str(name)
    eng = str(engine or "").lower()
    if eng == "postgres":
        return name if quoted else name.lower()
    if eng == "mssql":
        return name if case_sensitive else name.casefold()
    return name


def collation(engine, case_sensitive=None):
    """How identifiers of `engine` were compared, recorded next to an identity."""
    eng = str(engine or "").lower()
    if eng == "postgres":
        return "postgres_rules"
    if eng == "mssql":
        return "case_sensitive" if case_sensitive else "assumed_ci"
    return "as_written"


def object_identity(connector, connection, environment, database, schema, kind, name, engine="", quoted=True,
                    case_sensitive=None):
    """-> (normalised tuple, display dict). `quoted` True: names as stored in the catalog."""
    if kind not in DB_KINDS:
        raise ValueError("unknown object kind %r" % kind)
    n = lambda x: norm_ident(engine, x or "", quoted, case_sensitive)  # noqa: E731
    key = (str(connector or ""), str(connection or ""), str(environment or ""), n(database), n(schema), kind, n(name))
    display = {"connection": connection or "", "environment": environment or "", "database": database or "",
               "schema": schema or "", "name": name or "", "engine": str(engine or ""),
               "collation": collation(engine, case_sensitive)}
    return key, display


def object_id(connector, connection, environment, database, schema, kind, name, engine="", quoted=True,
              case_sensitive=None):
    return "o_" + _h(object_identity(connector, connection, environment, database, schema, kind, name, engine, quoted,
                                     case_sensitive)[0])


def column_id(obj_id, name, engine="", quoted=True, case_sensitive=None, member_kind="column"):
    """A column / parameter extends its object's identity (the object ID is the hash of that identity)."""
    if not valid_id(obj_id, "o"):
        raise ValueError("a member needs an object ID")
    if member_kind not in MEMBER_KINDS or member_kind == "field":
        raise ValueError("unknown member kind %r" % member_kind)
    return "c_" + _h((obj_id, member_kind, norm_ident(engine, name, quoted, case_sensitive)))


def field_id(node, name):
    """A field of a non-DB node (an event's payload field): its name is kept exactly as written."""
    if not valid_id(node, "n"):
        raise ValueError("a field needs a non-DB node ID")
    return "c_" + _h((node, "field", str(name or "")))


def node_id(repo, kind, name):
    """A non-DB node: (repo canonical id or "", kind, name); names are kept as written."""
    if kind not in NODE_KINDS:
        raise ValueError("unknown node kind %r" % kind)
    return "n_" + _h((str(repo or ""), kind, str(name or "")))


def edge_id(kind, source, target, column_mapping=None, condition=None):
    return "e_" + _h((kind, source, target, _canon(column_mapping or []), _canon(condition or [])))


def evidence_id(kind, provenance, scope=None):
    return "v_" + _h((kind, _canon(provenance or {}), _canon(scope or {})))


def claim_id(subject, predicate, value):
    """Same subject and predicate with another value -> another claim (conflicts keep both)."""
    return "k_" + _h((subject, predicate, _canon(value)))


def _canon(x):
    return json.loads(json.dumps(x, sort_keys=True, ensure_ascii=False, default=str))


def fingerprint(obj):
    """Stable fingerprint of a JSON-able value (sources, plans, scopes)."""
    return _h((_canon(obj),), 24)


# --------------------------------------------------------------------------- records

class RecordError(ValueError):
    """A record that can't be built (an unknown enum value, a missing field): nothing is stored."""


def _one_of(value, allowed, field):
    if value not in allowed:
        raise RecordError("%s must be one of %s (got %r)" % (field, ", ".join(allowed), value))
    return value


def _ids(xs, field, prefixes=None):
    if xs is None:
        return []
    if not isinstance(xs, (list, tuple)):
        raise RecordError("%s must be a list of IDs" % field)
    for x in xs:
        if not valid_id(x) or (prefixes and x[0] not in prefixes):
            raise RecordError("%s holds an invalid ID %r" % (field, x))
    return list(dict.fromkeys(xs))


def make_node(nid, kind, display, identity=None, parent=None, attrs=None, lifecycle="current"):
    """A node record. DB objects carry "o_" IDs, members "c_" (with `parent` = the object), others "n_"."""
    _one_of(kind, DB_KINDS + NODE_KINDS + MEMBER_KINDS, "kind")
    want = "o" if kind in DB_KINDS else "c" if kind in MEMBER_KINDS else "n"
    if not valid_id(nid, want):
        raise RecordError("a %s node needs a %s_ ID" % (kind, want))
    if kind in MEMBER_KINDS and not valid_id(parent, "n" if kind == "field" else "o"):
        raise RecordError("a %s needs its %s as parent" % (kind, "node" if kind == "field" else "object"))
    return {"id": nid, "schema_version": SCHEMA_VERSION, "kind": kind, "display": dict(display or {}),
            "identity": dict(identity or {}), "parent": parent, "attrs": dict(attrs or {}),
            "lifecycle": _one_of(lifecycle, LIFECYCLE, "lifecycle")}


def db_node(connector, connection, environment, database, schema, kind, name, engine="", quoted=True,
            case_sensitive=None, attrs=None):
    """A DB object node with its ID and display. The connector name is hashed into the ID only."""
    key, disp = object_identity(connector, connection, environment, database, schema, kind, name, engine, quoted,
                                case_sensitive)
    ident = {"connection": key[1], "environment": key[2], "database": key[3], "schema": key[4], "name": key[6]}
    return make_node("o_" + _h(key), kind, disp, identity=ident, attrs=attrs)


def member_node(obj, name, engine="", quoted=True, case_sensitive=None, member_kind="column", attrs=None):
    disp = dict((obj or {}).get("display") or {}, member=name, object_name=((obj or {}).get("display") or {}).get("name"))
    ident = dict((obj or {}).get("identity") or {}, member=norm_ident(engine, name, quoted, case_sensitive))
    return make_node(column_id(obj["id"], name, engine, quoted, case_sensitive, member_kind), member_kind, disp,
                     identity=ident, parent=obj["id"], attrs=attrs)


def field_node(node, name, attrs=None):
    """A field of a non-DB node (event / API operation): parent = that node."""
    disp = {"member": name, "object_name": ((node or {}).get("display") or {}).get("name"),
            "repo": ((node or {}).get("display") or {}).get("repo", "")}
    return make_node(field_id(node["id"], name), "field", disp, identity={"node": node["id"], "member": name},
                     parent=node["id"], attrs=attrs)


def code_node(repo, kind, name, attrs=None):
    return make_node(node_id(repo, kind, name), kind, {"name": name, "repo": repo or ""}, {"repo": repo or "",
                                                                                         "name": name}, attrs=attrs)


def _mapping(column_mapping):
    out = []
    for m in column_mapping or []:
        if not isinstance(m, dict):
            raise RecordError("column_mapping entries are objects")
        frm, to = m.get("from"), m.get("to")
        if not isinstance(frm, list) or not isinstance(to, list) or not frm or not to:
            raise RecordError("column_mapping from / to are non-empty lists (composite keys are lists, not strings)")
        out.append({"from": _ids(frm, "column_mapping.from", "c"), "to": _ids(to, "column_mapping.to", "c"),
                    "transform": str(m.get("transform") or ""), "transform_known": bool(m.get("transform_known"))})
    return out


def _condition(condition):
    if condition is None:
        return []
    if not isinstance(condition, list):
        raise RecordError("condition is a list of predicates (tenant predicates are list items, not a string)")
    return [dict(c) if isinstance(c, dict) else str(c) for c in condition]


def _applicability(a):
    a = dict(a or {})
    out = {k: a.get(k) or "" for k in ("env", "connection", "repo", "revision", "valid_from", "valid_to")}
    conns = a.get("connections")
    if conns is not None:
        if not isinstance(conns, list) or not all(isinstance(c, str) for c in conns):
            raise RecordError("applicability.connections is a list of connection IDs")
        out["connections"] = sorted(set(conns))
    return out


def make_edge(kind, source, target, column_mapping=None, condition=None, cardinality=None, transport="unknown",
              applicability=None, evidence=(), basis="inferred", lifecycle="current"):
    """An edge record (structure and flow layers stay apart: the layer follows the kind). A cardinality is kept only
    when declared (catalog, code or you): a sample can't establish one-to-one."""
    _one_of(kind, EDGE_KINDS, "kind")
    layer = next(lay for lay, ks in EDGE_LAYERS.items() if kind in ks)
    _one_of(transport, TRANSPORTS, "transport")
    _one_of(basis, BASIS, "basis")
    if cardinality is not None:
        _one_of(cardinality, CARDINALITIES, "cardinality")
        if basis in ("observed_sample", "inferred"):
            raise RecordError("a cardinality needs declared evidence, not a sample or an inference")
    for end, v in (("source", source), ("target", target)):
        if not valid_id(v) or v[0] not in "ocn":
            raise RecordError("edge %s must be a node ID" % end)
    mapping, cond = _mapping(column_mapping), _condition(condition)
    return {"id": edge_id(kind, source, target, mapping, cond), "schema_version": SCHEMA_VERSION, "layer": layer,
            "kind": kind, "source": source, "target": target, "column_mapping": mapping, "condition": cond,
            "cardinality": cardinality, "transport": transport, "applicability": _applicability(applicability),
            "evidence": _ids(list(evidence or []), "evidence", "v"), "basis": basis,
            "lifecycle": _one_of(lifecycle, LIFECYCLE, "lifecycle")}


CODE_FIELDS = ("repo", "revision", "content_hash", "path", "line_start", "line_end")


def _code_provenance(prov):
    """Code provenance: repo, revision (commit or "worktree"), content hash, repo-relative path and lines; never a
    line number alone."""
    missing = [k for k in CODE_FIELDS if prov.get(k) in (None, "")]
    if missing:
        raise RecordError("code provenance needs %s" % ", ".join(missing))
    for k in ("line_start", "line_end"):
        if isinstance(prov[k], bool) or not isinstance(prov[k], int) or prov[k] < 1:
            raise RecordError("%s is a line number from 1" % k)
    if prov["line_end"] < prov["line_start"]:
        raise RecordError("line_end is before line_start")
    if _rel_path_reason(prov["path"]):
        raise RecordError("path: %s" % _rel_path_reason(prov["path"]))
    if not re.match(r"^[0-9a-f]{64}$", str(prov["content_hash"])):
        raise RecordError("content_hash is the file's sha256")
    return {k: prov.get(k) for k in CODE_FIELDS + ("symbol",)}


def make_evidence(kind, provenance, scope=None, time=None, basis=None, quality=None, lifecycle="current", subjects=()):
    """An evidence record. Its kind decides its basis (KIND_BASIS); it may only be lowered to inferred, never raised
    or relabelled (a user note never becomes an observation)."""
    _one_of(kind, EVIDENCE_KINDS, "kind")
    cap = KIND_BASIS[kind]
    basis = basis or cap
    _one_of(basis, BASIS, "basis")
    if basis not in (cap, "inferred"):
        raise RecordError("%s evidence is %s (or inferred), not %s" % (kind, cap, basis))
    prov = dict(provenance or {})
    if kind == "source" and prov.get("type", "code") == "code":
        prov = dict(_code_provenance(prov), type="code")
    sc = {k: (scope or {}).get(k) or "" for k in ("env", "connection", "repo", "access_generation")}
    tm = {k: (time or {}).get(k) or "" for k in ("source_time", "collected_at", "valid_from", "valid_to")}
    tm["collected_at"] = tm["collected_at"] or common.now_iso()
    q = dict(quality or {})
    qual = {"coverage": _one_of(q.get("coverage", "not_checked"), COVERAGE, "coverage"),
            "freshness": _one_of(q.get("freshness", "unknown"), FRESHNESS, "freshness"), "as_of": q.get("as_of") or "",
            "validation": _one_of(q.get("validation", "unvalidated"), VALIDATION, "validation"),
            "conflicts": _ids(q.get("conflicts"), "conflicts"), "unsupported": [str(x) for x in q.get("unsupported") or []]}
    return {"id": evidence_id(kind, prov, sc), "schema_version": SCHEMA_VERSION, "kind": kind, "provenance": prov,
            "scope": sc, "time": tm, "basis": basis, "quality": qual,
            "lifecycle": _one_of(lifecycle, LIFECYCLE, "lifecycle"), "subjects": _ids(list(subjects or []), "subjects")}


def make_claim(subject, predicate, value, evidence=(), basis="inferred", scope=None, time=None, lifecycle="current",
               proposal=None):
    """A claim: subject + predicate + value, its evidence and basis. `proposal` (a model's own fields such as
    confidence) is kept for display; validate_claim never reads it for the basis."""
    if not valid_id(subject):
        raise RecordError("subject must be a record ID")
    if not isinstance(predicate, str) or not re.match(r"^[a-z][a-z0-9_]{0,40}$", predicate):
        raise RecordError("predicate is a short lower-case name")
    _one_of(basis, BASIS, "basis")
    sc = {k: (scope or {}).get(k) or "" for k in ("env", "connection", "repo")}
    tm = {k: (time or {}).get(k) or "" for k in ("valid_from", "valid_to")}
    return {"id": claim_id(subject, predicate, value), "schema_version": SCHEMA_VERSION, "subject": subject,
            "predicate": predicate, "value": _canon(value), "evidence": _ids(list(evidence or []), "evidence", "v"),
            "basis": basis, "scope": sc, "time": tm,
            "quality": {"validation": "unvalidated", "conflicts": [], "unsupported": []},
            "lifecycle": _one_of(lifecycle, LIFECYCLE, "lifecycle"), "proposal": dict(proposal or {})}


# --------------------------------------------------------------------------- basis

def counts(ev):
    """True when an evidence record may support anything now (not rejected, not awaiting review, not superseded)."""
    q = (ev or {}).get("quality") or {}
    return (ev or {}).get("lifecycle", "current") in ("current", "historical") and \
        q.get("validation") not in ("rejected", "needs_review")


def strongest_basis(evidence_list, structural=False):
    """The strongest basis the evidence supports, or None without usable evidence. `structural`: the claim asserts a
    constraint (STRUCTURAL_PREDICATES, a foreign key); then an observed sample supports nothing, since it only shows
    the sampled rows matched. A model proposal never supports more than inferred."""
    best = None
    for ev in evidence_list or []:
        if not counts(ev):
            continue
        b = ev.get("basis")
        cap = KIND_BASIS.get(ev.get("kind"), "inferred")
        if b != cap:  # a stored record edited to say more (or something else) counts as inferred
            b = "inferred"
        if structural and b == "observed_sample":
            continue
        if best is None or BASIS_RANK[b] > BASIS_RANK[best]:
            best = b
    return best


# --------------------------------------------------------------------------- code citations

def _rel_path_reason(rel):
    if not isinstance(rel, str) or not rel.strip():
        return "empty path"
    if "\x00" in rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel) or ":" in rel:
        return "absolute path or drive"
    if ".." in re.split(r"[\\/]+", rel):
        return "'..' in path"
    return None


CITE_MAX_BYTES = 5_000_000


def cite_code(repo_root, rel_path, line_start, line_end):
    """Check one code citation by reading the file (never writing): -> {exists, content_hash, lines (the file's line
    count), in_range, reason}. Only a repo-relative path inside `repo_root` is read; `..`, absolute paths and links
    that lead outside are refused."""
    out = {"exists": False, "content_hash": "", "lines": 0, "in_range": False, "reason": ""}
    why = _rel_path_reason(rel_path)
    if why or not repo_root:
        out["reason"] = why or "no repository"
        return out
    root = os.path.realpath(repo_root)
    p = os.path.realpath(os.path.join(root, rel_path))
    if not common.path_within(p, root) or p == root:
        out["reason"] = "outside the repository"
        return out
    try:
        if os.path.getsize(p) > CITE_MAX_BYTES:
            out["reason"] = "file too large to check"
            return out
        with open(p, "rb") as f:
            data = f.read()
    except OSError:
        out["reason"] = "no such file"
        return out
    n = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
    ok = isinstance(line_start, int) and isinstance(line_end, int) and 1 <= line_start <= line_end <= n
    out.update(exists=True, content_hash=hashlib.sha256(data).hexdigest(), lines=n, in_range=ok,
               reason="" if ok else "lines %s-%s outside 1-%d" % (line_start, line_end, n))
    return out


def repo_checker(repos):
    """A citation checker over {repo canonical id: root folder} (the selected repos)."""
    def check(repo, rel, start, end):
        root = (repos or {}).get(repo)
        if not root:
            return {"exists": False, "content_hash": "", "lines": 0, "in_range": False,
                    "reason": "repository not selected"}
        return cite_code(root, rel, start, end)
    return check


# --------------------------------------------------------------------------- validation

def _result(state, reasons, basis=None, used=()):
    return {"ok": state == "validated", "state": state, "reasons": list(reasons), "basis": basis,
            "evidence": list(used)}


def _check_evidence(ev, subject_ids, scope, repo_check):
    """(state, reason): "ok" | "needs_review" | "rejected" for one evidence record used by a claim / edge."""
    if not any(s in (ev.get("subjects") or []) for s in subject_ids):
        return "rejected", "evidence %s is about something else" % ev.get("id")
    esc = ev.get("scope") or {}
    for f in ("env", "connection", "repo"):
        want = (scope or {}).get(f) or ""
        have = esc.get(f) or ""
        if have and not want:
            return "rejected", "the claim is wider than evidence %s (%s %s)" % (ev.get("id"), f, have)
        if have and want and have != want:
            return "rejected", "evidence %s is from another %s" % (ev.get("id"), f)
    prov = ev.get("provenance") or {}
    if ev.get("kind") == "source" and prov.get("type", "code") == "code":
        if repo_check is None:
            return "needs_review", "code citation %s not checked (no repositories selected)" % ev.get("id")
        r = repo_check(prov.get("repo"), prov.get("path"), prov.get("line_start"), prov.get("line_end"))
        if not r.get("exists"):
            return "rejected", "code citation %s: %s" % (ev.get("id"), r.get("reason") or "no such file")
        if not r.get("in_range"):
            return "rejected", "code citation %s: %s" % (ev.get("id"), r.get("reason") or "lines out of range")
        if r.get("content_hash") != prov.get("content_hash"):
            return "needs_review", "code citation %s: the file changed since it was cited" % ev.get("id")
    if not counts(ev):
        return "needs_review", "evidence %s is %s" % (ev.get("id"), ev.get("lifecycle"))
    return "ok", ""


def _checker(repo_check):
    if repo_check is None or callable(repo_check):
        return repo_check
    return repo_checker(repo_check)


def _graph(graph, key):
    g = (graph or {}).get(key)
    return g if isinstance(g, dict) else {}


def _validate_support(subject_ids, ev_ids, scope, claimed, structural, graph, repo_check):
    """Shared by claims and edges: -> result dict."""
    evidence = _graph(graph, "evidence")
    reasons, used, review = [], [], False
    for vid in ev_ids:
        ev = evidence.get(vid)
        if not ev:
            return _result("rejected", ["unknown evidence %s (invented reference)" % vid])
        state, why = _check_evidence(ev, subject_ids, scope, repo_check)
        if state == "rejected":
            return _result("rejected", [why])
        if state == "needs_review":
            review = True
            reasons.append(why)
            continue
        used.append(ev)
    best = strongest_basis(used, structural=structural)
    if claimed not in BASIS_RANK:
        return _result("rejected", ["unknown basis %r" % claimed])
    if best is None and claimed != "inferred":
        return _result("needs_review" if review else "rejected",
                       reasons + ["%s without validated evidence" % BASIS_LABEL[claimed]], None, [e["id"] for e in used])
    if best is not None and BASIS_RANK[claimed] > BASIS_RANK[best]:
        if review:
            return _result("needs_review", reasons + ["basis %s waits for review (validated: %s)" % (claimed, best)],
                           best, [e["id"] for e in used])
        return _result("rejected", ["basis %s is stronger than the evidence (%s)" % (claimed, best)], best,
                       [e["id"] for e in used])
    return _result("needs_review" if review else "validated", reasons, claimed, [e["id"] for e in used])


def validate_claim(claim, graph, repo_check=None):
    """-> {ok, state: validated | needs_review | rejected, reasons, basis, evidence}. `graph`: {nodes, edges, evidence,
    claims} dicts by ID; `repo_check`: {repo id: root} or a callable(repo, path, start, end) (cite_code's result)."""
    c = claim if isinstance(claim, dict) else {}
    subj = c.get("subject")
    if not valid_id(subj) or (subj not in _graph(graph, "nodes") and subj not in _graph(graph, "edges")):
        return _result("rejected", ["unknown subject %s (invented reference)" % subj])
    structural = c.get("predicate") in STRUCTURAL_PREDICATES
    return _validate_support([subj], list(c.get("evidence") or []), c.get("scope") or {}, c.get("basis"), structural,
                             graph, _checker(repo_check))


def validate_edge(edge, graph, repo_check=None):
    """As validate_claim for an edge: both ends and every mapped column must exist (from-columns belong to the source,
    to-columns to the target), its evidence must name the edge, and a structural edge (foreign key) or a cardinality
    is never supported by an observed sample."""
    e = edge if isinstance(edge, dict) else {}
    nodes = _graph(graph, "nodes")
    for end in ("source", "target"):
        if e.get(end) not in nodes:
            return _result("rejected", ["unknown %s %s (invented reference)" % (end, e.get(end))])
    for m in e.get("column_mapping") or []:
        for side, owner in (("from", e.get("source")), ("to", e.get("target"))):
            for cid in m.get(side) or []:
                col = nodes.get(cid)
                if not col:
                    return _result("rejected", ["unknown column %s (invented reference)" % cid])
                if col.get("parent") != owner and cid != owner:
                    return _result("rejected", ["column %s does not belong to the edge's %s" %
                                                (cid, "source" if side == "from" else "target")])
    structural = e.get("layer") == "structure" and e.get("kind") == "foreign_key" or e.get("cardinality") is not None
    a = e.get("applicability") or {}
    scope = {"env": a.get("env") or "", "connection": a.get("connection") or "", "repo": a.get("repo") or ""}
    return _validate_support([e.get("id")], list(e.get("evidence") or []), scope, e.get("basis"), structural, graph,
                             _checker(repo_check))


def apply_validation(rec, result):
    """Record a validation result on a claim / edge: rejected records are not stored by callers; needs_review ones are
    stored with that lifecycle so they never read as current fact."""
    out = dict(rec)
    q = dict(out.get("quality") or {})
    q["validation"] = result["state"]
    q["unsupported"] = list(result.get("reasons") or [])
    out["quality"] = q
    if result["state"] == "needs_review":
        out["lifecycle"] = "needs_review"
    return out


def record_conflict(graph, claim):
    """Store `claim` in graph["claims"] next to any claim on the same subject and predicate with another value: both
    are kept, each listing the other under quality.conflicts. Returns the IDs it conflicts with."""
    claims = graph.setdefault("claims", {})
    others = [k for k, c in claims.items() if k != claim["id"] and c.get("subject") == claim["subject"] and
              c.get("predicate") == claim["predicate"] and c.get("value") != claim["value"]]
    mine = dict(claim, quality=dict(claim.get("quality") or {}))
    old = claims.get(claim["id"])
    if old:  # the same claim again: evidence accumulates, conflicts stay
        mine["evidence"] = list(dict.fromkeys(list(old.get("evidence") or []) + list(mine.get("evidence") or [])))
    mine["quality"]["conflicts"] = sorted(set((mine["quality"].get("conflicts") or []) + others))
    claims[claim["id"]] = mine
    for k in others:
        q = dict(claims[k].get("quality") or {})
        q["conflicts"] = sorted(set((q.get("conflicts") or []) + [claim["id"]]))
        claims[k] = dict(claims[k], quality=q)
    return others


# --------------------------------------------------------------------------- sampled links

SAMPLE_STRONG, SAMPLE_SOME = 0.8, 0.3  # how a sampled match rate is drawn; never a verification


def sample_label(sampled, matched, env="qa", date=""):
    """"Sample matched 95% (19 of 20, QA, 2026-11-05)" for a link-map sample; "Not sampled" without one. A sample
    supports only the sampled relationship (basis observed_sample): never "verified", never a foreign key."""
    try:
        s, m = int(sampled or 0), int(matched or 0)
    except (TypeError, ValueError):
        s, m = 0, 0
    if not s:
        return "Not sampled"
    return "Sample matched %d%% (%d of %d, %s%s)" % (round(100.0 * m / s), m, s, str(env or "qa").upper(),
                                                     ", " + str(date)[:10] if date else "")


def sample_status(sampled, matched):
    """(status, ratio): sample_matched (>= 80 % of sampled values found), sample_partial (>= 30 %), sample_weak,
    or not_sampled. Labels say what was observed; none of them means verified."""
    try:
        s, m = int(sampled or 0), int(matched or 0)
    except (TypeError, ValueError):
        s, m = 0, 0
    if not s:
        return "not_sampled", None
    r = m / float(s)
    return ("sample_matched" if r >= SAMPLE_STRONG else "sample_partial" if r >= SAMPLE_SOME else "sample_weak"), round(r, 2)
