"""Pure workflow validation, advisory coverage and conservative evidence applicability.

No collectors, tool execution or I/O. Historical observations never become current
because a plan/checklist/model says they are complete.
"""
import copy
import hashlib
import json
import math
import re

from .redact import redact

SCHEMA = 1
STAGES = {"new", "planning", "planned", "review_incomplete", "plan_reviewed", "approved",
          "execution_ready", "in_development", "paused", "awaiting_verification", "pr_review", "completed"}
ACTIVE = {"in_development", "awaiting_verification", "pr_review"}
COVERAGE = {"Covered": 1.0, "Partial": 0.5, "Missing": 0.0, "Unverified": 0.0}
ID = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
REQUIRED_SECTIONS = ("approach", "components", "database", "risks", "testing", "verification")
# STATE-01, row by row: (from, trigger, to). Only `new` enters planning, so its failure/cancel returns to `new`.
# Overlays never replace a stage; Resume Task and cancel are not transitions.
RULES = (
    ({"new"}, "generate", "planning"),
    ({"planning"}, "plan_saved", "planned"),
    ({"planning"}, "planning_failed", "new"),
    ({"planned"}, "open_review", "review_incomplete"),
    ({"review_incomplete"}, "review", "plan_reviewed"),
    ({"planned", "review_incomplete", "plan_reviewed", "approved"}, "improve", "review_incomplete"),
    ({"planned", "review_incomplete", "plan_reviewed"}, "execute", "approved"),
    ({"approved"}, "prepare", "execution_ready"),
    ({"execution_ready"}, "start", "in_development"),
    ({"execution_ready"}, "start", "awaiting_verification"),
    ({"execution_ready"}, "start", "pr_review"),
    ({"in_development", "awaiting_verification", "pr_review"}, "pause", "paused"),
    ({"paused"}, "resume_execution", "execution_ready"),
    ({"in_development"}, "request_verification", "awaiting_verification"),
    ({"in_development", "awaiting_verification"}, "observed_pr", "pr_review"),
    ({"awaiting_verification", "pr_review"}, "continuation", "execution_ready"),
    ({"awaiting_verification", "pr_review"}, "complete", "completed"),
    ({"completed"}, "reopen", "awaiting_verification"),
    ({"completed"}, "reopen", "pr_review"),
    ({"completed"}, "reopen", "paused"),
)
TRANSITIONS = {}
for _from, _trigger, _to in RULES:
    for _stage in _from:
        TRANSITIONS.setdefault(_stage, set()).add(_to)
TRANSITIONS.setdefault("completed", set())
OVERLAYS = ("blocked", "stale", "cancelled", "execution_status_unknown")
# Dispatch states of one approval/intent. The copy fallback stays `prepared`; only captured activity starts it.
DISPATCH = {"prepared": {"dispatching", "rejected", "unknown", "cancelled", "started"},
            "dispatching": {"started", "rejected", "unknown", "cancelled"},
            "unknown": {"started", "rejected", "cancelled"},
            "started": set(), "rejected": set(), "cancelled": set()}
OPEN_DISPATCH = {"prepared", "dispatching", "unknown"}
CONTEXT_KEYS = ("repo_id", "location", "head", "worktree", "config", "environment")
VERIFYING = {"test", "ci", "acceptance"}
MANUAL = {"acceptance", "code_inspection"}
DEFAULTS = {"history": [], "observations": [], "verifications": [], "completions": [], "obligations": [],
            "verification_request": None, "archived": None, "owner": None, "ingested_at": None,
            "legacy_ingested": [], "imported": {}, "child_plan": None, "pr_applied": []}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def text(value, limit=16000):
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise ValueError("text missing or exceeds its size limit")
    # Preserve exact accepted text. Never silently redact a developer's approved plan.
    if redact(value) != value or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise ValueError("remove sensitive data or terminal controls before saving")
    return value


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("invalid workflow identifier")
    return value


def bounded(value, limit=32768):
    """An optional structured field (lineage, source coverage, plan details): JSON, finite, sanitized, size-capped."""
    if not isinstance(value, dict):
        raise ValueError("structured detail must be an object")
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if len(raw.encode("utf-8")) > limit:
        raise ValueError("structured detail exceeds its size limit")
    if redact(raw) != raw or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", raw):
        raise ValueError("remove sensitive data or terminal controls before saving")
    return json.loads(raw)


