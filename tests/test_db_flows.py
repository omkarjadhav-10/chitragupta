"""Business-flow building (dbflows.build_flow, DB-08 / DB-09): ABC-1001's cancellation from the API through the order
service, the OrderCancelled event and the refund worker into billing.refunds on a second connection and engine, with
every established step cited (DBT-12), explicit gaps for the unknown deployed revision and dynamic SQL (DBT-13), the
transformed tenant-qualified mapping across postgres -> mssql while same-named objects stay candidates (DBT-14, flow
half), shared code usable while another person's ticket stays hidden and uncounted (DBT-04), saved versions with your
notes kept, and the model explanation validated against the flow's own IDs. No query anywhere."""
import json
import os
import unittest
from unittest import mock

from tests.dbfixture import FLOW, FakeTransport, Scenario, write_linkmap, write_ticket
from tests.support import Isolated


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()
        write_ticket(self.home)
        write_linkmap(self.home)
        self.ingest()

    def cfg(self):
        return self.sc.cfg()

    def ctx(self):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg())

    def ingest(self):
        from chitragupta import dbingest
        return dbingest.run(self.cfg())

    def flow(self, start=None, **kw):
        from chitragupta import dbflows
        return dbflows.build_flow(self.ctx(), start or {"kind": "ticket", "key": "ABC-1001"}, cfg=self.cfg(), **kw)

    def step(self, f, kind, needle):
        hits = [s for s in f["steps"] if s["kind"] == kind and needle in s["label"]]
        self.assertEqual(len(hits), 1, (kind, needle, [s["label"] for s in f["steps"]]))
        return hits[0]


