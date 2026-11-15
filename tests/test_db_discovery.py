"""Bounded metadata discovery and expansion (dbflows.discover_metadata, dbflows.expand; DB-07): <= 10 describes per
batch through the fake database MCP, resumable without describing twice, capability observations, a denied object
quarantined, partial inventory pages that never remove an object (DBT-10), and a 1,000-object generated catalog with
cycles kept to depth 3, 100 nodes and pages of 50 with a visible continuation (DBT-11)."""
import json
import os
import time
import unittest

from tests.dbfixture import SERVER, FakeTransport, Scenario
from tests.support import Isolated


def rj(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()
        self.fake = FakeTransport(self.sc)

    def cfg(self):
        return self.sc.cfg()

    def ctx(self):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg())

    def discover(self, seeds, **kw):
        from chitragupta import dbflows
        return dbflows.discover_metadata(self.ctx(), seeds, self.fake, self.cfg(), {}, **kw)

    def described(self):
        return [(c["input"].get("schema"), c["input"].get("table")) for c in self.fake.calls
                if c["tool"] == "describe_table"]


SEEDS = ["qa-main:shop.orders", "qa-main:shop.order_events", "qa-main:shop_audit.orders"]


class Discovery(Case):
    def test_batches_resume_without_describing_twice(self):
        """DBT-10: an interrupted discovery resumes where it stopped; nothing is described twice."""
        first = self.discover(SEEDS, batch=2)
        self.assertEqual((len(first["described"]), first["remaining"], first["more_available"]), (2, 1, True))
        second = self.discover(SEEDS, batch=2)
        self.assertEqual((len(second["described"]), second["remaining"], second["more_available"]), (1, 0, False))
        self.assertEqual(sorted(self.described()), sorted({("shop", "orders"), ("shop", "order_events"),
                                                           ("shop_audit", "orders")}))
        self.assertEqual(len(self.described()), 3)
        # only metadata: describes and catalog reads, never a row query
        for c in self.fake.calls:
            sql = c["input"].get("sql") or ""
            self.assertTrue(c["tool"] in ("describe_table", "run_query"))
            if sql:
                self.assertRegex(sql, r"(pg_trigger|pg_proc|sys\.triggers|sys\.sql_expression_dependencies)")

    def test_at_most_ten_per_batch_and_the_scope_is_never_widened(self):
        res = self.discover(SEEDS + ["qa-reporting:shop.orders", "dev-main:shop.orders", "qa-main:other.table"],
                            batch=50)
        self.assertLessEqual(len(self.described()), 10)
        whys = {s["why"] for s in res["skipped"]}
        self.assertIn("connection not approved", whys)
        self.assertIn("schema outside the approved scope", whys)
        self.assertFalse([c for c in self.fake.calls if c["input"].get("connectionId") in ("qa-reporting", "dev-main")])

    def test_cards_capabilities_and_graph(self):
        from chitragupta import dbconnector, dbstore, mcpjobs
        self.discover(["qa-main:shop.orders", "qa-billing:billing.refunds"])
        card = rj(mcpjobs.card_path("qa-billing", "billing.refunds"))
        self.assertEqual(card["card"]["references"][0]["definition"], "unavailable")
        caps = dbconnector.load_capabilities()
        k = caps["connections"]["qa-main"]
        self.assertEqual((k["capabilities"]["tables"]["state"], k["capabilities"]["tables"]["provenance"]),
                         ("supported", "observed_call"))
        self.assertTrue(k["last_authorized"])
        nodes = dbstore.load_graph()["nodes"]
        self.assertTrue([n for n in nodes.values() if n["display"].get("name") == "refunds" and n["attrs"].get("described")])

    def test_a_denied_describe_quarantines_the_object(self):
        from chitragupta import dbaccess, dbflows
        self.discover(["qa-billing:billing.refunds"])
        oid = [n["id"] for n in dbflows.Visible(self.ctx()).nodes.values() if n["id"][0] == "o" and
               n["display"].get("name") == "refunds"][0]
        self.fake.denied.add("qa-billing")
        res = self.discover(["qa-billing:billing.refunds", "qa-billing:billing.tenants"])
        self.assertEqual({f["why"] for f in res["failed"]}, {"permission denied"})
        self.assertIn("qa-billing|%s" % oid, dbaccess.quarantine_entries())
        with self.assertRaises(dbflows.NotFound):
            dbflows.passport(self.ctx(), oid)  # "Access denied at last check": hidden from normal views
        self.fake.denied.discard("qa-billing")
        self.discover(["qa-billing:billing.refunds", "qa-billing:billing.tenants", "qa-billing:billing.refunds"])
        self.assertNotIn("qa-billing|%s" % oid, dbaccess.quarantine_entries())  # a good read reconciles

    def test_partial_inventory_pages_never_remove_an_object(self):
        """DBT-10: a partial listing removes nothing; only a complete listing marks an object missing, kept."""
        from chitragupta import dbingest, dbflows, mcpjobs
        legacy = mcpjobs.card_path("qa-main", "shop.legacy_orders")
        rec = rj(mcpjobs.card_path("qa-main", "shop.orders"))
        rec["table"] = "shop.legacy_orders"
        mcpjobs.save_card(legacy, rec)
        dbingest.run(self.cfg())
        self.fake.page_size = 1
        part = self.discover([], inventory=[("qa-main", "shop")])
        self.assertEqual(part["inventory"][0]["complete"], False)
        vis = dbflows.Visible(self.ctx())
        lo = [n for n in vis.nodes.values() if n["id"][0] == "o" and n["display"].get("name") == "legacy_orders"][0]
        self.assertEqual(lo["lifecycle"], "current")
        self.fake.page_size = 50
        full = self.discover([], inventory=[("qa-main", "shop")])
        self.assertEqual((full["inventory"][0]["complete"], full["inventory"][0]["missing"]), (True, ["legacy_orders"]))
        vis = dbflows.Visible(self.ctx())
        lo = [n for n in vis.nodes.values() if n["id"][0] == "o" and n["display"].get("name") == "legacy_orders"][0]
        self.assertEqual(lo["lifecycle"], "historical")  # history kept, not deleted
        self.assertTrue(rj(legacy)["missing_since"])
        self.assertEqual(rj(legacy)["table"], "shop.legacy_orders")


