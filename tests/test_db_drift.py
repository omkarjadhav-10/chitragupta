"""Drift and reconciliation (dbimpact.flow_drift / reconcile, DB-16): DBT-27 (a schema or code change marks the dependent
saved flow and investigation "Requires revalidation" without losing a version, a run or a note; staleness comes from
the sources the flow recorded, with no rebuild on a read; authoritative removal keeps history but leaves current
lookups) and DBT-28's index / cache half (a revocation removes the content from search results, cached results and
exports; a reconciliation started under an older scope publishes nothing). Also the database search units (DB-15)."""
import json
import os
import unittest
from unittest import mock

from tests.dbfixture import FakeTransport, Scenario, write_linkmap, write_ticket
from tests.support import Isolated


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()
        write_ticket(self.home)
        write_linkmap(self.home)
        from chitragupta import dbflows, dbingest, dbtrace
        dbingest.run(self.cfg())
        self.sc.provider(timeout=True)
        ctx = self.ctx()
        self.fid = dbflows.build_flow(ctx, {"kind": "ticket", "key": "ABC-1001"}, save=True)["id"]
        vis = dbflows.Visible(ctx)
        self.oid = [n for n in vis.nodes if n.startswith("o_") and vis.label(n) == "shop.orders (qa-main)"][0]
        plan = dbtrace.make_plan(ctx, self.fid, "qa", None, {"object": self.oid, "key": {"tenant_id": "7",
                                                                                         "id": "1001"}})
        job = dbtrace.request_run(ctx, plan["id"], plan["fingerprint"], "test")
        dbtrace.run_job(job["id"], FakeTransport(self.sc), self.cfg(), {})
        self.iid = plan["investigation"]
        dbtrace.note_save(self.ctx(), self.iid, "my unsaved thoughts", 0)

    def cfg(self):
        return self.sc.cfg()

    def ctx(self):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg())

    def edit_card(self, conn, table, fn):
        p = os.path.join(self.home, "data", "db-schema", conn, table + ".json")
        with open(p, encoding="utf-8") as f:
            card = json.load(f)
        fn(card)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(card, f)
        from chitragupta import dbingest
        dbingest.run(self.cfg())


class Revalidation(Case):
    """DBT-27."""

    def test_a_schema_change_marks_the_flow_and_investigation_without_losing_history(self):
        from chitragupta import dbflows, dbimpact, dbstore, dbviews
        flow0 = dbstore.load("flows", self.fid)
        inv0 = dbstore.load("investigations", self.iid)
        self.assertEqual(dbimpact.flow_drift(self.ctx(), flow0)["state"], "current")
        self.edit_card("qa-main", "shop.orders", lambda c: c["describe"].__setitem__(
            2, dict(c["describe"][2], dataType="varchar(64)")))
        with mock.patch.object(dbflows, "build_flow", side_effect=AssertionError("a read rebuilt the flow")):
            v = dbviews.flow_view(self.ctx(), fid=self.fid)
            self.assertIn("stale_flow", [s["id"] for s in v["states"]])
            self.assertIn("metadata of shop.orders (qa-main) changed", v["data"]["review"]["reasons"])
            self.assertTrue(dbviews.flows_list(self.ctx())["items"][0]["stale"])
        s = dbimpact.reconcile(self.ctx(), self.cfg())
        self.assertEqual((s["state"], s["flows_marked"], s["investigations_marked"]), ("published", 1, 1))
        flow1, inv1 = dbstore.load("flows", self.fid), dbstore.load("investigations", self.iid)
        self.assertEqual(flow1["review"]["label"], "Requires revalidation")
        self.assertEqual(flow1["current"]["version"], flow0["current"]["version"])
        self.assertEqual(flow1["versions"], flow0["versions"])
        self.assertEqual(inv1["review"]["state"], "needs_review")
        self.assertEqual(inv1["runs"], inv0["runs"])  # earlier observations stay as they were (historical meaning)
        self.assertEqual(inv1["notes"], inv0["notes"])  # the note is untouched
        self.assertEqual(inv1["notes"]["text"], "my unsaved thoughts")
        again = dbimpact.reconcile(self.ctx(), self.cfg())
        self.assertEqual((again["flows_marked"], again["events"]), (0, 0))  # stable: a re-run adds nothing
        events = dbstore.read_file("reconcile")["events"]
        self.assertEqual(len(events), 2)
        dbflows.build_flow(self.ctx(), {"kind": "ticket", "key": "ABC-1001"}, save=True)  # revalidated by rebuilding
        self.assertNotIn("review", dbstore.load("flows", self.fid))
        self.assertEqual(dbimpact.flow_drift(self.ctx(), dbstore.load("flows", self.fid))["state"], "current")

    def test_a_code_change_marks_the_flow(self):
        from chitragupta import dbimpact, dbstore
        p = os.path.join(self.sc.repo, "shop-api", "orders", "cancel.py")
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write("# refactored\n")
        s = dbimpact.reconcile(self.ctx(), self.cfg())
        self.assertGreaterEqual(s["code_changed"], 1)
        reasons = dbstore.load("flows", self.fid)["review"]["reasons"]
        self.assertIn("cited code shop-api/orders/cancel.py changed since it was recorded", reasons)
        state = dbstore.read_file("reconcile")
        self.assertTrue(state["sources"]["ticket:ABC-1001"]["last_success"])
        self.assertEqual(state["generations"]["policy"], self.ctx().policy_generation)

    def test_authoritative_removal_keeps_history_out_of_current_lookups(self):
        from chitragupta import dbflows, dbviews
        self.assertEqual(dbflows.resolve_start(self.ctx(), "table", "orders")["state"], "ambiguous")
        self.edit_card("qa-main", "shop_audit.orders", lambda c: c.__setitem__("missing_since", "2026-11-07T10:00:00"))
        r = dbflows.resolve_start(self.ctx(), "table", "orders")
        self.assertEqual((r["state"], r["candidates"][0]["label"]), ("resolved", "shop.orders (qa-main)"))
        vis = dbflows.Visible(self.ctx())
        hist = [n for n in vis.nodes if vis.label(n) == "shop_audit.orders (qa-main)"][0]
        v = dbviews.table_view(self.ctx(), hist)  # opened by its ID it still shows, as history
        self.assertEqual(v["data"]["lifecycle"], "historical")


