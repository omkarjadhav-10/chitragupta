"""Change impact (dbimpact.impact, DB-13; DBT-23): evidenced edges traversed both ways as the change kind needs, direct
evidence kept apart from inferred association, stale and unavailable sources flagged, unknown coverage and traversal
limits shown, suggested validations as text (nothing is run: no test, migration or request), "No recorded impact links"
instead of "no impact", change kinds distinguished, and the cache: reads never recompute, a refresh does."""
import contextlib
import io
import json
import unittest
from unittest import mock

from tests.dbfixture import Scenario, write_linkmap, write_ticket
from tests.support import Isolated, write_config


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()
        write_ticket(self.home)
        write_linkmap(self.home)
        from chitragupta import dbingest
        dbingest.run(self.cfg())

    def cfg(self):
        return self.sc.cfg()

    def ctx(self):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg())

    def vis(self):
        from chitragupta import dbflows
        return dbflows.Visible(self.ctx())

    def oid(self, label):
        vis = self.vis()
        hits = [n for n in vis.nodes if n.startswith("o_") and vis.label(n) == label]
        self.assertEqual(len(hits), 1, label)
        return hits[0]

    def cid(self, obj_label, col):
        vis = self.vis()
        oid = self.oid(obj_label)
        hits = [n for n, x in vis.nodes.items() if x.get("parent") == oid and (x.get("display") or {}).get("member") == col]
        self.assertEqual(len(hits), 1, (obj_label, col))
        return hits[0]

    @staticmethod
    def rels(r):
        out = set()
        for it in r["items"]:
            out.add((it["relation"], it["label"]))
            out |= {(a["relation"], it["label"]) for a in it.get("also") or []}
        return out

    def cli(self, *argv):
        from chitragupta import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()


class Traversal(Case):
    """DBT-23: evidenced edges, both directions, basis apart, coverage and limits, nothing executed."""

    def test_evidenced_edges_both_ways_with_basis_flags_and_limits(self):
        from chitragupta import dbimpact
        r = dbimpact.impact(self.ctx(), self.oid("shop.orders (qa-main)"))
        rel = self.rels(r)
        for want in (("reader", "orders.cancel"), ("writer", "orders.cancel"), ("reader", "orders.legacy_ref"),
                     ("lineage", "OrderCancelled"), ("lineage", "billing.refunds (qa-billing)"),
                     ("consumer", "refunds.on_order_cancelled"), ("consumer", "reporting.on_order_cancelled"),
                     ("fk_dependent", "shop.order_events (qa-main)"), ("ticket", "ABC-1001")):
            self.assertIn(want, rel)
        deep = [it for it in r["items"] if it["label"] == "billing.refunds (qa-billing)"][0]
        self.assertEqual(deep["depth"], 2)
        self.assertEqual(deep["path"], ["shop.orders (qa-main)", "OrderCancelled", "billing.refunds (qa-billing)"])
        self.assertTrue(all(it["basis"] in ("direct", "inferred") for it in r["items"]))
        self.assertTrue(any(it["basis"] == "direct" for it in r["items"]))
        cov = " | ".join(r["coverage"])
        self.assertIn("Writers incomplete on qa-main", cov)  # the writers refresh of shop.orders failed
        self.assertIn("Static analysis may miss dynamic SQL", cov)
        self.assertIn("'orders' without its schema", cov)  # the archive job's bare `orders` may also be this one
        self.assertIn("depth 3 and 100 items", r["limits"]["text"])
        for it in r["items"]:
            self.assertTrue(it["why"] and it["validation"]["text"] and it["validation"]["rationale"], it)
        self.assertFalse(r["executed"])
        self.assertIn("nothing was run", r["suggest_note"])
        self.assertTrue(r["published"])

    def test_inferred_association_stays_apart(self):
        from chitragupta import dbimpact
        r = dbimpact.impact(self.ctx(), self.cid("shop.order_events (qa-main)", "order_id"))
        fk = [it for it in r["items"] if it["relation"] == "fk_reference"]
        self.assertTrue(fk and fk[0]["basis"] == "direct", r["items"])
        self.assertEqual(fk[0]["evidence_basis"], "declared_metadata")
        from chitragupta import common, dbmodel, dbstore
        g = dbstore.load_graph()  # without the declared key only the sampled link map's association is left
        common.write_json(dbstore.graph_path("edges"), {k: e for k, e in g["edges"].items()
                                                        if e["kind"] != "foreign_key"})
        r = dbimpact.impact(self.ctx(), self.cid("shop.order_events (qa-main)", "order_id"), save=False)
        assoc = [it for it in r["items"] if it["relation"] == "association"]
        self.assertTrue(assoc, r["items"])
        self.assertEqual(assoc[0]["basis"], "inferred")
        self.assertEqual(assoc[0]["basis_label"], "Inferred association")
        self.assertIn(dbmodel.BASIS_LABEL["observed_sample"], assoc[0]["evidence_label"])

    def test_stale_and_unavailable_sources_are_flagged(self):
        from chitragupta import dbimpact, dbstore
        g = dbstore.load_graph()
        fk = [e for e in g["edges"].values() if e["kind"] == "foreign_key"][0]
        dbstore.upsert("evidence", [dict(g["evidence"][v], quality=dict(g["evidence"][v]["quality"], freshness="stale",
                                                                         as_of="2026-09-01"))
                                    for v in fk["evidence"]])
        cfg = self.sc.config()
        cfg["mcp"]["database"]["scope"]["repos"] = []  # the code repository is no longer selected
        write_config(self.home, cfg)
        r = dbimpact.impact(self.ctx(), self.oid("shop.orders (qa-main)"), save=False)
        ev = [it for it in r["items"] if it["relation"] == "fk_dependent"][0]
        self.assertTrue(ev["stale"])
        self.assertIn("evidence stale (as of 2026-09-01)", ev["stale_reasons"])
        code = [it for it in r["items"] if it["label"] == "orders.cancel"][0]
        self.assertTrue(code["unavailable"])
        self.assertIn("code from a repository that is not selected", code["unavailable_reasons"])
        self.assertEqual(code["basis"], "inferred")  # unselected code is inferred context only
        self.assertIn("cite code that is unavailable", " | ".join(r["coverage"]))

    def test_no_recorded_dependents_is_never_no_impact(self):
        from chitragupta import dbimpact
        r = dbimpact.impact(self.ctx(), self.oid("shop_audit.orders (qa-main)"))
        self.assertEqual(r["headline"], "No recorded impact links")
        self.assertIn("not evidence that the change is safe", r["note"])
        text = json.dumps(r).lower()
        for bad in ("no impact\"", "safe to change", "no dependents"):
            self.assertNotIn(bad, text)
        self.assertIn("Static analysis may miss dynamic SQL", " ".join(r["coverage"]))


