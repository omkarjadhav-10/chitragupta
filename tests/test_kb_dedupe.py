"""K7: one record per API, table, query and file (pure key normalisers, merge, migration with aliases, render once)."""
import json
import os
import unittest

from tests.support import Isolated, rd, write_config

DAY = "2026-09-01"


def ctx(**kw):
    c = {"day": DAY, "session": "abcdef123456", "time": "10:00", "repo": "C:/w/shop-api",
         "branch": "feature/ABC-1001-cancel", "candidates": ["ABC-1001"]}
    c.update(kw)
    return c


class Normalisers(unittest.TestCase):
    def test_api_variants_fold(self):
        from chitragupta import kbnorm
        keys = {kbnorm.api_key("put", "/orders/:id/CANCEL_ORDER"),
                kbnorm.api_key("PUT", "/orders/{orderId}/CANCEL-ORDER"),
                kbnorm.api_key("PUT", "https://api.acme.test/orders/42/cancel-order"),
                kbnorm.api_key("PUT", "/orders/{{id}}/CANCEL_ORDER (v2)")}
        self.assertEqual(keys, {"PUT /orders/*/cancel-order"})
        self.assertNotEqual(kbnorm.api_key("PUT", "/orders/:id/cancel"), kbnorm.api_key("PUT", "/orders/:id/refund"))
        self.assertNotEqual(kbnorm.api_key("GET", "/orders/:id"), kbnorm.api_key("PUT", "/orders/:id"))

    def test_sql_set_prefix_spacing_and_case_fold(self):
        from chitragupta import kbnorm
        a = kbnorm.sql_key("set default_transaction_read_only=on;\nSELECT status FROM shop.orders WHERE id=:id")
        b = kbnorm.sql_key("select status\n  from shop.orders\n where id = :id;")
        self.assertEqual(a, b)
        # literals keep their case and spacing: a different value is a different query
        self.assertNotEqual(kbnorm.sql_key("select 1 from shop.orders where code = 'AB  c'"),
                            kbnorm.sql_key("select 1 from shop.orders where code = 'ab c'"))

    def test_table_key(self):
        from chitragupta import kbnorm
        self.assertEqual(kbnorm.table_key("Shop.Orders"), "shop.orders")
        self.assertEqual(kbnorm.table_key("[shop].[orders]"), "shop.orders")
        self.assertEqual(kbnorm.table_key("orders", known=["shop.orders", "shop.items"]), "shop.orders")
        self.assertEqual(kbnorm.table_key("orders", known=["shop.orders", "audit.orders"]), "orders")  # ambiguous

    def test_file_key(self):
        from chitragupta import kbnorm
        self.assertEqual(kbnorm.file_key("C:\\w\\shop-api\\src\\Order.java", "C:/w/shop-api"), "src/Order.java")
        self.assertEqual(kbnorm.file_key("./src/Order.java"), "src/Order.java")
        self.assertEqual(kbnorm.file_key("/home/dev/shop-api/src/Order.java", None, ["shop-api"]), "src/Order.java")
        self.assertEqual(kbnorm.file_key("Order.java (shop-api, full path not shown)"), "Order.java")


