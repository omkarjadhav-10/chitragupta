"""Query guard: one classifier for every database statement (database MCP jobs, the in-session guard, tests).

Allowed: exactly one SELECT / WITH … SELECT, catalog reads, EXPLAIN without ANALYZE, SHOW / DESCRIBE, and EXEC of a
read-only metadata procedure (sp_help …). Row reads carry TOP / LIMIT / FETCH FIRST <= max_rows (not needed for
catalog-only reads, aggregates, a table-free SELECT, or when the MCP tool's own limit caps the rows: require_limit=False).
Before any keyword check: Unicode NFKC, look-alike letters mapped to ASCII, zero-width characters dropped, comments
and string literals stripped, case folded. Anything non-ASCII left outside a literal is refused.
The guard doesn't know the database's dialect, so syntax the dialects read differently is refused rather than guessed:
MySQL / MariaDB executable comments (/*! */, /*M! */), a comment inside a comment, a backslash just before a closing
quote, dollar quoting, a quote or comment inside [brackets], and a `#` or a `--` without a following space when the
rest of that line holds a quote, bracket, ';' or comment start (a comment in one dialect, code in another). `--` +
space is a comment up to the next CR or LF; `#` is otherwise an ordinary character (T-SQL temp tables).
The catalog exemption from the row cap holds only when every FROM / JOIN / APPLY source is a catalog object.
MongoDB: find, aggregate without $out / $merge, count, distinct and list operations; no write operator or option.

statement_scope_reason(sql, engine, permitted): a second, separate check for connections with an approved scope
(mcp.database.scope). Every FROM / JOIN / APPLY source and every 3-part name anywhere must resolve to a permitted
database / schema of that connection: linked-server 4-part names, OPENQUERY / OPENROWSET / OPENDATASOURCE / dblink,
unqualified data objects (the server's default schema is not ours to assume), unknown objects when the caller lists
the known ones (a synonym could point anywhere) and engines without rules are refused. Identifier case follows the
engine: PostgreSQL folds unquoted names to lower case and keeps quoted ones; SQL Server compares case-insensitively
unless the connection is marked case-sensitive.
"""
import json
import re
import secrets
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
# Case-insensitive so statement_scope_reason can read a case-kept statement; sql_reason's input is already lower case.
_SOURCE_TOKEN = re.compile(r"\(|\)|,|[a-z_#@][a-z0-9_$#@]*(?:\s*\.\s*[a-z_#@][a-z0-9_$#@]*)*|\S", re.I)
# A table-free SELECT (SELECT 1, SELECT now()) needs no row limit, unless it calls a set-returning function.
_SET_RETURNING = re.compile(r"\b(?:generate_\w+|unnest|\w+_to_table|regexp_matches|json\w*_(?:elements|each|keys|recordset)"
                            r"\w*|\w+_series|openjson|string_split)\s*\(")
# COPY is a statement only at the head (it can't nest in a SELECT, and PostgreSQL needs ';' between statements, which
# is refused); T-SQL's COPY INTO is caught by INTO. So `copy` as a select-list column is a column.
_COLUMN_NAMED_LIKE_A_WORD = re.compile(r"((?:\bselect|\bdistinct|\ball|\btop\s*\(?\s*\d+\s*\)?|,)\s*)copy"
                                       r"(?=\s*(?:,|\bfrom\b|\bas\b|$))")
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


_UNDOUBLE = {'"': ('""', '"'), "`": ("``", "`"), "[": ("]]", "]")}


def _quoted_name(names, tag, opener, body):
    """Placeholder for a quoted identifier in names mode: ` <tag><n> `, its unescaped text kept in names[n]."""
    dbl, one = _UNDOUBLE[opener]
    names.append(body.replace(dbl, one))
    return " %s%d " % (tag, len(names) - 1)


def _lex(sql, names=None, tag="ident"):
    """-> (scrubbed, None) or (None, reason). One left-to-right pass: comments dropped, literals -> 's', quoted
    identifiers -> ident, case folded. Syntax the dialects read differently is refused (see the module doc).
    Names mode (`names` a list, used only by statement_scope_reason): each quoted identifier becomes `<tag><n>` with
    its unescaped text appended to `names`, and the case is kept."""
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
            elif names is None:
                out.append(" ident ")
            else:
                out.append(_quoted_name(names, tag, c, sql[i + 1:j]))
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
            out.append(" ident " if names is None else _quoted_name(names, tag, "[", sql[i + 1:j]))
            i = j + 1
            continue
        if c == "$" and not (out and _IDENT_CHAR.match(out[-1][-1:])) and _DOLLAR_TAG.match(sql, i):
            return None, "dollar-quoted string (dialects disagree)"
        out.append(c)
        i += 1
    return ("".join(out).lower() if names is None else "".join(out)), None


