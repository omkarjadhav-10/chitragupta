"""Gate 2: supersession sees every current candidate of a busy ticket, in batches, within the model budget.

One call was capped at 80 items, so most findings, how-it-works items and TODOs of a busy ticket were never judged.
Now a ticket with more candidates than fit gets more calls: each batch holds all its root causes, decisions, fixes
and notes plus a slice of the rest, newest first, and the budget gate is checked before every call. A fake model
stands in for claude. Synthetic data only (ABC-1001).
"""
import json
import unittest

from tests.support import PY, Isolated, write_config

T = "ABC-1001"
D = "2026-09-01"


class Batches(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY},
                                 "budget": {"model_daily_usd": 3}})
        from chitragupta import extract, modelbudget, store
        modelbudget._PENDING.clear()
        self.addCleanup(modelbudget._PENDING.clear)
        self.st = store.Store()
        tk = self.st.ticket(T)
        for i in range(5):
            self.add(tk, "rc%d" % i, "root_cause", "Root cause %d" % i, "2026-08-%02d" % (1 + i))
        for n in range(195):
            self.add(tk, "f%03d" % n, "finding", "Finding %d about shop.orders" % n, D, "%02d:%02d" % (8 + n // 60, n % 60))
        self.sent, self.usd = [], 0.05
        real = extract.call_claude

        def fake(kind, text, cfg, timeout=300):
            self.sent.append(json.loads(text))
            modelbudget.note(kind, 9000, 500, self.usd, cfg)
            return {"items": []}, self.usd
        extract.call_claude = fake
        self.addCleanup(setattr, extract, "call_claude", real)

    def add(self, tk, iid, kind, title, date, time="10:00"):
        tk["items"][iid] = {"id": iid, "kind": kind, "title": title, "detail": "", "date": date, "time": time,
                            "evidence": [], "confidence": "likely", "session": "abcdef12"}

    def test_200_candidates_take_two_calls_and_every_id_goes_once(self):
        from chitragupta import supersede
        health = {}
        cost, done = supersede.run_day(self.st, self.cfg(), health, D, tickets=[T])
        self.assertEqual(len(self.sent), 2)
        ids = [[r["id"] for r in s["items"]] for s in self.sent]
        for batch in ids:
            self.assertLessEqual(len(batch), supersede.MAX_ITEMS)
            self.assertTrue({"rc0", "rc4"} <= set(batch), "root causes go into every batch")
        seen = set(ids[0]) | set(ids[1])
        self.assertEqual(seen, set(self.st.ticket(T)["items"]))
        rest = [i for b in ids for i in b if i.startswith("f")]
        self.assertEqual(len(rest), len(set(rest)), "each finding is sent once")
        self.assertEqual(ids[0][5], "f194", "the newest of the rest first")
        self.assertAlmostEqual(cost, 0.10)

    def test_over_budget_after_the_first_batch_stops_cleanly(self):
        from chitragupta import common, supersede
        self.usd = 5.0  # the first call spends past the $3 cap
        health = {}
        cost, done = supersede.run_day(self.st, self.cfg(), health, D, tickets=[T])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(done, {T: 0})
        self.assertEqual(health["model_usage"][common.workday(common.now(), self.cfg())]["skipped"]["supersede"], 1)

    def cfg(self):
        from chitragupta import common
        return common.load_config()


if __name__ == "__main__":
    unittest.main()


class SingletonTail(Batches):
    def test_final_finding_is_classified_without_key_items(self):
        from chitragupta import supersede
        tk = self.st.ticket(T)
        tk["items"] = {}
        for n in range(161):
            self.add(tk, "f%03d" % n, "finding", "Finding %d about shop.orders" % n, D)
        supersede.run_day(self.st, self.cfg(), {}, D, tickets=[T])
        self.assertEqual({r["id"] for b in self.sent for r in b["items"]}, set(tk["items"]))
        self.assertEqual(len(self.sent), 2)
        self.assertGreaterEqual(len(self.sent[-1]["items"]), 2)
