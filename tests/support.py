"""Shared test helpers: an isolated environment per test, the hook launcher, tripwires.

Every test runs against a throwaway data home (CHITRAGUPTA_HOME) and a throwaway user folder
(CHITRAGUPTA_USER_DIR). Nothing here touches the network, a real `claude`, ~/.claude.json, Slack or a git remote.
All fixtures are synthetic (tickets ABC-…, hosts *.acme.test, schema `shop`).
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "chitragupta")
RUN_PY = os.path.join(ROOT, "hooks", "run.py")  # the plugin launcher Claude Code runs for every hook
STUB_MCP = os.path.join(ROOT, "tests", "stub_mcp.py")
PY = sys.executable
ENV_PREFIXES = ("CHITRAGUPTA_", "WORKLOG_")
# A synthetic git identity, so commits in temp repos never use (or need) the machine's global identity.
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@acme.test",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@acme.test"}
INTERNAL_VARS = ("CHITRAGUPTA_INTERNAL", "WORKLOG_INTERNAL")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def rmtree(path, *_):
    """shutil.rmtree that also removes read-only files (git object files on Windows); never raises."""
    def onerror(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    if path and os.path.exists(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=onerror)
        else:
            shutil.rmtree(path, onerror=onerror)


def rd(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def rdb(p):
    with open(p, "rb") as f:
        return f.read()


def wr(p, text):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_config(home, cfg):
    os.makedirs(os.path.join(home, ".worklog"), exist_ok=True)
    with open(os.path.join(home, ".worklog", "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)


def install_launcher(user_dir):
    """What setup does: the stable launcher at <user>/runtime/run.py loading runtime/<version>/chitragupta.
    Needed by anything persisted that calls the launcher (git pre-commit hook, replay.sh, the MCP guard hook)."""
    rt = os.path.join(user_dir, "runtime")
    os.makedirs(rt, exist_ok=True)
    shutil.copy(RUN_PY, os.path.join(rt, "run.py"))
    with open(os.path.join(rt, "current"), "w", encoding="utf-8") as f:
        f.write("test\n")
    dst = os.path.join(rt, "test", "chitragupta")
    if not os.path.isdir(dst):
        shutil.copytree(PACKAGE, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return os.path.join(rt, "run.py")


def child_env(home, user_dir, **extra):
    """Environment for a hook / guard subprocess: our temp folders, never an INTERNAL marker."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(ENV_PREFIXES)}
    env.update(GIT_IDENTITY)
    env.update(CHITRAGUPTA_HOME=home, CHITRAGUPTA_USER_DIR=user_dir, PYTHONDONTWRITEBYTECODE="1")
    env.update(extra)
    return env


def run_hook(event, payload, env, exe=None, raw_input=None):
    data = raw_input if raw_input is not None else json.dumps(payload).encode()
    return subprocess.run([exe or PY, "-B", RUN_PY, "hook", event], input=data, capture_output=True, env=env, timeout=30)


def clear_caches():
    from chitragupta import redact, replay
    redact._cfg_cache.clear()
    redact._public_cache.clear()
    replay._LEARNED.clear()


class Isolated:
    """Mixin: temp CHITRAGUPTA_HOME + CHITRAGUPTA_USER_DIR set in setUp, every CHITRAGUPTA_* / WORKLOG_* variable
    restored in tearDown; tripwires on everything that would leave the machine."""

    home_prefix = "cg-test-"

    def isolate(self, home=None):
        self._saved_env = {k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIXES) or k in GIT_IDENTITY}
        for k in self._saved_env:
            del os.environ[k]
        os.environ.update(GIT_IDENTITY)
        self.home = home or tempfile.mkdtemp(prefix=self.home_prefix)
        self.user_dir = tempfile.mkdtemp(prefix="cg-user-")
        self.claude_dir = os.path.join(self.user_dir, "claude")
        os.environ["CHITRAGUPTA_HOME"] = self.home
        os.environ["CHITRAGUPTA_USER_DIR"] = self.user_dir
        os.environ["CHITRAGUPTA_CLAUDE_DIR"] = self.claude_dir
        clear_caches()
        self.forbidden = []
        self._patch_tripwires()

    def _patch_tripwires(self):
        from chitragupta import mcpjobs, slack
        self._real_mcp = (mcpjobs.run, mcpjobs.claude_json, slack._run)

        def no_mcp(job, *a, **k):
            self.forbidden.append("mcpjobs.run(%s)" % job)
            raise mcpjobs.JobError("tests: MCP jobs never run", skip=True)

        def no_slack(*a, **k):
            self.forbidden.append("slack._run")
            raise RuntimeError("tests: Slack is never called")
        mcpjobs.run = no_mcp
        mcpjobs.claude_json = lambda: {}
        slack._run = no_slack

    def restore(self):
        from chitragupta import mcpjobs, slack
        mcpjobs.run, mcpjobs.claude_json, slack._run = self._real_mcp
        for k in [k for k in os.environ if k.startswith(ENV_PREFIXES) or k in GIT_IDENTITY]:
            del os.environ[k]
        os.environ.update(self._saved_env)
        clear_caches()
        for d in (self.home, self.user_dir):
            rmtree(d)
        self.assertEqual(self.forbidden, [], "something tried to reach an MCP server or Slack")

    def child_env(self, **extra):
        return child_env(self.home, self.user_dir, **extra)
