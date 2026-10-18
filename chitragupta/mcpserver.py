"""Chitragupta's read-only MCP server: Claude Code asks the worklog through 8 tools instead of reading files.

Started by Claude Code from the plugin's .mcp.json (bin/cg-mcp → hooks/run.py mcp). Transport: stdio,
newline-delimited JSON-RPC 2.0; stdout carries protocol messages only (anything else printed while serving goes to
stderr). Methods: initialize, notifications/initialized, ping, tools/list, tools/call.

Read-only by construction: it reads the data home (common.home()) through the dashboard's read functions, never
writes (except its own error log, through the path guard), never calls a model, opens no connection and starts no
process. Inside Chitragupta's own background `claude -p` calls (CHITRAGUPTA_INTERNAL=1) it lists no tools.

Every tool answers in compact Markdown with data-home file paths for drill-down, cut into pages of at most
MAX_CHARS characters ("more: page=N").
"""
import datetime as _dt
import io
import json
import os
import sys

from . import __version__
from . import common

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = PROTOCOLS[0]
MAX_CHARS = 8000
LIST_LIMIT = 40  # search results per call
INSTRUCTIONS = ("Chitragupta is the user's automatic worklog: tickets (findings, requests, queries, DB flows, "
                "branches), days, timesheets and a database/API knowledge base. Use these tools before reading "
                "worklog files; open the file paths they return only for more detail.")

SECTIONS = ("findings", "requests", "queries", "dbflow", "branches", "artifacts", "timeline", "notes")
SEARCH_TYPES = ("ticket", "finding", "query", "artifact", "api", "table", "integration", "day", "request", "branch")
PERIODS = ("week", "month", "day")


class ToolError(Exception):
    """A tool call that can't be answered (bad argument, unknown ticket): returned as isError, never logged."""


# --------------------------------------------------------------------------- tool list

def _s(desc=None, enum=None, kind="string"):
    p = {"type": kind}
    if enum:
        p["enum"] = list(enum)
    if desc:
        p["description"] = desc
    return p


_PAGE = _s(kind="integer")
_DATE = "YYYY-MM-DD, today, yesterday or -N"

TOOLS = [
    {"name": "search",
     "description": "Full-text search over the worklog: tickets, findings, queries, artifacts, APIs, tables, days. "
                    "Matches with file paths.",
     "inputSchema": {"type": "object", "properties": {
         "q": _s(), "type": _s(enum=SEARCH_TYPES), "ticket": _s(), "repo": _s(), "from": _s(_DATE), "to": _s(_DATE),
         "limit": _s(kind="integer"), "page": _PAGE}, "required": ["q"]}},
    {"name": "get_ticket",
     "description": "One ticket: summary and counts; `section` returns one part in full.",
     "inputSchema": {"type": "object", "properties": {
         "key": _s("e.g. ABC-1234"), "section": _s(enum=SECTIONS), "page": _PAGE}, "required": ["key"]}},
    {"name": "get_day",
     "description": "One work day: time per ticket, commits, the evening Jira worklog (as sent, or a preview).",
     "inputSchema": {"type": "object", "properties": {"date": _s(_DATE), "page": _PAGE}, "required": ["date"]}},
    {"name": "get_timesheet",
     "description": "Hours per ticket per week, month or day (default range: the current period).",
     "inputSchema": {"type": "object", "properties": {
         "period": _s(enum=PERIODS), "from": _s(_DATE), "to": _s(_DATE), "page": _PAGE}, "required": ["period"]}},
    {"name": "get_branch_status",
     "description": "A ticket's branches, PRs, delivery to each target branch, and alerts.",
     "inputSchema": {"type": "object", "properties": {"key": _s(), "repo": _s(), "page": _PAGE}, "required": ["key"]}},
    {"name": "get_db_table",
     "description": "A database table from the knowledge base: columns, links, code that writes it, tickets, queries.",
     "inputSchema": {"type": "object", "properties": {"name": _s("table or schema.table"), "page": _PAGE},
                     "required": ["name"]}},
    {"name": "list_tickets",
     "description": "Known tickets, newest activity first, one line each.",
     "inputSchema": {"type": "object", "properties": {
         "q": _s(), "status": _s(), "repo": _s(), "since": _s(_DATE), "page": _PAGE}}},
    {"name": "get_alerts",
     "description": "What needs attention: branch/PR delivery alerts and worklog health warnings.",
     "inputSchema": {"type": "object", "properties": {"page": _PAGE}}},
]
TOOL_NAMES = [t["name"] for t in TOOLS]


