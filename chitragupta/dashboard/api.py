"""Read-only views of the data home for the dashboard.

Every function here only reads: data/days, data/tickets, data/timesheets, data/calendar.json, data/qa-trace,
tickets/<T>/*.md, .worklog/health.json, .worklog/raw and .worklog/logs. Nothing is written, no model and no MCP
server is called. Results are plain JSON-ready dicts.
"""
import csv
import datetime as _dt
import hashlib
import io
import json
import os
import re
import threading

from .. import common

TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,6}$")
FINDING_KINDS = ["root_cause", "finding", "how_it_works", "fix", "decision", "config", "repro_test", "reference",
                 "blocker", "question", "todo"]

_cache = {}
_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- cached reads

def _stat_key(path):
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def read_json_cached(path, default=None):
    """JSON file contents, re-parsed only when mtime/size change. Callers must not mutate the result."""
    key = _stat_key(path)
    if key is None:
        return {} if default is None else default
    with _cache_lock:
        hit = _cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
    data = common.read_json(path, {} if default is None else default)
    with _cache_lock:
        if len(_cache) > 4000:
            _cache.clear()
        _cache[path] = (key, data)
    return data


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _tail_lines(path, n=20, max_bytes=65536):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # first line is probably cut
    return [l for l in lines if l.strip()][-n:]


def _today(cfg, now=None):
    return common.workday(now or common.now(), cfg)


def _rel(path):
    return os.path.relpath(path, common.home()).replace("\\", "/")


# --------------------------------------------------------------------------- tickets

def ticket_paths(key):
    base = common.hp("tickets", key)
    names = {"readme": "README.md", "queries": "queries.md", "requests": "requests.md", "replay": "replay.sh",
             "dbflow": "db-flow.md", "artifacts": os.path.join("artifacts", "INDEX.md")}
    out = {k: os.path.join(base, v) for k, v in names.items()}
    out["bruno"] = common.hp("bruno", key)
    out["folder"] = base
    return {k: v for k, v in out.items() if os.path.exists(v)}


def _ticket_minutes(tk):
    return int(sum(int((v or {}).get("minutes") or 0) for v in (tk.get("timeline") or {}).values()))


def _counts(tk):
    items = (tk.get("items") or {}).values()
    return {
        "findings": sum(1 for i in items if i.get("kind") not in ("todo", "question")),
        "queries": len(tk.get("queries") or {}),
        "requests": len(((tk.get("replay") or {}).get("final")) or []),
        "artifacts": len(tk.get("artifacts") or {}),
        "dbflow": len(tk.get("data_flow") or {}),
    }


def ticket_summary(key, tk):
    return {
        "key": key, "title": tk.get("title") or "", "status": tk.get("status") or ((tk.get("jira") or {}).get("status") or ""),
        "first": tk.get("first") or "", "last": tk.get("last") or "", "minutes": _ticket_minutes(tk),
        "tldr": tk.get("tldr") or "", "repos": [os.path.basename(str(r).rstrip("/\\")) for r in tk.get("repos") or []],
        "counts": _counts(tk),
    }


def tickets(q=None):
    """All known tickets, newest activity first; `q` filters by key / title / TL;DR (case-insensitive)."""
    d = common.hp("data", "tickets")
    out = []
    for fn in _listdir(d):
        if not fn.endswith(".json"):
            continue
        key = fn[:-5]
        if not TICKET_RE.match(key):
            continue
        tk = read_json_cached(os.path.join(d, fn))
        if isinstance(tk, dict):
            out.append(ticket_summary(key, tk))
    if q:
        ql = q.strip().lower()
        out = [t for t in out if ql in ("%s %s %s" % (t["key"], t["title"], t["tldr"])).lower()]
    out.sort(key=lambda t: (t["last"], t["key"]), reverse=True)
    return {"tickets": out, "count": len(out), "q": q or ""}


