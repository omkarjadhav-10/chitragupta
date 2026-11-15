"""Companion plan jobs: Generate Plan, Improve Plan and plan evaluation as explicit, coalesced, budgeted jobs.

Enqueue is one short transaction under the workflow writer lock (workflowstore.save's authorize hook runs after
the revision check, inside the lock): scope check, coalescing (one active mutating job per workflow; a repeated
click returns it), budget reservation, the job record (data/workflows/<wid>/jobs/<job>.json) and the checkpoint.
The runner (`agent companion-job <JOB>`) starts after the lock is released and never holds it across the model
call. Publication revalidates cancellation, scope, profile generation, selection and baseline under the lock: a
late, cancelled or stale result never becomes current. A valid plan is saved before any evaluation. Reads and
status never enqueue, retry or reconcile. Nothing here executes a plan or writes to Jira or a repository.
"""
import os
import re
import time

from . import common, companion as service, companionplanning as planning, jev, modelbudget, \
    workflowmodel as model, workflowstore
from .safety import workflowio

STATES = ("queued", "running", "succeeded", "partial", "failed", "cancel_requested", "cancelled", "outcome_unknown",
          "superseded")
ACTIVE = {"queued", "running", "cancel_requested"}
TERMINAL = set(STATES) - ACTIVE
KINDS = {"generate": "companion_plan", "improve": "companion_improve", "evaluate": "jev"}
PROMPT = "companion_plan"
JOB = re.compile(r"^cj_[a-f0-9]{32}$")
TIMEOUT_CAP = 180
SLACK_S = 120  # after the call's own timeout, a silent runner counts as interrupted (outcome unknown)
EDITABLE = {"planned", "review_incomplete", "plan_reviewed", "approved"}
clock = time.time  # tests freeze it


class _Abort(Exception):
    """Leave the transaction without a checkpoint."""


def path(wid, jid):
    return os.path.join(workflowstore.root(wid), "jobs", jid + ".json")


def load(wid, jid):
    if not isinstance(jid, str) or not JOB.fullmatch(jid):
        raise ValueError("invalid job ID")
    job = common.read_json(path(wid, jid), None)
    if not isinstance(job, dict) or job.get("id") != jid:
        raise ValueError("job not found")
    return job


def jobs(wid):
    """This workflow's job records, oldest first. A read: nothing is reconciled or retried."""
    try:
        names = os.listdir(os.path.join(workflowstore.root(wid), "jobs"))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        if name.endswith(".json") and JOB.fullmatch(name[:-5]):
            try:
                out.append(load(wid, name[:-5]))
            except ValueError:
                continue
    return sorted(out, key=lambda j: (j.get("seq") or 0, j.get("created_at") or "", j["id"]))


def locate(jid):
    if not isinstance(jid, str) or not JOB.fullmatch(jid):
        return None
    return next((wid for wid in workflowstore.ids() if os.path.exists(path(wid, jid))), None)


def public(job):
    """A job without its transient packet (prompt buffers never leave the runner)."""
    return {k: v for k, v in job.items() if k not in ("packet_text",)}


def ref(job):
    return {"kind": job["kind"], "status": job["state"], "attempt": job["attempt"], "at": job["created_at"],
            "result": job.get("result") or {}}


def _write(job):
    workflowio.durable_json(path(job["workflow"], job["id"]), job)


def _note(job, state, why):
    job["state"] = state
    job["updated_at"] = common.now_iso()
    job.setdefault("history", []).append({"state": state, "at": job["updated_at"], "reason": str(why)[:300]})
    if state in TERMINAL:
        job.pop("packet_text", None)  # transient buffers expire on job end
        job["reason"] = str(why)[:300]


def _spend(job, how, usd=None):
    """Ledger outcome of this job's reservation; never releases an ambiguous one as known-free."""
    rid = job.get("reservation")
    if not rid:
        return
    try:
        if how == "release":
            modelbudget._release(rid)
        elif how == "unknown":
            modelbudget._mark_unknown(rid)
        else:
            modelbudget._settle(rid, usd or 0.0)
    except ValueError:
        pass  # already settled / unknown: idempotent


