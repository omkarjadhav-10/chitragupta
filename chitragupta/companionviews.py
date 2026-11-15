"""Bounded, read-only Companion projections (ARCH-03) shared by the dashboard, CLI, MCP bridge and native mod.

One envelope, one allowlisted set of operations. Reads only cached canonical records: no collection, refresh,
model, provider, process, database query, job enqueue, implicit workflow creation or cursor acknowledgement. Every
read is scoped by the current profile and rechecked after reading, so an old-generation result never republishes
revoked content. Envelopes stay within 64 KiB and 50 items; longer content pages through HMAC-signed cursors bound
to the profile generation, the content generation and the view.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re

from . import brief, common, companion, jev, profile, workflowmodel as model, workflowstore
from .redact import redact

SCHEMA = 1
MAX_BYTES = 65536
PAGE_SIZE = 50
DB_BYTES = 8192
INPUT_BYTES = 8192
TEXT_PAGE = 8000
PACKET_PAGE = 24000
OPERATIONS = ("snapshot", "planner", "detail", "review", "plan", "changes", "verification", "jobs", "handoff",
              "database", "drafts")
ARGS = {"operation", "workflow", "ticket", "plan", "base", "offset", "cursor", "flow", "investigation", "privacy",
        "pinned", "group"}
GROUP_LABELS = {"new": "New", "planned": "Planned", "review_incomplete": "Review incomplete",
                "in_development": "In development", "paused": "Paused", "awaiting_verification": "Testing",
                "pr_review": "PR review", "completed": "Completed"}
SITUATIONS = {"new": "New task", "A": "Plan saved; review interrupted", "handoff": "Approved; handoff prepared",
              "B": "Interrupted development", "C": "Implementation done; testing remains", "D": "Existing PR or review",
              "completed": "Completed"}
NEXT_LABEL = {"new": "Generate Plan", "A": "Review saved plan", "handoff": "Copy approved plan",
              "B": "Review continuation", "C": "Verify Requirements", "D": "Open PR Review",
              "completed": "View completion evidence"}
RECOVERY_LABEL = {"repository_missing": "Resolve repository location", "location_moved": "Confirm repository location",
                  "different_repository": "Resolve repository identity", "detached_head": "Confirm intended branch",
                  "branch_deleted": "Resolve deleted branch", "requirements_changed": "Review changed requirements",
                  "execution_status_unknown": "Record handoff outcome", "blocked": "Resolve blocker",
                  "stale": "Review changed requirements"}
REMINDER_LABEL = {"mapping": "Resolve release mapping", "release-ambiguous": "Resolve release mapping",
                  "uat-unknown": "Record whether UAT is required", "uat-missing": "View UAT evidence",
                  "uat-verify": "View UAT evidence", "uat-deployed": "View UAT evidence",
                  "uat-accepted": "View UAT evidence"}
CONNECTION = {"ok": "Connected", "stale": "Stale", "partial": "Partial", "failing": "Failing", "never": "Not read yet",
              "off": "Not configured"}
RECOVERY_HINT = {"ok": "", "stale": "Open Health to see why the background run is behind",
                 "partial": "Open Health for the missing part; earlier results are kept",
                 "failing": "Run /cg:check or open Health; earlier results are kept",
                 "never": "Waits for the next background run", "off": "Turn it on in Setup"}
HIDDEN = "Title hidden (privacy mode)"
_WID = re.compile(r"^w_[a-f0-9]{64}$")
_PID = re.compile(r"^p_[a-f0-9]{64}$")
_KEY = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
_DBID = {"flow": re.compile(r"^f_[0-9a-f]{8,40}$"), "investigation": re.compile(r"^i_[0-9a-f]{8,40}$")}
_URL = re.compile(r"(?i)\b(?:https?|ftp|file)://\S+")
_ABS = re.compile(r"(?<![\w.])(?:[A-Za-z]:[\\/]|\\\\[\w.$]|/(?:home|Users|usr|etc|var|opt|tmp|mnt|root|srv)/)\S*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SLIM = {}   # wid -> (head stat, slim record): checkpoints are immutable, so a changed head is the only change
_JIRA = {}   # path -> (stat, {"card", "record"})


def safe(value, limit=300):
    return re.sub(r"[\x00-\x1f\x7f]", " ", redact(str(value or "")))[:limit]


def scrub(value, limit=300):
    """Overview text: no URLs, absolute paths or emails."""
    return _EMAIL.sub("[email]", _ABS.sub("[path]", _URL.sub("[link]", safe(value, 4 * limit))))[:limit]


def _size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def delivery_link(key):
    """The ticket's delivery details: the release plan and per-repository base, PR target, QA and UAT live on the
    ticket page's Branches & PRs tab (`tab=branches`; the ticket page has no `delivery` tab)."""
    return "#/tickets/%s?tab=branches" % key


def _repo(location):
    return safe(str(location or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1], 80) or "repository"


# ---------------------------------------------------------------- generations, cursors, envelope

def generations(cfg):
    """(profile_id, profile_generation, scope_generation, timezone, identity confirmed)."""
    p = profile.load(cfg)
    who = sorted(list(profile.account_key(a)) for a in p["accounts"]) + [["alias", a] for a in p["aliases"]]
    scope = {"repos": sorted(common.norm_path(r) for r in p["repos"]), "projects": sorted(p["projects"]),
             "include": sorted(p["include"]), "jira": sorted(a.get("instance", "") for a in p["jira_accounts"])}
    return ("pf_" + model.digest(who)[:16], profile.signature(cfg), "sg_" + model.digest(scope)[:16],
            p["settings"].get("timezone") or "local", bool(p["aliases"] or p["accounts"]))


def _key():
    """The per-home cursor key (dbviews' secret) when one exists, else a stable per-home binding key. Pages are
    re-scoped on every read, so the MAC binds a cursor to its view and generations; it never grants access."""
    try:
        from . import dbviews
        if os.path.isfile(dbviews.secret_path()):
            return dbviews._secret()
    except (ImportError, OSError, ValueError):
        pass
    return hashlib.sha256(("chitragupta-companion-cursor|" + common.norm_path(common.home())).encode()).digest()


def make_cursor(view, ref, offset, generation, content):
    body = json.dumps([view, ref, offset, generation, content], separators=(",", ":")).encode()
    token = common_b64(body)
    return token + "." + hmac.new(_key(), token.encode(), hashlib.sha256).hexdigest()[:32]


def common_b64(raw):
    import base64
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_cursor(token, view, ref, generation, content):
    """Offset of a valid cursor; a malformed, foreign, stale or other-view cursor asks for a reload."""
    import base64
    if token in (None, ""):
        return 0
    bad = ValueError("stale or invalid cursor; reload the view")
    if not isinstance(token, str) or len(token) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[0-9a-f]{32}", token):
        raise bad
    body, mac = token.rsplit(".", 1)
    if not hmac.compare_digest(mac, hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:32]):
        raise bad
    try:
        v, r, offset, gen, cont = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        raise bad from None
    if [v, r, gen, cont] != [view, ref, generation, content] or isinstance(offset, bool) \
            or not isinstance(offset, int) or not 0 <= offset <= 1000000:
        raise bad
    return offset


def _utc(moment):
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def day_view(cfg, moment=None):
    """(work day, its [start, end) in UTC with its length in hours, the moment in the profile zone): the same
    profile.day_of / day_bounds the progress and today views use, so a UTC midnight or a DST change agrees."""
    moment = moment or common.now()
    zone = profile.tzinfo(cfg)
    day = profile.day_of(moment, cfg, zone)
    start, end = profile.day_bounds(day, cfg, zone)
    hours = (end.astimezone(dt.timezone.utc) - start.astimezone(dt.timezone.utc)).total_seconds() / 3600
    return day, {"start": _utc(start), "end": _utc(end), "hours": hours}, \
        profile.in_zone(moment, zone).isoformat(timespec="seconds")


def _envelope(operation, cfg, privacy=False):
    pid, gen, scope, tz, _ = generations(cfg)
    moment = common.now()
    day, bounds, local = day_view(cfg, moment)
    return {"schema_version": SCHEMA, "operation": operation, "profile_id": pid, "profile_generation": gen,
            "scope_generation": scope, "generated_at": _utc(moment), "generated_local": local, "day": day,
            "day_bounds": bounds, "timezone": safe(tz, 60), "privacy": bool(privacy)}


def _finish(out, shrink=()):
    """Bound the envelope, then recheck the profile generation: a concurrent profile edit publishes nothing."""
    for step in shrink:
        if _size(out) <= MAX_BYTES:
            break
        step(out)
    if _size(out) > MAX_BYTES:
        raise ValueError("%s exceeds the bounded projection size" % out.get("operation", "projection"))
    if profile.signature(common.load_config()) != out["profile_generation"]:
        raise PermissionError("profile changed during read; reload")
    body = {k: v for k, v in out.items() if k not in ("generated_at", "generated_local", "snapshot_id")}
    out["snapshot_id"] = "s_" + model.digest(body)[:24]
    return out