def _items(tk):
    items = sorted((tk.get("items") or {}).values(),
                   key=lambda i: (i.get("date") or "", i.get("time") or ""), reverse=True)
    out = []
    for i in items:
        out.append({k: i.get(k) for k in ("id", "kind", "title", "detail", "date", "time", "confidence", "evidence")})
    order = {k: n for n, k in enumerate(FINDING_KINDS)}
    out.sort(key=lambda i: order.get(i.get("kind"), 99))
    return out


def _queries(tk):
    qs = sorted((tk.get("queries") or {}).items(), key=lambda kv: (kv[1].get("date") or "", kv[1].get("time") or ""),
                reverse=True)
    return [{"id": k, "dialect": v.get("dialect"), "kind": v.get("kind"), "purpose": v.get("purpose"), "text": v.get("text") or "",
             "tables": v.get("tables") or [], "revealed": v.get("revealed"), "date": v.get("date"), "time": v.get("time")}
            for k, v in qs]


_CURL_HEAD = re.compile(r"^###\s+(\d+)\.\s+(.*)$")


def parse_requests_md(text):
    """requests.md -> {"setup": <shell lines>, "steps": {n: {"name", "curl", "expected"}}}."""
    out = {"setup": "", "steps": {}}
    if not text:
        return out
    lines = text.splitlines()
    cur = None
    in_code = False
    buf = []
    in_curl_section = False
    for ln in lines:
        if ln.startswith("## "):
            in_curl_section = ln.strip().lower().startswith("## curl")
            cur = None
            continue
        if not in_curl_section:
            continue
        m = _CURL_HEAD.match(ln)
        if m and not in_code:
            cur = int(m.group(1))
            out["steps"][cur] = {"name": m.group(2).strip(), "curl": "", "expected": ""}
            continue
        if ln.strip().startswith("```"):
            if in_code:
                code = "\n".join(buf)
                if cur is None:
                    out["setup"] = code
                else:
                    out["steps"][cur]["curl"] = code
                buf = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            buf.append(ln)
        elif cur is not None and ln.startswith("Expected:"):
            out["steps"][cur]["expected"] = ln[len("Expected:"):].strip()
    return out


def _shell_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def curl_for(call):
    """A plain curl command from a captured call / step (method, url, headers, body)."""
    parts = ["curl -sS -i -X %s %s" % ((call.get("method") or "GET").upper(), _shell_quote(call.get("url") or ""))]
    for h in call.get("headers") or []:
        if isinstance(h, (list, tuple)) and len(h) == 2:
            parts.append("-H %s" % _shell_quote("%s: %s" % (h[0], h[1])))
        elif isinstance(h, str):
            parts.append("-H %s" % _shell_quote(h))
    body = call.get("body")
    if body not in (None, ""):
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False)
        parts.append("--data-binary %s" % _shell_quote(body))
    return " \\\n  ".join(parts)


def _requests(key, tk, paths):
    rp = tk.get("replay") or {}
    final = rp.get("final") or []
    md = parse_requests_md(common.read_text(paths["requests"])) if "requests" in paths else {"setup": "", "steps": {}}
    steps = []
    for s in final:
        n = s.get("n")
        from_md = md["steps"].get(n) or {}
        steps.append({
            "n": n, "name": s.get("name") or from_md.get("name") or "", "method": s.get("method"), "url": s.get("url"),
            "expected": s.get("expected") or from_md.get("expected") or "", "owner": s.get("owner"),
            "executed": s.get("executed", True), "scenario": s.get("scenario"),
            "critical_fields": s.get("critical_fields") or [],
            "curl": from_md.get("curl") or curl_for(s),
        })
    if not steps and md["steps"]:
        for n, s in sorted(md["steps"].items()):
            steps.append({"n": n, "name": s["name"], "expected": s["expected"], "curl": s["curl"]})
    return {"steps": steps, "setup": md["setup"], "scenarios": rp.get("scenarios") or [],
            "dropped": len(rp.get("dropped") or []), "captured": len(tk.get("api_calls") or {}),
            "bruno_folder": paths.get("bruno")}


