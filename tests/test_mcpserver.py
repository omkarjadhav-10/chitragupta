"""The read-only MCP server (chitragupta/mcpserver.py): protocol, the 8 tools on a synthetic home, size caps and
pages, errors, a clean stdout, nothing written, and its isolation from Chitragupta's own background calls."""
import io
import json
import os
import subprocess
import sys
import unittest

from tests import support
from tests.test_dashboard import TICKET, Base, _w, snapshot

TOOLS = ["search", "get_ticket", "get_day", "get_timesheet", "get_branch_status", "get_db_table", "list_tickets",
         "get_alerts"]


def srv():
    from chitragupta import mcpserver
    return mcpserver


def add_knowledge(home):
    """A catalog table with a schema card, a verified link, a writer, and branch tracking on ABC-1001."""
    _w(os.path.join(home, "data", "catalog.json"), {
        "tables": {"shop.orders": {"name": "shop.orders", "db": "shopdb", "purpose": "One row per order",
                                   "key_columns": ["id"], "tickets": [TICKET], "last_seen": "2026-10-15"}},
        "db_relations": {"r1": {"from": "shop.order_items.order_id", "to": "shop.orders.id", "kind": "fk",
                                "tickets": [TICKET]}},
        "db_writers": {"w1": {"table": "shop.orders", "writes": {"OrderService|total": {"component": "OrderService",
                                                                                     "column": "total",
                                                                                     "change": "sets the total"}},
                              "reads": {"ReportJob": {"component": "ReportJob"}}}},
        "apis": {"a1": {"method": "post", "path": "/orders", "host_or_service": "api.acme.test", "purpose": "Create an order",
                        "tickets": [TICKET]}}})
    _w(os.path.join(home, "data", "db-schema", "qa", "shop.orders.json"),
       {"table": "shop.orders", "describe": [{"name": "id"}, {"name": "total"}, {"name": "status"}]})
    _w(os.path.join(home, "data", "db-schema", "_linkmap.json"), {"env": "qa", "fetched_at": "2026-10-15", "result": {
        "links": [{"from": "shop.order_items.order_id", "to": "shop.orders.id", "sampled": 10, "matched": 9}],
        "hierarchy": [{"database": "shopdb", "schema": "shop", "tables": ["orders", "order_items"]}]}})
    p = os.path.join(home, "data", "tickets", TICKET + ".json")
    with open(p, encoding="utf-8") as f:
        tk = json.load(f)
    tk["git"] = {"summary": "1 branch, 1 PR", "repos": {"shop-api": {
        "strategy": "gitflow", "strategy_how": "config", "pr_source": "gh",
        "branches": [{"branch": "feature/ABC-1001-order-total", "parent": "develop", "status": "open", "prs": [7]}],
        "prs": [{"number": 7, "source": "feature/ABC-1001-order-total", "target": "develop", "state": "open"}],
        "targets": [], "rows": []}},
        "alerts": [{"kind": "gap", "text": "The fix is on develop but not on qa", "repo": "shop-api"}]}
    _w(p, tk)


class McpBase(Base):
    def setUp(self):
        super().setUp()
        add_knowledge(self.home)
        from chitragupta.dashboard import search
        search.reset()
        self.server = srv().Server()

    def rpc(self, method, params=None, id_=1):
        msg = {"jsonrpc": "2.0", "id": id_, "method": method}
        if params is not None:
            msg["params"] = params
        return self.server.handle(msg)

    def call(self, tool, /, **args):
        r = self.rpc("tools/call", {"name": tool, "arguments": args})
        self.assertIn("result", r, r)
        return r["result"]

    def text(self, tool, /, **args):
        res = self.call(tool, **args)
        self.assertFalse(res["isError"], res["content"][0]["text"])
        return res["content"][0]["text"]

    def error_text(self, tool, /, **args):
        res = self.call(tool, **args)
        self.assertTrue(res["isError"], res)
        return res["content"][0]["text"]


# --------------------------------------------------------------------------- protocol

