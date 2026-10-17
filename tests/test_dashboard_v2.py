"""Dashboard 2.0, read side: the day view, the year calendar, the knowledge base, search (FTS5 and the pure-Python
scan agree), the setup checklist, live MCP status, the static-file allowlist and the Server-Sent Events stream.

Everything runs against temp CHITRAGUPTA_HOME / CHITRAGUPTA_USER_DIR folders and ephemeral ports, on the synthetic
home of test_dashboard plus a synthetic catalog (schema `shop`, hosts *.acme.test).
"""
import datetime as _dt
import http.client
import json
import os
import socket
import tempfile
import time
import unittest

from tests import support
from tests.test_dashboard import TICKET, Base, _w, snapshot


LONG_EVIDENCE = "OrderItemRepository joins on order_id " * 40


def add_v2_fixtures(home, days):
    """Catalog, QA schema cards, link map, daily files, MCP status cache, branch alerts and commit details."""
    day, yday = days["day"], days["yday"]
    _w(os.path.join(home, "data", "catalog.json"), {
        "tables": {
            "shop.orders": {"name": "shop.orders", "db": "shop-db", "purpose": "One row per order",
                            "key_columns": ["id", "customer_id"], "relations": ["shop.order_items.order_id -> shop.orders.id"],
                            "tickets": [TICKET], "last_seen": day + "T10:00:00+00:00"},
            "shop.payment_ledger": {"name": "shop.payment_ledger", "db": "shop-db", "purpose": "Ledger of captured payments",
                                    "key_columns": ["id"], "relations": [], "tickets": ["ABC-1003"], "last_seen": yday},
            "customers": {"name": "customers", "db": "shop-db", "purpose": "Customers", "key_columns": ["id"],
                          "relations": [], "tickets": [], "last_seen": yday},
        },
        "db_relations": {
            "r1": {"from": "order_items.order_id", "to": "orders.id", "kind": "fk", "evidence": LONG_EVIDENCE, "tickets": [TICKET]},
            "r2": {"from": "shop.orders.customer_id", "to": "customers.id", "kind": "logical", "evidence": "join in OrderQuery",
                   "tickets": [TICKET]},
        },
        "db_writers": {
            "w1": {"table": "shop.orders",
                   "reads": {"OrderService.total": {"component": "OrderService.total", "tickets": [TICKET]}},
                   "writes": {"OrderService.create|status": {"component": "OrderService.create", "column": "status",
                                                             "change": "NEW on insert", "tickets": [TICKET]}}},
        },
        "apis": {
            "POST api.acme.test /v2/refunds/batch": {
                "method": "post", "path": "/v2/refunds/batch", "host_or_service": "api.acme.test", "direction": "outbound",
                "auth_type": "Bearer token", "purpose": "Creates refunds in bulk", "request_fields": ["orderIds"],
                "response_fields": ["batchId"], "tickets": ["ABC-1003"], "last_seen": yday + "T09:00:00+00:00"},
            "GET api.acme.test /orders/{id}": {
                "method": "GET", "path": "/orders/{id}", "host_or_service": "api.acme.test", "direction": "inbound",
                "auth_type": "Bearer token", "purpose": "Reads one order", "request_fields": [], "response_fields": ["total"],
                "tickets": [TICKET], "last_seen": day},
        },
        "integrations": {
            "pay": {"system": "Payment gateway sandbox", "artifact": "gateway client", "notes": "Settles nightly",
                    "tickets": [], "last_seen": yday},
        },
    })
    _w(os.path.join(home, "data", "db-schema", "qa-db", "shop.orders.json"), {
        "type": "postgres", "env": "qa", "connection": "qa-db", "table": "shop.orders", "fetched_at": yday,
        "card": {"found": True, "table": "shop.orders", "triggers": [], "writers": []},
        "describe": [{"name": "id", "dataType": "bigint", "isPrimaryKey": True},
                     {"name": "customer_id", "dataType": "bigint", "isForeignKey": True},
                     {"name": "status", "dataType": "text"}, {"name": "coupon_basis", "dataType": "numeric"}]})
    _w(os.path.join(home, "data", "db-schema", "_linkmap.json"), {
        "env": "qa", "fetched_at": yday + "T08:00:00+00:00", "result": {
            "hierarchy": [{"database": "qa-db", "schema": "shop", "tables": ["customers", "order_items", "orders"]}],
            "links": [
                {"from": "shop.order_items.order_id", "to": "shop.orders.id", "connection": "qa-db", "evidence": "fk",
                 "sampled": 20, "matched": 20, "checked": True},
                {"from": "shop.orders.customer_id", "to": "shop.customers.id", "connection": "qa-db", "evidence": "join",
                 "sampled": 20, "matched": 2, "checked": True},
            ]}})
    _w(os.path.join(home, "daily", day[:4], day[5:7], day + ".md"),
       "# %s\n\nPaired on the zeppelin migration plan for the checkout service.\n" % day)
    _w(os.path.join(home, "daily", yday[:4], yday[5:7], yday + ".md"), "# %s\n\nTraced the order insert.\n" % yday)
    _w(os.path.join(home, ".worklog", "cache", "mcp-status.json"), {
        "checked": day + "T09:00:00+00:00", "probed": True, "jobs_configured": True,
        "slack": {"status": "missing", "server": None, "detail": "no background Slack server", "state": "not_connected"},
        "jira": {"status": "connected", "server": "jira-test", "detail": "server 'jira-test' (user scope)", "enabled": True},
        "database": {"status": "signed out", "server": "db-test", "detail": "server 'db-test' (user scope)", "enabled": True}})
    p = os.path.join(home, "data", "tickets", "ABC-1002.json")
    with open(p, encoding="utf-8") as f:
        tk = json.load(f)
    tk["git"] = {"repos": {"shop-api": {"branches": [{"branch": "feature/ABC-1002-timeout"}]}},
                 "alerts": [{"kind": "unpushed", "text": "2 commits are not pushed", "repo": "shop-api", "commits": ["abc1234"]}]}
    _w(p, tk)
    p = os.path.join(home, "data", "days", day[:4], day + ".json")
    with open(p, encoding="utf-8") as f:
        dd = json.load(f)
    dd["commits"][0].update(date=day + "T11:05:00", repo_name="shop-api")
    dd["sessions"] = {"s1": {}, "s2": {}}
    _w(p, dd)