class Expansion(Case):
    """DBT-11: a 1,000-object generated catalog with cycles stays within the limits, with a visible continuation."""

    def build(self, n=1000):
        from chitragupta import dbmodel, dbstore
        nodes = [dbmodel.db_node(SERVER, "qa-main", "qa", "shop", "shop", "table", "t%04d" % i, "postgres")
                 for i in range(n)]
        edges = {}
        for i in range(n):
            for j in ((i + 1) % n, (i * 7 + 3) % n, (i + 500) % n, (i + 999) % n, (i * 13) % n):
                if j != i:
                    e = dbmodel.make_edge("logical_association", nodes[i]["id"], nodes[j]["id"], basis="inferred",
                                          applicability={"env": "qa", "connection": "qa-main"})
                    edges[e["id"]] = e
        dbstore.upsert("nodes", nodes)
        dbstore.upsert("edges", list(edges.values()))
        return nodes, edges

    def test_limits_cycles_and_continuation(self):
        from chitragupta import dbflows
        nodes, edges = self.build()
        self.assertGreaterEqual(len(edges), 4000)
        vis = dbflows.Visible(self.ctx())
        t0 = time.monotonic()
        seen, pages, cursor = [], 0, None
        while True:
            r = dbflows.expand(self.ctx(), [nodes[0]["id"]], cursor=cursor, vis=vis)
            pages += 1
            self.assertLessEqual(len(r["nodes"]), 50)
            self.assertLessEqual(r["depth"], 3)
            self.assertLessEqual(r["visited"], 100)
            ids = [x["id"] for x in r["nodes"]]
            self.assertFalse(set(ids) & set(seen), "a node came back twice (a cycle was not stopped)")
            seen += ids
            if not r["more_available"]:
                break
            cursor = r["cursor"]
            self.assertTrue(cursor)
            self.assertLess(pages, 60)
        self.assertGreater(len(seen), 100)  # continuation reached beyond the first bounded view
        self.assertLess(time.monotonic() - t0, 30)
        self.assertTrue(r["edges"]["structure"] or pages > 1)
        self.assertEqual(r["edges"]["flow"], [])  # layers stay apart

    def test_a_small_cycle_ends(self):
        from chitragupta import dbflows, dbmodel, dbstore
        a, b, c = (dbmodel.db_node(SERVER, "qa-main", "qa", "shop", "shop", "table", x, "postgres") for x in "abc")
        es = [dbmodel.make_edge("logical_association", x["id"], y["id"], applicability={"connection": "qa-main"})
              for x, y in ((a, b), (b, c), (c, a))]
        dbstore.upsert("nodes", [a, b, c])
        dbstore.upsert("edges", es)
        r = dbflows.expand(self.ctx(), [a["id"]])
        self.assertEqual(sorted(x["id"] for x in r["nodes"]), sorted([a["id"], b["id"], c["id"]]))
        self.assertFalse(r["more_available"])
        self.assertEqual(len(r["edges"]["structure"]), 3)


if __name__ == "__main__":
    unittest.main()
