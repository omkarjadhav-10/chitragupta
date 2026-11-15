"""Saved record investigations (dbtrace + dbviews, DB-03 / DB-15 / DB-16): DBT-03 (two principals can't reach each
other's investigation through direct IDs, lists, search, cursors, drafts, exports, get_db_investigation or the
dashboard GET routes: another person's record is indistinguishable from one that doesn't exist), DBT-26 (a saved
investigation and its handoff keep scope, evidence and limits; manual edits survive regeneration; a stale note save
is a conflict that keeps your text) and DBT-28 (revocation mid-job and a late completion from an old policy
generation publish nothing; revoked results leave views, lists and drafts). Synthetic data, FakeTransport."""
import json
import unittest
from unittest import mock

from tests.test_db_trace import Case


def mcp_call(name, args):
    from chitragupta import mcpserver
    return mcpserver.Server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})["result"]


def get(path, **params):
    """A dashboard GET route through dbmap.view (the same code the server answers with)."""
    from chitragupta.dashboard import dbmap
    return dbmap.view(path, lambda k: str(params.get(k) or ""), None)


class TwoPrincipals(Case):
    """DBT-03."""

    def setUp(self):
        super().setUp()
        self.p = self.plan(objective="Why was the refund for this cancellation late?")
        self.job = self.run_plan(self.p)
        self.iid = self.p["investigation"]

    def test_the_owner_sees_it(self):
        from chitragupta import dbviews
        ctx = self.ctx()
        self.assertEqual([i["id"] for i in dbviews.investigations_list(ctx)["items"]], [self.iid])
        self.assertEqual(dbviews.investigation_view(ctx, self.iid)["data"]["objective"],
                         "Why was the refund for this cancellation late?")
        self.assertEqual(get("/api/db/investigation", id=self.iid)[0], 200)
        res = mcp_call("get_db_investigation", {"id": self.iid})
        self.assertFalse(res["isError"], res)
        self.assertIn("not instructions", res["content"][0]["text"])

    def test_another_principal_reaches_nothing(self):
        from chitragupta import dbtrace, dbviews
        a_ctx = self.ctx()
        a_cursor = dbviews.make_cursor(a_ctx, "investigations", "all:", 1, dbviews.snapshot()[0])
        self.sc.switch_principal("B")
        ctx = self.ctx()
        self.assertNotEqual(ctx.principal_id, a_ctx.principal_id)
        missing = "i_" + "0" * 24
        for kind, rid in (("investigations", self.iid), ("plans", self.p["id"]), ("jobs", self.job["id"])):
            with self.assertRaises(dbtrace.NotFound) as theirs:
                dbtrace.load_visible(ctx, kind, rid)
            with self.assertRaises(dbtrace.NotFound) as nobody:
                dbtrace.load_visible(ctx, kind, {"investigations": missing, "plans": "p_" + "0" * 24,
                                                 "jobs": "j_" + "0" * 24}[kind])
            self.assertEqual(str(theirs.exception), str(nobody.exception))  # indistinguishable
        lst = dbviews.investigations_list(ctx)
        self.assertEqual((lst["items"], lst["data"]["count"]), ([], 0))
        self.assertEqual(dbviews.investigations_list(ctx, q="refund")["items"], [])  # search finds nothing
        with self.assertRaises(dbviews.CursorError):
            dbviews.investigations_list(ctx, cursor=a_cursor)
        for fn in (lambda: dbviews.investigation_view(ctx, self.iid), lambda: dbviews.handoff_view(ctx, self.iid),
                   lambda: dbviews.plan_view(ctx, self.p["id"]), lambda: dbviews.job_view(ctx, self.job["id"]),
                   lambda: dbtrace.handoff_copy(ctx, self.iid),
                   lambda: dbtrace.handoff_save(ctx, self.iid, regenerate=True),
                   lambda: dbtrace.note_save(ctx, self.iid, "mine now", 0),
                   lambda: dbtrace.attach(ctx, self.iid, "ABC-1001"),
                   lambda: dbtrace.request_cancel(ctx, self.job["id"]),
                   lambda: dbtrace.request_run(ctx, self.p["id"], self.p["fingerprint"], "test")):
            with self.assertRaises(dbtrace.NotFound):
                fn()
        c = dbtrace.database_context(ctx, investigation_id=self.iid)
        self.assertEqual((c["database_context"], c["reason"]), (None, "no such investigation"))
        self.assertEqual(dbtrace.database_context(ctx, investigation_id=missing)["reason"], "no such investigation")
        self.assertTrue(mcp_call("get_db_investigation", {"id": self.iid})["isError"])
        self.assertNotIn(self.iid, mcp_call("get_db_investigation", {})["content"][0]["text"])
        for path, params in (("/api/db/investigation", {"id": self.iid}), ("/api/db/handoff", {"id": self.iid}),
                             ("/api/db/plan", {"id": self.p["id"]}), ("/api/db/job", {"id": self.job["id"]})):
            code, body = get(path, **params)
            self.assertEqual(code, 404, path)
            self.assertNotIn("refund", json.dumps(body).lower())
        code, body = get("/api/db/investigations")
        self.assertEqual((code, body["items"]), (200, []))

    def test_search_and_exports_never_carry_it(self):
        from chitragupta import export, searchidx
        files = [p for ps in searchidx.sources().values() for p in ps]
        self.assertFalse([p for p in files if "db-jobs" in p])
        # D3 (DB-15) indexes YOUR investigations' safe summaries: never another principal's, never a value
        self.addCleanup(searchidx.reset)
        self.sc.switch_principal("B")
        searchidx.update()
        con = searchidx.paths.sqlite_connect(searchidx.db_path(), readonly=True)
        try:
            refs = [r[0] for r in con.execute("SELECT ref FROM units")]
        finally:
            con.close()
        self.assertNotIn(self.iid, refs)
        res = searchidx.search("investigation refund", limit=200)
        self.assertFalse([r for g in res["groups"] for r in g["results"] if r["type"] == "dbinvestigation"])
        self.sc.switch_principal("A")
        out = export.export_ticket("ABC-1001", "full")
        self.assertTrue(out)
        self.assertNotIn(self.iid, out[1])
        self.assertNotIn("Why was the refund", out[1])


