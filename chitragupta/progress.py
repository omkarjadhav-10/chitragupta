"""Personal progress from recorded evidence: a day, a week, a month, personal records, factual milestones, weekly wins,
next useful actions and the daily reflection. Read by the dashboard, the CLI (`progress`) and MCP (`get_progress`).

Everything is computed on read from the activity ledgers (activity.py), the evening Jira facts and the ticket records,
under profile.Policy, so the same evidence always gives the same numbers and the same sentences. The one write is the
reflection (data/reflections/<YYYY-MM>.json, local only, never in daily/ files).

Metrics of a day (days are work days in the profile's zone, from day_starts_at):
  commits           counted non-merge commits you authored (author time), one per change across refs, rebases and
                    cherry-picks
  lines             added / deleted lines of those commits without co-authors, after progress.exclude_globs: committed
                    change volume, not quality and not productivity
  shared            your authored commits that list co-authors: their whole diff, never split per person
  coauthored        commits where you are only a Co-authored-by
  prs_raised        your PRs by creation time (drafts counted and labelled); prs_merged by merge time
  tickets_completed distinct tickets moved into progress.done_statuses that day while yours (Jira status history from
                    the evening check); unknown without that history
Each carries status: known | zero | partial | stale | unknown, with why. Zero is only said when every source covering
the day is configured, current and read.
"""
import calendar as _cal
import datetime as _dt
import os
import re

from . import activity
from . import common
from . import profile
from .redact import redact

STALE_S = 3 * 3600  # a source last read longer ago than this makes today's numbers "stale"
REFLECTION_MAX = 1000
MAX_ACTIONS = 3
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH = re.compile(r"^\d{4}-\d{2}$")


def _plural(n, word, many=None):
    return "%d %s" % (n, word if n == 1 else (many or word + "s"))


def _d(s):
    return _dt.date.fromisoformat(s)


def _days(a, b):
    """Inclusive list of ISO days from a to b."""
    out, d, end = [], _d(a), _d(b)
    while d <= end:
        out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


# --------------------------------------------------------------------------- the evidence, read once

