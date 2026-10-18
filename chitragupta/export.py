"""One ticket as one self-contained HTML file: the dashboard's ticket page with the ticket's data embedded.

    export_ticket(key, mode)              -> (filename, html) or None for an unknown ticket; nothing is written
    export_to_file(key, mode, out_dir)    -> path of <home>/tickets/<KEY>/exports/<KEY>-<mode>-<YYYYMMDD-HHMM>.html

The file is the dashboard page (app/index.html, its CSS and ES modules inlined) plus a JSON snapshot of /api/tickets/<KEY> in <script id="cg-export">; the page
sees it, opens straight on the ticket and never calls the API. The vendored Mermaid is inlined only when the ticket has
a diagram (the branch graph or a DB-flow mermaid block). No external requests either way.

Modes
  full       everything the dashboard shows, as stored (local use: it carries PR links, paths and QA examples)
  team       for your team: secrets, patient data, QA values, file-system paths and "My notes" out; no request
             setup, headers or bodies (curl is rebuilt from method + URL only), no query result samples, no QA
             worked example / QA trace. SQL text, hosts, internal names, branch names and PR links stay.
  shareable  the public redaction pass on every string, then the same drops as team plus: no PR links, request
             hosts -> <host>, every remaining scheme://host -> <host>, and the deny-list's `internal:` names ->
             <internal> (share.denylist)

Exports live under tickets/, which the data home never pushes (storage.push_paths allows daily/ and README.md only).
"""
import copy
import json
import os
import posixpath
import re

from . import common
from . import gitview
from . import scanlib
from .redact import redact

MODES = ("full", "team", "shareable")
MARKER = "<!-- cg:export-data -->"
HEAD_MARKER = "<!-- cg:export-head -->"

_FILE_URL = re.compile(r"(?i)\bfile:/{2,3}[^\s\"'<>]*")
_URL_HOST = re.compile(r"(?i)\b(?:https?|wss?|ftp)://[^\s/\"'<>)\]]+")
_WIN_PATH = re.compile(r"(?i)(?<![\w/\\])[a-z]:[\\/][^\s\"'<>|*?]*")
_UNC_PATH = re.compile(r"(?<![\w\\])\\\\[^\s\\\"'<>]+\\[^\s\"'<>|]*")
_NIX_PATH = re.compile(r"(?<![\w.~/-])(?:~|/(?:c|d)/Users|/Users|/home|/mnt/[a-z]|/tmp|/var|/opt|/etc|/srv)/[^\s\"'<>|]*")
_QA_SECTION = re.compile(r"(?ms)^## Worked example \(QA\)\s*\n.*?(?=^## |\Z)")


# "## My notes" + the block render.py keeps across renders (an unterminated block runs to the end of the text).
_MY_NOTES = re.compile(r"(?s)(?:^|(?<=\n))(?:#{1,6}[ \t]+My notes[ \t]*\n\s*)?[ \t]*<!-- my-notes:start -->(.*?)"
                       r"(?:<!-- my-notes:end -->[ \t]*\n?|\Z)")


def _hit(stats, name, n=1):
    if stats is not None and n:
        stats[name] = stats.get(name, 0) + n


# --------------------------------------------------------------------------- shareable / team pass

def strip_my_notes(s, stats=None):
    """Your own "My notes" blocks out of one string (counted in stats["my-notes"] when they held anything)."""
    if not isinstance(s, str) or "my-notes:start" not in s:
        return s

    def cut(m):
        if m.group(1).strip():
            _hit(stats, "my-notes")
        return ""
    return _MY_NOTES.sub(cut, s)


def scrub(s, hosts=True, stats=None):
    """File-system paths (and, unless hosts=False, every scheme://host) out of one string."""
    if not isinstance(s, str) or not s:
        return s
    rules = [(_FILE_URL, "<path>")] + ([(_URL_HOST, "<host>")] if hosts else []) + \
        [(_UNC_PATH, "<path>"), (_WIN_PATH, "<path>"), (_NIX_PATH, "<path>")]
    for rx, token in rules:
        s, n = rx.subn(token, s)
        _hit(stats, "local-path" if token == "<path>" else "host", n)
    return s


