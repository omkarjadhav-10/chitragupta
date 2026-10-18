"""The persistent search index (chitragupta/searchidx.py): every unit type, FTS5 and the scan agree on the top
result, filters, the exact-key rule, incremental updates by content hash, rebuilds (schema change, a damaged file),
readers that never write, the render hook and `cli search`.

Synthetic home of test_dashboard / test_dashboard_v2 plus one ticket with a branch and a PR (repo `shop-web`).
"""
import contextlib
import io
import json
import os
import time
import unittest

from tests.test_dashboard import TICKET, _w
from tests.test_dashboard_v2 import V2Base

QUERIES = [  # (query, (type, ref)) — the overall best match, the same for both engines
    ("abc-1005", ("ticket", "ABC-1005")),
    ("create order", ("request", TICKET + "/step-2")),
    ("flaky cart", ("branch", "ABC-1005/shop-web/bugfix/ABC-1005-flaky-cart")),
    ("gateway timeout", ("pr", "ABC-1005/shop-web/pr-17")),
    ("orderquery", ("dblink", "shop.orders.customer_id -> customers.id")),
    ("payment_ledger", ("table", "shop.payment_ledger")),
    ("applied twice", ("finding", TICKET + "/i1")),
    ("/refunds/batch", ("api", "POST api.acme.test /v2/refunds/batch")),
    ("gateway sandbox", ("integration", "pay")),
    ("orders where", ("query", TICKET + "/q1")),
]


class IdxBase(V2Base):
    def setUp(self):
        super().setUp()
        _w(os.path.join(self.home, "data", "tickets", "ABC-1005.json"), {
            "id": "ABC-1005", "title": "Badge count", "last": self.days["yday"], "repos": ["C:/src/shop-web"],
            "git": {"repos": {"shop-web": {
                "branches": [{"branch": "bugfix/ABC-1005-flaky-cart", "parent": "main", "status": "open",
                              "last_commit": self.days["yday"] + "T10:00:00+00:00",
                              "commits": [{"sha": "b1", "subject": "Stabilise the badge"}]}],
                "prs": [{"number": 17, "title": "Raise the gateway timeout", "source": "bugfix/ABC-1005-flaky-cart",
                         "target": "main", "state": "active", "created_at": self.days["yday"] + "T11:00:00+00:00"}]}}}})
        from chitragupta import searchidx
        self.ix = searchidx

    def db(self):
        return os.path.join(self.home, ".worklog", "cache", "search.sqlite")

    def top(self, q, engine, **kw):
        res = self.ix.search(q, limit=1, engine=engine, **kw)
        self.assertEqual(res["engine"], engine)
        hits = [r for g in res["groups"] for r in g["results"]]
        return hits[0] if hits else None

    def engines(self):
        return ("fts5", "scan") if self.ix.fts5_available() else ("scan",)

    def all_results(self, q, **kw):
        return [r for g in self.ix.search(q, limit=200, **kw)["groups"] for r in g["results"]]


