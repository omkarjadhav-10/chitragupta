"""Capture scope: `.worklog-ignore` in a folder above a repo, learning only from captured work, the transcript
safety net under the hook's rules, and artifacts never filed from ignored or Chitragupta-owned folders.

Synthetic temp folders only (tickets ABC-…, DEMO-…).
"""
import json
import os
import tempfile
import unittest

from tests.support import PY, Isolated, rd, rmtree, run_hook, write_config


def _repo(parent, name, branch="feature/DEMO-1-selftest"):
    d = os.path.join(parent, name)
    os.makedirs(os.path.join(d, ".git"))
    with open(os.path.join(d, ".git", "HEAD"), "w") as f:
        f.write("ref: refs/heads/%s\n" % branch)
    return d


def _touch(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").close()


class ScopeCase(Isolated, unittest.TestCase):
    def setUp(self):
        h = tempfile.mkdtemp(prefix="cg-test-")
        write_config(h, {"ticket_prefixes": ["DEMO"], "capture_scope": {"include_paths": [], "exclude_paths": []},
                         "paths": {"python": PY}})
        self.isolate(h)
        self.work = tempfile.mkdtemp(prefix="cg-work-")
        self.addCleanup(rmtree, self.work)

    def tearDown(self):
        self.restore()

    def raw_lines(self):
        out = []
        for dp, _, fs in os.walk(os.path.join(self.home, ".worklog", "raw")):
            for f in fs:
                out += rd(os.path.join(dp, f)).splitlines()
        return out


# --------------------------------------------------------------------------- FX-8 (a) ignore marker above the repo

class IgnoreAncestorTests(ScopeCase):
    def test_nested_repo_inside_ignored_checkout_is_ignored(self):
        from chitragupta import common
        checkout = _repo(self.work, "checkout")
        _touch(os.path.join(checkout, ".worklog-ignore"))
        nested = _repo(checkout, ".dev-home")  # its own git repo, inside the ignored checkout
        sub = os.path.join(nested, "a", "b")
        os.makedirs(sub)
        self.assertTrue(common.repo_ignored(checkout))
        self.assertTrue(common.repo_ignored(nested))
        self.assertTrue(common.path_ignored(sub))
        self.assertFalse(common.repo_ignored(_repo(self.work, "other")))

    def test_hook_skips_nested_repo(self):
        checkout = _repo(self.work, "checkout")
        _touch(os.path.join(checkout, ".worklog-ignore"))
        nested = _repo(checkout, ".dev-home")
        env = self.child_env()
        run_hook("user-prompt", {"session_id": "s1", "cwd": nested, "user_prompt": "DEMO-1 private"}, env)
        run_hook("user-prompt", {"session_id": "s1", "cwd": os.path.join(checkout, "docs"), "user_prompt": "x"}, env)
        self.assertEqual(self.raw_lines(), [])
        ok = _repo(self.work, "shop-api")
        run_hook("user-prompt", {"session_id": "s2", "cwd": ok, "user_prompt": "x"}, env)
        self.assertEqual(len(self.raw_lines()), 1)

    def test_walk_stops_at_the_home_folder(self):
        from chitragupta import common
        top = os.path.join(self.work, "top")
        user_home = os.path.join(top, "me")
        repo = _repo(user_home, "shop-api")
        _touch(os.path.join(top, ".worklog-ignore"))  # above the home folder: not honoured
        saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
        os.environ["USERPROFILE"] = os.environ["HOME"] = user_home
        try:
            self.assertFalse(common.path_ignored(repo))
            _touch(os.path.join(user_home, ".worklog-ignore"))  # at the home folder: honoured
            self.assertTrue(common.path_ignored(repo))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# --------------------------------------------------------------------------- FX-8 (b) learn only from captured work

class LearnTests(ScopeCase):
    def _agent(self):
        from chitragupta import agent
        return agent.Agent()

    def _ev(self, repo, branch="feature/XYZ-1001-demo"):
        return {"event": "user-prompt", "ts": "2026-10-14T10:00:00+05:30", "session_id": "s1",
                "cwd": repo, "repo_root": repo, "branch": branch, "payload": {}}

    def test_never_learns_from_temp_ignored_or_own_folders(self):
        from unittest import mock
        from chitragupta import common
        scratch = os.path.join(self.work, "tmp")  # stands in for the system temp folder
        os.makedirs(scratch)
        checkout = _repo(self.work, "checkout")
        _touch(os.path.join(checkout, ".worklog-ignore"))
        bad = [_repo(scratch, "scratch-repo"),                                 # under the temp folder
               _repo(checkout, ".dev-home"),                                    # inside an ignored folder
               _repo(os.path.join(self.home, ".worklog"), "slack-sender"),     # inside the data home
               _repo(self.user_dir, "runtime-copy")]                            # Chitragupta's own folder
        with mock.patch.object(common, "temp_dirs", lambda: [scratch]):
            a = self._agent()
            for r in bad:
                a._learn(self._ev(r))
            self.assertNotIn("XYZ", a.cfg.get("ticket_prefixes"))
            self.assertEqual(a.cfg.get("watched_repos") or [], [])
            self.assertFalse(a.cfg_dirty)
            good = _repo(self.work, "shop-api")
            a._learn(self._ev(good))
            self.assertIn("XYZ", a.cfg["ticket_prefixes"])
            self.assertEqual([common.norm_path(r) for r in a.cfg["watched_repos"]], [common.norm_path(good)])

    def test_outside_capture_scope_teaches_nothing(self):
        from unittest import mock
        from chitragupta import common
        repo = _repo(self.work, "shop-api")
        with mock.patch.object(common, "temp_dirs", lambda: []):
            a = self._agent()
            a.cfg["capture_scope"] = {"include_paths": [], "exclude_paths": [self.work]}
            a._learn(self._ev(repo))
            self.assertNotIn("XYZ", a.cfg.get("ticket_prefixes"))
            self.assertEqual(a.cfg.get("watched_repos") or [], [])

    def test_temp_dirs_include_the_system_temp_folder(self):
        from chitragupta import common
        self.assertIn(common.norm_path(tempfile.gettempdir()), [common.norm_path(p) for p in common.temp_dirs()])
        self.assertFalse(common.learnable_path(_repo(self.work, "in-temp"), common.load_config()))


# --------------------------------------------------------------------------- FX-8 (c) the transcript safety net

class SafetyNetTests(ScopeCase):
    def setUp(self):
        super().setUp()
        self.tdir = os.path.join(self.work, "transcripts")
        os.environ["CHITRAGUPTA_TRANSCRIPTS_DIR"] = self.tdir
        self.n = 0

    def transcript(self, cwd, prompt="look at DEMO-7 please", branch=None):
        """A Claude Code transcript the hooks never saw, old enough for the safety net to read."""
        import time
        from chitragupta import common
        self.n += 1
        sid = "sess-%d" % self.n
        d = os.path.join(self.tdir, "proj-%d" % self.n)
        os.makedirs(d)
        ts = common.now_iso()
        recs = [{"type": "user", "cwd": cwd, "sessionId": sid, "timestamp": ts, "gitBranch": branch,
                 "message": {"role": "user", "content": prompt}},
                {"type": "assistant", "cwd": cwd, "sessionId": sid, "timestamp": ts, "gitBranch": branch,
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "SECRET_CONTENT answer"}]}}]
        p = os.path.join(d, sid + ".jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))
        old = time.time() - 600
        os.utime(p, (old, old))
        return sid

    def run_net(self):
        from chitragupta import agent
        a = agent.Agent()
        a.state["transcripts_baselined"] = True
        a.safety_net(False)
        return a

    def rows(self):
        return [json.loads(l) for l in self.raw_lines()]

    def test_home_folder_session_is_not_recovered(self):
        user_home = os.path.join(self.work, "me")  # the home folder itself: not a repo, not an included folder
        os.makedirs(user_home)
        self.transcript(user_home)
        self.run_net()
        self.assertEqual(self.rows(), [])

    def test_ignored_excluded_and_own_folders_are_skipped(self):
        checkout = _repo(self.work, "checkout")
        _touch(os.path.join(checkout, ".worklog-ignore"))
        self.transcript(_repo(checkout, ".dev-home"))
        excluded = _repo(self.work, "excluded")
        cfg_path = os.path.join(self.home, ".worklog", "config.json")
        cfg = json.loads(rd(cfg_path))
        cfg["capture_scope"]["exclude_paths"] = [excluded]
        write_config(self.home, cfg)
        self.transcript(excluded)
        self.transcript(_repo(self.user_dir, "runtime-copy"))
        self.run_net()
        self.assertEqual(self.rows(), [])

    def test_data_home_session_is_time_only(self):
        self.transcript(self.home)
        self.run_net()
        rows = self.rows()
        self.assertTrue(rows)
        self.assertTrue(all(r.get("source") == "time-only" and "payload" not in r for r in rows), rows)
        self.assertNotIn("SECRET_CONTENT", "".join(self.raw_lines()))
        self.assertIn("DEMO-7", [r.get("ticket") for r in rows])

    def test_hook_recorded_time_only_session_is_not_recorded_twice(self):
        sid = self.transcript(self.home)
        from chitragupta import common
        tdir = os.path.join(self.home, ".worklog", "raw", common.workday(common.now(), common.load_config()), "time")
        os.makedirs(tdir)
        with open(os.path.join(tdir, sid + ".jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": common.now_iso(), "event": "user-prompt", "session_id": sid,
                                "source": "time-only", "ticket": None}) + "\n")
        self.run_net()
        self.assertEqual(len(self.rows()), 1)

    def test_repo_session_is_recovered(self):
        repo = _repo(self.work, "shop-api")
        self.transcript(repo, branch="feature/DEMO-1-selftest")
        self.run_net()
        rows = self.rows()
        self.assertEqual({r["source"] for r in rows}, {"transcript"})
        self.assertEqual({r["ticket"] for r in rows}, {"DEMO-1"})