# --------------------------------------------------------------------------- helpers

def _cfg():
    return common.load_config()


def _home(*parts):
    return common.hp(*parts)


def _arg(args, name, required=False, kind=str):
    v = args.get(name)
    if v is None or v == "":
        if required:
            raise ToolError("`%s` is required" % name)
        return None
    if kind is int:
        if isinstance(v, bool):
            raise ToolError("`%s` must be a number" % name)
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ToolError("`%s` must be a number" % name)
    if not isinstance(v, str):
        raise ToolError("`%s` must be a string" % name)
    return v.strip()


def parse_date(s, cfg, name="date"):
    """YYYY-MM-DD | today | yesterday | -N (N work-day-calendar days back) -> ISO date."""
    s = (s or "").strip().lower()
    today = _dt.date.fromisoformat(common.workday(common.now(), cfg))
    if s in ("", "today", "0", "-0"):
        return today.isoformat()
    if s == "yesterday":
        return (today - _dt.timedelta(days=1)).isoformat()
    if s.startswith("-") and s[1:].isdigit() and len(s) <= 5:
        return (today - _dt.timedelta(days=int(s[1:]))).isoformat()
    try:
        return _dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise ToolError("`%s` must be %s, got %r" % (name, _DATE, s[:40]))


def _key(s):
    from .dashboard import api
    k = (s or "").strip().upper()
    if not api.TICKET_RE.match(k):
        raise ToolError("`key` must be a ticket key like ABC-1234, got %r" % (s or "")[:40])
    return k


def _hm(minutes):
    return common.fmt_minutes(int(minutes or 0)) if minutes else "0m"


def _one(s, n=200):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _cell(s, n=120):
    return _one(s, n).replace("|", "\\|")


def paginate(text, page=1, size=MAX_CHARS):
    """Cut Markdown into pages of <= size characters at line ends; -> (page text, pages)."""
    pages, cur, n = [], [], 0
    budget = size - 60  # room for the footer
    for line in text.split("\n"):
        while len(line) > budget:  # one huge line: hard cut
            if cur:
                pages.append("\n".join(cur))
                cur, n = [], 0
            pages.append(line[:budget])
            line = line[budget:]
        if n + len(line) + 1 > budget and cur:
            pages.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line)
        n += len(line) + 1
    if cur or not pages:
        pages.append("\n".join(cur))
    total = len(pages)
    page = max(1, int(page or 1))
    if page > total:
        return "No page %d: this answer has %d page(s)." % (page, total), total
    body = pages[page - 1].rstrip("\n")
    if page < total:
        body += "\n\n… more: page=%d (of %d)" % (page + 1, total)
    elif total > 1:
        body += "\n\n(page %d of %d)" % (page, total)
    return body, total


# --------------------------------------------------------------------------- search

def _search_backend():
    """The persistent index (chitragupta.searchidx) when installed, else the dashboard's in-memory search."""
    try:
        from . import searchidx
        return searchidx, True
    except ImportError:
        from .dashboard import search as dsearch
        return dsearch, False


def run_search(q, types=None, ticket=None, repo=None, frm=None, to=None, limit=LIST_LIMIT):
    """searchidx.search's contract on either backend (the fallback filters ticket and dates itself; it has no repo)."""
    mod, full = _search_backend()
    if full:
        return mod.search(q, types=types, ticket=ticket, repo=repo, frm=frm, to=to, limit=limit)
    filtered = bool(ticket or frm or to)
    res = mod.search(q, types=types, limit=200 if filtered else limit)
    if filtered:
        kept = 0
        groups = []
        for g in res.get("groups") or []:
            rs = [r for r in g["results"] if (not ticket or (r.get("ticket") or "").upper() == ticket.upper())
                  and (not frm or (r.get("date") or "") >= frm) and (not to or (r.get("date") or "9999") <= to)]
            rs = rs[:max(0, limit - kept)]
            kept += len(rs)
            if rs:
                groups.append(dict(g, results=rs, count=len(rs)))
        res = dict(res, groups=groups, total=kept)
    if repo:
        res = dict(res, note="repo filter needs the search index (not built here); results are not filtered by repo")
    return res