def _dbflow(key, paths):
    text = common.read_text(paths["dbflow"]) if "dbflow" in paths else ""
    qa = read_json_cached(common.hp("data", "qa-trace", key + ".json"))
    if qa and "## Worked example (QA)" not in text:
        try:  # the same section the tick writes into db-flow.md, built from data/qa-trace (read-only)
            from .. import kbjobs
            extra = kbjobs.dbflow_qa_section(key)
            if extra:
                text = (text.rstrip("\n") + "\n\n" + extra) if text else extra
        except Exception:  # noqa: BLE001 — the dashboard shows what it can
            pass
    tr = (qa or {}).get("trace") or {}
    return {"markdown": text, "qa_trace": {"env": qa.get("env"), "fetched_at": qa.get("fetched_at"),
                                           "summary": tr.get("summary"), "steps": tr.get("steps") or [],
                                           "changes": tr.get("changes") or []} if qa else None}


def _artifacts(key, tk):
    arts = sorted((tk.get("artifacts") or {}).items(), key=lambda kv: kv[1].get("last") or kv[1].get("date") or "", reverse=True)
    out = []
    for aid, a in arts:
        stored = a.get("stored_name") or a.get("stored")
        path = None
        if isinstance(stored, str) and stored:
            p = common.hp("tickets", key, "artifacts", os.path.basename(stored))
            path = p if os.path.exists(p) else None
        out.append({"id": aid, "name": a.get("name"), "kind": a.get("kind"), "purpose": a.get("purpose"),
                    "date": a.get("date"), "last": a.get("last"), "size": a.get("size"), "source": a.get("source"),
                    "storage": a.get("storage"), "versions": len(a.get("versions") or []) if isinstance(a.get("versions"), list)
                    else a.get("versions"), "path": path})
    return out


def ticket(key):
    """Everything the dashboard shows for one ticket, or None when unknown."""
    if not TICKET_RE.match(key or ""):
        return None
    tk = read_json_cached(common.hp("data", "tickets", key + ".json"))
    paths = ticket_paths(key)
    if not tk and not paths:
        return None
    tk = tk or {}
    out = ticket_summary(key, tk)
    out.update({
        "problem": tk.get("problem") or "", "title_source": tk.get("title_source") or "",
        "branches": tk.get("branches") or [], "jira": tk.get("jira") or {},
        "findings": _items(tk), "queries": _queries(tk), "requests": _requests(key, tk, paths),
        "dbflow": _dbflow(key, paths), "artifacts": _artifacts(key, tk),
        "timeline": [{"day": d, "minutes": int((v or {}).get("minutes") or 0), "line": (v or {}).get("line") or ""}
                     for d, v in sorted((tk.get("timeline") or {}).items(), reverse=True)],
        "files": sorted(((tk.get("files") or {}).values()), key=lambda f: f.get("last") or "", reverse=True)[:60],
        "paths": paths,
    })
    return out


# --------------------------------------------------------------------------- today

def day_data(day):
    return read_json_cached(common.hp("data", "days", day[:4], day + ".json"))


def _title(key):
    tk = read_json_cached(common.hp("data", "tickets", key + ".json"))
    return (tk or {}).get("title") or ""


def saved_worklog(day):
    sh = read_json_cached(common.hp("data", "timesheets", day + ".json"))
    if not sh:
        return None
    vs = sh.get("versions") or []
    v = vs[-1] if vs else {}
    return {"status": sh.get("status"), "sent_at": sh.get("sent_at"), "attempts": sh.get("attempts"),
            "last_error": sh.get("last_error"), "version": v.get("v"), "text": v.get("text") or "",
            "entries": [_entry(e) for e in sh.get("entries") or []]}


def _entry(e):
    from .. import timesheet as T
    m = int(e.get("minutes") or 0)
    return {"ticket": e.get("ticket"), "start": e.get("start_hhmm") or "", "minutes": m, "duration": T.jira_dur(m),
            "description": e.get("desc") or "", "manual": bool(e.get("manual"))}


