"""The connector contract (dbconnector): capabilities of a minimal connector (DBT-01) and without credentials (DBT-38),
argument mappings, both engine adapters across two connections (DBT-14, adapter half), hostile values and identifiers
(DBT-17), unsupported dialects and untouched string keys (DBT-18), and the transport's exact-call matching. Every
database call goes through the fixture's fake transport or a stand-in for mcpjobs.run; none reaches a server."""
import json
import unittest

from tests.dbfixture import ENGINE, SERVER, FakeTransport, Scenario, parse_select
from tests.support import Isolated, write_config

TENANTS = [("tenant_id", 7), ("id", 1001)]


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()

    def cfg(self):
        return self.sc.cfg()

    def permitted(self, conn):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg()).connections[conn]


class Capabilities(Case):
    """DBT-01 / DBT-38: supported where the evidence says so, unavailable stated plainly, nothing fabricated."""

    def listing(self, *tools):
        return ["mcp__%s__%s" % (SERVER, t) for t in tools] + ["mcp__other__run_query", "Read", "mcp__%s__bad name" % SERVER]

    def test_minimal_connector_with_describe_and_query_only(self):
        from chitragupta import dbconnector
        rec = dbconnector.assess(self.cfg(), self.listing("describe_table", "run_query"))
        c = rec["connector"]["capabilities"]
        self.assertEqual(c["describe"]["state"], "supported")
        self.assertEqual(c["catalog_read"]["state"], "supported")
        for k in ("connection_inventory", "schemas", "object_list"):
            with self.subTest(k=k):
                self.assertEqual(c[k]["state"], "unsupported")
                self.assertIn("not in the server's tool listing", c[k]["reason"])
        self.assertEqual(c["parameterized_query"]["state"], "unsupported")
        self.assertEqual(c["cancellation"]["state"], "unsupported")
        self.assertEqual(c["timeout"]["state"], "unsupported")
        for conn in ("qa-main", "qa-billing"):
            k = rec["connections"][conn]["capabilities"]
            self.assertEqual(k["tables"]["state"], "supported")
            self.assertEqual(k["triggers"]["state"], "supported")
            for kind in ("dependencies", "indexes", "audit_events"):
                self.assertEqual(k[kind]["state"], "unsupported", (conn, kind))
        self.assertEqual(set(rec["connections"]), {"qa-main", "qa-billing"})  # approved only
        self.assertEqual(dbconnector.sanitize_tools(self.listing("describe_table"), SERVER), ["describe_table"])

    def test_without_a_listing_nothing_is_claimed(self):
        from chitragupta import dbconnector
        rec = dbconnector.assess(self.cfg())
        for k in ("connection_inventory", "describe", "catalog_read"):
            self.assertEqual(rec["connector"]["capabilities"][k]["state"], "not_verified", k)
        self.assertEqual(rec["connections"]["qa-main"]["capabilities"]["tables"]["state"], "not_verified")
        cfg = self.sc.config()
        cfg["mcp"]["database"].pop("server")
        write_config(self.home, cfg)
        rec = dbconnector.assess(self.sc.cfg())
        self.assertTrue(all(v["state"] == "unsupported" for v in rec["connector"]["capabilities"].values()))

    def test_observed_outcomes_override_and_never_become_empty_lists(self):
        from chitragupta import dbconnector
        rec = dbconnector.assess(self.cfg(), self.listing("describe_table", "run_query", "list_tables"))
        dbconnector.observe_call(rec, "procedures", "permission_denied", connection="qa-billing")
        dbconnector.observe_call(rec, "object_list", "timeout")
        dbconnector.observe_call(rec, "tables", "ok", connection="qa-main")
        k = rec["connections"]["qa-billing"]["capabilities"]["procedures"]
        self.assertEqual((k["state"], k["provenance"]), ("permission_denied", "observed_call"))
        self.assertEqual(rec["connector"]["capabilities"]["object_list"]["state"], "not_verified")
        self.assertTrue(rec["connections"]["qa-main"]["last_authorized"])
        self.assertTrue(rec["connections"]["qa-billing"]["last_denied"])
        dbconnector.save_capabilities(rec)
        again = dbconnector.assess(self.cfg(), self.listing("describe_table", "run_query"),
                                   previous=dbconnector.load_capabilities())
        self.assertEqual(again["connections"]["qa-billing"]["capabilities"]["procedures"]["state"], "permission_denied")
        self.assertEqual(again["connector"]["capabilities"]["object_list"]["state"], "not_verified")
        with self.assertRaises(ValueError):
            dbconnector.observe_call(rec, "describe", "empty")

    def test_unknown_engines_get_no_catalog(self):
        from chitragupta import dbconnector
        cfg = self.sc.config()
        cfg["mcp"]["database"]["scope"]["connections"]["qa-docs"] = {"env": "qa", "engine": "mongodb", "schemas": ["app"]}
        write_config(self.home, cfg)
        self.sc.extra = {}
        from chitragupta import mcpjobs
        mcpjobs.ensure_files(self.sc.cfg())
        rec = dbconnector.assess(self.sc.cfg(), self.listing("describe_table", "run_query"))
        k = rec["connections"]["qa-docs"]["capabilities"]
        self.assertEqual(k["catalog_read"]["state"], "unsupported")
        self.assertEqual(k["procedures"]["state"], "unsupported")
        self.assertIsNone(dbconnector.adapter_for("mongodb"))


