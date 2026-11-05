"""Gate 2: an Epic's hub, dashboard page and export show its children, not the knowledge left from before.

A parent issue (jira.parent_issue_types) has its items archived as its children's work, but its TL;DR, problem,
tables, APIs, integrations, code map, queries and DB flow still showed child work as the Epic's own. Now the hub is
the Epic summary (each child with one line and its status) plus a collapsed "Archived (n)", and the dashboard API and
the export leave the knowledge tabs empty with a short note. Synthetic data only (ABC-1000 Epic, ABC-1001 child).
"""
import json
import os
import re
import unittest

from tests import test_brief as tb

EPIC = "ABC-1000"


class EpicPages(tb.BriefBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from chitragupta import common, store
        archived = {"at": "2026-09-02T10:00:00", "reason": "parent issue: work belongs to its child tickets"}
        common.write_json(store.ticket_path(EPIC), {
            "id": EPIC, "title": "Shop stock", "title_source": "jira", "jira": {"type": "Epic", "status": "In Progress"},
            "tldr": "Stock is held after cancel (child work).", "problem": "Cancel keeps stock reserved (child work).",
            "items": {"ep00000001": {"id": "ep00000001", "kind": "finding", "title": "Reservation rows stay",
                                     "date": tb.DAY, "archived": archived}},
            "tables": {"shop.reservations": {"name": "shop.reservations", "purpose": "held stock"}},
            "apis": {"POST /orders/{id}/cancel": {"method": "POST", "path": "/orders/{id}/cancel"}},
            "integrations": {"queue|stock": {"system": "queue", "artifact": "stock", "notes": "child"}},
            "files": {"src/shop/CancelService.java": {"path": "src/shop/CancelService.java", "changed": True,
                                                      "verified": True, "verified_by": "edit"}},
            "queries": {"q1": {"id": "q1", "text": "select * from shop.reservations", "date": tb.DAY, "sent": True}},
            "data_flow": {"x": {"table": "shop.reservations"}},
            "timeline": {tb.DAY: {"minutes": 30, "line": "looked at the epic"}}})
        tk = self.saved("ABC-1001")
        tk["jira"]["parent"] = EPIC
        common.write_json(store.ticket_path("ABC-1001"), tk)

    def test_hub_is_the_epic_summary(self):
        from chitragupta import render, store
        md = render.render_ticket(store.Store(), EPIC)
        self.assertIn("### Child tickets", md)
        self.assertIn("[ABC-1001](../ABC-1001/README.md) — Cancel keeps stock reserved _(QA)_", md)
        self.assertIn("Archived (1)", md)
        self.assertIn("Reservation rows stay", md.split("Archived (1)", 1)[1])
        for gone in ("## TL;DR", "## Problem", "## Database", "## APIs", "## Integrations", "## Code map",
                     "queries.md", "DB flow", "child work", "shop.reservations", "/orders/{id}/cancel"):
            self.assertNotIn(gone, md, gone)
        self.assertIn("## My notes", md)
        q = render.render_queries(store.Store(), EPIC)
        self.assertIn("Epic", q)
        self.assertNotIn("select", q)

    def test_api_and_export_leave_the_knowledge_tabs_empty(self):
        from chitragupta import export
        from chitragupta.dashboard import api
        t = api.ticket(EPIC)
        self.assertTrue(t["epic"])
        self.assertIn("child tickets", t["epic_note"])
        self.assertEqual((t["queries"], t["requests"]["steps"], t["dbflow"]["markdown"], t["files"], t["tldr"],
                          t["problem"]), ([], [], "", [], "", ""))
        self.assertTrue(t["brief"]["epic"])
        self.assertEqual([f["id"] for f in t["findings"]], ["ep00000001"])  # archived, one click away
        child = api.ticket("ABC-1001")
        self.assertFalse(child.get("epic"))
        _name, html = export.export_ticket(EPIC, "full")
        data = json.loads(re.search(r'<script id="cg-export" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertTrue(data["epic"])
        self.assertEqual(data["queries"], [])

    def test_page_shows_the_note_in_the_knowledge_tabs(self):
        from chitragupta.dashboard import server
        js = open(os.path.join(os.path.dirname(server.PAGE), "pages", "tickets.js"), encoding="utf-8").read()
        self.assertIn("t.epic", js)
        self.assertIn("epic_note", js)


if __name__ == "__main__":
    unittest.main()
