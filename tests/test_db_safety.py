"""Safety for the database flow: the plan pin (CHITRAGUPTA_DB_PIN) and the approved statement scope, decided through
safety.mcp.reason("mcp", …) exactly as the internal guard does; plus what row jobs leave behind (no raw stream in
cache/mcp, db-jobs out of the nightly backup and the search index). Synthetic data, temp homes, no server."""
import datetime as _dt
import json
import os
import time
import unittest
import zipfile
from unittest import mock

from tests.dbfixture import SERVER, Scenario
from tests.support import Isolated, write_config

Q = "mcp__%s__run_query" % SERVER
D = "mcp__%s__describe_table" % SERVER


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()

    def cfg(self):
        return self.sc.cfg()

    def reason(self, tool, inp):
        from chitragupta.safety import mcp
        return mcp.reason("mcp", tool, inp)

    def built(self, conn="qa-main", tenant=7, order=1001):
        from chitragupta import dbconnector
        if conn == "qa-billing":
            return dbconnector.build_select(dbconnector.adapter_for("mssql"),
                                            self.sc.metadata("qa-billing", "billing", "refunds"), ["status"],
                                            [("tenant_code", "T%03d" % tenant), ("order_ref", str(order))])[0]
        return dbconnector.build_select(dbconnector.adapter_for("postgres"), self.sc.metadata("qa-main", "shop", "orders"),
                                        ["status"], [("tenant_id", tenant), ("id", order)])[0]


class PinCase(Case):
    JOB = "j_0123456789abcdef01234567"

    def setUp(self):
        super().setUp()
        from chitragupta import common
        self.dir = common.wl("cache", "db-jobs")
        os.makedirs(self.dir, exist_ok=True)
        self.addCleanup(os.environ.pop, common.DB_PIN_ENV, None)

    def pin(self, calls, deadline=None, generation=None, job=None, server=SERVER):
        from chitragupta import common, dbaccess
        from chitragupta.safety import mcp
        job = job or self.JOB
        p = os.path.join(self.dir, job + ".pin.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "job": job, "server": server,
                       "deadline_epoch": time.time() + 120 if deadline is None else deadline,
                       "policy_generation": generation or dbaccess.policy_generation(self.cfg()),
                       "calls": [{"tool": t, "connection": c, "sha256": mcp.call_digest(inp)} for t, c, inp in calls]}, f)
        os.environ[common.DB_PIN_ENV] = p
        return p

    def q(self, sql, conn="qa-main"):
        return {"connectionId": conn, "sql": sql, "limit": 20}