class Mapping(Case):
    def test_different_argument_names_through_a_validated_mapping(self):
        from chitragupta import common, dbconnector, mcpjobs
        from chitragupta.safety import mcp
        cfg = self.sc.config()
        cfg["mcp"]["database"]["tools"].update(query="execute_sql", describe_table="get_table_schema")
        cfg["mcp"]["database"]["arguments"] = {"connection": "conn_id", "sql": "statement", "schema": "schemaName",
                                               "table": "tableName", "limit": "limit"}
        write_config(self.home, cfg)
        allow = mcpjobs.ensure_files(common.load_config())
        cfg = common.load_config()
        meta = self.sc.metadata("qa-main", "shop", "orders")
        sql, _ = dbconnector.build_select(dbconnector.adapter_for("postgres"), meta, ["status"], TENANTS,
                                          permitted=self.permitted("qa-main"))
        q = dbconnector.call_input(cfg, {"role": "query", "connection": "qa-main", "sql": sql, "limit": 20})
        self.assertEqual(q["tool"], "execute_sql")
        self.assertEqual(set(q["input"]), {"conn_id", "statement", "limit"})
        self.assertIsNone(mcp.mcp_reason(mcpjobs.mcp_tool(SERVER, q["tool"]), q["input"], allow))
        d = dbconnector.call_input(cfg, {"role": "describe_table", "connection": "qa-main", "schema": "shop",
                                         "table": "orders"})
        self.assertEqual(d["input"], {"conn_id": "qa-main", "schemaName": "shop", "tableName": "orders"})
        self.assertIsNone(mcp.mcp_reason(mcpjobs.mcp_tool(SERVER, d["tool"]), d["input"], allow))
        bad = dict(d["input"], schemaName="reporting")  # the mapped schema argument is scope-checked too
        self.assertIsNotNone(mcp.mcp_reason(mcpjobs.mcp_tool(SERVER, d["tool"]), bad, allow))

    def test_an_invalid_mapping_fails_closed(self):
        from chitragupta import dbconnector
        for args in ({"connection": "target"}, {"connection": "environment"}, {"sql": "body"},
                     {"schema": "database"}, {"table": "server"}, {"limit": "max_rows"}, {"params": "sql"},
                     {"schema": "tbl", "table": "tbl"}, {"connection": "conn id"}, {"colour": "x"}, "connectionId"):
            with self.subTest(args=args):
                cfg = self.sc.config()
                cfg["mcp"]["database"]["arguments"] = args
                write_config(self.home, cfg)
                c = self.sc.cfg()
                mapping, errs, _ = dbconnector.argument_mapping(c)
                self.assertIsNone(mapping)
                self.assertTrue(errs)
                rec = dbconnector.assess(c, ["mcp__%s__describe_table" % SERVER, "mcp__%s__run_query" % SERVER])
                self.assertEqual(rec["connector"]["capabilities"]["describe"]["state"], "unsupported")
                self.assertIn("argument mapping invalid", rec["connector"]["capabilities"]["describe"]["reason"])
                with self.assertRaises(dbconnector.BuildError):
                    dbconnector.call_input(c, {"role": "describe_table", "connection": "qa-main", "schema": "shop",
                                               "table": "orders"})

    def test_every_call_names_its_connection(self):
        from chitragupta import dbconnector
        with self.assertRaises(dbconnector.BuildError):
            dbconnector.call_input(self.cfg(), {"role": "query", "connection": "", "sql": "SELECT 1"})
        self.assertEqual(dbconnector.call_input(self.cfg(), {"role": "list_connections"})["input"], {})