class TestUnitsAndRanking(IdxBase):
    def test_every_type_is_indexed(self):
        self.ix.update()
        con = self.ix.paths.sqlite_connect(self.db(), readonly=True)
        try:
            types = {r[0] for r in con.execute("SELECT DISTINCT type FROM units")}
            row = con.execute("SELECT ticket, repo, href, path FROM units WHERE type = 'pr'").fetchone()
            calls = con.execute("SELECT COUNT(*) FROM units WHERE type = 'request' AND ref LIKE '%/call-%'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(types, set(self.ix.TYPE_ORDER))
        self.assertEqual(row, ("ABC-1005", "shop-web", "#/tickets/ABC-1005?tab=branches", "data/tickets/ABC-1005.json"))
        self.assertEqual(calls, 1)  # the captured API call, next to the two saved steps

    def test_fts_and_scan_agree(self):
        self.ix.update()
        for q, (typ, ref) in QUERIES:
            for engine in self.engines():
                top = self.top(q, engine)
                self.assertIsNotNone(top, (q, engine))
                self.assertEqual((top["type"], top["ref"]), (typ, ref), (q, engine))

    def test_result_fields_and_paths(self):
        for engine in self.engines():
            r = self.top("create order", engine)
            self.assertEqual(r["path"], "tickets/%s/requests.md" % TICKET)
            self.assertEqual(r["href"], "#/tickets/%s?tab=requests" % TICKET)
            self.assertEqual(r["repo"], "shop-api")
            art = self.top("order trace", engine, types=["artifact"])
            self.assertEqual(art["path"], "tickets/%s/artifacts/%s_order-trace.md" % (TICKET, self.days["yday"]))
            day = self.top("zeppelin", engine)
            d = self.days["day"]
            self.assertEqual((day["href"], day["path"]), ("#/day/" + d, "daily/%s/%s/%s.md" % (d[:4], d[5:7], d)))

    def test_exact_key_first_and_filters(self):
        self.ix.update()
        for engine in self.engines():
            res = self.ix.search("ABC-1005", engine=engine)
            self.assertEqual(res["groups"][0]["results"][0]["ref"], "ABC-1005")
            self.assertEqual(self.ix.search("ABC-1005", types=["pr"], engine=engine)["groups"][0]["type"], "pr")
            got = self.all_results("order", ticket=TICKET.lower(), engine=engine)
            self.assertTrue(got)
            self.assertTrue(all(r["ticket"] == TICKET or r["type"] in ("table", "api", "dblink") for r in got), got)
            self.assertIn("table", {r["type"] for r in got})  # shop.orders lists the ticket among its tickets
            web = self.all_results("bugfix", repo="SHOP-WEB", engine=engine)
            self.assertEqual({r["type"] for r in web}, {"branch", "pr"})
            self.assertEqual(self.all_results("bugfix", repo="shop-api", engine=engine), [])
            y = self.days["yday"]
            dated = self.all_results("order", frm=y, to=y, engine=engine)
            self.assertTrue(dated)
            self.assertTrue(all(r["date"] == y for r in dated), dated)
            self.assertEqual(self.all_results("order", frm="2001-01-01", to="2001-01-31", engine=engine), [])
            for bad in ({"frm": "2026-02-30"}, {"to": "yesterday"}):
                with self.assertRaises(ValueError):
                    self.ix.search("order", engine=engine, **bad)


class TestUpdates(IdxBase):
    def test_incremental_by_content_hash(self):
        first = self.ix.update()
        self.assertTrue(first["rebuilt"])
        self.assertEqual(first["changed"], first["sources"])
        again = self.ix.update()
        self.assertEqual((again["changed"], again["removed"], again["rebuilt"]), (0, 0, False))
        p = os.path.join(self.home, "data", "tickets", "ABC-1002.json")
        with open(p, encoding="utf-8") as f:
            tk = json.load(f)
        tk["tldr"] = "The quokka proxy drops idle connections."
        _w(p, tk)
        t0 = time.perf_counter()
        res = self.ix.update()
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual((res["changed"], res["removed"]), (1, 0))
        self.assertEqual(self.top("quokka", self.ix.default_engine())["ref"], "ABC-1002")
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # touched, same content: no re-index
        self.assertEqual(self.ix.update()["changed"], 0)
        os.remove(os.path.join(self.home, "data", "tickets", "ABC-1003.json"))
        res = self.ix.update()
        self.assertEqual((res["changed"], res["removed"]), (0, 1))
        self.assertFalse([r for r in self.all_results("refund report") if r["ticket"] == "ABC-1003"])

    def test_schema_change_and_damaged_file_rebuild(self):
        self.ix.update()
        saved = self.ix.SCHEMA
        self.addCleanup(setattr, self.ix, "SCHEMA", saved)
        self.assertEqual(self.ix.search("zeppelin")["total"], 1)  # a view open on the file
        self.ix.SCHEMA = saved + 1
        self.assertTrue(self.ix.update()["rebuilt"])
        self.assertEqual(self.ix.search("zeppelin")["total"], 1)  # reopened after the rebuild
        self.assertFalse(self.ix.update()["rebuilt"])
        self.ix.reset()
        with open(self.db(), "wb") as f:
            f.write(b"this is not a database" * 100)
        res = self.ix.update()
        self.assertTrue(res["rebuilt"])
        self.assertGreater(res["units"], 10)
        r = self.ix.rebuild()
        self.assertTrue(r["rebuilt"])
        self.assertEqual(r["units"], res["units"])

    def test_readers_never_write(self):
        st = self.ix.search("zeppelin")
        self.assertEqual(st["total"], 1)
        self.assertFalse(os.path.exists(self.db()), "a search created the index file")
        self.assertEqual(self.ix.stats()["view"], "memory")
        self.ix.update()
        self.assertEqual(self.ix.search("zeppelin")["total"], 1)
        self.assertEqual(self.ix.stats()["view"], "disk")
        before = os.stat(self.db()).st_mtime_ns, os.path.getsize(self.db())
        _w(os.path.join(self.home, "data", "tickets", "ABC-1006.json"), {"id": "ABC-1006", "title": "Wombat import"})
        self.ix.invalidate()
        self.assertEqual(self.top("wombat", self.ix.default_engine())["ref"], "ABC-1006")  # behind: a copy in memory
        self.assertEqual(self.ix.stats()["view"], "memory")
        self.assertEqual((os.stat(self.db()).st_mtime_ns, os.path.getsize(self.db())), before)
        s = self.ix.stats()
        self.assertEqual(s["file"], ".worklog/cache/search.sqlite")
        self.assertGreater(s["docs"], 10)  # what the view in memory holds
        self.assertIsNotNone(s["built_at"])

    def test_render_updates_the_index_and_fails_open(self):
        from chitragupta import render
        from chitragupta.store import Store
        render.render_all(Store(), {"ticket_prefixes": ["ABC"]}, {}, days=set(), tickets=set())
        self.assertTrue(os.path.isfile(self.db()))
        saved = self.ix.update
        self.addCleanup(setattr, self.ix, "update", saved)

        def boom(full=False):
            raise RuntimeError("index broke")
        self.ix.update = boom
        render.update_search_index()  # logged, not raised
        with open(os.path.join(self.home, ".worklog", "logs", "search.log"), encoding="utf-8") as f:
            self.assertIn("index broke", f.read())


class TestCliSearch(IdxBase):
    def cli(self, *argv):
        from chitragupta import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["search"] + list(argv))
        return rc, buf.getvalue()

    def test_prints_results_with_files(self):
        rc, out = self.cli("create", "order")
        self.assertEqual(rc, 0)
        self.assertIn("## Requests", out)
        self.assertIn("file: " + os.path.join(self.home, "tickets", TICKET, "requests.md"), out)
        self.assertFalse(os.path.exists(self.db()), "a plain search only reads")
        rc, out = self.cli("--rebuild")
        self.assertEqual(rc, 0)
        self.assertIn("Search index rebuilt", out)
        self.assertTrue(os.path.isfile(self.db()))
        rc, out = self.cli("flaky", "--type", "branch,pr", "--repo", "shop-web", "--json")
        self.assertEqual({g["type"] for g in json.loads(out)["groups"]}, {"branch", "pr"})
        self.assertEqual(self.cli("order", "--type", "nope")[0], 2)
        self.assertEqual(self.cli("order", "--from", "2026-13-01")[0], 2)
        self.assertIn("No results", self.cli("xyzzyplugh")[1])


if __name__ == "__main__":
    unittest.main()
