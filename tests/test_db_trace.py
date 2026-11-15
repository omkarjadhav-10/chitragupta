"""Record traces (dbtrace, DB-10 / DB-11 / DB-12): ABC-1001's cancellation followed for one tenant-qualified order key
from shop.orders on qa-main (postgres) through the documented OrderCancelled mapping and the billing.tenants lookup to
billing.refunds on qa-billing (mssql), through an explicit, reviewed, bounded plan and the fake database MCP
(FakeTransport: the real guard and the job's plan pin decide every call). DBT-16 (the plan constrains connections,
fields and budget; an expanded plan needs new intent; a wrong fingerprint is refused), DBT-18 (key values are never
rewritten; unsupported shapes fail closed), DBT-19 (row / query / byte limits and the deadline; a client timeout is
"stopped waiting", never a confirmed server cancellation), DBT-20 (a refund not observed is not a failure, deletion
or loss; next checks are offered), DBT-21 (observation times, eventual consistency and an absent baseline stay
visible), DBT-22 (repeated, cancelled and partial runs keep distinct observations; nothing is fabricated; the later
refund observation is added and the earlier bounded result kept), phases + pins and derived phases disabled without
a provider timeout. Synthetic data, temp homes, no server, no model."""
import json
import os
import unittest

from tests.dbfixture import Clock, FakeTransport, Scenario, write_linkmap, write_ticket
from tests.support import Isolated

KEY = {"tenant_id": "7", "id": "1001"}


class Case(Isolated, unittest.TestCase):
    timeout = True

    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()
        write_ticket(self.home)
        write_linkmap(self.home)
        self.sc.provider(timeout=self.timeout)
        from chitragupta import dbingest
        dbingest.run(self.cfg())
        self.fake = FakeTransport(self.sc)

    def cfg(self):
        return self.sc.cfg()

    def ctx(self):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg())

    def flow_id(self):
        from chitragupta import dbflows
        return dbflows.build_flow(self.ctx(), {"kind": "ticket", "key": "ABC-1001"}, save=True, cfg=self.cfg())["id"]

    def oid(self, label):
        from chitragupta import dbflows
        vis = dbflows.Visible(self.ctx())
        return [n for n in vis.nodes if n.startswith("o_") and vis.label(n) == label][0]

    def plan(self, key=None, connections=None, env="qa", **kw):
        from chitragupta import dbtrace
        return dbtrace.make_plan(self.ctx(), kw.pop("flow", None) or self.flow_id(), env, connections,
                                 {"object": self.oid("shop.orders (qa-main)"), "key": dict(KEY if key is None else key)},
                                 cfg=self.cfg(), **kw)

    def run_plan(self, plan, transport=None):
        from chitragupta import dbtrace
        job = dbtrace.request_run(self.ctx(), plan["id"], plan["fingerprint"], "test")
        return dbtrace.run_job(job["id"], transport or self.fake, self.cfg(), {})

    def inv(self, plan):
        from chitragupta import dbstore
        return dbstore.load("investigations", plan["investigation"])

    def obs(self, run, label):
        return [o for o in run["observations"] if o["object_label"] == label][0]


