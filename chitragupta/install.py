"""Install / activate the worklog. (Superseded by the setup module; kept callable.)

    install.py --show                print what activation will change (Gate 2), change nothing
    install.py --activate            back up + merge ~/.claude/settings.json, CLAUDE.md block, skill, permission, task
    install.py --task                (re)register the scheduled task only
    install.py --hooks-only          merge hooks + permission into settings.json only
    install.py --show-upgrade01      diffs for upgrade 01 (settings.json, CLAUDE.md block, skill) - changes nothing
    install.py --upgrade01           apply upgrade 01: settings.json hooks, CLAUDE.md block, skill (task unchanged)
"""
import copy
import datetime as _dt
import difflib
import json
import os
import shutil
import sys

from . import common

CLAUDE_DIR = common.env("CLAUDE_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
CLAUDE_MD = os.path.join(CLAUDE_DIR, "CLAUDE.md")
SKILL = os.path.join(CLAUDE_DIR, "skills", "worklog", "SKILL.md")
TASK_NAME = "WorklogAgent"
MD_START, MD_END = "<!-- worklog:start -->", "<!-- worklog:end -->"


def hook_mark():
    """Our hook entries run the stable launcher."""
    return common.launcher().replace("\\", "/")


def cfg_paths():
    cfg = common.load_config()
    p = cfg.get("paths") or {}
    return cfg, p.get("python"), p.get("pythonw")


def hook_entries():
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


def read_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def dump(s):
    return json.dumps(s, indent=2, ensure_ascii=False) + "\n"


def claude_md_block():
    return """%s
## Worklog
My Claude Code sessions are recorded automatically into %s by background hooks; don't write to those files yourself — except, only when I ask: appending a line to `inbox.md`, or creating a file in `_drop\\` whose name starts with the ticket ID (e.g. "save this analysis to the worklog for ABC-1001").
When a reply establishes something worth keeping — a root cause, how a flow works, an important query, table, API, config or decision — end that reply with one line: `Worklog note: <one specific sentence with file:line / table / endpoint>`. Only for genuinely important points, a few per session at most. Never put secrets or patient data in it.
For questions about my past work or a ticket's history, replaying a ticket's requests, saving requests to the worklog, a ticket's DB flow or its artifacts, use the worklog skill.
%s""" % (MD_START, common.home(), MD_END)


SKILL_BODY = r"""---
name: worklog
description: Answer questions from the user's automatic worklog at {home} — what they did on a day or week, everything known about a ticket (findings, root cause, how it works, DB queries, tables, APIs, integrations, files, decisions), standups, timesheets and handover notes; replay or run a ticket's saved requests, save requests to the worklog, show or map a ticket's DB flow (which tables and columns change, from what to what), and find a ticket's artifacts. Use whenever they mention a ticket ID, ask what they did or found, what we know about something, which tables, APIs or queries were involved, want to replay / save / pin a request, map a DB flow, find an analysis doc or export, or want a standup, timesheet, weekly summary or handover — even if they don't say "worklog".
---

# Worklog recall

The worklog at `{home}` is written automatically by a background agent from Claude Code sessions,
git commits, Bruno requests, SQL scripts, `inbox.md` and `_drop/`. Read it; don't edit it (two exceptions below).

## Where things live
- `tickets/INDEX.md` — every ticket: status, first/last worked, ≈ time, TL;DR
- `tickets/<TICKET>/README.md` — the ticket hub: quick links, Scenarios (trigger request · DB effect · verify query),
  replay steps, latest artifacts, TL;DR, problem, root cause & findings, how it works, fix, database, APIs,
  integrations, config, errors, repro, decisions, open questions, code map, timeline
- `tickets/<TICKET>/queries.md` — important queries, dated, with what each revealed
- `tickets/<TICKET>/requests.md` — the minimal replay set as curl for Git Bash (purpose + expected per step)
- `tickets/<TICKET>/replay.sh` — run it: `bash tickets/<TICKET>/replay.sh qa` (or `local`) from the Worklog folder
- `bruno/<TICKET>/` — the same requests in the single Bruno collection `{home}\bruno` (envs in `bruno/environments`,
  secrets in `bruno/.env`, git-ignored)
- `tickets/<TICKET>/db-flow.md` — DB flow: Mermaid flow + sequence, table map, column changes (before → after,
  set by, condition, file:line), relationships, verify queries, unknowns & conflicts
- `knowledge/db-map.md` — all tables across tickets, relationships, which components write which columns
- `tickets/<TICKET>/artifacts/INDEX.md` — every artifact (analysis docs, HTML pages, diagrams, payloads, scripts,
  exports, screenshots, published links) with date, purpose, source and where it is stored (`local/` = this machine only)
- `knowledge/db-tables.md`, `knowledge/apis.md` (with a "Replay in" column), `knowledge/integrations.md` — catalogs
- `knowledge/branches.md` — every local branch: parent, created, env status (qa / uat / staging / master)
- `daily/YYYY/MM/YYYY-MM-DD.md` — per day: summary, per-ticket work with times (incl. requests saved, DB flow
  updates, new artifacts), other work, outside Claude Code, commits, standup. A work day starts at {day}.
- `data/` — canonical JSON behind the markdown (useful for exact lookups)

## How to answer
1. Search in this order: ticket README → requests.md / db-flow.md / artifacts/INDEX.md → queries.md → knowledge/ →
   daily files by date → grep across the folder (ticket keys look like ABC-1234, XYZ-12).
2. Cite the file and date for every fact, e.g. "(tickets/ABC-1002/README.md, 2026-10-14)". Quote queries in
   fenced code blocks exactly as recorded.
3. Never claim more than the files say. If something isn't recorded, say so plainly.
4. Standups: use the "Standup for next working day" section of the latest daily file; if it is a draft, build
   Yesterday/Today/Blockers from that day's ticket sections, one line each with ticket refs.
5. Timesheets: sum the "≈ Time" column of each daily file's Tickets table per ticket and day; label totals as
   approximate (derived from Claude Code activity).
6. Weekly summaries and handovers: build from daily files for the range and the ticket READMEs.
7. "Replay / run the requests for <TICKET>": point to `bash tickets/<TICKET>/replay.sh qa` (and Bruno folder
   `bruno/<TICKET>`); you may run replay.sh yourself when asked. If it stops on a missing variable, the name to fill is in
   `bruno/.env.example`; the user fills `bruno/.env` themselves — never write secrets anywhere.

## Saving requests (only when the user asks to save / pin requests to the worklog)
End your final reply with one fenced block; the agent files it exactly as written and pins the requests:

```worklog-requests
{{"ticket": "ABC-1001", "scenario": "null role locks user",
 "requests": [
  {{"name": "Login to v2", "method": "POST", "url": "https://api.qa.acme.test/api/v1/auth/login",
   "headers": {{"Content-Type": "application/json"}},
   "body": {{"email": "{{{{process.env.ABC1001_EMAIL}}}}", "password": "{{{{process.env.ABC1001_PASSWORD}}}}"}},
   "expected": "200 with access_token", "purpose": "get a token"}},
  {{"name": "Update customer - null role", "method": "PUT", "url": "https://.../customers/42",
   "headers": {{"authorization": "api-key {{{{process.env.ORDERS_API_KEY}}}}"}},
   "body": {{"firstName": "{{{{process.env.ABC1001_FIRST_NAME}}}}", "role": null}},
   "expected": "200; is_locked -> true", "critical_fields": ["role = null"]}}
 ]}}
```
Strict JSON. Credentials, tokens, API keys and person values (names, DOB, MRN, phone, email, address, member /
accession IDs) are always `{{{{process.env.NAME}}}}` (ticket-scoped names like ABC1001_FIRST_NAME); everything else
literal — keep JSON null as null. Use the real captured URL and body; never invent fields.

## Mapping a DB flow (only when the user asks, e.g. "map the DB flow for ABC-1001")
Trace the code path: entry point (controller / listener / integration channel / job) → services → repositories / SQL /
procs → tables and columns. Answer in prose, then end the final reply with one fenced block:

```worklog-dbflow
{{"ticket": "ABC-1001", "overview": "2-4 sentences",
 "data_flow": [
  {{"scenario": "null role locks user", "step": 1, "trigger": "PUT /api/v1/customers/{{id}}",
   "component": "CustomerService.update (CustomerService.java:120)", "operation": "update",
   "table": "shop.customers", "columns_read": ["role"],
   "columns_written": [{{"column": "is_locked", "change": "false -> true", "condition": "isBlank(payload.role)"}}],
   "via": "JPA/EF entity", "evidence": ["CustomerService.java:120"], "confidence": "confirmed"}}
 ],
 "table_relations": [{{"from": "shop.orders.customer_id", "to": "shop.customers.id", "kind": "fk",
   "evidence": "Order.java:22"}}]}}
```
operation: select | insert | update | delete | upsert | merge | proc. confidence: confirmed (read in code / seen in
data) | likely | hypothesis. Every entry carries evidence (file:line, query, log line). Emit these blocks only when
asked to save or map something, and only in the final reply.

## The only writes allowed (only when the user explicitly asks)
- Append one line to `{home}\inbox.md` (prefix `TICKET-123:` to attach it; `ABC-1001: pin curl ...` pins a request).
- Create a file in `{home}\_drop\` whose name starts with the ticket ID, e.g. `ABC-1001_lock-analysis.md`
  ("save this analysis to the worklog for ABC-1001"); the agent files it under the ticket's artifacts.
Never edit any other worklog file.
"""


def skill_text():
    return SKILL_BODY.format(home=common.home(), day=common.load_config().get("day_starts_at", "04:00"))


def task_xml():
    cfg = common.load_config()
    user = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
    argv = common.self_command("agent", "tick", cfg=cfg, windowless=True)
    exe, arguments = argv[0], " ".join('"%s"' % a if " " in a or "\\" in a or "/" in a else a for a in argv[1:])
    start = _dt.datetime.now().replace(second=0, microsecond=0).isoformat()

    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Worklog agent: turns Claude Code activity into %s (hidden, every 5 minutes).</Description>
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
""" % (esc(common.home()), TASK_NAME, esc(user), start, esc(user), esc(exe), esc(arguments), esc(common.home()))


def startup_launcher_path():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
                        "WorklogAgent.vbs")


def register_task():
    xml_path = common.wl("cache", "WorklogAgent.xml")
    common.ensure_dir(os.path.dirname(xml_path))
    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(task_xml())
    rc, so, se = common.run(["schtasks", "/create", "/tn", TASK_NAME, "/xml", xml_path, "/f"], timeout=60)
    if rc == 0:
        return "scheduled task registered"
    # policy blocks Task Scheduler: hidden Startup-folder launcher running `agent loop`
    argv = common.self_command("agent", "loop", windowless=True)
    vbs = 'CreateObject("WScript.Shell").Run "%s", 0, False\r\n' % " ".join('""%s""' % a for a in argv)
    with open(startup_launcher_path(), "w", encoding="utf-8") as f:
        f.write(vbs)
    return "schtasks failed (%s); installed Startup-folder launcher %s" % ((se or so).strip(), startup_launcher_path())


def upsert_block(path, block):
    txt = common.read_text(path)
    i, j = txt.find(MD_START), txt.find(MD_END)
    if i >= 0 and j > i:
        new = txt[:i] + block + txt[j + len(MD_END):]
    else:
        new = (txt.rstrip("\n") + "\n\n" if txt.strip() else "") + block + "\n"
    common.write_text(path, new)


def show():
    before = read_settings()
    after = merged_settings(before)
    diff = difflib.unified_diff(dump(before).splitlines(), dump(after).splitlines(), "settings.json (current)",
                                "settings.json (after)", lineterm="", n=2)
    print("\n".join(diff))
    print("\n--- CLAUDE.md block ---\n" + claude_md_block())
    print("\n--- skill: %s ---\n%s" % (SKILL, skill_text()))
    print("\n--- permission ---\n" + permission_rule())
    print("\n--- task ---\n" + task_xml())


def activate(hooks_only=False):
    before = read_settings()
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = SETTINGS + ".worklog-bak." + stamp
    if os.path.exists(SETTINGS):
        shutil.copy2(SETTINGS, backup)
    after = merged_settings(before)
    tmp = SETTINGS + ".worklog.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump(after))
    with open(tmp, encoding="utf-8") as f:
        json.load(f)  # validate before replacing
    os.replace(tmp, SETTINGS)
    reread = read_settings()
    assert reread == after, "settings re-read mismatch"
    missing = [k for k in before if k not in reread]
    assert not missing, "lost keys: %s" % missing
    out = ["settings.json merged (backup: %s)" % backup]
    if hooks_only:
        return out
    upsert_block(CLAUDE_MD, claude_md_block())
    out.append("CLAUDE.md block written")
    common.write_text(SKILL, skill_text())
    out.append("skill written: " + SKILL)
    out.append(register_task())
    return out


def _diff(name, before, after):
    return "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), name + " (current)", name + " (after)",
                                          lineterm="", n=1)) or "(%s unchanged)" % name


def show_upgrade01():
    before = read_settings()
    print(_diff("settings.json", dump(before), dump(merged_settings(before))))
    md = common.read_text(CLAUDE_MD)
    i, j = md.find(MD_START), md.find(MD_END)
    cur = md[i:j + len(MD_END)] if i >= 0 and j > i else ""
    print(_diff("CLAUDE.md worklog block", cur, claude_md_block()))
    print(_diff("skills/worklog/SKILL.md", common.read_text(SKILL), skill_text()))


def upgrade01():
    out = activate(hooks_only=True)
    upsert_block(CLAUDE_MD, claude_md_block())
    out.append("CLAUDE.md block written")
    common.write_text(SKILL, skill_text())
    out.append("skill written: " + SKILL)
    return out


def main(argv):
    if "--show-upgrade01" in argv:
        show_upgrade01()
    elif "--upgrade01" in argv:
        print("\n".join(upgrade01()))
    elif "--show" in argv:
        show()
    elif "--activate" in argv:
        print("\n".join(activate()))
    elif "--hooks-only" in argv:
        print("\n".join(activate(hooks_only=True)))
    elif "--task" in argv:
        print(register_task())
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # pythonw has no console
    sys.exit(main(sys.argv[1:]))
