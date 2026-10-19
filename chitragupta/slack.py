"""Slack DM sender (official Slack MCP server, OAuth), token-lean.

The server is registered in local scope for .worklog/slack-sender only (its own git root), so no other Claude Code
session loads Slack's tools. One `claude -p` call per send: haiku, only the send tool allowed, lean flags, and the safety guard (profile
slack-send, fail-closed) checks the call itself: only the send tool, only to my own user ID, plain text, capped.
The tool-call text is compared with the intended message (stream-json) and any mismatch is logged.

Every call runs with --strict-mcp-config and an --mcp-config holding only the Slack server, so no other MCP server
loads. Mode `strict` passes .worklog/mcp/slack.json; mode `local` passes the sender folder's local-scope Slack
definition exactly as Claude Code has it (name, URL, OAuth client id: Claude Code keeps the sign-in per definition, so
the same definition finds it), copied to .worklog/cache/mcp/slack-local.json. A literal credential header would be
written there as a ${CHITRAGUPTA_RT_...} reference, its value passed only in the call's environment; OAuth tokens
stay in Claude Code's own store and are never read or copied.
"""
import json
import os
import re

from . import common
from . import mcpjobs
from .safety import claude as builder, commands

DEFAULT_URL = "https://mcp.slack.com/mcp"  # the public Slack MCP server
# Slack's hosted MCP has no dynamic client registration; Slack publishes this client id (and callback
# port) for connecting Claude: https://docs.slack.dev/ai/slack-mcp-server/connect-to-claude/
DEFAULT_CLIENT_ID = "1601185624273.8899143856786"
DEFAULT_CALLBACK_PORT = 3118


def server_def(cfg=None):
    """Slack server entry from jira_worklog.slack.server {url, client_id, callback_port}. Without a client_id the
    OAuth block already in .worklog/mcp/slack.json is kept, else Slack's published client id for Claude is used."""
    cfg = cfg if cfg is not None else common.load_config()
    sc = (((cfg.get("jira_worklog") or {}).get("slack") or {}).get("server")) or {}
    srv = {"type": "http", "url": sc.get("url") or DEFAULT_URL}
    old = ((common.read_json(common.wl("mcp", "slack.json"), {}) or {}).get("mcpServers") or {}).get("slack") or {}
    if sc.get("client_id"):
        oauth = {"clientId": str(sc["client_id"])}
        if sc.get("callback_port"):
            oauth["callbackPort"] = int(sc["callback_port"])
        srv["oauth"] = oauth
    elif isinstance(old.get("oauth"), dict):
        srv["oauth"] = old["oauth"]
    else:
        srv["oauth"] = {"clientId": DEFAULT_CLIENT_ID, "callbackPort": int(sc.get("callback_port") or DEFAULT_CALLBACK_PORT)}
    return srv


def _settings(cfg):
    return ((cfg or {}).get("jira_worklog") or {}).get("slack") or {}


def configured(cfg):
    """Slack is set up for background sends: `slack-discover` stored my user id and the send tool, which it only does
    after finding Slack's tools through the background config (.worklog/mcp/slack.json or the sender folder)."""
    sl = _settings(cfg)
    return bool(sl.get("self_user_id") and sl.get("send_tool"))


def state(cfg):
    """"ready" | "off" (jira_worklog.slack.enabled is false) | "not_connected" (no background Slack config).
    jira_worklog.slack.enabled defaults to true only when Slack is configured for background sends."""
    if _settings(cfg).get("enabled") is False:
        return "off"
    return "ready" if configured(cfg) else "not_connected"


NOT_CONNECTED_NOTE = "Slack isn't connected: the evening worklog is saved in the daily file and data/timesheets/"
OFF_NOTE = "Slack sending is off (jira_worklog.slack.enabled): the evening worklog is saved in the daily file"


def sender_dir():
    return common.wl("slack-sender")


def ensure_config(cfg=None):
    common.write_json(common.wl("mcp", "slack.json"), {"mcpServers": {"slack": server_def(cfg)}})


_CRED_HEADERS = ("authorization", "x-api-key", "cookie")


def local_server(cfg=None):
    """(name, definition) of the Slack server in the sender folder's local scope (~/.claude.json, read only), or
    (None, None)."""
    from . import plugin, setup
    cj = common.read_json(plugin.claude_json_path(), {}) or {}
    for name, s in setup.local_servers(cj, sender_dir()).items():
        if setup._is_slack(name, s):
            return name, s
    return None, None


