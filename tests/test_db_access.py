"""Database access (dbaccess): approved scope and its blocked sides (DBT-02), two principals, generations, revocation,
quarantine and routing (the D0 parts of DBT-03 / DBT-04), freshness labels and config validation. Synthetic data
only; no database call is made anywhere here."""
import datetime as _dt
import json
import os
import unittest

from tests.dbfixture import INVENTORY, SERVER, FakeTransport, Scenario
from tests.support import Isolated, write_config


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()

    def cfg(self):
        return self.sc.cfg()

    def ctx(self):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg())

    def node(self, conn, schema="shop", name="orders", env="qa", db="shop", engine="postgres"):
        from chitragupta import dbmodel
        return dbmodel.db_node(SERVER, conn, env, db, schema, "table", name, engine)


class ApprovedScope(Case):
    """DBT-02: only explicitly scoped, allowed-environment, guard-listed connections are approved."""

    def test_the_scenario(self):
        from chitragupta import dbaccess
        sc = dbaccess.approved_scope(self.cfg())
        self.assertEqual(sorted(sc["connections"]), ["qa-billing", "qa-main"])
        self.assertEqual(sc["provenance"], "mcp.database.scope")
        self.assertIn("not in qa_data.allowed_envs", sc["blocked"]["uat-main"])
        self.assertIn("not in qa_data.allowed_envs", sc["blocked"]["dev-main"])
        self.assertNotIn("qa-reporting", sc["connections"])  # only in the inventory
        self.assertEqual(sc["connections"]["qa-billing"]["engine"], "mssql")
        self.assertEqual(sc["repos"], [self.sc.repo_id()])

    def test_a_second_environment_only_when_allowed(self):
        from chitragupta import dbaccess
        self.sc.enable_dev()
        sc = dbaccess.approved_scope(self.cfg())
        self.assertIn("dev-main", sc["connections"])
        self.assertIn("uat-main", sc["blocked"])  # enabling dev never lets uat in
        self.assertEqual(self.cfg()["qa_data"]["allowed_envs"], ["qa", "dev"])

    def test_inventory_never_widens_the_scope(self):
        from chitragupta import dbaccess
        before = dbaccess.approved_scope(self.cfg())["connections"]
        dbaccess.record_inventory(INVENTORY + [{"id": "qa-extra", "env": "QA", "type": "postgres"}])
        after = dbaccess.approved_scope(self.cfg())
        self.assertEqual(sorted(after["connections"]), sorted(before))
        self.assertNotIn("qa-extra", after["connections"])
        self.assertNotIn("prod-main", after["connections"])

    def test_the_connector_reporting_another_environment_or_engine_blocks(self):
        from chitragupta import dbaccess
        dbaccess.record_inventory([{"id": "qa-main", "env": "PROD", "type": "postgres"},
                                   {"id": "qa-billing", "env": "QA", "type": "postgresql"}])
        sc = dbaccess.approved_scope(self.cfg())
        self.assertEqual(sc["blocked"]["qa-main"], "the connector reports another environment for it")
        self.assertEqual(sc["blocked"]["qa-billing"], "the connector reports another engine for it")
        self.assertEqual(sc["connections"], {})

    def test_production_unknown_and_invalid_entries_fail_closed(self):
        from chitragupta import dbaccess
        cfg = self.sc.config()
        conns = cfg["mcp"]["database"]["scope"]["connections"]
        conns["qa-prod-copy"] = {"env": "qa", "engine": "postgres", "schemas": ["shop"]}
        conns["qa-noenv"] = {"engine": "postgres", "schemas": ["shop"]}
        conns["qa-weird"] = {"env": "qa", "engine": "oracle"}
        conns["qa-extra-field"] = {"env": "qa", "engine": "postgres", "note": "x"}
        conns["qa-prodish"] = {"env": "prod", "engine": "postgres"}
        write_config(self.home, cfg)
        sc = dbaccess.approved_scope(self.cfg())
        for c in ("qa-prod-copy", "qa-noenv", "qa-weird", "qa-extra-field", "qa-prodish"):
            with self.subTest(c=c):
                self.assertIn(c, sc["blocked"])
                self.assertNotIn(c, sc["connections"])
        self.assertIn("production", sc["blocked"]["qa-prod-copy"])
        self.assertTrue(dbaccess.scope_errors(cfg["mcp"]["database"]["scope"]))

    def test_aliases_outside_scope_are_blocked(self):
        from chitragupta import dbaccess
        cfg = self.sc.config()
        cfg["mcp"]["database"]["scope"]["connections"]["qa-main"]["aliases"] = ["shop-qa", "qa-billing", "prod-shop",
                                                                                "shop-uat"]
        write_config(self.home, cfg)
        dbaccess.record_inventory([{"id": "shop-uat", "env": "UAT", "type": "postgres"}])
        sc = dbaccess.approved_scope(self.cfg())
        self.assertEqual(sc["aliases"], {"shop-qa": "qa-main"})
        self.assertEqual(set(sc["aliases_blocked"]), {"qa-billing", "prod-shop", "shop-uat"})
        self.assertEqual(sc["aliases_blocked"]["shop-uat"], "resolves outside the approved scope")

    def test_routing_is_explicit(self):
        from chitragupta import dbaccess
        ctx = self.ctx()
        self.assertEqual(dbaccess.resolve_connection(ctx, "qa-main"), ("qa-main", None))
        self.assertEqual(dbaccess.resolve_connection(ctx, "shop-qa"), ("qa-main", None))
        for name, env in ((None, None), ("", "qa"), ("uat-main", None), ("qa-reporting", None), ("prod-main", None),
                          ("qa-main", "uat"), ("qa-billing", "dev")):
            with self.subTest(name=name, env=env):
                c, why = dbaccess.resolve_connection(ctx, name, env)
                self.assertIsNone(c)
                self.assertTrue(why)

    def test_legacy_scope_is_derived_with_provenance(self):
        from chitragupta import dbaccess
        cfg = self.sc.config()
        db = cfg["mcp"]["database"]
        db.pop("scope")
        db.update(connections={"qa": "qa-main"}, allowed={"qa": ["qa-billing"], "uat": ["uat-main"]},
                  schema_map={"shop": "qa-main"}, types={"qa-billing": "mssql"})
        write_config(self.home, cfg)
        sc = dbaccess.approved_scope(self.cfg())
        self.assertEqual((sc["source"], sc["provenance"]), ("legacy", "derived from mcp.database.connections"))
        self.assertEqual(sorted(sc["connections"]), ["qa-billing", "qa-main"])
        self.assertEqual(sc["connections"]["qa-main"]["schemas"], ["shop"])
        self.assertIsNone(sc["connections"]["qa-billing"]["schemas"])  # unknown: no schema-naming statement is built
        self.assertIsNone(sc["connections"]["qa-main"]["engine"])      # never guessed from the name
        self.assertEqual(sc["connections"]["qa-billing"]["engine"], "mssql")
        self.assertIn("uat-main", sc["blocked"])
        self.assertEqual(dbaccess.guard_scopes(self.cfg()), (None, None))

    def test_the_guard_file_and_the_scope_share_one_derivation(self):
        from chitragupta import common, dbaccess, mcpjobs
        allow = common.read_json(common.wl("mcp", "allow.json"))
        entry = allow["servers"][SERVER]
        self.assertEqual(entry["connections"], mcpjobs.db_guard_connections(self.cfg()))
        self.assertEqual(entry["connections"], ["qa-billing", "qa-main"])
        self.assertEqual(sorted(entry["scopes"]), sorted(dbaccess.approved_scope(self.cfg())["connections"]))
        self.assertEqual(entry["scopes"]["qa-billing"], {"engine": "mssql", "databases": ["billing"],
                                                         "schemas": ["billing"], "case_sensitive": False})
        self.assertNotIn("uat-main", entry["connections"])