class Ctx:
    """Everything one answer needs, read once: profile, ledgers of the in-scope repositories, buckets per day."""

    def __init__(self, cfg=None, now=None, tz=profile.LOCAL):
        self.cfg = cfg if cfg is not None else common.load_config()
        self.now = now or common.now()
        self.pol = profile.Policy(self.cfg)
        self.sig = profile.signature(self.cfg)
        self.tz = profile.tzinfo(self.cfg) if tz is profile.LOCAL else tz
        self.today = self.day(self.now)
        self.window = profile.window_start(self.cfg, self.today)
        self.cov = profile.coverage(self.cfg, self.today)
        self.settings = profile.settings(self.cfg)
        self.done = set(profile.progress_settings(self.cfg)["done_statuses"])
        self.repos = []
        self.commits, self.raised, self.merged, self.future = {}, {}, {}, []
        self.not_counted = {}
        for path in self.pol.p["repos"]:
            self._add_repo(path)
        self._evening, self._tickets, self._cal = {}, None, None

    def day(self, ts):
        return profile.day_of(ts, self.cfg, self.tz)

    def _add_repo(self, path):
        led = activity.load(path)
        name = os.path.basename(str(path).rstrip("/\\"))
        r = {"path": path, "name": name, "ledger": led, "id": ((led or {}).get("repo") or {}).get("id") or "path:" + name}
        if led is None:
            r["commits"] = {"status": "missing", "why": "not collected yet"}
            r["prs"] = {"status": "missing", "why": "not collected yet"}
        elif led.get("profile_sig") != self.sig:
            r["commits"] = r["prs"] = {"status": "updating", "why": "updating for your changed profile"}
        else:
            c, p = led.get("commits") or {}, led.get("prs") or {}
            r["commits"] = {"status": c.get("status") or "missing", "why": c.get("why") or c.get("error") or "",
                            "at": c.get("scanned_at") or "", "since": led.get("window_start") or ""}
            r["prs"] = {"status": p.get("status") or "missing", "why": p.get("why") or p.get("error") or "",
                        "at": p.get("fetched_at") or "", "source": p.get("source") or "", "capped": bool(p.get("capped"))}
        self.repos.append(r)
        if r["commits"]["status"] in ("ok", "error") and led:  # an error keeps its last good items
            for sha, it in ((led.get("commits") or {}).get("items") or {}).items():
                rec = dict(it, repo=name, repo_id=r["id"])
                if it.get("state") == "future":
                    self.future.append(rec)
                    continue
                d = self.day(it.get("author_date"))
                if not d:
                    continue
                if it.get("state") == "counted":
                    self.commits.setdefault(d, []).append(rec)
                else:
                    self.not_counted.setdefault(d, []).append(rec)
        if r["prs"]["status"] in ("ok", "partial", "error") and led:
            seen = set()
            for k, p in ((led.get("prs") or {}).get("items") or {}).items():
                ident = (r["id"], str(p.get("number")))
                if ident in seen:
                    continue
                seen.add(ident)
                rec = dict(p, repo=name, repo_id=r["id"])
                if p.get("created_at") and not self._future(p["created_at"]):
                    self.raised.setdefault(self.day(p["created_at"]), []).append(rec)
                elif p.get("created_at"):
                    self.future.append(dict(rec, kind="pr"))
                if p.get("merged_at") and p.get("state") == "merged" and not self._future(p["merged_at"]):
                    self.merged.setdefault(self.day(p["merged_at"]), []).append(rec)

    def _future(self, ts):
        t = common.parse_ts(ts)
        return t is not None and t > self.now + activity.FUTURE_SLACK

    # ---- source coverage of one day
    def _repo_cov(self, src, day):
        """ok | stale | gap (with why) for one repo's commit or PR source on `day`."""
        st = src.get("status")
        if st not in ("ok", "partial"):
            return "gap", src.get("why") or st
        at = common.parse_ts(src.get("at"))
        if at is None:
            return "gap", "not read yet"
        if day > self.day(at):
            return "stale", "last read %s" % src.get("at")[:16].replace("T", " ")
        if day == self.today and (self.now - at).total_seconds() > STALE_S:
            return "stale", "last read %s" % src.get("at")[:16].replace("T", " ")
        if st == "partial":
            return "part", src.get("why") or "partial"
        return "ok", ""

    def coverage(self, kind, day):
        """(status, why) of a metric source on a day, before looking at the value: unknown | partial | stale | ok."""
        if day < self.window:
            return "unknown", "before the imported history (%s)" % self.cov["label"]
        if day > self.today:
            return "unknown", "that day hasn't come yet"
        if kind == "commits" and not self.pol.emails:
            return "unknown", "no commit email is confirmed in your profile"
        if not self.repos:
            return "unknown", "no repository is in your profile's scope"
        ok, stale, part, gaps = [], [], [], []
        for r in self.repos:
            st, why = self._repo_cov(r[kind], day)
            {"ok": ok, "stale": stale, "part": part}.get(st, gaps).append((r["name"], why))
        if not ok and not stale and not part:
            return "unknown", "; ".join("%s: %s" % g for g in gaps)
        if gaps or part:
            return "partial", "; ".join(["not included: %s (%s)" % g for g in gaps] + ["%s: %s" % p for p in part])
        if stale:
            return "stale", "; ".join("%s: %s" % s for s in stale)
        return "ok", ""

    def day_kind(self, day):
        """workday | half_day | leave | holiday | weekend (jira_worklog.workdays and data/calendar.json)."""
        from . import jiraworklog, timesheet
        if self._cal is None:
            self._cal = (jiraworklog.load_cal(), timesheet.settings(self.cfg))
        return timesheet.day_kind(day, *self._cal)

    # ---- tickets
    def tickets(self):
        if self._tickets is None:
            from .dashboard import api
            self._tickets = api.tickets()["tickets"]
        return self._tickets

    def ticket_record(self, key):
        from .store import ticket_path
        return common.read_json(ticket_path(key), {}) or {}

    def evening(self, day):
        if day not in self._evening:
            self._evening[day] = common.read_json(common.hp("data", "jira", "_evening", day + ".json"), {}) or {}
        return self._evening[day]


def _status(cov, value):
    st, why = cov
    if st == "ok":
        return ("known" if value else "zero"), why
    return st, why


def owned_at(ctx, key, tk, when):
    """True / False / None: was the ticket yours (assigned in Jira, per the recorded history, or included by you) at
    `when`? None when no assignee was recorded before then."""
    if str(key).upper() in ctx.pol.p["include"]:
        return True
    _, hist = ctx.pol.ownership_of(tk)  # only what was recorded against the Jira account confirmed now
    t = common.parse_ts(when)
    state = None
    for h in sorted(hist, key=lambda h: str(h.get("at"))):
        at = common.parse_ts(h.get("at"))
        if at is not None and t is not None and at <= t:
            state = h.get("state")
    if state is None:
        return None
    return state == "you"


