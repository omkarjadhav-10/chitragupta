"""`cli companion …`: the Companion's text fallback (MOD-07) when Mods are unavailable.

Concise text rendered from the same companionviews projections the dashboard, MCP bridge and native mod read.
Read-only: never writes, queues a job, acknowledges changes or touches global capture (`cli resume` /
`/cg:pause resume` stay the capture controls). Generation, improvement and approval happen only in the dashboard
Companion page or another explicit protected action.
"""
import json
import re

from . import common, companionviews as views

ACTIONS = ("planner", "resume", "review", "verification", "handoff", "changes", "draft")
_WID = re.compile(r"^w_[a-f0-9]{64}$")
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
DASHBOARD = "Open the dashboard Companion page (/cg:dashboard) to generate, improve, approve or record anything."


def _short(wid):
    return (wid or "")[:14]


def _pct(m):
    if not m or m.get("percent") is None:
        return "unavailable" if not m or m.get("state") == "unavailable" else "N/A"
    return "%.1f%% (%s/%s)" % (m["percent"], _num(m.get("numerator")), _num(m.get("denominator")))


def _num(v):
    return ("%g" % v) if isinstance(v, (int, float)) else "?"


def planner_text(env):
    if env.get("state") == "identity_unconfirmed":
        return [env["empty"], "Next: open profile setup (/cg:setup or the dashboard Profile page)."]
    lines = ["Daily Planner (%s; cached records only)" % env.get("scope", "your confirmed profile")]
    counts = ", ".join("%s %d" % (g["label"], g["count"]) for g in env["groups"] if g["count"])
    lines.append("Stages: " + (counts or "none"))
    if env.get("empty"):
        lines.append(env["empty"])
    group = None
    for c in env["items"]:
        if c["group_label"] != group:
            group = c["group_label"]
            lines.append("")
            lines.append("## " + group)
        lines.append("- %s  %s" % (c["ticket"], c["title"] or "Untitled"))
        lines.append("  %s · priority %s · Jira %s · %s" % (c["action"], c["priority"], c["jira_status"],
                                                           c["criteria"]["label"]))
        lines.append("  Why: " + c["why"])
        for a in c["attempts"]:
            lines.append("  attempt %s · %s · %s" % (_short(a["workflow"]), a["stage"], a["repository"]))
        if c.get("reconstruction"):
            lines.append("  " + c["reconstruction"])
    done = env["completed"]["count"]
    if done and env.get("group") != "completed":
        lines.append("")
        lines.append("Completed: %d (collapsed; --group completed lists them)" % done)
    if env.get("cursor"):
        lines.append("")
        lines.append("More: cli companion planner --cursor %s" % env["cursor"])
    lines.append("")
    lines.append(DASHBOARD)
    return lines


GRAPH_LISTS = (("completed", "Completed since"), ("invalidated", "No longer holds"), ("outstanding", "Outstanding"))
GRAPH_NEW = (("commits", "New commits"), ("tests", "New tests and runs"), ("prs", "Pull requests and release"),
             ("jira", "Jira changes"))


def graph_lines(g):
    """The Evidence Graph part of the resume brief (graphviews.resume); a missing graph is one line."""
    if not g:
        return []
    if g.get("state") != "ok":
        return ["", "Evidence graph: unavailable (%s)" % g.get("reason")]
    cp = g["checkpoint"]
    lines = ["", "Evidence graph since your checkpoint: " + (
        "sequence %s → %s" % (cp["seq"], g.get("to")) if cp["state"] == "ok" else "no comparison (%s)" % cp["reason"])]

    def show(title, part):
        if not part or not part["total"]:
            return
        lines.append("%s (%d):" % (title, part["total"]))
        for i in part["items"]:
            if "statuses" in i:
                text = ", ".join("%s %s" % (k.replace("_", " "), v) for k, v in i["statuses"].items())
            elif "from" in i and i.get("kind") != "requirements source":
                text = "%s: %s → %s" % (i["kind"].replace("_", " "), i["from"], i["to"])
            else:
                text = " · ".join(str(x) for x in (i.get("kind"), i.get("outcome"), i.get("applicability")) if x)
            lines.append("- %s: %s" % (i.get("label") or i.get("id") or "", text))
        if part["total"] > len(part["items"]):
            lines.append("  … %d in total" % part["total"])
    for name, title in GRAPH_LISTS[:2]:
        show(title, g.get(name))
    for name, title in GRAPH_NEW:
        show(title, (g.get("new") or {}).get(name))
    show(GRAPH_LISTS[2][1], g.get("outstanding"))
    lines.append("Graph next action: %s" % g.get("next_action"))
    return lines


