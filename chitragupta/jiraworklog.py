"""Daily Jira worklog -> Slack DM: agent wiring around timesheet.py (deterministic) and the few model calls.

State: data/calendar.json (leave / half days / holidays / extra workdays / manual time), data/timesheets/<day>.json
(entries, message versions, status, Slack ts). One message per day, plus one "Corrected update" when an inbox line
changes a day that was already messaged (up to 3 days back, debounced 15 min).

One builder, JiraWorklog.build(), makes the 20:00 text; the sender (run_day) and every preview (dashboard, `/cg:today`,
`agent worklog-show`) call it, so a preview from the same inputs is the message byte for byte. A preview reads the
latest evening-facts cache (data/jira/_evening/<day>.json) and the condensed-line cache (.worklog/cache/condense/<day>.json)
and calls neither Jira nor the model; refresh_facts() and preview_exact() are the two asked-for calls, within budget.
"""
import datetime as _dt
import hashlib
import json
import os
import re
import threading
import traceback

from . import common
from . import configstore
from . import extract
from . import mcpjobs
from . import slack
from . import timesheet as T
from .redact import redact
from .safety import paths

_AUTO = object()  # build(until=...): "now" for the current work day, the whole day otherwise
RESEND_WINDOW_S = 120  # a Resend request with the same id inside this window counts once
CONDENSE_KEEP_DAYS = 14
FACTS_REUSE_S = 30 * 60  # a Slack retry reuses evening facts fetched this recently (no second Jira job)

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


def evening_cache(day):
    """The latest evening-facts record of `day` ({"facts", "fetched_at", ...}) or {} (written by mcpjobs.jira_evening)."""
    rec = common.read_json(common.hp("data", "jira", "_evening", day + ".json"), {}) or {}
    return rec if isinstance(rec.get("facts"), dict) else {}


def condense_path(day):
    return common.wl("cache", "condense", day + ".json")


def _row_key(row):
    return hashlib.sha1(row.encode("utf-8")).hexdigest()[:20]


def prune_condense_cache(today, keep_days=CONDENSE_KEEP_DAYS):
    root = common.wl("cache", "condense")
    cutoff = (_dt.date.fromisoformat(today) - _dt.timedelta(days=keep_days)).isoformat()
    for fn in os.listdir(root) if os.path.isdir(root) else []:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", fn) and fn[:-5] < cutoff:
            try:
                paths.delete(os.path.join(root, fn))
            except OSError:
                pass


