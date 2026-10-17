"""Shared bits for the opt-in end-to-end scripts in this folder.

These scripts make REAL `claude -p` calls (they cost money and need a signed-in Claude Code), so they do nothing
unless CHITRAGUPTA_E2E=1. They never touch a real data home: everything runs in a throwaway CHITRAGUPTA_HOME and
CHITRAGUPTA_USER_DIR under the system temp folder, with synthetic data only, and is deleted afterwards
(pass --keep to inspect it). No Slack, no Jira, no push to anything but a local bare repo.

Optional: CHITRAGUPTA_E2E_CLAUDE = path to the claude executable (default: `claude` on PATH).
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests import support  # noqa: E402  (launcher install, child env, git identity)

RUN_PY = support.RUN_PY
PY = sys.executable


def gate(name):
    """Exit 0 with a note unless the run was asked for explicitly."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if os.environ.get("CHITRAGUPTA_E2E") != "1":
        print("%s: skipped (set CHITRAGUPTA_E2E=1 to run; it makes real claude calls)" % name)
        sys.exit(0)


def claude_exe():
    exe = os.environ.get("CHITRAGUPTA_E2E_CLAUDE") or shutil.which("claude")
    if not exe:
        print("claude executable not found (set CHITRAGUPTA_E2E_CLAUDE)")
        sys.exit(2)
    return exe


def paths_cfg():
    return {"python": PY, "pythonw": "", "claude": claude_exe(), "git": shutil.which("git") or "git"}


class TempEnv:
    """Temp home + user dir; os.environ points at them until close(). Never a real home."""

    def __init__(self, prefix):
        self.saved = {k: v for k, v in os.environ.items()
                      if k.startswith(support.ENV_PREFIXES) or k in support.GIT_IDENTITY}
        for k in self.saved:
            del os.environ[k]
        self.tmp = tempfile.mkdtemp(prefix=prefix)
        self.home = os.path.join(self.tmp, "home")
        self.user_dir = os.path.join(self.tmp, "user")
        os.makedirs(os.path.join(self.home, ".worklog"))
        os.makedirs(self.user_dir)
        os.environ.update(support.GIT_IDENTITY)
        os.environ.update(CHITRAGUPTA_HOME=self.home, CHITRAGUPTA_USER_DIR=self.user_dir,
                          CHITRAGUPTA_CLAUDE_DIR=os.path.join(self.user_dir, "claude"),
                          CHITRAGUPTA_NO_MAINTENANCE="1", CHITRAGUPTA_DASHBOARD="0", CHITRAGUPTA_NO_TASK="1")
        support.install_launcher(self.user_dir)

    def child_env(self, **extra):
        return support.child_env(self.home, self.user_dir, **extra)

    def close(self, keep=False):
        for k in [k for k in os.environ if k.startswith(support.ENV_PREFIXES) or k in support.GIT_IDENTITY]:
            del os.environ[k]
        os.environ.update(self.saved)
        if keep:
            print("kept:", self.tmp)
        else:
            support.rmtree(self.tmp)


class Checks:
    def __init__(self):
        self.results = []

    def __call__(self, cond, msg):
        self.results.append(bool(cond))
        print(("PASS " if cond else "FAIL ") + msg)
        return bool(cond)

    @property
    def ok(self):
        return all(self.results)

    def summary(self):
        print("\nRESULT: %s (%d/%d)" % ("ALL PASS" if self.ok else "FAILURES", sum(self.results), len(self.results)))
        return 0 if self.ok else 1
