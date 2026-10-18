"""Plugin glue that the SessionStart hook needs: the set-up check, paths under the Claude Code config folder,
the detached maintenance process and the weekly MCP reminder.

Kept import-light (hook.py loads it on every event): nothing here imports beyond `common`.

    python -m chitragupta plugin maintain          the detached maintenance run SessionStart starts
    python -m chitragupta plugin status [--json]   set-up state, runtime and maintenance stamps
"""
import json
import os
import sys
import time

from . import __version__
from . import common
from .safety import paths

NOT_SET_UP = "Chitragupta isn't set up yet — run /cg:setup to finish."
REMINDER_DAYS = 7
MAINTENANCE_EVERY = 600       # seconds between maintenance spawns while the runtime is current
MAINTENANCE_STALE_EVERY = 120  # ... and while the runtime is behind the plugin
LOCK_TTL = 900                # a maintenance lock older than this is abandoned

# Windows process creation flags for a detached, windowless child.



# --------------------------------------------------------------------------- paths outside the data home

def claude_dir():
    """Claude Code's config folder: CHITRAGUPTA_CLAUDE_DIR (tests), else CLAUDE_CONFIG_DIR, else ~/.claude."""
    d = common.env("CLAUDE_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.abspath(d or os.path.join(os.path.expanduser("~"), ".claude"))


def claude_json_path():
    """Claude Code's global state file: <CLAUDE_CONFIG_DIR>/.claude.json when a config dir is set, else ~/.claude.json."""
    if common.env("CLAUDE_DIR") or os.environ.get("CLAUDE_CONFIG_DIR"):
        return os.path.join(claude_dir(), ".claude.json")
    return os.path.join(os.path.expanduser("~"), ".claude.json")


def skill_path():
    return os.path.join(claude_dir(), "skills", "chitragupta", "SKILL.md")


def claude_md_path():
    return os.path.join(claude_dir(), "CLAUDE.md")


def runtime_dir():
    return os.path.join(common.user_dir(), "runtime")


# --------------------------------------------------------------------------- set-up state

def is_set_up():
    """A data home was chosen (CHITRAGUPTA_HOME or the pointer file) and it has a config."""
    if not (os.environ.get("CHITRAGUPTA_HOME") or os.path.isfile(common.home_pointer())):
        return False
    return os.path.isfile(common.wl("config.json"))


def not_set_up_output():
    return json.dumps({"systemMessage": NOT_SET_UP,
                       "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": NOT_SET_UP}},
                      ensure_ascii=True)


def write_not_set_up():
    sys.stdout.write(not_set_up_output())
    sys.stdout.flush()


def runtime_current():
    """True when the stable runtime holds this package's version and its launcher exists (two cheap reads)."""
    rt = runtime_dir()
    cur = common.read_text(os.path.join(rt, "current")).strip()
    return cur == __version__ and os.path.isfile(os.path.join(rt, "run.py")) \
        and os.path.isfile(os.path.join(rt, cur, "chitragupta", "__init__.py"))


def dashboard_available():
    return os.path.isfile(os.path.join(common.PACKAGE_DIR, "dashboard.py")) or \
        os.path.isfile(os.path.join(common.PACKAGE_DIR, "dashboard", "__init__.py"))


# --------------------------------------------------------------------------- weekly MCP reminder

def mcp_status_cache():
    return common.wl("cache", "mcp-status.json")


def _reminder_state():
    return common.wl("cache", "mcp-reminder.json")


def mcp_reminder(now=None):
    """One line for the SessionStart context when Slack or Jira isn't connected, at most once per 7 days.
    Reads the status cache that setup / doctor / maintenance write; never probes anything itself."""
    if (common.load_config().get("reminders") or {}).get("mcp") is False:  # turned off in /cg:setup
        return ""
    st = common.read_json(mcp_status_cache())
    if not isinstance(st, dict) or not st:
        return ""
    off = [label for key, label in (("slack", "Slack"), ("jira", "Jira"))
           if ((st.get(key) or {}).get("status") or "missing") != "connected"]
    if not off:
        return ""
    now = now or common.now()
    last = common.parse_ts(common.read_json(_reminder_state()).get("last"))
    if last and (now - last).total_seconds() < REMINDER_DAYS * 86400:
        return ""
    common.write_json(_reminder_state(), {"last": now.isoformat(timespec="seconds"), "off": off})
    effect = {"Slack": "the evening worklog is saved but not sent", "Jira": "ticket titles come from branch names"}
    return ("Chitragupta: %s %s connected for background jobs (%s); /cg:setup connects %s. "
            "This reminder repeats at most weekly." % (" and ".join(off), "aren't" if len(off) > 1 else "isn't",
                                                         "; ".join(effect[x] for x in off),
                                                         "them" if len(off) > 1 else "it"))


