"""Render markdown views from data/. Deterministic: same data + same health -> same bytes.
Text between <!-- my-notes:start --> and <!-- my-notes:end --> is preserved across renders."""
import datetime as _dt
import os
import re

from . import common
from .redact import redact
from .store import Store

NOTES_START, NOTES_END = "<!-- my-notes:start -->", "<!-- my-notes:end -->"
NOTES_BLOCK = NOTES_START + "\n" + NOTES_END
TAG = {"finding": "finding", "root_cause": "root cause", "how_it_works": "how it works", "fix": "fix",
       "decision": "decision", "config": "config", "error": "error", "repro_test": "repro", "reference": "reference",
       "todo": "todo", "question": "question", "blocker": "blocker", "note": "note"}
DIALECT = {"postgresql": ("PostgreSQL", "sql"), "sqlserver": ("SQL Server", "sql"), "mongodb": ("MongoDB", "javascript"),
           "mysql": ("MySQL", "sql"), "other": ("SQL", "sql")}
OPEN_KINDS = ("todo", "question", "blocker")


# --------------------------------------------------------------------------- helpers

def cell(s):
    return re.sub(r"\s+", " ", str(s or "")).replace("|", "\\|").strip()


def code(s):
    s = str(s or "")
    return "``%s``" % s if "`" in s else "`%s`" % s


def fence_safe(s):
    return str(s or "").replace("```", "ˋˋˋ")


def daily_rel(day):
    return "daily/%s/%s/%s.md" % (day[:4], day[5:7], day)


def extract_notes(path):
    txt = common.read_text(path)
    i, j = txt.find(NOTES_START), txt.find(NOTES_END)
    if i >= 0 and j > i:
        return txt[i: j + len(NOTES_END)]
    return NOTES_BLOCK


def write_view(path, body, mode="local", tail=None):
    """Write a rendered view, carrying over the existing my-notes block. Returns True if changed.
    mode: redaction mode (daily files: public). tail: extra section already masked for its own mode (QA values),
    inserted before "## My notes"."""
    notes = extract_notes(path)
    body = redact(body, mode=mode)
    if tail:
        i = body.rfind("## My notes")
        body = (body[:i] + tail.rstrip("\n") + "\n\n" + body[i:]) if i >= 0 else body + "\n" + tail
    if mode == "public":
        notes = redact(notes, mode="public")
    body = body.replace(NOTES_BLOCK, notes, 1)
    return common.write_text(path, body)


def item_line(it, with_date=False):
    ev = ", ".join(code(e) for e in (it.get("evidence") or [])[:5])
    conf = it.get("confidence") or ""
    parts = ["`[%s]`" % TAG.get(it.get("kind"), it.get("kind")), "**%s**" % it.get("title", "").strip()]
    if with_date:
        parts.insert(0, it.get("date", ""))
    s = " ".join(parts)
    if it.get("detail"):
        s += " — " + it["detail"].strip()
    tail = []
    if conf and conf != "confirmed":
        tail.append(conf)
    if ev:
        tail.append("evidence: " + ev)
    if it.get("history"):
        tail.append("updated from " + (it["history"][-1].get("confidence") or "earlier note"))
    if tail:
        s += " _(" + "; ".join(tail) + ")_"
    return s


def sort_items(items, newest_first=True):
    return sorted(items, key=lambda i: (i.get("date", ""), i.get("time", ""), i.get("title", "")), reverse=newest_first)


# --------------------------------------------------------------------------- branches

