"""Read-only views of the data home for the dashboard.

Every function here only reads: data/days, data/tickets (incl. the branch-tracking "git" block), data/timesheets,
data/calendar.json, data/qa-trace, data/catalog.json, data/db-schema, tickets/<T>/*.md, .worklog/health.json,
.worklog/cache/mcp-status.json, .worklog/raw and .worklog/logs. Nothing is written, no model and no MCP server is
called. Results are plain JSON-ready dicts.
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
        "counts": _counts(tk), "branches": _linked_branches(tk)[:5], "alerts": _alerts(tk),
    }


def _alerts(tk):
    """The branch-tracking alerts of a ticket, as {kind, text, repo} (empty when branch tracking found nothing)."""
    out = []
    for a in ((tk.get("git") or {}).get("alerts")) or []:
        if isinstance(a, dict):
            out.append({"kind": a.get("kind") or "", "text": a.get("text") or "", "repo": a.get("repo") or ""})
    return out


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


MAX_GIT_ROWS = 300


def _git(tk):
    """tk["git"] for the page (never mutating the cached record): every repo gets its Mermaid gitGraph ("graph", None
    when too large or not drawable) and a legend of renamed branches; long matrices are cut to MAX_GIT_ROWS rows."""
    g = tk.get("git")
    if not isinstance(g, dict) or not g:
        return {}
    from .. import gitview
    repos = {}
    for name, e in sorted((g.get("repos") or {}).items()):
        if not isinstance(e, dict):
            continue
        x = dict(e)
        rows = x.get("rows") or []
        if len(rows) > MAX_GIT_ROWS:
            x["rows"] = rows[-MAX_GIT_ROWS:]
            x["rows_cut"] = len(rows) - MAX_GIT_ROWS
        try:
            gr = gitview.graph(e)
        except Exception as ex:  # noqa: BLE001 — the tables still show
            gr = {"source": None, "legend": {}, "why": "graph failed: %s" % ex.__class__.__name__}
        x["graph"], x["graph_legend"], x["graph_why"] = gr["source"], gr["legend"], gr["why"]
        repos[name] = x
    return {"repos": repos, "alerts": list(g.get("alerts") or []), "summary": g.get("summary") or "",
            "checked_at": g.get("checked_at")}


def _linked_branches(tk):
    """The branches branch tracking linked to the ticket; before it has run, the names seen in sessions."""
    repos = ((tk.get("git") or {}).get("repos")) or {}
    names = [b["branch"] for x in repos.values() for b in x.get("branches") or [] if b.get("branch")]
    return names or [b for b in tk.get("branches") or [] if b and not b.startswith("refs/")]


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
        "branches": _linked_branches(tk), "jira": tk.get("jira") or {},
        "findings": _items(tk), "queries": _queries(tk), "requests": _requests(key, tk, paths),
        "dbflow": _dbflow(key, paths), "artifacts": _artifacts(key, tk),
        "timeline": [{"day": d, "minutes": int((v or {}).get("minutes") or 0), "line": (v or {}).get("line") or ""}
                     for d, v in sorted((tk.get("timeline") or {}).items(), reverse=True)],
        "files": sorted(((tk.get("files") or {}).values()), key=lambda f: f.get("last") or "", reverse=True)[:60],
        "paths": paths, "git": _git(tk), "readme": _read_capped(paths.get("readme")),
    })
    return out


README_MAX = 200_000


def _read_capped(path, limit=README_MAX):
    """A text file's first `limit` bytes ("" when missing); a cut multi-byte character is dropped."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if len(data) >= limit:
        text = text.rstrip("\ufffd")
    return text[1:] if text.startswith("\ufeff") else text


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
            "target": sh.get("target"), "entries": [_entry(e) for e in sh.get("entries") or []]}


def _entry(e):
    from .. import timesheet as T
    m = int(e.get("minutes") or 0)
    return {"ticket": e.get("ticket"), "start": e.get("start_hhmm") or "", "minutes": m, "duration": T.jira_dur(m),
            "description": e.get("desc") or "", "manual": bool(e.get("manual"))}


