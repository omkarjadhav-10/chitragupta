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


class FixState(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY}})

    def fixes(self, st):
        return {i["title"]: i for i in st.ticket("ABC-1001")["items"].values() if i["kind"] == "fix"}

    def test_edit_of_a_named_file_applies_the_fix(self):
        from chitragupta import store
        st = store.Store()
        res = {"items": [item("fix", "Release stock in CancelService.java", detail="call release() before save"),
                         item("fix", "Retry in StockJob.java", confidence="hypothesis")],
               "files": [{"path": "src/shop/CancelService.java", "ticket": "ABC-1001", "role": "service", "changed": True}]}
        store.merge_extraction(st, res, ctx(edited=["C:/w/shop-api/src/shop/CancelService.java"]), ["ABC"])
        f = self.fixes(st)
        a = f["Release stock in CancelService.java"]
        self.assertEqual((a["state"], a["applied_by"], a["applied_file"]), ("applied", "edit", "src/shop/CancelService.java"))
        self.assertEqual(f["Retry in StockJob.java"]["state"], "proposed")

    def test_commit_of_a_named_file_applies_the_fix(self):
        from chitragupta import store
        st = store.Store()
        store.merge_extraction(st, {"items": [item("fix", "Retry in StockJob.java")]}, ctx(), ["ABC"])
        tk = st.ticket("ABC-1001")
        self.assertEqual(self.fixes(st)["Retry in StockJob.java"]["state"], "proposed")
        tk["files"]["src/shop/StockJob.java"] = {"path": "src/shop/StockJob.java", "changed": True, "verified": True,
                                                 "verified_by": "commit"}
        self.assertEqual(store.mark_applied_fixes(tk, DAY), 1)
        self.assertEqual(self.fixes(st)["Retry in StockJob.java"]["applied_by"], "commit")

    def test_a_named_but_unverified_file_does_not_apply(self):
        from chitragupta import store
        st = store.Store()
        store.merge_extraction(st, {"items": [item("fix", "Guard in Ghost.java")]}, ctx(), ["ABC"])
        tk = st.ticket("ABC-1001")
        tk["files"]["src/Ghost.java"] = {"path": "src/Ghost.java", "changed": False, "verified": None,
                                         "claimed_changed": True}
        self.assertEqual(store.mark_applied_fixes(tk, DAY), 0)


