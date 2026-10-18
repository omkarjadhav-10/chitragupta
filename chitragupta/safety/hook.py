"""PreToolUse guard entry points (run as `<python> <launcher> safety internal <profile>` / `safety session`).

internal  inside Chitragupta's own `claude -p` calls. Fails closed: any error, unreadable input or unknown profile
          denies the call (and the hook is registered with onFailure: block, so a crash or timeout denies too).
session   registered by the /cg: skills. Does nothing unless a /cg: command is running in this session (the
          session's last prompt is a /cg: command, or UserPromptSubmit marked it); then the same checks apply and an
          error denies. Outside a /cg: command it returns at once: my own work is never blocked.
"""
import json
import os
import re
import sys

from .. import common

CG = re.compile(r"^\s*/cg:|<command-name>\s*/cg:")
TAIL_BYTES = 256 * 1024


def _deny(why, profile, data):
    from . import log, mcp
    log.record("tool", why, mcp.describe(data.get("tool_name"), data.get("tool_input")), profile, "safety." + profile)
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Chitragupta safety guard (read-only): " + why}}))
    sys.stdout.flush()
    sys.stderr.write("Chitragupta safety guard: " + why + "\n")
    return 2


def _read_input():
    raw = sys.stdin.buffer.read() or b"{}"
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("hook input is not an object")
    return data


def internal(profile):
    data = {}
    try:
        data = _read_input()
        from . import mcp
        why = mcp.reason(profile, data.get("tool_name"), data.get("tool_input"))
    except Exception as e:  # noqa: BLE001 — fail closed
        why = "guard error (%s: %s)" % (type(e).__name__, str(e)[:200])
    return _deny(why, profile, data) if why else 0


# --------------------------------------------------------------------------- /cg: activity

def mark_path(session_id):
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id or ""))[:80]
    return common.wl("cache", "cg-active", sid) if sid else None


def mark(session_id, prompt):
    """UserPromptSubmit: mark the session while a /cg: command runs; any other prompt clears it."""
    p = mark_path(session_id)
    if not p:
        return
    from . import paths
    try:
        if CG.search(prompt or ""):
            paths.write_text(p, common.now_iso() + "\n", caller="safety.mark")
        elif os.path.exists(p):
            paths.delete(p, caller="safety.mark")
    except Exception:  # noqa: BLE001
        pass


def clear(session_id):
    """Stop: the /cg: command is over."""
    p = mark_path(session_id)
    if p and os.path.exists(p):
        from . import paths
        try:
            paths.delete(p, caller="safety.clear")
        except Exception:  # noqa: BLE001
            pass


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return None


def last_prompt(transcript_path):
    """Text of the last real user prompt in a transcript (tool results and meta entries skipped), or None."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError, ValueError):
        return None
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
            continue
        t = _text((d.get("message") or {}).get("content"))
        if t is not None:
            return t
    return None


def cg_active(data):
    p = mark_path(data.get("session_id"))
    if p and os.path.exists(p):
        return True
    t = last_prompt(data.get("transcript_path"))
    return bool(t and CG.search(t))


def session():
    data = {}
    try:
        data = _read_input()
        if not cg_active(data):
            return 0
        from . import mcp
        why = mcp.reason("session", data.get("tool_name"), data.get("tool_input"))
    except Exception as e:  # noqa: BLE001 — fail closed once a /cg: command may be running
        why = "guard error (%s: %s)" % (type(e).__name__, str(e)[:200])
    return _deny(why, "session", data) if why else 0


def main(argv):
    try:
        if argv[:1] == ["internal"] and len(argv) == 2:
            return internal(argv[1])
        if argv[:1] == ["session"]:
            return session()
        if argv[:1] == ["selftest"]:
            from . import selftest
            res = selftest.run()
            print(json.dumps(res, indent=1))
            return 0 if res["ok"] else 1
        return _deny("unknown guard mode %r" % argv, "?", {})
    except Exception as e:  # noqa: BLE001
        return _deny("guard error (%s)" % type(e).__name__, "?", {})