def detail_text(d, changes=None):
    m = d["measures"]
    lines = ["%s · %s · %s" % (d["ticket"], d["title"], d["repository"]),
             "Task stage: %s (%s) · workflow %s · checkpoint revision %d %s" % (
                 d["stage"].replace("_", " "), d["situation_label"], _short(d["workflow"]), d["revision"],
                 d.get("last_checkpoint") or ""),
             "Approved: %s · selected %s" % (d["approved_label"], _short(d["selected_plan"]) or "none"),
             "", "Next action: %s. %s" % (d["next_action"]["label"], d["next_action"]["text"])]
    blocking = [r for r in d["recovery"] if r["blocking"]]
    for r in blocking[:3]:
        lines.append("Recovery: %s — %s %s" % (r["label"], r["reason"], r["action"]))
    lines.append("")
    lines.append("Plan coverage (advisory): %s" % (_pct(m["plan_coverage"]) if m["plan_coverage"] else "unavailable"))
    impl = m["implementation_progress"]
    lines.append("Implementation progress: %s" % ("%d of %d evidenced steps (%d pending, %d uncertain)" % (
        impl["numerator"], impl["denominator"], impl["pending"], impl["uncertain"]) if impl else "no plan"))
    lines.append("Functional verification: %s" % _pct(m["functional_verification"]))
    cont = d.get("continuation")
    if cont:
        if cont.get("carry_forward"):
            lines.append("Carry forward: " + ", ".join(s["step"] for s in cont["carry_forward"]))
        if cont.get("remaining"):
            lines.append("Remaining: " + ", ".join(cont["remaining"]))
        if cont.get("revalidate"):
            lines.append("Revalidate: " + ", ".join(cont["revalidate"]))
        if cont.get("scope"):
            lines.append("Scope: " + cont["scope"])
    ch = changes or {}
    lines.append("")
    lines.append("Since your last review: %s" % (ch.get("label") or d["changes"].get("label") or
                                                 "%d change(s)" % d["changes"]["total"]))
    for item in (ch.get("items") or d["changes"]["items"])[:10]:
        lines.append("- %s %s: %s (%s, %s)" % (item["change"], item["kind"], item["label"], item["provenance"],
                                              item.get("observed_at") or "time unknown"))
    if (ch.get("total") or d["changes"]["total"]) > 10:
        lines.append("  … %d in total" % (ch.get("total") or d["changes"]["total"]))
    lines += graph_lines(d.get("graph"))
    lines.append("Actions here: " + ", ".join(a["name"] for a in d["actions"]))
    if d.get("switch"):
        lines.append("Other active tasks: " + ", ".join("%s (%s)" % (s["ticket"], s["stage"]) for s in d["switch"]))
    lines.append("")
    lines.append("This is a task resume brief; it does not resume capture (/cg:pause resume does) and starts no work.")
    lines.append(DASHBOARD)
    return lines


def review_text(r):
    ev = r["evaluation"]
    lines = ["Jev Review · %s · selected %s" % (r["ticket"], _short(r["selected_plan"]) or "none"),
             "Evaluation: %s%s" % (ev["label"], (" — " + ev["reason"]) if ev.get("reason") else "")]
    if ev.get("percent") is not None:
        lines.append("  %s: %s" % (ev["origin"], _pct(ev)))
    if r.get("assessment"):
        lines.append("%s: %s" % (r["assessment_label"], _pct(r["assessment"])))
    if r["gaps"]:
        lines.append("Open criteria: " + ", ".join("%s %s" % (g["criterion"], g["status"]) for g in r["gaps"][:20]))
    cmp_ = r.get("comparison")
    if cmp_:
        lines.append("Comparison with the parent version: %s" % cmp_["direction"])
    lines.append("")
    for c in r["choices"]:
        lines.append("%s%s: %s" % ("[recommended] " if c["recommended"] else "", c["action"],
                                   "available" if c["available"] else "unavailable — " + c["reason"]))
    ex = r["execute"]
    if ex["plan"]:
        lines.append("Execute shows: version %s, digest %s, baseline %s (%s), target %s" % (
            _short(ex["plan"]), ex["digest"][:16], _short(ex["baseline"]), ex["baseline_completeness"],
            ex["target"]["repository"]))
        for risk in ex["risks"][:5]:
            lines.append("  risk: " + risk)
    lines.append("")
    lines.append(DASHBOARD)
    return lines


def verification_text(v):
    m = v["measures"]
    lines = ["Requirement verification · %s (recomputed for display; nothing recorded, no test run)" % v["ticket"],
             "Plan coverage (advisory): %s" % _pct(m["plan_coverage"]),
             "Implementation progress: %s" % ("%d of %d evidenced steps" % (
                 m["implementation_progress"]["numerator"], m["implementation_progress"]["denominator"])
                 if m["implementation_progress"] else "no plan"),
             "Functional verification: %s" % _pct(m["functional_verification"]), ""]
    for row in v["rows"]:
        lines.append("- %s %s: %s" % (row["criterion"], row["status"], row["text"]))
        lines.append("  " + row["applicability"])
    if v.get("cursor"):
        lines.append("More: cli companion verification %s --cursor %s" % (v["workflow"], v["cursor"]))
    return lines


def handoff_text(h):
    if h["state"] != "prepared_not_submitted":
        return ["No current approved handoff: %s" % (h.get("reason") or "approve an exact plan first"), DASHBOARD]
    lines = ["Prepared; not submitted · attempt %s · version %s · digest %s" % (
        h["attempt"], _short(h["plan"]), h["plan_digest"][:16]),
        "Copy it into a separate ordinary Claude turn yourself; normal permissions apply. Copying does not start "
        "execution, and nothing is recorded here.", "", h["text"]]
    if h.get("cursor"):
        lines.append("More: cli companion handoff %s --cursor %s" % (h["workflow"], h["cursor"]))
    return lines


