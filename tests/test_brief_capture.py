"""FX-20 inputs: what the extractor and the deterministic capture record for the ticket brief: before/after on
how-it-works items, the raw line each item came from, risks, fix state, test runs and the Jira description and
comments. Synthetic data only (ABC-1001, acme.test, shop)."""
import json
import os
import types
import unittest

from tests.support import PY, Isolated, write_config

DAY = "2026-09-01"
SID = "abcdef1234567890"


def ctx(**over):
    c = {"day": DAY, "session": SID, "time": "10:00", "repo": "C:/w/shop-api",
         "branch": "feature/ABC-1001-cancel-order", "candidates": ["ABC-1001"]}
    c.update(over)
    return c


def item(kind, title, **over):
    it = {"kind": kind, "ticket": "ABC-1001", "title": title, "detail": "", "evidence": [], "confidence": "likely",
          "time": "10:00", "updates": None, "phase": None, "line": None}
    it.update(over)
    return it


class ExtractSchema(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY}})

    def test_schema_has_phase_line_and_risk(self):
        from chitragupta import common, store
        with open(common.prompt_path("schemas/extract.json"), encoding="utf-8") as f:
            sch = json.load(f)["properties"]["items"]["items"]
        self.assertIn("risk", sch["properties"]["kind"]["enum"])
        self.assertIn("phase", sch["required"])
        self.assertIn("line", sch["required"])
        self.assertIn("risk", store.ITEM_KINDS)
        prompt = common.read_text(common.prompt_path("extract.md"))
        self.assertIn('"phase"', prompt)
        self.assertIn("[HH:MM Ln]", prompt)

    def test_read_lines_numbers_every_line(self):
        from chitragupta import agent
        p = os.path.join(self.home, "raw.jsonl")
        rows = [{"ts": "2026-09-01T10:0%d:00+05:30" % i, "event": "stop", "payload": {"message": "m%d" % i}} for i in range(3)]
        with open(p, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        evs, off = agent.read_lines(p, 0)
        self.assertEqual([e["_ln"] for e in evs], [1, 2, 3])
        first = len(json.dumps(rows[0])) + 1
        evs, _ = agent.read_lines(p, first)
        self.assertEqual([e["_ln"] for e in evs], [2, 3])

    def test_digest_lines_carry_their_raw_line(self):
        from chitragupta import extract
        ev = {"ts": "2026-09-01T10:00:00+05:30", "event": "user-prompt", "payload": {"prompt": "why does cancel fail"},
              "_ln": 7, "_t": "ABC-1001", "_src": "prompt"}
        d = extract.build_digests([ev], {"session": SID, "day": DAY}, ["ABC-1001"], {})[0]
        self.assertIn("[10:00 L7] PROMPT: why does cancel fail", d)

    def test_merge_keeps_phase_ref_and_risk(self):
        from chitragupta import store
        st = store.Store()
        res = {"items": [item("how_it_works", "Cancel releases stock in a job", phase="before", line=12),
                         item("how_it_works", "Cancel releases stock at once", phase="after"),
                         item("risk", "Double release when the job also runs", line=40),
                         item("finding", "Odd phase dropped", phase="sideways")]}
        store.merge_extraction(st, res, ctx(), ["ABC"])
        items = {i["title"]: i for i in st.ticket("ABC-1001")["items"].values()}
        a = items["Cancel releases stock in a job"]
        self.assertEqual((a["phase"], a["ref"]), ("before", "raw/%s/%s.jsonl:12" % (DAY, SID)))
        b = items["Cancel releases stock at once"]
        self.assertEqual(b["phase"], "after")
        self.assertNotIn("ref", b)
        self.assertEqual(items["Double release when the job also runs"]["kind"], "risk")
        self.assertNotIn("phase", items["Odd phase dropped"])


if __name__ == "__main__":
    unittest.main()
