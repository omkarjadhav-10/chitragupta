"""The /cg: commands' engine: `python -m chitragupta cli <command> …`.

  where [--json]                         data home, user folder, runtime, version, set up or not
  today [--date D] [--no-preview]        time per ticket so far + tonight's Jira-worklog preview
  ticket <KEY> [--json]                  a ticket's files and a short summary
  trace <KEY>                            the ticket's DB flow and its QA worked example
  hours [--week|--month] [--date D] [--csv]   hours per ticket for a week (by day) or a month (by ISO week)
  log <directive…>                       append ONE time:/leave:/half-day:/holiday:/workday:/resend: line to inbox.md
  log --today                            print the current work day (for resolving "Monday", "yesterday")
  pause | resume | status                the PAUSED switch (<home>/.worklog/PAUSED)

Everything is read-only except `log` (one line appended to inbox.md) and `pause`/`resume`.
No command calls a model, Jira, Slack or the database MCP.
"""
import argparse
import csv
import datetime as _dt
import io
import json
import os
import re
import sys

from . import __version__
from . import common
from . import timesheet as T

KEY_RX = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,9}-\d{1,6}$")
GRAMMAR = """Accepted lines (one per call):
  time: [YYYY-MM-DD] [HH:MM] <2h|1h 30m|45m> <TICKET> <what you did>
  leave: YYYY-MM-DD[..YYYY-MM-DD]
  half-day: YYYY-MM-DD
  holiday: YYYY-MM-DD [name]
  workday: YYYY-MM-DD
  resend: YYYY-MM-DD"""


def out(s=""):
    sys.stdout.write(s + "\n")


def today_day(cfg):
    return common.workday(common.now(), cfg)


def hours(m):
    return "%.1f" % ((m or 0) / 60.0)


def read_day(day):
    from . import store
    return common.read_json(store.day_path(day), None) or {}


def read_ticket(key):
    from . import store
    return common.read_json(store.ticket_path(key), None)


def norm_key(key):
    """Upper-cased ticket key, or None (after saying so) when it isn't one."""
    key = (key or "").strip()
    if not KEY_RX.match(key):
        out("Not a ticket key: %r (expected something like ABC-1234)." % key)
        return None
    return key.upper()


# --------------------------------------------------------------------------- where / status

def is_set_up():
    try:
        from . import plugin
        return bool(plugin.is_set_up())
    except Exception:  # plugin.py not there (older checkout) -> the same rule, inline
        configured = bool(os.environ.get("CHITRAGUPTA_HOME")) or os.path.isfile(common.home_pointer())
        return configured and os.path.isfile(common.wl("config.json"))


def runtime_info():
    rt = os.path.join(common.user_dir(), "runtime")
    cur = common.read_text(os.path.join(rt, "current")).strip()
    return {"dir": rt, "current": cur, "launcher": common.launcher(),
            "launcher_exists": os.path.isfile(common.launcher()), "up_to_date": cur == __version__}


def cmd_where(a):
    info = {"version": __version__, "home": common.home(), "home_source": "CHITRAGUPTA_HOME" if os.environ.get(
        "CHITRAGUPTA_HOME") else ("pointer" if os.path.isfile(common.home_pointer()) else "default"),
            "user_dir": common.user_dir(), "package": common.PACKAGE_DIR, "runtime": runtime_info(),
            "set_up": is_set_up(), "paused": os.path.exists(common.wl("PAUSED"))}
    if a.json:
        out(json.dumps(info, indent=2))
        return 0
    rt = info["runtime"]
    out("Chitragupta %s" % info["version"])
    out("Set up:    %s" % ("yes" if info["set_up"] else "no - run /cg:setup"))
    out("Data home: %s (%s)" % (info["home"], info["home_source"]))
    out("User dir:  %s" % info["user_dir"])
    out("Runtime:   %s (%s)" % (rt["dir"], ("version %s" % rt["current"]) if rt["current"] else "not installed"))
    out("Launcher:  %s%s" % (rt["launcher"], "" if rt["launcher_exists"] else " (missing)"))
    out("Package:   %s" % info["package"])
    if info["paused"]:
        out("Capture is PAUSED (run /cg:pause resume).")
    return 0


def cmd_status(a):
    health = common.read_json(common.wl("health.json"), {}) or {}
    p = common.wl("PAUSED")
    if os.path.exists(p):
        since = common.read_text(p).strip().splitlines()
        out("Paused%s." % (" since " + since[0] if since and since[0] else ""))
    else:
        out("Running (not paused).")
    out("Last tick: %s" % (health.get("last_tick") or "never"))
    if not is_set_up():
        out("Not set up yet - run /cg:setup.")
    return 0