def _result_path(r):
    """Absolute file to read for a result: its `path` (data-home-relative) or one derived from type and ref."""
    if r.get("path"):
        return _home(*str(r["path"]).replace("\\", "/").split("/"))
    t, ref, key = r.get("type"), str(r.get("ref") or ""), r.get("ticket") or ""
    if t == "day" and len(ref) == 10:
        return _home("daily", ref[:4], ref[5:7], ref + ".md")
    if t in ("ticket", "finding", "request", "branch") and key:
        return _home("tickets", key, "README.md")
    if t == "query" and key:
        return _home("tickets", key, "queries.md")
    if t == "artifact" and key:
        return _home("tickets", key, "artifacts", "INDEX.md")
    if t == "table":
        return _home("knowledge", "db-tables.md")
    if t == "api":
        return _home("knowledge", "apis.md")
    if t == "integration":
        return _home("knowledge", "integrations.md")
    return ""


def tool_search(args, cfg):
    q = _arg(args, "q", True)
    t = _arg(args, "type")
    if t and t not in SEARCH_TYPES:
        raise ToolError("`type` must be one of %s" % ", ".join(SEARCH_TYPES))
    ticket = _arg(args, "ticket")
    frm = parse_date(_arg(args, "from"), cfg, "from") if _arg(args, "from") else None
    to = parse_date(_arg(args, "to"), cfg, "to") if _arg(args, "to") else None
    limit = max(1, min(100, _arg(args, "limit", kind=int) or LIST_LIMIT))
    res = run_search(q, [t] if t else None, ticket.upper() if ticket else None, _arg(args, "repo"), frm, to, limit)
    total = res.get("total") or 0
    L = ['# Search "%s" — %d match%s' % (_one(q, 80), total, "" if total == 1 else "es")]
    if res.get("note"):
        L.append("_%s_" % res["note"])
    for g in res.get("groups") or []:
        L += ["", "## %s (%d)" % (g.get("label") or g.get("type"), g.get("count") or len(g["results"]))]
        for r in g["results"]:
            meta = " · ".join(x for x in (r.get("ticket") if r.get("ticket") != r.get("title") else "", r.get("repo"),
                                          r.get("date")) if x)
            L.append("- **%s**%s — %s" % (_one(r.get("title"), 120), " · " + meta if meta else "", _one(r.get("snippet"), 220)))
            p = _result_path(r)
            if p:
                L.append("  %s" % p)
    if not total:
        L.append("\nNo matches. Try fewer or shorter words, or list_tickets(q=…).")
    return "\n".join(L)


# --------------------------------------------------------------------------- tickets

def _ticket_record(key):
    from .dashboard import api
    return api.read_json_cached(_home("data", "tickets", key + ".json"))


def _summary_lines(t):
    c = t["counts"]
    L = ["# %s — %s" % (t["key"], t["title"] or "(no title)"),
         " · ".join(x for x in ("status " + t["status"] if t["status"] else "", "first %s" % t["first"] if t["first"] else "",
                                "last %s" % t["last"] if t["last"] else "", "time " + _hm(t["minutes"]),
                                "repos " + ", ".join(t["repos"]) if t["repos"] else "") if x)]
    if t.get("tldr"):
        L += ["", "TL;DR: " + _one(t["tldr"], 600)]
    if t.get("problem"):
        L += ["Problem: " + _one(t["problem"], 600)]
    L += ["", "Counts: %d findings · %d requests · %d queries · %d DB-flow steps · %d artifacts"
          % (c["findings"], c["requests"], c["queries"], c["dbflow"], c["artifacts"])]
    if t.get("branches"):
        L.append("Branches: " + ", ".join(t["branches"][:8]))
    for a in t.get("alerts") or []:
        L.append("⚠ %s%s" % (a["text"], " (%s)" % a["repo"] if a.get("repo") else ""))
    return L


def _notes(readme):
    from .render import NOTES_START, NOTES_END
    if NOTES_START in readme and NOTES_END in readme:
        return readme.split(NOTES_START, 1)[1].split(NOTES_END, 1)[0].strip()
    return ""