class JiraWorklog:
    def __init__(self, agent):
        self.a = agent
        self.cfg = agent.cfg
        self.ts = T.settings(self.cfg)
        self.condense_calls = 0  # model condense calls made by this instance (preview_exact counts them)

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
            recent = (_dt.date.fromisoformat(today) - _dt.date.fromisoformat(day)).days <= 3
            if sh.get("status") in ("sent", "nudged") and recent:
                sh["pending_correction"] = common.now_iso()  # debounced: sent >= 15 min after the last change
                save_sheet(day, sh)
            elif sh.get("status") == "not_connected" and recent:
                sh["resend"] = common.now_iso()  # never sent: re-compose the saved message (no "Corrected" header)
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
            if not t or not line or common.is_placeholder(t):
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
                if m <= 0 or not te.get("ticket") or common.is_placeholder(te["ticket"]):
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
            if t and common.is_placeholder(t):  # a configured example key is never work
                return
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
                if r.get("ticket") and not common.is_placeholder(r["ticket"]):
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

    def describe(self, entries, condense="model", facts=None, day=None):
        """Fill e['desc']: manual text | single line | joined lines (<= 30 words) | a condensed line when notes need
        condensing or Jira facts of the day (my comments, status moves) belong in an entry.
        condense: "model" (True) = cached condensed lines, one batched model call for the rest (then cached) |
        "cache" = cached lines only; an entry still to condense keeps its joined text and gets pending_condense |
        False = no condensing. The cache (per `day`) is keyed by a hash of each entry's condense input."""
        if condense is True:
            condense = "model"
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
        if need and condense in ("model", "cache"):
            rows = {}
            for i, t, ls in need:
                row = "%s: %s" % (t, " | ".join(ls) or "(no notes)")
                if jf.get(i):
                    row += "\n   Jira today: " + " | ".join(jf[i])
                rows[i] = row
            cache = (common.read_json(condense_path(day), {}) or {}) if day else {}
            lines = cache.get("rows") if isinstance(cache.get("rows"), dict) else {}
            missing = []
            for i in rows:
                hit = lines.get(_row_key(rows[i]))
                if not isinstance(hit, dict):
                    missing.append(i)
                elif hit.get("text"):  # a cached "text": null = the model gave nothing usable: the joined text stays
                    entries[i]["desc"] = hit["text"]
            if missing and condense == "model":
                self._condense(entries, rows, missing, lines, day)
            elif missing:
                for i in missing:
                    entries[i]["pending_condense"] = True
        for e in entries:
            if not e.get("manual") and line_ok(e.get("desc"), e["ticket"]):
                e["desc"] = self.fallback(e["ticket"])
        return entries

    def _condense(self, entries, rows, missing, lines, day):
        """One batched model call for the entries in `missing`; every answer (or "nothing usable") is cached."""
        try:
            stdin = "\n".join("%d. %s" % (i, rows[i]) for i in missing)
            out, cost = extract.call_claude("worklog_condense", stdin, self.cfg, timeout=180)
            self.condense_calls += 1
            self._cost("describe", cost)
        except Exception as ex:  # noqa: joined lines stay (and nothing is cached: the next build tries again)
            common.log("agent", "condense failed: %r" % ex)
            return
        got = {}
        for d in out.get("descriptions") or []:
            k = d.get("i")
            if not (isinstance(k, int) and k in missing):
                continue
            why = line_ok(d.get("text"), entries[k]["ticket"])
            if why:
                common.log("agent", "condensed line rejected (%s): %s" % (why, common.truncate(d.get("text"), 200)))
            else:
                got[k] = d["text"].strip()
                entries[k]["desc"] = got[k]
        if not day:
            return
        at = common.now_iso()
        for i in missing:
            lines[_row_key(rows[i])] = {"ticket": entries[i]["ticket"], "text": got.get(i), "at": at}
        try:
            common.write_json(condense_path(day), {"day": day, "rows": lines})
        except Exception:  # a cache that can't be written costs a model call next time, nothing more
            common.log("agent", "condense cache not written: " + traceback.format_exc())

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
        return [dict(m) for m in ((cal.get("time") or {}).get(day) or []) if not common.is_placeholder(m.get("ticket"))]

    def fact_tickets(self, blocks, manual):
        return sorted({b["ticket"] for b in blocks if b.get("ticket")} | {m["ticket"] for m in manual})

    def build(self, day, send_day=None, *, until=_AUTO, facts="live", condense="model", correction=False, delivery=True):
        """The 20:00 message: the one builder the sender and every preview call.
        until: activity up to then (default: now for the current work day, the whole day otherwise).
        facts: "live" = one read-only jira-evening job (the sender) | "recent" = the evening-facts cache when it was
        fetched within FACTS_REUSE_S, else live (a Slack retry) | "cached" = the latest evening-facts cache, no call
        (a preview) | None = no Jira. Only when mcp.jira.enabled; they add my Jira comments as activity points and
        subtract time already logged in Jira.
        condense: "model" | "cache" | False, see describe().
        -> T.compose's result ("kind", "text", "entries", "note", "target", "tracked", ...) with the public pass and
        the delivery-check block applied to "text", plus "facts" (bool), "facts_at" (ISO time of the Jira data or
        None), "facts_source" ("live" | "cached" | None), "pending_condense" (bool; entries carry it too),
        "delivery" (the block or "") and "blocks" (the activity blocks it was built from)."""
        now = common.now()
        send_day = send_day or common.workday(now, self.cfg)
        if until is _AUTO:
            until = now if day == send_day else None
        cal = load_cal()
        bl = self.blocks(day, until)
        manual = self.manual_for(day, cal)
        fx, facts_at, source = None, None, None
        if facts and mcpjobs.enabled(self.cfg, "jira") and configstore.on(self.cfg, "mcp.jira.evening"):
            tickets = self.fact_tickets(bl, manual)
            rec = evening_cache(day) if tickets and facts == "recent" else {}
            fetched = common.parse_ts(rec.get("fetched_at"))
            if rec.get("facts") is not None and fetched and (now - fetched).total_seconds() <= FACTS_REUSE_S:
                fx, facts_at, facts = rec["facts"], rec["fetched_at"], "cached"  # a retry minutes later: no new call
            elif tickets and facts in ("live", "recent"):
                facts = "live"
                try:
                    fx = mcpjobs.jira_evening(day, tickets, self.cfg, self.a.health)
                    facts_at = common.now_iso()
                    self.a.health.setdefault("jira_worklog", {})["jira_facts"] = facts_at
                except mcpjobs.JobError as ex:
                    common.log("agent", "jira evening facts skipped: %s" % ex)
                    self.a.health.setdefault("jira_worklog", {})["jira_skipped"] = "%s %s" % (common.now_iso(), ex)
                except Exception:
                    common.log("agent", "jira evening facts failed: " + traceback.format_exc())
            elif tickets and facts == "cached":
                rec = evening_cache(day)
                fx, facts_at = rec.get("facts"), rec.get("fetched_at")
            if fx:
                source = facts
            else:
                fx = facts_at = None
        already = []
        if fx:
            bl = self.blocks(day, until, fx.get("comments"))
            per = {}
            for w in fx.get("worklogs") or []:
                per[w["ticket"]] = per.get(w["ticket"], 0) + int(w.get("minutes") or 0)
            already = sorted(per.items())

        def fill(entries):
            self.describe(entries, condense=condense, facts=fx, day=day)
        res = T.compose(day, send_day, bl, manual, cal, self.ts, already=already, correction=correction, describe=fill,
                        placeholders=common.placeholder_tickets(self.cfg))
        if res["kind"] == "nudge" and day != send_day:
            res = {"kind": "none", "reason": "nothing tracked (a late nudge is not sent)"}
        res["delivery"] = ""
        if res.get("kind") == "message":
            res["text"] = redact(res["text"], mode="public")
            if delivery:
                try:  # after the closing note; the Jira entries above stay exactly as composed
                    day_tickets = sorted({e["ticket"] for e in res.get("entries") or [] if e.get("ticket")} |
                                         {t for t in (self.store.day(day).get("tickets") or {}) if not common.is_placeholder(t)})
                    res["delivery"] = delivery_block(self.store, day_tickets)
                    if res["delivery"]:
                        res["text"] = res["text"] + "\n\n" + res["delivery"]
                except Exception:
                    common.log("agent", "delivery check block failed: " + traceback.format_exc())
        res.update(facts=bool(fx), facts_at=facts_at, facts_source=source, blocks=bl,
                   pending_condense=any(e.get("pending_condense") for e in res.get("entries") or []))
        return res

    def compose(self, day, send_day=None, correction=False, use_jira=True, until=None, describe=True):
        """build() without the delivery block (the Jira part only); kept for older callers."""
        return self.build(day, send_day, until=until, facts="live" if use_jira else None,
                          condense="model" if describe else False, correction=correction, delivery=False)

    def preview(self, day, send_day=None):
        """What the sender would send for `day` now, from the cached Jira facts and cached condensed lines: no Jira
        call, no model call, nothing written. Entries still to condense carry pending_condense."""
        return self.build(day, send_day, facts="cached", condense="cache")

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
                out.append((day, "retry" if sh.get("status") == "failed" and sh.get("attempts") else "first"))
        return out

    def tick(self):
        if not self.ts.get("enabled", True):
            return
        h = self.a.health.setdefault("jira_worklog", {})
        h.setdefault("active_from", common.workday(common.now(), self.cfg))
        sstate = slack.state(self.cfg)
        if sstate == "ready":
            h.pop("slack", None)
            h.pop("slack_note", None)
        else:  # shown by doctor / health; never a SessionStart warning
            h["slack"] = sstate
            h["slack_note"] = slack.OFF_NOTE if sstate == "off" else slack.NOT_CONNECTED_NOTE
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
        """Compose and send `day`. why: first | retry (a failed send again: reuses the evening facts fetched within
        FACTS_REUSE_S) | resend | correction."""
        sh = load_sheet(day)
        today = common.workday(common.now(), self.cfg)
        res = self.build(day, today, facts="recent" if why == "retry" else "live", condense="model",
                         correction=(why == "correction"))
        sh.pop("resend", None)
        sh.pop("pending_correction", None)
        if res["kind"] == "none":
            sh.update(status="none", reason=res.get("reason"), checked_at=common.now_iso())
            save_sheet(day, sh)
            return sh
        text = res["text"]
        ver = {"v": len(sh.get("versions") or []) + 1, "at": common.now_iso(), "kind": res["kind"], "why": why, "text": text,
               "jira_facts": res.get("facts", False), "jira_facts_at": res.get("facts_at")}
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
        h = self.a.health.setdefault("jira_worklog", {})
        sstate = slack.state(self.cfg)
        if sstate != "ready":  # no Slack: the message stays in the daily file and the timesheet; no retries
            sh.update(status="not_connected", slack=sstate, checked_at=common.now_iso())
            ver["slack"] = {"ok": False, "state": sstate}  # nothing was sent; the history says why
            sh.pop("attempts", None)
            save_sheet(day, sh)
            h["slack"] = sstate
            h["slack_note"] = slack.OFF_NOTE if sstate == "off" else slack.NOT_CONNECTED_NOTE
            return sh
        h.pop("slack", None)
        h.pop("slack_note", None)
        r = slack.send(self.cfg, text, self.a.health)
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


