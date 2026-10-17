"""The stable runtime: a copy of the package that outlives plugin updates.

Layout under the user folder (%USERPROFILE%\\.chitragupta, or CHITRAGUPTA_USER_DIR):
    runtime/run.py                       the launcher (copied from <plugin>/hooks/run.py)
    runtime/current                      the version the launcher loads
    runtime/<version>/chitragupta/       the package (no __pycache__)
    runtime/<version>/user-skill/        the recall skill template (so setup works from the runtime too)

The scheduled task, the data home's git pre-commit hook, replay.sh and the MCP guard all call runtime/run.py,
never the versioned plugin folder, which moves on every `/plugin update`.

    python -m chitragupta runtime status [--json]
    python -m chitragupta runtime install [--force]
"""
import hashlib
import json
import os
import re
import shutil
import sys

from . import __version__
from . import common

KEEP = 2  # newest versions kept, the current one always among them
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.tmp")
_SKIP_DIRS = {"__pycache__"}


def runtime_dir():
    return os.path.join(common.user_dir(), "runtime")


def launcher_path():
    return os.path.join(runtime_dir(), "run.py")


def current_version():
    return common.read_text(os.path.join(runtime_dir(), "current")).strip() or None


def package_version(pkg_dir=None):
    """__version__ of the package at pkg_dir, read from its __init__.py without importing it."""
    if not pkg_dir or os.path.abspath(pkg_dir) == os.path.abspath(common.PACKAGE_DIR):
        return __version__
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", common.read_text(os.path.join(pkg_dir, "__init__.py")))
    return m.group(1) if m else None


def vkey(v):
    """(major, minor, patch, …) for a plain dotted version, else None ('test', 'dev')."""
    if v and re.fullmatch(r"\d+(\.\d+)*", v):
        return tuple(int(x) for x in v.split("."))
    return None


def newer(a, b):
    """True when version a is strictly newer than b (unparseable versions are older than any real one)."""
    ka, kb = vkey(a), vkey(b)
    if ka is None:
        return False
    return kb is None or ka > kb


def _manifest(root):
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in _SKIP_DIRS)
        for fn in fns:
            if fn.endswith((".pyc", ".pyo", ".tmp")):
                continue
            p = os.path.join(dp, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root).replace("\\", "/")] = hashlib.sha1(f.read()).hexdigest()
    return out


def same_tree(a, b):
    return os.path.isdir(a) and os.path.isdir(b) and _manifest(a) == _manifest(b)


def launcher_source(pkg_dir):
    """<plugin>/hooks/run.py next to the package, or the runtime's own run.py when the package is a runtime copy."""
    plugin_run = os.path.join(os.path.dirname(pkg_dir), "hooks", "run.py")
    if os.path.isfile(plugin_run):
        return plugin_run
    own = os.path.join(os.path.dirname(os.path.dirname(pkg_dir)), "run.py")
    return own if os.path.isfile(own) else None


def user_skill_source(pkg_dir):
    """<root>/user-skill next to the package (the plugin's, or a runtime copy's own)."""
    d = os.path.join(os.path.dirname(pkg_dir), "user-skill")
    return d if os.path.isdir(d) else None


def _write_bytes_atomic(path, data):
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    common.ensure_dir(os.path.dirname(path))
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def _rmtree(p):
    shutil.rmtree(p, ignore_errors=True)


def versions():
    """Installed version folders (those holding a package), oldest first."""
    rt = runtime_dir()
    out = []
    for d in os.listdir(rt) if os.path.isdir(rt) else []:
        if d.startswith(".") or not os.path.isfile(os.path.join(rt, d, "chitragupta", "__init__.py")):
            continue
        out.append(d)
    return sorted(out, key=lambda v: (vkey(v) is not None, vkey(v) or (), os.path.getmtime(os.path.join(rt, v))))