def _completed(ctx, day):
    """-> (value or None, status, why, [evidence])."""
    if not ctx.pol.jira_account():
        return None, "unknown", "no Jira account is confirmed in your profile", []
    if day < ctx.window or day > ctx.today:
        return None, "unknown", ctx.coverage("prs", day)[1], []
    ev = ctx.evening(day)
    if not ev or ev.get("changelogs") is None:
        return None, "unknown", "no Jira status history was read for that day (the evening check reads it for the " \
                                "tickets you worked on)", []
    seen, unsure, out = set(), 0, []
    for ch in sorted((ev.get("facts") or {}).get("status_changes") or [], key=lambda c: str(c.get("time"))):
        if str(ch.get("to") or "").strip().lower() not in ctx.done or ctx.day(ch.get("time")) != day:
            continue
        key = str(ch.get("ticket") or "").upper()
        if not key or key in seen:
            continue  # a ticket completed, reopened and completed again the same day is one completed ticket
        mine = owned_at(ctx, key, ctx.ticket_record(key), ch.get("time"))
        if mine is None:
            unsure += 1
            continue
        if mine:
            seen.add(key)
            out.append({"key": key, "at": ch.get("time"), "to": ch.get("to")})
    why = "only tickets you worked on that day are checked"
    if unsure:
        return len(seen), "partial", "%s moved to done whose owner at the time isn't recorded" % _plural(unsure, "ticket"), out
    return len(seen), ("known" if seen else "zero"), why, out


def day_metrics(ctx, day, evidence=False):
    """The metrics of one day, JSON-ready."""
    com = ctx.commits.get(day) or []
    solo = [c for c in com if c.get("role") == "author" and not c.get("coauthored")]
    shared = [c for c in com if c.get("role") == "author" and c.get("coauthored")]
    coauth = [c for c in com if c.get("role") == "coauthor"]
    authored = solo + shared
    ccov = ctx.coverage("commits", day)
    pcov = ctx.coverage("prs", day)
    raised, merged = ctx.raised.get(day) or [], ctx.merged.get(day) or []
    n_done, t_st, t_why, t_ev = _completed(ctx, day)
    c_st, c_why = _status(ccov, len(authored))
    out = {
        "date": day,
        "kind": ctx.day_kind(day),
        "commits": {"value": len(authored) if ccov[0] != "unknown" else None, "status": c_st, "why": c_why},
        "lines": {"added": sum(c.get("added", 0) for c in solo) if ccov[0] != "unknown" else None,
                  "deleted": sum(c.get("deleted", 0) for c in solo) if ccov[0] != "unknown" else None,
                  "status": c_st, "why": c_why},
        "shared": {"commits": len(shared), "added": sum(c.get("added", 0) for c in shared),
                   "deleted": sum(c.get("deleted", 0) for c in shared)},
        "coauthored": {"commits": len(coauth), "added": sum(c.get("added", 0) for c in coauth),
                       "deleted": sum(c.get("deleted", 0) for c in coauth)},
        "excluded": {k: sum((c.get("excluded") or {}).get(k, 0) for c in authored) for k in ("added", "deleted", "files")},
        "binary_files": sum(c.get("binary_files", 0) for c in authored),
        "prs_raised": dict(zip(("status", "why"), _status(pcov, len(raised))),
                           value=len(raised) if pcov[0] != "unknown" else None,
                           drafts=sum(1 for p in raised if p.get("draft"))),
        "prs_merged": dict(zip(("status", "why"), _status(pcov, len(merged))),
                           value=len(merged) if pcov[0] != "unknown" else None),
        "tickets_completed": {"value": n_done, "status": t_st, "why": t_why},
    }
    parts = [out["commits"]["value"], out["prs_raised"]["value"], out["prs_merged"]["value"], n_done]
    known = [p for p in parts if p is not None]
    out["activity"] = sum(known) if known else None
    if evidence:
        out["evidence"] = {
            "commits": [_commit_ev(c) for c in sorted(com, key=lambda c: str(c.get("author_date")))],
            "not_counted": [_commit_ev(c) for c in sorted(ctx.not_counted.get(day) or [], key=lambda c: str(c.get("author_date")))],
            "prs_raised": [_pr_ev(p) for p in sorted(raised, key=lambda p: str(p.get("created_at")))],
            "prs_merged": [_pr_ev(p) for p in sorted(merged, key=lambda p: str(p.get("merged_at")))],
            "tickets_completed": t_ev,
        }
        if day == ctx.today:
            out["unresolved"] = [{"repo": f.get("repo"), "what": "PR #%s" % f.get("number") if f.get("kind") == "pr"
                                  else "commit " + str(f.get("sha", ""))[:9],
                                  "at": f.get("created_at") or f.get("author_date"),
                                  "why": "its time is in the future, so it isn't counted on any day"}
                                 for f in ctx.future]
    return out


