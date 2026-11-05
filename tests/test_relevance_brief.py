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


class QuerySelectionContract(unittest.TestCase):
    def test_early_sent_cause_payload_has_date_sent_and_bounded_sql(self):
        from chitragupta import relevance
        tk = {"queries": {"early": {"id": "early", "date": "2026-09-01", "sent": True,
              "text": "select  state\n  from shop.orders " + " " * 20 + "where note = '" + "x" * 200 + "'",
              "purpose": "First cause check", "revealed": "Reserved after cancellation"}}}
        q = relevance.candidates(tk)["queries"][0]
        self.assertEqual(q["date"], "2026-09-01")
        self.assertIs(q["sent"], True)
        self.assertEqual(q["sql"], " ".join(tk["queries"]["early"]["text"].split())[:160])
        self.assertEqual(len(q["sql"]), 160)

    def test_eight_pick_schema_and_five_key_three_context_validation(self):
        from pathlib import Path
        from chitragupta import relevance
        schema = json.loads(Path("chitragupta/prompts/schemas/brief.json").read_text())
        queries = schema["properties"]["relevance"]["properties"]["queries"]
        self.assertEqual(queries["maxItems"], 8)
        self.assertIn("context", queries["items"]["properties"]["role"]["enum"])
        tk = {"id": "ABC-1001", "queries": {str(i): {"id": str(i), "text": "select col%d from shop.orders" % i,
              "revealed": "Reserved", "sent": True} for i in range(10)}}
        picks = [{"id": str(i), "role": "cause"} for i in range(6)]
        picks += [{"id": str(i), "role": "context"} for i in range(6, 10)]
        dropped = relevance.apply_picks(tk, {"queries": picks})
        groups = relevance.query_groups(tk)
        self.assertEqual([(h, len(qs)) for h, qs in groups], [("Key queries", 5), ("Also used", 3)])
        self.assertEqual({q["id"] for _, qs in groups for q in qs}, {"0", "1", "2", "3", "4", "6", "7", "8"})
        self.assertEqual({d["id"] for d in dropped}, {"5", "9"})
        # A duplicate cannot consume a slot or change the first accepted role.
        relevance.apply_picks(tk, {"queries": [{"id": "0", "role": "cause"}, {"id": "0", "role": "context"}]})
        self.assertEqual(tk["queries"]["0"]["role"], "cause")
