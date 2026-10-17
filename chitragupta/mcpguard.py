"""Read-only guard for the worklog's MCP jobs (PreToolUse hook in .worklog/mcp/readonly-settings.json, run as
`"<python>" "<launcher>" mcpguard`, i.e. `python -m chitragupta mcpguard`).

Denies every tool that is not on the read allowlist (.worklog/mcp/allow.json) and every database statement that
is not a single read: SELECT / WITH ... SELECT / EXPLAIN / SHOW / DESCRIBE, catalog reads and read-only metadata
procs (sp_help, sp_helptext, ...), Mongo find / count / listCollections / listIndexes / aggregate without
$out / $merge. Row reads must carry TOP / LIMIT / FETCH FIRST <= max_rows and run on an allowed connection.

Usable as a library too: check(tool_name, tool_input, allow) -> None (allowed) or a reason string.
"""
import json
import re
import sys
import time

from . import common

ALLOW_PATH = common.env("GUARD_ALLOW") or common.wl("mcp", "allow.json")

READ_START = {"select", "with", "explain", "show", "describe", "desc"}
META_PROCS = {"sp_help", "sp_helptext", "sp_columns", "sp_pkeys", "sp_fkeys", "sp_helpindex", "sp_helpconstraint",
              "sp_helptrigger", "sp_tables", "sp_depends", "sp_stored_procedures", "sp_sproc_columns"}
# anywhere in the statement (as a whole word, outside literals and quoted identifiers) -> denied
DENY_WORDS = ("insert", "update", "delete", "merge", "drop", "alter", "create", "truncate", "grant", "revoke", "deny",
              "exec", "execute", "into", "upsert", "openrowset", "opendatasource", "openquery", "openxml", "dbcc", "bulk",
              "waitfor", "declare", "begin", "commit", "rollback", "savepoint", "kill", "shutdown", "backup", "restore",
              "vacuum", "reindex", "attach", "detach", "pragma", "deallocate")
DENY_FUNCS = ("nextval", "setval", "pg_sleep", "pg_terminate_backend", "pg_cancel_backend", "pg_advisory", "dblink",
              "lo_import", "lo_export", "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_reload_conf",
              "set_config", "txid_current", "pg_switch_wal", "pg_create", "pg_drop", "pg_logical", "pg_stat_reset",
              "xp_", "sp_configure", "sp_executesql", "sp_oa", "fn_get_audit_file", "query_to_xml", "pg_notify")
LOCK_HINT = re.compile(r"\bwith\s*\(\s*(?:nolock|readuncommitted|updlock|xlock|holdlock|rowlock|paglock|tablock|tablockx|"
                       r"serializable|readpast|repeatableread|readcommitted|nowait)\b")
CATALOG = re.compile(r"\b(?:information_schema|pg_catalog|sys)\s*\.|\bpg_(?:class|attribute|index|constraint|proc|trigger|"
                     r"namespace|views|tables|indexes|description|get_\w+)\b|\bsqlite_master\b")
LIMIT = re.compile(r"\btop\s*\(?\s*(\d+)|\blimit\s+(\d+)|\bfetch\s+(?:first|next)\s+(\d+)\s+rows?\b")
AGG_ONLY = re.compile(r"^\s*select\s+(?:count|sum|min|max|avg|count_big)\s*\(")
MONGO_OK = {"find", "count", "countdocuments", "estimateddocumentcount", "listcollections", "listindexes", "aggregate",
            "distinct", "getindexes"}
SQL_KEYS = ("query", "sql", "statement", "text", "command", "q")


def load_allow(path=ALLOW_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def split_tool(name):
    if not name.startswith("mcp__"):
        return None, name
    rest = name[5:]
    i = rest.find("__")
    return (rest[:i], rest[i + 2:]) if i > 0 else (None, rest)


# --------------------------------------------------------------------------- SQL

def _scrub(sql):
    """Lowercase, drop comments, replace literals and quoted identifiers by placeholders."""
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s)
    s = re.sub(r"(?s)\$([A-Za-z_]*)\$.*?\$\1\$", " 's' ", s)  # postgres dollar quoting
    s = re.sub(r"N?'(?:[^']|'')*'", " 's' ", s)
    s = re.sub(r"\"(?:[^\"]|\"\")*\"|\[[^\]]*\]|`[^`]*`", " ident ", s)
    return s.lower()


