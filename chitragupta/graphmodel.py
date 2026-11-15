"""Evidence Graph vocabulary: node and edge types, deterministic IDs, basis, lifecycle, row versions.

Pure: no clock, no random value, no I/O. The same inputs give byte-identical rows.
"""
import re

from .companionviews import safe
from .workflowmodel import digest

NODE_TYPES = {
    "ticket": "gt_", "requirement": "gr_", "requirement_revision": "gv_", "plan": "gp_", "plan_step": "gs_",
    "commit": "gm_", "code_change": "gc_", "test_case": "gk_", "test_run": "gx_", "observation": "go_",
    "pull_request": "gq_", "release_evidence": "gl_",
}
EDGE_PREFIX = "ge_"
EDGE_TYPES = (
    "HAS_REQUIREMENT", "SUPERSEDES", "ADDRESSED_BY", "DEPENDS_ON", "HAS_EVIDENCE", "IMPLEMENTED_BY",
    "PART_OF_COMMIT", "EQUIVALENT_TO", "CHERRY_PICKED_FROM", "SQUASHED_INTO", "COVERED_BY_TEST", "EXECUTED_IN",
    "SUPPORTED_BY", "INCLUDED_IN_PR", "CHECKED_BY", "HAS_RELEASE_EVIDENCE",
)
BASIS = ("observed", "declared", "inferred", "unknown")
LIFECYCLE = ("current", "superseded", "removed", "historical", "source_missing")
ID = re.compile(r"^g[a-z]_[0-9a-f]{24}$")

LABEL_LIMIT = 300
ATTR_LIMIT = 1000
NODE_COLUMNS = {"lifecycle": "current", "repo_id": "", "location": "", "workflow": "", "source_ref": "",
                "observed_at": ""}
EDGE_COLUMNS = {"lifecycle": "current", "observed_at": "", "workflow": "", "generation": 0}
_SCALAR = (str, int, float, bool, type(None))


def node_id(type, *identity):
    """Prefix + first 24 hex of sha256 over the canonical JSON of the identity tuple."""
    if type not in NODE_TYPES:
        raise ValueError("unknown node type: %r" % (type,))
    if not identity or not all(isinstance(part, _SCALAR) for part in identity):
        raise ValueError("node identity must be a non-empty tuple of scalars")
    return NODE_TYPES[type] + digest(list(identity))[:24]


def edge_id(type, src, dst, scope, basis):
    if type not in EDGE_TYPES:
        raise ValueError("unknown edge type: %r" % (type,))
    if basis not in BASIS:
        raise ValueError("unknown basis: %r" % (basis,))
    return EDGE_PREFIX + digest([type, src, dst, scope, basis])[:24]


def _clean(value):
    """Attributes as stored: strings redacted and bounded, containers copied, nothing else allowed."""
    if isinstance(value, str):
        return safe(value, ATTR_LIMIT)
    if isinstance(value, _SCALAR):
        return value
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("attribute names must be strings")
        return {key: _clean(item) for key, item in value.items()}
    raise ValueError("attribute value of unsupported type: %s" % type(value).__name__)


def _columns(defaults, cols):
    unknown = sorted(set(cols) - set(defaults))
    if unknown:
        raise ValueError("unknown column: %s" % ", ".join(unknown))
    out = dict(defaults, **cols)
    if out["lifecycle"] not in LIFECYCLE:
        raise ValueError("unknown lifecycle: %r" % (out["lifecycle"],))
    return out


def version(row):
    """16 hex over the stored row; `generation` (and the version itself) do not count."""
    return digest({key: value for key, value in row.items() if key not in ("generation", "version")})[:16]


def node(type, identity, ticket, label, attrs=None, **cols):
    row = {"id": node_id(type, *identity), "type": type, "ticket": ticket, "label": safe(label, LABEL_LIMIT),
           "attrs": _clean(dict(attrs or {}))}
    row.update(_columns(NODE_COLUMNS, cols))
    row["version"] = version(row)
    return row


def edge(type, src, dst, ticket, basis, scope="", evidence_ref="", confidence=None, attrs=None, **cols):
    eid = edge_id(type, src, dst, scope, basis)
    for end in (src, dst):
        if not isinstance(end, str) or not ID.match(end):
            raise ValueError("edge endpoint is not a graph id: %r" % (end,))
    if basis == "inferred":
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("an inferred edge needs a confidence between 0 and 1")
    elif confidence is not None:
        raise ValueError("confidence is only allowed on an inferred edge")
    row = {"id": eid, "type": type, "src": src, "dst": dst, "ticket": ticket, "basis": basis, "scope": scope,
           "evidence_ref": evidence_ref, "confidence": confidence, "attrs": _clean(dict(attrs or {}))}
    row.update(_columns(EDGE_COLUMNS, cols))
    row["version"] = version(row)
    return row