class TestRuns(Isolated, unittest.TestCase):
    """A Bash call that runs a test runner records a test on the turn's ticket: command, status, counts. No model."""

    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY}})

    def test_runner_commands(self):
        from chitragupta import testruns
        yes = ["pytest -q tests/test_cancel.py", "py -3 -m unittest tests.test_cancel", "python -m pytest -x",
               "mvn -q test -Dtest=CancelTest", "./gradlew test", "npm test", "npm run test -- cancel", "go test ./...",
               "dotnet test Shop.sln", "bru run --env QA", "cd shop && pytest"]
        no = ["git status", "cat test.txt", "npm install", "echo pytest is great > notes.txt", "ls tests"]
        self.assertEqual([c for c in yes if not testruns.is_test_command(c)], [])
        self.assertEqual([c for c in no if testruns.is_test_command(c)], [])

    def test_counts(self):
        from chitragupta import testruns
        p = testruns.parse_counts
        self.assertEqual(p("==== 2 failed, 10 passed, 1 skipped in 3.2s ===="), {"passed": 10, "failed": 2, "skipped": 1})
        self.assertEqual(p("Ran 12 tests in 0.31s\n\nFAILED (failures=1, errors=2)"), {"total": 12, "failed": 1, "errors": 2})
        self.assertEqual(p("Ran 4 tests in 0.1s\n\nOK"), {"total": 4, "passed": 4})
        self.assertEqual(p("Tests run: 9, Failures: 0, Errors: 0, Skipped: 1"), {"total": 9, "failed": 0, "errors": 0, "skipped": 1})
        self.assertEqual(p("Tests:       1 failed, 11 passed, 12 total"), {"failed": 1, "passed": 11, "total": 12})
        self.assertEqual(p("nothing to see"), {})

    def test_hook_keeps_only_numbers(self):
        from chitragupta import hook
        from chitragupta.redact import redact
        resp = {"stdout": "tests/test_cancel.py ..F\nE AssertionError: secret-ish output\n=== 1 failed, 2 passed in 0.5s ===",
                "stderr": "", "interrupted": False}
        oc = hook._outcome("Bash", {"command": "pytest tests"}, resp, self.home, redact)
        self.assertEqual(oc, {"test": {"status": "failed", "counts": {"failed": 1, "passed": 2}}})
        self.assertNotIn("AssertionError", json.dumps(oc))
        self.assertEqual(hook._outcome("Bash", {"command": "git status"}, resp, self.home, redact), {})

    def test_recorded_on_the_turns_ticket(self):
        from chitragupta import features, store
        st = store.Store()
        ag = types.SimpleNamespace(store=st, cfg={"ticket_prefixes": ["ABC"]}, state={},
                                   ensure_tslot=lambda day, t, repo=None, branch=None: st.day(day)["tickets"].setdefault(t, {}))
        f = features.Features(ag)
        ev = {"ts": "2026-09-01T10:05:00+05:30", "event": "post-tool", "_t": "ABC-1001", "_src": "prompt", "_ln": 9,
              "payload": {"tool": "Bash", "input": {"command": "py -3 -m pytest tests/test_cancel.py --token=abc"},
                          "outcome": {"test": {"status": "passed", "counts": {"passed": 3}}}}}
        f.on_events(DAY, SID, {}, [ev])
        tests = st.ticket("ABC-1001")["tests"]
        self.assertEqual(len(tests), 1)
        rec = list(tests.values())[0]
        self.assertEqual((rec["status"], rec["counts"], rec["at"], rec["day"]), ("passed", {"passed": 3}, ev["ts"], DAY))
        self.assertEqual(rec["ref"], "raw/%s/%s.jsonl:9" % (DAY, SID))
        self.assertTrue(rec["command"].startswith("py -3 -m pytest tests/test_cancel.py"))
        self.assertLessEqual(len(rec["command"]), 200)
        f.on_events(DAY, SID, {}, [dict(ev)])  # the same event again: one record
        self.assertEqual(len(st.ticket("ABC-1001")["tests"]), 1)


def jira_raw(n_comments=12):
    issue = {"key": "ABC-1001", "summary": "Cancel keeps stock reserved", "issue_type": {"name": "Bug"},
             "description": "Steps: cancel an order on shop.acme.test; the stock stays reserved. Reporter: qa@acme.test",
             "comments": [{"id": str(i), "body": ("Comment %d " % i) + "x" * 700, "created": "2026-09-0%dT10:00:00.000+0000" % (1 + i % 9),
                           "author": {"display_name": "Someone", "email": "someone@acme.test"}} for i in range(n_comments)]}
    return {"result": json.dumps(issue)}


class JiraSources(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY}})

    def test_card_sources_keep_description_and_last_ten_comments(self):
        from chitragupta import mcpjobs
        src = mcpjobs.card_sources(jira_raw())
        self.assertIn("the stock stays reserved", src["description"])
        self.assertNotIn("qa@acme.test", src["description"])  # redacted
        self.assertEqual(len(src["comments"]), 10)
        self.assertTrue(src["comments"][0]["text"].startswith("Comment 2 "))
        self.assertTrue(all(len(c["text"]) <= 500 for c in src["comments"]))
        self.assertNotIn("author", src["comments"][0])  # no names
        self.assertEqual(mcpjobs.card_sources(None), {"description": "", "comments": []})

    def test_jira_card_stores_them(self):
        from chitragupta import common, mcpjobs
        real = mcpjobs.run
        self.addCleanup(setattr, mcpjobs, "run", real)
        raw = jira_raw(3)
        mcpjobs.run = lambda *a, **k: {"out": {"found": True, "title": "Cancel keeps stock reserved", "type": "Bug",
                                                "status": "QA", "summary": "s"},
                                        "calls": [{"name": "mcp__mcp-atlassian__jira_get_issue", "input": {},
                                                   "result": json.dumps(raw)}]}
        rec = mcpjobs.jira_card("ABC-1001", common.load_config(), {})
        self.assertEqual(len(rec["card"]["comments"]), 3)
        self.assertIn("stock stays reserved", rec["card"]["description"])
        saved = common.read_json(common.hp("data", "jira", "ABC-1001.json"))
        self.assertEqual(saved["card"]["comments"], rec["card"]["comments"])


if __name__ == "__main__":
    unittest.main()
