"""Stage C gaps: `agent db-trace` / `agent db-discover` (dbtrace.agent_main) and dbflows.run_refresh run in-process
with the fake transport injected (no subprocess, no mcpjobs.run), including a job revoked or cancelled before its
first call; and the workspace actions db-mapping-confirm, db-metadata-refresh, db-investigation-attach and
db-handoff-save through the protected HTTP write path (page token, Origin, size, rate, audit record)."""
import contextlib
import http.client
import io
import json
import re
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
        from chitragupta import dbconnector, dbflows, dbingest
        dbingest.run(self.cfg())
        self.sc.provider(timeout=True)
        ctx = self.ctx()
        self.fid = dbflows.build_flow(ctx, {"kind": "ticket", "key": "ABC-1001"}, save=True)["id"]
        vis = dbflows.Visible(ctx)
        self.oid = [n for n in vis.nodes if n.startswith("o_") and vis.label(n) == "shop.orders (qa-main)"][0]
        self.fake = FakeTransport(self.sc)
        p = mock.patch.object(dbconnector, "McpJobTransport", lambda: self.fake)
        p.start()
        self.addCleanup(p.stop)

    def cfg(self):
        return self.sc.cfg()

    def ctx(self):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg())

    def job(self):
        from chitragupta import dbtrace
        plan = dbtrace.make_plan(self.ctx(), self.fid, "qa", None, {"object": self.oid, "key": {"tenant_id": "7",
                                                                                                "id": "1001"}})
        return plan, dbtrace.request_run(self.ctx(), plan["id"], plan["fingerprint"], "test")

    def agent(self, cmd, *argv):
        from chitragupta import dbtrace
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = dbtrace.agent_main(cmd, list(argv))
        return code, buf.getvalue()


class Runner(Case):
    def test_agent_db_trace_runs_the_queued_job_in_process(self):
        from chitragupta import common, dbstore
        plan, job = self.job()
        code, out = self.agent("db-trace", job["id"])
        self.assertEqual(code, 0, out)
        self.assertIn("job %s:" % job["id"], out)
        rec = dbstore.load("jobs", job["id"])
        self.assertIn(rec["state"], ("completed", "partial"))
        self.assertTrue(self.fake.calls)
        self.assertTrue(all(c["pin"] for c in self.fake.calls))  # every call under the job's plan pin
        inv = dbstore.load("investigations", plan["investigation"])
        self.assertEqual(len(inv["runs"]), 1)
        self.assertIsInstance(common.read_json(common.wl("health.json"), None), dict)  # budget counters saved
        self.assertEqual(self.agent("db-trace", "j_" + "0" * 24), (2, "no such job\n"))
        self.assertEqual(self.agent("db-trace", "../x")[0], 2)

    def test_a_job_revoked_before_its_first_call_publishes_nothing(self):
        from chitragupta import dbaccess, dbstore
        plan, job = self.job()
        inv0 = dbstore.load("investigations", plan["investigation"])
        dbaccess.revoke("qa-billing", "test", cfg=self.cfg())
        code, out = self.agent("db-trace", job["id"])
        self.assertEqual(code, 0, out)
        self.assertEqual(dbstore.load("jobs", job["id"])["state"], "revoked")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(dbstore.load("investigations", plan["investigation"])["runs"], inv0["runs"])

    def test_a_job_cancelled_before_its_first_call(self):
        from chitragupta import dbstore, dbtrace
        plan, job = self.job()
        dbtrace.request_cancel(self.ctx(), job["id"])
        code, out = self.agent("db-trace", job["id"])
        self.assertEqual(code, 0, out)
        self.assertEqual(dbstore.load("jobs", job["id"])["state"], "cancelled")
        self.assertEqual(self.fake.calls, [])
        runs = dbstore.load("investigations", plan["investigation"])["runs"]
        self.assertTrue(runs, runs)
        self.assertEqual({o["status"] for o in runs[-1]["observations"]} - {"mapping_unresolved"}, {"not_queried"})

    def test_agent_db_discover_and_run_refresh(self):
        from chitragupta import dbaccess, dbflows, dbstore
        q = dbflows.queue_refresh(self.ctx(), ["qa-main:shop.orders", "qa-reporting:shop.orders"], cfg=self.cfg())
        self.assertEqual(q["queued"], ["qa-main:shop.orders"])
        self.assertEqual(q["skipped"][0]["why"], "connection not approved")
        code, out = self.agent("db-discover")
        self.assertEqual(code, 0, out)
        self.assertIn("1 discovery request(s) run", out)
        self.assertEqual(dbstore.read_file("refresh")["requests"], [])
        self.assertTrue([c for c in self.fake.calls if c["tool"] == "describe_table"])
        self.assertTrue(dbstore.list_snapshots("qa-main"))  # a metadata snapshot after the refresh
        dbflows.queue_refresh(self.ctx(), ["qa-billing:billing.refunds"], cfg=self.cfg())
        dbaccess.revoke("qa-billing", "test", cfg=self.cfg())  # revoked after queueing: checked again when run
        n = len(self.fake.calls)
        res = dbflows.run_refresh(self.fake, self.cfg())
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["skipped"][0]["why"], "connection not approved")
        self.assertEqual(len(self.fake.calls), n)