class Principals(Case):
    """Two people, one machine: personal records follow the confirmed identity; shared technical context doesn't."""

    def test_two_principals(self):
        from chitragupta import dbaccess, dbmodel
        a = self.ctx()
        inv = {"id": dbmodel.new_id("i"), "owner": a.principal_id, "connections": ["qa-main"], "objective": "ABC-1001"}
        tech = self.node("qa-main")
        self.assertTrue(dbaccess.decide(a, inv)["allowed"])
        self.assertEqual(dbaccess.decide(a, inv)["category"], "personal")
        self.sc.switch_principal("B")
        b = self.ctx()
        self.assertNotEqual(a.principal_id, b.principal_id)
        self.assertNotEqual(a.profile_generation, b.profile_generation)
        d = dbaccess.decide(b, inv)
        self.assertEqual((d["allowed"], d["category"]), (False, "outside"))
        self.assertNotIn("ABC-1001", json.dumps(d))  # a denial carries nothing of the record
        self.assertEqual(dbaccess.visible(b, [inv, tech]), [tech])
        self.assertEqual(dbaccess.decide(b, tech)["category"], "technical")  # shared schema context stays usable

    def test_no_confirmed_identity_means_no_personal_records(self):
        from chitragupta import dbaccess, dbmodel
        cfg = self.sc.config()
        cfg["git_author_emails"] = []
        write_config(self.home, cfg)
        ctx = self.ctx()
        self.assertIsNone(ctx.principal_id)
        d = dbaccess.decide(ctx, {"id": dbmodel.new_id("i"), "owner": None, "connections": ["qa-main"]})
        self.assertEqual((d["allowed"], d["reason"]), (False, "missing_identity"))
        self.assertTrue(dbaccess.decide(ctx, self.node("qa-main"))["allowed"])

    def test_a_shared_artifact_must_be_redacted(self):
        from chitragupta import dbaccess, dbmodel
        ctx = self.ctx()
        art = {"id": dbmodel.new_id("f", "shared"), "shared": True, "connections": ["qa-main"]}
        self.assertFalse(dbaccess.decide(ctx, art)["allowed"])
        self.assertEqual(dbaccess.decide(ctx, dict(art, redacted=True))["category"], "shared")
        self.assertFalse(dbaccess.decide(ctx, dict(art, redacted=True, connections=["uat-main"]))["allowed"])


