"""Canned decisions /cg:doctor runs to show "Safety: enforced". Pure checks: nothing starts, nothing is written,
nothing is logged."""
import os

from .. import common
from . import commands, mcp, paths, query

GIT = "git"


def _cfg():
    return {"watched_repos": [], "jira_worklog": {"slack": {"self_user_id": "U0SELFTEST", "send_tool": "mcp__slack__slack_send_message"}}}


def cases():
    cfg = _cfg()
    home = common.home()
    must_block = [
        ("git commit in a work repo", commands.reason([GIT, "-C", home] + commands.HARDEN + ["commit", "-m", "x"], "git-work", cfg=cfg)),
        ("git push --force (data home)", commands.reason([GIT, "-C", home, "push", "--force", "origin", "main"], "git-home", cfg=cfg)),
        ("gh pr merge", commands.reason(["gh", "pr", "merge", "1"], "host", cfg=cfg)),
        ("az vm delete", commands.reason(["az", "vm", "delete", "--name", "x", "--output", "json"], "host", cfg=cfg)),
        ("claude bypass", commands.reason(["claude", "-p", "x", "--dangerously-skip-permissions"], "claude", cfg=cfg)),
        ("unknown executable", commands.reason(["kubectl", "get", "pods"], "self", cfg=cfg)),
        ("SQL UPDATE", query.sql_reason("UPDATE t SET a = 1")),
        ("SQL hidden write", query.sql_reason("SELECT 1 /* x */; DELETE FROM t")),
        ("Mongo deleteMany", query.mongo_reason({"operation": "deleteMany", "collection": "c"})),
        ("write outside the roots", paths.allowed(os.path.join(os.path.dirname(home.rstrip("\\/")) or home, "elsewhere", "x.txt"))),
        ("UNC path", paths.allowed("\\\\server\\share\\x.txt")),
        ("Slack to another user", mcp.slack_send_reason("mcp__slack__slack_send_message", {"channel_id": "U0OTHER", "message": "x"}, cfg)),
        ("Jira write tool", mcp.mcp_reason("mcp__mcp-atlassian__jira_transition_issue", {}, {"servers": {"mcp-atlassian": {"read": ["jira_transition_issue"]}}})),
        ("/cg: git commit", mcp.session_reason("Bash", {"command": "git commit -am x"}, cfg)),
        ("internal Bash", mcp.reason("none", "Bash", {"command": "dir"}, cfg)),
    ]
    must_allow = [
        ("SQL SELECT TOP 5", query.sql_reason("SELECT TOP 5 * FROM t")),
        ("write in the data home", paths.allowed(os.path.join(home, "inbox.md"))),
        ("Slack DM to myself", mcp.slack_send_reason("mcp__slack__slack_send_message", {"channel_id": "U0SELFTEST", "message": "x"}, cfg)),
    ]
    return must_block, must_allow


def run():
    bad = []
    try:
        must_block, must_allow = cases()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "failed": ["selftest crashed: %r" % e]}
    bad += ["not blocked: %s" % n for n, r in must_block if not r]
    bad += ["blocked: %s (%s)" % (n, r) for n, r in must_allow if r]
    return {"ok": not bad, "failed": bad, "checks": len(must_block) + len(must_allow)}
