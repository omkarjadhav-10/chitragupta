"""The existing brief call also chooses identified core records and flow scenarios."""
import json
import unittest
from tests.test_brief import BriefBase, T, it


class BriefPicks(BriefBase, unittest.TestCase):
    def test_one_call_picks_known_records_and_rejects_ghosts(self):
        from chitragupta import brief, store
        st = store.Store()
        tk = st.ticket(T)
        tk["queries"] = {"q1": {"id": "q1", "text": "select state from shop.orders", "purpose": "Cancel state", "revealed": "Reserved", "sent": True}}
        tk["tables"]["shop.orders"] = {"name": "shop.orders"}
        tk["apis"]["GET /orders"] = {"path": "/orders", "purpose": "Cancel state"}
        tk["data_flow"] = {"f1": {"scenario": "cancel", "component": "CancelService.release", "table": "shop.orders", "operation": "update"}}
        self.output["relevance"] = {"queries": [{"id": "q1", "role": "cause"}, {"id": "ghost", "role": "verify"}],
              "tables": ["shop.orders"], "apis": ["GET /orders"], "scenarios": [{"id": "cancel", "relevance": "core"}]}
        res = brief.generate(T, store=st, cfg=self.cfg(), health=self.health)
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.calls), 1)
        data = json.loads(self.calls[0][1])
        self.assertEqual(data["relevance_candidates"]["queries"][0]["id"], "q1")
        self.assertEqual(tk["queries"]["q1"]["role"], "cause")
        self.assertTrue(tk["brief"]["relevance_dropped"])
        self.assertIsNone(brief.stale(tk, T, self.cfg()))
        self.assertNotIn("ghost", tk["queries"])

    def test_dbflow_input_has_problem_current_cause_and_fix(self):
        from chitragupta import dbflow
        from tests.test_brief import ticket
        tk = ticket()
        tk["problem"] = "Cancelled stock remains reserved"
        data = json.loads(dbflow.consolidation_input(T, tk))
        self.assertEqual(data["problem"], tk["problem"])
        self.assertTrue(data["root_cause"])
        self.assertTrue(data["fix"])
        self.assertNotIn("nightly job only", json.dumps(data["root_cause"]))
