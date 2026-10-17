"""Dashboard UI checks in a real browser, on a synthetic data home (never a real one).

    python scripts/ui/run.py [--shots DIR]

Builds the synthetic home the unit tests use (tests/test_dashboard.py + tests/test_dashboard_v2.py fixtures) in a
temp folder, serves the dashboard from this checkout on a free port, and runs scripts/ui/check.js: every page in light
and dark (axe, console errors, requests leaving 127.0.0.1, first-view time), 1440/1280/900 px layouts, the command
palette, shortcuts, tabs and a keyboard-only walk. Needs Node and, once, `npm ci` in scripts/ui (playwright-core,
which drives the installed Chrome or Edge; nothing is downloaded at run time). axe-core is vendored in vendor/.
Dev only: nothing here ships in the runtime.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv):
    shots = argv[argv.index("--shots") + 1] if "--shots" in argv else ""
    home = tempfile.mkdtemp(prefix="cg-ui-home-")
    user = tempfile.mkdtemp(prefix="cg-ui-user-")
    for k in [k for k in os.environ if k.startswith(("CHITRAGUPTA_", "WORKLOG_"))]:
        del os.environ[k]
    os.environ.update(CHITRAGUPTA_HOME=home, CHITRAGUPTA_USER_DIR=user, CHITRAGUPTA_CLAUDE_DIR=os.path.join(user, "claude"),
                      CHITRAGUPTA_NO_TASK="1")
    sys.path.insert(0, ROOT)
    from tests.test_dashboard import build_home
    from tests.test_dashboard_v2 import add_v2_fixtures
    from chitragupta.dashboard import server
    days = build_home(home)
    add_v2_fixtures(home, days)
    srv, _t = server.start_in_thread(0)
    try:
        base = "http://127.0.0.1:%d/" % srv.port
        print("serving the synthetic home at %s" % base)
        cmd = ["node", os.path.join(HERE, "check.js"), base, days["day"]] + ([shots] if shots else [])
        return subprocess.call(cmd, cwd=HERE)
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(user, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
