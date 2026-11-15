"""FLOW-04 audience drafts and FLOW-05 decision context for the Companion.

Three deterministic templates, PR reviewer, QA tester and Next developer session, built only from the workflow's own
projections: requirement baseline, approved plan, evidence and its applicability, the verification ledger, delivery
reminders, recorded decisions and an optional cached database context summary (no rows, keys or SQL). No model,
provider, process or network call, and nothing is sent: copying is the reader's own action on text that passed
redaction, after a fresh, access-checked read. Drafts are immutable versions in the workflow record (`audience_drafts`)
saved through companion._change under an expected revision. Generating adds a generated version and never replaces
your edit; a version whose source generation no longer matches current evidence keeps its text and says `Source
evidence changed`. Decisions reuse the ticket's knowledge items and their supersession (supersede.py): no second store.
"""
import os
import re

from . import common, supersede, workflowmodel as model
from . import companionviews as views
from .redact import redact

AUDIENCES = {"pr": "PR reviewer", "qa": "QA tester", "session": "Next developer session"}
ORIGIN_LABEL = {"generated": "Generated", "manual": "Your edit"}
TEMPLATE = "drafts-v1"
TEXT_BYTES = 8000
VERSIONS = 40
LIST = 8
STALE = "Source evidence changed"
DECISION_KINDS = ("decision", "fix", "root_cause")
NOT_A_PROHIBITION = "context only, not a prohibition here"
_DID = re.compile(r"^d_[a-f0-9]{64}$")
_TEST_REF = re.compile(r"^test:(.{1,200})$")
_REMINDER = {"mapping": "Release mapping unresolved", "release-ambiguous": "Release mapping unresolved",
             "uat-unknown": "Whether UAT is required is not recorded",
             "uat-missing": "UAT required: the current changes are not evidenced in UAT",
             "uat-verify": "UAT required: reaching UAT is unverified",
             "uat-deployed": "UAT deployment of the current revision is unverified",
             "uat-accepted": "UAT acceptance of the current revision is unverified"}


class Unchanged(Exception):
    """Nothing new to save: the action answers with the current record and a notice."""


def _audience(value):
    if value not in AUDIENCES:
        raise ValueError("choose a draft audience: pr, qa or session")
    return value


def _selection(value):
    """The session-local database selection a draft may summarise ({flow?, investigation?}), validated."""
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid database selection")
    out = {}
    for key in ("flow", "investigation"):
        v = value.get(key)
        if v in (None, ""):
            continue
        if not isinstance(v, str) or not views._DBID[key].fullmatch(v):
            raise ValueError("invalid %s ID" % key)
        out[key] = v
    return out or None


def export(text):
    """Copy/export text: existing redaction, then no email address or local absolute path; line breaks kept."""
    out = redact(str(text or ""))
    out = views._ABS.sub("[path]", views._EMAIL.sub("[email]", out))
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", out)


def _text(value):
    """A manual edit: sanitized and size-capped like plan text, with nothing a shared draft should not carry."""
    text = model.text(value, TEXT_BYTES)
    if not text.strip():
        raise ValueError("the draft is empty")
    if views._EMAIL.search(text) or views._ABS.search(text):
        raise ValueError("remove email addresses and local absolute paths: drafts are written to be shared")
    return text


def _line(value, limit=200):
    return views.scrub(value, limit)


def _more(rows, limit=LIST):
    return rows[:limit] + (["- … and %d more in the dashboard Companion page" % (len(rows) - limit)]
                           if len(rows) > limit else [])


# ---------------------------------------------------------------- FLOW-05: decisions and failed approaches

def _status(item):
    if item.get("review"):
        return "needs_review", "Needs review"
    if item.get("status") == "superseded":
        return "superseded", "Superseded"
    if item.get("status") == "confirmed" or supersede.is_user_note(item):
        return "confirmed", "Confirmed"
    if item.get("status") == "hypothesis":
        return "proposed", "Proposed"
    return "recorded", "Recorded (captured; not confirmed)"


def _sessions():
    state = common.read_json(common.wl("state.json"), {}) or {}
    rows = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    return {str(k): v for k, v in rows.items() if isinstance(v, dict)}


def _origin(item, tk, sessions):
    """(repository location, environment, basis) a negative finding was observed in: a cited test's captured
    context first, else the session that recorded it. Unknown stays unknown."""
    tests = tk.get("tests") if isinstance(tk.get("tests"), dict) else {}
    for ev in item.get("evidence") or []:
        m = _TEST_REF.match(str(ev))
        ctx = ((tests.get(m.group(1)) or {}).get("context") or {}) if m else {}
        if isinstance(ctx, dict) and (ctx.get("location") or ctx.get("environment")):
            return ctx.get("location"), ctx.get("environment"), "test:" + m.group(1)
    ref = str(item.get("ref") or "")
    sid = ref.rsplit("/", 1)[-1].split(".jsonl")[0] if ref.startswith("raw/") else ""
    short = str(item.get("session") or "")
    hits = [v for k, v in sessions.items() if (sid and k == sid) or (not sid and short and k.startswith(short))]
    if len(hits) == 1 and hits[0].get("repo_root"):
        return hits[0]["repo_root"], None, "session"
    return None, None, None


