"""Table passports (dbflows.passport, DB-06; DBT-09): identity and environment, purpose cited or "Unconfirmed meaning",
columns, keys, relationships by layer, readers / writers with the owner unknown, routines and triggers, and per-source
state. A failed writers refresh keeps what was known as stale, a hidden routine definition makes writers incomplete,
and no passport ever says "No writers" or runs a query."""
import json
import unittest

from tests.dbfixture import FakeTransport, Scenario, write_ticket
from tests.support import Isolated


def rj(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()
        write_ticket(self.home)

    def cfg(self):
        return self.sc.cfg()

    def ctx(self):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg())

    def ingest(self, **kw):
        from chitragupta import dbingest
        return dbingest.run(self.cfg(), **kw)

    def oid(self, conn, schema, name):
        from chitragupta import dbstore
        return [n["id"] for n in dbstore.load_graph()["nodes"].values() if n["id"][0] == "o" and
                (n["display"].get("connection"), n["display"].get("schema"), n["display"].get("name")) ==
                (conn, schema, name)][0]

    def passport(self, conn, schema, name):
        from chitragupta import dbflows
        return dbflows.passport(self.ctx(), self.oid(conn, schema, name), cfg=self.cfg())


class Passport(Case):
    def test_identity_columns_relationships_and_owner(self):
        self.ingest()
        p = self.passport("qa-main", "shop", "orders")
        self.assertEqual((p["display"]["connection"], p["environment"], p["display"]["engine"]), ("qa-main", "qa", "postgres"))
        self.assertEqual(p["purpose"]["label"], "Unconfirmed meaning")
        cols = {c["name"]: c for c in p["columns"]}
        self.assertEqual([c["name"] for c in p["columns"]][:2], ["tenant_id", "id"])  # catalog order
        self.assertEqual((cols["status"]["type"], cols["status"]["nullable"]), ("varchar(32)", False))
        self.assertTrue(cols["customer_email"]["sensitive"])
        self.assertEqual(p["keys"]["primary"], ["tenant_id", "id"])
        self.assertEqual([f["kind"] for f in p["keys"]["foreign"]], ["foreign_key"])
        self.assertEqual({r["kind"] for r in p["relationships"]["structure"]}, {"foreign_key", "logical_association"})
        self.assertTrue({"reads", "writes", "transforms"} <= {r["kind"] for r in p["relationships"]["flow"]})
        w = p["writers"]
        self.assertEqual([x["name"] for x in w], ["orders.cancel"])
        self.assertEqual(w[0]["basis"], "code_supported")
        self.assertEqual(w[0]["owner"], "unknown")  # the writing service is not an accountable owner
        self.assertIn("owner unknown", p["owner"]["text"].lower())
        self.assertEqual(p["disclaimer"], "Static analysis may miss dynamic SQL.")
        self.assertTrue(w[0]["evidence"][0]["cite"].startswith("shop-api/orders/cancel.py:"))

    def test_failed_writer_discovery_keeps_stale_knowledge_and_never_says_no_writers(self):
        """DBT-09."""
        from chitragupta import mcpjobs
        p = mcpjobs.card_path("qa-main", "shop.orders")
        old = rj(p)
        rows = [{"schema_name": "shop", "name": "archive_orders", "kind": "procedure",
                 "src": "UPDATE shop.orders SET status = 'archived' WHERE id = 1"}]
        good = mcpjobs.card_record("shop.orders", "qa-main", "qa", "postgres", old, old["describe"], [], rows,
                                   "2026-11-01T09:00:00+05:30")
        mcpjobs.save_card(p, mcpjobs.card_record("shop.orders", "qa-main", "qa", "postgres", good, good["describe"],
                                                 None, None, "2026-11-06T09:00:00+05:30"))
        self.ingest(now="2026-11-06T10:00:00+05:30")
        pp = self.passport("qa-main", "shop", "orders")
        names = sorted(w["name"] for w in pp["writers"])
        self.assertEqual(names, ["orders.cancel", "shop.archive_orders (qa-main)"])
        routine = [w for w in pp["writers"] if w["name"].startswith("shop.archive")][0]
        self.assertEqual(routine["basis"], "declared_metadata")
        self.assertEqual(pp["writers_state"], "incomplete")
        self.assertIn("Writers incomplete: the last writers refresh failed", pp["writers_text"])
        self.assertNotIn("No writers", json.dumps(pp))
        src = pp["sources"]["writers"]
        self.assertEqual((src["state"], src["coverage"], src["freshness"], src["last_success"]),
                         ("failed", "partial", "stale", "2026-11-01T09:00:00+05:30"))
        self.assertEqual(pp["sources"]["description"]["state"], "ok")

    def test_hidden_definitions_keep_columns_and_make_writers_incomplete(self):
        from chitragupta import dbflows
        self.ingest()
        fake = FakeTransport(self.sc)
        dbflows.discover_metadata(self.ctx(), ["qa-billing:billing.refunds"], fake, self.cfg(), {})
        p = self.passport("qa-billing", "billing", "refunds")
        self.assertEqual([c["name"] for c in p["columns"]], ["tenant_code", "order_ref", "status", "created_at"])
        self.assertEqual(p["writers_state"], "incomplete")
        self.assertIn("definition unavailable: billing.usp_issue_refund", p["writers_text"])
        self.assertIn("unavailable", [r["definition"] for r in p["routines"]])
        self.assertNotIn("No writers", json.dumps(p))

    def test_complete_and_current_writers_still_hedge(self):
        from chitragupta import mcpjobs
        p = mcpjobs.card_path("qa-main", "shop.order_events")
        old = rj(p)
        mcpjobs.save_card(p, mcpjobs.card_record("shop.order_events", "qa-main", "qa", "postgres", old,
                                                 old["describe"], [], [], "2026-11-06T09:00:00+05:30"))
        self.ingest(now="2026-11-06T10:00:00+05:30")
        pp = self.passport("qa-main", "shop", "order_events")
        self.assertEqual(pp["writers_state"], "known")
        self.assertIn("Static analysis may miss dynamic SQL", pp["writers_text"])

    def test_opening_a_passport_runs_no_query(self):
        from chitragupta import dbviews
        self.ingest()
        fake = FakeTransport(self.sc)
        before = json.dumps(__import__("chitragupta.dbstore", fromlist=["x"]).load_graph(), sort_keys=True)
        for _ in range(3):
            e = dbviews.table_view(self.ctx(), self.oid("qa-main", "shop", "orders"), cfg=self.cfg())
            self.assertEqual(e["kind"], "table")
            dbviews.render_text(e)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.forbidden, [])  # the mcpjobs.run tripwire never fired
        after = json.dumps(__import__("chitragupta.dbstore", fromlist=["x"]).load_graph(), sort_keys=True)
        self.assertEqual(before, after)

    def test_an_unapproved_connection_has_no_passport(self):
        from chitragupta import dbaccess, dbflows
        self.ingest()
        oid = self.oid("qa-billing", "billing", "refunds")
        dbaccess.revoke("qa-billing", "test", cfg=self.cfg())
        with self.assertRaises(dbflows.NotFound):
            dbflows.passport(self.ctx(), oid, cfg=self.cfg())


if __name__ == "__main__":
    unittest.main()
