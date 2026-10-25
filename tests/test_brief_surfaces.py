"""FX-20: the ticket brief comes first on every surface that shows a ticket: the dashboard's ticket page, the hub
README, the single-file export, the SessionStart context, MCP get_ticket and the recall skill.

The surfaces render only from brief.view(tk); these tests feed a synthetic view (tests/fixtures/brief/) through a
patched brief.view, and check the fallback (no brief yet) with the real one. All data is synthetic (ABC-1001, shop-api,
*.acme.test) in a temp CHITRAGUPTA_HOME.
"""
import copy
import json
import os
import re
import unittest
from unittest import mock

from tests import support
from tests.support import Isolated
from tests.test_dashboard import build_home

TICKET = "ABC-1001"
FIX = os.path.join(support.ROOT, "tests", "fixtures", "brief")


def fixture_view():
    with open(os.path.join(FIX, TICKET + ".view.json"), encoding="utf-8") as f:
        return json.load(f)


def patched_view(v=None):
    """brief.view patched to return `v` (the ABC-1001 fixture by default) for ABC-1001 and the real view otherwise."""
    from chitragupta import brief
    real = brief.view
    v = fixture_view() if v is None else v

    def fake(tk, key=None):
        return copy.deepcopy(v) if (tk or {}).get("id") == TICKET or key == TICKET else real(tk, key)
    return mock.patch.object(brief, "view", fake)


