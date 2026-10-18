"""Read-only MCP harness and the worklog's MCP jobs (Jira cards / evening facts; database-MCP schema cards, QA trace,
link map). The Git-host PR job (hostmcp.py) runs through the same harness.

Every job is one `claude -p` call (haiku, lean flags) with only the server(s) and read tools that job needs:
  --strict-mcp-config --mcp-config <per-job file>   --allowedTools <job reads>   --disallowedTools <server writes>
  built by chitragupta.safety.claude: no built-in tools, dontAsk, and a fail-closed PreToolUse guard (profile mcp)
Raw tool results are taken from the stream-json events and written to local files; the model only picks the calls
and returns a short JSON. Never run from hooks; budget = mcp.daily_call_budget calls a day.

The database MCP is whatever server mcp.database.server names (no default: absent -> database jobs are skipped).
Its tool names come from mcp.database.tools {query, describe_table, list_tables, list_schemas, list_connections}
(defaults: run_query, describe_table, list_tables, list_schemas, list_connections), so any read-only database MCP
can be wired by config. Argument names (connectionId, schema, table, limit, sql) are that server's API.
"""
import datetime as _dt
import json
import os
import re
import time

from . import common
from . import extract
from .redact import redact
from .safety import claude as builder, commands, paths

MCP_DIR = None  # set lazily: <home>/.worklog/mcp


def mcp_dir():
    return common.wl("mcp")


def user_env(name):
    """Env var from this process, else the Windows user environment (set after this process started)."""
    v = os.environ.get(name)
    if v:
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except (ImportError, OSError):
        return None


_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


def _refs(obj):
    return set(_REF.findall(json.dumps(obj)))


# --------------------------------------------------------------------------- server definitions

def claude_json():
    """Claude Code's ~/.claude.json (read only); $CLAUDE_CONFIG_DIR/.claude.json when that variable is set."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~")
    return common.read_json(os.path.join(base, ".claude.json"), {}) or {}


_RUNTIME = {}  # credentials read from ~/.claude.json for this process only: passed to the job's env, never written


def server_def(name):
    """User-scope server definition from ~/.claude.json. A literal credential header is replaced by a
    ${CHITRAGUPTA_RT_...} reference in the per-job file; its value only goes into the job process's environment."""
    s = (claude_json().get("mcpServers") or {}).get(name)
    if not s:
        raise JobError("server %s is not configured" % name, skip=True)
    s = json.loads(json.dumps(s))
    for k, v in list((s.get("headers") or {}).items()):
        if k.lower() in ("authorization", "x-api-key", "cookie") and not _REF.search(str(v)):
            var = "CHITRAGUPTA_RT_" + re.sub(r"[^A-Z0-9]", "_", ("%s_%s" % (name, k)).upper())
            _RUNTIME[var] = str(v)
            s["headers"][k] = "${%s}" % var
    return s


def job_config(server, tools, job):
    s = server_def(server)
    if server == "mcp-atlassian":  # the server itself enforces read-only and loads only this job's tools
        env = {k: v for k, v in (s.get("env") or {}).items()
               if not (k.startswith("CONFLUENCE_") and all(t.startswith("jira_") for t in tools))}
        env["READ_ONLY_MODE"] = "true"
        env["ENABLED_TOOLS"] = ",".join(tools)
        s["env"] = env
    _harden_git_host(server, s)
    path = os.path.join(common.wl("cache", "mcp"), job + ".json")
    common.write_json(path, {"mcpServers": {server: s}})
    return path, _refs(s)


def _harden_git_host(server, s):
    """A GitHub MCP server also runs in its own read-only mode with only the pull-request toolset (stdio: env;
    the hosted server: headers). The guard and the allowlist decide either way; this only narrows what loads."""
    entry = ((common.read_json(os.path.join(mcp_dir(), "allow.json"), {}) or {}).get("servers") or {}).get(server) or {}
    if entry.get("role") != "git-host" or entry.get("provider") != "github":
        return
    if s.get("url"):
        s.setdefault("headers", {}).update({"X-MCP-Readonly": "true", "X-MCP-Toolsets": "pull_requests"})
    else:
        s.setdefault("env", {}).update({"GITHUB_READ_ONLY": "1", "GITHUB_TOOLSETS": "pull_requests"})


# --------------------------------------------------------------------------- the call

class JobError(Exception):
    def __init__(self, msg, skip=False):
        super().__init__(msg)
        self.skip = skip  # offline / signed out / over budget: skip quietly


def budget_left(health, cfg, day):
    m = health.setdefault("mcp", {})
    calls = m.setdefault("calls", {})
    for d in list(calls):
        if d < (_dt.date.fromisoformat(day) - _dt.timedelta(days=7)).isoformat():
            del calls[d]
    return int(((cfg.get("mcp") or {}).get("daily_call_budget")) or 10) - int(calls.get(day) or 0)


_SAVED = re.compile(r"(?:saved to:?)\s+([A-Za-z]:[\\/][^\r\n]*?\.(?:txt|json))", re.I)


def _persisted(content):
    """Full text of a tool result Claude Code moved to a file (the model only saw a preview). The file sits under
    ~/.claude/projects/; it is read and then deleted, so the data lives only in the local knowledge base."""
    if not isinstance(content, str) or "saved to" not in content:
        return None
    m = _SAVED.search(content)
    if not m or not os.path.isfile(m.group(1)):
        return None
    p = m.group(1)
    try:
        txt = common.read_text(p)
        paths.delete(p, caller="mcpjobs.persisted")
        return txt
    except OSError:
        return None


def unwrap(x):
    """Tool result text -> JSON: handles {"result": "<json>"}, [{"type":"text","text": ...}] and nested strings."""
    for _ in range(4):
        if isinstance(x, str):
            try:
                x = json.loads(x)
                continue
            except ValueError:
                return x
        if isinstance(x, list) and x and all(isinstance(i, dict) and "text" in i for i in x):
            x = "\n".join(i["text"] for i in x)
            continue
        if isinstance(x, dict) and set(x) == {"result"} and isinstance(x["result"], str):
            x = x["result"]
            continue
        break
    return x