def worklog_preview(day, cfg):
    """Tonight's Jira worklog as the tick would compose it now, without the model condense call, without Jira,
    and without saving anything. Reuses jiraworklog/timesheet read-only."""
    return _preview(day, cfg)[0]


def _preview(day, cfg):
    """(preview, activity blocks): the blocks are the ones the preview was composed from ([] on failure), so the
    day view draws its lanes without building a second Agent."""
    bl = []
    try:
        from .. import agent as agentmod
        from .. import jiraworklog
        from .. import timesheet as T
        from ..redact import redact
        a = agentmod.Agent()
        j = a.jwl
        now = common.now()
        today = common.workday(now, a.cfg)
        until = now if day == today else None  # a past day is composed whole
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
        return out, bl
    except Exception as e:  # noqa: BLE001
        return ({"kind": "error", "reason": "preview unavailable: %s" % (str(e) or e.__class__.__name__)[:200], "entries": []},
                bl)


def _lanes(blocks):
    """Activity blocks -> one lane per ticket ("" = work without a ticket), most minutes first."""
    per = {}
    for b in blocks or []:
        key = b.get("ticket") or ""
        lane = per.setdefault(key, {"ticket": key, "title": _title(key) if key else "", "minutes": 0.0, "spans": []})
        lane["minutes"] += float(b.get("minutes") or 0)
        for s, e in b.get("spans") or []:
            if s and e:
                lane["spans"].append((s, e))
    out = []
    for lane in per.values():
        lane["spans"] = [[common.hhmm(s), common.hhmm(e)] for s, e in sorted(lane["spans"])]
        lane["minutes"] = int(round(lane["minutes"]))
        out.append(lane)
    out.sort(key=lambda l: (-l["minutes"], l["ticket"] == "", l["ticket"]))
    return out


def _last_activity(blocks):
    ends = [e for b in blocks or [] for _, e in b.get("spans") or [] if e]
    return common.hhmm(max(ends)) if ends else None


def _commit_list(dd):
    out = []
    for c in dd.get("commits") or []:
        if not isinstance(c, dict):
            continue
        repo = c.get("repo_name") or os.path.basename(str(c.get("repo") or "").rstrip("/\\"))
        out.append({"time": common.hhmm(c.get("date")) if c.get("date") else "", "ticket": c.get("ticket") or "",
                    "repo": repo, "short": c.get("short") or (c.get("sha") or "")[:7], "subject": c.get("subject") or ""})
    out.sort(key=lambda c: c["time"])
    return out


def _sessions(dd):
    s = dd.get("sessions")
    return len(s) if isinstance(s, (dict, list)) else 0


def today(cfg=None, now=None, preview=True):
    """Today's view (see day())."""
    cfg = cfg or common.load_config()
    return _day_view(_today(cfg, now), cfg, preview)


def day(date, cfg=None, preview=True):
    """One work day (YYYY-MM-DD; ValueError when malformed): time per ticket, other work, commits, sessions,
    activity lanes and the Jira worklog (the saved one, else a preview: today's up to now, a past day's whole)."""
    _dt.date.fromisoformat(date)
    return _day_view(date, cfg or common.load_config(), preview)