def add_declaration(edge, by, at, ref):
    """Record one more declaration of the same edge: kept in time order, never duplicated, never replaced."""
    entry = {"by": safe(by, LABEL_LIMIT), "at": safe(at, LABEL_LIMIT), "ref": safe(ref, LABEL_LIMIT)}
    declarations = list(edge["attrs"].get("declarations") or [])
    if entry not in declarations:
        declarations.append(entry)
    declarations.sort(key=lambda item: (item["at"], item["by"], item["ref"]))
    edge["attrs"]["declarations"] = declarations
    edge["version"] = version(edge)
    return edge


def _rows(rows):
    plain = [{key: value for key, value in row.items() if key != "generation"} for row in rows]
    return sorted(plain, key=digest)


def content(nodes, edges, statuses):
    """One digest naming what the graph is, whatever order the rows arrive in and whichever generation wrote them."""
    return digest({"nodes": _rows(nodes), "edges": _rows(edges), "statuses": _rows(statuses)})


# --- status rules (design section 11). Builders and readers call these; nothing else decides a state. ---

ORDER = {   # least favourable first: the first one present wins when scopes are combined
    "plan_coverage": ("Missing", "Unverified", "Partial", "Covered"),
    "implementation": ("None", "Unverified association", "Partial", "Linked"),
    "verification": ("Failed", "Historical only", "Unverified", "Partial", "Verified within recorded scope"),
}
DEVELOPER_ORIGIN = "developer assessment"
JEV_ORIGIN = "Jev classification"
PLAN_CHANGED = "requirement changed after this plan; re-evaluate"
VERIFY_CHANGED = ("verified for the earlier wording confirmed in this task's baseline; "
                  "the requirement has changed since")
_LEDGER = {"Verified": "Verified within recorded scope", "Partial": "Partial", "Missing": "Failed"}
_VERIFY_REASON = {"Verified within recorded scope": "applicable passing evidence with complete coverage",
                  "Partial": "applicable passing evidence with partial coverage",
                  "Failed": "an applicable mapped check failed",
                  "Historical only": "passing evidence exists only for an earlier revision or context",
                  "Unverified": "no applicable passing evidence"}
_PR_DONE = ("merged", "closed", "abandoned", "completed", "declined")
_PROPOSAL = {"schema", "origin", "type", "src", "dst", "confidence", "rationale"}


def _status(state, basis, reason, **extra):
    return dict({"state": state, "basis": basis, "reason": reason}, **extra)


def plan_coverage(has_plan, plan_revision_current, evaluation, mapped):
    """`evaluation` is None or {state, origin, confidence?} for this criterion on the plan in question."""
    if not has_plan:
        return _status("Unverified", "unknown", "no plan")
    if not plan_revision_current:
        return _status("Unverified", "observed", PLAN_CHANGED)
    if evaluation:
        state = evaluation.get("state")
        if state not in ORDER["plan_coverage"]:
            raise ValueError("unknown plan coverage state: %r" % (state,))
        if evaluation.get("origin") == DEVELOPER_ORIGIN:
            return _status(state, "declared", DEVELOPER_ORIGIN)
        confidence = evaluation.get("confidence")   # shown, never used
        shown = isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        return _status(state, "inferred", JEV_ORIGIN + (" (confidence %g)" % confidence if shown else ""))
    if mapped:
        return _status("Covered", "declared", "plan mapping; not evaluated")
    return _status("Missing", "declared", "no plan step addresses this requirement")


def _counts(row):
    return (row.get("type") == "IMPLEMENTED_BY" and row.get("basis") == "declared"
            and not (row.get("attrs") or {}).get("ambiguous"))


def implementation(declared_edges, steps, ticket_has_changes):
    """`steps`: ids of the implementation steps addressing the requirement. Inferred or ambiguous edges never count."""
    counted = sorted((row for row in declared_edges if _counts(row)), key=lambda row: row["id"])
    ids = [row["id"] for row in counted]
    wanted = set(steps)
    if counted:
        have = wanted & {row["src"] for row in counted}
        if have == wanted:
            return _status("Linked", "declared", "every implementation step has a declared link" if wanted
                           else "the requirement has a declared link", edges=ids)
        return _status("Partial", "declared", "%d of %d implementation steps have a declared link"
                       % (len(have), len(wanted)), edges=ids)
    if ticket_has_changes:
        return _status("Unverified association", "observed",
                       "changes exist for the ticket; none is linked to this requirement", edges=[])
    return _status("None", "unknown", "nothing recorded", edges=[])


