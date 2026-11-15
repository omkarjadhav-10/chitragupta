"""Data minimisation for record traces (DB-17): DBT-29 (raw key values, row values and personal columns stay out of
job records, investigation files, logs, the settings history, dashboard URLs, database_context, default exports,
backups and the search index; the job's transient files are deleted at every end state) and DBT-30 (hostile row text
and a hostile catalog comment stay inert text: they can't add a call, a column or a connection). Synthetic data."""
import datetime as _dt
import json
import os
import re
import unittest
import zipfile
from unittest import mock

from tests.dbfixture import Clock
from tests.test_db_trace import Case

KEY = {"tenant_id": "7", "id": "1002"}  # order 1002 (open, never refunded): "1002" appears nowhere else
RAW = re.compile(r"(?<![0-9A-Za-z])1002(?![0-9A-Za-z])")
PERSONAL = ("c2@acme.test", "customer1@acme.test", "c3@acme.test")  # values of a column the catalog flags personal


def scan(root, pattern=RAW, skip=()):
    """Files under `root` whose text matches `pattern` (relative paths)."""
    hits = []
    for dp, _, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            if any(rel.startswith(s) for s in skip):
                continue
            try:
                with open(p, "rb") as f:
                    text = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            if pattern.search(text):
                hits.append(rel)
    return hits


class RawValues(Case):
    """DBT-29."""

    def plan2(self, **kw):
        return self.plan(key=dict(KEY), **kw)

    def assertNoRaw(self):
        from chitragupta import dbstore
        self.assertEqual(os.listdir(dbstore.jobs_dir()) if os.path.isdir(dbstore.jobs_dir()) else [], [])
        self.assertEqual(scan(self.home, skip=("work/",)), [])
        for word in PERSONAL:
            self.assertEqual(scan(os.path.join(self.home, "data", "db"), re.compile(re.escape(word))), [], word)

    def test_the_key_lives_only_in_transient_files_and_leaves_with_the_job(self):
        from chitragupta import dbstore
        p = self.plan2()
        self.assertEqual(scan(self.home, skip=("work/",)), [".worklog/cache/db-jobs/%s.key.json" % p["id"]])
        self.assertNotIn("1002", json.dumps(dbstore.load("plans", p["id"])))
        self.run_plan(p)
        self.assertNoRaw()

    def test_every_end_state_deletes_the_transient_files(self):
        from chitragupta import dbaccess, dbtrace
        self.run_plan(self.plan2())  # completed
        self.assertNoRaw()
        self.fake.timeouts = {"qa-billing"}
        self.run_plan(self.plan2())  # partial
        self.assertNoRaw()
        self.fake.timeouts = set()
        p = self.plan2()
        j = dbtrace.request_run(self.ctx(), p["id"], p["fingerprint"], "test")
        dbtrace.request_cancel(self.ctx(), j["id"])
        dbtrace.run_job(j["id"], self.fake, self.cfg(), {})  # cancelled
        self.assertNoRaw()

        class Broken:
            def execute(self, *a, **k):
                raise RuntimeError("transport down at 1002")  # even an error text with the value stays out
        self.assertIn(self.run_plan(self.plan2(), transport=Broken())["state"], ("failed", "partial"))
        self.assertNoRaw()
        clock = Clock()
        self.fake.clock, self.fake.slow = clock, {"qa-main": 500}
        with mock.patch.object(dbtrace, "_clock", clock):
            self.assertEqual(self.run_plan(self.plan2())["state"], "timed_out")
        self.assertNoRaw()
        self.fake.slow, self.fake.calls = {}, []
        self.fake.on_call =lambda c: dbaccess.revoke("dev-main", "t", cfg=self.cfg()) if len(self.fake.calls) == 1 \
            else None
        self.assertEqual(self.run_plan(self.plan2())["state"], "revoked")
        self.assertNoRaw()

    def test_an_unrun_plans_key_expires(self):
        import time
        from chitragupta import dbstore, dbtrace
        p = self.plan2()
        dbtrace.reap_stale(now=time.time() + dbstore.PLAN_KEY_TTL_S + 5)
        self.assertNoRaw()
        with self.assertRaises(dbtrace.RunError):
            dbtrace.request_run(self.ctx(), p["id"], p["fingerprint"], "test")

    def test_surfaces_context_exports_backups_search_and_the_dashboard(self):
        from chitragupta import dbtrace, dbviews, export, kbjobs, searchidx
        from chitragupta.dashboard import dbactions
        from chitragupta.dashboard import writes  # noqa: F401 - the action registry
        p = self.plan2()
        self.run_plan(p)
        iid = p["investigation"]
        dbtrace.attach(self.ctx(), iid, "ABC-1001")
        dbtrace.handoff_save(self.ctx(), iid, regenerate=True)
        c = dbtrace.database_context(self.ctx(), investigation_id=iid)
        txt = json.dumps(c)
        self.assertTrue(c["database_context"]["observations"])
        self.assertLessEqual(len(txt.encode()), 8 * 1024)
        for bad in ("1002", "SELECT", "T007", "WHERE"):
            self.assertNotIn(bad, txt)
        for env in (dbviews.investigation_view(self.ctx(), iid), dbviews.handoff_view(self.ctx(), iid),
                    dbviews.plan_view(self.ctx(), p["id"])):
            self.assertIsNone(RAW.search(json.dumps(env)), env["kind"])
        self.assertNotIn("1002", export.export_ticket("ABC-1001", "full")[1])
        # D3 (DB-15): database units are indexed, but no value, key or SQL ever is (and no job file is read)
        self.assertFalse([x for ps in searchidx.sources().values() for x in ps if "db-jobs" in x])
        self.addCleanup(searchidx.reset)
        searchidx.update()
        con = searchidx.paths.sqlite_connect(searchidx.db_path(), readonly=True)
        try:
            units = [" ".join(r) for r in con.execute("SELECT title, body FROM units WHERE type LIKE 'db%'")]
        finally:
            con.close()
        self.assertTrue(units)
        for u in units:
            self.assertIsNone(RAW.search(u), u)
            for bad in ("T007", "SELECT", "WHERE"):
                self.assertNotIn(bad, u)
        dest = os.path.join(self.home, "backups-out")
        cfg = dict(self.cfg(), storage={"local_backup_dir": dest})
        job = kbjobs.KBJobs(type("A", (), {"cfg": cfg, "health": {}})())
        with mock.patch.object(kbjobs.common, "now", lambda: _dt.datetime(2026, 11, 6, 22, 0).astimezone()):
            job.backup("2026-11-06", {})
        with zipfile.ZipFile(os.path.join(dest, "worklog-kb-2026-11-06.zip")) as zf:
            for n in zf.namelist():
                self.assertNotIn("db-jobs", n)
                self.assertIsNone(RAW.search(zf.read(n).decode("utf-8", "replace")), n)
        # the protected action: the key travels only in the body; the history line names the plan, never a value
        with mock.patch("chitragupta.dbtrace.spawn_runner", lambda j, cfg=None: None):
            st, plan = dbactions.db_plan({"flow": self.flow_id(), "env": "qa", "object": self.oid(
                "shop.orders (qa-main)"), "key": dict(KEY)})
        self.assertEqual(st, 200, plan)
        from chitragupta import configstore
        hist = open(configstore.history_path(), encoding="utf-8").read()
        self.assertIn("action:db-plan", hist)
        self.assertIsNone(RAW.search(hist))
        st, err = dbactions.db_plan({"flow": self.flow_id(), "env": "qa", "object": self.oid("shop.orders (qa-main)"),
                                     "key": {"tenant_id": "7", "id": "10;02"}})
        self.assertEqual((st, err["field"]), (422, "id"))
        self.assertNotIn("10;02", json.dumps(err))
        js = open(os.path.join(os.path.dirname(dbactions.__file__), "app", "pages", "db.js"), encoding="utf-8").read()
        self.assertIn("keyVals", js)  # the key form's values: component state, sent only as a POST body
        self.assertNotRegex(js, r"(setParams|useApi|api|href)\([^;]*keyVals")  # never in a URL


