"""Daily Jira worklog -> Slack DM: agent wiring around timesheet.py (deterministic) and the few model calls.

State: data/calendar.json (leave / half days / holidays / extra workdays / manual time), data/timesheets/<day>.json
(entries, message versions, status, Slack ts). One message per day, plus one "Corrected update" when an inbox line
changes a day that was already messaged (up to 3 days back, debounced 15 min).
"""
import datetime as _dt
import json
import os
import re
import traceback

from . import common
from . import extract
from . import mcpjobs
from . import slack
from . import timesheet as T
from .redact import redact

EXAMPLES = [
    "Retested the document upload fix on QA and checked the file shows up in the admin portal. Wrote the RCA comment.",
    "Found why document download fails, reproduced it on QA, and saved the steps to test it.",
    "Checked the fix for all account access on/off cases, ran the tests, replied to the review comment and wrote the RCA.",
]

# --------------------------------------------------------------------------- description guard

_PATH = re.compile(r"[A-Za-z]:[\\/]|(?:^|[\s(])[.~]?/[\w.-]+/|\\[\w.-]+\\|\b[\w-]+\.(?:java|py|js|ts|tsx|jsx|sql|json|xml|bru|"
                   r"yml|yaml|md|html|cs|sh|ps1|kt|go|rb|php|properties|csv|txt|log|env)\b", re.I)
