"""Relevance is additive, grounded, and keeps distinct before/after outcomes."""
import copy
import unittest
from tests.support import Isolated


def query(i, text="select id from shop.orders where id = 1", revealed="One row", **extra):
    return dict(id=i, text=text, purpose="Order state", revealed=revealed, tables=["shop.orders"], sent=True,
                date="2026-09-01", time="10:" + i[-2:], **extra)


class Relevance(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.tk = {"id": "ABC-1001", "problem": "Cancel leaves shop.orders reserved", "items": {},
                   "queries": {}, "tables": {}, "apis": {}, "files": {}}

    def test_noise_rules_and_review_never_delete(self):
        from chitragupta import relevance
        qs = [query("q01", "select * from information_schema.tables"), query("q02", "SHOW tables"),
              query("q03", "psql -c select 1"), query("q04", revealed="Result not recorded"),
              dict(query("q05"), purpose="Find a test order"), query("q06", review={"why": "wrong ticket"})]
        self.tk["queries"] = {q["id"]: q for q in qs}
        relevance.classify(self.tk)
        self.assertEqual(len(self.tk["queries"]), 6)
        for q in qs:
            self.assertNotEqual(q["relevance"], "core")
            self.assertTrue(q["hidden_reason"])

    def test_newest_per_outcome_and_cited_older_survive(self):
        from chitragupta import relevance
        qs = [query("q01", revealed="No rows"), query("q02", revealed="One row"), query("q03", revealed="One row")]
        self.tk["queries"] = {q["id"]: q for q in qs}
        relevance.classify(self.tk)
        self.assertEqual(qs[1]["variant_of"], "q03")
        self.assertEqual({q["id"] for q in relevance.core(self.tk, "queries")}, {"q01", "q03"})
        self.tk["brief"] = {"sections": {"root_cause": {"claims": [{"text": "Evidence", "cites": ["query:q02"]}]}}}
        relevance.classify(self.tk)
        self.assertEqual(qs[1]["relevance"], "core")
        self.assertFalse(qs[1]["variant_of"])

    def test_projection_is_evidence_not_a_duplicate(self):
        from chitragupta import relevance
        a, b = query("q01", "select state from shop.orders"), query("q02", "select owner from shop.orders")
        self.tk["queries"] = {"q01": a, "q02": b}
        relevance.classify(self.tk)
        self.assertEqual(len(relevance.core(self.tk, "queries")), 2)

    def test_invalid_model_ids_and_reviewed_picks_are_rejected(self):
        from chitragupta import relevance
        self.tk["queries"] = {"q01": query("q01"), "q02": query("q02", review={"why": "wrong ticket"})}
        self.tk["tables"] = {"shop.orders": {"name": "shop.orders"}}
        self.tk["apis"] = {"GET /orders": {"path": "/orders"}}
        self.tk["dbflow_view"] = {"scenarios": [{"name": "cancel"}], "data_flow": [{"scenario": "cancel"}]}
        relevance.classify(self.tk)
        bad = relevance.apply_picks(self.tk, {"queries": [{"id": "q01", "role": "verify"}, {"id": "q02", "role": "cause"},
                                                {"id": "ghost", "role": "cause"}],
                "tables": ["shop.orders", "shop.ghost"], "apis": ["GET /orders", "GET /ghost"],
                "scenarios": [{"id": "ghost", "relevance": "core"}, {"id": "cancel", "relevance": "core"}]})
        self.assertTrue(bad)
        self.assertEqual(self.tk["queries"]["q01"]["role"], "verify")
        self.assertNotEqual(self.tk["queries"]["q02"]["relevance"], "core")
        self.assertNotIn("shop.ghost", self.tk["tables"])
        self.assertEqual(self.tk["dbflow_view"]["scenarios"][0]["relevance"], "core")

    def test_owner_kept_record_can_be_core(self):
        from chitragupta import relevance
        self.tk["tables"] = {"shop.orders": {"name": "shop.orders", "review": None, "review_kept": "2026-09-01"}}
        relevance.classify(self.tk)
        self.assertEqual(len(relevance.core(self.tk, "tables")), 1)

    def test_classification_idempotent_and_migration_backed(self):
        from chitragupta import common, kb51, migrate, relevance, store
        self.assertIn(("kb51-relevance", "kb51:record_relevance"), migrate.KB_STEPS)
        self.tk["queries"] = {"q01": query("q01", "select * from pg_catalog.pg_class")}
        common.write_json(store.ticket_path("ABC-1001"), self.tk)
        before = common.read_text(store.ticket_path("ABC-1001"))
        backups = []
        kb51.record_relevance(True, [], backups.append)
        self.assertEqual(common.read_text(store.ticket_path("ABC-1001")), before)
        kb51.record_relevance(False, [], backups.append)
        self.assertIn(store.ticket_path("ABC-1001"), backups[-1])
        tk = common.read_json(store.ticket_path("ABC-1001"))
        first = copy.deepcopy(tk)
        relevance.classify(tk)
        self.assertEqual(tk, first)
        self.assertEqual(len(tk["queries"]), 1)

    def test_order_is_role_then_cited_sent_and_newest(self):
        from chitragupta import relevance
        self.tk["queries"] = {
            "cited": dict(query("q01", "select a from shop.orders"), id="cited", sent=None, date="2026-08-01"),
            "sent": dict(query("q02", "select b from shop.orders"), id="sent", date="2026-08-01"),
            "new": dict(query("q03", "select c from shop.orders"), id="new", sent=None, date="2026-09-02"),
            "old": dict(query("q04", "select d from shop.orders"), id="old", sent=None, date="2026-09-01")}
        self.tk["brief"] = {"sections": {"findings": {"claims": [{"cites": ["query:cited"]}]}}}
        self.assertEqual([q["id"] for q in relevance.core(self.tk,"queries")], ["cited", "sent", "new", "old"])
        self.assertEqual(relevance.heading(self.tk,"queries"), "Recorded queries (not yet ranked)")
        for key, role in (("old", "cause"), ("new", "fix-design"), ("sent", "verify")):
            self.tk["queries"][key].update(role=role, relevance_source="brief")
        self.assertEqual([q["id"] for q in relevance.core(self.tk,"queries")], ["old", "new", "sent", "cited"])
        self.assertEqual(relevance.heading(self.tk,"queries"), "Key queries")