def _section(key, t, tk, section):
    p = t["paths"]
    L = ["", "## %s" % section.capitalize()]
    if section == "findings":
        for f in t["findings"]:
            L.append("- [%s] **%s**%s%s" % (f.get("kind"), _one(f.get("title"), 200),
                                           " (%s)" % f["confidence"] if f.get("confidence") else "",
                                           " · %s" % f["date"] if f.get("date") else ""))
            if f.get("detail"):
                L.append("  " + _one(f["detail"], 700))
            ev = f.get("evidence")
            if ev:
                L.append("  evidence: " + _one(", ".join(ev) if isinstance(ev, list) else ev, 300))
        L.append("\nFile: %s" % p.get("readme", _home("tickets", key, "README.md")))
    elif section == "requests":
        r = t["requests"]
        for s in r["steps"]:
            L.append("%s. %s — %s %s%s%s" % (s.get("n"), _one(s.get("name"), 100), (s.get("method") or "").upper(),
                                            _one(s.get("url"), 200), " → expect " + _one(s["expected"], 80) if s.get("expected") else "",
                                            " (not run)" if s.get("executed") is False else ""))
        if not r["steps"]:
            L.append("No saved requests.")
        L.append("\n%d calls captured, %d dropped." % (r.get("captured") or 0, r.get("dropped") or 0))
        for k, label in (("requests", "curl commands"), ("replay", "replay script"), ("bruno", "Bruno folder")):
            if p.get(k):
                L.append("%s: %s" % (label, p[k]))
    elif section == "queries":
        for q in t["queries"]:
            L.append("- %s%s · %s%s" % (_one(q.get("purpose") or "query", 160), " (%s)" % q["dialect"] if q.get("dialect") else "",
                                       q.get("date") or "", " · tables " + ", ".join(q["tables"][:8]) if q.get("tables") else ""))
            L += ["```sql", (q.get("text") or "").strip()[:1500], "```"]
        if not t["queries"]:
            L.append("No saved queries.")
        if p.get("queries"):
            L.append("File: %s" % p["queries"])
    elif section == "dbflow":
        md = (t["dbflow"] or {}).get("markdown") or ""
        L.append(md.strip() if md else "No DB flow yet.")
        if p.get("dbflow"):
            L.append("\nFile: %s" % p["dbflow"])
    elif section == "branches":
        from . import cli
        L = [""] + cli.branches_report(key, tk)
    elif section == "artifacts":
        for a in t["artifacts"]:
            L.append("- %s · %s%s%s" % (a.get("name"), a.get("kind") or "", " · " + _one(a["purpose"], 160) if a.get("purpose") else "",
                                       " · " + (a.get("last") or a.get("date") or "")))
            if a.get("path"):
                L.append("  %s" % a["path"])
        if not t["artifacts"]:
            L.append("No artifacts.")
        if p.get("artifacts"):
            L.append("Index: %s" % p["artifacts"])
    elif section == "timeline":
        for d in t["timeline"]:
            L.append("- %s · %s%s" % (d["day"], _hm(d["minutes"]), " · " + _one(d["line"], 200) if d.get("line") else ""))
    elif section == "notes":
        n = _notes(t.get("readme") or "")
        L.append(n or "No notes (write them between the my-notes markers in the ticket README).")
        if p.get("readme"):
            L.append("\nFile: %s" % p["readme"])
    return L


def tool_get_ticket(args, cfg):
    from .dashboard import api
    key = _key(_arg(args, "key", True))
    section = _arg(args, "section")
    if section and section not in SECTIONS:
        raise ToolError("`section` must be one of %s" % ", ".join(SECTIONS))
    t = api.ticket(key)
    if t is None:
        raise ToolError("No ticket %s in the worklog (list_tickets shows the known ones)." % key)
    L = _summary_lines(t)
    if section:
        L += _section(key, t, _ticket_record(key) or {}, section)
    else:
        L += ["", "Sections (get_ticket section=…): " + ", ".join(SECTIONS),
              "Folder: %s" % t["paths"].get("folder", _home("tickets", key))]
    return "\n".join(L)


def tool_list_tickets(args, cfg):
    from .dashboard import api
    q, status, repo = _arg(args, "q"), _arg(args, "status"), _arg(args, "repo")
    since = parse_date(_arg(args, "since"), cfg, "since") if _arg(args, "since") else None
    ts = api.tickets(q)["tickets"]
    if status:
        ts = [t for t in ts if status.lower() in (t["status"] or "").lower()]
    if repo:
        ts = [t for t in ts if any(repo.lower() == r.lower() for r in t["repos"])]
    if since:
        ts = [t for t in ts if (t["last"] or "") >= since]
    L = ["# Tickets — %d" % len(ts)]
    for t in ts:
        L.append("- %s · %s · %s · last %s · %s%s%s" % (
            t["key"], _one(t["title"] or "(no title)", 90), t["status"] or "-", t["last"] or "-", _hm(t["minutes"]),
            " · " + ", ".join(t["repos"]) if t["repos"] else "", " · ⚠ %d alert(s)" % len(t["alerts"]) if t["alerts"] else ""))
    if not ts:
        L.append("No tickets match.")
    else:
        L.append("\nDetail: get_ticket(key=…). Files: %s" % _home("tickets", "<KEY>", "README.md"))
    return "\n".join(L)


