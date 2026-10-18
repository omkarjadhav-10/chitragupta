"""The dashboard's allowlisted actions (POST /api/actions/<name>). Each one does a single own thing through the same
code the CLI uses; nothing here runs a model unless the action is one of the few listed as doing so."""
import os

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
