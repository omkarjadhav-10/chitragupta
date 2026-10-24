"""Item history retains full prior text and raw provenance without a five-version cap."""
import unittest
from tests.test_item_merge_order import MergeOrder, item


class History(MergeOrder):
    def test_older_update_keeps_its_detail_and_ref(self):
        self.merge("2026-09-05", item("Fix is on qa", detail="Newer.", line=5))
        iid = self.only()["id"]
        self.merge("2026-09-02", item("Fix not on qa", detail="Older evidence.", line=2, updates=iid))
        self.assertTrue(any(h.get("detail") == "Older evidence." and h.get("ref", "").endswith(":2")
                            for h in self.only().get("history", [])))

    def test_same_id_older_retry_is_preserved_and_history_keeps_all_versions(self):
        self.merge("2026-09-09", item("Cancel skips release", detail="Current.", line=9))
        for n in range(1, 8):
            self.merge("2026-09-%02d" % n, item("Cancel skips release", detail="Historical %d." % n, line=n))
        h = self.only().get("history", [])
        self.assertEqual(len(h), 7)
        self.assertEqual({v.get("detail") for v in h}, {"Historical %d." % n for n in range(1, 8)})
        self.assertTrue(all(v.get("ref") and v.get("session") and v.get("time") for v in h))

    def test_replacing_a_version_keeps_its_full_detail(self):
        self.merge("2026-09-01", item("First cause", detail="Initial reasoning.", line=1))
        iid = self.only()["id"]
        self.merge("2026-09-05", item("Correct cause", detail="Correction.", line=5, updates=iid))
        self.assertEqual(self.only()["history"][0].get("detail"), "Initial reasoning.")
        self.assertNotIn("history", self.only()["history"][0])