# --------------------------------------------------------------------------- FX-8 (d) artifacts

class ArtifactScopeTests(ScopeCase):
    def _file(self, path, text="# Notes\n\n" + "analysis line\n" * 10):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_excluded_covers_ignored_and_own_folders(self):
        from chitragupta import artifacts, common
        checkout = _repo(self.work, "checkout")
        _touch(os.path.join(checkout, ".worklog-ignore"))
        cfg = common.load_config()
        for p in (os.path.join(checkout, "src", "deep", "notes.md"),            # in an ignored checkout
                  os.path.join(checkout, ".dev-home", "tickets", "plan.md"),     # nested repo inside it
                  os.path.join(self.home, "knowledge", "plan.md"),               # the data home
                  os.path.join(self.user_dir, "runtime", "x", "notes.md"),       # runtime / settings folder
                  os.path.join(os.path.dirname(common.PACKAGE_DIR), "docs", "notes.md")):  # the plugin itself
            self.assertTrue(artifacts.excluded(p, cfg), p)
        self.assertFalse(artifacts.excluded(os.path.join(self.work, "shop-api", "analysis.md"), cfg))

    def test_session_edits_in_an_ignored_checkout_are_not_filed(self):
        from chitragupta import agent, common
        checkout = _repo(self.work, "checkout")
        _touch(os.path.join(checkout, ".worklog-ignore"))
        bad = self._file(os.path.join(checkout, "chitragupta", "notes.md"))
        good = self._file(os.path.join(self.work, "out", "analysis.md"))
        a = agent.Agent()
        day, ts = common.workday(common.now(), a.cfg), common.now_iso()
        for p in (bad, good):  # as queued by a session tagged DEMO-1 working from another folder
            a.features.queue_artifact(p, "DEMO-1", day, "s1", ts, None, None, ts)
        a.features.process_artifacts()
        srcs = [r.get("source") for r in (a.store.ticket("DEMO-1").get("artifacts") or {}).values()]
        self.assertEqual([common.norm_path(s) for s in srcs], [common.norm_path(good)])

if __name__ == "__main__":
    unittest.main()
