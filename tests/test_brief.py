"""FX-20: the ticket brief. Grounded in the ticket's current items and deterministic facts, validated claim by claim,
regenerated only when its inputs change and within the daily cap; the user's notes win. A fake model stands in for
claude. Synthetic data only (ABC-1001, acme.test, shop)."""
import copy
import json
import os
import unittest

from tests.support import PY, Isolated, write_config

DAY = "2026-09-01"
T = "ABC-1001"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def it(iid, kind, title, **over):
    d = {"id": iid, "kind": kind, "title": title, "detail": "", "evidence": ["CancelService.java:88"],
         "confidence": "confirmed", "date": DAY, "time": "10:00", "session": "abcdef12", "first_date": DAY}
    d.update(over)
    return d


def ticket():
    items = [it("rc00000001", "root_cause", "Stock is released by a nightly job only", status="superseded",
                superseded_by="rc00000002", date="2026-08-30"),
             it("rc00000002", "root_cause", "Cancel never calls release() in CancelService.java"),
             it("fi00000001", "finding", "shop.reservations rows stay active after cancel"),
             it("fi00000002", "finding", "The nightly job skips cancelled orders", confidence="likely"),
             it("hw00000001", "how_it_works", "Cancel sets orders.status and returns", phase="before"),
             it("hw00000002", "how_it_works", "Cancel releases the reservation, then sets the status", phase="after"),
             it("fx00000001", "fix", "Call release() in CancelService.java before save", state="applied"),
             it("rk00000001", "risk", "Partial cancels release the full quantity"),
             it("rv00000001", "finding", "Unrelated ABC-1002 login timeout", review={"why": "names ABC-1002"})]
    return {"id": T, "title": "Cancel keeps stock reserved", "title_source": "jira", "repos": ["shop-api"],
            "branches": ["feature/ABC-1001-cancel"], "first": "2026-08-30", "last": DAY, "status_inferred": "fix_ready",
            "jira": {"type": "Bug", "status": "QA"}, "tldr": "Cancel kept stock reserved.", "tldr_date": DAY,
            "items": {i["id"]: i for i in items},
            "files": {"src/shop/CancelService.java": {"path": "src/shop/CancelService.java", "changed": True,
                                                      "verified": True, "verified_by": "commit"},
                      "src/shop/Ghost.java": {"path": "src/shop/Ghost.java", "changed": False, "verified": False}},
            "tables": {"shop.reservations": {"name": "shop.reservations", "purpose": "stock held for orders"}},
            "queries": {}, "apis": {},
            "commits": {SHA: {"sha": SHA, "short": SHA[:9], "repo_name": "shop-api", "date": DAY + "T11:00:00+05:30",
                              "ref": "feature/ABC-1001-cancel", "subject": "ABC-1001 release stock on cancel"}},
            "git": {"repos": {"shop-api": {
                "branches": [{"branch": "feature/ABC-1001-cancel", "status": "merged", "own": 1, "on_remote": True,
                              "local": True, "prs": [7]}],
                "prs": [{"number": 7, "source": "feature/ABC-1001-cancel", "target": "qa", "state": "merged",
                         "merged_at": "2026-09-01T12:00:00Z"}],
                "targets": [{"role": "qa", "name": "qa"}],
                "rows": [{"id": "r1", "shas": [SHA[:9]], "subject": "ABC-1001 release stock on cancel", "date": DAY,
                          "cells": {"qa": {"s": "in"}}}]}}, "checked_at": DAY + "T13:00:00+05:30"},
            "tests": {"te00000001": {"id": "te00000001", "at": DAY + "T11:30:00+05:30", "day": DAY,
                                     "command": "pytest tests/test_cancel.py", "status": "passed", "counts": {"passed": 4}}},
            "timeline": {}, "replay": {}}


def c(text, *cites, label=None):
    d = {"text": text, "cites": list(cites)}
    if label is not None:
        d["label"] = label
    return d


def good_output():
    return {
        "root_cause": {"confidence": "confirmed", "claims": [c("Cancel never releases the reservation.", "rc00000002")]},
        "findings": {"claims": [c("Reservation rows stay active after cancel.", "fi00000001", label=""),
                                c("The nightly job skips cancelled orders.", "fi00000002", label=""),
                                c("Ruled out: the nightly job alone.", "fi00000002", label="ruled-out")]},
        "before": {"steps": [c("Cancel sets the status and returns.", "hw00000001")]},
        "proposed": {"claims": [c("Release the reservation before saving the cancel.", "fx00000001")],
                     "rejected": c("", )},
        "fixes_made": {"summary": c("The commit releases stock on cancel.", "commit:" + SHA[:7], "pr:7")},
        "after": {"steps": [c("Cancel releases the reservation, then sets the status.", "hw00000002")],
                  "verified": {"state": "verified", "text": "The cancel tests pass.", "cites": ["test:te00000001"]}},
        "cons": {"claims": [c("Partial cancels release the full quantity.", "rk00000001", label="recorded"),
                            c("Concurrent cancels may release twice.", "fx00000001", label="suggested")]},
    }


