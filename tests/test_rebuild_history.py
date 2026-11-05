"""A rebuild unions both histories before applying the inline cap."""
from tests.test_item_merge_order import MergeOrder, item


class RebuildHistory(MergeOrder):
    def test_restore_unites_old_and_reextracted_history(self):
        from chitragupta import rebuild
        self.merge("2026-09-09", item("Current", detail="Current."))
        current = self.only()
        current["history"] = [dict(item("New prior"), date="2026-09-05")]
        old = dict(current, history=[dict(item("Old prior"), date="2026-09-01")])
        runner = rebuild.Rebuild.__new__(rebuild.Rebuild)
        runner.store = self.st
        self.assertEqual(runner.restore({("ABC-1001", current["id"]): old}), 1)
        self.assertEqual([h["title"] for h in current["history"]], ["Old prior", "New prior"])