def prune(keep_version, keep=KEEP):
    """Remove all but the newest `keep` versions; `keep_version` always stays. Returns the removed names."""
    vs = [v for v in versions() if v != keep_version]
    stay = set(vs[-(keep - 1):]) if keep > 1 else set()
    removed = []
    for v in vs:
        if v not in stay:
            _rmtree(os.path.join(runtime_dir(), v))
            removed.append(v)
    return removed


def needs_refresh():
    """The runtime is missing, incomplete or older than this package (it is never downgraded)."""
    cur = current_version()
    if cur == __version__:
        return not (os.path.isfile(launcher_path())
                    and os.path.isfile(os.path.join(runtime_dir(), cur, "chitragupta", "__init__.py")))
    return not newer(cur, __version__)


def install(pkg_dir=None, force=False):
    """Copy the package into runtime/<version>/, refresh the launcher, point `current` at it, keep the newest 2.
    Idempotent (unchanged files are not rewritten). A newer installed runtime is kept unless force=True."""
    src = os.path.abspath(pkg_dir or common.PACKAGE_DIR)
    ver = package_version(src)
    if not ver:
        raise RuntimeError("no __version__ in %s" % src)
    rt = runtime_dir()
    cur = current_version()
    res = {"version": ver, "path": os.path.join(rt, ver), "previous": cur, "copied": False, "launcher_updated": False,
           "current_changed": False, "removed": [], "changed": False}
    if not force and cur and cur != ver and newer(cur, ver) and os.path.isfile(launcher_path()):
        res.update(version=cur, path=os.path.join(rt, cur), kept_newer=True)
        return res
    common.ensure_dir(rt)
    target = os.path.join(rt, ver)
    # Folders next to the package that it needs at run time: the recall skill template and vendored assets.
    extras = {"user-skill": user_skill_source(src)}
    tp = os.path.join(os.path.dirname(src), "third_party")
    extras["third_party"] = tp if os.path.isdir(tp) else None
    fresh = same_tree(src, os.path.join(target, "chitragupta")) and all(
        not s or same_tree(s, os.path.join(target, name)) for name, s in extras.items())
    if not fresh:
        tmp = os.path.join(rt, ".%s.tmp-%d" % (ver, os.getpid()))
        _rmtree(tmp)
        shutil.copytree(src, os.path.join(tmp, "chitragupta"), ignore=_IGNORE)
        for name, s in extras.items():
            if s:
                shutil.copytree(s, os.path.join(tmp, name), ignore=_IGNORE)
        if os.path.exists(target):
            old = os.path.join(rt, ".%s.old-%d" % (ver, os.getpid()))
            _rmtree(old)
            os.replace(target, old)
            os.replace(tmp, target)
            _rmtree(old)
        else:
            os.replace(tmp, target)
        res["copied"] = True
    lsrc = launcher_source(src)
    if lsrc and os.path.abspath(lsrc) != os.path.abspath(launcher_path()):
        with open(lsrc, "rb") as f:
            res["launcher_updated"] = _write_bytes_atomic(launcher_path(), f.read())
    elif not os.path.isfile(launcher_path()):
        raise RuntimeError("no launcher (hooks/run.py) next to %s" % src)
    if cur != ver:
        _write_bytes_atomic(os.path.join(rt, "current"), (ver + "\n").encode("utf-8"))
        res["current_changed"] = True
    res["removed"] = prune(ver)
    res["changed"] = bool(res["copied"] or res["launcher_updated"] or res["current_changed"] or res["removed"])
    return res


def status():
    cur = current_version()
    return {"package_version": __version__, "current": cur, "current_ok": cur == __version__,
            "launcher": launcher_path(), "launcher_exists": os.path.isfile(launcher_path()),
            "versions": versions(), "needs_refresh": needs_refresh(), "path": runtime_dir()}


def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "install":
        print(json.dumps(install(force="--force" in argv), indent=1))
        return 0
    if cmd == "status":
        st = status()
        print(json.dumps(st, indent=1) if "--json" in argv else "\n".join("%s: %s" % kv for kv in st.items()))
        return 0 if st["current_ok"] and st["launcher_exists"] else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # pythonw has no console
    sys.exit(main(sys.argv[1:]))