class TestProtocol(McpBase):
    def test_initialize_echoes_supported_versions(self):
        m = srv()
        for v in ("2024-11-05", "2025-03-26", "2025-06-18"):
            r = self.rpc("initialize", {"protocolVersion": v, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
            self.assertEqual(r["result"]["protocolVersion"], v)
        r = self.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertEqual(r["capabilities"], {"tools": {}})
        self.assertEqual(r["serverInfo"]["name"], "chitragupta")
        self.assertIn("before reading", r["instructions"])
        self.assertEqual(self.rpc("initialize")["result"]["protocolVersion"], m.LATEST)

    def test_ping_notifications_and_errors(self):
        self.assertEqual(self.rpc("ping"), {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}))
        self.assertEqual(self.rpc("resources/list")["error"]["code"], -32601)
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "no/such"}))  # notification: no reply
        self.assertEqual(self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": [1]})["error"]["code"],
                         -32602)
        self.assertEqual(self.rpc("tools/call", {"arguments": {}})["error"]["code"], -32602)
        r = self.rpc("tools/call", {"name": "write_file", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("Unknown tool", r["error"]["message"])
        self.assertEqual(self.rpc("tools/call", {"name": "search", "arguments": "q"})["error"]["code"], -32602)
        self.assertEqual(self.server.handle_line("{not json")["error"]["code"], -32700)
        self.assertEqual(self.server.handle({"id": 3, "method": "ping"})["error"]["code"], -32600)
        self.assertIsNone(self.server.handle_line("   "))
        batch = self.server.handle_line(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                                    {"jsonrpc": "2.0", "method": "notifications/initialized"}]))
        self.assertEqual(batch, [{"jsonrpc": "2.0", "id": 1, "result": {}}])

    def test_tools_list_shape_and_size(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], TOOLS)
        for t in tools:
            self.assertLessEqual(len(t["description"]), 140, t["name"])
            self.assertEqual(t["inputSchema"]["type"], "object")
            for r in t["inputSchema"].get("required", []):
                self.assertIn(r, t["inputSchema"]["properties"])
        self.assertLess(len(json.dumps(tools, separators=(",", ":"))), 3500)  # ~800 tokens if loaded upfront

    def test_tool_argument_errors_are_results(self):
        self.assertIn("`q` is required", self.error_text("search"))
        self.assertIn("ticket key", self.error_text("get_ticket", key="../../etc"))
        self.assertIn("No ticket ABC-9999", self.error_text("get_ticket", key="ABC-9999"))
        self.assertIn("section", self.error_text("get_ticket", key=TICKET, section="secrets"))
        self.assertIn("`date` must be", self.error_text("get_day", date="last tuesday"))
        self.assertIn("`period`", self.error_text("get_timesheet", period="year"))
        self.assertIn("after", self.error_text("get_timesheet", period="day", **{"from": "2026-10-10", "to": "2026-10-01"}))
        self.assertIn("must be a number", self.error_text("search", q="x", limit="lots"))
        self.assertIn("must be a string", self.error_text("get_ticket", key=5))

    def test_a_crashing_tool_is_an_error_result_and_logged(self):
        m = srv()
        real = m.HANDLERS["get_alerts"]
        m.HANDLERS["get_alerts"] = lambda a, c: 1 / 0
        try:
            self.assertIn("get_alerts failed: ZeroDivisionError", self.error_text("get_alerts"))
        finally:
            m.HANDLERS["get_alerts"] = real
        self.assertIn("ZeroDivisionError", support.rd(os.path.join(self.home, ".worklog", "logs", "mcp-server.log")))


# --------------------------------------------------------------------------- tools