class V2Base(Base):
    def setUp(self):
        super().setUp()
        add_v2_fixtures(self.home, self.days)
        from chitragupta.dashboard import api, search
        api._setup_cache.update(at=0.0, home=None, value=None)
        search.reset()

    def tearDown(self):
        from chitragupta.dashboard import search
        search.reset()
        super().tearDown()


# --------------------------------------------------------------------------- day, calendar, tickets

class TestDayView(V2Base):
    def test_today_has_the_new_keys(self):
        from chitragupta.dashboard import api
        d = api.today()
        for k in ("lanes", "commit_list", "sessions", "last_activity", "target_minutes", "day_kind", "worklog_preview"):
            self.assertIn(k, d)
        self.assertEqual(d["sessions"], 2)
        self.assertEqual(d["commit_list"], [{"time": "11:05", "ticket": TICKET, "repo": "shop-api", "short": "aaaaaaa",
                                             "subject": "Fix order total"}])
        lane = d["lanes"][0]
        self.assertEqual(lane["ticket"], TICKET)
        self.assertEqual(lane["title"], "Order total is wrong for discounted items")
        self.assertGreater(lane["minutes"], 0)
        for s, e in lane["spans"]:
            self.assertRegex(s, r"^\d\d:\d\d$")
            self.assertRegex(e, r"^\d\d:\d\d$")
        self.assertRegex(d["last_activity"], r"^\d\d:\d\d$")
        self.assertIn(d["day_kind"], ("workday", "weekend", "leave", "half_day", "holiday"))
        if d["worklog_preview"]["kind"] == "message":
            self.assertEqual(d["target_minutes"], d["worklog_preview"]["target"])

    def test_past_day(self):
        from chitragupta.dashboard import api
        d = api.day(self.days["yday"])
        self.assertEqual(d["day"], self.days["yday"])
        self.assertEqual([t["key"] for t in d["tickets"]], ["ABC-1003", TICKET])
        self.assertEqual(d["worklog_saved"]["status"], "sent")
        self.assertEqual(d["commit_list"], [])
        self.assertEqual(d["sessions"], 0)
        self.assertIsInstance(d["lanes"], list)
        self.assertNotEqual(d["worklog_preview"]["kind"], "error", d["worklog_preview"])
        with self.assertRaises(ValueError):
            api.day("2026-02-30")
        empty = api.day("2001-01-01", preview=False)
        self.assertEqual((empty["tickets"], empty["lanes"], empty["last_activity"]), ([], [], None))

    def test_ticket_summary_alerts_branches_and_readme(self):
        from chitragupta.dashboard import api
        rows = {t["key"]: t for t in api.tickets()["tickets"]}
        self.assertEqual(rows["ABC-1002"]["alerts"], [{"kind": "unpushed", "text": "2 commits are not pushed", "repo": "shop-api"}])
        self.assertEqual(rows["ABC-1002"]["branches"], ["feature/ABC-1002-timeout"])
        self.assertEqual(rows[TICKET]["alerts"], [])
        self.assertEqual(rows[TICKET]["branches"], ["feature/ABC-1001-order-total"])
        self.assertEqual(api.ticket(TICKET)["readme"], "# ABC-1001\n")
        self.assertEqual(api.ticket("ABC-1002")["readme"], "")

    def test_calendar_kinds_and_targets(self):
        from chitragupta.dashboard import api
        today = _dt.date.fromisoformat(self.days["day"])
        sat = today - _dt.timedelta(days=70)
        while sat.weekday() != 5:
            sat += _dt.timedelta(days=1)
        d = [(sat + _dt.timedelta(days=i)).isoformat() for i in range(6)]  # Sat .. Thu
        _w(os.path.join(self.home, "data", "calendar.json"), {
            "workday": {d[0]: "Release weekend"}, "holiday": {d[2]: "Founders day"}, "half_day": {d[3]: True},
            "leave": {d[4]: "Family"}, "time": {d[5]: [{"ticket": "ABC-1002", "start": "18:00", "minutes": 30, "text": "call"}]}})
        _w(os.path.join(self.home, "data", "days", d[5][:4], d[5] + ".json"),
           {"date": d[5], "tickets": {TICKET: {"minutes": 100}}, "other": {"shop-api": {"minutes": 5}}})
        c = api.calendar(d[0], d[5])
        self.assertEqual((c["from"], c["to"], c["hours_per_day"]), (d[0], d[5], 8))
        self.assertEqual(c["workdays"], ["Mon", "Tue", "Wed", "Thu", "Fri"])
        got = [(x["kind"], x["note"], x["target"]) for x in c["days"]]
        self.assertEqual(got, [("extra_workday", "Release weekend", 480), ("weekend", "", 0), ("holiday", "Founders day", 0),
                               ("half_day", "", 240), ("leave", "Family", 0), ("workday", "", 480)])
        self.assertEqual(c["days"][5]["minutes"], 135)
        self.assertEqual(c["days"][5]["tickets"], {TICKET: 100, "ABC-1002": 30})
        default = api.calendar()
        self.assertEqual(len(default["days"]), 365)
        self.assertEqual(default["to"], self.days["day"])
        self.assertEqual(len(api.calendar("2000-01-01", self.days["day"])["days"]), 400)
        for frm, to in (("x", None), ("2026-02-30", None), (self.days["day"], self.days["yday"])):
            with self.assertRaises(ValueError):
                api.calendar(frm, to)