def _commit_ev(c):
    why = {"equivalent": "the same change as %s (a rebase or cherry-pick), counted once" % str(c.get("equivalent_to"))[:9],
           "integration": "the squash / rebase merge of %s, whose commits are already counted" % c.get("integration_of"),
           "unreachable": "no longer on any branch (amended, rewritten or deleted)"}.get(c.get("state"), "")
    return {"repo": c.get("repo"), "sha": str(c.get("sha", ""))[:9], "subject": c.get("subject") or "",
            "added": c.get("added", 0), "deleted": c.get("deleted", 0), "role": c.get("role"),
            "shared": bool(c.get("coauthored")), "ticket": c.get("ticket"), "at": c.get("author_date"),
            "state": c.get("state"), "why": why, "excluded": c.get("excluded") or {}, "binary_files": c.get("binary_files", 0)}


def _pr_ev(p):
    return {"repo": p.get("repo"), "number": p.get("number"), "title": p.get("title") or "", "url": p.get("url") or "",
            "state": p.get("state"), "draft": bool(p.get("draft")), "review": p.get("review") or "",
            "created_at": p.get("created_at"), "merged_at": p.get("merged_at"), "ticket": p.get("ticket"),
            "target": p.get("target")}


# --------------------------------------------------------------------------- records

RECORDS = (("commits", "Most authored commits in a day"), ("prs_raised", "Most PRs raised in a day"),
           ("prs_merged", "Most PRs merged in a day"), ("tickets_completed", "Most tickets completed in a day"),
           ("activity", "Most active day"))


def records(ctx):
    """Personal records over the imported history. Earliest day wins a tie (the others are counted). A day only
    counts with a known value; a record is never called all-time: its coverage says where it comes from."""
    days = [day_metrics(ctx, d) for d in _days(ctx.window, ctx.today)] if ctx.window <= ctx.today else []
    out = []
    for rid, label in RECORDS:
        best, ties, any_known = None, 0, False
        for m in days:
            v = m["activity"] if rid == "activity" else m[rid]["value"]
            if v is None:
                continue
            any_known = True
            if not v:
                continue
            if best is None or v > best[1]:
                best, ties = (m, v), 0
            elif v == best[1]:
                ties += 1
        rec = {"id": rid, "label": label, "value": best[1] if best else None, "date": best[0]["date"] if best else None,
               "ties": ties, "available": any_known, "coverage": ctx.cov["label"]}
        if not any_known:
            rec["why"] = ("needs Jira status history (a confirmed Jira account and the evening check)"
                          if rid == "tickets_completed" else "no day with recorded data yet")
        elif not best:
            rec["why"] = "none yet %s" % ctx.cov["label"]
        if best:
            m = best[0]
            rec["breakdown"] = {"commits": m["commits"]["value"], "prs_raised": m["prs_raised"]["value"],
                                "prs_merged": m["prs_merged"]["value"], "tickets_completed": m["tickets_completed"]["value"]}
        if rid == "activity":
            rec["definition"] = ("The most distinct activity events in one day: authored commits + PRs raised + PRs merged "
                                 "+ tickets completed (only the kinds recorded that day). A count, not a score.")
        out.append(rec)
    return {"coverage": ctx.cov, "records": out}


def celebrations(ctx, day=None, recs=None):
    """Factual sentences about `day` (default today), only when profile.celebrations is on. Same evidence, same text."""
    if not ctx.settings.get("celebrations", True):
        return []
    day = day or ctx.today
    m = day_metrics(ctx, day)
    recs = {r["id"]: r for r in (recs or records(ctx))["records"]}
    word = "today" if day == ctx.today else "on %s" % day
    out = []

    def compare(rid, value, what):
        r = recs.get(rid) or {}
        if not value or not r.get("value"):
            return ""
        if r["date"] == day and r["value"] == value:
            return " That's your highest %s day %s%s." % (what, ctx.cov["label"], " (tied)" if r.get("ties") else "")
        if r["value"] == value:
            return " That matches your highest %s day %s." % (what, ctx.cov["label"])
        return " Your highest %s day %s was %d." % (what, ctx.cov["label"], r["value"])

    raised, merged = m["prs_raised"]["value"] or 0, m["prs_merged"]["value"] or 0
    if raised or merged:
        if raised and merged:
            s = "You opened %s %s and merged %d." % (_plural(raised, "PR"), word, merged)
        elif raised:
            s = "You opened %s %s." % (_plural(raised, "PR"), word)
        else:
            s = "You merged %s %s." % (_plural(merged, "PR"), word)
        out.append(s + compare("prs_raised", raised, "PR-opening"))
    n = m["commits"]["value"] or 0
    if n:
        out.append("You made %s %s (committed changes +%d / -%d lines)." % (
            _plural(n, "authored commit"), word, m["lines"]["added"] or 0, m["lines"]["deleted"] or 0) +
            compare("commits", n, "commit"))
    t = m["tickets_completed"]["value"] or 0
    if t:
        out.append("You completed %s %s." % (_plural(t, "ticket"), word) + compare("tickets_completed", t, "ticket-completing"))
    return out


