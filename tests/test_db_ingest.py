"""Database-flow ingestion (dbingest) and the additive flow-fact format (dbflow): schema cards, ticket data_flow,
table_relations, the link map, confirmed mappings and the consolidated view become the canonical graph, idempotently,
fingerprinted per source and bounded per run, with no query and no model. Synthetic data only."""
import json
import os
import unittest

from tests.dbfixture import FLOW, Scenario, write_linkmap, write_ticket
from tests.support import Isolated


def rj(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()

    def cfg(self):
        return self.sc.cfg()

    def ingest(self, **kw):
        from chitragupta import dbingest
        return dbingest.run(self.cfg(), **kw)

    def graph(self):
        from chitragupta import dbstore
        return dbstore.load_graph()

    def obj(self, conn, schema, name):
        g = self.graph()
        hits = [n for n in g["nodes"].values() if n["id"][0] == "o" and (n["display"] or {}).get("connection") == conn
                and n["display"].get("schema") == schema and n["display"].get("name") == name]
        self.assertEqual(len(hits), 1, (conn, schema, name))
        return hits[0]


class FlowFacts(Case):
    """The additive flow-fact format: optional fields kept when given, absent otherwise; event-only entries kept."""

    def test_old_entries_normalise_as_before(self):
        from chitragupta import dbflow
        old = {"scenario": "s", "component": "C", "operation": "update", "table": "shop.orders",
               "columns_written": ["status"], "evidence": ["x.py:1"], "confidence": "confirmed"}
        rec = dbflow._norm_entry(old, "2026-11-06", "extraction")
        self.assertEqual(set(rec), {"scenario", "step", "trigger", "component", "operation", "table", "columns_read",
                                    "columns_written", "via", "transaction", "evidence", "confidence", "date",
                                    "source"})
        self.assertEqual(dbflow.flow_key("ABC-1001", rec),
                         __import__("chitragupta.common", fromlist=["x"]).stable_id("ABC-1001", "s", "C", "shop.orders",
                                                                                     "update"))

    def test_optional_facts_and_event_only_entries(self):
        from chitragupta import dbflow, store as storemod
        st = storemod.Store()
        e = {"component": "orders.cancel", "operation": "publish", "event": "OrderCancelled", "async": True,
             "path": "cancel", "path_conditions": ["status = 'open'"], "repo": "acme-shop", "revision": "abc123",
             "mapping": [{"from": ["shop.orders.tenant_id", "shop.orders.id"], "to": "OrderCancelled.key",
                          "transform": "tenant:id"}], "evidence": ["shop-api/orders/cancel.py:12"]}
        n_new, _ = dbflow.merge(st, "ABC-1001", [e, {"component": "x", "operation": "select"}], [], "2026-11-06")
        self.assertEqual(n_new, 1)  # an entry with neither table nor event is still dropped
        rec = list(st.ticket("ABC-1001")["data_flow"].values())[0]
        self.assertEqual(rec["event"], "OrderCancelled")
        self.assertEqual(rec["mapping"][0]["from"], ["shop.orders.tenant_id", "shop.orders.id"])
        self.assertEqual(rec["mapping"][0]["to"], ["OrderCancelled.key"])
        self.assertEqual((rec["path"], rec["async"], rec["repo"], rec["revision"]), ("cancel", True, "acme-shop", "abc123"))
        # an update that doesn't repeat an optional fact keeps it
        dbflow.merge(st, "ABC-1001", [dict({k: v for k, v in e.items() if k not in ("mapping", "async")},
                                           evidence=["shop-api/orders/cancel.py:12", "note"])], [], "2026-11-07")
        rec = list(st.ticket("ABC-1001")["data_flow"].values())[0]
        self.assertEqual(rec["async"], True)
        self.assertTrue(rec["mapping"])
        # the table views skip event-only facts: db-flow.md and the catalog are unchanged by them
        tk = st.ticket("ABC-1001")
        self.assertEqual(dbflow.entries(tk), [])
        self.assertIn("_No DB flow traced yet._", dbflow.render_ticket("ABC-1001", tk, "notes"))

    def test_the_block_schema_accepts_the_new_facts_and_old_blocks(self):
        from chitragupta import blocks
        new = {"ticket": "ABC-1001", "data_flow": [dict(FLOW[3]), dict(FLOW[5])]}
        obj, errs = blocks.parse("dbflow", json.dumps(new))
        self.assertEqual(errs, [])
        old = {"ticket": "ABC-1001", "data_flow": [{"component": "C", "operation": "update", "table": "shop.orders",
                                                    "evidence": ["x.py:3"]}]}
        self.assertEqual(blocks.parse("dbflow", json.dumps(old))[1], [])
        bad = {"ticket": "ABC-1001", "data_flow": [{"component": "C", "operation": "drop", "evidence": []}]}
        self.assertTrue(blocks.parse("dbflow", json.dumps(bad))[1])


class Cards(Case):
    def test_cards_of_approved_connections_only(self):
        s = self.ingest()
        self.assertEqual(s["cards"], 5)
        self.assertIn(("card:dev-main/shop.orders", "connection dev-main is not approved"), s["skipped"])
        o = self.obj("qa-main", "shop", "orders")
        self.assertEqual((o["display"]["environment"], o["display"]["engine"], o["display"]["database"]),
                         ("qa", "postgres", "shop"))  # from the approved scope entry, never guessed
        b = self.obj("qa-billing", "billing", "refunds")
        self.assertEqual(b["display"]["engine"], "mssql")
        self.assertEqual(b["attrs"]["sources"]["describe"]["page"], "partial")
        self.assertEqual(b["attrs"]["sources"]["describe"]["coverage"], "partial")
        w = o["attrs"]["sources"]["writers"]
        self.assertEqual((w["state"], w["freshness"], w["error"]), ("failed", "stale", "no writers result"))
        g = self.graph()
        cols = {n["display"]["member"]: n["attrs"] for n in g["nodes"].values() if n.get("parent") == o["id"]}
        self.assertEqual(cols["tenant_id"]["primary_key"], True)
        self.assertEqual(cols["customer_email"]["sensitive"], True)
        self.assertEqual(cols["status"]["data_type"], "varchar(32)")
        self.assertIs(cols["status"]["nullable"], False)
        fk = [e for e in g["edges"].values() if e["kind"] == "foreign_key"]
        self.assertEqual(len(fk), 1)
        self.assertEqual((fk[0]["basis"], fk[0]["target"]), ("declared_metadata", o["id"]))
        ev = g["evidence"][fk[0]["evidence"][0]]
        self.assertEqual((ev["kind"], ev["scope"]["connection"]), ("catalog", "qa-main"))

    def test_idempotent_fingerprinted_and_bounded(self):
        from chitragupta import dbstore
        first = self.ingest(max_cards=2)
        self.assertEqual(first["cards"], 2)  # bounded: the rest wait for the next run
        second = self.ingest(max_cards=2)
        self.assertEqual(second["cards"], 2)
        self.ingest()
        before = json.dumps(self.graph(), sort_keys=True)
        again = self.ingest()
        self.assertEqual((again["cards"], again["tickets"], again["changed"]), (0, 0, {}))
        self.assertGreaterEqual(again["unchanged"], 5)
        forced = self.ingest(force=True)
        self.assertEqual(sum(forced["changed"].values()), 0)  # same sources, same records: nothing added
        self.assertEqual(json.dumps(self.graph(), sort_keys=True), before)
        self.assertTrue(dbstore.read_file("sources")["sources"]["card:qa-main/shop.orders"]["fingerprint"])

    def test_a_failed_source_keeps_its_old_data(self):
        from chitragupta import mcpjobs
        p = mcpjobs.card_path("qa-main", "shop.orders")
        old = rj(p)
        rows = [{"schema_name": "shop", "name": "archive_orders", "kind": "procedure",
                 "src": "UPDATE shop.orders SET status = 'archived' WHERE id = 1"}]
        good = mcpjobs.card_record("shop.orders", "qa-main", "qa", "postgres", old, old["describe"], [], rows,
                                   "2026-11-01T09:00:00+05:30")
        mcpjobs.save_card(p, good)
        failed = mcpjobs.card_record("shop.orders", "qa-main", "qa", "postgres", good, mcpjobs.NO_RESULT, None, None,
                                     "2026-11-06T09:00:00+05:30")
        mcpjobs.save_card(p, failed)
        self.ingest(now="2026-11-06T10:00:00+05:30")
        o = self.obj("qa-main", "shop", "orders")
        g = self.graph()
        w = [e for e in g["edges"].values() if e["kind"] == "writes" and e["target"] == o["id"]]
        self.assertEqual(len(w), 1)  # the known writer is kept
        self.assertEqual(w[0]["basis"], "declared_metadata")
        st = o["attrs"]["sources"]["writers"]
        self.assertEqual((st["state"], st["coverage"], st["freshness"]), ("failed", "partial", "stale"))
        self.assertEqual(st["last_success"], "2026-11-01T09:00:00+05:30")
        self.assertEqual(o["attrs"]["sources"]["describe"]["state"], "failed")
        self.assertTrue([n for n in g["nodes"].values() if n.get("parent") == o["id"]])  # columns kept


class Tickets(Case):
    def setUp(self):
        super().setUp()
        write_ticket(self.home)
        write_linkmap(self.home)
        self.s = self.ingest()

    def test_names_resolve_only_through_one_approved_connection(self):
        g = self.graph()
        unresolved = [c for c in g["claims"].values() if c["predicate"] == "unresolved_table"]
        self.assertEqual(len(unresolved), 1)
        v = unresolved[0]["value"]
        self.assertEqual((v["ref"], v["state"]), ("orders", "needs_resolution"))
        both = {self.obj("qa-main", "shop", "orders")["id"], self.obj("qa-main", "shop_audit", "orders")["id"]}
        self.assertEqual(set(v["candidates"]), both)  # never the first match
        self.assertEqual(unresolved[0]["context"]["tickets"], ["ABC-1001"])
        writes = {(g["nodes"][e["source"]]["display"]["name"], g["nodes"][e["target"]]["display"]["name"])
                  for e in g["edges"].values() if e["kind"] == "writes"}
        self.assertIn(("orders.cancel", "orders"), writes)
        self.assertIn(("refunds.on_order_cancelled", "refunds"), writes)
        audit = self.obj("qa-main", "shop_audit", "orders")["id"]
        self.assertFalse([e for e in g["edges"].values() if audit in (e["source"], e["target"])])

    def test_code_citations_are_checked_against_the_selected_repos(self):
        g = self.graph()
        code = [v for v in g["evidence"].values() if v["kind"] == "source" and v["provenance"].get("type") == "code"]
        self.assertTrue(code)
        for v in code:
            p = v["provenance"]
            self.assertEqual((p["repo"], p["revision"]), (self.sc.repo_id(), "worktree"))
            self.assertEqual(len(p["content_hash"]), 64)
            self.assertNotIn(":", p["path"])
            self.assertEqual(v["basis"], "code_supported")
        cancel = [v for v in code if v["provenance"]["path"] == "shop-api/orders/cancel.py"
                  and v["provenance"]["line_start"] == 12][0]
        a, b, h = self.sc.citation("shop-api/orders/cancel.py", "bus.publish")
        self.assertEqual((cancel["provenance"]["line_start"], cancel["provenance"]["content_hash"]), (a, h))
        self.assertEqual(cancel["context"]["tickets"], ["ABC-1001"])
        notes = [v for v in g["evidence"].values() if v["provenance"].get("type") == "session_record"]
        self.assertEqual(len(notes), 1)  # AuditJob.java:12 is in no selected repository
        self.assertEqual((notes[0]["basis"], notes[0]["quality"]["validation"]), ("inferred", "unvalidated"))
        self.assertEqual(notes[0]["provenance"]["ticket"], "ABC-1001")

    def test_events_mappings_and_lists(self):
        g = self.graph()
        tr = [e for e in g["edges"].values() if e["kind"] == "transforms"]
        self.assertEqual(len(tr), 2)  # orders -> OrderCancelled and OrderCancelled -> refunds
        to_event = [e for e in tr if g["nodes"][e["target"]]["kind"] == "event"][0]
        self.assertEqual(len(to_event["column_mapping"]), 2)
        self.assertTrue(all(isinstance(m["from"], list) and isinstance(m["to"], list) for m in to_event["column_mapping"]))
        self.assertEqual(to_event["transport"], "event")
        self.assertEqual(sorted(to_event["applicability"].get("connections") or [to_event["applicability"]["connection"]]),
                         ["qa-main"])
        pub = [e for e in g["edges"].values() if e["kind"] == "publishes"]
        con = [e for e in g["edges"].values() if e["kind"] == "consumes"]
        self.assertEqual((len(pub), len(con)), (1, 2))
        self.assertTrue(all(e["async"] for e in pub + con))
        legacy = [c for c in g["claims"].values() if c["predicate"] == "mapping_unresolved"]
        self.assertEqual(len(legacy), 1)
        self.assertIn("LegacyExport.legacyRef", legacy[0]["value"]["to"])

    def test_relations_are_inferred_and_samples_are_observations(self):
        g = self.graph()
        la = [e for e in g["edges"].values() if e["kind"] == "logical_association"]
        self.assertEqual(len(la), 1)  # the relation and the link map name the same sampled pair
        e = la[0]
        self.assertEqual(e["claimed_kind"], "fk")
        kinds = sorted(g["evidence"][v]["kind"] for v in e["evidence"])
        self.assertEqual(kinds, ["observation", "source"])
        self.assertEqual(e["basis"], "observed_sample")  # never declared, never a foreign key
        obs = [g["evidence"][v] for v in e["evidence"] if g["evidence"][v]["kind"] == "observation"][0]
        self.assertEqual(obs["provenance"]["label"], "19 of 20 sampled values matched, QA, 2026-11-05")
        self.assertNotIn("verified", json.dumps(obs))

    def test_a_proposal_cannot_invent(self):
        view = [dict(FLOW[1]),  # the same write: kept as a model proposal, at most inferred
                {"component": "orders.cancel", "operation": "delete", "table": "shop.ghosts", "evidence": ["x.py:1"]},
                {"component": "orders.cancel", "operation": "update", "table": "shop.orders",
                 "evidence": ["shop-api/orders/ghost.py:3"]}]
        write_ticket(self.home, view=view)
        s = self.ingest()
        self.assertEqual(s["rejected"], 2)
        g = self.graph()
        self.assertFalse([n for n in g["nodes"].values() if n["display"].get("name") == "ghosts"])
        props = [v for v in g["evidence"].values() if v["kind"] == "model_proposal"]
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["basis"], "inferred")
        upd = [e for e in g["edges"].values() if e["kind"] == "writes" and props[0]["id"] in e["evidence"]][0]
        self.assertEqual(upd["basis"], "code_supported")  # the session's own citation, not the proposal, says so


