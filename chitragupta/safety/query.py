"""Query guard: one classifier for every database statement (database MCP jobs, the in-session guard, tests).

Allowed: exactly one SELECT / WITH … SELECT, catalog reads, EXPLAIN without ANALYZE, SHOW / DESCRIBE, and EXEC of a
read-only metadata procedure (sp_help …). Row reads carry TOP / LIMIT / FETCH FIRST <= max_rows.
Before any keyword check: Unicode NFKC, look-alike letters mapped to ASCII, zero-width characters dropped, comments
and string literals stripped, case folded. Anything non-ASCII left outside a literal is refused.
MongoDB: find, aggregate without $out / $merge, count, distinct and list operations; no write operator or option.
"""
import json
import re
import unicodedata

READ_START = {"select", "with", "explain", "show", "describe", "desc"}
META_PROCS = {"sp_help", "sp_helptext", "sp_columns", "sp_pkeys", "sp_fkeys", "sp_helpindex", "sp_helpconstraint",
              "sp_helptrigger", "sp_tables", "sp_depends", "sp_stored_procedures", "sp_sproc_columns"}
# anywhere in the statement (as a whole word, outside literals and quoted identifiers) -> denied
DENY_WORDS = ("insert", "update", "delete", "merge", "drop", "alter", "create", "truncate", "grant", "revoke", "deny",
              "exec", "execute", "into", "upsert", "openrowset", "opendatasource", "openquery", "openxml",
              "dbcc", "bulk", "waitfor", "declare", "begin", "commit", "rollback", "savepoint", "kill", "shutdown",
              "backup", "restore", "vacuum", "reindex", "attach", "detach", "pragma", "deallocate", "call", "copy",
              "listen", "notify", "lock", "set", "use", "handler", "prepare", "do", "cluster", "reconfigure",
              "disable", "enable", "analyze", "analyse")
DENY_FUNCS = ("nextval", "setval", "pg_sleep", "pg_terminate_backend", "pg_cancel_backend", "pg_advisory",
              "pg_try_advisory", "dblink", "lo_import", "lo_export", "lo_unlink", "lo_create", "lo_creat", "lo_put",
              "lo_from_bytea", "lo_truncate", "lo_open", "lo_write", "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
              "pg_reload_conf", "pg_rotate_logfile", "pg_promote", "pg_file_write", "set_config", "txid_current",
              "pg_switch_wal", "pg_create", "pg_drop", "pg_logical", "pg_stat_reset", "pg_notify", "query_to_xml",
              "xp_", "sp_configure", "sp_executesql", "sp_oa", "fn_get_audit_file", "sleep", "benchmark", "load_file",
              "get_lock", "release_lock", "sys_exec", "sys_eval")
LOCK_HINT = re.compile(r"\bwith\s*\(\s*(?:nolock|readuncommitted|updlock|xlock|holdlock|rowlock|paglock|tablock|tablockx|"
                       r"serializable|readpast|repeatableread|readcommitted|nowait)\b")
CATALOG = re.compile(r"\b(?:information_schema|pg_catalog|sys)\s*\.|\bpg_(?:class|attribute|index|constraint|proc|trigger|"
                     r"namespace|views|tables|indexes|description|get_\w+)\b|\bsqlite_master\b")
LIMIT = re.compile(r"\btop\s*\(?\s*(\d+)|\blimit\s+(\d+)|\bfetch\s+(?:first|next)\s+(\d+)\s+rows?\b")
AGG_ONLY = re.compile(r"^\s*select\s+(?:count|sum|min|max|avg|count_big)\s*\(")

# Cyrillic / Greek / other letters that look like Latin ones (NFKC leaves them alone)
_CONFUSABLE = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y",
    "х": "x", "ѕ": "s", "і": "i", "ј": "j", "ԁ": "d", "ɡ": "g", "һ": "h", "ӏ": "l", "ո": "n", "ս": "u", "ԝ": "w",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
    "Ѕ": "S", "І": "I", "Ј": "J", "α": "a", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "ν": "v", "ε": "e", "ι": "i",
    "κ": "k", "χ": "x", "Α": "A", "Β": "B", "Ε": "E", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O",
    "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X", "Ζ": "Z", "ı": "i", "ſ": "s",
})
_ZERO_WIDTH = re.compile("[­᠎​-‏‪-‮⁠-⁤﻿]")


