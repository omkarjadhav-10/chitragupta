"""Model spend: a per-day ledger of every internal model call, and the daily cap that stops the optional ones.

health.json "model_usage": {day: {"calls", "in", "out", "usd", "by_purpose": {purpose: {"calls", "in", "out", "usd"}},
"skipped": {purpose: n}}}, kept 30 days next to "cost_by_day".

extract.call_claude notes each call here (note()); the agent moves the notes into its health (drain()) before it
checks the cap and when it saves health.json, so a call made anywhere in a run is counted once. MCP jobs record
straight into the health they are given (record(), purpose "mcp").

The cap (`budget.model_daily_usd`, default $3) only stops the purposes in GATED: supersession and ticket briefs, which
can wait a day. Extraction, the day summary, curation and the rest are never stopped by it. Briefs also stop after
`brief.daily_cap` (default 8) a day.

Companion reservations (COMPANION purposes) are reserved before dispatch in a ledger at data/workflows/.budget.json,
under the workflow writer lock (an OS lock shared by every process), so a reservation and the job/checkpoint that
needs it publish in one short transaction. reserve -> settle(actual) | release (known not sent) | mark_unknown
(ambiguous: still counted). The cap check is today's health spend for every other purpose plus the ledger's
reserved/unknown estimates and settled actuals; health's own companion purposes are left out, so nothing counts twice.
"""
import math
import os
import time

from . import common
from . import configstore

GATED = ("supersede", "brief")
DEFAULT_DAILY_USD = 3.0
DEFAULT_BRIEF_CAP = 8
KEEP_DAYS = 30

_PENDING = []  # [(day, purpose, in, out, usd)] noted by call_claude, not yet in a health dict
_LAST = {}  # purpose -> (in, out, usd) of its most recent call, for the record of what that call made (a brief)


def usage_tokens(data):
    """(input, output) tokens from a claude JSON result's "usage" (cache reads and writes count as input)."""
    u = (data or {}).get("usage") or {}
    if not isinstance(u, dict):
        return 0, 0
    tin = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return tin, int(u.get("output_tokens") or 0)


def note(purpose, tokens_in, tokens_out, usd, cfg):
    """One finished model call, held until the agent's health is at hand (drain)."""
    _PENDING.append((common.workday(common.now(), cfg), purpose, int(tokens_in or 0), int(tokens_out or 0),
                     float(usd or 0)))
    _LAST[purpose] = (int(tokens_in or 0), int(tokens_out or 0), float(usd or 0))


def last_call(purpose):
    """(tokens in, tokens out, usd) of the most recent call for `purpose` in this process, or (0, 0, 0.0)."""
    return _LAST.get(purpose, (0, 0, 0.0))


def drain(health, cfg=None):
    """Move the noted calls into `health`."""
    while _PENDING:
        day, purpose, tin, tout, usd = _PENDING.pop(0)
        record(health, day, purpose, tin, tout, usd)


def _add(d, tin, tout, usd):
    d["calls"] = int(d.get("calls") or 0) + 1
    d["in"] = int(d.get("in") or 0) + tin
    d["out"] = int(d.get("out") or 0) + tout
    d["usd"] = round(float(d.get("usd") or 0) + usd, 4)


def record(health, day, purpose, tokens_in, tokens_out, usd):
    led = health.setdefault("model_usage", {})
    d = led.setdefault(day, {})
    tin, tout, usd = int(tokens_in or 0), int(tokens_out or 0), float(usd or 0)
    _add(d, tin, tout, usd)
    _add(d.setdefault("by_purpose", {}).setdefault(purpose, {}), tin, tout, usd)
    for k in sorted(led)[:-KEEP_DAYS]:
        del led[k]


def daily_usd(cfg):
    try:
        return float(configstore.get(cfg or {}, "budget.model_daily_usd", DEFAULT_DAILY_USD))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_USD


def brief_cap(cfg):
    try:
        return int(configstore.get(cfg or {}, "brief.daily_cap", DEFAULT_BRIEF_CAP))
    except (TypeError, ValueError):
        return DEFAULT_BRIEF_CAP


def _today(cfg):
    return common.workday(common.now(), cfg or {})


def spent(health, cfg, day=None):
    return float(((health.get("model_usage") or {}).get(day or _today(cfg)) or {}).get("usd") or 0)


def budget_left(health, cfg, purpose=None, day=None):
    """Dollars left under today's cap (never below 0)."""
    drain(health, cfg)
    return round(max(0.0, daily_usd(cfg) - spent(health, cfg, day)), 4)


