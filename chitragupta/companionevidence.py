"""Workflow evidence from existing canonical ticket records only.

No new collector, transcript read, git command or model call. Stable IDs are `kind:<source key>`;
a materially changed source keeps its ID with a new version marker, so replays dedupe and late
events stay new. The ingested watermark and the acknowledged cursor are separate: checkpoints and
the tick never acknowledge. Cited evidence is pinned minimally so ticket pruning cannot erase it;
uncited pruned sources become unavailable. CI check runs arrive the same way, beside the ticket's PR facts
(git.repos[<repo>].ci from hosts.ci_record via delivery), one row per attempt; only the developer maps one to a
criterion.
"""
import os
import re

from . import brief, common, store, workflowmodel as model, workflowstore
from .redact import redact

CAP = 300          # newest sources per kind considered for one workflow
TICK_LIMIT = 20    # workflows checkpointed per tick
DELTA_LIMIT = 50
UNREAD_LIMIT = 100
OBSERVATIONS = 50
EVIDENCE_KINDS = {"test", "ci", "commit", "file", "pr", "acceptance", "decision", "code_inspection"}
_LEGACY = re.compile(r"^(items|files|commits|tests|git|release):[a-f0-9]{64}$")
_LEGACY_KIND = {"tests": "test", "commits": "commit", "files": "file", "items": "decision"}
_HEAD = re.compile(r"^[0-9a-fA-F]{7,64}$")


def safe(value, limit=200):
    return re.sub(r"[\x00-\x1f\x7f]", " ", redact(str(value or "")))[:limit]


def version(*parts):
    return model.digest(list(parts))[:16]


def repo_key(location):
    from .gitcmd import repo_key as key
    return key(location)


def _newest(records, field, cap=CAP):
    if not isinstance(records, dict):
        return []
    rows = [(str(k), v) for k, v in records.items() if isinstance(v, dict)]
    return sorted(rows, key=lambda kv: (str(kv[1].get(field) or ""), kv[0]), reverse=True)[:cap]


def captured_context(value):
    """Fields captured at execution time only; never reconstructed from a later repository state."""
    if not isinstance(value, dict):
        return None
    ctx = {k: value[k] for k in model.CONTEXT_KEYS if isinstance(value.get(k), str) and value[k]}
    return ctx or None


def requirement_marker(key):
    """(revision, fetched_at) of the cached Jira card, or (None, None) when it is unavailable."""
    card = brief.jira_card(key)
    if not card:
        return None, None
    from .companion import source_revision
    fetched = (common.read_json(common.hp("data", "jira", key + ".json"), {}) or {}).get("fetched_at")
    return source_revision(key), fetched or None


CI_CHECKOUT = "clean (CI checkout)"
CI_OUTCOMES = {"passed", "failed", "interrupted", "running", "action_required", "neutral", "skipped", "stale", "unknown"}
_RUN_ID = re.compile(r"^[1-9][0-9]{0,19}$")


def ci_rows(name, p):
    """FLOW-03 check runs of one PR fact (hosts.ci_record) -> [(row, version material)], one per run attempt with the
    stable ID `ci:<provider>:<repo>:<run id>:<attempt>`. The run's head is its revision. GitHub runs it on a clean
    checkout of that commit and states no config, environment or location, so those stay unknown and a CI row is never
    `current` by itself (workflowmodel.applies); on another head it is historical. Nothing maps it to a criterion."""
    from .activity import canonical_id
    ci = p.get("ci") if isinstance(p.get("ci"), dict) else {}
    provider, path = safe(ci.get("provider") or "github", 20), str(ci.get("repo") or "")
    repo_id = canonical_id({"host": provider, "path": path}, "") if provider == "github" and "/" in path else None
    out = []
    for block in ci.get("heads") or []:
        head = str((block or {}).get("head") or "").lower() if isinstance(block, dict) else ""
        if not _HEAD.fullmatch(head):
            continue
        for x in block.get("runs") or []:
            if not isinstance(x, dict) or not _RUN_ID.match(str(x.get("id"))) or isinstance(x.get("attempt"), bool) \
                    or not isinstance(x.get("attempt"), int) or x["attempt"] < 1:
                continue
            ctx = {"head": head, "worktree": "clean"}
            if repo_id:
                ctx["repo_id"] = repo_id
            outcome = x.get("outcome") if x.get("outcome") in CI_OUTCOMES else "unknown"
            row = {"id": "ci:%s:%s:%s:%s" % (provider, name, x["id"], x["attempt"]), "kind": "ci", "origin": "captured",
                   "source_ref": "git:%s:pr:%s:ci:%s" % (name, p["number"], x["id"]), "provider": provider,
                   "observed_at": x.get("completed_at") or x.get("started_at") or block.get("observed_at") or None,
                   "outcome": outcome, "label": safe("%s (attempt %d)" % (x.get("name") or "check", x["attempt"])),
                   "check": safe(x.get("name"), 200), "app": safe(x.get("app"), 100), "attempt": x["attempt"],
                   "pr": p["number"], "head": head, "checkout": CI_CHECKOUT, "context": ctx}
            out.append((row, (outcome, x.get("status"), x.get("conclusion"), x.get("started_at"),
                              x.get("completed_at"), head)))
    return out