class Adapters(Case):
    """DBT-14 (adapter half): both engines build, quote and limit their own way; the statements pass the guard and
    the approved scope; the fake MCP evaluates them."""

    def run_calls(self, *calls):
        fake = FakeTransport(self.sc)
        return fake, fake.execute("test", list(calls), self.cfg(), {})

    def test_two_engines_two_connections(self):
        from chitragupta import dbconnector
        from chitragupta.safety import query
        pg, ms = dbconnector.adapter_for("postgres"), dbconnector.adapter_for("mssql")
        orders = self.sc.metadata("qa-main", "shop", "orders")
        refunds = self.sc.metadata("qa-billing", "billing", "refunds")
        s1, _ = dbconnector.build_select(pg, orders, ["tenant_id", "id", "status"], TENANTS, [("id", "DESC")], 20,
                                         permitted=self.permitted("qa-main"))
        self.assertEqual(s1, 'SELECT "tenant_id", "id", "status" FROM "shop"."orders" WHERE "tenant_id" = 7 AND '
                             '"id" = 1001 ORDER BY "id" DESC LIMIT 20')
        # the event carries orderRef = str(orders.id) and the consumer maps tenantId -> tenant_code by a lookup:
        # the destination key is the documented string, never the source integer coerced
        s2, _ = dbconnector.build_select(ms, refunds, ["order_ref", "status"],
                                         [("tenant_code", "T007"), ("order_ref", "1001")], limit=5,
                                         permitted=self.permitted("qa-billing"))
        self.assertEqual(s2, "SELECT TOP (5) [order_ref], [status] FROM [billing].[refunds] WHERE [tenant_code] = "
                             "N'T007' AND [order_ref] = N'1001'")
        with self.assertRaises(dbconnector.BuildError):
            dbconnector.build_select(ms, refunds, ["status"], [("order_ref", 1001)])
        for sql, eng, conn in ((s1, "postgres", "qa-main"), (s2, "mssql", "qa-billing")):
            self.assertIsNone(query.sql_reason(sql, 20))
            self.assertIsNone(query.statement_scope_reason(sql, eng, self.permitted(conn)))
            self.assertIsNotNone(parse_select(eng, sql))
        fake, res = self.run_calls({"call_id": "a", "role": "query", "connection": "qa-main", "sql": s1, "limit": 20},
                                   {"call_id": "b", "role": "query", "connection": "qa-billing", "sql": s2, "limit": 5})
        self.assertEqual([r["status"] for r in res], ["ok", "ok"])
        self.assertEqual(res[0]["rows"], [{"tenant_id": 7, "id": 1001, "status": "cancelled"}])
        self.assertEqual(res[1]["rows"], [])  # the refund isn't there yet: an empty bounded read, not an error
        self.sc.refund_visible = True
        _, res = self.run_calls({"call_id": "b", "role": "query", "connection": "qa-billing", "sql": s2, "limit": 5})
        self.assertEqual(res[0]["rows"], [{"order_ref": "1001", "status": "pending"}])
        self.assertEqual(len(fake.calls), 2)

    def test_quoting_and_limits(self):
        from chitragupta import dbconnector
        pg, ms = dbconnector.adapter_for("postgres"), dbconnector.adapter_for("mssql")
        self.assertEqual(pg.quote('x"y'), '"x""y"')
        self.assertEqual(ms.quote("x]y"), "[x]]y]")
        self.assertEqual(pg.quote("Order Lines"), '"Order Lines"')
        meta = dbconnector.Metadata("postgres", "shop", 'we"ird', [{"name": 'co"l', "class": "integer"}])
        sql, _ = dbconnector.build_select(pg, meta, ['co"l'], [('co"l', 5)], limit=3)
        self.assertEqual(sql, 'SELECT "co""l" FROM "shop"."we""ird" WHERE "co""l" = 5 LIMIT 3')
        msm = dbconnector.Metadata("mssql", "billing", "re]f", [{"name": "c]1", "class": "integer"}])
        sql, _ = dbconnector.build_select(ms, msm, ["c]1"], [("c]1", 5)], limit=3)
        self.assertEqual(sql, "SELECT TOP (3) [c]]1] FROM [billing].[re]]f] WHERE [c]]1] = 5")
        orders = self.sc.metadata("qa-main", "shop", "orders")
        for lim in (0, 21, 500, "20", True, None):
            with self.subTest(limit=lim):
                with self.assertRaises(dbconnector.BuildError):
                    dbconnector.build_select(pg, orders, ["id"], TENANTS, limit=lim)

    def test_unqualified_or_wider_reads_are_never_built(self):
        from chitragupta import dbconnector
        pg = dbconnector.adapter_for("postgres")
        orders = self.sc.metadata("qa-main", "shop", "orders")
        for cols, preds in (([], TENANTS), (["id"], [])):
            with self.subTest(cols=cols, preds=preds):
                with self.assertRaises(dbconnector.BuildError):
                    dbconnector.build_select(pg, orders, cols, preds)
        with self.assertRaises(dbconnector.BuildError):  # a column the metadata doesn't have
            dbconnector.build_select(pg, orders, ["password"], TENANTS)
        with self.assertRaises(dbconnector.BuildError):  # outside the connection's approved scope
            audit = self.sc.metadata("qa-billing", "billing", "refunds")
            dbconnector.build_select(dbconnector.adapter_for("mssql"), audit, ["status"], [("order_ref", "1")],
                                     permitted={"databases": ["billing"], "schemas": ["other"]})
        with self.assertRaises(dbconnector.BuildError):
            dbconnector.build_select(pg, orders, ["id"], [("id", list(range(21)))])
        sql, _ = dbconnector.build_select(pg, orders, ["id"], [("tenant_id", 7), ("id", [1001, 1002])])
        self.assertIn('"id" IN (1001, 1002)', sql)

    def test_same_named_columns_elsewhere_are_not_joined(self):
        """An `id` of the unrelated shop_audit.orders isn't a key of shop.orders: separate metadata, separate reads."""
        from chitragupta import dbconnector
        pg = dbconnector.adapter_for("postgres")
        audit = self.sc.metadata("qa-main", "shop_audit", "orders")
        sql, _ = dbconnector.build_select(pg, audit, ["id", "status"], [("tenant_id", 7), ("id", 1001)])
        self.assertIn('FROM "shop_audit"."orders"', sql)
        _, res = self.run_calls({"call_id": "a", "role": "query", "connection": "qa-main", "sql": sql, "limit": 20})
        self.assertEqual(res[0]["rows"], [{"id": 1001, "status": "archived"}])
        self.assertNotIn("JOIN", sql.upper())