# --------------------------------------------------------------------------- knowledge, status, setup

class TestKnowledgeStatusSetup(V2Base):
    def test_knowledge(self):
        from chitragupta.dashboard import api
        k = api.knowledge()
        self.assertEqual(set(k), {"tables", "relations", "writers", "apis", "integrations", "linkmap"})
        t = {x["name"]: x for x in k["tables"]}
        self.assertEqual(set(t["shop.orders"]), {"name", "db", "purpose", "key_columns", "columns", "relations", "tickets",
                                                 "last_seen"})
        self.assertEqual(t["shop.orders"]["columns"], ["id", "customer_id", "status", "coupon_basis"])
        self.assertEqual(t["customers"]["columns"], [])
        rel = {(r["from"], r["to"]): r for r in k["relations"]}
        self.assertTrue(rel[("order_items.order_id", "orders.id")]["verified"])  # 20/20 sampled on QA
        self.assertFalse(rel[("shop.orders.customer_id", "customers.id")]["verified"])  # 2/20: not confirmed
        self.assertLessEqual(len(rel[("order_items.order_id", "orders.id")]["evidence"]), api.EVIDENCE_MAX)
        self.assertEqual(k["writers"][0]["writes"][0], {"component": "OrderService.create", "column": "status",
                                                        "change": "NEW on insert", "tickets": [TICKET]})
        a = {x["path"]: x for x in k["apis"]}["/v2/refunds/batch"]
        self.assertEqual((a["method"], a["host"], a["direction"]), ("POST", "api.acme.test", "outbound"))
        self.assertEqual(set(a), {"method", "path", "host", "direction", "auth_type", "purpose", "request_fields",
                                  "response_fields", "tickets", "last_seen"})
        self.assertEqual(k["integrations"][0]["system"], "Payment gateway sandbox")
        self.assertEqual(k["linkmap"], {"env": "qa", "fetched_at": self.days["yday"] + "T08:00:00+00:00", "schemas": 1,
                                        "tables": 3, "links": 2, "verified": 1})
        os.remove(os.path.join(self.home, "data", "db-schema", "_linkmap.json"))
        k = api.knowledge()
        self.assertIsNone(k["linkmap"])
        self.assertFalse(any(r["verified"] for r in k["relations"]))

    def test_status_mcp_live_and_search_stats(self):
        from chitragupta.dashboard import api
        s = api.status()
        live = s["mcp"]["live"]
        self.assertEqual(live["checked_at"], self.days["day"] + "T09:00:00+00:00")
        self.assertTrue(live["probed"])
        self.assertEqual([(x["role"], x["server"], x["status"]) for x in live["servers"]],
                         [("slack", "", "missing"), ("jira", "jira-test", "connected"), ("database", "db-test", "signed out")])
        self.assertNotIn("status", s["mcp"])
        self.assertEqual(set(s["search"]), {"engine", "docs", "built_at", "build_ms"})
        os.remove(os.path.join(self.home, ".worklog", "cache", "mcp-status.json"))
        self.assertIsNone(api.status()["mcp"]["live"])

    def test_setup_checklist(self):
        from chitragupta import install, setup
        from chitragupta.dashboard import api
        calls = []

        def fake_run(args, cwd=None, timeout=60):
            calls.append(list(args))
            return 0, "%s version 1.0" % os.path.basename(str(args[0])), ""
        saved = (setup.RUNNER, setup._claude_exe, install.RUNNER)
        self.addCleanup(lambda: (setattr(setup, "RUNNER", saved[0]), setattr(setup, "_claude_exe", saved[1]),
                                 setattr(install, "RUNNER", saved[2])))
        setup.RUNNER = fake_run
        setup._claude_exe = lambda cfg=None: ["claude"]
        install.RUNNER = None  # CHITRAGUPTA_NO_TASK (support.isolate) keeps schtasks from running
        before = snapshot(self.home)
        r = api.setup()
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(set(r), {"checked_at", "items"})
        ids = [i["id"] for i in r["items"]]
        for want in ("python", "git", "claude", "home", "config", "hooks", "tick", "runtime", "task", "skill", "claude-md",
                     "slack", "jira", "database", "errors"):
            self.assertIn(want, ids)
        for i in r["items"]:
            self.assertEqual(set(i), {"id", "label", "status", "detail", "fix"})
            self.assertIn(i["status"], ("ok", "warn", "bad", "off"))
            self.assertIn(i["fix"]["kind"], ("slash", "cli", "claude", "none"))
            if i["status"] == "ok":
                self.assertEqual(i["fix"], {"kind": "none", "text": ""})
        by = {i["id"]: i for i in r["items"]}
        self.assertEqual(by["git"]["status"], "ok")
        self.assertEqual(by["jira"]["status"], "ok")
        self.assertEqual(by["slack"]["status"], "off")
        self.assertEqual(by["database"]["status"], "warn")
        self.assertEqual(by["database"]["fix"]["kind"], "claude")
        self.assertIn("db-test", by["database"]["fix"]["text"])
        self.assertEqual(by["push"]["status"], "off")  # no GitHub remote in the synthetic config
        self.assertEqual(by["runtime"]["fix"], {"kind": "slash", "text": "/cg:setup"})
        self.assertTrue(calls, "doctor ran through the stub")
        self.assertFalse([c for c in calls if "mcp" in c], "never `claude mcp list`")
        n = len(calls)
        self.assertIs(api.setup(), r)  # cached for 60 s
        self.assertEqual(len(calls), n)