def _clean_obj(o, mode, stats, deny):
    """Every string: My notes out, the redaction pass for `mode`, paths (and for public: hosts and the deny-list's
    internal names) out. Public also masks internal names in dict keys (repo names)."""
    if isinstance(o, str):
        o = redact(strip_my_notes(o, stats), stats, mode=mode)
        o = scrub(o, hosts=mode == "public", stats=stats)
        return scanlib.mask_internal(o, deny, stats) if mode == "public" and deny else o
    if isinstance(o, list):
        return [_clean_obj(x, mode, stats, deny) for x in o]
    if isinstance(o, dict):
        key = (lambda k: scanlib.mask_internal(k, deny, stats)) if mode == "public" and deny else (lambda k: k)
        return {key(k): _clean_obj(v, mode, stats, deny) for k, v in o.items()}
    return o


def _strip_host(url):
    return _URL_HOST.sub("<host>", url) if isinstance(url, str) else url


def drop_local(t, mode, stats=None):
    """In place, the parts that never leave the machine (both `team` and `shareable`): local paths and the file
    list, Jira fields, request setup lines, headers and bodies (curl rebuilt from method + URL), query result
    samples, the QA trace and worked example; `shareable` also drops PR links and request hosts."""
    from .dashboard import api
    public = mode in ("shareable", "public")
    for k, empty in (("paths", {}), ("files", []), ("jira", {})):
        if k in t:
            t[k] = empty
    for a in t.get("artifacts") or []:
        if a.get("path"):
            _hit(stats, "local-path")
        a["path"] = None
    rq = t.get("requests") or {}
    if rq.get("setup"):
        _hit(stats, "request-setup")
    if rq.get("bruno_folder"):
        _hit(stats, "local-path")
    if "setup" in rq or "bruno_folder" in rq:
        rq["setup"], rq["bruno_folder"] = "", None
    for s in rq.get("steps") or []:
        if re.search(r"(?:^|\s)(?:-H|--header|-d|--data[\w-]*|-F|--form)\s", s.get("curl") or ""):
            _hit(stats, "request-body")
        if public:
            s["url"] = _strip_host(s.get("url"))
        s["curl"] = api.curl_for({"method": s.get("method"), "url": s.get("url") or ""}) if s.get("url") else ""
    if rq.get("scenarios"):
        rq["scenarios"] = [{"name": sc.get("name"), "summary": sc.get("summary") or ""} for sc in rq["scenarios"]
                           if isinstance(sc, dict)]
    for q in t.get("queries") or []:
        if q.get("revealed"):
            _hit(stats, "query-sample")
        q["revealed"] = None  # a summary of the result rows
    df = t.get("dbflow")
    if isinstance(df, dict):
        if df.get("qa_trace"):
            _hit(stats, "qa-trace")
        df["qa_trace"] = None
        md, n = _QA_SECTION.subn("", df.get("markdown") or "")
        _hit(stats, "qa-example", n)
        df["markdown"] = md
    if public:
        for e in ((t.get("git") or {}).get("repos") or {}).values():
            for p in e.get("prs") or []:
                if p.get("url"):
                    _hit(stats, "pr-link")
                p["url"] = None
    return t


def clean(t, mode, stats=None, deny=None):
    """A redacted copy of a ticket (or part of one) for `mode` team | shareable: drop_local(), then every string
    through _clean_obj() until stable, then the branch graphs drawn again from the redacted data."""
    t = drop_local(copy.deepcopy(t), mode, stats)
    rmode = "public" if mode in ("shareable", "public") else "team"
    for _ in range(3):  # until stable: the passes never undo each other, but check
        before = t
        t = _clean_obj(t, rmode, stats, deny)
        if t == before:
            break
    for e in ((t.get("git") or {}).get("repos") or {}).values():
        gr = gitview.graph(e)
        e["graph"], e["graph_legend"], e["graph_why"] = gr["source"], gr["legend"], gr["why"]
    return t


