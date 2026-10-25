"""K8 applies to record content before it can become its own overlap evidence."""
from tests.test_kb_offticket import Base, T, ctx


class RecordReview(Base):
    def merge_records(self, src="branch"):
        from chitragupta import store
        result = {"queries": [{"text": "select id from shop.sessions", "purpose": "ABC-1002 login lockout"}],
                  "tables": [{"name": "shop.sessions", "purpose": "ABC-1002 login lockout"}],
                  "apis": [{"method": "GET", "path": "/sessions", "purpose": "ABC-1002 login lockout"}]}
        store.merge_extraction(self.st, result, ctx(src), ["ABC"])
        return [next(iter(self.st.ticket(T)[k].values())) for k in ("queries", "apis")] + [self.st.ticket(T)["tables"]["shop.sessions"]]

    def test_all_three_kinds_are_flagged_without_moving_them(self):
        for rec in self.merge_records():
            self.assertEqual(rec["review"]["suggest"], "ABC-1002")
        self.assertFalse(self.st.ticket("ABC-1002")["queries"])

    def test_repeat_cannot_turn_a_record_into_its_own_overlap_evidence(self):
        self.merge_records()
        for rec in self.merge_records():
            self.assertTrue(rec.get("review"))

    def test_owner_keep_survives_repeat(self):
        for rec in self.merge_records():
            rec.update(review=None, review_kept="2026-09-01")
        for rec in self.merge_records():
            self.assertFalse(rec.get("review"))
            self.assertEqual(rec["review_kept"], "2026-09-01")

    def test_related_record_is_kept(self):
        from chitragupta import store
        store.merge_extraction(self.st, {"queries": [{"text": "select id from shop.orders", "purpose": "ABC-1002 stock fix"}]}, ctx(), ["ABC"])
        self.assertFalse(next(iter(self.st.ticket(T)["queries"].values())).get("review"))

    def test_backed_migration_is_additive_and_idempotent(self):
        from chitragupta import common, kb51, migrate, store
        self.assertIn(("kb51-record-review", "kb51:record_review"), migrate.KB_STEPS)
        self.merge_records()
        for rec in self.merge_records():
            rec.pop("review", None)
        common.write_json(store.ticket_path(T), self.st.ticket(T))
        backups, notes = [], []
        before = common.read_text(store.ticket_path(T))
        kb51.record_review(True, notes, backups.append)
        self.assertEqual(before, common.read_text(store.ticket_path(T)))
        kb51.record_review(False, notes, backups.append)
        self.assertIn(store.ticket_path(T), backups[-1])
        saved = common.read_json(store.ticket_path(T))
        self.assertTrue(saved["tables"]["shop.sessions"]["review"])
        self.assertEqual(len(saved["tables"]), 2)
        after = common.read_text(store.ticket_path(T))
        kb51.record_review(False, [], backups.append)
        self.assertEqual(after, common.read_text(store.ticket_path(T)))