def _short_text(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _date(iso):
    d = common.parse_ts(iso)
    return d.strftime("%Y-%m-%d %H:%M") if d else "—"


def branch_parent(rec):
    p = rec.get("parent")
    if not p:
        return "?"
    src = rec.get("parent_source") or ""
    if src in ("command", "reflog", "reflog (HEAD)"):
        return code(p) + (" _(command)_" if src == "command" else "")
    alts = rec.get("parent_alternatives") or []
    return code(p) + (" _(inferred; same commit as %s)_" % ", ".join(alts) if alts else " _(inferred)_")


def branch_created(rec):
    c = _date(rec.get("created_at"))
    src = rec.get("created_source")
    if src == "first own commit":
        c = "≈ " + c + " _(first commit)_"
    elif src == "local checkout":
        c = "≤ " + c + " _(checked out from remote)_"
    return c


def branch_where(rec):
    if rec.get("deleted_at") and not rec.get("local") and not rec.get("on_remote"):
        return "deleted %s" % rec["deleted_at"][:10]
    w = [x for x, ok in (("local", rec.get("local")), ("remote", rec.get("on_remote"))) if ok]
    if rec.get("remote_gone"):
        w.append("deleted on remote")
    return " + ".join(w) or "—"


def branch_table(recs, with_repo=False, ticket_link="../tickets/"):
    """ticket_link=None hides the Ticket column."""
    recs = sorted(recs, key=lambda r: (r.get("created_at") or r.get("first_seen") or ""), reverse=True)
    head = (["Repo"] if with_repo else []) + ["Branch"] + (["Ticket"] if ticket_link is not None else []) + \
        ["Parent", "Created", "Last commit", "Own commits / parent moved", "Unpushed", "Where"]
    L = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in recs:
        ab = "%s / %s" % (r.get("ahead"), r.get("behind") if r.get("behind") is not None else "?") \
            if r.get("ahead") is not None else "—"
        row = ([cell(r.get("repo_name"))] if with_repo else [])
        row.append(code(r["branch"]))
        if ticket_link is not None:
            row.append("[%s](%s%s/README.md)" % (r["ticket"], ticket_link, r["ticket"]) if r.get("ticket") else "—")
        row += [branch_parent(r), branch_created(r),
                "%s — %s" % (_date(r.get("last_commit")), cell(_short_text(r.get("last_subject"), 60))), ab,
                str(r.get("unpushed") or 0), branch_where(r)]
        L.append("| " + " | ".join(row) + " |")
    return L


def render_branches(store):
    allb = store.branches()
    L = ["# Branches", "",
         "Work branches in watched repos: where each came from, when it was created, and what is still unpushed.",
         "Parent and created time come from the captured command or git's reflog (exact) unless marked _inferred_ or",
         "_first commit_. Whether the work reached qa / release / … is on each ticket's page (Branches & PRs).", ""]
    if not allb:
        L.append("_No branches recorded yet._")
    for repo in sorted(allb):
        recs = [r for r in allb[repo].values() if isinstance(r, dict) and r.get("branch")]
        live = [r for r in recs if r.get("local") or r.get("on_remote") or not r.get("deleted_at")]
        gone = [r for r in recs if r not in live]
        L += ["## " + repo, ""] + (branch_table(live) if live else ["_No work branches._"]) + [""]
        if gone:
            L += ["<details><summary>Deleted (%d, kept for history)</summary>" % len(gone), ""] + \
                 branch_table(gone) + ["", "</details>", ""]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- branches & PRs per ticket (delivery)

ALERT_ORDER = ["lost", "only-copy", "gap", "unpushed", "pr-base", "wrong-parent", "check", "pr-stale"]
DELIVERY_SYMBOL = {"in": "✓", "same": "✓", "squash": "✓", "open": "⏳", "missing": "✗", "reverted": "↩", "check": "?"}
PR_SOURCE = {"merge-commit": "PR info from merge commits only", "gh": "PRs from gh", "glab": "PRs from glab",
             "az": "PRs from az"}


def git_alerts(tk):
    al = list(((tk.get("git") or {}).get("alerts")) or [])
    return sorted(al, key=lambda a: ALERT_ORDER.index(a["kind"]) if a.get("kind") in ALERT_ORDER else 99)


def alert_line(t, tk):
    """One factual line for SessionStart: the most serious alert, and how many more."""
    al = git_alerts(tk)
    if not al:
        return ""
    line = "%s: %s" % (t, al[0]["text"])
    return line + (" (+%d more on the ticket page)" % (len(al) - 1) if len(al) > 1 else "")


def git_section(tk):
    g = tk.get("git") or {}
    repos = g.get("repos") or {}
    if not repos:
        return None
    L = ["_%s_" % g.get("summary", "")]
    al = git_alerts(tk)
    if al:
        L += ["", "**Needs attention**"] + ["- ⚠ %s%s%s" % (a["text"], " — " if a.get("commits") else "",
                                                           ", ".join(code(c) for c in a.get("commits", [])[:8])) for a in al]
    for name, x in sorted(repos.items()):
        how = ("strategy %s (%s)" % (x["strategy"], x.get("strategy_how"))) if x.get("strategy") else (x.get("note") or "no strategy")
        L += ["", "#### %s — %s · %s" % (name, how, PR_SOURCE.get(x.get("pr_source"), "PR info unavailable"))]
        if x.get("rows"):
            tg = x.get("targets") or []
            L += ["", "| Commit | Subject | " + " | ".join(cell(t["name"]) for t in tg) + " |", "|---|---|" + "---|" * len(tg)]
            for row in x["rows"]:
                cells = []
                for t in tg:
                    c = row["cells"].get(t["role"]) or {}
                    extra = c.get("why") or ("PR #%s" % c["pr"] if c.get("pr") else "")
                    cells.append(DELIVERY_SYMBOL.get(c.get("s"), "?") + (" " + cell(extra) if extra else ""))
                L.append("| %s | %s | %s |" % (", ".join(code(s) for s in row["shas"][:3]), cell(_short_text(row["subject"], 70)),
                                              " | ".join(cells)))
        if x.get("branches"):
            L += ["", "| Branch | Parent | Status | PRs | Unpushed | Own / behind | Linked by |", "|---|---|---|---|---|---|---|"]
            for b in x["branches"]:
                st = b["status"] + (" · %s?" % b["suggested"] if b.get("suggested") else "")
                L.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                    code(b["branch"]), branch_parent(b), cell(st), ", ".join("#%s" % n for n in b.get("prs") or []) or "—",
                    b.get("unpushed") or 0, "%s / %s" % (b.get("own"), b.get("behind") if b.get("behind") is not None else "?"),
                    b.get("link") or "—"))
        if x.get("prs"):
            L += ["", "| PR | Source → target | State | Review | Merged |", "|---|---|---|---|---|"]
            for pr in x["prs"]:
                num = "[#%s](%s)" % (pr["number"], pr["url"]) if (pr.get("url") or "").startswith("http") else "#%s" % pr["number"]
                L.append("| %s | %s → %s | %s | %s | %s |" % (num, code(pr.get("source")), code(pr.get("target")), pr.get("state"),
                                                            pr.get("review") or "—", _date(pr.get("merged_at")) if pr.get("merged_at") else "—"))
    L += ["", "_Fix a wrong link or mark a branch abandoned: `/cg:branch link|unlink|abandon|restore …`._"]
    return L


def ticket_branches(store, t):
    return [r for repo in store.branches().values() for r in repo.values() if r.get("ticket") == t]


# --------------------------------------------------------------------------- daily file

