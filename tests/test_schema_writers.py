"""Schema cards: one deterministic writers query per table (ORDER BY, schema-aware, write verbs only), replies matched
by the exact SQL built, and a merge that never loses a known writer (synthetic `shop` schema, no MCP calls)."""
import json
import os
import unittest

from tests.support import Isolated

DB = "sqldb"
WRITER = {"schema_name": "shop", "name": "set_order_total", "kind": "function",
          "src": "BEGIN UPDATE shop.orders SET total = p_total, updated_at = now() WHERE id = p_id; END"}
READER = {"schema_name": "report", "name": "read_orders", "kind": "function",
          "src": "BEGIN RETURN QUERY SELECT id, total FROM shop.orders WHERE status = 'open'; END"}
OTHER = {"schema_name": "billing", "name": "fix_totals", "kind": "procedure",
         "src": "BEGIN UPDATE billing.orders SET total = 0 WHERE id = 1; END"}
BARE_SAME = {"schema_name": "shop", "name": "close_order", "kind": "procedure",
             "src": "BEGIN UPDATE orders SET status = 'closed' WHERE id = p_id; DELETE FROM orders WHERE id = 0; END"}
BARE_OTHER = {"schema_name": "report", "name": "touch_orders", "kind": "procedure",
              "src": "BEGIN UPDATE orders SET seen = true; END"}


