"""Dashboard adapter for the Companion: GET reads through the shared projections, one protected POST action.

GET /api/companion?operation=&workflow=&ticket=&cursor= — `ticket` is the operation's one target argument: a ticket
key (snapshot selection, options, proposal), a plan ID (plan, diff, editor), a draft version ID (draft_version), a
planner group, or a database flow or investigation ID (database, drafts). Reads never write, enqueue or acknowledge. POST /api/actions/companion runs the allowlisted
companion.action under the dashboard's Origin/token checks; the core MCP stays read-only.
"""
import re

from .. import common, companion, companionviews, profile, workflowstore
from .writes import action, record_action

_PID = re.compile(r"^p_[a-f0-9]{64}$")
_DID = re.compile(r"^d_[a-f0-9]{64}$")
_KEY = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


def _view_args(operation, workflow, key, cursor):
    args = {"operation": operation}
    if workflow:
        args["workflow"] = workflow
    if cursor:
        args["cursor"] = cursor
    if not key:
        return args
    if operation == "snapshot":
        args["ticket"] = key
    elif operation == "planner":
        args["group"] = key
    elif operation == "plan":
        args["plan"] = key
    elif operation in ("database", "drafts"):
        args["flow" if key.startswith("f_") else "investigation"] = key
    else:
        raise ValueError("this operation takes no target argument")
    return args


def _proposal(key):
    """Proposed requirement baselines from the cached Jira card, one per confirmed Jira instance: proposed rows,
    gaps, truncation and what `create` takes once you confirm. Nothing is written; Jira is never called."""
    from .. import companionplanning as planning
    cfg = common.load_config()
    companion.ticket(key, cfg)
    out = []
    for inst in sorted({a.get("instance", "") for a in profile.load(cfg)["jira_accounts"]}):
        try:
            prop = planning.propose(key, inst, cfg)
        except PermissionError as exc:
            out.append({"instance": inst, "available": False, "reason": str(exc)[:200]})
            continue
        out.append({"instance": inst, "available": True, "criteria": prop["criteria"], "proposed": prop["proposed"],
                    "gaps": prop["gaps"], "completeness": prop["completeness"], "state": prop["state"],
                    "label": prop["label"], "overall_coverage": prop["overall_coverage"],
                    "truncated": prop["coverage"].get("description") == "truncated",
                    "confirmable": planning.confirmable(prop)})
    return out


def read(operation, workflow="", key="", cursor=""):
    if operation in {"planning_prompt", "improvement_prompt"}:
        from .. import companionexecution
        return {"text": companionexecution.planning_prompt(workflow, operation == "improvement_prompt")}
    if operation in {"options", "proposal"}:
        cfg = common.load_config()
        if key and not _KEY.fullmatch(key):
            raise ValueError("invalid ticket key")
        if key:
            companion.ticket(key, cfg)
        p = profile.load(cfg)
        out = {"profile_generation": profile.signature(cfg), "repositories": p["repos"],
               "jira_instances": sorted({a.get("instance", "") for a in p["jira_accounts"]})}
        if operation == "proposal":
            if not key:
                raise ValueError("name the ticket")
            out["proposals"] = _proposal(key)
        return out
    if operation == "draft_version":  # one saved draft version's export text, to view or edit it
        from .. import companiondrafts
        rec = companion.get(workflow)
        if not _DID.fullmatch(key or ""):
            raise ValueError("invalid draft version")
        found = companiondrafts.version(rec, key)
        if found is None:
            raise ValueError("draft version not found")
        return {"profile_generation": rec["binding"]["profile_generation"], "revision": rec["revision"],
                "workflow": workflow, "version": found}
    if operation == "editor":
        rec = companion.get(workflow)
        pid = key or rec["selected_plan"]
        if pid and (not _PID.fullmatch(pid) or pid not in rec["plans"]):
            raise ValueError("invalid plan")
        return {"profile_generation": rec["binding"]["profile_generation"], "revision": rec["revision"],
                "workflow": workflow, "stage": rec["stage"], "plan": rec["plans"].get(pid) if pid else None,
                "baseline": rec["baselines"][rec["baseline"]]}
    if operation == "diff":
        args = _view_args("plan", workflow, key, cursor)
        args["base"] = "parent"
        return companionviews.read(args)
    return companionviews.read(_view_args(operation, workflow, key, cursor))


@action("companion", cap=256 * 1024)
def mutate(body):
    try:
        rec = companion.action(body)
        if "attention" in rec and "id" not in rec:  # presentation state: no workflow changed
            record_action("companion", True, "%s %s" % (body.get("action"), str(body.get("item"))[:120]))
            return 200, {"ok": True, "attention": rec["attention"], "notice": str(rec.get("notice") or "")[:300]}
        if "reconciled" in rec and "id" not in rec:  # profile reconciliation: counts only, never another task's data
            record_action("companion", True, "reconcile_profile %d re-bound, %d left out" % (rec["reconciled"],
                                                                                             rec["left_out"]))
            return 200, {"ok": True, "reconciled": rec["reconciled"], "left_out": rec["left_out"],
                         "notice": str(rec.get("notice") or "")[:300]}
        record_action("companion", True, str(body.get("action")) + " " + rec["id"])
        out = {"ok": True, "workflow": rec["id"], "revision": rec["revision"], "stage": rec["stage"]}
        if isinstance(rec.get("job"), dict):
            out["job"] = companionviews._job(rec["job"])
            out["coalesced"] = bool(rec.get("coalesced"))
        if rec.get("notice"):
            out["notice"] = str(rec["notice"])[:300]
        return 200, out
    except workflowstore.Conflict as exc:
        return 409, {"error": str(exc), "revision": (exc.current or {}).get("revision"), "retain_draft": True}
    except PermissionError as exc:
        return 403, {"error": str(exc), "revoke": True}
    except (ValueError, TypeError, KeyError) as exc:
        return 400, {"error": str(exc)}
    except (TimeoutError, OSError):
        return 503, {"error": "Not saved. Keep your draft and reload the checkpoint before retrying. Nothing was sent.",
                     "retain_draft": True}
