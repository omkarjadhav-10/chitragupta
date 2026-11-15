"""Jev adapter: request building, response validation, retry policy and scoring. Live transport is blocked.

No SDK, HTTP client, credential lookup or model proxy. The default transport raises
PermissionError(UNAVAILABLE): the safety contract permits no inference POST (JEV-03 is a proposed
amendment, not a shipped one). Tests inject a transport `fn(body_bytes, timeout) -> (status, headers,
body_bytes)`; it receives no URL, key or header, so source text can never pick an endpoint or reveal a
credential. Request/answer shapes are this adapter's normalized contract, not a claim of live wire
compatibility. Jev classifies; Claude explains; every statement keeps its origin label.
"""
import hashlib
import json
import math
import re
import time

from . import workflowmodel as model
from .redact import redact

UNAVAILABLE = "Jev unavailable by current safety policy"
MAX_QUESTIONS = 50
MAX_BYTES = 256 * 1024
REQUEST_TIMEOUT_S = 30
DEADLINE_S = 120
MAX_RETRIES = 2
RETRYABLE = {429, 529}
# Pinned adapter identity (confirm the model against the provider's model list before any enablement).
MODEL = "jev-1"
RUBRIC = "coverage-v1"
ADAPTER = "jev-adapter-1"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"  # documented destination; never passed to a transport
CREDENTIAL_ENV = "TYPESAFE_API_KEY"  # a reference only; this module never reads its value
CHOICES = list(model.COVERAGE)  # Covered / Partial / Missing / Unverified
FEASIBILITY = ["Feasible", "Uncertain", "Infeasible", "Unverified"]
RISK = ["No known gaps", "Gaps present", "Unverified"]
ORIGINS = {"jev": "Jev classification", "claude": "Claude suggestion", "fact": "recorded fact",
           "developer": "developer decision"}
STATUS = {400: "invalid_request", 401: "authentication", 402: "quota", 403: "permission", 413: "too_large",
          415: "unsupported_media", 422: "validation", 429: "rate_limited", 529: "overloaded"}
_ABS = re.compile(r"(?<![\w.])(?:[A-Za-z]:[\\/]|\\\\[\w.]|~[\\/]|/(?:home|Users|usr|etc|var|opt|tmp|mnt|root|srv)/)")
_URL = re.compile(r"(?i)\b(?:https?|ftp|file|wss?)://")
_TOOL = re.compile(r"(?i)(--dangerously|bypasspermissions|permission[-_ ]mode|allowed[-_ ]?tools|mcp__\w|"
                   r"skip[-_ ]permissions|ignore (?:all |the )?(?:previous|prior) instructions|"
                   r"(?:disable|turn off) (?:the )?(?:guard|safety|sandbox))")
_DBROW = re.compile(r"(?i)\b(?:where|and|or|set)\s+[\w.]{1,60}\s*(?:=|\bin\s*\()\s*(?:'[^']{1,200}'|\"[^\"]{1,200}\"|\d{3,})|"
                    r"\bvalues\s*\(\s*(?:'|\"|\d)")


def _no_constant(name):
    raise ValueError("non-finite number " + name)


def blocked_transport(*args, **kwargs):
    """The production transport: inference POST is not permitted by the current safety contract."""
    raise PermissionError(UNAVAILABLE)


def risk(value):
    """Why a value may not leave this machine, or None: secrets, absolute paths, URLs (endpoint/redirect
    injection), tool or permission requests, raw database keys or rows, terminal controls."""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    if redact(raw) != raw:
        return "secret"
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", raw):
        return "control characters"
    for name, rx in (("absolute path", _ABS), ("URL", _URL), ("tool or permission request", _TOOL),
                     ("raw database key or row", _DBROW)):
        if rx.search(raw):
            return name
    return None


def _clip(value, limit):
    return " ".join(str(value or "").split())[:limit]