class Explain(Case):
    def test_every_established_step_is_cited(self):
        """DBT-12: API -> service -> event -> consumer -> second database, each established step cited."""
        f = self.flow()
        kinds = {s["kind"] for s in f["steps"]}
        self.assertTrue({"entry", "service", "read", "decision", "write", "transaction", "publish", "consume",
                         "downstream_effect", "unknown_boundary"} <= kinds)
        for s in f["steps"]:
            if s["basis"] == "inferred":
                continue
            self.assertTrue(s["evidence"], s["label"])
            v = s["evidence"][0]
            self.assertEqual(v["validation"], "validated", s["label"])
            self.assertTrue(v["path"] and v["line_start"] and v["content_hash"] and v["repo"], s["label"])
        entry = self.step(f, "entry", "POST /orders/{id}/cancel")
        write = self.step(f, "write", "update shop.orders")
        pub = self.step(f, "publish", "OrderCancelled")
        con = self.step(f, "consume", "refunds.on_order_cancelled")
        down = self.step(f, "downstream_effect", "billing.refunds")
        self.assertEqual(down["nodes"][-1:], [n["id"] for n in f["nodes"] if n["label"].startswith("billing.refunds")])
        order = [entry, write, pub, con, down]
        self.assertEqual([s["position"] for s in order], sorted(s["position"] for s in order))
        self.assertTrue(pub["async"] and con["async"])
        self.assertEqual(con["order_basis"], "an event is published before it is consumed")
        self.assertEqual(sorted(f["connections"]), ["qa-billing", "qa-main"])
        self.assertTrue(f["scope_line"].startswith("2 connections · "))

    def test_unknown_revision_and_dynamic_sql_stay_explicit_gaps(self):
        """DBT-13."""
        from chitragupta import dbflows
        f = self.flow()
        self.assertEqual(f["label"], dbflows.FLOW_LABEL)
        self.assertEqual(f["label"], "Code model; deployed revision unknown")
        gaps = {g["kind"]: g for g in f["gaps"]}
        for k in ("deployed_revision_unknown", "dynamic_sql", "runtime_events_unavailable", "mapping_unresolved",
                  "unknown_boundary"):
            self.assertIn(k, gaps)
            self.assertTrue(gaps[k]["next"])
        self.assertTrue(gaps["dynamic_sql"]["steps"])
        rep = [g for g in f["gaps"] if g["kind"] == "runtime_events_unavailable" and "reporting" in g["text"]]
        self.assertTrue(rep)
        self.assertTrue([s for s in f["steps"] if s["kind"] == "unknown_boundary" and "reporting" in s["label"]])
        self.assertNotIn("verified", json.dumps(f).lower())

    def test_the_mapping_crosses_engines_and_names_alone_never_join(self):
        """DBT-14 (flow half): tenant-qualified, transformed mapping across postgres -> mssql; same-named objects
        elsewhere stay candidates."""
        f = self.flow()
        lin = {(tuple(x["from"]), tuple(x["to"])): x for x in f["lineage"]}
        self.assertEqual(lin[(("shop.orders.id",), ("OrderCancelled.orderRef",))]["transform"], "str(order.id)")
        tc = lin[(("OrderCancelled.tenantId",), ("billing.refunds.tenant_code",))]
        self.assertEqual(tc["transform"], "lookup billing.tenants.tenant_code by tenant_id")
        self.assertEqual(tc["transport"], "Event")
        self.assertIn((("shop.orders.tenant_id",), ("OrderCancelled.tenantId",)), lin)
        self.assertIn((("OrderCancelled.orderRef",), ("billing.refunds.order_ref",)), lin)
        engines = {n["connection"]: n for n in f["nodes"] if n["connection"]}
        self.assertEqual(set(engines), {"qa-main", "qa-billing"})
        down = self.step(f, "downstream_effect", "billing.refunds")
        self.assertEqual(len(down["lineage"]), 2)
        labels = " ".join(n["label"] for n in f["nodes"])
        self.assertNotIn("shop_audit.orders", labels)  # the same-named orders elsewhere is never joined
        cand = [s for s in f["steps"] if s["kind"] == "unknown_boundary" and "could be" in s["label"]][0]
        self.assertIn("shop.orders (qa-main)", cand["label"])
        self.assertIn("shop_audit.orders (qa-main)", cand["label"])
        self.assertIsNone(cand["order"])

    def test_a_node_start_and_environments(self):
        from chitragupta import dbflows
        vis = dbflows.Visible(self.ctx())
        ev = [n for n in vis.nodes.values() if n["kind"] == "event"][0]
        f = self.flow({"kind": "node", "id": ev["id"]})
        self.assertTrue([s for s in f["steps"] if s["kind"] == "publish"])
        with self.assertRaises(dbflows.FlowError):
            self.flow(env="uat")  # no approved connection in uat: refused, not widened
        with self.assertRaises(dbflows.FlowError):
            self.flow({"kind": "node", "id": "o_" + "0" * 24})

    def test_a_cross_environment_mapping_is_not_a_flow_edge(self):
        from chitragupta import dbingest
        self.sc.enable_dev()
        self.ingest()
        dbingest.confirm_mapping(self.cfg(), "qa-main:shop.orders.status", "dev-main:shop.orders.status")
        self.ingest()
        f = self.flow()
        self.assertFalse([n for n in f["nodes"] if n["connection"] == "dev-main"])

    def test_cycles_and_unconstrained_steps_stay_unordered(self):
        from chitragupta import dbflows
        steps = {k: {"id": k} for k in ("a", "b", "c", "d")}
        dbflows._rank(steps, [("a", "b", "x"), ("b", "a", "y"), ("c", "b", "z")])
        self.assertIsNone(steps["a"]["order"])
        self.assertEqual(steps["a"]["order_basis"], "in a cycle: no order")
        self.assertIsNone(steps["d"]["order"])
        self.assertEqual(steps["d"]["order_basis"], "no evidence orders this step")
        self.assertEqual(steps["c"]["order"], 0)

    def test_stages_come_from_evidence_and_separate_paths_restart(self):
        """DB-08: no invented sequence. The two writes of one transaction share a stage; the legacy export path is a
        separate group numbered from 1, never after the refund steps; the list index is never shown."""
        from chitragupta import dbflows, dbviews
        f = self.flow()
        upd = self.step(f, "write", "update shop.orders")
        ins = self.step(f, "write", "insert shop.order_events")
        self.assertEqual(upd["stage"], ins["stage"])
        self.assertTrue(upd["stage_shared"] and ins["stage_shared"])
        self.assertEqual((upd["group_kind"], ins["group_kind"]), ("main", "main"))
        down = self.step(f, "downstream_effect", "billing.refunds")
        self.assertGreater(down["stage"], upd["stage"])
        legacy = sorted((s for s in f["steps"] if "legacy_ref" in s["label"]), key=lambda s: s["stage"])
        self.assertEqual([s["stage"] for s in legacy], [1, 2])
        self.assertEqual({s["group_kind"] for s in legacy}, {"separate"})
        self.assertTrue(legacy[0]["group_label"].startswith("Separate path: "))
        self.assertNotEqual(legacy[0]["group"], down["group"])
        loose = [s for s in f["steps"] if s["stage"] is None]
        self.assertTrue(loose and all(s["group_kind"] in ("unordered", "main") for s in loose))
        self.assertIn("same stage", dbflows.stage_text(upd))
        text = dbviews.render_text(dbviews.flow_view(self.ctx(), start={"kind": "ticket", "key": "ABC-1001"}))
        main, sep = text.index("### Main path"), text.index("### Separate path: ")
        self.assertLess(main, sep)
        sep_block = text[sep:].split("\n### ")[0]
        self.assertIn("\n1. [Service] orders.legacy_ref handles it", sep_block)
        self.assertIn("2. [Read] orders.legacy_ref reads shop.orders", sep_block)
        self.assertIn("(same stage, no order between them) [Write] orders.cancel update shop.orders", text)

    def test_building_runs_no_query(self):
        fake = FakeTransport(self.sc)
        for _ in range(3):
            self.flow()
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.forbidden, [])


