"""`python -m chitragupta <module> [args…]` runs chitragupta.<module> as a script.

The launchers (hooks/run.py in the plugin, run.py in the stable runtime) put the
package on sys.path and call run().
"""
import runpy
import sys

ENTRY_MODULES = {
    "agent", "hook", "redact", "mcpguard", "bruenv", "install", "uninstall", "migrate",
    "setup", "doctor", "dashboard", "timesheet", "upgrade01", "mermaid_check",
}


def run(argv):
    if not argv or argv[0] not in ENTRY_MODULES:
        print("usage: chitragupta <%s> [args…]" % "|".join(sorted(ENTRY_MODULES)), file=sys.stderr)
        return 2
    mod, rest = argv[0], argv[1:]
    sys.argv = ["chitragupta." + mod, *rest]
    try:
        runpy.run_module("chitragupta." + mod, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