class PlanPin(PinCase):
    def test_a_pinned_call_passes_once(self):
        inp = self.q(self.built())
        self.pin([("run_query", "qa-main", inp)])
        self.assertIsNone(self.reason(Q, inp))
        self.assertIn("already used", self.reason(Q, inp))
        with open(os.path.join(self.dir, self.JOB + ".calls.jsonl"), encoding="utf-8") as f:
            ledger = [json.loads(x) for x in f]
        self.assertEqual(len(ledger), 1)
        self.assertNotIn("1001", json.dumps(ledger))  # the ledger keeps the hash, never the statement or its key

    def test_two_identical_entries_allow_exactly_two_uses(self):
        inp = self.q(self.built())
        self.pin([("run_query", "qa-main", inp), ("run_query", "qa-main", inp)])
        self.assertIsNone(self.reason(Q, inp))
        self.assertIsNone(self.reason(Q, inp))
        self.assertIsNotNone(self.reason(Q, inp))

    def test_anything_else_is_denied(self):
        inp = self.q(self.built())
        self.pin([("run_query", "qa-main", inp)])
        other = self.q(self.built(order=1002))
        self.assertIn("not in job", self.reason(Q, other))
        self.assertIsNotNone(self.reason(Q, dict(inp, connectionId="qa-billing")))  # same statement, other connection
        self.assertIsNotNone(self.reason(D, {"connectionId": "qa-main", "schema": "shop", "table": "orders"}))
        self.assertIsNone(self.reason("StructuredOutput", {}))
        self.assertIsNone(self.reason(Q, inp))  # still unused after the refusals

    def test_after_the_deadline(self):
        inp = self.q(self.built())
        self.pin([("run_query", "qa-main", inp)], deadline=time.time() - 1)
        self.assertIn("past its deadline", self.reason(Q, inp))

    def test_after_cancel(self):
        inp = self.q(self.built())
        self.pin([("run_query", "qa-main", inp)])
        open(os.path.join(self.dir, self.JOB + ".cancel"), "w").close()
        self.assertIn("cancelled", self.reason(Q, inp))

    def test_after_a_policy_change(self):
        from chitragupta import dbaccess
        inp = self.q(self.built())
        self.pin([("run_query", "qa-main", inp)])
        dbaccess.revoke("qa-billing", "", self.cfg())  # any change of the policy, not only of this connection
        self.assertIn("policy changed", self.reason(Q, inp))
        dbaccess.restore("qa-billing", self.cfg())
        self.assertIsNone(self.reason(Q, inp))
        inp2 = self.q(self.built(order=1002))
        self.pin([("run_query", "qa-main", inp2)])
        self.sc.enable_dev()
        self.assertIn("policy changed", self.reason(Q, inp2))

    def test_the_ordinary_guard_runs_first(self):
        bad = self.q("DELETE FROM shop.orders WHERE id = 1001")
        wide = self.q("SELECT id FROM reporting.orders WHERE id = 1 LIMIT 5")
        self.pin([("run_query", "qa-main", bad), ("run_query", "qa-main", wide)])
        self.assertIn("DELETE", self.reason(Q, bad))
        self.assertIn("outside the approved scope", self.reason(Q, wide))
        self.assertFalse([f for f in os.listdir(self.dir) if ".used." in f])  # nothing was claimed

    def test_a_pin_must_be_ours(self):
        from chitragupta import common
        inp = self.q(self.built())
        good = self.pin([("run_query", "qa-main", inp)])
        for path in (os.path.join(self.home, "elsewhere", self.JOB + ".pin.json"),
                     os.path.join(self.dir, "notajob.pin.json"), os.path.join(self.dir, "missing-j.pin.json"),
                     os.path.join(self.dir, "j_ffffffffffffffff.pin.json")):
            with self.subTest(path=path):
                os.environ[common.DB_PIN_ENV] = path
                self.assertIsNotNone(self.reason(Q, inp))
        with open(good, "w") as f:
            f.write("{not json")
        os.environ[common.DB_PIN_ENV] = good
        self.assertIn("unreadable", self.reason(Q, inp))
        self.pin([("run_query", "qa-main", inp)], server="other-db")
        self.assertIn("not in job", self.reason(Q, inp))
        self.pin([("run_query", "qa-main", inp)], job="j_aaaaaaaaaaaaaaaa")
        with open(os.path.join(self.dir, "j_aaaaaaaaaaaaaaaa.pin.json")) as f:
            data = json.load(f)
        data["job"] = self.JOB  # a pin naming another job
        with open(os.path.join(self.dir, "j_aaaaaaaaaaaaaaaa.pin.json"), "w") as f:
            json.dump(data, f)
        self.assertIn("unreadable or not for job", self.reason(Q, inp))

    def test_without_a_pin_the_ordinary_guard_decides_alone(self):
        from chitragupta import common
        os.environ.pop(common.DB_PIN_ENV, None)
        inp = self.q(self.built())
        self.assertIsNone(self.reason(Q, inp))
        self.assertIsNone(self.reason(Q, inp))
        os.environ[common.DB_PIN_ENV] = ""
        self.assertIsNone(self.reason(Q, inp))


