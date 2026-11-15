"""Honest capability states (DBT-38; DB-01, DB-17): no database MCP, no credentials (every call denied) and missing
tools give `unsupported` / `not_verified` / `permission_denied` with their reasons in the dashboard workspace, the CLI
and the MCP text, never a silent "works" or an empty list; and the whole database test set runs with the
`mcpjobs.run` tripwire armed (tests.support.Isolated), so nothing reaches a real service."""
import contextlib
import io
import unittest

from tests.dbfixture import SERVER, FakeTransport, Scenario, write_ticket
from tests.support import Isolated, write_config


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()
        write_ticket(self.home)
        from chitragupta import dbingest
        dbingest.run(self.cfg())

    def cfg(self):
        return self.sc.cfg()

    def ctx(self):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg())

    def cli(self, *argv):
        from chitragupta import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def mcp_table(self, name):
        from chitragupta import mcpserver
        return mcpserver.call_tool("get_db_table", {"name": name})["content"][0]["text"]


class States(Case):
    """DBT-38."""

    def test_no_database_mcp_is_said_everywhere(self):
        from chitragupta import dbconnector, dbviews
        from chitragupta.dashboard import dbmap
        cfg = self.sc.config()
        cfg["mcp"]["database"].pop("server")
        write_config(self.home, cfg)
        rec = dbconnector.assess(self.cfg())
        self.assertTrue(all(v["state"] == "unsupported" and "no database MCP configured" in v["reason"]
                            for v in rec["connector"]["capabilities"].values()), rec["connector"])
        code, ws = dbmap.view("/api/db/workspace", lambda k: "", self.cfg())
        self.assertEqual(code, 200)
        self.assertIn("no_database_mcp", [s["id"] for s in ws["states"]])
        text = dbviews.render_text(ws)
        self.assertIn("No database MCP configured", text)
        self.assertIn("Name the database MCP server in /cg:setup", text)

    def test_missing_tools_are_unsupported_with_reasons(self):
        from chitragupta import dbconnector
        rec = dbconnector.assess(self.cfg(), tools_loaded=["mcp__%s__run_query" % SERVER])  # describe not listed
        dbconnector.save_capabilities(rec)
        caps = rec["connector"]["capabilities"]
        self.assertEqual(caps["describe"]["state"], "unsupported")
        self.assertTrue(caps["describe"]["reason"])
        code, out = self.cli("db", "table", "shop.orders")
        self.assertEqual(code, 0, out)
        for text in (out, self.mcp_table("shop.orders")):
            self.assertIn("## Capabilities not verified or not available", text)
            self.assertIn("on qa-main: Not available (", text)

    def test_no_credentials_means_denied_or_not_verified_never_empty(self):
        from chitragupta import dbconnector, dbflows, dbviews
        fake = FakeTransport(self.sc)
        fake.denied = {"qa-main"}  # the database MCP answers, but this account may not read the connection
        fake.timeouts = {"qa-billing"}  # no answer at all
        res = dbflows.discover_metadata(self.ctx(), ["qa-main:shop.orders", "qa-billing:billing.refunds"], fake,
                                        self.cfg(), {})
        self.assertEqual(len(res["described"]), 0)
        conns = dbconnector.load_capabilities()["connections"]
        states = {c: {k: v["state"] for k, v in conns[c]["capabilities"].items() if v.get("provenance") ==
                      "observed_call"} for c in ("qa-main", "qa-billing")}
        self.assertIn("permission_denied", states["qa-main"].values())
        self.assertIn("not_verified", states["qa-billing"].values())
        self.assertNotIn("supported", set(states["qa-main"].values()) | set(states["qa-billing"].values()))
        ws = dbviews.workspace_summary(self.ctx(), self.cfg(), health={})
        self.assertIn("permission_denied", [s["id"] for s in ws["states"]])
        text = dbviews.render_text(ws)
        self.assertIn("Access denied", text)
        self.assertIn("Not verified", text)
        p = dbviews.table_view(self.ctx(), "billing.refunds")  # the cached card stays: nothing was emptied
        self.assertGreater(p["data"]["columns_count"], 0)


class Tripwire(Case):
    def test_the_real_transport_is_never_reached(self):
        from chitragupta import dbconnector, mcpjobs
        with self.assertRaises(mcpjobs.JobError):
            mcpjobs.run("db-probe")
        self.assertEqual(self.forbidden, ["mcpjobs.run(db-probe)"])
        self.forbidden.clear()
        try:
            dbconnector.McpJobTransport().execute("db-probe", [{"call_id": "c1", "role": "describe_table",
                                                                "connection": "qa-main", "schema": "shop",
                                                                "table": "orders"}], self.cfg(), {})
        except mcpjobs.JobError:
            pass
        self.assertTrue(self.forbidden and all(f.startswith("mcpjobs.run(") for f in self.forbidden), self.forbidden)
        self.forbidden.clear()  # seen: every database test runs with this tripwire armed


if __name__ == "__main__":
    unittest.main()
