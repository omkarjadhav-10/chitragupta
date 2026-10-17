"""Jira worklog timesheet: a day's activity -> back-to-back entries totalling the target -> the Slack message.

Everything here is deterministic. Inputs are plain dicts so the golden tests can drive it directly:
  block  {ticket, start: datetime|None, end: datetime|None, minutes, lines: [str]}
  manual {ticket, start: "HH:MM"|None, minutes, text}
"""
import datetime as _dt
import re

DEFAULTS = {
    "enabled": True, "send_at": "20:00", "day_start": "10:30", "hours_per_day": 8,
    "lunch": {"minutes": 60, "preferred": "14:00", "window": ["12:30", "15:00"]},
    "step_minutes": 30, "max_entries": 6, "stretch_note_below_minutes": 360,
    "workdays": ["Mon", "Tue", "Wed", "Thu", "Fri"], "unassigned_time": "distribute", "leave_ticket": None,
    "model": "haiku", "block_gap_minutes": 20, "point_minutes": 15, "late_days": 3, "correction_debounce_minutes": 15,
    "slack": {"self_user_id": "", "send_tool": "", "mcp_config": ".worklog/mcp/slack.json"},
    "example_ticket": "ABC-1234",  # ticket key shown in the "nothing tracked" nudge
}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def settings(cfg):
    out = dict(DEFAULTS)
    for k, v in ((cfg or {}).get("jira_worklog") or {}).items():
        out[k] = dict(DEFAULTS[k], **v) if isinstance(v, dict) and isinstance(DEFAULTS.get(k), dict) else v
    return out


# --------------------------------------------------------------------------- formatting

def mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def clock(m):
    return "%02d:%02d" % ((m // 60) % 24, m % 60)


def jira_dur(m):
    """Jira time-spent syntax: 2h, 1h 30m, 30m."""
    h, r = divmod(int(m), 60)
    parts = (["%dh" % h] if h else []) + (["%dm" % r] if r else [])
    return " ".join(parts) or "0m"


def tracked_dur(m):
    """Exact tracked time in the closing note: 4h 26m, 1h 06m, 45m."""
    m = int(round(m))
    return "%dm" % m if m < 60 else "%dh %02dm" % divmod(m, 60)


def and_list(xs):
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def day_label(day):
    d = _dt.date.fromisoformat(day) if isinstance(day, str) else day
    return "%d %s" % (d.day, d.strftime("%b"))


def header(day, send_day, tag="", correction=False):
    """Header by message age. tag: '', 'half day', 'weekend', 'holiday', 'leave day'."""
    d = _dt.date.fromisoformat(day)
    s = _dt.date.fromisoformat(send_day)
    lab = day_label(d)
    if correction:
        return "Corrected update for %s%s:" % (lab, " (%s)" % tag if tag else "")
    age = (s - d).days
    if age <= 0:
        return "Today's update (%s%s):" % (lab, ", " + tag if tag else "")
    if age == 1:
        return "Yesterday's update (%s%s):" % (lab, ", " + tag if tag else "")
    if age == 2:
        return "Update for %s%s, the day before yesterday:" % (lab, " (%s)" % tag if tag else "")
    return "Update for %s%s:" % (lab, " (%s)" % tag if tag else "")


def table(rows):
    """rows: [(start, ticket, time)] -> box-drawing table; headers centred, values left-aligned."""
    heads = ("Start", "Ticket", "Time")
    w = [max([len(heads[i])] + [len(r[i]) for r in rows]) + 2 for i in range(3)]

    def line(l, mid, r):
        return l + mid.join("─" * x for x in w) + r

    def centre(s, x):
        left = (x - len(s)) // 2
        return " " * left + s + " " * (x - len(s) - left)

    out = [line("┌", "┬", "┐"), "│" + "│".join(centre(h, w[i]) for i, h in enumerate(heads)) + "│"]
    for r in rows:
        out.append(line("├", "┼", "┤"))
        out.append("│" + "│".join((" " + r[i]).ljust(w[i]) for i in range(3)) + "│")
    out.append(line("└", "┴", "┘"))
    return out


# --------------------------------------------------------------------------- blocks from activity points

def build_blocks(points, gap_minutes=20):
    """points: [{ts: datetime, ticket|None, dur: minutes (0 = instant event, 15 = commit/Bruno/Jira comment)}].
    Per ticket, events less than gap apart form one block; each block counts (end - start) + 1 minute.
    Returns blocks ordered by start, consecutive same-ticket blocks merged."""
    per = {}
    for p in sorted(points, key=lambda p: p["ts"]):
        t = p.get("ticket")
        end = p["ts"] + _dt.timedelta(minutes=p.get("dur") or 0)
        bl = per.setdefault(t, [])
        if bl and (p["ts"] - bl[-1]["end"]).total_seconds() < gap_minutes * 60:
            bl[-1]["end"] = max(bl[-1]["end"], end)
        else:
            bl.append({"ticket": t, "start": p["ts"], "end": end})
    out = []
    for bl in per.values():
        for b in bl:
            b["minutes"] = (b["end"] - b["start"]).total_seconds() / 60 + 1
            b["spans"] = [(b["start"], b["end"])]
            b["lines"] = []
            out.append(b)
    out.sort(key=lambda b: b["start"])
    return out


def merge_consecutive(items):
    out = []
    for it in items:
        if out and not it.get("manual") and not out[-1].get("manual") and out[-1]["ticket"] == it["ticket"]:
            _absorb(out[-1], it)
        else:
            out.append(dict(it))
    return out


def _absorb(dst, src):
    dst["minutes"] = dst["minutes"] + src["minutes"]
    dst["lines"] = list(dst.get("lines") or []) + [l for l in src.get("lines") or [] if l not in (dst.get("lines") or [])]
    dst["spans"] = list(dst.get("spans") or []) + list(src.get("spans") or [])
    dst["notes"] = list(dst.get("notes") or []) + [x for x in src.get("notes") or [] if x not in (dst.get("notes") or [])]
    if src.get("start") is not None and (dst.get("start") is None or src["start"] < dst["start"]):
        dst["start"] = src["start"]
    dst["known"] = dst.get("known", True) and src.get("known", True)


# --------------------------------------------------------------------------- allocation

def allocate(blocks, manual, target, step=30, max_entries=6, unassigned_ticket=None):
    """-> entries [{ticket, minutes, manual, text, lines, known, spans}] in work order, summing to target (when
    target is a multiple of step plus the manual minutes)."""
    items = []
    for b in blocks:
        t = b.get("ticket") or unassigned_ticket
        if not t:
            continue  # no-ticket time: distributed proportionally by the scaling below
        items.append({"ticket": t, "minutes": float(b["minutes"]), "manual": False, "lines": list(b.get("lines") or []),
                      "notes": list(b.get("notes") or []),
                      "start": b.get("start"), "known": b.get("start") is not None, "spans": list(b.get("spans") or [])})
    for m in manual:
        items.append({"ticket": m["ticket"], "minutes": int(m["minutes"]), "manual": True, "text": m.get("text") or "",
                      "lines": [], "start": m.get("start_dt"), "known": m.get("start_dt") is not None, "spans": []})
    known = sorted([i for i in items if i["known"]], key=lambda i: i["start"])
    unknown = sorted([i for i in items if not i["known"]], key=lambda i: (i["ticket"], i["manual"]))
    items = merge_consecutive(known + unknown)

    avail = target - sum(i["minutes"] for i in items if i["manual"])
    if avail <= 0:
        return [i for i in items if i["manual"]]
    # small blocks: join a same-ticket block, keep 30 min when described, else drop (time redistributed)
    while True:
        auto = [i for i in items if not i["manual"]]
        free = [i for i in auto if not i.get("fixed")]
        total = sum(i["minutes"] for i in free)
        if not free or total <= 0:
            break
        room = avail - step * sum(1 for i in auto if i.get("fixed"))
        small = None
        for i in free:
            if i["minutes"] * room / total < step:
                small = i
                break
        if small is None:
            break
        same = [j for j in auto if j is not small and j["ticket"] == small["ticket"]]
        if same:
            k = items.index(small)
            dst = min(same, key=lambda j: abs(items.index(j) - k))
            _absorb(dst, small)
            items.remove(small)
            items = merge_consecutive(items)
        elif small["lines"]:
            small["fixed"] = True
        else:
            items.remove(small)
            items = merge_consecutive(items)
    # too many entries: merge same-ticket fragments, smallest first
    while len(items) > max_entries:
        frag = [i for i in items if not i["manual"] and
                sum(1 for j in items if not j["manual"] and j["ticket"] == i["ticket"]) > 1]
        if not frag:
            break
        small = min(frag, key=lambda i: i["minutes"])
        k = items.index(small)
        dst = min([j for j in items if j is not small and not j["manual"] and j["ticket"] == small["ticket"]],
                  key=lambda j: abs(items.index(j) - k))
        _absorb(dst, small)
        items.remove(small)
        items = merge_consecutive(items)
    # largest-remainder rounding to whole steps
    auto = [i for i in items if not i["manual"]]
    fixed = [i for i in auto if i.get("fixed")]
    free = [i for i in auto if not i.get("fixed")]
    for i in fixed:
        i["minutes"] = step
    units = int(round((avail - step * len(fixed)) / step))
    total = sum(i["minutes"] for i in free)
    if free and total > 0:
        exact = [i["minutes"] * units / total for i in free]
        base = [int(x) for x in exact]
        order = sorted(range(len(free)), key=lambda k: (-(exact[k] - base[k]), k))
        for k in order[:units - sum(base)]:
            base[k] += 1
        for i, u in zip(free, base):
            i["minutes"] = u * step
    items = [i for i in items if i["minutes"] > 0]
    return merge_consecutive(items)


def layout(entries, day_start="10:30", lunch=None, with_lunch=True):
    """Assign HH:MM starts, back to back, with the lunch on the boundary nearest the preferred time inside the
    window, else inside the entry spanning the preferred time (no split)."""
    lunch = lunch or DEFAULTS["lunch"]
    t = mins(day_start)
    ends, cur = [], t
    for e in entries:
        cur += e["minutes"]
        ends.append(cur)
    lunch_after, lunch_inside = None, None
    if with_lunch and entries:
        pref, lo, hi = mins(lunch["preferred"]), mins(lunch["window"][0]), mins(lunch["window"][1])
        cands = [(abs(b - pref), b, k) for k, b in enumerate(ends[:-1]) if lo <= b <= hi]
        if cands:
            lunch_after = min(cands)[2]
        else:
            start = t
            for k, e in enumerate(entries):
                if start < pref < start + e["minutes"]:
                    lunch_inside = k
                    break
                start += e["minutes"]
    cur = t
    for k, e in enumerate(entries):
        e["start_hhmm"] = clock(cur)
        cur += e["minutes"]
        if k == lunch_after or k == lunch_inside:
            cur += lunch["minutes"]
    return entries


# --------------------------------------------------------------------------- the message

def closing_note(entries, tracked, padded, target_label, already=None, threshold=360, unassigned=0):
    parts = []
    if already:
        parts.append("Already in Jira: %s." % and_list("%s %s" % (t, jira_dur(m)) for t, m in already))
    total = sum(tracked.values()) + unassigned
    if padded and total < threshold:
        per = ["%s on %s" % (tracked_dur(m), t) for t, m in sorted(tracked.items(), key=lambda kv: (-kv[1], kv[0])) if m > 0]
        if unassigned > 0:
            per.append("%s without a ticket" % tracked_dur(unassigned))
        about = int(round(total / 30.0)) * 30
        parts.append("Only about %s was recorded that day%s. I stretched these to make %s." % (
            jira_dur(about), (": " + and_list(per)) if per else "", target_label))
    if any(not e.get("known", True) for e in entries):
        if len(entries) == 2:
            parts.append("The worklog doesn't say which ticket came first, so swap the start times if you did %s earlier."
                         % entries[1]["ticket"])
        elif len(entries) > 2:
            parts.append("The worklog doesn't say which order these came in, so reorder the start times if needed.")
    return " ".join(parts)


def render_message(hdr, entries, note=""):
    rows = [(e["start_hhmm"], e["ticket"], jira_dur(e["minutes"])) for e in entries]
    L = [hdr, "", "```"] + table(rows) + ["```", ""]
    for e in entries:
        L += ["%s · %s · %s" % (e["ticket"], e["start_hhmm"], jira_dur(e["minutes"])), e.get("desc") or "", ""]
    L = L[:-1]
    if note:
        L += ["", note]
    return "\n".join(L)


def nudge(day, send_day, example_ticket="ABC-1234"):
    lab = day_label(day)
    return ("Nothing was tracked for %s, so there is no worklog for it.\n"
            "On leave? Tell Claude \"I was on leave on %s\" (or add `leave: %s` to inbox.md).\n"
            "Worked without Claude Code? Tell Claude \"log 2h on %s on %s: what you did\"." % (
                lab, lab, day, example_ticket, lab))


# --------------------------------------------------------------------------- calendar and inbox grammar

_DATE = r"\d{4}-\d{2}-\d{2}"
_DUR = r"(?:\d+(?:\.\d+)?\s*h(?:\s*\d+\s*m)?|\d+\s*m)"
DIRECTIVE = re.compile(r"^\s*(?:[-*]\s*)?(time|leave|half-day|holiday|workday|resend)\s*:\s*(.*)$", re.I)
_TIME = re.compile(r"^(?:(" + _DATE + r")\s+)?(?:(\d{1,2}:\d{2})\s+)?(" + _DUR + r")\s+([A-Za-z][A-Za-z0-9]{1,9}-\d+)\s*:?\s*(.*)$", re.I)


def parse_dur(s):
    s = s.lower().replace(" ", "")
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+)m)?", s)
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(round(float(m.group(1) or 0) * 60)) + int(m.group(2) or 0)