def timeout(cfg):
    from . import configstore
    try:
        value = int(configstore.get(cfg or {}, "companion.timeout_s", TIMEOUT_CAP))
    except (TypeError, ValueError):
        value = TIMEOUT_CAP
    return max(30, min(TIMEOUT_CAP, value))


def _reconcile(r):
    """On explicit actions only: a runner silent past its deadline left its outcome unknown; a queued job whose
    reservation expired never ran. Mutates job files (lock held) and the record's job references."""
    now, changed = clock(), False
    for job in jobs(r["id"]):
        cancelled = (r["jobs"].get(job["id"]) or {}).get("status") == "cancelled"  # workflow-level cancel
        if cancelled and job["state"] == "queued":
            _spend(job, "release")
            _note(job, "cancelled", "cancelled with the task operation; nothing was sent")
        elif cancelled and job["state"] == "running":
            _note(job, "cancel_requested", "cancelled with the task operation; the result will not be published")
            _write(job)
            changed = True
            continue
        elif job["state"] in ("running", "cancel_requested") and now > float(job.get("deadline_epoch") or now + 1):
            _spend(job, "unknown")
            _note(job, "outcome_unknown", "the runner stopped before reporting; the call may have been accepted")
        elif job["state"] == "queued" and now > float(job.get("reservation_expires") or now + 1):
            _spend(job, "release")
            _note(job, "failed", "reservation expired before the runner started; request it again")
        else:
            continue
        _write(job)
        changed = True
        r["jobs"][job["id"]] = ref(job)
        if job["kind"] == "generate" and r["stage"] == "planning":
            service.move(r, "new", "planning_failed", "planning job ended: " + job["state"])
    return changed


def reconcile(wid, cfg=None):
    """Explicit actions only (never reads): persist _reconcile as its own checkpoint. -> saved record or None."""
    rec = workflowstore.load(wid)
    if not rec:
        return None

    def hook(r):
        service.authorize(r, cfg)
        if not _reconcile(r):
            raise _Abort()
        r["reason"] = "job reconciliation"
    try:
        return workflowstore.save(rec, rec["revision"], hook)
    except (_Abort, workflowstore.Conflict):
        return None


def _rebased(wid, revision, cfg):
    """The caller's revision, moved past a reconciliation checkpoint made for this very click."""
    before = (workflowstore.load(wid) or {}).get("revision")
    saved = reconcile(wid, cfg)
    return saved["revision"] if saved and revision == before else revision


def _active(wid, rec=None):
    refs = (rec or {}).get("jobs") or {}
    return [j for j in jobs(wid) if j["state"] in ACTIVE and (refs.get(j["id"]) or {}).get("status") != "cancelled"]


def _result(rec, job, coalesced=False, note=""):
    out = dict(rec)
    out.update(job=public(job), coalesced=coalesced)
    if note:
        out["notice"] = note
    return out


def _guard(r, kind):
    if r.get("archived"):
        raise ValueError("workflow archived; start a new attempt instead")
    if kind == "generate":
        if r["plans"] or r["stage"] != "new":
            raise ValueError("a plan already exists for this task: use Improve Plan, or save a manual version")
    elif kind == "improve":
        if not r["selected_plan"] or r["stage"] not in EDITABLE:
            raise ValueError("Improve Plan needs a selected plan before execution starts (the task is %s)" % r["stage"])
        service.require_settled(r)
    elif kind == "evaluate":
        if not r["selected_plan"] or r["stage"] not in EDITABLE:
            raise ValueError("evaluate a selected plan before execution starts")


def latest_evaluation(rec, plan_id, usable=True):
    rows = [e for k, e in sorted((rec.get("evaluations") or {}).items()) if isinstance(e, dict)
            and e.get("plan") == plan_id and (not usable or e.get("answers"))]
    return rows[-1] if rows else None


def _database(body, cfg):
    flow, inv = body.get("flow"), body.get("investigation")
    if not flow and not inv:
        return None
    try:
        from . import dbaccess, dbtrace
        out = dbtrace.database_context(dbaccess.AccessContext(cfg), flow, inv, cfg=cfg)
        return out.get("database_context") or {"available": False, "reason": out.get("reason") or "unavailable"}
    except (ValueError, OSError, KeyError, TypeError):
        return {"available": False, "reason": "Database context unavailable"}


