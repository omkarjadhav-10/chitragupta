"""Pure key normalisers for the knowledge records, so one API, table, query or file is one record.

    api_key     METHOD + path template (ids folded by apicalls.path_shape; '-' and '_' are one; the host is not part)
    table_key   lower-case, schema-qualified when the ticket or catalog knows exactly one schema for a bare name
    sql_key     SQL with whitespace and case collapsed outside literals, leading session SET statements and the
                trailing ';' removed
    file_key    repo-relative posix path

No I/O. Merging duplicates (keeping every source's data and the old keys as aliases) is in store.py.
"""
import re

from .apicalls import path_shape

_NOTE = re.compile(r"\s*\([^()]*\)\s*$")  # "Order.java (shop-api, full path not shown)", "/x (v2)"


def api_key(method, path):
    m = (method or "").upper().strip() or "ANY"
    p = _NOTE.sub("", (path or "").strip())
    shape = path_shape(p).replace("_", "-")
    return "%s %s" % (m, shape or p.lower())


def table_key(name, known=()):
    n = re.sub(r"[\[\]\"`\s]", "", (name or "").strip().lower())
    if n and "." not in n:
        hits = {k for k in known or () if k.endswith("." + n)}
        if len(hits) == 1:
            return hits.pop()
    return n


_LIT = re.compile(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")")
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_PUNCT = re.compile(r"\s*([=<>!(),;])\s*")
_SET = re.compile(r"^set\s[^;]*;\s*")


def sql_key(text):
    parts = _LIT.split((text or "").strip())
    out = []
    for i, p in enumerate(parts):
        if i % 2:  # a quoted literal or identifier: exactly as written
            out.append(p)
            continue
        p = _COMMENT.sub(" ", p).lower()
        p = re.sub(r"\s+", " ", p)
        out.append(_PUNCT.sub(r"\1", p))
    s = "".join(out).strip()
    while _SET.match(s):
        s = _SET.sub("", s, count=1)
    return s.rstrip("; ").strip()


def file_key(path, repo_root=None, repo_names=()):
    p = _NOTE.sub("", (path or "").strip().strip("`\"'"))
    p = re.sub(r":\d+$", "", p).replace("\\", "/")
    p = re.sub(r"/{2,}", "/", p)
    if repo_root:
        r = str(repo_root).replace("\\", "/").rstrip("/")
        if r and p.lower().startswith(r.lower() + "/"):
            return p[len(r) + 1:]
    if re.match(r"^([A-Za-z]:)?/", p):
        segs = p.split("/")
        low = [s.lower() for s in segs]
        for name in repo_names or ():
            at = [i for i in range(len(low) - 1) if name and low[i] == name.lower()]
            if at:
                return "/".join(segs[at[-1] + 1:])
    return re.sub(r"^\./", "", p)



def edited_files(events):
    """Paths written by Edit/Write tool events: with a commit, the only proof that a file was changed."""
    out = []
    for e in events or []:
        p = e.get("payload") or {}
        ki = p.get("input") or {}
        if e.get("event") == "post-tool" and p.get("tool") in ("Edit", "Write", "MultiEdit", "NotebookEdit") \
                and ki.get("file_path") and ki["file_path"] not in out:
            out.append(ki["file_path"])
    return out