class StatementScope(Case):
    """Through the guard with the allow.json the fixture's config generates (scopes for qa-main and qa-billing)."""

    def test_builder_statements_pass(self):
        self.assertIsNone(self.reason(Q, {"connectionId": "qa-main", "sql": self.built(), "limit": 20}))
        self.assertIsNone(self.reason(Q, {"connectionId": "qa-billing", "sql": self.built("qa-billing"), "limit": 20}))
        self.assertIsNone(self.reason(D, {"connectionId": "qa-billing", "schema": "billing", "table": "refunds"}))
        self.assertIsNone(self.reason(D, {"connectionId": "qa-main", "table": "shop_audit.orders"}))

    def test_targets_outside_the_scope_are_denied(self):
        for conn, sql in (
                ("qa-main", "SELECT id FROM reporting.daily LIMIT 5"),
                ("qa-main", "SELECT id FROM analytics.shop.orders LIMIT 5"),
                ("qa-main", "SELECT id FROM srv.shop.shop.orders LIMIT 5"),
                ("qa-billing", "SELECT TOP 5 id FROM [other].[billing].[refunds]"),
                ("qa-billing", "SELECT TOP 5 id FROM [linked].[billing].[billing].[refunds]"),
                ("qa-billing", "SELECT TOP 5 * FROM OPENQUERY(other, 'SELECT 1')"),
                ("qa-billing", "SELECT TOP 5 id FROM billing.refunds r JOIN hr.staff s ON s.id = r.order_ref"),
                ("qa-main", "SELECT id FROM orders LIMIT 5"),
                ("qa-billing", "SELECT TOP 5 id FROM refunds"),
                ("qa-main", "SELECT o.id, (SELECT max(x) FROM payroll.t) FROM shop.orders o LIMIT 5"),
                ("qa-main", "SELECT id FROM shop.orders WHERE id IN (SELECT id FROM \"Shop\".orders) LIMIT 5"),
                ("qa-main", "SELECT other.fn(id) FROM shop.orders LIMIT 5"),
                ("qa-billing", "SELECT TOP 5 x FROM sys.objects o CROSS APPLY other.fn(o.object_id) f"),
                ("qa-billing", "SELECT TOP 5 name FROM otherdb.sys.objects"),
                ("qa-main", "SELECT id FROM \"shop.orders\" LIMIT 5")):
            with self.subTest(sql=sql):
                why = self.reason(Q, {"connectionId": conn, "sql": sql, "limit": 20})
                self.assertIsNotNone(why)

    def test_object_arguments_are_scoped(self):
        for inp in ({"connectionId": "qa-main", "schema": "reporting", "table": "daily"},
                    {"connectionId": "qa-main", "table": "reporting.daily"},
                    {"connectionId": "qa-main", "table": "orders"},
                    {"connectionId": "qa-main", "table": "x.shop.orders"},
                    {"connectionId": "qa-main", "schema": "shop", "table": "shop.orders"},
                    {"connectionId": "qa-main", "schema": "sh\"op", "table": "orders"}):
            with self.subTest(inp=inp):
                self.assertIsNotNone(self.reason(D, inp))

    def test_a_guard_listed_connection_without_scope_is_denied(self):
        from chitragupta import mcpjobs
        cfg = self.sc.config()
        cfg["mcp"]["database"]["allowed"] = {"qa": ["qa-legacy"]}
        write_config(self.home, cfg)
        allow = mcpjobs.ensure_files(self.cfg())
        self.assertIn("qa-legacy", allow["servers"][SERVER]["connections"])
        self.assertNotIn("qa-legacy", allow["servers"][SERVER]["scopes"])
        self.assertIn("no approved scope", self.reason(Q, {"connectionId": "qa-legacy",
                                                             "sql": "SELECT id FROM shop.orders LIMIT 5"}))
        self.assertIn("no approved scope", self.reason(D, {"connectionId": "qa-legacy", "table": "shop.orders"}))
        self.assertIsNone(self.reason("mcp__%s__list_connections" % SERVER, {}))  # connectionless: unchanged

    def test_case_rules_follow_the_engine(self):
        self.assertIsNone(self.reason(Q, {"connectionId": "qa-main", "sql": "SELECT id FROM SHOP.orders LIMIT 5"}))
        self.assertIsNotNone(self.reason(Q, {"connectionId": "qa-main", "sql": 'SELECT id FROM "SHOP".orders LIMIT 5'}))
        self.assertIsNone(self.reason(Q, {"connectionId": "qa-billing", "sql": "SELECT TOP 5 id FROM [BILLING].refunds"}))

    def test_without_an_explicit_scope_nothing_changes(self):
        from chitragupta import mcpjobs
        cfg = self.sc.config()
        cfg["mcp"]["database"].pop("scope")
        cfg["mcp"]["database"]["allowed"] = {"qa": ["qa-billing"]}
        write_config(self.home, cfg)
        allow = mcpjobs.ensure_files(self.cfg())
        self.assertNotIn("scopes", allow["servers"][SERVER])
        self.assertIsNone(self.reason(Q, {"connectionId": "qa-main", "sql": "SELECT id FROM reporting.x LIMIT 5"}))
        self.assertIsNotNone(self.reason(Q, {"connectionId": "qa-main", "sql": "DELETE FROM reporting.x"}))


