"""Shared bounded projections (dbviews, DB-19) and the read surfaces built on them: the envelope, opaque cursors bound
to the principal, the policy and the snapshot (malformed, foreign and stale ones refused), pages capped at 50 items
and 64 KiB with a continuation (never silent truncation), access decided on every page, IDs and names checked, the
required states with their next steps, and DBT-15 (dashboard routes, MCP tools, CLI read commands and repeated reads
make zero database calls and write nothing) and DBT-33 (the graph's edges are the step list's edges; details by ID)."""
import contextlib
import http.client
import io
import json
import os
import unittest
from unittest import mock

from tests.dbfixture import SERVER, FakeTransport, Scenario, write_linkmap, write_ticket
from tests.support import Isolated, write_config

ENVELOPE = {"schema_version", "kind", "id", "profile_generation", "policy_generation", "scope_generation",
            "environment", "snapshot_at", "source_revision", "coverage", "freshness", "capabilities", "state", "states",
            "data", "items", "more_available", "cursor", "links"}


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

    def many_tables(self, n=120):
        from chitragupta import dbmodel, dbstore
        dbstore.upsert("nodes", [dbmodel.db_node(SERVER, "qa-main", "qa", "shop", "shop", "table", "orders_%03d" % i,
                                                 "postgres") for i in range(n)])


class Envelope(Case):
    def test_every_view_has_the_envelope(self):
        from chitragupta import dbflows, dbviews
        ctx = self.ctx()
        f = dbflows.build_flow(ctx, {"kind": "ticket", "key": "ABC-1001"}, save=True)
        for e in (dbviews.workspace_summary(ctx, self.cfg()), dbviews.table_view(ctx, "shop.orders"),
                  dbviews.flow_view(ctx, fid=f["id"]), dbviews.flows_list(ctx), dbviews.resolve(ctx, "orders")):
            self.assertEqual(set(e) - {"page"}, ENVELOPE, e["kind"])
            self.assertEqual(e["policy_generation"], ctx.policy_generation)
            self.assertLessEqual(len(json.dumps(e).encode()), dbviews.MAX_BYTES)
            self.assertTrue(dbviews.render_text(e).strip())
        amb = dbviews.table_view(ctx, "orders")
        self.assertEqual((amb["kind"], amb["state"]), ("candidates", "ambiguous_object"))
        self.assertEqual(len(amb["items"]), 2)
        self.assertEqual(dbviews.table_view(ctx, "shop.orders")["kind"], "table")

    def test_table_text_shows_states_and_env_filters_on_the_environment(self):
        from chitragupta import dbingest, dbviews
        ctx = self.ctx()
        e = dbviews.table_view(ctx, "billing.refunds")
        self.assertEqual(e["states"][0]["id"], "partial_pages")
        self.assertIn("- Partial metadata: This object was described from a partial page.", dbviews.render_text(e))
        self.assertEqual(len(dbviews.table_view(ctx, "orders", env="qa")["items"]), 2)
        self.assertEqual(dbviews.table_view(ctx, "orders", env="a")["items"], [])  # not a suffix of "QA"
        self.sc.enable_dev()
        dbingest.run(self.cfg())
        dev = dbviews.table_view(self.ctx(), "shop.orders", env="dev")
        self.assertEqual((dev["kind"], dev["environment"]), ("table", "DEV"))
        self.assertEqual(dbviews.table_view(self.ctx(), "shop.orders")["kind"], "candidates")

    def test_ids_and_names_are_checked(self):
        from chitragupta import dbflows, dbviews
        ctx = self.ctx()
        for bad in ("../../etc/passwd", "C:\\x", "SELECT 1", "shop.orders; DROP TABLE x", "mcp__shopdb-stub__run_query",
                    "x' OR '1'='1", ""):
            with self.assertRaises(dbviews.ViewError, msg=bad):
                dbviews.table_view(ctx, bad)
        with self.assertRaises(dbviews.ViewError):
            dbviews.flow_view(ctx, fid="f_../x")
        with self.assertRaises(dbflows.NotFound):
            dbviews.flow_view(ctx, fid="f_" + "a" * 24)
        with self.assertRaises(dbviews.ViewError):
            dbviews.resolve(ctx, "orders", kind="sql")