def ci_summary(tk, binding):
    """Each open PR of the bound repository with its CURRENT head's CI state (hosts.ci_state): runs on an earlier head
    never clear a missing or failing check on the current one (A14); unread or unreadable is never "passed"."""
    from . import hosts
    repo = repo_key(binding["location"])
    git = tk.get("git") if isinstance(tk.get("git"), dict) else {}
    r = (git.get("repos") or {}).get(repo) if isinstance(git.get("repos"), dict) else None
    out = []
    for p in (r.get("prs") or []) if isinstance(r, dict) else []:
        if isinstance(p, dict) and p.get("number") is not None and p.get("state") in ("open", "draft"):
            out.append(dict(hosts.ci_state(with_ci(r, p)), pr="pr:%s:%s" % (repo, p["number"])))
    return out


def with_ci(r, p):
    """A ticket PR with its check runs: delivery keeps them beside the PRs, in git.repos[<repo>].ci[<number>]."""
    ci = (r.get("ci") or {}).get(str(p.get("number"))) if isinstance(r.get("ci"), dict) else None
    return dict(p, ci=ci) if isinstance(ci, dict) else p


def sources(tk, binding, requirements=(None, None)):
    """{stable ID: minimal sanitized row} from the ticket record, scoped to the workflow's repository."""
    repo = repo_key(binding["location"])
    out = {}

    def add(row, *material):
        row["version"] = version(*material)
        out[row["id"]] = row
    for tid, t in _newest(tk.get("tests"), "at"):
        status = t.get("status") if t.get("status") in {"passed", "failed", "interrupted", "unknown"} else "unknown"
        counts = ({k: v for k, v in t["counts"].items() if isinstance(v, int) and not isinstance(v, bool)}
                  if isinstance(t.get("counts"), dict) else {})
        row = {"id": "test:" + tid, "kind": "test", "source_ref": "tests:" + tid, "observed_at": t.get("at") or None,
               "outcome": status, "label": safe(t.get("command")), "origin": "captured"}
        if counts:
            row["counts"] = counts
        ctx = captured_context(t.get("context"))
        if ctx:
            row["context"] = ctx
        add(row, status, t.get("exit"), counts, ctx)
    for sha, c in _newest(tk.get("commits"), "date"):
        if c.get("repo_name") and c["repo_name"] != repo:
            continue
        add({"id": "commit:" + sha, "kind": "commit", "source_ref": "commits:" + sha, "origin": "captured",
             "observed_at": c.get("date") or None, "outcome": "observed", "label": safe(c.get("subject")),
             "repo": safe(c.get("repo_name"), 100)}, c.get("ref"), len(c.get("files") or []))
    for path, f in _newest(tk.get("files"), "last"):
        if f.get("repo") and f["repo"] != repo:
            continue
        outcome = "changed" if f.get("changed") or f.get("committed") else "referenced"
        add({"id": "file:" + path, "kind": "file", "source_ref": "files:" + path, "origin": "captured",
             "observed_at": f.get("last") or None, "outcome": outcome, "label": safe(path, 300)},
            outcome, f.get("last"), bool(f.get("committed")))
    git = tk.get("git") if isinstance(tk.get("git"), dict) else {}
    checks = []
    for name, r in sorted((git.get("repos") or {}).items()):
        if name != repo or not isinstance(r, dict):
            continue
        for p in r.get("prs") or []:
            if not isinstance(p, dict) or p.get("number") is None:
                continue
            head = p.get("head") or p.get("head_at_merge")
            add({"id": "pr:%s:%s" % (name, p["number"]), "kind": "pr", "source_ref": "git:%s:pr:%s" % (name, p["number"]),
                 "origin": "captured", "observed_at": git.get("checked_at") or None,
                 "outcome": p.get("state") if p.get("state") in {"open", "draft", "merged", "closed"} else "unknown",
                 "review": safe(p.get("review"), 40), "head": safe(head, 64) or None,
                 "branch": safe(p.get("source"), 200), "label": safe(p.get("title"))},
                p.get("state"), p.get("review"), head)
            checks += ci_rows(name, with_ci(r, p))
    for row, material in sorted(checks, key=lambda x: (str(x[0].get("observed_at") or ""), x[0]["id"]), reverse=True)[:CAP]:
        add(row, *material)
    for iid, it in _newest(tk.get("items"), "date"):
        if it.get("kind") != "decision" or it.get("archived"):
            continue
        add({"id": "decision:" + iid, "kind": "decision", "source_ref": "items:" + iid, "origin": "captured",
             "observed_at": it.get("date") or None, "outcome": "recorded", "label": safe(it.get("title"))},
            it.get("title"), it.get("detail"))
    release = tk.get("release") if isinstance(tk.get("release"), dict) else {}
    for rem in (release.get("reminders") or [])[:CAP]:
        if isinstance(rem, dict) and rem.get("id"):
            add({"id": "release:" + safe(rem["id"], 200), "kind": "release", "source_ref": "release:" + safe(rem["id"], 200),
                 "origin": "captured", "observed_at": release.get("checked_at") or None,
                 "outcome": safe(rem.get("severity") or "info", 20), "release_kind": safe(rem.get("kind"), 40),
                 "label": safe(rem.get("text"), 250)}, rem.get("text"), rem.get("severity"))
    revision, fetched = requirements
    if revision:
        add({"id": "requirements:" + binding["ticket"], "kind": "requirements", "origin": "captured",
             "source_ref": "jira:" + binding["ticket"], "observed_at": fetched, "outcome": "observed",
             "label": "Jira requirement fields"}, revision)
    return out


