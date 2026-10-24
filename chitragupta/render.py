"""Render markdown views from data/. Deterministic: same data + same health -> same bytes.
Text between <!-- my-notes:start --> and <!-- my-notes:end --> is preserved across renders."""
import datetime as _dt
import os
import re

from . import common
from . import supersede
from .redact import redact
from .store import Store, display_title, jira_status, lookup

NOTES_START, NOTES_END = "<!-- my-notes:start -->", "<!-- my-notes:end -->"
NOTES_BLOCK = NOTES_START + "\n" + NOTES_END
TAG = {"finding": "finding", "root_cause": "root cause", "how_it_works": "how it works", "fix": "fix",
       "decision": "decision", "config": "config", "error": "error", "repro_test": "repro", "reference": "reference",
       "todo": "todo", "question": "question", "blocker": "blocker", "note": "note", "risk": "risk"}
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
    conf = "" if it.get("status") == "confirmed" else it.get("confidence") or ""
    if it.get("status") == "hypothesis":
        conf = "hypothesis"
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


def hub_item(it):
    return "- " + item_anchor(it) + item_line(it, True)


def item_block(items, kinds):
    """A hub section's items (K3): the current ones; done TODOs and items a newer one replaced follow, collapsed and
    labelled. Archived items and items waiting for review (K8) are left out here."""
    byid = {i.get("id"): i for i in items}
    sel = [i for i in items if i.get("kind") in kinds and not i.get("archived") and not i.get("review")]
    cur = [i for i in sel if i.get("status") not in ("superseded", "done")]
    done = [i for i in sel if i.get("status") == "done"]
    sup = [i for i in sel if i.get("status") == "superseded"]
    L = [hub_item(i) for i in sort_items(cur)]
    if done:
        L += ["", "<details><summary>Done (%d)</summary>" % len(done), ""]
        L += [hub_item(i) + (" _(done: %s)_" % i["closed_by"] if i.get("closed_by") else "") for i in sort_items(done)]
        L += ["", "</details>"]
    if sup:
        L += ["", "<details><summary>Superseded (%d)</summary>" % len(sup), ""]
        for i in sort_items(sup):
            rep = byid.get(i.get("superseded_by")) or {}
            by = "[%s](#i-%s)" % ((rep.get("title") or "").replace("]", ")").replace("[", "("), rep["id"]) if rep                 else "a newer item"
            L.append(hub_item(i) + " — replaced by " + by)
        L += ["", "</details>"]
    return L


def needs_review(t, items):
    """Items flagged as maybe another ticket's (K8), collapsed, each with the inbox lines that resolve it."""
    flagged = sort_items([i for i in items if i.get("review") and not i.get("archived")])
    if not flagged:
        return []
    L = ["<details><summary>Needs review (%d)</summary>" % len(flagged), "",
         "_Recorded under %s but maybe not about it. Add a line to inbox.md: `%s: keep <id>` keeps it here, "
         "`%s: move <id> <KEY>` copies it to that ticket and archives it here._" % (t, t, t), ""]
    for i in flagged:
        r = i["review"]
        to = r.get("suggest") or "<KEY>"
        L.append("%s — _%s_ · `%s: keep %s` · `%s: move %s %s`" % (hub_item(i), r.get("why") or "", t, i["id"], t,
                                                                  i["id"], to))
    return L + ["", "</details>"]


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
             "az": "PRs from az", "mcp:github": "PRs from the GitHub MCP",
             "mcp:azure-devops": "PRs from the Azure DevOps MCP"}


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
        for x in dd["carried_todos"]:
            x = x if isinstance(x, dict) else {"text": str(x)}
            L.append("- [x] %s _(done: %s)_" % (x.get("text"), x["closed_by"]) if x.get("closed_by") else "- [ ] " + (x.get("text") or ""))
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
                t, t, title_cell(tk), cell(", ".join(slot.get("where", []))), fmt_time(slot.get("minutes")),
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


def heading(t, tk, level="#"):
    """`# KEY — Jira title`, or just `# KEY` while the ticket has no card (a hint is never shown as the title)."""
    title = display_title(tk)
    return "%s %s — %s" % (level, t, title) if title else "%s %s" % (level, t)


