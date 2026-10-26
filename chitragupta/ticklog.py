"""Background-run records: a small ring of the last background runs in <home>/.worklog/logs/ticks.jsonl, one JSON line
each: {"at", "outcome": ok | error | skipped-busy | skipped-upgrade, "ms"}.

A run that finds the agent lock busy can't write health.json (the run holding the lock owns it), so every run,
skips included, appends one line here; the run holding the lock trims the file back to the last KEEP lines.
stale_warning() is the "events are being captured but nothing processes them" check that SessionStart, doctor, the
Health page and get_alerts show. It only lists two folders and reads this small file, so SessionStart stays fast.
"""
import json
import os

from . import common
from .safety import paths

KEEP = 50
STALE_TICK_S = 30 * 60  # no successful background run for this long ...
RECENT_EVENT_S = 3600  # ... while hooks recorded events this recently: warn


def path():
    return common.wl("logs", "ticks.jsonl")


def record(outcome, ms=0, trim=False, steps=None):
    """Append one run record; never raises. trim=True (only under the agent lock) keeps the last KEEP lines."""
    try:
        row = {"at": common.now_iso(), "outcome": outcome, "ms": max(0,int(ms))}
        if steps is not None:
            row["steps"] = {name:max(0,int(value)) for name,value in steps.items()}
        paths.append_text(path(), json.dumps(row) + "\n",
                          caller="ticklog")
        if trim:
            lines = common.read_text(path()).splitlines()
            if len(lines) > KEEP:
                paths.write_text(path(), "\n".join(lines[-KEEP:]) + "\n", caller="ticklog")
    except OSError:
        pass


def recent(n=KEEP):
    """The last n records, oldest first."""
    out = []
    for line in common.read_text(path()).splitlines()[-n:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("at"):
            out.append(rec)
    return out


def last_event_time():
    """Newest hook event file time (epoch seconds) in the two most recent raw day folders, or 0."""
    root = common.wl("raw")
    try:
        days = sorted(d for d in os.listdir(root) if len(d) == 10 and d[4] == "-")[-2:]
    except OSError:
        return 0.0
    best = 0.0
    for d in days:
        for sub in (os.path.join(root, d), os.path.join(root, d, "time")):
            try:
                names = os.listdir(sub)
            except OSError:
                continue
            for fn in names:
                if fn.endswith(".jsonl"):
                    try:
                        best = max(best, os.path.getmtime(os.path.join(sub, fn)))
                    except OSError:
                        pass
    return best


def stale_warning(health, now=None, last_event=None):
    """One line when hooks recorded events in the last hour but no background run succeeded for over 30 minutes
    (with how the runs since then ended), else ""."""
    now = now or common.now()
    last_event = last_event_time() if last_event is None else last_event
    if not last_event or now.timestamp() - last_event > RECENT_EVENT_S:
        return ""
    ok = common.parse_ts((health or {}).get("last_tick_ok"))
    if ok and (now - ok).total_seconds() <= STALE_TICK_S:
        return ""
    if not ok:  # a new install: the first runs come within minutes of setup
        try:
            if now.timestamp() - os.path.getctime(common.wl("config.json")) < STALE_TICK_S:
                return ""
        except OSError:
            return ""
    since = [r for r in recent() if not ok or (common.parse_ts(r.get("at")) or now) > ok]
    counts = {}
    for r in since:
        if r.get("outcome") != "ok":
            counts[r.get("outcome")] = counts.get(r.get("outcome"), 0) + 1
    tail = ""
    if counts:
        tail = " (since then: %s)" % ", ".join("%s x%d" % (k, v) for k, v in sorted(counts.items()))
    elif not since:
        tail = " (no background run recorded since then: is the scheduled task running? /cg:doctor)"
    when = ("since %s" % ok.strftime("%Y-%m-%d %H:%M")) if ok else "yet"
    return ("Worklog recorder status: Claude Code hooks recorded events in the last hour but no background run has "
            "succeeded %s%s." % (when, tail))


def slow_warning(now=None):
    """Warn about the most recent completed/error tick in the last hour; skips do not hide a slow run."""
    now = now or common.now()
    row = next((r for r in reversed(recent()) if r.get("outcome") in ("ok","error")),None)
    if not row or int(row.get("ms") or 0) <= 60000:
        return ""
    at = common.parse_ts(row.get("at"))
    if not at or (now-at).total_seconds() > 3600:
        return ""
    steps = sorted((row.get("steps") or {}).items(),key=lambda pair:pair[1],reverse=True)[:3]
    detail = "; slowest steps: " + ", ".join("%s %.1fs" % (k,v/1000) for k,v in steps) if steps else ""
    return "Last background tick took %.1fs (over 60s)%s." % (row["ms"]/1000,detail)