def current_sources(rec):
    """Rows for a record's own ticket. Caller has authorized the record."""
    key = rec["binding"]["ticket"]
    tk = common.read_json(store.ticket_path(key), {}) or {}
    return tk, sources(tk, rec["binding"], requirement_marker(key))


def _migrate(rec, tk, rows):
    """Legacy value-digest IDs map to stable IDs once; unmappable ones are labeled imported history."""
    if not any(_LEGACY.match(x) for x in rec["ingested"]):
        return
    for kind, new_kind in _LEGACY_KIND.items():
        for key, value in (tk.get(kind) or {}).items() if isinstance(tk.get(kind), dict) else ():
            old, new = kind + ":" + model.digest([key, value]), new_kind + ":" + str(key)
            if old in rec["ingested"] and new in rows:
                rec["ingested"].pop(old)
                rec["ingested"][new] = rows[new]["version"]
                if old in rec["acknowledged"]:
                    rec["acknowledged"].pop(old)
                    rec["acknowledged"][new] = rows[new]["version"]
    for old in sorted(x for x in rec["ingested"] if _LEGACY.match(x)):
        rec["ingested"].pop(old)
        rec["acknowledged"].pop(old, None)
        rec["legacy_ingested"] = sorted(set(rec["legacy_ingested"]) | {old})


def ingest(rec, tk, rows):
    """Advance the ingested watermark only; returns new or materially changed stable IDs."""
    _migrate(rec, tk, rows)
    fresh = []
    for sid, row in sorted(rows.items()):
        if rec["ingested"].get(sid) != row["version"]:
            rec["ingested"][sid] = row["version"]
            fresh.append(sid)
    return fresh


def unread(rec):
    """Ingested but neither acknowledged nor unchanged imported history (FLOW-01 first use)."""
    imported = rec.get("imported") or {}
    return sorted(sid for sid, ver in rec["ingested"].items()
                  if rec["acknowledged"].get(sid) != ver and imported.get(sid) != ver)