def _lineage_row(row, out):
    """Optional criterion lineage: cited source fragment, content digest and compound parent."""
    fragment = row.get("fragment")
    if fragment is not None:
        if not isinstance(fragment, dict) or not isinstance(fragment.get("index", 0), int) \
                or not re.fullmatch(r"[a-f0-9]{8,64}", str(fragment.get("digest") or "")):
            raise ValueError("invalid criterion source fragment")
        out["fragment"] = {"field": text(str(fragment.get("field") or ""), 100), "index": fragment.get("index", 0),
                           "digest": fragment["digest"]}
    if row.get("digest") is not None:
        if not re.fullmatch(r"[a-f0-9]{8,64}", str(row["digest"])):
            raise ValueError("invalid criterion digest")
        out["digest"] = row["digest"]
    if row.get("parent") is not None:
        out["parent"] = identifier(row["parent"])


def baseline(criteria, completeness="partial", source_revision="", assumptions=None, lineage=None, coverage=None):
    if not isinstance(criteria, list) or len(criteria) > 500:
        raise ValueError("requirements must be a list of at most 500 criteria")
    if completeness not in {"complete", "partial", "unavailable"}:
        raise ValueError("invalid requirements completeness")
    rows, seen = [], set()
    for row in criteria:
        cid = identifier(row.get("id"))
        if cid in seen:
            raise ValueError("duplicate criterion")
        seen.add(cid)
        weight = row.get("weight", 1)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0:
            raise ValueError("criterion weights must be positive finite numbers")
        wording = text(row.get("text"), 8000)
        if not wording.strip():
            raise ValueError("empty criterion")
        excluded = row.get("excluded", False)
        if not isinstance(excluded, bool):
            raise ValueError("excluded must be boolean")
        rationale = text(row.get("rationale", ""), 2000)
        if excluded and not rationale.strip():
            raise ValueError("excluded criterion needs developer rationale")
        rows.append({"id": cid, "text": wording, "weight": weight, "excluded": excluded,
                     "rationale": rationale, "source": text(row.get("source", "local clarification"), 2000)})
        _lineage_row(row, rows[-1])
    if not math.isfinite(sum(row["weight"] for row in rows)):
        raise ValueError("total criterion weight must remain finite")
    out = {"schema_version": SCHEMA, "criteria": rows, "completeness": completeness,
           "source_revision": text(source_revision, 200), "assumptions": [text(s, 2000) for s in (assumptions or [])],
           "rubric": "coverage-v1"}
    # Optional: absent fields keep the identity of baselines saved before them.
    if lineage is not None:
        out["lineage"] = bounded(lineage)
    if coverage is not None:
        out["coverage"] = bounded(coverage)
    out["id"] = "b_" + digest(out)
    return out


def plan(body, requirements, parent=None, source="developer"):
    if source not in {"developer", "claude"} or not isinstance(body, dict):
        raise ValueError("invalid plan source/body")
    sections = {s: text(body.get(s), 8000) for s in REQUIRED_SECTIONS}
    if any(not value.strip() for value in sections.values()):
        raise ValueError("all plan sections required; record unknown/not applicable explicitly")
    steps, seen = [], set()
    criteria = {c["id"] for c in requirements["criteria"]}
    raw = body.get("steps")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 200:
        raise ValueError("plan needs 1 to 200 structured steps")
    for step in raw:
        sid = identifier(step.get("id"))
        deps = step.get("depends_on", [])
        maps = step.get("criteria", [])
        if sid in seen or not isinstance(deps, list) or not set(deps).issubset(seen):
            raise ValueError("steps must have unique IDs in dependency order")
        if not isinstance(maps, list) or not set(maps).issubset(criteria):
            raise ValueError("step refers to unknown requirements")
        kind = step.get("kind", "implementation")
        if kind not in {"implementation", "verification"}:
            raise ValueError("invalid step kind")
        steps.append({"id": sid, "text": text(step.get("text"), 4000), "kind": kind,
                      "criteria": maps, "depends_on": deps})
        seen.add(sid)
    out = {"schema_version": SCHEMA, "text": text(body.get("text"), 64000), "sections": sections,
           "steps": steps, "baseline": requirements["id"], "parent": parent, "source": source,
           "assumptions": [text(s, 2000) for s in body.get("assumptions", [])]}
    if body.get("details") is not None:  # structured files/tests/mapping/unknowns of a generated plan
        out["details"] = bounded(body["details"], 65536)
    if not out["text"].strip():
        raise ValueError("empty plan")
    out["id"] = "p_" + digest(out)
    return out