class Hostile(Case):
    """DBT-17: quotes, semicolons, control characters, malicious identifiers and side-effect functions fail safely."""

    def test_values_stay_literals(self):
        from chitragupta import dbconnector
        from chitragupta.safety import query
        ms = dbconnector.adapter_for("mssql")
        refunds = self.sc.metadata("qa-billing", "billing", "refunds")
        for v in ("O'Brien", "1001'; DROP TABLE billing.refunds; --", "x' OR '1'='1", "pg_sleep(10)",
                  "a /* b */ c", "a -- b", "xp_cmdshell('dir')"):
            with self.subTest(value=v):
                v = v[:32]
                sql, _ = dbconnector.build_select(ms, refunds, ["status"], [("order_ref", v)])
                self.assertIn("N'%s'" % v.replace("'", "''"), sql)
                self.assertIsNone(query.sql_reason(sql, 20), sql)
                self.assertIsNone(query.statement_scope_reason(sql, "mssql", self.permitted("qa-billing")), sql)
        for v in ("a\nb", "a\x00b", "a\x1bb", "a\\b", "tab\tkey", "x" * 33, ""):
            with self.subTest(declined=repr(v)):
                with self.assertRaises(dbconnector.BuildError) as cm:
                    dbconnector.build_select(ms, refunds, ["status"], [("order_ref", v)])
                self.assertEqual(cm.exception.gap, "mapping_unresolved")

    def test_hostile_identifiers_are_refused(self):
        from chitragupta import dbconnector
        pg, ms = dbconnector.adapter_for("postgres"), dbconnector.adapter_for("mssql")
        for adapter in (pg, ms):
            for ident in ('orders"; DROP TABLE x; --', "orders]; DROP TABLE x; --", "a--b", "a/*b", "a;b", "o'rders",
                          "shop.orders", "a\nb", "a\\b", "", " orders", "x" * 129, "`t`"):
                with self.subTest(engine=adapter.engine, ident=ident):
                    with self.assertRaises(dbconnector.BuildError):
                        adapter.quote(ident)
        with self.assertRaises(dbconnector.BuildError):
            ms.quote('a"b')  # the guard refuses a quote inside [brackets]
        meta = dbconnector.Metadata("postgres", "shop", "orders; DROP TABLE x", [{"name": "id", "class": "integer"}])
        with self.assertRaises(dbconnector.BuildError):
            dbconnector.build_select(pg, meta, ["id"], [("id", 1)])

    def test_side_effect_functions_never_get_in(self):
        from chitragupta import dbconnector
        pg = dbconnector.adapter_for("postgres")
        orders = self.sc.metadata("qa-main", "shop", "orders")
        for bad in ("pg_sleep(10)", "nextval('s')", "1; DELETE FROM shop.orders"):
            with self.subTest(column=bad):
                with self.assertRaises(dbconnector.BuildError):
                    dbconnector.build_select(pg, orders, [bad], TENANTS)
            with self.subTest(integer_key=bad):
                with self.assertRaises(dbconnector.BuildError):
                    dbconnector.build_select(pg, orders, ["id"], [("id", bad)])
        with self.assertRaises(dbconnector.BuildError):
            dbconnector.build_select(pg, orders, ["id"], TENANTS, order_by=[("id", "DESC; DROP TABLE x")])

    def test_catalog_names_are_refused_not_stripped(self):
        from chitragupta import dbconnector, mcpjobs
        pg = dbconnector.adapter_for("postgres")
        self.assertEqual(pg.catalog("triggers", "shop", ["orders"]), mcpjobs.triggers_sql("postgres", "shop", ["orders"]))
        for schema, names in (("sh-op", ["orders"]), ("shop", ["or'ders"]), ("shop", ["orders; x"])):
            with self.subTest(schema=schema, names=names):
                with self.assertRaises(dbconnector.BuildError):
                    pg.catalog("triggers", schema, names)
        self.assertEqual(mcpjobs._ident("or'ders"), "orders")  # what the old helper would have silently queried