def over_budget(health, cfg, purpose, day=None):
    """True when a gated purpose must wait for tomorrow (today's spend reached the cap, or the brief count did); the
    refusal is logged and counted under "skipped". Ungated purposes (extraction and the rest) are always False."""
    if purpose not in GATED:
        return False
    day = day or _today(cfg)
    drain(health, cfg)
    usd, cap = spent(health, cfg, day), daily_usd(cfg)
    why = ""
    if usd >= cap:
        why = "today's model spend $%.2f reached the $%.2f cap (budget.model_daily_usd)" % (usd, cap)
    elif purpose == "brief":
        n = int(((((health.get("model_usage") or {}).get(day) or {}).get("by_purpose") or {}).get("brief") or {})
                .get("calls") or 0)
        if n >= brief_cap(cfg):
            why = "%d briefs today reached brief.daily_cap %d" % (n, brief_cap(cfg))
    if not why:
        return False
    d = health.setdefault("model_usage", {}).setdefault(day, {})
    sk = d.setdefault("skipped", {})
    sk[purpose] = int(sk.get(purpose) or 0) + 1
    common.log("agent", "model budget: %s skipped, %s" % (purpose, why))
    return True


def summary(health, cfg, day=None):
    """Today's spend against the cap, for Health, doctor and the dashboard."""
    day = day or _today(cfg)
    d = (health.get("model_usage") or {}).get(day) or {}
    usd, cap = float(d.get("usd") or 0), daily_usd(cfg)
    return {"day": day, "usd": round(usd, 4), "cap": cap, "over": usd >= cap, "calls": int(d.get("calls") or 0),
            "in": int(d.get("in") or 0), "out": int(d.get("out") or 0), "by_purpose": d.get("by_purpose") or {},
            "skipped": d.get("skipped") or {}, "brief_cap": brief_cap(cfg), "gated": list(GATED)}


# --------------------------------------------------------------------------- companion reservations

COMPANION = ("companion_plan", "companion_improve", "companion_continuation", "companion_verification", "jev")
JEV_DAILY_USD = 0.50
JEV_TASK_USD = 0.10
MAX_PAID_JOBS = 2
RESERVATION_TTL_S = 1800
COUNTED = {"reserved", "unknown"}


class Refused(ValueError):
    """A paid action that cannot be reserved now; the reason names the limit and the setting."""


def _ledger_path():
    return common.hp("data", "workflows", ".budget.json")


def _ledger():
    led = common.read_json(_ledger_path(), {}) or {}
    entries = led.get("entries") if isinstance(led.get("entries"), dict) else {}
    return {"schema_version": 1, "entries": entries}


def _write(led):
    from .safety import workflowio
    keep = set(sorted({e.get("day") or "" for e in led["entries"].values()})[-KEEP_DAYS:])
    led["entries"] = {k: e for k, e in led["entries"].items() if (e.get("day") or "") in keep or
                      e.get("state") in COUNTED}
    workflowio.durable_json(_ledger_path(), led)


def _usd(value):
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def call_ceiling(cfg, purpose):
    """(usd, "") per call, or (None, why the paid action is off). No ceiling configured -> disabled."""
    key = "jev.call_usd" if purpose == "jev" else "budget.companion_call_usd"
    v = _usd(configstore.get(cfg or {}, key))
    if v is None:
        return None, "Paid action off: set %s, a per-call spend ceiling, to enable it" % key
    if v > daily_usd(cfg):
        return None, "Paid action off: %s ($%.2f) is above the daily cap" % (key, v)
    return v, ""


def jev_caps(cfg):
    """(daily, per task per day): the Jev subcap, never above its proposed product defaults."""
    day = _usd(configstore.get(cfg or {}, "jev.daily_usd")) or JEV_DAILY_USD
    task = _usd(configstore.get(cfg or {}, "jev.task_daily_usd")) or JEV_TASK_USD
    return min(day, JEV_DAILY_USD), min(task, JEV_TASK_USD, day)


def _committed(entries, day, purpose=None, task=None):
    total = 0.0
    for e in entries.values():
        if e.get("day") != day or (purpose and e.get("purpose") != purpose) or (task and e.get("task") != task):
            continue
        if e.get("state") in COUNTED:
            total += float(e.get("usd") or 0)
        elif e.get("state") == "settled":
            total += float(e.get("actual") or 0)
    return round(total, 6)


def other_spend(cfg, day=None):
    """Today's recorded health spend for every non-companion purpose (the ledger owns the companion ones)."""
    day = day or _today(cfg)
    d = ((common.read_json(common.wl("health.json"), {}) or {}).get("model_usage") or {}).get(day) or {}
    own = sum(float(((d.get("by_purpose") or {}).get(p) or {}).get("usd") or 0) for p in COMPANION)
    return round(max(0.0, float(d.get("usd") or 0) - own), 6)


