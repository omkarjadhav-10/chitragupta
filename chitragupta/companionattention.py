"""FLOW-07 attention presentation: acknowledge and snooze, per profile. Presentation only.

`data/workflows/.attention.json` (written only through safety/workflowio, under its writer lock) holds, per profile,
the attention items you acknowledged or snoozed, keyed by the item's stable source ID together with the generation you
saw. Materially new evidence changes an item's generation and reopens it; a snooze ends at its expiry. Release
reminders, task overlays and job outcomes are required: hidden ones stay in the full list, labelled. Nothing here
touches a ticket record, a workflow, a release or test result, or a completion gate, and nothing is sent anywhere.
"""
import datetime as dt
import re

from . import common
from .safety import workflowio

SCHEMA = 1
SNOOZE = {"1h": dt.timedelta(hours=1), "1d": dt.timedelta(days=1), "1w": dt.timedelta(days=7)}
SNOOZE_LABEL = {"1h": "1 hour", "1d": "1 day", "1w": "1 week"}
REQUIRED = {"release", "workflow", "job"}
INITIAL = 3
KEEP = 500
_ID = re.compile(r"^(alert|release|workflow|job):[A-Za-z0-9_.:@-]{1,200}$")
_GEN = re.compile(r"^[a-f0-9]{8,64}$")


def path():
    return common.hp("data", "workflows", ".attention.json")


def _load():
    data = common.read_json(path(), {}) or {}
    if not isinstance(data, dict) or data.get("schema_version", SCHEMA) != SCHEMA \
            or not isinstance(data.get("profiles", {}), dict):
        return {"schema_version": SCHEMA, "profiles": {}}
    data.setdefault("schema_version", SCHEMA)
    data.setdefault("profiles", {})
    return data


def entries(profile_id):
    prof = _load()["profiles"].get(profile_id) or {}
    items = prof.get("items") if isinstance(prof, dict) else None
    return {k: v for k, v in (items or {}).items() if isinstance(v, dict)}


def present(items, profile_id, now=None):
    """Each item with `required` and `presentation` {state: active | acknowledged | snoozed, until, changed}. An entry
    whose generation differs (materially new evidence) or whose snooze expired leaves the item active."""
    now = now or common.now()
    seen = entries(profile_id)
    out = []
    for it in items:
        e = seen.get(it["id"])
        state, until = "active", None
        if e and e.get("generation") == it["generation"]:
            if e.get("state") == "acknowledged":
                state = "acknowledged"
            elif e.get("state") == "snoozed":
                expires = common.parse_ts(e.get("until"))
                if expires and expires > now:
                    state, until = "snoozed", e["until"]
        changed = bool(e) and e.get("generation") != it["generation"]
        label = {"active": "Changed since you %s it" % ("snoozed" if (e or {}).get("state") == "snoozed"
                                                         else "acknowledged") if changed else "",
                 "acknowledged": "Acknowledged; returns when its evidence changes",
                 "snoozed": "Snoozed until %s" % (until or "")}[state]
        out.append(dict(it, required=it["kind"] in REQUIRED,
                        presentation={"state": state, "until": until, "changed": changed, "label": label}))
    return out


def _prune(items, now):
    for k in [k for k, v in items.items() if v.get("state") == "snoozed"
              and (common.parse_ts(v.get("until")) or now) <= now - dt.timedelta(days=7)]:
        del items[k]
    if len(items) > KEEP:
        for k in sorted(items, key=lambda k: str(items[k].get("at") or ""))[:len(items) - KEEP]:
            del items[k]


def _write(profile_id, change):
    """One locked read-change-write of this profile's presentation entries (durable, atomic replace)."""
    now = common.now()
    with workflowio.lock():
        data = _load()
        prof = data["profiles"].setdefault(profile_id, {"items": {}})
        if not isinstance(prof.get("items"), dict):
            prof["items"] = {}
        change(prof["items"], now)
        _prune(prof["items"], now)
        workflowio.durable_json(path(), data)


def action(body):
    """attention_ack / attention_snooze / attention_unsnooze on a currently recorded item of your profile, at the
    generation you saw. -> {"attention": {...}, "notice"}."""
    from . import companionviews as views
    name = body.get("action")
    item, generation = body.get("item"), body.get("generation")
    if not isinstance(item, str) or not _ID.fullmatch(item):
        raise ValueError("invalid attention item")
    cfg = common.load_config()
    pid, _, _, _, confirmed = views.generations(cfg)
    if not confirmed:
        raise PermissionError("confirm your identity in setup first")
    if name != "attention_unsnooze":
        current = {it["id"]: it for it in views.attention_items(cfg)}
        it = current.get(item)
        if it is None:
            raise ValueError("This attention item is no longer recorded for your profile; reload")
        if not isinstance(generation, str) or not _GEN.fullmatch(generation) or generation != it["generation"]:
            raise ValueError("This attention item changed since you loaded it; reload and review it again")
    duration = body.get("duration")
    if name == "attention_snooze" and duration not in SNOOZE:
        raise ValueError("snooze for 1h, 1d or 1w")
    result = {}

    def change(items, now):
        if name == "attention_unsnooze":
            result["state"] = "active"
            items.pop(item, None)
            return
        entry = {"state": "acknowledged" if name == "attention_ack" else "snoozed", "generation": generation,
                 "at": now.isoformat(timespec="seconds"), "until": None}
        if name == "attention_snooze":
            entry["until"] = (now + SNOOZE[duration]).isoformat(timespec="seconds")
        items[item] = entry
        result.update(state=entry["state"], until=entry["until"])
    if name not in {"attention_ack", "attention_snooze", "attention_unsnooze"}:
        raise ValueError("unknown companion action")
    _write(pid, change)
    notice = {"acknowledged": "Acknowledged. It returns when its evidence changes; delivery and tests are unchanged.",
              "snoozed": "Snoozed for %s. Required items stay in the full list; delivery and tests are unchanged."
                         % SNOOZE_LABEL.get(duration, ""),
              "active": "Shown again."}[result["state"]]
    return {"attention": dict(result, item=item), "notice": notice}
