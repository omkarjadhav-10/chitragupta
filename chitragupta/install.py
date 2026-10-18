"""Windows integration that setup reuses: the `Chitragupta` scheduled task and its Startup-folder fallback (the
Windows backend of scheduler.py), plus settings.json hook helpers for running the engine without the plugin
(development and the e2e self-test).

    python -m chitragupta install --task        (re)register the scheduled task (or the Startup fallback)
    python -m chitragupta install --task-xml    print the task XML, change nothing
    python -m chitragupta install --status      is the task (or the fallback) installed?

The plugin's hooks come from hooks/hooks.json; nothing here edits ~/.claude/settings.json.
Every schtasks call goes through RUNNER (common.run by default) so tests can replace it; CHITRAGUPTA_NO_TASK=1
turns every real Task Scheduler call off (dev and test sessions).
"""
import copy
import datetime as _dt
import json
import os
import re
import sys

from . import common
from . import plugin
from .safety import commands, paths as sp

TASK_NAME = "Chitragupta"
STARTUP_FILE = "Chitragupta.vbs"
RUNNER = None  # tests: a callable(args, timeout=…) -> (rc, stdout, stderr)


def _run(args, timeout=60):
    """schtasks through the command guard (purpose `task`: the Chitragupta task only)."""
    if RUNNER is not None:
        return RUNNER(args, timeout=timeout)
    return commands.run(args, "task", timeout=timeout)


def _scheduler():
    """Task Scheduler is reachable: Windows (or a test RUNNER), and CHITRAGUPTA_NO_TASK is not set."""
    return RUNNER is not None or (os.name == "nt" and not common.env("NO_TASK"))


# --------------------------------------------------------------------------- scheduled task

def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _quote(a):
    return '"%s"' % a if (" " in a or "\\" in a or "/" in a) else a


def task_action(cfg=None):
    """(command, arguments) the task runs: windowless python + the stable launcher + `agent tick --from-task`
    (--from-task also starts the dashboard, so it is up after logon)."""
    argv = common.self_command("agent", "tick", "--from-task", cfg=cfg or common.load_config(), windowless=True)
    return argv[0], " ".join(_quote(a) for a in argv[1:])