def title_cell(tk):
    title = display_title(tk)
    return cell(title) if title else ("_hint: %s_" % cell(tk["title_hint"]) if tk.get("title_hint") else "")


def status_text(tk):
    """'Jira: QA COMPLETED · Inferred: in_progress' (the parts that are known)."""
    parts = [("Jira", jira_status(tk)), ("Inferred", tk.get("status_inferred") or "")]
    return " · ".join("%s: %s" % (k, v) for k, v in parts if v) or "—"


def fmt_time(m):
    return ("≈ " + common.fmt_minutes(m)) if m else "—"


def ticket_section(store, dd, t, slot):
    tk = store.ticket(t)
    L = [heading(t, tk, "###")]
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
    tables = [r["name"] for r in _found(tk["tables"], slot.get("table_keys"))]
    qs = [q for q in _found(tk["queries"], slot.get("query_ids")) if q.get("sent") is not False]  # never-sent SQL: data only
    if tables or qs:
        L.append("- **DB**" + (" — tables: " + ", ".join(code(x) for x in tables) if tables else ""))
        for q in qs:
            L.append("  - query: [%s](../../../tickets/%s/queries.md#q-%s) _(%s · %s)_" % (
                cell(q.get("purpose") or common.truncate(q.get("text"), 60)), t, q["id"],
                DIALECT.get(q.get("dialect"), ("SQL",))[0], q.get("kind")))
    apis = _found(tk["apis"], slot.get("api_keys"))
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


def _found(d, keys):
    """The records of a day slot's keys, each once (an old key resolves through the record's aliases)."""
    out = []
    for k in keys or []:
        r = lookup(d, k)
        if r is not None and not any(r is x for x in out):
            out.append(r)
    return out


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
        y.append("%s: %s" % (t, last[-1]["text"] if last else (display_title(tk) or "worked on it")))
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
    from . import gitsync
    pw = gitsync.privacy_warning(health)
    if pw and not any(pw in w for w in health.get("warnings") or []):
        line += "  \n⚠ " + pw
    for w in health.get("warnings") or []:
        line += "  \n⚠ " + w
    return line


# --------------------------------------------------------------------------- ticket files

def total_minutes(tk):
    return sum((v or {}).get("minutes") or 0 for v in tk.get("timeline", {}).values())


