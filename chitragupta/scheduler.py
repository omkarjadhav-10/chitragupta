"""Chitragupta's own background job, one interface for every OS.

    windows  the `Chitragupta` Task Scheduler task (install.py); the Startup-folder launcher when it is refused
    launchd  macOS: the LaunchAgent ~/Library/LaunchAgents/dev.chitragupta.tick.plist (every 5 minutes, at login)
    systemd  Linux: the user units chitragupta.service + chitragupta.timer in ~/.config/systemd/user/
             (every 5 minutes, 1 minute after login, missed runs caught up after sleep)
    cron     Linux without systemd user units: one crontab line marked `# chitragupta`

Every backend runs the same command as the Windows task: <python> <stable launcher> agent tick --from-task.

    register(cfg)    create or replace the job          -> {ok, method, detail}
    unregister()     remove it (uninstall)              -> [lines]
    status(cfg)      is it there and up to date?        -> {os, method, installed, current, status, last_result,
                                                            last_run, next_run, detail, files}
    repair(cfg)      register again when missing or different (setup, doctor --repair, Settings)
                                                        -> {ok, changed, method, detail}

Commands go through the command guard (purposes `launchd`, `systemd`, `cron`, `task`), files through the path guard.
RUNNER replaces the command executor in tests: a callable(argv, purpose, input_text=None, timeout=60) ->
(rc, stdout, stderr). CHITRAGUPTA_NO_TASK=1 turns every real scheduler call off (dev and tests); SYSTEM and
LINUX_METHOD pin the OS and the Linux backend for tests.
"""
import os
import posixpath
import re
import shutil
import sys

from . import common

LABEL = "dev.chitragupta.tick"
SERVICE = "chitragupta.service"
TIMER = "chitragupta.timer"
UNITS = (SERVICE, TIMER)
CRON_MARK = "# chitragupta"
INTERVAL_S = 300

RUNNER = None        # tests: fake executor (see the module docstring)
SYSTEM = None        # tests: "windows" | "macos" | "linux"
LINUX_METHOD = None  # tests: "systemd" | "cron" | "none"


# --------------------------------------------------------------------------- where

def system():
    """windows | macos | linux. A test that injects install.RUNNER (fake schtasks) gets the Windows backend."""
    if SYSTEM:
        return SYSTEM
    from . import install
    if install.RUNNER is not None or os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def uid():
    """The current user's id (launchd's gui/<uid> domain); None where there is none (Windows)."""
    return os.getuid() if hasattr(os, "getuid") else None


def user_home():
    return os.path.expanduser("~")


def launch_agents_dir():
    """~/Library/LaunchAgents (CHITRAGUPTA_LAUNCH_AGENTS_DIR overrides it for tests)."""
    return common.env("LAUNCH_AGENTS_DIR") or os.path.join(user_home(), "Library", "LaunchAgents")


def plist_path():
    return os.path.join(launch_agents_dir(), LABEL + ".plist")


