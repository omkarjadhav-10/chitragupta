"""Chitragupta launcher.

Two homes, one file:
  * plugin hooks:   <python> ${CLAUDE_PLUGIN_ROOT}/hooks/run.py hook <event>
  * stable runtime: <python> ~/.chitragupta/runtime/run.py <module> [args…]
    (setup copies this file there; it loads runtime/<version in "current">/chitragupta)

Hooks fail open: any problem → exit 0 silently. The launcher never waits on a model.
`safety session` (the /cg: skills' PreToolUse guard) fails open the same way when the package can't load: it is
registered without onFailure: block, so a broken install never blocks the session. Once the package has loaded, the
guard itself fails closed while a /cg: command is confirmed running (chitragupta/safety/hook.py).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def package_root():
    """Directory that contains the `chitragupta` package."""
    plugin_root = os.path.dirname(HERE)  # <plugin>/hooks/run.py
    if os.path.isfile(os.path.join(plugin_root, "chitragupta", "__init__.py")):
        return plugin_root
    try:  # <user>/.chitragupta/runtime/run.py + current
        with open(os.path.join(HERE, "current"), encoding="utf-8-sig") as f:
            version = f.read().strip()
    except OSError:
        return None
    root = os.path.join(HERE, version)
    return root if os.path.isfile(os.path.join(root, "chitragupta", "__init__.py")) else None


def is_hook(argv):
    return bool(argv) and argv[0] == "hook"


def fails_open(argv):
    """Hooks, and the session guard: a launcher or package-load failure exits 0 silently."""
    return is_hook(argv) or list(argv[:2]) == ["safety", "session"]


def main(argv):
    hook = is_hook(argv)
    quiet = fails_open(argv)
    try:
        if os.environ.get("CHITRAGUPTA_INTERNAL") == "1" and hook:
            return 0  # the engine's own child processes are never captured
        root = package_root()
        if root is None:
            return 0 if quiet else 3
        if root not in sys.path:
            sys.path.insert(0, root)
        from chitragupta.__main__ import run
        rc = run(argv)
        return 0 if hook else rc
    except BaseException:  # noqa: BLE001 — hooks (and the session guard) must never break a session
        if quiet:
            return 0
        raise


if __name__ == "__main__":
    code = main(sys.argv[1:])
    if is_hook(sys.argv[1:]):
        sys.stdout.flush()
        os._exit(0)
    sys.exit(code)