def _applies(item, tk, sessions, here, env):
    repo, item_env, basis = _origin(item, tk, sessions)
    name = views._repo(repo) if repo else None
    if repo and common.norm_path(repo) != common.norm_path(here):
        return {"state": "other_repository", "repository": name, "environment": item_env, "basis": basis,
                "label": "Recorded in another repository (%s); %s" % (name, NOT_A_PROHIBITION)}
    if item_env and env and str(item_env).lower() != str(env).lower():
        return {"state": "other_environment", "repository": name, "environment": item_env, "basis": basis,
                "label": "Recorded in environment %s; this task records %s: %s" % (
                    _line(item_env, 40), _line(env, 40), NOT_A_PROHIBITION)}
    if repo or item_env:
        return {"state": "here", "repository": name, "environment": item_env, "basis": basis,
                "label": "Recorded in this repository%s" % (" (environment %s)" % _line(item_env, 40)
                                                           if item_env else "")}
    return {"state": "unknown", "repository": None, "environment": None, "basis": None,
            "label": "Applicability unknown: shown for review, not a prohibition"}


def decisions(rec, tk, env=None):
    """The ticket's decisions as the Companion shows them everywhere (detail, drafts): current advice with what it
    superseded, the history, conflicting evidence for review, and failed approaches with their applicability."""
    items = {k: v for k, v in (tk.get("items") or {}).items() if isinstance(v, dict) and not v.get("archived")}
    when = lambda i: (str(i.get("date") or ""), str(i.get("time") or ""), str(i.get("id") or ""))  # noqa: E731
    kinds = [i for i in items.values() if i.get("kind") in DECISION_KINDS]
    current = sorted((i for i in kinds if supersede.is_current(i)), key=when, reverse=True)
    current.sort(key=lambda i: _status(i)[0] != "confirmed")

    def row(i, extra=None):
        st, label = _status(i)
        out = {"id": _line(i.get("id"), 80), "kind": i.get("kind"), "title": _line(i.get("title"), 200),
               "status": st, "status_label": label, "date": _line(i.get("date"), 20) or None,
               "link": "items:" + _line(i.get("id"), 80)}
        out.update(extra or {})
        return out

    def replaced(target):
        return sorted((i for i in kinds if i.get("status") == "superseded" and
                       supersede._chain_reaches(items, i.get("superseded_by"), target)), key=when, reverse=True)
    history = sorted((i for i in kinds if i.get("status") == "superseded"), key=when, reverse=True)
    subjects = {}
    for i in current:
        subj = " ".join(str(i.get("subject") or "").lower().split())
        if subj:
            subjects.setdefault(subj, []).append(i)
    conflicts = [{"subject": _line(s, 80), "items": [row(i) for i in rows[:4]],
                  "label": "Conflicting evidence: review before relying on either"}
                 for s, rows in sorted(subjects.items())
                 if len({" ".join(str(i.get("title") or "").lower().split()) for i in rows}) > 1]
    review = sorted((i for i in kinds if i.get("review") and i.get("status") != "superseded"), key=when, reverse=True)
    sessions = _sessions()
    failed = sorted((i for i in items.values() if i.get("kind") == "error" or
                     str(i.get("status") or "").lower() in ("rejected", "reverted")), key=when, reverse=True)
    rejected = [row(i, {"status": "rejected", "status_label": "Rejected approach",
                        "applies": _applies(i, tk, sessions, rec["binding"]["location"], env)}) for i in failed[:10]]
    rows = [row(i, {"supersedes": [{"id": _line(o.get("id"), 80), "title": _line(o.get("title"), 160),
                                    "date": _line(o.get("date"), 20) or None} for o in replaced(i.get("id"))[:3]]})
            for i in current[:5]]
    return {"relevant": rows[:2], "current": rows, "current_total": len(current),
            "history": [row(i, {"superseded_by": _line(i.get("superseded_by"), 80)}) for i in history[:5]],
            "superseded_total": len(history), "conflicts": conflicts[:3], "needs_review": [row(i) for i in review[:3]],
            "rejected": rejected[:5], "rejected_here": [r for r in rejected if r["applies"]["state"] == "here"][:2],
            "rejected_elsewhere": sum(1 for r in rejected if r["applies"]["state"] != "here"),
            "link": "#/tickets/%s" % rec["binding"]["ticket"],
            "note": "Recorded knowledge items and their supersession. A later decision supersedes an earlier one "
                    "visibly; a failed approach is context for its own repository or environment, never a "
                    "prohibition elsewhere."}