def _day_view(day, cfg, preview=True):
    """The builder today() and day() share. `preview=False` skips the preview and the lanes (both come from the
    same activity blocks, which need an Agent)."""
    from .. import timesheet as T
    dd = day_data(day) or {}
    rows = []
    for key, slot in (dd.get("tickets") or {}).items():
        rows.append({"key": key, "title": _title(key), "minutes": int(slot.get("minutes") or 0),
                     "first_time": slot.get("first_time") or "", "where": slot.get("where") or [],
                     "did": [w.get("text") for w in (slot.get("what_i_did") or [])[-3:] if isinstance(w, dict)]})
    rows.sort(key=lambda r: (-r["minutes"], r["key"]))
    other = sorted(({"group": g, "minutes": int((v or {}).get("minutes") or 0)} for g, v in (dd.get("other") or {}).items()),
                   key=lambda r: -r["minutes"])
    saved = saved_worklog(day)
    out = {"day": day, "updated_at": dd.get("updated_at"), "closed": bool(dd.get("closed")),
           "summary": dd.get("summary") or "", "tickets": rows, "other": other,
           "total_minutes": sum(r["minutes"] for r in rows) + sum(r["minutes"] for r in other),
           "commits": len(dd.get("commits") or []), "worklog_saved": saved,
           "commit_list": _commit_list(dd), "sessions": _sessions(dd)}
    blocks, pv = [], {}
    if preview:
        pv, blocks = _preview(day, cfg)
        out["worklog_preview"] = pv
    out["lanes"] = _lanes(blocks)
    out["last_activity"] = _last_activity(blocks)
    target = (saved or {}).get("target")
    out["target_minutes"] = target if target is not None else pv.get("target")
    kind = pv.get("day_kind")
    if not kind:
        try:
            kind = T.day_kind(day, read_json_cached(common.hp("data", "calendar.json")), T.settings(cfg))
        except (TypeError, ValueError, AttributeError):
            kind = None
    out["day_kind"] = kind
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


# --------------------------------------------------------------------------- calendar (year heatmap)

CALENDAR_MAX_DAYS = 400


def _date_arg(s, name):
    try:
        return _dt.date.fromisoformat(s)
    except (TypeError, ValueError):
        raise ValueError("bad %s date (YYYY-MM-DD): %r" % (name, str(s)[:40]))