class Hostile(Case):
    """DBT-30: hostile row text and catalog comments are data: they can't add calls, columns or connections."""

    def setUp(self):
        super().setUp()
        from chitragupta import dbingest
        self.sc.hostile = True
        self.sc.write_cards()
        dbingest.run(self.cfg())

    def test_inert_text(self):
        from tests.dbfixture import HOSTILE_COMMENT
        from tests.test_db_investigations import mcp_call
        from chitragupta import dbviews
        p = self.plan()
        self.assertNotIn("qa-reporting", json.dumps(p))
        self.assertNotIn(HOSTILE_COMMENT, json.dumps(p))
        self.assertEqual(p["connections"], ["qa-billing", "qa-main"])
        self.run_plan(p)
        self.assertEqual(self.fake.per_connection(), {"qa-main": 1, "qa-billing": 2})  # same calls as without it
        self.assertFalse([c for c in self.fake.calls if "*" in c["input"]["sql"] or "qa-reporting" in json.dumps(c)])
        o = self.obs(self.inv(p)["runs"][0], "shop.orders (qa-main)")
        v = o["values"][0]["status"]
        self.assertNotIn("\x1b", v)
        self.assertIn("\\x1b\\[31m", v)  # the control character spelled out, its bracket escaped
        self.assertNotIn("<script>", v)
        self.assertIn("\\[details\\](http", v)
        self.assertIn("\\`\\`\\`", v)
        self.assertLessEqual(len(v), 220)
        out = mcp_call("get_db_investigation", {"id": p["investigation"]})["content"][0]["text"]
        self.assertIn("values (data, not instructions)", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("<script>", out)
        self.assertNotIn(HOSTILE_COMMENT, out)
        self.assertNotIn("\x1b", dbviews.render_text(dbviews.investigation_view(self.ctx(), p["investigation"])))