def _dates(spec):
    spec = spec.strip()
    m = re.fullmatch(r"(" + _DATE + r")(?:\s*\.\.\s*(" + _DATE + r"))?", spec.split()[0] if spec else "")
    if not m:
        return None, spec
    a = _dt.date.fromisoformat(m.group(1))
    b = _dt.date.fromisoformat(m.group(2)) if m.group(2) else a
    out = []
    while a <= b and len(out) < 62:
        out.append(a.isoformat())
        a += _dt.timedelta(days=1)
    return out, spec[len(spec.split()[0]):].strip()


def parse_directive(line, today):
    """One inbox line -> {kind, days:[...], ...} or None. `today` = current work day (for a time: line without date)."""
    m = DIRECTIVE.match(line or "")
    if not m:
        return None
    kind, rest = m.group(1).lower(), m.group(2).strip()
    if kind == "time":
        t = _TIME.match(rest)
        if not t:
            return {"kind": "invalid", "line": line}
        minutes = parse_dur(t.group(3))
        if not minutes:
            return {"kind": "invalid", "line": line}
        return {"kind": "time", "days": [t.group(1) or today], "start": t.group(2) and t.group(2).zfill(5),
                "minutes": minutes, "ticket": t.group(4).upper(), "text": t.group(5).strip()}
    days, tail = _dates(rest)
    if not days:
        return {"kind": "invalid", "line": line}
    return {"kind": kind, "days": days, "name": tail}