# --------------------------------------------------------------------------- delivery check (branch tracking)

_PR_NO = re.compile(r"PR #(\d+)")
_HINT = re.compile(r":\s*/cg:branch\s+\S+(?:\s+\S+){1,3}\s*$")


def _public(text):
    """The public pass, except PR numbers (the digit-run rule would mask them; they aren't sensitive)."""
    keep = []

    def hold(m):
        keep.append(m.group(1))
        return "PR #\x00%d\x00" % (len(keep) - 1)
    out = redact(_PR_NO.sub(hold, text), mode="public")
    return re.sub(r"PR #\x00(\d+)\x00", lambda m: "PR #" + keep[int(m.group(1))], out)


def delivery_block(store, tickets, max_lines=12):
    """'Delivery check' lines for the day's tickets with branch alerts, or "" (no extra model call: same send)."""
    from . import render
    lines = []
    for t in tickets:
        for a in render.git_alerts(store.ticket(t)):
            shas = a.get("commits") or []
            text = _HINT.sub(".", a["text"])  # the /cg:branch hint stays on the ticket page and in the session line
            lines.append("• %s: %s%s" % (t, text, " (%s%s)" % (", ".join(shas[:3]), ", …" if len(shas) > 3 else "")
                                          if shas and a.get("kind") in ("lost", "only-copy", "unpushed", "gap") else ""))
    if not lines:
        return ""
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["• … %d more on the ticket pages" % (len(lines) - max_lines)]
    return _public("Delivery check\n" + "\n".join(lines))