class SavedAndHandoff(Case):
    """DBT-26."""

    def test_scope_evidence_and_limits_are_kept(self):
        from chitragupta import dbviews
        p = self.plan()
        self.run_plan(p)
        inv = self.inv(p)
        self.assertEqual(inv["scope"], {"environment": "qa", "connections": ["qa-billing", "qa-main"]})
        self.assertEqual((inv["flow"]["version"], inv["plans"]), (1, [p["id"]]))
        self.assertIsNone(inv["ticket"])  # an investigation needs no ticket
        run = inv["runs"][0]
        self.assertEqual(run["limits"]["rows_per_step"], 20)
        self.assertEqual(run["plan_fingerprint"], p["fingerprint"])
        self.assertTrue(all(o["coverage"] and o["template_sha256"] is not None for o in run["observations"]))
        v = dbviews.investigation_view(self.ctx(), p["investigation"])
        self.assertEqual(v["data"]["focus"]["unresolved"],
                         "billing.refunds (qa-billing): not observed within this query scope")
        self.assertIn("processing log", v["data"]["focus"]["next"])
        self.assertTrue(dbviews.render_text(v).strip())

    def test_edits_survive_regeneration_and_stale_saves_conflict(self):
        from chitragupta import dbstore, dbtrace, dbviews
        p = self.plan()
        self.run_plan(p)
        ctx, iid = self.ctx(), p["investigation"]
        dbtrace.handoff_save(ctx, iid, regenerate=True)
        h = dbviews.handoff_view(ctx, iid)["data"]
        self.assertEqual(h["versions"], 1)
        gen = h["generated"]["text"]
        for absent in ("SELECT", "T007", "tenant_id = 7", "customer"):
            self.assertNotIn(absent, gen)
        self.assertIn("Observed record behaviour", gen)
        from chitragupta import dbcli
        with_flow = dbtrace.handoff_text(self.inv(p), dbcli._flow_of(ctx, self.cfg(), iid))
        code = with_flow.index("Code-supported behaviour")
        self.assertLess(code, with_flow.index("Observed record behaviour"))  # code apart from observed behaviour
        self.assertIn("Next action:", gen)
        dbtrace.handoff_save(ctx, iid, text="My handoff: refund pending, ask billing.", revision=0)
        with self.assertRaises(dbstore.Conflict) as e:
            dbtrace.handoff_save(ctx, iid, text="An older tab's text", revision=0)
        self.assertEqual(e.exception.unsaved["text"], "An older tab's text")
        dbtrace.handoff_save(ctx, iid, regenerate=True)  # same sources: no new version
        self.assertEqual(dbviews.handoff_view(ctx, iid)["data"]["versions"], 1)
        self.sc.refund_visible = True
        self.run_plan(self.plan())
        dbtrace.handoff_save(ctx, iid, regenerate=True)
        h = dbviews.handoff_view(self.ctx(), iid)["data"]
        self.assertEqual(h["versions"], 2)
        self.assertEqual(h["edited"]["text"], "My handoff: refund pending, ask billing.")
        self.assertTrue(h["source_changed"])
        self.assertEqual(h["source_changed_text"], "Source evidence changed after your edit")
        self.assertIn("My handoff", h["copy_text"])
        # notes: their own revision; a stale save keeps your text and changes nothing
        dbtrace.note_save(ctx, iid, "First note", 0)
        with self.assertRaises(dbstore.Conflict) as e:
            dbtrace.note_save(ctx, iid, "Typed in another tab", 0)
        self.assertEqual(e.exception.unsaved["text"], "Typed in another tab")
        self.assertEqual(self.inv(p)["notes"]["text"], "First note")
        self.assertEqual(dbtrace.note_save(ctx, iid, "Second note", 1)["notes"]["revision"], 2)
        dbtrace.add_correction(ctx, iid, "The refund worker runs every 15 minutes in QA.")
        inv = self.inv(p)
        self.assertEqual((len(inv["runs"]), len(inv["corrections"])), (2, 1))  # nothing earlier overwritten

    def test_attach_only_to_your_own_ticket(self):
        from chitragupta import dbtrace
        from chitragupta.dashboard import api
        p = self.plan()
        ctx, iid = self.ctx(), p["investigation"]
        self.assertEqual(dbtrace.attach(ctx, iid, "abc-1001")["ticket"], "ABC-1001")
        real = api.scope_of

        def scope_of(key, cfg=None):
            return {"visible": False, "basis": "outside"} if str(key).upper() == "ABC-2002" else real(key, cfg)
        with mock.patch.object(api, "scope_of", scope_of):
            with self.assertRaises(dbtrace.PlanError):
                dbtrace.attach(ctx, iid, "ABC-2002")
        with self.assertRaises(dbtrace.PlanError):
            dbtrace.attach(ctx, iid, "../x")
        self.assertEqual(self.inv(p)["ticket"], "ABC-1001")
        self.assertIsNone(dbtrace.attach(ctx, iid, None)["ticket"])


