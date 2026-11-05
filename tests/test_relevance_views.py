"""Focused views retain full evidence in generated companion files."""
import unittest
from tests.test_brief_surfaces import WithQueryIds, TICKET


class FocusedViews(WithQueryIds):
    def prepared(self):
        from chitragupta import relevance, store
        st = store.Store()
        tk = st.ticket(TICKET)
        tk["queries"] = {"q1": {"id": "q1", "text": "select state from shop.orders", "purpose": "Cancel state", "revealed": "Reserved", "sent": True},
                          "q2": {"id": "q2", "text": "select * from information_schema.tables", "purpose": "Discover schema", "revealed": "Many", "sent": True}}
        relevance.classify(tk)
        return st, tk

    def test_hub_shows_core_and_collapsed_links(self):
        from chitragupta import render
        st, tk = self.prepared()
        md = render.render_ticket(st, TICKET)
        self.assertIn("## Recorded queries (not yet ranked)", md)
        self.assertIn("Cancel state", md)
        self.assertNotIn("Discover schema", md)
        self.assertIn("All recorded queries (2)", md)
        self.assertIn("queries-recorded.md", md)
        self.assertLess(len(md.encode("utf-8")), 40000)

    def test_queries_show_key_first_and_keep_full_companion(self):
        from chitragupta import render
        st, tk = self.prepared()
        md = render.render_queries(st, TICKET)
        self.assertIn("Cancel state", md)
        self.assertNotIn("information_schema.tables", md)
        self.assertIn("All recorded (2)", md)
        allmd = render.render_recorded_queries(st, TICKET)
        self.assertIn("information_schema.tables", allmd)
        self.assertIn("noise", allmd)
        self.assertLess(len(md.encode("utf-8")), 15000)

    def test_global_catalogs_hide_noise_without_changing_catalog(self):
        import copy
        from chitragupta import common, relevance, render, store
        from chitragupta.dashboard import api
        st, tk = self.prepared()
        tk["tables"]["shop.sessions"] = {"name": "shop.sessions", "relevance": "noise", "hidden_reason": "other ticket"}
        tk["tables"]["shop.orders"] = {"name": "shop.orders", "relevance": "core"}
        cat = st.catalog()
        cat["tables"] = {k: dict(v, tickets=[TICKET]) for k,v in tk["tables"].items()}
        before = copy.deepcopy(cat)
        md = render.render_catalogs(st)["db-tables.md"]
        self.assertIn("shop.orders", md)
        self.assertNotIn("shop.sessions", md)
        self.assertEqual(cat, before)
        common.write_json(store.ticket_path(TICKET), tk)
        common.write_json(store.catalog_path(), cat)
        self.assertNotIn("shop.sessions", str(api.knowledge()["tables"]))

    def test_ticket_api_is_compact_and_loads_recorded_details_on_demand(self):
        from unittest import mock
        from chitragupta import common, store
        from chitragupta.dashboard import api
        st, tk = self.prepared()
        tk["items"]["old"] = {"id": "old", "kind": "finding", "title": "Prior claim", "detail": "x" * 300000,
                                 "archived": {"reason": "owner archive"}}
        common.write_json(store.ticket_path(TICKET), tk)
        with mock.patch.object(api, "_read_capped", side_effect=AssertionError("eager hub read")):
            data = api.ticket(TICKET)
        self.assertTrue(data["lazy"])
        self.assertEqual([q["id"] for q in data["queries"]], ["q1"])
        self.assertNotIn("old", [i["id"] for i in data["findings"]])
        self.assertEqual(data.get("readme"), "")
        self.assertEqual(data["record_counts"]["queries"], 2)
        self.assertLess(len(__import__("json").dumps(data).encode("utf-8")), 250000)
        self.assertEqual(len(api.ticket_detail(TICKET, "queries")["queries"]), 2)
        self.assertIn("old", [i["id"] for i in api.ticket_detail(TICKET, "findings")["findings"]])

    def test_db_map_does_not_restore_noise_from_schema_cards(self):
        from chitragupta import common, store
        from chitragupta.dashboard import dbmap
        st, tk = self.prepared()
        tk["tables"] = {"shop.orders": {"name": "shop.orders", "relevance": "core"},
                         "shop.sessions": {"name": "shop.sessions", "relevance": "noise"}}
        common.write_json(store.ticket_path(TICKET), tk)
        cat = {"tables": {k: dict(v, tickets=[TICKET]) for k,v in tk["tables"].items()}}
        common.write_json(store.catalog_path(), cat)
        common.write_json(common.hp("data", "db-schema", "_linkmap.json"),
            {"result": {"hierarchy": [{"database": "shop", "schema": "shop", "tables": ["orders", "sessions"]}], "links": []}})
        names = [t["key"] for d in dbmap.tree()["databases"] for s in d["schemas"] for t in s["tables"]]
        self.assertIn("shop.orders", names)
        self.assertNotIn("shop.sessions", names)

    def test_picked_role_order_survives_api_projection(self):
        from chitragupta import common, relevance, store
        from chitragupta.dashboard import api
        st, tk = self.prepared()
        tk["queries"] = {
            "verify": {"id": "verify", "text": "select state from shop.orders", "revealed": "Passed", "sent": True,
                       "date": "2026-09-02", "role": "verify", "relevance_source": "brief", "relevance": "core"},
            "cause": {"id": "cause", "text": "select owner from shop.orders", "revealed": "Missing", "sent": True,
                       "date": "2026-09-01", "role": "cause", "relevance_source": "brief", "relevance": "core"}}
        common.write_json(store.ticket_path(TICKET), tk)
        self.assertEqual([q["id"] for q in api.ticket(TICKET)["queries"]], ["cause", "verify"])
        self.assertIn("## Key queries", render_md(st))

    def test_key_and_context_lists_keep_eight_defaults_but_five_hub_headlines(self):
        from chitragupta import common, relevance, render, store, mcpserver
        from chitragupta.dashboard import api
        st, tk = self.prepared()
        tk["queries"] = {str(i): {"id": str(i), "text": "select col%d from shop.orders" % i,
              "purpose": "Synthetic query %d" % i, "revealed": "Reserved", "sent": True} for i in range(9)}
        relevance.apply_picks(tk, {"queries": [{"id": str(i), "role": "cause" if i < 5 else "context"} for i in range(8)]})
        common.write_json(store.ticket_path(TICKET), tk)
        hub, queries = render.render_ticket(st, TICKET), render.render_queries(st, TICKET)
        self.assertNotIn("Synthetic query 5", hub)
        self.assertIn("## Also used", queries)
        self.assertIn("Synthetic query 7", queries)
        self.assertNotIn("Synthetic query 8", queries)
        data = api.ticket(TICKET)
        self.assertEqual(len(data["queries"]), 8)
        self.assertEqual([q["role"] for q in data["queries"]], ["cause"] * 5 + ["context"] * 3)
        lines = "\n".join(mcpserver._summary_lines(data))
        self.assertIn("Also used:", lines)
        self.assertIn("Synthetic query 7", lines)
        # Before picks, eight queries are honestly labelled as unranked.
        for q in tk["queries"].values():
            for k in ("role", "relevance_source", "relevance"): q.pop(k, None)
        relevance.classify(tk)
        groups = relevance.query_groups(tk)
        self.assertEqual(groups[0][0], "Recorded queries (not yet ranked)")
        self.assertEqual(len(groups[0][1]), 8)
        self.assertNotIn("Also used", render.render_queries(st, TICKET))

    def test_focused_epic_query_companion_and_context_keep_child_evidence_out(self):
        from chitragupta import render
        st, tk = self.prepared()
        tk["jira"] = {"type": "Epic"}
        self.assertNotIn("Cancel state", render.render_queries(st, TICKET))
        self.assertIn(render.EPIC_NOTE, render.render_queries(st, TICKET))
        self.assertNotIn("Cancel state", render.render_context(st, TICKET))
        self.assertIn("Cancel state", render.render_recorded_queries(st, TICKET))
        self.assertEqual(len(tk["queries"]), 2)

    def test_focused_epic_api_core_records_stay_under_children(self):
        from chitragupta import common, store
        from chitragupta.dashboard import api
        st, tk = self.prepared()
        tk["jira"] = {"type": "Epic"}
        tk["tables"] = {"shop.orders": {"name": "shop.orders", "relevance": "core"}}
        tk["apis"] = {"GET /orders": {"method": "GET", "path": "/orders", "relevance": "core"}}
        common.write_json(store.ticket_path(TICKET), tk)
        data = api.ticket(TICKET)
        self.assertEqual(data["core"], {"tables": [], "apis": []})
        self.assertEqual(data["queries"], [])


def render_md(st):
    from chitragupta import render
    return render.render_ticket(st, TICKET)