class Mappings(Case):
    def test_confirm_validates_then_ingests_as_your_note(self):
        from chitragupta import dbingest, dbstore
        write_ticket(self.home)
        self.ingest()
        with self.assertRaises(dbingest.MappingError):
            dbingest.confirm_mapping(self.cfg(), "orders.id", "billing.refunds.order_ref")  # bare: ambiguous
        with self.assertRaises(dbingest.MappingError):
            dbingest.confirm_mapping(self.cfg(), "shop.orders.id; DROP", "billing.refunds.order_ref")
        self.assertEqual(dbingest.mapping_records(), {})
        rec = dbingest.confirm_mapping(self.cfg(), "shop.orders.id", "billing.refunds.order_ref",
                                       via="OrderCancelled.orderRef", transform="str(id)", note="read in consumer.py")
        self.assertEqual(rec["from"], ["shop.orders.id"])
        self.ingest()
        g = self.graph()
        notes = [v for v in g["evidence"].values() if v["kind"] == "user_note"]
        self.assertEqual(len(notes), 1)  # one confirmation ...
        self.assertEqual(notes[0]["basis"], "user_confirmed")
        hops = [e for e in g["edges"].values() if notes[0]["id"] in e["evidence"]]
        self.assertEqual(len(hops), 2)  # ... supporting both hops: orders -> event, event -> refunds
        self.assertTrue(all(e["basis"] in ("user_confirmed", "code_supported") for e in hops))
        self.assertTrue(dbstore.read_file("mappings")["mappings"])

    def test_a_cross_environment_mapping_is_not_a_flow_edge(self):
        from chitragupta import dbingest
        self.sc.enable_dev()
        self.ingest()
        dbingest.confirm_mapping(self.cfg(), "qa-main:shop.orders.status", "dev-main:shop.orders.status")
        self.ingest()
        g = self.graph()
        dev = self.obj("dev-main", "shop", "orders")["id"]
        self.assertFalse([e for e in g["edges"].values() if dev in (e["source"], e["target"]) and e["layer"] == "flow"])
        x = [c for c in g["claims"].values() if c["predicate"] == "cross_environment_mapping"]
        self.assertEqual(len(x), 1)
        self.assertEqual((x[0]["value"]["to"], x[0]["basis"]), (dev, "user_confirmed"))


class Tick(Case):
    def test_the_tick_ingests_and_saves_flows_without_a_query(self):
        from chitragupta import dbingest, dbstore
        write_ticket(self.home)
        state = {}
        s = dbingest.tick(self.cfg(), state)
        self.assertEqual(s["tickets"], 1)
        self.assertEqual(state["db_ingest"]["flows"], 1)
        self.assertEqual(len(dbstore.list_ids("flows")), 1)
        self.assertEqual(self.forbidden, [])

    def test_no_approved_connection_no_step(self):
        from chitragupta import dbingest
        from tests.support import write_config
        cfg = self.sc.config()
        cfg["mcp"]["database"].pop("scope")
        cfg["mcp"]["database"]["connections"] = {}
        write_config(self.home, cfg)
        write_ticket(self.home)
        self.assertIsNone(dbingest.tick(self.sc.cfg(), {}))
        self.assertFalse(os.path.exists(os.path.join(self.home, "data", "db", "graph")))


if __name__ == "__main__":
    unittest.main()