def render_day(store, day, cfg, health):
    dd = store.day(day)
    d = _dt.date.fromisoformat(day)
    upd = common.parse_ts(dd.get("updated_at"))
    L = ["# Worklog — %s" % d.strftime("%a %d %b %Y"),
         "_Auto-generated · last updated %s · day closes %s · edit only inside \"My notes\"_" %
         (upd.strftime("%H:%M") if upd else "--:--", cfg.get("day_starts_at", "04:00")), ""]

    # Summary
    L.append("## Summary")
    if dd.get("summary"):
        L.append(dd["summary"].strip() + ("" if dd.get("summary_final") else "  \n_(rolling — finalised when the day closes)_"))
    else:
        acts = [a["text"] for a in dd.get("activity", [])][-5:]
        L.append(" ".join(acts) + "  \n_(rolling — finalised when the day closes)_" if acts else "_No activity recorded yet._")
    L.append("")

    if dd.get("carried_todos"):
        L.append("## Carried over from previous day")
        L += ["- [ ] " + t for t in dd["carried_todos"]]
        L.append("")

    # Tickets table
    tickets = sorted(dd.get("tickets", {}).items(), key=lambda kv: -(kv[1].get("minutes") or 0))
    L.append("## Tickets")
    if tickets:
        L += ["| Ticket | Title | Repo · branch | ≈ Time | Findings | Queries | APIs |", "|---|---|---|---|---|---|---|"]
        for t, slot in tickets:
            tk = store.ticket(t)
            nf = len([i for i in slot.get("item_ids", []) if tk["items"].get(i, {}).get("kind") not in OPEN_KINDS])
            L.append("| [%s](../../../tickets/%s/README.md) | %s | %s | %s | %d | %d | %d |" % (
                t, t, cell(tk.get("title")), cell(", ".join(slot.get("where", []))), fmt_time(slot.get("minutes")),
                nf, len(slot.get("query_ids", [])), len(slot.get("api_keys", []))))
        L.append("")
        for t, slot in tickets:
            L += ticket_section(store, dd, t, slot)
    else:
        L += ["_No ticket work recorded._", ""]

    # Other work
    if dd.get("other"):
        L.append("## Other work (no ticket)")
        for g, slot in sorted(dd["other"].items()):
            L += other_section(dd, g, slot)

    # Fallbacks without any slot
    pend = [(sid, fb) for sid, fb in sorted(dd.get("fallback", {}).items())
            if not fb.get("ticket") and fb.get("group") not in dd.get("other", {})]
    if pend:
        if not dd.get("other"):
            L.append("## Other work (no ticket)")
        for sid, fb in pend:
            L.append("### %s" % fb.get("group", "session " + sid[:8]))
            L += fallback_lines(fb)
            L.append("")

    # Outside Claude Code
    out = dd.get("outside", {})
    if out.get("bruno") or out.get("sql") or out.get("inbox"):
        L.append("## Outside Claude Code")
        for b in out.get("bruno", []):
            L.append("- %s Bruno · %s %s — %s _(%s)_%s" % (common.hhmm(b.get("ts")), b.get("method"), code(b.get("url")),
                                                          b.get("name"), b.get("collection"),
                                                          " · " + b["ticket"] if b.get("ticket") else ""))
        for s in out.get("sql", []):
            L.append("- %s SQL script %s (%s)%s" % (common.hhmm(s.get("ts")), code(s.get("name")),
                                                    DIALECT.get(s.get("dialect"), ("SQL",))[0],
                                                    " · " + s["ticket"] if s.get("ticket") else ""))
            L += ["", "  ```" + DIALECT.get(s.get("dialect"), ("", "sql"))[1]] + \
                 ["  " + x for x in fence_safe(common.truncate(s.get("text"), 1500)).splitlines()] + ["  ```", ""]
        for n in out.get("inbox", []):
            L.append("- %s Note%s: %s" % (common.hhmm(n.get("ts")), " · " + n["ticket"] if n.get("ticket") else "", n.get("text")))
        L.append("")

    if dd.get("unfiled_artifacts"):
        L.append("## Unfiled artifacts")
        L.append("_No ticket could be inferred. Rename with the ticket ID (e.g. `%s_name.md`) and drop into `_drop/`._"
                 % example_ticket(cfg))
        L += ["- %s %s _(%s)_" % (u.get("time", ""), code(u.get("path")), u.get("source")) for u in dd["unfiled_artifacts"][-30:]]
        L.append("")

    # Commits
    L.append("## Commits today")
    if dd.get("commits"):
        for c in sorted(dd["commits"], key=lambda c: c.get("date", "")):
            L.append("- %s %s %s %s _(%s%s)_" % (common.hhmm(c.get("date")), c.get("repo_name"), code(c.get("short")),
                                                c.get("subject"), c.get("ref") or "-", " · " + c["ticket"] if c.get("ticket") else ""))
    else:
        L.append("_None in watched repos._")
    L.append("")

    # Branch activity
    acts = sorted(dd.get("branch_activity") or [], key=lambda a: a.get("ts", ""))
    if acts:
        L.append("## Branch activity")
        for a in acts[-60:]:
            k = a.get("kind")
            if k == "switch":
                what = "switched %s → %s" % (code(a.get("from")), code(a.get("to")))
            elif k == "create":
                what = "**created** %s from %s" % (code(a.get("branch")), code(a.get("from") or "?"))
            elif k == "delete":
                what = "deleted local branch %s" % code(a.get("branch"))
            else:
                what = "%s on %s" % (a.get("text"), code(a.get("branch") or "?"))
            L.append("- %s %s · %s" % (common.hhmm(a.get("ts")), a.get("repo_name"), what))
        L.append("")

    # Jira worklog (the Slack message, kept here so nothing is lost when a send fails)
    from . import jiraworklog
    L += jiraworklog.daily_section(day)

    # Standup
    L.append("## Standup for next working day")
    su = dd.get("standup") or {}
    if not su:
        su = draft_standup(store, dd)
        L.append("_Draft — the final version is written when the day closes._")
    for label, key in (("Yesterday", "yesterday"), ("Today", "today"), ("Blockers", "blockers")):
        vals = su.get(key) or []
        L.append("**%s**" % label)
        L += ["- " + v for v in vals] if vals else ["- none"]
    L.append("")

    L += ["## My notes", NOTES_BLOCK, "", "---", health_footer(dd, health)]
    return "\n".join(L) + "\n"


def fmt_time(m):
    return ("≈ " + common.fmt_minutes(m)) if m else "—"


def ticket_section(store, dd, t, slot):
    tk = store.ticket(t)
    L = ["### %s — %s" % (t, tk.get("title") or "(untitled)")]
    cons = (dd.get("consolidated") or {}).get(t) or {}
    did = cons.get("what_i_did") or []
    if did:
        L.append("- **What I did**")
        L += ["  - " + x for x in did]
    elif slot.get("what_i_did"):
        L.append("- **What I did**")
        L += ["  - %s — %s" % (w.get("time") or "--:--", w.get("text")) for w in sorted(slot["what_i_did"], key=lambda w: w.get("time", ""))]
    items = [tk["items"][i] for i in slot.get("item_ids", []) if i in tk["items"]]
    finds = [i for i in sort_items(items, False) if i.get("kind") not in OPEN_KINDS]
    if finds:
        L.append("- **Findings**")
        L += ["  - " + item_line(i) for i in finds]
    tables = [tk["tables"][k]["name"] for k in slot.get("table_keys", []) if k in tk["tables"]]
    qs = [tk["queries"][q] for q in slot.get("query_ids", []) if q in tk["queries"]]
    if tables or qs:
        L.append("- **DB**" + (" — tables: " + ", ".join(code(x) for x in tables) if tables else ""))
        for q in qs:
            L.append("  - query: [%s](../../../tickets/%s/queries.md#q-%s) _(%s · %s)_" % (
                cell(q.get("purpose") or common.truncate(q.get("text"), 60)), t, q["id"],
                DIALECT.get(q.get("dialect"), ("SQL",))[0], q.get("kind")))
    apis = [tk["apis"][k] for k in slot.get("api_keys", []) if k in tk["apis"]]
    igs = [tk["integrations"][k] for k in slot.get("integration_keys", []) if k in tk["integrations"]]
    if apis or igs:
        L.append("- **APIs / integrations**")
        L += ["  - %s %s%s — %s" % (a.get("method"), code(a.get("path")),
                                    " @ " + a["host_or_service"] if a.get("host_or_service") else "", a.get("purpose") or a.get("direction"))
              for a in apis]
        L += ["  - %s · %s — %s" % (g.get("system"), g.get("artifact"), g.get("notes")) for g in igs]
    summary = (tk.get("git") or {}).get("summary")
    if summary:
        L.append("- **Branches & PRs** — " + summary)
    commits = [c for c in dd.get("commits", []) if c.get("ticket") == t]
    if slot.get("files_changed") or commits:
        L.append("- **Files changed · commits**")
        L += ["  - " + code(f) for f in slot.get("files_changed", [])[:25]]
        L += ["  - commit %s %s" % (code(c.get("short")), c.get("subject")) for c in commits]
    for key, label in (("requests_changed", "Requests saved / changed"), ("dbflow_updates", "DB flow updates"),
                       ("new_artifacts", "New artifacts")):
        if slot.get(key):
            L.append("- **%s**" % label)
            L += ["  - " + x for x in slot[key][:15]]
    opens = [i for i in sort_items(items, False) if i.get("kind") in OPEN_KINDS]
    nxt = cons.get("next_steps") or []
    if opens or nxt:
        L.append("- **Next / open questions / blockers**")
        L += ["  - " + item_line(i) for i in opens]
        L += ["  - next: " + n for n in nxt]
    for sid, fb in sorted(dd.get("fallback", {}).items()):
        if fb.get("ticket") == t:
            L += fallback_lines(fb)
    L.append("")
    return L