class Dialects(Case):
    """DBT-18: unsupported dialects and parameters fail closed; string keys are never rewritten."""

    def test_no_adapter_no_statement(self):
        from chitragupta import dbconnector
        for eng in ("mongodb", "mysql", "", None, "oracle"):
            with self.subTest(engine=eng):
                self.assertIsNone(dbconnector.adapter_for(eng))
                with self.assertRaises(dbconnector.BuildError):
                    dbconnector.build_select(None, self.sc.metadata("qa-main", "shop", "orders"), ["id"], TENANTS)
        with self.assertRaises(dbconnector.BuildError):  # metadata of one engine, adapter of another
            dbconnector.build_select(dbconnector.adapter_for("mssql"), self.sc.metadata("qa-main", "shop", "orders"),
                                     ["id"], TENANTS)

    def test_string_keys_stay_exactly_as_written(self):
        from chitragupta import dbconnector
        ms = dbconnector.adapter_for("mssql")
        refunds = self.sc.metadata("qa-billing", "billing", "refunds")
        for v in ("00123", " 1001", "1001 ", "1,001", "ABC-1001"):
            with self.subTest(value=v):
                sql, _ = dbconnector.build_select(ms, refunds, ["status"], [("order_ref", v)])
                self.assertIn("[order_ref] = N'%s'" % v, sql)
        orders = self.sc.metadata("qa-main", "shop", "orders")
        pg = dbconnector.adapter_for("postgres")
        for v in ("00123", "1,001", " 7", "7.0", "1e3", "0x1F", 7.5, True, None):
            with self.subTest(integer=v):
                with self.assertRaises(dbconnector.BuildError):
                    dbconnector.build_select(pg, orders, ["id"], [("tenant_id", v), ("id", 1001)])
        self.assertIn('"tenant_id" = 7', dbconnector.build_select(pg, orders, ["id"], [("tenant_id", "7")])[0])

    def test_types_without_an_encoder_are_declined(self):
        from chitragupta import dbconnector
        for t in ("jsonb", "bytea", "xml", "boolean", "float", "money", "integer[]", "geometry", ""):
            with self.subTest(type=t):
                self.assertEqual(dbconnector.column_class(t), (None, None))
        self.assertEqual(dbconnector.column_class("varchar(32)"), ("string", 32))
        self.assertEqual(dbconnector.column_class("character varying(8)"), ("string", 8))
        self.assertEqual(dbconnector.column_class("timestamp with time zone"), ("datetime", None))
        meta = dbconnector.Metadata("postgres", "shop", "docs", [{"name": "body", "class": None, "data_type": "jsonb"}])
        with self.assertRaises(dbconnector.BuildError):
            dbconnector.build_select(dbconnector.adapter_for("postgres"), meta, ["body"], [("body", "{}")])

    def test_typed_encoders(self):
        from chitragupta import dbconnector
        ms = dbconnector.adapter_for("mssql")
        col = lambda c, n=None: {"name": "x", "class": c, "max_length": n}  # noqa: E731
        self.assertEqual(ms.literal("2026-11-06", col("date")).sql, "N'2026-11-06'")
        self.assertEqual(ms.literal("2026-11-06T10:42:00", col("datetime")).sql, "N'2026-11-06T10:42:00'")
        self.assertEqual(ms.literal("-12.50", col("decimal")).sql, "-12.50")
        self.assertEqual(ms.literal("0b7c2d52-2a5e-4c3e-9e1a-3f2d8c4b5a61", col("uuid")).sql,
                         "N'0b7c2d52-2a5e-4c3e-9e1a-3f2d8c4b5a61'")
        for v, c in (("2026-13-01", "date"), ("2026-11-06 25:00", "datetime"), ("12,5", "decimal"), ("1", "uuid"),
                     (12, "decimal")):
            with self.subTest(value=v, cls=c):
                with self.assertRaises(dbconnector.BuildError):
                    ms.literal(v, col(c))

    def test_bound_parameters_only_when_mapped(self):
        from chitragupta import dbconnector
        pg, ms = dbconnector.adapter_for("postgres"), dbconnector.adapter_for("mssql")
        orders = self.sc.metadata("qa-main", "shop", "orders")
        with self.assertRaises(dbconnector.BuildError):
            dbconnector.build_select(pg, orders, ["id"], [("id", dbconnector.Param(1001))])
        sql, bound = dbconnector.build_select(pg, orders, ["id"], TENANTS, params=True)
        self.assertEqual((sql.split(" WHERE ")[1], bound), ('"tenant_id" = $1 AND "id" = $2 LIMIT 20', ["7", "1001"]))
        refunds = self.sc.metadata("qa-billing", "billing", "refunds")
        sql, bound = dbconnector.build_select(ms, refunds, ["status"], [("order_ref", "00123")], params=True)
        self.assertTrue(sql.endswith("[order_ref] = @p1"))
        self.assertEqual(bound, ["00123"])
        with self.assertRaises(dbconnector.BuildError):
            dbconnector.build_select(pg, orders, ["id"], [("id", "00123")], params=True)
        lit = pg.literal(7, orders.column("tenant_id"))
        self.assertIn('"tenant_id" = 7', dbconnector.build_select(pg, orders, ["id"], [("tenant_id", lit)])[0])
        with self.assertRaises(dbconnector.BuildError):  # a literal made for another column type
            dbconnector.build_select(pg, orders, ["id"], [("status", lit)])

    def test_missing_metadata_is_a_gap_not_an_empty_table(self):
        from chitragupta import dbconnector
        with self.assertRaises(dbconnector.BuildError) as cm:
            dbconnector.Metadata.from_describe("mssql", "billing", "usp_issue_refund", [])
        self.assertEqual(cm.exception.gap, "definition_unavailable")


