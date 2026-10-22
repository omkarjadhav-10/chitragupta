"""K9: a delivery stage computed from branch and PR data, shown above the model's TL;DR, which never contradicts it."""
import copy
import unittest

from tests.support import PY, Isolated, write_config

DAY = "2026-09-03"


def git(state="merged", rows_release="missing", unpushed=0, on_remote=True, prs=True):
    pr = {"number": 7, "source": "feature/ABC-1001-cancel", "target": "qa", "state": state,
          "merged_at": "2026-09-02T10:00:00Z" if state == "merged" else None}
    return {"repos": {"shop-api": {
        "strategy": "qa-first",
        "branches": [{"branch": "feature/ABC-1001-cancel", "status": "merged" if state == "merged" else "active", "own": 2,
                      "unpushed": unpushed, "on_remote": on_remote, "local": True, "prs": [7] if prs else []}],
        "prs": [pr] if prs else [],
        "targets": [{"role": "qa", "name": "qa"}, {"role": "release", "name": "release/2026.09"}],
        "rows": [{"id": "r1", "shas": ["abc1234"], "subject": "ABC-1001 fix", "date": "2026-09-01",
                  "cells": {"qa": {"s": "in" if state == "merged" else ("open" if prs else "missing")},
                            "release": {"s": rows_release}}}]}},
        "summary": "Branches 1 · PRs 1 merged · Delivery ⚠ 1 commit not in release/2026.09",
        "checked_at": "2026-09-03T09:00:00+05:30"}


class Stage(unittest.TestCase):
    def test_values(self):
        from chitragupta import delivery
        self.assertIsNone(delivery.stage({}))
        self.assertEqual(delivery.stage({"repos": {"shop-api": {"branches": [], "prs": []}}})["kind"], "not started")
        s = delivery.stage(git(prs=False, unpushed=2, on_remote=False))
        self.assertEqual(s["kind"], "committed")
        self.assertIn("2 commits not pushed", s["text"])
        self.assertEqual(delivery.stage(git(prs=False))["kind"], "pushed")
        s = delivery.stage(git(state="open"))
        self.assertEqual(s["kind"], "pr open")
        self.assertIn("PR #7 open → qa", s["text"])
        s = delivery.stage(git())
        self.assertEqual(s["kind"], "merged")
        self.assertIn("Merged to qa (PR #7, 2026-09-02)", s["text"])
        self.assertIn("not yet in release/2026.09", s["text"])
        s = delivery.stage(git(rows_release="in"))
        self.assertEqual(s["kind"], "in release")
        self.assertNotIn("not yet", s["text"])

    def test_contradicting_sentence_is_marked(self):
        from chitragupta import delivery
        st = delivery.stage(git())
        out = delivery.check_tldr("Cancel releases the stock now. The fix is ready but the commit is pending. "
                                  "It is not yet on the QA server.", st)
        self.assertEqual([o for _, o in out], [None, "outdated: PR #7 merged 2026-09-02", "outdated: PR #7 merged 2026-09-02"])
        # no merged PR: nothing to contradict
        self.assertEqual([o for _, o in delivery.check_tldr("Commit pending.", delivery.stage(git(prs=False)))], [None])


class Surfaces(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY}})

    def ticket(self, st):
        tk = st.ticket("ABC-1001")
        tk.update(git=copy.deepcopy(git()), tldr="Fix written for the cancel flow. Commit pending, PR not raised yet.",
                  tldr_date=DAY, first=DAY, last=DAY)
        return tk

    def test_hub_and_context_put_the_stage_first_and_mark_the_tldr(self):
        from chitragupta import render, store
        st = store.Store()
        self.ticket(st)
        md = render.render_ticket(st, "ABC-1001")
        self.assertIn("Delivery (computed from git and PRs", md)
        self.assertLess(md.index("Merged to qa (PR #7, 2026-09-02)"), md.index("## TL;DR"))
        self.assertIn("outdated: PR #7 merged 2026-09-02", md)
        self.assertIn("Fix written for the cancel flow.", md)
        ctx = render.render_context(st, "ABC-1001")
        self.assertLess(ctx.index("Delivery (computed"), ctx.index("TL;DR"))
        self.assertIn("outdated: PR #7 merged 2026-09-02", ctx)

    def test_dashboard_and_consolidation_get_the_stage(self):
        from chitragupta import agent, common, store
        from chitragupta.dashboard import api
        st = store.Store()
        self.ticket(st)
        st.day(DAY)["tickets"]["ABC-1001"] = {"minutes": 30, "item_ids": [], "query_ids": [], "what_i_did": []}
        st.touch_ticket("ABC-1001")
        st.touch_day(DAY)
        st.save()
        t = api.ticket("ABC-1001")
        self.assertEqual(t["delivery"]["kind"], "merged")
        self.assertEqual(t["tldr_check"][1]["outdated"], "outdated: PR #7 merged 2026-09-02")
        common.load_config()
        a = agent.Agent()
        self.assertIn("Merged to qa (PR #7, 2026-09-02)", a.consolidation_input(DAY))
        self.assertIn("delivery_stage", common.read_text(common.prompt_path("consolidate.md")))


if __name__ == "__main__":
    unittest.main()
