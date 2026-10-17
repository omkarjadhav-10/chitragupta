"""Ticket view for branch tracking: the gitGraph built from tk["git"], the dashboard API and page hooks, and the
self-contained ticket export (full / shareable) — over pure functions and a real local server.

All data is synthetic (ticket ABC-1001, repo shop-api, hosts *.acme.test) and lives in a temp CHITRAGUPTA_HOME.
"""
import copy
import http.client
import json
import os
import re
import unittest

from tests import support
from tests.support import Isolated
from tests.test_dashboard import build_home, snapshot

from chitragupta import common

TICKET = "ABC-1001"
PR_URL = "https://git.acme.test/shop/shop-api/pull/12"
QA_URL = "https://shop-qa.acme.test/api/orders/42"


def git_entry():
    """One repo of tk["git"]["repos"] as delivery.compute() writes it: 2 branches, 2 PRs, 3 rows, 2 alerts."""
    return {
        "repo": "shop-api", "strategy": "release-then-qa", "strategy_how": "repo default", "pr_source": "gh",
        "branches": [
            {"branch": "feature/ABC-1001-order-total", "parent": "release/1.0", "parent_role": "release",
             "parent_source": "command", "role_guessed": False, "created_at": "2026-10-01T09:00:00+05:30",
             "link": "branch name", "status": "merged", "suggested": None, "prs": [12], "unpushed": 0, "ahead": 0,
             "behind": 3, "last_commit": "2026-10-02T11:00:00+05:30", "local": True, "on_remote": True, "own": 2,
             "commits": [{"sha": "b2c3d4e5f", "subject": "ABC-1001 Add a test for the \"total\"", "date": "2026-10-02T11:00:00+05:30"},
                         {"sha": "a1b2c3d4e", "subject": "ABC-1001 Fix order total", "date": "2026-10-01T10:00:00+05:30"}]},
            {"branch": "hotfix/ABC-1001+qa", "parent": "qa", "parent_role": "qa", "parent_source": "inferred",
             "role_guessed": True, "created_at": None, "link": "commit message", "status": "pr_open",
             "suggested": "abandon (superseded by feature/ABC-1001-order-total)", "prs": [13], "unpushed": 1,
             "ahead": 2, "behind": 0, "last_commit": "2026-10-04T10:00:00+05:30", "local": True, "on_remote": True, "own": 2,
             "commits": [{"sha": "d4e5f6a7b", "subject": "ABC-1001 Late fix", "date": "2026-10-04T10:00:00+05:30"},
                         {"sha": "c3d4e5f6a", "subject": "ABC-1001 Fix order total", "date": "2026-10-03T09:30:00+05:30"}]},
        ],
        "prs": [
            {"number": 12, "title": "Fix order total", "source": "feature/ABC-1001-order-total", "target": "release/1.0",
             "state": "merged", "review": "approved", "created_at": "2026-10-02T12:00:00+05:30",
             "merged_at": "2026-10-02T15:00:00+05:30", "merge_commit": "e5f6a7b8c9", "head_at_merge": "b2c3d4e5f",
             "url": PR_URL, "src": "gh"},
            {"number": 13, "title": "Order total on qa", "source": "hotfix/ABC-1001+qa", "target": "qa", "state": "open",
             "review": "approved", "created_at": "2026-10-03T10:00:00+05:30", "merged_at": None, "merge_commit": None,
             "head_at_merge": None, "url": "https://git.acme.test/shop/shop-api/pull/13", "src": "gh"},
        ],
        "targets": [{"role": "release", "name": "release/1.0", "how": "PR #12 target", "any": None},
                    {"role": "qa", "name": "qa", "how": "base", "any": None}],
        "rows": [
            {"id": "1111aaaa2222", "shas": ["a1b2c3d4e", "c3d4e5f6a"], "loose": [], "subject": "ABC-1001 Fix order total",
             "date": "2026-10-01T10:00:00+05:30", "branches": ["feature/ABC-1001-order-total", "hotfix/ABC-1001+qa"],
             "cells": {"release": {"s": "in", "ref": "origin/release/1.0", "via": "a1b2c3d4e"}, "qa": {"s": "open", "pr": 13}}},
            {"id": "3333bbbb4444", "shas": ["b2c3d4e5f"], "loose": [], "subject": "ABC-1001 Add a test for the \"total\"",
             "date": "2026-10-02T11:00:00+05:30", "branches": ["feature/ABC-1001-order-total"],
             "cells": {"release": {"s": "squash", "pr": 12, "via": "e5f6a7b8c"}, "qa": {"s": "missing", "why": "no PR carries it"}}},
            {"id": "5555cccc6666", "shas": ["d4e5f6a7b"], "loose": [], "subject": "ABC-1001 Late fix",
             "date": "2026-10-04T10:00:00+05:30", "branches": ["hotfix/ABC-1001+qa"],
             "cells": {"release": {"s": "missing", "why": "pushed after PR #12 merged"},
                       "qa": {"s": "check", "via": "f6a7b8c9d", "why": "same message, different patch"}}},
        ],
        "alerts": [
            {"kind": "gap", "text": "1 commit in release/1.0 but not in qa.", "commits": ["b2c3d4e5f"], "target": "qa"},
            {"kind": "lost", "text": "1 commit not in release/1.0 — pushed after PR #12 merged.", "commits": ["d4e5f6a7b"],
             "target": "release"},
        ],
    }