# ---------------------------------------------------------------- sources (read only)

def gather(rec, cfg, selection=None):
    """Every input a template uses, plus its source generation (a digest of material inputs only: a refresh time or
    a re-read of an unchanged source never makes a draft stale)."""
    from . import companion, companionevidence as evidence
    ctx = companion.context(rec)
    tk, rows = evidence.current_sources(rec)
    base = rec["baselines"][rec["baseline"]]
    plan_id = rec.get("approved_plan") or rec.get("selected_plan")
    plan = rec["plans"].get(plan_id) if plan_id else None
    led = evidence.ledger(rec, ctx, "")
    current = evidence.applicable(ctx)
    groups = model.reconcile(plan, rec["evidence"], current) if plan else None
    sit, stage = companion.situation(rec)
    obs = companion.observation(rec)
    db = views.database_context(cfg, selection) if selection else None
    release = tk.get("release") if isinstance(tk.get("release"), dict) else {}
    repo = evidence.repo_key(rec["binding"]["location"])
    stages = ((release.get("repos") or {}).get(repo) or {}).get("stages") or {}
    uat = release.get("uat") if isinstance(release.get("uat"), dict) else {}
    replay = [s for s in ((tk.get("replay") or {}).get("final") or []) if isinstance(s, dict)]
    know = decisions(rec, tk, obs.get("environment"))
    data = {"rec": rec, "ticket": rec["binding"]["ticket"], "title": tk.get("title") or tk.get("title_hint") or "",
            "repository": views._repo(rec["binding"]["location"]), "base": base, "plan_id": plan_id, "plan": plan,
            "approved": rec.get("approved_plan"), "ledger": led, "groups": groups, "situation": sit, "stage": stage,
            "observation": obs, "db": db, "selection": selection, "rows": rows, "stages": stages, "uat": uat,
            "reminders": [r for r in (release.get("reminders") or []) if isinstance(r, dict)], "replay": replay,
            "knowledge": know, "manual": [e for e in rec["evidence"] if e.get("kind") in model.MANUAL],
            "notes": [d for d in rec.get("decisions") or [] if isinstance(d, dict)]}
    material = {"template": TEMPLATE, "baseline": base["id"], "plan": plan_id,
                "plan_digest": model.digest(plan) if plan else None, "approved": rec.get("approved_plan"),
                "stage": rec["stage"], "situation": sit, "groups": groups,
                "ledger": [(r["criterion"], r["status"]) for r in led["rows"]],
                "rows": sorted((sid, r.get("version")) for sid, r in rows.items()),
                "evidence": sorted((e.get("id"), e.get("version") or e.get("observed_at")) for e in rec["evidence"]),
                "observation": {k: obs.get(k) for k in ("head", "branch", "worktree", "environment", "config")},
                "stages": {k: (v or {}).get("state") for k, v in stages.items() if isinstance(v, dict)},
                "uat": {"revision": (uat.get("revision") or {}).get("rev"),
                        "requirement": (uat.get("requirement") or {}).get("state"),
                        "deployed": (uat.get("deployed") or {}).get("state"),
                        "accepted": (uat.get("accepted") or {}).get("state")},
                "replay": [(s.get("n"), s.get("method"), s.get("url"), s.get("expected")) for s in replay],
                "knowledge": {k: know[k] for k in ("current", "history", "conflicts", "needs_review", "rejected")},
                "notes": [(d.get("text"), d.get("at")) for d in data["notes"]],
                "request": rec.get("verification_request"),
                "db": {k: v for k, v in (db or {}).items() if k != "links"} or None}
    data["generation"] = "g_" + model.digest(material)[:24]
    return data


# ---------------------------------------------------------------- templates

def _purpose(d):
    base = d["base"]
    n = sum(1 for c in base["criteria"] if not c["excluded"])
    plan = ("approved version %s" % d["approved"][:14] if d["approved"] else
            "selected version %s (not approved)" % d["plan_id"][:14] if d["plan_id"] else "none saved")
    return ["%s: %s" % (d["ticket"], _line(d["title"]) or "Untitled ticket"),
            "Requirement baseline: %d confirmed criteri%s (%s)." % (n, "on" if n == 1 else "a", base["completeness"]),
            "Plan: %s." % plan,
            "Task stage: %s (%s)." % (views.GROUP_LABELS.get(d["rec"]["stage"], d["rec"]["stage"].replace("_", " ")),
                                      views.SITUATIONS.get(d["situation"], d["situation"]))]