def _scrub(sql):
    """Lowercase, drop comments, replace literals and quoted identifiers by placeholders ("" when refused)."""
    return _lex(sql)[0] or ""


def _sources(s):
    """FROM / JOIN / APPLY sources of a scrubbed statement, at every depth: [(name, is_function_call)]. A derived
    table `( … )` is skipped here; its own FROM is found inside it. Comma lists (FROM a, b) count every item."""
    toks = [re.sub(r"\s+", "", t) for t in _SOURCE_TOKEN.findall(s)]
    out, listing, expect = [], [False], False
    for k, t in enumerate(toks):
        w = t.lower()  # keywords in any case; names are returned as written
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
            if w in ("only", "lateral"):
                continue
            expect = False
            if t != "(" and t != ",":
                out.append((t, toks[k + 1:k + 2] == ["("]))
            continue
        if w in ("from", "join", "apply"):
            listing[-1], expect = True, True
        elif t == "," and listing[-1]:
            expect = True
        elif w in _FROM_END:
            listing[-1] = False
    return out


def _no_table(s):
    """True for a SELECT that reads no table: no FROM anywhere and no set-returning function."""
    return not re.search(r"\bfrom\b", s) and not _SET_RETURNING.search(s)


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
    cols = _COLUMN_NAMED_LIKE_A_WORD.sub(r"\1column_ref", s)  # SELECT TOP 5 copy FROM t: a column, not COPY
    for w in DENY_WORDS:
        if re.search(r"(?<![\w.$#@])%s(?![\w$#@])" % w, cols):
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
    if first in ("select", "with") and not _catalog_only(s) and not AGG_ONLY.match(s) and not _no_table(s):
        nums = [int(x) for m in LIMIT.finditer(s) for x in m.groups() if x]
        if require_limit and not nums:
            return "row read without TOP/LIMIT"
        if nums and max(nums) > max_rows:  # even when the tool's own limit caps the rows
            return "row limit %d is over %d" % (max(nums), max_rows)
    return None


# --------------------------------------------------------------------------- statement scope (approved connections)

SCOPE_ENGINES = ("postgres", "mssql")
_FEDERATION = re.compile(r"\b(?:openquery|openrowset|opendatasource|openxml|dblink\w*)\b", re.I)
_NAME = re.compile(r"[A-Za-z_#@][A-Za-z0-9_$#@]*(?:\s*\.\s*[A-Za-z_#@][A-Za-z0-9_$#@]*)*")
_CTE = re.compile(r"([a-z_][a-z0-9_$#@]*)\s*(?:\([^()]*\))?\s+as\s*(?:not\s+)?(?:materialized\s*)?\(")
_CATALOG_SCHEMAS = frozenset({"pg_catalog", "information_schema", "sys"})


def _listed(permitted, key):
    v = (permitted or {}).get(key) if isinstance(permitted, dict) else None
    return [x for x in v if isinstance(x, str) and x] if isinstance(v, (list, tuple, set, frozenset)) else []


def scope_norm(engine, name, quoted, case_sensitive=False):
    """How `engine` compares an identifier: PostgreSQL folds an unquoted name to lower case and keeps a quoted one;
    SQL Server compares case-insensitively unless the connection is case-sensitive. Scope lists hold catalog names
    as stored, so they count as quoted."""
    if engine == "postgres":
        return name if quoted else name.lower()
    return name if case_sensitive else name.casefold()


class _Scope:
    def __init__(self, engine, permitted):
        self.eng = engine
        self.cs = bool((permitted or {}).get("case_sensitive"))
        self.dbs = {self.n(d, True) for d in _listed(permitted, "databases")}
        self.schemas = {self.n(s, True) for s in _listed(permitted, "schemas")}
        objs = (permitted or {}).get("objects")
        self.objects = None
        if isinstance(objs, (list, tuple, set, frozenset)):  # known objects: anything else could be a synonym
            self.objects = {".".join(self.n(p, True) for p in o.split(".", 1)) for o in objs if isinstance(o, str) and "." in o}

    def n(self, name, quoted):
        return scope_norm(self.eng, name, quoted, self.cs)

    def object(self, parts, label):
        """None when [(name, quoted)] (schema.object or db.schema.object) is inside the scope, else the reason."""
        joined = ".".join(p for p, _ in parts)
        if len(parts) >= 4:
            return "four-part (linked server) name %s" % joined
        if len(parts) == 1:
            return "unqualified %s %s: the server's default schema is not assumed" % (label, joined)
        if len(parts) == 3:
            if self.n(*parts[0]) not in self.dbs:
                return "database %s is outside the approved scope" % parts[0][0]
            parts = parts[1:]
        if not self.schemas:
            return "the approved scope lists no schemas for this connection"
        if self.n(*parts[0]) not in self.schemas:
            return "schema %s is outside the approved scope" % parts[0][0]
        if self.objects is not None and "%s.%s" % (self.n(*parts[0]), self.n(*parts[1])) not in self.objects:
            return "%s %s is not a known object of the approved scope (it could be a synonym)" % (label, joined)
        return None


