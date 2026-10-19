"""PreToolUse guard entry points (run as `<python> <launcher> safety internal <profile>` / `safety session`).

internal  inside Chitragupta's own `claude -p` calls. Fails closed: any error, unreadable input or unknown profile
          denies the call (and the hook is registered with onFailure: block, so a crash or timeout denies too).
session   registered by the /cg: skills (skill hooks stay registered for the rest of the session). It acts only for
          the prompt a /cg: command expanded: the synchronous UserPromptExpansion hook (`hook cg-expand`, matched to
          the /cg: command names) records that prompt's `prompt_id` in a per-session mark, and every PreToolUse input
          carries the `prompt_id` of the prompt being processed. Same id → the /cg: command is running: the session
          checks apply and an error denies. Any other prompt, no mark, or no `prompt_id` in the input → it returns at
          once, so my own work is never blocked. A missing `prompt_id` fails toward not guarding; the skill's
          `allowed-tools` stays the second fence.
"""
import json
import os
import re
import sys

from .. import common

# /cg: command names as UserPromptExpansion reports them ("cg:today"), or the typed prompt ("/cg:today …").
CG_NAME = re.compile(r"^/?cg:[A-Za-z0-9_-]+$")
CG_PROMPT = re.compile(r"^\s*/cg:[A-Za-z0-9_-]+(?:\s|$)")


def _deny(why, profile, data, category=""):
    from . import log, mcp
    log.record("tool", why, mcp.describe(data.get("tool_name"), data.get("tool_input")), profile, "safety." + profile,
               category=category)
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


# --------------------------------------------------------------------------- /cg: activity (keyed on prompt_id)

def mark_path(session_id):
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id or ""))[:80]
    return common.wl("cache", "cg-active", sid) if sid else None


def is_cg_command(data):
    """True for a UserPromptExpansion input of one of this plugin's /cg: commands."""
    name = str(data.get("command_name") or "").strip()
    return bool(CG_NAME.match(name) or CG_PROMPT.match(str(data.get("prompt") or "")))


def _read_mark(p):
    m = common.read_json(p, {}) if p else {}
    return m if isinstance(m, dict) else {}


def expand(data):
    """UserPromptExpansion (synchronous, before the prompt reaches Claude): record which prompt is a /cg: command.
    Never raises. Without a prompt_id there is nothing to key on, so any earlier mark is removed instead."""
    try:
        if not isinstance(data, dict) or not is_cg_command(data):
            return
        p = mark_path(data.get("session_id"))
        if not p:
            return
        from . import paths
        pid = str(data.get("prompt_id") or "")
        if pid:
            paths.write_text(p, json.dumps({"prompt_id": pid, "session_id": str(data.get("session_id")),
                                            "at": common.now_iso()}) + "\n", caller="safety.expand")
        elif os.path.exists(p):
            paths.delete(p, caller="safety.expand")
    except Exception:  # noqa: BLE001 — a hook must never fail
        pass


def clear(session_id, prompt_id=None):
    """Stop: tidy up the mark of the prompt that just finished. Only that prompt's mark: a late (async) Stop of an
    earlier turn must not remove the mark of a /cg: command that started since. Harmless cleanup — a stale mark never
    matches another prompt anyway."""
    p = mark_path(session_id)
    if not (p and os.path.exists(p)):
        return
    try:
        if not prompt_id or _read_mark(p).get("prompt_id") != str(prompt_id):
            return
        from . import paths
        paths.delete(p, caller="safety.clear")
    except Exception:  # noqa: BLE001
        pass


def cg_active(data):
    """True only while the prompt being processed is the /cg: command the expansion hook recorded."""
    pid = data.get("prompt_id")
    if not pid:
        return False  # no prompt_id: fail toward not guarding (allowed-tools is the second fence)
    p = mark_path(data.get("session_id"))
    if not (p and os.path.exists(p)):
        return False
    return _read_mark(p).get("prompt_id") == str(pid)


def session():
    try:
        data = _read_input()
        if not cg_active(data):
            return 0
    except Exception:  # noqa: BLE001 — not confirmed as a /cg: command: never block my own work
        return 0
    try:
        from . import mcp
        why = mcp.reason("session", data.get("tool_name"), data.get("tool_input"), server_info=data.get("mcp_server"))
        cat = mcp.OTHER_PLUGIN if why == mcp.OTHER_PLUGIN_REASON else ""
    except Exception as e:  # noqa: BLE001 — fail closed once a /cg: command is confirmed running
        why, cat = "guard error (%s: %s)" % (type(e).__name__, str(e)[:200]), ""
    return _deny(why, "session", data, cat) if why else 0


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