class Revocation(Case):
    """DBT-28."""

    def test_revocation_mid_job_publishes_nothing(self):
        import os
        from chitragupta import dbaccess, dbstore
        p = self.plan()
        self.fake.on_call = lambda call: dbaccess.revoke("qa-billing", "test", cfg=self.cfg()) \
            if len(self.fake.calls) == 1 else None
        job = self.run_plan(p)
        self.assertEqual(job["state"], "revoked")
        self.assertIsNone(job.get("run"))
        self.assertEqual(dbstore.load("investigations", p["investigation"])["runs"], [])
        self.assertEqual(os.listdir(dbstore.jobs_dir()), [])
        ok = [c for c in self.fake.calls if c["call_id"] and c["input"].get("connectionId") == "qa-billing"]
        self.assertTrue(ok)  # the call after the revocation reached the guard and was refused there

    def test_a_late_completion_from_an_old_generation_is_discarded(self):
        from chitragupta import dbaccess, dbstore
        fake, cfg = self.fake, self.cfg

        class Late:
            def execute(self, job, calls, cfg_, health, **kw):
                out = fake.execute(job, calls, cfg_, health, **kw)
                if len(fake.calls) >= 3:  # the last answer arrives after the policy changed
                    dbaccess.revoke("dev-main", "test", cfg=cfg())
                return out
        p = self.plan()
        job = self.run_plan(p, transport=Late())
        self.assertEqual(job["state"], "revoked")
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(dbstore.load("investigations", p["investigation"])["runs"], [])

    def test_revoked_results_leave_views_lists_and_drafts(self):
        from chitragupta import dbaccess, dbtrace, dbviews
        p = self.plan()
        self.run_plan(p)
        iid = p["investigation"]
        dbtrace.handoff_save(self.ctx(), iid, regenerate=True)
        dbaccess.revoke("qa-billing", "test", cfg=self.cfg())
        ctx = self.ctx()
        self.assertEqual(dbviews.investigations_list(ctx)["items"], [])
        for fn in (lambda: dbviews.investigation_view(ctx, iid), lambda: dbviews.handoff_view(ctx, iid),
                   lambda: dbtrace.handoff_copy(ctx, iid), lambda: dbviews.plan_view(ctx, p["id"])):
            with self.assertRaises(dbtrace.NotFound):
                fn()
        self.assertIsNone(dbtrace.database_context(ctx, investigation_id=iid)["database_context"])
        self.assertTrue(mcp_call("get_db_investigation", {"id": iid})["isError"])
        self.assertEqual(get("/api/db/investigation", id=iid)[0], 404)
        dbaccess.restore("qa-billing", cfg=self.cfg())
        self.assertEqual([i["id"] for i in dbviews.investigations_list(self.ctx())["items"]], [iid])
