"""MCP tool guard and the per-profile decision for every tool call a guarded model makes.

Profiles
  none            extraction / consolidation: no tool at all (only the structured answer)
  mcp             Jira / database / Git-host MCP jobs: read tools on .worklog/mcp/allow.json, every statement through the
                  query guard
  slack-send      only the configured send tool, only to my own Slack user ID (or my DM channel), plain text, capped
  slack-discover  only reading my own profile / looking up a user
  session         a /cg: command in my session: read tools, Chitragupta's own helper, the read tools of Chitragupta's
                  own MCP server (OWN_MCP_TOOLS, no allow.json entry needed), other MCP reads as for `mcp`

A floor that allow.json can't lift: an MCP tool whose name says it writes (create, update, delete, send, comment,
transition, upload, …) is never a read, whatever the allowlist says. The Slack send tool is the one exception, and
only under slack-send.

Git-host MCP servers (allow.json entries with "role": "git-host"): "PR data may be read through a Git host's MCP server
only with the read tools `/cg:setup` classified as read, through the same read-only harness and budget as the other
MCP jobs." A second floor holds here: the tool must also be one of the provider's classified PR / branch reads below
(GIT_HOST_READ), and a tool that takes an action / method argument only with a read action. An unknown provider has
no read tools at all.

Approved scope (a database server entry with `scopes`, written from mcp.database.scope): after the connection check,
a call to a connection without a scope entry is refused, schema / object arguments must name a permitted schema, and
a query tool's statement must pass query.statement_scope_reason for that connection's engine and permitted
databases / schemas. Without `scopes` (no explicit scope configured) nothing here changes.

Plan pin (profile mcp, a row-trace job's own `claude -p` call, CHITRAGUPTA_DB_PIN = <home>/.worklog/cache/db-jobs/
<job>.pin.json): after the ordinary guard allows a call, it is allowed only when (tool, connection, sha256 of the
statement) is one of the pin's calls not yet used (each use is claimed once with an exclusive marker file and appended
to <job>.calls.jsonl), before the pin's deadline_epoch, without a <job>.cancel flag, and while the access policy
generation (dbaccess.policy_generation) still equals the pin's. The pin can only refuse more; it never allows a call
the ordinary guard refused.
"""
import hashlib
import json
import os
import re
import shlex
import time

from .. import common
from . import query

ALLOW_PATH_PARTS = ("mcp", "allow.json")
WRITE_VERB = re.compile(r"(?:^|_)(?:create|update|delete|remove|add|edit|set|transition|assign|move|link|unlink|upload|"
                        r"archive|send|post|put|patch|write|insert|merge|drop|alter|execute|exec|approve|close|complete|"
                        r"lock|unlock|schedule|react|reply|comment|import|rename|copy|invite|kick|join|leave|publish|"
                        r"restore|reset|revoke|grant|vote|run_pipeline|trigger|cancel|abandon|reopen|resolve)(?:_|$)",
                        re.I)
MAX_SLACK_CHARS = 8000
SLACK_KEYS = {"channel_id", "channel", "message", "text", "unfurl_app_links", "unfurl_links", "unfurl_media"}
SQL_KEYS = ("query", "sql", "statement", "text", "command", "q")
# Database servers with `connections` in allow.json: the argument that picks the connection (any key containing
# "connection", or one of these), and other routing arguments whose value must also be an allowed connection.
CONN_KEYS = ("conn", "conn_id", "connid", "datasource", "data_source", "database_id", "dsn")
ROUTING_KEYS = ("env", "environment", "server", "server_name", "servername", "host", "hostname", "instance", "target",
                "profile", "cluster", "db", "database", "dbname", "db_name")