class TestTools(McpBase):
    def test_search(self):
        t = self.text("search", q="discount")
        self.assertIn("Discount applied twice", t)
        self.assertIn(os.path.join(self.home, "tickets", TICKET, "README.md"), t)
        t = self.text("search", q=TICKET)
        self.assertIn("## Tickets", t)
        self.assertIn("No matches", self.text("search", q="zzqqxx"))
        only = self.text("search", q="order", type="query")
        self.assertIn("## Queries", only)
        self.assertNotIn("## Tickets", only)
        self.assertNotIn("ABC-1001", self.text("search", q="order", ticket="ABC-1003").split("\n", 1)[1])

    def test_search_uses_the_index_when_present(self):
        """searchidx (item 1) wins over the dashboard search, with every filter passed through."""
        from unittest import mock
        from chitragupta import searchidx
        seen = {}

        def search(q, types=None, ticket=None, repo=None, frm=None, to=None, limit=40):
            seen.update(q=q, types=types, ticket=ticket, repo=repo, frm=frm, to=to, limit=limit)
            return {"q": q, "engine": "fts5", "took_ms": 1, "total": 1, "groups": [{"type": "query", "label": "Queries", "count": 1,
                    "results": [{"type": "query", "ref": "ABC-1001/q1", "title": "Find the order", "ticket": TICKET,
                                 "repo": "shop-api", "date": "2026-10-15", "href": "", "path": "tickets/ABC-1001/queries.md",
                                 "snippet": "SELECT …", "hits": []}]}]}
        with mock.patch.object(searchidx, "search", search):
            t = self.text("search", q="order", type="query", ticket="abc-1001", repo="shop-api", **{"from": "2026-10-01",
                                                                                                  "to": "2026-10-16"})
        self.assertEqual(seen, {"q": "order", "types": ["query"], "ticket": "ABC-1001", "repo": "shop-api",
                                "frm": "2026-10-01", "to": "2026-10-16", "limit": 40})
        self.assertIn(os.path.join(self.home, "tickets", "ABC-1001", "queries.md"), t)
        self.assertIn("shop-api", t)

    def test_get_ticket_summary_and_sections(self):
        t = self.text("get_ticket", key="abc-1001")
        self.assertIn("# ABC-1001 — Order total is wrong for discounted items", t)
        self.assertIn("Counts: 2 findings · 2 requests · 1 queries", t)
        self.assertIn("Sections (get_ticket section=…)", t)
        self.assertIn("The fix is on develop but not on qa", t)
        want = {"findings": "[root_cause] **Discount applied twice**", "requests": "2. Create order — POST {{shopBaseUrl}}/orders",
                "queries": "SELECT * FROM shop.orders WHERE id = 42", "dbflow": "## Worked example (QA)",
                "branches": "feature/ABC-1001-order-total ← develop", "artifacts": "order-trace.md",
                "timeline": "1h 35m · fixed", "notes": "No notes"}
        for section, needle in want.items():
            s = self.text("get_ticket", key=TICKET, section=section)
            self.assertIn(needle, s, section)
        self.assertIn(os.path.join(self.home, "tickets", TICKET, "requests.md"), self.text("get_ticket", key=TICKET, section="requests"))

    def test_get_day(self):
        d = self.days
        t = self.text("get_day", date="today")
        self.assertIn("# %s — 2h 30m tracked" % d["day"], t)
        self.assertIn("| ABC-1001 | Order total is wrong for discounted items | 1h 35m |", t)
        self.assertIn("Fix order total", t)
        self.assertIn("Jira worklog — not sent (preview", t)
        y = self.text("get_day", date="yesterday")
        self.assertIn("Jira worklog — sent", y)
        self.assertIn("Today's update", y)
        self.assertEqual(self.text("get_day", date="-1"), y)
        self.assertEqual(self.text("get_day", date=d["yday"]), y)

    def test_get_timesheet(self):
        d = self.days
        w = self.text("get_timesheet", period="week", **{"from": d["old"]})
        self.assertIn("| ABC-1002 | Checkout timeout |", w)
        self.assertIn("Other work", w)
        self.assertIn("leave (Family)", w)
        day = self.text("get_timesheet", period="day", **{"from": d["yday"], "to": d["yday"]})
        self.assertIn("| ABC-1003 | Refund report | 3.33 | 3.33 |", day)
        m = self.text("get_timesheet", period="month")
        self.assertIn("per month", m)
        self.assertIn("No tracked time", self.text("get_timesheet", period="day", **{"from": "2001-01-01", "to": "2001-01-02"}))

    def test_get_branch_status(self):
        t = self.text("get_branch_status", key=TICKET)
        self.assertIn("## shop-api · strategy gitflow (config) · PRs from gh", t)
        self.assertIn("PR #7", t)
        self.assertIn("The fix is on develop but not on qa", t)
        self.assertIn("No branch data for repo", self.error_text("get_branch_status", key=TICKET, repo="other"))
        self.assertIn("shop-api", self.text("get_branch_status", key=TICKET, repo="SHOP-API"))
        self.assertIn("No branches recorded", self.text("get_branch_status", key="ABC-1002"))

    def test_get_db_table(self):
        for name in ("shop.orders", "orders", "SHOP.ORDERS"):
            t = self.text("get_db_table", name=name)
            self.assertIn("# shop.orders (shopdb)", t)
        self.assertIn("Columns (3): id, total, status", t)
        self.assertIn("shop.order_items.order_id → shop.orders.id · fk · verified", t)
        self.assertIn("9/10 verified", t)
        self.assertIn("writes: OrderService .total", t)
        self.assertIn("SELECT * FROM shop.orders WHERE id = 42", t)
        self.assertIn("No table", self.error_text("get_db_table", name="shop.nothing"))

    def test_list_tickets(self):
        t = self.text("list_tickets")
        self.assertIn("# Tickets — 3", t)
        self.assertIn("- ABC-1001 · Order total is wrong for discounted items · In Progress", t)
        self.assertIn("⚠ 1 alert(s)", t)
        self.assertIn("# Tickets — 1", self.text("list_tickets", q="checkout"))
        self.assertIn("# Tickets — 1", self.text("list_tickets", status="progress"))
        self.assertIn("# Tickets — 1", self.text("list_tickets", repo="shop-api"))
        self.assertIn("# Tickets — 2", self.text("list_tickets", since="today"))

    def test_get_alerts(self):
        t = self.text("get_alerts")
        self.assertIn("ABC-1001 · The fix is on develop but not on qa (shop-api)", t)
        self.assertIn("Last error:", t)


