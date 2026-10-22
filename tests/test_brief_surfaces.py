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


if __name__ == "__main__":
    unittest.main()