# --------------------------------------------------------------------------- next actions

def next_actions(ctx, day=None):
    """A few concrete next steps from your own work: your open PRs' recorded review state, then the release / UAT
    reminders and delivery alerts of tickets that are yours now. Nothing on a day off. Inactivity alone never becomes
    a blocker."""
    from . import timesheet
    day = day or ctx.today
    kind = ctx.day_kind(day)
    if kind not in ("workday", "half_day"):
        return {"day_off": True, "kind": kind, "actions": [], "more": 0,
                "note": "A day off (%s): nothing is suggested today." % timesheet.TAGS.get(kind, kind)}
    acts = []
    order = {"approved": 0, "changes_requested": 1, "": 2}
    open_prs = []
    for r in ctx.repos:
        led = r.get("ledger") or {}
        if r["prs"]["status"] not in ("ok", "partial", "error"):
            continue
        for p in ((led.get("prs") or {}).get("items") or {}).values():
            if p.get("state") in ("open", "draft"):
                open_prs.append(dict(p, repo=r["name"]))
    for p in sorted(open_prs, key=lambda p: (p.get("state") == "draft", order.get(p.get("review") or "", 2),
                                             str(p.get("created_at")))):
        name = "%s PR #%s" % (p["repo"], p.get("number"))
        since = str(p.get("created_at") or "")[:10]
        if p.get("state") == "draft":
            text = "%s is a draft (opened %s): mark it ready for review when it is." % (name, since)
        elif p.get("review") == "approved":
            text = "%s is approved: merge it when you're ready." % name
        elif p.get("review") == "changes_requested":
            text = "%s has changes requested." % name
        else:
            text = "%s is open, awaiting review since %s." % (name, since)
        acts.append({"kind": "pr", "text": text, "url": p.get("url") or "", "ticket": p.get("ticket"),
                     "why": "a PR you authored (%s)" % (p.get("review") or p.get("state"))})
    for t in ctx.tickets():
        own = t.get("ownership") or {}
        if not own.get("current"):
            continue
        for kind, x in [("release", r) for r in t.get("reminders") or []] + [("delivery", a) for a in t.get("alerts") or []]:
            text = str(x.get("text") or "")
            if not text.startswith(t["key"]):  # reminders often name their ticket already
                text = "%s: %s" % (t["key"], text)
            acts.append({"kind": kind, "text": text, "ticket": t["key"], "url": "", "why": own.get("reason") or ""})
    return {"day_off": False, "kind": kind, "actions": acts[:MAX_ACTIONS], "more": max(0, len(acts) - MAX_ACTIONS),
            "note": "" if acts else "Nothing from your PRs or tickets needs a next step right now."}


# --------------------------------------------------------------------------- your tickets

def my_tickets(ctx):
    """Your current tickets with why they are yours; reassigned ones apart (your activity on them is kept)."""
    cur, gone, other = [], [], 0
    for t in ctx.tickets():
        own = t.get("ownership") or {}
        row = {"key": t["key"], "title": t.get("title") or t.get("title_hint") or "", "status": t.get("status") or "",
               "last": t.get("last") or "", "state": own.get("state"), "reason": own.get("reason") or ""}
        if own.get("current"):
            cur.append(row)
        elif own.get("state") == "reassigned":
            gone.append(row)
        else:
            other += 1
    return {"current": cur, "reassigned": gone, "not_yours": other}


# --------------------------------------------------------------------------- reflections

def reflection_path(day):
    return common.hp("data", "reflections", day[:7] + ".json")


def read_reflection(day):
    rec = ((common.read_json(reflection_path(day), {}) or {}).get("days") or {}).get(day)
    return rec if isinstance(rec, dict) else None


def save_reflection(day, text, now=None):
    """Your note for a day (self-reported, local only). Secrets and patient data are masked before it is saved."""
    if not isinstance(day, str) or not DAY.match(day):
        raise ValueError("Give the day as YYYY-MM-DD.")
    if not isinstance(text, str):
        raise ValueError("The note is text.")
    text = text.strip()
    if len(text) > REFLECTION_MAX:
        raise ValueError("Keep the note under %d characters." % REFLECTION_MAX)
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise ValueError("The note has control characters.")
    at = (now or common.now()).isoformat(timespec="seconds")
    p = reflection_path(day)
    data = common.read_json(p, {}) or {}
    days = data.setdefault("days", {})
    old = days.get(day) or {}
    days[day] = {"text": redact(text, mode="local"), "at": old.get("at") or at, "updated_at": at}
    common.write_json(p, data)
    return days[day]