def other_section(dd, g, slot):
    L = ["### %s" % g]
    if slot.get("minutes"):
        L.append("_%s_" % fmt_time(slot["minutes"]))
    if slot.get("what_i_did"):
        L.append("- **What I did**")
        L += ["  - %s — %s" % (w.get("time") or "--:--", w.get("text")) for w in sorted(slot["what_i_did"], key=lambda w: w.get("time", ""))]
    items = sort_items(slot.get("items", {}).values(), False)
    if items:
        L.append("- **Findings**")
        L += ["  - " + item_line(i) for i in items]
    if slot.get("tables"):
        L.append("- **DB** — tables: " + ", ".join(code(t["name"]) for t in slot["tables"].values()))
    for q in slot.get("queries", {}).values():
        lang = DIALECT.get(q.get("dialect"), ("SQL", "sql"))
        L += ["  - query (%s · %s): %s" % (lang[0], q.get("kind"), q.get("purpose")), "", "    ```" + lang[1]] + \
             ["    " + x for x in fence_safe(q.get("text")).splitlines()] + ["    ```", ""]
    if slot.get("apis") or slot.get("integrations"):
        L.append("- **APIs / integrations**")
        L += ["  - %s %s — %s" % (a.get("method"), code(a.get("path")), a.get("purpose")) for a in slot.get("apis", {}).values()]
        L += ["  - %s · %s — %s" % (i.get("system"), i.get("artifact"), i.get("notes")) for i in slot.get("integrations", {}).values()]
    if slot.get("files_changed"):
        L.append("- **Files changed**")
        L += ["  - " + code(f) for f in slot["files_changed"][:25]]
    for sid, fb in sorted(dd.get("fallback", {}).items()):
        if not fb.get("ticket") and fb.get("group") == g:
            L += fallback_lines(fb)
    L.append("")
    return L


def fallback_lines(fb):
    L = ["- _AI summary pending_ (session %s, %s)" % (fb.get("session", "")[:8], fb.get("reason", "extraction not done yet"))]
    if fb.get("files"):
        L.append("  - files edited: " + ", ".join(code(f) for f in fb["files"][:15]))
    if fb.get("commands"):
        L.append("  - commands: " + "; ".join(code(c) for c in fb["commands"][:10]))
    for s in fb.get("sql", [])[:5]:
        L.append("  - SQL seen: " + code(s))
    return L


def draft_standup(store, dd):
    y, today, blockers = [], [], []
    for t, slot in dd.get("tickets", {}).items():
        tk = store.ticket(t)
        last = sorted(slot.get("what_i_did", []), key=lambda w: w.get("time", ""))
        y.append("%s: %s" % (t, last[-1]["text"] if last else (tk.get("title") or "worked on it")))
        for i in slot.get("item_ids", []):
            it = tk["items"].get(i) or {}
            if it.get("kind") == "todo":
                today.append("%s: %s" % (t, it["title"]))
            elif it.get("kind") == "blocker":
                blockers.append("%s: %s" % (t, it["title"]))
    return {"yesterday": y[:8], "today": today[:8], "blockers": blockers[:5]}


def health_footer(dd, health):
    sessions = dd.get("sessions", {})
    extracted = len([s for s in sessions.values() if s.get("extracted")])
    lp = common.parse_ts(health.get("last_push"))
    st = dd.get("stats", {})
    bits = ["%d sessions" % len(sessions), "%d extracted" % extracted,
            "last push %s" % (lp.strftime("%H:%M") if lp else "never"),
            "est. usage $%.2f" % (st.get("cost_usd") or 0), "%d errors" % (st.get("errors") or 0)]
    line = "Recorder health: " + " · ".join(bits)
    if health.get("privacy") == "public":
        line += "  \n**⚠ Pushing is blocked: the GitHub repo is PUBLIC. Make it private; commits continue locally.**"
    for w in health.get("warnings") or []:
        line += "  \n⚠ " + w
    return line


# --------------------------------------------------------------------------- ticket files

def total_minutes(tk):
    return sum((v or {}).get("minutes") or 0 for v in tk.get("timeline", {}).values())