def build_request(baseline, plan, offset=0):
    """Minimized System One request for one batch: one atomic choice question per applicable criterion
    (at most 48 here), plus separate feasibility and risk/test-gap questions; the rest is an explicit bounded
    continuation and stays Unverified in the denominator. Raises ValueError (nothing sent) when any input
    carries a secret, path, URL, tool request or raw DB data, or exceeds the size cap."""
    criteria = [c for c in baseline["criteria"] if not c["excluded"]]
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or (criteria and offset >= len(criteria)):
        raise ValueError("invalid evaluation batch")
    batch = criteria[offset:offset + MAX_QUESTIONS - 2]
    state = {"rubric": RUBRIC, "adapter": ADAPTER,
             "requirements": [{"id": c["id"], "text": _clip(c["text"], 1000)} for c in batch],
             "plan": {"approach": _clip(plan["sections"]["approach"], 4000),
                      "testing": _clip(plan["sections"]["testing"], 2000),
                      "verification": _clip(plan["sections"]["verification"], 2000),
                      "steps": [{"id": s["id"], "kind": s["kind"], "criteria": s["criteria"], "text": _clip(s["text"], 500)}
                                for s in plan["steps"][:100]]}}
    why = risk(state)
    if why:
        raise ValueError("Jev request refused before sending: %s in the evaluation input" % why)
    rule = ("Covered: the plan maps every aspect to implementation and validation steps. Partial: some aspects. "
            "Missing: a known omission in sufficiently complete inputs. Unverified: inputs are insufficient.")
    questions = [{"name": "criterion:" + c["id"], "type": "choice", "choices": CHOICES,
                  "question": "Classify the plan's coverage of requirement %s. %s" % (c["id"], rule)} for c in batch]
    questions.append({"name": "feasibility", "type": "choice", "choices": FEASIBILITY,
                      "question": "Is the plan technically feasible as described?"})
    questions.append({"name": "risk_test_gaps", "type": "choice", "choices": RISK,
                      "question": "Does the plan have known risk or test gaps?"})
    request = {"model": MODEL, "state": state, "questions": questions}
    body = json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(body) > MAX_BYTES:
        raise ValueError("Jev request refused before sending: larger than 256 KiB")
    nxt = offset + len(batch)
    return {"request": request, "body": body, "bytes": len(body), "digest": hashlib.sha256(body).hexdigest(),
            "evaluated": [c["id"] for c in batch], "questions": len(questions),
            "continuation": nxt if nxt < len(criteria) else None,
            "remaining": [c["id"] for c in criteria[nxt:]], "baseline": baseline["id"], "plan": plan["id"]}


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1