def resolve(rec, rows, sid):
    """A source as it is now, its pinned copy, or `unavailable` once pruned and uncited."""
    if sid in rows:
        return dict(rows[sid], state="available")
    pinned = [e for e in rec["evidence"] if e.get("id") == sid]
    if pinned:
        return dict(pinned[-1], state="pinned copy; source no longer retained")
    return {"id": sid, "kind": sid.split(":", 1)[0], "state": "unavailable", "label": "Source no longer retained"}


def delta(rec, rows):
    """Bounded meaningful changes with provenance; reading it never moves any cursor."""
    ids = unread(rec)
    items = []
    for sid in ids:
        row = resolve(rec, rows, sid)
        seen = rec["acknowledged"].get(sid) or (rec.get("imported") or {}).get(sid)
        items.append({"id": sid, "kind": row.get("kind"), "label": row.get("label"), "outcome": row.get("outcome"),
                      "observed_at": row.get("observed_at"), "provenance": row.get("source_ref"),
                      "state": row["state"], "change": "changed" if seen else "new"})
    items.sort(key=lambda r: (str(r.get("observed_at") or ""), r["id"]), reverse=True)
    gap = sorted(sid for sid, row in rows.items() if rec["ingested"].get(sid) != row["version"])
    return {"items": items[:DELTA_LIMIT], "total": len(items), "more": len(items) > DELTA_LIMIT,
            "unprocessed": gap[:DELTA_LIMIT], "unprocessed_total": len(gap),
            "watermark": rec.get("ingested_at"), "first_use": rec.get("revision", 0) <= 1,
            "legacy": len(rec.get("legacy_ingested") or [])}


def pin(row, criteria, steps, coverage, baseline, actor, at):
    """Minimal sanitized copy of a cited source: no output, transcript or raw stream."""
    keep = {k: row[k] for k in ("id", "kind", "version", "source_ref", "observed_at", "outcome", "label", "counts",
                                "context", "repo", "review", "head", "origin", "provider", "attempt", "checkout")
            if k in row}
    keep.update(criteria=sorted(criteria), steps=sorted(steps), coverage=coverage, baseline=baseline,
                mapped_by=actor, mapped_at=at, pinned=True)
    return keep


def observation(body, at):
    """A developer-recorded context observation; unknown stays unknown."""
    if not isinstance(body, dict):
        raise ValueError("observation must be an object")
    worktree = body.get("worktree", "unknown")
    if worktree not in {"clean", "dirty", "unknown"}:
        raise ValueError("worktree must be clean, dirty or unknown")
    head = body.get("head")
    if head is not None and (not isinstance(head, str) or not _HEAD.fullmatch(head)):
        raise ValueError("head must be a commit ID (7 to 64 hexadecimal characters) or absent")
    out = {"label": "developer-recorded", "at": at, "head": head.lower() if head else None, "worktree": worktree,
           "detached": body.get("detached") is True}
    for key, limit in (("branch", 200), ("config", 200), ("environment", 200), ("repo_id", 300), ("location", 1000)):
        value = body.get(key)
        out[key] = model.text(value, limit) if isinstance(value, str) and value.strip() else None
    if out["location"] and not os.path.isabs(out["location"]):
        raise ValueError("observed location must be absolute")
    return out


def applicable(ctx):
    return {k: ctx.get(k) for k in model.CONTEXT_KEYS}


def manual_row(body, ctx, baseline, at):
    """Self-reported acceptance / code inspection with actor, time, revision and limits."""
    kind = body.get("kind", "acceptance")
    if kind not in model.MANUAL:
        raise ValueError("manual evidence is an acceptance observation or a code inspection")
    if body.get("outcome") not in {"passed", "failed"}:
        raise ValueError("record what was observed: passed or failed")
    text = model.text(body.get("text"), 2000)
    limits = model.text(body.get("limits"), 1000)
    revision = body.get("revision")
    if not text.strip() or not limits.strip():
        raise ValueError("describe the observation and its limits")
    if not isinstance(revision, str) or not _HEAD.fullmatch(revision):
        raise ValueError("record the revision (commit ID) the observation applies to")
    actor = model.text(body.get("actor") or "developer", 100)
    context = applicable(ctx) if (ctx.get("head") or "") == revision.lower() else \
        {"repo_id": ctx.get("repo_id"), "location": ctx.get("location"), "head": revision.lower(),
         "worktree": None, "config": None, "environment": None}
    sid = "%s:%s" % (kind, version(text, revision, actor, at))
    return {"id": sid, "kind": kind, "source_ref": "manual:" + sid, "observed_at": at, "outcome": body["outcome"],
            "label": safe(text, 300), "actor": actor, "revision": revision.lower(), "limits": safe(limits, 500),
            "context": context, "origin": "self-reported", "baseline": baseline}