# --------------------------------------------------------------------------- maintenance

def _stamp():
    return common.wl("cache", "maintenance.stamp")


def own_launcher():
    """The launcher that runs *this* package: <plugin>/hooks/run.py, or runtime/run.py for a runtime copy."""
    plugin_run = os.path.join(os.path.dirname(common.PACKAGE_DIR), "hooks", "run.py")
    if os.path.isfile(plugin_run):
        return plugin_run
    runtime_run = os.path.join(os.path.dirname(os.path.dirname(common.PACKAGE_DIR)), "run.py")
    return runtime_run if os.path.isfile(runtime_run) else common.launcher()


def maintenance_due(now=None):
    if common.env("NO_MAINTENANCE"):
        return False
    try:
        age = (now or time.time()) - os.path.getmtime(_stamp())
    except OSError:
        return True
    return age >= (MAINTENANCE_EVERY if runtime_current() else MAINTENANCE_STALE_EVERY)


def maintenance_argv(cfg=None):
    return [common.python_exe(cfg, windowless=True), own_launcher(), "plugin", "maintain"]


def spawn_maintenance(cfg=None, popen=None):
    """Start ONE detached, windowless maintenance process and return at once (never waits). Returns True if started."""
    if not maintenance_due():
        return False
    try:
        common.write_text(_stamp(), common.now_iso() + "\n")
    except OSError:
        return False
    from .safety import commands
    try:
        commands.spawn(maintenance_argv(cfg), cwd=common.home(), cfg=cfg, popen=popen)
        return True
    except (OSError, ValueError):
        return False


def after_session_start(cfg=None):
    """Called by the SessionStart hook once its output is written. Never raises."""
    try:
        return spawn_maintenance(cfg)
    except Exception:  # noqa: BLE001 — a hook must never fail
        return False


def _lock_path():
    return os.path.join(common.user_dir(), "maintenance.lock")


def _take_lock():
    p = _lock_path()
    common.ensure_dir(os.path.dirname(p))
    for _ in range(2):
        try:
            os.close(paths.create_exclusive(p, ("%d %s\n" % (os.getpid(), common.now_iso())).encode()))
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(p) > LOCK_TTL:
                    paths.delete(p)
                    continue
            except OSError:
                continue
            return False
    return False


def maintain(mcp_refresh=None):
    """Refresh the runtime when it is behind, keep the dashboard running (if this version has one), and refresh
    the MCP status cache once a day (only if setup created it). Returns a list of what was done."""
    done = []
    if not _take_lock():
        return ["another maintenance run is active"]
    try:
        try:
            from . import runtime
            if runtime.needs_refresh():
                res = runtime.install()
                done.append("runtime %s %s" % (res.get("version"), "installed" if res.get("changed") else "unchanged"))
        except Exception as e:  # noqa: BLE001
            done.append("runtime failed: %r" % e)
        if dashboard_available():
            try:
                from . import dashboard  # Phase 4 module; tolerated absent
                ensure = getattr(dashboard, "ensure", None)
                if callable(ensure):
                    done.append("dashboard %s" % (ensure(),))
            except Exception as e:  # noqa: BLE001
                done.append("dashboard failed: %r" % e)
        try:
            path = mcp_status_cache()
            if os.path.isfile(path) and time.time() - os.path.getmtime(path) > 86400:
                if mcp_refresh is None:
                    from . import setup
                    setup.mcp_status(probe=True)
                else:
                    mcp_refresh()
                done.append("mcp status refreshed")
        except Exception as e:  # noqa: BLE001
            done.append("mcp status failed: %r" % e)
    finally:
        try:
            paths.delete(_lock_path())
        except OSError:
            pass
    if is_set_up():
        common.log("maintenance", "; ".join(done) or "nothing to do")
    return done


def status():
    stamp = None
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(_stamp())))
    except OSError:
        pass
    return {"version": __version__, "set_up": is_set_up(), "home": common.home(), "user_dir": common.user_dir(),
            "pointer": common.read_text(common.home_pointer()).strip() or None,
            "home_env": os.environ.get("CHITRAGUPTA_HOME") or None, "runtime_current": runtime_current(),
            "last_maintenance_spawn": stamp, "claude_dir": claude_dir()}


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "maintain":
        maintain()
        return 0
    if cmd == "status":
        st = status()
        print(json.dumps(st, indent=1) if "--json" in argv else "\n".join("%s: %s" % kv for kv in st.items()))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # pythonw has no console
    sys.exit(main(sys.argv[1:]))