def parse_response(built, raw, baseline):
    """Validate one decoded response body (bytes/str/dict). Invalid or absent answers are Unverified; valid
    ones are kept in a visibly partial evaluation. Confidence never changes coverage."""
    problems, answers, findings, usage, returned = [], {}, {}, {}, None
    data = raw
    if isinstance(raw, (bytes, str)):
        size = len(raw.encode("utf-8") if isinstance(raw, str) else raw)
        if size > MAX_BYTES:
            data, problems = None, ["response larger than 256 KiB"]
        else:
            try:
                data = json.loads(raw, parse_constant=_no_constant)
            except ValueError:
                data, problems = None, ["response is not valid JSON"]
    if data is not None and not isinstance(data, dict):
        data, problems = None, ["response is not an object"]
    if data is not None:
        returned = data.get("model")
        if returned != MODEL:
            problems.append("unexpected model %s" % _clip(returned, 60))
            data = None
    if data is not None:
        raw_answers = data.get("answers")
        if isinstance(raw_answers, dict):
            raw_answers = [dict(v, name=k) if isinstance(v, dict) else {"name": k} for k, v in raw_answers.items()]
        if not isinstance(raw_answers, list):
            problems.append("no answers")
            raw_answers = []
        asked = {"criterion:" + cid for cid in built["evaluated"]} | {"feasibility", "risk_test_gaps"}
        seen, decisions = set(), []
        for a in raw_answers[:MAX_QUESTIONS * 2]:
            name = a.get("name") if isinstance(a, dict) else None
            if name not in asked or name in seen:
                problems.append("unknown or repeated answer %s" % _clip(name, 60))
                if name in seen:
                    decisions = [d for d in decisions if d["id"] != name[len("criterion:"):]]
                    findings.pop(name, None)
                continue
            seen.add(name)
            choice, confidence = a.get("choice"), a.get("confidence")
            probs = a.get("probabilities")
            if probs is not None and (not isinstance(probs, dict) or not all(_finite(v) for v in probs.values())):
                problems.append("invalid probabilities for %s" % name)
                continue
            if name.startswith("criterion:"):
                decisions.append({"id": name[len("criterion:"):], "status": choice, "confidence": confidence})
            elif choice in (FEASIBILITY if name == "feasibility" else RISK) and _finite(confidence):
                findings[name] = {"choice": choice, "confidence": confidence, "origin": ORIGINS["jev"]}
            else:
                problems.append("invalid answer for %s" % name)
        try:
            checked = validate_decisions(baseline, decisions)
        except ValueError:
            checked, problems = None, problems + ["oversized decisions"]
        if checked:
            answers = checked["answers"]
            if len(answers) < len(decisions):
                problems.append("invalid choice or confidence")
        u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage = {k: u[k] for k in ("input_tokens", "output_tokens", "cost_usd") if _number(u.get(k))}
    for row in answers.values():
        row["origin"] = ORIGINS["jev"]
    unevaluated = [c["id"] for c in baseline["criteria"] if not c["excluded"] and c["id"] not in answers]
    available = bool(answers)
    coverage = model.score(baseline, {k: v["status"] for k, v in answers.items()}, available)
    if not available:
        coverage.update(label="Evaluation unavailable", reason="no valid Jev answer")
    return {"provider": "Jev", "origin": ORIGINS["jev"], "model": returned if returned == MODEL else None,
            "rubric": RUBRIC, "adapter": ADAPTER, "answers": answers, "findings": findings,
            "state": "available" if available and not problems and not unevaluated else
                     "partial" if available else "unavailable",
            "partial": bool(problems or unevaluated), "problems": problems[:20], "unevaluated": unevaluated,
            "coverage": coverage, "usage": usage, "continuation": built.get("continuation")}


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def _retry_after(headers, now, deadline):
    try:
        wait = float((headers or {}).get("Retry-After") or (headers or {}).get("retry-after") or 1)
    except (TypeError, ValueError):
        wait = 1.0
    wait = max(0.0, wait) if math.isfinite(wait) else 1.0
    return wait if now + wait < deadline else None


def evaluate(built=None, baseline=None, transport=None, cancelled=None, check=None, clock=time.monotonic,
             sleep=time.sleep, deadline_s=DEADLINE_S, timeout_s=REQUEST_TIMEOUT_S):
    """One bounded evaluation attempt. The default transport is blocked and raises PermissionError(UNAVAILABLE)
    before anything else. Policy: 30 s per request inside a 120 s deadline; at most two retries, only for
    429/529, honouring Retry-After within the deadline; no retry for any other status, guard refusal or
    revocation; a timeout after possible acceptance is outcome_unknown; cancellation and `check()` (scope
    revalidation) run before every dispatch. Returns {state, category, attempts, retries, audit, evaluation}."""
    if transport is None or transport is blocked_transport:
        raise PermissionError(UNAVAILABLE)
    start = clock()
    deadline = start + deadline_s
    attempts, retries, category, state, evaluation = 0, 0, "", "failed", None
    while True:
        if cancelled and cancelled():
            state, category = "cancelled", "cancelled before dispatch"
            break
        try:
            if check:
                check()
        except PermissionError:
            state, category = "failed", "access revoked"
            break
        now = clock()
        if now >= deadline:
            state, category = "failed", "deadline reached before dispatch"
            break
        attempts += 1
        try:
            status, headers, body = transport(built["body"], timeout=min(timeout_s, deadline - now))
        except PermissionError:
            state, category = "unavailable", "guard refusal"
            break
        except TimeoutError:
            state, category = "outcome_unknown", "timeout after possible acceptance"
            break
        except OSError:
            state, category = "failed", "offline"
            break
        if status == 200:
            evaluation = parse_response(built, body, baseline)
            state = evaluation["state"] if evaluation["state"] != "available" else "succeeded"
            category = "invalid response" if evaluation["state"] == "unavailable" else ""
            break
        if status in RETRYABLE and retries < MAX_RETRIES:
            wait = _retry_after(headers, clock(), deadline)
            if wait is not None:
                retries += 1
                sleep(wait)
                continue
        category = STATUS.get(status, "redirect refused" if 300 <= status < 400 else "provider error")
        state = "failed"
        break
    audit = {"plan": built.get("plan") if built else None, "baseline": built.get("baseline") if built else None,
             "input_digest": built.get("digest") if built else None, "provider": "Jev", "model": MODEL,
             "adapter": ADAPTER, "attempts": attempts, "retries": retries, "elapsed_s": round(clock() - start, 3),
             "questions": built.get("questions") if built else 0, "bytes": built.get("bytes") if built else 0,
             "state": state, "category": category, "usage": (evaluation or {}).get("usage") or {}}
    return {"state": state, "category": category, "attempts": attempts, "retries": retries, "audit": audit,
            "evaluation": evaluation}