def map_evidence(rec, rows, body, ctx, at):
    """record_evidence: pin an existing source, or a manual observation, to criteria/steps."""
    baseline = rec["baselines"][rec["baseline"]]
    plan_id = rec.get("approved_plan") or rec.get("selected_plan")
    criteria, steps = body.get("criteria") or [], body.get("steps") or []
    if not isinstance(criteria, list) or not isinstance(steps, list) or not (criteria or steps):
        raise ValueError("map the evidence to at least one criterion or plan step")
    if not set(criteria) <= {c["id"] for c in baseline["criteria"]}:
        raise ValueError("unknown criterion for the current baseline")
    if steps and (not plan_id or not set(steps) <= {s["id"] for s in rec["plans"][plan_id]["steps"]}):
        raise ValueError("unknown step of the approved or selected plan")
    coverage = body.get("coverage", "partial")
    if coverage not in {"complete", "partial"}:
        raise ValueError("coverage is complete or partial for the mapped criteria")
    if body.get("manual") is not None:
        row = manual_row(body["manual"], ctx, baseline["id"], at)
        row.update(criteria=sorted(set(criteria)), steps=sorted(set(steps)), coverage=coverage, pinned=True,
                   mapped_by=row["actor"], mapped_at=at)
        if steps:  # a step id means something only inside its plan version
            row["plan"] = plan_id
        rec["evidence"].append(row)
        return row
    sid = body.get("evidence")
    if not isinstance(sid, str) or sid not in rows:
        raise ValueError("evidence is not available in the current ticket record")
    if rows[sid]["kind"] not in EVIDENCE_KINDS:
        raise ValueError("release reminders and requirement changes are not evidence")
    actor = model.text(body.get("actor") or "developer", 100)
    # Steps are amended only on a pin of the same plan: the same step id in another plan version is another step,
    # so that mapping becomes its own pin. A criteria-only mapping (criteria belong to the baseline, not to a plan)
    # also amends a pin that records no plan, which is every pin saved before plans were recorded.
    same_plan = (plan_id,) if steps else (None, plan_id)
    for e in rec["evidence"]:
        if e.get("id") == sid and e.get("version") == rows[sid]["version"] and e.get("baseline") == baseline["id"] \
                and e.get("plan") in same_plan:
            e["criteria"] = sorted(set(e.get("criteria", [])) | set(criteria))
            e["steps"] = sorted(set(e.get("steps", [])) | set(steps))
            e["coverage"] = coverage
            return e
    row = pin(rows[sid], set(criteria), set(steps), coverage, baseline["id"], actor, at)
    if steps:
        row["plan"] = plan_id
    rec["evidence"].append(row)
    return row


def ledger(rec, ctx, at, note=""):
    """FLOW-16 result: criterion ledger plus three separate measures. Pure over the given context."""
    baseline = rec["baselines"][rec["baseline"]]
    current = applicable(ctx)
    result = model.verification(baseline, rec["evidence"], current)
    plan_id = rec.get("approved_plan") or rec.get("selected_plan")
    plan = rec["plans"].get(plan_id) if plan_id else None
    steps = {}
    for step in (plan or {}).get("steps", []):
        for cid in step["criteria"]:
            steps.setdefault(cid, []).append(step["id"])
    text = {c["id"]: c["text"] for c in baseline["criteria"]}
    for row in result["rows"]:
        row["text"] = safe(text.get(row["criterion"]), 300)
        row["plan_steps"] = steps.get(row["criterion"], [])
        row["evidence"] = row["evidence"][:20]
    groups = model.reconcile(plan, rec["evidence"], current) if plan else None
    out = {"at": at, "baseline": baseline["id"], "plan": plan_id, "context": current,
           "context_digest": model.digest(current), "rows": result["rows"], "note": model.text(note, 2000),
           "measures": {
               "plan_coverage": plan_coverage(rec, plan_id),
               "implementation_progress": model.progress(plan, groups) if plan else None,
               "functional_verification": {"percent": result["percent"], "numerator": result["numerator"],
                                           "denominator": result["denominator"], "gaps": result["gaps"],
                                           "label": "verified criterion weight / applicable criterion weight"}}}
    out["id"] = "v_" + model.digest(out)
    return out


