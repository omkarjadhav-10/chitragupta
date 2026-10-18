"""Violation log: %USERPROFILE%\\.chitragupta\\logs\\security.log, one JSON line per blocked action.

Read by the dashboard's Health page and /cg:doctor. Never raises.
"""
import json
import os
import time

from .. import common

MAX_BYTES = 1_000_000


class Blocked(PermissionError):
    """A side effect outside the allowlist. Never retried. A PermissionError, so code that already tolerates a failed
    write (except OSError) carries on; the block itself is in security.log either way."""

    def __init__(self, layer, reason, what="", purpose="", caller=""):
        super().__init__("%s: %s" % (layer, reason))
        self.layer, self.reason, self.what, self.purpose, self.caller = layer, reason, what, purpose, caller


def path():
    return os.path.join(common.user_dir(), "logs", "security.log")


def record(layer, reason, what="", purpose="", caller=""):
    """Append one violation. `what` is the blocked command / path / statement / tool (truncated)."""
    p = path()
    line = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "layer": layer, "caller": caller or "",
                       "purpose": purpose or "", "what": common.truncate(str(what), 600), "reason": reason},
                      ensure_ascii=False)
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


def summary(days=7):
    """{total, recent (last `days`), by_layer, last} for Health and doctor."""
    es = entries(10_000)
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
    recent = [e for e in es if (e.get("at") or "") >= cutoff]
    by = {}
    for e in recent:
        by[e.get("layer") or "?"] = by.get(e.get("layer") or "?", 0) + 1
    return {"total": len(es), "recent": len(recent), "days": days, "by_layer": by, "last": es[-5:]}