def git_block():
    e = git_entry()
    return {"repos": {"shop-api": e}, "sigs": {"shop-api": "0123456789abcdef"}, "sig": "ok",
            "alerts": [dict(a, repo="shop-api") for a in e["alerts"]],
            "summary": "Branches 2 · PRs 1 merged, 1 open · Delivery ⚠ 2 commits not in qa",
            "checked_at": "2026-10-04T12:00:00+05:30"}


def add_git(home):
    """The dashboard's synthetic home plus a branch-tracking block, a QA request with a body, and a local path."""
    p = os.path.join(home, "data", "tickets", TICKET + ".json")
    with open(p, encoding="utf-8") as f:
        tk = json.load(f)
    tk["git"] = git_block()
    tk["replay"]["final"].append({"n": 3, "name": "Read the order", "method": "GET", "url": QA_URL, "expected": "200",
                                  "headers": [["Content-Type", "application/json"]], "body": {"note": "qa-only-body-marker"}})
    tk["items"]["i1"]["evidence"] = ["C:\\src\\shop-api\\OrderService.java:88"]
    tk["queries"]["q1"]["revealed"] = "Order 42 has total 99.50 (sample row)"
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(tk, f, indent=1)


def embedded(html):
    m = re.search(r'<script id="cg-export" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else None


def strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, list):
        for x in o:
            yield from strings(x)
    elif isinstance(o, dict):
        for x in o.values():
            yield from strings(x)


# --------------------------------------------------------------------------- gitview (pure)