class Decisions(Case):
    def test_technical_records_need_approved_connections_and_selected_repos(self):
        from chitragupta import dbaccess, dbmodel
        ctx = self.ctx()
        self.assertTrue(dbaccess.decide(ctx, self.node("qa-main"))["allowed"])
        for conn, env in (("uat-main", "uat"), ("qa-reporting", "qa"), ("dev-main", "dev"), ("prod-main", "prod")):
            with self.subTest(conn=conn):
                self.assertFalse(dbaccess.decide(ctx, self.node(conn, env=env))["allowed"])
        svc = dbmodel.code_node(self.sc.repo_id(), "service", "shop-api")
        self.assertTrue(dbaccess.decide(ctx, svc)["allowed"])
        other = dbmodel.code_node("path:/elsewhere", "service", "shop-api")
        self.assertFalse(dbaccess.decide(ctx, other)["allowed"])
        # code from an unselected repo next to an approved connection: inferred context only
        ev = {"id": dbmodel.new_id("v", "x"), "scope": {"connection": "qa-main", "repo": "path:/elsewhere"}}
        d = dbaccess.decide(ctx, ev)
        self.assertTrue(d["allowed"] and d.get("inferred_only"))
        self.assertFalse(dbaccess.decide(ctx, {"id": dbmodel.new_id("k", "x")})["allowed"])  # names nothing
        self.assertFalse(dbaccess.decide(ctx, {"id": "../x"})["allowed"])

    def test_an_edge_is_only_as_visible_as_its_ends(self):
        from chitragupta import dbaccess, dbmodel
        ctx = self.ctx()
        a, b = self.node("qa-main"), self.node("uat-main", env="uat")
        e = dbmodel.make_edge("copies", a["id"], b["id"])
        graph = {"nodes": {a["id"]: a, b["id"]: b}}
        self.assertFalse(dbaccess.decide(ctx, e, graph)["allowed"])
        c = self.node("qa-billing", schema="billing", name="refunds", db="billing", engine="mssql")
        graph["nodes"][c["id"]] = c
        self.assertTrue(dbaccess.decide(ctx, dbmodel.make_edge("copies", a["id"], c["id"]), graph)["allowed"])