class Transports(Case):
    def test_the_fake_refuses_what_the_guard_refuses_and_records_every_call(self):
        fake = FakeTransport(self.sc)
        calls = [{"call_id": "1", "role": "query", "connection": "qa-main", "sql": "SELECT id FROM shop.orders LIMIT 5"},
                 {"call_id": "2", "role": "query", "connection": "qa-main", "sql": "SELECT id FROM reporting.x LIMIT 5"},
                 {"call_id": "3", "role": "query", "connection": "qa-main", "sql": "DELETE FROM shop.orders"},
                 {"call_id": "4", "role": "describe_table", "connection": "qa-reporting", "schema": "shop",
                  "table": "orders"},
                 {"call_id": "5", "role": "describe_table", "connection": "qa-billing", "schema": "billing",
                  "table": "refunds"},
                 {"call_id": "6", "role": "query", "connection": "uat-main", "sql": 'SELECT "id" FROM "shop"."orders" '
                                                                                    'WHERE "id" = 1 LIMIT 5'}]
        res = fake.execute("t", calls, self.cfg(), {})
        st = {r["call_id"]: (r["status"], r["error"]) for r in res}
        self.assertEqual(st["1"], ("error", "unsupported statement"))  # passes the guard, not the builder's shape
        self.assertIn("outside the approved scope", st["2"][1])
        self.assertIn("safety guard", st["3"][1])
        self.assertIn("safety guard", st["4"][1])  # qa-reporting isn't an allowed connection at all
        self.assertEqual(st["5"][0], "ok")
        self.assertIn("safety guard", st["6"][1])
        self.assertEqual(len(fake.calls), 6)
        fake.denied.add("qa-billing")
        self.assertEqual(fake.execute("t", calls[4:5], self.cfg(), {})[0]["status"], "permission_denied")
        fake.timeouts.add("qa-main")
        q = {"call_id": "7", "role": "describe_table", "connection": "qa-main", "schema": "shop", "table": "orders"}
        self.assertEqual(fake.execute("t", [q], self.cfg(), {})[0]["status"], "missing")

    def test_partial_pages_say_so(self):
        fake = FakeTransport(self.sc)
        fake.page_size = 1
        r = fake.execute("t", [{"call_id": "1", "role": "list_tables", "connection": "qa-main", "schema": "shop"}],
                         self.cfg(), {})[0]
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["rows"][-1]["more_available"])

    def test_mcp_job_transport_matches_exact_calls_only(self):
        from chitragupta import dbconnector, mcpjobs
        from chitragupta.safety import mcp as guard
        pg = dbconnector.adapter_for("postgres")
        sql, _ = dbconnector.build_select(pg, self.sc.metadata("qa-main", "shop", "orders"), ["status"], TENANTS)
        seen = {}

        def fake_run(job, servers, reads, writes, task, schema, cfg, health, **k):
            seen.update(job=job, servers=servers, task=task, **k)
            q = mcpjobs.mcp_tool(SERVER, "run_query")
            d = mcpjobs.mcp_tool(SERVER, "describe_table")
            return {"calls": [
                {"name": q, "input": {"connectionId": "qa-main", "sql": sql + " ", "limit": 20},
                 "result": "1 row(s) in 1ms\n\n" + json.dumps([{"status": "cancelled"}]), "is_error": False},
                {"name": q, "input": {"connectionId": "qa-main", "sql": sql.replace("1001", "1002"), "limit": 20},
                 "result": json.dumps([{"status": "open"}]), "is_error": False},  # changed: not ours
                {"name": d, "input": {"connectionId": "qa-billing", "schema": "billing", "table": "refunds"},
                 "result": "permission denied for table refunds", "is_error": True}],
                "out": {"done": True}}
        real = mcpjobs.run
        mcpjobs.run = fake_run
        try:
            res = dbconnector.McpJobTransport().execute("j_0123456789abcdef", [
                {"call_id": "a", "role": "query", "connection": "qa-main", "sql": sql, "limit": 20},
                {"call_id": "b", "role": "describe_table", "connection": "qa-billing", "schema": "billing",
                 "table": "refunds"},
                {"call_id": "c", "role": "describe_table", "connection": "qa-main", "schema": "shop", "table": "orders"}],
                self.cfg(), {}, pin="x.pin.json", deadline_s=60)
        finally:
            mcpjobs.run = real
        by = {r["call_id"]: r for r in res}
        self.assertEqual((by["a"]["status"], by["a"]["rows"]), ("ok", [{"status": "cancelled"}]))
        self.assertEqual(by["a"]["statement"], sql)
        self.assertEqual(by["b"]["status"], "permission_denied")
        self.assertEqual(by["c"]["status"], "missing")
        self.assertEqual((seen["keep_stream"], seen["pin"], seen["timeout"]), (False, "x.pin.json", 60))
        self.assertEqual(sorted(seen["servers"][SERVER]), ["describe_table", "run_query"])
        self.assertIn(json.dumps({"connectionId": "qa-main", "sql": sql, "limit": 20}), seen["task"])
        self.assertEqual(guard.call_statement({"sql": sql + " "}), sql)


if __name__ == "__main__":
    unittest.main()