class MergeAndRender(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"]})

    def test_variants_merge_into_one_record_each(self):
        from chitragupta import render, store
        st = store.Store()
        res = {"apis": [{"method": "PUT", "path": "/orders/:id/CANCEL_ORDER", "host_or_service": "order-service", "ticket": "ABC-1001"},
                        {"method": "PUT", "path": "/orders/{orderId}/CANCEL-ORDER", "host_or_service": "https://api.acme.test",
                         "ticket": "ABC-1001"}],
               "queries": [{"ticket": "ABC-1001", "text": "set default_transaction_read_only=on; select * from shop.orders where id=:id"},
                           {"ticket": "ABC-1001", "text": "SELECT * FROM shop.orders WHERE id = :id;"}],
               "tables": [{"name": "orders", "db": "shop", "ticket": "ABC-1001", "purpose": "orders"},
                          {"name": "shop.orders", "db": "shop", "ticket": "ABC-1001", "key_columns": ["id"]}],
               "files": [{"path": "C:/w/shop-api/src/Order.java", "ticket": "ABC-1001", "role": "entity"},
                         {"path": "src/Order.java", "ticket": "ABC-1001"},
                         {"path": "Order.java", "ticket": "ABC-1001"}]}
        store.merge_extraction(st, res, ctx(), ["ABC"])
        tk = st.ticket("ABC-1001")
        self.assertEqual(list(tk["apis"]), ["PUT /orders/*/cancel-order"])
        self.assertEqual(set(tk["apis"]["PUT /orders/*/cancel-order"]["hosts"]), {"order-service", "https://api.acme.test"})
        self.assertEqual(len(tk["queries"]), 1)
        self.assertEqual(list(tk["tables"]), ["shop.orders"])
        self.assertEqual(tk["tables"]["shop.orders"]["key_columns"], ["id"])
        self.assertEqual(list(tk["files"]), ["src/Order.java"])
        slot = st.day(DAY)["tickets"]["ABC-1001"]
        self.assertEqual(len(slot["api_keys"]), 1)
        self.assertEqual(len(slot["query_ids"]), 1)
        self.assertEqual(len(st.catalog()["apis"]), 1)
        self.assertEqual(list(st.catalog()["tables"]), ["shop.orders"])

    def test_changed_file_rendered_once_in_the_code_map(self):
        from chitragupta import render, store
        st = store.Store()
        tk = st.ticket("ABC-1001")
        tk["files"] = {"src/Order.java": {"path": "src/Order.java", "role": "entity", "changed": True, "last": DAY, "verified": True},
                       "src/Read.java": {"path": "src/Read.java", "role": "read only", "changed": False, "last": DAY,
                                         "verified": True}}
        md = render.render_ticket(st, "ABC-1001")
        self.assertEqual(md.count("src/Order.java"), 1)
        self.assertIn("| File | Role | Changed |", md)
        self.assertNotIn("Files changed:", md)


class Migration(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        from chitragupta import common, migrate
        write_config(self.home, {"ticket_prefixes": ["ABC"]})
        cfg = common.read_json(common.wl("config.json"), {})
        cfg["chitragupta_migrations"] = list(migrate.all_ids())
        cfg["chitragupta_migrations"].remove("kb5-dedupe-keys")
        common.write_json(common.wl("config.json"), cfg)
        q1 = "select status from shop.orders where id = :id;"
        q2 = "set default_transaction_read_only=on; select status from shop.orders where id=:id"
        self.old1 = common.stable_id("ABC-1001", "query", q1)
        self.old2 = common.stable_id("ABC-1001", "query", q2)
        tk = {"id": "ABC-1001", "title": "", "items": {}, "repos": ["shop-api"],
              "queries": {self.old1: {"id": self.old1, "date": "2026-08-30", "text": q1, "purpose": "status"},
                          self.old2: {"id": self.old2, "date": DAY, "text": q2, "purpose": "status again"}},
              "apis": {"PUT /orders/:id/CANCEL_ORDER order-service": {"method": "PUT", "path": "/orders/:id/CANCEL_ORDER",
                                                                       "host_or_service": "order-service", "last": "2026-08-30"},
                       "PUT /orders/{orderId}/CANCEL-ORDER https://api.acme.test": {
                           "method": "PUT", "path": "/orders/{orderId}/CANCEL-ORDER", "host_or_service": "https://api.acme.test",
                           "last": DAY}},
              "tables": {"orders": {"name": "orders", "db": "shop", "purpose": "orders"},
                         "shop.orders": {"name": "shop.orders", "db": "shop", "key_columns": ["id"]}},
              "files": {"C:\\w\\shop-api\\src\\Order.java": {"path": "C:\\w\\shop-api\\src\\Order.java", "changed": True, "last": DAY},
                        "src/Order.java": {"path": "src/Order.java", "role": "entity", "changed": False, "last": "2026-08-30"}}}
        common.write_json(common.hp("data", "tickets", "ABC-1001.json"), tk)
        common.write_json(common.hp("data", "catalog.json"), {"tables": {"orders": {"name": "orders", "tickets": ["ABC-1001"]},
                                                                        "shop.orders": {"name": "shop.orders", "tickets": ["ABC-1001"]}},
                                                             "apis": {k: dict(v, tickets=["ABC-1001"]) for k, v in tk["apis"].items()},
                                                             "integrations": {}})
        common.write_json(common.hp("data", "days", "2026", DAY + ".json"),
                          {"date": DAY, "tickets": {"ABC-1001": {"query_ids": [self.old1, self.old2], "api_keys": list(tk["apis"]),
                                                                 "table_keys": ["orders", "shop.orders"],
                                                                 "files_changed": ["C:\\w\\shop-api\\src\\Order.java"]}},
                           "other": {}})

    def test_rekeys_merges_and_keeps_aliases(self):
        from chitragupta import common, migrate, render, store
        dry = migrate.run(dry_run=True)
        self.assertIn("kb5-dedupe-keys", dry["applied"])
        self.assertEqual(len(common.read_json(common.hp("data", "tickets", "ABC-1001.json"))["apis"]), 2)
        res = migrate.run(wait_s=0)
        self.assertIn("kb5-dedupe-keys", res["applied"], res)
        self.assertTrue(os.path.isfile(os.path.join(res["backups"][0], "data", "tickets", "ABC-1001.json")))
        self.assertTrue(os.path.isfile(os.path.join(res["backups"][0], "data", "catalog.json")))
        tk = common.read_json(common.hp("data", "tickets", "ABC-1001.json"))
        self.assertEqual(len(tk["apis"]), 1)
        self.assertEqual(len(tk["queries"]), 1)
        (qid, q), = tk["queries"].items()
        self.assertEqual(set(q["aliases"]), {self.old1, self.old2})
        self.assertEqual(set(q["days"]), {"2026-08-30", DAY})
        self.assertEqual(list(tk["tables"]), ["shop.orders"])
        self.assertEqual(tk["tables"]["shop.orders"]["purpose"], "orders")  # the bare record's data is kept
        self.assertEqual(list(tk["files"]), ["src/Order.java"])
        self.assertTrue(tk["files"]["src/Order.java"]["changed"])
        cat = common.read_json(common.hp("data", "catalog.json"))
        self.assertEqual((len(cat["apis"]), list(cat["tables"])), (1, ["shop.orders"]))
        dd = common.read_json(common.hp("data", "days", "2026", DAY + ".json"))
        slot = dd["tickets"]["ABC-1001"]
        self.assertEqual(slot["query_ids"], [qid])
        self.assertEqual(slot["table_keys"], ["shop.orders"])
        self.assertEqual(slot["files_changed"], ["src/Order.java"])
        qmd = render.render_queries(store.Store(), "ABC-1001")
        for old in (self.old1, self.old2, qid):
            self.assertIn('<a id="q-%s"></a>' % old, qmd)
        self.assertTrue(migrate.run(wait_s=0)["already"])


if __name__ == "__main__":
    unittest.main()