class Cursors(Case):
    def test_pages_and_cursor_binding(self):
        from chitragupta import dbviews
        self.many_tables()
        ctx = self.ctx()
        first = dbviews.resolve(ctx, "orders_", kind="table")
        self.assertEqual(len(first["items"]), dbviews.PAGE)
        self.assertTrue(first["more_available"])
        second = dbviews.resolve(ctx, "orders_", kind="table", cursor=first["cursor"])
        self.assertFalse({c["id"] for c in first["items"]} & {c["id"] for c in second["items"]})
        for bad in ("nope", first["cursor"][:-1] + ("0" if first["cursor"][-1] != "0" else "1"), "x" * 5000):
            with self.assertRaises(dbviews.CursorError):
                dbviews.resolve(ctx, "orders_", kind="table", cursor=bad)
        with self.assertRaises(dbviews.CursorError):  # another view's cursor
            dbviews.flows_list(ctx, cursor=first["cursor"])
        self.sc.switch_principal("B")  # another principal: the cursor isn't theirs
        with self.assertRaises(dbviews.CursorError):
            dbviews.resolve(self.ctx(), "orders_", kind="table", cursor=first["cursor"])

    def test_a_policy_change_or_new_data_invalidates_cursors(self):
        from chitragupta import dbaccess, dbmodel, dbstore, dbviews
        self.many_tables()
        first = dbviews.resolve(self.ctx(), "orders_", kind="table")
        dbstore.upsert("nodes", [dbmodel.db_node(SERVER, "qa-main", "qa", "shop", "shop", "table", "orders_new",
                                                 "postgres")])
        with self.assertRaises(dbviews.CursorError) as e:
            dbviews.resolve(self.ctx(), "orders_", kind="table", cursor=first["cursor"])
        self.assertIn("stale", str(e.exception))
        first = dbviews.resolve(self.ctx(), "orders_", kind="table")
        self.assertTrue(first["cursor"])
        dbaccess.revoke("qa-billing", "test", cfg=self.cfg())
        with self.assertRaises(dbviews.CursorError):
            dbviews.resolve(self.ctx(), "orders_", kind="table", cursor=first["cursor"])

    def test_access_is_decided_on_every_page(self):
        from chitragupta import dbaccess, dbviews
        ctx = self.ctx()
        before = {c["label"] for c in dbviews.resolve(ctx, "refunds", kind="table")["items"]}
        self.assertEqual(before, {"billing.refunds (qa-billing)"})
        dbaccess.revoke("qa-billing", "test", cfg=self.cfg())
        after = dbviews.resolve(self.ctx(), "refunds", kind="table")
        self.assertEqual(after["items"], [])  # no title, no count
        self.assertNotIn("refunds", json.dumps(dbviews.flows_list(self.ctx())))

    def test_the_64_kib_cap_continues_instead_of_cutting(self):
        from chitragupta import dbviews
        ctx = self.ctx()
        snap = dbviews.snapshot()
        items = [{"id": "x_%024d" % i, "text": "y" * 3000} for i in range(40)] + [{"id": "x_big", "text": "z" * 70000}]
        e = dbviews.paged(ctx, dbviews.envelope(ctx, "test", None, snap=snap), "test", "r", items, None, snap)
        self.assertLess(len(e["items"]), 40)
        self.assertTrue(e["more_available"])
        self.assertLessEqual(len(json.dumps(e).encode()), dbviews.MAX_BYTES)
        seen = len(e["items"])
        while e["more_available"]:
            e = dbviews.paged(ctx, dbviews.envelope(ctx, "test", None, snap=snap), "test", "r", items, e["cursor"], snap)
            self.assertLessEqual(len(json.dumps(e).encode()), dbviews.MAX_BYTES)
            seen += len(e["items"])
        self.assertEqual(seen, len(items))  # every item reached, none dropped
        self.assertTrue(e["items"][-1]["truncated"])  # the oversized one is an explicit stub
        self.assertIn("larger than one page", e["items"][-1]["reason"])