# --------------------------------------------------------------------------- days and timesheets

def tool_get_day(args, cfg):
    from .dashboard import api
    day = parse_date(_arg(args, "date", True), cfg)
    d = api.day(day, cfg, preview=False)
    L = ["# %s — %s tracked%s%s" % (day, _hm(d["total_minutes"]), " · " + d["day_kind"] if d.get("day_kind") else "",
                                    " · %d session(s)" % d["sessions"] if d.get("sessions") else "")]
    if d.get("summary"):
        L += ["", _one(d["summary"], 800)]
    if d["tickets"]:
        L += ["", "| Ticket | Title | Time | Did |", "|---|---|---|---|"]
        for r in d["tickets"]:
            L.append("| %s | %s | %s | %s |" % (r["key"], _cell(r["title"], 70), _hm(r["minutes"]), _cell("; ".join(x for x in r["did"] if x), 200)))
    for o in d["other"]:
        L.append("- other: %s · %s" % (_one(o["group"], 80), _hm(o["minutes"])))
    if d["commit_list"]:
        L += ["", "Commits:"] + ["- %s %s %s%s · %s" % (c["time"], c["repo"], c["short"], " [%s]" % c["ticket"] if c["ticket"] else "",
                                                        _one(c["subject"], 120)) for c in d["commit_list"][:40]]
    saved = d.get("worklog_saved")
    if saved:
        L += ["", "## Jira worklog — %s%s" % (saved.get("status") or "saved", " at " + saved["sent_at"] if saved.get("sent_at") else "")]
        for e in saved.get("entries") or []:
            L.append("- %s %s %s · %s" % (e["start"], e["ticket"], e["duration"], _one(e["description"], 160)))
        if saved.get("text"):
            L += ["", saved["text"].strip()]
    else:
        pv = api.worklog_preview(day, cfg)
        L += ["", "## Jira worklog — not sent (preview: %s)" % (pv.get("kind") or "?")]
        for e in pv.get("entries") or []:
            L.append("- %s %s %s · %s" % (e["start"], e["ticket"], e["duration"], _one(e["description"], 160)))
        if pv.get("reason"):
            L.append(_one(pv["reason"], 300))
    p = _home("daily", day[:4], day[5:7], day + ".md")
    L.append("\nDaily file: %s" % (p if os.path.exists(p) else "(none yet)"))
    return "\n".join(L)


def _period_of(day, period):
    from .dashboard import api
    return day if period == "day" else api._period(day, period)