def _verified(d):
    rows = [r for r in d["ledger"]["rows"] if r["status"] == "Verified" and not r["excluded"]]
    out = ["- %s %s: verified by current applicable %s evidence" % (r["criterion"], _line(r.get("text"), 160),
                                                                    "/".join(r.get("basis") or ["captured"]))
           for r in rows]
    out = _more(out) or ["- None verified by current applicable evidence."]
    impl = d["ledger"]["measures"]["implementation_progress"]
    if impl:
        out.append("Implementation: %d of %d approved steps evidenced (%d pending, %d uncertain)." % (
            impl["numerator"], impl["denominator"], impl["pending"], impl["uncertain"]))
    return out


def _evidence(d):
    rows = d["rows"].values()
    newest = lambda kind: sorted((r for r in rows if r["kind"] == kind),  # noqa: E731
                                 key=lambda r: (str(r.get("observed_at") or ""), r["id"]), reverse=True)
    out = []
    tests = newest("test")
    if tests:
        count = {k: sum(1 for t in tests if t["outcome"] == k) for k in ("passed", "failed")}
        out.append("- Recorded test runs: %d (%d passed, %d failed, %d other). A run verifies a criterion only when "
                   "it is mapped to it and its captured context matches the current one." % (
                       len(tests), count["passed"], count["failed"], len(tests) - count["passed"] - count["failed"]))
        out += ["  - %s: %s (%s)" % (t["outcome"], _line(t.get("label"), 120), _line(t.get("observed_at"), 25)
                                     or "time unknown") for t in tests[:3]]
    ci = newest("ci")
    out += ["- CI %s: %s" % (_line(c.get("outcome"), 20), _line(c.get("label"), 120)) for c in ci[:3]] or \
        ["- CI checks unavailable."]
    for p in newest("pr")[:3]:
        out.append("- PR %s: %s%s" % (p["id"].rsplit(":", 1)[-1], p.get("outcome") or "unknown",
                                       ", review: " + _line(p.get("review"), 40) if p.get("review") else ""))
    commits = newest("commit")
    if commits:
        out.append("- Commits observed in %s: %d (latest: %s)." % (d["repository"], len(commits),
                                                                  _line(commits[0].get("label"), 100)))
    files = [f for f in newest("file") if f.get("outcome") == "changed"]
    if files:
        out.append("- Files changed: %d (%s)." % (len(files), ", ".join(_line(f.get("label"), 80) for f in files[:5])))
    if not out:
        out.append("- No captured evidence for this repository yet.")
    if d["manual"]:
        out.append("Self-reported (developer observations, not captured evidence):")
        out += ["- %s %s at revision %s: %s (limits: %s)" % (
            e.get("kind"), e.get("outcome"), _line(e.get("revision"), 16), _line(e.get("label"), 160),
            _line(e.get("limits"), 120)) for e in d["manual"][-5:]]
    return _more(out, LIST + 8)


def _decisions(d):
    k = d["knowledge"]
    out = []
    for r in k["current"][:4]:
        sup = "; supersedes: " + ", ".join('"%s"' % s["title"] for s in r["supersedes"]) if r["supersedes"] else ""
        out.append("- [%s] %s (%s%s)%s" % (r["status_label"], r["title"], r["kind"],
                                           ", " + r["date"] if r["date"] else "", sup))
    for r in k["history"][:3]:
        out.append("- Earlier advice kept as history: %s (superseded)" % r["title"])
    for c in k["conflicts"]:
        out.append("- %s on %s: %s" % (c["label"], c["subject"], " vs ".join('"%s"' % i["title"] for i in c["items"])))
    for r in k["needs_review"]:
        out.append("- Needs review: %s" % r["title"])
    for r in k["rejected"][:4]:
        out.append("- Rejected approach: %s. %s" % (r["title"], r["applies"]["label"]))
    if d["notes"]:
        out.append("Developer notes (self-reported):")
        out += ["- %s" % _line(n.get("text"), 200) for n in d["notes"][-3:]]
    return out or ["- No decisions recorded for this ticket."]


def _reminder_line(r):
    kind = str(r.get("kind") or "")
    text = _REMINDER.get(kind) or "Delivery reminder (%s)" % _line(kind or "unspecified", 40)
    return "- %s%s." % (text, " on " + _line(r.get("repo"), 60) if r.get("repo") else "")