class RawRows(Case):
    """DB-17 groundwork: a row job keeps no raw stream; db-jobs never reaches a backup or the search index."""

    def run_job(self, keep_stream, pin=None):
        from chitragupta import common, mcpjobs
        from chitragupta.safety import commands
        mcpjobs.run = self._real_mcp[0]
        seen = {}
        stream = "\n".join(json.dumps(e) for e in (
            {"type": "system", "subtype": "init", "tools": [], "mcp_servers": [{"name": SERVER, "status": "connected"}]},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": Q,
                                                           "input": {"connectionId": "qa-main", "sql": "x"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                      "content": '[{"customer_email": "customer1@acme.test"}]'}]}},
            {"type": "result", "subtype": "success", "result": '{"done": true}', "usage": {}, "total_cost_usd": 0}))

        def fake(argv, purpose, cwd=None, input_text=None, timeout=120, env_extra=None, cfg=None):
            seen["env"] = dict(env_extra or {})
            return 0, stream, ""
        with mock.patch.object(commands, "run", fake), mock.patch.object(mcpjobs, "claude_exe", lambda c: ["claude"]), \
                mock.patch.object(mcpjobs, "claude_json", lambda: {"mcpServers": {SERVER: {"type": "http",
                                                                                          "url": "https://db.acme.test/mcp"}}}):
            res = mcpjobs.run("database-row-test", {SERVER: ["run_query"]}, [Q], [], "task", {"type": "object"},
                              self.cfg(), {}, day="2026-11-06", keep_stream=keep_stream, pin=pin)
        return res, seen, common.read_text(common.wl("cache", "mcp", "database-row-test.last.jsonl"))

    def test_keep_stream_false_keeps_only_a_summary(self):
        from chitragupta import common
        res, seen, kept = self.run_job(True)
        self.assertIn("customer1@acme.test", kept)  # the existing default is unchanged
        self.assertEqual(seen["env"][common.DB_PIN_ENV], "")  # never inherited from this process
        res, seen, kept = self.run_job(False, pin=os.path.join(self.home, ".worklog", "cache", "db-jobs", "j_ab12cd34.pin.json"))
        self.assertNotIn("customer1@acme.test", kept)
        line = json.loads(kept)
        self.assertEqual((line["calls"], line["tools"], line["rc"], line["stream"]), (1, [Q], 0, "not kept"))
        self.assertTrue(seen["env"][common.DB_PIN_ENV].endswith("j_ab12cd34.pin.json"))
        self.assertEqual(res["calls"][0]["result"], '[{"customer_email": "customer1@acme.test"}]')  # in memory only

    def test_backup_and_search_skip_db_jobs(self):
        from chitragupta import common, kbjobs, searchidx
        jobs = common.wl("cache", "db-jobs")
        os.makedirs(jobs, exist_ok=True)
        with open(os.path.join(jobs, "j_ab12cd34.raw.json"), "w") as f:
            f.write('[{"customer_email": "customer1@acme.test"}]')
        dest = os.path.join(self.home, "backups-out")
        cfg = dict(self.cfg(), storage={"local_backup_dir": dest})
        write_config(self.home, cfg)
        job = kbjobs.KBJobs(type("A", (), {"cfg": cfg, "health": {}})())
        late = _dt.datetime(2026, 11, 6, 22, 0).astimezone()
        with mock.patch.object(kbjobs.common, "now", lambda: late):
            job.backup("2026-11-06", {})
        z = os.path.join(dest, "worklog-kb-2026-11-06.zip")
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
        self.assertTrue(any(n.endswith("config.json") for n in names))
        self.assertFalse([n for n in names if "db-jobs" in n])
        files = [p for ps in searchidx.sources().values() for p in ps]
        self.assertFalse([p for p in files if "db-jobs" in p or os.sep + "cache" + os.sep in p])

    def test_job_files_are_removed_with_the_job(self):
        from chitragupta import dbstore
        j = "j_ab12cd34ef56ab78"
        os.makedirs(dbstore.jobs_dir(), exist_ok=True)
        for s in (".pin.json", ".calls.jsonl", ".raw.json", ".used.0"):
            with open(os.path.join(dbstore.jobs_dir(), j + s), "w") as f:
                f.write("x")
        self.assertEqual(dbstore.delete_job_files(j, keep=(".calls.jsonl",)), 3)
        self.assertEqual(os.listdir(dbstore.jobs_dir()), [j + ".calls.jsonl"])
        with self.assertRaises(ValueError):
            dbstore.job_file("../x", ".pin.json")


