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

    def test_newer_shorter_detail_keeps_rich_text_and_advances_time(self):
        self.merge("2026-09-01", item("Cancel skips release", detail="An earlier long explanation.", time="09:00"))
        self.merge("2026-09-01", item("Cancel skips release", detail="Newer.", time="12:00", line=12))
        self.merge("2026-09-01", item("Cancel skips release", detail="Retried old explanation with more words.", time="10:00"))
        self.assertEqual((self.only()["detail"], self.only()["time"]), ("An earlier long explanation.", "12:00"))

    def test_newer_remention_does_not_raise_confidence(self):
        self.merge("2026-09-01", item("Cancel skips release", detail="An explanation.", confidence="hypothesis"))
        self.merge("2026-09-02", item("Cancel skips release", detail="An explanation.", confidence="confirmed"))
        self.assertEqual(self.only()["confidence"], "hypothesis")

    def test_explicit_identical_update_is_not_counted(self):
        self.merge("2026-09-01", item("Cancel skips release"))
        iid = self.only()["id"]
        self.st.delta.clear()
        self.merge("2026-09-02", item("Cancel skips release", updates=iid))
        self.assertEqual(self.st.delta.get("ABC-1001", {}).get("updates", 0), 0)
        self.assertFalse(self.only().get("history"))

    def test_whitespace_and_case_do_not_invalidate_the_brief(self):
        from chitragupta import brief
        self.merge("2026-09-01", item("Cancel skips release", detail="The row stays reserved."))
        tk = self.st.ticket("ABC-1001")
        before = brief.source_hash("ABC-1001", tk, card={})
        self.only()["detail"] = " THE  row stays reserved. "
        self.assertEqual(before, brief.source_hash("ABC-1001", tk, card={}))