# --------------------------------------------------------------------------- search

QUERIES = [
    ("abc-1001", ("ticket", TICKET)),                 # an exact ticket key
    ("checko", ("ticket", "ABC-1002")),               # a prefix
    ("payment_ledger", ("table", "shop.payment_ledger")),  # a table name
    ("applied twice", ("finding", TICKET + "/i1")),   # a finding phrase
    ("/refunds/batch", ("api", "POST api.acme.test /v2/refunds/batch")),  # an API path fragment
    ("zeppelin", ("day", None)),                      # a word in a daily file
    ("orders where", ("query", TICKET + "/q1")),      # query text
    ("gateway sandbox", ("integration", "pay")),
    ("coupon_bas", ("table", "shop.orders")),         # a column from the QA schema card
]


class TestSearch(V2Base):
    def ranked_first(self, q, engine):
        """The overall best match (search groups by type, so ask for one result over all groups)."""
        from chitragupta.dashboard import search
        res = search.search(q, limit=1, engine=engine)
        hits = [r for g in res["groups"] for r in g["results"]]
        self.assertEqual(res["engine"], engine)
        return hits[0] if hits else None

    def test_engines_agree(self):
        from chitragupta.dashboard import search
        if not search.fts5_available():
            self.skipTest("this SQLite has no FTS5")
        for q, (typ, ref) in QUERIES:
            ref = ref or self.days["day"]
            for engine in ("fts5", "scan"):
                top = self.ranked_first(q, engine)
                self.assertIsNotNone(top, (q, engine))
                self.assertEqual((top["type"], top["ref"]), (typ, ref), (q, engine))

    def test_result_shape_snippets_and_groups(self):
        from chitragupta.dashboard import search
        for engine in ("fts5", "scan") if search.fts5_available() else ("scan",):
            res = search.search("order", engine=engine)
            self.assertEqual(set(res), {"q", "engine", "took_ms", "total", "groups"})
            order = [g["type"] for g in res["groups"]]
            self.assertEqual(order, [t for t in search.TYPE_ORDER if t in order])
            self.assertGreater(res["total"], 1)
            for g in res["groups"]:
                self.assertEqual(set(g), {"type", "label", "count", "results"})
                self.assertLessEqual(len(g["results"]), search.PER_GROUP)
                for r in g["results"]:
                    self.assertEqual(set(r), {"type", "ref", "title", "ticket", "date", "href", "snippet", "hits"})
                    self.assertLessEqual(len(r["snippet"]), search.SNIPPET + 2)
                    self.assertNotIn("<", r["snippet"].replace("<br", ""))
                    for start, n in r["hits"]:
                        self.assertEqual(r["snippet"][start:start + n].lower()[:5], "order"[:n][:5])
            hrefs = {r["type"]: r["href"] for g in res["groups"] for r in g["results"]}
            self.assertTrue(hrefs["ticket"].startswith("#/tickets/ABC-"))
            self.assertEqual(hrefs["query"], "#/tickets/%s?tab=queries" % TICKET)
            res = search.search("refunds", types=["api"], engine=engine)
            self.assertEqual([g["type"] for g in res["groups"]], ["api"])
            self.assertEqual(res["groups"][0]["results"][0]["href"], "#/knowledge?tab=apis&q=%2Fv2%2Frefunds%2Fbatch")
            self.assertEqual(search.search("order", limit=2, engine=engine)["groups"][0]["count"] >= 1, True)
            self.assertEqual(sum(len(g["results"]) for g in search.search("order", limit=2, engine=engine)["groups"]), 2)
            few = search.search("order", limit=3, engine=engine)  # each group's best match comes before depth
            self.assertEqual(len(few["groups"]), 3)
            self.assertTrue(all(len(g["results"]) == 1 for g in few["groups"]))

    def test_hostile_queries(self):
        from chitragupta.dashboard import search
        for engine in ("fts5", "scan") if search.fts5_available() else ("scan",):
            for q in ('"a" OR b*', "NEAR(", "title:order", "*", '"', ")(", "order AND", "-", "^order", "a" * 5000,
                      "x " * 50, "\x00", "émoji 🎉 order", ""):
                res = search.search(q, engine=engine)
                self.assertIsInstance(res["total"], int, (q, engine))
            self.assertEqual(search.search("", engine=engine)["total"], 0)

    def test_parse_and_snippet(self):
        from chitragupta.dashboard import search
        self.assertEqual(search.parse_query("ABC-1001 Ordér"), ["abc", "1001", "order"])
        self.assertEqual(len(search.parse_query("a b c d e f g h i j")), 8)
        self.assertEqual(search.fts_expr(["abc", "10"]), '"abc" "10"*')
        text, hits = search.snippet("t", "x " * 200 + "the Order total is wrong", ["order", "tot"])
        self.assertTrue(text.startswith("\u2026"))
        self.assertEqual([text[s:s + n] for s, n in hits], ["Order", "tot"])
        text, hits = search.snippet("Refund report", "", ["refun"])
        self.assertEqual((text, hits), ("Refund report", [[0, 5]]))
        text, hits = search.snippet("t", "\U0001F389 order", ["order"])  # offsets count UTF-16 code units
        self.assertEqual(hits, [[3, 5]])

    def test_index_rebuilds_when_data_changes(self):
        from chitragupta.dashboard import api, search
        engine = search.default_engine()
        self.assertEqual(search.search("quokka", engine=engine)["total"], 0)
        first = search.stats()
        self.assertEqual(first["engine"], engine)
        self.assertGreater(first["docs"], 10)
        _w(os.path.join(self.home, "data", "tickets", "ABC-1004.json"), {"id": "ABC-1004", "title": "Quokka export", "last": self.days["day"]})
        api._cursor_cache.update(at=0.0)
        res = search.search("quokka", engine=engine)
        self.assertEqual(res["groups"][0]["results"][0]["ref"], "ABC-1004")
        self.assertEqual(search.stats()["docs"], first["docs"] + 1)

    def test_scan_forced_by_env(self):
        from chitragupta.dashboard import search
        os.environ["CHITRAGUPTA_SEARCH_ENGINE"] = "scan"
        self.assertEqual(search.default_engine(), "scan")
        self.assertEqual(search.search("refund")["engine"], "scan")