def worklog_preview(day, cfg):
    """Tonight's Jira worklog as the tick would compose it now, without the model condense call, without Jira,
    and without saving anything. Reuses jiraworklog/timesheet read-only."""
    try:
        from .. import agent as agentmod
        from .. import jiraworklog
        from .. import timesheet as T
        from ..redact import redact
        a = agentmod.Agent()
        j = a.jwl
        now = common.now()
        today = common.workday(now, a.cfg)
        until = now if day == today else None
        cal = jiraworklog.load_cal()
        bl = j.blocks(day, until)
        manual = j.manual_for(day, cal)
        res = T.compose(day, today, bl, manual, cal, j.ts, describe=lambda es: j.describe(es, condense=False, facts=None))
        out = {"kind": res.get("kind"), "reason": res.get("reason"), "note": res.get("note") or "",
               "target": res.get("target"), "tracked": res.get("tracked") or {}, "day_kind": res.get("day_kind")}
        text = res.get("text") or ""
        out["text"] = redact(text, mode="public") if res.get("kind") == "message" else text
        out["entries"] = [_entry(e) for e in res.get("entries") or []]
        if res.get("kind") == "message":  # descriptions get the same public masking as the message
            for e in out["entries"]:
                e["description"] = redact(e["description"], mode="public")
        out["send_at"] = j.ts.get("send_at")
        return out
    except Exception as e:  # noqa: BLE001
        return {"kind": "error", "reason": "preview unavailable: %s" % (str(e) or e.__class__.__name__)[:200], "entries": []}


def today(cfg=None, now=None, preview=True):
    cfg = cfg or common.load_config()
    day = _today(cfg, now)
    dd = day_data(day) or {}
    rows = []
    for key, slot in (dd.get("tickets") or {}).items():
        rows.append({"key": key, "title": _title(key), "minutes": int(slot.get("minutes") or 0),
                     "first_time": slot.get("first_time") or "", "where": slot.get("where") or [],
                     "did": [w.get("text") for w in (slot.get("what_i_did") or [])[-3:] if isinstance(w, dict)]})
    rows.sort(key=lambda r: (-r["minutes"], r["key"]))
    other = sorted(({"group": g, "minutes": int((v or {}).get("minutes") or 0)} for g, v in (dd.get("other") or {}).items()),
                   key=lambda r: -r["minutes"])
    out = {"day": day, "updated_at": dd.get("updated_at"), "closed": bool(dd.get("closed")),
           "summary": dd.get("summary") or "", "tickets": rows, "other": other,
           "total_minutes": sum(r["minutes"] for r in rows) + sum(r["minutes"] for r in other),
           "commits": len(dd.get("commits") or []), "worklog_saved": saved_worklog(day)}
    if preview:
        out["worklog_preview"] = worklog_preview(day, cfg)
    return out


# --------------------------------------------------------------------------- timesheet

def _iso_week(day):
    y, w, _ = _dt.date.fromisoformat(day).isocalendar()
    return "%04d-W%02d" % (y, w)


def _period(day, period):
    return _iso_week(day) if period == "week" else day[:7]


def all_days(limit_days=400, today_day=None):
    """[(day, day-json)] for the newest `limit_days` day files."""
    root = common.hp("data", "days")
    files = []
    for y in _listdir(root):
        yd = os.path.join(root, y)
        if not (y.isdigit() and os.path.isdir(yd)):
            continue
        for fn in _listdir(yd):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", fn):
                files.append((fn[:-5], os.path.join(yd, fn)))
    files.sort()
    files = files[-limit_days:]
    return [(d, read_json_cached(p)) for d, p in files]


