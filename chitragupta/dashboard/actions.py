"""The dashboard's allowlisted actions (POST /api/actions/<name>). Each one does a single own thing through the same
code the CLI uses. Only two run anything paid, both read-only and both within the daily MCP budget (the 20:00
reserve is kept): worklog-refresh (one Jira read) and worklog-exact (one batched condensing call)."""
import datetime as _dt
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
    if not isinstance(d, str) or not DAY.match(d):
        return None
    try:
        _dt.date.fromisoformat(d)
    except ValueError:
        return None
    return d


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


CAL_KINDS = ("leave", "half-day", "holiday", "workday", "clear")


@action("calendar-mark", cap=512)
def calendar_mark(body):
    """Mark a day as leave, a half day, a holiday (with a name) or an extra workday, or clear its mark: one line in
    inbox.md, the same as /cg:log; the next background run applies it to the calendar."""
    from .. import jiraworklog
    from .. import timesheet as T
    day, kind = _day(body), body.get("kind")
    if not day or kind not in CAL_KINDS:
        return 400, {"error": "Give the day as YYYY-MM-DD and one of: " + ", ".join(CAL_KINDS) + "."}
    name = body.get("name") if kind == "holiday" else ""
    name = " ".join(str(name or "").split())[:60]  # one line, no control characters
    line = ("%s: %s %s" % (kind, day, name)).strip()
    d = T.parse_directive(line, common.workday(common.now(), common.load_config()))
    if not d or d["kind"] in ("invalid", "time", "resend") or d["days"] != [day]:
        return 400, {"error": "That mark couldn't be read."}
    jiraworklog.append_inbox(line, repeat=True)
    record_action("calendar-mark", True, line)
    return 200, {"ok": True, "line": line}


@action("search-rebuild", cap=256)
def search_rebuild(body):
    """Rebuild the search index from the data home (the same as /cg:search --rebuild); it is a cache in the data home."""
    from .. import searchidx
    try:
        res = searchidx.rebuild()
    except Exception as e:  # noqa: BLE001
        record_action("search-rebuild", False, str(e)[:200])
        return 502, {"error": "The index couldn't be rebuilt: %s" % (str(e)[:200] or e.__class__.__name__)}
    record_action("search-rebuild", True)
    return 200, {"ok": True, "stats": searchidx.stats(), "result": res if isinstance(res, dict) else None}


@action("scheduler-repair", cap=256)
def scheduler_repair(body):
    """Re-register the background job when it is missing or differs (the same as `setup scheduler --repair`)."""
    from .. import scheduler
    try:
        res = scheduler.repair(common.load_config())
    except Exception as e:  # noqa: BLE001
        record_action("scheduler-repair", False, str(e)[:200])
        return 502, {"error": "The repair couldn't run: %s" % (str(e)[:200] or e.__class__.__name__)}
    record_action("scheduler-repair", res.get("ok"), res.get("detail"))
    return (200 if res.get("ok") else 502), dict(res, status=scheduler.status(common.load_config()))


@action("share-ticket", cap=2048)
def share_ticket(body):
    """Share a ticket: the same as `cli share KEY --yes`. The bundle is written only when the scan passes."""
    from .. import share
    key, level, target = body.get("key"), body.get("level"), body.get("target")
    sections = body.get("sections")
    if not isinstance(key, str) or not isinstance(sections, (list, type(None))) or \
            not all(isinstance(x, (str, type(None))) for x in (level, target)):
        return 400, {"error": "Give key, sections (a list), level and target."}
    if sections is not None and not all(isinstance(s, str) for s in sections):
        return 400, {"error": "sections must be names"}
    try:
        res = share.share(key, sections or None, level or None, target or None)
    except share.ShareError as e:
        return 400, {"error": str(e)}
    record_action("share-ticket", bool(res.get("ok")), "%s %s -> %s%s" % (
        res.get("key"), res.get("level"), res.get("target"), " (blocked by the scan)" if res.get("blocked") else ""))
    out = {k: res.get(k) for k in ("ok", "blocked", "hits", "problems", "path", "committed", "pushed", "push_error",
                                   "key", "level", "sections", "target")}
    return 200, out
