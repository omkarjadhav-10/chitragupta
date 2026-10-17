"""Dashboard: read-only API on a synthetic data home, HTTP safety, daemon start/stop/replace, page smoke test.

Everything runs against temp CHITRAGUPTA_HOME / CHITRAGUPTA_USER_DIR folders and ephemeral ports.
"""
import datetime as _dt
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from tests import support
from tests.support import Isolated

from chitragupta import __version__ as VERSION
from chitragupta import common

TICKET = "ABC-1001"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _w(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, indent=1)


REQUESTS_MD = """# ABC-1001 — Replay requests

## Steps

| # | Request | Expected | Source |
|---|---|---|---|
| 1 | Log in | 200 | generated |
| 2 | Create order | 201 | yours |

## curl (Git Bash)

```bash
cd /c/home
ENV=qa
eval "$(py bruenv shell $ENV)"
```

### 1. Log in
Expected: 200

```bash
curl -sS -i -X POST "${shopBaseUrl}/auth/login" \\
  -H "Content-Type: application/json"
```

### 2. Create order
Expected: 201

```bash
curl -sS -i -X POST "${shopBaseUrl}/orders" \\
  -H "Authorization: Bearer ${token}"
```
"""

DBFLOW_MD = """# ABC-1001 — DB flow

_Generated from what Claude Code sessions read._

## Overview
Creating an order writes `shop.orders` then `shop.order_items`.

## Flow
```mermaid
flowchart LR
  a["OrderService.create"] --> b["shop.orders"]
  b --> c["shop.order_items"]
```

## Table map

| Step | Table | Operation |
|---|---|---|
| create 1 | `shop.orders` | insert |
| create 2 | `shop.order_items` | insert |

## Worked example (QA)

_Traced on 2026-10-15 (QA, connection `qa-db`) from the ids used in this ticket's requests: `42`._

| Step | Table | Found by | Values | Note |
|---|---|---|---|---|
| 1 | `shop.orders` | id = 42 | status NEW | created |

```mermaid
flowchart LR
  s1["shop.orders<br/>id = 42"]
```

## My notes
<!-- my-notes:start -->
<!-- my-notes:end -->
"""


