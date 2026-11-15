"""The database workspace's stage-E contracts that need no browser (the browser half is scripts/ui/dbcheck.js):
the contextual Known impacts / Compare metadata actions are protected POSTs that compute from cached metadata only
(never a query, a model or a process) while their GET reads never compute; the legacy DB map honours the approved
scope (a revoked or removed connection leaves it, DB-02 / DB-03); and the page's static contracts (one job poller,
a specific next step for every job state and observation status, DB-23)."""
import http.client
import json
import os
import re
import unittest
from unittest import mock

from tests.dbfixture import Scenario, write_linkmap, write_ticket
from tests.support import Isolated, write_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "chitragupta", "dashboard", "app")


class Case(Isolated, unittest.TestCase):
    dev = False

    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build(dev=self.dev)
        write_ticket(self.home)
        write_linkmap(self.home)
        from chitragupta import dbingest
        dbingest.run(self.cfg())

    def cfg(self):
        return self.sc.cfg()

    def vis(self):
        from chitragupta import dbaccess, dbflows
        return dbflows.Visible(dbaccess.AccessContext(self.cfg()))

    def oid(self, label):
        vis = self.vis()
        return [n for n in vis.nodes if n.startswith("o_") and vis.label(n) == label][0]

    def cid(self, label, col):
        vis, oid = self.vis(), self.oid(label)
        return [n for n, x in vis.nodes.items() if x.get("parent") == oid and (x.get("display") or {}).get("member") == col][0]


def http_call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", "127.0.0.1:%d" % port)
        data = json.dumps(body).encode() if isinstance(body, dict) else b""
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


class ContextualActions(Case):
    """Known impacts and Compare metadata from the dashboard: GET reads the cache, the protected POST computes from
    cached metadata; nothing queries, calls a model or starts a process; a blocked side widens nothing."""
    dev = True

    def test_compute_actions_are_protected_and_never_query(self):
        from chitragupta import dbstore
        from chitragupta.dashboard import server
        from chitragupta.safety import commands
        calls = []
        srv, _ = server.start_in_thread(0)
        try:
            port = srv.port
            page = http_call(port, "GET", "/")[1].decode("utf-8")
            tok = re.search(r'<meta name="cg-token" content="([^"]+)">', page).group(1)
            good = {"Origin": "http://127.0.0.1:%d" % port, "X-Chitragupta-Token": tok, "Content-Type": "application/json"}
            col = self.cid("billing.refunds (qa-billing)", "order_ref")
            with mock.patch.object(commands, "run", lambda *a, **k: calls.append(("run", a))), \
                    mock.patch.object(commands, "spawn", lambda *a, **k: calls.append(("spawn", a))):
                st, v = http_call(port, "GET", "/api/db/impact?target=%s&change=rename" % col)
                self.assertEqual((st, v["state"]), (200, "not_computed"))  # a read never computes
                body = {"target": col, "change": "rename"}
                self.assertEqual(http_call(port, "POST", "/api/actions/db-impact-compute", body,
                                           dict(good, **{"X-Chitragupta-Token": "forged"}))[0], 403)
                st, v = http_call(port, "POST", "/api/actions/db-impact-compute", body, good)
                self.assertEqual((st, v["kind"], v["state"]), (200, "impact", "ok"), v)
                self.assertTrue(v["data"]["headline"].startswith("3 affected: 2 recorded impact links"), v["data"])
                self.assertIn(("mapper", "refunds.on_order_cancelled"), {(i["relation"], i["label"]) for i in v["items"]})
                st, again = http_call(port, "GET", "/api/db/impact?id=" + v["id"])
                self.assertEqual((st, again["data"]["headline"]), (200, v["data"]["headline"]))
                st, amb = http_call(port, "POST", "/api/actions/db-impact-compute", {"target": "shop.orders.status"}, good)
                self.assertEqual(st, 409, amb)  # two environments hold shop.orders: nothing chosen for you
                st, ok = http_call(port, "POST", "/api/actions/db-impact-compute",
                                   {"target": "shop.orders.status", "env": "dev", "change": "type"}, good)
                self.assertEqual((st, ok["data"]["target"]["connection"]), (200, "dev-main"))
                pairs_before = dbstore.read_file("comparisons")
                st, b = http_call(port, "POST", "/api/actions/db-compare-compute",
                                  {"left": "qa-main", "right": "uat-main", "objects": ["shop.orders"],
                                   "confirm": ["shop.orders"]}, good)
                self.assertEqual((st, b["state"]), (200, "blocked_comparison"))
                self.assertIn("uat is not in qa_data.allowed_envs", b["data"]["blocked"]["reason"])
                self.assertEqual(dbstore.read_file("comparisons"), pairs_before)  # a blocked side records nothing
                st, c = http_call(port, "POST", "/api/actions/db-compare-compute",
                                  {"left": "qa-main", "right": "dev-main", "objects": ["shop.orders"],
                                   "confirm": ["shop.orders"]}, good)
                self.assertEqual((st, c["kind"], c["data"]["categories"]["changed"]), (200, "compare", 1), c)
                diff = [d for it in c["items"] for d in it.get("differences") or []]
                self.assertIn(("status", "varchar(32)", "varchar(20)"), [(d["column"], d["left"], d["right"]) for d in diff])
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(calls, [])
        self.assertEqual(self.forbidden, [])  # mcpjobs.run (the provider / model transport) never ran


