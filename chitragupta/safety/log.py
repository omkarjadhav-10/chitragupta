"""Violation log: %USERPROFILE%\\.chitragupta\\logs\\security.log, one JSON line per blocked action.

Read by the dashboard's Health page and /cg:doctor. Never raises.
`at` is local time with its UTC offset ("2026-10-16T10:00:00+05:30"); lines written before that have no offset and
are read as local time.
"""
import datetime as _dt
import json
import os
import re
import sys
import time

from .. import common

MAX_BYTES = 1_000_000
# Categories that are recorded but are not violations: left out of summary()'s counts (Health, doctor, get_alerts).
#   other-plugin  another plugin's MCP tool called during a /cg: command: denied and skipped (safety/mcp.py, Policy A)
NOT_VIOLATIONS = frozenset({"other-plugin"})


class Blocked(PermissionError):
    """A side effect outside the allowlist. Never retried. A PermissionError, so code that already tolerates a failed
    write (except OSError) carries on; the block itself is in security.log either way."""

    def __init__(self, layer, reason, what="", purpose="", caller=""):
        super().__init__("%s: %s" % (layer, reason))
        self.layer, self.reason, self.what, self.purpose, self.caller = layer, reason, what, purpose, caller


def path():
    return os.path.join(common.user_dir(), "logs", "security.log")


def _stack_caller():
    """The module that asked for the blocked action: the first frame outside the safety package (else the first
    frame outside this module), by module name like safety.commands' caller."""
    f = sys._getframe(2) if hasattr(sys, "_getframe") else None
    first = ""
    while f is not None:
        name = f.f_globals.get("__name__", "") or ""
        if name != __name__:
            first = first or name
            if not name.startswith("chitragupta.safety"):
                return name
        f = f.f_back
    return first


def _now():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _epoch(at):
    """Seconds since the epoch for an `at` value, with or without a UTC offset (none = local time); None if unreadable."""
    try:
        d = _dt.datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.astimezone()  # naive = local time
    return d.timestamp()


def record(layer, reason, what="", purpose="", caller="", category=""):
    """Append one block. `what` is the blocked command / path / statement / tool (truncated). `category` (only written
    when set) marks a block that is not a violation (NOT_VIOLATIONS). No caller given: taken from the stack."""
    p = path()
    try:
        caller = caller or _stack_caller()
    except Exception:  # noqa: BLE001 — never raises
        caller = ""
    entry = {"at": _now(), "layer": layer, "caller": caller or "",
             "purpose": purpose or "", "what": common.truncate(str(what), 600), "reason": reason}
    if category:
        entry["category"] = category
    line = json.dumps(entry, ensure_ascii=False)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if os.path.exists(p) and os.path.getsize(p) > MAX_BYTES:
            os.replace(p, p + ".1")
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except OSError:
        pass


def block(layer, reason, what="", purpose="", caller=""):
    """Record and raise."""
    record(layer, reason, what, purpose, caller)
    raise Blocked(layer, reason, what, purpose, caller)


def entries(limit=200):
    """Newest last: [{at, layer, caller, purpose, what, reason}]."""
    out = []
    for p in (path() + ".1", path()):
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return out[-limit:]


_OFFSET = re.compile(r"[+-]\d\d:?\d\d$|Z$")
_LEGACY_OTHER = re.compile(r"^server plugin_\S+ is not on the allowlist$")


def category(e):
    """The line's category. Lines written before categories existed (no UTC offset in `at`) are read as other-plugin
    when they are the session guard's "server plugin_… is not on the allowlist" refusal of a tool: that is how those
    versions logged another plugin's tool during a /cg: command. Lines with an offset are taken as written."""
    c = e.get("category")
    if c or _OFFSET.search(str(e.get("at") or "")):
        return c or ""
    if e.get("layer") == "tool" and e.get("caller") == "safety.session" and _LEGACY_OTHER.match(str(e.get("reason") or "")):
        return "other-plugin"
    return ""


def is_violation(e):
    return category(e) not in NOT_VIOLATIONS


def summary(days=7):
    """{total, recent (last `days`), by_layer, last, other_plugin, other_plugin_by_layer} for Health and doctor.
    Counts violations only, and `by_layer` breaks down the same `recent` window; `other_plugin` is the number of other
    plugins' tool calls skipped during /cg: commands in that window."""
    every = entries(10_000)
    es = [e for e in every if is_violation(e)]
    cutoff = time.time() - days * 86400

    def is_recent(e):
        t = _epoch(e.get("at"))
        return t is not None and t >= cutoff

    def layers(rows):
        by = {}
        for e in rows:
            by[e.get("layer") or "?"] = by.get(e.get("layer") or "?", 0) + 1
        return by
    recent = [e for e in es if is_recent(e)]
    skipped = [e for e in every if category(e) == "other-plugin" and is_recent(e)]
    return {"total": len(es), "recent": len(recent), "days": days, "by_layer": layers(recent), "last": es[-5:],
            "other_plugin": len(skipped), "other_plugin_by_layer": layers(skipped)}
