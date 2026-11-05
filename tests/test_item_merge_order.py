"""Gate 2: re-extracting an OLDER session never overwrites a NEWER item's text.

A retried session from an earlier day came back after later days and its "updates" replaced the newer text, which
moved to history. Now an older incoming version only joins the history (and its evidence is kept); a newer one
updates as before. Synthetic data only (ABC-1001).
"""
import unittest

from tests.support import PY, Isolated, write_config

T = "ABC-1001"


def ctx(day, sid="abcdef1234567890"):
    return {"day": day, "session": sid, "time": "10:00", "repo": None, "branch": None, "candidates": [T],
            "default": T, "turns": [("09:00", T, "prompt", 1)]}


def item(title, time="10:00", **over):
    it = {"kind": "finding", "ticket": T, "title": title, "detail": title + " (detail)", "evidence": [title[:12]],
          "confidence": "likely", "time": time, "updates": None, "phase": None, "line": None}
    it.update(over)
    return it


class MergeOrder(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY}})
        from chitragupta import store
        self.st = store.Store()

    def merge(self, day, it):
        from chitragupta import store
        store.merge_extraction(self.st, {"items": [it]}, ctx(day), ["ABC"])

    def only(self):
        items = list(self.st.ticket(T)["items"].values())
        self.assertEqual(len(items), 1)
        return items[0]

    def test_older_update_after_a_newer_one_keeps_the_newer_text(self):
        self.merge("2026-09-01", item("Fix is not on qa"))
        iid = self.only()["id"]
        self.merge("2026-09-05", item("Fix is on qa", confidence="confirmed", updates=iid))
        self.merge("2026-09-02", item("Fix is still not on qa", updates=iid))  # a retried older session
        it = self.only()
        self.assertEqual((it["title"], it["detail"], it["confidence"], it["date"]),
                         ("Fix is on qa", "Fix is on qa (detail)", "confirmed", "2026-09-05"))
        hist = [h["title"] for h in it["history"]]
        self.assertIn("Fix is still not on qa", hist)
        self.assertIn("Fix is not on qa", hist)
        self.assertIn("Fix is still", it["evidence"])

    def test_a_newer_update_still_replaces_the_text(self):
        self.merge("2026-09-01", item("Fix is not on qa"))
        iid = self.only()["id"]
        self.merge("2026-09-01", item("Fix is on qa", time="11:30", updates=iid))
        it = self.only()
        self.assertEqual((it["title"], it["time"]), ("Fix is on qa", "11:30"))
        self.assertEqual([h["title"] for h in it["history"]], ["Fix is not on qa"])

    def test_same_item_again_from_an_older_session_keeps_the_newer_detail(self):
        self.merge("2026-09-05", item("Cancel skips release", detail="Short, newer."))
        self.merge("2026-09-01", item("Cancel skips release", detail="A much longer and older description of it."))
        self.assertEqual(self.only()["detail"], "Short, newer.")


if __name__ == "__main__":
    unittest.main()
