"""Operational bounds on a large synthetic fixture (DB-25, DBT-37): 1,200 objects, > 5,000 relationships (foreign-key
cycles inside each component, 24 disconnected components, code readers / writers) and 120 saved investigations,
generated once. Retrieval and expansion stay bounded (<= 100 nodes per view, <= 50 items per page, a continuation
cursor), a detail is <= 64 KiB, database_context <= 8 KiB, cached detail reads are timed (p50 / p95), reads make zero
transport, model or process calls, a repeated request queues one job, and an exhausted budget gives a deterministic,
resumable result. Set CG_SCALE_REPORT=1 to print the measured numbers (they are recorded in the implementation plan)."""
import json
import os
import random
import sys
import time
import unittest
from unittest import mock

from tests.dbfixture import SERVER, Scenario
from tests.support import Isolated

TABLES, COMPONENTS, CODE, INVESTIGATIONS = 1200, 24, 240, 120


def generate(ctx, repo_id):
    """Write the large graph straight into the store (no ingestion): -> (table ids, code ids)."""
    from chitragupta import dbmodel, dbstore
    nodes, edges, tables, cols = [], [], [], {}
    for i in range(TABLES):
        t = dbmodel.db_node(SERVER, "qa-main", "qa", "shop", "shop", "table", "t%04d" % i, "postgres",
                            attrs={"described": True, "fetched_at": "2026-11-06T09:00:00+05:30"})
        nodes.append(t)
        tables.append(t["id"])
        cs = [dbmodel.member_node(t, n, "postgres", attrs={"data_type": "integer", "nullable": False,
                                                           "primary_key": n == "id"}) for n in ("id", "ref")]
        nodes += cs
        cols[t["id"]] = [c["id"] for c in cs]
    app = {"connection": "qa-main", "env": "qa"}
    size = TABLES // COMPONENTS
    for i, tid in enumerate(tables):  # a foreign-key ring per component (cycles), components never touch
        base = (i // size) * size
        for step in (1, 7, 13):
            j = base + (i - base + step) % size
            edges.append(dbmodel.make_edge("foreign_key", tid, tables[j], column_mapping=[
                {"from": [cols[tid][1]], "to": [cols[tables[j]][0]], "transform": "", "transform_known": True}],
                applicability=app, basis="declared_metadata"))
    code = []
    rnd = random.Random(7)
    for k in range(CODE):
        n = dbmodel.code_node(repo_id, "code_symbol", "svc.handler_%03d" % k)
        n["applicability"] = {"repos": [repo_id], "connections": ["qa-main"]}
        nodes.append(n)
        code.append(n["id"])
        comp = (k % COMPONENTS) * size
        for t in rnd.sample(range(comp, comp + size), 6):
            e = dbmodel.make_edge("reads" if t % 2 else "writes", n["id"], tables[t], transport="direct_sql",
                                  applicability=app, basis="inferred")
            e["columns"] = {"read": [cols[tables[t]][0]], "written": [cols[tables[t]][1]] if t % 2 == 0 else []}
            edges.append(e)
    dbstore.upsert("nodes", nodes)
    dbstore.upsert("edges", edges)
    return tables, code, len(edges)


def investigations(ctx, flow):
    from chitragupta import dbmodel, dbstore
    ids = []
    for i in range(INVESTIGATIONS):
        obs = [{"step": "s%d" % j, "object_label": "shop.t%04d" % (i * 5 + j), "status": "observed",
                "status_label": "Observed", "observed_at": "2026-11-06T10:%02d:00+00:00" % j, "values": {"id": j}}
               for j in range(3)]
        rec = {"id": dbmodel.new_id("i", "scale", i), "kind": "investigation", "owner": ctx.principal_id,
               "created_at": "2026-11-06T10:00:00+05:30", "objective": "Scale investigation %d" % i,
               "scope": {"environment": "qa", "connections": ["qa-main"]},
               "flow": {"id": flow["id"], "title": flow["title"], "version": 1},
               "start": {"object": flow["objects"][0], "object_label": "shop.t0000", "fields": ["id"]},
               "key_fingerprint": "k%d" % i, "plans": [], "unknowns": [], "next_action": "Check the consumer log.",
               "runs": [{"n": 1, "job": dbmodel.new_id("j", "scale", i), "state": "completed",
                         "state_label": "Completed", "ended_at": "2026-11-06T10:05:00+05:30",
                         "observations": obs, "unknowns": [], "next_checks": [], "connections": ["qa-main"],
                         "window": {"from": "", "to": "", "note": ""}, "limits": {}}],
               "notes": {"text": "", "revision": 0, "at": ""}, "corrections": [], "superseded_by": None,
               "ticket": None, "handoff": {"generated": [], "edited": None, "source_changed": False}, "revision": 0}
        ids.append(dbstore.save("investigations", rec)["id"])
    return ids


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))]