def _unresolved(d):
    out = ["- %s %s" % (r["criterion"], views._applicability_text(r))
           for r in d["ledger"]["rows"] if r["status"] != "Verified" and not r["excluded"]]
    plan = d["plan"]
    if plan:
        risks = (plan.get("details") or {}).get("risks") or [ln.strip("- ").strip() for ln in
                                                             plan["sections"]["risks"].splitlines() if ln.strip()]
        out += ["- Plan risk: %s" % _line(r, 200) for r in risks[:4]]
    out += [_reminder_line(r) for r in d["reminders"][:5]]
    out += ["- PR %s has requested changes." % p["id"].rsplit(":", 1)[-1] for p in d["rows"].values()
            if p["kind"] == "pr" and p.get("review") == "changes_requested"]
    if d["knowledge"]["conflicts"]:
        out.append("- Conflicting decisions need review (see Decisions).")
    return _more(out) or ["- None recorded."]


def _environments(d):
    out = []
    for role, s in sorted(d["stages"].items()):
        if isinstance(s, dict) and s.get("state"):
            out.append("- %s on %s: %s" % (role.upper(), d["repository"], _line(s.get("state"), 30).replace("_", " ")))
    envs = sorted({(r.get("context") or {}).get("environment") for r in d["rows"].values()
                   if r["kind"] == "test" and (r.get("context") or {}).get("environment")})
    if envs:
        out.append("- Environments in captured test runs: %s" % ", ".join(_line(e, 40) for e in envs[:5]))
    if d["observation"].get("environment"):
        out.append("- Developer-recorded environment (self-reported): %s" % _line(d["observation"]["environment"], 60))
    if d["db"] and d["db"].get("available") and d["db"].get("environment"):
        out.append("- Database observations: %s" % _line(d["db"]["environment"], 40))
    return out


def _steps(d):
    out = []
    for s in d["replay"][:LIST]:
        path = re.sub(r"^\{\{[^}]+\}\}", "", re.sub(r"^https?://[^/]+", "", str(s.get("url") or "")))
        out.append("%s. %s: %s %s%s" % (s.get("n"), _line(s.get("name"), 120) or "Saved request",
                                       _line(s.get("method"), 10) or "GET", _line(path, 160) or "(no path)",
                                       " (changes data)" if s.get("mutates") else ""))
    return out


def _expected(d):
    out = ["- Step %s: %s" % (s.get("n"), _line(s.get("expected"), 200)) for s in d["replay"] if s.get("expected")]
    out += ["- %s: %s" % (c["id"], _line(c["text"], 200)) for c in d["base"]["criteria"] if not c["excluded"]]
    return _more(out)


def _where(d):
    from . import companion
    obs = d["observation"]
    out = ["- Stage: %s; next: %s" % (views.SITUATIONS.get(d["situation"], d["situation"]),
                                      companion.NEXT.get(d["situation"], "Review the task"))]
    if obs:
        out.append("- Working tree (developer-recorded, self-reported, %s): HEAD %s, branch %s, %s." % (
            _line(obs.get("at"), 25), _line(obs.get("head"), 16) or "unknown", _line(obs.get("branch"), 80) or "unknown",
            obs.get("worktree") or "unknown"))
    if d["rec"].get("summary") and d["rec"]["summary"] != "No previous checkpoint":
        out.append("- Where you stopped (self-reported): %s" % _line(d["rec"]["summary"], 300))
    return out


def _continuation(d):
    g = d["groups"]
    out = []
    if g:
        for key, label in (("completed", "Carry forward (evidenced)"), ("pending", "Remaining"),
                           ("uncertain", "Uncertain"), ("revalidate", "Revalidate")):
            if g.get(key):
                out.append("- %s: %s" % (label, ", ".join(g[key][:15])))
    req = d["rec"].get("verification_request") or {}
    out += ["- Remaining check: %s" % _line(c, 200) for c in (req.get("checks") or [])[:5]]
    return out or ["- No approved plan steps to continue."]


def _database(d):
    db = d["db"]
    if not db or not db.get("available"):
        return []
    obs = db.get("observations") or []
    times = sorted(o.get("observed_at") for o in obs if o.get("observed_at"))
    out = ["- Flow: %s (environment %s, freshness %s)" % (_line(db.get("operation_label"), 120) or "unnamed",
                                                         _line(db.get("environment"), 20) or "unknown",
                                                         _line(db.get("freshness"), 20) or "unknown"),
           "- Code-supported path: %s" % (_line(db.get("coverage"), 200) or "not recorded"),
           "- Observed records: last evidenced step %s" % (_line(db.get("last_evidenced_step"), 200) or "none"),
           "- Observation window: %s" % ("%s to %s" % (times[0], times[-1]) if times else
                                         _line(db.get("updated_at"), 25) or "unknown")]
    out += ["  - %s: %s" % (_line(o.get("step"), 120), _line(o.get("status_label"), 60)) for o in obs[:3]]
    out.append("- Unknown boundary: %s" % (_line(db.get("largest_gap"), 240) or "none recorded"))
    if db.get("next_action"):
        out.append("- Next check: %s" % _line(db["next_action"], 300))
    return out


