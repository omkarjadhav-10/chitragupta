"""Static checks (safety §3.8): only chitragupta/safety/ may start a process, change a file or open a connection,
and the internal-call builder can never emit a bypass."""
import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "chitragupta")
SAFETY = os.path.join(PKG, "safety")
# The dashboard server is the one listener: it binds 127.0.0.1 only (rule 9, tested in test_dashboard).
LISTENER = os.path.join(PKG, "dashboard", "server.py")

BANNED_MODULES = {"subprocess", "socket", "http.client", "urllib.request", "ctypes", "tempfile"}
BANNED_CALLS = {
    ("os", "system"), ("os", "popen"), ("os", "startfile"), ("os", "remove"), ("os", "unlink"), ("os", "rename"),
    ("os", "renames"), ("os", "replace"), ("os", "rmdir"), ("os", "removedirs"), ("os", "makedirs"), ("os", "mkdir"),
    ("os", "chmod"), ("os", "utime"), ("os", "symlink"), ("os", "link"), ("os", "truncate"), ("os", "fdopen"),
    ("os", "open"), ("os", "write"), ("os", "kill"),
    ("shutil", "move"), ("shutil", "copy"), ("shutil", "copy2"), ("shutil", "copyfile"), ("shutil", "copytree"),
    ("shutil", "rmtree"), ("shutil", "chown"), ("shutil", "make_archive"), ("shutil", "unpack_archive"),
}
BANNED_PREFIX = (("os", "spawn"), ("os", "exec"))
PATHLIB_WRITES = {"write_text", "write_bytes", "touch", "mkdir", "unlink", "rename", "replace", "rmdir", "symlink_to",
                  "hardlink_to", "chmod"}
SAFE_RECEIVERS = {"common", "paths", "safety", "commands"}


def py_files():
    files = [os.path.join(ROOT, "hooks", "run.py")]
    for dp, dns, fns in os.walk(PKG):
        dns[:] = [d for d in dns if d != "__pycache__"]
        if os.path.normcase(dp).startswith(os.path.normcase(SAFETY)):
            continue
        files += [os.path.join(dp, f) for f in fns if f.endswith(".py")]
    return files


def _write_mode(call):
    mode = None
    if len(call.args) >= 2:
        mode = call.args[1]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return bool(set(mode.value) & set("wax+"))
    return True  # a computed mode counts as a write


def _memory_db(call):
    """sqlite3.connect(":memory:") touches no file; any other database goes through safety.paths.sqlite_connect."""
    a = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "database"), None)
    return isinstance(a, ast.Constant) and a.value == ":memory:" and not any(k.arg == "uri" for k in call.keywords)


def violations(path, source=None):
    if source is None:
        with open(path, encoding="utf-8") as f:
            source = f.read()
    tree = ast.parse(source, path)
    out = []
    listener = os.path.normcase(path) == os.path.normcase(LISTENER)
    uses_pathlib = any(isinstance(n, (ast.Import, ast.ImportFrom)) and
                       ("pathlib" in [a.name for a in n.names] if isinstance(n, ast.Import) else n.module == "pathlib")
                       for n in ast.walk(tree))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                out += ["%d: from sqlite3 import %s" % (node.lineno, a.name) for a in node.names if a.name == "connect"]
            if isinstance(node, ast.ImportFrom) and node.module in ("os", "shutil"):
                out += ["%d: from %s import %s" % (node.lineno, node.module, a.name) for a in node.names
                        if (node.module, a.name) in BANNED_CALLS]
            for n in names:
                top = n if n in BANNED_MODULES else n.split(".")[0] if n.split(".")[0] in BANNED_MODULES else None
                if listener and n in ("socket", "http.server"):
                    continue
                if top or n in ("http.client", "urllib.request"):
                    out.append("%d: import %s" % (node.lineno, n))
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == "open" and _write_mode(node):
                out.append("%d: open() in a write mode" % node.lineno)
            if isinstance(f, ast.Attribute):
                recv = f.value.id if isinstance(f.value, ast.Name) else None
                if (recv, f.attr) in BANNED_CALLS or any(recv == m and f.attr.startswith(p) for m, p in BANNED_PREFIX):
                    out.append("%d: %s.%s()" % (node.lineno, recv, f.attr))
                if recv == "sqlite3" and f.attr == "connect" and not _memory_db(node):
                    out.append("%d: sqlite3.connect() of a file (use safety.paths.sqlite_connect)" % node.lineno)
                if recv == "zipfile" and f.attr in ("ZipFile", "PyZipFile") and _write_mode(node):
                    out.append("%d: zipfile.%s() for writing" % (node.lineno, f.attr))
                if recv == "io" and f.attr == "open" and _write_mode(node):
                    out.append("%d: io.open() in a write mode" % node.lineno)
                if uses_pathlib and f.attr in PATHLIB_WRITES and recv not in SAFE_RECEIVERS:
                    out.append("%d: .%s() (pathlib-style write)" % (node.lineno, f.attr))
                if recv == "winreg" and re.match(r"(Set|Create|Delete)", f.attr):
                    out.append("%d: winreg.%s()" % (node.lineno, f.attr))
    return out


class StaticSafetyTest(unittest.TestCase):
    def test_only_safety_has_side_effects(self):
        bad = {}
        for p in py_files():
            v = violations(p)
            if v:
                bad[os.path.relpath(p, ROOT)] = v
        self.assertEqual(bad, {}, "side effects outside chitragupta/safety/:\n" +
                         "\n".join("%s\n  %s" % (k, "\n  ".join(v)) for k, v in bad.items()))

    def test_sqlite_files_only_through_the_guard(self):
        bad = violations("x.py", "import sqlite3\nsqlite3.connect(p)\nsqlite3.connect('C:/x.db')\n"
                                 "sqlite3.connect(database=p)\nsqlite3.connect(':memory:', uri=True)\n"
                                 "from sqlite3 import connect\n")
        self.assertEqual(len(bad), 5, bad)
        self.assertEqual(violations("x.py", "import sqlite3\nsqlite3.connect(':memory:')\n"
                                            "sqlite3.connect(database=':memory:', check_same_thread=False)\n"), [])

    def test_builder_never_bypasses(self):
        from chitragupta.safety import claude as builder
        src = open(builder.__file__, encoding="utf-8").read()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        body = code.split('"""', 2)[2]  # skip the module docstring, which names what it never emits
        for bad in ("dangerously-skip-permissions", "bypassPermissions", "allow-dangerously", "acceptEdits",
                    "permission-prompt-tool", "disableAllHooks"):
            self.assertNotIn(bad, body)
        for profile in builder.PROFILES:
            argv = builder.build(["claude"], "x", profile, "haiku", allowed=["mcp__s__read_thing"])
            joined = " ".join(argv)
            self.assertNotIn("dangerously", joined)
            self.assertNotIn("bypassPermissions", joined)
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
            self.assertEqual(argv[argv.index("--tools") + 1], "")

    def test_no_off_switch(self):
        """The safety package reads no environment variable and no setting that could switch it off."""
        for fn in os.listdir(SAFETY):
            if not fn.endswith(".py"):
                continue
            src = open(os.path.join(SAFETY, fn), encoding="utf-8").read()
            for pat in (r"os\.environ\.get\(|os\.getenv\(|common\.env\(", r"\benabled\b.*safety|safety.*\benabled\b",
                        r"disable_safety|safety_off|SAFETY_OFF|NO_GUARD|GUARD_ALLOW"):
                self.assertIsNone(re.search(pat, src), "%s: %s" % (fn, pat))


if __name__ == "__main__":
    unittest.main()
