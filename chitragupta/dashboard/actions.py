"""The dashboard's allowlisted actions (POST /api/actions/<name>). Each one does a single own thing through the same
code the CLI uses. Only two run anything paid, both read-only and both within the daily MCP budget (the 20:00
reserve is kept): worklog-refresh (one Jira read) and worklog-exact (one batched condensing call)."""
import os
import re

from .. import common
from ..safety import paths
from .writes import action, record_action


@action("capture-pause", cap=256)
def capture_pause(body):
    p = common.wl("PAUSED")
    if not os.path.exists(p):
        common.write_text(p, common.now_iso() + "\n")
    record_action("capture-pause", True)
    return 200, {"ok": True, "paused": True}


@action("capture-resume", cap=256)
def capture_resume(body):
    p = common.wl("PAUSED")
    if os.path.exists(p):
        paths.delete(p, caller="dashboard")
    record_action("capture-resume", True)
    return 200, {"ok": True, "paused": False}


@action("mcp-check", cap=256)
def mcp_check(body):
    """The live check: the same call /cg:doctor makes (`claude mcp list`, no model call), cached for the page."""
    import time
    from .. import setup
    t0 = time.monotonic()
    try:
        st = setup.mcp_status(common.load_config(), probe=True)
    except Exception as e:  # noqa: BLE001
        common.write_json(common.wl("cache", "mcp-check.json"), {"at": common.now_iso(), "error": str(e)[:300]})
        record_action("mcp-check", False, str(e)[:200])
        return 502, {"error": "The check couldn't run: %s" % (str(e)[:200] or e.__class__.__name__)}
    took = round(time.monotonic() - t0, 1)
    common.write_json(common.wl("cache", "mcp-check.json"), {"at": common.now_iso(), "seconds": took})
    record_action("mcp-check", True, "%.1f s" % took)
    return 200, {"ok": True, "seconds": took, "status": st}


@action("relations-detect", cap=512)
def relations_detect(body):
    """Propose base relations for one watched repo from its local refs: read-only git through gitro, no fetch, nothing
    saved. The page shows the proposals; saving them is a normal settings change."""
    from .. import gitcmd, relations
    cfg = common.load_config()
    repos = {os.path.basename(str(r).rstrip("/\\")): r for r in cfg.get("watched_repos") or [] if r}
    name = body.get("repo")
    if not isinstance(name, str) or name not in repos:
        return 404, {"error": "Not a watched repo."}
    conf = gitcmd.repo_conf(cfg, name)
    try:
        res = relations.detect(cfg, repos[name], conf["bases"], conf["remote"])
    except Exception as e:  # noqa: BLE001
        record_action("relations-detect", False, str(e)[:200])
        return 502, {"error": "Detection couldn't run: %s" % (str(e)[:200] or e.__class__.__name__)}
    record_action("relations-detect", True, name)
    return 200, {"ok": True, "repo": name, "proposals": res.get("proposals") or []}


DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day(body):
    d = body.get("day")
    return d if isinstance(d, str) and DAY.match(d) else None


def _preview_buttons():
    from .. import configstore
    return configstore.on(common.load_config(), "dashboard.preview_buttons")


def _preview_answer(name, res):
    ok = bool(res.get("ok"))
    record_action(name, ok, res.get("skipped") or "")
    out = {"ok": ok, "preview": res.get("preview")}
    if not ok:
        out["skipped"] = res.get("skipped") or "skipped"
    if "model_calls" in res:
        out["model_calls"] = res["model_calls"]
    return 200, out


@action("worklog-refresh", cap=256)
def worklog_refresh(body):
    """Refresh from Jira: one read-only evening-facts job for the day, then the preview from the fresh data."""
    from .. import jiraworklog
    day = _day(body)
    if not day:
        return 400, {"error": "Give the day as YYYY-MM-DD."}
    if not _preview_buttons():
        return 403, {"error": "The preview buttons are switched off in Settings."}
    return _preview_answer("worklog-refresh", jiraworklog.refresh_facts(day, wait_s=3))


@action("worklog-exact", cap=256)
def worklog_exact(body):
    """Preview exactly: condense now the lines the preview marks "condensed at 20:00" and cache them for the sender."""
    from .. import jiraworklog
    day = _day(body)
    if not day:
        return 400, {"error": "Give the day as YYYY-MM-DD."}
    if not _preview_buttons():
        return 403, {"error": "The preview buttons are switched off in Settings."}
    return _preview_answer("worklog-exact", jiraworklog.preview_exact(day, wait_s=3))


@action("worklog-resend", cap=256)
def worklog_resend(body):
    """Send again: one `resend: <day>` line in inbox.md (the next tick sends it); a repeated request id within two
    minutes (a double click) counts once."""
    from .. import jiraworklog
    day, rid = _day(body), body.get("request_id")
    if not day or not isinstance(rid, str) or not rid.strip():
        return 400, {"error": "Give the day as YYYY-MM-DD and a request id."}
    if day > common.workday(common.now(), common.load_config()):
        return 400, {"error": "That day hasn't happened yet."}
    res = jiraworklog.request_resend(day, rid[:100])
    record_action("worklog-resend", True, day + (" (duplicate)" if res["duplicate"] else ""))
    return 200, {"ok": True, "duplicate": res["duplicate"], "day": day}