class Shared(Case):
    """DBT-04: shared code context stays usable when the ticket that recorded it is someone else's; their ticket is
    neither shown nor counted."""

    def test_shared_code_without_the_other_persons_ticket(self):
        from chitragupta import dbflows
        from chitragupta.dashboard import api
        other = [dict(FLOW[3]), {"scenario": "x", "component": "orders.notify", "operation": "select",
                                 "table": "shop.orders", "evidence": ["Notify.java:4"]}]
        write_ticket(self.home, "ABC-2002", flow=other, relations=[])
        snap = self.snapshot_outside_db()
        self.ingest()
        real = api.scope_of

        def scope_of(key, cfg=None):
            return {"visible": False, "basis": "outside"} if str(key).upper() == "ABC-2002" else real(key, cfg)
        with mock.patch.object(api, "scope_of", scope_of):
            vis = dbflows.Visible(self.ctx())
            pub = [e for e in vis.edges.values() if e["kind"] == "publishes"][0]
            self.assertEqual(sorted(pub["context"]["tickets"]), ["ABC-1001", "ABC-2002"])
            self.assertEqual(vis.tickets(pub), ["ABC-1001"])  # the other ticket is not shown, nor counted
            self.assertFalse([v for v in vis.evidence.values() if (v.get("provenance") or {}).get("ticket") == "ABC-2002"])
            with self.assertRaises(dbflows.FlowError):
                dbflows.build_flow(self.ctx(), {"kind": "ticket", "key": "ABC-2002"}, vis=vis)
            ev = [n for n in vis.nodes.values() if n["kind"] == "event"][0]
            f = dbflows.build_flow(self.ctx(), {"kind": "node", "id": ev["id"]}, vis=vis)
            self.assertTrue([s for s in f["steps"] if s["kind"] == "publish" and s["basis"] == "code_supported"])
            out = json.dumps(f)
            self.assertNotIn("ABC-2002", out)
            self.assertNotIn("Notify.java", out)
        # nothing outside data/db changed: no ticket, progress or activity record counts this
        self.assertEqual(self.snapshot_outside_db(), snap)

    def snapshot_outside_db(self):
        out = {}
        for dp, dns, fns in os.walk(self.home):
            rel = os.path.relpath(dp, self.home).replace("\\", "/")
            if rel.startswith("data/db") or rel.startswith(".worklog"):
                continue
            for fn in fns:
                with open(os.path.join(dp, fn), "rb") as f:
                    out[rel + "/" + fn] = f.read()
        return out


