"""Migrations for the knowledge fixes (0.5.0): the backup they all take first."""
import os
import unittest

from tests.support import Isolated


class BackupTreeTests(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()

    def write(self, *parts, text="x"):
        p = os.path.join(self.home, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_same_names_in_different_folders_never_collide(self):
        from chitragupta import migrate
        a = self.write("data", "tickets", "ABC-1001.json", text="ticket")
        b = self.write("data", "days", "2026", "ABC-1001.json", text="day")
        self.write("tickets", "ABC-1001", "README.md", text="hub")
        dest = migrate._backup_tree([a, b, os.path.join(self.home, "tickets"), os.path.join(self.home, "nope")], "kb")
        self.assertTrue(os.path.basename(dest).startswith("chitragupta-kb-"))
        self.assertTrue(dest.startswith(os.path.join(self.home, ".worklog", "backups")))

        def read(*parts):
            with open(os.path.join(dest, *parts), encoding="utf-8") as f:
                return f.read()
        self.assertEqual(read("data", "tickets", "ABC-1001.json"), "ticket")
        self.assertEqual(read("data", "days", "2026", "ABC-1001.json"), "day")
        self.assertEqual(read("tickets", "ABC-1001", "README.md"), "hub")

    def test_two_backups_in_the_same_second_get_their_own_folder(self):
        from chitragupta import migrate
        a = self.write("data", "meta.json")
        self.assertNotEqual(migrate._backup_tree([a], "kb"), migrate._backup_tree([a], "kb"))


CALLS = []


def step_ok(dry_run, notes, backup):
    from chitragupta import common
    p = common.hp("data", "x.json")
    backup([p])
    CALLS.append(("ok", dry_run))
    notes.append("x upgraded")
    if not dry_run:
        common.write_json(p, {"v": 2})


def step_boom(dry_run, notes, backup):
    CALLS.append(("boom", dry_run))
    raise ValueError("bad data")


class StepTests(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        from chitragupta import common, migrate
        common.write_json(common.wl("config.json"), {"chitragupta_migrations": list(migrate.MIGRATIONS)})
        common.write_json(common.hp("data", "x.json"), {"v": 1})
        self._steps = list(migrate.KB_STEPS)
        self.addCleanup(lambda: migrate.KB_STEPS.__setitem__(slice(None), self._steps))
        CALLS.clear()

    def cfg(self):
        from chitragupta import common
        return common.read_json(common.wl("config.json"), {})

    def test_backup_first_then_marked_once(self):
        from chitragupta import common, migrate
        migrate.KB_STEPS[:] = [("kb-test-ok", "tests.test_kb_migrations:step_ok")]
        dry = migrate.run(dry_run=True)
        self.assertEqual(dry["applied"], ["kb-test-ok"])
        self.assertEqual(common.read_json(common.hp("data", "x.json"), {}), {"v": 1})
        self.assertNotIn("kb-test-ok", self.cfg()["chitragupta_migrations"])
        res = migrate.run(wait_s=0)
        self.assertEqual(res["applied"], ["kb-test-ok"])
        self.assertEqual(common.read_json(common.hp("data", "x.json"), {}), {"v": 2})
        with open(os.path.join(res["backups"][0], "data", "x.json"), encoding="utf-8") as f:
            self.assertIn('"v": 1', f.read())
        self.assertIn("kb-test-ok", self.cfg()["chitragupta_migrations"])
        self.assertTrue(migrate.run(wait_s=0)["already"])
        self.assertIsNone(migrate.run_in_tick())
        self.assertEqual(CALLS, [("ok", True), ("ok", False)])

    def test_a_failing_step_stays_pending_and_stops_later_ones(self):
        from chitragupta import migrate
        migrate.KB_STEPS[:] = [("kb-test-boom", "tests.test_kb_migrations:step_boom"),
                               ("kb-test-ok", "tests.test_kb_migrations:step_ok")]
        res = migrate.run(wait_s=0)
        self.assertIn("bad data", res["error"])
        self.assertEqual(CALLS, [("boom", False)])
        done = self.cfg()["chitragupta_migrations"]
        self.assertNotIn("kb-test-boom", done)
        self.assertNotIn("kb-test-ok", done)


if __name__ == "__main__":
    unittest.main()