def render_ticket(store, t):
    tk = store.ticket(t)
    items = list(tk["items"].values())
    by = lambda *kinds: sort_items([i for i in items if i.get("kind") in kinds])
    L = ["# %s — %s" % (t, tk.get("title") or "(untitled)")]
    ql = quick_links(t, tk)
    if ql:
        L += [ql, ""]
    L += ["| Status (inferred) | Repos | First worked | Last worked | ≈ Total time |", "|---|---|---|---|---|",
         "| %s | %s | %s | %s | %s |" % (tk.get("status") or "—", cell(", ".join(tk.get("repos", []))), tk.get("first") or "—",
                                       tk.get("last") or "—", fmt_time(total_minutes(tk))), ""]
    sec = lambda title, lines: L.extend(["## " + title] + (lines or ["_Nothing recorded yet._"]) + [""])
    L += hub_sections(t, tk)

    sec("TL;DR", [tk["tldr"] + ("  \n_(as of %s)_" % tk["tldr_date"] if tk.get("tldr_date") else "")] if tk.get("tldr") else None)
    sec("Problem / context", [tk["problem"]] if tk.get("problem") else None)
    sec("Root cause & key findings", ["- " + item_line(i, True) for i in by("root_cause", "finding", "note")])
    sec("How it works", ["- " + item_line(i, True) for i in by("how_it_works")])
    fix = ["- " + item_line(i, True) for i in by("fix")]
    changed = [f for f in tk["files"].values() if f.get("changed")]
    if changed:
        fix += ["", "Files changed:"] + ["- %s%s" % (code(f["path"]), " — " + f["role"] if f.get("role") else "") for f in sorted(changed, key=lambda f: f["path"])]
    if tk.get("commits"):
        fix += ["", "Commits:"] + ["- %s %s %s %s _(%s)_" % (c.get("date", "")[:10], c.get("repo_name"), code(c.get("short")), c.get("subject"), c.get("ref"))
                                   for c in sorted(tk["commits"].values(), key=lambda c: c.get("date", ""), reverse=True)]
    sec("Fix / changes", fix if any(x.strip() for x in fix) else None)
    gs = git_section(tk)
    if gs:
        sec("Branches & PRs", gs + ["", "_All branches: [knowledge/branches.md](../../knowledge/branches.md)_"])
    else:
        br = ticket_branches(store, t)
        sec("Branches", branch_table(br, with_repo=True, ticket_link=None) +
            ["", "_All branches: [knowledge/branches.md](../../knowledge/branches.md)_"] if br else None)
    db = []
    if tk["tables"]:
        db += ["| Table / collection | DB | Purpose | Key columns | Relations |", "|---|---|---|---|---|"]
        db += ["| %s | %s | %s | %s | %s |" % (code(v["name"]), cell(v.get("db")), cell(v.get("purpose")),
                                              cell(", ".join(v.get("key_columns", []))), cell("; ".join(v.get("relations", []))))
               for _, v in sorted(tk["tables"].items())]
    if tk["queries"]:
        db += ["", "%d quer%s — see [queries.md](queries.md)." % (len(tk["queries"]), "y" if len(tk["queries"]) == 1 else "ies")]
    sec("Database", db)
    api = []
    if tk["apis"]:
        api += ["| Method | Endpoint | Service/host | Direction | Purpose | Request fields | Response fields | Auth |", "|---|---|---|---|---|---|---|---|"]
        api += ["| %s | %s | %s | %s | %s | %s | %s | %s |" % (a.get("method"), code(a.get("path")), cell(a.get("host_or_service")),
                                                             cell(a.get("direction")), cell(a.get("purpose")), cell(", ".join(a.get("request_fields", []))),
                                                             cell(", ".join(a.get("response_fields", []))), cell(a.get("auth_type")))
                for _, a in sorted(tk["apis"].items())]
    sec("APIs", api)
    sec("Integrations", ["- **%s** · %s — %s" % (g.get("system"), g.get("artifact"), g.get("notes")) for _, g in sorted(tk["integrations"].items())])
    sec("Config & environment", ["- " + item_line(i, True) for i in by("config")])
    sec("Errors seen", ["- " + item_line(i, True) for i in by("error")])
    rep = ["- " + item_line(i, True) for i in by("repro_test")]
    rep += ["- Bruno · %s %s — %s _(%s, last %s)_" % (b.get("method"), code(b.get("url")), b.get("name"), b.get("collection"), b.get("last"))
            for _, b in sorted(tk.get("bruno", {}).items())]
    sec("How to reproduce / test", rep)
    sec("Decisions", ["- " + item_line(i, True) for i in by("decision")])
    sec("Open questions · TODO · blockers", ["- " + item_line(i, True) for i in by("question", "todo", "blocker")])
    sec("Code map", ["- %s — %s" % (code(f["path"]), f.get("role") or ("changed" if f.get("changed") else "")) for f in sorted(tk["files"].values(), key=lambda f: f["path"])])
    sec("References consulted", ["- " + item_line(i, True) for i in by("reference")])
    tl = ["- [%s](../../%s) — %s · %s" % (d, daily_rel(d), fmt_time((v or {}).get("minutes")), (v or {}).get("line") or "")
          for d, v in sorted(tk.get("timeline", {}).items(), reverse=True)]
    sec("Timeline", tl)
    L += ["## My notes", NOTES_BLOCK, ""]
    return "\n".join(L)


def quick_links(t, tk):
    h = common.home()
    have = lambda *p: os.path.exists(os.path.join(h, *p))
    links = []
    if (tk.get("replay") or {}).get("final") or have("tickets", t, "requests.md"):
        links.append("[Requests](requests.md)")
    if have("bruno", t):
        links.append("[Bruno folder](../../bruno/%s)" % t)
    if have("tickets", t, "replay.sh"):
        links.append("[Replay](replay.sh) `bash tickets/%s/replay.sh qa`" % t)
    if tk.get("data_flow"):
        links.append("[DB flow](db-flow.md)")
    if tk.get("queries"):
        links.append("[Queries](queries.md)")
    if tk.get("artifacts"):
        links.append("[Artifacts](artifacts/INDEX.md)")
    return " · ".join(links)


_STOP = {"the", "a", "an", "of", "to", "for", "with", "on", "in", "and", "or", "is", "shows", "user", "users", "request", "send", "sends"}


def _words(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2 and w not in _STOP}


def scenario_rows(tk):
    """Scenario | Trigger request | DB effect | Verify — ties requests, DB flow and queries together."""
    from . import dbflow
    final = (tk.get("replay") or {}).get("final") or []
    flows = dbflow.entries(tk)
    names = []
    for s in final:
        if s.get("scenario") and s["scenario"] not in names:
            names.append(s["scenario"])
    view_names = [sc.get("name") for sc in (tk.get("dbflow_view") or {}).get("scenarios") or [] if sc.get("name")]
    for n in view_names or [e.get("scenario") for e in flows]:
        if n and n not in names:
            names.append(n)
    names = names[:6]
    if not names and final:
        names = [""]
    triggers = [s for s in final if not s.get("repeat") and not re.search(r"(?i)login|token|auth", s.get("name") or "")]
    # unlabelled steps (e.g. your own folder.bru order) go to the one scenario they share the most words with
    home_of = {}
    for s in triggers:
        if not s.get("scenario") and names:
            best = max(names, key=lambda n: len(_words(n) & _words(s.get("name"))))
            if len(_words(best) & _words(s.get("name"))) >= 2:
                home_of[best] = home_of.get(best) or s
    rows = []
    for n in names:
        steps = [s for s in final if (s.get("scenario") or "") == n] or (final if len(names) == 1 else [])
        trig = next((s for s in steps if s in triggers), steps[0] if steps else None)
        trig = trig or home_of.get(n)
        es = [e for e in flows if e.get("scenario") == n] or (flows if len(names) == 1 else [])
        changes = [(c["column"], c["change"]) for e in es for c in e.get("columns_written") or [] if c.get("change")]
        changes.sort(key=lambda x: 0 if re.search(r"->|→", x[1]) else 1)
        eff = list(dict.fromkeys("`%s`: %s" % c for c in changes))
        ver, seen = [], set()
        for e in es:
            for q in dbflow.verify_links(tk, e)[:1]:
                if q["id"] in seen:
                    continue
                seen.add(q["id"])
                label = 'queries.md → "%s"' % (q.get("purpose") or "query")[:60]
                ver.append("[%s](queries.md#q-%s)" % (label.replace("]", ")"), q["id"]))
        rows.append("| %s | %s | %s | %s |" % (cell(n or "main"), cell("%d %s" % (trig["n"], trig["name"])) if trig else "—",
                                              cell("; ".join(eff[:3])) or "—", ", ".join(ver[:2]) or "—"))
    return rows