def verification(ledger_row, confirmed_revision, current_revision):
    """Map one `workflowmodel.verification` row. It speaks for the revision its baseline confirmed, never a newer one."""
    if not ledger_row or not confirmed_revision:
        return _status("Unverified", "unknown", "no confirmed baseline covers this requirement")
    if confirmed_revision != current_revision:
        return _status("Unverified", "unknown", VERIFY_CHANGED)
    cited = ledger_row.get("evidence") or []
    if ledger_row.get("status") in _LEDGER:
        state = _LEDGER[ledger_row["status"]]
        used = [c for c in cited if c.get("applicability") == "current"]
    else:
        used = [c for c in cited if c.get("applicability") == "historical" and c.get("outcome") == "passed"]
        state = "Historical only" if used else "Unverified"
    kinds = {c.get("basis") for c in used}
    basis = "observed" if "captured" in kinds else "declared" if "self-reported" in kinds else "unknown"
    return _status(state, basis, _VERIFY_REASON[state])


def combine(dimension, states):
    """The least favourable state among scopes; None when there is none. Callers leave excluded rows out."""
    if dimension not in ORDER:
        raise ValueError("unknown dimension: %r" % (dimension,))
    states = list(states)
    for state in states:
        if state not in ORDER[dimension]:
            raise ValueError("unknown %s state: %r" % (dimension, state))
    return min(states, key=ORDER[dimension].index) if states else None


def _state(row, dimension):
    return ((row.get("statuses") or {}).get(dimension) or {}).get("state")


def _reason(row, dimension):
    return ((row.get("statuses") or {}).get(dimension) or {}).get("reason")


_ACTIONS = (
    ("failed verification: %s", lambda r: _state(r, "verification") == "Failed"),
    ("requirement changed after plan or baseline: %s",
     lambda r: _reason(r, "plan_coverage") == PLAN_CHANGED or _reason(r, "verification") == VERIFY_CHANGED),
    ("requirement missing from the plan: %s", lambda r: _state(r, "plan_coverage") == "Missing"),
    ("review or rerun the applicable validation: %s", lambda r: _state(r, "verification") == "Historical only"),
    ("no linked implementation: %s", lambda r: _state(r, "implementation") in ("None", "Unverified association")),
    ("unverified: %s", lambda r: _state(r, "verification") == "Unverified"),
    ("partially verified: %s", lambda r: _state(r, "verification") == "Partial"),
)


def next_action(rows, prs, source=""):
    """One line, first match wins. Reads `row["statuses"][dimension]` = the combined {state, reason}."""
    if not rows:
        return "no requirements read from Jira yet" + (" (source: %s)" % source if source else "")
    rows = [row for row in rows if not row.get("excluded")]
    for wording, test in _ACTIONS:
        for row in rows:
            if test(row):
                return wording % str((row.get("aliases") or [row.get("id")])[0])
    numbers = sorted(pr["number"] for pr in prs or []
                     if isinstance(pr.get("number"), int) and str(pr.get("state") or "").lower() not in _PR_DONE)
    if numbers:
        return "review the recorded pull request #%d" % numbers[0]
    return "nothing outstanding in recorded evidence"


def proposal(obj, ticket="", scope=""):
    """The versioned contract for a proposed relationship. Whatever is offered, the result is an inferred edge."""
    if not isinstance(obj, dict) or set(obj) != _PROPOSAL:
        raise ValueError("a proposal has exactly: %s" % ", ".join(sorted(_PROPOSAL)))
    if obj["schema"] != 1 or isinstance(obj["schema"], bool):
        raise ValueError("unsupported proposal schema: %r" % (obj["schema"],))
    for name in ("origin", "rationale"):
        if not isinstance(obj[name], str) or not obj[name].strip():
            raise ValueError("proposal %s is missing" % name)
    for name in ("src", "dst"):
        if not isinstance(obj[name], str) or not ID.match(obj[name]) or obj[name][:3] not in NODE_TYPES.values():
            raise ValueError("proposal %s is not a node id" % name)
    if obj["src"] == obj["dst"]:
        raise ValueError("a proposal links two different nodes")
    return edge(obj["type"], obj["src"], obj["dst"], ticket, "inferred", scope=scope, confidence=obj["confidence"],
                attrs={"origin": obj["origin"], "rationale": obj["rationale"]})