def reflections(a, b):
    out = {}
    for ym in sorted({d[:7] for d in _days(a, b)}):
        for d, rec in ((common.read_json(common.hp("data", "reflections", ym + ".json"), {}) or {}).get("days") or {}).items():
            if a <= d <= b and isinstance(rec, dict) and str(rec.get("text") or "").strip():
                out[d] = rec
    return out


# --------------------------------------------------------------------------- answers

def sources(ctx):
    """What the numbers rest on, per repository and for Jira: shown as tracking transparency."""
    st = profile.status(ctx.cfg)
    return {"repos": [{"name": r["name"], "commits": r["commits"], "prs": r["prs"]} for r in ctx.repos],
            "git": st["git"], "jira": st["jira"], "hosts": st["hosts"], "timezone": st["timezone"],
            "coverage": ctx.cov, "signature": ctx.sig}


def today(cfg=None, now=None, day=None):
    ctx = Ctx(cfg, now)
    day = day or ctx.today
    m = day_metrics(ctx, day, evidence=True)
    recs = records(ctx)
    note = ""
    if not (m["activity"] or 0):
        note = ("No commits or PRs are recorded for this day. Design, debugging, reviews, learning and helping "
                "teammates often leave no commit: the reflection is a place for those.")
    return {"date": day, "today": day == ctx.today, "metrics": m, "celebrations": celebrations(ctx, day, recs),
            "note": note, "next_actions": next_actions(ctx, day), "reflection": read_reflection(day),
            "reflection_on": bool(ctx.settings.get("reflection", True)), "tickets": my_tickets(ctx),
            "profile": {"display_name": ctx.pol.p["display_name"], "timezone": ctx.settings["timezone"] or "",
                        "animations": bool(ctx.settings.get("animations", True)), "coverage": ctx.cov},
            "sources": sources(ctx)}


def _sum(days, path):
    vals, sts = [], set()
    for m in days:
        cur = m
        for k in path:
            cur = cur[k]
        st = m[path[0]]["status"] if isinstance(m[path[0]], dict) and "status" in m[path[0]] else "known"
        sts.add(st)
        if cur is not None:
            vals.append(cur)
    status = "unknown" if not vals else ("partial" if sts & {"unknown", "partial"} else
                                        "stale" if "stale" in sts else ("known" if sum(vals) else "zero"))
    return {"value": sum(vals) if vals else None, "status": status}


def week(cfg=None, date=None, now=None):
    """Monday to Sunday around `date` (default today): each day and the totals; days off are only marked."""
    ctx = Ctx(cfg, now)
    d = _d(date or ctx.today)
    start = d - _dt.timedelta(days=d.weekday())
    days = [day_metrics(ctx, x) for x in _days(start.isoformat(), (start + _dt.timedelta(days=6)).isoformat())]
    totals = {"commits": _sum(days, ("commits", "value")), "lines_added": _sum(days, ("lines", "added")),
              "lines_deleted": _sum(days, ("lines", "deleted")), "prs_raised": _sum(days, ("prs_raised", "value")),
              "prs_merged": _sum(days, ("prs_merged", "value")),
              "tickets_completed": _sum(days, ("tickets_completed", "value"))}
    return {"start": days[0]["date"], "end": days[-1]["date"], "days": days, "totals": totals, "coverage": ctx.cov}


def month(cfg=None, ym=None, now=None):
    """A Monday-first calendar of a month: activity count per day (None when unknown) and a 0-4 level."""
    ctx = Ctx(cfg, now)
    ym = ym or ctx.today[:7]
    if not MONTH.match(ym):
        raise ValueError("month must be YYYY-MM")
    y, mo = int(ym[:4]), int(ym[5:])
    last = _cal.monthrange(y, mo)[1]
    days = [day_metrics(ctx, "%s-%02d" % (ym, i)) for i in range(1, last + 1)]
    peak = max([m["activity"] or 0 for m in days] + [1])
    cells = []
    for m in days:
        a = m["activity"]
        lvl = 0 if not a else min(4, 1 + int(3 * (a - 1) / max(1, peak - 1))) if peak > 1 else 1
        cells.append({"date": m["date"], "kind": m["kind"], "activity": a, "level": lvl,
                      "commits": m["commits"]["value"], "prs_raised": m["prs_raised"]["value"],
                      "prs_merged": m["prs_merged"]["value"], "tickets_completed": m["tickets_completed"]["value"],
                      "status": m["commits"]["status"] if m["commits"]["status"] != "zero" else m["prs_raised"]["status"]})
    lead = _d(days[0]["date"]).weekday()
    grid = [None] * lead + cells
    grid += [None] * (-len(grid) % 7)
    return {"month": ym, "weeks": [grid[i:i + 7] for i in range(0, len(grid), 7)], "coverage": ctx.cov,
            "today": ctx.today}