def hub_sections(t, tk):
    from . import artifacts as artmod
    L = []
    rows = scenario_rows(tk)
    if rows:
        L += ["## Scenarios", "", "| Scenario | Trigger request | DB effect | Verify |", "|---|---|---|---|"] + rows + [""]
    final = (tk.get("replay") or {}).get("final") or []
    if final:
        L += ["## Replay requests", "",
              "Run: `bash tickets/%s/replay.sh qa` · Bruno: folder `%s` in `Worklog/bruno` · curl: [requests.md](requests.md)" % (t, t), "",
              "| # | Request | Expected |", "|---|---|---|"]
        L += ["| %d | %s%s | %s |" % (s["n"], cell(s["name"]), " _(again)_" if s.get("repeat") else "", cell(s.get("expected")) or "2xx") for s in final]
        L.append("")
    arts = artmod.latest(tk, 5)
    if arts:
        L += ["## Latest artifacts", ""]
        for a in arts:
            target = a["source"] if a.get("kind") == "link" else ("artifacts/" + a["stored"] if a.get("stored") and not a["stored"].startswith("../")
                                                                  else (a["stored"][3:] if a.get("stored") else "artifacts/INDEX.md"))
            L.append("- %s [%s](%s) — %s _(%s)_" % (a.get("date", ""), cell(a.get("name")), target.replace(" ", "%20"), cell(a.get("purpose")) or a.get("kind", ""),
                                                   a.get("storage", "")))
        L += ["", "_All: [artifacts/INDEX.md](artifacts/INDEX.md)_", ""]
    return L


def render_queries(store, t):
    tk = store.ticket(t)
    L = ["# %s — Queries" % t, ""]
    qs = sorted(tk["queries"].values(), key=lambda q: (q.get("date", ""), q.get("time", "")), reverse=True)
    if not qs:
        L.append("_No queries recorded yet._")
    for q in qs:
        lang = DIALECT.get(q.get("dialect"), ("SQL", "sql"))
        L += ['<a id="q-%s"></a>' % q["id"],
              "## %s %s · %s · %s — %s" % (q.get("date"), q.get("time") or "", lang[0], q.get("kind"), q.get("purpose") or "query")]
        if q.get("tables"):
            L.append("Tables: " + ", ".join(code(x) for x in q["tables"]))
        if q.get("revealed"):
            L.append("Revealed: " + q["revealed"])
        L += ["```" + lang[1], fence_safe(q.get("text")).rstrip(), "```", ""]
    return "\n".join(L) + "\n"


def render_index(store):
    L = ["# Tickets", "", "| Ticket | Title | Status | First | Last | ≈ Time | TL;DR |", "|---|---|---|---|---|---|---|"]
    rows = []
    for t in Store.all_ticket_ids():
        tk = store.ticket(t)
        rows.append((tk.get("last") or "", t, tk))
    for _, t, tk in sorted(rows, reverse=True):
        L.append("| [%s](%s/README.md) | %s | %s | %s | %s | %s | %s |" % (
            t, t, cell(tk.get("title")), tk.get("status") or "—", tk.get("first") or "—", tk.get("last") or "—",
            fmt_time(total_minutes(tk)), cell(common.truncate(tk.get("tldr"), 220))))
    if not rows:
        L.append("| — | _No tickets yet_ | | | | | |")
    return "\n".join(L) + "\n"


def _tlinks(ts):
    return ", ".join("[%s](../tickets/%s/README.md)" % (t, t) for t in ts or [])


def render_catalogs(store):
    cat = store.catalog()
    tb = ["# Database tables & collections", "", "| Table/collection | DB | Purpose | Key columns | Tickets | Last seen |", "|---|---|---|---|---|---|"]
    tb += ["| %s | %s | %s | %s | %s | %s |" % (code(v["name"]), cell(v.get("db")), cell(v.get("purpose")), cell(", ".join(v.get("key_columns", []))),
                                              _tlinks(v.get("tickets")), v.get("last_seen", "")) for _, v in sorted(cat["tables"].items())]
    ap = ["# APIs", "", "| Method | Endpoint | Service/host | Direction | Purpose | Tickets | Replay in | Last seen |", "|---|---|---|---|---|---|---|---|"]
    replays = replay_index(store)
    ap += ["| %s | %s | %s | %s | %s | %s | %s | %s |" % (v.get("method"), code(v.get("path")), cell(v.get("host_or_service")), cell(v.get("direction")),
                                                        cell(v.get("purpose")), _tlinks(v.get("tickets")), replay_in(replays, v), v.get("last_seen", ""))
           for _, v in sorted(cat["apis"].items())]
    ig = ["# Integrations", "", "| System | Artifact | Notes | Tickets |", "|---|---|---|---|"]
    ig += ["| %s | %s | %s | %s |" % (cell(v.get("system")), cell(v.get("artifact")), cell(v.get("notes")), _tlinks(v.get("tickets")))
           for _, v in sorted(cat["integrations"].items())]
    return {"db-tables.md": "\n".join(tb) + "\n", "apis.md": "\n".join(ap) + "\n", "integrations.md": "\n".join(ig) + "\n"}


def replay_index(store):
    """[(method, path shape, ticket)] for every saved replay step."""
    from . import apicalls
    out = []
    for t in Store.all_ticket_ids():
        for s in (store.ticket(t).get("replay") or {}).get("final") or []:
            out.append((s.get("method"), apicalls.path_shape(s.get("url")), t))
    return out


def replay_in(index, api):
    from . import apicalls
    shape = apicalls.path_shape(api.get("path"))
    ts = sorted({t for m, p, t in index if shape and m == api.get("method") and (p == shape or p.endswith(shape) or shape.endswith(p) and len(p) > 6)})
    return ", ".join("[%s](../tickets/%s/requests.md)" % (t, t) for t in ts)


def render_context(store, t):
    """SessionStart context: plain facts, <= 4000 chars."""
    tk = store.ticket(t)
    path = os.path.join(common.home(), "tickets", t, "README.md")
    al = alert_line(t, tk)
    L = ([al] if al else []) + ["Worklog facts recorded automatically for %s%s (full file: %s)." % (t, " — " + tk["title"] if tk.get("title") else "", path),
         "Status (inferred): %s. First worked %s, last worked %s, about %s in total." % (
             tk.get("status") or "unknown", tk.get("first"), tk.get("last"), common.fmt_minutes(total_minutes(tk)))
         if tk.get("first") else "No Claude Code activity recorded for it yet."]
    if tk.get("tldr"):
        L.append("TL;DR: " + tk["tldr"])
    final = (tk.get("replay") or {}).get("final") or []
    if final:
        L.append("Saved requests (bash tickets/%s/replay.sh qa; Bruno folder bruno/%s): %s." % (
            t, t, "; ".join("%d %s%s" % (s["n"], s["name"], " (again)" if s.get("repeat") else "") for s in final[:8])))
    from . import dbflow
    dl = dbflow.summary_line(tk)
    if dl:
        L.append(common.truncate(dl, 500))
    from . import artifacts as artmod
    arts = artmod.latest(tk, 3)
    if arts:
        L.append("Latest artifacts: " + "; ".join("%s (%s)" % (a.get("name"), common.truncate(a.get("purpose") or a.get("kind"), 60)) for a in arts) + ".")
    g = tk.get("git") or {}
    if g.get("summary"):
        L.append("Branches & PRs: " + g["summary"] + ".")
    for name, x in sorted((g.get("repos") or {}).items()):
        for b in x.get("branches", [])[:6]:
            if b.get("status") == "abandoned":
                continue
            L.append("- %s (%s): from %s, %s%s" % (b["branch"], name, b.get("parent") or "?", b["status"],
                                                 "; PR " + ", ".join("#%s" % n for n in b["prs"]) if b.get("prs") else ""))
    finds = sort_items([i for i in tk["items"].values() if i.get("kind") not in OPEN_KINDS])[:8]
    if finds:
        L.append("Recent findings:")
        for i in finds:
            ev = "; ".join((i.get("evidence") or [])[:2])
            L.append("- [%s] %s%s%s" % (TAG.get(i["kind"], i["kind"]), i["title"],
                                        " — " + common.truncate(i.get("detail"), 220) if i.get("detail") else "", " (" + ev + ")" if ev else ""))
    opens = sort_items([i for i in tk["items"].values() if i.get("kind") in OPEN_KINDS])[:6]
    if opens:
        L.append("Open TODOs / questions:")
        L += ["- %s" % i["title"] for i in opens]
    txt = "\n".join(L)
    return redact(common.truncate(txt, 4000))