def apply_directive(cal, d):
    """Update data/calendar.json content (dict) with a parsed directive. Returns the days it affects."""
    for k in ("leave", "half_day", "holiday", "workday", "time"):
        cal.setdefault(k, {})
    kind = d["kind"]
    if kind == "time":
        day = d["days"][0]
        lst = cal["time"].setdefault(day, [])
        rec = {"ticket": d["ticket"], "start": d.get("start"), "minutes": d["minutes"], "text": d["text"]}
        if rec not in lst:
            lst.append(rec)
    elif kind in ("leave", "half-day", "holiday", "workday"):
        key = kind.replace("-", "_")
        for day in d["days"]:
            cal[key][day] = d.get("name") or True
    return d["days"]


def day_kind(day, cal, ts):
    """'workday' | 'half_day' | 'leave' | 'holiday' | 'weekend'."""
    cal = cal or {}
    if day in (cal.get("leave") or {}):
        return "leave"
    if day in (cal.get("holiday") or {}):
        return "holiday"
    if day in (cal.get("half_day") or {}):
        return "half_day"
    wd = WEEKDAYS[_dt.date.fromisoformat(day).weekday()]
    if day in (cal.get("workday") or {}) or wd in ts["workdays"]:
        return "workday"
    return "weekend"


TAGS = {"workday": "", "half_day": "half day", "weekend": "weekend", "holiday": "holiday", "leave": "leave day"}


