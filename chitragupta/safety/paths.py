"""Path guard: the only functions in Chitragupta that create, change, move or delete a file.

A path is allowed only when its real location (junctions and symlinks followed, Windows case-insensitive) is under
one of Chitragupta's own write roots:

  tree     the data home · %USERPROFILE%\\.chitragupta · <claude dir>/skills/chitragupta
  file     <claude dir>/CLAUDE.md (the chitragupta block) and CLAUDE.md.chitragupta.bak ·
           the one Startup-folder launcher (only used when Task Scheduler refuses the task) ·
           the dashboard's Chitragupta.url shortcut in the Start menu and on the Desktop
  pattern  worklog-kb-*.zip in storage.local_backup_dir (the nightly backup and its pruning)
  share    <folder>/shares/ of each configured folder share target (share.targets, kind "folder"): sharing writes
           redacted bundles there, only when you ask (safety rule: "Sharing writes redacted bundles to my configured
           share target, only when I ask."). Never the folder itself or anything beside shares/.
  delete   <claude dir>/projects/<the sandbox's folder>/ : the tool-result copies of our own MCP jobs (delete only)

Rejected before anything else: empty paths, UNC and device paths (\\\\server, \\\\?\\, \\\\.\\), NUL, `..` components,
alternate data streams (a ':' after the drive letter). Sources of copies are only read, never moved.
A SQLite database file is opened only through sqlite_connect() (write roots checked; ATTACH refused).
"""
import fnmatch
import os
import re
import shutil
import time

from .. import common
from .log import block

LAYER = "path"
BACKUP_PATTERN = "worklog-kb-*.zip*"


_DELETED = re.compile(r"(?i)^(?:\\\\\?\\)?[a-z]:\\\$extend\\\$deleted\\")


def _real(p):
    a = os.path.abspath(p)
    r = os.path.realpath(a)
    if _DELETED.match(r):  # Windows: a file another thread is deleting right now resolves into NTFS's $Deleted folder;
        r = os.path.join(os.path.realpath(os.path.dirname(a)), os.path.basename(a))  # its folder is what counts
    if r.startswith("\\\\?\\"):
        r = r[4:]
    return os.path.normcase(r)


def _claude_dir():
    from .. import plugin
    return plugin.claude_dir()


def sandbox():
    return common.wl("sandbox")


def tmp_dir():
    """Chitragupta's own temp folder (bruenv request bodies); never the system temp."""
    return common.wl("tmp")