class Actions(Case):
    """db-mapping-confirm, db-metadata-refresh, db-investigation-attach, db-handoff-save over HTTP."""

    def setUp(self):
        super().setUp()
        from chitragupta import dbtrace
        from chitragupta.dashboard import server
        self.spawned = []
        p = mock.patch.object(dbtrace, "spawn_agent", lambda *a, **k: self.spawned.append(a))
        p.start()
        self.addCleanup(p.stop)
        self.srv, _ = server.start_in_thread(0)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.port
        page = self.http("GET", "/")[1].decode("utf-8")
        tok = re.search(r'<meta name="cg-token" content="([^"]+)">', page).group(1)
        self.good = {"Origin": "http://127.0.0.1:%d" % self.port, "X-Chitragupta-Token": tok,
                     "Content-Type": "application/json"}

    def http(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest(method, path, skip_host=True)
            c.putheader("Host", "127.0.0.1:%d" % self.port)
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

    def post(self, action, body, headers=None):
        return self.http("POST", "/api/actions/" + action, body, self.good if headers is None else headers)

    def refused(self, action, body):
        bad = (dict(self.good, **{"X-Chitragupta-Token": "forged"}), dict(self.good, Origin="http://evil.acme.test"),
               {k: v for k, v in self.good.items() if k != "Origin"},
               {k: v for k, v in self.good.items() if k != "X-Chitragupta-Token"})
        for h in bad:
            self.assertEqual(self.post(action, body, h)[0], 403, (action, h))
        self.assertEqual(self.post(action, dict(body, pad="x" * 70000))[0], 413, action)

    def test_the_four_actions_through_the_protected_path(self):
        from chitragupta import configstore, dbstore, dbtrace
        plan, job = self.job()
        dbtrace.run_job(job["id"], self.fake, self.cfg(), {})
        iid = plan["investigation"]
        cases = [("db-mapping-confirm", {"from": "shop.orders.id", "to": "billing.refunds.order_ref"}),
                 ("db-metadata-refresh", {"seeds": ["qa-main:shop.orders"]}),
                 ("db-investigation-attach", {"investigation": iid, "ticket": "ABC-1001"}),
                 ("db-handoff-save", {"investigation": iid, "regenerate": True})]
        for action, body in cases:
            before = (dbstore.read_file("mappings"), dbstore.read_file("refresh"),
                      dbstore.load("investigations", iid))
            self.refused(action, body)
            self.assertEqual((dbstore.read_file("mappings"), dbstore.read_file("refresh"),
                              dbstore.load("investigations", iid)), before, action)  # refused: nothing written
            st, out = self.post(action, body)
            self.assertEqual(st, 200, (action, out))
        self.assertEqual(len(dbstore.read_file("mappings")["mappings"]), 1)
        self.assertEqual(dbstore.read_file("refresh")["requests"][0]["seeds"], ["qa-main:shop.orders"])
        self.assertEqual([a[0] for a in self.spawned], ["db-discover"])
        inv = dbstore.load("investigations", iid)
        self.assertEqual(inv["ticket"], "ABC-1001")
        self.assertTrue(inv["handoff"]["generated"])
        st, out = self.post("db-handoff-save", {"investigation": iid, "text": "edited draft", "revision": 99})
        self.assertEqual((st, out.get("conflict")), (409, True))
        self.assertEqual(out["yours"], "edited draft")  # a stale save keeps your text
        self.assertEqual(self.post("db-metadata-refresh", {"seeds": "nope"})[0], 400)
        self.assertEqual(self.post("db-investigation-attach", {"investigation": "../x", "ticket": "ABC-1001"})[0], 400)
        keys = [h["key"] for h in configstore.history()]
        for action, _ in cases:
            self.assertIn("action:" + action, keys)  # every write recorded
        self.srv.limiter.per_minute = 1
        codes = [self.post("db-metadata-refresh", {"seeds": ["qa-main:shop.orders"]})[0] for _ in range(3)]
        self.assertIn(429, codes)


if __name__ == "__main__":
    unittest.main()