def cmd_pause(a):
    p = common.wl("PAUSED")
    if os.path.exists(p):
        out("Already paused (%s)." % p)
        return 0
    common.ensure_dir(os.path.dirname(p))
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(common.now_iso() + "\n")
    out("Paused. Hooks capture nothing and the background tick only does housekeeping until you resume.")
    return 0


def cmd_resume(a):
    p = common.wl("PAUSED")
    if not os.path.exists(p):
        out("Not paused.")
        return 0
    os.remove(p)
    out("Resumed. Capture starts again with the next Claude Code event.")
    return 0


# --------------------------------------------------------------------------- today

def day_rows(dd, store=None):
    rows = []
    for t, slot in sorted((dd.get("tickets") or {}).items(), key=lambda kv: -(kv[1].get("minutes") or 0)):
        title = ""
        if store is not None:
            title = (store.ticket(t).get("title") or "")
        rows.append((t, title, slot.get("minutes") or 0))
    other = sum((s or {}).get("minutes") or 0 for s in (dd.get("other") or {}).values())
    return rows, other


def preview(day, cfg):
    """Tonight's message as the 20:00 job would build it, without Jira or a model call (descriptions are the
    recorded lines, not condensed). Nothing is saved or sent."""
    from . import agent as agentmod
    from . import jiraworklog
    from .redact import redact
    a = agentmod.Agent()
    jwl = a.jwl
    cal = jiraworklog.load_cal()
    until = common.now() if day == today_day(cfg) else None
    res = T.compose(day, today_day(cfg), jwl.blocks(day, until), jwl.manual_for(day, cal), cal, jwl.ts,
                    describe=lambda entries: jwl.describe(entries, condense=False))
    if res.get("kind") == "message":
        return redact(res["text"], mode="public")
    return res.get("text") or "(no message for %s: %s)" % (day, res.get("reason"))


def cmd_today(a):
    cfg = common.load_config()
    day = a.date or today_day(cfg)
    from .store import Store
    st = Store()
    dd = read_day(day)
    rows, other = day_rows(dd, st)
    total = sum(m for _, _, m in rows) + other
    wd = T.WEEKDAYS[_dt.date.fromisoformat(day).weekday()]
    out("# %s (%s) - about %s tracked%s" % (day, wd, common.fmt_minutes(total),
                                           " so far" if day == today_day(cfg) else ""))
    out("_From Claude Code activity; approximate. Updated %s._" % (dd.get("updated_at") or "never"))
    out()
    if rows or other:
        out("| Ticket | Title | ≈ Time |")
        out("|---|---|---|")
        for t, title, m in rows:
            out("| %s | %s | %s |" % (t, title.replace("|", "/"), common.fmt_minutes(m)))
        if other:
            out("| (no ticket) | | %s |" % common.fmt_minutes(other))
    else:
        out("_Nothing tracked yet._")
    cal = common.read_json(common.hp("data", "calendar.json"), {}) or {}
    manual = (cal.get("time") or {}).get(day) or []
    if manual:
        out()
        out("Logged by hand (time: lines):")
        for m in manual:
            out("- %s %s%s %s" % (m.get("ticket"), T.jira_dur(m.get("minutes") or 0),
                                  (" at " + m["start"]) if m.get("start") else "", m.get("text") or ""))
    kind = T.day_kind(day, cal, T.settings(cfg))
    if kind != "workday":
        out()
        out("Calendar: %s." % T.TAGS[kind])
    if a.no_preview:
        return 0
    out()
    ts = T.settings(cfg)
    if not ts.get("enabled", True):
        out("Jira worklog is off (jira_worklog.enabled = false).")
        return 0
    out("## Jira worklog preview (sent at %s; nothing is sent now)" % ts.get("send_at", "20:00"))
    out()
    try:
        out(preview(day, cfg))
    except Exception as ex:  # a preview problem must not hide the time table
        out("(preview unavailable: %s)" % ex)
    return 0


# --------------------------------------------------------------------------- ticket / trace