def weekly_wins(cfg=None, date=None, now=None):
    """A factual summary of the week around `date`: merged PRs, PRs raised, tickets completed, commits and committed
    changes, records set, the day summaries already written at day close, and your own notes (self-reported). No model
    call. `copy_text` has passed the public redaction (what may leave this machine)."""
    ctx = Ctx(cfg, now)
    w = week(ctx.cfg, date, ctx.now)
    a, b = w["start"], w["end"]
    merged = [p for d in _days(a, b) for p in sorted(ctx.merged.get(d) or [], key=lambda p: str(p.get("merged_at")))]
    raised = [p for d in _days(a, b) for p in ctx.raised.get(d) or []]
    done = []
    for m in w["days"]:
        if m["tickets_completed"]["value"]:
            done += [t["key"] for t in _completed(ctx, m["date"])[3]]
    recs = [r for r in records(ctx)["records"] if r.get("date") and a <= r["date"] <= b]
    from .store import day_path
    summaries = []
    for d in _days(a, b):
        s = str((common.read_json(day_path(d), {}) or {}).get("summary") or "").strip()
        if s:
            summaries.append((d, " ".join(s.split())))
    notes = reflections(a, b)
    t = w["totals"]
    L = ["# Weekly wins: %s to %s" % (a, b), ""]
    if merged:
        L.append("## Merged PRs")
        L += ["- %s #%s %s%s" % (p["repo"], p.get("number"), p.get("title") or "", " (%s)" % p["url"] if p.get("url") else "")
              for p in merged]
        L.append("")
    facts = []
    if raised:
        drafts = sum(1 for p in raised if p.get("draft"))
        facts.append("- PRs raised: %d%s." % (len(raised), " (%d draft)" % drafts if drafts else ""))
    if done:
        facts.append("- Tickets completed: %s." % ", ".join(sorted(set(done))))
    if t["commits"]["value"]:
        facts.append("- Authored commits: %d; committed changes +%d / -%d lines." % (
            t["commits"]["value"], t["lines_added"]["value"] or 0, t["lines_deleted"]["value"] or 0))
    for r in recs:
        facts.append("- Personal record, %s: %s on %s (%s)." % (r["label"][:1].lower() + r["label"][1:], r["value"], r["date"],
                                                              r["coverage"]))
    if facts:
        L += ["## This week"] + facts
    if summaries:
        L += ["", "## Day notes (from the day-close summary)"] + ["- %s: %s" % (d, common.truncate(s, 300)) for d, s in summaries]
    if notes:
        L += ["", "## Your notes (self-reported)"] + ["- %s: %s" % (d, " ".join(n["text"].split())) for d, n in sorted(notes.items())]
    if len(L) == 2:
        L.append("Nothing is recorded for this week yet. Work that leaves no commit or PR still counts: add a note.")
    md = "\n".join(L).rstrip() + "\n"
    return {"start": a, "end": b, "markdown": md, "copy_text": redact(md, mode="public"), "totals": t,
            "merged": [_pr_ev(p) for p in merged], "raised": [_pr_ev(p) for p in raised], "tickets_completed": sorted(set(done)),
            "records": recs, "notes": notes, "coverage": ctx.cov}


def records_answer(cfg=None, now=None):
    return records(Ctx(cfg, now))


# --------------------------------------------------------------------------- Markdown for the CLI and MCP

def _v(x):
    return "unknown" if x is None else str(x)


def _metric_line(label, m, extra=""):
    st = m["status"]
    if st in ("known", "zero"):
        return "- %s: %s%s" % (label, _v(m["value"]), extra)
    return "- %s: %s (%s%s)%s" % (label, _v(m.get("value")), st, ": " + m["why"] if m.get("why") else "", extra)