def statement_scope_reason(sql, engine, permitted):
    """None when every object `sql` reads lies inside `permitted` ({databases, schemas, case_sensitive, objects?}) of
    one connection of engine `engine`, else the reason. Run after sql_reason (the read-only check); it does not
    replace it."""
    eng = str(engine or "").strip().lower()
    if eng not in SCOPE_ENGINES:
        return "no statement scope rules for engine %s (refused, not guessed)" % (eng or "unknown")
    if not isinstance(sql, str) or not sql.strip():
        return "empty statement"
    sql = normalise(sql)
    tag = "q%sz" % secrets.token_hex(6)  # unguessable placeholder for quoted identifiers
    names = []
    scrubbed, why = _lex(sql, names=names, tag=tag)
    if why:
        return why
    parts = [p for p in scrubbed.split(";") if p.strip()]
    if len(parts) != 1:
        return "more than one statement"
    s = parts[0]
    m = _FEDERATION.search(s)
    if m:
        return "federated or linked-server access (%s)" % m.group(0).upper()
    sc = _Scope(eng, permitted)
    ph = re.compile(r"^%s(\d+)$" % tag)

    def resolve(name):
        out = []
        for p in re.sub(r"\s+", "", name).split("."):
            hit = ph.match(p)
            if not hit:
                out.append((p, False))
                continue
            q = names[int(hit.group(1))]
            if not q.strip() or "." in q:
                return None
            out.append((q, True))
        return out
    ctes = set(_CTE.findall(s.lower()))
    for name, fn in _sources(s):
        ps = resolve(name)
        if ps is None:
            return "quoted identifier that is empty or contains a dot"
        joined = ".".join(p for p, _ in ps)
        if not any(q for _, q in ps) and CATALOG_SOURCE.fullmatch(joined.lower()):
            if len(ps) == 3 and sc.n(*ps[0]) not in sc.dbs:
                return "catalog of database %s, outside the approved scope" % ps[0][0]
            continue
        if len(ps) == 1 and not ps[0][1] and not fn and ps[0][0].lower() in ctes:
            continue
        why = sc.object(ps, "function" if fn else "object")
        if why:
            return why
    for m in _NAME.finditer(s):  # 3-part names and schema-qualified function calls anywhere (select list, WHERE …)
        raw = re.sub(r"\s+", "", m.group(0))
        if "." not in raw:
            continue
        ps = resolve(raw)
        if ps is None:
            return "quoted identifier that is empty or contains a dot"
        call = s[m.end():].lstrip()[:1] == "("
        catalog = (not ps[0][1]) and ps[0][0].lower() in _CATALOG_SCHEMAS
        if len(ps) >= 4:
            return "four-part (linked server) name %s" % ".".join(p for p, _ in ps)
        if len(ps) == 3:
            if sc.n(*ps[0]) in sc.dbs:
                if sc.n(*ps[1]) not in sc.schemas and not ((not ps[1][1]) and ps[1][0].lower() in _CATALOG_SCHEMAS):
                    return "schema %s is outside the approved scope" % ps[1][0]
            elif sc.n(*ps[0]) not in sc.schemas:
                return "cross-database reference %s" % ".".join(p for p, _ in ps)
        elif call and not catalog and sc.n(*ps[0]) not in sc.schemas:
            return "function in schema %s, outside the approved scope" % ps[0][0]
    return None


def name_scope_reason(value, engine, permitted, schema=None):
    """A tool's object argument (describe_table table=…, list_tables schema=…), not SQL: "object", "schema.object" or
    "db.schema.object", plus the separate schema argument when there is one. Exact names (no quoting syntax: a
    quote or bracket in the value is refused)."""
    eng = str(engine or "").strip().lower()
    if eng not in SCOPE_ENGINES:
        return "no scope rules for engine %s" % (eng or "unknown")
    sc = _Scope(eng, permitted)
    vals = [v for v in (schema, value) if v is not None]
    if any(not isinstance(v, str) or not v.strip() or re.search(r"[\"'`\[\];\x00-\x1f]", v) for v in vals):
        return "object argument with quoting or control characters"
    if value is None:
        return None if sc.n(schema, True) in sc.schemas else "schema %s is outside the approved scope" % schema
    ps = [(p, True) for p in value.split(".")]
    if schema is not None:
        if len(ps) > 1:
            return "object argument qualified twice"
        ps = [(schema, True)] + ps
    if len(ps) == 1:
        return "unqualified object argument %s: the server's default schema is not assumed" % value
    return sc.object(ps, "object")


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