class States(Case):
    def test_required_states_have_next_steps(self):
        from chitragupta import dbflows, dbviews
        cfg = self.sc.config()
        cfg["mcp"]["database"]["scope"]["connections"]["qa-noenv"] = {"engine": "postgres", "schemas": ["x"]}
        cfg["git_author_emails"] = []
        write_config(self.home, cfg)
        dbflows.build_flow(self.ctx(), {"kind": "ticket", "key": "ABC-1001"}, save=True)
        write_ticket(self.home, flow=[dict(__import__("tests.dbfixture", fromlist=["x"]).FLOW[0], step=9)])
        from chitragupta import dbingest
        dbingest.run(self.cfg())
        w = dbviews.workspace_summary(self.ctx(), self.cfg(),
                                      health={"mcp": {"offline": {SERVER: "2026-11-06T10:00:00+05:30"}}})
        ids = {s["id"] for s in w["states"]}
        for sid in ("unknown_environment", "missing_identity", "partial_pages", "disconnected_source", "stale_flow"):
            self.assertIn(sid, ids)
        self.assertTrue(all(s["next"] for s in w["states"]))
        self.assertEqual(w["data"]["environment"], "QA")
        cfg["mcp"]["database"].pop("server")
        cfg["mcp"]["database"]["scope"] = {"connections": {"uat-only": {"env": "uat", "engine": "postgres"}}}
        write_config(self.home, cfg)
        w = dbviews.workspace_summary(self.ctx(), self.cfg(), health={})
        ids = {s["id"] for s in w["states"]}
        self.assertTrue({"no_database_mcp", "no_selected_connection"} <= ids)
        self.assertTrue(all(t and n for t, n in dbviews.STATES.values()))  # every state has a title and a next step
        self.assertEqual(len(dbviews.STATES), 11)