def render_ticket(store, t):
    tk = store.ticket(t)
    items = [i for i in tk["items"].values() if isinstance(i, dict)]
    by = lambda *kinds: item_block(items, kinds)
    L = [heading(t, tk)]
    ql = quick_links(t, tk)
    if ql:
        L += [ql, ""]
    if not display_title(tk) and tk.get("title_hint"):
        L += ["_hint: %s (no Jira card yet)_" % tk["title_hint"], ""]
    L += ["| Status | Repos | First worked | Last worked | ≈ Total time |", "|---|---|---|---|---|",
         "| %s | %s | %s | %s | %s |" % (status_text(tk), cell(", ".join(tk.get("repos", []))), tk.get("first") or "—",
                                       tk.get("last") or "—", fmt_time(total_minutes(tk))), ""]
    sec = lambda title, lines: L.extend(["## " + title] + (lines or ["_Nothing recorded yet._"]) + [""])
    st = delivery_stage(tk)
    if st:
        L += ["**%s:** %s" % (stage_label(st), st["text"]), ""]
    L += render_brief(brief_view(tk, t), tk["items"])
    L += hub_sections(t, tk)

    sec("TL;DR", [tldr_text(tk, st) + ("  \n_(as of %s)_" % tk["tldr_date"] if tk.get("tldr_date") else "")] if tk.get("tldr") else None)
    sec("Problem / context", [tk["problem"]] if tk.get("problem") else None)
    sec("Root cause & key findings", by("root_cause", "finding", "note"))
    sec("How it works", by("how_it_works"))
    fix = by("fix")
    changed = [f for f in tk["files"].values() if f.get("changed") and f.get("verified") is True]
    if changed:
        fix += ["", "%s changed — see the Code map below." % ("1 file" if len(changed) == 1 else "%d files" % len(changed))]
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
    nq = len(shown_queries(tk))
    if nq:
        db += ["", "%d quer%s — see [queries.md](queries.md)." % (nq, "y" if nq == 1 else "ies")]
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
    sec("Config & environment", by("config"))
    sec("Errors seen", by("error"))
    rep = by("repro_test")
    rep += ["- Bruno · %s %s — %s _(%s, last %s)_" % (b.get("method"), code(b.get("url")), b.get("name"), b.get("collection"), b.get("last"))
            for _, b in sorted(tk.get("bruno", {}).items())]
    sec("How to reproduce / test", rep)
    sec("Decisions", by("decision"))
    if by("risk"):
        sec("Risks", by("risk"))
    sec("Open questions · TODO · blockers", by("question", "todo", "blocker"))
    rv = needs_review(t, items)
    if rv:
        L += ["## Needs review"] + rv + [""]
    sec("Code map", code_map(tk))
    sec("References consulted", by("reference"))
    tl = ["- [%s](../../%s) — %s · %s" % (d, daily_rel(d), fmt_time((v or {}).get("minutes")), (v or {}).get("line") or "")
          for d, v in sorted(tk.get("timeline", {}).items(), reverse=True)]
    sec("Timeline", tl)
    L += ["## My notes", NOTES_BLOCK, ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- the brief (FX-20), from brief.view(tk)

BRIEF_ORDER = ("root_cause", "findings", "before", "after", "proposed", "fixes_made", "cons")
BRIEF_HEAD = {"root_cause": "Root cause", "findings": "Helpful findings", "before": "How it works today",
              "after": "How it works after the fix", "proposed": "Proposed fix", "fixes_made": "Fixes made",
              "cons": "Possible cons"}
CONFIDENCE = {"confirmed": "Confirmed", "likely": "Likely", "hypothesis": "Hypothesis", "none": "Not known"}
CLAIM_LABEL = {"recorded": "Recorded", "suggested": "Suggested", "ruled-out": "Ruled out"}
CITE_KINDS = ("commit", "pr", "jira", "query", "table", "replay", "test", "raw", "note")
NOT_YET = "Not recorded yet"


def brief_view(tk, t=None):
    from . import brief
    return brief.view(tk, t)


def brief_sections(v):
    """The view's sections in order, each a dict with "id" (the view may give a list or a dict by id)."""
    s = (v or {}).get("sections") or []
    if isinstance(s, dict):
        keys = [k for k in BRIEF_ORDER if s.get(k)] + [k for k in s if k not in BRIEF_ORDER]
        return [dict(s[k], id=k) for k in keys if isinstance(s[k], dict)]
    return [x for x in s if isinstance(x, dict) and x.get("id")]


def brief_heading(s):
    return s.get("heading") or BRIEF_HEAD.get(s["id"]) or s["id"].replace("_", " ").capitalize()


def brief_rows(s, key):
    """A section's claims, steps or list rows, each text once: a view that gives the same rows as steps and as claims
    (older views did, for the before/after sections) prints them once, as steps."""
    rows = [c for c in s.get(key) or [] if isinstance(c, dict)]
    if key == "claims" and s.get("steps"):
        seen = {" ".join(str(x.get("text") or "").split()) for x in s["steps"] if isinstance(x, dict)}
        rows = [c for c in rows if " ".join(str(c.get("text") or "").split()) not in seen]
    return rows


def brief_empty_text(s):
    """What an empty section says: the view's text ("Not recorded yet", "Not started", …), never its flag."""
    for k in ("text", "empty"):
        if isinstance(s.get(k), str) and s[k]:
            return s[k]
    return NOT_YET


def brief_empty(s):
    return not (s.get("claims") or s.get("steps") or s.get("list") or (s.get("summary") or {}).get("text")
                or (s.get("rejected") or {}).get("text") or (s.get("verified") or {}).get("text"))


def cite_parts(c):
    """(id, kind, rest, label) of one cite: an evidence id ("c607fa7d13", "commit:a1b2c3d", …) or {id, label, kind}."""
    cid = str((c.get("id") if isinstance(c, dict) else c) or "")
    pre, _, tail = cid.partition(":")
    rest = tail if pre in CITE_KINDS else cid
    kind = (c.get("kind") if isinstance(c, dict) else None) or (pre if pre in CITE_KINDS else "item")
    label = (c.get("label") if isinstance(c, dict) else None) or {
        "commit": "commit %s" % rest[:7], "pr": "PR #%s" % rest, "jira": rest, "query": "query", "table": rest,
        "replay": "replay step %s" % rest, "test": "test", "raw": "session log", "note": "your note"}.get(kind) or "item %s" % rest[:10]
    return cid, kind, rest, label


def cite_target(kind, rest):
    """Where a cite points from the hub README, or None (shown as text)."""
    if kind in ("item", "note", "test"):
        return "#i-" + rest
    if kind == "query":
        return "queries.md#q-" + rest
    if kind == "replay":
        return "requests.md"
    return None


def claim_texts(s):
    """[(label or None, text, cites)] of a section's claims, steps and list rows, in order (for the plain surfaces)."""
    out = []
    for key in ("steps", "claims", "list"):
        for c in brief_rows(s, key):
            if c.get("text"):
                out.append((CLAIM_LABEL.get(c.get("label")), c["text"].strip(), c.get("cites") or []))
    for key in ("rejected", "summary", "verified"):
        c = s.get(key)
        if isinstance(c, dict) and c.get("text"):
            label = {"rejected": "Rejected", "summary": None}.get(key)
            if key == "verified":
                label = "Verified" if c.get("state") == "verified" else "Not verified"
            out.append((label, c["text"].strip(), c.get("cites") or []))
    return out


def render_brief(v, items=None):
    """`## Brief` for the hub README: each section with its claims, evidence as footnotes ([^n]) that point at the
    items' anchors (#i-<id>), queries.md or requests.md. `items`: the ticket's items, to label bare item ids."""
    v = v or {}
    items = items or {}
    notes, order = {}, []

    def refs(cites):
        out = ""
        for c in cites or []:
            cid, kind, rest, label = cite_parts(c)
            it = items.get(rest) if kind in ("item", "note", "test") and not isinstance(c, dict) else None
            if it:
                label = ", ".join(x for x in (TAG.get(it.get("kind"), it.get("kind") or "item"), it.get("date") or "") if x)
            if not cid:
                continue
            if cid not in notes:
                order.append(cid)
                notes[cid] = (len(order), kind, rest, label)
            out += "[^%d]" % notes[cid][0]
        return out

    def line(label, text, cites):
        return ("**%s:** " % label if label else "") + (text or "").strip() + refs(cites)

    h = v.get("header") or {}
    key = v.get("key") or h.get("key") or ""
    L = ["## Brief", ""]
    meta = [x for x in ("Generated %s" % h["updated"][:16].replace("T", " ") if h.get("updated") else "",
                        "Jira: %s" % h["jira_status"] if h.get("jira_status") else "",
                        "Inferred: %s" % h["status_inferred"] if h.get("status_inferred") else "") if x]
    if meta and v.get("has_brief"):
        L += ["_%s_" % " · ".join(meta), ""]
    if v.get("stale") and v.get("has_brief"):
        L += ["> **New findings since %s.** Refresh the brief with `/cg:ticket %s --brief` or *Refresh brief* in the "
              "dashboard." % (str(v["stale"].get("since") or "")[:16].replace("T", " "), key), ""]
    if v.get("epic"):
        L += ["### Child tickets", ""]
        L += ["- [%s](../%s/README.md)%s%s%s" % (c.get("key"), c.get("key"), " — " + c["title"] if c.get("title") else "",
                                                 " _(%s)_" % c["status"] if c.get("status") else "",
                                                 ": " + c["line"] if c.get("line") else "")
              for c in v.get("children") or []] or ["_No child tickets recorded yet._"]
        L.append("")
    elif not v.get("has_brief"):
        rc = (v.get("fallback") or {}).get("root_cause") or {}
        conf = CONFIDENCE.get(rc.get("confidence"), rc.get("confidence"))
        L += ["_No brief yet: it is written at day close, or now with `/cg:ticket %s --brief`. Until then, the current "
              "root cause:_" % key, "", "### Root cause" + (" (%s)" % conf if conf else ""), "",
              line(None, rc["text"], rc.get("cites")) if rc.get("text") else "_%s._" % NOT_YET, ""]
    else:
        for s in brief_sections(v):
            head = "### " + brief_heading(s)
            if s["id"] == "root_cause" and s.get("confidence") and not brief_empty(s):
                head += " (%s)" % CONFIDENCE.get(s["confidence"], s["confidence"])
            if s["id"] == "fixes_made":
                head += " _(%s)_" % (s.get("source") or "from git and PRs")
            if s.get("override"):
                head += " _(your correction)_"
            L += [head, ""]
            if brief_empty(s):
                L += ["_%s._" % brief_empty_text(s).rstrip("."), ""]
                continue
            L += ["%d. %s" % (n, line(None, x.get("text"), x.get("cites"))) for n, x in enumerate(brief_rows(s, "steps"), 1)]
            L += [("" if s["id"] == "root_cause" else "- ") + line(CLAIM_LABEL.get(c.get("label")), c.get("text"), c.get("cites"))
                  for c in brief_rows(s, "claims")]
            L += ["- " + line(None, f.get("text"), f.get("cites")) for f in s.get("list") or []]
            for key_, label in (("rejected", "Rejected"), ("summary", None), ("verified", None)):
                c = s.get(key_)
                if isinstance(c, dict) and c.get("text"):
                    if key_ == "verified":
                        label = "Verified" if c.get("state") == "verified" else "Not verified"
                    L += ["", line(label, c["text"], c.get("cites"))]
            L.append("")
    for cid in order:
        n, kind, rest, label = notes[cid]
        target = cite_target(kind, rest)
        L.append("[^%d]: %s" % (n, "[%s](%s)" % (label.replace("]", ")"), target) if target
                                else "%s (`%s`)" % (label, cid) if label != cid else "`%s`" % cid))
    if order:
        L.append("")
    return L


def item_anchor(i):
    """`<a id="i-<id>"></a>` (and one per alias) so the brief's footnotes and the dashboard can point at an item."""
    return "".join('<a id="i-%s"></a>' % x for x in [i.get("id")] + list(i.get("aliases") or []) if x)


def code_map(tk):
    """One table of the ticket's verified files (edited, committed or found in git), changed ones first, each listed
    once; paths only the model named, and that git hasn't confirmed, go under a collapsed list."""
    files = sorted(tk.get("files", {}).values(), key=lambda f: (not f.get("changed"), f.get("path") or ""))
    if not files:
        return None
    ok = [f for f in files if f.get("verified") is True]
    rest = [f for f in files if f.get("verified") is not True]
    L = []
    if ok:
        L += ["| File | Role | Changed |", "|---|---|---|"]
        L += ["| %s | %s | %s |" % (code(f["path"]), cell(f.get("role")), "yes" if f.get("changed") else "—") for f in ok]
    if rest:
        L += ["", "<details><summary>Unverified paths (%d)</summary>" % len(rest), "",
              "_Named in the notes but not (yet) found in the repo: not edited in a session, not in a commit._", ""]
        L += ["- %s%s%s" % (code(f["path"]), " — " + cell(f.get("role")) if f.get("role") else "",
                            " _(pending check)_" if f.get("verified") is None else "") for f in rest]
        L += ["", "</details>"]
    return L


def delivery_stage(tk):
    from . import delivery
    return delivery.stage(tk.get("git") or {})


def stage_label(st):
    return "Delivery (computed from git and PRs%s)" % (", checked " + st["checked_at"][:10] if st.get("checked_at") else "")


def tldr_text(tk, st, md=True):
    """The model's TL;DR with every sentence that contradicts the computed delivery stage marked outdated."""
    from . import delivery
    out = []
    for s, note in delivery.check_tldr(tk.get("tldr"), st):
        out.append(s if not note else ("_%s:_ ~~%s~~" % (note, s) if md else "(%s) %s" % (note, s)))
    return " ".join(out)


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
    allq = sorted(tk["queries"].values(), key=lambda q: (q.get("date", ""), q.get("time", "")), reverse=True)
    qs = [q for q in allq if q.get("sent") is not False]
    if not qs:
        L.append("_No queries recorded yet._")
    for q in qs:
        lang = DIALECT.get(q.get("dialect"), ("SQL", "sql"))
        L += ['<a id="q-%s"></a>' % x for x in [q["id"]] + list(q.get("aliases") or [])]
        L.append("## %s %s · %s · %s — %s" % (q.get("date"), q.get("time") or "", lang[0], q.get("kind"), q.get("purpose") or "query"))
        if q.get("tables"):
            L.append("Tables: " + ", ".join(code(x) for x in q["tables"]))
        if q.get("revealed"):
            L.append("Revealed: " + q["revealed"])
        if q.get("ref"):
            L.append("Sent: " + code(q["ref"]))
        elif q.get("sent") is None and q.get("evidence"):
            L.append("Saved in: " + ", ".join(code(e) for e in q["evidence"][:2]))
        L += ["```" + lang[1], fence_safe(q.get("text")).rstrip(), "```", ""]
    hidden = len(allq) - len(qs)
    if hidden:
        L += ["_%d more quer%s the notes mention %s never found among the SQL sent; kept in the data, not listed "
              "here (dashboard: Not run)._" % (hidden, "y" if hidden == 1 else "ies", "was" if hidden == 1 else "were"), ""]
    return "\n".join(L) + "\n"


def shown_queries(tk):
    """The ticket's queries that were sent (or saved by you, or recorded before sending was checked): sent != False."""
    return [q for q in (tk.get("queries") or {}).values() if isinstance(q, dict) and q.get("sent") is not False]


def render_index(store):
    L = ["# Tickets", "", "| Ticket | Title | Status | First | Last | ≈ Time | TL;DR |", "|---|---|---|---|---|---|---|"]
    rows = []
    for t in Store.all_ticket_ids():
        tk = store.ticket(t)
        rows.append((tk.get("last") or "", t, tk))
    for _, t, tk in sorted(rows, reverse=True):
        L.append("| [%s](%s/README.md) | %s | %s | %s | %s | %s | %s |" % (
            t, t, title_cell(tk), status_text(tk), tk.get("first") or "—", tk.get("last") or "—",
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
    title = display_title(tk)
    L = ([al] if al else []) + ["Worklog facts recorded automatically for %s%s (full file: %s)." % (t, " — " + title if title else "", path),
         "Status: %s. First worked %s, last worked %s, about %s in total." % (
             status_text(tk) if status_text(tk) != "—" else "unknown", tk.get("first"), tk.get("last"), common.fmt_minutes(total_minutes(tk)))
         if tk.get("first") else "No Claude Code activity recorded for it yet."]
    st = delivery_stage(tk)
    if st:
        L.append("%s: %s." % (stage_label(st), st["text"]))
    v = brief_view(tk, t)
    L += brief_context_lines(v)
    cur = [i for i in current_items(tk) if isinstance(i, dict)]
    opens = sort_items([i for i in cur if i.get("kind") in OPEN_KINDS])[:6]
    if opens:
        L.append("Open TODOs / questions:")
        L += ["- %s" % common.truncate(i["title"], 200) for i in opens]
    if tk.get("tldr"):
        L.append("TL;DR: " + tldr_text(tk, st, md=False))
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
    # the newest findings fill what is left of the 4000 characters, whole lines only
    txt = common.truncate("\n".join(L), CONTEXT_MAX)
    finds = sort_items([i for i in cur if i.get("kind") not in OPEN_KINDS])[:40]
    if finds and len(txt) + 20 < CONTEXT_MAX:
        rows = []
        for i in finds:
            ev = "; ".join((i.get("evidence") or [])[:2])
            rows.append("- [%s] %s%s%s" % (TAG.get(i["kind"], i["kind"]), i["title"],
                                           " — " + common.truncate(i.get("detail"), 220) if i.get("detail") else "", " (" + ev + ")" if ev else ""))
        add = "\nRecent findings:"
        for r in rows:
            if len(txt) + len(add) + 1 + len(r) > CONTEXT_MAX:
                break
            add += "\n" + r
        if add != "\nRecent findings:":
            txt += add
    return redact(common.truncate(txt, CONTEXT_MAX))


CONTEXT_MAX = 4000
CONTEXT_CLAIM = 300


def current_items(tk):
    """The ticket's current items: the brief engine's definition when it has one, else not archived, not superseded,
    not flagged for review, and (files) not unverified / (queries) not unsent."""
    from . import brief
    fn = getattr(brief, "current_items", None)
    if fn:
        return fn(tk)
    return [i for i in (tk.get("items") or {}).values() if isinstance(i, dict) and not i.get("archived")
            and (i.get("status") or "current") != "superseded" and not i.get("review")
            and i.get("verified") is not False and i.get("sent") is not False]


def _cite_ids(cites, n=3):
    ids = [cite_parts(c)[0] for c in cites or []]
    ids = [x for x in ids if x][:n]
    return " [%s]" % ", ".join(ids) if ids else ""


def brief_context_lines(v):
    """The brief for the SessionStart context: root cause, fixes made and possible cons, each claim with its first
    evidence ids; the fallback's root cause until there is a brief; an Epic's children."""
    v = v or {}
    if v.get("epic"):
        ch = v.get("children") or []
        return ["Epic. Child tickets: %s." % "; ".join("%s %s%s" % (c.get("key"), common.truncate(c.get("title"), 80) or "",
                                                                     " (%s)" % c["status"] if c.get("status") else "") for c in ch[:12])] if ch else []
    if not v.get("has_brief"):
        rc = (v.get("fallback") or {}).get("root_cause") or {}
        if not rc.get("text"):
            return []
        return ["Root cause (%s; no brief yet): %s%s" % (rc.get("confidence") or "unknown", common.truncate(rc["text"], CONTEXT_CLAIM),
                                                         _cite_ids(rc.get("cites")))]
    by = {s["id"]: s for s in brief_sections(v)}
    out = ["Brief (generated %s%s; evidence ids in brackets, get_ticket section=brief has them all):" % (
        ((v.get("header") or {}).get("updated") or "?")[:16].replace("T", " "),
        "; new findings since %s" % str(v["stale"].get("since") or "")[:16].replace("T", " ") if v.get("stale") else "")]
    rc = by.get("root_cause")
    if rc and not brief_empty(rc):
        out.append("Root cause (%s): %s" % (rc.get("confidence") or "unknown", " ".join(
            common.truncate(text, CONTEXT_CLAIM) + _cite_ids(cites) for _l, text, cites in claim_texts(rc))))
    fm = by.get("fixes_made")
    if fm and not brief_empty(fm):
        out.append("Fixes made (%s): %s" % (fm.get("source") or "from git and PRs", "; ".join(
            common.truncate(text, CONTEXT_CLAIM) + _cite_ids(cites) for _l, text, cites in claim_texts(fm))))
    cons = by.get("cons")
    if cons and not brief_empty(cons):
        out.append("Possible cons: %s" % "; ".join(("%s: " % lab if lab else "") + common.truncate(text, CONTEXT_CLAIM) + _cite_ids(cites)
                                                   for lab, text, cites in claim_texts(cons)))
    return out if len(out) > 1 else []


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
- Only `daily/` (plus this README, `.gitignore` and `.gitattributes`) is pushed; everything else stays on this machine.
- QA query results and QA worked examples may be kept in the local knowledge base (never pushed, never in daily
  files or Slack). Production data, API response bodies, log payloads and raw HL7/FHIR messages are never stored.
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
    update_search_index()
    return changed


def update_search_index():
    """Bring the search index (.worklog/cache/search.sqlite) up to date with what was just rendered. Fail-open: the
    index is a rebuildable cache, so an error is logged and the tick carries on."""
    try:
        from . import searchidx
        res = searchidx.update()
        if res["changed"] or res["removed"]:
            common.log("search", "index updated: %(changed)d changed, %(removed)d removed, %(units)d units, %(ms)d ms" % res)
    except Exception:  # noqa: BLE001
        import traceback
        common.log("search", "index update failed: " + traceback.format_exc())