def score(requirements, classifications, available=True):
    """Confidence never participates. Partial answers retain the entire denominator."""
    numerator, denominator = 0.0, 0.0
    counts = dict.fromkeys(COVERAGE, 0)
    for criterion in requirements["criteria"]:
        if criterion["excluded"]:
            continue
        status = classifications.get(criterion["id"], "Unverified")
        if status not in COVERAGE:
            status = "Unverified"
        weight = criterion["weight"]
        numerator += weight * COVERAGE[status]
        denominator += weight
        counts[status] += 1
    return {"percent": round(numerator / denominator * 100, 1) if denominator and available else None,
            "numerator": numerator, "denominator": denominator, "counts": counts,
            "label": "Coverage of available criteria" if requirements["completeness"] != "complete" else "Plan coverage",
            "state": "available" if available else "unavailable", "baseline": requirements["id"]}


def _unknown(value):
    # A dirty tree has no captured fingerprint: it can never equal an unchanged state.
    return not isinstance(value, str) or value.strip().lower() in {"", "unknown", "dirty"}


def applies(observation, current):
    """(current|historical|unknown, reason). Unknown state is never reconstructed or treated as unchanged."""
    saved = observation.get("context") or {}
    missing = [k for k in CONTEXT_KEYS if _unknown(saved.get(k)) or _unknown(current.get(k))]
    changed = [k for k in CONTEXT_KEYS if k not in missing and saved[k] != current[k]]
    if changed:  # a known later change is the more specific reason; neither state can verify
        return "historical", "changed since observation: " + ", ".join(changed)
    if missing:
        return "unknown", "not captured: " + ", ".join(missing)
    return "current", ""


def applicability(observation, current):
    """Unknown dirty/config/env state cannot equal unchanged. No later reconstruction."""
    return applies(observation, current)[0]


def scoped(e):
    """Captured rows need a source and time; manual rows also actor, revision and limits (self-reported)."""
    if not (e.get("source_ref") and e.get("observed_at")):
        return False
    return e.get("kind") not in MANUAL or bool(e.get("actor") and e.get("revision") and e.get("limits"))


def reconcile(saved_plan, evidence, current):
    """Evidence-backed step state. A blank checklist never reopens evidenced work; a historical pass
    never survives a later change: that step is uncertain and listed for revalidation."""
    groups = {"completed": [], "pending": [], "uncertain": [], "verification": [], "revalidate": []}
    for step in saved_plan["steps"]:
        if step["kind"] == "verification":
            groups["verification"].append(step["id"])
            continue
        observations = [e for e in evidence if step["id"] in e.get("steps", [])]
        current_rows = [e for e in observations if applicability(e, current) == "current"]
        valid = [e for e in current_rows if e.get("kind") in VERIFYING | MANUAL
                 and scoped(e) and e.get("outcome") == "passed"]
        failures = [e for e in current_rows if e.get("outcome") == "failed"]
        key = "completed" if valid and not failures else "uncertain" if observations else "pending"
        groups[key].append(step["id"])
        if key == "uncertain" and not current_rows and any(e.get("outcome") == "passed" for e in observations):
            groups["revalidate"].append(step["id"])
    return groups


def progress(saved_plan, groups):
    """Evidenced implementation steps / approved implementation steps. Verification steps never count."""
    total = sum(1 for s in saved_plan["steps"] if s["kind"] == "implementation")
    return {"numerator": len(groups["completed"]), "denominator": total,
            "pending": len(groups["pending"]), "uncertain": len(groups["uncertain"]),
            "label": "evidenced implementation steps / approved implementation steps"}


