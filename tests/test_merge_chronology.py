"""Retry order keeps the newest stable-id version, including its timestamp."""
import unittest
from tests.test_item_merge_order import MergeOrder, item


class Chronology(MergeOrder):
    def test_same_id_versions_advance_the_timestamp_before_an_older_retry(self):
        self.merge("2026-09-01", item("Cancel skips release", detail="Initial."))
        self.merge("2026-09-05", item("Cancel skips release", detail="Newer finding with enough detail.", line=5))
        self.merge("2026-09-02", item("Cancel skips release", detail="An older finding with a much longer explanation."))
        self.assertEqual(self.only()["detail"], "Newer finding with enough detail.")
        self.assertEqual(self.only()["date"], "2026-09-05")
        self.assertTrue(self.only()["ref"].endswith(":5"))

    def test_newer_shorter_detail_wins_and_a_same_day_retry_does_not(self):
        self.merge("2026-09-01", item("Cancel skips release", detail="An earlier long explanation.", time="09:00"))
        self.merge("2026-09-01", item("Cancel skips release", detail="Newer.", time="12:00", line=12))
        self.merge("2026-09-01", item("Cancel skips release", detail="Retried old explanation with more words.", time="10:00"))
        self.assertEqual((self.only()["detail"], self.only()["time"]), ("Newer.", "12:00"))