def tool_get_timesheet(args, cfg):
    from .dashboard import api
    period = _arg(args, "period", True)
    if period not in PERIODS:
        raise ToolError("`period` must be one of %s" % ", ".join(PERIODS))
    today = parse_date("today", cfg)
    frm = parse_date(_arg(args, "from"), cfg, "from") if _arg(args, "from") else None
    to = parse_date(_arg(args, "to"), cfg, "to") if _arg(args, "to") else today
    if not frm:  # the current period
        td = _dt.date.fromisoformat(to)
        frm = {"day": td, "week": td - _dt.timedelta(days=td.weekday()), "month": td.replace(day=1)}[period].isoformat()
    if frm > to:
        raise ToolError("`from` is after `to`")
    per = {}
    for day, dd in api.all_days():
        if not frm <= day <= to:
            continue
        b = per.setdefault(_period_of(day, period), {})
        for key, slot in ((dd or {}).get("tickets") or {}).items():
            b[key] = b.get(key, 0) + int((slot or {}).get("minutes") or 0)
        other = sum(int((v or {}).get("minutes") or 0) for v in ((dd or {}).get("other") or {}).values())
        if other:
            b[""] = b.get("", 0) + other
    cal_rows, manual = api.calendar_days()
    for m in manual:  # time logged by hand counts too
        if m.get("ticket") and frm <= (m.get("date") or "") <= to:
            b = per.setdefault(_period_of(m["date"], period), {})
            b[m["ticket"]] = b.get(m["ticket"], 0) + int(m.get("minutes") or 0)
    periods = sorted(per)
    keys = sorted({k for p in periods for k in per[p]}, key=lambda k: (k == "", k))
    rows = [(k, {p: per[p].get(k, 0) for p in periods}) for k in keys]
    rows.sort(key=lambda r: (r[0] == "", -sum(r[1].values()), r[0]))
    L = ["# Timesheet %s → %s (per %s, hours)" % (frm, to, period)]
    if not periods:
        L.append("No tracked time in this range.")
    else:
        L += ["", "| Ticket | Title | %s | Total |" % " | ".join(periods), "|---|---|%s---|" % ("---|" * len(periods))]
        for k, cells in rows:
            L.append("| %s | %s | %s | %.2f |" % (k or "(no ticket)", _cell(api._title(k), 50) if k else "Other work",
                                                " | ".join("%.2f" % (cells[p] / 60.0) if cells[p] else "" for p in periods),
                                                sum(cells.values()) / 60.0))
        L.append("| **Total** | | %s | %.2f |" % (" | ".join("%.2f" % (sum(per[p].values()) / 60.0) for p in periods),
                                                 sum(sum(per[p].values()) for p in periods) / 60.0))
    cal = [c for c in cal_rows if frm <= c["date"] <= to]
    if cal:
        L += ["", "Calendar: " + ", ".join("%s %s%s" % (c["date"], c["kind"].replace("_", " "), " (%s)" % c["name"] if c["name"] else "")
                                           for c in sorted(cal, key=lambda c: c["date"]))]
    L.append("\nDay files: %s" % _home("data", "days", "<YYYY>", "<date>.json"))
    return "\n".join(L)


# --------------------------------------------------------------------------- branches, tables, alerts

def tool_get_branch_status(args, cfg):
    from . import cli
    key = _key(_arg(args, "key", True))
    repo = _arg(args, "repo")
    tk = _ticket_record(key)
    if not tk:
        raise ToolError("No ticket %s in the worklog (list_tickets shows the known ones)." % key)
    if repo:
        g = dict(tk.get("git") or {})
        repos = {n: x for n, x in (g.get("repos") or {}).items() if n.lower() == repo.lower()}
        if not repos:
            raise ToolError("No branch data for repo %r on %s (repos: %s)" % (
                repo, key, ", ".join(sorted((g.get("repos") or {}))) or "none"))
        g["repos"] = repos
        g["alerts"] = [a for a in g.get("alerts") or [] if not a.get("repo") or a.get("repo", "").lower() == repo.lower()]
        tk = dict(tk, git=g)
    return "\n".join(cli.branches_report(key, tk))


def _match_tables(name, tables):
    n = name.lower().strip("` ")
    exact = [t for t in tables if t["name"].lower() == n]
    if exact:
        return exact
    tail = [t for t in tables if t["name"].lower().rsplit(".", 1)[-1] == n.rsplit(".", 1)[-1]
            and (("." not in n) or t["name"].lower().endswith(n))]
    return tail


def _ratio_status(s, m):
    return "verified" if s and m / s >= 0.8 else ("likely" if s and m / s >= 0.3 else "unverified")