# ---------------------------------------------------------------- enqueue

def enqueue(wid, revision, kind, cfg=None, retry=False, database=None, spawn=None):
    """Explicit Generate Plan / Improve Plan / Evaluate. Returns the workflow with `job` and `coalesced`."""
    cfg = cfg if cfg is not None else common.load_config()
    if kind not in KINDS:
        raise ValueError("unknown job kind")
    service.get(wid)  # scope first: nothing is reconciled or written for a workflow you may not see
    revision = _rebased(wid, revision, cfg)
    rec = service.get(wid)
    live = [j for j in _active(wid, rec) if j["kind"] == kind]
    if live and (not isinstance(revision, int) or isinstance(revision, bool) or rec["revision"] != revision):
        return _respawn(rec, live[0], retry, cfg, spawn)  # a repeated click: the job it already started
    if not isinstance(revision, int) or isinstance(revision, bool) or rec["revision"] != revision:
        raise workflowstore.Conflict(rec, {"expected_revision": revision})
    purpose = KINDS[kind]
    ceiling, why = modelbudget.call_ceiling(cfg, purpose)
    if kind == "evaluate":
        reason = jev.preflight(cfg, acknowledgement())
        if reason:
            raise ValueError(reason)
    if ceiling is None:
        raise ValueError(why)
    _guard(rec, kind)
    text, data = "", {"evidence": {"files": []}}
    if kind != "evaluate":
        tk = service.ticket(rec["binding"]["ticket"], cfg)
        evaluation = latest_evaluation(rec, rec["selected_plan"]) if kind == "improve" else None
        text, data = planning.packet(rec, tk, "improve" if kind == "improve" else "generate", evaluation, database)
    box = {}

    def tx(r):  # inside the writer lock, after the revision check
        service.authorize(r, cfg)
        _reconcile(r)
        busy = _active(wid, r)
        if busy:
            box["existing"] = busy[0]
            raise _Abort()
        prior = [j for j in jobs(wid) if j["kind"] == kind]
        if prior and prior[-1]["state"] == "outcome_unknown" and retry is not True:
            raise ValueError("Outcome unknown for attempt %s: the provider may have processed it. Retry explicitly "
                             "(possible duplicate cost); nothing is resent automatically." % prior[-1]["attempt"])
        _guard(r, kind)
        jid = "cj_" + os.urandom(16).hex()
        plan_id = r["selected_plan"] if kind != "generate" else None
        digest = model.digest({"kind": kind, "workflow": wid, "baseline": r["baseline"], "plan": plan_id,
                               "packet": model.digest(text), "rubric": jev.RUBRIC})
        res = modelbudget._reserve(purpose, ceiling, cfg, task=r["binding"]["ticket"], job=jid)
        now = common.now_iso()
        job = {"id": jid, "workflow": wid, "kind": kind, "purpose": purpose, "state": "queued",
               "seq": len(jobs(wid)) + 1,
               "attempt": "at_" + model.digest([wid, kind, digest, len(prior)])[:24], "input_digest": digest,
               "baseline": r["baseline"], "plan": plan_id, "revision": r["revision"] + 1,
               "profile_generation": r["binding"]["profile_generation"], "reservation": res["id"],
               "reservation_usd": res["usd"], "reservation_expires": res["expires_epoch"], "created_at": now,
               "updated_at": now, "packet_text": text, "packet_digest": model.digest(text),
               "evidence_files": [f["path"] for f in data["evidence"]["files"]], "omitted": data.get("omitted", []),
               "retry_of": prior[-1]["id"] if prior and retry is True else None, "result": {},
               "history": [{"state": "queued", "at": now, "reason": "explicit %s request" % kind}]}
        try:
            _write(job)
        except (OSError, ValueError):
            modelbudget._release(res["id"])
            raise
        r["jobs"][jid] = ref(job)
        if kind == "generate":
            service.move(r, "planning", "generate", "Generate Plan queued (%s)" % job["attempt"])
        elif kind == "evaluate" and r["stage"] == "planned":
            service.move(r, "review_incomplete", "open_review", "evaluation requested")
        r["next_action"] = "Plan job queued; it runs in the background and you can cancel it"
        r["reason"] = "%s job queued" % kind
        box["job"] = job
    try:
        saved = workflowstore.save(rec, revision, tx)
    except _Abort:
        if box["existing"]["kind"] != kind:
            raise ValueError("A %s job is still active for this task (attempt %s): wait for it or cancel it first"
                             % (box["existing"]["kind"], box["existing"]["attempt"])) from None
        return _respawn(service.get(wid), box["existing"], retry, cfg, spawn)
    except workflowstore.Conflict:
        live = [j for j in _active(wid, service.get(wid)) if j["kind"] == kind]
        if live:
            return _respawn(service.get(wid), live[0], retry, cfg, spawn)
        raise
    except OSError:
        if box.get("job"):
            _abandon(box["job"])
        raise
    return _start(saved, box["job"], cfg, spawn)