def markdown(period="today", date=None, cfg=None, now=None, include_ids=False):
    cfg = cfg if cfg is not None else common.load_config()
    head = profile.context_text(cfg)
    L = []
    if head:
        L += [head, ""]
    if include_ids:
        p = profile.load(cfg)
        L += ["Confirmed commit emails: %s" % (", ".join(p["aliases"]) or "none"),
              "Confirmed accounts: %s" % (", ".join("%s %s %s" % (a["provider"], a["instance"] or "-", a["id"] or a["login"])
                                                    for a in p["accounts"] + ([p["jira"]] if p["jira"] and p["jira"].get("legacy") else []))
                                          or "none"), ""]
    if period in ("today", "day"):
        t = today(cfg, now, date)
        m = t["metrics"]
        L.append("# Progress %s (%s)" % (t["date"], m["kind"].replace("_", " ")))
        L.append(_metric_line("Authored commits", m["commits"]))
        ln = m["lines"]
        L.append(_metric_line("Committed changes", {"value": None if ln["added"] is None else "+%d / -%d lines" % (ln["added"], ln["deleted"]),
                                                     "status": ln["status"], "why": ln["why"]}))
        if m["shared"]["commits"]:
            L.append("- Shared commits (with co-authors, not split): %d (+%d / -%d)" % (
                m["shared"]["commits"], m["shared"]["added"], m["shared"]["deleted"]))
        if m["coauthored"]["commits"]:
            L.append("- Co-authored only: %d (+%d / -%d)" % (m["coauthored"]["commits"], m["coauthored"]["added"],
                                                            m["coauthored"]["deleted"]))
        if m["excluded"]["files"]:
            L.append("- Left out of line counts (lockfiles, generated, vendored): +%d / -%d in %s" % (
                m["excluded"]["added"], m["excluded"]["deleted"], _plural(m["excluded"]["files"], "file")))
        L.append(_metric_line("PRs raised", m["prs_raised"], " (%d draft)" % m["prs_raised"]["drafts"] if m["prs_raised"]["drafts"] else ""))
        L.append(_metric_line("PRs merged", m["prs_merged"]))
        L.append(_metric_line("Tickets completed", m["tickets_completed"]))
        ev = m["evidence"]
        if ev["commits"]:
            L += ["", "## Commits"] + ["- %s %s %s (+%d / -%d)%s" % (c["repo"], c["sha"], c["subject"], c["added"], c["deleted"],
                                                                    " · shared" if c["shared"] else " · co-authored" if c["role"] == "coauthor" else "")
                                       for c in ev["commits"]]
        if ev["prs_raised"] or ev["prs_merged"]:
            L += ["", "## PRs"] + ["- raised %s #%s %s%s" % (p["repo"], p["number"], p["title"], " (draft)" if p["draft"] else "")
                                   for p in ev["prs_raised"]] + \
                ["- merged %s #%s %s" % (p["repo"], p["number"], p["title"]) for p in ev["prs_merged"]]
        for u in m.get("unresolved") or []:
            L.append("- Not counted: %s %s at %s (%s)" % (u["repo"], u["what"], u["at"], u["why"]))
        if t["celebrations"]:
            L += [""] + t["celebrations"]
        if t["note"]:
            L += ["", t["note"]]
        na = t["next_actions"]
        L += ["", "## Next"] + ([na["note"]] if na["note"] else []) + ["- " + x["text"] for x in na["actions"]]
        if t["reflection"]:
            L += ["", "Your note (self-reported): " + t["reflection"]["text"]]
    elif period == "week":
        w = week(cfg, date, now)
        tt = w["totals"]
        L.append("# Week %s to %s" % (w["start"], w["end"]))
        for label, k in (("Authored commits", "commits"), ("Lines added", "lines_added"), ("Lines deleted", "lines_deleted"),
                         ("PRs raised", "prs_raised"), ("PRs merged", "prs_merged"), ("Tickets completed", "tickets_completed")):
            L.append(_metric_line(label, dict(tt[k], why="")))
        L.append("")
        for m in w["days"]:
            L.append("- %s %s: %s commits, %s raised, %s merged" % (m["date"], m["kind"].replace("_", " "),
                                                                    _v(m["commits"]["value"]), _v(m["prs_raised"]["value"]),
                                                                    _v(m["prs_merged"]["value"])))
    elif period == "month":
        mo = month(cfg, (date or "")[:7] or None, now)
        L.append("# %s" % mo["month"])
        for wk in mo["weeks"]:
            L.append("- " + " ".join("%s:%s" % (c["date"][8:], "?" if c["activity"] is None else c["activity"]) if c else "--"
                                     for c in wk))
    elif period == "records":
        r = records_answer(cfg, now)
        L.append("# Personal records (%s)" % r["coverage"]["label"])
        for x in r["records"]:
            if x["value"]:
                L.append("- %s: %s on %s%s" % (x["label"], x["value"], x["date"],
                                              " (tied with %s)" % _plural(x["ties"], "other day") if x["ties"] else ""))
            else:
                L.append("- %s: %s" % (x["label"], x.get("why") or "none yet"))
    elif period == "wins":
        L.append(weekly_wins(cfg, date, now)["copy_text"])
    else:
        raise ValueError("period must be today, week, month, records or wins")
    return "\n".join(L).rstrip() + "\n"
