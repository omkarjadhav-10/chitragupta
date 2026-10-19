"""Query guard: one classifier for every database statement (database MCP jobs, the in-session guard, tests).

Allowed: exactly one SELECT / WITH … SELECT, catalog reads, EXPLAIN without ANALYZE, SHOW / DESCRIBE, and EXEC of a
read-only metadata procedure (sp_help …). Row reads carry TOP / LIMIT / FETCH FIRST <= max_rows.
Before any keyword check: Unicode NFKC, look-alike letters mapped to ASCII, zero-width characters dropped, comments
and string literals stripped, case folded. Anything non-ASCII left outside a literal is refused.
The guard doesn't know the database's dialect, so syntax the dialects read differently is refused rather than guessed:
MySQL / MariaDB executable comments (/*! */, /*M! */), a comment inside a comment, a backslash just before a closing
quote, dollar quoting, a quote or comment inside [brackets], and a `#` or a `--` without a following space when the
rest of that line holds a quote, bracket, ';' or comment start (a comment in one dialect, code in another). `--` +
space is a comment up to the next CR or LF; `#` is otherwise an ordinary character (T-SQL temp tables).
The catalog exemption from the row cap holds only when every FROM / JOIN / APPLY source is a catalog object.
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
LIMIT = re.compile(r"\btop\s*\(?\s*(\d+)|\blimit\s+(\d+)|\bfetch\s+(?:first|next)\s+(\d+)\s+rows?\b")
AGG_ONLY = re.compile(r"^\s*select\s+(?:count|sum|min|max|avg|count_big)\s*\(")
CATALOG_SOURCE = re.compile(r"(?:[\w$#@]+\.)?(?:information_schema|pg_catalog|sys)\.[\w$#@]+|(?:pg_catalog\.)?(?:pg_(?:"
                            r"class|attribute|index|constraint|proc|trigger|namespace|views|tables|indexes|description|"
                            r"get_\w+)|sqlite_master)")
_SOURCE_TOKEN = re.compile(r"\(|\)|,|[a-z_#@][a-z0-9_$#@]*(?:\s*\.\s*[a-z_#@][a-z0-9_$#@]*)*|\S")
_FROM_END = {"where", "group", "having", "order", "limit", "offset", "fetch", "union", "intersect", "except", "window",
             "qualify", "for", "select", "returning", "into"}

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


_WS = " \t\r\n\f\v"
_AMBIGUOUS_TAIL = re.compile(r"['\"`\[;]|/\*")  # what would change how the rest of the text is read
_IDENT_CHAR = re.compile(r"[\w$#@]")
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


def _eol(sql, i):
    """Index of the CR or LF that ends the line at i (either one ends a `--` comment in some dialect)."""
    ends = [j for j in (sql.find("\n", i), sql.find("\r", i)) if j >= 0]
    return min(ends) if ends else len(sql)


def _odd_backslashes_before(text, j):
    k = j
    while k > 0 and text[k - 1] == "\\":
        k -= 1
    return (j - k) % 2 == 1


def _lex(sql):
    """-> (scrubbed, None) or (None, reason). One left-to-right pass: comments dropped, literals -> 's', quoted
    identifiers -> ident, case folded. Syntax the dialects read differently is refused (see the module doc)."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c, nxt = sql[i], sql[i + 1:i + 2]
        if c == "-" and nxt == "-":
            if i + 2 >= n or sql[i + 2] in _WS:
                i = _eol(sql, i)  # the line break itself stays (batch separator check)
                continue
            if _AMBIGUOUS_TAIL.search(sql, i + 2, _eol(sql, i)):
                return None, "'--' without a following space (a comment in some dialects, code in MySQL)"
            out.append("--")
            i += 2
            continue
        if c == "#":
            if _AMBIGUOUS_TAIL.search(sql, i + 1, _eol(sql, i)):
                return None, "'#' before a quote, bracket or ';' on the same line (a comment in MySQL)"
            out.append("#")
            i += 1
            continue
        if c == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            if j < 0:
                return None, "unterminated comment"
            body = sql[i + 2:j]
            if body.startswith("!") or re.match(r"[Mm]!", body):
                return None, "MySQL executable comment /*!"
            if "/*" in body:
                return None, "comment inside a comment (dialects disagree where it ends)"
            out.append(" ")
            i = j + 2
            continue
        if c in "'\"`":
            j = i + 1
            while True:
                j = sql.find(c, j)
                if j < 0:
                    return None, "unbalanced quotes"
                if c != "`" and _odd_backslashes_before(sql, j):
                    return None, "backslash before a quote (dialects disagree where the literal ends)"
                if sql[j + 1:j + 2] == c:  # doubled quote = an escaped one
                    j += 2
                    continue
                break
            if c == "'":
                if out and out[-1] in tuple("NnEeBbXx") and (len(out) < 2 or not _IDENT_CHAR.match(out[-2][-1:])):
                    out.pop()  # the N'..' / E'..' / B'..' / X'..' prefix
                out.append(" 's' ")
            else:
                out.append(" ident ")
            i = j + 1
            continue
        if c == "[":
            j = sql.find("]", i + 1)
            while j >= 0 and sql[j + 1:j + 2] == "]":  # ]] = an escaped ]
                j = sql.find("]", j + 2)
            if j < 0:
                return None, "unbalanced brackets"
            if re.search(r"['\"`;]|--|/\*", sql[i + 1:j]):
                return None, "quote or comment inside [brackets] (dialects disagree)"
            out.append(" ident ")
            i = j + 1
            continue
        if c == "$" and not (out and _IDENT_CHAR.match(out[-1][-1:])) and _DOLLAR_TAG.match(sql, i):
            return None, "dollar-quoted string (dialects disagree)"
        out.append(c)
        i += 1
    return "".join(out).lower(), None