def calendar(frm=None, to=None, cfg=None):
    """Minutes per day and per ticket with each day's kind and target, for a year heatmap. Default range: the 365
    days ending today; longer ranges keep their last 400 days. ValueError on a malformed or reversed range.
    kind: workday | weekend | leave | half_day | holiday | extra_workday (a calendar `workday:` on a day off)."""
    from .. import timesheet as T
    cfg = cfg or common.load_config()
    end = _date_arg(to, "to") if to else _dt.date.fromisoformat(_today(cfg))
    start = _date_arg(frm, "from") if frm else end - _dt.timedelta(days=364)
    if start > end:
        raise ValueError("from is after to")
    start = max(start, end - _dt.timedelta(days=CALENDAR_MAX_DAYS - 1))
    ts = T.settings(cfg)
    try:
        hours = float(ts.get("hours_per_day") or 8)
    except (TypeError, ValueError):
        hours = 8.0
    full = int(round(hours * 60))
    cal = read_json_cached(common.hp("data", "calendar.json")) or {}
    days = []
    d = start
    while d <= end:
        iso = d.isoformat()
        dd = day_data(iso) or {}
        per = {}
        for key, slot in (dd.get("tickets") or {}).items():
            per[key] = per.get(key, 0) + int((slot or {}).get("minutes") or 0)
        other = sum(int((v or {}).get("minutes") or 0) for v in (dd.get("other") or {}).values())
        for m in ((cal.get("time") or {}).get(iso)) or []:  # time logged by hand counts too
            if isinstance(m, dict) and m.get("ticket"):
                per[m["ticket"]] = per.get(m["ticket"], 0) + int(m.get("minutes") or 0)
        kind = T.day_kind(iso, cal, ts)
        note = ""
        for src in ("leave", "holiday", "half_day", "workday"):
            v = (cal.get(src) or {}).get(iso)
            if v is not None:
                note = v if isinstance(v, str) else ""
                break
        if kind == "workday" and iso in (cal.get("workday") or {}) and T.WEEKDAYS[d.weekday()] not in ts["workdays"]:
            kind = "extra_workday"
        target = {"workday": full, "extra_workday": full, "half_day": full // 2}.get(kind, 0)
        days.append({"date": iso, "minutes": sum(per.values()) + other, "tickets": {k: v for k, v in per.items() if v},
                     "kind": kind, "note": note, "target": target})
        d += _dt.timedelta(days=1)
    return {"from": start.isoformat(), "to": end.isoformat(), "hours_per_day": int(hours) if hours == int(hours) else hours,
            "workdays": list(ts.get("workdays") or []), "days": days}


# --------------------------------------------------------------------------- knowledge (catalog + DB link map)

EVIDENCE_MAX = 300
TEXT_MAX = 600
FIELDS_MAX = 40


def _cut(s, n=TEXT_MAX):
    s = s if isinstance(s, str) else ("" if s is None else str(s))
    return s if len(s) <= n else s[:n - 1] + "…"


def _strs(xs, n=FIELDS_MAX):
    return [_cut(x, 120) for x in (xs if isinstance(xs, list) else [])[:n] if x not in (None, "")]


def _schema_columns():
    """{lowercase "schema.table" and bare "table": [column names]} from data/db-schema/<connection>/*.json."""
    root = common.hp("data", "db-schema")
    full, bare = {}, {}
    for conn in _listdir(root):
        cdir = os.path.join(root, conn)
        if conn.startswith("_") or not os.path.isdir(cdir):
            continue
        for fn in _listdir(cdir):
            if not fn.endswith(".json") or fn.startswith("_"):
                continue
            rec = read_json_cached(os.path.join(cdir, fn))
            if not isinstance(rec, dict):
                continue
            name = str(rec.get("table") or fn[:-5]).lower()
            cols = [c.get("name") for c in rec.get("describe") or [] if isinstance(c, dict) and c.get("name")]
            if not cols:
                continue
            full.setdefault(name, cols)
            bare.setdefault(name.rsplit(".", 1)[-1], []).append(cols)
    return full, {k: v[0] for k, v in bare.items() if len(v) == 1}  # a bare name only when it is unambiguous


def _verified_links(lm):
    """Link-map links that QA sampling confirmed (>= 80 % of sampled values found), as lowercase part lists."""
    out = []
    for ln in ((lm or {}).get("result") or {}).get("links") or []:
        try:
            s, m = int(ln.get("sampled") or 0), int(ln.get("matched") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if ln.get("checked", True) and s and m / s >= 0.8:
            out.append((str(ln.get("from") or "").lower().split("."), str(ln.get("to") or "").lower().split(".")))
    return out


def _ends_with(parts, ref):
    return 0 < len(ref) <= len(parts) and parts[-len(ref):] == ref


def _link_matches(a, b, links):
    """A catalog relation a -> b ("table.column", "schema.table.column" or plain tables) matches a verified link
    when each side is a suffix of the link's side (with or without the column), in either direction."""
    a, b = a.lower().split("."), b.lower().split(".")
    for lf, lt in links:
        for x, y in ((lf, lt), (lt, lf)):
            if (_ends_with(x, a) or _ends_with(x[:-1], a)) and (_ends_with(y, b) or _ends_with(y[:-1], b)):
                return True
    return False


def knowledge():
    """The knowledge base the tick builds from sessions: tables (with columns from the QA schema cards), relations
    (verified = confirmed by the QA link map), the code that writes each table, APIs and integrations."""
    cat = read_json_cached(common.hp("data", "catalog.json")) or {}
    lm = read_json_cached(common.hp("data", "db-schema", "_linkmap.json")) or {}
    full, bare = _schema_columns()
    tables = []
    for key, t in sorted((cat.get("tables") or {}).items()):
        if not isinstance(t, dict):
            continue
        name = t.get("name") or key
        cols = full.get(name.lower()) or bare.get(name.lower().rsplit(".", 1)[-1]) or []
        tables.append({"name": name, "db": t.get("db") or "", "purpose": _cut(t.get("purpose")),
                       "key_columns": _strs(t.get("key_columns")), "columns": _strs(cols, 400),
                       "relations": _strs(t.get("relations"), 60), "tickets": _strs(t.get("tickets")),
                       "last_seen": t.get("last_seen") or ""})
    links = _verified_links(lm)
    relations = []
    for r in (cat.get("db_relations") or {}).values():
        if not isinstance(r, dict):
            continue
        a, b = str(r.get("from") or ""), str(r.get("to") or "")
        relations.append({"from": a, "to": b, "kind": r.get("kind") or "", "evidence": _cut(r.get("evidence"), EVIDENCE_MAX),
                          "tickets": _strs(r.get("tickets")), "verified": bool(a and b and _link_matches(a, b, links))})
    relations.sort(key=lambda r: (r["from"], r["to"]))
    writers = []
    for w in (cat.get("db_writers") or {}).values():
        if not isinstance(w, dict):
            continue
        reads = [{"component": (v or {}).get("component") or k, "tickets": _strs((v or {}).get("tickets"))}
                 for k, v in sorted((w.get("reads") or {}).items()) if isinstance(v, dict) or v is None]
        writes = [{"component": (v or {}).get("component") or k.split("|")[0], "column": (v or {}).get("column") or "",
                   "change": _cut((v or {}).get("change"), EVIDENCE_MAX), "tickets": _strs((v or {}).get("tickets"))}
                  for k, v in sorted((w.get("writes") or {}).items()) if isinstance(v, dict) or v is None]
        writers.append({"table": w.get("table") or "", "reads": reads[:60], "writes": writes[:60]})
    writers.sort(key=lambda w: w["table"])
    apis = []
    for a in (cat.get("apis") or {}).values():
        if not isinstance(a, dict):
            continue
        apis.append({"method": (a.get("method") or "").upper(), "path": _cut(a.get("path"), 300),
                     "host": a.get("host_or_service") or a.get("host") or "", "direction": a.get("direction") or "",
                     "auth_type": _cut(a.get("auth_type"), 200), "purpose": _cut(a.get("purpose")),
                     "request_fields": _strs(a.get("request_fields")), "response_fields": _strs(a.get("response_fields")),
                     "tickets": _strs(a.get("tickets")), "last_seen": a.get("last_seen") or ""})
    apis.sort(key=lambda a: (a["host"], a["path"], a["method"]))
    integrations = []
    for k, i in sorted((cat.get("integrations") or {}).items()):
        if isinstance(i, dict):
            integrations.append({"system": i.get("system") or k, "artifact": _cut(i.get("artifact"), 300),
                                 "notes": _cut(i.get("notes")), "tickets": _strs(i.get("tickets")),
                                 "last_seen": i.get("last_seen") or ""})
    res = lm.get("result") if isinstance(lm, dict) else None
    linkmap = None
    if isinstance(res, dict):
        hier = [h for h in res.get("hierarchy") or [] if isinstance(h, dict)]
        linkmap = {"env": lm.get("env") or "", "fetched_at": lm.get("fetched_at") or "",
                   "schemas": len({(h.get("database"), h.get("schema")) for h in hier}),
                   "tables": sum(len(h.get("tables") or []) for h in hier),
                   "links": len(res.get("links") or []), "verified": len(links)}
    return {"tables": tables, "relations": relations, "writers": writers, "apis": apis, "integrations": integrations,
            "linkmap": linkmap}


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
            "last_skip_reason": m.get("last_skip_reason") or "", "live": mcp_live()}


def mcp_live():
    """The last `claude mcp list` check as setup / doctor / maintenance cached it (never run from here):
    {"checked_at", "probed", "servers": [{"role", "server", "status", "detail"}]}, or None before the first check."""
    from .. import plugin
    st = read_json_cached(plugin.mcp_status_cache())
    if not isinstance(st, dict) or not st:
        return None
    servers = []
    for role in ("slack", "jira", "database"):
        r = st.get(role)
        if isinstance(r, dict):
            servers.append({"role": role, "server": r.get("server") or "", "status": r.get("status") or "unknown",
                            "detail": _cut(r.get("detail"), 400)})
    return {"checked_at": st.get("checked") or st.get("checked_at"), "probed": bool(st.get("probed")), "servers": servers}


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
        "search": _search_stats(),
    }


