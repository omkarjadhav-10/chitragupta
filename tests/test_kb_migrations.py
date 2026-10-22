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


class RawRelativeKeysTests(Isolated, unittest.TestCase):
    """O4: state.sessions.*.files keyed "<day>/<sid>.jsonl", so a copied or moved home never re-extracts its raw."""
    D = "2026-10-14"

    def setUp(self):
        self.isolate()
        import json
        from unittest import mock
        from chitragupta import common, extract, migrate
        common.write_json(common.wl("config.json"), {"ticket_prefixes": ["ABC"],
                                                     "chitragupta_migrations": list(migrate.MIGRATIONS)})
        self.calls = []
        p = mock.patch.object(extract, "call_claude", lambda *a, **k: self.calls.append(a[0]) or ({}, 0.0))
        p.start()
        self.addCleanup(p.stop)
        self.raw = common.wl("raw", self.D, "s1.jsonl")
        os.makedirs(os.path.dirname(self.raw))
        with open(self.raw, "w", encoding="utf-8") as f:
            for i, (ev, pl) in enumerate((("user-prompt", {"prompt": "look at ABC-1001 " + "x" * 2000}),
                                          ("stop", {"message": "y" * 2000}))):
                f.write(json.dumps({"ts": "%sT10:0%d:00+05:30" % (self.D, i), "event": ev, "session_id": "s1",
                                    "branch": "fix/ABC-1001-x", "payload": pl}) + "\n")
        self.size = os.path.getsize(self.raw)

    def test_copied_home_finds_nothing_new(self):
        from chitragupta import agent, common, migrate
        done = {"read": self.size, "extracted": self.size}
        common.write_json(common.wl("state.json"), {"sessions": {"s1": {
            "files": {"C:\\Users\\someone\\OldHome\\.worklog\\raw\\%s\\s1.jsonl" % self.D: done,
                      "/mnt/backup/home/.worklog/raw/%s/s1.jsonl" % self.D: {"read": 10, "extracted": 5}},
            "pending_chars": 0, "days": [self.D], "source": "hook"}}})
        res = migrate.run(wait_s=0)
        self.assertIn("raw-relative-keys", res.get("applied") or [], res)
        with open(os.path.join(res["backups"][-1], ".worklog", "state.json"), encoding="utf-8") as f:
            self.assertIn("OldHome", f.read())  # the old state was backed up first
        files = common.read_json(common.wl("state.json"))["sessions"]["s1"]["files"]
        self.assertEqual(files, {"%s/s1.jsonl" % self.D: done})  # duplicates merged by the larger offsets
        a = agent.Agent()
        a.ingest()
        a.run_extractions(any_size=True)
        self.assertEqual(self.calls, [])
        self.assertEqual(a.state["sessions"]["s1"]["pending_chars"], 0)
        self.assertEqual(list(a.state["sessions"]["s1"]["files"]), ["%s/s1.jsonl" % self.D])

    def test_new_state_uses_relative_keys(self):
        from chitragupta import agent
        a = agent.Agent()
        a.ingest()
        a.run_extractions(any_size=True)
        self.assertEqual(self.calls, ["extract"])
        self.assertEqual(list(a.state["sessions"]["s1"]["files"]), ["%s/s1.jsonl" % self.D])


if __name__ == "__main__":
    unittest.main()