class Journey(Case):
    def test_the_two_connection_journey_end_to_end(self):
        """D2 exit: start key -> shop.orders (qa-main) -> billing.tenants lookup -> billing.refunds (qa-billing), with
        exact call counts, phases and pins; the refund isn't there yet, then a later run observes it."""
        from chitragupta import dbstore
        p = self.plan()
        steps = {s["object_label"]: s for s in p["steps"]}
        self.assertEqual(p["connections"], ["qa-billing", "qa-main"])
        self.assertEqual(p["start"]["fields"], ["tenant_id", "id"])
        refund = steps["billing.refunds (qa-billing)"]
        self.assertEqual(refund["status"], "planned")
        self.assertEqual(sorted(refund["key_columns"]), ["order_ref", "tenant_code"])  # the full composite key
        self.assertEqual(refund["stage"], 1)
        look = steps["billing.tenants (qa-billing)"]
        self.assertEqual((look["role"], look["status"], look["stage"]), ("lookup", "planned", 0))
        self.assertEqual(steps["shop.order_events (qa-main)"]["status"], "mapping_unresolved")
        self.assertIn("partial key", steps["shop.order_events (qa-main)"]["reason"])
        self.assertNotIn("customer_email", json.dumps(steps["shop.orders (qa-main)"]["columns"]))
        self.assertEqual(steps["shop.orders (qa-main)"]["retained"], ["status", "cancelled_at"])
        job = self.run_plan(p)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(self.fake.per_connection(), {"qa-main": 1, "qa-billing": 2})
        self.assertEqual([c["pin"] is not None for c in self.fake.calls], [True, True, True])
        self.assertEqual(len(job["phases"]), 2)  # start key phase (2 calls), then the derived refund phase
        self.assertEqual([ph["calls"] for ph in job["phases"]], [2, 1])
        self.assertTrue(job["accounting"]["reconciled"])
        self.assertEqual(job["accounting"]["ledger"], 3)
        sqls = [c["input"]["sql"] for c in self.fake.calls]
        self.assertIn('WHERE "tenant_id" = 7 AND "id" = 1001', sqls[0])
        self.assertIn("[tenant_id] = 7", sqls[1])
        self.assertIn("[order_ref] = N'1001'", sqls[2])
        self.assertIn("[tenant_code] IN (N'T007')", sqls[2])
        run = self.inv(p)["runs"][0]
        self.assertEqual(self.obs(run, "shop.orders (qa-main)")["status"], "observed")
        self.assertEqual(self.obs(run, "shop.orders (qa-main)")["values"], [{"status": "cancelled",
                                                                             "cancelled_at": "2026-11-06T10:40:00+05:30"}])
        r1 = self.obs(run, "billing.refunds (qa-billing)")
        self.assertEqual(r1["status"], "not_observed_in_scope")
        # later: the delayed consumer wrote the refund; the same investigation adds a run and keeps the first
        self.sc.refund_visible = True
        p2 = self.plan()
        self.assertEqual(p2["fingerprint"], p["fingerprint"])
        self.assertEqual(p2["investigation"], p["investigation"])
        self.run_plan(p2)
        inv = self.inv(p)
        self.assertEqual([r["n"] for r in inv["runs"]], [1, 2])
        self.assertEqual(self.obs(inv["runs"][0], "billing.refunds (qa-billing)")["status"], "not_observed_in_scope")
        self.assertEqual(self.obs(inv["runs"][1], "billing.refunds (qa-billing)")["status"], "observed")
        self.assertEqual(os.listdir(dbstore.jobs_dir()), [])  # every transient file is gone

    def test_a_char_key_stays_text_and_a_downstream_start_reaches_nothing_by_guessing(self):
        """DBT-18: '00123' for a varchar key is sent as the text it is; starting from billing.refunds reaches no other
        object (mappings are followed forward only, never guessed backwards)."""
        from chitragupta import dbtrace
        p = dbtrace.make_plan(self.ctx(), self.flow_id(), "qa", None,
                              {"object": self.oid("billing.refunds (qa-billing)"),
                               "key": {"tenant_code": "T007", "order_ref": "00123"}}, cfg=self.cfg())
        self.assertEqual(p["start"]["fields"], ["tenant_code", "order_ref"])
        self.assertEqual([s["status"] for s in p["steps"] if s["role"] != "start"],
                         ["mapping_unresolved"] * (len(p["steps"]) - 1))
        self.run_plan(p)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIn("[order_ref] = N'00123'", self.fake.calls[0]["input"]["sql"])