class Generations(Case):
    """Revocation and policy changes invalidate cached context: an old decision can't be reused."""

    def test_policy_generation_changes_with_the_policy(self):
        from chitragupta import dbaccess
        g0 = dbaccess.policy_generation(self.cfg())
        self.assertEqual(g0, dbaccess.policy_generation(self.cfg()))
        self.sc.enable_dev()
        g1 = dbaccess.policy_generation(self.cfg())
        self.assertNotEqual(g0, g1)
        self.sc.enable_dev(False)
        self.assertEqual(dbaccess.policy_generation(self.cfg()), g0)
        cfg = self.sc.config()
        cfg["mcp"]["database"]["scope"]["connections"]["qa-billing"]["schemas"] = ["billing", "reporting"]
        write_config(self.home, cfg)
        self.assertNotEqual(dbaccess.policy_generation(self.cfg()), g0)
        self.sc.write()
        cfg = self.sc.config()
        cfg["mcp"]["database"]["arguments"]["limit"] = ""
        write_config(self.home, cfg)
        self.assertNotEqual(dbaccess.policy_generation(self.cfg()), g0)

    def test_revocation_hides_cached_context_and_updates_the_guard(self):
        from chitragupta import common, dbaccess
        refunds = self.node("qa-billing", schema="billing", name="refunds", db="billing", engine="mssql")
        before = self.ctx()
        self.assertTrue(dbaccess.decide(before, refunds)["allowed"])
        dbaccess.revoke("qa-billing", "left the project", self.cfg())
        after = self.ctx()
        self.assertNotEqual(before.policy_generation, after.policy_generation)
        self.assertFalse(dbaccess.decide(after, refunds)["allowed"])
        self.assertEqual(dbaccess.approved_scope(self.cfg())["blocked"]["qa-billing"], "revoked")
        entry = common.read_json(common.wl("mcp", "allow.json"))["servers"][SERVER]
        self.assertNotIn("qa-billing", entry["scopes"])
        self.assertNotIn("qa-billing", entry["connections"])
        dbaccess.restore("qa-billing", self.cfg())
        self.assertTrue(dbaccess.decide(self.ctx(), refunds)["allowed"])
        self.assertEqual(self.ctx().policy_generation, before.policy_generation)


class Quarantine(Case):
    def test_permission_denied_quarantines_until_a_good_read(self):
        from chitragupta import dbaccess, dbmodel
        refunds = self.node("qa-billing", schema="billing", name="refunds", db="billing", engine="mssql")
        col = dbmodel.member_node(refunds, "order_ref", engine="mssql")
        orders = self.node("qa-main")
        dbaccess.quarantine("qa-billing", refunds["id"], "permission denied")
        ctx = self.ctx()
        d = dbaccess.decide(ctx, refunds)
        self.assertEqual((d["allowed"], d["reason"]), (False, "Access denied at last check"))
        self.assertFalse(dbaccess.decide(ctx, col)["allowed"])  # its columns too
        self.assertTrue(dbaccess.decide(ctx, orders)["allowed"])
        self.assertTrue(dbaccess.reconcile("qa-billing", refunds["id"]))
        self.assertTrue(dbaccess.decide(self.ctx(), refunds)["allowed"])
        dbaccess.quarantine("qa-billing")  # the whole connection
        self.assertFalse(dbaccess.decide(self.ctx(), col)["allowed"])
        self.assertTrue(dbaccess.reconcile("qa-billing"))
        self.assertTrue(dbaccess.decide(self.ctx(), col)["allowed"])