def _abandon(job):
    """The checkpoint did not publish: the job never ran and its estimate is freed."""
    try:
        with workflowio.lock():
            job = load(job["workflow"], job["id"])
            _spend(job, "release")
            _note(job, "failed", "not saved; nothing was sent")
            _write(job)
    except (OSError, ValueError, TimeoutError):
        common.log("companion", "job %s left queued after a failed save" % job["id"])


def _start(rec, job, cfg, spawn):
    try:
        (spawn or spawn_runner)(job["id"], cfg)
    except OSError as exc:
        common.log("companion", "runner for %s did not start: %s" % (job["id"], exc))
        return _result(rec, job, note="Queued, but the runner did not start; request it again with retry")
    return _result(rec, job)


def _respawn(rec, job, retry, cfg, spawn):
    """A repeated click focuses the running job. Only an explicit retry restarts an undispatched queued job
    within its unexpired reservation (for example after a restart)."""
    if retry is True and job["state"] == "queued" and not job.get("dispatched_at") \
            and clock() < float(job.get("reservation_expires") or 0):
        return _start(rec, job, cfg, spawn)
    return _result(rec, job, coalesced=True)


def spawn_runner(jid, cfg=None):
    """Start `agent companion-job <JOB>` detached and windowless (safety.commands.spawn, purpose self)."""
    from .safety import commands
    cfg = common.load_config() if cfg is None else cfg
    argv = common.self_command("agent", "companion-job", jid, cfg=cfg, windowless=True)
    commands.spawn(argv, cwd=common.home() if os.path.isdir(common.home()) else None, cfg=cfg)


# ---------------------------------------------------------------- runner

def _claude(kind, text, cfg, timeout=None):
    from . import extract
    return extract.call_claude(kind, text, cfg, timeout=timeout)


def _classify(exc):
    """(state, sanitized category): never the provider's raw text."""
    message = str(exc).lower()
    if isinstance(exc, TimeoutError) or "rc=124" in message or "timeout" in message:
        return "outcome_unknown", "timeout after possible acceptance"
    if getattr(exc, "limit", False):
        return "failed", "rate limited or quota reached"
    return "failed", "provider failure"


def _stop(rec, job, cfg):
    if rec is None:
        return "failed", "workflow missing"
    if job["state"] == "cancel_requested" or (rec["jobs"].get(job["id"]) or {}).get("status") == "cancelled":
        return "cancelled", "cancelled before dispatch"
    try:
        service.authorize(rec, cfg)
    except PermissionError:
        return "superseded", "access changed; nothing was sent"
    if rec["binding"]["profile_generation"] != job["profile_generation"]:
        return "superseded", "profile changed; nothing was sent"
    if rec["baseline"] != job["baseline"]:
        return "superseded", "requirements changed before dispatch; nothing was sent"
    if job["kind"] != "generate" and rec["selected_plan"] != job["plan"]:
        return "superseded", "the selected plan changed before dispatch; nothing was sent"
    if clock() > float(job.get("reservation_expires") or 0):
        return "failed", "reservation expired before dispatch; request it again"
    return None


