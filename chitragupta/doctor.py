"""Diagnose a Chitragupta install. Never repairs anything; each failing row names the fix.

    python -m chitragupta doctor [--json] [--fast]

Rows are {name, ok, detail}; exit 1 when any row fails. --fast uses the cached MCP status instead of running
`claude mcp list`. Slack, Jira and the database MCP are optional: "missing" is reported but is not a failure;
a configured server that is signed out or offline is.
"""
import json
import os
import sys
import time

from . import __version__
from . import common
from . import plugin

HOOK_WINDOW = 24 * 3600
TICK_MAX_AGE = 3600
NEW_SETUP_GRACE = 15 * 60
ERROR_WINDOW = 24 * 3600


def _row(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def _age(ts):
    d = common.parse_ts(ts) if isinstance(ts, str) else ts
    if not d:
        return None
    return (common.now() - d).total_seconds()


def _fmt_age(sec):
    if sec is None:
        return "never"
    if sec < 90:
        return "%ds ago" % sec
    if sec < 5400:
        return "%dm ago" % (sec // 60)
    if sec < 172800:
        return "%.1fh ago" % (sec / 3600)
    return "%dd ago" % (sec // 86400)


def newest_mtime(root, depth=2, suffix=".jsonl"):
    best = 0.0
    if not os.path.isdir(root):
        return 0.0
    base = root.rstrip("\\/").count(os.sep)
    for dp, dns, fns in os.walk(root):
        if dp.count(os.sep) - base >= depth:
            dns[:] = []
        for fn in fns:
            if fn.endswith(suffix):
                try:
                    best = max(best, os.path.getmtime(os.path.join(dp, fn)))
                except OSError:
                    pass
    return best


def _new_setup():
    try:
        return time.time() - os.path.getctime(common.wl("config.json")) < NEW_SETUP_GRACE
    except OSError:
        return False


def hooks_row():
    """Raw events in the last 24 h, unless Claude Code itself wasn't used in that time."""
    last_raw = newest_mtime(common.wl("raw"), depth=3)
    now = time.time()
    if last_raw and now - last_raw < HOOK_WINDOW:
        return _row("hooks", True, "last event %s" % _fmt_age(now - last_raw))
    last_session = newest_mtime(os.path.join(plugin.claude_dir(), "projects"), depth=2)
    if not last_session or now - last_session > HOOK_WINDOW or (last_raw and last_session <= last_raw):
        return _row("hooks", True, "no Claude Code sessions to capture in the last 24 h" if last_raw else
                    "no events yet: they appear after the next Claude Code session")
    return _row("hooks", False, "Claude Code sessions ran %s but no hook event since %s: is the plugin enabled "
                "(/plugin) and set up? (sessions in excluded or .worklog-ignore folders are never captured)"
                % (_fmt_age(now - last_session), _fmt_age(now - last_raw) if last_raw else "setup"))


def tick_row(health, task_expected=True):
    a = _age(health.get("last_tick_ok"))
    if a is not None and a <= TICK_MAX_AGE:
        return _row("last tick", True, _fmt_age(a))
    if a is None and (_new_setup() or not task_expected):
        return _row("last tick", True, "no background run yet (the task runs every 5 minutes)"
                    if task_expected else "no background run yet (no task registered)")
    last_any = _age(health.get("last_tick"))
    detail = "last successful background run %s" % _fmt_age(a)
    if last_any is not None and (a is None or last_any < a):
        detail += "; last attempt %s (%s)" % (_fmt_age(last_any), (health.get("last_error") or "")[:160])
    return _row("last tick", False, detail + " (see the task row; `setup apply` re-registers it)")


def push_row(cfg, health):
    if not (cfg.get("github_remote") or "").strip():
        return _row("last push", True, "no GitHub remote configured (local only)")
    if health.get("privacy") == "public":
        return _row("last push", False, "the GitHub repo is public, so pushes are blocked")
    pf = health.get("push_failing_since")
    if pf:
        return _row("last push", False, "pushes failing since %s" % pf)
    a = _age(health.get("last_push"))
    return _row("last push", True, _fmt_age(a) if a is not None else "nothing pushed yet")


def errors_row(health):
    issues = []
    le = health.get("last_error") or ""
    a = _age(le.split(" ", 1)[0]) if le else None
    if a is not None and a < ERROR_WINDOW:
        issues.append("agent: %s" % le[:200])
    for name in ("hook-errors", "maintenance"):
        p = common.wl("logs", name + ".log")
        try:
            if time.time() - os.path.getmtime(p) < ERROR_WINDOW:
                lines = [ln for ln in common.read_text(p).splitlines() if ln.strip()]
                last = lines[-1] if lines else ""
                if name == "hook-errors" or "failed" in last:
                    issues.append("%s: %s" % (name, last[:200]))
        except OSError:
            pass
    return _row("recent errors", not issues, "; ".join(issues) or "none in the last 24 h")


def slack_row(r, cfg):
    """Slack from the engine's own view (slack.state: ready | off | not_connected) plus the server's live status."""
    from . import slack
    state = slack.state(cfg)
    status = r.get("status") or "unknown"
    if state == "off":
        return _row("Slack", True, slack.OFF_NOTE)
    if status in ("signed out", "offline", "not listed"):
        return _row("Slack", False, "%s: %s (sign in once: `setup add-slack` prints the steps)" % (status, r.get("detail")))
    if state == "not_connected":
        hint = "run `setup slack-user`" if status in ("connected", "configured") else "run `setup add-slack`"
        return _row("Slack", True, "%s (%s)" % (slack.NOT_CONNECTED_NOTE, hint))
    return _row("Slack", True, "%s: %s" % (status if status != "missing" else "ready", r.get("detail") or ""))


def mcp_rows(st, cfg=None):
    cfg = cfg if cfg is not None else common.load_config()
    if not st:
        return [_row(label, True, "not checked (run doctor without --fast)") for label in ("Slack", "Jira", "database MCP")]
    rows = [slack_row(st.get("slack") or {}, cfg)]
    for key, label in (("jira", "Jira"), ("database", "database MCP")):
        r = st.get(key) or {}
        status = r.get("status") or "unknown"
        if status in ("connected", "configured"):
            ok = True
        elif status in ("missing", "unsupported"):
            ok = not (key == "database" and r.get("server"))  # a chosen database server that vanished is a failure
        else:
            ok = False  # signed out / offline / not listed
        rows.append(_row(label, ok, "%s: %s" % (status, r.get("detail") or "")))
    return rows


def run(probe=True, task_expected=True, mcp=None):
    from . import install, runtime, setup
    task_expected = task_expected and not common.env("NO_TASK")
    rows = []
    cfg_raw, cfg_err = setup.read_raw_config()
    cfg = common.load_config()
    env = [r for r in setup.environment(cfg) if r["name"] in ("python", "git", "claude")]
    rows += env
    h = setup.home_state()
    src = "CHITRAGUPTA_HOME" if h["env"] else "pointer %s" % h["pointer_file"] if h["pointer"] else "default"
    rows.append(_row("home", plugin.is_set_up() and os.path.isdir(h["resolved"]),
                     "%s (%s)%s" % (h["resolved"], src, "" if plugin.is_set_up() else ": not set up, run /cg:setup")))
    if cfg_err:
        rows.append(_row("config", False, cfg_err))
    elif not h["config_exists"]:
        rows.append(_row("config", False, "%s missing: run /cg:setup" % common.wl("config.json")))
    else:
        rows.append(_row("config", True, "%s parses (%d keys)" % (common.wl("config.json"), len(cfg_raw or {}))))
    health = common.read_json(common.wl("health.json"))
    rows.append(hooks_row())
    rows.append(tick_row(health, task_expected))
    rows.append(push_row(cfg, health))
    rt = runtime.status()
    rows.append(_row("runtime", rt["current_ok"] and rt["launcher_exists"],
                     "%s current=%s, package %s%s" % (rt["path"], rt["current"], __version__,
                                                      "" if rt["current_ok"] and rt["launcher_exists"]
                                                      else ": re-run `setup apply` (or start a new session)")))
    ts = install.task_status()
    rows.append(_row("task", ts["installed"] or not task_expected, ts["detail"]))
    sk = setup.skill_installed()
    rows.append(_row("recall skill", sk["installed"], sk["path"] + ("" if sk["installed"] else
                     (" belongs to another skill (name: %s)" % sk.get("name") if os.path.exists(sk["path"])
                      else " missing: re-run `setup apply`"))))
    bl = setup.block_installed()
    rows.append(_row("CLAUDE.md block", bl["installed"], bl["path"] + ("" if bl["installed"] else
                     ": %s" % (bl["problem"] or "block missing, re-run `setup apply`"))))
    st = mcp if mcp is not None else (setup.mcp_status(cfg, probe=True) if probe else setup.cached_mcp_status())
    rows += mcp_rows(st, cfg)
    from .safety import status as safety_status
    rows += safety_status.doctor_rows(cfg, next((r["detail"] for r in env if r["name"] == "claude" and r["ok"]), ""))
    rows.append(errors_row(health))
    return rows


def main(argv):
    rows = run(probe="--fast" not in argv)
    if "--json" in argv:
        print(json.dumps(rows, indent=1))
    else:
        for r in rows:
            print("[%s] %-16s %s" % ("ok" if r["ok"] else "!!", r["name"], r["detail"]))
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # pythonw has no console
    sys.exit(main(sys.argv[1:]))