# Tools that take no connection (identity, listing the connections). allow.json `connectionless` overrides it per
# server; a query tool is never connectionless.
CONNECTIONLESS_DEFAULT = ["whoami", "list_connections"]
# Git-host MCP read tools per provider, as /cg:setup classifies them (everything else on such a server is a write or
# unknown, and blocked). tool -> None, or the read values of its `action` / `method` argument.
# Azure DevOps (microsoft/azure-devops-mcp): v2.4+ names (one tool per area, with an action) and the older names.
# GitHub (github/github-mcp-server, and the older reference server): pull-request and branch reads.
GIT_HOST_READ = {
    "azure-devops": {
        "repo_pull_request": {"get", "list", "list_by_commits"},
        "repo_branch": {"get", "list", "list_mine"},
        "repo_repository": {"get", "list"},
        "repo_list_pull_requests_by_repo_or_project": None, "repo_list_pull_requests_by_repo": None,
        "repo_list_pull_requests_by_project": None, "repo_get_pull_request_by_id": None,
        "repo_list_pull_requests_by_commits": None, "repo_list_branches_by_repo": None,
        "repo_list_my_branches_by_repo": None, "repo_get_branch_by_name": None, "repo_list_repos_by_project": None,
        "repo_get_repo_by_name_or_id": None,
    },
    "github": {
        "list_pull_requests": None, "search_pull_requests": None, "list_branches": None,
        "pull_request_read": {"get", "get_status", "get_files", "get_commits", "get_reviews", "get_review_comments",
                              "get_comments", "get_check_runs", "get_diff"},
        "get_pull_request": None, "get_pull_request_status": None, "get_pull_request_reviews": None,
        "get_pull_request_files": None, "get_pull_request_comments": None,
    },
}
ACTION_KEYS = ("action", "method")
# Chitragupta's own MCP server (chitragupta/mcpserver.py, started from the plugin manifest as server `chitragupta` of
# plugin `cg`): read-only by construction, every tool a read of the data home. Pinned here (a test keeps it equal to
# mcpserver.TOOL_NAMES) so the guard never imports the server. Session profile only: internal calls never see it.
OWN_MCP_SERVER = "plugin_cg_chitragupta"
OWN_MCP_TOOLS = frozenset({"search", "get_ticket", "get_day", "get_timesheet", "get_branch_status", "get_db_table",
                           "get_db_flow", "get_db_investigation", "get_db_impact", "get_db_comparison",
                           "list_tickets", "get_alerts", "get_progress"})
# Policy A for another plugin's MCP tools during a /cg: command: denied (a /cg: command only uses Chitragupta's tools),
# but they're the other plugin's own background calls, not an attempt to write: logged with this category and left
# out of the violation counts (log.summary).
OTHER_PLUGIN = "other-plugin"
OTHER_PLUGIN_REASON = "not a Chitragupta tool; skip this call and don't retry it"
SESSION_READ = {"Read", "Grep", "Glob", "LS", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
                "ToolSearch", "AskUserQuestion", "StructuredOutput", "ListMcpResourcesTool", "ReadMcpResourceTool"}


def load_allow():
    return common.read_json(common.wl(*ALLOW_PATH_PARTS), {}) or {}


def split_tool(name):
    if not name.startswith("mcp__"):
        return None, name
    rest = name[5:]
    i = rest.find("__")
    return (rest[:i], rest[i + 2:]) if i > 0 else (None, rest)


def writes(tool):
    return bool(WRITE_VERB.search(tool or ""))