def parse_stream(text):
    """stream-json lines -> {tools_loaded, calls:[{name,input,result,is_error}], out, usage, cost, result}"""
    calls, by_id = [], {}
    res = {"tools_loaded": [], "calls": calls, "out": None, "usage": {}, "cost": 0.0, "result": ""}
    for line in (text or "").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
            res["tools_loaded"] = d.get("tools") or []
            res["mcp_servers"] = d.get("mcp_servers") or []
        elif t == "assistant":
            for c in (d.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use":
                    rec = {"id": c.get("id"), "name": c.get("name"), "input": c.get("input"), "result": None, "is_error": None}
                    calls.append(rec)
                    by_id[c.get("id")] = rec
        elif t == "user":
            for c in (d.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in by_id:
                    content = c.get("content")
                    if isinstance(content, list):
                        content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                    err = bool(c.get("is_error"))
                    full = _persisted(content)
                    if full is not None:  # Claude Code saved a large result to a file: take it raw from there
                        content, err = full, False
                    by_id[c["tool_use_id"]].update(result=content, is_error=err)
        elif t == "result":
            res["usage"] = d.get("usage") or {}
            res["cost"] = float(d.get("total_cost_usd") or 0)
            res["result"] = d.get("result") or ""
            res["is_error"] = d.get("is_error")
            res["subtype"] = d.get("subtype")
            out = d.get("structured_output")
            if out is None and isinstance(d.get("result"), str):
                try:
                    out = json.loads(d["result"])
                except ValueError:
                    m = re.search(r"\{.*\}", d["result"], re.S)
                    try:
                        out = json.loads(m.group(0)) if m else None
                    except ValueError:
                        out = None
            res["out"] = out
    u = res["usage"]
    res["input_tokens"] = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + \
        int(u.get("cache_creation_input_tokens") or 0)
    res["output_tokens"] = int(u.get("output_tokens") or 0)
    return res


def claude_exe(cfg):
    exe = extract.resolve_claude(cfg)
    if not exe:
        raise JobError("claude executable not found", skip=True)
    return exe


def run(job, servers, reads, writes, task, schema, cfg, health, day=None, max_turns=5, model="haiku", timeout=300,
        stdin_text=None, servers_override=None):
    """Run one read-only job. servers: {name: [tool names]} (reads per server). Returns parse_stream() result."""
    day = day or common.workday(common.now(), cfg)
    manual = common.env("MCP_MANUAL") == "1"  # CLI / asked-for runs: counted apart from the background budget
    if not manual and budget_left(health, cfg, day) <= 0:
        raise JobError("daily MCP call budget used up", skip=True)
    merged, refs = {"mcpServers": {}}, set()
    if not servers_override and common.env("MCP_OVERRIDE"):  # tests: stand-in servers
        servers_override = common.read_json(common.env("MCP_OVERRIDE"), {})
    if servers_override:
        merged["mcpServers"] = servers_override
    else:
        for srv, tools in servers.items():
            path, r = job_config(srv, tools, job + "-" + srv)
            merged["mcpServers"].update(common.read_json(path)["mcpServers"])
            refs |= r
    cfg_path = os.path.join(common.wl("cache", "mcp"), job + ".json")
    common.write_json(cfg_path, merged)
    env_extra = dict(common.INTERNAL_ENV, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    for name in refs:
        v = _RUNTIME.get(name) or user_env(name)
        if v:
            env_extra[name] = v
    missing = [n for n in refs if n not in env_extra]
    if missing:  # signed out: don't spend a model call to find out
        health.setdefault("mcp", {}).setdefault("offline", {})[job] = "missing env %s" % ",".join(sorted(missing))
        raise JobError("signed out: env %s not set" % ", ".join(sorted(missing)), skip=True)
    if not os.path.isfile(os.path.join(mcp_dir(), "allow.json")):
        ensure_files(cfg)  # never run a job without the read allowlist
    args = builder.build(claude_exe(cfg), task, "mcp", model, common.prompt_path("mcp_job.md"), allowed=reads,
                         disallowed=writes, mcp_config=cfg_path, json_schema=schema, output="stream-json",
                         max_turns=max_turns, cfg=cfg)
    m = health.setdefault("mcp", {})
    ck = "manual_calls" if manual else "calls"
    m.setdefault(ck, {})[day] = int(m.setdefault(ck, {}).get(day) or 0) + 1
    t0 = time.time()
    common.ensure_dir(common.wl("sandbox"))
    rc, so, se = commands.run(args, "claude", cwd=common.wl("sandbox"), input_text=stdin_text or "", timeout=timeout,
                              env_extra=env_extra, cfg=cfg)
    res = parse_stream(so)
    common.write_text(os.path.join(common.wl("cache", "mcp"), job + ".last.jsonl"), redact(so or "", mode="secrets"))
    res["seconds"] = round(time.time() - t0, 1)
    res["missing_env"] = missing
    srv_state = {s.get("name"): s.get("status") for s in res.get("mcp_servers") or []}
    jl = m.setdefault("jobs", {})
    jl[job] = {"at": common.now_iso(), "in": res["input_tokens"], "out": res["output_tokens"], "cost": round(res["cost"], 4),
               "calls": len(res["calls"]), "servers": srv_state, "rc": rc}
    common.log("mcp", "%s rc=%s servers=%s calls=%s in=%s out=%s $%.4f" % (
        job, rc, srv_state, [c["name"] for c in res["calls"]], res["input_tokens"], res["output_tokens"], res["cost"]))
    bad = [n for n, st in srv_state.items() if st != "connected"]
    if bad:
        m.setdefault("offline", {})[",".join(bad)] = common.now_iso()
        raise JobError("server offline or signed out: %s%s" % (", ".join(bad), " (missing env %s)" % missing if missing else ""),
                       skip=True)
    if rc != 0 and not res["out"]:
        raise JobError("claude rc=%s: %s" % (rc, (se or so)[-300:].strip()))
    for c in res["calls"]:
        if c.get("is_error") and ("read-only guard" in str(c.get("result")) or "safety guard" in str(c.get("result"))):
            m.setdefault("denied", []).append({"at": common.now_iso(), "job": job, "tool": c["name"]})
            m["denied"] = m["denied"][-20:]
    return res


def tool_results(res, tool_suffix):
    out = []
    for c in res["calls"]:
        if c["name"].endswith("__" + tool_suffix) and c.get("result") is not None and not c.get("is_error"):
            txt = c["result"]
            try:
                out.append((c["input"], json.loads(txt)))
            except (TypeError, ValueError):
                out.append((c["input"], txt))
    return out


# --------------------------------------------------------------------------- allowlist

ALLOW_DEFAULT = {
    "max_rows": 20,
    "servers": {
        "mcp-atlassian": {
            "read": ["jira_get_issue", "jira_search", "jira_batch_get_changelogs", "jira_get_worklog", "jira_get_user_profile"],
            "write": ["jira_add_comment", "jira_add_issues_to_sprint", "jira_add_watcher", "jira_add_worklog",
                      "jira_assign_issue", "jira_batch_create_issues", "jira_batch_create_versions",
                      "jira_create_customer_request", "jira_create_issue", "jira_create_issue_link",
                      "jira_create_remote_issue_link", "jira_create_sprint", "jira_create_version", "jira_delete_issue",
                      "jira_download_attachments", "jira_edit_comment", "jira_link_to_epic", "jira_move_issue",
                      "jira_move_issues_to_backlog", "jira_remove_issue_link", "jira_remove_watcher",
                      "jira_transition_issue", "jira_update_issue", "jira_update_proforma_form_answers",
                      "jira_update_sprint", "jira_update_version", "confluence_add_comment", "confluence_add_inline_comment",
                      "confluence_add_label", "confluence_copy_page", "confluence_create_page",
                      "confluence_create_page_from_template", "confluence_delete_attachment", "confluence_delete_page",
                      "confluence_download_attachment", "confluence_download_content_attachments", "confluence_move_page",
                      "confluence_reply_to_comment", "confluence_set_page_restrictions", "confluence_update_page",
                      "confluence_update_page_section", "confluence_upload_attachment", "confluence_upload_attachments"],
        },
    },
}

# Allowlist entry for the database MCP, filed under the configured server name (mcp.database.server).
# The tool names are that server's tools.
DATABASE_ALLOW_DEFAULT = {
    "read": ["whoami", "list_connections", "list_schemas", "list_tables", "describe_table", "run_query",
             "get_artifact", "list_artifacts", "get_write_request"],
    "write": ["create_artifact", "update_artifact", "archive_artifact", "create_write_request", "update_write_request"],
    "query_tools": ["run_query"],
    "connections": {"qa": "", "dev": ""},
}


# Roles the jobs use -> the database MCP's tool names (config mcp.database.tools overrides each one).
DATABASE_TOOLS_DEFAULT = {"query": "run_query", "describe_table": "describe_table", "list_tables": "list_tables",
                          "list_schemas": "list_schemas", "list_connections": "list_connections"}


def db_tools(cfg):
    """{role: tool name} for the database MCP: mcp.database.tools over DATABASE_TOOLS_DEFAULT."""
    over = (((cfg or {}).get("mcp") or {}).get("database") or {}).get("tools") or {}
    out = dict(DATABASE_TOOLS_DEFAULT)
    for role in out:
        v = over.get(role) if isinstance(over, dict) else None
        if isinstance(v, str) and v.strip():
            out[role] = v.strip()
    return out


def db_tool(cfg, role):
    return db_tools(cfg)[role]


def _database_allow_entry(cfg):
    """DATABASE_ALLOW_DEFAULT with the configured tool names: a renamed tool replaces the default name in the read
    list (and in query_tools for the query role)."""
    entry = json.loads(json.dumps(DATABASE_ALLOW_DEFAULT))
    _apply_tool_names(entry, cfg, replace=True)
    return entry


def _apply_tool_names(entry, cfg, replace=False):
    """Make the configured (non-default) tool names readable; the query tool is also a query_tools entry, so the
    guard checks its statements. Default names are left exactly as they are."""
    for role, name in db_tools(cfg).items():
        default = DATABASE_TOOLS_DEFAULT[role]
        if name == default:
            continue
        lists = [entry.setdefault("read", [])]
        if role == "query":
            lists.append(entry.setdefault("query_tools", []))
        for lst in lists:
            if replace and default in lst:
                lst[lst.index(default)] = name
            elif name not in lst:
                lst.append(name)
    return entry


def allow_default(cfg=None):
    """Default allowlist: the Jira server's tools, plus the database MCP's under its configured name (if any), plus
    each configured Git-host server with its provider's classified read tools (hostmcp)."""
    from . import hostmcp
    allow = json.loads(json.dumps(ALLOW_DEFAULT))
    srv = server_name(cfg or {}, "database")
    if srv:
        allow["servers"].setdefault(srv, _database_allow_entry(cfg))
    for provider, name in hostmcp.configured(cfg or {}).items():
        allow["servers"].setdefault(name, hostmcp.allow_entry(provider))
    return allow


def configured(cfg):
    """True when some MCP job can run: Jira jobs enabled, database jobs enabled with a server name, or a Git-host
    server recorded by setup. Only then does the daily housekeeping write the guard files (.worklog/mcp/allow.json,
    readonly-settings.json)."""
    from . import hostmcp
    return enabled(cfg, "jira") or bool(enabled(cfg, "database") and server_name(cfg, "database")) or \
        bool(hostmcp.configured(cfg))


def mcp_tool(server, tool):
    return "mcp__%s__%s" % (server, tool)


def ensure_files(cfg):
    """allow.json (kept, only created) and readonly-settings.json (regenerated)."""
    d = mcp_dir()
    common.ensure_dir(d)
    ap = os.path.join(d, "allow.json")
    allow = common.read_json(ap, None)
    if not allow:
        allow = allow_default(cfg)
    db = (cfg.get("mcp") or {}).get("database") or {}
    srv = server_name(cfg, "database")
    if srv:
        envs = (cfg.get("qa_data") or {}).get("allowed_envs") or ["qa"]
        allowed = set()
        for env in envs:  # only connections of the allowed environments; production never
            allowed |= set(((db.get("allowed") or {}).get(env)) or [])
            if (db.get("connections") or {}).get(env):
                allowed.add(db["connections"][env])
        entry = allow.setdefault("servers", {}).setdefault(srv, _database_allow_entry(cfg))
        _apply_tool_names(entry, cfg)
        entry["connections"] = sorted(a for a in allowed if a and "prod" not in a.lower())
    from . import hostmcp
    for provider, name in hostmcp.configured(cfg).items():  # re-classified every time: only the provider's reads
        allow.setdefault("servers", {})[name] = hostmcp.allow_entry(provider, allow["servers"].get(name))
    allow["max_rows"] = int((cfg.get("qa_data") or {}).get("max_rows_per_query") or 20)
    common.write_json(ap, allow)
    # Kept for inspection; every job passes the same settings inline (safety.claude.guard_settings, fail-closed).
    common.write_text(os.path.join(d, "readonly-settings.json"),
                      json.dumps(json.loads(builder.guard_settings("mcp", cfg)), indent=2) + "\n")
    return allow


DEFAULT_SERVERS = {"jira": "mcp-atlassian"}  # the database MCP has no default name: mcp.database.server


def server_name(cfg, key):
    """Configured MCP server name for "jira" / "database" ("" when the database server is not configured)."""
    return (((cfg.get("mcp") or {}).get(key) or {}).get("server")) or DEFAULT_SERVERS.get(key, "")


def enabled(cfg, key):
    """MCP jobs run only when mcp.<key>.enabled is true (absent config = off)."""
    return bool(((cfg.get("mcp") or {}).get(key) or {}).get("enabled", False))


# --------------------------------------------------------------------------- Jira jobs

CARD_SCHEMA = {"type": "object", "properties": {
    "found": {"type": "boolean"}, "title": {"type": "string"}, "type": {"type": "string"}, "status": {"type": "string"},
    "priority": {"type": "string"}, "sprint": {"type": "string"}, "parent": {"type": "string"},
    "summary": {"type": "string", "description": "2-3 plain sentences: the problem and where it stands"},
    "error": {"type": "string"}},
    "required": ["found", "title", "status", "summary"], "additionalProperties": False}


def jira_card(key, cfg, health):
    srv = server_name(cfg, "jira")
    reads = [mcp_tool(srv, "jira_get_issue")]
    task = ("Call jira_get_issue once with issue_key=%s, fields=\"summary,issuetype,status,priority,parent,issuelinks,"
            "description,comment,customfield_10020,labels\", comment_limit=30. Then return the JSON. found=false only when the "
            "tool says the issue does not exist. summary: 2-3 plain sentences on the problem and where it stands." % key)
    res = run("jira-card-" + key, {srv: ["jira_get_issue"]}, reads, _writes(srv), task, CARD_SCHEMA, cfg, health, max_turns=3)
    raw = tool_results(res, "jira_get_issue")
    out = res.get("out") or {}
    rec = {"key": key, "fetched_at": common.now_iso(), "card": out, "raw": raw[0][1] if raw else None,
           "error": next((c["result"] for c in res["calls"] if c.get("is_error")), None)}
    path = common.hp("data", "jira", key + ".json")
    old = common.read_json(path, {}) or {}
    if not raw and old.get("raw"):
        rec["raw"] = old["raw"]  # keep the last good copy
    common.write_json(path, rec)
    if raw and out.get("found"):
        health.setdefault("mcp", {})["jira_auth_ok"] = common.now_iso()
    return rec


DONE_SCHEMA = {"type": "object", "properties": {"done": {"type": "boolean"}}, "required": ["done"],
               "additionalProperties": False}


def jira_evening(day, tickets, cfg, health):
    """One read-only job at 20:00 for the day's tickets: jira_search (comments, worklogs, status) + changelogs.
    The model only makes the two calls; the facts are extracted here from the raw results (Claude Code moves large
    results to files, which parse_stream reads), so long comment texts never pass through the model."""
    srv = server_name(cfg, "jira")
    keys = sorted(tickets)
    jql = "key in (%s)" % ", ".join(keys)
    task = ("Call jira_search exactly once with jql=%s, fields=\"status,comment,worklog\", limit=%d and no other arguments. "
            "Then call jira_batch_get_changelogs exactly once with issue_ids_or_keys=\"%s\". Results may be saved to files: "
            "that is expected, do not retry or read them. Then return {\"done\": true}." % (
                json.dumps(jql), len(keys) + 1, ",".join(keys)))
    res = run("jira-evening", {srv: ["jira_search", "jira_batch_get_changelogs"]},
              [mcp_tool(srv, "jira_search"), mcp_tool(srv, "jira_batch_get_changelogs")], _writes(srv), task,
              DONE_SCHEMA, cfg, health, day=day, max_turns=4, timeout=400)
    search = [unwrap(v) for _, v in tool_results(res, "jira_search")]
    changes = [unwrap(v) for _, v in tool_results(res, "jira_batch_get_changelogs")]
    if not search or not isinstance(search[0], dict):
        err = next((c["result"] for c in res["calls"] if c.get("is_error")), res.get("result"))
        raise JobError("jira_search returned nothing usable: %s" % str(err)[:200], skip=True)
    facts = evening_facts(day, search[0], changes[0] if changes else None, cfg)
    common.write_json(common.hp("data", "jira", "_evening", day + ".json"),
                      {"day": day, "fetched_at": common.now_iso(), "jql": jql, "facts": facts,
                       "raw": search[0], "changelogs": changes[0] if changes else None})
    for iss in search[0].get("issues") or []:  # keep the per-ticket copy current too
        p = common.hp("data", "jira", iss.get("key", "_") + ".json")
        rec = common.read_json(p, {}) or {}
        rec.update(key=iss.get("key"), evening=iss, evening_at=common.now_iso())
        common.write_json(p, rec)
    return facts


def _me(cfg):
    emails = {e.lower() for e in cfg.get("git_author_emails") or []}
    acct = ((cfg.get("mcp") or {}).get("jira") or {}).get("account_id")
    return emails, acct


def _mine(author, emails, acct):
    a = author or {}
    em = (a.get("email") or a.get("emailAddress") or "").lower()
    aid = a.get("account_id") or a.get("accountId")
    return (em and em in emails) or (acct and aid == acct)


def _local(ts):
    t = common.parse_ts(ts) if isinstance(ts, str) else None
    if t is None and isinstance(ts, str):
        try:
            t = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f%z")
        except ValueError:
            return None
    return t.astimezone() if t else None


def evening_facts(day, search, changes, cfg):
    start, end = common.day_bounds(day, cfg)
    emails, acct = _me(cfg)
    ids = {}
    out = {"comments": [], "worklogs": [], "status_changes": []}
    for iss in search.get("issues") or []:
        key = iss.get("key")
        ids[str(iss.get("id"))] = key
        for c in iss.get("comments") or (iss.get("comment") or {}).get("comments") or []:
            t = _local(c.get("created"))
            if t and start <= t < end and _mine(c.get("author"), emails, acct):
                body = " ".join(str(c.get("body") or "").split())
                out["comments"].append({"ticket": key, "time": t.isoformat(timespec="seconds"), "text": body[:300]})
        for w in (iss.get("worklog") or {}).get("worklogs") or []:
            t = _local(w.get("started"))
            if t and start <= t < end and _mine(w.get("author"), emails, acct):
                out["worklogs"].append({"ticket": key, "started": t.isoformat(timespec="seconds"),
                                        "minutes": int(round(int(w.get("timeSpentSeconds") or 0) / 60))})
    for ch in changes or []:
        key = ids.get(str(ch.get("issue_id"))) or ch.get("issue_key") or ch.get("key")
        for h in ch.get("changelogs") or []:
            t = _local(h.get("created"))
            if not (t and start <= t < end):
                continue
            for it in h.get("items") or []:
                if it.get("field") == "status":
                    out["status_changes"].append({"ticket": key, "time": t.isoformat(timespec="seconds"),
                                                  "from": it.get("from_string"), "to": it.get("to_string"),
                                                  "mine": bool(_mine(h.get("author"), emails, acct))})
    return out


def _writes(srv, cfg=None):
    a = (allow_default(cfg)["servers"].get(srv) or {}).get("write") or []
    allow = common.read_json(os.path.join(mcp_dir(), "allow.json"), None)
    if allow:
        a = ((allow.get("servers") or {}).get(srv) or {}).get("write") or a
    return [mcp_tool(srv, t) for t in a]


# --------------------------------------------------------------------------- database MCP jobs

def database_conn(cfg, env="qa", table=None):
    """Connection for a table: by its schema (mcp.database.schema_map), else the env's default connection."""
    db = (cfg.get("mcp") or {}).get("database") or {}
    if table and "." in table:
        parts = table.lower().split(".")
        for sch in parts[:-1][::-1]:
            c = (db.get("schema_map") or {}).get(sch)
            if c and (not env or c.startswith(env)):
                return c
        if db.get("schema_map"):
            return ""  # a schema we can't place: never guess a connection
    return ((db.get("connections")) or {}).get(env) or ""


def _db(srv, *tools):
    return [mcp_tool(srv, t) for t in tools]


def _db_server(cfg):
    srv = server_name(cfg, "database")
    if not srv:
        raise JobError("no database MCP server configured (mcp.database.server)", skip=True)
    return srv


CATALOG_SQL = {
    "postgres": {
        "triggers": ("SELECT c.relname AS table_name, t.tgname AS trigger_name, pg_get_triggerdef(t.oid) AS trigger_def, "
                     "p.prosrc AS function_src FROM pg_trigger t JOIN pg_class c ON t.tgrelid = c.oid "
                     "JOIN pg_namespace n ON c.relnamespace = n.oid JOIN pg_proc p ON p.oid = t.tgfoid "
                     "WHERE NOT t.tgisinternal AND n.nspname = '{schema}' AND c.relname IN ({names}) LIMIT 20"),
        "writers": ("SELECT n.nspname AS schema_name, p.proname AS name, CASE p.prokind WHEN 'p' THEN 'procedure' "
                    "ELSE 'function' END AS kind, p.prosrc AS src FROM pg_proc p JOIN pg_namespace n "
                    "ON p.pronamespace = n.oid WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                    "AND ({likes}) LIMIT 20"),
        "like": "p.prosrc ILIKE '%{name}%'",
    },
    "mssql": {
        "triggers": ("SELECT TOP 20 OBJECT_NAME(t.parent_id) AS table_name, t.name AS trigger_name, m.definition AS trigger_def, "
                     "m.definition AS function_src FROM sys.triggers t JOIN sys.sql_modules m ON m.object_id = t.object_id "
                     "WHERE OBJECT_NAME(t.parent_id) IN ({names})"),
        "writers": ("SELECT TOP 20 OBJECT_SCHEMA_NAME(o.object_id) AS schema_name, o.name AS name, o.type_desc AS kind, "
                    "m.definition AS src FROM sys.sql_modules m JOIN sys.objects o ON o.object_id = m.object_id "
                    "WHERE o.type IN ('P', 'FN', 'TF', 'IF', 'V', 'TR') AND ({likes})"),
        "like": "m.definition LIKE '%{name}%'",
    },
}

SCHEMA_DONE = {"type": "object", "properties": {"done": {"type": "boolean"}}, "required": ["done"], "additionalProperties": False}


def conn_type(cfg, conn):
    t = ((((cfg.get("mcp") or {}).get("database") or {}).get("types")) or {}).get(conn)
    if t:
        return t
    return "mssql" if "mssql" in conn else ("mongodb" if "mongo" in conn else ("elasticsearch" if "-es" in conn else "postgres"))


def rows(result):
    """Query-tool result of the database MCP (JSON rows, or a text line such as 'N row(s) in Xms' followed by a JSON
    array) -> list of dicts."""
    r = unwrap(result)
    if isinstance(r, list):
        return [x for x in r if isinstance(x, dict)]
    if isinstance(r, str):
        i = r.find("[")
        if i >= 0:
            try:
                v = json.loads(r[i:])
                return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
            except ValueError:
                return []
    return []


def _writes_of(src, table):
    """Columns a proc/function/trigger body writes on `table`: UPDATE ... SET a = .., INSERT INTO t (a, b), NEW.a :=."""
    if not src:
        return []
    name = re.escape(table.split(".")[-1])
    out = []
    for m in re.finditer(r"(?is)\bupdate\s+(?:[\w\"\[\]]+\.)?[\"\[]?%s[\"\]]?\s+(?:as\s+\w+\s+)?set\s+(.*?)(?:\bwhere\b|\bfrom\b|;|\breturning\b|$)" % name, src):
        out += [c for c in re.findall(r"(?:^|,)\s*(?:\w+\.)?[\"\[]?(\w+)[\"\]]?\s*=(?!=)", m.group(1))]
    for m in re.finditer(r"(?is)\binsert\s+into\s+(?:[\w\"\[\]]+\.)?[\"\[]?%s[\"\]]?\s*\(([^)]*)\)" % name, src):
        out += [c.strip(" \"[]\n\t") for c in m.group(1).split(",")]
    out += re.findall(r"(?i)\bNEW\.(\w+)\s*:=", src)
    seen = []
    for c in out:
        if c and c not in seen:
            seen.append(c)
    return seen


def schema_cards(tables, cfg, health, env="qa", focus_columns=None):
    """Batch (<= 10 tables, one connection) schema cards in ONE model turn: describe_table for each table plus two
    catalog queries written here (triggers with their function source; procs/functions/views whose source names the
    tables). The card is built in Python from the raw rows -> data/db-schema/<connection>/<schema>.<table>.json."""
    srv = _db_server(cfg)
    q_tool, d_tool = db_tool(cfg, "query"), db_tool(cfg, "describe_table")
    tables = list(tables)[:10]
    conn = database_conn(cfg, env, tables[0] if tables else None)
    if not conn:
        raise JobError("no database MCP %s connection configured" % env, skip=True)
    tables = [t for t in tables if database_conn(cfg, env, t) == conn]
    ctype = conn_type(cfg, conn)
    sqlset = CATALOG_SQL.get(ctype)
    by_schema = {}
    for t in tables:
        parts = t.split(".")
        by_schema.setdefault(parts[-2] if len(parts) > 1 else "", []).append(parts[-1])
    calls = []
    for sch, names in by_schema.items():
        for n in names:
            calls.append("%s connectionId=%s%s table=%s" % (d_tool, conn, " schema=%s" % sch if sch else "", n))
    if sqlset:
        all_names = [n for ns in by_schema.values() for n in ns]
        quoted = ", ".join("'%s'" % n.replace("'", "") for n in all_names)
        for sch, names in by_schema.items():
            calls.append("%s connectionId=%s limit=20 sql=%s" % (q_tool, conn, json.dumps(
                sqlset["triggers"].format(schema=sch.replace("'", ""), names=", ".join("'%s'" % n for n in names)))))
        calls.append("%s connectionId=%s limit=20 sql=%s" % (q_tool, conn, json.dumps(
            sqlset["writers"].format(likes=" OR ".join(sqlset["like"].format(name=n.replace("'", "")) for n in all_names)))))
        del quoted
    task = ("Make ALL of these tool calls in a single turn (parallel), with exactly these arguments, then return "
            "{\"done\": true}. Do not make any other call, do not retry, do not read saved outputs.\n" +
            "\n".join("%d. %s" % (i + 1, c) for i, c in enumerate(calls)))
    res = run("database-schema", {srv: [d_tool, q_tool]}, _db(srv, d_tool, q_tool),
              _writes(srv, cfg), task, SCHEMA_DONE, cfg, health, max_turns=3, timeout=600)
    desc = {}
    for inp, val in tool_results(res, d_tool):
        desc[str(inp.get("table") or "").lower()] = unwrap(val)
    trig, writers = [], []
    for c in res["calls"]:
        if not c["name"].endswith("__" + q_tool) or c.get("is_error"):
            continue
        sql = str((c.get("input") or {}).get("sql") or "").lower()
        if "trigger_name" in sql:
            trig += rows(c["result"])
        elif " src " in sql or "as src" in sql:
            writers += rows(c["result"])
    saved = []
    for t in tables:
        name = t.split(".")[-1]
        tg = []
        for r in trig:
            if str(r.get("table_name") or "").lower() != name.lower():
                continue
            m = re.search(r"(?i)\b(BEFORE|AFTER|INSTEAD OF)\s+(.+?)\s+ON\b", str(r.get("trigger_def") or ""))
            tg.append({"name": r.get("trigger_name"), "event": (m.group(1) + " " + m.group(2)) if m else "",
                       "sets_columns": _writes_of(r.get("function_src"), t) or
                       sorted(set(re.findall(r"(?i)\bNEW\.(\w+)\s*:=", str(r.get("function_src") or ""))))})
        wr = []
        for r in writers:
            src = str(r.get("src") or "")
            if not re.search(r"(?i)\b%s\b" % re.escape(name), src):
                continue
            wr.append({"name": "%s.%s" % (r.get("schema_name"), r.get("name")), "kind": str(r.get("kind") or "").lower(),
                       "writes": _writes_of(src, t), "reads": []})
        rec = {"table": t, "connection": conn, "env": env, "type": ctype, "fetched_at": common.now_iso(),
               "describe": desc.get(name.lower()), "card": {"table": t, "found": name.lower() in desc, "triggers": tg,
                                                             "writers": wr}}
        p = common.hp("data", "db-schema", _safe(conn), _safe(t) + ".json")
        common.write_json(p, json.loads(redact(json.dumps(rec, ensure_ascii=False), mode="secrets")))
        saved.append(p)
    return saved, res


TRACE_SCHEMA = {"type": "object", "properties": {
    "steps": {"type": "array", "items": {"type": "object", "properties": {
        "table": {"type": "string"}, "key": {"type": "string", "description": "column = value used to find the row(s)"},
        "values": {"type": "string", "description": "the relevant columns and their actual values, short"},
        "note": {"type": "string"}}, "required": ["table", "key", "values", "note"], "additionalProperties": False}},
    "changes": {"type": "array", "items": {"type": "object", "properties": {
        "table": {"type": "string"}, "column": {"type": "string"}, "before": {"type": "string"}, "after": {"type": "string"}},
        "required": ["table", "column", "before", "after"], "additionalProperties": False}},
    "summary": {"type": "string"}}, "required": ["steps", "changes", "summary"], "additionalProperties": False}


def _card(cfg, table, env="qa"):
    conn = database_conn(cfg, env, table)
    return common.read_json(common.hp("data", "db-schema", _safe(conn), _safe(table) + ".json"), {}) or {}


def _cols(card):
    d = card.get("describe")
    return [c for c in d if isinstance(c, dict) and c.get("name")] if isinstance(d, list) else []


def trace_plan(start_ids, flow_tables, cfg, env="qa"):
    """-> (start_table, start_value, [(table, connectionId, sql)]). Keys come from the schema cards: the start table by
    its id, tables with a foreign key to it, and its audit table. Only needed columns, newest first, <= 20 rows.
    An id key names its entity (orderId -> orders); mcp.database.entity_prefixes lists system prefixes to strip
    first (e.g. "legacy" for legacyOrderId)."""
    vals = [kv.split("=", 1) for kv in start_ids if "=" in kv]
    tables = sorted(flow_tables)
    start = None
    prefixes = [p for p in (((cfg.get("mcp") or {}).get("database") or {}).get("entity_prefixes") or []) if p]
    ent_rx = (r"(?i)^(%s)|id$" % "|".join(re.escape(p) for p in prefixes)) if prefixes else r"(?i)id$"
    for k, v in vals:
        ent = re.sub(ent_rx, "", k).lower()
        for t in tables:
            name = t.split(".")[-1].lower()
            if ent and name.rstrip("s") == ent.rstrip("s") and any(c["name"] == "id" for c in _cols(_card(cfg, t, env))):
                start = (t, v)
                break
        if start:
            break
    if not start:
        return None, None, []
    st, sv = start
    sname = st.split(".")[-1].lower()
    lit = sv if re.fullmatch(r"\d+", sv) else "'%s'" % sv.replace("'", "")
    plan = []
    for t in tables:
        card = _card(cfg, t, env)
        cols = _cols(card)
        if not cols:
            continue
        names = [c["name"] for c in cols]
        key = None
        if t == st:
            key = "id"
        else:
            for c in cols:
                ref = str(c.get("references") or "").lower()
                if ref.endswith("%s.id" % sname):
                    key = c["name"]
                    break
            if not key and t.split(".")[-1].lower().startswith(sname) and "id" in names:
                key = "id"  # audit / history copy of the start table
        if not key:
            continue
        tiers = [lambda n: n in (flow_tables.get(t) or []),
                 lambda n: re.search(r"(?i)lock|status|attempt|reason|result|event|role|active", n) and not n.endswith("_mod"),
                 lambda n: n in ("rev", "revtype") or re.search(r"(?i)(occurred|updated|locked|revoked)_at$", n)]
        phi = {c["name"] for c in cols if c.get("isPhiField")}  # columns the server flags as patient data
        pick = [key] + [n for tier in tiers for n in names if tier(n) and n != key and n not in phi]
        pick = list(dict.fromkeys(pick))[:12]
        order = next((n for n in ("rev", "occurred_at", "updated_at", "created_at", "id") if n in names and n != key or n == "rev"
                      and n in names), None)
        ctype = conn_type(cfg, card.get("connection") or database_conn(cfg, env, t))
        cl = ", ".join(pick)
        if ctype == "mssql":
            sql = "SELECT TOP 20 %s FROM %s WHERE %s = %s%s" % (cl, t, key, lit, (" ORDER BY %s DESC" % order) if order else "")
        else:
            sql = "SELECT %s FROM %s WHERE %s = %s%s LIMIT 20" % (cl, t, key, lit, (" ORDER BY %s DESC" % order) if order else "")
        plan.append((t, card.get("connection") or database_conn(cfg, env, t), sql))
    return st, sv, plan[:8]


def qa_trace(ticket, start_ids, flow_tables, cfg, health, before=None, env="qa"):
    """Follow a real QA record through the flow's tables in ONE model turn: the SELECTs are planned here from the
    schema cards (trace_plan); the model runs them in parallel and summarises the journey with the actual values."""
    srv = _db_server(cfg)
    q_tool = db_tool(cfg, "query")
    st, sv, plan = trace_plan(start_ids, flow_tables, cfg, env)
    if not plan:
        raise JobError("no trace plan for %s (schema cards missing or no start id)" % ticket, skip=True)
    task = (
        "Ticket %s, QA. Make ALL of these %s calls in a single turn (parallel), with exactly these arguments; no "
        "other calls, no retries:\n%s\nThen return the journey table by table with the actual values (key = the WHERE "
        "condition; values = the relevant columns of the newest row and how many rows came back)%s." % (
            ticket, q_tool, "\n".join("%d. %s connectionId=%s limit=20 sql=%s" % (i + 1, q_tool, c, json.dumps(q))
                                      for i, (t, c, q) in enumerate(plan)),
            ("; values recorded before the replay: %s - report what changed in 'changes'" % json.dumps(before)[:1500])
            if before else "; leave 'changes' empty"))
    res = run("database-trace-" + ticket, {srv: [q_tool]}, _db(srv, q_tool), _writes(srv, cfg), task,
              TRACE_SCHEMA,
              cfg, health, max_turns=3, timeout=600)
    got = [{"sql": c["input"], "rows": rows(c["result"])[:20]} for c in res["calls"]
           if c["name"].endswith("__" + q_tool) and not c.get("is_error")]
    rec = {"ticket": ticket, "env": env, "connection": plan[0][1], "fetched_at": common.now_iso(), "start_ids": start_ids,
           "start": {"table": st, "value": sv}, "trace": res.get("out") or {}, "queries": got}
    common.write_json(common.hp("data", "qa-trace", ticket + ".json"),
                      json.loads(redact(json.dumps(rec, ensure_ascii=False, default=str), mode="secrets")))
    return rec


def _split(col):
    """'schema.table.column' (optionally db-prefixed) -> (schema, table, column)."""
    parts = col.split(".")
    if len(parts) < 3:
        return None
    return parts[-3], parts[-2], parts[-1]


def link_sql(ctype, frm, to, n=20):
    fs, ft, fc = frm
    ts, tt, tc = to
    if ctype == "mssql":
        return ("SELECT COUNT(*) AS sampled, COUNT(t.%s) AS matched FROM (SELECT DISTINCT TOP %d f.%s AS v FROM %s.%s f "
                "WHERE f.%s IS NOT NULL) s LEFT JOIN %s.%s t ON t.%s = s.v" % (tc, n, fc, fs, ft, fc, ts, tt, tc))
    return ("SELECT COUNT(*) AS sampled, COUNT(t.%s) AS matched FROM (SELECT DISTINCT f.%s AS v FROM %s.%s f WHERE f.%s "
            "IS NOT NULL LIMIT %d) s LEFT JOIN %s.%s t ON t.%s = s.v" % (tc, fc, fs, ft, fc, n, ts, tt, tc))


def link_map(candidates, tables, cfg, health, env="qa"):
    """Verify candidate links (same connection) with one aggregate query each, all in ONE model turn: sample <= 20
    distinct non-null values on the 'from' side and count how many exist on the 'to' side. Cross-database candidates
    are listed as unverified. -> data/db-schema/_linkmap.json"""
    srv = _db_server(cfg)
    q_tool = db_tool(cfg, "query")
    jobs, links, seen = [], [], set()
    for frm, to, why in candidates:
        a, b = _split(frm), _split(to)
        if not a or not b or (frm, to) in seen:
            continue
        seen.add((frm, to))
        ca, cb = database_conn(cfg, env, frm.rsplit(".", 1)[0]), database_conn(cfg, env, to.rsplit(".", 1)[0])
        if not ca or ca != cb:
            links.append({"from": frm, "to": to, "sampled": 0, "matched": 0, "evidence": why + "; cross-database, not sampled"})
            continue
        jobs.append((frm, to, why, ca, link_sql(conn_type(cfg, ca), a, b)))
    jobs = jobs[:12]
    if jobs:
        task = ("Make ALL of these %s calls in a single turn (parallel), with exactly these arguments, then return "
                "{\"done\": true}. No other calls, no retries.\n" % q_tool +
                "\n".join("%d. %s connectionId=%s limit=20 sql=%s" % (i + 1, q_tool, j[3], json.dumps(j[4]))
                          for i, j in enumerate(jobs)))
        res = run("database-linkmap", {srv: [q_tool]}, _db(srv, q_tool), _writes(srv, cfg), task, SCHEMA_DONE,
                  cfg,
                  health, max_turns=3, timeout=600)
        got = {}
        for c in res["calls"]:
            if c["name"].endswith("__" + q_tool) and not c.get("is_error"):
                r = rows(c["result"])
                if r:
                    got[str((c.get("input") or {}).get("sql") or "").strip()] = r[0]
        for frm, to, why, conn, sql in jobs:
            r = got.get(sql.strip())
            links.append({"from": frm, "to": to, "connection": conn, "evidence": why,
                          "sampled": int((r or {}).get("sampled") or 0), "matched": int((r or {}).get("matched") or 0),
                          "checked": bool(r)})
    hier = {}
    for t in sorted(tables):
        parts = t.split(".")
        if len(parts) < 2:
            continue
        conn = database_conn(cfg, env, t)
        hier.setdefault((conn, parts[-2]), []).append(parts[-1])
    rec = {"env": env, "fetched_at": common.now_iso(), "result": {
        "links": links, "hierarchy": [{"database": c, "schema": s, "tables": sorted(v)} for (c, s), v in sorted(hier.items())]}}
    common.write_json(common.hp("data", "db-schema", "_linkmap.json"), rec)
    return rec


def _safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s))[:120]