def calendar_days(cal=None):
    cal = cal if cal is not None else read_json_cached(common.hp("data", "calendar.json"))
    out = []
    for kind in ("leave", "half_day", "holiday", "workday"):
        for day, name in sorted(((cal or {}).get(kind) or {}).items()):
            out.append({"date": day, "kind": kind, "name": name if isinstance(name, str) else ""})
    manual = []
    for day, lst in sorted(((cal or {}).get("time") or {}).items()):
        for m in lst or []:
            manual.append({"date": day, "ticket": m.get("ticket"), "start": m.get("start"), "minutes": m.get("minutes"),
                           "text": m.get("text")})
    out.sort(key=lambda r: r["date"], reverse=True)
    manual.sort(key=lambda r: r["date"], reverse=True)
    return out, manual


def timesheet(period="week", limit=12, cfg=None):
    """Minutes per ticket per ISO week (YYYY-Www) or month (YYYY-MM), newest `limit` periods; calendar entries."""
    period = "month" if period == "month" else "week"
    per = {}  # period -> ticket -> minutes
    titles = {}
    for day, dd in all_days():
        p = _period(day, period)
        bucket = per.setdefault(p, {})
        for key, slot in ((dd or {}).get("tickets") or {}).items():
            bucket[key] = bucket.get(key, 0) + int((slot or {}).get("minutes") or 0)
        other = sum(int((v or {}).get("minutes") or 0) for v in ((dd or {}).get("other") or {}).values())
        if other:
            bucket[""] = bucket.get("", 0) + other
    cal_rows, manual = calendar_days()
    for m in manual:  # time logged by hand (inbox `time:` lines) counts too
        try:
            p = _period(m["date"], period)
        except (TypeError, ValueError):
            continue
        if m.get("ticket"):
            b = per.setdefault(p, {})
            b[m["ticket"]] = b.get(m["ticket"], 0) + int(m.get("minutes") or 0)
    periods = sorted(per, reverse=True)[:max(1, int(limit))]
    keys = sorted({k for p in periods for k in per[p]}, key=lambda k: (k == "", k))
    for k in keys:
        if k:
            titles[k] = _title(k)
    rows = []
    for k in keys:
        cells = {p: per[p].get(k, 0) for p in periods}
        rows.append({"ticket": k, "title": titles.get(k, "") if k else "Other work (no ticket)", "minutes": cells,
                     "total": sum(cells.values())})
    rows.sort(key=lambda r: (r["ticket"] == "", -r["total"], r["ticket"]))
    totals = {p: sum(per[p].values()) for p in periods}
    in_range = [c for c in cal_rows if periods and _period(c["date"], period) in periods]
    return {"period": period, "periods": periods, "rows": rows, "totals": totals,
            "calendar": in_range if periods else cal_rows[:30], "manual": manual[:50]}


def timesheet_csv(period="week", limit=104):
    ts = timesheet(period, limit)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["period", "ticket", "title", "minutes", "hours"])
    for p in sorted(ts["periods"]):
        for r in ts["rows"]:
            m = r["minutes"].get(p, 0)
            if m:
                w.writerow([p, r["ticket"] or "(no ticket)", _csv_safe(r["title"]), m, "%.2f" % (m / 60.0)])
    for c in sorted(ts["calendar"], key=lambda c: c["date"]):
        w.writerow([c["date"], "(%s)" % c["kind"].replace("_", " "), _csv_safe(c["name"]), "", ""])
    return buf.getvalue()


def _csv_safe(s):
    """Keep spreadsheet apps from treating a cell as a formula."""
    s = s or ""
    return ("'" + s) if s[:1] in ("=", "+", "-", "@") else s


# --------------------------------------------------------------------------- health / status

def latest_raw_event():
    """(ts, path) of the newest captured hook event."""
    root = common.wl("raw")
    for day in reversed(_listdir(root)):
        dp = os.path.join(root, day)
        if not os.path.isdir(dp):
            continue
        cands = []
        for sub in (dp, os.path.join(dp, "time")):
            for fn in _listdir(sub):
                if fn.endswith(".jsonl"):
                    p = os.path.join(sub, fn)
                    k = _stat_key(p)
                    if k:
                        cands.append((k[0], p))
        if not cands:
            continue
        _, p = max(cands)
        for line in reversed(_tail_lines(p, 5, 16384)):
            try:
                ts = json.loads(line).get("ts")
            except ValueError:
                continue
            if ts:
                return ts, p
        return _dt.datetime.fromtimestamp(max(cands)[0] / 1e9).astimezone().isoformat(timespec="seconds"), p
    return None, None