# --------------------------------------------------------------------------- HTTP: routes, static files, SSE

class V2Http(V2Base):
    def setUp(self):
        super().setUp()
        from chitragupta.dashboard import server
        self.server_mod = server
        self.srv, self.thread = server.start_in_thread(0)
        self.port = self.srv.port
        self.socks = []

    def tearDown(self):
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def get(self, path, method="GET", host=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest(method, path, skip_host=True)
            c.putheader("Host", host if host is not None else "127.0.0.1:%d" % self.port)
            for k, v in (headers or {}).items():
                c.putheader(k, v)
            c.endheaders()
            r = c.getresponse()
            return r, r.read()
        finally:
            c.close()

    def stream(self, headers=None):
        """Open /api/events on a raw socket; returns (socket, buffered bytes so far)."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.socks.append(s)
        extra = "".join("%s: %s\r\n" % kv for kv in (headers or {}).items())
        s.sendall(("GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAccept: text/event-stream\r\n%s\r\n"
                   % (self.port, extra)).encode())
        return s

    def read_until(self, s, marker, timeout=5.0, buf=b""):
        end = time.monotonic() + timeout
        while marker not in buf and time.monotonic() < end:
            s.settimeout(max(0.05, end - time.monotonic()))
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        return buf

    def cursors(self, buf):
        return [json.loads(line[len(b"data: "):])["cursor"] for line in buf.split(b"\n") if line.startswith(b"data: ")]

    # ---------------------------------------------------------------- routes
    def test_new_routes(self):
        r, body = self.get("/api/day/" + self.days["yday"])
        self.assertEqual(r.status, 200)
        d = json.loads(body)
        self.assertEqual(d["day"], self.days["yday"])
        self.assertIn("lanes", d)
        today = json.loads(self.get("/api/today")[1])
        self.assertEqual(today["commit_list"][0]["short"], "aaaaaaa")
        for bad in ("/api/day/2026-13-01", "/api/day/2026-02-30", "/api/day/26-01-01", "/api/day/x", "/api/day/",
                    "/api/calendar?from=nope", "/api/calendar?from=2026-02-30", "/api/calendar?from=2026-10-10&to=2026-10-01"):
            self.assertEqual(self.get(bad)[0].status, 400, bad)
        c = json.loads(self.get("/api/calendar?from=%s&to=%s" % (self.days["yday"], self.days["day"]))[1])
        self.assertEqual([x["date"] for x in c["days"]], [self.days["yday"], self.days["day"]])
        k = json.loads(self.get("/api/knowledge")[1])
        self.assertEqual(k["linkmap"]["env"], "qa")
        s = json.loads(self.get("/api/search?q=refun&type=ticket,api&limit=5")[1])
        self.assertEqual([g["type"] for g in s["groups"]], ["ticket", "api"])
        self.assertEqual(json.loads(self.get("/api/search?q=")[1])["total"], 0)
        st = json.loads(self.get("/api/status")[1])
        self.assertGreater(st["search"]["docs"], 0)
        self.assertEqual(st["search"]["engine"], s["engine"])

    def test_setup_route(self):
        from chitragupta import setup
        saved = (setup.RUNNER, setup._claude_exe)
        self.addCleanup(lambda: (setattr(setup, "RUNNER", saved[0]), setattr(setup, "_claude_exe", saved[1])))
        setup.RUNNER = lambda args, cwd=None, timeout=60: (0, "version 1.0", "")
        setup._claude_exe = lambda cfg=None: ["claude"]
        r, body = self.get("/api/setup")
        self.assertEqual(r.status, 200)
        self.assertTrue(json.loads(body)["items"])

    # ---------------------------------------------------------------- static files
    def test_static_allowlist(self):
        srv = self.server_mod
        app = tempfile.mkdtemp(prefix="cg-app-")
        self.addCleanup(support.rmtree, app)
        files = {"index.html": "<!doctype html><title>t</title>", "main.js": "export const x = 1;\n", "app.css": "a{}",
                 "icon.svg": "<svg/>", "lib/ui.js": "export {};\n", "manifest.webmanifest": "{}", "data.json": "{}",
                 "secret.py": "import os\n", ".hidden.js": "x", "notes.txt": "x", "page.html": "x"}
        for rel, text in files.items():
            _w(os.path.join(app, *rel.split("/")), text)
        saved = (srv.APP_DIR, srv.PAGE, srv.MANIFEST)
        self.addCleanup(lambda: (setattr(srv, "APP_DIR", saved[0]), setattr(srv, "PAGE", saved[1]),
                                 setattr(srv, "MANIFEST", saved[2]), srv._static.update(at=None, map={})))
        srv.APP_DIR, srv.PAGE, srv.MANIFEST = app, os.path.join(app, "index.html"), os.path.join(app, "manifest.webmanifest")
        srv._static.update(at=None, map={})
        for path, ctype in (("/app/main.js", "text/javascript; charset=utf-8"), ("/app/app.css", "text/css; charset=utf-8"),
                            ("/app/icon.svg", "image/svg+xml"), ("/app/lib/ui.js", "text/javascript; charset=utf-8"),
                            ("/app/data.json", "application/json"), ("/manifest.webmanifest", "application/manifest+json"),
                            ("/", "text/html; charset=utf-8"), ("/index.html", "text/html; charset=utf-8")):
            r, body = self.get(path)
            self.assertEqual(r.status, 200, path)
            self.assertEqual(r.getheader("Content-Type"), ctype, path)
            self.assertEqual(r.getheader("Cache-Control"), "no-cache", path)
            r2, b2 = self.get(path, headers={"If-None-Match": r.getheader("ETag")})
            self.assertEqual((r2.status, b2), (304, b""), path)
        for path in ("/app/secret.py", "/app/.hidden.js", "/app/notes.txt", "/app/page.html", "/app/index.html",
                     "/app/../server.py", "/app/%2e%2e/server.py", "/app/lib/../main.js", "/app/lib%2fui.js", "/app/missing.js",
                     "/app/Main.js", "/app/main.js/", "/app/main.js%00.png"):
            r, body = self.get(path)
            self.assertEqual(r.status, 404, path)
            self.assertNotIn(b"import os", body)
        _w(os.path.join(app, "late.js"), "export {};\n")  # picked up on the next walk (at most 2 s later)
        srv._static["at"] = time.monotonic() - srv.STATIC_RESCAN
        self.assertEqual(self.get("/app/late.js")[0].status, 200)
        os.remove(os.path.join(app, "manifest.webmanifest"))
        self.assertEqual(self.get("/manifest.webmanifest")[0].status, 404)

    def test_real_app_files(self):
        srv = self.server_mod
        for rel, ctype in (("main.js", "text/javascript; charset=utf-8"), ("app.css", "text/css; charset=utf-8")):
            if not os.path.isfile(os.path.join(srv.APP_DIR, rel)):
                self.skipTest("the page's files are not there yet")
            r, body = self.get("/app/" + rel)
            self.assertEqual((r.status, r.getheader("Content-Type")), (200, ctype))

    def test_vendor_preact(self):
        for name in ("preact.module.js", "hooks.module.js"):
            if not self.server_mod.vendor_path("preact", name):
                self.skipTest("Preact is not vendored here")
            r, body = self.get("/vendor/" + name)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.getheader("Content-Type"), "text/javascript; charset=utf-8")
            self.assertEqual(r.getheader("Cache-Control"), "no-cache")
            self.assertTrue(r.getheader("ETag"))
        for p in ("/vendor/../x", "/vendor/preact/LICENSE", "/vendor/preact.module.js/", "/vendor/LICENSE"):
            self.assertEqual(self.get(p)[0].status, 404, p)

    # ---------------------------------------------------------------- server-sent events
    def test_events_first_cursor_and_change(self):
        from chitragupta.dashboard import api
        s = self.stream()
        buf = self.read_until(s, b"\n\ndata: ", 5)
        buf = self.read_until(s, b"}\n\n", 5, buf)
        head, _, body = buf.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        hl = head.decode().lower()
        self.assertIn("content-type: text/event-stream; charset=utf-8", hl)
        self.assertIn("cache-control: no-store", hl)
        self.assertIn("x-frame-options: deny", hl)
        self.assertIn("connection: close", hl)
        self.assertNotIn("content-length", hl)
        self.assertTrue(body.startswith(b"retry: 3000\n\nevent: cursor\ndata: "), body)
        first = self.cursors(body)
        self.assertEqual(len(first), 1)
        self.assertRegex(first[0], r"^[0-9a-f]{16}$")
        with open(os.path.join(self.home, "data", "tickets", "ABC-1002.json"), "a", encoding="utf-8") as f:
            f.write("\n")
        t0 = time.monotonic()
        body = self.read_until(s, b"}\n\n", 4, body[body.index(b"}\n\n") + 3:])
        self.assertLess(time.monotonic() - t0, 3.5)
        later = self.cursors(body)
        self.assertEqual(len(later), 1, body)
        self.assertNotEqual(later[0], first[0])
        self.assertEqual(later[0], api.changes()["cursor"])

    def test_events_limit_and_release(self):
        open_ = []
        for _ in range(self.server_mod.MAX_STREAMS):
            s = self.stream()
            self.assertIn(b"event: cursor", self.read_until(s, b"event: cursor"))
            open_.append(s)
        self.assertEqual(self.srv.streams, self.server_mod.MAX_STREAMS)
        r, body = self.get("/api/events")
        self.assertEqual(r.status, 503)
        self.assertEqual(r.getheader("Retry-After"), "5")
        self.assertEqual(json.loads(body), {"error": "too many live connections"})
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(self.get("/api/health")[0].status, 200)  # everything else still answers
        for s in open_:
            s.close()
        self.assertTrue(self.server_mod.wait_until(lambda: self.srv.streams == 0, 5), self.srv.streams)
        s = self.stream()
        self.assertIn(b"event: cursor", self.read_until(s, b"event: cursor"))

    def test_events_end_on_shutdown(self):
        socks = [self.stream() for _ in range(3)]
        for s in socks:
            self.assertIn(b"event: cursor", self.read_until(s, b"event: cursor"))
        t0 = time.monotonic()
        self.srv.shutdown()
        self.assertLess(time.monotonic() - t0, 2.0, "shutdown waited for the streams")
        for s in socks:
            self.read_until(s, b"\x00never", 3)  # returns at EOF
            s.settimeout(0.5)
            self.assertEqual(s.recv(10), b"")
        self.assertLess(time.monotonic() - t0, 4.0)
        self.assertTrue(self.server_mod.wait_until(lambda: self.srv.streams == 0, 3))

    def test_events_host_and_origin_checked(self):
        for host in ("evil.test", "127.0.0.1"):
            r, _ = self.get("/api/events", host=host)
            self.assertEqual(r.status, 403, host)
        r, _ = self.get("/api/events", headers={"Origin": "http://evil.test"})
        self.assertEqual(r.status, 403)
        s = self.stream(headers={"Origin": "http://localhost:%d" % self.port})
        self.assertIn(b"event: cursor", self.read_until(s, b"event: cursor"))
        r, body = self.get("/api/events", method="HEAD")
        self.assertEqual((r.status, body), (200, b""))
        self.assertTrue(self.server_mod.wait_until(lambda: self.srv.streams == 1, 2), self.srv.streams)

    # ---------------------------------------------------------------- read-only
    def test_new_routes_leave_the_home_untouched(self):
        from chitragupta import setup
        saved = (setup.RUNNER, setup._claude_exe)
        self.addCleanup(lambda: (setattr(setup, "RUNNER", saved[0]), setattr(setup, "_claude_exe", saved[1])))
        setup.RUNNER = lambda args, cwd=None, timeout=60: (0, "version 1.0", "")
        setup._claude_exe = lambda cfg=None: ["claude"]
        before = snapshot(self.home)
        for p in ("/api/day/" + self.days["day"], "/api/day/" + self.days["yday"], "/api/day/2001-01-01", "/api/today",
                  "/api/calendar", "/api/calendar?from=2025-01-01&to=2025-12-31", "/api/knowledge", "/api/search?q=order",
                  "/api/search?q=abc-1001&type=ticket", "/api/setup", "/api/status", "/api/tickets", "/vendor/preact.module.js",
                  "/manifest.webmanifest", "/app/main.js"):
            self.assertIn(self.get(p)[0].status, (200, 404), p)
        s = self.stream()
        self.assertIn(b"event: cursor", self.read_until(s, b"event: cursor"))
        s.close()
        self.assertEqual(snapshot(self.home), before, "the dashboard wrote into the data home")


if __name__ == "__main__":
    unittest.main()


class TestOpenAndShortcut(support.Isolated, unittest.TestCase):
    """`dashboard shortcut` writes one plain .url file into the configured folders and uninstall removes only ours;
    with NO_TASK and no override (dev and tests) nothing is written anywhere."""

    def setUp(self):
        self.isolate()

    def tearDown(self):
        self.restore()

    def test_shortcut_written_and_removed(self):
        from chitragupta import install
        self.assertEqual(install.shortcut_dirs(desktop=True), [])
        self.assertEqual(install.write_shortcuts("http://127.0.0.1:7419/#/today")["paths"], [])
        sm, dk = os.path.join(self.user_dir, "sm"), os.path.join(self.user_dir, "desk")
        os.environ["CHITRAGUPTA_STARTMENU_DIR"], os.environ["CHITRAGUPTA_DESKTOP_DIR"] = sm, dk
        r = install.write_shortcuts("http://127.0.0.1:7419/#/today", desktop=True)
        self.assertTrue(r["ok"])
        self.assertEqual(sorted(r["paths"]), sorted([os.path.join(sm, "Chitragupta.url"), os.path.join(dk, "Chitragupta.url")]))
        with open(r["paths"][0], encoding="utf-8", newline="") as f:
            self.assertEqual(f.read(), "[InternetShortcut]\r\nURL=http://127.0.0.1:7419/#/today\r\n")
        other = os.path.join(sm, "Other.url")
        support.wr(other, "[InternetShortcut]\r\nURL=http://example.invalid/\r\n")
        self.assertEqual(len(install.remove_shortcuts()), 2)
        self.assertFalse(os.path.exists(r["paths"][0]))
        self.assertTrue(os.path.exists(other), "someone else's shortcut is left alone")

    def test_open_without_browser_prints_the_route(self):
        import contextlib
        import io
        from chitragupta import dashboard
        from chitragupta.dashboard import __main__ as cli
        calls = []
        real = cli.ensure
        cli.ensure = lambda **k: calls.append(k) or {"status": "running", "url": dashboard.url(7419)}
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["open", "tickets/ABC-1001", "--no-browser"])
        finally:
            cli.ensure = real
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("#/tickets/ABC-1001", out.getvalue())