def _next(audience, d):
    prs = [p for p in d["rows"].values() if p["kind"] == "pr" and p.get("outcome") in ("open", "draft")]
    gaps = [r["criterion"] for r in d["ledger"]["rows"] if r["status"] != "Verified" and not r["excluded"]]
    if audience == "pr":
        out = ["- Review %s against the criteria above%s." % (
            "PR " + ", ".join(p["id"].rsplit(":", 1)[-1] for p in prs[:3]) if prs else "the changes",
            "; check the unverified ones first (%s)" % ", ".join(gaps[:8]) if gaps else "")]
    elif audience == "qa":
        out = ["- Check each expected outcome in the environments above and record what you observe in the Companion "
               "as a self-reported observation."]
    else:
        from . import companion
        out = ["- %s." % companion.NEXT.get(d["situation"], "Review the task")]
        if d["groups"] and d["groups"].get("revalidate"):
            out.append("- Revalidate first: %s." % ", ".join(d["groups"]["revalidate"][:10]))
    return out


def _missing(audience, d):
    base, out = d["base"], []
    if base["completeness"] != "complete":
        out.append("Requirements are %s: the cached Jira card may be truncated or criteria were left out."
                   % base["completeness"])
    out += [_line(g, 200) for g in ((base.get("coverage") or {}).get("gaps") or [])[:3]]
    if not d["approved"]:
        out.append("No approved plan.")
    kinds = {r["kind"] for r in d["rows"].values()}
    gaps = [r["criterion"] for r in d["ledger"]["rows"] if r["status"] != "Verified" and not r["excluded"]]
    if "test" not in kinds:
        out.append("No recorded test runs.")
    if gaps:
        out.append("No current applicable verifying evidence for: %s." % ", ".join(gaps[:12]))
    if "ci" not in kinds:
        out.append("CI checks unavailable.")
    if audience in ("pr", "session") and "pr" not in kinds:
        out.append("No pull request observed for %s." % d["repository"])
    if not d["observation"]:
        out.append("Working-tree state not recorded (Chitragupta does not inspect it).")
    uat = d["uat"]
    req = (uat.get("requirement") or {}).get("state")
    if req == "required":
        if not _evidenced(uat, "deployed"):
            out.append("UAT deployment of the current revision is unverified.")
        if not _evidenced(uat, "accepted"):
            out.append("UAT acceptance of the current revision is unverified.")
    elif req != "not_required":
        out.append("Whether UAT is required is not recorded.")
    if audience == "qa":
        if not d["replay"]:
            out.append("No reproducible steps recorded (no saved requests).")
        if not _environments(d):
            out.append("No configured environments recorded.")
    if audience in ("qa", "session") and not (d["db"] or {}).get("available"):
        out.append("Database context: %s." % (_line((d["db"] or {}).get("reason"), 120) or
                                               "no flow or investigation selected"))
    return out or ["Nothing missing in the recorded sources (they may still be partial)."]


def _evidenced(uat, kind):
    """Release tracking recorded `kind` (deployed / accepted) for the current UAT revision, not an earlier one."""
    rev = (uat.get("revision") or {}).get("rev")
    ev = uat.get(kind) or {}
    return bool(rev) and ev.get("state") == "yes" and (not ev.get("rev") or ev.get("rev") == rev)


def _delivery(d):
    """Positive delivery claims only with current evidence for the current UAT revision."""
    uat = d["uat"]
    return ["- %s for the current revision (evidence: %s)." % (word, _line((uat.get(kind) or {}).get("source"), 80)
                                                               or "recorded")
            for kind, word in (("deployed", "Deployed to UAT"), ("accepted", "Accepted in UAT"))
            if _evidenced(uat, kind)]