class Search(Case):
    """DB-15 recall units and DBT-28's index / cache half."""

    def setUp(self):
        super().setUp()
        from chitragupta import searchidx
        self.addCleanup(searchidx.reset)  # the open index view, before the temp home is removed

    def results(self, q):
        from chitragupta import searchidx
        res = searchidx.search(q, limit=200)
        return res, [(r["type"], r["title"]) for g in res["groups"] for r in g["results"]]

    def test_units_access_checked_on_every_result_and_revocation(self):
        from chitragupta import dbaccess, dbimpact, dbstore, dbtrace, searchidx
        searchidx.update()
        con = searchidx.paths.sqlite_connect(searchidx.db_path(), readonly=True)
        try:
            types = {r[0] for r in con.execute("SELECT DISTINCT type FROM units")}
            bodies = " ".join(r[0] for r in con.execute("SELECT body FROM units WHERE type LIKE 'db%'"))
        finally:
            con.close()
        self.assertTrue(set(searchidx.DB_TYPES) <= types)
        for raw in ("1001", "T007", "SELECT ", "my unsaved thoughts", "customer1@acme.test"):
            self.assertNotIn(raw, bodies)  # no value, key, SQL or note is indexed
        res, hits = self.results("refunds")
        self.assertIn(("dbobject", "billing.refunds (qa-billing)"), hits)
        _, hits = self.results("investigation")
        self.assertTrue(any(t == "dbinvestigation" for t, _ in hits), hits)
        _, hits = self.results("abc 1001")
        self.assertTrue(any(t == "dbflow" for t, _ in hits), hits)
        dbimpact.impact(self.ctx(), self.oid)  # names qa-billing (the refund lineage)
        rid = dbimpact.impact_id(self.oid)
        self.assertIsNotNone(dbimpact.cached("impact", rid))
        dbaccess.revoke("qa-billing", "test", cfg=self.cfg())
        res, hits = self.results("refunds")  # no index update: decided at query time
        self.assertFalse([h for h in hits if h[0].startswith("db") and "billing" in h[1]], hits)
        self.assertNotIn("billing.refunds", json.dumps(res))  # no title, count or snippet
        _, hits = self.results("investigation")
        self.assertFalse([h for h in hits if h[0] == "dbinvestigation"])
        _, hits = self.results("abc 1001")
        self.assertFalse([h for h in hits if h[0] == "dbflow"])
        s = dbimpact.reconcile(self.ctx(), self.cfg())
        self.assertEqual(s["caches_removed"], 1)
        self.assertIsNone(dbimpact.cached("impact", rid))
        with self.assertRaises(dbtrace.NotFound):
            dbtrace.handoff_copy(self.ctx(), self.iid)  # the export path re-checks access
        searchidx.update()
        con = searchidx.paths.sqlite_connect(searchidx.db_path(), readonly=True)
        try:
            left = [r[0] for r in con.execute("SELECT title FROM units WHERE type LIKE 'db%'")]
        finally:
            con.close()
        self.assertFalse([t for t in left if "billing" in t], left)  # re-projected after the reconciliation
        self.assertTrue(dbstore.read_file("reconcile")["last_run"]["caches_removed"] == 1)

    def test_investigation_units_are_their_owners_only(self):
        from chitragupta import searchidx
        searchidx.update()
        self.assertTrue(any(t == "dbinvestigation" for t, _ in self.results("investigation")[1]))
        self.sc.switch_principal("B")
        _, hits = self.results("investigation")
        self.assertFalse([h for h in hits if h[0] == "dbinvestigation"], hits)

    def test_a_late_reconcile_from_an_older_scope_publishes_nothing(self):
        from chitragupta import dbaccess, dbimpact, dbstore
        self.edit_card("qa-main", "shop.orders", lambda c: c["describe"].__setitem__(
            2, dict(c["describe"][2], dataType="varchar(64)")))
        before = (dbstore.load("flows", self.fid), dbstore.load("investigations", self.iid),
                  dbstore.read_file("reconcile"))
        old = self.ctx()
        s = dbimpact.reconcile(old, self.cfg(),
                               before_publish=lambda: dbaccess.revoke("qa-billing", "late", cfg=self.cfg()))
        self.assertEqual((s["state"], s["flows_marked"], s["investigations_marked"]), ("superseded", 0, 0))
        self.assertEqual((dbstore.load("flows", self.fid), dbstore.load("investigations", self.iid),
                          dbstore.read_file("reconcile")), before)


if __name__ == "__main__":
    unittest.main()