def _project_slug(p):
    """Claude Code's folder name under projects/ for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(p))


def roots():
    """[(kind, path)] — computed on every call (tests switch homes through the environment)."""
    cd = _claude_dir()
    md = os.path.join(cd, "CLAUDE.md")
    out = [("tree", common.home()), ("tree", common.user_dir()), ("tree", os.path.join(cd, "skills", "chitragupta")),
           ("file", md), ("file", md + ".chitragupta.bak"),
           ("delete", os.path.join(cd, "projects", _project_slug(sandbox())))]
    try:
        from .. import install
        out.append(("file", install.startup_launcher_path()))
        out += [("file", os.path.join(d, install.SHORTCUT_FILE)) for d in install.shortcut_dirs(desktop=True)]
    except Exception:  # noqa: BLE001
        pass
    try:  # macOS / Linux: the scheduler's own LaunchAgent and systemd user units, nothing else in those folders
        from .. import scheduler
        out += [("file", p) for p in scheduler.own_files()]
        out += [("mkdir", d) for d in (scheduler.launch_agents_dir(), scheduler.systemd_user_dir())]
    except Exception:  # noqa: BLE001
        pass
    bd = _backup_dir()
    if bd:
        out.append(("pattern", bd))
    out += [("tree", d) for d in _cfg_roots()["shares"] if os.path.isdir(os.path.dirname(d))]
    return out


SHARE_DIR = "shares"
_BD = {}


def share_folder_roots(cfg, existing=True):
    """<path>/shares for every folder share target whose path is an absolute folder (not a drive root) that exists
    (existing=False: whether it exists or not; the existence check then happens at use)."""
    out = []
    for t in ((cfg or {}).get("share") or {}).get("targets") or []:
        if not isinstance(t, dict) or t.get("kind") != "folder":
            continue
        p = t.get("path")
        if not isinstance(p, str) or not p.strip() or _syntax(p.strip()) or not os.path.isabs(p.strip()):
            continue
        p = os.path.abspath(p.strip())
        if os.path.dirname(p) == p or (existing and not os.path.isdir(p)):
            continue
        out.append(os.path.join(p, SHARE_DIR))
    return out


def _cfg_roots():
    """Roots that come from config.json (backup dir, share folders), re-read only when the file changes."""
    p = common.wl("config.json")
    try:
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)  # size too: Windows file times can repeat within one clock tick
    except OSError:
        return {"backup": "", "shares": []}
    if key not in _BD:
        _BD.clear()
        try:
            cfg = common.read_json(p, {}) or {}
            _BD[key] = {"backup": ((cfg.get("storage") or {}).get("local_backup_dir") or "").strip(),
                        "shares": share_folder_roots(cfg, existing=False)}
        except Exception:  # noqa: BLE001
            _BD[key] = {"backup": "", "shares": []}
    return _BD[key]


def _backup_dir():
    """storage.local_backup_dir."""
    return _cfg_roots()["backup"]


def _syntax(raw):
    """Reason the raw path is refused outright, or None."""
    if not raw:
        return "empty path"
    if "\x00" in raw:
        return "NUL in path"
    if raw.startswith(("\\\\", "//")):
        return "UNC or device path"
    rest = raw[2:] if re.match(r"^[A-Za-z]:", raw) else raw
    if ":" in rest:
        return "alternate data stream or device name"
    if ".." in re.split(r"[\\/]+", raw):
        return "'..' in path"
    return None


def allowed(path, op="write"):
    """None when `path` may be changed by `op` (write | delete | mkdir), else the reason."""
    raw = os.fspath(path) if path is not None else ""
    bad = _syntax(raw)
    if bad:
        return bad
    real = _real(raw)
    for kind, root in roots():
        rr = _real(root)
        if kind == "tree" and (real == rr or real.startswith(rr.rstrip(os.sep) + os.sep)):
            return None
        if kind == "file" and real == rr:
            return None
        if kind == "pattern" and ((os.path.dirname(real) == rr and fnmatch.fnmatch(os.path.basename(real), BACKUP_PATTERN))
                                  or (real == rr and op == "write")):
            return None
        if kind == "delete" and op == "delete" and real.startswith(rr.rstrip(os.sep) + os.sep):
            return None
        if kind == "mkdir" and op == "mkdir" and (real == rr or rr.startswith(real.rstrip(os.sep) + os.sep)):
            return None  # create that folder (and its parents) only; nothing else is written there
    return "outside Chitragupta's write roots"


def check(path, op="write", caller=""):
    """Raise Blocked (and log) unless allowed; returns the path unchanged."""
    reason = allowed(path, op)
    if reason:
        block(LAYER, reason, "%s %s" % (op, path), op, caller)
    return path


# --------------------------------------------------------------------------- the write functions

def makedirs(p, caller=""):
    if p and not os.path.isdir(p):
        if allowed(p, "mkdir") is not None:
            check(p, caller=caller)
        os.makedirs(p, exist_ok=True)


def write_text(path, text, caller="", newline="\n"):
    """Atomic UTF-8 (no BOM) write. Returns True if the content changed."""
    check(path, caller=caller)
    try:
        with open(path, encoding="utf-8", newline="") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    makedirs(os.path.dirname(path), caller)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)
    _replace_retry(tmp, path)
    return True


def write_bytes(path, data, caller=""):
    check(path, caller=caller)
    makedirs(os.path.dirname(path), caller)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "wb") as f:
        f.write(data)
    _replace_retry(tmp, path)


def _replace_retry(tmp, path):
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # file briefly locked by an editor / AV scanner
            time.sleep(0.2 * (attempt + 1))
    os.replace(tmp, path)


def append_text(path, text, caller=""):
    check(path, caller=caller)
    makedirs(os.path.dirname(path), caller)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text)


def open_write(path, mode="w", caller="", **kw):
    """An open file for streaming writes (mode w / wb / a / x); the caller closes it."""
    if not set(mode) & set("wax+"):
        raise ValueError("open_write needs a write mode")
    check(path, caller=caller)
    makedirs(os.path.dirname(path), caller)
    return open(path, mode, **kw)


def zip_writer(path, caller=""):
    import zipfile
    check(path, caller=caller)
    makedirs(os.path.dirname(path), caller)
    return zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)


def create_exclusive(path, data=b"", caller=""):
    """Create `path` only if it doesn't exist (O_EXCL; a lock file). Returns the open fd; FileExistsError if taken."""
    check(path, caller=caller)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    if data:
        os.write(fd, data)
    return fd