def plan_coverage(rec, plan_id):
    """Latest evaluation of this plan, kept apart from verification; never a Jev score invented here."""
    from . import jev
    rows = [e for e in (rec.get("evaluations") or {}).values()
            if isinstance(e, dict) and e.get("plan") == plan_id and e.get("coverage")]
    return rows[-1]["coverage"] if rows else {"state": "unavailable", "percent": None, "reason": jev.UNAVAILABLE}


def recovery(rec, ctx, tk, rows, card_available):
    """Specific recovery states; none of them checks out, stashes, switches or recreates anything."""
    out = []
    b = rec["binding"]
    obs = (rec.get("observations") or [None])[-1] or {}

    def add(state, reason, action, blocking=True):
        out.append({"state": state, "reason": reason, "action": action, "blocking": blocking})
    if not ctx.get("repository_available"):
        add("repository_missing", "No repository folder at the bound location.",
            "Map the moved clone explicitly; a folder with the same name is not accepted automatically.")
    if obs.get("location") and common.norm_path(obs["location"]) != common.norm_path(b["location"]):
        add("location_moved", "Last observation was recorded at a different location.",
            "Confirm an explicit location mapping before any handoff.")
    if obs.get("repo_id") and obs["repo_id"] != b["repo_id"]:
        add("different_repository", "The observed repository differs from the bound one.",
            "Resolve the repository; a matching folder name is insufficient. Nothing is switched or stashed.")
    if obs.get("detached"):
        add("detached_head", "Detached HEAD in the last observation.",
            "Confirm the intended branch yourself; Chitragupta never checks out a branch.")
    if obs.get("worktree") == "dirty":
        add("dirty_worktree", "Uncommitted changes are not fingerprinted; evidence applicability is unknown.",
            "Review the differences yourself; no automatic switch, stash or reset.", blocking=False)
    branch = obs.get("branch")
    git = (tk.get("git") or {}).get("repos") or {} if isinstance(tk.get("git"), dict) else {}
    for row in ((git.get(repo_key(b["location"])) or {}).get("branches") or []):
        if isinstance(row, dict) and branch and row.get("branch") == branch and row.get("status") == "deleted_remote" \
                and not row.get("local"):
            add("branch_deleted", "Branch %s no longer exists; its commits and PRs stay referenced." % safe(branch, 120),
                "Resolve the branch yourself; Chitragupta never checks out or recreates it.")
    baseline = rec["baselines"][rec["baseline"]]
    if not card_available:
        add("requirements_unavailable", "Jira requirements are not cached or not reachable; freshness unknown.",
            "Review the last authorized baseline and acknowledge its freshness limit.", blocking=False)
    elif ctx.get("requirements_source") != baseline["source_revision"]:
        add("requirements_changed", "Jira requirements changed since the saved baseline.",
            "Record a new reviewed baseline before any handoff.")
    same = {"stale": "requirements_changed", "blocked": "repository_missing"}
    for name in model.OVERLAYS:
        overlay = rec["overlays"].get(name)
        if overlay and not any(o["state"] in {name, same.get(name)} for o in out):
            add(name, overlay.get("reason") or name, overlay.get("recovery") or "Review", name != "cancelled")
    return out


def obligations(rec, current, rows):
    """Unfinished work retained when an observed PR moves the stage to review."""
    plan_id = rec.get("approved_plan") or rec.get("selected_plan")
    if not plan_id:
        return []
    groups = model.reconcile(rec["plans"][plan_id], rec["evidence"], current)
    gaps = model.verification(rec["baselines"][rec["baseline"]], rec["evidence"], current)["gaps"]
    out = []
    if groups["pending"] or groups["uncertain"]:
        out.append({"kind": "implementation", "steps": groups["pending"] + groups["uncertain"]})
    if gaps or groups["verification"]:
        out.append({"kind": "verification", "criteria": gaps, "steps": groups["verification"]})
    return out