# --------------------------------------------------------------------------- compose a day

def compose(day, send_day, blocks, manual, cal, ts, already=None, correction=False, describe=None, unassigned_minutes=0):
    """-> {"kind": "message"|"nudge"|"none", "text", "entries", "note"}.
    blocks: per-ticket activity blocks (no-ticket blocks have ticket None); manual: calendar time entries.
    already: [(ticket, minutes)] logged in Jira by me that day. describe(entries) fills e["desc"]."""
    kind = day_kind(day, cal, ts)
    step = int(ts["step_minutes"])
    unassigned_ticket = ts["unassigned_time"] if ts.get("unassigned_time") not in (None, "", "distribute") else None
    tracked = {}
    for b in blocks:
        if b.get("ticket") or unassigned_ticket:
            t = b.get("ticket") or unassigned_ticket
            tracked[t] = tracked.get(t, 0) + b["minutes"]
    for m in manual:
        tracked[m["ticket"]] = tracked.get(m["ticket"], 0) + m["minutes"]
    unassigned = 0 if unassigned_ticket else sum(b["minutes"] for b in blocks if not b.get("ticket"))
    unassigned += unassigned_minutes
    have = any(v > 0 for v in tracked.values())
    tag = TAGS[kind]
    hours = int(ts["hours_per_day"]) * 60
    if kind == "leave" and not have:
        if ts.get("leave_ticket"):
            manual = [{"ticket": ts["leave_ticket"], "minutes": hours, "text": "Leave"}]
            tracked = {}
            have, padded, target = True, False, hours
        else:
            return {"kind": "none", "reason": "leave"}
    elif kind in ("weekend", "holiday", "leave"):
        if not have:
            return {"kind": "none", "reason": kind}
        target = int(round(sum(tracked.values()) / step)) * step
        padded = False
        if target <= 0:
            return {"kind": "none", "reason": kind + " (under %d min)" % (step // 2)}
    else:
        if not have:
            return {"kind": "nudge", "text": nudge(day, send_day, ts.get("example_ticket") or "ABC-1234")}
        target = hours // 2 if kind == "half_day" else hours
        padded = True
    full_target = target
    already = [(t, m) for t, m in (already or []) if m > 0]
    target -= sum(m for _, m in already)
    for m in manual:
        if m.get("start"):
            m["start_dt"] = _dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(*map(int, m["start"].split(":")))).astimezone()
    if target <= 0:
        note = closing_note([], {}, False, "", already) + " That covers the full %s, so there is nothing left to log." % (
            jira_dur(full_target))
        return {"kind": "message", "text": header(day, send_day, tag, correction) + "\n\n" + note, "entries": [], "note": note}
    entries = allocate(blocks, manual, target, step, int(ts["max_entries"]), unassigned_ticket)
    if not entries:  # e.g. a weekend with only slivers of ticket time: every block rounds below one step
        return {"kind": "none", "reason": "%s (no ticket block reaches %d min)" % (kind, step)}
    layout(entries, ts["day_start"], ts["lunch"], with_lunch=target > 240)
    if describe:
        describe(entries)
    for e in entries:
        if e.get("manual"):
            e["desc"] = e.get("text") or e.get("desc") or ""
    label = jira_dur(full_target)
    note = closing_note(entries, tracked, padded, label, already, int(ts["stretch_note_below_minutes"]), unassigned)
    text = render_message(header(day, send_day, tag, correction), entries, note)
    return {"kind": "message", "text": text, "entries": entries, "note": note, "target": full_target,
            "tracked": {k: round(v) for k, v in tracked.items()}, "unassigned": round(unassigned), "day_kind": kind}