def ticket_files(key):
    h = common.home()
    cands = [("README", ("tickets", key, "README.md")), ("requests", ("tickets", key, "requests.md")),
             ("replay", ("tickets", key, "replay.sh")), ("bruno", ("bruno", key)),
             ("db-flow", ("tickets", key, "db-flow.md")), ("queries", ("tickets", key, "queries.md")),
             ("artifacts", ("tickets", key, "artifacts", "INDEX.md")), ("data", ("data", "tickets", key + ".json")),
             ("jira", ("data", "jira", key + ".json")), ("qa-trace", ("data", "qa-trace", key + ".json"))]
    return [(name, os.path.join(h, *parts)) for name, parts in cands if os.path.exists(os.path.join(h, *parts))]


def ticket_summary(key, tk):
    from . import render
    items = list((tk.get("items") or {}).values())
    closed = [i for i in items if i.get("kind") not in render.OPEN_KINDS]
    opens = [i for i in items if i.get("kind") in render.OPEN_KINDS]
    rcs = sorted([i for i in items if i.get("kind") == "root_cause"], key=lambda i: i.get("date") or "", reverse=True)
    timeline = sorted((tk.get("timeline") or {}).items(), reverse=True)
    return {"key": key, "title": tk.get("title") or "", "status": tk.get("status") or "", "first": tk.get("first") or "",
            "last": tk.get("last") or "", "minutes": render.total_minutes(tk), "tldr": tk.get("tldr") or "",
            "findings": len(closed), "open": len(opens), "queries": len(tk.get("queries") or {}),
            "apis": len(tk.get("apis") or {}), "requests": len((tk.get("replay") or {}).get("final") or []),
            "dbflow_steps": len(tk.get("data_flow") or {}), "artifacts": len(tk.get("artifacts") or {}),
            "root_causes": [i.get("text") or "" for i in rcs[:3]],
            "recent_days": [{"day": d, "minutes": (v or {}).get("minutes") or 0, "line": (v or {}).get("line") or ""}
                            for d, v in timeline[:5]]}


def cmd_ticket(a):
    key = norm_key(a.key)
    if not key:
        return 0  # informational: the /cg: skills inject this output
    tk = read_ticket(key)
    files = ticket_files(key)
    if not tk and not files:
        out("Nothing recorded for %s in %s." % (key, common.home()))
        return 0
    s = ticket_summary(key, tk or {})
    if a.json:
        s["files"] = dict(files)
        out(json.dumps(s, indent=2, ensure_ascii=False))
        return 0
    out("# %s%s" % (key, (" - " + s["title"]) if s["title"] else ""))
    out("Status (inferred): %s · first worked %s · last worked %s · ≈ %s in total" % (
        s["status"] or "unknown", s["first"] or "-", s["last"] or "-", common.fmt_minutes(s["minutes"])))
    if s["tldr"]:
        out()
        out("TL;DR: " + s["tldr"])
    for rc in s["root_causes"]:
        out("- Root cause: " + rc)
    out()
    out("Recorded: %d findings, %d open items, %d queries, %d APIs, %d saved requests, %d DB-flow steps, %d artifacts" % (
        s["findings"], s["open"], s["queries"], s["apis"], s["requests"], s["dbflow_steps"], s["artifacts"]))
    if s["recent_days"]:
        out()
        out("Recent days:")
        for d in s["recent_days"]:
            out("- %s · %s · %s" % (d["day"], common.fmt_minutes(d["minutes"]), d["line"]))
    out()
    out("Files:")
    for name, p in files:
        out("- %s: %s" % (name, p))
    return 0


def database_configured(cfg):
    from . import mcpjobs
    return (mcpjobs.configured(cfg) and mcpjobs.enabled(cfg, "database")
            and bool(((cfg.get("mcp") or {}).get("database") or {}).get("server")))


def cmd_trace(a):
    key = norm_key(a.key)
    if not key:
        return 0
    cfg = common.load_config()
    p = common.hp("tickets", key, "db-flow.md")
    if os.path.isfile(p):
        out("<!-- %s -->" % p)
        out(common.read_text(p).rstrip())
    else:
        out("No DB flow recorded for %s yet. Ask Claude to \"map the DB flow for %s\" and it is filed from the reply." % (
            key, key))
    out()
    out("## QA worked example")
    qa = common.read_json(common.hp("data", "qa-trace", key + ".json"), None)
    if qa:
        tr = qa.get("trace") or {}
        out("Fetched %s (%s, connection %s)." % (qa.get("fetched_at") or "?", qa.get("env") or "qa",
                                                qa.get("connection") or "?"))
        if tr.get("summary"):
            out(tr["summary"])
        for s in tr.get("steps") or []:
            out("- `%s` where %s: %s%s" % (s.get("table"), s.get("key"), s.get("values"),
                                           (" (%s)" % s["note"]) if s.get("note") else ""))
        for c in tr.get("changes") or []:
            out("- changed `%s.%s`: %s -> %s" % (c.get("table"), c.get("column"), c.get("before"), c.get("after")))
    else:
        out("No QA worked example saved for %s." % key)
    out()
    if database_configured(cfg):
        out("database_mcp: configured (%s) - `agent qa-trace %s` can fetch a fresh QA example (read-only SELECTs)." % (
            cfg["mcp"]["database"]["server"], key))
    else:
        out("database_mcp: not configured - QA examples need a database MCP (see /cg:setup).")
    return 0