def example_ticket(cfg):
    """Ticket key used in generated examples (config render.example_ticket)."""
    return (cfg.get("render") or {}).get("example_ticket") or "ABC-1234"


def render_readme(cfg):
    return README_TEMPLATE.format(
        home=common.home(), day=cfg.get("day_starts_at"), ex=example_ticket(cfg),
        models="extract: %s, consolidate: %s" % ((cfg.get("models") or {}).get("extract"), (cfg.get("models") or {}).get("consolidate")),
        prefixes=", ".join(cfg.get("ticket_prefixes") or []) or "(learned from branch names)",
        push=cfg.get("push_interval_minutes"), retention=cfg.get("raw_retention_days"),
        python=common.python_exe(cfg), launcher=common.launcher())


PUBLIC_README = """# Worklog

My daily engineering logs: one Markdown file per work day under `daily/<year>/<month>/`, with what I did per
ticket, commits, the Jira worklog I logged and the standup. Written automatically.

Everything else (ticket notes, queries, requests, DB flows, artifacts) is a local knowledge base on my machine and is
not in this repository; links into it from the daily files only work there.
"""

README_TEMPLATE = """# Worklog

_Generated by Chitragupta — do not edit (regenerated on every render)._

An automatic record of my engineering work: what I did each day per ticket, what I learned
(root causes, how things work, queries, tables, APIs, integrations), and a living file per ticket.

## Where things are
| Path | What |
|---|---|
| `daily/YYYY/MM/YYYY-MM-DD.md` | Daily file: summary, per-ticket work, other work, outside Claude Code, commits, standup |
| `tickets/INDEX.md` | All tickets: status, dates, ≈ time, TL;DR |
| `tickets/<TICKET>/README.md` | Everything ever learned about one ticket |
| `tickets/<TICKET>/queries.md` | Every important query for that ticket, dated |
| `tickets/<TICKET>/requests.md` · `replay.sh` · `payloads/` | The ticket's minimal replay set as curl, and a runnable script |
| `tickets/<TICKET>/db-flow.md` | How data flows across tables for the ticket: flow, sequence, column changes, relationships, verify queries |
| `tickets/<TICKET>/artifacts/` | Every artifact made for the ticket + `INDEX.md`; `local/` (git-ignored) holds binaries and originals |
| `bruno/` | ONE Bruno collection for all tickets: `bruno/<TICKET>/NN name.bru`, `environments/`, `.env` (git-ignored) |
| `knowledge/db-map.md` | Every table seen across tickets, relationships, which components write which columns |
| `_drop/` | Drop any file here with a ticket ID in its name; it is moved into that ticket's artifacts (git-ignored) |
| `knowledge/db-tables.md`, `apis.md`, `integrations.md` | Cross-ticket catalogs |
| `knowledge/branches.md` | Every work branch: parent, created, last commit, unpushed, where it lives |
| `data/` | Canonical JSON the markdown is rendered from (never hand-edit) |
| `inbox.md` | Manual notes, filed automatically (git-ignored). Prefix with `{ex}:` to attach to a ticket |
| `.worklog/` | Config, optional prompt overrides, health; raw capture/logs/state are git-ignored |

Write your own notes **only** between `<!-- my-notes:start -->` and `<!-- my-notes:end -->` in any
daily or ticket file — that block survives every re-render.

## Commands (in any Claude Code session)
| Command | What |
|---|---|
| `/cg:today` | Time per ticket so far today and a preview of tonight's Jira worklog |
| `/cg:ticket <TICKET>` | Where a ticket's notes, requests, DB flow and artifacts are, with a short summary |
| `/cg:timesheet` | Hours per ticket by week or month (leave and holidays included) |
| `/cg:trace <TICKET>` | The ticket's DB flow and, when a database MCP is set up, a worked QA example |
| `/cg:log <line>` | Add an inbox line: `time:`, `leave:`, `half-day:`, `holiday:`, `workday:`, `resend:` |
| `/cg:pause` | Pause or resume recording |
| `/cg:dashboard` | Open the local read-only dashboard |
| `/cg:setup` · `/cg:doctor` | Set up or repair; check that everything is running |

You can also just ask (e.g. "what did I find on {ex}?"): the `chitragupta` skill answers from this folder.

## How it works
1. **Capture** — the Chitragupta plugin's Claude Code hooks (SessionStart, UserPromptSubmit, PostToolUse, Stop,
   SessionEnd) append one redacted line per event to `.worklog/raw/<work-day>/<session>.jsonl`.
   Hooks are async (except SessionStart), never call Claude, never print except SessionStart context, and always
   exit 0. Tool *outputs* are never recorded.
2. **Process** — the hidden scheduled task `Chitragupta` runs `agent tick` through the stable runtime
   (`{launcher}`) at logon and every 5 minutes:
   ingest raw events (plus a safety-net scan of Claude Code transcripts), extract knowledge with a headless,
   tool-less `claude -p` call (models: {models}), pick up my git commits, Bruno requests, SQL scripts and
   `inbox.md`, merge into `data/`, render markdown, redact-check, commit, and push every {push} minutes
   (or when a session ends / a day closes).
   Branches, commits and PRs per ticket come from git and the Git host's CLI, no AI: parent, PRs, and
   whether every commit reached the targets of the repo's branching strategy (`"branching"` in config).
3. **Close the day** — after {day} the previous work day is consolidated (final summary, per-ticket TL;DR and
   status, standup) and open TODOs carry into the next day. Work before {day} counts toward the previous day.

**Latency:** a session is extracted ~10 min after it goes idle or ends (or every 20 min while active, once it
has ~1,500 new characters). Until then the daily file shows commands/files marked _AI summary pending_.

Ticket prefixes: {prefixes}. New prefixes seen in branch names are added automatically.

## Replay a ticket's requests
- **Once:** open the collection `{home}/bruno` in Bruno (Open Collection). New ticket folders appear by themselves.
- **Once:** fill `bruno/.env` (git-ignored) with the passwords / API keys named in `bruno/.env.example`. Person fields
  get obviously synthetic defaults; change one only when a step needs an existing record. Your values are never changed.
- **Run:** `bash tickets/<TICKET>/replay.sh qa` (or `local`) from this folder — every step with status vs expected, stops
  at the first failure. Or run the folder in Bruno, or `bru run <TICKET> --env QA` inside `bruno/` if the Bruno CLI is installed.
- Only the requests needed to reproduce/verify are kept (auth → setup → trigger → verify). Failed attempts, typos,
  duplicates, health checks and look-arounds are left out. Scenario-critical fields (e.g. `"role": null`) stay exact.
- **Your edits win:** edit a generated `.bru` in Bruno and it is yours (never overwritten; requests.md / replay.sh follow
  your version). Delete one and it is never recreated. Add one to a ticket folder and it is adopted.
- **Save on purpose:** in any session say "save the login and the order-update call for {ex} to the worklog";
  or add an inbox line `{ex}: pin curl ...`.

## DB flow
- `tickets/<TICKET>/db-flow.md` is built from code Claude read or changed, queries that ran, and Claude's explanations.
- For a deep trace ask in a session: "map the DB flow for {ex}" — the `chitragupta` skill traces entry point → services →
  repositories/SQL → tables/columns and the result is filed exactly as traced.

## Artifacts
- Docs, HTML pages, diagrams, payloads, queries, requests, investigation scripts, exports, screenshots and published links
  made during a ticket's work are filed under `tickets/<TICKET>/artifacts/` automatically (text redacted; CSV/XLSX as header
  + row count; binaries local-only).
- `_drop/`: drop any file with the ticket ID in its name (artifacts made in claude.ai: download them there,
  e.g. `{ex}_lock-analysis.html`). Without an ID it goes to the ticket active in the last 2 hours, else it is
  listed as unfiled in the daily file.

## Controls
- **Pause everything:** `/cg:pause`, or create an empty file `.worklog/PAUSED`. Delete it to resume.
- **Exclude a repo:** create an empty `.worklog-ignore` in that repo's root.
- **Add a note:** append a line to `inbox.md` (e.g. `{ex}: QA confirmed the fix on UAT`).
- **Health:** `/cg:doctor`; footer of each daily file; `.worklog/health.json`; logs in `.worklog/logs/`.
- **Force a run:** `"{python}" "{launcher}" agent tick --force`
- **Uninstall:** `"{python}" "{launcher}" uninstall` — removes the scheduled task, the runtime, the `chitragupta`
  skill and the CLAUDE.md block; keeps all data. Then remove the plugin with `/plugin`.

## Privacy rules
- The GitHub repo must be **private**. Before the first push and daily, the agent asks the GitHub API anonymously;
  if the repo is visible (HTTP 200) pushing stops and a warning appears here and in SessionStart context.
- Redaction runs at capture, before extraction, before rendering, and as a pre-commit hook. It masks secrets
  (passwords in URLs/connection strings, Authorization headers, JWTs, AWS/GitHub/Slack/Azure keys, private keys,
  secret assignments) and patient data (HL7 field values, SSNs, emails, phones, DOBs, MRN/member/accession values,
  person fields in SQL/JSON). Masks keep shape: digits → `X`, letters → `x`.
- Query results, API response bodies, log payloads and raw HL7/FHIR messages are never stored.
- Raw capture is kept {retention} days after it has been processed and pushed, then pruned.

## Troubleshooting
- Nothing new in the daily file → check `.worklog/logs/agent.log` and `.worklog/health.json`.
- Hook problems → `.worklog/logs/hook-errors.log` (hooks never block Claude Code).
- Push failing → run `git push` once in this folder to refresh Git Credential Manager.
- Scheduled task → `schtasks /query /tn Chitragupta /v`; re-register it with `/cg:setup`.
- Slack or Jira not connected → everything still works: the evening worklog is saved in the daily file
  (`## Jira worklog`) and `data/timesheets/`, ticket titles come from branch names. Connect them with `/cg:setup`.
"""