class Scale(Isolated, unittest.TestCase):
    """DBT-37. One generated fixture, every bound checked on it."""

    def size(self, x):
        return len(json.dumps(x, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))

    def test_large_fixture_stays_bounded(self):
        from chitragupta import (dbaccess, dbconnector, dbflows, dbimpact, dbstore, dbtrace, dbviews, mcpjobs,
                                 mcpserver)
        from chitragupta.safety import commands
        self.isolate()
        sc = Scenario(self.home).build()
        cfg = sc.cfg()
        ctx = dbaccess.AccessContext(cfg)
        t0 = time.perf_counter()
        tables, code, n_edges = generate(ctx, sc.repo_id())
        flow = dbflows.build_flow(ctx, {"kind": "node", "id": code[0]}, save=True, cfg=cfg)
        inv_ids = investigations(ctx, flow)
        gen_s = time.perf_counter() - t0
        g = dbstore.load_graph()
        objs = [n for n in g["nodes"].values() if n["id"].startswith("o_")]
        self.assertGreaterEqual(len(objs), 1000)
        self.assertGreaterEqual(n_edges, 5000)
        self.assertGreaterEqual(len(dbstore.list_ids("investigations", limit=1000)), 100)
        calls = []
        tripped = [(commands, "run"), (commands, "spawn"), (dbconnector.McpJobTransport, "execute"), (mcpjobs, "run")]
        patches = [mock.patch.object(o, n, lambda *a, _n=n, **k: calls.append(_n)) for o, n in tripped]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        ctx = dbaccess.AccessContext(cfg)
        # retrieval and expansion: <= 100 nodes per view, pages of <= 50, a continuation
        ex = dbflows.expand(ctx, [tables[0]])
        self.assertLessEqual(ex["visited"], 100)
        self.assertLessEqual(len(ex["nodes"]), 50)
        self.assertTrue(ex["more_available"] and ex["cursor"])
        ex2 = dbflows.expand(ctx, [tables[0]], cursor=ex["cursor"])
        self.assertTrue(set(n["id"] for n in ex2["nodes"]).isdisjoint(n["id"] for n in ex["nodes"]))
        # cached detail reads, timed
        rnd = random.Random(11)
        sample = rnd.sample(tables, 40)
        times, sizes = [], []
        for oid in sample:
            a = time.perf_counter()
            v = dbviews.table_view(ctx, oid)
            times.append((time.perf_counter() - a) * 1000)
            sizes.append(self.size(v))
        fl, invs, iv = [], [], []
        for _ in range(10):
            a = time.perf_counter()
            fv = dbviews.flow_view(ctx, fid=flow["id"])
            fl.append((time.perf_counter() - a) * 1000)
            a = time.perf_counter()
            il = dbviews.investigations_list(ctx)
            invs.append((time.perf_counter() - a) * 1000)
            a = time.perf_counter()
            one = dbviews.investigation_view(ctx, inv_ids[0])
            iv.append((time.perf_counter() - a) * 1000)
        for env in (fv, il, one):
            self.assertLessEqual(self.size(env), dbviews.MAX_BYTES)
            self.assertLessEqual(len(env["items"]), 50)
        self.assertTrue(il["more_available"] and il["cursor"])
        self.assertEqual(len(il["items"]), 50)
        ctxt = dbtrace.database_context(ctx, flow["id"], inv_ids[0], cfg=cfg)
        self.assertLessEqual(self.size(ctxt), 8 * 1024)
        self.assertIsNotNone(ctxt["database_context"])
        p50, p95 = pct(times, 50), pct(times, 95)
        self.assertLess(p50, 500, times)  # DB-25 target for a cached detail read
        self.assertLessEqual(max(sizes), dbviews.MAX_BYTES)
        # the impact of a hub table: bounded, deterministic, paged
        r1 = dbimpact.impact(ctx, tables[0], cfg=cfg)
        r2 = dbimpact.impact(ctx, tables[0], save=False, cfg=cfg)
        self.assertLessEqual(r1["summary"]["items"], 100)
        self.assertEqual([i["id"] for i in r1["items"]], [i["id"] for i in r2["items"]])
        a = time.perf_counter()
        imv = dbviews.impact_view(ctx, target=tables[0])
        imp_ms = (time.perf_counter() - a) * 1000
        self.assertLessEqual(len(imv["items"]), 50)
        self.assertLessEqual(self.size(imv), dbviews.MAX_BYTES)
        mcp = mcpserver.call_tool("get_db_impact", {"id": tables[0]})
        self.assertFalse(mcp["isError"], mcp)
        self.assertEqual(calls, [])  # no transport, model or process call during any read
        self.report = {"objects": len(objs), "relationships": n_edges, "investigations": len(inv_ids),
                       "generate_s": round(gen_s, 2), "passport_p50_ms": round(p50, 1), "passport_p95_ms": round(p95, 1),
                       "passport_max_bytes": max(sizes), "flow_view_p50_ms": round(pct(fl, 50), 1),
                       "flow_view_bytes": self.size(fv), "investigations_list_p50_ms": round(pct(invs, 50), 1),
                       "investigation_view_p95_ms": round(pct(iv, 95), 1), "context_bytes": self.size(ctxt),
                       "impact_items": r1["summary"]["items"], "impact_view_ms": round(imp_ms, 1),
                       "impact_view_bytes": self.size(imv), "expand_visited": ex["visited"]}
        self.budget_and_duplicates(ctx, cfg, flow, tables, calls)
        if os.environ.get("CG_SCALE_REPORT"):
            sys.stderr.write("\nDBT-37 " + json.dumps(self.report) + "\n")

    def budget_and_duplicates(self, ctx, cfg, flow, tables, calls):
        from chitragupta import dbflows, dbimpact, dbmodel, dbstore
        # a repeated metadata-refresh request queues one job
        first = dbflows.queue_refresh(ctx, [tables[0], tables[1]], cfg=cfg)
        again = dbflows.queue_refresh(ctx, [tables[1], tables[0]], cfg=cfg)
        self.assertTrue(again.get("already_queued"))
        self.assertEqual(len(dbstore.read_file("refresh")["requests"]), 1, first)
        # budget exhaustion: a bounded, resumable, deterministic reconciliation
        rec = dbstore.load("flows", flow["id"])
        for i in range(59):
            fid = dbmodel.new_id("f", "scale", i)
            dbstore.save("flows", dict(rec, id=fid, revision=0, current=dict(rec["current"], id=fid,
                                                                             deps={"card:gone/x": "old"})))
        passes = []
        for _ in range(4):
            a = time.perf_counter()
            s = dbimpact.reconcile(ctx, cfg, budget=25)
            passes.append((s["flows_checked"], s["flows_marked"], s["more_available"],
                           round((time.perf_counter() - a) * 1000)))
        self.assertEqual([(p[0], p[2]) for p in passes], [(25, True), (25, True), (10, False), (25, True)])
        self.assertEqual(sum(p[1] for p in passes[:3]), 59)  # every changed flow marked once, the rest resumed
        self.assertEqual(passes[3][1], 0)  # the next round starts over and finds nothing new to mark
        marked = [f for f in dbstore.iter_records("flows", limit=100) if (f.get("review") or {}).get("state")]
        self.assertEqual(len(marked), 59)
        self.assertEqual(calls, [])
        self.report["reconcile_pass_ms"] = [p[3] for p in passes]


if __name__ == "__main__":
    unittest.main()