class ZeroQueries(Case):
    """DBT-15: opening, rendering, recall and repeated reads make zero database calls and write nothing; DBT-33."""

    def setUp(self):
        super().setUp()
        from chitragupta import dbconnector, dbflows
        self.fid = dbflows.build_flow(self.ctx(), {"kind": "ticket", "key": "ABC-1001"}, save=True)["id"]
        self.calls = []
        real_fake, real_mcp = FakeTransport.execute, dbconnector.McpJobTransport.execute

        def rec(name):
            def ex(this, *a, **k):
                self.calls.append(name)
                return []
            return ex
        FakeTransport.execute = rec("fake")
        dbconnector.McpJobTransport.execute = rec("mcp")
        self.addCleanup(lambda: (setattr(FakeTransport, "execute", real_fake),
                                 setattr(dbconnector.McpJobTransport, "execute", real_mcp)))

    def snapshot(self):
        out = {}
        for sub in ("data", os.path.join(".worklog", "db-view.key")):
            p = os.path.join(self.home, sub)
            for dp, _, fns in os.walk(p) if os.path.isdir(p) else [(os.path.dirname(p), [], [os.path.basename(p)])]:
                for fn in fns:
                    fp = os.path.join(dp, fn)
                    if os.path.isfile(fp):
                        with open(fp, "rb") as f:
                            out[fp] = f.read()
        return out

    def get(self, port, path):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            c.putrequest("GET", path, skip_host=True)
            c.putheader("Host", "127.0.0.1:%d" % port)
            c.endheaders()
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        finally:
            c.close()

    def test_dashboard_mcp_and_cli_reads(self):
        from chitragupta import cli, dbflows, mcpserver
        from chitragupta.dashboard import server
        before = self.snapshot()
        oid = [n["id"] for n in dbflows.Visible(self.ctx()).nodes.values() if n["id"][0] == "o" and
               n["display"].get("name") == "refunds"][0]
        srv, _ = server.start_in_thread(0)
        try:
            for _ in range(2):
                st, ws = self.get(srv.port, "/api/db/workspace")
                self.assertEqual((st, ws["kind"]), (200, "workspace"))
                st, fl = self.get(srv.port, "/api/db/flows")
                self.assertEqual([x["id"] for x in fl["items"]], [self.fid])
                st, f = self.get(srv.port, "/api/db/flow?id=" + self.fid)
                self.assertEqual((st, f["kind"]), (200, "flow"))
                st, f2 = self.get(srv.port, "/api/db/flow?start=ticket:ABC-1001")
                self.assertEqual(st, 200)
                st, p = self.get(srv.port, "/api/db/passport?id=" + oid)
                self.assertEqual((st, p["data"]["name"]), (200, "billing.refunds"))
                st, r = self.get(srv.port, "/api/db/resolve?q=orders&kind=table")
                self.assertEqual((st, r["state"]), (200, "ambiguous"))
                self.assertEqual(self.get(srv.port, "/api/db/passport?id=../x")[0], 400)
                self.assertEqual(self.get(srv.port, "/api/db/flow?id=f_" + "0" * 24)[0], 404)
                self.assertEqual(self.get(srv.port, "/api/db/flows?cursor=forged.0000")[0], 400)
                self.assertEqual(self.get(srv.port, "/api/db")[0], 200)  # the DB map keeps working
                self.assertEqual(self.get(srv.port, "/api/db/graph")[0], 200)
        finally:
            srv.shutdown()
            srv.server_close()
        # DBT-33: the page's graph holds exactly the edges its steps stand on; each detail opens by ID
        step_edges = {s["edge"] for s in f["items"] if s["edge"]}
        self.assertEqual({e["id"] for e in f["data"]["graph"]["edges"]}, step_edges)
        ids = {s["id"] for s in f["items"]}
        self.assertTrue(all(e["step"] in ids for e in f["data"]["graph"]["edges"]))
        nodes = {n["id"] for n in f["data"]["graph"]["nodes"]}
        self.assertTrue(all(e["source"] in nodes and e["target"] in nodes for e in f["data"]["graph"]["edges"]))
        s = mcpserver.Server()
        for args in ({"name": "shop.orders"}, {"name": "orders"}, {"name": oid}):
            res = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "get_db_table", "arguments": args}})["result"]
            self.assertFalse(res["isError"], res)
        for args in ({}, {"id": self.fid}, {"start": "ABC-1001"}, {"start": "orders", "kind": "table"}):
            res = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "get_db_flow", "arguments": args}})["result"]
            self.assertFalse(res["isError"], res)
        for argv in (["db", "table", "shop.orders"], ["db", "flow", "ABC-1001", "--no-save"], ["db", "flows"],
                     ["db", "resolve", "orders"], ["db", "table", "orders"]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(argv), 0, argv)
            self.assertTrue(buf.getvalue().strip())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.forbidden, [])
        self.assertEqual(self.snapshot(), before)

    def test_cli_db_text_on_a_legacy_console_encoding(self):
        """`cli db …` through the launcher path (python -m chitragupta cli) prints its arrows on a cp1252 console:
        cli's __main__ block reconfigures stdout to UTF-8 for every db command."""
        import subprocess
        import sys
        from tests.support import ROOT
        env = self.child_env()
        for k in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO"):
            env.pop(k, None)
        for argv in (["db", "flow", "ABC-1001", "--no-save"], ["db", "table", "billing.refunds"]):
            r = subprocess.run([sys.executable, "-m", "chitragupta", "cli"] + argv, capture_output=True, env=env,
                               cwd=ROOT, timeout=60)
            self.assertEqual((r.returncode, r.stderr), (0, b""), argv)
            self.assertIn("→", r.stdout.decode("utf-8"))

    def test_mcp_flow_text(self):
        from chitragupta import mcpserver
        s = mcpserver.Server()

        def call(args):
            return s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "get_db_flow", "arguments": args}})["result"]
        t = call({"start": "ABC-1001"})["content"][0]["text"]
        self.assertIn("Code model; deployed revision unknown", t)
        self.assertIn("shop-api/orders/cancel.py:12", t)
        self.assertIn("lineage: OrderCancelled.tenantId → billing.refunds.tenant_code", t)
        amb = call({"start": "orders", "kind": "table"})["content"][0]["text"]
        self.assertIn("Candidates for 'orders'", amb)
        self.assertTrue(call({"id": "f_" + "0" * 24})["isError"])
        self.assertTrue(call({"id": self.fid, "cursor": "forged.00"})["isError"])
        t = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "get_db_table", "arguments": {"name": "billing.refunds"}}})["result"]
        txt = t["content"][0]["text"]
        self.assertIn("# billing.refunds · Table (qa-billing, QA)", txt)
        self.assertIn("Unconfirmed meaning", txt)