def _search_stats():
    try:
        from . import search
        return search.stats()
    except Exception:  # noqa: BLE001 — the health page shows the rest
        return None


# --------------------------------------------------------------------------- setup checklist

SETUP_TTL = 60.0
_setup_cache = {"at": 0.0, "home": None, "value": None}
_setup_lock = threading.Lock()
# doctor row -> (id, label, status when failing, fix kind, fix text). The fix is what the user runs.
_SETUP_ROWS = {
    "python": ("python", "Python 3.10+", "bad", "none", "Install Python 3.10 or newer with the py launcher, then reopen Claude Code"),
    "git": ("git", "Git", "bad", "none", "Install Git, then reopen Claude Code"),
    "claude": ("claude", "Claude Code CLI", "bad", "cli", "claude install"),
    "home": ("home", "Data home", "bad", "slash", "/cg:setup"),
    "config": ("config", "Configuration", "bad", "slash", "/cg:setup"),
    "hooks": ("hooks", "Session capture (hooks)", "bad", "claude",
              "Run /plugin, make sure the cg plugin is enabled, then start a new Claude Code session"),
    "last tick": ("tick", "Background tick", "warn", "slash", "/cg:setup"),
    "last push": ("push", "GitHub backup push", "warn", "slash", "/cg:doctor"),
    "runtime": ("runtime", "Stable runtime", "bad", "slash", "/cg:setup"),
    "task": ("task", "Scheduled task", "bad", "slash", "/cg:setup"),
    "recall skill": ("skill", "Recall skill", "bad", "slash", "/cg:setup"),
    "CLAUDE.md block": ("claude-md", "CLAUDE.md block", "bad", "slash", "/cg:setup"),
    "Slack": ("slack", "Slack (evening worklog DM)", "warn", "slash", "/cg:setup"),
    "Jira": ("jira", "Jira MCP", "warn", "slash", "/cg:setup"),
    "database MCP": ("database", "Database MCP", "warn", "slash", "/cg:setup"),
    "recent errors": ("errors", "Recent errors", "warn", "slash", "/cg:doctor"),
}
_MCP_ROLE = {"Slack": "slack", "Jira": "jira", "database MCP": "database"}


