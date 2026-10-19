"""Safety status for /cg:doctor and the dashboard's Health page.

"enforced" needs all of: the canned guard decisions hold (selftest), every /cg: skill registers the session guard
(without onFailure: block: a launcher that can't start must not block the session; the guard itself fails closed once a
/cg: command is confirmed running), and Claude Code is new enough for `onFailure: block` on the internal calls' guard
(2.1.295).
"""
import glob
import os
import re

from .. import common
from . import log, selftest

MIN_CLAUDE = (2, 1, 295)
SESSION_HOOK = re.compile(r'args:\s*\[[^\]]*"safety",\s*"session"\]')


def skills_dir():
    d = os.path.join(os.path.dirname(common.PACKAGE_DIR), "skills")
    return d if os.path.isdir(d) else None


def unguarded_skills():
    """/cg: skills without the session guard; None when this copy has no skills folder (the runtime copy)."""
    d = skills_dir()
    if not d:
        return None
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*", "SKILL.md"))):
        if not SESSION_HOOK.search(common.read_text(p)):
            out.append(os.path.basename(os.path.dirname(p)))
    return out


def claude_version(detail):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", detail or "")
    return tuple(int(x) for x in m.groups()) if m else None


def database_read_only(cfg):
    """None when no database MCP job is on; else True / False for mcp.database.read_only_login."""
    db = (cfg.get("mcp") or {}).get("database") or {}
    if not (db.get("enabled") and db.get("server")):
        return None
    return db.get("read_only_login") is True


def status(cfg=None, claude_detail=None):
    cfg = cfg if cfg is not None else common.load_config()
    problems = []
    st = selftest.run()
    if not st["ok"]:
        problems += st["failed"]
    sk = unguarded_skills()
    if sk:
        problems.append("/cg: skills without the session guard: %s" % ", ".join(sk))
    if claude_detail is not None:
        v = claude_version(claude_detail)
        if not v or v < MIN_CLAUDE:
            problems.append("Claude Code %s is older than %s (needed for the internal calls' fail-closed guard)"
                            % (".".join(map(str, v)) if v else "?", ".".join(map(str, MIN_CLAUDE))))
    s = log.summary(days=7)
    day = log.summary(days=1)
    return {"enforced": not problems, "problems": problems, "checks": st.get("checks", 0),
            "skills_checked": sk is not None, "blocks_7d": s["recent"], "blocks_24h": day["recent"],
            "by_layer": s["by_layer"], "last": s["last"], "log": log.path(), "skipped_7d": s.get("other_plugin", 0),
            "database_read_only": database_read_only(cfg),
            "fetch": (cfg.get("git") or {}).get("allow_fetch", True) is not False}


def doctor_rows(cfg=None, claude_detail=None):
    s = status(cfg, claude_detail)
    rows = [{"name": "Safety", "ok": s["enforced"],
             "detail": ("enforced (%d guard checks%s; git fetch %s)" % (
                 s["checks"], ", every /cg: skill guarded" if s["skills_checked"] else "", "on" if s["fetch"] else "off"))
             if s["enforced"] else "NOT enforced: " + "; ".join(s["problems"])}]
    if s["blocks_24h"]:
        rows.append({"name": "security log", "ok": False, "detail": "%d blocked action(s) in the last 24 h (%s) — see %s"
                     % (s["blocks_24h"], ", ".join("%s %d" % kv for kv in sorted(s["by_layer"].items())), s["log"])})
    else:
        rows.append({"name": "security log", "ok": True, "detail": "no blocked actions in the last 24 h (%d in 7 days)%s"
                     % (s["blocks_7d"], "; %d other-plugin tool call(s) skipped during /cg: commands (not violations)"
                        % s["skipped_7d"] if s["skipped_7d"] else "")})
    if s["database_read_only"] is False:
        rows.append({"name": "database login", "ok": False, "detail": "the database MCP's read-only login isn't confirmed: "
                     "give it a read-only login, then set mcp.database.read_only_login to true"})
    return rows