def local_config(cfg=None):
    """Write .worklog/cache/mcp/slack-local.json with only the local-scope Slack server. -> (path, env) or
    (None, {}) when there is none. A literal credential header becomes a ${CHITRAGUPTA_RT_...} reference whose value
    goes only into the call's environment (env)."""
    name, s = local_server(cfg)
    if not s:
        return None, {}
    s, env = json.loads(json.dumps(s)), {}
    for k, v in list((s.get("headers") or {}).items()):
        if k.lower() in _CRED_HEADERS and not mcpjobs._REF.search(str(v)):
            var = "CHITRAGUPTA_RT_" + re.sub(r"[^A-Z0-9]", "_", ("%s_%s" % (name, k)).upper())
            env[var] = str(v)
            s["headers"][k] = "${%s}" % var
    for var in mcpjobs._refs(s):  # ${VAR} the definition already references: from this process or the user env
        if var not in env and mcpjobs.user_env(var):
            env[var] = mcpjobs.user_env(var)
    path = os.path.join(common.wl("cache", "mcp"), "slack-local.json")
    common.write_json(path, {"mcpServers": {name: s}})
    return path, env


def _mcp_config(cfg, mode):
    """(--mcp-config path, env) for the call: only the Slack server, in either mode."""
    if mode == "local":
        return local_config(cfg)
    return common.wl("mcp", "slack.json"), {}


def _args(cfg, task, allowed, mode, max_turns=3, disallowed=None, profile="slack-send", mcp_config=None):
    """One guarded call (safety.claude): `slack-send` lets only the send tool reach my own user ID; `slack-discover`
    only reads my profile. Disallowed tools are dropped from the context: ~23k -> ~4k input tokens per turn.
    Always --strict-mcp-config with only the Slack server (see the module docstring)."""
    if mcp_config is None:
        mcp_config = _mcp_config(cfg, mode)[0]
    return builder.build(mcpjobs.claude_exe(cfg), task, profile, "haiku", common.prompt_path("slack_send.md"),
                         allowed=allowed or [], disallowed=disallowed or [], mcp_config=mcp_config,
                         output="stream-json", max_turns=max_turns, cfg=cfg)


def _cwd(mode):
    return sender_dir() if mode == "local" else common.wl("sandbox")


def _others(cfg, keep):
    tools = ((cfg.get("jira_worklog") or {}).get("slack") or {}).get("tools") or []
    return [t for t in tools if t not in keep]