class Store(Case):
    def test_revisions_and_conflicts_keep_the_unsaved_note(self):
        from chitragupta import dbmodel, dbstore
        rid = dbmodel.new_id("i")
        a = dbstore.save("investigations", {"id": rid, "owner": "u_1", "notes": "first"})
        self.assertEqual(a["revision"], 1)
        b = dbstore.save("investigations", dict(a, notes="second"))
        self.assertEqual(b["revision"], 2)
        with self.assertRaises(dbstore.Conflict) as cm:
            dbstore.save("investigations", dict(a, notes="typed while a refresh saved"))
        self.assertEqual(cm.exception.unsaved["notes"], "typed while a refresh saved")
        self.assertEqual(cm.exception.current["notes"], "second")
        self.assertEqual(dbstore.load("investigations", rid)["notes"], "second")
        for bad in ("../x", "f_0123456789abcdef", "i_XYZ"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    dbstore.record_path("investigations", bad)
        self.assertEqual(dbstore.read_file("meta")["schema_version"], 1)

    def test_graph_pages_and_checkpoints(self):
        from chitragupta import dbmodel, dbstore
        nodes = [dbmodel.db_node(SERVER, "qa-main", "qa", "shop", "shop", "table", "t%03d" % i, "postgres")
                 for i in range(120)]
        self.assertEqual(dbstore.upsert("nodes", nodes), 120)
        self.assertEqual(dbstore.upsert("nodes", nodes), 0)  # the same records again: nothing changes
        g = dbstore.load_graph()["nodes"]
        seen, cursor = [], None
        while True:
            p = dbstore.page(g, cursor, limit=500)
            self.assertLessEqual(len(p["items"]), dbstore.PAGE_MAX)
            seen += [n["id"] for n in p["items"]]
            if not p["more_available"]:
                break
            cursor = p["next"]
        self.assertEqual(sorted(seen), sorted(g))
        dbstore.save_checkpoint("qa-main.list_tables.shop", "shop.t049", seen[:50])
        cp = dbstore.load_checkpoint("qa-main.list_tables.shop")
        self.assertEqual((cp["cursor"], len(cp["seen"]), cp["done"]), ("shop.t049", 50, False))
        with self.assertRaises(ValueError):
            dbstore.checkpoint_path("../escape")

    def test_a_stale_lock_is_taken_over(self):
        from chitragupta import common, dbstore
        p = common.wl("locks", dbstore.LOCK_NAME)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("999 crashed")
        old = time.time() - dbstore.LOCK_STALE_S - 5
        os.utime(p, (old, old))
        with dbstore.lock(wait_s=1):
            pass
        self.assertFalse(os.path.exists(p))
        with open(p, "w") as f:
            f.write("1 busy")
        with self.assertRaises(dbstore.Busy):
            with dbstore.lock(wait_s=0.2):
                pass


if __name__ == "__main__":
    unittest.main()