def _ago_minutes(ts, now):
    d = common.parse_ts(ts) if isinstance(ts, str) else None
    return None if d is None else max(0, int((now - d).total_seconds() // 60))


def _mcp(h, cfg, today_day):
    m = h.get("mcp") or {}
    servers = {}
    for job, j in (m.get("jobs") or {}).items():
        for name, st in ((j or {}).get("servers") or {}).items():
            at = (j or {}).get("at") or ""
            if name not in servers or at > servers[name]["at"]:
                servers[name] = {"server": name, "status": st, "at": at, "job": job}
    tokens_in = tokens_out = calls = 0
    cost = 0.0
    for job, j in (m.get("jobs") or {}).items():
        at = (j or {}).get("at")
        if at and common.workday(at, cfg) == today_day:
            tokens_in += int(j.get("in") or 0)
            tokens_out += int(j.get("out") or 0)
            calls += int(j.get("calls") or 0) if isinstance(j.get("calls"), (int, float)) else len(j.get("calls") or [])
            cost += float(j.get("cost") or 0)
    slack_tokens = ((h.get("jira_worklog") or {}).get("tokens") or {}).get(today_day)
    if isinstance(slack_tokens, dict):
        tokens_in += int(slack_tokens.get("in") or slack_tokens.get("input") or 0)
        tokens_out += int(slack_tokens.get("out") or slack_tokens.get("output") or 0)
    mc = cfg.get("mcp") or {}
    configured = []
    for name in ("jira", "database"):
        c = mc.get(name) or {}
        configured.append({"role": name, "enabled": bool(c.get("enabled")), "server": c.get("server") or ""})
    return {"servers": sorted(servers.values(), key=lambda s: s["server"]), "configured": configured,
            "calls_today": int(((m.get("calls") or {}).get(today_day)) or 0), "job_tool_calls_today": calls,
            "tokens_in_today": tokens_in, "tokens_out_today": tokens_out, "cost_today": round(cost, 4),
            "budget": mc.get("daily_call_budget"), "skip": m.get("skip") or {}, "offline": m.get("offline") or {},
            "last_skip_reason": m.get("last_skip_reason") or "", "status": h.get("mcp_status") or None}


def _slack(h, cfg):
    jw = h.get("jira_worklog") or {}
    sl = (cfg.get("jira_worklog") or {}).get("slack") or {}
    sheets = []
    d = common.hp("data", "timesheets")
    for fn in reversed(_listdir(d)):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", fn):
            sh = read_json_cached(os.path.join(d, fn))
            last = (sh.get("versions") or [{}])[-1].get("slack") or {}
            sheets.append({"day": fn[:-5], "status": sh.get("status"), "sent_at": sh.get("sent_at"),
                           "attempts": sh.get("attempts"), "last_error": sh.get("last_error") or last.get("error")})
            if len(sheets) >= 5:
                break
    enabled = sl.get("enabled")
    slack_h = h.get("slack") if isinstance(h.get("slack"), dict) else {}
    return {"enabled": enabled, "configured": bool(sl.get("self_user_id")), "send_at": (cfg.get("jira_worklog") or {}).get("send_at"),
            # jiraworklog writes health.jira_worklog.slack ("off" / "not_connected" / …) + slack_note
            "status": (jw.get("slack") if isinstance(jw.get("slack"), str) else "") or jw.get("slack_status")
                      or slack_h.get("status") or "",
            "note": jw.get("slack_note") or "",
            "send_warning": jw.get("send_warning"), "last_mismatch": jw.get("last_mismatch"), "recent": sheets}


_ERR = re.compile(r"error|failed|traceback|exception|rc=(?!0\b)\d+", re.I)


def recent_errors(h, n=12):
    out = []
    if h.get("last_error"):
        out.append({"source": "health", "text": str(h["last_error"])[:400]})
    if h.get("last_push_error"):
        out.append({"source": "push", "text": str(h["last_push_error"])[:400]})
    for name in ("agent", "hook-errors", "mcp", "dashboard"):
        p = common.wl("logs", name + ".log")
        for line in _tail_lines(p, 200):
            if name == "hook-errors" or _ERR.search(line):
                out.append({"source": name, "text": line[:400]})
    for line in _tail_lines(common.wl("logs", "mcp-guard.log"), 50):
        if " DENY " in line:
            out.append({"source": "mcp-guard", "text": line[:400]})
    return out[-n:]


def status(cfg=None, now=None, server=None):
    cfg = cfg or common.load_config()
    now = now or common.now()
    h = read_json_cached(common.wl("health.json"))
    today_day = _today(cfg, now)
    raw_ts, _ = latest_raw_event()
    hook_ts = max([t for t in (raw_ts, h.get("last_hook_event")) if t] or [None], key=lambda t: t or "")
    ago = {k: _ago_minutes(h.get(k), now) for k in ("last_tick", "last_tick_ok", "last_push", "last_commit")}
    return {
        "now": now.isoformat(timespec="seconds"), "day": today_day, "home": common.home(),
        "paused": os.path.exists(common.wl("PAUSED")),
        "hooks": {"last_event": hook_ts, "last_raw_event": raw_ts, "minutes_ago": _ago_minutes(hook_ts, now)},
        "tick": {"last_tick": h.get("last_tick"), "last_tick_ok": h.get("last_tick_ok"),
                 "minutes_ago": ago["last_tick"], "ok_minutes_ago": ago["last_tick_ok"]},
        "push": {"last_push": h.get("last_push"), "last_commit": h.get("last_commit"), "minutes_ago": ago["last_push"],
                 "error": h.get("last_push_error"), "remote_set": bool(cfg.get("github_remote"))},
        "slack": _slack(h, cfg),
        "mcp": _mcp(h, cfg, today_day),
        "today": h.get("today") if (h.get("today") or {}).get("day") == today_day else {"day": today_day},
        "warnings": list(h.get("warnings") or []),
        "errors_total": h.get("errors_total") or 0,
        "errors": recent_errors(h),
        "dashboard": server or {},
    }


# --------------------------------------------------------------------------- change cursor

_cursor_cache = {"at": 0.0, "value": None}


def _watch_paths(cfg):
    day = _today(cfg)
    yield common.wl("health.json")
    yield common.hp("data", "calendar.json")
    for d in (common.hp("data", "tickets"), common.hp("data", "timesheets"), common.hp("data", "qa-trace"),
              common.hp("data", "days", day[:4]), common.wl("raw", day), common.wl("raw", day, "time")):
        yield d
        for fn in _listdir(d):
            yield os.path.join(d, fn)
    troot = common.hp("tickets")
    for t in _listdir(troot):
        for fn in ("README.md", "requests.md", "db-flow.md", "queries.md"):
            yield os.path.join(troot, t, fn)


def changes(cfg=None, max_age=2.0):
    """An opaque cursor that moves whenever a file the dashboard reads changes."""
    import time
    now = time.monotonic()
    home = common.home()
    if _cursor_cache["value"] and _cursor_cache.get("home") == home and now - _cursor_cache["at"] < max_age:
        return {"cursor": _cursor_cache["value"]}
    cfg = cfg or common.load_config()
    h = hashlib.sha1()
    for p in _watch_paths(cfg):
        k = _stat_key(p)
        if k:
            h.update(("%s|%d|%d\n" % (p, k[0], k[1])).encode("utf-8", "replace"))
    value = h.hexdigest()[:16]
    _cursor_cache.update(at=now, value=value, home=home)
    return {"cursor": value}