def shareable(t, stats=None, deny=None):
    """A copy of api.ticket(...) that is safe to hand to someone else (the share level `public`). `deny`: the
    deny-list rules whose `internal:` names become <internal> (share.deny_rules(); None = load the configured list)."""
    if deny is None:
        from . import share
        deny = share.deny_rules()
    return clean(t, "shareable", stats, deny)


def team(t, stats=None):
    """A copy of api.ticket(...) for your team (the share level `team`): like shareable, but SQL text, hosts,
    internal names and PR links stay."""
    return clean(t, "team", stats)


# --------------------------------------------------------------------------- the file

def snapshot(key, mode="full", now=None):
    """The ticket's data as embedded in the export, or None when the ticket is unknown."""
    if mode not in MODES:
        raise ValueError("mode must be full, team or shareable")
    from .dashboard import api
    t = api.ticket((key or "").upper())
    if not t:
        return None
    t = json.loads(json.dumps(t, default=str))  # a private copy: api results may be cached
    if mode == "shareable":
        t = shareable(t)
    elif mode == "team":
        t = team(t)
    t["export"] = {"at": (now or common.now()).isoformat(timespec="minutes"), "mode": mode}
    return t


def needs_mermaid(t):
    if "```mermaid" in ((t.get("dbflow") or {}).get("markdown") or ""):
        return True
    return any(e.get("graph") for e in ((t.get("git") or {}).get("repos") or {}).values())


def _app_file(rel):
    from .dashboard import server
    return common.read_text(os.path.join(os.path.dirname(server.PAGE), rel.replace("/", os.sep)))


_REL_IMPORT = re.compile(r"""(\bfrom\s*|\bimport\s*\(?\s*)(['"])(\.{1,2}/[^'"]+)\2""")
_ABS_IMPORT = re.compile(r"""(\bfrom\s*|\bimport\s*\(?\s*)(['"])(/(?:app|vendor)/[^'"]+)\2""")


# An export only ever shows the ticket page: these modules become stubs that keep their exported names (main.js
# imports them) and render nothing; the ones only stubbed pages import are left out.
_STUBBED = {"/app/pages/" + p + ".js" for p in ("today", "timesheet", "knowledge", "db", "team", "search", "health",
                                               "setup", "settings")} | {"/app/lib/share.js"}
_UNUSED = {"/app/lib/mcpstatus.js", "/app/lib/scheduler.js"}
_EXPORTED = re.compile(r"^export\s+(?:function|const|let)\s+(\w+)", re.M)


def _stub(src):
    return "".join("export function %s() { return null; }\n" % n for n in _EXPORTED.findall(src))


def app_modules():
    """{"@cg/app/…js" | "@cg/vendor/…js": source} — every ES module the page loads, each import rewritten to those bare
    names so an import map can point every one at a blob: URL (the export has no server to resolve paths against)."""
    from .dashboard import server
    app = os.path.dirname(server.PAGE)
    mods = {}
    for root, _dirs, files in os.walk(app):
        for fn in sorted(files):
            if not fn.endswith(".js") or fn == "theme.js":
                continue
            key = "/app/" + os.path.relpath(os.path.join(root, fn), app).replace(os.sep, "/")
            base = posixpath.dirname(key)
            if key in _UNUSED:
                continue
            src = common.read_text(os.path.join(root, fn))
            if key in _STUBBED:
                mods[key] = _stub(src)
                continue

            def absolute(m, base=base):
                return m.group(1) + m.group(2) + posixpath.normpath(posixpath.join(base, m.group(3))) + m.group(2)
            mods[key] = _REL_IMPORT.sub(absolute, src)
    for path, (sub, name) in server.VENDOR.items():
        p = server.vendor_path(sub, name)
        mods[path] = common.read_text(p) if p else ""
    # a blob: module can't resolve "/x.js" against its own URL, so every specifier becomes a bare "@cg/x.js" name
    return {"@cg" + k: _ABS_IMPORT.sub(lambda m: m.group(1) + m.group(2) + "@cg" + m.group(3) + m.group(2), v)
            for k, v in mods.items()}