def run(jid, cfg=None, caller=None, transport=None):
    """Run one queued job once. `caller` replaces extract.call_claude and `transport` the blocked Jev transport
    (tests only). Returns the final job record."""
    cfg = cfg if cfg is not None else common.load_config()
    wid = locate(jid)
    if wid is None:
        return None
    with workflowio.lock():
        job = load(wid, jid)
        if job["state"] != "queued":
            return public(job)
        stop = _stop(workflowstore.load(wid), job, cfg)
        if stop:
            _spend(job, "release")
            _note(job, *stop)
            _write(job)
            return public(job)
        limit = timeout(cfg)
        job.update(dispatched_at=common.now_iso(), deadline_epoch=clock() + limit + SLACK_S)
        _note(job, "running", "dispatched" if job["kind"] != "evaluate" else "evaluation started")
        _write(job)
    if job["kind"] == "evaluate":
        return _evaluate(wid, jid, job["plan"], cfg, transport, own_reservation=True)
    out, cost, failure = None, None, None
    try:
        out, cost = (caller or _claude)(PROMPT, job["packet_text"], cfg, timeout=limit)
    except Exception as exc:  # noqa: BLE001 - any failure of the call is recorded, never raised into the agent
        failure = _classify(exc)
    return _publish(wid, jid, out, cost, failure, cfg, transport)


def _prepare(wid, jid, out):
    """(version, problem): validated plan version, or why the output is only an incomplete draft."""
    rec, job = workflowstore.load(wid), load(wid, jid)
    base = rec["baselines"].get(job["baseline"])
    try:
        body = planning.validate_output(out, base, job.get("evidence_files") or [],
                                        origin={"job": jid, "attempt": job["attempt"], "kind": job["kind"]})
        return model.plan(body, base, job.get("plan"), source="claude"), None
    except ValueError as exc:
        return None, str(exc)[:300]


def _commit(wid, jid, cfg, mutate, reason, spend=("settle", None)):
    """One checkpoint whose hook re-reads the job inside the writer lock. Revoked access publishes nothing:
    the job alone records it (its reservation settled per `spend`). -> (saved record or None, job)."""
    for _ in range(3):
        rec = workflowstore.load(wid)
        box = {}

        def hook(r):
            job = load(wid, jid)
            box["job"] = job
            try:
                service.authorize(r)  # the config as it is now, never the one loaded at dispatch
                if r["binding"]["profile_generation"] != job["profile_generation"]:
                    raise PermissionError("profile changed")
            except PermissionError:
                raise _Abort()
            mutate(r, job)
            r["jobs"][jid] = ref(job)
            r["reason"] = reason
            _write(job)
        try:
            return workflowstore.save(rec, rec["revision"], hook), box["job"]
        except workflowstore.Conflict:
            continue
        except _Abort:
            with workflowio.lock():
                job = load(wid, jid)
                if job["state"] not in TERMINAL:
                    _spend(job, *spend)
                    _note(job, "superseded", "access changed during the job; nothing was published")
                    _write(job)
            return None, job
    raise TimeoutError("workflow kept changing; the job result was not published")


def _cancelled(r, job):
    return job["state"] == "cancel_requested" or (r["jobs"].get(job["id"]) or {}).get("status") == "cancelled"


def _back(r, job, why):
    if job["kind"] == "generate" and r["stage"] == "planning":
        service.move(r, "new", "planning_failed", why)