_SQL = re.compile(r"\b(?:SELECT|UPDATE|INSERT|DELETE|JOIN|WHERE|FROM\s+\w+\.\w+|GROUP BY|ORDER BY|ALTER|DROP|TRUNCATE)\b|"
                  r"\bselect\s+\S+\s+from\b|\b\w+\.\w+\.\w+\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_DIGITS = re.compile(r"\d{5,}")
_IDENT = re.compile(r"\b[a-z]+_[a-z0-9_]+\b|\b[a-z]+[A-Z][A-Za-z]+\b|\b[A-Z][a-z]+[A-Z][a-z]+[A-Za-z]*\b|\(\)|::")
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


def line_ok(line, ticket=None):
    """None when the line is fine for a Jira description, else why it is rejected."""
    if not isinstance(line, str) or not line.strip():
        return "empty"
    for rx, why in ((_PATH, "path or file name"), (_SQL, "SQL"), (_EMAIL, "email"), (_DIGITS, "long digit run"),
                    (_IDENT, "code identifier")):
        if rx.search(line):
            return why
    if any(t != ticket for t in _TICKET.findall(line)):
        return "other ticket id"
    if len(line.split()) > 40:
        return "too long"
    if redact(line, mode="public") != line:
        return "sensitive value"
    return None


def words(s):
    return len((s or "").split())


# --------------------------------------------------------------------------- state files

def cal_path():
    return common.hp("data", "calendar.json")


def load_cal():
    return common.read_json(cal_path(), {}) or {}


def sheet_path(day):
    return common.hp("data", "timesheets", day + ".json")


def load_sheet(day):
    return common.read_json(sheet_path(day), {}) or {}


def save_sheet(day, sh):
    sh["day"] = day
    common.write_json(sheet_path(day), sh)


class JiraWorklog:
    def __init__(self, agent):
        self.a = agent
        self.cfg = agent.cfg
        self.ts = T.settings(self.cfg)

    @property
    def store(self):
        return self.a.store

    # ------------------------------------------------------------------ inbox directives (§5)
    def handle_inbox(self, n):
        """True when the inbox line was a worklog directive (consumed here)."""
        today = common.workday(common.now(), self.cfg)
        d = T.parse_directive(n.get("raw") or n.get("text") or "", today)
        if not d:
            return False
        h = self.a.health.setdefault("jira_worklog", {})
        if d["kind"] == "invalid":
            h["invalid_inbox"] = (h.get("invalid_inbox") or [])[-4:] + [d["line"][:120]]
            return True
        cal = load_cal()
        if d["kind"] == "resend":
            for day in d["days"]:
                sh = load_sheet(day)
                sh["resend"] = common.now_iso()
                save_sheet(day, sh)
            return True
        days = T.apply_directive(cal, d)
        common.write_json(cal_path(), cal)
        for day in days:
            sh = load_sheet(day)
            if sh.get("status") in ("sent", "nudged") and (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(day)).days <= 3:
                sh["pending_correction"] = common.now_iso()  # debounced: sent >= 15 min after the last change
                save_sheet(day, sh)
        return True

    # ------------------------------------------------------------------ extraction additions (§3)
    def current_lines(self, day, cands, first_ts):
        dd = self.store.day(day)
        out = {}
        gap = self.ts["block_gap_minutes"] * 60
        for t in cands:
            lst = (dd.get("worklog_lines") or {}).get(t) or []
            if lst and first_ts and (first_ts - common.parse_ts(lst[-1]["to"])).total_seconds() < gap:
                out[t] = lst[-1]["line"]
        return out

    def merge_extraction(self, day, res, evs):
        dd = self.store.day(day)
        gap = self.ts["block_gap_minutes"] * 60
        for wl in res.get("worklog_lines") or []:
            t, line = (wl.get("ticket") or "").upper(), " ".join((wl.get("line") or "").split())
            if not t or not line:
                continue
            stamps = [common.parse_ts(e.get("ts")) for e in evs if e.get("_t") == t and e.get("ts")] or \
                     [common.parse_ts(e.get("ts")) for e in evs if e.get("ts")]
            stamps = [s for s in stamps if s]
            if not stamps:
                continue
            f, to = min(stamps), max(stamps)
            lst = dd.setdefault("worklog_lines", {}).setdefault(t, [])
            why = line_ok(line, t)
            if why:
                common.log("agent", "worklog line rejected (%s) for %s" % (why, t))
                if lst and (f - common.parse_ts(lst[-1]["to"])).total_seconds() < gap:
                    lst[-1]["to"] = max(to, common.parse_ts(lst[-1]["to"])).isoformat(timespec="seconds")
                continue
            if lst and (f - common.parse_ts(lst[-1]["to"])).total_seconds() < gap:
                lst[-1].update(line=line, to=max(to, common.parse_ts(lst[-1]["to"])).isoformat(timespec="seconds"))
            else:
                lst.append({"from": f.isoformat(timespec="seconds"), "to": to.isoformat(timespec="seconds"), "line": line})
            self.store.touch_day(day)
        entries = res.get("time_entries") or []
        if entries:
            cal = load_cal()
            for te in entries:
                try:
                    dday = te.get("date") or day
                    _dt.date.fromisoformat(dday)
                    m = int(te.get("minutes") or 0)
                except (ValueError, TypeError):
                    continue
                if m <= 0 or not te.get("ticket"):
                    continue
                st = te.get("start") if re.fullmatch(r"\d{1,2}:\d{2}", te.get("start") or "") else None
                T.apply_directive(cal, {"kind": "time", "days": [dday], "start": st and st.zfill(5), "minutes": m,
                                        "ticket": te["ticket"].upper(), "text": " ".join((te.get("text") or "").split())})
            common.write_json(cal_path(), cal)

    # ------------------------------------------------------------------ activity -> blocks (§2.1)
    def points(self, day, until=None, jira_comments=None):
        start, end = common.day_bounds(day, self.cfg)
        until = min(until or end, end)
        pm = self.ts["point_minutes"]
        pts = []

        def add(ts, t, dur=0):
            if ts and start <= ts < until:
                pts.append({"ts": ts, "ticket": t, "dur": dur})
        for ev in self.a.day_events(day):
            add(common.parse_ts(ev.get("ts")), ev.get("_t"))
        # sessions inside the worklog folder: time-only events, ticket via the session's last mentioned ticket
        tdir = common.wl("raw", day, "time")
        sticky = {}
        for fn in sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []:
            rows = []
            for line in common.read_text(os.path.join(tdir, fn)).splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
            for r in sorted(rows, key=lambda r: r.get("ts") or ""):
                if r.get("ticket"):
                    sticky[r.get("session_id")] = r["ticket"]
                t = sticky.get(r.get("session_id"))
                if t:  # worklog-system sessions without a ticket are not ticket work
                    add(common.parse_ts(r.get("ts")), t)
        dd = self.store.day(day)
        for c in dd.get("commits") or []:
            if c.get("ticket"):
                add(common.parse_ts(c.get("date")), c["ticket"], pm)
        for b in (dd.get("outside") or {}).get("bruno") or []:
            if b.get("ticket"):
                add(common.parse_ts(b.get("ts")), b["ticket"], pm)
        gen = (common.read_json(common.hp("data", "generated.json"), {}) or {}).get("files") or {}
        broot = common.hp("bruno")
        for t in os.listdir(broot) if os.path.isdir(broot) else []:
            if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}-\d+", t):
                continue
            for fn in os.listdir(os.path.join(broot, t)):
                rel = "bruno/%s/%s" % (t, fn)
                if (gen.get(rel) or {}).get("owner") == "engine":
                    continue
                try:
                    mt = _dt.datetime.fromtimestamp(os.path.getmtime(os.path.join(broot, t, fn))).astimezone()
                except OSError:
                    continue
                add(mt, t, pm)
        for c in jira_comments or []:
            add(common.parse_ts(c.get("time")), c.get("ticket"), pm)
        return pts

    def blocks(self, day, until=None, jira_comments=None):
        bl = T.build_blocks(self.points(day, until, jira_comments), self.ts["block_gap_minutes"])
        lines = (self.store.day(day).get("worklog_lines") or {})
        gap = _dt.timedelta(minutes=self.ts["block_gap_minutes"])
        used = set()
        for b in bl:
            for k, ln in enumerate(lines.get(b["ticket"]) or []):
                f = common.parse_ts(ln["from"])
                if (b["ticket"], k) not in used and f and b["start"] - gap <= f <= b["end"] + gap:
                    b["lines"].append(ln["line"])
                    used.add((b["ticket"], k))
        # lines whose block fell outside the window still describe that ticket's nearest block
        for t, lst in lines.items():
            for k, ln in enumerate(lst):
                if (t, k) in used:
                    continue
                same = [b for b in bl if b["ticket"] == t]
                if same:
                    f = common.parse_ts(ln["from"])
                    min(same, key=lambda b: abs((b["start"] - f).total_seconds()))["lines"].append(ln["line"])
        # days from before worklog lines existed: the session summaries ("what I did") become raw notes for the
        # condense call, each on the ticket's nearest block
        dd = self.store.day(day)
        d0 = _dt.date.fromisoformat(day)
        for t, slot in (dd.get("tickets") or {}).items():
            if lines.get(t):
                continue
            same = [b for b in bl if b["ticket"] == t]
            for w in slot.get("what_i_did") or []:
                if not same or not w.get("text"):
                    continue
                try:
                    hh, mm = map(int, (w.get("time") or "12:00").split(":"))
                    at = _dt.datetime.combine(d0, _dt.time(hh, mm)).astimezone()
                except ValueError:
                    at = same[0]["start"]
                b = min(same, key=lambda b: abs((b["start"] - at).total_seconds()))
                b.setdefault("notes", []).append(w["text"])
        return bl

    # ------------------------------------------------------------------ descriptions (§3)
    def fallback(self, ticket):
        tk = self.store.ticket(ticket)
        items = sorted((tk.get("items") or {}).values(), key=lambda i: (i.get("date") or "", i.get("time") or ""), reverse=True)
        for i in items:
            if i.get("kind") in ("finding", "root_cause", "fix", "how_it_works"):
                title = (i.get("title") or "").rstrip(".")
                cand = "Worked on %s: %s." % (ticket, title[:1].lower() + title[1:])
                if not line_ok(cand, ticket) and words(cand) <= 25 and not re.search(r"\d|[+/:=]", title):
                    return cand
        return "Worked on %s." % ticket

    def describe(self, entries, condense=True, facts=None):
        """Fill e['desc']: manual text | single line | joined lines (<= 30 words) | one batched model call when
        notes need condensing or Jira facts of the day (my comments, status moves) belong in an entry."""
        jf = self._facts_per_entry(entries, facts)
        need = []
        for i, e in enumerate(entries):
            if e.get("manual"):
                e["desc"] = e.get("text") or ""
                continue
            ls = []
            for l in e.get("lines") or []:
                if l not in ls:
                    ls.append(l)
            if not ls and e.get("notes"):
                e["desc"] = self.fallback(e["ticket"])
                need.append((i, e["ticket"], [common.truncate(x, 600) for x in e["notes"][:6]]))
            elif not ls:
                e["desc"] = self.fallback(e["ticket"])
                if jf.get(i):
                    need.append((i, e["ticket"], []))
            elif len(ls) == 1 and not jf.get(i):
                e["desc"] = ls[0]
            elif words(" ".join(ls)) <= 30 and not jf.get(i):
                e["desc"] = " ".join(x if x.endswith(".") else x + "." for x in ls)
            else:
                e["desc"] = " ".join(ls) if words(" ".join(ls)) <= 30 else ls[0]
                need.append((i, e["ticket"], ls))
        if need and condense:
            try:
                rows = []
                for i, t, ls in need:
                    row = "%d. %s: %s" % (i, t, " | ".join(ls) or "(no notes)")
                    if jf.get(i):
                        row += "\n   Jira today: " + " | ".join(jf[i])
                    rows.append(row)
                out, cost = extract.call_claude("worklog_condense", "\n".join(rows), self.cfg, timeout=180)
                self._cost("describe", cost)
                for d in out.get("descriptions") or []:
                    k = d.get("i")
                    if not (isinstance(k, int) and 0 <= k < len(entries)):
                        continue
                    why = line_ok(d.get("text"), entries[k]["ticket"])
                    if why:
                        common.log("agent", "condensed line rejected (%s): %s" % (why, common.truncate(d.get("text"), 200)))
                    else:
                        entries[k]["desc"] = d["text"].strip()
            except Exception as ex:  # noqa: joined lines stay
                common.log("agent", "condense failed: %r" % ex)
        for e in entries:
            if not e.get("manual") and line_ok(e.get("desc"), e["ticket"]):
                e["desc"] = self.fallback(e["ticket"])
        return entries

    def _facts_per_entry(self, entries, facts):
        """Jira facts of the day -> {entry index: ["comment (RCA ...): <snippet>", "moved to READY FOR TESTING"]}"""
        out = {}
        if not facts:
            return out
        items = [(f["ticket"], f["time"], "my comment: " + common.truncate(f.get("text") or "", 160)) for f in facts.get("comments") or []]
        items += [(f["ticket"], f["time"], "status moved to %s%s" % (f.get("to"), "" if f.get("mine") else " (by someone else)"))
                  for f in facts.get("status_changes") or []]
        for t, ts, text in items:
            cand = [i for i, e in enumerate(entries) if e["ticket"] == t and not e.get("manual")]
            if not cand:
                continue
            at = common.parse_ts(ts)
            best = cand[-1]
            for i in cand:
                if any(s - _dt.timedelta(minutes=20) <= at <= e + _dt.timedelta(minutes=20) for s, e in entries[i].get("spans") or []
                       if s and e):
                    best = i
                    break
            out.setdefault(best, []).append(text)
        return out

    def _cost(self, what, cost):
        h = self.a.health.setdefault("jira_worklog", {})
        day = common.workday(common.now(), self.cfg)
        c = h.setdefault("cost", {}).setdefault(day, {})
        c[what] = round(c.get(what, 0) + float(cost or 0), 4)
        for d in list(h["cost"]):
            if d < (_dt.date.fromisoformat(day) - _dt.timedelta(days=14)).isoformat():
                del h["cost"][d]

    # ------------------------------------------------------------------ compose (§2, §4, §7.2)
    def manual_for(self, day, cal):
        return [dict(m) for m in ((cal.get("time") or {}).get(day) or [])]

    def compose(self, day, send_day=None, correction=False, use_jira=True, until=None, describe=True):
        send_day = send_day or common.workday(common.now(), self.cfg)
        cal = load_cal()
        bl = self.blocks(day, until)
        manual = self.manual_for(day, cal)
        facts = None
        if use_jira and mcpjobs.enabled(self.cfg, "jira"):
            tickets = sorted({b["ticket"] for b in bl if b.get("ticket")} | {m["ticket"] for m in manual})
            if tickets:
                try:
                    facts = mcpjobs.jira_evening(day, tickets, self.cfg, self.a.health)
                    self.a.health.setdefault("jira_worklog", {})["jira_facts"] = common.now_iso()
                except mcpjobs.JobError as ex:
                    common.log("agent", "jira evening facts skipped: %s" % ex)
                    self.a.health.setdefault("jira_worklog", {})["jira_skipped"] = "%s %s" % (common.now_iso(), ex)
                except Exception:
                    common.log("agent", "jira evening facts failed: " + traceback.format_exc())
        already = []
        if facts:
            bl = self.blocks(day, until, facts.get("comments"))
            per = {}
            for w in facts.get("worklogs") or []:
                per[w["ticket"]] = per.get(w["ticket"], 0) + int(w.get("minutes") or 0)
            already = sorted(per.items())

        def fill(entries):
            self.describe(entries, condense=describe, facts=facts)
        res = T.compose(day, send_day, bl, manual, cal, self.ts, already=already, correction=correction,
                        describe=fill if describe else None)
        if res.get("kind") == "message":
            res["text"] = redact(res["text"], mode="public")
        res["facts"] = bool(facts)
        return res

    # ------------------------------------------------------------------ schedule (§10)
    def send_time(self, day):
        h, m = map(int, self.ts["send_at"].split(":"))
        return _dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(h, m)).astimezone()

    def due_days(self, now=None):
        now = now or common.now()
        today = common.workday(now, self.cfg)
        since = (self.a.health.get("jira_worklog") or {}).get("active_from") or today
        out = []
        for k in range(int(self.ts["late_days"]), -1, -1):
            day = (_dt.date.fromisoformat(today) - _dt.timedelta(days=k)).isoformat()
            if day < since:
                continue
            sh = load_sheet(day)
            if day == today and now < self.send_time(day) and not sh.get("resend"):
                continue
            if sh.get("resend"):
                out.append((day, "resend"))
            elif sh.get("pending_correction"):
                pc = common.parse_ts(sh["pending_correction"])
                if (now - pc).total_seconds() >= self.ts["correction_debounce_minutes"] * 60:
                    out.append((day, "correction"))
            elif sh.get("status") in (None, "", "failed") and int(sh.get("attempts") or 0) < 4:
                out.append((day, "first"))
        return out

    def tick(self):
        if not self.ts.get("enabled", True):
            return
        h = self.a.health.setdefault("jira_worklog", {})
        h.setdefault("active_from", common.workday(common.now(), self.cfg))
        due = self.due_days()
        if not due:
            return
        for day, why in due:
            if why in ("first", "resend"):  # make the lines current before composing
                try:
                    self.a.run_extractions(only_day=day, any_size=True)
                except Exception:
                    common.log("agent", "pre-send extraction failed: " + traceback.format_exc())
            self.run_day(day, why)

    def run_day(self, day, why, send=True):
        sh = load_sheet(day)
        now = common.now()
        today = common.workday(now, self.cfg)
        until = now if day == today else None
        res = self.compose(day, today, correction=(why == "correction"), until=until)
        sh.pop("resend", None)
        sh.pop("pending_correction", None)
        if res["kind"] == "nudge" and day != today:
            res = {"kind": "none", "reason": "nothing tracked (a late nudge is not sent)"}
        if res["kind"] == "none":
            sh.update(status="none", reason=res.get("reason"), checked_at=common.now_iso())
            save_sheet(day, sh)
            return sh
        text = res["text"]
        ver = {"v": len(sh.get("versions") or []) + 1, "at": common.now_iso(), "kind": res["kind"], "why": why, "text": text,
               "jira_facts": res.get("facts", False)}
        if res["kind"] == "message":
            sh["entries"] = [{k: (v.isoformat() if isinstance(v, _dt.datetime) else v) for k, v in e.items()
                              if k in ("ticket", "minutes", "start_hhmm", "desc", "manual", "known")} for e in res["entries"]]
            sh["tracked"], sh["target"] = res.get("tracked"), res.get("target")
        sh.setdefault("versions", []).append(ver)
        sh["status"] = "composed"
        save_sheet(day, sh)
        self.store.touch_day(day)  # the daily file shows the message under "## Jira worklog"
        if not send:
            return sh
        r = slack.send(self.cfg, text, self.a.health)
        h = self.a.health.setdefault("jira_worklog", {})
        ver["slack"] = {k: r.get(k) for k in ("ok", "ts", "error", "tokens", "mismatch")}
        if r.get("tokens"):
            h.setdefault("tokens", {})[day] = r["tokens"]
        if r.get("ok"):
            sh.update(status="nudged" if res["kind"] == "nudge" else "sent", sent_at=common.now_iso(), attempts=0,
                      slack_ts=r.get("ts"))
            h.pop("send_warning", None)
        else:
            sh["attempts"] = int(sh.get("attempts") or 0) + 1
            sh["status"] = "failed"
            sh["last_error"] = r.get("error")
            if sh["attempts"] >= 4:
                h["send_warning"] = "%s: Slack send failed %d times (%s); the message is in the daily file" % (
                    day, sh["attempts"], (r.get("error") or "")[:120])
        save_sheet(day, sh)
        return sh


# --------------------------------------------------------------------------- daily file section

def daily_section(day):
    sh = load_sheet(day)
    vs = sh.get("versions") or []
    if not vs:
        return []
    v = vs[-1]
    st = {"sent": "sent to Slack %s" % common.hhmm(sh.get("sent_at")), "nudged": "nudge sent", "failed": "Slack send failed — retrying",
          "composed": "composed, not sent yet"}.get(sh.get("status"), sh.get("status") or "")
    return ["## Jira worklog", "_%s · version %d_" % (st, v["v"]), "", "~~~text", v["text"], "~~~", ""]