# --------------------------------------------------------------------------- daily file section

def daily_section(day):
    sh = load_sheet(day)
    vs = sh.get("versions") or []
    if not vs:
        return []
    v = vs[-1]
    st = {"sent": "sent to Slack %s" % common.hhmm(sh.get("sent_at")), "nudged": "nudge sent", "failed": "Slack send failed — retrying",
          "composed": "composed, not sent yet", "not_connected": "saved here, Slack isn't connected"}.get(sh.get("status"), sh.get("status") or "")
    if sh.get("status") == "not_connected" and sh.get("slack") == "off":
        st = "saved here, Slack sending is off"
    return ["## Jira worklog", "_%s · version %d_" % (st, v["v"]), "", "~~~text", v["text"], "~~~", ""]


# --------------------------------------------------------------------------- preview (dashboard, /cg:today, worklog-show)

def preview_view(res, ts):
    """build()'s result as plain JSON-ready data for a preview: the exact text plus the per-entry fields, the Jira
    data's time ("Jira data as of HH:MM") and which descriptions are still condensed at 20:00."""
    msg = res.get("kind") == "message"
    entries = []
    for e in res.get("entries") or []:
        m = int(e.get("minutes") or 0)
        desc = e.get("desc") or ""
        entries.append({"ticket": e.get("ticket"), "start": e.get("start_hhmm") or "", "minutes": m,
                        "duration": T.jira_dur(m), "description": redact(desc, mode="public") if msg else desc,
                        "manual": bool(e.get("manual")), "pending_condense": bool(e.get("pending_condense"))})
    at = res.get("facts_at")
    return {"kind": res.get("kind"), "reason": res.get("reason"), "text": res.get("text") or "", "note": res.get("note") or "",
            "target": res.get("target"), "tracked": res.get("tracked") or {}, "day_kind": res.get("day_kind"),
            "entries": entries, "delivery": res.get("delivery") or "", "send_at": ts.get("send_at"),
            "facts": bool(res.get("facts")), "facts_at": at, "facts_source": res.get("facts_source"),
            "facts_as_of": ("Jira data as of %s" % common.hhmm(at)) if at else "",
            "pending_condense": bool(res.get("pending_condense")), "condensed": not res.get("pending_condense")}