def task_xml(cfg=None, user=None, start=None):
    """Logon (+1 min) and every 5 minutes, hidden, IgnoreNew, 20-minute limit, interactive token, least privilege."""
    cfg = cfg or common.load_config()
    user = user or "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
    start = start or _dt.datetime.now().replace(second=0, microsecond=0).isoformat()
    exe, arguments = task_action(cfg)
    home = common.home()
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Chitragupta: turns Claude Code activity into the worklog in %s (hidden, every 5 minutes).</Description>
    <URI>\\%s</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>%s</UserId>
      <Delay>PT1M</Delay>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>%s</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT20M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%s</Command>
      <Arguments>%s</Arguments>
      <WorkingDirectory>%s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""" % (_esc(home), TASK_NAME, _esc(user), start, _esc(user), _esc(exe), _esc(arguments), _esc(home))


def startup_dir():
    """The per-user Startup folder (CHITRAGUPTA_STARTUP_DIR overrides it for tests)."""
    return common.env("STARTUP_DIR") or os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                                                     "Start Menu", "Programs", "Startup")


def startup_launcher_path():
    return os.path.join(startup_dir(), STARTUP_FILE)


def startup_vbs(cfg=None):
    argv = common.self_command("agent", "loop", cfg=cfg, windowless=True)
    return 'CreateObject("WScript.Shell").Run "%s", 0, False\r\n' % " ".join('""%s""' % a for a in argv)


# --------------------------------------------------------------------------- dashboard shortcut

SHORTCUT_FILE = "Chitragupta.url"


def shortcut_dirs(desktop=False):
    """Start menu Programs folder (and the Desktop). CHITRAGUPTA_STARTMENU_DIR / CHITRAGUPTA_DESKTOP_DIR override them;
    with CHITRAGUPTA_NO_TASK set and no override (dev and tests) there is no folder, so nothing is written."""
    out = []
    sm = common.env("STARTMENU_DIR") or (None if common.env("NO_TASK") or os.name != "nt" else
                                         os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"))
    if sm:
        out.append(sm)
    if desktop:
        dk = common.env("DESKTOP_DIR") or (None if common.env("NO_TASK") or os.name != "nt" else
                                           os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"))
        if dk:
            out.append(dk)
    return out


def shortcut_text(url):
    """An Internet Shortcut: plain text, opens the address in the default browser; nothing is executed."""
    return "[InternetShortcut]\r\nURL=%s\r\n" % url


def write_shortcuts(url, desktop=False):
    """-> {ok, paths, detail}. Rewrites only our own file name; never touches other shortcuts."""
    paths = []
    try:
        for d in shortcut_dirs(desktop):
            p = os.path.join(d, SHORTCUT_FILE)
            common.write_text(p, shortcut_text(url))
            paths.append(p)
    except OSError as e:
        return {"ok": False, "paths": paths, "detail": "shortcut not written: %s" % e}
    return {"ok": True, "paths": paths, "detail": ("shortcut " + ", ".join(paths)) if paths else "shortcut skipped (no Start menu here)"}


def remove_shortcuts():
    out = []
    for d in shortcut_dirs(desktop=True):
        p = os.path.join(d, SHORTCUT_FILE)
        try:
            if os.path.isfile(p) and "[InternetShortcut]" in common.read_text(p):
                sp.delete(p)
                out.append("shortcut removed: %s" % p)
        except OSError as e:
            out.append("shortcut left: %s (%s)" % (p, e))
    return out


def register_task(cfg=None):
    """Register the task; when Task Scheduler refuses (policy), install the hidden Startup-folder launcher instead.
    -> {ok, method: "task" | "startup" | "skipped", detail}"""
    if not _scheduler():
        if os.name == "nt":
            return {"ok": True, "method": "skipped", "detail": "CHITRAGUPTA_NO_TASK is set"}
        return {"ok": False, "method": "skipped", "detail": "scheduled task is Windows-only"}
    cfg = cfg or common.load_config()
    xml_path = common.wl("cache", TASK_NAME + ".xml")
    with sp.open_write(xml_path, "w", encoding="utf-16") as f:
        f.write(task_xml(cfg))
    rc, so, se = _run(["schtasks", "/create", "/tn", TASK_NAME, "/xml", xml_path, "/f"])
    if rc == 0:
        if os.path.exists(startup_launcher_path()):  # an earlier fallback is no longer needed
            try:
                sp.delete(startup_launcher_path())
            except OSError:
                pass
        return {"ok": True, "method": "task", "detail": "scheduled task %s registered" % TASK_NAME}
    try:
        with sp.open_write(startup_launcher_path(), "w", encoding="utf-8") as f:
            f.write(startup_vbs(cfg))
    except OSError as e:
        return {"ok": False, "method": "none", "detail": "schtasks failed (%s); Startup launcher failed: %s"
                % ((se or so).strip(), e)}
    return {"ok": True, "method": "startup", "detail": "schtasks failed (%s); installed the Startup-folder launcher %s"
            % ((se or so).strip()[:200], startup_launcher_path())}


def unregister_task():
    out = []
    if _scheduler():
        rc, so, se = _run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
        out.append("scheduled task %s removed" % TASK_NAME if rc == 0 else
                   "scheduled task %s: %s" % (TASK_NAME, (se or so).strip()[:200] or "not found"))
    if os.path.exists(startup_launcher_path()):
        sp.delete(startup_launcher_path())
        out.append("Startup launcher removed")
    return out


def _cmdline_key(s):
    return re.sub(r"\s+", " ", (s or "").replace('"', "")).strip().lower()


def task_status(cfg=None):
    """-> {installed, method: task|startup|none, current, status, last_result, last_run, next_run, detail}
    `current`: the task runs what task_action() gives now (None when unknown)."""
    res = {"installed": False, "method": "none", "current": None, "status": "", "last_result": "", "last_run": "",
           "next_run": ""}
    if _scheduler():
        rc, so, se = _run(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST", "/v"], timeout=30)
        if rc == 0:
            res.update(installed=True, method="task")
            for key, field in (("status", "Status"), ("last_result", "Last Result"), ("last_run", "Last Run Time"),
                               ("next_run", "Next Run Time"), ("task_to_run", "Task To Run")):
                m = re.search(r"^%s:\s*(.*)$" % re.escape(field), so, re.M)
                if m:
                    res[key] = m.group(1).strip()
            if res.get("task_to_run"):
                exe, arguments = task_action(cfg)
                res["current"] = _cmdline_key(res["task_to_run"]) == _cmdline_key(exe + " " + arguments)
    if not res["installed"] and os.path.exists(startup_launcher_path()):
        res.update(installed=True, method="startup")
    if res["method"] == "task":
        res["detail"] = "task %s: %s, last result %s%s" % (TASK_NAME, res["status"] or "?", res["last_result"] or "?",
                                                          " (runs an older command: repair)" if res["current"] is False
                                                          else "")
    elif res["method"] == "startup":
        res["detail"] = "Startup-folder launcher %s (Task Scheduler unavailable)" % startup_launcher_path()
    else:
        res["detail"] = "scheduled task %s not registered" % TASK_NAME
    return res


# --------------------------------------------------------------------------- settings.json hooks (dev / e2e only)

def settings_path():
    return os.path.join(plugin.claude_dir(), "settings.json")


def hook_mark():
    """Our hook entries run the stable launcher."""
    return common.launcher().replace("\\", "/")


def hook_entries():
    """Hook groups equivalent to hooks/hooks.json, but through the stable launcher (no plugin needed)."""
    cfg = common.load_config()

    def h(windowless, event, asynchronous, timeout=None):
        argv = [a.replace("\\", "/") for a in common.self_command("hook", event, cfg=cfg, windowless=windowless)]
        d = {"type": "command", "command": argv[0], "args": argv[1:]}
        if asynchronous:
            d["async"] = True
        if timeout:
            d["timeout"] = timeout
        return d

    return {
        "SessionStart": {"matcher": "startup|resume|clear|compact|fork", "hooks": [h(False, "session-start", False, 10)]},
        "UserPromptSubmit": {"hooks": [h(True, "user-prompt", True)]},
        "PostToolUse": {"matcher": "Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|Read|WebFetch|Artifact|mcp__.*",
                        "hooks": [h(True, "post-tool", True)]},
        "Stop": {"hooks": [h(True, "stop", True)]},
        "SessionEnd": {"hooks": [h(True, "session-end", True)]},
    }


def permission_rule():
    p = common.home().replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return "Read(/%s/**)" % p


def _is_ours(group):
    mark = hook_mark()
    return any(mark in " ".join([str(x.get("command", ""))] + [str(a) for a in x.get("args", [])]).replace("\\", "/")
               for x in group.get("hooks", []))


def strip_ours(settings):
    s = copy.deepcopy(settings)
    hooks = s.get("hooks") or {}
    for ev in list(hooks):
        hooks[ev] = [g for g in hooks[ev] if not _is_ours(g)]
        if not hooks[ev]:
            del hooks[ev]
    if "hooks" in s and not s["hooks"]:
        del s["hooks"]
    perms = s.get("permissions") or {}
    if perms.get("allow"):
        perms["allow"] = [r for r in perms["allow"] if r != permission_rule()]
        if not perms["allow"]:
            del perms["allow"]
    if "permissions" in s and not s["permissions"]:
        del s["permissions"]
    return s


def merged_settings(settings):
    s = strip_ours(settings)
    hooks = s.setdefault("hooks", {})
    for ev, group in hook_entries().items():
        hooks.setdefault(ev, []).append(group)
    perms = s.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    if permission_rule() not in allow:
        allow.append(permission_rule())
    return s


def read_settings(path=None):
    try:
        with open(path or settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def dump(s):
    return json.dumps(s, indent=2, ensure_ascii=False) + "\n"


def main(argv):
    if "--task-xml" in argv:
        print(task_xml())
    elif "--task" in argv:
        print(json.dumps(register_task(), indent=1))
    elif "--status" in argv:
        st = task_status()
        print(json.dumps(st, indent=1))
        return 0 if st["installed"] else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # pythonw has no console
    sys.exit(main(sys.argv[1:]))