def draft_text(env, audience=None):
    """FLOW-04 text: one audience's current version (or a preview when none is saved), or the three audiences."""
    if not audience:
        lines = ["Drafts · %s · workflow %s (local text; nothing is sent)" % (env["ticket"], _short(env["workflow"]))]
        for a in env["audiences"]:
            cur = a.get("current")
            lines.append("- %s: %s" % (a["label"], "version %d (%s)%s" % (
                cur["number"], cur["origin_label"], " · " + cur["stale_label"] if cur["stale"] else "")
                if cur else "no saved version; a preview is available"))
        lines.append("Print one: cli companion draft %s --audience pr|qa|session" % env["workflow"])
        return lines + [DASHBOARD]
    a = next(x for x in env["audiences"] if x["id"] == audience)
    cur = a.get("current")
    if cur:
        lines = ["%s draft · version %d (%s) · %s" % (a["label"], cur["number"], cur["origin_label"], cur.get("at") or "")]
        if cur["stale"]:
            lines.append("%s since this version was generated. Your text is kept; Generate draft in the dashboard "
                         "to compare." % cur["stale_label"])
        if a.get("regenerated"):
            lines.append(a["regenerated"]["label"] + "; the diff is in the dashboard Drafts section.")
        lines += ["", cur["text"]]
    else:
        lines = ["%s draft · preview (not saved)" % a["label"], "", a["preview"]["text"]]
    return lines + ["", "Local text only: nothing was sent. Copy it yourself; " + DASHBOARD[0].lower() + DASHBOARD[1:]]


def _workflows_for(key):
    cfg = common.load_config()
    from .dashboard import api
    if not api.scope_of(key, cfg).get("visible"):
        raise PermissionError("ticket is unavailable in the current profile")
    return [r for r in views.index(cfg).get(key) or [] if not r.get("archived")]


def run(a, out):
    """-> exit code. Errors are messages; nothing is written."""
    target = " ".join(a.target).strip()
    base = {"privacy": True} if a.privacy else {}
    try:
        if a.action == "planner":
            env = views.read(dict(base, operation="planner", **({"cursor": a.cursor} if a.cursor else {}),
                                  **({"group": a.group} if a.group else {})))
            return _emit(a, out, env, planner_text)
        if not target:
            out("Name a task: cli companion %s <%s>%s" % (a.action, "TICKET|w_ID" if a.action == "resume" else "w_ID",
                                                         " --audience pr|qa|session" if a.action == "draft" else ""))
            return 2
        if a.action == "resume" and not _WID.fullmatch(target):
            if not _KEY.fullmatch(target):
                out("Not a ticket key or workflow ID: %s" % target[:60])
                return 2
            key = target.upper()
            recs = _workflows_for(key)
            if not recs:
                env = views.read(dict(base, operation="snapshot", ticket=key))
                card = next((c for c in env["items"] if c["ticket"] == key), None)
                out("%s has no task workflow yet. %s" % (key, (card or {}).get("reconstruction") or
                                                        "Start it from the Daily Planner."))
                out(DASHBOARD)
                return 0
            if len(recs) > 1:
                out("%s has %d task attempts; which one? (nothing was chosen for you)" % (key, len(recs)))
                for r in recs:
                    out("- %s · %s · %s · cli companion resume %s" % (_short(r["id"]), r["stage"].replace("_", " "),
                                                                     views._repo(r["binding"]["location"]), r["id"]))
                return 0
            target = recs[0]["id"]
        if not _WID.fullmatch(target):
            out("Name a workflow ID (w_…); cli companion planner lists them")
            return 2
        args = dict(base, workflow=target, **({"cursor": a.cursor} if a.cursor else {}))
        if a.action == "resume":
            d = views.read(dict(base, operation="detail", workflow=target))
            ch = views.read(dict(base, operation="changes", workflow=target))
            if a.json:
                out(json.dumps({"detail": d, "changes": ch}, ensure_ascii=False, indent=1))
                return 0
            out("\n".join(detail_text(d, ch)))
            return 0
        if a.action == "draft":
            env = views.read(dict(base, operation="drafts", workflow=target))
            return _emit(a, out, env, lambda e: draft_text(e, getattr(a, "audience", None)))
        render = {"review": review_text, "verification": verification_text, "handoff": handoff_text,
                  "changes": lambda c: detail_text(views.read(dict(base, operation="detail", workflow=target)), c)}
        return _emit(a, out, views.read(dict(args, operation=a.action)), render[a.action])
    except PermissionError as exc:
        out("Not available: %s" % exc)
        return 2
    except ValueError as exc:
        out("Not possible: %s" % exc)
        return 2


def _emit(a, out, env, render):
    out(json.dumps(env, ensure_ascii=False, indent=1) if a.json else "\n".join(render(env)))
    return 0
