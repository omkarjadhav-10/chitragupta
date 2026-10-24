"""Gate 2: every extracted item points at a raw line (`ref`).

Real data had no `ref` on any item: a prompt and schema override in <home>/.worklog/prompts, written for an older
version, had no per-item "line", so the model never sent one. A stale override (one that lacks a field the packaged
schema requires) is now ignored together with its prompt, and an item without a line gets the raw line of the turn it
happened in (its prompt line). Synthetic data only (ABC-1001).
"""
import json
import os
import unittest

from tests.support import PY, Isolated, write_config

DAY = "2026-09-01"
SID = "abcdef1234567890"
T = "ABC-1001"


def item(title, **over):
    it = {"kind": "finding", "ticket": T, "title": title, "detail": "", "evidence": [], "confidence": "likely",
          "time": "10:00", "updates": None, "phase": None}  # no "line": an older schema or a model that left it out
    it.update(over)
    return it


class Base(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY}})


class TurnLineFallback(Base):
    def events(self):
        def ev(hhmm, n, event="user-prompt", **p):
            return {"ts": "%sT%s:00+05:30" % (DAY, hhmm), "event": event, "payload": p or {"prompt": "why " + hhmm},
                    "_ln": n, "_t": T, "_src": "prompt"}
        return [ev("10:00", 3), ev("10:05", 4, "stop", message="cancel skips release()"),
                ev("10:30", 9), ev("10:40", 11, "stop", message="the job runs nightly")]

    def test_digest_turns_carry_their_prompt_line(self):
        from chitragupta import extract
        d = extract.build_digests(self.events(), {"session": SID, "day": DAY}, [T], {})[0]
        self.assertEqual([(t[0], t[3]) for t in d.turns], [("10:00", 3), ("10:30", 9)])

    def test_an_item_without_a_line_gets_its_turns_line(self):
        from chitragupta import extract, store
        d = extract.build_digests(self.events(), {"session": SID, "day": DAY}, [T], {})[0]
        st = store.Store()
        ctx = {"day": DAY, "session": SID, "time": "10:00", "repo": None, "branch": None, "candidates": [T],
               "default": d.default, "turns": d.turns}
        res = {"items": [item("Cancel skips release", time="10:06"), item("The job runs nightly", time="10:41"),
                         item("Stated with a date", time="2026-09-01 10:35"),
                         item("The model gave a line", time="10:41", line=11), item("No time at all", time="")]}
        store.merge_extraction(st, res, ctx, ["ABC"])
        refs = {i["title"]: i.get("ref") for i in st.ticket(T)["items"].values()}
        base = "raw/%s/%s.jsonl:" % (DAY, SID)
        self.assertEqual(refs["Cancel skips release"], base + "3")
        self.assertEqual(refs["The job runs nightly"], base + "9")
        self.assertEqual(refs["Stated with a date"], base + "9")
        self.assertEqual(refs["The model gave a line"], base + "11")
        self.assertIsNone(refs["No time at all"])  # no time: no turn to point at, rather than a wrong one


class StaleOverride(Base):
    def override(self, name, data):
        p = os.path.join(self.home, ".worklog", "prompts", *name.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(data) if not isinstance(data, str) else data)
        return p

    def packaged(self, name):
        from chitragupta import common
        return os.path.join(common.PROMPTS_DIR, *name.split("/"))

    def test_an_override_schema_without_the_line_field_is_ignored_with_its_prompt(self):
        from chitragupta import common
        with open(self.packaged("schemas/extract.json"), encoding="utf-8") as f:
            sch = json.load(f)
        it = sch["properties"]["items"]["items"]
        for k in ("line", "phase"):
            it["properties"].pop(k)
            it["required"].remove(k)
        self.override("schemas/extract.json", sch)
        self.override("extract.md", "An old prompt without the line rule.")
        self.assertEqual(common.prompt_path("schemas/extract.json"), self.packaged("schemas/extract.json"))
        self.assertEqual(common.prompt_path("extract.md"), self.packaged("extract.md"))
        self.assertIn("stale", common.read_text(common.wl("logs", "agent.log")))

    def test_a_current_override_still_wins(self):
        from chitragupta import common
        with open(self.packaged("schemas/extract.json"), encoding="utf-8") as f:
            sch = json.load(f)
        sch["properties"]["items"]["items"]["properties"]["title"]["maxLength"] = 100
        p = self.override("schemas/extract.json", sch)
        q = self.override("extract.md", "My own extraction prompt.")
        self.assertEqual(common.prompt_path("schemas/extract.json"), p)
        self.assertEqual(common.prompt_path("extract.md"), q)


if __name__ == "__main__":
    unittest.main()