def mcp_reason(tool_name, tool_input, allow=None):
    """The `mcp` profile: allowlist + floor + query guard."""
    allow = load_allow() if allow is None else allow
    server, tool = split_tool(tool_name)
    if not server:
        return "built-in tool %s is not allowed in a read-only job" % tool_name
    srv = (allow.get("servers") or {}).get(server)
    if not srv:
        return "server %s is not on the allowlist" % server
    if writes(tool) and tool not in (srv.get("query_tools") or []):  # a query tool (execute_sql) passes the query guard
        return "tool %s writes" % tool
    if tool in (srv.get("write") or []) or tool not in (srv.get("read") or []):
        return "tool %s is not on the read allowlist" % tool
    inp = tool_input if isinstance(tool_input, dict) else {}
    if srv.get("role") == "git-host":
        return git_host_reason(srv.get("provider"), tool, inp)
    max_rows = int(allow.get("max_rows") or 20)
    if srv.get("connections") is not None:
        why = connection_reason(srv, tool, inp) or scope_reason(srv, tool, inp)
        if why:
            return why
    if tool in (srv.get("query_tools") or []):
        lim = inp.get("limit")
        capped = False
        if lim is not None:
            n = lim if isinstance(lim, int) and not isinstance(lim, bool) else \
                int(lim.strip()) if isinstance(lim, str) and lim.strip().isdigit() else None
            if n is None:
                return "limit %r is not a whole number" % (lim,)
            if n < 1 or n > max_rows:
                return "limit %s is outside 1..%d" % (n, max_rows)
            capped = True  # the tool caps the rows itself: the statement needs no TOP / LIMIT of its own
        sql = next((inp[k] for k in SQL_KEYS if isinstance(inp.get(k), str)), None)
        if _is_mongo(srv, inp) or (sql is None and "filter" in inp):
            return query.mongo_reason(inp)
        if sql is None:
            return "query tool called without a recognisable statement field"
        rest = {k: v for k, v in inp.items() if k not in SQL_KEYS}
        m = query.MONGO_WRITE_KEYS.search(json.dumps(rest, ensure_ascii=False))
        if m:
            return "write operator or option %s next to the statement" % m.group(0).strip('":')
        return query.sql_reason(sql, max_rows, require_limit=not capped) or _statement_scope(srv, inp, sql)
    return None


# --------------------------------------------------------------------------- approved scope (allow.json `scopes`)

SCHEMA_ARGS = ("schema", "schema_name", "schemaname", "owner")
OBJECT_ARGS = ("table", "table_name", "tablename", "object", "object_name", "view", "view_name")


def _scope_of(srv, inp):
    """(None, None) without `scopes`; else (connection, its scope dict or None)."""
    scopes = srv.get("scopes")
    if not isinstance(scopes, dict):
        return None, None
    conns = [str(v) for k, v in inp.items() if _conn_key(k) and isinstance(v, (str, int)) and not isinstance(v, bool)]
    if not conns:
        return "", None
    sc = scopes.get(conns[0])
    return conns[0], sc if isinstance(sc, dict) else None


def scope_reason(srv, tool, inp):
    """A database server with `scopes`: the call's connection has an approved scope, and its schema / object arguments
    name only permitted schemas. Connectionless calls (connection_reason already allowed them) pass."""
    conn, sc = _scope_of(srv, inp)
    if conn is None or conn == "":
        return None
    if sc is None:
        return "connection %s has no approved scope" % conn[:80]
    names = srv.get("scope_args") if isinstance(srv.get("scope_args"), dict) else {}
    s_keys = set(SCHEMA_ARGS) | {str(k).lower() for k in names.get("schema") or []}
    o_keys = set(OBJECT_ARGS) | {str(k).lower() for k in names.get("table") or []}
    schema = [v for k, v in inp.items() if str(k).lower() in s_keys]
    objs = [v for k, v in inp.items() if str(k).lower() in o_keys]
    if len(schema) > 1 or len(objs) > 1:
        return "more than one schema or object argument"
    if not schema and not objs:
        return None
    return query.name_scope_reason(objs[0] if objs else None, sc.get("engine"), sc, schema[0] if schema else None)


def _statement_scope(srv, inp, sql):
    conn, sc = _scope_of(srv, inp)
    if conn is None:
        return None
    if sc is None:
        return "connection %s has no approved scope" % (conn or "?")[:80]
    return query.statement_scope_reason(sql, sc.get("engine"), sc)


# --------------------------------------------------------------------------- plan pin (CHITRAGUPTA_DB_PIN)

PIN_DIR = ("cache", "db-jobs")
PIN_NAME = re.compile(r"^(j_[0-9a-f]{8,40})\.pin\.json$")