class TraceReads(Case):
    """DBT-15 (job polling, investigation, plan, handoff and context reads make zero transport calls and never
    enqueue or start anything) and DBT-32 (every read re-checks scope; the db-* writes are protected actions; no GET
    mutates the data home)."""

    def setUp(self):
        super().setUp()
        from chitragupta import dbflows, dbtrace
        self.sc.provider(timeout=True)
        ctx = self.ctx()
        self.fid = dbflows.build_flow(ctx, {"kind": "ticket", "key": "ABC-1001"}, save=True)["id"]
        vis = dbflows.Visible(ctx)
        self.oid = [n for n in vis.nodes if n.startswith("o_") and vis.label(n) == "shop.orders (qa-main)"][0]
        self.plan = dbtrace.make_plan(ctx, self.fid, "qa", None, {"object": self.oid, "key": {"tenant_id": "7",
                                                                                              "id": "1001"}})
        job = dbtrace.request_run(ctx, self.plan["id"], self.plan["fingerprint"], "test")
        self.job = dbtrace.run_job(job["id"], FakeTransport(self.sc), self.cfg(), {})
        self.iid = self.plan["investigation"]
        self.events = []
        for mod, name in ((dbtrace, "spawn_agent"), (dbtrace, "spawn_runner")):
            patcher = mock.patch.object(mod, name, lambda *a, _n=name, **k: self.events.append(_n))
            patcher.start()
            self.addCleanup(patcher.stop)
        for cls in (FakeTransport, __import__("chitragupta.dbconnector", fromlist=["x"]).McpJobTransport):
            patcher = mock.patch.object(cls, "execute", lambda this, *a, **k: self.events.append("execute") or [])
            patcher.start()
            self.addCleanup(patcher.stop)

    def snap(self):
        out = {}
        for dp, _, fns in os.walk(os.path.join(self.home, "data")):
            for fn in fns:
                with open(os.path.join(dp, fn), "rb") as f:
                    out[os.path.join(dp, fn)] = f.read()
        jobs = os.path.join(self.home, ".worklog", "cache", "db-jobs")
        out["db-jobs"] = sorted(os.listdir(jobs)) if os.path.isdir(jobs) else []
        return out

    def test_polling_and_reads_never_query_or_enqueue(self):
        """DBT-15."""
        from chitragupta import cli, dbviews, mcpserver
        before = self.snap()
        ctx = self.ctx()
        for _ in range(3):
            self.assertTrue(dbviews.job_view(ctx, self.job["id"])["data"]["terminal"])
        for env in (dbviews.investigation_view(ctx, self.iid), dbviews.investigations_list(ctx),
                    dbviews.plan_view(ctx, self.plan["id"]), dbviews.handoff_view(ctx, self.iid),
                    dbviews.context_view(ctx, self.fid, self.iid), dbviews.key_spec_view(ctx, self.fid)):
            self.assertLessEqual(len(json.dumps(env).encode()), dbviews.MAX_BYTES)
            self.assertTrue(dbviews.render_text(env).strip(), env["kind"])
        res = mcpserver.Server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "get_db_investigation", "arguments": {"id": self.iid}}})["result"]
        self.assertFalse(res["isError"])
        for argv in (["db", "status", self.job["id"]], ["db", "investigation", self.iid], ["db", "investigations"],
                     ["db", "context", "--investigation", self.iid], ["db", "handoff", self.iid]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(argv), 0, argv)
        self.assertEqual(self.events, [])
        self.assertEqual(self.forbidden, [])
        self.assertEqual(self.snap(), before)

    def http(self, port, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            c.putrequest(method, path, skip_host=True)
            c.putheader("Host", "127.0.0.1:%d" % port)
            data = json.dumps(body).encode() if isinstance(body, dict) else (body or b"")
            for k, v in (headers or {}).items():
                c.putheader(k, v)
            if method == "POST":
                c.putheader("Content-Length", str(len(data)))
            c.endheaders()
            if data:
                c.send(data)
            r = c.getresponse()
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] == b"{" else raw)
        finally:
            c.close()

    def test_reads_recheck_scope_and_writes_are_protected(self):
        """DBT-32."""
        import re
        from chitragupta import dbaccess
        from chitragupta.dashboard import server
        srv, _ = server.start_in_thread(0)
        try:
            port = srv.port
            routes = ["/api/db/investigations", "/api/db/investigation?id=" + self.iid,
                      "/api/db/plan?id=" + self.plan["id"], "/api/db/plan?flow=" + self.fid,
                      "/api/db/plan?start=ticket:ABC-1001&tenant_id=7&order=1001", "/api/db/job?id=" + self.job["id"],
                      "/api/db/handoff?id=" + self.iid, "/api/db/context?flow=%s&investigation=%s" % (self.fid, self.iid),
                      "/api/db/context", "/api/db/workspace", "/api/db/flows", "/api/db/flow?id=" + self.fid]
            before = self.snap()
            for r in routes:
                st, body = self.http(port, "GET", r)
                self.assertEqual(st, 200, (r, str(body)[:200]))
            self.assertEqual(self.snap(), before)  # no GET mutates anything
            self.assertEqual(self.events, [])
            for bad in ("/api/db/investigation?id=../x", "/api/db/plan?id=p_x", "/api/db/plan",
                        "/api/db/job?id=" + "j_" + "0" * 24):
                self.assertIn(self.http(port, "GET", bad)[0], (400, 404), bad)
            dbaccess.revoke("qa-billing", "test", cfg=self.cfg())  # scope is re-checked on the next request
            self.assertEqual(self.http(port, "GET", "/api/db/investigation?id=" + self.iid)[0], 404)
            self.assertEqual(self.http(port, "GET", "/api/db/investigations")[1]["items"], [])
            dbaccess.restore("qa-billing", cfg=self.cfg())
            before = self.snap()  # (revoke / restore wrote data/db/revocations.json)
            page = self.http(port, "GET", "/")[1].decode("utf-8")
            tok = re.search(r'<meta name="cg-token" content="([^"]+)">', page).group(1)
            good = {"Origin": "http://127.0.0.1:%d" % port, "X-Chitragupta-Token": tok,
                    "Content-Type": "application/json"}
            body = {"flow": self.fid, "env": "qa", "object": self.oid, "key": {"tenant_id": "7", "id": "1001"}}
            no_origin = {k: v for k, v in good.items() if k != "Origin"}
            for hdrs in (dict(good, **{"X-Chitragupta-Token": "forged"}), dict(good, Origin="http://evil.acme.test"),
                         no_origin, {k: v for k, v in good.items() if k != "X-Chitragupta-Token"}):
                self.assertEqual(self.http(port, "POST", "/api/actions/db-plan", body, hdrs)[0], 403, hdrs)
            big = dict(body, objective="x" * 8000)
            self.assertEqual(self.http(port, "POST", "/api/actions/db-plan", big, good)[0], 413)
            self.assertEqual(self.snap(), before)  # nothing refused wrote anything
            st, plan = self.http(port, "POST", "/api/actions/db-plan", body, good)
            self.assertEqual((st, plan["kind"]), (200, "plan"))
            st, _ = self.http(port, "POST", "/api/actions/db-trace-run", {"plan": plan["id"], "fingerprint": "0" * 24},
                              good)
            self.assertEqual(st, 409)
            st, job = self.http(port, "POST", "/api/actions/db-trace-run",
                                {"plan": plan["id"], "fingerprint": plan["data"]["fingerprint"]}, good)
            self.assertEqual((st, job["kind"], self.events), (200, "job", ["spawn_runner"]))
            self.assertEqual(self.http(port, "POST", "/api/actions/db-trace-cancel", {"job": job["id"]}, good)[0], 200)
            log = os.path.join(self.home, ".worklog", "logs", "dashboard.log")
            self.assertNotIn("1001", open(log, encoding="utf-8").read() if os.path.isfile(log) else "")
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