def verification(requirements, evidence, current):
    """Requirement ledger. Verified needs current, scoped test/CI/acceptance evidence mapped to the
    criterion on this baseline. Plan coverage, Jev confidence, PRs, commits and old passes never verify."""
    rows = []
    for criterion in requirements["criteria"]:
        refs = [e for e in evidence if criterion["id"] in e.get("criteria", [])
                and e.get("baseline") == requirements["id"]]
        applicable = [e for e in refs if applicability(e, current) == "current" and scoped(e)
                      and e.get("kind") in VERIFYING]
        passed = [e for e in applicable if e.get("outcome") == "passed"]
        failed = any(e.get("outcome") == "failed" for e in applicable)
        status = ("Missing" if failed else "Verified" if any(e.get("coverage") == "complete" for e in passed)
                  else "Partial" if passed else "Unverified")
        cited = []
        for e in refs:
            state, reason = applies(e, current)
            cited.append({"id": e["id"], "applicability": state, "reason": reason, "kind": e.get("kind"),
                          "outcome": e.get("outcome"), "observed_at": e.get("observed_at"),
                          "revision": (e.get("context") or {}).get("head"),
                          "basis": "self-reported" if e.get("kind") in MANUAL else "captured"})
        invalidated = [c["reason"] for c in cited if c["applicability"] != "current"]
        rows.append({"criterion": criterion["id"], "status": status, "excluded": criterion["excluded"],
                     "failing": failed, "evidence": cited,
                     "basis": sorted({c["basis"] for c in cited if c["applicability"] == "current"}),
                     "applicable_revision": current.get("head") if not _unknown(current.get("head")) else None,
                     "observed_at": max((e.get("observed_at") or "" for e in applicable), default=None) or None,
                     "invalidation": "; ".join(sorted(set(invalidated))) if status != "Verified" else ""})
    denominator = sum(c["weight"] for c in requirements["criteria"] if not c["excluded"])
    verified = {r["criterion"] for r in rows if r["status"] == "Verified" and not r["excluded"]}
    numerator = sum(c["weight"] for c in requirements["criteria"] if c["id"] in verified)
    return {"rows": rows, "percent": round(100 * numerator / denominator, 1) if denominator else None,
            "numerator": numerator, "denominator": denominator,
            "gaps": [r["criterion"] for r in rows if r["status"] != "Verified" and not r["excluded"]]}


def transition(record, target, trigger=None):
    """Validate one STATE-01 row. Without a trigger only the edge is checked (read-side callers)."""
    stage = record["stage"]
    allowed = [r for r in RULES if stage in r[0] and r[2] == target and (trigger is None or r[1] == trigger)]
    if not allowed:
        nxt = ", ".join(sorted(TRANSITIONS.get(stage, ()))) or "none"
        raise ValueError("unsupported workflow transition: %s to %s%s; allowed next stages: %s. Nothing was changed."
                         % (stage, target, " by " + trigger if trigger else "", nxt))
    record["stage"] = target


def dispatch(approval, state):
    """Advance an intent's dispatch state; terminal states never reopen and nothing is resent."""
    old = approval.get("dispatch", "prepared")
    if state not in DISPATCH.get(old, set()):
        raise ValueError("handoff is %s; it cannot become %s. Record a new reviewed approval instead." % (old, state))
    approval["dispatch"] = state


def normalize(record):
    """Older records tolerate absent fields; legacy cursor lists become {stable ID: version}."""
    out = dict(record)
    for key, value in DEFAULTS.items():
        if out.get(key) is None and value is not None:
            out[key] = copy.deepcopy(value)
        out.setdefault(key, copy.deepcopy(value))
    for key in ("ingested", "acknowledged"):
        value = out.get(key)
        out[key] = ({str(x): "" for x in value} if isinstance(value, list)
                    else dict(value) if isinstance(value, dict) else {})
    overlays = out.get("overlays") if isinstance(out.get("overlays"), dict) else {}
    out["overlays"] = {k: overlays.get(k) for k in OVERLAYS}
    return out


def rollup(workflows):
    """Per-repository workflows of one ticket. Never hides an unfinished repository."""
    rows = [{"repo_id": w["binding"]["repo_id"], "location": w["binding"]["location"], "stage": w["stage"],
             "workflow": w["id"], "archived": bool(w.get("archived"))} for w in workflows]
    open_rows = [r for r in rows if r["stage"] != "completed"]  # archived is not completion
    done = sum(1 for r in rows if r["stage"] == "completed")
    return {"total": len(rows), "completed": done, "unfinished": open_rows, "repositories": rows,
            "complete": bool(rows) and not open_rows and done == len(rows),
            "label": "%d of %d repositories completed" % (done, len(rows)) if rows else "No workflows"}