class LegacyMap(Case):
    """The DB map rail and table pages show only approved connections once an explicit scope exists."""

    def keys(self):
        from chitragupta.dashboard import dbmap
        return {(d["name"], t["key"]) for d in dbmap.tree()["databases"] for s in d["schemas"] for t in s["tables"]}

    def test_a_connection_out_of_scope_leaves_the_map(self):
        from chitragupta import dbaccess
        from chitragupta.dashboard import dbmap
        self.assertIn(("qa-billing", "billing.refunds"), self.keys())
        self.assertTrue(dbmap.table("billing.refunds")["columns"])
        cfg = self.sc.config()
        del cfg["mcp"]["database"]["scope"]["connections"]["qa-billing"]  # removed from the scope (the config file)
        write_config(self.home, cfg)
        self.assertNotIn("qa-billing", {db for db, _ in self.keys()})
        self.assertFalse((dbmap.table("billing.refunds") or {}).get("columns"))
        self.sc.write()
        dbaccess.revoke("qa-billing", "test", cfg=self.cfg())  # revoked: the same
        self.assertNotIn("qa-billing", {db for db, _ in self.keys()})
        dbaccess.restore("qa-billing", cfg=self.cfg())
        self.assertIn(("qa-billing", "billing.refunds"), self.keys())

    def test_without_an_explicit_scope_the_legacy_map_is_unchanged(self):
        cfg = self.sc.config()
        del cfg["mcp"]["database"]["scope"]
        write_config(self.home, cfg)
        self.assertIn(("qa-billing", "billing.refunds"), self.keys())  # nothing to decide with: every card shows


class PageContracts(unittest.TestCase):
    """Static contracts of pages/db.js and lib/dbx.js (DB-23, DB-25): one job poller, a specific next step for every
    job state and observation status, reads through GET projections and writes only through runAction."""

    def read(self, rel):
        with open(os.path.join(APP, *rel.split("/")), encoding="utf-8") as f:
            return f.read()

    def test_one_poller_and_next_steps_for_every_state(self):
        from chitragupta import dbmodel, dbtrace
        js = self.read("pages/db.js") + self.read("lib/dbx.js")
        self.assertEqual(js.count("setInterval"), 0)
        self.assertEqual(len(re.findall(r"setTimeout\(tick", js)), 2)  # the one job poller (its two retry paths)
        block = js[js.index("const JOB_NEXT"):js.index("const OBS_NEXT")]
        for state in dbtrace.ACTIVE + dbtrace.TERMINAL:
            self.assertRegex(block, r"\b%s:" % state, state)
        block = js[js.index("const OBS_NEXT"):js.index("};", js.index("const OBS_NEXT"))]
        for status in dbmodel.STEP_STATUSES:
            self.assertRegex(block, r"\b%s:" % status, status)

    def test_reads_are_cached_projections_and_writes_are_protected_actions(self):
        js = self.read("pages/db.js") + self.read("lib/dbx.js")
        self.assertNotIn("fetch(", js)  # everything goes through core.js api() / runAction()
        for action in re.findall(r"runAction\('([a-z-]+)'", js):
            self.assertTrue(action.startswith("db-"), action)
        self.assertIn("runAction('db-impact-compute'", js)
        self.assertIn("runAction('db-compare-compute'", js)
        for path in re.findall(r"'(/api/db/[a-z]+)", js):
            self.assertIn(path, ("/api/db/table", "/api/db/resolve", "/api/db/plan", "/api/db/job", "/api/db/investigation",
                                 "/api/db/investigations", "/api/db/handoff", "/api/db/context", "/api/db/passport",
                                 "/api/db/flow", "/api/db/workspace", "/api/db/impact", "/api/db/compare"), path)