# A classic script: blob: URLs for every module, an import map in front of them, then the entry module.
_LOADER = """(function () {
  var M = JSON.parse(document.getElementById('cg-modules').textContent), map = { imports: {} };
  Object.keys(M).forEach(function (k) { map.imports[k] = URL.createObjectURL(new Blob([M[k]], { type: 'text/javascript' })); });
  var im = document.createElement('script'); im.type = 'importmap'; im.textContent = JSON.stringify(map);
  document.head.appendChild(im);
  var s = document.createElement('script'); s.type = 'module'; s.src = map.imports['@cg/app/main.js'];
  document.body.appendChild(s);
})();"""
EXPORT_CSP = ("default-src 'none'; script-src 'unsafe-inline' blob:; style-src 'unsafe-inline'; img-src data: blob:; "
              "font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'")
_PAGE_TAGS = (r'<link rel="icon"[^>]*>\n?', r'<link rel="manifest"[^>]*>\n?', r'<script src="/app/theme.js"></script>\n?',
              r'<link rel="stylesheet" href="/app/app.css">\n?', r'<script type="module" src="/app/main.js"></script>\n?')


def _mermaid_js():
    from .dashboard import server
    p = server.mermaid_path()
    return common.read_text(p) if p else ""


def _json_for_script(obj):
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") \
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _inline(text, tag):
    return re.sub(r"(?i)</" + tag, "<\\\\/" + tag, text)


def render(t):
    """The dashboard page (app/index.html) with its CSS, scripts, the snapshot and — when a diagram needs it — Mermaid
    inlined. A CSP inside the file blocks every request."""
    from .dashboard import server
    page = common.read_text(server.PAGE)
    if MARKER not in page or HEAD_MARKER not in page:
        raise RuntimeError("the dashboard page has no export markers")
    for tag in _PAGE_TAGS:
        page = re.sub(tag, "", page)
    head = ('<meta http-equiv="Content-Security-Policy" content="%s">\n<script>%s</script>\n<style>%s</style>\n'
            % (EXPORT_CSP, _inline(_app_file("theme.js"), "script"), _inline(_app_file("app.css"), "style")))
    data = '<script id="cg-export" type="application/json">%s</script>\n' % _json_for_script(t)
    data += '<script id="cg-modules" type="application/json">%s</script>\n' % _json_for_script(app_modules())
    if needs_mermaid(t):
        js = _mermaid_js()
        if js:
            data += "<script>%s</script>\n" % _inline(js, "script")
    data += "<script>%s</script>\n" % _LOADER
    title = "%s · Chitragupta export" % t.get("key", "")
    page = page.replace("<title>Chitragupta</title>", "<title>%s</title>" % _esc(title), 1)
    return page.replace(HEAD_MARKER, head, 1).replace(MARKER, data, 1)


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def filename(key, mode, now=None):
    return "%s-%s-%s.html" % (key.upper(), mode, (now or common.now()).strftime("%Y%m%d-%H%M"))


def export_ticket(key, mode="full", now=None):
    """(filename, html) for one ticket, or None when it is unknown. Reads only."""
    now = now or common.now()
    t = snapshot(key, mode, now)
    if t is None:
        return None
    return filename(t["key"], mode, now), render(t)


def export_to_file(key, mode="full", out_dir=None, now=None):
    """Write the export under <home>/tickets/<KEY>/exports/ (or out_dir) and return its path."""
    res = export_ticket(key, mode, now)
    if res is None:
        raise ValueError("unknown ticket: %s" % key)
    name, html = res
    d = out_dir or common.hp("tickets", key.upper(), "exports")
    path = os.path.join(d, name)
    common.write_text(path, html)
    return path
