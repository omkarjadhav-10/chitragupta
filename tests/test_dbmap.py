"""DB map: the databases → schemas → tables tree, one table's panel (columns, links with their QA status, readers and
writers, tickets, saved queries, 1–2 hop neighbourhood) and the routes that serve them."""
import json
import unittest

from tests import test_dashboard as td

from chitragupta import common


def build_db(home_hp):
    td._w(home_hp("data", "db-schema", "_linkmap.json"), {"env": "qa", "fetched_at": "2026-10-10T10:00:00+05:30", "result": {
        "hierarchy": [{"database": "qa-shop", "schema": "shop", "tables": ["orders", "customers", "order_lines"]},
                      {"database": "qa-billing", "schema": "billing", "tables": ["invoices"]}],
        "links": [{"from": "shop.orders.customer_id", "to": "shop.customers.id", "sampled": 20, "matched": 20,
                   "checked": True, "connection": "qa-shop", "evidence": "foreign key"},
                  {"from": "shop.order_lines.order_id", "to": "shop.orders.id", "sampled": 20, "matched": 8,
                   "checked": True, "connection": "qa-shop", "evidence": "sampled"},
                  {"from": "billing.invoices.customer_id", "to": "shop.customers.id", "sampled": 20, "matched": 2,
                   "checked": True, "connection": "qa-billing", "evidence": "sampled"}]}})
    td._w(home_hp("data", "db-schema", "qa-shop", "shop.orders.json"), {"fetched_at": "2026-10-10T10:00:00+05:30", "describe": [
        {"name": "id", "dataType": "integer", "isPrimaryKey": True, "nullable": False},
        {"name": "id", "dataType": "integer", "isPrimaryKey": True, "nullable": False},
        {"name": "customer_id", "dataType": "integer", "isForeignKey": True, "nullable": False},
        {"name": "email", "dataType": "text", "isPhiField": True, "nullable": True}]})
    td._w(home_hp("data", "catalog.json"), {
        "tables": {"shop.orders": {"name": "shop.orders", "db": "free text", "purpose": "Orders placed in the shop",
                                   "key_columns": ["id"], "tickets": [td.TICKET]},
                   "coupons": {"name": "coupons", "db": "free text"}},
        "db_relations": {"r1": {"from": "shop.orders.coupon_code", "to": "coupons.code", "kind": "join", "tickets": [td.TICKET]},
                         "r2": {"from": "orders.customer_id", "to": "customers.id", "kind": "foreign key", "tickets": ["ABC-1002"]}},
        "db_writers": {"shop.orders": {"table": "shop.orders",
                                       "writes": {"w1": {"component": "OrderService.place", "column": "status", "change": "= NEW",
                                                         "tickets": [td.TICKET]}},
                                       "reads": {"x1": {"component": "OrderService.find", "tickets": []}}}}})
    p = home_hp("data", "tickets", td.TICKET + ".json")
    with open(p, encoding="utf-8") as f:
        tk = json.load(f)
    tk["queries"] = {"q1": {"text": "select id, status from shop.orders where id = 1", "tables": ["shop.orders"],
                            "dialect": "postgresql", "purpose": "Check one order", "date": "2026-10-11"},
                     "q2": {"text": "select 1 from billing.invoices", "tables": ["billing.invoices"], "date": "2026-10-11"}}
    td._w(p, tk)