class BriefBase(Isolated):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY},
                                 "brief": {"daily_cap": 8}, "budget": {"model_daily_usd": 3}})
        from chitragupta import common, extract, modelbudget, store
        common.write_json(store.ticket_path(T), ticket())
        common.write_json(common.hp("data", "jira", T + ".json"),
                          {"key": T, "fetched_at": DAY + "T09:00:00+05:30", "raw": {"result": "{}"},
                           "card": {"found": True, "title": "Cancel keeps stock reserved", "type": "Bug", "status": "QA",
                                    "description": "Cancel an order: stock stays reserved.",
                                    "comments": [{"id": "1", "at": DAY, "text": "Seen again on shop.acme.test"}]}})
        self.calls, self.output = [], good_output()
        real = extract.call_claude
        modelbudget._PENDING.clear()

        def fake(kind, text, cfg, timeout=300):
            self.calls.append((kind, text))
            modelbudget.note(kind, 12000, 900, 0.05, cfg)
            return copy.deepcopy(self.output), 0.05

        extract.call_claude = fake
        self.addCleanup(setattr, extract, "call_claude", real)
        self.addCleanup(modelbudget._PENDING.clear)
        self.health = {}

    def cfg(self):
        from chitragupta import common
        return common.load_config()

    def gen(self, key=T, st=None, **kw):
        from chitragupta import brief, store
        st = st or store.Store()
        res = brief.generate(key, store=st, cfg=self.cfg(), health=self.health, **kw)
        st.save()
        return res, st

    def saved(self, key=T):
        from chitragupta import common, store
        return common.read_json(store.ticket_path(key))


class CurrentItems(BriefBase, unittest.TestCase):
    def test_superseded_and_flagged_items_are_not_current(self):
        from chitragupta import brief
        ids = [i["id"] for i in brief.current_items(ticket())]
        self.assertNotIn("rc00000001", ids)  # superseded
        self.assertNotIn("rv00000001", ids)  # flagged for review
        self.assertIn("rc00000002", ids)

    def test_input_has_current_items_and_facts_never_superseded_text(self):
        from chitragupta import brief
        text = brief.build_input(T, ticket(), self.cfg())
        self.assertIn("rc00000002", text)
        self.assertNotIn("nightly job only", text)
        self.assertNotIn("login timeout", text)
        for fact in ("commit:" + SHA[:7], "pr:7", "test:te00000001", "jira:" + T, "table:shop.reservations"):
            self.assertIn(fact, text)
        self.assertIn("Seen again on shop.acme.test", text)  # a Jira comment
        self.assertNotIn('"payload"', text)  # never raw events