def _publish(wid, jid, out, cost, failure, cfg, transport):
    version, problem = (None, None) if failure else _prepare(wid, jid, out)

    def mutate(r, job):
        job["spent"] = cost
        if _cancelled(r, job):
            _spend(job, "settle", cost)
            _note(job, "cancelled", "cancelled; the late result was not published")
            _back(r, job, "planning cancelled")
            return
        if failure:
            _spend(job, "unknown" if failure[0] == "outcome_unknown" else "settle", cost)
            _note(job, *failure)
            _back(r, job, "planning job ended: " + failure[1])
            r["next_action"] = ("Outcome unknown: retry explicitly (possible duplicate cost)"
                                if failure[0] == "outcome_unknown" else "Plan job failed: " + failure[1])
            return
        _spend(job, "settle", cost)
        if problem:
            did = "d_" + model.digest([jid, problem])[:24]
            r.setdefault("drafts", {})[did] = {"job": jid, "attempt": job["attempt"], "state": "incomplete",
                                               "reason": problem, "at": common.now_iso(),
                                               "label": "Incomplete draft; not selectable for approval"}
            job["result"] = {"draft": did}
            _note(job, "partial", problem)
            _back(r, job, "incomplete generated draft")
            r["next_action"] = "Generated output was incomplete; retry or save a manual plan"
            return
        service.add_plan(r, version)
        job["result"] = {"plan": version["id"]}
        if r["baseline"] != job["baseline"]:
            _note(job, "superseded", "requirements changed during the job; kept as a historical version")
            return
        if job["kind"] == "generate" and r["stage"] == "planning" and not r["selected_plan"]:
            r["selected_plan"] = version["id"]
            service.move(r, "planned", "plan_saved", "generated plan saved (%s)" % job["attempt"])
            r["next_action"] = "Review the generated plan"
        else:  # never silently replaces the selected or a manual version
            parent = r["plans"].get(job.get("plan") or r["selected_plan"] or "")
            r.setdefault("candidates", {})[version["id"]] = {
                "parent": parent["id"] if parent else None, "job": jid, "attempt": job["attempt"],
                "at": common.now_iso(), "state": "candidate",
                "diff": planning.diff(parent, version) if parent else None}
            r["next_action"] = "Review the candidate and its diff; Select it to replace the selected plan"
        job["phase"] = "evaluating"
        _note(job, "running", "plan saved before evaluation")
    how = "unknown" if failure and failure[0] == "outcome_unknown" else "settle"
    saved, job = _commit(wid, jid, cfg, mutate, "plan job result", (how, cost))
    if saved is None or job["state"] in TERMINAL:
        return public(job)
    return _evaluate(wid, jid, job["result"]["plan"], cfg, transport)


# ---------------------------------------------------------------- evaluation (Jev adapter; transport blocked)

def acknowledgement():
    return common.read_json(common.hp("data", "workflows", ".jev-ack.json"), {}) or {}


def _eval_id(r, body):
    return "e_%04d_%s" % (len(r.get("evaluations") or {}) + 1, model.digest(body)[:24])


def _evaluate(wid, jid, plan_id, cfg, transport, own_reservation=False):
    """One bounded evaluation attempt of a saved plan. Unavailable (policy, budget, refusal or the blocked
    transport) is recorded as `Evaluation unavailable`, never 0% and never passing."""
    rec = workflowstore.load(wid)
    job = load(wid, jid)
    plan = rec["plans"][plan_id]
    base = rec["baselines"][plan["baseline"]]
    reason, result, res_id, built = jev.preflight(cfg, acknowledgement()), None, None, None
    if not reason:
        ceiling, reason = modelbudget.call_ceiling(cfg, "jev")
    if not reason:
        try:
            built = jev.build_request(base, plan)
        except ValueError as exc:
            reason = str(exc)
    if not reason and not own_reservation:
        try:
            res_id = modelbudget.reserve("jev", ceiling, cfg, task=rec["binding"]["ticket"])["id"]
        except modelbudget.Refused as exc:
            reason = str(exc)
    elif own_reservation:
        res_id = job.get("reservation")
    if not reason:
        def cancelled():
            j = load(wid, jid)
            return j["state"] == "cancel_requested"

        def check():
            service.authorize(workflowstore.load(wid))  # current config before each dispatch
        try:
            result = jev.evaluate(built, base, transport=transport, cancelled=cancelled, check=check)
        except PermissionError as exc:
            reason = str(exc) or jev.UNAVAILABLE
    if res_id:
        try:
            if result is None or result["attempts"] == 0:
                modelbudget.release(res_id)
            elif result["state"] == "outcome_unknown":
                modelbudget.mark_unknown(res_id)
            else:
                modelbudget.settle(res_id, (result.get("evaluation") or {}).get("usage", {}).get("cost_usd")
                                   or modelbudget.reservation(res_id).get("usd"))
        except ValueError:
            pass
    if result and result.get("evaluation"):
        evaluation = dict(result["evaluation"])
        if result["state"] not in ("succeeded", "partial"):
            evaluation.update(jev.unavailable(base, "Evaluation %s" % result["category"]))
    elif result:
        evaluation = jev.unavailable(base, "Evaluation %s: %s" % (result["state"].replace("_", " "), result["category"]))
    else:
        evaluation = jev.unavailable(base, reason or jev.UNAVAILABLE)
    evaluation.update(plan=plan_id, baseline=base["id"], job=jid, attempt=job["attempt"],
                      audit=(result or {}).get("audit") or {"state": "not sent", "reason": (reason or "")[:200]})

    def mutate(r, j):
        if _cancelled(r, j):
            _note(j, "cancelled", "cancelled; the evaluation was not published")
            return
        body = dict(evaluation, at=common.now_iso(), stale=r["baseline"] != base["id"])
        eid = _eval_id(r, body)
        r["evaluations"][eid] = dict(body, id=eid)
        j["result"] = dict(j.get("result") or {}, evaluation=eid)
        j["evaluation_state"] = body["state"]
        if result and result["state"] == "outcome_unknown":
            _note(j, "outcome_unknown", "evaluation timed out after possible acceptance; plan kept")
        elif j["kind"] == "evaluate" and body["state"] == "unavailable":
            _note(j, "failed", body["coverage"].get("reason") or jev.UNAVAILABLE)
        else:
            _note(j, "partial" if body["state"] == "partial" else "succeeded",
                  "plan saved; evaluation " + body["state"])
    saved, job = _commit(wid, jid, cfg, mutate, "evaluation recorded")
    return public(job)