def _scrub(sql):
    """Lowercase, drop comments, replace literals and quoted identifiers by placeholders ("" when refused)."""
    return _lex(sql)[0] or ""


def _sources(s):
    """FROM / JOIN / APPLY sources of a scrubbed statement, at every depth: [(name, is_function_call)]. A derived
    table `( … )` is skipped here; its own FROM is found inside it. Comma lists (FROM a, b) count every item."""
    toks = [re.sub(r"\s+", "", t) for t in _SOURCE_TOKEN.findall(s)]
    out, listing, expect = [], [False], False
    for k, t in enumerate(toks):
        if t == "(":
            listing.append(False)
            expect = False
            continue
        if t == ")":
            if len(listing) > 1:
                listing.pop()
            expect = False
            continue
        if expect:
            if t in ("only", "lateral"):
                continue
            expect = False
            if t != "(" and t != ",":
                out.append((t, toks[k + 1:k + 2] == ["("]))
            continue
        if t in ("from", "join", "apply"):
            listing[-1], expect = True, True
        elif t == "," and listing[-1]:
            expect = True
        elif t in _FROM_END:
            listing[-1] = False
    return out


def _catalog_only(s):
    """True when the statement reads catalog objects only: every source is a catalog object or a CTE name."""
    ctes = set(re.findall(r"([a-z_][a-z0-9_$#@]*)\s*(?:\([^()]*\))?\s+as\s*(?:not\s+)?(?:materialized\s*)?\(", s)) - {"ident"}
    src = _sources(s)
    cat = [n for n, _ in src if CATALOG_SOURCE.fullmatch(n)]
    return bool(cat) and all(CATALOG_SOURCE.fullmatch(n) or (n in ctes and not fn) for n, fn in src)


def sql_reason(sql, max_rows=20, require_limit=True):
    """None when the statement is a single read, else the reason it is denied."""
    if not isinstance(sql, str) or not sql.strip():
        return "empty statement"
    sql = normalise(sql)
    scrubbed, why = _lex(sql)
    if why:
        return why
    s = scrubbed.strip()
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
    if re.search(r"(?m)^\s*go\s*$", scrubbed):
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
    if require_limit and first in ("select", "with") and not _catalog_only(s) and not AGG_ONLY.match(s):
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