class Generate(BriefBase, unittest.TestCase):
    def test_conflicting_root_causes_only_the_current_one(self):
        from chitragupta import brief
        self.output["root_cause"]["claims"].append(c("A nightly job releases stock.", "rc00000001"))
        self.gen()
        b = self.saved()["brief"]
        rc = b["sections"]["root_cause"]
        self.assertEqual([x["cites"] for x in rc["claims"]], [["rc00000002"]])
        self.assertIn({"section": "root_cause", "text": "A nightly job releases stock.", "why": "superseded"}, b["dropped"])
        v = brief.view(self.saved())
        self.assertNotIn("nightly job releases", json.dumps(v))
        self.assertEqual(v["sections"][0]["heading"], "Root cause")
        self.assertEqual(v["sections"][0]["confidence"], "confirmed")

    def test_a_claim_citing_a_missing_id_is_dropped_and_logged(self):
        from chitragupta import common
        self.output["findings"]["claims"].append(c("Orders table has a trigger.", "deadbeef00", label=""))
        self.output["findings"]["claims"].append(c("No evidence at all.", label=""))
        res, _ = self.gen()
        self.assertTrue(res["ok"], res)
        dropped = self.saved()["brief"]["dropped"]
        self.assertIn({"section": "findings", "text": "Orders table has a trigger.", "why": "unknown-id"}, dropped)
        self.assertIn({"section": "findings", "text": "No evidence at all.", "why": "no-cite"}, dropped)
        log = common.read_text(common.wl("logs", "agent.log"))
        self.assertIn("brief %s: 2 claims dropped" % T, log)

    def test_an_unverified_path_never_appears(self):
        from chitragupta import brief
        self.output["findings"]["claims"].append(c("Ghost.java guards the cancel.", "fi00000001", label=""))
        self.output["before"]["steps"].append(c("The request reaches /api/v9/unknown first.", "hw00000001"))
        self.gen()
        b = self.saved()["brief"]
        whys = {d["text"]: d["why"] for d in b["dropped"]}
        self.assertEqual(whys["Ghost.java guards the cancel."], "unverified-path")
        self.assertEqual(whys["The request reaches /api/v9/unknown first."], "unknown-name")
        v = json.dumps(brief.view(self.saved()))
        self.assertNotIn("Ghost.java", v)

    def test_fixes_made_is_always_the_branch_and_pr_data(self):
        from chitragupta import brief
        self.output["fixes_made"]["summary"] = c("Three commits and PR #9 shipped it.", "commit:9999999", "pr:9")
        self.gen()
        v = brief.view(self.saved())
        fm = [s for s in v["sections"] if s["id"] == "fixes_made"][0]
        self.assertEqual([x["id"] for x in fm["items"]], ["commit:" + SHA[:7], "pr:7", "stage"])
        self.assertIsNone(fm["summary"])
        self.assertEqual(brief.fixes_made(self.saved()), fm["items"])
        # no commits and no PRs: "Not started", whatever the model wrote
        tk = self.saved()
        tk["commits"], tk["git"] = {}, {}
        fm = [s for s in brief.view(tk)["sections"] if s["id"] == "fixes_made"][0]
        self.assertEqual((fm["items"], fm["text"], fm["summary"]), ([], "Not started", None))

    def test_empty_sections_say_not_recorded_yet(self):
        from chitragupta import brief
        self.output = {"root_cause": {"confidence": "none", "claims": []}, "findings": {"claims": []},
                       "before": {"steps": []}, "proposed": {"claims": [], "rejected": c("")},
                       "fixes_made": {"summary": c("")},
                       "after": {"steps": [], "verified": {"state": "not-verified", "text": "", "cites": []}},
                       "cons": {"claims": []}}
        self.gen()
        v = brief.view(self.saved())
        by = {s["id"]: s for s in v["sections"]}
        for sid in ("findings", "before", "proposed", "after", "cons"):
            self.assertTrue(by[sid]["empty"], sid)
            self.assertEqual(by[sid]["text"], "Not recorded yet", sid)
        self.assertEqual(by["root_cause"]["text"], "Not found yet")
        self.assertEqual(by["after"]["verified"]["state"], "not-verified")

    def test_verified_needs_a_passing_test_or_replay(self):
        from chitragupta import brief
        self.output["after"]["verified"] = {"state": "verified", "text": "It works.", "cites": ["hw00000002"]}
        self.gen()
        after = [s for s in brief.view(self.saved())["sections"] if s["id"] == "after"][0]
        self.assertEqual(after["verified"]["state"], "not-verified")

    def test_cons_are_labelled_and_suggestions_need_a_fix(self):
        self.output["cons"]["claims"] += [c("Could slow down cancel.", "fi00000001", label="suggested"),
                                         c("May confuse reports.", "fx00000001", label="suggested"),
                                         c("May hit the cache.", "fx00000001", label="suggested")]
        self.gen()
        b = self.saved()["brief"]
        cons = b["sections"]["cons"]["claims"]
        self.assertEqual([x["label"] for x in cons], ["recorded", "suggested", "suggested"])
        whys = {d["text"]: d["why"] for d in b["dropped"]}
        self.assertEqual(whys["Could slow down cancel."], "suggested-without-fix")
        self.assertEqual(whys["May hit the cache."], "too-many-suggested")

    def test_brief_record_shape(self):
        self.gen()
        b = self.saved()["brief"]
        self.assertEqual((b["v"], b["kind"], b["model"]), (1, "bug", "sonnet"))
        self.assertEqual(b["tokens"], {"in": 12000, "out": 900})
        self.assertEqual(b["cost_usd"], 0.05)
        self.assertEqual(set(b["sections"]), {"root_cause", "findings", "before", "proposed", "fixes_made", "after", "cons"})
        self.assertTrue(b["source_hash"])
        self.assertEqual(self.calls[0][0], "brief")


