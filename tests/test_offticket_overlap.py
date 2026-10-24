"""Gate 2: "names another ticket" flags an item only when it shares nothing with this ticket.

An item that says how this ticket relates to another one ("ABC-1002's commit to shop.orders is the cancel fix") was
hidden from every current view because it named the other key. Rule (a) now also needs no shared file, table,
endpoint or title word; the migration kb5-review-recheck clears flags that would no longer be set (backing up the
ticket files first) and never touches an item you resolved. Synthetic data only (ABC-…, schema `shop`).
"""
import json
import os
import unittest

from tests.support import Isolated, write_config
from tests.test_kb_offticket import Base, T


class Overlap(Base):
    def test_naming_another_ticket_but_sharing_a_table_is_not_flagged(self):
        it = self.merge("ABC-1002's commit to shop.orders is the cancel fix")
        self.assertFalse(it.get("review"))

    def test_relation_sharing_a_title_word_is_not_flagged(self):
        it = self.merge("The same stock fix is already on qa via ABC-1002")
        self.assertFalse(it.get("review"))

    def test_naming_another_ticket_and_sharing_nothing_is_still_flagged(self):
        it = self.merge("ABC-1002: login lockout reset never clears failed_attempts")
        self.assertEqual(it["review"]["suggest"], "ABC-1002")


class Recheck(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"]})
        from chitragupta import common, store
        rv = {"why": "names ABC-1002, not ABC-1001", "suggest": "ABC-1002", "at": "2026-09-01T10:00:00"}

        def it(iid, title, **kw):
            d = {"id": iid, "kind": "finding", "title": title, "detail": "", "evidence": [], "date": "2026-09-01",
                 "time": "10:00", "session": "abcdef12", "src": "branch"}
            d.update(kw)
            return d
        items = [it("i000000001", "Cancel keeps the reservation row in shop.orders"),
                 it("i000000002", "ABC-1002's commit to shop.orders is the cancel fix", review=dict(rv)),
                 it("i000000003", "ABC-1002: login lockout reset never clears failed_attempts", review=dict(rv)),
                 it("i000000004", "ABC-1002 cancel note you kept", review=None, review_kept="2026-09-02T09:00:00"),
                 it("i000000005", "ABC-1002 cancel note you moved", review=dict(rv),
                    archived={"at": "2026-09-02T09:00:00", "reason": "moved to ABC-1002"})]
        tk = {"id": T, "title": "Cancel order keeps stock reserved", "title_source": "jira",
              "items": {i["id"]: i for i in items}, "tables": {"shop.orders": {"name": "shop.orders"}}}
        common.write_json(store.ticket_path(T), tk)
        self.before = json.dumps(tk, sort_keys=True)

    def run_step(self, dry_run=False):
        from chitragupta import kb5
        backups, notes = [], []
        kb5.review_recheck(dry_run, notes, backups.append)
        return backups, notes

    def saved(self):
        from chitragupta import common, store
        return common.read_json(store.ticket_path(T))["items"]

    def test_clears_only_flags_that_no_longer_hold_after_a_backup(self):
        from chitragupta import store
        backups, notes = self.run_step(dry_run=True)
        self.assertEqual(json.dumps(json.loads(open(store.ticket_path(T), encoding="utf-8").read()), sort_keys=True),
                         self.before, "a dry run writes nothing")
        backups, notes = self.run_step()
        self.assertEqual(backups, [[store.ticket_path(T)]])
        self.assertTrue(notes)
        items = self.saved()
        self.assertIsNone(items["i000000002"]["review"])
        self.assertTrue(items["i000000002"]["review_cleared"]["at"])
        self.assertEqual(items["i000000003"]["review"]["suggest"], "ABC-1002")
        self.assertEqual(items["i000000004"]["review_kept"], "2026-09-02T09:00:00")
        self.assertNotIn("review_cleared", items["i000000004"])
        self.assertEqual(items["i000000005"]["review"]["suggest"], "ABC-1002", "an item you moved is left alone")
        self.assertEqual(len(items), 5, "nothing deleted")

    def test_registered_before_the_schema_version_step(self):
        from chitragupta import migrate
        ids = [s[0] for s in migrate.KB_STEPS]
        self.assertIn(("kb5-review-recheck", "kb5:review_recheck"), migrate.KB_STEPS)
        self.assertLess(ids.index("kb5-review-recheck"), ids.index("kb5-schema-version"))


if __name__ == "__main__":
    unittest.main()