# --------------------------------------------------------------------------- caps and pages

class TestPages(McpBase):
    def test_paginate(self):
        m = srv()
        text = "\n".join("line %04d %s" % (i, "x" * 90) for i in range(400))
        p1, n = m.paginate(text, 1)
        self.assertGreater(n, 1)
        self.assertLessEqual(len(p1), m.MAX_CHARS)
        self.assertTrue(p1.endswith("… more: page=2 (of %d)" % n))
        last, _ = m.paginate(text, n)
        self.assertIn("(page %d of %d)" % (n, n), last)
        self.assertIn("line 0399", last)
        joined = "".join(m.paginate(text, i)[0] for i in range(1, n + 1))
        for i in (0, 123, 399):
            self.assertIn("line %04d" % i, joined)
        self.assertIn("No page 99", m.paginate(text, 99)[0])
        huge, n2 = m.paginate("y" * 20000, 1)
        self.assertLessEqual(len(huge), m.MAX_CHARS)
        self.assertEqual(n2, 3)
        self.assertEqual(m.paginate("short", 1), ("short", 1))

    def test_big_ticket_is_paged(self):
        p = os.path.join(self.home, "data", "tickets", TICKET + ".json")
        tk = json.loads(support.rd(p))
        for i in range(300):
            tk["items"]["x%d" % i] = {"id": "x%d" % i, "kind": "finding", "title": "Finding number %d" % i,
                                      "detail": "detail " * 40, "date": self.days["day"]}
        _w(p, tk)
        t1 = self.text("get_ticket", key=TICKET, section="findings")
        self.assertLessEqual(len(t1), srv().MAX_CHARS)
        self.assertIn("more: page=2", t1)
        t2 = self.text("get_ticket", key=TICKET, section="findings", page=2)
        self.assertNotEqual(t1, t2)
        self.assertIn("No page 999", self.text("get_ticket", key=TICKET, section="findings", page=999))
        self.assertIn("`page` must be a number", self.error_text("get_ticket", key=TICKET, page="two"))


# --------------------------------------------------------------------------- read-only, stdout, isolation

ALL_CALLS = [("search", {"q": "order"}), ("search", {"q": "discount", "ticket": TICKET, "from": "-30"}),
             ("get_ticket", {"key": TICKET})] + [("get_ticket", {"key": TICKET, "section": s}) for s in
                                                 ("findings", "requests", "queries", "dbflow", "branches", "artifacts",
                                                  "timeline", "notes")] + [
    ("get_day", {"date": "today"}), ("get_day", {"date": "yesterday"}), ("get_timesheet", {"period": "week"}),
    ("get_timesheet", {"period": "month", "from": "-60"}), ("get_branch_status", {"key": TICKET}),
    ("get_db_table", {"name": "orders"}), ("list_tickets", {}), ("get_alerts", {}), ("get_ticket", {"key": "ABC-9999"})]