def sql_check(sql, max_rows=20, require_limit=True):
    """None when the statement is a single read, else the reason it is denied."""
    if not isinstance(sql, str) or not sql.strip():
        return "empty statement"
    if "'" in re.sub(r"N?'(?:[^']|'')*'", "", re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.S)):
        return "unbalanced quotes"
    s = _scrub(sql).strip()
    parts = [p for p in s.split(";") if p.strip()]
    if len(parts) != 1:
        return "more than one statement"
    s = parts[0].strip()
    words = re.findall(r"[a-z_][a-z0-9_$#@]*", s)
    if not words:
        return "no statement"
    first = words[0]
    if first in ("exec", "execute"):
        m = re.match(r"exec(?:ute)?\s+(?:ident\s*\.\s*|\w+\s*\.\s*)*(\w+)", s)
        proc = m.group(1) if m else ""
        if proc not in META_PROCS:
            return "EXEC of a procedure that is not a read-only metadata proc (%s)" % (proc or "?")
        rest = s[m.end():]
        if re.search(r"\b(?:%s)\b" % "|".join(w for w in DENY_WORDS if w not in ("exec", "execute")), rest):
            return "write keyword in EXEC arguments"
        return None
    if first not in READ_START:
        return "statement starts with %s" % first.upper()
    for w in DENY_WORDS:
        if re.search(r"(?<![\w.$#@])%s(?![\w$#@])" % w, s):
            return "write or control keyword %s" % w.upper()
    if re.search(r"(?m)^\s*go\s*$", _scrub(sql)) or re.search(r"\block\s+(?:table|in)\b", s):
        return "batch separator or LOCK"
    for f in DENY_FUNCS:
        if re.search(r"(?<![\w.])%s\w*\s*\(" % re.escape(f), s) or (f.endswith("_") and re.search(r"\b%s\w+" % f, s)):
            return "function with side effects (%s)" % f
    if LOCK_HINT.search(s) or re.search(r"\bfor\s+(?:update|share|no\s+key\s+update|key\s+share)\b", s):
        return "locking hint"
    # a second top-level SELECT without UNION/INTERSECT/EXCEPT = a second statement (T-SQL needs no ';')
    depth, prev = 0, ""
    seen_select = False
    for tok in re.findall(r"\(|\)|[a-z_][a-z0-9_]*", s):
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and tok in ("select", "with", "explain", "show", "describe"):
            if seen_select and tok == "select" and prev not in ("union", "intersect", "except", "all", "distinct", "as"):
                return "more than one statement"
            if tok == "select":
                seen_select = True
        prev = tok if tok not in ("(", ")") else prev
    if depth != 0:
        return "unbalanced parentheses"
    if require_limit and first in ("select", "with") and not CATALOG.search(s) and not AGG_ONLY.match(s):
        nums = [int(x) for m in LIMIT.finditer(s) for x in m.groups() if x]
        if not nums:
            return "row read without TOP/LIMIT"
        if max(nums) > max_rows:
            return "row limit %d is over %d" % (max(nums), max_rows)
    return None


def mongo_check(inp, max_rows=20):
    text = json.dumps(inp, ensure_ascii=False)
    op = ""
    for k in ("operation", "op", "method", "command"):
        if isinstance(inp.get(k), str):
            op = inp[k]
            break
    m = re.match(r"\s*db(?:\.\w+|\[['\"][^'\"]+['\"]\])*\.(\w+)\s*\(", next((v for v in inp.values() if isinstance(v, str) and
                                                                           v.strip().startswith("db.")), "") or "")
    if m:
        op = m.group(1)
    op = op.lower()
    if op not in MONGO_OK:
        return "Mongo operation %s is not a read" % (op or "?")
    if re.search(r"\$(?:out|merge)\b", text):
        return "aggregate with $out/$merge"
    if re.search(r"\$(?:function|accumulator|where)\b", text):
        return "server-side JavaScript"
    return None


# --------------------------------------------------------------------------- the decision

def check(tool_name, tool_input, allow=None):
    allow = load_allow() if allow is None else allow
    server, tool = split_tool(tool_name)
    if not server:
        if tool_name in ("StructuredOutput",):  # the --json-schema answer itself
            return None
        return "built-in tool %s is not allowed in a read-only job" % tool_name
    srv = (allow.get("servers") or {}).get(server)
    if not srv:
        return "server %s is not on the allowlist" % server
    if tool in (srv.get("write") or []) or tool not in (srv.get("read") or []):
        return "tool %s is not on the read allowlist" % tool
    inp = tool_input if isinstance(tool_input, dict) else {}
    max_rows = int(allow.get("max_rows") or 20)
    conns = srv.get("connections")
    if conns is not None:
        given = [v for k, v in inp.items() if "connection" in k.lower() or k.lower() in ("conn", "datasource", "database_id")]
        allowed = {str(c) for c in conns.values() if c} if isinstance(conns, dict) else {str(c) for c in conns if c}
        for g in given:
            if str(g) not in allowed:
                return "connection %s is not an allowed (QA/dev) connection" % g
    if tool in (srv.get("query_tools") or []):
        lim = inp.get("limit")
        if lim is not None and (not isinstance(lim, int) or lim > max_rows):
            return "limit %s is over %d" % (lim, max_rows)
        if any(k in inp for k in ("pipeline", "collection", "filter")) or any(
                isinstance(v, str) and v.strip().startswith("db.") for v in inp.values()):
            return mongo_check(inp, max_rows)
        sql = next((inp[k] for k in SQL_KEYS if isinstance(inp.get(k), str)), None)
        if sql is None:
            return "query tool called without a recognisable statement field"
        return sql_check(sql, max_rows)
    return None


def _log(msg):
    """<home>/.worklog/logs/mcp-guard.log (never the package folder)."""
    try:
        common.append_line(common.wl("logs", "mcp-guard.log"), "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except OSError:
        pass


def main():
    try:
        data = json.loads((sys.stdin.buffer.read() or b"{}").decode("utf-8-sig"))
    except Exception:
        data = {}
    name = str(data.get("tool_name") or "")
    reason = check(name, data.get("tool_input") or {})
    if reason:
        _log("DENY %s: %s" % (name, reason))
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                            "permissionDecisionReason": "worklog read-only guard: " + reason}}))
        return 0
    _log("allow %s" % name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
