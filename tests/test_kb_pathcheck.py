"""K6a: `changed` only from Edit/Write events and commits; model-mentioned paths are verified against git (read-only)."""
import os
import subprocess
import tempfile
import unittest

from tests.support import GIT_IDENTITY, PY, Isolated, rmtree, write_config, backup_file

DAY = "2026-09-01"
T = "ABC-1001"


def git(r, *a):
    env = dict(os.environ, **GIT_IDENTITY)
    return subprocess.run(["git", "-C", r] + list(a), check=True, capture_output=True, text=True, env=env).stdout.strip()


def make_repo():
    r = os.path.join(tempfile.mkdtemp(prefix="cg-repo-"), "shop-api")
    os.makedirs(os.path.join(r, "src"))
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "commit.gpgsign", "false")
    with open(os.path.join(r, "src", "Real.java"), "w") as f:
        f.write("class Real {}\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    git(r, "checkout", "-q", "-b", "feature/%s-cancel" % T)
    with open(os.path.join(r, "src", "Branch.java"), "w") as f:
        f.write("class Branch {}\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "%s branch file" % T)
    git(r, "checkout", "-q", "main")
    return r


class PathCheck(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.repo = make_repo()
        self.addCleanup(rmtree, os.path.dirname(self.repo))
        self.cfg = {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY},
                    "watched_repos": [self.repo]}
        write_config(self.home, self.cfg)
        from chitragupta import common
        common.load_config()

    def ctx(self, **kw):
        c = {"day": DAY, "session": "abcdef123456", "time": "10:00", "repo": self.repo,
             "branch": "feature/%s-cancel" % T, "candidates": [T]}
        c.update(kw)
        return c

    def test_changed_only_from_edit_events(self):
        from chitragupta import store
        st = store.Store()
        res = {"files": [{"path": "src/Real.java", "changed": True, "ticket": T},
                         {"path": "src/Invented.java", "changed": True, "ticket": T}]}
        store.merge_extraction(st, res, self.ctx(edited=[os.path.join(self.repo, "src", "Real.java")]), ["ABC"])
        f = st.ticket(T)["files"]
        self.assertEqual((f["src/Real.java"]["changed"], f["src/Real.java"]["verified"]), (True, True))
        self.assertEqual((f["src/Invented.java"]["changed"], f["src/Invented.java"]["verified"]), (False, None))
        self.assertEqual(st.day(DAY)["tickets"][T]["files_changed"], ["src/Real.java"])

    def test_pathcheck_verifies_against_the_branch_head_and_caches(self):
        from chitragupta import common, gitro, pathcheck, render, store
        tip = git(self.repo, "rev-parse", "feature/%s-cancel" % T)
        common.write_json(common.hp("data", "git", "shop-api", "branches.json"),
                          {"feature/%s-cancel" % T: {"branch": "feature/%s-cancel" % T, "ticket": T, "tip": tip}})
        st = store.Store()
        tk = st.ticket(T)
        tk["repos"] = ["shop-api"]
        tk["files"] = {p: {"path": p, "changed": False, "verified": None, "repo": "shop-api"}
                       for p in ("src/Real.java", "src/Branch.java", "src/Invented.java")}
        st.touch_ticket(T)
        calls = []
        real = gitro.run
        self.addCleanup(setattr, gitro, "run", real)
        gitro.run = lambda cfg, repo, args, **kw: calls.append(args) or real(cfg, repo, args, **kw)
        n = pathcheck.run(st, self.cfg)
        self.assertEqual(n, 3)
        self.assertTrue(all(a[0] == "rev-parse" for a in calls))
        f = tk["files"]
        self.assertEqual([f[p]["verified"] for p in ("src/Real.java", "src/Branch.java", "src/Invented.java")],
                         [True, True, False])
        self.assertTrue(os.path.isfile(common.wl("cache", "pathcheck.json")))
        f["src/Invented.java"]["verified"] = None  # asked again: answered from the cache, no git call
        calls.clear()
        pathcheck.run(store.Store(), self.cfg)
        pathcheck.run(st, self.cfg)
        self.assertEqual(calls, [])
        self.assertFalse(f["src/Invented.java"]["verified"])
        md = render.render_ticket(st, T)
        cm = md[md.index("## Code map"):]
        self.assertIn("src/Real.java", cm)
        self.assertIn("Unverified paths (1)", cm)
        self.assertLess(cm.index("Unverified paths"), cm.index("src/Invented.java"))

    def test_budget_bounds_the_git_calls(self):
        from chitragupta import pathcheck, store
        st = store.Store()
        tk = st.ticket(T)
        tk["repos"] = ["shop-api"]
        tk["files"] = {"src/F%d.java" % i: {"path": "src/F%d.java" % i, "verified": None, "repo": "shop-api"} for i in range(5)}
        st.touch_ticket(T)
        self.assertEqual(pathcheck.run(st, self.cfg, budget=3), 3)  # the repo's HEAD (no branch tracked) + 2 paths
        self.assertEqual(sum(1 for r in tk["files"].values() if r["verified"] is None), 3)


class Migration(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        from chitragupta import common, migrate
        write_config(self.home, {"ticket_prefixes": ["ABC"]})
        cfg = common.read_json(common.wl("config.json"), {})
        cfg["chitragupta_migrations"] = [m for m in migrate.all_ids() if m != "kb5-paths-verified"]
        common.write_json(common.wl("config.json"), cfg)
        common.write_json(common.hp("data", "tickets", T + ".json"),
                          {"id": T, "files": {"src/A.java": {"path": "src/A.java", "changed": True},
                                              "src/B.java": {"path": "src/B.java", "changed": False, "verified": True}}})

    def test_marks_pending(self):
        from chitragupta import common, migrate, store
        res = migrate.run(wait_s=0)
        self.assertIn("kb5-paths-verified", res["applied"], res)
        f = common.read_json(store.ticket_path(T))["files"]
        self.assertIsNone(f["src/A.java"]["verified"])
        self.assertTrue(f["src/A.java"]["changed"])  # kept; the path check decides whether it is shown
        self.assertTrue(f["src/B.java"]["verified"])
        self.assertTrue(os.path.isfile(backup_file(res["backups"], "kb5-paths-verified", "data", "tickets", T + ".json")))


if __name__ == "__main__":
    unittest.main()