def render(audience, d):
    """(text, missing information, self-reported section names) of one deterministic template."""
    sections = [("Purpose", _purpose(d), False)]
    if audience == "qa":
        envs = _environments(d)
        steps = _steps(d)
        if envs:
            sections.append(("Configured environments", envs, True))
        if steps:
            sections.append(("Reproducible steps", steps, True))
        sections.append(("Expected outcomes", _expected(d), True))
    if audience == "session":
        sections.append(("Where it stands", _where(d), False))
    sections.append(("Verified changes", _verified(d) + _delivery(d), True))
    if audience == "session":
        sections.append(("Continuation", _continuation(d), True))
    sections.append(("Evidence", _evidence(d), True))
    if audience in ("pr", "session"):
        sections.append(("Decisions", _decisions(d), True))
    sections.append(("Unresolved risks and questions", _unresolved(d), True))
    sections.append(("Next action", _next(audience, d), False))
    db = _database(d)
    if db:
        sections.append(("Database context (cached; code-supported apart from observed; no rows or keys)", db, True))
    missing = _missing(audience, d)
    sections.append(("Missing information", ["- " + m for m in missing], False))
    labelled = [s for s, lines, _ in sections if any("self-reported" in ln.lower() for ln in lines)]
    head = ["%s draft · %s · %s" % (AUDIENCES[audience], d["ticket"], d["repository"]),
            "Generated from Companion records (source generation %s). Local text: nothing is sent; review and edit "
            "before sharing." % d["generation"]]

    def assemble():  # redaction-stable, so an unedited generated version is also a valid manual save
        out = list(head)
        for title, lines, _ in sections:
            out += ["", "## " + title] + lines
        return redact("\n".join(out))
    text = assemble()
    while len(text.encode("utf-8")) > TEXT_BYTES:
        cand = [i for i, (_, lines, trim) in enumerate(sections) if trim and len(lines) > 1]
        if not cand:
            break
        i = max(cand, key=lambda j: len("\n".join(sections[j][1])))
        title, lines, trim = sections[i]
        keep = lines[:max(1, len(lines) // 2)]
        sections[i] = (title, keep + (["- … more in the dashboard Companion page"]
                                      if not keep[-1].startswith("- … more") else []), len(keep) > 1)
        text = assemble()
    if len(text.encode("utf-8")) > TEXT_BYTES:
        text = text.encode("utf-8")[:TEXT_BYTES - 64].decode("utf-8", "ignore").rsplit("\n", 1)[0] + \
            "\n(cut at 8,000 bytes)"
    return text, missing, labelled


# ---------------------------------------------------------------- versions and actions

def _bucket(rec, audience):
    drafts = rec.setdefault("audience_drafts", {})
    return drafts.setdefault(audience, {"versions": [], "current": None})


def _add(rec, audience, origin, text, parent, generation, selection, missing, labelled):
    b = _bucket(rec, audience)
    v = {"audience": audience, "number": (b["versions"][-1]["number"] + 1) if b["versions"] else 1,
         "origin": origin, "parent": parent, "text": text, "source_generation": generation,
         "database": selection or None, "missing": [_line(m, 200) for m in missing[:20]], "self_reported": labelled,
         "template": TEMPLATE, "at": common.now_iso()}
    v["id"] = "d_" + model.digest(v)
    b["versions"].append(v)
    while len(b["versions"]) > VERSIONS:
        old = next((x for x in b["versions"] if x["origin"] == "generated" and x["id"] != b["current"]
                    and x["id"] != v["id"]), None)
        if old is None:
            raise ValueError("%d draft versions kept for this audience; earlier checkpoints keep the rest. Archive "
                             "this attempt or keep editing the current version." % VERSIONS)
        b["versions"].remove(old)  # still in the earlier immutable checkpoints
    return v


def _apply(wid, revision, fn, reason):
    from . import companion
    try:
        return companion._change(wid, revision, fn, reason)
    except Unchanged as same:
        return dict(companion.get(wid), notice=str(same))


def generate(wid, revision, audience, selection=None):
    """Draft Generate: a new generated version from current projections (no model). Your current edit stays
    current; the new version is kept beside it with a diff."""
    audience, selection = _audience(audience), _selection(selection)
    cfg = common.load_config()
    kept = []

    def change(rec):
        d = gather(rec, cfg, selection)
        b = _bucket(rec, audience)
        cur = next((v for v in b["versions"] if v["id"] == b["current"]), None)
        if cur and cur["origin"] == "generated" and cur["source_generation"] == d["generation"] \
                and (cur.get("database") or None) == selection:
            raise Unchanged("The %s draft already matches current evidence; nothing new to save." % AUDIENCES[audience])
        latest = next((v for v in reversed(b["versions"]) if v["origin"] == "generated"), None)
        if cur and cur["origin"] == "manual" and latest and latest["number"] > cur["number"] \
                and latest["source_generation"] == d["generation"] and (latest.get("database") or None) == selection:
            raise Unchanged("Generated version %d already matches current evidence; your edit stays current."
                            % latest["number"])
        text, missing, labelled = render(audience, d)
        v = _add(rec, audience, "generated", text, cur["id"] if cur else None, d["generation"], selection, missing,
                 labelled)
        if cur is None or cur["origin"] == "generated":
            b["current"] = v["id"]
        else:
            kept.append(v["number"])
    out = _apply(wid, revision, change, "%s draft generated (no model)" % AUDIENCES[audience])
    if kept and not out.get("notice"):
        out = dict(out, notice="Generated version %d. Your edit stays current; compare them in Drafts." % kept[0])
    return out


def save(wid, revision, audience, text, base=None):
    """Draft Save new version: your edit as a new immutable version (expected revision; 409 keeps your text)."""
    audience = _audience(audience)
    body = _text(text)
    if base is not None and (not isinstance(base, str) or not _DID.fullmatch(base)):
        raise ValueError("invalid base draft version")
    cfg = common.load_config()

    def change(rec):
        b = _bucket(rec, audience)
        parent = next((v for v in b["versions"] if v["id"] == base), None)
        if base is not None and parent is None:
            raise ValueError("the base version is not a draft of this audience")
        if parent and parent["text"] == body and b["current"] == base:
            raise Unchanged("No changes to save.")
        generation = parent["source_generation"] if parent else gather(rec, cfg)["generation"]
        v = _add(rec, audience, "manual", body, base, generation, (parent or {}).get("database"), [],
                 ["Your edit"])
        b["current"] = v["id"]
    return _apply(wid, revision, change, "%s draft edited" % AUDIENCES[audience])


def action(body):
    wid, rev = body.get("workflow"), body.get("revision")
    if body.get("action") == "draft_generate":
        sel = {k: body.get(k) for k in ("flow", "investigation") if body.get(k)}
        return generate(wid, rev, body.get("audience"), sel or None)
    if body.get("action") == "draft_save":
        return save(wid, rev, body.get("audience"), body.get("text"), body.get("base"))
    raise ValueError("unknown companion action")


# ---------------------------------------------------------------- read projection

def version(rec, vid):
    """One saved version's export text (for viewing or editing an older version); None when absent."""
    for b in (rec.get("audience_drafts") or {}).values():
        for v in b.get("versions") or []:
            if v["id"] == vid:
                return {"id": v["id"], "audience": v["audience"], "number": v["number"], "origin": v["origin"],
                        "origin_label": ORIGIN_LABEL[v["origin"]], "parent": v.get("parent"), "at": v.get("at"),
                        "text": export(v["text"]), "current": b.get("current") == v["id"]}
    return None


def view(rec, cfg, selection=None):
    """The `drafts` projection body: per audience the current version (with `Source evidence changed` when its
    sources moved), a regenerated version kept beside your edit with its diff, the version list, or a preview."""
    from . import companionplanning as planning
    selection = _selection(selection)
    cache = {}

    def gen(sel):
        key = repr(sorted((sel or {}).items()))
        if key not in cache:
            cache[key] = gather(rec, cfg, sel)
        return cache[key]
    now = gen(selection)
    out = []
    for aud, label in AUDIENCES.items():
        b = (rec.get("audience_drafts") or {}).get(aud) or {"versions": [], "current": None}
        vs = b.get("versions") or []
        cur = next((v for v in vs if v["id"] == b.get("current")), None)
        entry = {"id": aud, "label": label, "versions_total": len(vs), "current": None, "regenerated": None,
                 "preview": None,
                 "versions": [{"id": v["id"], "number": v["number"], "origin": v["origin"],
                               "origin_label": ORIGIN_LABEL[v["origin"]], "at": v.get("at"), "parent": v.get("parent"),
                               "current": v["id"] == b.get("current")} for v in reversed(vs)][:10]}
        if cur:
            stale = cur["source_generation"] != gen(cur.get("database"))["generation"]
            entry["current"] = {"id": cur["id"], "number": cur["number"], "origin": cur["origin"],
                                "origin_label": ORIGIN_LABEL[cur["origin"]], "parent": cur.get("parent"),
                                "at": cur.get("at"), "source_generation": cur["source_generation"],
                                "database": cur.get("database"), "text": export(cur["text"]),
                                "missing": cur.get("missing") or [], "self_reported": cur.get("self_reported") or [],
                                "stale": stale, "stale_label": STALE if stale else None}
            latest = next((v for v in reversed(vs) if v["origin"] == "generated"), None)
            if cur["origin"] == "manual" and latest and latest["number"] > cur["number"]:
                entry["regenerated"] = {"id": latest["id"], "number": latest["number"], "at": latest.get("at"),
                                        "diff": planning.diff(cur, latest, lines=120, size=3000),
                                        "label": "Generated version %d is kept beside your edit; nothing was "
                                                 "overwritten" % latest["number"]}
        else:
            text, missing, labelled = render(aud, now)
            entry["preview"] = {"text": export(text), "missing": missing, "self_reported": labelled,
                                "source_generation": now["generation"],
                                "label": "Preview (not saved): Generate draft keeps it as a version"}
        out.append(entry)
    return {"audiences": out, "source_generation": now["generation"], "decisions": now["knowledge"],
            "note": "Local drafts; nothing is sent. Copy re-reads this view, which re-checks your access and removes "
                    "tokens, email addresses and local absolute paths."}