def mkstemp(prefix="cg-", suffix="", caller=""):
    """(fd, path) of a new file in <home>/.worklog/tmp."""
    import tempfile
    d = tmp_dir()
    makedirs(d, caller)
    fd, p = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=d)
    return fd, p


def replace(src, dst, caller=""):
    """Move within the write roots (both ends checked)."""
    check(src, "delete", caller)
    check(dst, caller=caller)
    os.replace(src, dst)


def copy(src, dst, caller=""):
    """Copy (never move) from anywhere readable into the write roots."""
    check(dst, caller=caller)
    makedirs(os.path.dirname(dst), caller)
    shutil.copy2(src, dst)


def copytree(src, dst, ignore=None, caller=""):
    check(dst, caller=caller)
    shutil.copytree(src, dst, ignore=ignore)


def delete(path, caller=""):
    check(path, "delete", caller)
    os.remove(path)


def rmdir(path, caller=""):
    check(path, "delete", caller)
    os.rmdir(path)


def rmtree(path, caller=""):
    check(path, "delete", caller)
    shutil.rmtree(path, ignore_errors=True)


def chmod(path, mode, caller=""):
    check(path, caller=caller)
    os.chmod(path, mode)


def touch(path, caller=""):
    check(path, caller=caller)
    os.utime(path)


# --------------------------------------------------------------------------- SQLite (the search index cache)

_SQLITE_ATTACH = 24  # sqlite3.SQLITE_ATTACH (not exported by every Python build)


def _no_attach(action, *_):
    """Authorizer: ATTACH could create or change a database file anywhere, so it is refused on every connection
    (VACUUM attaches internally, so VACUUM and VACUUM INTO are refused with it)."""
    import sqlite3
    return sqlite3.SQLITE_DENY if action == _SQLITE_ATTACH else sqlite3.SQLITE_OK


def sqlite_connect(path, readonly=False, caller="", timeout=5.0):
    """A SQLite connection to a database file (SQLite also writes `<file>-journal` next to it).

    readonly  opened as `mode=ro`: nothing can be written, so the path only has to pass the syntax checks; a missing
              file raises sqlite3.OperationalError instead of being created
    else      the file must be under a write root (it is created when missing)

    Every connection refuses ATTACH (and so VACUUM [INTO]), so a statement can't write a database file elsewhere. ":memory:" is
    allowed outside this function; nothing else is (tests/test_safety_static.py)."""
    import sqlite3
    import urllib.parse
    raw = os.fspath(path) if path is not None else ""
    if readonly:
        bad = _syntax(raw)
        if bad:
            block(LAYER, bad, "read %s" % raw, "read", caller)
        uri = "file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(raw).replace("\\", "/"), safe="/:")
        con = sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
    else:
        check(raw, caller=caller)
        makedirs(os.path.dirname(raw), caller)
        con = sqlite3.connect(raw, timeout=timeout, check_same_thread=False)
    con.set_authorizer(_no_attach)
    return con