def tool_get_db_table(args, cfg):
    from .dashboard import api
    name = _arg(args, "name", True)
    kb = api.knowledge()
    hits = _match_tables(name, kb["tables"])
    if not hits:
        near = [t["name"] for t in kb["tables"] if name.lower().rsplit(".", 1)[-1] in t["name"].lower()][:15]
        raise ToolError("No table %r in the knowledge base.%s" % (name, (" Similar: " + ", ".join(near)) if near else ""))
    if len(hits) > 1:
        return "Several tables match %r: %s. Ask again with the full name." % (name, ", ".join(t["name"] for t in hits))
    t = hits[0]
    tn = t["name"].lower()
    short = tn.rsplit(".", 1)[-1]

    def involves(ref):
        parts = str(ref or "").lower().split(".")
        return tn in str(ref or "").lower() or short in parts[-2:]
    L = ["# %s%s" % (t["name"], " (%s)" % t["db"] if t.get("db") else "")]
    if t.get("purpose"):
        L.append(_one(t["purpose"], 600))
    if t.get("key_columns"):
        L.append("Key columns: " + ", ".join(t["key_columns"]))
    if t.get("columns"):
        L.append("Columns (%d): %s" % (len(t["columns"]), ", ".join(t["columns"][:150])))
    rel = [r for r in kb["relations"] if involves(r["from"]) or involves(r["to"])]
    if rel or t.get("relations"):
        L += ["", "## Relations"]
        L += ["- %s → %s%s%s" % (r["from"], r["to"], " · " + r["kind"] if r.get("kind") else "", " · verified" if r["verified"] else "")
              for r in rel[:40]]
        L += ["- " + _one(x, 160) for x in t.get("relations") or [] if not rel][:20]
    lm = api.read_json_cached(_home("data", "db-schema", "_linkmap.json")) or {}
    links = [ln for ln in ((lm.get("result") or {}).get("links") or []) if isinstance(ln, dict)
             and (involves(ln.get("from")) or involves(ln.get("to")))]
    if links:
        L += ["", "## Link map (QA sampling)"]
        for ln in links[:40]:
            s, m = int(ln.get("sampled") or 0), int(ln.get("matched") or 0)
            L.append("- %s → %s · %d/%d %s" % (ln.get("from"), ln.get("to"), m, s, _ratio_status(s, m)))
    w = next((x for x in kb["writers"] if x["table"].lower() in (tn, short)), None)
    if w:
        L += ["", "## Code that touches it"]
        L += ["- writes: %s%s%s" % (x["component"], " ." + x["column"] if x.get("column") else "",
                                    " — " + _one(x["change"], 160) if x.get("change") else "") for x in w["writes"][:30]]
        L += ["- reads: %s" % x["component"] for x in w["reads"][:30]]
    if t.get("tickets"):
        L += ["", "Tickets: " + ", ".join(t["tickets"])]
    qs = []
    for fn in api._listdir(_home("data", "tickets")):
        key = fn[:-5]
        if not fn.endswith(".json") or not api.TICKET_RE.match(key):
            continue
        tk = api.read_json_cached(_home("data", "tickets", fn)) or {}
        for q in (tk.get("queries") or {}).values():
            if isinstance(q, dict) and any(involves(x) for x in q.get("tables") or []):
                qs.append((q.get("date") or "", key, q))
    if qs:
        L += ["", "## Saved queries (%d)" % len(qs)]
        for date, key, q in sorted(qs, key=lambda x: x[0], reverse=True)[:10]:
            L += ["- %s · %s · %s" % (key, date, _one(q.get("purpose") or "query", 140)),
                  "  `%s`" % _one(q.get("text"), 400).replace("`", "'")]
        L.append("Queries live in %s" % _home("tickets", "<KEY>", "queries.md"))
    L.append("\nKnowledge files: %s · %s" % (_home("knowledge", "db-tables.md"), _home("knowledge", "db-map.md")))
    return "\n".join(L)


def tool_get_alerts(args, cfg):
    from .dashboard import api
    L = ["# Alerts"]
    n = 0
    for t in api.tickets()["tickets"]:
        for a in t["alerts"]:
            if n == 0:
                L += ["", "## Branches and PRs"]
            n += 1
            L.append("- %s · %s%s" % (t["key"], _one(a["text"], 240), " (%s)" % a["repo"] if a.get("repo") else ""))
    h = api.read_json_cached(common.wl("health.json")) or {}
    warn = []
    if os.path.exists(common.wl("PAUSED")):
        warn.append("Capture is paused (/cg:pause off to resume).")
    lt = common.parse_ts(h.get("last_tick")) if isinstance(h.get("last_tick"), str) else None
    if lt is None:
        warn.append("The background tick has never run (/cg:doctor).")
    elif (common.now() - lt).total_seconds() > 3600:
        warn.append("The background tick last ran %s (/cg:doctor)." % h["last_tick"])
    for w in h.get("warnings") or []:
        warn.append(_one(w if isinstance(w, str) else json.dumps(w, ensure_ascii=False), 300))
    for k, label in (("last_error", "Last error"), ("last_push_error", "Last push error")):
        if h.get(k):
            warn.append("%s: %s" % (label, _one(h[k], 300)))
    try:
        from .safety import log as slog
        s = slog.summary(1)
        if s["recent"]:
            warn.append("The safety layer blocked %d action(s) in the last 24 h (Health page / /cg:doctor)." % s["recent"])
    except Exception:  # noqa: BLE001 — the rest still shows
        pass
    if warn:
        L += ["", "## Worklog health"] + ["- " + w for w in warn]
    if len(L) == 1:
        L.append("Nothing needs attention.")
    L.append("\nHealth: %s" % common.wl("health.json"))
    return "\n".join(L)