class ChangeKinds(Case):
    """DBT-23: rename, type, nullability, default, semantic and application-only changes are told apart."""

    def kinds(self, target, kind):
        from chitragupta import dbimpact
        r = dbimpact.impact(self.ctx(), target, {"kind": kind}, save=False)
        return self.rels(r), {(x["relation"], x["label"]) for x in r["excluded"]}, r

    def test_rename_stops_at_the_first_mapping_type_follows_the_value(self):
        idc = self.cid("shop.orders (qa-main)", "id")
        got, skipped, _ = self.kinds(idc, "rename")
        self.assertIn(("lineage", "OrderCancelled"), got)
        self.assertNotIn(("lineage", "billing.refunds (qa-billing)"), got)
        self.assertIn(("lineage", "billing.refunds (qa-billing)"), skipped)
        self.assertNotIn(("consumer", "refunds.on_order_cancelled"), got)
        got, _, _ = self.kinds(idc, "type")
        self.assertIn(("lineage", "billing.refunds (qa-billing)"), got)
        self.assertIn(("consumer", "refunds.on_order_cancelled"), got)
        self.assertIn(("fk_dependent", "shop.order_events (qa-main)"), got)

    def test_default_and_application_only_changes(self):
        status = self.cid("shop.orders (qa-main)", "status")
        got, skipped, r = self.kinds(status, "default")
        self.assertIn(("writer", "orders.cancel"), got)
        self.assertNotIn(("reader", "orders.cancel"), {(a, b) for a, b in got if a == "reader"})
        self.assertTrue(any(rel == "reader" for rel, _ in skipped) or
                        all(a["relation"] != "reader" for it in r["items"] for a in it["also"]))
        got, skipped, _ = self.kinds(self.oid("shop.orders (qa-main)"), "application_only")
        self.assertIn(("entry", "POST /orders/{id}/cancel"), got)
        self.assertIn(("published", "OrderCancelled"), got)
        self.assertIn(("fk_dependent", "shop.order_events (qa-main)"), skipped)
        self.assertNotIn(("fk_dependent", "shop.order_events (qa-main)"), got)
        got, skipped, _ = self.kinds(self.oid("shop.orders (qa-main)"), "nullability")
        self.assertIn(("fk_dependent", "shop.order_events (qa-main)"), skipped)

    def test_a_column_only_reaches_relations_that_use_it(self):
        from chitragupta import dbimpact
        r = dbimpact.impact(self.ctx(), self.cid("shop.orders (qa-main)", "customer_email"), save=False)
        self.assertNotIn(("lineage", "OrderCancelled"), self.rels(r))  # only tenant_id and id are mapped onward
        self.assertNotIn(("reader", "orders.cancel"), self.rels(r))  # it reads tenant_id, id, status
        with self.assertRaises(dbimpact.ImpactError):
            dbimpact.impact(self.ctx(), self.oid("shop.orders (qa-main)"), {"kind": "drop everything"})
        with self.assertRaises(dbimpact.ImpactError):
            dbimpact.impact(self.ctx(), "o_" + "0" * 24)