def build_home(home, now=None):
    """A small synthetic data home: today's day file and raw events, three tickets, one with requests, a DB flow,
    queries and artifacts, older days in other weeks/months, calendar entries, a sent timesheet and health."""
    now = now or common.now()
    cfg = {"ticket_prefixes": ["ABC"], "day_starts_at": "00:00", "github_remote": "",
           "jira_worklog": {"slack": {"self_user_id": ""}}}
    _w(os.path.join(home, ".worklog", "config.json"), cfg)
    day = now.date().isoformat()
    yday = (now.date() - _dt.timedelta(days=1)).isoformat()
    old = (now.date() - _dt.timedelta(days=40)).isoformat()

    def dayfile(d, tickets, other=None):
        _w(os.path.join(home, "data", "days", d[:4], d + ".json"), {
            "date": d, "updated_at": now.isoformat(timespec="seconds"), "closed": d != day,
            "summary": "Worked on order creation." if d == day else "",
            "tickets": {k: {"minutes": m, "first_time": "09:10", "what_i_did": [{"time": "09:10", "text": "Traced the order insert."}]}
                        for k, m in tickets.items()},
            "other": {k: {"minutes": m} for k, m in (other or {}).items()},
            "commits": [{"sha": "a" * 40, "short": "aaaaaaa", "ticket": TICKET, "subject": "Fix order total"}] if d == day else [],
        })
    dayfile(day, {TICKET: 95, "ABC-1002": 40}, {"shop-api · main": 15})
    dayfile(yday, {TICKET: 120, "ABC-1003": 200})
    dayfile(old, {"ABC-1002": 300})

    _w(os.path.join(home, "data", "tickets", TICKET + ".json"), {
        "id": TICKET, "title": "Order total is wrong for discounted items", "status": "In Progress",
        "first": old, "last": day, "repos": ["C:/src/shop-api"], "branches": ["feature/ABC-1001-order-total"],
        "tldr": "Discount applied twice in OrderService.total.", "problem": "Totals are off for discounted orders.",
        "items": {
            "i1": {"id": "i1", "kind": "root_cause", "title": "Discount applied twice", "detail": "OrderService.total adds it again.",
                   "date": day, "time": "10:00", "confidence": "confirmed", "evidence": ["OrderService.java:88"]},
            "i2": {"id": "i2", "kind": "how_it_works", "title": "Orders are written in one transaction", "detail": "",
                   "date": yday, "time": "11:00"},
            "i3": {"id": "i3", "kind": "todo", "title": "Add a regression test", "date": day},
        },
        "queries": {"q1": {"dialect": "PostgreSQL", "kind": "read", "purpose": "Find the order",
                           "text": "SELECT * FROM shop.orders WHERE id = 42", "tables": ["shop.orders"], "date": day, "time": "10:05"}},
        "timeline": {old: {"minutes": 0, "line": "first seen"}, yday: {"minutes": 120, "line": "traced"},
                     day: {"minutes": 95, "line": "fixed"}},
        "replay": {"final": [
            {"n": 1, "name": "Log in", "method": "POST", "url": "{{shopBaseUrl}}/auth/login", "expected": "200", "owner": "engine"},
            {"n": 2, "name": "Create order", "method": "POST", "url": "{{shopBaseUrl}}/orders", "expected": "201", "owner": "user"},
        ], "dropped": [{"call_id": "x", "reason": "duplicate"}]},
        "api_calls": {"c1": {"method": "POST", "url": "https://api.acme.test/orders"}},
        "data_flow": {"f1": {"table": "shop.orders", "operation": "insert"}},
        "artifacts": {"a1": {"name": "order-trace.md", "kind": "analysis", "purpose": "Trace of the order insert",
                             "date": yday, "last": day, "stored_name": yday + "_order-trace.md", "versions": [1, 2]}},
        "files": {"src/OrderService.java": {"path": "src/OrderService.java", "role": "service", "changed": True, "last": day}},
    })
    _w(os.path.join(home, "data", "tickets", "ABC-1002.json"), {"id": "ABC-1002", "title": "Checkout timeout", "last": day,
                                                              "timeline": {day: {"minutes": 40}}})
    _w(os.path.join(home, "data", "tickets", "ABC-1003.json"), {"id": "ABC-1003", "title": "Refund report", "last": yday,
                                                              "timeline": {yday: {"minutes": 200}}})
    _w(os.path.join(home, "tickets", TICKET, "requests.md"), REQUESTS_MD)
    _w(os.path.join(home, "tickets", TICKET, "db-flow.md"), DBFLOW_MD)
    _w(os.path.join(home, "tickets", TICKET, "README.md"), "# ABC-1001\n")
    _w(os.path.join(home, "tickets", TICKET, "artifacts", yday + "_order-trace.md"), "# trace\n")
    _w(os.path.join(home, "data", "qa-trace", TICKET + ".json"), {
        "ticket": TICKET, "env": "qa", "fetched_at": yday + "T10:00:00+00:00",
        "trace": {"summary": "Order 42 created.", "steps": [{"table": "shop.orders", "key": "id = 42", "values": "status NEW"}]}})
    _w(os.path.join(home, "data", "calendar.json"), {
        "leave": {(now.date() - _dt.timedelta(days=3)).isoformat(): "Family"}, "holiday": {}, "half_day": {}, "workday": {},
        "time": {day: [{"ticket": "ABC-1002", "start": "18:00", "minutes": 30, "text": "Call about checkout"}]}})
    _w(os.path.join(home, "data", "timesheets", yday + ".json"), {
        "day": yday, "status": "sent", "sent_at": yday + "T20:01:00+00:00", "attempts": 0,
        "entries": [{"ticket": TICKET, "minutes": 240, "start_hhmm": "10:30", "desc": "Traced the order insert."}],
        "versions": [{"v": 1, "text": "Today's update", "kind": "message", "slack": {"ok": True}}]})
    _w(os.path.join(home, ".worklog", "health.json"), {
        "last_tick": now.isoformat(timespec="seconds"), "last_tick_ok": now.isoformat(timespec="seconds"),
        "last_push": "", "errors_total": 1, "last_error": "2026-10-16T10:00:00+00:00 extract 1234abcd: claude rc=124: timeout after 300s",
        "warnings": [], "today": {"day": day, "sessions": 2, "extracted": 1, "errors": 0, "cost_usd": 0.12},
        "mcp": {"calls": {day: 2}, "jobs": {"jira-card-ABC-1001": {"at": now.isoformat(timespec="seconds"), "calls": 1, "in": 1200,
                                                                   "out": 300, "rc": 0, "servers": {"jira": "connected"}}}}})
    # raw hook events for today (the Jira-worklog preview builds its blocks from these)
    start = max(now - _dt.timedelta(minutes=50), now.replace(hour=0, minute=0, second=0, microsecond=0))
    evs = []
    t = start
    while t < now - _dt.timedelta(minutes=1):
        evs.append({"ts": t.isoformat(timespec="seconds"), "event": "post-tool", "session_id": "s1", "source": "hook",
                    "cwd": "C:/src/shop-api", "repo_root": "C:/src/shop-api", "branch": "feature/ABC-1001-order-total",
                    "payload": {"tool": "Read", "input": {"file_path": "OrderService.java"}}})
        t += _dt.timedelta(minutes=5)
    _w(os.path.join(home, ".worklog", "raw", day, "s1.jsonl"), "".join(json.dumps(e) + "\n" for e in evs))
    _w(os.path.join(home, ".worklog", "logs", "agent.log"), "2026-10-16T10:00:00+00:00 pushed\n"
                                                            "2026-10-16T11:00:00+00:00 condense failed: boom\n")
    return {"day": day, "yday": yday, "old": old}