class Saved(Case):
    def test_versions_only_on_change_and_notes_survive(self):
        from chitragupta import dbflows, dbstore
        a = self.flow(save=True)
        b = self.flow(save=True)
        self.assertEqual((a["version"], b["version"]), (1, 1))
        rec = dbstore.load("flows", a["id"])
        rec["notes"] = "Refund worker owned by the billing team (my note)."
        dbstore.save("flows", rec)
        write_ticket(self.home, flow=FLOW + [{"scenario": "cancel", "step": 6, "component": "refunds.on_order_cancelled",
                                              "operation": "select", "table": "billing.tenants",
                                              "columns_read": ["tenant_code"],
                                              "evidence": ["billing-worker/refunds/consumer.py:6"]}])
        self.ingest()
        c = self.flow(save=True)
        self.assertEqual(c["version"], 2)
        rec = dbstore.load("flows", a["id"])
        self.assertEqual(rec["notes"], "Refund worker owned by the billing team (my note).")
        self.assertEqual([v["version"] for v in rec["versions"]], [1])
        self.assertEqual(rec["current"]["version"], 2)
        self.assertEqual(dbflows.saved_flow(a["id"])["id"], a["id"])
        self.assertIsNone(dbflows.saved_flow("../etc"))


class Explanation(Case):
    def test_validated_or_deterministic(self):
        from chitragupta import dbflows
        f = self.flow()
        s0 = f["steps"][0]
        det = dbflows.explain_flow(f)
        self.assertEqual(det["source"], "deterministic")
        self.assertEqual(len(det["items"]), len(f["steps"]))
        good = {"summary": "Cancelling an order publishes an event the refund worker consumes.",
                "steps": [{"step": s0["id"], "text": "The API calls the order service.",
                           "evidence": [s0["evidence"][0]["id"]]}]}
        out = dbflows.explain_flow(f, caller=lambda kind, inp, cfg: (good, 0.0), cfg={})
        self.assertEqual(out["source"], "model")
        bad = {"summary": "x", "steps": [{"step": "x_" + "1" * 24, "text": "Invented.", "evidence": []}]}
        out = dbflows.explain_flow(f, caller=lambda kind, inp, cfg: (bad, 0.0), cfg={})
        self.assertEqual(out["source"], "deterministic")
        self.assertIn("unknown step", out["reason"])
        stolen = {"summary": "x", "steps": [{"step": s0["id"], "text": "ok", "evidence": ["v_" + "2" * 24]}]}
        self.assertEqual(dbflows.explain_flow(f, caller=lambda *a: (stolen, 0.0), cfg={})["source"], "deterministic")
        promo = {"summary": "x", "steps": [{"step": s0["id"], "text": "Verified end to end.", "evidence": []}]}
        self.assertEqual(dbflows.explain_flow(f, caller=lambda *a: (promo, 0.0), cfg={})["source"], "deterministic")

        def boom(*a):
            raise RuntimeError("model unavailable")
        out = dbflows.explain_flow(f, caller=boom, cfg={})
        self.assertEqual(out["source"], "deterministic")
        self.assertIn("model call failed", out["reason"])
        spent = {"model_usage": {}}
        with mock.patch("chitragupta.modelbudget.budget_left", return_value=0.0):
            out = dbflows.explain_flow(f, caller=lambda *a: (good, 0.0), cfg={}, health=spent)
        self.assertEqual((out["source"], out["reason"]), ("deterministic", "today's model budget is spent"))


if __name__ == "__main__":
    unittest.main()