def preview(day, with_blocks=False):
    """The message the sender would send for `day` if it ran now, built by the sender's own builder from the latest
    Jira evening-facts cache and the condensed-line cache. No Jira call, no model call, nothing written; safe from
    any process at any time. -> preview_view() (+ "blocks", the raw activity blocks, when with_blocks)."""
    from . import agent as agentmod
    a = agentmod.Agent()
    res = a.jwl.preview(day)
    out = preview_view(res, a.jwl.ts)
    if with_blocks:
        out["blocks"] = res.get("blocks") or []
    return out


def _manual():
    return common.env("MCP_MANUAL") == "1"  # CLI runs the user typed: counted apart from the background budget


def _locked(wait_s, fn):
    """fn(agent) under the agent lock: refresh and exact preview save health and caches, never beside a tick."""
    from . import agent as agentmod
    lock = agentmod.Lock()
    if not lock.acquire_wait(wait_s):
        return {"ok": False, "skipped": "the agent is busy; try again in a minute"}
    a = None
    try:
        a = agentmod.Agent()
        return fn(a)
    finally:
        if a:
            try:
                agentmod.save_state(a)
            except Exception:
                common.log("agent", "state save failed: " + traceback.format_exc())
        lock.release()


def _over_budget(a):
    from . import kbjobs
    today = common.workday(common.now(), a.cfg)
    return not _manual() and mcpjobs.budget_left(a.health, a.cfg, today) <= kbjobs.EVENING_RESERVE


_BUDGET = "the daily MCP call budget is used up (the 20:00 calls are kept)"


def refresh_facts(day, wait_s=None):
    """"Refresh from Jira": one read-only jira-evening job for `day` (comments, worklogs, status moves) within the
    daily MCP budget (the 20:00 reserve is kept); then the preview from the fresh cache.
    -> {"ok": True, "preview": preview_view()} | {"ok": False, "skipped": why[, "preview"]}"""
    _dt.date.fromisoformat(day)

    def go(a):
        j = a.jwl

        def skip(why):
            return {"ok": False, "skipped": why, "preview": preview_view(j.preview(day), j.ts)}
        if not mcpjobs.enabled(a.cfg, "jira"):
            return skip("Jira is off (mcp.jira.enabled)")
        if _over_budget(a):
            return skip(_BUDGET)
        today = common.workday(common.now(), a.cfg)
        tickets = j.fact_tickets(j.blocks(day, common.now() if day == today else None), j.manual_for(day, load_cal()))
        if not tickets:
            return skip("no tickets on %s" % day)
        try:
            mcpjobs.jira_evening(day, tickets, a.cfg, a.health)
        except mcpjobs.JobError as ex:
            return skip(str(ex))
        a.health.setdefault("jira_worklog", {})["jira_facts"] = common.now_iso()
        return {"ok": True, "preview": preview_view(j.preview(day), j.ts)}
    return _locked(wait_s, go)