def _page(rows, offset, build, limit=PAGE_SIZE):
    """(page rows, count) of at most `limit` rows that keep the envelope `build(rows)` within MAX_BYTES."""
    n = min(limit, max(0, len(rows) - offset))
    while n > 1 and _size(build(rows[offset:offset + n])) > MAX_BYTES:
        n = max(1, n // 2)
    return rows[offset:offset + n], n


# ---------------------------------------------------------------- cached, authorized workflow index

def _slim(rec):
    """What the planner and summaries need: never plan text, evaluations or evidence."""
    base = rec["baselines"][rec["baseline"]]
    return {"id": rec["id"], "binding": dict(rec["binding"]), "stage": rec["stage"], "revision": rec["revision"],
            "archived": rec.get("archived"), "baseline": rec["baseline"], "baselines": {rec["baseline"]: base},
            "overlays": dict(rec.get("overlays") or {}), "jobs": dict(rec.get("jobs") or {}),
            "updated_at": rec.get("updated_at"), "selected_plan": rec.get("selected_plan"),
            "approved_plan": rec.get("approved_plan"), "next_action": rec.get("next_action"),
            "owner": rec.get("owner"), "resume_stage": rec.get("resume_stage"), "plans": len(rec.get("plans") or {})}


def visibility(cfg, any_generation=False):
    """companion.authorize's four checks (profile generation, ticket in the personal projection, repository in scope,
    authorized Jira instance) against one scope index built once per read, instead of once per workflow.
    tests/test_companion_views.BatchAuthorization keeps it equal to companion.authorize. `any_generation` drops the
    generation check only, to count what an explicit reconciliation would re-bind."""
    from .dashboard import api
    sig = profile.signature(cfg)
    scope = api.scope_index(cfg)
    pol = profile.Policy(cfg)
    instances = {a.get("instance", "") for a in pol.p["jira_accounts"]}

    def ok(binding):
        return bool((any_generation or binding.get("profile_generation") == sig)
                    and (scope.get(binding.get("ticket")) or {}).get("visible")
                    and pol.repo_in_scope(binding.get("location")) and binding.get("jira_instance") in instances)
    return ok


def _slims():
    """Every workflow's slim record, cached per head (checkpoints are immutable)."""
    from .dashboard import api
    for wid in workflowstore.ids():
        stat = api._stat_key(os.path.join(workflowstore.root(wid), "head.json"))
        if stat is None:
            continue
        hit = _SLIM.get(wid)
        if hit and hit[0] == stat:
            yield hit[1]
            continue
        try:
            rec = workflowstore.load(wid)
        except ValueError:
            continue
        if rec:
            _SLIM[wid] = (stat, _slim(rec))
            yield _SLIM[wid][1]


def reconcilable(cfg):
    """How many tasks were saved under an earlier profile generation that the current profile still authorizes in
    full (ticket, repository, Jira instance): an explicit reconciliation re-binds them. Counted, never shown."""
    sig = profile.signature(cfg)
    fits = visibility(cfg, any_generation=True)
    return sum(1 for s in _slims() if s["binding"].get("profile_generation") != sig and not s.get("archived")
               and fits(s["binding"]))


def index(cfg, hidden=None):
    """{ticket: [slim workflow]} visible under the current profile; `hidden` collects per-ticket counts of the
    workflows this profile may not see (counted, never shown)."""
    out = {}
    visible = visibility(cfg)
    for slim in _slims():
        if not visible(slim["binding"]):
            if hidden is not None:
                hidden[slim["binding"]["ticket"]] = hidden.get(slim["binding"]["ticket"], 0) + 1
            continue
        out.setdefault(slim["binding"]["ticket"], []).append(slim)
    return out


def rollup(cfg, key):
    """companion.rollup over the cached index: per-repository workflows of one ticket; hidden ones only counted."""
    hidden = {}
    out = model.rollup(index(cfg, hidden).get(key) or [])
    out["hidden"] = hidden.get(key, 0)
    if out["hidden"]:
        out["label"] += "; %d repository workflow not visible in this profile" % out["hidden"]
    return out


def _jira(key):
    """brief.jira_card plus the raw cached record, re-read only when the file changes."""
    from .dashboard import api
    path = common.hp("data", "jira", key + ".json")
    stat = api._stat_key(path)
    hit = _JIRA.get(path)
    if hit and hit[0] == stat:
        return hit[1]
    value = {"card": brief.jira_card(key) if stat else {}, "record": common.read_json(path, {}) or {} if stat else {}}
    if len(_JIRA) > 4000:
        _JIRA.clear()
    _JIRA[path] = (stat, value)
    return value


def _cards(cfg, idx, pinned=()):
    from . import companionplanning as planning
    from .dashboard import api
    rows = api.tickets(cfg=cfg)["tickets"]
    records = api._ticket_records()
    # a ticket that is no longer yours (reassigned, someone else's) is history, not work to plan: it stays listed only
    # while you have a task on it
    plannable = [r for r in rows if idx.get(r["key"]) or (r.get("ownership") or {}).get("state")
                 not in ("reassigned", "other", "outside")]
    keys = [r["key"] for r in plannable]
    return planning.cards(plannable, {k: _jira(k) for k in keys}, {k: idx.get(k) or [] for k in keys},
                          {k: records.get(k) or {} for k in keys}, pinned, planning.criteria_fields(cfg)), rows


def _card_view(card, privacy):
    out = {k: card[k] for k in ("ticket", "group", "stage", "workflow", "action", "pinned", "priority", "jira_status",
                                "due", "criteria", "history", "branches", "prs", "dependencies", "blockers",
                                "freshness", "gaps", "reconstruction")}
    out.update(id="card:" + card["ticket"], source={"type": "ticket record + cached Jira card",
                                                     "ref": "tickets:" + card["ticket"]},
               title=HIDDEN if privacy else card["title"], why=scrub(card["why"], 300),
               group_label=GROUP_LABELS.get(card["group"], card["group"]),
               attempts=[dict(a) for a in card["attempts"]][:10],
               coverage={"jira": "partial" if card["freshness"]["state"] == "cached" else "unavailable",
                         "ticket": "cached"},
               freshness=dict(card["freshness"], state="cached" if card["freshness"]["fetched_at"] else "unknown"))
    return out


# ---------------------------------------------------------------- snapshot (persistent companion)

def _sources(cfg):
    out = []
    try:
        conn = profile.connections(cfg)
    except Exception:  # noqa: BLE001 - one broken ledger never blanks the other sources
        conn = {"git": {"state": "never", "at": ""}, "jira": {"state": "never", "at": ""}, "hosts": {}}
    rows = [("git", "Git commits", conn.get("git") or {}), ("jira", "Jira", conn.get("jira") or {})]
    rows += [("host:%s:%d" % (safe(k[0], 20), i), "%s pull requests" % safe(k[0], 20), v)
             for i, (k, v) in enumerate(sorted((conn.get("hosts") or {}).items()))]
    for sid, label, c in rows:
        state = c.get("state") or "never"
        at = c.get("at") or None
        out.append({"id": sid, "label": label, "state": CONNECTION.get(state, "Unknown"),
                    "observed_at": at, "success_at": at if state in ("ok", "stale", "partial") else None,
                    "completeness": "complete" if state == "ok" else "partial" if state == "partial" else
                    "not_configured" if state == "off" else "unavailable" if state in ("failing", "never") else "partial",
                    "freshness": "current" if state in ("ok", "partial") else "stale" if state == "stale" else "unknown",
                    "access": "denied" if state == "failing" else "configured" if state != "off" else "not_configured",
                    "recovery": RECOVERY_HINT.get(state, "Open Health")})
    out.append({"id": "jira_cards", "label": "Cached Jira cards", "state": "Partial", "observed_at": None,
                "success_at": None, "completeness": "partial", "freshness": "unknown", "access": "configured",
                "recovery": "Descriptions are capped at 3,000 characters and ten comments; requirements may be incomplete"})
    out.append({"id": "jev", "label": "Jev evaluation", "state": "Unavailable", "observed_at": None, "success_at": None,
                "completeness": "unavailable", "freshness": "unknown", "access": "blocked",
                "recovery": jev.UNAVAILABLE + "; manual review stays available"})
    return out


def capabilities(cfg):
    from . import companionjobs, modelbudget
    ceiling, why = modelbudget.call_ceiling(cfg, "companion_plan")
    jev_why = jev.preflight(cfg, companionjobs.acknowledgement()) or jev.UNAVAILABLE
    return {"generate_plan": {"available": ceiling is not None, "reason": why},
            "improve_plan": {"available": ceiling is not None,
                             "reason": why + "; a manual new version stays available" if why else ""},
            "manual_plan": {"available": True, "reason": ""},
            "jev": {"available": False, "reason": jev_why},
            "direct_execution": {"available": False, "reason": "Direct session dispatch is unavailable; use the "
                                                               "reviewed copy (Prepared; not submitted)"},
            "copy_handoff": {"available": True, "reason": ""},
            "database_context": {"available": True, "reason": "Cached flows and investigations only; never queries"},
            "mutations": {"available": False, "reason": "Read-only projection; act in the dashboard Companion page"}}


def _slug(value, limit=60):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-")[:limit] or "none"


def _attention_items(cfg, rows, idx):
    """(items, left out): one item per stable source ID, with the generation of its material evidence (FLOW-07), from
    the same ticket alerts/reminders get_alerts reads plus workflow overlays. A delivery reminder about UAT carries
    the UAT revision it refers to, so a new commit reopening UAT is a new generation. Repeated reads never multiply."""
    from .dashboard import api
    yours = [t for t in rows if t["ownership"]["state"] not in ("reassigned", "other", "outside")
             and (t.get("scope") or {}).get("basis") != "context"]
    records = api._ticket_records()
    found = {}

    def add(iid, material, kind, ticket, label, text, source):
        if iid not in found:
            found[iid] = {"id": iid, "generation": model.digest(material)[:16], "kind": kind, "ticket": ticket,
                          "label": label, "text": scrub(text, 240), "source": source}
    for t in yours:
        alerts = {}
        for a in t["alerts"]:
            alerts.setdefault((a.get("kind") or "alert", a.get("repo") or ""), []).append(a)
        for (kind, repo), group in sorted(alerts.items()):
            add("alert:%s:%s:%s" % (t["key"], _slug(kind, 40), _slug(repo)), sorted(str(a.get("text")) for a in group),
                "branch", t["key"], "Review branch alert",
                str(group[0].get("text") or "") + (" (and %d more)" % (len(group) - 1) if len(group) > 1 else ""),
                "branch tracking")
        grouped = {}
        for r in t.get("reminders") or []:
            label = REMINDER_LABEL.get(r["kind"], "Review delivery reminder")
            grouped.setdefault(label, []).append(r)
        rel = (records.get(t["key"]) or {}).get("release")
        uat = rel.get("uat") if isinstance(rel, dict) and isinstance(rel.get("uat"), dict) else {}
        for label, rs in sorted(grouped.items()):
            repos = sorted({r["repo"] for r in rs if r.get("repo")})
            about_uat = any(str(r.get("kind") or "").startswith("uat") for r in rs)
            add("release:%s:%s" % (t["key"], _slug(label)),
                [sorted([r["id"], r["kind"], r.get("severity"), r["text"]] for r in rs),
                 (uat.get("revision") or {}).get("rev") if about_uat else None],
                "release", t["key"], label, rs[0]["text"] + (" (%d repositories)" % len(repos) if len(repos) > 1 else ""),
                "release tracking")
    for key, recs in idx.items():
        for r in recs:
            if r.get("archived"):
                continue
            for name in ("blocked", "stale", "execution_status_unknown"):
                o = (r.get("overlays") or {}).get(name)
                if isinstance(o, dict):
                    add("workflow:%s:%s" % (r["id"][:14], name), [o.get("reason"), o.get("at")], "workflow", key,
                        RECOVERY_LABEL.get(name, "Review task"), o.get("reason"), "task workflow")
            unknown = sorted(k for k, j in (r.get("jobs") or {}).items()
                             if isinstance(j, dict) and j.get("status") == "outcome_unknown")
            if unknown:
                add("job:%s" % r["id"][:14], unknown, "job", key, "Review job outcome", "Plan job outcome unknown",
                    "local job")
    left = sum(1 for t in rows if t not in yours and (t["alerts"] or t.get("reminders")))
    return list(found.values()), left


def attention_items(cfg):
    """The attention items recorded for your profile now (an acknowledge/snooze action checks its item against
    these, so an item outside your scope or one that changed since it was shown cannot be acted on)."""
    from .dashboard import api
    return _attention_items(cfg, api.tickets(cfg=cfg)["tickets"], index(cfg))[0]


def _attention(cfg, rows, idx, pid):
    """Bounded attention summary honouring your acknowledgements and snoozes (presentation only): active items
    first (required ones first), then hidden required items, labelled; hidden optional items are only counted."""
    from . import companionattention as att
    raw, left = _attention_items(cfg, rows, idx)
    shown = att.present(raw, pid)
    active = sorted((i for i in shown if i["presentation"]["state"] == "active"), key=lambda i: not i["required"])
    hidden = [i for i in shown if i["presentation"]["state"] != "active"]
    listed = active + [dict(i, text=scrub("%s. %s" % (i["presentation"]["label"], i["text"]), 240))
                       for i in hidden if i["required"]]
    kinds = {}
    for i in shown:
        kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
    count = lambda state: sum(1 for i in hidden if i["presentation"]["state"] == state)  # noqa: E731
    return {"total": len(active), "recorded": len(shown), "active": len(active),
            "hidden": {"acknowledged": count("acknowledged"), "snoozed": count("snoozed"), "total": len(hidden),
                       "listed": sum(1 for i in hidden if i["required"])},
            "items": listed[:10], "more": len(listed) > 10, "initial": att.INITIAL, "by_kind": kinds, "left_out": left,
            "semantics": "Recorded alerts and reminders. Acknowledging or snoozing changes only what is shown here, "
                         "never delivery or test state; unread or unavailable sources may hide more",
            "coverage": "partial", "snooze": [{"id": k, "label": v} for k, v in att.SNOOZE_LABEL.items()]}


def _select(args, cfg, idx):
    """Session-local context: explicit > evidenced (an active execution owner) > ambiguous > absent. A ticket-like
    name in a branch is never evidence."""
    from .dashboard import api
    wid, key = args.get("workflow"), args.get("ticket")
    flat = {r["id"]: r for recs in idx.values() for r in recs}
    if wid:
        if wid in flat:
            return {"state": "explicit", "ticket": flat[wid]["binding"]["ticket"], "workflow": wid, "candidates": []}
        return {"state": "absent", "reason": "The selected task is unavailable in this profile", "candidates": []}
    if key:
        if not api.scope_of(key, cfg).get("visible"):
            return {"state": "absent", "reason": "The selected ticket is unavailable in this profile", "candidates": []}
        recs = [r for r in idx.get(key) or [] if not r.get("archived")]
        if len(recs) > 1:
            return {"state": "ambiguous", "ticket": key, "workflow": None, "reason": "Several attempts; choose one",
                    "candidates": [{"workflow": r["id"], "ticket": key, "stage": r["stage"],
                                    "repository": _repo(r["binding"]["location"]),
                                    "evidence": "workflow attempt updated %s" % (r.get("updated_at") or "unknown")}
                                   for r in recs][:10]}
        return {"state": "explicit", "ticket": key, "workflow": recs[0]["id"] if recs else None, "candidates": []}
    owned = []
    for recs in idx.values():
        for r in recs:
            own = companion.owner_state(r)
            if own and own["state"] == "active" and not r.get("archived"):
                owned.append((r, own))
    if len(owned) == 1:
        r, own = owned[0]
        return {"state": "evidenced", "ticket": r["binding"]["ticket"], "workflow": r["id"], "candidates": [],
                "evidence": "Observed execution activity associated by you (%s)" % safe(own.get("observed_at"), 40)}
    if owned:
        return {"state": "ambiguous", "ticket": None, "workflow": None,
                "reason": "Several tasks have observed execution activity; choose one",
                "candidates": [{"workflow": r["id"], "ticket": r["binding"]["ticket"], "stage": r["stage"],
                                "repository": _repo(r["binding"]["location"]),
                                "evidence": "observed execution activity %s" % safe(o.get("observed_at"), 40)}
                               for r, o in owned][:10]}
    return {"state": "absent", "reason": "No task selected", "candidates": []}


def _workflow_context(wid):
    from . import companionevidence as evidence
    res = companion.resume(wid)
    rec = companion.get(wid)
    cov = res["measures"]["plan_coverage"] or {}
    blocking = [r for r in res["recovery"] if r["blocking"]]
    return res, {
        "workflow": wid, "revision": res["revision"], "stage": res["durable_stage"], "resume_stage": res["stage"],
        "situation": res["situation"], "situation_label": SITUATIONS.get(res["situation"], res["situation"]),
        "selected_plan": rec["selected_plan"], "approved_plan": rec["approved_plan"],
        "checkpoint": {"id": "r%d" % rec["revision"], "revision": rec["revision"], "at": rec.get("updated_at"),
                       "ingested_at": rec.get("ingested_at")},
        "coverage": {"state": cov.get("state"), "percent": cov.get("percent"), "label": cov.get("label") or
                     "Evaluation unavailable", "baseline": cov.get("baseline") or rec["baseline"],
                     "reason": safe(cov.get("reason"), 200) or None},
        "freshness": {"state": "unknown" if res["delta"]["unprocessed_total"] else "current",
                      "at": rec.get("ingested_at"), "unprocessed": res["delta"]["unprocessed_total"]},
        "progress": res["measures"]["implementation_progress"],
        "verification": {k: res["measures"]["functional_verification"][k] for k in ("percent", "numerator", "denominator")},
        "owner": (res.get("ownership") or {}).get("label") or "No execution owner",
        "actions": res["available_actions"], "unread": res["unread_total"],
        "recovery": [{"state": r["state"], "label": RECOVERY_LABEL.get(r["state"], "Review"), "blocking": r["blocking"]}
                     for r in res["recovery"]][:5],
        "blocking": bool(blocking), "unread_limit": evidence.UNREAD_LIMIT}


def _next_action(sel, res, wctx, cards, attention):
    if res is not None:
        blocking = [r for r in res["recovery"] if r["blocking"]]
        if blocking:
            r = blocking[0]
            return {"label": RECOVERY_LABEL.get(r["state"], "Resolve recovery"), "text": safe(r["action"], 300),
                    "kind": "recovery", "evidence": ["recovery:" + r["state"]], "ticket": sel["ticket"],
                    "workflow": sel["workflow"]}
        rel = [a for a in attention["items"] if a["ticket"] == sel["ticket"] and a["kind"] == "release"
               and a["label"] == "Resolve release mapping"]
        if rel:
            return {"label": "Resolve release mapping", "text": rel[0]["text"], "kind": "delivery",
                    "evidence": [rel[0]["id"]], "ticket": sel["ticket"], "workflow": sel["workflow"],
                    "target": delivery_link(sel["ticket"])}
        if res["unread_total"] and not res["delta"]["first_use"]:
            return {"label": "Review Changes", "text": "%d change(s) since your last review" % res["unread_total"],
                    "kind": "delta", "evidence": ["delta:%d" % res["unread_total"]], "ticket": sel["ticket"],
                    "workflow": sel["workflow"]}
        return {"label": NEXT_LABEL.get(res["situation"], "Resume Task"), "text": safe(res["next_action"], 300),
                "kind": "workflow", "evidence": ["checkpoint:r%d" % wctx["revision"]], "ticket": sel["ticket"],
                "workflow": sel["workflow"]}
    if sel.get("state") == "ambiguous":
        return {"label": "Choose a task", "text": sel.get("reason") or "", "kind": "selection",
                "evidence": [c["workflow"] for c in sel["candidates"]][:5], "ticket": None, "workflow": None}
    if cards:
        c = cards[0]
        return {"label": "%s %s" % (c["action"], c["ticket"]), "text": scrub(c["why"], 300), "kind": "planner",
                "evidence": ["card:" + c["ticket"]], "ticket": c["ticket"], "workflow": c["workflow"]}
    return None


def snapshot(args=None):
    """The persistent companion summary (Focus / Evidence / Attention) plus the first planner items."""
    from .dashboard import api
    args = args or {}
    cfg = common.load_config()
    privacy = _privacy(args, cfg)
    out = _envelope("snapshot", cfg, privacy)
    confirmed = generations(cfg)[4]
    out.update(context={"state": "absent", "reason": "No task selected", "candidates": []},
               database_context=database_context(cfg, args), workflow_context=None, sources=_sources(cfg),
               capabilities=capabilities(cfg), focus=None, next_action=None, items=[], more=False, cursor=None,
               attention_summary={"total": 0, "recorded": 0, "active": 0, "items": [], "more": False, "by_kind": {},
                                  "hidden": {"acknowledged": 0, "snoozed": 0, "total": 0, "listed": 0},
                                  "initial": 3, "left_out": 0, "semantics": "Nothing read",
                                  "coverage": "unavailable", "snooze": []},
               empty=None, state="ready", reconcile=None)
    if not confirmed:
        out.update(state="identity_unconfirmed", empty="Confirm your identity and scope in Setup. Nothing of yours "
                                                       "is shown until then.",
                   next_action={"label": "Open profile setup", "text": "Confirm your commit email and accounts",
                                "kind": "setup", "evidence": [], "ticket": None, "workflow": None,
                                "target": "#/profile?section=identity"})
        return _finish(out)
    idx = index(cfg)
    stale = reconcilable(cfg)
    out["reconcile"] = {"count": stale, "label": "%d task%s saved before your profile changed still fit%s it: "
                                                 "reconcile to show %s again" % (
                            stale, "" if stale == 1 else "s", "s" if stale == 1 else "", "it" if stale == 1 else "them"),
                        "note": "Tasks outside your profile stay hidden and unchanged"} if stale else None
    planner_data, rows = _cards(cfg, idx, _pinned(args))
    cards = planner_data["items"]
    gen = _content(out["profile_generation"], "all", cards)
    page, n = _page(cards, 0, lambda rs: dict(out, items=[_card_view(c, privacy) for c in rs]), 20)
    out["items"] = [_card_view(c, privacy) for c in page]
    out["more"] = n < len(cards)
    out["cursor"] = make_cursor("planner:all", "", n, out["profile_generation"], gen) if out["more"] else None
    out["attention_summary"] = _attention(cfg, rows, idx, out["profile_id"])
    sel = _select(args, cfg, idx)
    out["context"] = sel
    res = None
    if sel.get("workflow"):
        res, out["workflow_context"] = _workflow_context(sel["workflow"])
    if sel.get("ticket"):
        row = next((r for r in rows if r["key"] == sel["ticket"]), None) or {}
        facts = []
        if row.get("release"):
            facts.append("Release: " + scrub(row["release"].get("label"), 120))
        if res is not None:
            facts.append("Stage: " + GROUP_LABELS.get(res["durable_stage"], res["durable_stage"].replace("_", " ")))
            if res["unread_total"]:
                facts.append("%d change(s) since your last review" % res["unread_total"])
        out["focus"] = {"ticket": sel["ticket"], "title": HIDDEN if privacy else safe(row.get("title") or
                                                                                       row.get("title_hint"), 200),
                        "repository": _repo(next((r["binding"]["location"] for r in idx.get(sel["ticket"], [])
                                                  if r["id"] == sel.get("workflow")), "")) if sel.get("workflow") else None,
                        "facts": facts[:3], "jira_status": safe(row.get("jira_status"), 60) or "Unavailable",
                        "links": {"ticket": "#/tickets/" + sel["ticket"], "delivery": delivery_link(sel["ticket"]),
                                  "companion": "#/companion?w=%s" % (sel.get("workflow") or "")}}
    out["next_action"] = _next_action(sel, res, out["workflow_context"], cards, out["attention_summary"])
    if not rows:
        out.update(state="empty", empty="No recorded work yet. Work in a repository in your scope with Claude Code; "
                                        "captured tickets, plans and evidence appear here.",
                   next_action={"label": "Open setup", "text": "Check capture and scope", "kind": "setup",
                                "evidence": [], "ticket": None, "workflow": None, "target": "#/setup"})
    return _finish(out, (lambda o: o.update(items=o["items"][:10]),
                         lambda o: o["attention_summary"].update(items=o["attention_summary"]["items"][:3])))


def _content(generation, group, pool):
    """Content generation of a planner listing: an insert or stage change invalidates its cursors."""
    return model.digest([generation, group, [(c["ticket"], c["stage"], c["workflow"]) for c in pool]])[:32]


def _privacy(args, cfg):
    from . import configstore
    if args.get("privacy") is not None:
        return args["privacy"] is True
    return configstore.get(cfg, "companion.privacy", False) is True


def _pinned(args):
    pins = args.get("pinned") or []
    return [p for p in pins if isinstance(p, str) and _KEY.fullmatch(p)][:10]


# ---------------------------------------------------------------- planner (FLOW-12)

def planner(args=None):
    args = args or {}
    cfg = common.load_config()
    privacy = _privacy(args, cfg)
    out = _envelope("planner", cfg, privacy)
    if not generations(cfg)[4]:
        out.update(state="identity_unconfirmed", items=[], groups=[], completed={"collapsed": True, "count": 0},
                   group="all", scope="Nothing is listed until your identity is confirmed", total=0, more=False,
                   cursor=None, empty="Confirm your identity and scope in Setup first.")
        return _finish(out)
    group = args.get("group") or "all"
    if group != "all" and group not in GROUP_LABELS:
        raise ValueError("unknown planner group")
    data, rows = _cards(cfg, index(cfg), _pinned(args))
    pool = data["items"] + data["completed"]["items"] if group in ("all", "completed") else data["items"]
    pool = [c for c in pool if group == "all" and c["group"] != "completed" or c["group"] == group]
    gen = _content(out["profile_generation"], group, pool)
    offset = read_cursor(args.get("cursor"), "planner:" + group, "", out["profile_generation"], gen)
    out.update(state="ready" if rows else "empty", group=group, scope=data["scope"],
               groups=[{"id": g, "label": GROUP_LABELS[g], "count": data["groups"].get(g, 0)} for g in GROUP_LABELS],
               completed={"collapsed": group != "completed", "count": data["completed"]["count"]},
               total=len(pool), empty=None if rows else "No recorded work yet. Captured tickets appear here.")
    page, n = _page(pool, offset, lambda rs: dict(out, items=[_card_view(c, privacy) for c in rs]))
    out["items"] = [_card_view(c, privacy) for c in page]
    out["offset"] = offset
    out["more"] = offset + n < len(pool)
    out["cursor"] = make_cursor("planner:" + group, "", offset + n, out["profile_generation"], gen) \
        if out["more"] else None
    return _finish(out)


# ---------------------------------------------------------------- one workflow

def _wid(value):
    if not isinstance(value, str) or not _WID.fullmatch(value):
        raise ValueError("invalid workflow ID")
    return value


def _start(operation, args):
    cfg = common.load_config()
    if not generations(cfg)[4]:
        raise PermissionError("confirm your identity in setup first")
    wid = _wid(args.get("workflow"))
    rec = companion.get(wid)
    return cfg, rec, _envelope(operation, cfg, _privacy(args, cfg))


def _action_rows(res, rec):
    names = res["available_actions"]
    stage = rec["stage"]
    rows = [{"name": n, "available": True, "reason": ""} for n in names]
    extra = []
    if stage in model.ACTIVE:
        extra.append({"name": "Pause", "available": True, "reason": "Task pause only; capture keeps running"})
    if stage in model.ACTIVE | {"paused", "completed"}:
        extra.append({"name": "Verify Requirements", "available": True, "reason": ""})
    if stage in {"awaiting_verification", "pr_review"}:
        extra.append({"name": "Complete", "available": True,
                      "reason": "Gated: every applicable criterion Verified, PR merged or not required, no open "
                                "PR or UAT gap"})
    if stage == "completed":
        extra.append({"name": "Reopen", "available": True, "reason": "Explain why; completion history is kept"})
    if res["unread_total"]:
        extra.append({"name": "Mark changes reviewed", "available": True, "reason": ""})
    if stage != "completed":
        extra.append({"name": "Cancel", "available": True, "reason": "Cancels a pending job or handoff; nothing is "
                                                                     "rolled back"})
    seen = {r["name"] for r in rows}
    return rows + [e for e in extra if e["name"] not in seen]


def _ledger_rows(rows, evidence_limit):
    out = []
    for r in rows:
        cited = r.get("evidence") or []
        out.append({"criterion": r["criterion"], "text": safe(r.get("text"), 300), "status": r["status"],
                    "excluded": r["excluded"], "failing": r.get("failing", False), "basis": r.get("basis") or [],
                    "plan_steps": (r.get("plan_steps") or [])[:20], "applicable_revision": r.get("applicable_revision"),
                    "observed_at": r.get("observed_at"), "invalidation": safe(r.get("invalidation"), 300),
                    "applicability": _applicability_text(r),
                    "evidence": [{k: (safe(v, 200) if isinstance(v, str) else v) for k, v in c.items()}
                                 for c in cited[:evidence_limit]], "evidence_total": len(cited)})
    return out


def _applicability_text(row):
    if row["excluded"]:
        return "Excluded with developer rationale; not in the denominator"
    if row["status"] == "Verified":
        return "Verified by current applicable %s evidence" % "/".join(row.get("basis") or ["captured"])
    if row.get("failing"):
        return "Missing: a current mapped check failed"
    cited = row.get("evidence") or []
    reasons = lambda state: sorted({c.get("reason") for c in cited if c.get("applicability") == state  # noqa: E731
                                    and c.get("reason")})
    parts = []
    if reasons("historical"):  # observed under a context that has since changed
        parts.append("earlier evidence no longer applies (%s)" % "; ".join(reasons("historical")))
    if reasons("unknown"):  # its context was never captured: not known to be old, not known to apply
        parts.append("applicability unknown (%s)" % "; ".join(reasons("unknown")))
    if parts:
        return safe("%s: %s" % (row["status"], "; ".join(parts)), 300)
    if row.get("invalidation"):
        return "%s: earlier evidence no longer applies (%s)" % (row["status"], safe(row["invalidation"], 160))
    return "%s: no current applicable evidence" % row["status"]


def detail(wid, args=None):
    """RESUME-02 view of one workflow, with only its stage-appropriate actions, its decisions (FLOW-05) and its
    impact suggestions from recorded links (FLOW-06)."""
    from . import companiondrafts, companionexecution, companionimpact, companionjobs
    args = dict(args or {}, workflow=wid)
    cfg, rec, out = _start("detail", args)
    res = companion.resume(wid)
    key = rec["binding"]["ticket"]
    tk = companion.ticket(key, cfg)
    card = brief.jira_card(key) or {}
    baseline = rec["baselines"][rec["baseline"]]
    ctx = res["context"]
    ledger = model.verification(baseline, rec["evidence"], {k: ctx.get(k) for k in model.CONTEXT_KEYS})
    plan_ids = companion.plan_order(rec)  # oldest first: version numbers follow creation, not plan IDs
    candidates = rec.get("candidates") or {}
    jobs = [companionjobs.public(j) for j in companionjobs.jobs(wid)]
    active = [j for j in jobs if j["state"] in companionjobs.ACTIVE
              and (rec["jobs"].get(j["id"]) or {}).get("status") != "cancelled"]
    rel = tk.get("release") if isinstance(tk.get("release"), dict) else {}
    cont = res["continuation"]
    out.update(
        workflow=wid, revision=rec["revision"], ticket=key, title=HIDDEN if out["privacy"] else safe(tk.get("title") or
                                                                                                    tk.get("title_hint")),
        repository=_repo(rec["binding"]["location"]), stage=rec["stage"], resume_stage=res["stage"],
        situation=res["situation"], situation_label=SITUATIONS.get(res["situation"], res["situation"]),
        archived=bool(rec.get("archived")), resume_id=res["id"], context_digest=res["context_digest"],
        last_session=rec.get("session"), last_checkpoint=rec.get("updated_at"), selected_plan=rec["selected_plan"],
        approved_plan=rec["approved_plan"] or None, child_plan=rec.get("child_plan"),
        approved_label="No approved plan" if not rec["approved_plan"] else "Approved version " + rec["approved_plan"][:14],
        next_action={"label": NEXT_LABEL.get(res["situation"], "Resume Task"), "text": safe(res["next_action"], 300)},
        actions=_action_rows(res, rec), progress={k: (v or [])[:50] for k, v in (res["progress"] or {}).items()},
        measures={"plan_coverage": res["measures"]["plan_coverage"],
                  "implementation_progress": res["measures"]["implementation_progress"],
                  "functional_verification": dict(res["measures"]["functional_verification"],
                                                  gaps=res["measures"]["functional_verification"]["gaps"][:50],
                                                  label="verified criterion weight / applicable criterion weight")},
        continuation={k: (v[:30] if isinstance(v, list) else v) for k, v in cont.items()} if cont else None,
        changes={"total": res["delta"]["total"], "items": res["delta"]["items"][:10], "more": res["delta"]["total"] > 10,
                 "first_use": res["delta"]["first_use"], "watermark": res["delta"]["watermark"],
                 "unprocessed_total": res["delta"]["unprocessed_total"],
                 "label": "No previous checkpoint" if res["delta"]["first_use"] and not res["delta"]["total"] else None},
        context={"head": ctx.get("head"), "branch": safe(ctx.get("branch"), 200) or None, "worktree": ctx.get("worktree"),
                 "observation": ctx.get("observation"), "observed_at": ctx.get("observed_at"),
                 "repository_available": ctx.get("repository_available"), "coverage": ctx.get("coverage"),
                 "reason": safe(ctx.get("reason"), 200), "changed": res["changes"][:10]},
        recovery=[dict(r, label=RECOVERY_LABEL.get(r["state"], "Review")) for r in res["recovery"]][:10],
        handoff=res["handoff"], ownership=res.get("ownership"), switch=res["switch"],
        prs=[{k: (safe(v, 200) if isinstance(v, str) else v) for k, v in p.items()} for p in res["prs"]][:10],
        ci=[{k: (safe(v, 200) if isinstance(v, str) else v) for k, v in c.items()} for c in res.get("ci") or []][:10],
        plan_versions=[{"id": p, "source": rec["plans"][p]["source"], "parent": rec["plans"][p]["parent"],
                        "selected": p == rec["selected_plan"], "approved": p == rec["approved_plan"],
                        "candidate": (candidates.get(p) or {}).get("state"),
                        "baseline_current": rec["plans"][p]["baseline"] == rec["baseline"], "number": i + 1}
                       for i, p in enumerate(plan_ids)][-20:],
        plan_versions_total=len(plan_ids),
        verification={"rows": _ledger_rows(ledger["rows"][:20], 5), "more": len(ledger["rows"]) > 20,
                      "percent": ledger["percent"], "numerator": ledger["numerator"],
                      "denominator": ledger["denominator"], "gaps": ledger["gaps"][:50]},
        baseline={"id": baseline["id"], "completeness": baseline["completeness"],
                  "criteria": [{"id": c["id"], "text": safe(c["text"], 300), "weight": c["weight"],
                                "excluded": c["excluded"]} for c in baseline["criteria"][:50]],
                  "criteria_total": len(baseline["criteria"]),
                  "label": "Plan coverage" if baseline["completeness"] == "complete" else "Coverage of available criteria",
                  "gaps": [safe(g, 200) for g in ((baseline.get("coverage") or {}).get("gaps") or [])[:10]]},
        jira={"status": safe(card.get("status"), 100) or "Unavailable", "priority": safe(card.get("priority"), 100)
              or "Unavailable", "coverage": baseline["completeness"]},
        decisions=[{"text": safe(d.get("text"), 300), "source": d.get("source"), "at": d.get("at"),
                    "kind": d.get("kind")} for d in rec["decisions"][-5:]],
        knowledge=companiondrafts.decisions(rec, tk, companion.observation(rec).get("environment")),
        impact=companionimpact.suggestions(rec, tk, cfg),
        summary=safe(rec.get("summary"), 1000),
        sessions=companionexecution.observed_sessions(rec) if rec["stage"] in {"approved", "execution_ready"} else [],
        jobs={"active": [_job(j) for j in active][:3], "total": len(jobs)},
        delivery={"release": safe((rel.get("v") and brief_release(rel)) or "", 200) or None,
                  "reminders": [{"kind": r.get("kind"), "label": REMINDER_LABEL.get(r.get("kind"), "Delivery reminder"),
                                 "text": safe(r.get("text"), 250)} for r in (rel.get("reminders") or [])[:3]
                                if isinstance(r, dict)],
                  "guidance": "Open the ticket's delivery details for repository-specific base, PR target, QA and UAT"},
        rollup=rollup(cfg, key),
        overlays={k: v for k, v in rec["overlays"].items() if v},
        database_context=database_context(cfg, args),
        links={"ticket": "#/tickets/" + key, "delivery": delivery_link(key), "database": "#/db"})
    if out["rollup"]:
        out["rollup"] = {"label": out["rollup"]["label"], "total": out["rollup"]["total"],
                         "completed": out["rollup"]["completed"], "hidden": out["rollup"]["hidden"],
                         "unfinished": [{"workflow": r["workflow"], "stage": r["stage"], "repository": _repo(r["location"])}
                                        for r in out["rollup"]["unfinished"]][:10]}
    companion.authorize(rec)
    return _finish(out, (lambda o: o["verification"].update(rows=o["verification"]["rows"][:5], more=True),
                         lambda o: o["changes"].update(items=o["changes"]["items"][:3]),
                         lambda o: o.update(plan_versions=o["plan_versions"][-5:]),
                         lambda o: o["impact"].update(items=o["impact"]["items"][:3], more=o["impact"]["total"] > 3),
                         lambda o: o["knowledge"].update(current=o["knowledge"]["current"][:2],
                                                         history=o["knowledge"]["history"][:2],
                                                         rejected=o["knowledge"]["rejected"][:2])))


def brief_release(rel):
    from . import release
    return release.release_label(rel)


def _job(j):
    keep = ("id", "kind", "state", "phase", "attempt", "created_at", "updated_at", "dispatched_at", "reason", "result",
            "evaluation_state", "retry_of", "plan", "baseline", "reservation_usd", "purpose")
    out = {k: j.get(k) for k in keep}
    out["omitted"] = [safe(x, 160) for x in (j.get("omitted") or [])[:5]]
    out["evidence_files"] = len(j.get("evidence_files") or [])
    out["history"] = [{"state": h.get("state"), "at": h.get("at"), "reason": safe(h.get("reason"), 200)}
                      for h in (j.get("history") or [])[-5:]]
    out["label"] = {"queued": "Queued", "running": "Running", "cancel_requested": "Cancel requested",
                    "succeeded": "Succeeded", "partial": "Partial", "failed": "Failed", "cancelled": "Cancelled",
                    "outcome_unknown": "Outcome unknown", "superseded": "Superseded"}.get(j.get("state"), "Unknown")
    return out


# ---------------------------------------------------------------- Jev Review (FLOW-13 / PLAN-03)

def _evaluation_view(e):
    cov = e.get("coverage") or {}
    answers = e.get("answers") or {}
    return {"id": e.get("id"), "plan": e.get("plan"), "origin": e.get("origin") or jev.ORIGINS["jev"],
            "provider": e.get("provider"), "model": e.get("model"), "rubric": e.get("rubric"),
            "state": e.get("state"), "label": "Evaluation unavailable" if e.get("state") == "unavailable"
            else cov.get("label") or "Coverage", "percent": cov.get("percent"), "numerator": cov.get("numerator"),
            "denominator": cov.get("denominator"), "counts": cov.get("counts"),
            "reason": safe(cov.get("reason") or e.get("reason"), 300) or None, "stale": bool(e.get("stale")),
            "at": e.get("at"), "partial": bool(e.get("partial")), "unevaluated": len(e.get("unevaluated") or []),
            "answers": [{"criterion": cid, "status": (a or {}).get("status"), "confidence": (a or {}).get("confidence"),
                         "origin": (a or {}).get("origin") or e.get("origin")}
                        for cid, a in sorted(answers.items())][:50],
            "confidence_note": "Confidence describes the answer distribution; it never changes coverage or approves",
            "note": safe(e.get("note"), 500) or None}


def review(args):
    from . import companionevidence as evidence, companionjobs, companionplanning as planning, modelbudget
    cfg, rec, out = _start("review", args)
    wid = rec["id"]
    current = companion.context(rec)
    tk, rows = evidence.current_sources(rec)
    recovery = evidence.recovery(rec, current, tk, rows, bool(brief.jira_card(rec["binding"]["ticket"])))
    sel = rec["selected_plan"]
    plan = rec["plans"].get(sel) if sel else None
    base = rec["baselines"][rec["baseline"]]
    evals = [e for _, e in sorted((rec.get("evaluations") or {}).items()) if isinstance(e, dict) and e.get("plan") == sel]
    jev_rows = [e for e in evals if e.get("origin") != "developer assessment"]
    dev_rows = [e for e in evals if e.get("origin") == "developer assessment"]
    usable = companionjobs.latest_evaluation(rec, sel) if sel else None
    reason = jev.preflight(cfg, companionjobs.acknowledgement()) or jev.UNAVAILABLE
    evaluation = _evaluation_view(jev_rows[-1]) if jev_rows else {
        "state": "unavailable", "label": "Evaluation unavailable", "reason": reason, "origin": jev.ORIGINS["jev"],
        "percent": None, "requested": False}
    gaps = planning.gap_review(base, plan, usable)["open"] if plan else []
    compare = None
    if plan and plan.get("parent"):
        old = companionjobs.latest_evaluation(rec, plan["parent"])
        if old and usable:
            compare = planning.compare(old, usable, rec["baselines"].get(old.get("baseline")),
                                       rec["baselines"].get(usable.get("baseline")))
        else:
            compare = {"comparable": False, "reasons": ["no usable evaluation of both versions"], "direction":
                       "not comparable", "advisory": "Advisory only; 100% is never required to execute"}
    ceiling, why = modelbudget.call_ceiling(cfg, "companion_improve")
    editable = rec["stage"] in companionjobs.EDITABLE
    improve = {"action": "Improve Plan", "available": bool(plan) and editable,
               "reason": "" if plan and editable else "Save a plan first" if not plan else
               "Execution has started; revise through Pause and a child plan",
               "modes": {"claude": {"available": bool(plan) and editable and ceiling is not None, "reason": why},
                         "manual": {"available": bool(plan) and editable, "reason": ""}}}
    blocking = [r for r in recovery if r["blocking"]]
    exec_reason = ""
    if not plan:
        exec_reason = "Save and select a plan first"
    elif rec["stage"] in {"approved", "execution_ready"}:
        exec_reason = "Already approved; copy the prepared handoff from Handoff"
    elif rec["stage"] in model.ACTIVE | {"paused", "completed"}:
        exec_reason = "Execution already started; use Resume Execution"
    elif rec["stage"] in {"new", "planning"}:
        exec_reason = "Planning is still running"
    elif plan["baseline"] != rec["baseline"]:
        exec_reason = "The selected plan belongs to an older requirement baseline"
    elif current["requirements_source"] != base["source_revision"]:
        exec_reason = "Jira requirements changed; review a new baseline first"
    elif not current["repository_available"]:
        exec_reason = "Repository unavailable; resolve its location first"
    elif blocking:
        exec_reason = "Resolve first: " + safe(blocking[0]["reason"], 200)
    execute = {"action": "Execute Plan", "available": not exec_reason, "reason": exec_reason}
    want_improve = bool(gaps) and improve["available"]
    if not want_improve and not execute["available"] and improve["available"]:
        want_improve = True
    if not improve["available"] and not execute["available"]:
        want_improve = bool(gaps)
    improve["recommended"], execute["recommended"] = want_improve, not want_improve
    improve["why"] = ("%d criteria Missing, Partial or Unverified" % len(gaps)) if gaps else ""
    execute["why"] = "Advisory coverage is never required to execute; acknowledge remaining risks"
    risks = []
    if plan:
        risks = (plan.get("details") or {}).get("risks") or [ln.strip("- ").strip() for ln in
                                                              plan["sections"]["risks"].splitlines() if ln.strip()]
    candidates = [{"plan": pid, "parent": c.get("parent"), "state": c.get("state"), "at": c.get("at"),
                   "attempt": c.get("attempt"), "diff": {"text": safe((c.get("diff") or {}).get("text"), 4000),
                                                          "truncated": bool((c.get("diff") or {}).get("truncated"))}
                   if c.get("diff") else None, "selectable": c.get("state") == "candidate"
                   and rec["plans"].get(pid, {}).get("baseline") == rec["baseline"] and editable}
                  for pid, c in sorted((rec.get("candidates") or {}).items(), key=lambda kv: kv[1].get("at") or "")][-5:]
    out.update(workflow=wid, revision=rec["revision"], ticket=rec["binding"]["ticket"], stage=rec["stage"],
               selected_plan=sel, approved_plan=rec["approved_plan"] or None,
               versions=[{"id": p, "source": rec["plans"][p]["source"], "parent": rec["plans"][p]["parent"],
                          "selected": p == sel, "number": i + 1} for i, p in enumerate(companion.plan_order(rec))][-10:],
               evaluation=evaluation, assessment=_evaluation_view(dev_rows[-1]) if dev_rows else None,
               assessment_label="Developer assessment (yours; never a Jev score)",
               comparison=compare, gaps=gaps[:50], choices=[improve, execute],
               candidates=candidates,
               drafts=[{"id": d, "reason": safe(v.get("reason"), 300), "label": v.get("label"), "at": v.get("at")}
                       for d, v in sorted((rec.get("drafts") or {}).items())][-3:],
               criteria=[{"id": c["id"], "text": safe(c["text"], 300), "excluded": c["excluded"]}
                         for c in base["criteria"][:50]],
               execute={"plan": sel, "digest": model.digest(plan) if plan else None, "baseline": base["id"],
                        "baseline_completeness": base["completeness"],
                        "target": {"repository": _repo(rec["binding"]["location"]),
                                   "location": rec["binding"]["location"],
                                   "session": "A separate ordinary Claude Code turn you start (copy fallback)"},
                        "context_digest": model.digest(current),
                        "findings": {"evaluation": evaluation["label"], "reason": evaluation.get("reason"),
                                     "open_criteria": gaps[:20]},
                        "risks": [safe(r, 300) for r in risks[:10]],
                        "acknowledgement": "I reviewed this exact version and accept the unresolved risks, the "
                                           "unavailable Jev evaluation and the unknown working-tree state.",
                        "outcome": "Prepared; not submitted"},
               capabilities=capabilities(cfg))
    return _finish(out, (lambda o: [c.update(diff=None) for c in o["candidates"]],
                         lambda o: o["evaluation"].update(answers=o["evaluation"].get("answers", [])[:10])))


# ---------------------------------------------------------------- plan text, changes, verification, jobs, handoff

def plan_page(wid, plan_id=None, offset=0, args=None):
    from . import companionplanning as planning
    args = dict(args or {}, workflow=wid)
    cfg, rec, out = _start("plan", args)
    pid = plan_id or rec["selected_plan"]
    if not isinstance(pid, str) or not _PID.fullmatch(pid) or pid not in rec["plans"]:
        raise ValueError("invalid plan")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("invalid plan page")
    version = rec["plans"][pid]
    digest = model.digest(version)
    if args.get("cursor"):
        offset = read_cursor(args["cursor"], "plan", pid, out["profile_generation"], digest)
    raw = version["text"]
    out.update(workflow=wid, plan=pid, digest=digest, source=version["source"], parent=version["parent"],
               baseline=version["baseline"], baseline_current=version["baseline"] == rec["baseline"],
               selected=pid == rec["selected_plan"], approved=pid == rec["approved_plan"], offset=offset,
               length=len(raw), exact=True)
    if offset == 0:
        out["sections"] = version["sections"]
        out["steps"] = version["steps"][:50]
        out["steps_more"] = len(version["steps"]) > 50
        out["assumptions"] = version.get("assumptions") or []
        base = args.get("base")
        if base:
            other = rec["plans"].get(version["parent"] if base == "parent" else base)
            out["diff"] = planning.diff(other, version) if other else None
    size = TEXT_PAGE
    while True:
        out["text"] = raw[offset:offset + size]
        if _size(out) <= MAX_BYTES or size <= 500:
            break
        size //= 2
        out.pop("diff", None) if size < TEXT_PAGE // 2 else None
    nxt = offset + size
    out["next"] = nxt if nxt < len(raw) else None
    out["cursor"] = make_cursor("plan", pid, nxt, out["profile_generation"], digest) if nxt < len(raw) else None
    companion.authorize(rec)
    return _finish(out)


def changes(args):
    """FLOW-01 delta: unread ingested evidence vs your acknowledged cursor, with provenance. Never acknowledges."""
    from . import companionevidence as evidence
    cfg, rec, out = _start("changes", args)
    _, rows = evidence.current_sources(rec)
    summary = evidence.delta(rec, rows)
    ids = evidence.unread(rec)
    gen = model.digest([rec["revision"], ids])[:32]
    offset = read_cursor(args.get("cursor"), "changes", rec["id"], out["profile_generation"], gen)
    items = []
    for sid in ids:
        row = evidence.resolve(rec, rows, sid)
        seen = rec["acknowledged"].get(sid) or (rec.get("imported") or {}).get(sid)
        items.append({"id": sid, "kind": row.get("kind"), "label": safe(row.get("label"), 200),
                      "outcome": row.get("outcome"), "observed_at": row.get("observed_at"),
                      "provenance": row.get("source_ref"), "state": row["state"], "change": "changed" if seen else "new",
                      "source_type": row.get("origin") or "captured"})
    items.sort(key=lambda r: (str(r.get("observed_at") or ""), r["id"]), reverse=True)
    out.update(workflow=rec["id"], revision=rec["revision"], ticket=rec["binding"]["ticket"], total=len(items),
               ingested=len(rec["ingested"]), acknowledged=len(rec["acknowledged"]), first_use=summary["first_use"],
               imported=len(rec.get("imported") or {}), watermark=summary["watermark"],
               unprocessed_total=summary["unprocessed_total"], legacy=summary["legacy"],
               label="No previous checkpoint" if summary["first_use"] and not items else
               "Changes since your last review" if items else "No new changes since your last review",
               note="Reading this never marks changes reviewed; use Mark changes reviewed")
    page, n = _page(items, offset, lambda rs: dict(out, items=rs, unread=[r["id"] for r in rs]))
    out.update(items=page, unread=[r["id"] for r in page], offset=offset, more=offset + n < len(items),
               cursor=make_cursor("changes", rec["id"], offset + n, out["profile_generation"], gen)
               if offset + n < len(items) else None)
    return _finish(out)


def verification(args):
    """FLOW-16 ledger recomputed from current evidence (nothing recorded): three separate measures."""
    from . import companionevidence as evidence
    cfg, rec, out = _start("verification", args)
    ctx = companion.context(rec)
    led = evidence.ledger(rec, ctx, common.now_iso())
    gen = model.digest([rec["revision"], led["context_digest"]])[:32]
    offset = read_cursor(args.get("cursor"), "verification", rec["id"], out["profile_generation"], gen)
    last = rec["verifications"][-1] if rec.get("verifications") else None
    out.update(workflow=rec["id"], revision=rec["revision"], ticket=rec["binding"]["ticket"], stage=rec["stage"],
               baseline=led["baseline"], plan=led["plan"], context_digest=led["context_digest"],
               measures=led["measures"],
               definitions={"plan_coverage": "Advisory plan coverage from the latest evaluation; never verification",
                            "implementation_progress": "Evidenced implementation steps / approved implementation steps",
                            "functional_verification": "Verified criterion weight / applicable criterion weight"},
               recorded={"id": last["id"], "at": last["at"]} if last else None,
               note="Recomputed for display; Verify Requirements records an immutable result. Nothing runs tests.")
    rows = _ledger_rows(led["rows"], 10)
    page, n = _page(rows, offset, lambda rs: dict(out, rows=rs))
    out.update(rows=page, total=len(rows), offset=offset, more=offset + n < len(rows),
               cursor=make_cursor("verification", rec["id"], offset + n, out["profile_generation"], gen)
               if offset + n < len(rows) else None)
    return _finish(out)


def jobs(args):
    """Local job records only: reading never reconciles, retries or enqueues."""
    from . import companionjobs, modelbudget
    cfg, rec, out = _start("jobs", args)
    rows = [companionjobs.public(j) for j in companionjobs.jobs(rec["id"])]
    active = [j for j in rows if j["state"] in companionjobs.ACTIVE
              and (rec["jobs"].get(j["id"]) or {}).get("status") != "cancelled"]
    try:
        budget = modelbudget.companion_summary(cfg)
        budget = {k: budget.get(k) for k in ("day", "cap", "remaining", "plan_call_ceiling", "plan_reason")} | \
            {"unknown": len(budget.get("unknown") or []), "jev": {k: (budget.get("jev") or {}).get(k)
                                                                 for k in ("daily", "per_task", "used", "reason")}}
    except (OSError, ValueError, TypeError):
        budget = {"remaining": None, "plan_reason": "Budget ledger unavailable"}
    out.update(workflow=rec["id"], revision=rec["revision"], stage=rec["stage"],
               active=[_job(j) for j in active][:3], jobs=[_job(j) for j in reversed(rows)][:20], total=len(rows),
               budget=budget, poll_after_s=15, drafts=len(rec.get("drafts") or {}),
               note="Local job state only; reading never retries or starts work")
    return _finish(out, (lambda o: o.update(jobs=o["jobs"][:5]),))


def handoff(args):
    """The copy packet of the current approved intent: Prepared; not submitted. Never records or sends anything."""
    cfg, rec, out = _start("handoff", args)
    intent = companion.latest_intent(rec)
    out.update(workflow=rec["id"], revision=rec["revision"], ticket=rec["binding"]["ticket"], stage=rec["stage"],
               state="prepared_not_submitted", label="Prepared; not submitted", text=None, next=None, cursor=None)
    try:
        text = companion.packet(rec["id"])
    except ValueError as exc:
        out.update(state="unavailable", label="No current approved handoff", reason=safe(exc, 300),
                   dispatch=companion.intent_state(intent) if intent else None,
                   attempt=companion.attempt_id(intent) if intent else None)
        return _finish(out)
    digest = model.digest(text)
    offset = read_cursor(args.get("cursor"), "handoff", rec["id"], out["profile_generation"], digest)
    out.update(plan=intent["plan"], plan_digest=intent["plan_digest"], attempt=companion.attempt_id(intent),
               dispatch=companion.intent_state(intent), expires_at=intent.get("expires_at"),
               copied=bool(intent.get("copied_at")), packet_digest=digest, length=len(text), offset=offset,
               text=text[offset:offset + PACKET_PAGE], reason=None,
               note="Paste it into a separate ordinary Claude turn; normal permissions apply. Copying does not "
                    "start execution.")
    nxt = offset + PACKET_PAGE
    if nxt < len(text):
        out.update(next=nxt, cursor=make_cursor("handoff", rec["id"], nxt, out["profile_generation"], digest))
    return _finish(out)


def drafts(args):
    """FLOW-04 audience drafts of one task: per audience its current version (`Source evidence changed` when the
    sources moved since), a regenerated version kept beside your edit with the diff, or a preview. Never saves."""
    from . import companiondrafts
    cfg, rec, out = _start("drafts", args)
    sel = {k: args[k] for k in ("flow", "investigation") if args.get(k)} or None
    out.update(workflow=rec["id"], revision=rec["revision"], ticket=rec["binding"]["ticket"], stage=rec["stage"],
               archived=bool(rec.get("archived")), **companiondrafts.view(rec, cfg, sel))
    companion.authorize(rec)
    return _finish(out, (lambda o: [a.update(versions=a["versions"][:3]) for a in o["audiences"]],
                         lambda o: [a["regenerated"]["diff"].update(text=a["regenerated"]["diff"]["text"][:800],
                                                                    truncated=True)
                                    for a in o["audiences"] if a.get("regenerated")],
                         lambda o: o["decisions"].update(current=o["decisions"]["current"][:2], history=[],
                                                         rejected=o["decisions"]["rejected"][:2])))


# ---------------------------------------------------------------- database context (DB-19 / DB-22)

def database_context(cfg, args):
    """The ≤ 8 KiB database_context of a session-local flow/investigation selection, or unavailable + reason."""
    flow, inv = args.get("flow"), args.get("investigation")
    if not flow and not inv:
        return {"available": False, "reason": "No database flow or investigation selected"}
    try:
        from . import dbaccess, dbtrace
        got = dbtrace.database_context(dbaccess.AccessContext(cfg), flow, inv, cfg=cfg)
    except Exception:  # noqa: BLE001 - database context is optional; the rest of the view still renders
        return {"available": False, "reason": "Database context unavailable"}
    ctx = got.get("database_context")
    if not ctx:
        return {"available": False, "reason": safe(got.get("reason") or "unavailable", 200)}
    ctx = dict(ctx, available=True)
    while _size(ctx) > DB_BYTES and ctx.get("observations"):
        ctx["observations"] = ctx["observations"][:-1]
    if _size(ctx) > DB_BYTES:
        return {"available": False, "reason": "Database context exceeds 8 KiB"}
    fid = ctx.get("flow_id")
    ctx["links"] = {"flow": "#/db?flow=%s" % fid, "evidence": "#/db?flow=%s&tab=evidence" % fid,
                    "journey": "#/db?flow=%s&tab=journey" % fid} if fid else {"workspace": "#/db"}
    return ctx


def database(args):
    cfg = common.load_config()
    out = _envelope("database", cfg, _privacy(args, cfg))
    if not generations(cfg)[4]:
        raise PermissionError("confirm your identity in setup first")
    out["database_context"] = database_context(cfg, args)
    return _finish(out)


# ---------------------------------------------------------------- one allowlisted entry point

def _validate(args):
    if not isinstance(args, dict) or set(args) - ARGS:
        raise ValueError("invalid companion read arguments")
    if _size(args) > INPUT_BYTES:
        raise ValueError("companion read arguments exceed 8 KiB")
    op = args.get("operation", "snapshot")
    if op not in OPERATIONS:
        raise ValueError("unknown companion read operation")
    for name, rx in (("workflow", _WID), ("ticket", _KEY), ("plan", _PID), ("flow", _DBID["flow"]),
                     ("investigation", _DBID["investigation"])):
        v = args.get(name)
        if v is not None and (not isinstance(v, str) or not rx.fullmatch(v)):
            raise ValueError("invalid %s argument" % name)
    base = args.get("base")
    if base is not None and base != "parent" and (not isinstance(base, str) or not _PID.fullmatch(base)):
        raise ValueError("invalid base argument")
    if args.get("cursor") is not None and (not isinstance(args["cursor"], str) or len(args["cursor"]) > 1024):
        raise ValueError("stale or invalid cursor; reload the view")
    if args.get("privacy") is not None and not isinstance(args["privacy"], bool):
        raise ValueError("privacy must be true or false")
    pins = args.get("pinned")
    if pins is not None and (not isinstance(pins, list) or len(pins) > 10
                             or not all(isinstance(p, str) and _KEY.fullmatch(p) for p in pins)):
        raise ValueError("pinned must list at most ten ticket keys")
    if args.get("group") is not None and args["group"] != "all" and args["group"] not in GROUP_LABELS:
        raise ValueError("unknown planner group")
    off = args.get("offset", 0)
    if isinstance(off, bool) or not isinstance(off, int) or not 0 <= off <= 10 ** 7:
        raise ValueError("invalid offset")
    return op


def read(args):
    op = _validate(args)
    if op == "snapshot":
        return snapshot(args)
    if op == "planner":
        return planner(args)
    if op == "database":
        return database(args)
    if op == "detail":
        return detail(args.get("workflow"), args)
    if op == "plan":
        return plan_page(args.get("workflow"), args.get("plan"), args.get("offset", 0), args)
    return {"review": review, "changes": changes, "verification": verification, "jobs": jobs,
            "handoff": handoff, "drafts": drafts}[op](args)