def normalise(text):
    t = unicodedata.normalize("NFKC", text or "")
    return _ZERO_WIDTH.sub("", t).translate(_CONFUSABLE)


def _scrub(sql):
    """Lowercase, drop comments, replace literals and quoted identifiers by placeholders."""
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    s = re.sub(r"--[^\n]*|#[^\n]*", " ", s)
    s = re.sub(r"(?s)\$([A-Za-z_]*)\$.*?\$\1\$", " 's' ", s)  # postgres dollar quoting
    s = re.sub(r"[NnEeBbXx]?'(?:[^'\\]|''|\\.)*'", " 's' ", s)
    s = re.sub(r"\"(?:[^\"]|\"\")*\"|\[[^\]]*\]|`[^`]*`", " ident ", s)
    return s.lower()


def sql_reason(sql, max_rows=20, require_limit=True):
    """None when the statement is a single read, else the reason it is denied."""
    if not isinstance(sql, str) or not sql.strip():
        return "empty statement"
    sql = normalise(sql)
    if "/*" in re.sub(r"/\*.*?\*/", "", sql, flags=re.S):
        return "unterminated comment"
    if "'" in re.sub(r"[NnEeBbXx]?'(?:[^'\\]|''|\\.)*'", "", re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.S)):
        return "unbalanced quotes"
    s = _scrub(sql).strip()
    if re.search(r"[^\x00-\x7f]", s):
        return "non-ASCII text outside string literals"
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
    if first == "explain" and re.search(r"\banaly[sz]e\b", s):
        return "EXPLAIN ANALYZE runs the statement"
    for w in DENY_WORDS:
        if re.search(r"(?<![\w.$#@])%s(?![\w$#@])" % w, s):
            return "write or control keyword %s" % w.upper()
    if re.search(r"(?m)^\s*go\s*$", _scrub(sql)):
        return "batch separator"
    for f in DENY_FUNCS:
        if re.search(r"(?<![\w.])%s\w*\s*\(" % re.escape(f), s) or (f.endswith("_") and re.search(r"\b%s\w+" % f, s)):
            return "function with side effects (%s)" % f
    if LOCK_HINT.search(s) or re.search(r"\bfor\s+(?:update|share|no\s+key\s+update|key\s+share)\b", s):
        return "locking hint"
    # a second top-level SELECT without UNION/INTERSECT/EXCEPT = a second statement (T-SQL needs no ';')
    depth, prev, seen_select = 0, "", False
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


# --------------------------------------------------------------------------- MongoDB

MONGO_OK = {"find", "count", "countdocuments", "estimateddocumentcount", "listcollections", "listindexes", "aggregate",
            "distinct", "getindexes", "listdatabases"}
MONGO_WRITE_KEYS = re.compile(r"\$(?:out|merge|function|accumulator|where|set|unset|inc|push|pull|pullall|addtoset|"
                              r"rename|currentdate|setoninsert|pop|bit|mul)\b|\"(?:upsert|writeconcern|bypassdocumentvalidation|"
                              r"new|returndocument|update|replacement|documents|deletes|updates)\"\s*:", re.I)


def mongo_reason(inp):
    text = normalise(json.dumps(inp, ensure_ascii=False))
    op = ""
    for k in ("operation", "op", "method", "command"):
        if isinstance(inp.get(k), str):
            op = inp[k]
            break
    shell = next((v for v in inp.values() if isinstance(v, str) and v.strip().startswith("db.")), "")
    if shell:
        calls = re.findall(r"\.(\w+)\s*\(", normalise(shell))
        bad = [c for c in calls if c.lower() not in MONGO_OK | {"limit", "sort", "skip", "project", "toarray",
                                                                "pretty", "count", "projection", "hint", "explain"}]
        if bad:
            return "Mongo operation %s is not a read" % bad[0]
        op = calls[0] if calls else op
    op = normalise(op).lower()
    if op not in MONGO_OK:
        return "Mongo operation %s is not a read" % (op or "?")
    m = MONGO_WRITE_KEYS.search(text)
    if m:
        return "Mongo write operator or option %s" % m.group(0).strip('":')
    return None