class Base(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.days = build_home(self.home)

    def tearDown(self):
        self.restore()


# --------------------------------------------------------------------------- dashboard

class TestDashboard(Base):
    def test_api_ticket_carries_the_view(self):
        from chitragupta.dashboard import api
        with patched_view():
            t = api.ticket(TICKET)
        self.assertEqual(t["brief"], fixture_view())

    def test_fallback_without_a_brief(self):
        from chitragupta.dashboard import api
        b = api.ticket(TICKET)["brief"]
        self.assertFalse(b["has_brief"])
        self.assertFalse(b["epic"])
        self.assertEqual(b["header"]["key"], TICKET)
        fb = b["fallback"]
        self.assertEqual(fb["root_cause"]["confidence"], "confirmed")
        self.assertIn("Discount applied twice", fb["root_cause"]["text"])
        self.assertEqual(fb["root_cause"]["cites"], ["i1"])
        self.assertEqual(fb["tldr"], "Discount applied twice in OrderService.total.")

    def test_findings_carry_the_stage_two_fields(self):
        from chitragupta.dashboard import api
        p = os.path.join(self.home, "data", "tickets", TICKET + ".json")
        tk = json.loads(support.rd(p))
        tk["items"]["i4"] = {"id": "i4", "kind": "root_cause", "title": "A duplicate scheduler", "date": "2026-10-14",
                             "status": "superseded", "superseded_by": "i1", "subject": "send twice"}
        tk["items"]["i5"] = {"id": "i5", "kind": "finding", "title": "Checkout uses a cache", "date": "2026-10-14",
                             "review": {"why": "names another ticket", "suggest": "ABC-1002", "at": "2026-10-15"}}
        support.wr(p, json.dumps(tk))
        f = {i["id"]: i for i in api.ticket(TICKET)["findings"]}
        self.assertEqual((f["i4"]["status"], f["i4"]["superseded_by"]), ("superseded", "i1"))
        self.assertEqual(f["i5"]["review"]["suggest"], "ABC-1002")
        self.assertIsNone(f["i1"]["status"])

    def test_page_opens_on_the_brief(self):
        from chitragupta.dashboard import server
        app = os.path.dirname(server.PAGE)
        js = support.rd(os.path.join(app, "pages", "brief.js"))
        tickets = support.rd(os.path.join(app, "pages", "tickets.js"))
        css = support.rd(os.path.join(app, "app.css"))
        for hook in ("runAction('brief-refresh'", "New findings since", "Refresh brief", "from git and PRs", "Not recorded yet",
                     "'Recorded:'", "'Suggested:'", "'Ruled out:'", "Rejected:", "All details", "class=\"cite\"",
                     "Child tickets", "No brief yet"):
            self.assertIn(hook, js)
        self.assertIn("<${Brief}", tickets)
        self.assertIn("id=\"all-findings\"", tickets)
        self.assertIn("superseded", tickets)
        self.assertIn("Needs review", tickets)
        for rule in ("a.cite", ".claim", ".ba", "@media print"):
            self.assertIn(rule, css)
        for src in (js, tickets):
            self.assertEqual(re.findall(r"(?i)(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", src), [], "no external URLs")
            self.assertNotIn("dangerouslySetInnerHTML", src)


# --------------------------------------------------------------------------- hub README

class WithQueryIds(Base):
    def setUp(self):
        super().setUp()
        p = os.path.join(self.home, "data", "tickets", TICKET + ".json")
        tk = json.loads(support.rd(p))
        for k, q in tk["queries"].items():
            q["id"] = k  # the store keeps a query's id on the record; the dashboard fixture leaves it out
        support.wr(p, json.dumps(tk))


class TestReadme(WithQueryIds):
    def test_brief_section_golden(self):
        from chitragupta import render
        got = "\n".join(render.render_brief(fixture_view())) + "\n"
        want = support.rd(os.path.join(FIX, TICKET + ".README-brief.md"))
        self.assertEqual(got, want)

    def test_readme_starts_with_the_brief(self):
        from chitragupta import render
        from chitragupta.store import Store
        v = fixture_view()
        v["sections"][0]["claims"][0]["cites"].append("i1")
        with patched_view(v):
            md = render.render_ticket(Store(), TICKET)
        heads = re.findall(r"(?m)^## .+$", md)
        self.assertEqual(heads[0], "## Brief")
        self.assertLess(md.index("## Brief"), md.index("## TL;DR"))
        # every item line carries its anchor, and the brief's footnote points at it
        self.assertIn('- <a id="i-i1"></a>', md)
        self.assertIn("(#i-i1)", md)
        self.assertRegex(md, r"\[\^\d+\]: \[root cause, ")

    def test_fallback_without_a_brief(self):
        from chitragupta import render
        from chitragupta.store import Store
        md = render.render_ticket(Store(), TICKET)
        brief = md.split("## Brief", 1)[1].split("\n## ", 1)[0]
        self.assertIn("No brief yet", brief)
        self.assertIn("Discount applied twice", brief)
        self.assertIn("(#i-i1)", brief)


# --------------------------------------------------------------------------- single-file export

class TestExport(Base):
    def embedded(self, html):
        m = re.search(r'<script id="cg-export" type="application/json">(.*?)</script>', html, re.S)
        return json.loads(m.group(1))

    def test_full_export_carries_the_brief_and_renders_it_first(self):
        from chitragupta import export
        with patched_view():
            _name, html = export.export_ticket(TICKET, "full")
        data = self.embedded(html)
        self.assertEqual(data["brief"]["sections"][0]["id"], "root_cause")
        self.assertEqual(data["brief"]["header"]["pr"]["url"], fixture_view()["header"]["pr"]["url"])
        mods = json.loads(re.search(r'<script id="cg-modules" type="application/json">(.*?)</script>', html, re.S).group(1))
        brief_js = mods.get("@cg/app/pages/brief.js") or ""
        self.assertIn("export function Brief(", brief_js, "the brief module ships whole, not stubbed")
        overview = mods["@cg/app/pages/tickets.js"].split("function Overview(", 1)[1].split("\n}\n", 1)[0]
        self.assertLess(overview.index("<${Brief}"), overview.index("<${AllFindings}"))
        self.assertIn(".allfind { break-before: page; }", html, "print starts the details on page two")

    def test_shareable_export_redacts_the_brief(self):
        from chitragupta import export
        v = fixture_view()
        v["sections"][1]["claims"][0]["text"] = "The log at C:\\Users\\dev\\shop\\mail.log shows it; see https://wiki.acme.test/x"
        with patched_view(v):
            _name, html = export.export_ticket(TICKET, "shareable")
        b = self.embedded(html)["brief"]
        self.assertIsNone(b["header"]["pr"]["url"])
        text = b["sections"][1]["claims"][0]["text"]
        self.assertNotIn("C:\\Users", text)
        self.assertNotIn("wiki.acme.test", text)
        self.assertIn("<path>", text)


# --------------------------------------------------------------------------- SessionStart context

class TestContext(WithQueryIds):
    def add_findings(self, n):
        p = os.path.join(self.home, "data", "tickets", TICKET + ".json")
        tk = json.loads(support.rd(p))
        for k in range(n):
            tk["items"]["f%02d" % k] = {"id": "f%02d" % k, "kind": "finding", "title": "Finding number %d about the mail queue" % k,
                                        "detail": "Long detail " + "x" * 200, "date": "2026-10-%02d" % (1 + k % 20), "time": "10:00",
                                        "evidence": ["MailRetryJob.java:%d" % k]}
        tk["items"]["old"] = {"id": "old", "kind": "todo", "title": "Superseded todo", "status": "superseded", "date": "2026-10-20"}
        support.wr(p, json.dumps(tk))

    def test_brief_first_findings_fill_the_rest(self):
        from chitragupta import render
        from chitragupta.store import Store
        self.add_findings(40)
        with patched_view():
            ctx = render.render_context(Store(), TICKET)
        self.assertLessEqual(len(ctx), 4000)
        rc = ctx.index("Root cause (confirmed): The retry job resends the email")
        for part in ("Fixes made (from git and PRs): 2 commits on feature/ABC-1001", "Possible cons: Recorded: A crash can leave rows",
                     "Suggested: A second worker", "Open TODOs / questions:", "- Add a regression test"):
            self.assertGreater(ctx.index(part), rc - 1, part)
        self.assertIn("[c607fa7d13", ctx)  # evidence ids stay, for get_ticket section=brief
        self.assertNotIn("Superseded todo", ctx)
        fi = ctx.index("Recent findings:")
        self.assertGreater(fi, ctx.index("- Add a regression test"))
        shown = ctx.count("Finding number")
        self.assertTrue(1 <= shown < 40, shown)
        self.assertNotIn("…", ctx[-3:], "the findings stop on a whole line, not a cut one")

    def test_fallback_root_cause_without_a_brief(self):
        from chitragupta import render
        from chitragupta.store import Store
        ctx = render.render_context(Store(), TICKET)
        self.assertIn("What is the current root cause, and how certain is it? (confirmed) Discount applied twice", ctx)
        self.assertIn("OrderService.total adds it again",ctx)
        self.assertIn("[i1]",ctx)
        self.assertIn("Nothing verified yet", ctx)
        self.assertNotIn("Recent findings:", ctx)
        self.assertNotIn("TL;DR:",ctx)
        self.assertLessEqual(len(ctx), 4000)


# --------------------------------------------------------------------------- recall skill

class TestRecallSkill(unittest.TestCase):
    def test_answers_from_the_brief_first(self):
        body = support.rd(os.path.join(support.ROOT, "user-skill", "chitragupta", "SKILL.md"))
        for must in ("(sections: brief, findings", "get_ticket(key, section?)", "Refresh brief", "/cg:ticket <KEY> --brief",
                     "answer from the brief", "corrections win", "## Brief"):
            self.assertTrue(must in body, must)


if __name__ == "__main__":
    unittest.main()