# --------------------------------------------------------------------------- orchestrate

def render_all(store, cfg, health, days=None, tickets=None, everything=False):
    """Render the given (or all) views. Returns list of changed paths."""
    changed = []
    h = common.home()
    days = Store.all_days() if everything else sorted(days or [])
    tickets = Store.all_ticket_ids() if everything else sorted(tickets or [])
    for d in days:
        p = os.path.join(h, *daily_rel(d).split("/"))
        if write_view(p, render_day(store, d, cfg, health), mode="public"):
            changed.append(p)
    for t in tickets:
        p = os.path.join(h, "tickets", t, "README.md")
        if write_view(p, render_ticket(store, t)):
            changed.append(p)
        q = os.path.join(h, "tickets", t, "queries.md")
        if common.write_text(q, render_queries(store, t)):
            changed.append(q)
        tk = store.ticket(t)
        dfp = os.path.join(h, "tickets", t, "db-flow.md")
        if tk.get("data_flow") or os.path.exists(dfp):
            from . import dbflow
            notes = extract_notes(dfp)
            from . import kbjobs
            tail = redact(kbjobs.dbflow_qa_section(t, cfg), mode="secrets")
            if write_view(dfp, dbflow.render_ticket(t, tk, NOTES_BLOCK, cfg), tail=tail) or notes != NOTES_BLOCK:
                changed.append(dfp)
        if tk.get("artifacts"):
            from . import artifacts as artmod
            ip = os.path.join(h, "tickets", t, "artifacts", "INDEX.md")
            if common.write_text(ip, redact(artmod.render_index(t, tk))):
                changed.append(ip)
        common.write_text(common.wl("cache", "context", t + ".md"), render_context(store, t))
    if everything or tickets or days:
        if common.write_text(os.path.join(h, "tickets", "INDEX.md"), render_index(store)):
            changed.append("tickets/INDEX.md")
        for name, body in render_catalogs(store).items():
            if common.write_text(os.path.join(h, "knowledge", name), body):
                changed.append("knowledge/" + name)
        from . import dbflow
        from . import kbjobs
        dbm = redact(dbflow.render_db_map(store)) + redact(kbjobs.dbmap_section(), mode="secrets")
        if common.write_text(os.path.join(h, "knowledge", "db-map.md"), dbm):
            changed.append("knowledge/db-map.md")
    if everything or store.branches_dirty:
        if common.write_text(os.path.join(h, "knowledge", "branches.md"), render_branches(store)):
            changed.append("knowledge/branches.md")
    if common.write_text(os.path.join(h, "README.md"), PUBLIC_README):
        changed.append("README.md")
    common.write_text(common.wl("README.md"), render_readme(cfg))
    return changed