def _observe(rec, rows, tk, stale_intent):
    """Mutates the loaded record; True when a checkpoint is warranted."""
    from . import companion
    fresh = ingest(rec, tk, rows)
    changed = bool(fresh)
    now = common.now_iso()
    if stale_intent is not None:
        model.dispatch(stale_intent, "unknown")
        stale_intent.setdefault("dispatch_history", []).append(
            {"state": "unknown", "at": now, "reason": "no acknowledgement before expiry"})
        companion.overlay(rec, "execution_status_unknown", "Handoff was not acknowledged before it expired.",
                          "Record whether it was submitted; nothing is resent automatically.")
        changed = True
    current = applicable(rec.get("context") or {})
    # Each open PR moves the stage once; an explicit continuation back to development is not bounced.
    open_prs = sorted(sid for sid, r in rows.items() if r["kind"] == "pr" and r["outcome"] in {"open", "draft"}
                      and sid not in rec["pr_applied"])
    if open_prs and rec["stage"] in {"in_development", "awaiting_verification"}:
        rec["obligations"] = obligations(rec, current, rows)
        companion.move(rec, "pr_review", "observed_pr", "observed " + ", ".join(open_prs[:5]))
        rec["next_action"] = "Review existing PR; unfinished work stays listed"
        rec["pr_applied"] = sorted(set(rec["pr_applied"]) | set(open_prs))
        changed = True
    elif open_prs and rec["stage"] == "paused" and rec.get("resume_stage") in {"in_development", "awaiting_verification"}:
        rec["obligations"] = obligations(rec, current, rows)
        rec["history"].append({"from": "paused", "to": "paused", "trigger": "observed_pr", "at": now,
                               "note": "resume stage %s -> pr_review" % rec["resume_stage"]})
        rec["resume_stage"] = "pr_review"
        rec["pr_applied"] = sorted(set(rec["pr_applied"]) | set(open_prs))
        changed = True
    key = "requirements:" + rec["binding"]["ticket"]
    marker = rows.get(key)
    baseline = rec["baselines"][rec["baseline"]]
    drifted = bool(marker) and marker["version"] != version(baseline["source_revision"])
    if drifted and not rec["overlays"]["stale"]:
        companion.overlay(rec, "stale", "Jira requirements changed since the saved baseline.",
                          "Record a new reviewed baseline before any handoff.")
        changed = True
    elif marker and not drifted and rec["overlays"]["stale"]:
        rec["overlays"]["stale"] = None
        changed = True
    missing = not os.path.isdir(rec["binding"]["location"])
    blocked = rec["overlays"]["blocked"]
    if missing and not blocked:
        companion.overlay(rec, "blocked", "Repository folder not found at the bound location.",
                          "Map the moved clone explicitly; a same-named folder is not accepted automatically.")
        changed = True
    elif not missing and blocked and blocked.get("reason", "").startswith("Repository folder"):
        rec["overlays"]["blocked"] = None
        changed = True
    if changed:
        if rec.get("owner") and any(rows[s]["kind"] in {"commit", "file", "test"} for s in fresh):
            rec["owner"]["observed_at"] = now  # new captured activity; still not proof of completion
        rec["ingested_at"] = now
    return changed


def tick(cfg, touched=(), limit=TICK_LIMIT):
    """Bounded tick step: checkpoint newly ingested evidence for open workflows. Never acknowledges,
    never calls a model, provider or git, and does nothing while capture is paused."""
    if os.path.exists(common.wl("PAUSED")) or not os.path.isdir(common.hp("data", "workflows")):
        return 0
    from . import companion
    touched, done = set(touched or ()), 0
    for wid in workflowstore.ids():
        if done >= limit:
            break
        try:
            rec = workflowstore.load(wid)
            if not rec or rec.get("archived") or rec["stage"] == "completed":
                continue
            intent = rec["approvals"][-1] if rec["approvals"] else None
            stale = intent if intent and intent.get("dispatch") == "dispatching" and \
                companion.intent_state(intent) == "unknown" else None
            if rec["binding"]["ticket"] not in touched and stale is None:
                continue
            companion.authorize(rec, cfg)
            tk, rows = current_sources(rec)
            if _observe(rec, rows, tk, stale):
                rec["reason"] = "tick: evidence checkpoint"
                workflowstore.save(rec, rec["revision"], lambda r: companion.authorize(r, cfg), timeout=1.0)
                done += 1
        except (PermissionError, workflowstore.Conflict):
            continue  # revoked scope or a concurrent explicit save: nothing ingested under the wrong identity
        except (ValueError, OSError) as exc:
            common.log("companion", "workflow tick skipped %s: %s" % (wid[:14], exc))
    return done