# --------------------------------------------------------------------------- hours

def iso_week(day):
    y, w, _ = _dt.date.fromisoformat(day).isocalendar()
    return "%d-W%02d" % (y, w)


def period_days(mode, day):
    d = _dt.date.fromisoformat(day)
    if mode == "month":
        first = d.replace(day=1)
        nxt = (first + _dt.timedelta(days=32)).replace(day=1)
        n = (nxt - first).days
    else:
        first = d - _dt.timedelta(days=d.weekday())
        n = 7
    return [(first + _dt.timedelta(days=i)).isoformat() for i in range(n)]


def hours_table(mode, day):
    """-> (columns, rows [(ticket, title, {col: minutes}, total)], calendar notes, manual entries)."""
    from .store import Store
    st = Store()
    days = period_days(mode, day)
    col_of = (lambda d: iso_week(d)) if mode == "month" else (lambda d: d)
    cols = []
    for d in days:
        if col_of(d) not in cols:
            cols.append(col_of(d))
    per, other = {}, {}
    for d in days:
        dd = read_day(d)
        for t, slot in (dd.get("tickets") or {}).items():
            m = (slot or {}).get("minutes") or 0
            if m:
                per.setdefault(t, {})[col_of(d)] = per.get(t, {}).get(col_of(d), 0) + m
        o = sum((s or {}).get("minutes") or 0 for s in (dd.get("other") or {}).values())
        if o:
            other[col_of(d)] = other.get(col_of(d), 0) + o
    rows = [(t, st.ticket(t).get("title") or "", v, sum(v.values())) for t, v in per.items()]
    rows.sort(key=lambda r: (-r[3], r[0]))
    if other:
        rows.append(("(no ticket)", "", other, sum(other.values())))
    cal = common.read_json(common.hp("data", "calendar.json"), {}) or {}
    notes = []
    for key, label in (("leave", "leave"), ("half_day", "half day"), ("holiday", "holiday"), ("workday", "working day")):
        for d in sorted(cal.get(key) or {}):
            if d in days:
                name = (cal[key] or {}).get(d)
                notes.append((d, label + ((" " + name) if isinstance(name, str) and name else "")))
    manual = [(d, m) for d in days for m in ((cal.get("time") or {}).get(d) or [])]
    return cols, rows, sorted(notes), manual


def cmd_hours(a):
    cfg = common.load_config()
    mode = "month" if a.month else "week"
    day = a.date or today_day(cfg)
    cols, rows, notes, manual = hours_table(mode, day)
    if mode == "week":
        heads = ["%s %s" % (T.WEEKDAYS[_dt.date.fromisoformat(c).weekday()], c[8:]) for c in cols]
        label = "week %s (%s to %s)" % (iso_week(day), cols[0], cols[-1])
    else:
        heads = cols
        label = "month %s" % day[:7]
    if a.csv:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["ticket", "title"] + cols + ["total_hours"])
        for t, title, v, tot in rows:
            w.writerow([t, title] + [("%.2f" % (v[c] / 60.0)) if v.get(c) else "" for c in cols] + ["%.2f" % (tot / 60.0)])
        sys.stdout.write(buf.getvalue())
        return 0
    out("# Hours per ticket, %s" % label)
    out("_Approximate: from Claude Code activity (data/days). Hours, one decimal._")
    out()
    if not rows:
        out("_Nothing tracked in this period._")
    else:
        out("| Ticket | Title | %s | Total |" % " | ".join(heads))
        out("|---|---|%s---|" % ("---|" * len(cols)))
        for t, title, v, tot in rows:
            out("| %s | %s | %s | %s |" % (t, title.replace("|", "/")[:60],
                                         " | ".join(hours(v[c]) if v.get(c) else "" for c in cols), hours(tot)))
        totals = {c: sum(r[2].get(c, 0) for r in rows) for c in cols}
        out("| **Total** | | %s | %s |" % (" | ".join(hours(totals[c]) if totals[c] else "" for c in cols),
                                          hours(sum(totals.values()))))
    if notes:
        out()
        out("Calendar: " + "; ".join("%s %s" % (d, lab) for d, lab in notes))
    if manual:
        out()
        out("Logged by hand (time: lines, not in the table):")
        for d, m in manual:
            out("- %s %s %s %s" % (d, m.get("ticket"), T.jira_dur(m.get("minutes") or 0), m.get("text") or ""))
    return 0