class SchemaWriters(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        from chitragupta import common, mcpjobs
        self.cfg = json.loads(json.dumps(common.load_config()))
        self.cfg["mcp"]["database"].update(enabled=True, server=DB, connections={"qa": "qa-shop"})
        self.replies = []
        self.tasks = []

        def fake_run(job, servers, reads, writes, task, schema, cfg, health, **k):
            self.tasks.append(task)
            return {"calls": self.replies.pop(0), "out": {"done": True}, "input_tokens": 0, "output_tokens": 0}
        mcpjobs.run = fake_run  # restore() puts the tripwire back

    def calls(self, writer_rows=None, writer_sql=None, writer_error=False, describe=True, table="orders"):
        from chitragupta import mcpjobs
        out = []
        if describe:
            out.append({"name": "mcp__%s__describe_table" % DB, "input": {"table": table},
                        "result": json.dumps([{"name": "id"}, {"name": "total"}]), "is_error": False})
        out.append({"name": "mcp__%s__run_query" % DB, "input": {"sql": mcpjobs.triggers_sql("postgres", "shop", [table])},
                    "result": "0 row(s) in 2ms", "is_error": False})
        if writer_rows is not None or writer_error:
            sql = writer_sql or mcpjobs.writers_sql("postgres", "shop", table)
            res = "%d row(s) in 3ms\n%s" % (len(writer_rows or []), json.dumps(writer_rows)) if writer_rows else "0 row(s) in 3ms"
            out.append({"name": "mcp__%s__run_query" % DB, "input": {"connectionId": "qa-shop", "limit": 20, "sql": sql},
                        "result": "connection refused" if writer_error else res, "is_error": writer_error})
        return out

    def card(self, table="shop.orders"):
        from chitragupta import common
        return common.read_json(common.hp("data", "db-schema", "qa-shop", table + ".json"), {})

    def run_job(self, calls):
        from chitragupta import mcpjobs
        self.replies.append(calls)
        return mcpjobs.schema_cards(["shop.orders"], self.cfg, {})

    def test_writer_sql_passes_guard(self):
        from chitragupta import mcpjobs
        from chitragupta.safety import query
        for ctype in ("postgres", "mssql"):
            for sql in (mcpjobs.writers_sql(ctype, "shop", "orders"), mcpjobs.triggers_sql(ctype, "shop", ["orders", "docs"])):
                with self.subTest(ctype=ctype, sql=sql[:60]):
                    self.assertIsNone(query.sql_reason(sql, 20))
                    self.assertIn("ORDER BY", sql)
            w = mcpjobs.writers_sql(ctype, "shop", "orders")
            self.assertIn("orders", w)
            self.assertNotIn("docs", w)
        # identifiers are sanitised: nothing but [A-Za-z0-9_] reaches the SQL
        sql = mcpjobs.writers_sql("postgres", "sh'op", "ord'ers")
        self.assertNotIn("sh'op", sql)
        self.assertIn("shop", sql)
        self.assertIsNone(query.sql_reason(sql, 20))

    def test_one_writers_query_per_table(self):
        from chitragupta import mcpjobs
        self.replies.append([])
        mcpjobs.schema_cards(["shop.orders", "shop.docs"], self.cfg, {})
        task = self.tasks[-1]
        self.assertIn(json.dumps(mcpjobs.writers_sql("postgres", "shop", "orders")), task)
        self.assertIn(json.dumps(mcpjobs.writers_sql("postgres", "shop", "docs")), task)

    def test_same_fixture_twice_same_writers(self):
        self.run_job(self.calls([WRITER, READER, OTHER, BARE_SAME, BARE_OTHER]))
        c1 = self.card()["card"]
        self.assertEqual([w["name"] for w in c1["writers"]], ["shop.close_order", "shop.set_order_total"])
        self.assertEqual(next(w for w in c1["writers"] if w["name"] == "shop.set_order_total")["writes"],
                         ["total", "updated_at"])
        self.assertEqual([r["name"] for r in c1["references"]],
                         ["billing.fix_totals", "report.read_orders", "report.touch_orders"])
        self.run_job(self.calls([BARE_OTHER, BARE_SAME, OTHER, READER, WRITER]))  # same rows, another order
        c2 = self.card()["card"]
        strip = lambda ws: [{k: v for k, v in w.items() if k not in ("first_seen", "last_seen")} for w in ws]  # noqa: E731
        self.assertEqual(strip(c1["writers"]), strip(c2["writers"]))
        self.assertEqual(strip(c1["references"]), strip(c2["references"]))

    def test_failed_or_missing_call_keeps_known_writers(self):
        from chitragupta import common
        self.run_job(self.calls([WRITER, READER]))
        rec = self.card()
        rec["sources"]["writers"]["fetched_at"] = "2026-10-01T00:00:00+05:30"
        common.write_json(common.hp("data", "db-schema", "qa-shop", "shop.orders.json"), rec)
        for calls in (self.calls(None), self.calls(writer_error=True),
                      self.calls([], writer_sql="SELECT p.proname AS name, p.prosrc AS src FROM pg_proc p LIMIT 20")):
            self.run_job(calls)
            rec = self.card()
            self.assertEqual([w["name"] for w in rec["card"]["writers"]], ["shop.set_order_total"])
            self.assertNotIn("missing_since", rec["card"]["writers"][0])
            self.assertEqual(rec["sources"]["writers"]["fetched_at"], "2026-10-01T00:00:00+05:30")
            self.assertTrue(rec["sources"]["writers"]["error"])

    def test_missing_describe_keeps_old_columns(self):
        self.run_job(self.calls([WRITER]))
        self.run_job(self.calls([WRITER], describe=False))
        rec = self.card()
        self.assertEqual([c["name"] for c in rec["describe"]], ["id", "total"])
        self.assertTrue(rec["card"]["found"])

    def test_writer_gone_from_a_good_run_is_marked_not_dropped(self):
        self.run_job(self.calls([WRITER]))
        self.run_job(self.calls([]))
        rec = self.card()
        self.assertEqual([w["name"] for w in rec["card"]["writers"]], ["shop.set_order_total"])
        self.assertTrue(rec["card"]["writers"][0]["missing_since"])
        self.assertEqual(rec["sources"]["writers"]["rows"], 0)
        self.run_job(self.calls([WRITER]))  # back again: no longer missing
        self.assertNotIn("missing_since", self.card()["card"]["writers"][0])

    def test_other_schema_same_name_not_writer(self):
        from chitragupta import mcpjobs
        self.assertEqual(mcpjobs._write_ops(OTHER["src"], "shop.orders", "billing"), [])
        self.assertEqual(mcpjobs._write_ops(BARE_OTHER["src"], "shop.orders", "report"), [])
        self.assertEqual(mcpjobs._write_ops(BARE_SAME["src"], "shop.orders", "shop"), ["update", "delete"])
        self.assertEqual(mcpjobs._write_ops("INSERT INTO [shop].[orders] (id) VALUES (1)", "shop.orders", "dbo"), ["insert"])

    def test_db_flow_facts_use_writers_and_references(self):
        from chitragupta import common, kbjobs
        self.run_job(self.calls([READER]))
        common.write_json(common.hp("data", "tickets", "ABC-1001.json"),
                          {"data_flow": {"x": {"table": "shop.orders", "column": "total"}}})
        txt = kbjobs.dbflow_qa_section("ABC-1001", self.cfg)
        self.assertIn("no database trigger, procedure or function writes it", txt)
        self.assertIn("read by `report.read_orders`", txt)


class SchemaWritersMigration(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()

    def test_empty_writes_move_to_references(self):
        from chitragupta import common, mcpjobs
        p = common.hp("data", "db-schema", "qa-shop", "shop.orders.json")
        common.write_json(p, {"table": "shop.orders", "fetched_at": "2026-10-17T02:48:48+05:30", "card": {
            "table": "shop.orders", "found": True, "triggers": [],
            "writers": [{"name": "report.sync_grid", "kind": "function", "writes": [], "reads": []},
                        {"name": "shop.set_order_total", "kind": "function", "writes": ["total"], "reads": []}]}})
        common.write_json(common.hp("data", "db-schema", "_linkmap.json"), {"env": "qa"})
        backed = []
        notes = []
        mcpjobs.migrate_schema_writers(False, notes, backed.append)
        self.assertEqual(backed, [[common.hp("data", "db-schema")]])
        rec = common.read_json(p, {})
        self.assertEqual([w["name"] for w in rec["card"]["writers"]], ["shop.set_order_total"])
        self.assertEqual([w["name"] for w in rec["card"]["references"]], ["report.sync_grid"])
        self.assertEqual(rec["sources"]["writers"]["fetched_at"], "2026-10-17T02:48:48+05:30")
        self.assertEqual(rec["sources"]["describe"]["fetched_at"], "2026-10-17T02:48:48+05:30")
        self.assertTrue(notes)
        before = json.dumps(rec, sort_keys=True)
        mcpjobs.migrate_schema_writers(False, [], backed.append)  # a second run changes nothing
        self.assertEqual(json.dumps(common.read_json(p, {}), sort_keys=True), before)
        self.assertEqual(common.read_json(common.hp("data", "db-schema", "_linkmap.json"), {}), {"env": "qa"})

    def test_registered(self):
        from chitragupta import migrate
        self.assertIn(("kb5-db-schema-writers", "mcpjobs:migrate_schema_writers"), migrate.KB_STEPS)

    def test_dry_run_writes_nothing(self):
        from chitragupta import common, mcpjobs
        p = common.hp("data", "db-schema", "qa-shop", "shop.orders.json")
        rec = {"table": "shop.orders", "fetched_at": "2026-10-17T02:48:48+05:30",
               "card": {"writers": [{"name": "report.sync_grid", "writes": []}]}}
        common.write_json(p, rec)
        notes = []
        mcpjobs.migrate_schema_writers(True, notes, lambda paths: None)
        self.assertEqual(common.read_json(p, {}), rec)
        self.assertTrue(os.path.isfile(p))
        self.assertTrue(notes)


if __name__ == "__main__":
    unittest.main()