class TestReadOnly(McpBase):
    def test_every_tool_leaves_the_home_byte_identical(self):
        before, user_before = snapshot(self.home), snapshot(self.user_dir)
        for name, args in ALL_CALLS:
            self.call(name, **args)
            self.assertEqual(snapshot(self.home), before, "%s(%s) wrote into the data home" % (name, args))
        self.assertEqual(snapshot(self.user_dir), user_before)

    def test_serve_writes_only_protocol_lines_to_stdout(self):
        m = srv()
        real = m.HANDLERS["list_tickets"]

        def noisy(a, c):
            print("stray print from a tool")
            return real(a, c)
        m.HANDLERS["list_tickets"] = noisy
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_tickets", "arguments": {}}},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_day", "arguments": {"date": "today"}}}]
        inp = io.BytesIO(("\n".join(json.dumps(x) for x in lines) + "\n").encode())
        out, err = io.BytesIO(), io.StringIO()
        saved = sys.stderr
        sys.stderr = err
        try:
            m.serve(inp, out)
        finally:
            sys.stderr = saved
            m.HANDLERS["list_tickets"] = real
        replies = [json.loads(l) for l in out.getvalue().decode("utf-8").splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertIn("stray print", err.getvalue())
        self.assertNotIn(b"\r\n", out.getvalue())

    def test_launcher_subprocess_stdout_is_clean(self):
        """hooks/run.py mcp in a real process: every stdout line is a JSON-RPC reply."""
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_ticket", "arguments": {"key": TICKET}}}]
        p = subprocess.run([sys.executable, "-B", support.RUN_PY, "mcp"], capture_output=True, timeout=60,
                           input=("\n".join(json.dumps(m) for m in msgs) + "\n").encode(), env=self.child_env())
        self.assertEqual(p.returncode, 0, p.stderr)
        replies = [json.loads(l) for l in p.stdout.decode("utf-8").splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(len(replies[1]["result"]["tools"]), 8)
        self.assertIn("Order total is wrong", replies[2]["result"]["content"][0]["text"])

    def test_internal_calls_get_no_tools(self):
        os.environ["CHITRAGUPTA_INTERNAL"] = "1"
        try:
            s = srv().Server()
            self.assertEqual(s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"], [])
            self.assertEqual(s.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})["result"]["instructions"], "")
            r = s.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_alerts", "arguments": {}}})
            self.assertEqual(r["error"]["code"], -32602)
        finally:
            del os.environ["CHITRAGUPTA_INTERNAL"]


class TestIsolation(unittest.TestCase):
    def test_background_calls_switch_this_plugin_off(self):
        """Every internal claude -p call uses --strict-mcp-config (plugin MCP servers are not started under it) and
        settings that turn this plugin off as well."""
        from chitragupta.safety import claude as builder
        for cfg_path in (None, "x.json"):
            argv = builder.build(["claude"], "x", "none", "haiku", mcp_config=cfg_path)
            settings = json.loads(argv[argv.index("--settings") + 1])
            self.assertEqual(settings["enabledPlugins"], {"cg@chitragupta": False})
            self.assertIn("--strict-mcp-config", argv)
            self.assertEqual(cfg_path is not None, "--mcp-config" in argv)
            self.assertNotIn("--plugin-dir", argv)

    def test_plugin_manifest_and_launchers(self):
        root = support.ROOT
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            man = json.load(f)
        self.assertEqual(man["mcpServers"], {"chitragupta": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/cg-mcp", "args": []}})
        sh = support.rd(os.path.join(root, "bin", "cg-mcp"))
        self.assertTrue(sh.startswith("#!/bin/sh\n"))
        self.assertIn('"$here/../hooks/run.py" mcp', sh)
        cmd = support.rdb(os.path.join(root, "bin", "cg-mcp.cmd"))
        self.assertIn(b"\r\n", cmd)  # cmd.exe wants CRLF (.gitattributes)
        self.assertIn(b'py -3 "%~dp0..\\hooks\\run.py" mcp', cmd)

    @unittest.skipUnless(os.name == "nt", "the .cmd launcher runs on Windows")
    def test_windows_launcher_starts_the_server(self):
        tmp = support.tempfile.mkdtemp(prefix="cg-mcp-")
        try:
            env = support.child_env(tmp, tmp)
            msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            p = subprocess.run([os.path.join(support.ROOT, "bin", "cg-mcp.cmd")], input=(msg + "\n").encode(),
                               capture_output=True, timeout=60, env=env)
            self.assertEqual(json.loads(p.stdout.decode().splitlines()[0])["result"]["serverInfo"]["name"], "chitragupta")
        finally:
            support.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