HANDLERS = {
    "search": tool_search, "get_ticket": tool_get_ticket, "get_day": tool_get_day, "get_timesheet": tool_get_timesheet,
    "get_branch_status": tool_get_branch_status, "get_db_table": tool_get_db_table, "list_tickets": tool_list_tickets,
    "get_alerts": tool_get_alerts,
}


# --------------------------------------------------------------------------- protocol

class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _log(msg):
    try:
        common.log("mcp-server", msg)
    except Exception:  # noqa: BLE001 — logging never breaks the server
        pass


def call_tool(name, args):
    """-> MCP CallToolResult."""
    if name not in HANDLERS:
        raise RpcError(-32602, "Unknown tool: %s" % name)
    if not isinstance(args, dict):
        raise RpcError(-32602, "arguments must be an object")
    try:
        cfg = _cfg()
        text = HANDLERS[name](args, cfg)
        page = args.get("page")
        try:
            page = int(page) if page not in (None, "") else 1
        except (TypeError, ValueError):
            raise ToolError("`page` must be a number")
        text, _ = paginate(text, page)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except Exception as e:  # noqa: BLE001 — a tool failure is a result, never a dead server
        _log("tool %s failed: %s: %s" % (name, e.__class__.__name__, str(e)[:300]))
        return {"content": [{"type": "text", "text": "%s failed: %s" % (name, e.__class__.__name__)}], "isError": True}


class Server:
    def __init__(self):
        self.internal = common.internal()  # inside our own background claude -p calls: serve nothing

    def initialize(self, params):
        asked = (params or {}).get("protocolVersion")
        return {"protocolVersion": asked if asked in PROTOCOLS else LATEST, "capabilities": {"tools": {}},
                "serverInfo": {"name": "chitragupta", "title": "Chitragupta worklog", "version": __version__},
                "instructions": "" if self.internal else INSTRUCTIONS}

    def handle(self, msg):
        """One JSON-RPC message -> response dict, or None for a notification / a response from the client."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        method = msg.get("method")
        has_id = "id" in msg
        if method is None:  # a response to something we never send
            return None
        if not isinstance(method, str):
            return _error(msg.get("id"), -32600, "Invalid Request")
        params = msg.get("params")
        if params is not None and not isinstance(params, dict):
            return _error(msg.get("id"), -32602, "params must be an object") if has_id else None
        try:
            if method == "initialize":
                result = self.initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [] if self.internal else TOOLS}
            elif method == "tools/call":
                if self.internal:
                    raise RpcError(-32602, "Unknown tool: %s" % (params or {}).get("name"))
                name = (params or {}).get("name")
                if not isinstance(name, str) or not name:
                    raise RpcError(-32602, "tools/call needs a tool name")
                result = call_tool(name, (params or {}).get("arguments") or {})
            elif method.startswith("notifications/"):
                return None
            else:
                raise RpcError(-32601, "Method not found: %s" % method)
        except RpcError as e:
            return _error(msg.get("id"), e.code, e.message) if has_id else None
        except Exception as e:  # noqa: BLE001
            _log("%s failed: %s: %s" % (method, e.__class__.__name__, str(e)[:300]))
            return _error(msg.get("id"), -32603, "Internal error") if has_id else None
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result} if has_id else None

    def handle_line(self, line):
        """One line from stdin -> the reply (dict, list or None)."""
        line = line.strip()
        if not line:
            return None
        try:
            msg = json.loads(line)
        except ValueError:
            return _error(None, -32700, "Parse error")
        if isinstance(msg, list):  # a batch (2025-03-26)
            if not msg:
                return _error(None, -32600, "Invalid Request")
            out = [r for r in (self.handle(m) for m in msg) if r is not None]
            return out or None
        return self.handle(msg)


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None):
    """Read requests until EOF. stdout gets protocol lines only: everything else printed meanwhile goes to stderr."""
    inp = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    saved = sys.stdout
    sys.stdout = sys.stderr
    server = Server()
    try:
        for raw in inp:
            reply = server.handle_line(raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw)
            if reply is None:
                continue
            data = json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n"
            out.write(data if isinstance(out, io.TextIOBase) else data.encode("utf-8"))
            out.flush()
    finally:
        sys.stdout = saved
    return 0


if __name__ == "__main__":
    try:
        sys.exit(serve())
    except KeyboardInterrupt:
        sys.exit(0)