def _run(cfg, task, allowed, mode, timeout=180, disallowed=None, profile="slack-send"):
    common.ensure_dir(_cwd(mode))
    path, env = _mcp_config(cfg, mode)
    if not path:  # no model call without the one Slack server to load
        res = mcpjobs.parse_stream("")
        res.update(rc=1, stderr="", result="no Slack server in the local scope of %s (run `setup add-slack`)" %
                   sender_dir())
        return res
    rc, so, se = commands.run(_args(cfg, task, allowed, mode, disallowed=disallowed, profile=profile, mcp_config=path),
                              "claude", cwd=_cwd(mode), input_text="", timeout=timeout, cfg=cfg,
                              env_extra=dict(common.INTERNAL_ENV, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", **env))
    res = mcpjobs.parse_stream(so)
    res["rc"], res["stderr"] = rc, (se or "")[-400:]
    return res


def discover(cfg, email):
    """Find the send / user-lookup tools and my Slack user ID. Tries the strict per-call config first (no other
    servers), then the sender folder's local scope. Returns {mode, self_user_id, send_tool, tools, tokens}."""
    ensure_config(cfg)
    last = None
    for mode in ("strict", "local"):
        res = _run(cfg, "List nothing yet. Reply READY.", [], mode, profile="slack-discover")
        slack_tools = [t for t in res["tools_loaded"] if t.startswith("mcp__slack__")]
        st = {s.get("name"): s.get("status") for s in res.get("mcp_servers") or []}
        last = {"mode": mode, "servers": st, "tools": slack_tools, "rc": res["rc"]}
        if not slack_tools:
            continue
        send = next((t for t in slack_tools if re.search(r"send_message$", t)), None) or \
            next((t for t in slack_tools if "send" in t and "message" in t), None)
        look = [t for t in slack_tools if t.endswith("read_user_profile")] or \
            [t for t in slack_tools if re.search(r"user", t) and re.search(r"search|find|lookup|read|get|list", t)]
        task = ("Call %s with no arguments (it returns my own profile), then reply with only JSON "
                '{"user_id": "U...", "name": "..."}, or {"user_id": "", "name": "FAILED <reason>"}.' % look[0].split("__")[-1]) \
            if look and look[0].endswith("read_user_profile") else \
            ("Find the Slack user whose email is %s using the user lookup/search tool, then reply with only JSON "
             '{"user_id": "U...", "name": "..."}, or {"user_id": "", "name": "FAILED <reason>"}.' % email)
        r2 = _run(cfg, task, look[:1], mode, disallowed=[t for t in slack_tools if t not in look[:1]],
                  profile="slack-discover")
        out = r2.get("out") or {}
        uid = out.get("user_id") or ""
        if not uid:
            m = re.search(r"\b(U[A-Z0-9]{8,12})\b", json.dumps([c.get("result") for c in r2["calls"]]))
            uid = m.group(1) if m else ""
        return {"mode": mode, "self_user_id": uid, "send_tool": send, "tools": slack_tools, "lookup_tools": look,
                "tokens": {"probe_in": res["input_tokens"], "lookup_in": r2["input_tokens"]}, "servers": st}
    return dict(last or {}, error="Slack MCP tools not available (signed in?)")


def send(cfg, text, health):
    """-> {ok, ts, error, tokens}. Never raises."""
    sl = _settings(cfg)
    uid, tool, mode = sl.get("self_user_id"), sl.get("send_tool"), sl.get("mode") or "strict"
    if not (uid and tool):
        return {"ok": False, "error": "Slack not set up (self_user_id / send_tool missing)", "not_connected": True}
    if common.env("SLACK_FAIL"):  # tests: forced failure
        return {"ok": False, "error": "forced failure (WORKLOG_SLACK_FAIL)"}
    task = ("Send the text between <msg> tags, exactly, as a Slack DM to %s using %s. Do nothing else. "
            "Reply SENT or FAILED <reason>.\n<msg>\n%s\n</msg>" % (uid, tool.split("__")[-1], text))
    try:
        res = _run(cfg, task, [tool], mode, disallowed=_others(cfg, [tool]))
    except Exception as e:  # noqa
        return {"ok": False, "error": "send crashed: %r" % e}
    calls = [c for c in res["calls"] if c["name"] == tool]
    tokens = {"in": res["input_tokens"], "out": res["output_tokens"], "cost": round(res["cost"], 4)}
    if not calls:  # the model wrote the call as text instead of calling: one retry, firmer
        res = _run(cfg, "Make the tool call now (a real tool call, not text). " + task, [tool], mode,
                   disallowed=_others(cfg, [tool]))
        calls = [c for c in res["calls"] if c["name"] == tool]
        tokens = {k: tokens[k] + v for k, v in (("in", res["input_tokens"]), ("out", res["output_tokens"]),
                                                 ("cost", round(res["cost"], 4)))}
    if not calls:  # say whether the server connected and the tool loaded: "not available" has two different causes
        srv = ", ".join("%s %s" % (s.get("name"), s.get("status")) for s in res.get("mcp_servers") or []) or "none"
        return {"ok": False, "error": "no send call: %s [servers: %s; send tool loaded: %s]" % (
            (res.get("result") or res["stderr"])[:200], srv, "yes" if tool in (res.get("tools_loaded") or []) else "no"),
            "tokens": tokens}
    c = calls[-1]
    sent_text = next((v for k, v in (c.get("input") or {}).items() if isinstance(v, str) and k in ("text", "message", "content")), "")
    mismatch = sent_text.strip() != text.strip()
    if mismatch:
        common.log("slack", "MISMATCH between intended and sent text:\n--- intended\n%s\n--- sent\n%s" % (text, sent_text))
        health.setdefault("jira_worklog", {})["last_mismatch"] = common.now_iso()
    if c.get("is_error"):
        return {"ok": False, "error": "send tool error: %s" % str(c.get("result"))[:200], "tokens": tokens}
    m = re.search(r"\"?(?:ts|message_ts)\"?\s*[:=]\s*\"?(\d{9,11}\.\d{3,8})", str(c.get("result")))
    ok = (res.get("result") or "").strip().upper().startswith("SENT") or bool(m)
    return {"ok": ok, "ts": m.group(1) if m else "", "mismatch": mismatch, "tokens": tokens,
            "error": "" if ok else (res.get("result") or "")[:200]}