def snapshot(root):
    out = {}
    for dp, dns, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            st = os.stat(p)
            out[os.path.relpath(p, root)] = (st.st_mtime_ns, st.st_size)
    return out


class Base(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.days = build_home(self.home)
        from chitragupta import extract
        self._real_call = extract.call_claude

        def no_model(*a, **k):
            self.forbidden.append("extract.call_claude")
            raise RuntimeError("tests: no model calls")
        extract.call_claude = no_model

    def tearDown(self):
        from chitragupta import extract
        extract.call_claude = self._real_call
        self.restore()


# --------------------------------------------------------------------------- API (pure functions)

class TestApi(Base):
    def test_today(self):
        from chitragupta.dashboard import api
        d = api.today()
        self.assertEqual(d["day"], self.days["day"])
        self.assertEqual([t["key"] for t in d["tickets"]], [TICKET, "ABC-1002"])
        self.assertEqual(d["tickets"][0]["minutes"], 95)
        self.assertEqual(d["tickets"][0]["title"], "Order total is wrong for discounted items")
        self.assertEqual(d["total_minutes"], 95 + 40 + 15)
        pv = d["worklog_preview"]
        self.assertIn(pv["kind"], ("message", "nudge", "none"), pv)
        if pv["kind"] == "message":
            self.assertTrue(pv["entries"])
            e = pv["entries"][0]
            self.assertRegex(e["start"], r"^\d\d:\d\d$")
            self.assertTrue(e["duration"])
            self.assertTrue(e["description"])
            self.assertIn("ABC-1002", [x["ticket"] for x in pv["entries"]])  # the manual `time:` entry
        self.assertIsNone(d["worklog_saved"])  # nothing saved for today: the preview never saves

    def test_tickets_and_search(self):
        from chitragupta.dashboard import api
        d = api.tickets()
        self.assertEqual(d["count"], 3)
        self.assertEqual(d["tickets"][-1]["key"], "ABC-1003")
        self.assertEqual([t["key"] for t in api.tickets("discount")["tickets"]], [TICKET])
        self.assertEqual([t["key"] for t in api.tickets("checkout")["tickets"]], ["ABC-1002"])
        self.assertEqual(api.tickets("nothing-like-this")["count"], 0)

    def test_ticket_detail(self):
        from chitragupta.dashboard import api
        t = api.ticket(TICKET)
        self.assertEqual(t["findings"][0]["kind"], "root_cause")
        self.assertEqual(len(t["queries"]), 1)
        steps = t["requests"]["steps"]
        self.assertEqual([s["n"] for s in steps], [1, 2])
        self.assertIn('curl -sS -i -X POST "${shopBaseUrl}/auth/login"', steps[0]["curl"])
        self.assertIn("Authorization: Bearer", steps[1]["curl"])
        self.assertIn("bruenv shell", t["requests"]["setup"])
        self.assertIn("```mermaid", t["dbflow"]["markdown"])
        self.assertIn("## Worked example (QA)", t["dbflow"]["markdown"])
        self.assertEqual(t["dbflow"]["qa_trace"]["summary"], "Order 42 created.")
        self.assertEqual(t["artifacts"][0]["name"], "order-trace.md")
        self.assertTrue(t["artifacts"][0]["path"].endswith("_order-trace.md"))
        self.assertIn("dbflow", t["paths"])
        self.assertIsNone(api.ticket("../../etc"))
        self.assertIsNone(api.ticket("ABC-9999"))

    def test_curl_fallback(self):
        from chitragupta.dashboard import api
        c = api.curl_for({"method": "post", "url": "https://api.acme.test/x", "headers": [["A", "it's"]], "body": {"k": 1}})
        self.assertEqual(c, "curl -sS -i -X POST 'https://api.acme.test/x' \\\n  -H 'A: it'\\''s' \\\n  --data-binary '{\"k\": 1}'")

    def test_timesheet(self):
        from chitragupta.dashboard import api
        w = api.timesheet("week")
        today_week = api._iso_week(self.days["day"])
        self.assertIn(today_week, w["periods"])
        row = next(r for r in w["rows"] if r["ticket"] == TICKET)
        self.assertGreaterEqual(row["total"], 95 + 120)
        row2 = next(r for r in w["rows"] if r["ticket"] == "ABC-1002")
        self.assertGreaterEqual(row2["minutes"][today_week], 40 + 30)  # tracked + logged by hand
        self.assertTrue(any(r["ticket"] == "" for r in w["rows"]))  # other work
        self.assertTrue(any(c["kind"] == "leave" for c in w["calendar"]))
        m = api.timesheet("month")
        self.assertEqual(m["period"], "month")
        self.assertIn(self.days["old"][:7], m["periods"])
        csv_text = api.timesheet_csv("month")
        self.assertTrue(csv_text.startswith("period,ticket,title,minutes,hours\r\n"))
        self.assertIn("%s,ABC-1002,Checkout timeout,300,5.00" % self.days["old"][:7], csv_text)
        self.assertIn("(leave)", csv_text)

    def test_status(self):
        from chitragupta.dashboard import api
        s = api.status()
        self.assertIsNotNone(s["hooks"]["last_raw_event"])
        self.assertLessEqual(s["hooks"]["minutes_ago"], 60)
        self.assertEqual(s["tick"]["minutes_ago"], 0)
        self.assertFalse(s["push"]["remote_set"])
        self.assertEqual(s["mcp"]["servers"][0], {"server": "jira", "status": "connected", "at": s["mcp"]["servers"][0]["at"],
                                                  "job": "jira-card-ABC-1001"})
        self.assertEqual(s["mcp"]["calls_today"], 2)
        self.assertEqual((s["mcp"]["tokens_in_today"], s["mcp"]["tokens_out_today"]), (1200, 300))
        self.assertEqual(s["slack"]["recent"][0]["status"], "sent")
        texts = " ".join(e["text"] for e in s["errors"])
        self.assertIn("timeout after 300s", texts)
        self.assertIn("condense failed", texts)

    def test_changes_cursor_moves(self):
        from chitragupta.dashboard import api
        c1 = api.changes(max_age=0)["cursor"]
        self.assertEqual(api.changes(max_age=0)["cursor"], c1)
        p = os.path.join(self.home, "data", "tickets", "ABC-1002.json")
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n")
        self.assertNotEqual(api.changes(max_age=0)["cursor"], c1)

    def test_empty_home(self):
        from chitragupta.dashboard import api
        support.rmtree(self.home)
        os.makedirs(self.home)
        self.assertEqual(api.tickets()["count"], 0)
        self.assertEqual(api.today(preview=False)["tickets"], [])
        self.assertEqual(api.timesheet()["periods"], [])
        self.assertIsNone(api.status()["hooks"]["last_event"])


# --------------------------------------------------------------------------- HTTP server (in-process, ephemeral port)

class TestHttp(Base):
    def setUp(self):
        super().setUp()
        from chitragupta.dashboard import server
        self.srv, self.thread = server.start_in_thread(0)
        self.port = self.srv.port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def get(self, path, method="GET", host=None, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest(method, path, skip_host=True)
            c.putheader("Host", host if host is not None else "127.0.0.1:%d" % self.port)
            for k, v in (headers or {}).items():
                c.putheader(k, v)
            if body is not None:
                c.putheader("Content-Length", str(len(body)))
            c.endheaders(body)
            r = c.getresponse()
            return r, r.read()
        finally:
            c.close()

    def raw(self, data):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            s.sendall(data)
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return buf
        finally:
            s.close()

    def alive(self):
        r, body = self.get("/api/health")
        return r.status == 200 and json.loads(body)["app"] == "chitragupta"

    def test_health_identity(self):
        r, body = self.get("/api/health")
        self.assertEqual(r.status, 200)
        h = json.loads(body)
        self.assertEqual(h, {"app": "chitragupta", "version": VERSION, "pid": os.getpid(), "home": common.home()})

    def test_bound_to_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        self.assertFalse(self.srv.allow_reuse_address)

    def test_security_headers(self):
        r, _ = self.get("/api/health")
        self.assertEqual(r.getheader("Content-Security-Policy").split("; ")[:6],
                         ["default-src 'self'", "script-src 'self' 'unsafe-inline'", "style-src 'self' 'unsafe-inline'",
                          "img-src 'self' data:", "connect-src 'self'", "frame-ancestors 'none'"])
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
        r, _ = self.get("/api/health", headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_host_check(self):
        self.assertEqual(self.get("/api/health", host="localhost:%d" % self.port)[0].status, 200)
        for bad in ("evil.test", "evil.test:%d" % self.port, "127.0.0.1", "127.0.0.1:1", "127.0.0.1.evil.test:%d" % self.port, ""):
            r, body = self.get("/api/today", host=bad)
            self.assertEqual(r.status, 403, bad)
            self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        r, _ = self.get("/api/health", headers={"Origin": "http://evil.test"})
        self.assertEqual(r.status, 403)
        self.assertTrue(self.alive())

    def test_methods(self):
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "FOO"):
            r, _ = self.get("/api/health", method=m, body=b"{}" if m in ("POST", "PUT") else None)
            self.assertEqual(r.status, 405, m)
            self.assertEqual(r.getheader("Allow"), "GET, HEAD")
        r, body = self.get("/api/health", method="HEAD")
        self.assertEqual(r.status, 200)
        self.assertEqual(body, b"")
        self.assertTrue(self.alive())

    def test_malformed_requests(self):
        for data in (b"GARBAGE\r\n\r\n", b"GET\r\n\r\n", b"GET / HTTP/9.9\r\nHost: x\r\n\r\n", b"\xff\xfe\x00junk\r\n\r\n",
                     b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n", b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nBad header\r\n\r\n" % self.port):
            resp = self.raw(data)
            self.assertTrue(resp.startswith(b"HTTP/1."), data[:30])
            self.assertRegex(resp.split(b"\r\n")[0], rb" (400|403|414|505) ")
            self.assertTrue(self.alive())
        # a client that connects and says nothing does not block others
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            self.assertTrue(self.alive())
        finally:
            s.close()

    def test_paths_cannot_escape(self):
        for p in ("/../../etc/passwd", "/page.html", "/chitragupta/common.py", "/vendor/../server.py", "/api/tickets/..%2F..%2Fx",
                  "/api/tickets/ABC-1001/../../x", "/%2e%2e/%2e%2e/LICENSE", "/third_party/mermaid/LICENSE", "/api/tickets/abc"):
            r, body = self.get(p)
            self.assertIn(r.status, (400, 404), p)
            self.assertNotIn(b"import ", body)

    def test_page_smoke(self):
        r, body = self.get("/")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/html"))
        html = body.decode("utf-8")
        self.assertIn("<title>Chitragupta</title>", html)
        self.assertEqual(re.findall(r"(?i)\b(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", html), [], "no external URLs")
        self.assertNotRegex(html, r"(?i)<(script|link|img|iframe)[^>]+(src|href)\s*=\s*[\"']?(https?:|//)")
        for route in ("#/today", "#/tickets", "#/timesheet", "#/health"):
            self.assertIn(route, html)
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertIn("localStorage", html)
        self.assertIn('class="skip"', html)
        self.assertIn("/api/changes", html)
        etag = r.getheader("ETag")
        r2, b2 = self.get("/", headers={"If-None-Match": etag})
        self.assertEqual(r2.status, 304)
        r, body = self.get("/vendor/mermaid.min.js")
        self.assertEqual(r.status, 200)
        self.assertGreater(len(body), 100000)
        self.assertTrue(r.getheader("Content-Type").startswith("text/javascript"))

    def test_api_routes(self):
        r, body = self.get("/api/today")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertEqual(json.loads(body)["tickets"][0]["key"], TICKET)
        self.assertEqual(json.loads(self.get("/api/tickets?q=refund")[1])["tickets"][0]["key"], "ABC-1003")
        t = json.loads(self.get("/api/tickets/abc-1001")[1])
        self.assertEqual(t["key"], TICKET)
        self.assertEqual(self.get("/api/tickets/ABC-4242")[0].status, 404)
        ts = json.loads(self.get("/api/timesheet?period=month&limit=x")[1])
        self.assertEqual(ts["period"], "month")
        r, body = self.get("/api/timesheet.csv?period=week")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/csv"))
        self.assertIn("attachment", r.getheader("Content-Disposition"))
        self.assertTrue(body.decode("utf-8-sig").startswith("period,ticket,title,minutes,hours"))
        st = json.loads(self.get("/api/status")[1])
        self.assertEqual(st["dashboard"]["port"], self.port)
        self.assertRegex(json.loads(self.get("/api/changes")[1])["cursor"], r"^[0-9a-f]{16}$")
        self.assertEqual(self.get("/api/nope")[0].status, 404)

    def test_shutdown_needs_token(self):
        for q in ("", "?token=", "?token=wrong"):
            self.assertEqual(self.get("/api/shutdown" + q)[0].status, 403)
        self.assertTrue(self.alive())
        with open(os.path.join(self.user_dir, "dashboard", "%d.token" % self.port), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["token"], self.srv.token)

    def test_read_only(self):
        before = snapshot(self.home)
        for p in ("/", "/api/today", "/api/tickets", "/api/tickets/ABC-1001", "/api/tickets/ABC-1002", "/api/timesheet",
                  "/api/timesheet?period=month", "/api/timesheet.csv", "/api/status", "/api/changes", "/api/health"):
            self.assertEqual(self.get(p)[0].status, 200, p)
        self.assertEqual(snapshot(self.home), before, "the dashboard wrote into the data home")


# --------------------------------------------------------------------------- daemon (real detached processes)

STUB = textwrap.dedent("""
    import json, os, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port, body = int(sys.argv[1]), sys.argv[2]
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            b = body.replace("PID", str(os.getpid())).encode()
            self.send_response(200 if self.path == "/api/health" else 404)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        def log_message(self, *a):
            pass
    class S(HTTPServer):
        allow_reuse_address = False
    S(("127.0.0.1", port), H).serve_forever()
""")


class TestDaemon(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        build_home(self.home)
        self.port = free_port()
        os.environ["CHITRAGUPTA_DASHBOARD_PORT"] = str(self.port)
        os.environ.pop("CHITRAGUPTA_DASHBOARD", None)  # support.isolate() turns autostart off; these tests start it
        self.stubs = []
        self.stub_py = os.path.join(self.user_dir, "stub_health.py")
        with open(self.stub_py, "w", encoding="utf-8") as f:
            f.write(STUB)

    def tearDown(self):
        from chitragupta import dashboard
        try:
            dashboard.stop(self.port)
        finally:
            for p in self.stubs:
                if p.poll() is None:
                    p.kill()
                p.wait(10)
            time.sleep(0.2)
            self.restore()

    def stub(self, body):
        p = subprocess.Popen([sys.executable, "-B", self.stub_py, str(self.port), body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.stubs.append(p)
        from chitragupta import dashboard
        deadline = time.monotonic() + 10
        while dashboard.probe(self.port, 1.0)["state"] == "down" and time.monotonic() < deadline:
            time.sleep(0.1)
        return p

    def test_start_running_stop(self):
        from chitragupta import dashboard
        self.assertEqual(dashboard.probe(self.port)["state"], "down")
        t0 = time.monotonic()
        self.assertEqual(dashboard.probe(self.port, 0.15)["state"], "down")
        self.assertLess(time.monotonic() - t0, 1.0, "a down probe must be fast")
        r = dashboard.ensure(wait=15)
        self.assertEqual(r["status"], "started", r)
        st = dashboard.probe(self.port, 1.5)
        self.assertEqual(st["state"], "ours")
        self.assertEqual(st["health"]["version"], VERSION)
        self.assertNotEqual(st["health"]["pid"], os.getpid())
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".worklog", "logs", "dashboard.log")))
        r2 = dashboard.ensure(timeout=1.5)
        self.assertEqual(r2["status"], "running")
        self.assertEqual(r2["pid"], st["health"]["pid"])
        s = dashboard.stop(self.port)
        self.assertTrue(s["stopped"], s)
        self.assertEqual(dashboard.probe(self.port)["state"], "down")

    def test_replace_older_version(self):
        from chitragupta import dashboard
        old = self.stub(json.dumps({"app": "chitragupta", "version": "0.0.1", "pid": "PID", "home": self.home}).replace('"PID"', "PID"))
        st = dashboard.probe(self.port, 1.5)
        self.assertEqual(st["state"], "ours")
        self.assertEqual(st["health"]["version"], "0.0.1")
        r = dashboard.ensure(timeout=1.5, wait=15)
        if r["status"] != "replaced":  # show the spawned server's own log in the failure
            try:
                with open(dashboard.log_path(), encoding="utf-8", errors="replace") as f:
                    r["log"] = f.read()[-3000:]
            except OSError as e:
                r["log"] = repr(e)
        self.assertEqual(r["status"], "replaced", r)
        old.wait(10)
        st = dashboard.probe(self.port, 1.5)
        self.assertEqual(st["state"], "ours")
        self.assertEqual(st["health"]["version"], VERSION)

    def test_newer_version_is_kept(self):
        from chitragupta import dashboard
        newer = self.stub(json.dumps({"app": "chitragupta", "version": "99.0.0", "pid": "PID", "home": self.home}).replace('"PID"', "PID"))
        r = dashboard.ensure(timeout=1.5)
        self.assertEqual(r["status"], "running")
        self.assertEqual(r["version"], "99.0.0")
        self.assertIsNone(newer.poll())  # never downgraded

    def test_foreign_app_left_alone(self):
        from chitragupta import dashboard
        other = self.stub(json.dumps({"app": "something-else", "version": "1.0"}))
        self.assertEqual(dashboard.probe(self.port, 1.5)["state"], "foreign")
        r = dashboard.ensure(timeout=1.5)
        self.assertEqual(r["status"], "other")
        s = dashboard.stop(self.port)
        self.assertFalse(s["stopped"])
        self.assertIsNone(other.poll())

    def test_other_home_left_alone(self):
        from chitragupta import dashboard
        other_home = tempfile.mkdtemp(prefix="cg-other-")
        try:
            p = self.stub(json.dumps({"app": "chitragupta", "version": "0.0.1", "pid": "PID", "home": other_home}).replace('"PID"', "PID"))
            r = dashboard.ensure(timeout=1.5)
            self.assertEqual(r["status"], "other")
            self.assertIsNone(p.poll())
        finally:
            support.rmtree(other_home)

    def test_disabled(self):
        from chitragupta import dashboard
        os.environ["CHITRAGUPTA_DASHBOARD"] = "off"
        self.assertEqual(dashboard.ensure()["status"], "disabled")
        self.assertEqual(dashboard.probe(self.port)["state"], "down")

    def test_cli(self):
        env = self.child_env(CHITRAGUPTA_DASHBOARD_PORT=str(self.port), CHITRAGUPTA_DASHBOARD="1")
        run = lambda *a: subprocess.run([sys.executable, "-B", "-m", "chitragupta", "dashboard", *a], cwd=support.ROOT, env=env,
                                        capture_output=True, text=True, timeout=60)
        r = run("url", "tickets/ABC-1001")
        self.assertEqual(r.stdout.strip(), "http://127.0.0.1:%d/#/tickets/ABC-1001" % self.port)
        self.assertIn("not running", run("status").stdout)
        r = run("ensure")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("http://127.0.0.1:%d/" % self.port, r.stdout)
        st = json.loads(run("status", "--json").stdout)
        self.assertEqual(st["state"], "ours")
        self.assertEqual(run("stop").returncode, 0)
        self.assertEqual(json.loads(run("status", "--json").stdout)["state"], "down")


if __name__ == "__main__":
    unittest.main()
