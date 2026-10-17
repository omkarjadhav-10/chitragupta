"""Stable runtime: copy, launcher, `current`, pruning, never-downgrade. Temp user folder only."""
import os
import subprocess
import tempfile
import unittest

from tests.support import PY, Isolated, rd, rmtree, wr


def fake_plugin(root, version, extra=None):
    """<root>/chitragupta (version), <root>/hooks/run.py, <root>/user-skill/chitragupta/SKILL.md, a __pycache__."""
    pkg = os.path.join(root, "chitragupta")
    os.makedirs(os.path.join(pkg, "__pycache__"))
    os.makedirs(os.path.join(root, "hooks"))
    os.makedirs(os.path.join(root, "user-skill", "chitragupta"))
    wr(os.path.join(pkg, "__init__.py"), '__version__ = "%s"\n' % version)
    wr(os.path.join(pkg, "mod.py"), extra or "X = 1\n")
    wr(os.path.join(pkg, "__pycache__", "mod.cpython-313.pyc"), "junk")
    wr(os.path.join(root, "hooks", "run.py"), "# launcher %s\n" % version)
    wr(os.path.join(root, "user-skill", "chitragupta", "SKILL.md"), "---\nname: chitragupta\n---\n")
    return pkg


class RuntimeTests(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        from chitragupta import runtime
        self.rt = runtime
        self.src = tempfile.mkdtemp(prefix="cg-plugin-")
        self.addCleanup(rmtree, self.src)

    def tearDown(self):
        self.restore()

    def rtp(self, *p):
        return os.path.join(self.user_dir, "runtime", *p)

    def test_install_copies_package_launcher_skill_and_current(self):
        pkg = fake_plugin(self.src, "0.2.0")
        res = self.rt.install(pkg)
        self.assertTrue(res["changed"])
        self.assertEqual(rd(self.rtp("current")).strip(), "0.2.0")
        self.assertEqual(rd(self.rtp("run.py")), "# launcher 0.2.0\n")
        self.assertTrue(os.path.isfile(self.rtp("0.2.0", "chitragupta", "mod.py")))
        self.assertTrue(os.path.isfile(self.rtp("0.2.0", "user-skill", "chitragupta", "SKILL.md")))
        self.assertFalse(os.path.exists(self.rtp("0.2.0", "chitragupta", "__pycache__")))
        again = self.rt.install(pkg)  # idempotent
        self.assertFalse(again["changed"])
        wr(os.path.join(pkg, "mod.py"), "X = 2\n")  # same version, new content -> recopied
        third = self.rt.install(pkg)
        self.assertTrue(third["copied"])
        self.assertEqual(rd(self.rtp("0.2.0", "chitragupta", "mod.py")), "X = 2\n")
        self.assertEqual([d for d in os.listdir(self.rtp()) if d.startswith(".")], [])  # no temp leftovers

    def test_keeps_newest_two(self):
        for v in ("0.0.1", "0.0.3", "0.0.2", "test"):
            os.makedirs(self.rtp(v, "chitragupta"))
            wr(self.rtp(v, "chitragupta", "__init__.py"), "")
        wr(self.rtp("current"), "0.0.3\n")
        self.rt.install(fake_plugin(self.src, "0.2.0"))
        self.assertEqual(sorted(self.rt.versions()), ["0.0.3", "0.2.0"])

    def test_never_downgrades_unless_forced(self):
        newer = tempfile.mkdtemp(prefix="cg-plugin-")
        self.addCleanup(rmtree, newer)
        self.rt.install(fake_plugin(newer, "9.0.0"))
        res = self.rt.install(fake_plugin(self.src, "0.2.0"))
        self.assertTrue(res.get("kept_newer"))
        self.assertEqual(rd(self.rtp("current")).strip(), "9.0.0")
        self.rt.install(os.path.join(self.src, "chitragupta"), force=True)
        self.assertEqual(rd(self.rtp("current")).strip(), "0.2.0")

    def test_needs_refresh(self):
        self.assertTrue(self.rt.needs_refresh())  # nothing installed
        self.rt.install()
        self.assertFalse(self.rt.needs_refresh())
        wr(self.rtp("current"), "test\n")  # a test / dev copy is older than any release
        self.assertTrue(self.rt.needs_refresh())
        wr(self.rtp("current"), "99.0.0\n")
        os.makedirs(self.rtp("99.0.0", "chitragupta"))
        self.assertFalse(self.rt.needs_refresh())

    def test_install_from_a_runtime_copy_keeps_its_launcher(self):
        self.rt.install(fake_plugin(self.src, "0.2.0"))
        res = self.rt.install(self.rtp("0.2.0", "chitragupta"))
        self.assertFalse(res["changed"])
        self.assertEqual(rd(self.rtp("run.py")), "# launcher 0.2.0\n")

    def test_real_package_runs_from_the_runtime(self):
        res = self.rt.install()
        self.assertTrue(os.path.isfile(self.rtp(res["version"], "chitragupta", "__main__.py")))
        p = subprocess.run([PY, "-B", self.rtp("run.py"), "migrate", "--dry-run"], capture_output=True, text=True,
                           env=self.child_env(), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"dry_run": true', p.stdout)


if __name__ == "__main__":
    unittest.main()