# ---------------------------------------------------------------- explicit actions

def cancel_job(wid, revision, jid):
    """Idempotent. A queued job is cancelled (estimate released); a running one becomes cancel_requested: its
    late result is never published. Planning returns to its prior stage. Nothing is rolled back."""
    service.get(wid)
    job = load(wid, jid)
    if job["state"] in TERMINAL or job["state"] == "cancel_requested":
        return _result(service.get(wid), job)
    revision = _rebased(wid, revision, None)
    rec = service.get(wid)
    if not isinstance(revision, int) or isinstance(revision, bool) or rec["revision"] != revision:
        raise workflowstore.Conflict(rec, {"expected_revision": revision})
    box = {}

    def tx(r):
        service.authorize(r)
        j = load(wid, jid)
        box["job"] = j
        if j["state"] in TERMINAL or j["state"] == "cancel_requested":
            raise _Abort()
        if j["state"] == "queued":
            _spend(j, "release")
            _note(j, "cancelled", "cancelled before dispatch")
        else:
            _note(j, "cancel_requested", "cancel requested; a call already sent cannot be recalled, its result "
                                         "will not be published")
        _write(j)
        r["jobs"][jid] = ref(j)
        _back(r, j, "planning cancelled")
        r["next_action"] = "Job cancelled; kept versions are unchanged"
        r["reason"] = "job cancelled"
    try:
        saved = workflowstore.save(rec, revision, tx)
    except _Abort:
        return _result(service.get(wid), box["job"])
    return _result(saved, box["job"])


def select_candidate(wid, revision, plan_id):
    """Explicit: a candidate never replaces the selected version by itself."""
    def change(r):
        cand = (r.get("candidates") or {}).get(plan_id)
        if not cand or plan_id not in r["plans"] or cand.get("state") != "candidate":
            raise ValueError("not a candidate plan of this task")
        if r["plans"][plan_id]["baseline"] != r["baseline"]:
            raise ValueError("the candidate belongs to an older requirement baseline; rebase the plan explicitly")
        if r["stage"] not in EDITABLE:
            raise ValueError("select a candidate before execution starts (the task is %s)" % r["stage"])
        if r["stage"] == "approved":
            service.supersede(r, "candidate selected before dispatch")
        service.move(r, "review_incomplete", "improve", "candidate selected")
        r["selected_plan"] = plan_id
        cand.update(state="selected", selected_at=common.now_iso())
        r["next_action"] = "Review the selected plan"
    return service._change(wid, revision, change, "candidate selected")