class Cli(Case):
    def test_plan_run_wait_status_note_and_handoff(self):
        """`cli db plan … --run --wait` (the runner patched to run in-process with the fake transport; the CLI itself
        never calls the MCP), then status, investigation, note (with its revision) and handoff."""
        import contextlib
        import io
        from unittest import mock
        from chitragupta import cli, dbtrace

        def runner(job_id, cfg=None):
            dbtrace.run_job(job_id, self.fake, self.cfg(), {})

        def call(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(list(argv))
            return rc, buf.getvalue()
        with mock.patch.object(dbtrace, "spawn_runner", runner):
            rc, out = call("db", "plan", "ABC-1001", "--env", "qa", "--object", "shop.orders", "--key", "tenant_id=7",
                           "--key", "id=1001", "--run", "--wait")
        self.assertEqual(rc, 0, out)
        self.assertIn("# Trace plan", out)
        self.assertIn("All planned reads completed", out)
        self.assertIn("Not observed within this query scope", out)
        self.assertEqual(self.fake.per_connection(), {"qa-main": 1, "qa-billing": 2})
        rc, out = call("db", "plan", "ABC-1001", "--env", "qa", "--object", "shop.orders", "--key", "id=1001")
        self.assertEqual(rc, 2)
        self.assertIn("The key is: tenant_id=…, id=…", out)
        iid = json.loads(call("db", "investigations", "--json")[1])["items"][0]["id"]
        self.assertEqual(call("db", "note", iid, "--text", "checked", "--revision", "0")[0], 0)
        rc, out = call("db", "note", iid, "--text", "stale", "--revision", "0")
        self.assertEqual(rc, 3)
        self.assertIn("Your text, kept here:\nstale", out)
        rc, out = call("db", "handoff", iid, "--regenerate", "--copy")
        self.assertEqual(rc, 0)
        self.assertIn("Next action:", out)


class NoTimeout(Case):
    timeout = False

    def test_derived_phases_are_disabled_and_the_plan_says_so(self):
        from chitragupta import dbtrace
        p = self.plan()
        self.assertFalse(p["capabilities"]["derived_phases"])
        self.assertIn(dbtrace.NO_TIMEOUT, p["capabilities"]["text"])
        steps = {s["object_label"]: s for s in p["steps"]}
        self.assertEqual(steps["billing.refunds (qa-billing)"]["status"], "not_queried")
        self.assertIn(dbtrace.NO_TIMEOUT, steps["billing.refunds (qa-billing)"]["reason"])
        self.assertEqual(steps["billing.tenants (qa-billing)"]["status"], "not_queried")  # it would only feed that
        self.assertEqual(p["queries_planned"], 1)
        job = self.run_plan(p)
        self.assertEqual(self.fake.per_connection(), {"qa-main": 1})
        self.assertEqual(job["state"], "completed")
        r = self.obs(self.inv(p)["runs"][0], "billing.refunds (qa-billing)")
        self.assertEqual(r["status"], "not_queried")
        self.assertIn("review a new plan for the next step", r["reason"])


class Intent(Case):
    """DBT-16: the plan fixes connections, fields and budget; anything wider is a new plan needing new intent."""

    def test_a_wrong_fingerprint_or_policy_runs_nothing(self):
        from chitragupta import dbaccess, dbtrace
        p = self.plan()
        with self.assertRaises(dbtrace.RunError):
            dbtrace.request_run(self.ctx(), p["id"], "0" * 24, "test")
        with self.assertRaises(dbtrace.RunError):
            dbtrace.request_run(self.ctx(), p["id"], None, "test")
        dbaccess.revoke("dev-main", "test", cfg=self.cfg())  # any policy change after the review
        with self.assertRaises(dbtrace.RunError) as e:
            dbtrace.request_run(self.ctx(), p["id"], p["fingerprint"], "test")
        self.assertIn("policy changed", str(e.exception))
        self.assertEqual(self.fake.calls, [])
        with self.assertRaises(dbtrace.NotFound):
            dbtrace.request_run(self.ctx(), "p_" + "0" * 24, p["fingerprint"], "test")

    def test_connections_and_budget_are_part_of_the_plan(self):
        from chitragupta import dbtrace
        narrow = self.plan(connections=["qa-main"])
        self.assertEqual(narrow["connections"], ["qa-main"])
        self.assertFalse([s for s in narrow["steps"] if s.get("connection") == "qa-billing"])
        wide = self.plan()
        self.assertNotEqual(narrow["fingerprint"], wide["fingerprint"])
        with self.assertRaises(dbtrace.RunError):  # the reviewed narrow plan's fingerprint can't run the wide plan
            dbtrace.request_run(self.ctx(), wide["id"], narrow["fingerprint"], "test")
        self.run_plan(narrow)
        self.assertEqual(self.fake.per_connection(), {"qa-main": 1})
        for bad in (["qa-reporting"], ["uat-main"], ["prod-main"], ["dev-main"]):
            with self.assertRaises(dbtrace.PlanError, msg=bad):
                self.plan(connections=bad)
        self.sc.extra = {"mcp": dict(self.sc.config()["mcp"])}
        self.sc.extra["mcp"]["database"] = dict(self.sc.extra["mcp"]["database"], trace_limits={"queries": 1})
        self.sc.write()
        small = self.plan()
        self.assertEqual(small["queries_planned"], 1)
        self.assertNotEqual(small["fingerprint"], wide["fingerprint"])
        over = [s for s in small["steps"] if "budget" in s["reason"]]
        self.assertTrue(over)

    def test_one_active_job_per_plan(self):
        from chitragupta import dbtrace
        p = self.plan()
        started = []
        a = dbtrace.request_run(self.ctx(), p["id"], p["fingerprint"], "test", start=started.append)
        b = dbtrace.request_run(self.ctx(), p["id"], p["fingerprint"], "test", start=started.append)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(started, [a["id"]])
        self.assertEqual(self.fake.calls, [])  # requesting is not running: the runner does that
        dbtrace.run_job(a["id"], self.fake, self.cfg(), {})
        self.assertEqual(dbtrace.run_job(a["id"], self.fake, self.cfg(), {})["state"], "completed")  # never twice
        self.assertEqual(len(self.fake.calls), 3)
        with self.assertRaises(dbtrace.RunError) as e:  # the key went with the run: give it again
            dbtrace.request_run(self.ctx(), p["id"], p["fingerprint"], "test")
        self.assertIn("review the plan again with the key", str(e.exception))


class Keys(Case):
    """DBT-18: key fields come from metadata; values are checked, never rewritten; unsupported shapes fail closed."""

    def test_required_fields_and_typed_values(self):
        from chitragupta import dbtrace
        with self.assertRaises(dbtrace.PlanError) as e:
            self.plan(key={"id": "1001"})
        self.assertEqual((e.exception.required, e.exception.field), (["tenant_id", "id"], "tenant_id"))
        with self.assertRaises(dbtrace.PlanError) as e:
            self.plan(key=dict(KEY, order_id="1001"))
        self.assertIn("not part of it: order_id", str(e.exception))
        for bad in ("12;--", "00123", " 7", "7 ", "7.0", "1e3", "", "７"):
            with self.assertRaises(dbtrace.PlanError, msg=repr(bad)) as e:
                self.plan(key={"tenant_id": bad, "id": "1001"})
            self.assertEqual(e.exception.field, "tenant_id")
            if bad.strip():
                self.assertNotIn(bad.strip(), str(e.exception).replace("tenant_id", ""))  # never echoed
        self.assertEqual(self.fake.calls, [])
        p = self.plan(key={"tenant_id": 7, "id": 1001})  # whole numbers given as numbers are the same key
        self.assertEqual(p["key_fingerprint"], self.plan()["key_fingerprint"])

    def test_an_unsupported_dialect_fails_closed(self):
        from chitragupta import dbtrace
        ctx = self.ctx()
        ctx.connections["qa-main"] = dict(ctx.connections["qa-main"], engine="mongodb")
        with self.assertRaises(dbtrace.PlanError) as e:
            dbtrace.make_plan(ctx, self.flow_id(), "qa", None, {"object": self.oid("shop.orders (qa-main)"),
                                                                "key": dict(KEY)}, cfg=self.cfg())
        self.assertIn("fail closed", str(e.exception))

    def test_transforms(self):
        from chitragupta import dbtrace
        P = dbtrace.parse_transform
        self.assertEqual(P("as is")["kind"], "identity")
        self.assertEqual(P("str(order.id)")["kind"], "to_string")
        self.assertEqual(P("lookup billing.tenants.tenant_code by tenant_id"),
                         {"kind": "lookup", "schema": "billing", "table": "tenants", "out": "tenant_code",
                          "by": "tenant_id"})
        self.assertEqual(P("prefix 'ORD-'"), {"kind": "prefix", "literal": "ORD-"})
        c = P("concat(tenant_id, '-', id)", ["tenant_id", "id"])
        self.assertEqual(c["parts"], [{"field": "tenant_id"}, {"literal": "-"}, {"field": "id"}])
        self.assertEqual(dbtrace._apply(c, [["7"], ["1001"]]), ["7-1001"])
        for unknown in ("encode_legacy(order.id)", "hash(id)", "concat(id, x)", "lookup tenants by id", "upper(x)",
                        "prefix 'a'; DROP"):
            self.assertIsNone(P(unknown, ["id"]), unknown)


class Limits(Case):
    """DBT-19: row, query and byte limits and the deadline hold; a client timeout only stops waiting."""

    def limit(self, **kw):
        cfg = self.sc.config()
        cfg["mcp"]["database"]["trace_limits"] = kw
        self.sc.extra = {"mcp": cfg["mcp"]}
        self.sc.write()

    def test_rows_queries_and_bytes(self):
        self.limit(rows_per_step=1, value_bytes=10)
        p = self.plan()
        self.assertEqual({s["limit"] for s in p["steps"] if s["status"] == "planned"}, {1})
        self.run_plan(p)
        self.assertTrue(all(c["input"]["limit"] == 1 for c in self.fake.calls))
        self.assertTrue(all("LIMIT 1" in c["input"]["sql"] or "TOP (1)" in c["input"]["sql"] for c in self.fake.calls))
        o = self.obs(self.inv(p)["runs"][0], "shop.orders (qa-main)")
        self.assertEqual((o["status"], o["coverage"]), ("truncated", "partial"))  # the limit was hit: maybe more rows
        self.assertTrue(o["values_cut"])
        self.assertEqual(o["values"], [])

    def test_the_deadline_stops_waiting_and_publishes_nothing_from_that_phase(self):
        from chitragupta import dbtrace
        clock = Clock()
        self.fake.clock, self.fake.slow = clock, {"qa-main": 500}
        real = dbtrace._clock
        dbtrace._clock = clock
        self.addCleanup(setattr, dbtrace, "_clock", real)
        job = self.run_plan(self.plan())
        self.assertEqual(job["state"], "timed_out")
        self.assertEqual(len(self.fake.calls), 2)  # phase 1 was sent; phase 2 never
        self.assertFalse(job["in_flight"]["cancelled_at_provider"])
        self.assertIn(dbtrace.WAIT_WORDS, job["in_flight"]["text"])
        run = self.inv({"investigation": job["investigation"]})["runs"][0]
        self.assertEqual({o["status"] for o in run["observations"] if o["role"] != "record" or
                          o["object_label"] != "shop.order_events (qa-main)"}, {"not_queried"})
        self.assertEqual([o["values"] for o in run["observations"]], [[]] * len(run["observations"]))

    def test_a_client_timeout_is_not_a_server_cancellation(self):
        from chitragupta import dbtrace
        self.fake.timeouts = {"qa-billing"}
        job = self.run_plan(self.plan())
        self.assertEqual(job["state"], "partial")
        run = self.inv({"investigation": job["investigation"]})["runs"][0]
        look = self.obs(run, "billing.tenants (qa-billing)")
        self.assertEqual(look["status"], "failed")
        self.assertIn(dbtrace.WAIT_WORDS, look["reason"])
        self.assertIsNone(run["in_flight"])  # nothing claims the provider stopped the query
        self.assertFalse([o for o in run["observations"] if "cancel" in (o["reason"] + o["status"]).lower()])
        self.assertEqual(job["retries"], 0)

    def test_cancel_in_flight_stops_new_calls(self):
        from chitragupta import dbtrace
        p = self.plan()
        job = dbtrace.request_run(self.ctx(), p["id"], p["fingerprint"], "test")
        self.fake.on_call = lambda call: dbtrace.request_cancel(self.ctx(), job["id"]) if len(self.fake.calls) == 1 \
            else None
        done = dbtrace.run_job(job["id"], self.fake, self.cfg(), {})
        self.assertEqual(done["state"], "cancelled")
        self.assertEqual(len(self.fake.calls), 2)  # the second call of that phase was refused by the pin
        self.assertFalse(done["in_flight"]["cancelled_at_provider"])
        self.assertIn("Cancellation requested", done["in_flight"]["text"])
        self.assertIn(dbtrace.WAIT_WORDS, done["in_flight"]["text"])
        run = self.inv(p)["runs"][0]
        self.assertEqual(run["state"], "cancelled")
        self.assertFalse([o for o in run["observations"] if o["status"] in ("observed", "not_observed_in_scope")])

    def test_cancel_before_start_runs_nothing(self):
        from chitragupta import dbtrace
        p = self.plan()
        job = dbtrace.request_run(self.ctx(), p["id"], p["fingerprint"], "test")
        self.assertEqual(dbtrace.request_cancel(self.ctx(), job["id"])["state"], "cancel_requested")
        self.assertEqual(dbtrace.run_job(job["id"], self.fake, self.cfg(), {})["state"], "cancelled")
        self.assertEqual(self.fake.calls, [])


class Semantics(Case):
    def test_not_observed_is_never_failure_or_loss(self):
        """DBT-20."""
        from chitragupta import dbtrace
        job = self.run_plan(self.plan())
        self.assertEqual(job["state"], "completed")
        run = self.inv({"investigation": job["investigation"]})["runs"][0]
        r = self.obs(run, "billing.refunds (qa-billing)")
        self.assertEqual((r["status"], r["status_label"]), ("not_observed_in_scope",
                                                            "Not observed within this query scope"))
        self.assertEqual(r["reason"], dbtrace.NOT_OBSERVED)
        self.assertEqual(r["coverage_label"], "Complete for the declared bounded query")
        self.assertTrue(r["async_boundary"])
        self.assertIn(dbtrace.RERUN, r["next"])
        self.assertTrue([x for x in r["next"] if "processing log" in x])
        for word in ("failed", "deleted", "lost", "data loss"):
            self.assertNotIn(word, (r["status"] + r["status_label"]).lower())
        self.assertNotIn("fail", run["state"])

    def test_times_windows_and_baselines(self):
        """DBT-21: separate observation times per connection, source times as returned + UTC only with a zone, the
        window and the clock note; a first observation has no 'before'."""
        from chitragupta import dbtrace
        job = self.run_plan(self.plan())
        run = self.inv({"investigation": job["investigation"]})["runs"][0]
        o = self.obs(run, "shop.orders (qa-main)")
        self.assertEqual(o["source_times"], [{"column": "cancelled_at", "as_returned": "2026-11-06T10:40:00+05:30",
                                              "utc": "2026-11-06T05:10:00Z", "zone": "known"}])
        self.assertTrue(o["observed_at"].endswith("Z"))
        self.assertEqual(run["window"]["note"], dbtrace.CLOCK_NOTE)
        self.assertTrue(run["window"]["from"] <= run["window"]["to"])
        st = dbtrace._source_times({"retained": ["created_at"], "meta": [{"name": "created_at", "class": "datetime"}]},
                                   [{"created_at": "2026-11-06T10:42:00"}])
        self.assertEqual(st, [{"column": "created_at", "as_returned": "2026-11-06T10:42:00", "utc": "",
                               "zone": "zone unknown"}])
        self.sc.refund_visible = True
        self.run_plan(self.plan())
        ch = dbtrace.changes(self.inv({"investigation": job["investigation"]}))
        first = [c for c in ch if c["kind"] == "first_observed"]
        self.assertEqual(len(first), 1)
        self.assertIn("Before state unavailable", first[0]["text"])

    def test_repeated_cancelled_and_partial_runs_stay_distinct(self):
        """DBT-22: identical reads are no change; a real change shows both observations; a partial and a cancelled
        run keep their own observations."""
        from chitragupta import dbtrace
        p = self.plan()
        self.run_plan(p)
        self.fake.timeouts = {"qa-billing"}
        self.run_plan(self.plan())
        self.fake.timeouts = set()
        p3 = self.plan()
        j3 = dbtrace.request_run(self.ctx(), p3["id"], p3["fingerprint"], "test")
        dbtrace.request_cancel(self.ctx(), j3["id"])
        dbtrace.run_job(j3["id"], self.fake, self.cfg(), {})
        self.run_plan(self.plan())
        inv = self.inv(p)
        self.assertEqual([r["state"] for r in inv["runs"]], ["completed", "partial", "cancelled", "completed"])
        ids = [o["id"] for r in inv["runs"] for o in r["observations"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse([c for c in dbtrace.changes(inv) if c["kind"] == "changed"])  # same values: no event
        real = self.sc.rows

        def rows(conn, schema, table):
            out = real(conn, schema, table)
            return [dict(r, status="refunded") if table == "orders" and r.get("id") == 1001 and r.get("tenant_id") == 7
                    else r for r in out]
        self.sc.rows = rows
        self.run_plan(self.plan())
        ch = [c for c in dbtrace.changes(self.inv(p)) if c["kind"] == "changed"]
        self.assertEqual(len(ch), 1)
        self.assertEqual((ch[0]["column"], ch[0]["before"]["value"], ch[0]["after"]["value"]),
                         ("status", "cancelled", "refunded"))
        self.assertTrue(ch[0]["before"]["observed_at"] and ch[0]["after"]["observed_at"])

    def test_a_configured_lag_only_raises_a_signal(self):
        cfg = self.sc.config()
        cfg["mcp"]["database"]["lag_signals"] = {"qa-billing:billing.refunds": 60}
        self.sc.extra = {"mcp": cfg["mcp"]}
        self.sc.write()
        job = self.run_plan(self.plan())
        r = self.obs(self.inv({"investigation": job["investigation"]})["runs"][0], "billing.refunds (qa-billing)")
        self.assertEqual(r["status"], "not_observed_in_scope")
        self.assertTrue(r["signals"] and r["signals"][0].startswith("Investigation signal"))
        self.assertIn("not evidence of a failure", r["signals"][0])

    def test_permission_denied_quarantines_and_is_not_retried(self):
        from chitragupta import dbaccess, dbconnector
        self.fake.denied = {"qa-billing"}
        job = self.run_plan(self.plan())
        self.assertEqual(job["state"], "partial")
        self.assertEqual(self.fake.per_connection(), {"qa-main": 1, "qa-billing": 1})
        run = self.inv({"investigation": job["investigation"]})["runs"][0]
        self.assertEqual(self.obs(run, "billing.tenants (qa-billing)")["status"], "permission_denied")
        self.assertEqual(self.obs(run, "billing.refunds (qa-billing)")["status"], "not_queried")
        q = dbaccess.quarantine_entries()
        self.assertTrue([e for e in q.values() if e["connection"] == "qa-billing"])
        caps = dbconnector.load_capabilities()["connections"]["qa-billing"]["capabilities"]
        self.assertEqual(caps["row_query"]["state"], "permission_denied")


class LegacyTrace(Case):
    """/cg:trace's QA example (mcpjobs.trace_plan / qa_trace) keeps its file format, with the PRD's semantics: a quoted
    value is declined (reported), never stripped (DBT-18); a numeric-looking string is a number only for a column the
    card declares integer; the name-similarity start is labelled a guess; a model before/after is kept as observed
    only when both values were really observed."""

    def test_values_are_declined_or_typed_never_rewritten(self):
        from chitragupta import mcpjobs
        cfg = self.cfg()
        report = []
        st, sv, plan = mcpjobs.trace_plan(["orderId=1001'--"], {"shop.orders": ["status"]}, cfg, report=report)
        self.assertEqual((st, plan), (None, []))
        self.assertIn("declined (never stripped)", report[0])
        st, sv, plan = mcpjobs.trace_plan(["orderId=1001"], {"shop.orders": ["status"]}, cfg)
        self.assertEqual(st, "shop.orders")
        self.assertIn("WHERE id = 1001", plan[0][2])  # the card declares id an integer
        st, sv, plan = mcpjobs.trace_plan(["orderId=00123"], {"shop.orders": ["status"]}, cfg)
        self.assertIn("WHERE id = '00123'", plan[0][2])  # not a whole number as written: kept as text
        self.assertEqual(mcpjobs._trace_literal("1001", {"dataType": "varchar(32)"}), "'1001'")
        self.assertEqual(mcpjobs._trace_literal("1001", {}), "'1001'")  # type unknown: never a number

    def test_model_changes_are_kept_only_when_observed(self):
        from unittest import mock
        from chitragupta import common, mcpjobs

        def fake(job, servers, reads, writes, task, schema, cfg, health, **k):
            return {"calls": [{"name": "mcp__shopdb-stub__run_query", "input": {"connectionId": "qa-main", "sql":
                               "SELECT id, status FROM shop.orders WHERE id = 1001 LIMIT 20"},
                               "result": json.dumps([{"id": 1001, "status": "cancelled"}]), "is_error": False}],
                    "out": {"steps": [{"table": "shop.orders", "key": "id = 1001", "values": "status=cancelled",
                                       "note": ""}], "summary": "Order cancelled.", "changes": [
                        {"table": "shop.orders", "column": "status", "before": "open", "after": "cancelled"},
                        {"table": "shop.orders", "column": "cancelled_at", "before": "-", "after": "now"}]}}
        with mock.patch.object(mcpjobs, "run", fake):
            rec = mcpjobs.qa_trace("ABC-1001", ["orderId=1001"], {"shop.orders": ["status"]}, self.cfg(), {},
                                   before={"shop.orders": {"status": "open"}})
        ch = rec["trace"]["changes"]
        self.assertEqual([c["validated"] for c in ch], [True, False])
        self.assertEqual(ch[1]["basis"], "model summary, unvalidated")
        self.assertEqual(rec["start"]["basis"], "inferred")
        self.assertIn("a guess, never a mapping", rec["start"]["how"])
        saved = common.read_json(common.hp("data", "qa-trace", "ABC-1001.json"))
        self.assertEqual(saved["trace"]["changes"][1]["basis"], "model summary, unvalidated")
        from chitragupta import kbjobs
        md = kbjobs.dbflow_qa_section("ABC-1001", self.cfg())
        self.assertIn("now (model summary, unvalidated)", md)
        self.assertIn("a guess, never a mapping", md)


if __name__ == "__main__":
    unittest.main()