class TestGitGraph(unittest.TestCase):
    def test_graph_shape(self):
        from chitragupta import gitview
        g = gitview.graph(git_entry())
        src = g["source"]
        self.assertIsNotNone(src, g["why"])
        self.assertEqual(gitview.check(src), [])
        lines = [l.strip() for l in src.splitlines()]
        self.assertTrue(lines[0].startswith('%%{init: {"gitGraph": {"mainBranchName": "release/1.0"'))
        self.assertEqual(lines[1], "gitGraph")
        self.assertIn('branch "qa"', lines)
        self.assertIn('checkout "release/1.0"', lines)
        self.assertIn('branch "feature/ABC-1001-order-total"', lines)
        self.assertIn('branch "hotfix/ABC-1001-qa"', lines)  # '+' is not drawable
        self.assertEqual(g["legend"], {"hotfix/ABC-1001-qa": "hotfix/ABC-1001+qa"})
        merges = [l for l in lines if l.startswith("merge ")]
        self.assertEqual(merges, ['merge "feature/ABC-1001-order-total" id: "PR 12 into release/1.0" tag: "PR #12"'])
        self.assertEqual(lines[lines.index(merges[0]) - 1], 'checkout "release/1.0"')
        commits = [l for l in lines if l.startswith("commit ")]
        order = [next(i for i, l in enumerate(commits) if sha in l) for sha in ("a1b2c3d", "b2c3d4e", "c3d4e5f", "d4e5f6a")]
        self.assertEqual(order, sorted(order), "commits in date order")
        self.assertIn('tag: "cherry-pick of a1b2c3d"', next(l for l in commits if "c3d4e5f" in l))
        self.assertIn("type: REVERSE", next(l for l in commits if "d4e5f6a" in l))   # missing from a target
        self.assertNotIn("type: REVERSE", next(l for l in commits if "a1b2c3d" in l))
        self.assertNotIn('\\"', src)
        self.assertNotIn('total""', src)  # the quote in a subject is stripped, not passed to Mermaid
        self.assertEqual(gitview.gitgraph(git_entry()), src)

    def test_none_when_not_drawable(self):
        from chitragupta import gitview
        self.assertIsNone(gitview.gitgraph({}))
        self.assertIsNone(gitview.gitgraph(None))
        self.assertIsNone(gitview.gitgraph({"branches": []}))
        self.assertIsNone(gitview.gitgraph(git_entry(), max_commits=3))
        e = git_entry()
        e["branches"][0]["commits"] = [{"sha": "%07xab" % i, "subject": "c%d" % i, "date": "2026-10-01T10:%02d:00+00:00" % (i % 60)}
                                       for i in range(45)]
        self.assertIsNone(gitview.gitgraph(e))
        self.assertIn("too many commits", gitview.graph(e)["why"])
        e = git_entry()
        for b in e["branches"]:
            b["commits"] = []
        self.assertIsNone(gitview.gitgraph(e))

    def test_never_merges_into_itself(self):
        from chitragupta import gitview
        e = git_entry()
        e["prs"].append({"number": 14, "source": "feature/ABC-1001-order-total", "target": "feature/ABC-1001-order-total",
                         "state": "merged", "merged_at": "2026-10-05T10:00:00+05:30"})
        e["prs"].append({"number": 15, "source": "feature/ABC-1001-order-total", "target": "release/1.0",
                         "state": "merged", "merged_at": "2026-10-06T10:00:00+05:30"})  # nothing new since #12
        e["branches"].append({"branch": "feature/ABC-1001-empty", "parent": "qa", "created_at": "2026-10-05T09:00:00+05:30",
                              "commits": []})
        e["prs"].append({"number": 16, "source": "feature/ABC-1001-empty", "target": "qa", "state": "merged",
                         "merged_at": "2026-10-05T11:00:00+05:30"})
        src = gitview.gitgraph(e)
        self.assertEqual(gitview.check(src), [])
        self.assertEqual(len([l for l in src.splitlines() if l.strip().startswith("merge ")]), 1)

    def test_awkward_names_and_subjects(self):
        from chitragupta import gitview
        e = git_entry()
        e["branches"][0]["branch"] = 'feat/"quoted" name;%%{x}'
        e["prs"][0]["source"] = e["branches"][0]["branch"]
        e["branches"][1]["branch"] = "commit"           # a gitGraph keyword
        e["branches"][1]["parent"] = "../../qa"
        e["prs"][1]["source"], e["prs"][1]["target"] = "commit", "../../qa"
        e["branches"][0]["commits"][0]["subject"] = 'He said "x"; %%{init: {}}%% <script>alert(1)</script>'
        g = gitview.graph(e)
        self.assertIsNotNone(g["source"], g["why"])
        self.assertEqual(gitview.check(g["source"]), [])
        self.assertNotIn("<script", g["source"])
        self.assertEqual(g["source"].count("%%{"), 1)  # only our own init directive
        self.assertIn("../../qa", g["legend"].values())

    def test_no_bases_known(self):
        from chitragupta import gitview
        e = git_entry()
        for b in e["branches"]:
            b["parent"] = None
        e["prs"] = []
        src = gitview.gitgraph(e)
        self.assertIn('"mainBranchName": "base"', src)
        self.assertEqual(gitview.check(src), [])

    def test_checker_catches_mistakes(self):
        from chitragupta import gitview
        self.assertTrue(gitview.check('gitGraph\n  commit id: "a"\n  merge main\n'))
        self.assertTrue(gitview.check('gitGraph\n  checkout nowhere\n'))
        self.assertTrue(gitview.check('gitGraph\n  commit id: "a"\n  commit id: "a"\n'))
        self.assertTrue(gitview.check('gitGraph\n  commit id: "a"\n  branch x\n  checkout main\n  merge x\n'))  # same head
        self.assertTrue(gitview.check("flowchart LR\n  a --> b\n"))
        self.assertEqual(gitview.check('gitGraph\n  commit id: "a"\n  branch x\n  commit id: "b"\n  checkout main\n  merge x\n'), [])


# --------------------------------------------------------------------------- data home based

