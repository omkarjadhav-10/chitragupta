"""The dashboard's allowlisted actions (POST /api/actions/<name>). Each one does a single own thing through the same
code the CLI uses. Three run anything paid: worklog-refresh (one Jira read) and worklog-exact (one batched condensing
call), both within the daily MCP budget (the 20:00 reserve is kept), and brief-refresh (one model call for one
ticket's brief, within brief.daily_cap and budget.model_daily_usd, none when its inputs are unchanged)."""
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


@action("brief-refresh", cap=256)
def brief_refresh(body):
    """Refresh brief: write one ticket's brief now, the same as `/cg:ticket <KEY> --brief`. Writes only that ticket's
    record and hub in the data home; no call when its inputs are unchanged."""
    from .. import brief, store
    key = body.get("key")
    if not isinstance(key, str) or not store.TICKET_RX.match(key):
        return 400, {"error": "Give a ticket key such as ABC-1001."}
    if not os.path.exists(store.ticket_path(key)):
        return 404, {"error": "Nothing is recorded for %s." % key}
    res = brief.refresh(key, wait_s=3)
    record_action("brief-refresh", bool(res.get("ok")), "%s %s" % (key, res.get("skipped") or "written"))
    out = {"ok": bool(res.get("ok")), "view": res.get("view")}
    if res.get("skipped"):
        out["skipped"] = res["skipped"]
    return 200, out


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


@action("profile-discover", cap=256)
def profile_discover(body):
    """Find identity suggestions: git config in the watched repos always (local); with {"hosts": true} the account each
    host CLI is signed in as (one read each); with {"jira": true} one Jira lookup for your primary email (a read-only
    MCP job within the daily MCP budget, the 20:00 reserve kept). Nothing is confirmed: suggestions only."""
    from .. import jiraworklog, mcpjobs, profile
    hosts_too, jira = body.get("hosts", False), body.get("jira", False)
    if not isinstance(hosts_too, bool) or not isinstance(jira, bool):
        return 400, {"error": "hosts and jira are true or false."}
    if jira:
        def go(a):
            if not mcpjobs.enabled(a.cfg, "jira"):
                return {"ok": False, "skipped": "Jira is off (mcp.jira.enabled)"}
            if jiraworklog._over_budget(a):
                return {"ok": False, "skipped": jiraworklog._BUDGET}
            return {"ok": True, "res": profile.discover(a.cfg, hosts_too=hosts_too, jira=True, health=a.health)}
        got = jiraworklog._locked(3, go)
    else:
        try:
            got = {"ok": True, "res": profile.discover(common.load_config(), hosts_too=hosts_too)}
        except Exception as e:  # noqa: BLE001
            got = {"ok": False, "skipped": str(e)[:200] or e.__class__.__name__}
    if not got.get("ok"):
        record_action("profile-discover", False, got.get("skipped") or "")
        return 200, {"ok": False, "skipped": got.get("skipped") or "skipped"}
    res = got["res"]
    errors = res.get("errors") or {}
    record_action("profile-discover", True, "%d suggestion(s)%s" % (len(res.get("candidates") or []),
                                                                    "; " + ", ".join(sorted(errors)) if errors else ""))
    return 200, {"ok": True, "candidates": len(res.get("candidates") or []), "errors": errors}


@action("reflection-save", cap=2048)
def reflection_save(body):
    """Your note for a day ("What moved forward today?"): self-reported, kept in data/reflections on this machine, never
    in the pushed daily files."""
    from .. import configstore, profile, progress
    cfg = common.load_config()
    if not configstore.on(cfg, "profile.reflection"):
        return 403, {"error": "The daily reflection is switched off in your profile."}
    day, text = _day({"day": body.get("date")}), body.get("text")
    if not day or not isinstance(text, str):
        return 400, {"error": "Give the date as YYYY-MM-DD and the note as text."}
    if day > max(profile.today(cfg), common.workday(common.now(), cfg)):  # days follow the profile's zone
        return 400, {"error": "That day hasn't happened yet."}
    try:
        rec = progress.save_reflection(day, text)
    except ValueError as e:
        return 400, {"error": str(e)}
    record_action("reflection-save", True, day)
    return 200, {"ok": True, "date": day, "reflection": rec}


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
