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


if __name__ == "__main__":
    unittest.main()
