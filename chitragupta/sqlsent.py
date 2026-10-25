"""K6b: which SQL was actually sent. Pure functions, no I/O.

A query the model reports is matched to the statements of the same session that really ran: the `sql` / `query`
argument of a database MCP call (hook.py keeps an MCP call's arguments as JSON), or a SQL command line (psql,
sqlcmd …) in a Bash / PowerShell call. Both sides are compared as K7's normalised SQL (kbnorm.sql_key); the model's
`:name` and `?` placeholders match any value. An MCP argument the hook cut at its size limit matches by prefix.
"""
import json
import re

from . import common, kbnorm

_PH = re.compile(r"(?<![:\w]):[A-Za-z_]\w*|\?")
_ARG = re.compile(r'"(?:sql|query)"\s*:\s*"((?:[^"\\]|\\.)*)', re.S)
_CUT = " …[truncated]"
_SQLISH = re.compile(r"(?i)\b(select|with|insert|update|delete|merge|exec|explain|show|describe)\b")
MIN_LITERAL = 12  # a pattern needs this much fixed text to match inside a command line


def _walk(obj):
    if isinstance(obj, dict):
        for k in ("sql", "query"):
            if isinstance(obj.get(k), str) and obj[k].strip():
                return obj[k]
        for v in obj.values():
            hit = _walk(v)
            if hit:
                return hit
    return None


def mcp_sql(arguments):
    """(sql, truncated) from a database MCP call's recorded arguments, or (None, False)."""
    s = arguments if isinstance(arguments, str) else json.dumps(arguments or {})
    try:
        hit = _walk(json.loads(s))
        return (hit, False) if hit else (None, False)
    except ValueError:
        pass
    m = _ARG.search(s or "")
    if not m:
        return None, False
    raw = m.group(1)
    closed = s[m.end():m.end() + 1] == '"'
    try:
        txt = json.loads('"' + raw + '"')
    except ValueError:
        txt = raw.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    if txt.endswith(_CUT):
        txt = txt[:-len(_CUT)]
    return (txt, not closed) if txt.strip() else (None, False)


def loose(text):
    """Lower-case, comments and spacing collapsed, quotes kept: for SQL inside a shell command."""
    p = re.sub(r"--[^\n]*|/\*.*?\*/", " ", text or "", flags=re.S).lower()
    p = re.sub(r"\s+", " ", p)
    return re.sub(r"\s*([=<>!(),;])\s*", r"\1", p).strip().rstrip("; ")


def sent_statements(events):
    """[{key, loose, cli, truncated, ref}] for the SQL the events sent (post-tool events only)."""
    out = []
    for e in events or []:
        if e.get("event") != "post-tool":
            continue
        p = e.get("payload") or {}
        ki = p.get("input") or {}
        tool = p.get("tool") or ""
        if tool.startswith("mcp__") and ki.get("arguments"):
            sql, cut = mcp_sql(ki["arguments"])
            if sql:
                out.append({"key": kbnorm.sql_key(sql), "loose": loose(sql), "cli": False, "truncated": cut,
                            "ref": e.get("_ref"), "time": common.hhmm(e.get("ts")), "line": e.get("_ln")})
        elif ki.get("command") and _SQLISH.search(ki["command"]):
            out.append({"key": "", "loose": loose(ki["command"]), "cli": True, "truncated": False, "ref": e.get("_ref"), "time": common.hhmm(e.get("ts")), "line": e.get("_ln")})
    return out


def _pattern(key):
    parts = _PH.split(key)
    return ".+?".join(re.escape(x) for x in parts), parts


def matches(text, s):
    """True when the model's query `text` is the statement `s` (placeholders as wildcards)."""
    key = kbnorm.sql_key(text)
    if not key:
        return False
    if s["cli"]:
        pat, parts = _pattern(loose(key))
        return len("".join(parts)) >= MIN_LITERAL and re.search(pat, s["loose"], re.S) is not None
    pat, parts = _pattern(key)
    if re.fullmatch(pat, s["key"], re.S):
        return True
    if s["truncated"]:  # the hook kept only the start: compare the fixed text before the first placeholder
        head = s["key"][:-20] if len(s["key"]) > 40 else s["key"]
        lead = parts[0]
        if len(parts) == 1:
            return lead.startswith(head)
        return len(lead) >= 40 and (head.startswith(lead) or lead.startswith(head))
    return False


def find(text, sent, variants=(), line=None):
    """The first sent statement the query (or one of its other written forms) matches, else None."""
    candidates = list(sent or ())
    if type(line) is int:
        candidates.sort(key=lambda s: s.get("line") != line)
    for t in [text] + [v for v in variants or () if v]:
        for s in candidates:
            if matches(t, s):
                return s
    return None