def _setup_item(row, mcp):
    name = row.get("name") or ""
    rid, label, failing, kind, text = _SETUP_ROWS.get(
        name, (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), name, "warn", "slash", "/cg:doctor"))
    detail = row.get("detail") or ""
    status = "ok" if row.get("ok") else failing
    role = _MCP_ROLE.get(name)
    if role:
        st = (mcp or {}).get(role) or {}
        live = st.get("status")
        if not mcp:
            status = "warn"  # never checked: doctor without --fast (or /cg:setup) runs the live check
            kind, text = "slash", "/cg:doctor"
        elif live in ("missing", "unsupported") or (role == "slack" and st.get("state") == "off"):
            status = "off"
        elif live in ("signed out", "offline") and role != "slack":
            kind, text = "claude", "Run /mcp in Claude Code and sign in to the %s server" % (st.get("server") or label)
    elif name == "last push" and row.get("ok") and "no GitHub remote" in detail:
        status = "off"
    if status == "ok":
        kind, text = "none", ""
    return {"id": rid, "label": label, "status": status, "detail": _cut(detail, 400), "fix": {"kind": kind, "text": text}}


def setup(max_age=SETUP_TTL):
    """A read-only setup checklist from doctor's fast mode (the cached MCP status; never `claude mcp list`).
    doctor still runs `git --version`, `claude --version` and the scheduled-task query, so the result is cached."""
    home = common.home()
    import time
    with _setup_lock:
        c = _setup_cache
        if c["value"] is not None and c["home"] == home and time.monotonic() - c["at"] < max_age:
            return c["value"]
    from .. import doctor
    from .. import setup as setupmod
    rows = doctor.run(probe=False)
    mcp = setupmod.cached_mcp_status()
    value = {"checked_at": common.now_iso(), "items": [_setup_item(r, mcp) for r in rows]}
    with _setup_lock:
        _setup_cache.update(at=time.monotonic(), home=home, value=value)
    return value


# --------------------------------------------------------------------------- change cursor

_cursor_cache = {"at": 0.0, "value": None}


def _watch_paths(cfg):
    day = _today(cfg)
    yield common.wl("health.json")
    yield common.hp("data", "calendar.json")
    yield common.hp("data", "catalog.json")
    yield common.hp("data", "db-schema", "_linkmap.json")
    yield common.hp("daily", day[:4], day[5:7], day + ".md")
    from .. import plugin
    yield plugin.mcp_status_cache()
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