# --------------------------------------------------------------------------- log

def normalize_directive(words):
    line = " ".join(w for w in words if w is not None).strip()
    line = re.sub(r"^\s*[-*]\s*", "", line)
    m = re.match(r"^(time|leave|half-day|holiday|workday|resend)\b\s*:?\s*", line, re.I)
    if m:  # "leave 2026-10-20" -> "leave: 2026-10-20"
        line = "%s: %s" % (m.group(1).lower(), line[m.end():].strip())
    return line.strip()


def validate_directive(line, today):
    """-> (parsed, error)."""
    if "\n" in line or "\r" in line:
        return None, "one line only"
    try:
        d = T.parse_directive(line, today)
    except ValueError as ex:  # e.g. 2026-02-30
        return None, "bad date: %s" % ex
    if not d:
        return None, "not a worklog directive"
    if d["kind"] == "invalid":
        return None, "could not read %r" % line
    if d["kind"] == "time" and not d.get("text"):
        return None, "time: needs a short description of what you did"
    if d["kind"] == "time" and d.get("start"):
        hh, mm = map(int, d["start"].split(":"))
        if hh > 23 or mm > 59:
            return None, "bad start time %s" % d["start"]
    return d, None


def cmd_log(a):
    cfg = common.load_config()
    today = today_day(cfg)
    if a.today:  # for resolving relative dates: the current work day
        out("%s (%s); a work day starts at %s" % (today, T.WEEKDAYS[_dt.date.fromisoformat(today).weekday()],
                                                  cfg.get("day_starts_at") or "04:00"))
        return 0
    line = normalize_directive(a.directive)
    d, err = validate_directive(line, today)
    if err:
        sys.stderr.write("Not added: %s.\n%s\n" % (err, GRAMMAR))
        return 2
    path = common.hp("inbox.md")
    existing = common.read_text(path)
    if any(l.strip() == line for l in existing.splitlines()):
        out("Already in inbox.md: `%s` (the agent ignores repeated lines, so nothing was added)." % line)
        return 0
    common.ensure_dir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(line + "\n")
    out("Added to inbox.md: `%s`" % line)
    out("The background agent applies it on its next tick (within about 5 minutes).")
    from . import jiraworklog
    for day in d["days"]:
        sh = jiraworklog.load_sheet(day)
        age = (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(day)).days
        if d["kind"] != "resend" and sh.get("status") in ("sent", "nudged") and 0 <= age <= 3:
            out("%s was already messaged, so a \"Corrected update\" follows about 15 minutes later." % day)
    return 0


# --------------------------------------------------------------------------- main

def parser():
    p = argparse.ArgumentParser(prog="chitragupta cli", description="Chitragupta commands (read-only except log/pause).")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("where")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_where)
    s = sub.add_parser("today")
    s.add_argument("--date")
    s.add_argument("--no-preview", action="store_true")
    s.set_defaults(fn=cmd_today)
    s = sub.add_parser("ticket")
    s.add_argument("key", nargs="?", default="")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ticket)
    s = sub.add_parser("trace")
    s.add_argument("key", nargs="?", default="")
    s.set_defaults(fn=cmd_trace)
    s = sub.add_parser("hours")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--week", action="store_true")
    g.add_argument("--month", action="store_true")
    s.add_argument("--date")
    s.add_argument("--csv", action="store_true")
    s.set_defaults(fn=cmd_hours)
    s = sub.add_parser("log")
    s.add_argument("--today", action="store_true")
    s.add_argument("directive", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_log)
    for name, fn in (("pause", cmd_pause), ("resume", cmd_resume), ("status", cmd_status)):
        sub.add_parser(name).set_defaults(fn=fn)
    return p


def _check_date(s):
    if s:
        try:
            _dt.date.fromisoformat(s)
        except ValueError:
            raise SystemExit("bad date %r (use YYYY-MM-DD)" % s)


def main(argv):
    a = parser().parse_args(argv)
    _check_date(getattr(a, "date", None))
    return a.fn(a)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv[1:]))