class Cache(Case):
    """Reads (view, MCP, GET, CLI without --refresh) never recompute; --refresh does; nothing runs anything."""

    def test_reads_use_the_cache_and_nothing_executes(self):
        from chitragupta import dbaccess, dbimpact, dbviews, mcpserver
        from chitragupta.safety import commands
        calls = []
        with mock.patch.object(commands, "run", lambda *a, **k: calls.append(("run", a)) or None), \
                mock.patch.object(commands, "spawn", lambda *a, **k: calls.append(("spawn", a)) or None):
            code, out = self.cli("db", "impact", "shop.orders", "--change", "type")
            self.assertEqual(code, 0, out)
            self.assertIn("# Impact · Type change on shop.orders (qa-main)", out)
            self.assertIn("Suggested check:", out)
            self.assertIn("nothing was run", out)
        self.assertEqual(calls, [])
        oid = self.oid("shop.orders (qa-main)")
        rid = dbimpact.impact_id(oid, {"kind": "type"})
        with mock.patch.object(dbimpact, "impact", side_effect=AssertionError("a read recomputed")):
            v = dbviews.impact_view(self.ctx(), target=oid, change="type")
            self.assertEqual((v["kind"], v["id"], v["state"]), ("impact", rid, "ok"))
            self.assertLessEqual(len(json.dumps(v).encode()), dbviews.MAX_BYTES)
            res = mcpserver.call_tool("get_db_impact", {"id": oid, "change": "type"})
            self.assertFalse(res["isError"], res)
            self.assertIn("recorded impact link", res["content"][0]["text"])
            self.assertFalse(mcpserver.call_tool("get_db_impact", {"id": rid})["isError"])
            self.assertIn(rid, mcpserver.call_tool("get_db_impact", {})["content"][0]["text"])
            nc = dbviews.impact_view(self.ctx(), target=oid, change="rename")
            self.assertEqual(nc["state"], "not_computed")
            self.assertEqual(self.cli("db", "impact", "shop.orders", "--change", "type")[0], 0)  # cached: no compute
        write_ticket(self.home, flow=[dict(__import__("tests.dbfixture", fromlist=["x"]).FLOW[0], step=9)])
        from chitragupta import dbingest
        dbingest.run(self.cfg())
        v = dbviews.impact_view(self.ctx(), target=oid, change="type")
        self.assertEqual(v["state"], "stale_result")
        before = dbimpact.cached("impact", rid)["computed_at"]
        code, out = self.cli("db", "impact", oid, "--change", "type", "--refresh")
        self.assertEqual(code, 0, out)
        self.assertEqual(dbviews.impact_view(dbaccess.AccessContext(self.cfg()), target=oid, change="type")["state"],
                         "ok")
        self.assertGreaterEqual(dbimpact.cached("impact", rid)["computed_at"], before)

    def test_a_result_from_an_older_scope_publishes_nothing(self):
        from chitragupta import dbaccess, dbimpact
        ctx = self.ctx()
        oid = self.oid("shop.orders (qa-main)")
        dbaccess.revoke("qa-billing", "test", cfg=self.cfg())
        r = dbimpact.impact(ctx, oid)  # computed under the context from before the revocation
        self.assertFalse(r["published"])
        self.assertTrue(r.get("superseded"))
        self.assertIsNone(dbimpact.cached("impact", r["id"]))