class Kinds(BriefBase, unittest.TestCase):
    def test_story_heading_is_why_this_change(self):
        from chitragupta import brief, common, store
        tk = self.saved()
        tk["jira"]["type"] = "Story"
        common.write_json(store.ticket_path(T), tk)
        self.gen()
        self.assertIn('"heading_1":"Why this change"', self.calls[0][1])
        v = brief.view(self.saved())
        self.assertEqual(v["kind"], "story")
        self.assertEqual(v["sections"][0]["heading"], "Why this change")

    def test_no_brief_falls_back_to_root_cause_stage_and_tldr(self):
        from chitragupta import brief
        v = brief.view(ticket())
        self.assertFalse(v["has_brief"])
        self.assertEqual(v["fallback"]["root_cause"]["id"], "rc00000002")
        self.assertIn("Merged to qa", v["fallback"]["stage"])
        self.assertEqual(v["fallback"]["tldr"], "Cancel kept stock reserved.")
        self.assertEqual(v["header"]["jira_status"], "QA")
        self.assertEqual(v["header"]["status_inferred"], "fix_ready")


class Corrections(BriefBase, unittest.TestCase):
    def add_note(self, iid, text):
        from chitragupta import common, store
        tk = self.saved()
        tk["items"][iid] = it(iid, "note", text[:90], detail=text, evidence=["inbox.md"], session="inbox", time="12:00")
        common.write_json(store.ticket_path(T), tk)

    def test_a_note_correction_wins_and_survives_regeneration(self):
        from chitragupta import brief
        self.add_note("no00000001", "root cause: the cancel endpoint skips release() when the order has a coupon")
        self.gen()
        b = self.saved()["brief"]
        self.assertEqual(b["overrides"], {"root_cause": "note:no00000001"})
        rc = brief.view(self.saved())["sections"][0]
        self.assertEqual([x["text"] for x in rc["claims"]],
                         ["the cancel endpoint skips release() when the order has a coupon"])
        self.assertEqual(rc["override"], "note:no00000001")
        # new findings arrive; the model still says its own root cause; the note keeps winning
        self.add_note("no00000002", "fix: also release on partial cancel")
        self.gen()
        self.assertEqual(len(self.calls), 2)
        v = brief.view(self.saved())
        by = {s["id"]: s for s in v["sections"]}
        self.assertEqual(by["root_cause"]["claims"][0]["text"],
                         "the cancel endpoint skips release() when the order has a coupon")
        self.assertEqual(by["proposed"]["claims"][0]["text"], "also release on partial cancel")
        self.assertEqual(self.saved()["brief"]["overrides"],
                         {"root_cause": "note:no00000001", "proposed": "note:no00000002"})


class WhenToRegenerate(BriefBase, unittest.TestCase):
    def test_unchanged_inputs_no_regeneration(self):
        res, _ = self.gen()
        self.assertTrue(res["ok"])
        res, _ = self.gen()
        self.assertEqual(res["skipped"], "unchanged")
        self.assertEqual(len(self.calls), 1)

    def test_changed_inputs_make_it_stale(self):
        from chitragupta import brief, common, store
        self.gen()
        tk = self.saved()
        self.assertIsNone(brief.stale(tk))
        tk["items"]["fi00000003"] = it("fi00000003", "finding", "Coupons skip the release path", date="2026-09-02")
        common.write_json(store.ticket_path(T), tk)
        st = brief.stale(self.saved())
        self.assertEqual(st, {"since": tk["brief"]["generated_at"]})
        self.assertEqual(brief.view(self.saved())["stale"], st)
        res, _ = self.gen()
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(brief.stale(self.saved()))

    def test_daily_cap_holds(self):
        from chitragupta import common, store
        cfg = common.read_json(common.wl("config.json"))
        cfg["brief"] = {"daily_cap": 1}
        common.write_json(common.wl("config.json"), cfg)
        tk2 = ticket()
        tk2["id"] = "ABC-1002"
        common.write_json(store.ticket_path("ABC-1002"), tk2)
        self.assertTrue(self.gen()[0]["ok"])
        res, _ = self.gen("ABC-1002")
        self.assertFalse(res["ok"])
        self.assertEqual(res["skipped"], "budget")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("brief", self.saved("ABC-1002"))

    def test_spend_cap_holds(self):
        from chitragupta import common, modelbudget
        modelbudget.record(self.health, common.workday(common.now(), self.cfg()), "extract", 1, 1, 3.0)
        res, _ = self.gen()
        self.assertEqual(res["skipped"], "budget")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
