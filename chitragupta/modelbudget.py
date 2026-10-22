"""Model spend: a per-day ledger of every internal model call, and the daily cap that stops the optional ones.

health.json "model_usage": {day: {"calls", "in", "out", "usd", "by_purpose": {purpose: {"calls", "in", "out", "usd"}},
"skipped": {purpose: n}}}, kept 30 days next to "cost_by_day".

extract.call_claude notes each call here (note()); the agent moves the notes into its health (drain()) before it
checks the cap and when it saves health.json, so a call made anywhere in a run is counted once. MCP jobs record
straight into the health they are given (record(), purpose "mcp").

The cap (`budget.model_daily_usd`, default $3) only stops the purposes in GATED: supersession and ticket briefs, which
can wait a day. Extraction, the day summary, curation and the rest are never stopped by it. Briefs also stop after
`brief.daily_cap` (default 8) a day.
"""
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