class Freshness(Case):
    def test_last_confirmed_never_verified(self):
        from chitragupta import dbaccess, dbconnector
        ctx = self.ctx()
        self.assertEqual(dbaccess.authorization(ctx, "qa-main")["state"], "unknown")
        rec = dbconnector.assess(self.cfg())
        now = _dt.datetime(2026, 11, 6, 12, 0).astimezone()
        dbconnector.observe_call(rec, "describe", "ok", connection="qa-main",
                                 at=(now - _dt.timedelta(days=2)).isoformat(timespec="seconds"))
        dbconnector.observe_call(rec, "describe", "ok", connection="qa-billing",
                                 at=(now - _dt.timedelta(days=9)).isoformat(timespec="seconds"))
        dbconnector.save_capabilities(rec)
        ctx = self.ctx()
        a, b = dbaccess.authorization(ctx, "qa-main", now), dbaccess.authorization(ctx, "qa-billing", now)
        self.assertEqual((a["state"], b["state"]), ("current", "stale"))
        self.assertEqual(a["label"], "access last confirmed 2026-11-04")
        for x in (a, b):
            self.assertNotIn("verified", x["label"])
        cfg = self.sc.config()
        cfg["mcp"]["database"]["authorization_max_age_days"] = 30
        write_config(self.home, cfg)
        self.assertEqual(dbaccess.authorization(self.ctx(), "qa-billing", now)["state"], "current")


class ConfigValidation(Case):
    def test_scope_keys_are_validated_and_not_dashboard_editable(self):
        from chitragupta import settings
        cfg = self.cfg()
        for key, value in (("mcp.database.scope", {"connections": {"qa-x": {"env": "qa"}}}),
                           ("mcp.database.arguments", {"connection": "connectionId"}),
                           ("mcp.database.authorization_max_age_days", 3)):
            with self.subTest(key=key):
                _, errors = settings.validate({key: value}, cfg)
                self.assertIn("/cg:setup", errors[key])
                self.assertIsNone(settings.check_locked(key, value, cfg))
        bad = [("mcp.database.scope", {"connections": {"prod-main": {"env": "qa"}}}),
               ("mcp.database.scope", {"connections": {"qa-x": {"env": "qa", "engine": "oracle"}}}),
               ("mcp.database.scope", {"connections": {"qa-x": {"env": "QA env"}}}),
               ("mcp.database.scope", {"connections": {}, "extra": 1}),
               ("mcp.database.scope", {"connections": {"qa-x": {"env": "qa", "schemas": ["sh;op"]}}}),
               ("mcp.database.arguments", {"connection": "target"}), ("mcp.database.arguments", {"sql": "body"}),
               ("mcp.database.arguments", {"schema": "database"}), ("mcp.database.arguments", {"limit": "max"}),
               ("mcp.database.authorization_max_age_days", 0), ("mcp.database.authorization_max_age_days", True)]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                self.assertIsNotNone(settings.check_locked(key, value, cfg))

    def test_the_settings_page_shows_a_summary(self):
        from chitragupta import settings
        rows = {r["key"]: r for g in settings.describe()["groups"] for r in g["items"]}
        self.assertFalse(rows["mcp.database.scope"]["editable"])
        self.assertIn("qa-main (qa)", rows["mcp.database.scope"]["value"])
        self.assertIn("1 repository", rows["mcp.database.scope"]["value"])
        self.assertIn("connection → connectionId", rows["mcp.database.arguments"]["value"])


class NoQueries(Case):
    def test_access_decisions_make_no_database_call(self):
        from chitragupta import dbaccess
        fake = FakeTransport(self.sc)
        ctx = self.ctx()
        for n in (self.node("qa-main"), self.node("uat-main", env="uat")):
            dbaccess.decide(ctx, n)
        dbaccess.approved_scope(self.cfg())
        dbaccess.policy_generation(self.cfg())
        self.assertEqual(fake.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".worklog", "cache", "db-jobs")))


if __name__ == "__main__":
    unittest.main()