def systemd_user_dir():
    """~/.config/systemd/user, or $XDG_CONFIG_HOME/systemd/user (CHITRAGUPTA_SYSTEMD_USER_DIR overrides it)."""
    return common.env("SYSTEMD_USER_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.join(user_home(), ".config"), "systemd", "user")


def unit_paths():
    return [os.path.join(systemd_user_dir(), u) for u in UNITS]


def own_files():
    """The only files a scheduler backend writes (the path guard's roots for it)."""
    return [plist_path()] + unit_paths()


def _enabled():
    return RUNNER is not None or not common.env("NO_TASK")


def systemd_available():
    """A systemd user manager we can talk to: systemd booted, systemctl present and a user bus."""
    rt = os.environ.get("XDG_RUNTIME_DIR") or ("/run/user/%s" % uid())
    return os.path.isdir("/run/systemd/system") and bool(shutil.which("systemctl")) and \
        (os.path.exists(os.path.join(rt, "bus")) or bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")))


def linux_method():
    if LINUX_METHOD:
        return LINUX_METHOD
    if systemd_available():
        return "systemd"
    return "cron" if shutil.which("crontab") else "none"


def method_label(cfg=None):
    """Short text for setup's OS row."""
    s = system()
    if s == "windows":
        return "Windows (Task Scheduler)"
    if s == "macos":
        return "macOS (launchd LaunchAgent)"
    m = linux_method()
    return {"systemd": "Linux (systemd user timer)", "cron": "Linux (cron)"}.get(
        m, "Linux, but no systemd user session and no crontab: nothing can run the background job")


def _run(argv, purpose, input_text=None, timeout=60):
    if RUNNER is not None:
        return RUNNER(argv, purpose, input_text=input_text, timeout=timeout)
    from .safety import commands
    return commands.run(argv, purpose, input_text=input_text, timeout=timeout)


# --------------------------------------------------------------------------- what (pure, golden-tested)

def job_argv(cfg=None):
    return common.self_command("agent", "tick", "--from-task", cfg=cfg or common.load_config())


def job_path(home_dir, argv=()):
    """PATH for the job: launchd, systemd and cron start with a minimal one, but the tick needs git and claude.
    POSIX paths always (these files only exist on macOS and Linux)."""
    dirs = [posixpath.dirname(a) for a in argv[:1] if posixpath.isabs(a)]
    dirs += [posixpath.join(home_dir, ".local", "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
             "/usr/sbin", "/sbin"]
    out = []
    for d in dirs:
        if d and d not in out:
            out.append(d)
    return ":".join(out)


def _xml(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def plist(argv, data_home, home_dir):
    """The LaunchAgent: every 5 minutes and at login (RunAtLoad), in the background, never two at once."""
    args = "\n".join("    <string>%s</string>" % _xml(a) for a in argv)
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>%s</string>
  <key>ProgramArguments</key>
  <array>
%s
  </array>
  <key>WorkingDirectory</key>
  <string>%s</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>%s</string>
  </dict>
  <key>StartInterval</key>
  <integer>%d</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>LowPriorityIO</key>
  <true/>
</dict>
</plist>
""" % (LABEL, args, _xml(data_home), _xml(job_path(home_dir, argv)), INTERVAL_S)


def _sd_quote(a):
    """One systemd ExecStart= word: quoted when it holds a space, quote, backslash or specifier."""
    if re.search(r'[\s"\'\\%$;]', a) or not a:
        return '"%s"' % a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return a


def service_unit(argv, data_home, home_dir):
    return """[Unit]
Description=Chitragupta: turn Claude Code activity into the worklog (one background run)

[Service]
Type=oneshot
ExecStart=%s
WorkingDirectory=%s
Environment=%s
Nice=10
TimeoutStartSec=20min
""" % (" ".join(_sd_quote(a) for a in argv), _sd_quote(data_home), _sd_quote("PATH=" + job_path(home_dir, argv)))


def timer_unit():
    """Every 5 minutes on the clock (OnCalendar + Persistent: a run missed while asleep or off is caught up) and
    1 minute after login (OnStartupSec: the user manager starts at login)."""
    return """[Unit]
Description=Chitragupta: every 5 minutes, and 1 minute after login

[Timer]
OnStartupSec=1min
OnCalendar=*:0/5
Persistent=true
AccuracySec=30s
Unit=%s

[Install]
WantedBy=timers.target
""" % SERVICE


def _sh_quote(a):
    a = str(a).replace("%", "\\%")  # cron turns a bare % into a newline
    return a if re.match(r"^[A-Za-z0-9_@%+=:,./\\-]+$", a) else "'" + a.replace("'", "'\\''") + "'"


def cron_line(argv, home_dir):
    """The one crontab line: every 5 minutes, output dropped (the tick logs to the data home), marked as ours."""
    return "*/5 * * * * PATH=%s %s >/dev/null 2>&1 %s" % (_sh_quote(job_path(home_dir, argv)),
                                                          " ".join(_sh_quote(a) for a in argv), CRON_MARK)


def expected(cfg=None):
    """{path or "crontab": content} the current OS's backend would write."""
    cfg = cfg or common.load_config()
    argv, home, hd = job_argv(cfg), common.home(), user_home()
    s = system()
    if s == "macos":
        return {plist_path(): plist(argv, home, hd)}
    if s == "linux":
        if linux_method() == "cron":
            return {"crontab": cron_line(argv, hd)}
        return {unit_paths()[0]: service_unit(argv, home, hd), unit_paths()[1]: timer_unit()}
    return {}


def is_ours(line):
    return line.rstrip().endswith(" " + CRON_MARK)


def cron_table(current, line=None):
    """`current` with our marked line(s) removed, plus `line` (when given) at the end."""
    keep = [ln for ln in (current or "").splitlines() if not is_ours(ln)]
    while keep and not keep[-1].strip():
        keep.pop()
    if line:
        keep.append(line)
    return "\n".join(keep) + "\n" if keep else ""


def others(table):
    """The lines of a crontab that aren't ours (trailing blank lines ignored): what our edits must never change."""
    return cron_table(table).splitlines()


# --------------------------------------------------------------------------- the interface

def register(cfg=None):
    """-> {ok, method, detail}. Creates or replaces the job for this OS."""
    s = system()
    cfg = cfg or common.load_config()
    if s == "windows":
        from . import install
        return install.register_task(cfg)
    if not _enabled():
        return {"ok": True, "method": "skipped", "detail": "CHITRAGUPTA_NO_TASK is set"}
    if s == "macos":
        return _launchd_register(cfg)
    m = linux_method()
    if m == "systemd":
        res = _systemd_register(cfg)
        if res["ok"] or not _has_crontab():
            return res
        _drop_units()  # no usable user manager after all: cron instead
        fallback = _cron_register(cfg)
        fallback["detail"] = "%s; %s" % (res["detail"], fallback["detail"])
        return fallback
    if m == "cron":
        return _cron_register(cfg)
    return {"ok": False, "method": "none", "detail": "no systemd user session and no crontab: run "
            "`agent loop` in a terminal that stays open, or install cron"}


def unregister():
    """-> [lines]. Removes every trace of the job for this OS (all Linux variants)."""
    s = system()
    if s == "windows":
        from . import install
        return install.unregister_task()
    if s == "macos":
        return _launchd_unregister()
    return _systemd_unregister() + _cron_unregister()


def status(cfg=None):
    """-> {os, method, installed, current, status, last_result, last_run, next_run, detail, files}
    `current`: True when the job is what register() would write now, False when it differs, None when unknown."""
    s = system()
    cfg = cfg or common.load_config()
    res = {"os": s, "method": "none", "installed": False, "current": None, "status": "", "last_result": "",
           "last_run": "", "next_run": "", "detail": "", "files": []}
    if s == "windows":
        from . import install
        res.update(install.task_status(cfg))
        return res
    if s == "macos":
        return _launchd_status(cfg, res)
    res = _systemd_status(cfg, res)
    if not res["installed"]:
        res = _cron_status(cfg, dict(res, files=[]))
    if not res["installed"]:
        res["detail"] = "no background job registered (%s)" % ("systemd user timer" if linux_method() == "systemd"
                                                              else "crontab line")
    return res


def repair(cfg=None):
    """Register again when the job is missing or differs from what register() would write.
    -> {ok, changed, method, detail}"""
    cfg = cfg or common.load_config()
    st = status(cfg)
    if st["installed"] and st["current"] is not False:
        return {"ok": True, "changed": False, "method": st["method"], "detail": "already registered: " + st["detail"]}
    r = register(cfg)
    return {"ok": r["ok"], "changed": r["ok"] and r.get("method") != "skipped", "method": r.get("method"),
            "detail": ("re-registered (%s): " % ("was different" if st["installed"] else "was missing")) + r["detail"]}


# --------------------------------------------------------------------------- launchd (macOS)

def _service():
    return "gui/%s/%s" % (uid(), LABEL)


def _write(path, text):
    """True when the file changed."""
    from .safety import paths as sp
    return sp.write_text(path, text, caller="scheduler")


def _launchd_register(cfg):
    p = plist_path()
    try:
        changed = _write(p, plist(job_argv(cfg), common.home(), user_home()))
    except OSError as e:
        return {"ok": False, "method": "launchd", "detail": "LaunchAgent not written: %s" % e}
    loaded = _run(["launchctl", "print", _service()], "launchd", timeout=30)[0] == 0
    if loaded and not changed:
        return {"ok": True, "method": "launchd", "detail": "LaunchAgent %s already loaded (unchanged)" % LABEL}
    if loaded:
        _run(["launchctl", "bootout", _service()], "launchd", timeout=30)
    rc, so, se = _run(["launchctl", "bootstrap", "gui/%s" % uid(), p], "launchd", timeout=30)
    if rc != 0:
        return {"ok": False, "method": "launchd", "detail": "launchctl bootstrap failed: %s"
                % ((se or so).strip()[:200] or "rc %s" % rc)}
    return {"ok": True, "method": "launchd", "detail": "LaunchAgent %s loaded (%s)" % (LABEL, p)}


def _launchd_unregister():
    out = []
    p = plist_path()
    if _enabled():
        rc, so, se = _run(["launchctl", "bootout", _service()], "launchd", timeout=30)
        if rc == 0:
            out.append("LaunchAgent %s unloaded" % LABEL)
    if os.path.exists(p):
        from .safety import paths as sp
        sp.delete(p, caller="scheduler")
        out.append("LaunchAgent removed: %s" % p)
    return out or ["LaunchAgent %s: not registered" % LABEL]


def _field(text, name):
    m = re.search(r"^\s*%s\s*=\s*(.+)$" % re.escape(name), text or "", re.M)
    return m.group(1).strip() if m else ""


def _launchd_status(cfg, res):
    p = plist_path()
    want = plist(job_argv(cfg), common.home(), user_home())
    have = common.read_text(p) if os.path.exists(p) else None
    res["files"] = [p] if have is not None else []
    loaded, out = False, ""
    if _enabled():
        rc, out, _ = _run(["launchctl", "print", _service()], "launchd", timeout=30)
        loaded = rc == 0
    res.update(method="launchd" if (loaded or have is not None) else "none", installed=loaded and have is not None,
               current=None if have is None else have == want, status=_field(out, "state"),
               last_result=_field(out, "last exit code"))
    if res["installed"]:
        res["detail"] = "LaunchAgent %s: %s, last exit %s%s" % (LABEL, res["status"] or "loaded",
                                                                 res["last_result"] or "?",
                                                                 "" if res["current"] else " (plist differs: repair)")
    elif have is not None:
        res["detail"] = "LaunchAgent %s exists but is not loaded (repair loads it)" % p
    else:
        res["detail"] = "LaunchAgent %s not registered" % LABEL
    return res


# --------------------------------------------------------------------------- systemd (Linux)

def _systemctl(*args, timeout=30):
    return _run(["systemctl", "--user"] + list(args), "systemd", timeout=timeout)


def _systemd_register(cfg):
    svc, tmr = unit_paths()
    try:
        _write(svc, service_unit(job_argv(cfg), common.home(), user_home()))
        _write(tmr, timer_unit())
    except OSError as e:
        return {"ok": False, "method": "systemd", "detail": "units not written: %s" % e}
    for step in (("daemon-reload",), ("enable", TIMER), ("start", TIMER)):
        rc, so, se = _systemctl(*step)
        if rc != 0:
            return {"ok": False, "method": "systemd", "detail": "systemctl --user %s failed: %s"
                    % (" ".join(step), (se or so).strip()[:200] or "rc %s" % rc)}
    _cron_unregister()  # an earlier cron fallback is no longer needed
    return {"ok": True, "method": "systemd", "detail": "systemd user timer %s enabled (%s)" % (TIMER, tmr)}


def _drop_units():
    from .safety import paths as sp
    for p in unit_paths():
        if os.path.exists(p):
            sp.delete(p, caller="scheduler")


def _systemd_unregister():
    out = []
    files = [p for p in unit_paths() if os.path.exists(p)]
    if files and _enabled():
        _systemctl("stop", TIMER)
        _systemctl("disable", TIMER)
    if files:
        from .safety import paths as sp
        for p in files:
            sp.delete(p, caller="scheduler")
        if _enabled():
            _systemctl("daemon-reload")
        out.append("systemd user units removed: %s" % ", ".join(files))
    return out


def _systemd_status(cfg, res):
    svc, tmr = unit_paths()
    have = [common.read_text(p) if os.path.exists(p) else None for p in (svc, tmr)]
    res["files"] = [p for p, h in zip((svc, tmr), have) if h is not None]
    if not res["files"]:
        return res
    want = [service_unit(job_argv(cfg), common.home(), user_home()), timer_unit()]
    enabled = active = ""
    out = ""
    if _enabled():
        enabled = _systemctl("is-enabled", TIMER)[1].strip()
        active = _systemctl("is-active", TIMER)[1].strip()
        out = _systemctl("status", TIMER)[1]
    m = re.search(r"Trigger:\s*(.+)$", out, re.M)
    res.update(method="systemd", installed=enabled == "enabled" and None not in have, current=have == want,
               status=active or enabled, next_run=m.group(1).strip() if m else "")
    if res["installed"]:
        res["detail"] = "systemd user timer %s: %s%s" % (TIMER, active or "enabled",
                                                        "" if res["current"] else " (units differ: repair)")
    else:
        res["detail"] = "systemd user units in %s but the timer is %s (repair enables it)" % (
            systemd_user_dir(), enabled or "not enabled")
    return res


# --------------------------------------------------------------------------- cron (Linux fallback)

def _has_crontab():
    return RUNNER is not None or bool(shutil.which("crontab"))


def _crontab():
    """(ok, table). No crontab yet counts as an empty one."""
    rc, so, se = _run(["crontab", "-l"], "cron", timeout=30)
    if rc == 0:
        return True, so
    if "no crontab" in (se or so).lower():
        return True, ""
    return False, (se or so).strip()[:200] or "rc %s" % rc


def _install_table(table):
    rc, so, se = _run(["crontab", "-"], "cron", input_text=table, timeout=30)
    return rc == 0, (se or so).strip()[:200]


def _cron_register(cfg):
    ok, cur = _crontab()
    if not ok:
        return {"ok": False, "method": "cron", "detail": "crontab -l failed: %s" % cur}
    new = cron_table(cur, cron_line(job_argv(cfg), user_home()))
    if cur.splitlines() == new.splitlines():
        return {"ok": True, "method": "cron", "detail": "crontab line %s already there (unchanged)" % CRON_MARK}
    ok, err = _install_table(new)
    if not ok:
        return {"ok": False, "method": "cron", "detail": "crontab install failed: %s" % err}
    return {"ok": True, "method": "cron", "detail": "crontab line %s installed (every 5 minutes)" % CRON_MARK}


def _cron_unregister():
    if not _enabled() or not _has_crontab():
        return []
    ok, cur = _crontab()
    if not ok or not any(is_ours(ln) for ln in cur.splitlines()):
        return []
    ok, err = _install_table(cron_table(cur))
    return ["crontab line %s removed" % CRON_MARK if ok else "crontab line left: %s" % err]


def _cron_status(cfg, res):
    if not _enabled() or not _has_crontab():
        return res
    ok, cur = _crontab()
    mine = [ln for ln in (cur.splitlines() if ok else []) if is_ours(ln)]
    if not mine:
        return res
    want = cron_line(job_argv(cfg), user_home())
    res.update(method="cron", installed=True, current=mine == [want], status="installed")
    res["detail"] = "crontab line %s every 5 minutes%s" % (CRON_MARK, "" if res["current"] else " (differs: repair)")
    return res