class DbMapData(td.Base):
    def setUp(self):
        super().setUp()
        build_db(common.hp)
        from chitragupta.dashboard import api
        api._cache.clear()

    def test_tree(self):
        from chitragupta.dashboard import dbmap
        t = dbmap.tree()
        shape = {d["name"]: {s["name"]: [x["name"] for x in s["tables"]] for s in d["schemas"]} for d in t["databases"]}
        self.assertEqual(shape["qa-shop"], {"shop": ["customers", "order_lines", "orders"]})
        self.assertEqual(shape["qa-billing"], {"billing": ["invoices"]})
        # the catalog's free-text db label doesn't create a database; an unplaced table goes last
        self.assertEqual(shape[dbmap.UNPLACED], {dbmap.NO_SCHEMA: ["coupons"]})
        self.assertEqual(t["databases"][-1]["name"], dbmap.UNPLACED)
        self.assertEqual(t["counts"], {"tables": 5, "links": 4, "verified": 1})
        self.assertEqual(t["linkmap"]["env"], "qa")

    def test_table_panel(self):
        from chitragupta.dashboard import dbmap
        d = dbmap.table("shop.orders")
        self.assertEqual((d["database"], d["schema"], d["purpose"]), ("qa-shop", "shop", "Orders placed in the shop"))
        self.assertEqual([(c["name"], c["pk"], c["fk"], c["phi"]) for c in d["columns"]],
                         [("id", True, False, False), ("customer_id", False, True, False), ("email", False, False, True)])
        st = {(l["from"], l["to"]): (l["status"], l["ratio"]) for l in d["links"]}
        self.assertEqual(st[("shop.orders", "shop.customers")], ("verified", 1.0))
        self.assertEqual(st[("shop.order_lines", "shop.orders")], ("likely", 0.4))
        self.assertEqual(st[("shop.orders", "coupons")], ("unverified", None))
        # a catalog relation that names a QA link without schemas joins it instead of adding a second link
        self.assertEqual(len(st), 3)
        self.assertIn("ABC-1002", d["tickets"])
        self.assertEqual(d["writers"]["writes"][0]["component"], "OrderService.place")
        self.assertEqual([q["id"] for q in d["queries"]], ["q1"])

    def test_hops_and_names(self):
        from chitragupta.dashboard import dbmap
        one = dbmap.table("shop.order_lines")
        self.assertEqual(one["graph"]["nodes"], ["shop.order_lines", "shop.orders"])
        two = dbmap.table("shop.order_lines", hops=2)
        self.assertIn("shop.customers", two["graph"]["nodes"])
        self.assertIn("coupons", two["graph"]["nodes"])
        self.assertNotIn("billing.invoices", two["graph"]["nodes"])
        self.assertEqual(dbmap.table("INVOICES")["key"], "billing.invoices")  # a bare, any-case name resolves
        self.assertIsNone(dbmap.table("nope"))
        weak = {(l["from"], l["status"], l["ratio"]) for l in dbmap.table("billing.invoices")["links"]}
        self.assertEqual(weak, {("billing.invoices", "unverified", 0.1)})

    def test_empty_home(self):
        from chitragupta.dashboard import dbmap
        from chitragupta.safety import paths
        for p in (common.hp("data", "catalog.json"), common.hp("data", "db-schema", "_linkmap.json"),
                  common.hp("data", "db-schema", "qa-shop", "shop.orders.json")):
            paths.delete(p, caller="test")
        from chitragupta.dashboard import api
        api._cache.clear()
        self.assertEqual(dbmap.tree()["counts"], {"tables": 0, "links": 0, "verified": 0})
        self.assertEqual(dbmap.graph()["graph"]["edges"], [])


class DbMapHttp(td.Base):
    get = td.TestHttp.get  # borrowed, not inherited: subclassing TestHttp would run all its tests again

    def setUp(self):
        super().setUp()
        build_db(common.hp)
        from chitragupta.dashboard import api, server
        api._cache.clear()
        self.srv, self.thread = server.start_in_thread(0)
        self.port = self.srv.port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def test_routes(self):
        r, body = self.get("/api/db")
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(body)["counts"]["tables"], 5)
        r, body = self.get("/api/db/table?key=shop.orders&hops=2")
        self.assertEqual((r.status, json.loads(body)["graph"]["hops"]), (200, 2))
        r, body = self.get("/api/db/table?key=nope")
        self.assertEqual(r.status, 404)
        r, body = self.get("/api/db/graph")
        self.assertEqual(len(json.loads(body)["graph"]["edges"]), 4)
        r, _ = self.get("/api/db", method="POST", body=b"{}")
        self.assertEqual(r.status, 405)


if __name__ == "__main__":
    unittest.main()