def unavailable(baseline, reason=UNAVAILABLE):
    """Recorded when no evaluation could run: coverage shows `Evaluation unavailable`, never 0% or passing."""
    coverage = model.score(baseline, {}, available=False)
    coverage.update(label="Evaluation unavailable", reason=reason)
    return {"provider": "Jev", "origin": ORIGINS["jev"], "model": None, "rubric": RUBRIC, "adapter": ADAPTER,
            "answers": {}, "findings": {}, "state": "unavailable", "partial": True, "problems": [],
            "unevaluated": [c["id"] for c in baseline["criteria"] if not c["excluded"]], "coverage": coverage,
            "usage": {}, "reason": reason}


def preflight(cfg, acknowledgement=None):
    """None when a (future) dispatch may proceed, else why not. Live transport stays blocked regardless."""
    jev = (cfg or {}).get("jev") if isinstance((cfg or {}).get("jev"), dict) else {}
    if any(k in jev for k in ("key", "api_key", "token", "secret", "password")):
        return "Remove the Jev key from config: only a credential reference (an environment variable name) is allowed"
    if jev.get("enabled") is not True:
        return UNAVAILABLE
    name = jev.get("credential_env", CREDENTIAL_ENV)
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", name):
        return "Set jev.credential_env to an environment variable name"
    if not acknowledgement or acknowledgement.get("digest") != disclosure(cfg)["digest"]:
        return "Review and acknowledge the Jev data-sharing disclosure first"
    return None  # the default transport still refuses: JEV-03's transport amendment is not shipped


def disclosure(cfg=None):
    """What a hosted evaluation would send and where (shown before any first use)."""
    out = {"destination": ENDPOINT, "model": MODEL, "rubric": RUBRIC, "adapter": ADAPTER,
           "data": ["requirement wording (redacted)", "plan approach, steps, testing and verification excerpts"],
           "never": ["source code", "transcripts", "credentials", "absolute paths", "database keys or rows"],
           "retention": "Governed by your provider agreement; deleting local records cannot retract a sent payload.",
           "cost": "Reserved against the daily cap and the Jev subcap before dispatch."}
    out["digest"] = model.digest(out)
    return out


def validate_decisions(requirements, decisions, available=True):
    """Internal normalized adapter contract, not an assertion about live wire compatibility."""
    if not isinstance(decisions, list) or len(decisions) > MAX_QUESTIONS:
        raise ValueError("invalid or oversized Jev decisions")
    if len(json.dumps(decisions).encode("utf-8")) > MAX_BYTES:
        raise ValueError("Jev response exceeds size limit")
    valid_ids = {c["id"] for c in requirements["criteria"]}
    normalized, seen, invalid = {}, set(), False
    for answer in decisions:
        if not isinstance(answer, dict):
            invalid = True
            continue
        cid = answer.get("id")
        if not isinstance(cid, str) or cid not in valid_ids:
            invalid = True
            continue
        if cid in seen:
            normalized.pop(cid, None)
            invalid = True
            continue
        seen.add(cid)
        status, confidence = answer.get("status"), answer.get("confidence")
        if (not isinstance(status, str) or status not in model.COVERAGE or isinstance(confidence, bool)
                or not isinstance(confidence, (float, int)) or not math.isfinite(confidence)
                or not 0 <= confidence <= 1):
            invalid = True
            continue
        normalized[cid] = {"status": status, "confidence": confidence}
    return {"provider": "Jev", "rubric": "coverage-v1", "answers": normalized,
            "partial": invalid or len(normalized) < len(valid_ids),
            "coverage": model.score(requirements, {k: v["status"] for k, v in normalized.items()}, available)}