def preview_exact(day, wait_s=None):
    """"Preview exactly": condense now the entries the preview marks "condensed at 20:00" (one batched model call,
    counted in the daily MCP budget; the 20:00 reserve is kept) and cache the lines, so the preview and the 20:00
    message share them. Uses the cached Jira facts and never calls Jira; no call at all when nothing is pending.
    -> {"ok": True, "preview": preview_view(), "model_calls": n} | {"ok": False, "skipped": why, "preview"}"""
    _dt.date.fromisoformat(day)

    def go(a):
        j = a.jwl
        res = j.preview(day)
        if not res.get("pending_condense"):
            return {"ok": True, "preview": preview_view(res, j.ts), "model_calls": 0}
        if _over_budget(a):
            return {"ok": False, "skipped": _BUDGET, "preview": preview_view(res, j.ts)}
        res = j.build(day, facts="cached", condense="model")
        if j.condense_calls:
            today = common.workday(common.now(), a.cfg)
            calls = a.health.setdefault("mcp", {}).setdefault("manual_calls" if _manual() else "calls", {})
            calls[today] = int(calls.get(today) or 0) + j.condense_calls
        return {"ok": True, "preview": preview_view(res, j.ts), "model_calls": j.condense_calls}
    return _locked(wait_s, go)


# --------------------------------------------------------------------------- inbox lines, resend requests, history

_REQUESTS = threading.Lock()


def append_inbox(line, repeat=False):
    """Append one line to inbox.md (never rewritten). A line already there is skipped unless `repeat` (action lines
    such as resend: count every copy). -> True when written."""
    path = common.hp("inbox.md")
    existing = common.read_text(path)
    if not repeat and any(l.strip() == line for l in existing.splitlines()):
        return False
    paths.append_text(path, ("\n" if existing and not existing.endswith("\n") else "") + line + "\n")
    return True


def request_resend(day, request_id):
    """Ask for `day`'s message to be sent again: adds one `resend: <day>` line to inbox.md (the next tick sends it).
    A request whose id was already seen in the last RESEND_WINDOW_S seconds (a double click) counts once.
    -> {"ok": True, "duplicate": bool, "day", "line"}; ValueError for a bad day or an empty id."""
    _dt.date.fromisoformat(day)
    rid = str(request_id or "").strip()[:100]
    if not rid:
        raise ValueError("a resend request needs an id")
    line = "resend: " + day
    with _REQUESTS:
        path = common.wl("resend_requests.json")
        now = common.now().timestamp()
        ids = (common.read_json(path, {}) or {}).get("ids") or {}
        ids = {k: v for k, v in ids.items() if isinstance(v, dict) and 0 <= now - float(v.get("t") or 0) < RESEND_WINDOW_S}
        if rid in ids:
            return {"ok": True, "duplicate": True, "day": ids[rid].get("day") or day, "line": ids[rid].get("line") or line}
        append_inbox(line, repeat=True)
        ids[rid] = {"t": now, "day": day, "line": line, "at": common.now_iso()}
        common.write_json(path, {"ids": ids})
    return {"ok": True, "duplicate": False, "day": day, "line": line}


def send_history(day):
    """Every version of `day`'s message, oldest first: what was composed, why (first | resend | correction), when,
    and what Slack said. -> [{"v", "at", "kind", "why", "sent", "slack_ts", "error", "slack_state", "jira_facts",
    "jira_facts_at", "text"}]; slack_state: sent | failed | not_connected | off | not sent."""
    out = []
    for v in load_sheet(day).get("versions") or []:
        if not isinstance(v, dict):
            continue
        sl = v.get("slack") if isinstance(v.get("slack"), dict) else {}
        state = "sent" if sl.get("ok") else (sl.get("state") or ("failed" if sl else "not sent"))
        out.append({"v": v.get("v"), "at": v.get("at"), "kind": v.get("kind"), "why": v.get("why"),
                    "sent": bool(sl.get("ok")), "slack_ts": sl.get("ts"), "error": sl.get("error"), "slack_state": state,
                    "jira_facts": bool(v.get("jira_facts")), "jira_facts_at": v.get("jira_facts_at"),
                    "text": v.get("text") or ""})
    return out