def call_statement(inp):
    """What a pin hashes for a call: the statement of a SQL call (stripped), else the call's other arguments as
    canonical JSON (the connection argument is pinned separately)."""
    inp = inp if isinstance(inp, dict) else {}
    sql = next((inp[k] for k in SQL_KEYS if isinstance(inp.get(k), str)), None)
    if sql is not None:
        return sql.strip()
    return json.dumps({k: v for k, v in inp.items() if not _conn_key(k)}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def call_digest(inp):
    return hashlib.sha256(call_statement(inp).encode("utf-8")).hexdigest()


def _call_connection(inp):
    conns = [str(v) for k, v in (inp or {}).items() if _conn_key(k) and isinstance(v, (str, int)) and not isinstance(v, bool)]
    return conns[0] if len(conns) == 1 else ""


def pin_reason(pin_path, tool_name, tool_input, generation=None, now=None):
    """None when the call is an unused call of the job's pin (and claims it), else the reason. `generation`: the
    current policy generation (computed from the config when None)."""
    from . import paths
    d = os.path.abspath(common.wl(*PIN_DIR))
    p = os.path.abspath(str(pin_path or ""))
    m = PIN_NAME.match(os.path.basename(p))
    if not m or os.path.normcase(os.path.dirname(p)) != os.path.normcase(d):
        return "plan pin outside Chitragupta's job folder"
    job = m.group(1)
    pin = common.read_json(p, None)
    if not isinstance(pin, dict) or pin.get("job") != job or not isinstance(pin.get("calls"), list):
        return "plan pin unreadable or not for job %s" % job
    if os.path.exists(os.path.join(d, job + ".cancel")):
        return "job %s was cancelled" % job
    deadline = pin.get("deadline_epoch")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return "plan pin without a deadline"
    if (time.time() if now is None else now) > deadline:
        return "job %s is past its deadline" % job
    if generation is None:
        from .. import dbaccess
        generation = dbaccess.policy_generation(common.load_config())
    if not pin.get("policy_generation") or pin.get("policy_generation") != generation:
        return "the access policy changed since job %s was planned" % job
    server, tool = split_tool(str(tool_name or ""))
    if not server or server != pin.get("server"):
        return "server %s is not in job %s's plan" % (server or tool_name, job)
    inp = tool_input if isinstance(tool_input, dict) else {}
    conn, digest = _call_connection(inp), call_digest(inp)
    matched = False
    for i, c in enumerate(pin["calls"]):
        if not isinstance(c, dict) or c.get("tool") != tool or str(c.get("connection") or "") != conn or \
                c.get("sha256") != digest:
            continue
        matched = True
        try:
            os.close(paths.create_exclusive(os.path.join(d, "%s.used.%d" % (job, i)), caller="safety.pin"))
        except FileExistsError:
            continue  # this copy was used; an identical later entry may still be free
        paths.append_text(os.path.join(d, job + ".calls.jsonl"), json.dumps(
            {"at": common.now_iso(), "call": i, "tool": tool, "connection": conn, "sha256": digest}) + "\n",
            caller="safety.pin")
        return None
    return ("pinned call already used (job %s)" % job) if matched else ("call is not in job %s's plan" % job)


def _is_mongo(srv, inp):
    """A MongoDB call: a pipeline / collection argument, a db.* shell string, or a connection typed (or named) mongo.
    A SQL call that only has a `filter` argument is not one."""
    if any(k in inp for k in ("pipeline", "collection")):
        return True
    if any(isinstance(v, str) and v.strip().startswith("db.") for v in inp.values()):
        return True
    types = srv.get("types") if isinstance(srv.get("types"), dict) else {}
    conns = [str(v) for k, v in inp.items() if _conn_key(k) and isinstance(v, (str, int))]
    return any("mongo" in c.lower() or str(types.get(c) or "").lower() in ("mongo", "mongodb") for c in conns)


def _conn_key(k):
    k = str(k).lower()
    return "connection" in k or k in CONN_KEYS


def connection_reason(srv, tool, inp):
    """A database server with `connections`: every tool call names exactly one allowed connection (so the server's
    default connection is never used), except the identity / list-connections tools on `connectionless` (never a
    query tool). A routing argument under another name (env, server, database …) must also name an allowed one."""
    conns = srv.get("connections")
    if isinstance(conns, dict):
        ok = {str(c) for c in conns.values() if c}
    elif isinstance(conns, list):
        ok = {str(c) for c in conns if c}
    else:
        ok = set()
    given = []
    for k, v in inp.items():
        if not (_conn_key(k) or str(k).lower() in ROUTING_KEYS):
            continue
        if isinstance(v, bool) or not isinstance(v, (str, int)) or str(v) not in ok:
            return "connection %s=%s is not an allowed (QA/dev) connection" % (k, str(v)[:80])
        if _conn_key(k):
            given.append(v)
    free = srv.get("connectionless")
    free = CONNECTIONLESS_DEFAULT if not isinstance(free, list) else free
    if tool in free and tool not in (srv.get("query_tools") or []):
        return None
    if len(given) != 1:
        return "tool %s needs exactly one allowed connection argument (got %d)" % (tool, len(given))
    return None


def git_host_reason(provider, tool, inp):
    """A Git-host server's tool call: a classified PR / branch read of its provider, with a read action."""
    reads = GIT_HOST_READ.get(str(provider or ""))
    if not reads or tool not in reads:
        return "tool %s is not a classified %s read" % (tool, provider or "Git-host")
    acts = reads[tool]
    given = [k for k in ACTION_KEYS if k in inp]
    if acts is None:
        return "tool %s with an action argument" % tool if given else None
    if len(given) != 1 or str(inp[given[0]]) not in acts:
        return "tool %s with action %s (only %s)" % (tool, inp.get(given[0]) if given else "none",
                                                     ", ".join(sorted(acts)))
    return None


def _slack_cfg(cfg):
    return ((cfg or {}).get("jira_worklog") or {}).get("slack") or {}


def slack_send_reason(tool_name, tool_input, cfg):
    sl = _slack_cfg(cfg)
    if tool_name == "StructuredOutput":
        return None
    if not sl.get("send_tool") or tool_name != sl.get("send_tool"):
        return "only the configured Slack send tool"
    inp = tool_input if isinstance(tool_input, dict) else {}
    extra = set(inp) - SLACK_KEYS
    if extra:
        return "Slack send with %s (no threads, drafts, files or scheduling)" % ", ".join(sorted(extra))
    target = str(inp.get("channel_id") or inp.get("channel") or "")
    mine = {str(sl.get("self_user_id") or "")} | ({str(sl["self_dm_channel"])} if sl.get("self_dm_channel") else set())
    if not target or target not in mine - {""}:
        return "Slack send to %s, not my own user ID" % (target or "?")
    text = next((v for k, v in inp.items() if k in ("message", "text") and isinstance(v, str)), None)
    if text is None:
        return "Slack send without text"
    if len(text) > MAX_SLACK_CHARS:
        return "Slack message over %d characters" % MAX_SLACK_CHARS
    return None


def slack_discover_reason(tool_name, tool_input):
    if tool_name == "StructuredOutput":
        return None
    server, tool = split_tool(tool_name)
    if server and not writes(tool) and (tool.endswith("read_user_profile") or re.search(r"search_users|users_search|"
                                                                                         r"lookup_user|find_user", tool)):
        return None
    return "only reading my own Slack profile while setting up"


# --------------------------------------------------------------------------- session (/cg: commands)

_META = re.compile(r"[;&|`$<>\r\n]|\$\(")


def _own_helper(path):
    """True for Chitragupta's launcher: the stable runtime/run.py or a plugin's hooks/run.py next to the package."""
    p = os.path.normcase(os.path.realpath(path.strip('"\'')))
    if p == os.path.normcase(os.path.realpath(common.launcher())):
        return True
    root = os.path.dirname(os.path.dirname(p))
    return os.path.basename(p) == "run.py" and os.path.basename(os.path.dirname(p)) == "hooks" and \
        os.path.isfile(os.path.join(root, "chitragupta", "__init__.py"))


def helper_command(cmd):
    """True when a shell command line is exactly one call of Chitragupta's own helper, nothing chained."""
    c = (cmd or "").strip()
    if c.startswith("& "):  # PowerShell call operator
        c = c[2:].strip()
    if not c or _META.search(c):
        return False
    try:
        tok = shlex.split(c, posix=False)
    except ValueError:
        return False
    tok = [t.strip('"\'') for t in tok]
    if len(tok) >= 3 and tok[0].lower() in ("py", "py.exe") and tok[1] == "-3":
        tok = tok[2:]
    elif tok and os.path.basename(tok[0]).lower() in ("python", "python.exe", "pythonw", "pythonw.exe", "python3"):
        tok = tok[1:]
    else:
        return False
    return len(tok) >= 2 and _own_helper(tok[0])


def own_mcp_reason(tool, server_info=None):
    """A tool of Chitragupta's own MCP server: None for one of its read tools. When Claude Code says where the server
    came from (mcp_server.source), it must be a plugin server — a user or project server that borrows the name is not
    ours."""
    src = server_info.get("source") if isinstance(server_info, dict) else None
    if src is not None and src != "plugin":
        return "server %s from %s is not Chitragupta's own MCP server" % (OWN_MCP_SERVER, src)
    if tool not in OWN_MCP_TOOLS or writes(tool):
        return "tool %s is not one of Chitragupta's own read tools" % tool
    return None


def other_plugin(tool_name, server_info=None):
    """True for an MCP tool of another plugin: by the hook input's mcp_server.source when Claude Code sends it, else by
    the `mcp__plugin_<plugin>_<server>__` name prefix. Chitragupta's own server is never "other"."""
    server, _ = split_tool(str(tool_name or ""))
    if not server or server == OWN_MCP_SERVER:
        return False
    src = server_info.get("source") if isinstance(server_info, dict) else None
    if src is not None:
        return src == "plugin"
    return server.startswith("plugin_")


def session_reason(tool_name, tool_input, cfg, server_info=None):
    inp = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in SESSION_READ:
        return None
    if tool_name in ("Bash", "PowerShell"):
        return None if helper_command(str(inp.get("command") or "")) else \
            "during a /cg: command only Chitragupta's own helper may run"
    if tool_name == "Skill":
        s = str(inp.get("skill") or inp.get("name") or "")
        return None if s == "chitragupta" or s.startswith("cg:") else "only Chitragupta's skills"
    server, tool = split_tool(tool_name)
    if server == OWN_MCP_SERVER:
        return own_mcp_reason(tool, server_info)
    if other_plugin(tool_name, server_info):
        return OTHER_PLUGIN_REASON
    if server:
        if "slack" in server.lower():
            return "no Slack tools during a /cg: command"
        return mcp_reason(tool_name, inp)
    return "%s is not a read tool (/cg: commands only read)" % tool_name


# --------------------------------------------------------------------------- the decision

def reason(profile, tool_name, tool_input, cfg=None, server_info=None):
    """None = allowed. Unknown profiles and unknown tools are refused. `server_info`: the hook input's `mcp_server`
    ({name, source}) for an MCP tool, when Claude Code sends it."""
    tool_name = str(tool_name or "")
    if profile == "none":
        return None if tool_name == "StructuredOutput" else "no tools in this job (%s)" % tool_name
    if profile == "mcp":
        if tool_name == "StructuredOutput":
            return None
        why = mcp_reason(tool_name, tool_input)  # the ordinary guard first, always
        pin = common.db_pin_path()
        return why or (pin_reason(pin, tool_name, tool_input) if pin else None)
    cfg = common.load_config() if cfg is None else cfg
    if profile == "slack-send":
        return slack_send_reason(tool_name, tool_input, cfg)
    if profile == "slack-discover":
        return slack_discover_reason(tool_name, tool_input)
    if profile == "session":
        return session_reason(tool_name, tool_input, cfg, server_info)
    return "unknown guard profile %r" % profile


def describe(tool_name, tool_input):
    try:
        return "%s %s" % (tool_name, json.dumps(tool_input, ensure_ascii=False)[:500])
    except (TypeError, ValueError):
        return str(tool_name)