def manual_assessment(wid, revision, plan_id, classifications, note=""):
    """Developer per-criterion classification: a separate `developer assessment`, never labeled Jev."""
    def change(r):
        if plan_id not in r["plans"]:
            raise ValueError("unknown plan")
        base = r["baselines"][r["plans"][plan_id]["baseline"]]
        ids = {c["id"] for c in base["criteria"]}
        if not isinstance(classifications, dict) or not classifications or not set(classifications) <= ids \
                or not all(v in model.COVERAGE for v in classifications.values()):
            raise ValueError("classify known criteria as Covered, Partial, Missing or Unverified")
        coverage = model.score(base, classifications)
        coverage["label"] = "Developer assessment: " + coverage["label"].lower()
        body = {"plan": plan_id, "baseline": base["id"], "origin": "developer assessment", "provider": None,
                "model": None, "rubric": base["rubric"], "state": "available",
                "answers": {k: {"status": v, "origin": jev.ORIGINS["developer"]} for k, v in sorted(classifications.items())},
                "coverage": coverage, "note": model.text(note if isinstance(note, str) else "", 2000),
                "at": common.now_iso(), "stale": r["baseline"] != base["id"]}
        eid = _eval_id(r, body)
        r["evaluations"][eid] = dict(body, id=eid)
        r["next_action"] = "Developer assessment recorded; review or execute when ready"
    return service._change(wid, revision, change, "developer assessment recorded")


def evaluate_plan(wid, revision, cfg=None, spawn=None):
    """Explicit evaluation of the selected plan. With Jev off or blocked (today) it records `Evaluation
    unavailable` at once; nothing is reserved, queued or sent."""
    cfg = cfg if cfg is not None else common.load_config()
    reason = jev.preflight(cfg, acknowledgement())
    if not reason:
        return enqueue(wid, revision, "evaluate", cfg, spawn=spawn)

    def change(r):
        if not r["selected_plan"] or r["stage"] not in EDITABLE:
            raise ValueError("evaluate a selected plan before execution starts")
        plan = r["plans"][r["selected_plan"]]
        base = r["baselines"][plan["baseline"]]
        body = dict(jev.unavailable(base, reason), plan=plan["id"], baseline=base["id"], at=common.now_iso(),
                    audit={"state": "not sent", "reason": reason[:200]}, stale=False)
        eid = _eval_id(r, body)
        r["evaluations"][eid] = dict(body, id=eid)
        if r["stage"] == "planned":
            service.move(r, "review_incomplete", "open_review", "evaluation requested; unavailable")
        r["next_action"] = "Evaluation unavailable: review manually, improve, or execute with acknowledgement"
    return service._change(wid, revision, change, "evaluation unavailable")


def action(body):
    name = body.get("action")
    wid, rev = body.get("workflow"), body.get("revision")
    if name in ("generate_plan", "improve_plan"):
        cfg = common.load_config()
        return enqueue(wid, rev, "generate" if name == "generate_plan" else "improve", cfg,
                       retry=body.get("retry") is True, database=_database(body, cfg))
    if name == "cancel_job":
        return cancel_job(wid, rev, body.get("job"))
    if name == "select_candidate":
        return select_candidate(wid, rev, body.get("plan_id"))
    if name == "manual_assessment":
        return manual_assessment(wid, rev, body.get("plan_id"), body.get("classifications"), body.get("note", ""))
    if name == "accept_requirement":
        return planning.accept_requirement(wid, rev, body.get("proposals") or [], body.get("clarification"),
                                           body.get("rationale", ""))
    if name == "evaluate_plan":
        return evaluate_plan(wid, rev)
    raise ValueError("unknown plan job action")


def agent_main(argv):
    """`agent companion-job <JOB>`: run one queued job, then move this process's noted model spend into health."""
    jid = argv[0] if argv else ""
    if not JOB.fullmatch(jid) or not locate(jid):
        print("no such job")
        return 2
    cfg = common.load_config()
    job = run(jid, cfg)
    print("job %s: %s" % (jid, (job or {}).get("state")))
    if modelbudget._PENDING:
        from .agent import Lock
        lock = Lock()
        if lock.acquire_wait(60):
            try:
                health = common.read_json(common.wl("health.json"), {}) or {}
                modelbudget.drain(health, cfg)
                common.write_json(common.wl("health.json"), health)
            finally:
                lock.release()
    return 0