class Base(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.days = build_home(self.home)
        add_git(self.home)

    def tearDown(self):
        self.restore()


class TestTicketApi(Base):
    def test_ticket_has_git_and_graph(self):
        from chitragupta import gitview
        from chitragupta.dashboard import api
        t = api.ticket(TICKET)
        g = t["git"]
        self.assertEqual(sorted(g), ["alerts", "checked_at", "repos", "summary"])  # no internal signatures
        self.assertEqual(len(g["alerts"]), 2)
        e = g["repos"]["shop-api"]
        self.assertEqual(gitview.check(e["graph"]), [])
        self.assertEqual(e["graph_legend"], {"hotfix/ABC-1001-qa": "hotfix/ABC-1001+qa"})
        self.assertIsNone(e["graph_why"])
        self.assertEqual(len(e["rows"]), 3)
        self.assertEqual(e["prs"][0]["url"], PR_URL)
        self.assertNotIn("graph", api.read_json_cached(common.hp("data", "tickets", TICKET + ".json"))["git"]["repos"]["shop-api"],
                         "the cached record must not be mutated")
        self.assertLess(len(json.dumps(t)), 200000)
        self.assertEqual(api.ticket("ABC-1002")["git"], {})

    def test_long_matrix_is_cut(self):
        from chitragupta.dashboard import api
        tk = {"git": {"repos": {"shop-api": dict(git_entry(), rows=[dict(git_entry()["rows"][0], id="%012x" % i)
                                                                      for i in range(api.MAX_GIT_ROWS + 20)])}}}
        e = api._git(tk)["repos"]["shop-api"]
        self.assertEqual(len(e["rows"]), api.MAX_GIT_ROWS)
        self.assertEqual(e["rows_cut"], 20)

    def test_page_hooks(self):
        from chitragupta.dashboard import server
        html = support.rd(server.PAGE)
        for hook in ('id="git-alerts"', "Branches & PRs", "data-mmd-quiet", "gitBlock(t.git)", "gitAlerts(t.git)",
                     "/export?mode=", "Export (full)", "Export (shareable)", "<!-- cg:export-data -->", "cg-export",
                     "abandoned? (suggested)", "(inferred)", "PR info from merge commits", "PR info unavailable",
                     "td.dv.good", "td.dv.bad", "td.dv.open", "td.dv.check", "isHttp(p.url)", 'class="scroll"'):
            self.assertIn(hook, html)
        self.assertEqual(re.findall(r"(?i)\b(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", html), [], "no external URLs")
        # every git value goes through esc(); the PR link only for http(s) URLs
        js = html[html.index("function gitAlerts"):html.index("function exportControl")]
        self.assertNotRegex(js, r"\+ (?:a|b|p|r|c|e|t)\.(?:text|branch|title|subject|parent|source|target|why|link|url|summary) \+")


# --------------------------------------------------------------------------- export

class TestExport(Base):
    def test_full(self):
        from chitragupta import export
        name, html = export.export_ticket(TICKET, "full")
        self.assertRegex(name, r"^ABC-1001-full-\d{8}-\d{4}\.html$")
        data = embedded(html)
        self.assertEqual(data["key"], TICKET)
        self.assertEqual(data["export"]["mode"], "full")
        self.assertIn(PR_URL, html)
        for subject in ("Fix order total", "Late fix", "Add a test for the"):
            self.assertIn(subject, html)
        self.assertIn("## Worked example (QA)", data["dbflow"]["markdown"])
        self.assertIn("__esbuild_esm_mermaid_nm", html)  # diagrams -> Mermaid inlined
        self.assertIn("<title>ABC-1001 · Chitragupta export</title>", html)
        self.self_contained(html)

    def test_shareable(self):
        from chitragupta import export, gitview, redact
        from chitragupta.dashboard import server
        name, html = export.export_ticket(TICKET, "shareable")
        self.assertRegex(name, r"^ABC-1001-shareable-\d{8}-\d{4}\.html$")
        data = embedded(html)
        blob = json.dumps(data, ensure_ascii=False)
        page_only = html.replace(support.rd(server.mermaid_path()), "")
        for bad in ("https://", "http://", "shop-qa.acme.test", "git.acme.test", "C:\\", "C:/", "qa-only-body-marker",
                    "99.50", self.home.replace("\\", "/"), self.home):
            self.assertNotIn(bad, blob, bad)
        self.assertNotIn("https://", page_only)
        self.assertEqual(data["paths"], {})
        self.assertTrue(all(a["path"] is None for a in data["artifacts"]))
        self.assertIsNone(data["requests"]["bruno_folder"])
        step3 = next(s for s in data["requests"]["steps"] if s["n"] == 3)
        self.assertEqual(step3["url"], "<host>/api/orders/42")
        self.assertNotIn("--data-binary", step3["curl"])
        self.assertNotIn("Worked example (QA)", data["dbflow"]["markdown"])
        self.assertIsNone(data["dbflow"]["qa_trace"])
        e = data["git"]["repos"]["shop-api"]
        self.assertTrue(all(p["url"] is None for p in e["prs"]))
        self.assertEqual(gitview.check(e["graph"]), [])
        redact._public_cache.clear()
        for s in strings(data):
            if s is e["graph"]:
                continue
            self.assertEqual(redact.redact(s, mode="public"), s, s)
            self.assertEqual(export.scrub(s), s, s)
        self.self_contained(html)

    def self_contained(self, html):
        self.assertNotRegex(html, r"(?i)<script[^>]+src\s*=\s*[\"']?(?:https?:|//)")
        self.assertNotRegex(html, r"(?i)<link[^>]+href\s*=\s*[\"']?(?:https?:|//)")
        self.assertNotRegex(html, r"(?i)<(?:img|iframe)[^>]+src\s*=\s*[\"']?(?:https?:|//)")
        self.assertEqual(html.count('<script id="cg-export"'), 1)
        self.assertNotIn("<!-- cg:export-data -->", html)
        data = html[html.index('<script id="cg-export"'):]
        data = data[:data.index("</script>")]
        self.assertNotIn("<", data[len('<script id="cg-export" type="application/json">'):])

    def test_no_mermaid_without_diagrams(self):
        from chitragupta import export
        name, html = export.export_ticket("ABC-1002", "full")
        self.assertNotIn("__esbuild_esm_mermaid_nm", html)
        self.assertLess(len(html), 300000)
        self.assertIsNone(export.export_ticket("ABC-4242", "full"))
        with self.assertRaises(ValueError):
            export.export_ticket(TICKET, "everything")

    def test_export_to_file_stays_local(self):
        from chitragupta import export, gitsync
        p = export.export_to_file(TICKET, "shareable")
        rel = os.path.relpath(p, self.home).replace("\\", "/")
        self.assertRegex(rel, r"^tickets/ABC-1001/exports/ABC-1001-shareable-\d{8}-\d{4}\.html$")
        self.assertTrue(os.path.isfile(p))
        self.assertFalse(gitsync.allowed_path(rel))
        self.assertFalse(gitsync.allowed_path(rel, gitsync.push_paths(common.load_config())))
        with self.assertRaises(ValueError):
            export.export_to_file("ABC-4242")

    def test_shareable_does_not_touch_the_record(self):
        from chitragupta import export
        from chitragupta.dashboard import api
        before = copy.deepcopy(api.ticket(TICKET))
        export.export_ticket(TICKET, "shareable")
        self.assertEqual(api.ticket(TICKET), before)


class TestExportHttp(Base):
    def setUp(self):
        super().setUp()
        from chitragupta.dashboard import server
        self.srv, self.thread = server.start_in_thread(0)
        self.port = self.srv.port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def get(self, path, method="GET", host=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            c.putrequest(method, path, skip_host=True)
            c.putheader("Host", host if host is not None else "127.0.0.1:%d" % self.port)
            if body is not None:
                c.putheader("Content-Length", str(len(body)))
            c.endheaders(body)
            r = c.getresponse()
            return r, r.read()
        finally:
            c.close()

    def test_export_endpoint(self):
        before = snapshot(self.home)
        r, body = self.get("/api/tickets/abc-1001/export?mode=shareable")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/html"))
        self.assertRegex(r.getheader("Content-Disposition"), r'^attachment; filename="ABC-1001-shareable-\d{8}-\d{4}\.html"$')
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
        html = body.decode("utf-8")
        self.assertEqual(embedded(html)["export"]["mode"], "shareable")
        r, body = self.get("/api/ticket/ABC-1001/export?mode=full")
        self.assertEqual(r.status, 200)
        self.assertIn(PR_URL, body.decode("utf-8"))
        r, body = self.get("/api/tickets/ABC-1001/export", method="HEAD")
        self.assertEqual((r.status, body), (200, b""))
        self.assertEqual(self.get("/api/tickets/ABC-1001/export?mode=raw")[0].status, 400)
        self.assertEqual(self.get("/api/tickets/ABC-4242/export?mode=full")[0].status, 404)
        self.assertEqual(self.get("/api/tickets/..%2F..%2Fx/export")[0].status, 404)
        for bad in ("evil.test", "evil.test:%d" % self.port, ""):
            self.assertEqual(self.get("/api/tickets/ABC-1001/export?mode=full", host=bad)[0].status, 403, bad)
        self.assertEqual(self.get("/api/tickets/ABC-1001/export?mode=full", method="POST", body=b"{}")[0].status, 405)
        self.assertEqual(snapshot(self.home), before, "the export endpoint wrote into the data home")

    def test_ticket_json_has_git(self):
        r, body = self.get("/api/tickets/ABC-1001")
        t = json.loads(body)
        self.assertEqual(t["git"]["repos"]["shop-api"]["graph"].splitlines()[1], "gitGraph")


if __name__ == "__main__":
    unittest.main()