def _reserve(purpose, usd, cfg, task=None, job=None):
    """reserve() with the workflow writer lock already held (enqueue transactions)."""
    if purpose not in COMPANION:
        raise ValueError("unknown companion budget purpose")
    usd = _usd(usd)
    if usd is None:
        raise Refused("a reservation needs a positive finite estimate")
    day = _today(cfg)
    led = _ledger()
    entries = led["entries"]
    cap = daily_usd(cfg)
    used = other_spend(cfg, day) + _committed(entries, day)
    if used + usd > cap + 1e-9:
        raise Refused("Budget: $%.2f used or reserved of today's $%.2f cap (budget.model_daily_usd); this needs "
                      "$%.2f" % (used, cap, usd))
    if purpose == "jev":
        daily, per_task = jev_caps(cfg)
        if _committed(entries, day, "jev") + usd > daily + 1e-9:
            raise Refused("Budget: the Jev daily subcap $%.2f is reached (jev.daily_usd)" % daily)
        if _committed(entries, day, "jev", task) + usd > per_task + 1e-9:
            raise Refused("Budget: the Jev per-task subcap $%.2f is reached (jev.task_daily_usd)" % per_task)
    running = sum(1 for e in entries.values() if e.get("state") == "reserved" and e.get("job"))
    try:
        limit = min(int(configstore.get(cfg or {}, "companion.max_paid_jobs", MAX_PAID_JOBS)), MAX_PAID_JOBS)
    except (TypeError, ValueError):
        limit = MAX_PAID_JOBS
    if job and running >= limit:
        raise Refused("Busy: %d paid jobs are already running in this profile; wait or cancel one" % running)
    rid = "r_%x%s" % (time.time_ns(), os.urandom(6).hex())
    entries[rid] = {"id": rid, "purpose": purpose, "task": task, "job": job, "day": day, "usd": round(usd, 6),
                    "actual": None, "state": "reserved", "at": common.now_iso(),
                    "expires_epoch": time.time() + RESERVATION_TTL_S}
    _write(led)
    return dict(entries[rid])


def _change(rid, state, actual=None, allow=("reserved",)):
    led = _ledger()
    e = led["entries"].get(rid)
    if not e:
        raise ValueError("unknown reservation")
    if e["state"] == state:
        return dict(e)  # idempotent
    if e["state"] not in allow:
        raise ValueError("reservation is %s; it cannot become %s" % (e["state"], state))
    e.update(state=state, changed_at=common.now_iso())
    if actual is not None:
        e["actual"] = round(max(0.0, float(actual)), 6)
    _write(led)
    return dict(e)


def _settle(rid, actual_usd):
    return _change(rid, "settled", _usd(actual_usd) or 0.0, allow=("reserved", "unknown"))


def _release(rid):
    """Known not sent: frees the estimate. An unknown outcome is never released as known-free."""
    return _change(rid, "released", allow=("reserved",))


def _mark_unknown(rid):
    return _change(rid, "unknown", allow=("reserved",))


def reservation(rid):
    return dict(_ledger()["entries"].get(rid) or {})


def _locked(fn, *args):
    from .safety import workflowio
    with workflowio.lock():
        return fn(*args)


def reserve(purpose, usd, cfg, task=None, job=None):
    """Atomically reserve `usd` for one paid call, or raise Refused naming the cap that stops it."""
    return _locked(_reserve, purpose, usd, cfg, task, job)


def settle(rid, actual_usd):
    return _locked(_settle, rid, actual_usd)


def release(rid):
    return _locked(_release, rid)


def mark_unknown(rid):
    return _locked(_mark_unknown, rid)


def companion_summary(cfg):
    """Read-only: today's cap, other spend, the companion ledger by purpose and state, Jev subcaps, ceilings."""
    day = _today(cfg)
    entries = _ledger()["entries"]
    cap, other = daily_usd(cfg), other_spend(cfg, day)
    committed = _committed(entries, day)
    rows = {}
    for e in entries.values():
        if e.get("day") != day:
            continue
        r = rows.setdefault(e.get("purpose"), {"reserved": 0.0, "unknown": 0.0, "settled": 0.0, "released": 0})
        if e.get("state") in COUNTED:
            r[e["state"]] = round(r[e["state"]] + float(e.get("usd") or 0), 6)
        elif e.get("state") == "settled":
            r["settled"] = round(r["settled"] + float(e.get("actual") or 0), 6)
        else:
            r["released"] += 1
    daily, per_task = jev_caps(cfg)
    plan, why = call_ceiling(cfg, "companion_plan")
    jcall, jwhy = call_ceiling(cfg, "jev")
    return {"day": day, "cap": cap, "other_spend": other, "companion": committed,
            "remaining": round(max(0.0, cap - other - committed), 4), "by_purpose": rows,
            "jev": {"daily": daily, "per_task": per_task, "used": _committed(entries, day, "jev"),
                    "call_ceiling": jcall, "reason": jwhy},
            "plan_call_ceiling": plan, "plan_reason": why,
            "unknown": sorted(k for k, e in entries.items() if e.get("state") == "unknown")}