class ReviewFollowUps(Case):
    """Stage D review: --env / connection narrow a shared name everywhere, the headline counts what is listed, and
    the component implementing a mapping into the column is listed with the mapping's citation."""

    def test_env_and_connection_narrow_a_name_two_environments_share(self):
        from chitragupta import dbingest, mcpserver
        from chitragupta.dashboard import dbmap
        self.sc.enable_dev()
        dbingest.run(self.cfg())
        code, out = self.cli("db", "impact", "shop.orders.status", "--change", "type")
        self.assertEqual(code, 0, out)
        self.assertIn("Several objects match", out)  # no environment named: never narrowed for you
        self.assertIn("(dev-main)", out)
        for argv, want in ((("--env", "qa"), "(qa-main, QA)"), (("--env", "dev"), "(dev-main, DEV)"),
                           (("--connection", "qa-main"), "(qa-main, QA)")):
            code, out = self.cli("db", "impact", "shop.orders.status", "--change", "type", *argv)
            self.assertEqual(code, 0, out)
            self.assertIn("# Impact · Type change on shop.orders.status %s" % want, out)
            self.assertNotIn("Several objects match", out)
        code, out = self.cli("db", "impact", "shop.orders.status", "--change", "type", "--env", "uat")
        self.assertEqual(code, 2, out)
        self.assertIn("in UAT", out)  # uat is not approved: nothing there is visible, nothing is widened
        text = mcpserver.call_tool("get_db_impact", {"id": "shop.orders.status", "change": "type", "env": "dev"})
        self.assertIn("shop.orders.status (dev-main, DEV)", text["content"][0]["text"])
        self.assertIn("Candidates", mcpserver.call_tool("get_db_impact", {"id": "shop.orders.status",
                                                                          "change": "type"})["content"][0]["text"])
        args = {"target": "shop.orders.status", "change": "type", "env": "qa"}
        status, body = dbmap.view("/api/db/impact", lambda k: args.get(k, ""), cfg=self.cfg())
        self.assertEqual((status, body["kind"], body["data"]["target"]["connection"]), (200, "impact", "qa-main"))
        args.pop("env")
        status, body = dbmap.view("/api/db/impact", lambda k: args.get(k, ""), cfg=self.cfg())
        self.assertEqual((status, body["kind"], body["state"]), (200, "candidates", "ambiguous"))

    def test_the_headline_counts_exactly_what_is_listed(self):
        from chitragupta import dbviews
        order_ref = self.cid("billing.refunds (qa-billing)", "order_ref")
        for target, change in ((order_ref, "rename"), (self.oid("shop.orders (qa-main)"), None),
                               (self.cid("shop.orders (qa-main)", "id"), "type")):
            code, out = self.cli("db", "impact", target, *(["--change", change] if change else []))
            self.assertEqual(code, 0, out)
            v = dbviews.impact_view(self.ctx(), target=target, change=change)
            total = v["page"]["total"]
            links = sum(1 for it in v["items"] if it["relation"] != "ticket")
            tickets = total - links
            head = v["data"]["headline"]
            self.assertTrue(head.startswith("%d affected: %d recorded impact link" % (total, links)) if tickets else
                            head.startswith("%d recorded impact link" % links), (head, total, links))
            if tickets:
                self.assertIn("%d ticket" % tickets, head)
            self.assertIn("## Affected (%d)" % total, out)
            self.assertIn(head, out)

    def test_the_component_implementing_a_mapping_is_listed_with_its_citation(self):
        from chitragupta import dbimpact
        order_ref = self.cid("billing.refunds (qa-billing)", "order_ref")
        for kind in ("rename", "type", "nullability"):
            r = dbimpact.impact(self.ctx(), order_ref, {"kind": kind}, save=False)
            rel = self.rels(r)
            self.assertIn(("upstream", "OrderCancelled"), rel)
            self.assertIn(("mapper", "refunds.on_order_cancelled"), rel, kind)
            self.assertNotIn("reporting.on_order_cancelled", {b for _, b in rel})  # consumes the event, writes nothing
            up = [it for it in r["items"] if it["relation"] == "upstream"][0]
            impl = [it for it in r["items"] if it["relation"] == "mapper"][0]
            self.assertEqual(impl["basis"], "direct")
            self.assertEqual(impl["evidence_basis"], "code_supported")
            self.assertIn("billing-worker/refunds/consumer.py:7-8", impl["cite"])
            self.assertEqual(impl["cite"], up["cite"])  # the same citation as the mapping
        _, skipped, _ = ChangeKinds.kinds(self, order_ref, "default")
        self.assertIn(("mapper", "refunds.on_order_cancelled"), skipped)


if __name__ == "__main__":
    unittest.main()
