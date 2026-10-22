"""Which ticket a prompt, an event and an extracted item belong to (K2 pasted breadcrumbs and parent issue types).

Synthetic data only (tickets ABC-…, schema `shop`).
"""
import datetime as dt
import json
import os
import tempfile
import unittest

from tests.support import Isolated, rd, rmtree, run_hook, write_config

D = "2026-10-14"


def at(hh, mm, day=D):
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm)).astimezone()


def ev(event, hh, mm, sid="s1", branch=None, prompt=None, repo=None, **kw):
    e = {"ts": at(hh, mm).isoformat(timespec="seconds"), "event": event, "session_id": sid, "source": "hook",
         "branch": branch, "repo_root": repo, "cwd": repo, "payload": {}}
    if prompt is not None:
        e["payload"] = {"prompt": prompt}
    e.update(kw)
    return e


class AttrCase(Isolated, unittest.TestCase):
    def setUp(self):
        h = tempfile.mkdtemp(prefix="cg-test-")
        write_config(h, {"ticket_prefixes": ["ABC"]})
        self.isolate(h)
        self.work = tempfile.mkdtemp(prefix="cg-work-")
        self.addCleanup(rmtree, self.work)

    def card(self, key, type_, parent=""):
        from chitragupta import common
        common.write_json(common.hp("data", "jira", key + ".json"),
                          {"key": key, "card": {"found": True, "title": "t", "status": "Open", "type": type_,
                                                "parent": parent}})

    def repo(self, name, branch):
        d = os.path.join(self.work, name)
        os.makedirs(os.path.join(d, ".git"))
        with open(os.path.join(d, ".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/%s\n" % branch)
        return d


# --------------------------------------------------------------------------- K2 breadcrumbs and pasted text

class PromptTicketTests(AttrCase):
    def pt(self, text):
        from chitragupta import common
        return common.prompt_ticket(text, ["ABC"], common.load_config())

    def test_pasted_jira_breadcrumb_gives_the_last_id(self):
        paste = "<pasted_content>\nSpaces\nShop\n\nABC-1000\n\nABC-1001\n\nOrders not linked\n</pasted_content>"
        self.assertEqual(self.pt(paste), ("ABC-1001", "jira-title"))
        self.assertEqual(self.pt("ABC-1000 / ABC-1001"), ("ABC-1001", "jira-title"))
        self.assertEqual(self.pt("ABC-1000 > ABC-1001"), ("ABC-1001", "jira-title"))
        self.assertEqual(self.pt("Spaces / Shop / ABC-1000 / ABC-1001\nOrders not linked\nwhy?"), ("ABC-1001", "jira-title"))

    def test_own_words_beat_a_pasted_block(self):
        self.assertEqual(self.pt("fix ABC-1002 the same way:\n<pasted_content>\nABC-1001 stack trace\n</pasted_content>"),
                         ("ABC-1002", "prompt"))
        self.assertEqual(self.pt("ABC-1000 / ABC-1001\nnow compare with ABC-1003"), ("ABC-1003", "prompt"))
        self.assertEqual(self.pt("look at ABC-1002 and ABC-1003"), ("ABC-1002", "prompt"))
        self.assertEqual(self.pt("no ticket here"), (None, None))

    def test_parent_issue_types_never_win(self):
        self.card("ABC-1000", "Epic")
        self.assertEqual(self.pt("ABC-1000 overview"), (None, None))
        self.assertEqual(self.pt("ABC-1000 overview, then ABC-1001"), ("ABC-1001", "prompt"))
        self.card("ABC-1005", "Story", parent="ABC-1004 (Shop rework)")
        self.assertEqual(self.pt("ABC-1004 and ABC-1005"), ("ABC-1005", "prompt"))  # named as another one's parent
        self.card("ABC-1000", "Bug")
        self.assertEqual(self.pt("ABC-1000 overview"), ("ABC-1000", "prompt"))

    def test_parent_types_are_configurable(self):
        from chitragupta import common
        self.card("ABC-1000", "Feature")
        cfg = common.load_config()
        self.assertEqual(cfg["jira"]["parent_issue_types"], ["Epic", "Initiative"])
        self.assertEqual(common.prompt_ticket("ABC-1000", ["ABC"], cfg), ("ABC-1000", "prompt"))
        cfg["jira"]["parent_issue_types"] = ["feature"]
        self.assertEqual(common.prompt_ticket("ABC-1000", ["ABC"], cfg), (None, None))
        from chitragupta import settings
        clean, errors = settings.validate({"jira.parent_issue_types": ["Epic", "Initiative", "Feature Set"]})
        self.assertEqual(errors, {})
        self.assertIn("jira.parent_issue_types", settings.validate({"jira.parent_issue_types": ["<b>"]})[1])


class ParentNeverGetsWorkTests(AttrCase):
    def test_breadcrumb_attribution_and_minutes(self):
        from chitragupta import agent
        self.card("ABC-1000", "Epic")
        evs = [ev("user-prompt", 10, 0, branch="qa", prompt="ABC-1000 / ABC-1001\nOrders not linked"),
               ev("post-tool", 10, 5, branch="qa"), ev("stop", 10, 9, branch="qa"),
               ev("user-prompt", 11, 0, sid="s2", branch="qa", prompt="ABC-1000 overview please"),
               ev("post-tool", 11, 5, sid="s2", branch="qa")]
        out = agent.attribute([dict(e) for e in evs], ["ABC"])
        self.assertEqual([e["_t"] for e in out], ["ABC-1001"] * 3 + [None, None])

    def test_recompute_minutes_creates_no_parent_slot(self):
        from chitragupta import agent, common
        self.card("ABC-1000", "Epic")
        p = common.wl("raw", D, "s1.jsonl")
        os.makedirs(os.path.dirname(p))
        with open(p, "w", encoding="utf-8") as f:
            for e in (ev("user-prompt", 10, 0, branch="qa", prompt="ABC-1000 overview"), ev("post-tool", 10, 5, branch="qa"),
                      ev("stop", 10, 9, branch="qa")):
                f.write(json.dumps(e) + "\n")
        a = agent.Agent()
        a.recompute_minutes(D)
        self.assertNotIn("ABC-1000", a.store.day(D)["tickets"])

    def test_model_item_for_a_parent_goes_to_the_turn_ticket(self):
        from chitragupta import store as storemod
        from chitragupta.store import Store
        self.card("ABC-1000", "Epic")
        st = Store()
        res = {"items": [{"kind": "finding", "title": "Orders lose their link on save", "ticket": "ABC-1000"}],
               "queries": [{"text": "select 1 from shop.orders", "ticket": "ABC-1000"}]}
        storemod.merge_extraction(st, res, {"day": D, "session": "s1", "time": "10:00", "repo": None, "branch": "qa",
                                            "candidates": ["ABC-1001"]}, ["ABC"])
        self.assertNotIn("ABC-1000", st.day(D)["tickets"])
        self.assertEqual(len(st.ticket("ABC-1001")["items"]), 1)
        self.assertEqual(len(st.ticket("ABC-1001")["queries"]), 1)

    def test_hook_files_the_breadcrumb_child(self):
        repo = self.repo("shop-api", "qa")
        run_hook("user-prompt", {"session_id": "h1", "cwd": repo, "user_prompt": "ABC-1000 / ABC-1001\nwhy?"},
                 self.child_env())
        from chitragupta import common
        rows = [json.loads(l) for l in rd(common.wl("raw", common.workday(common.now(), common.load_config()),
                                                     "h1.jsonl")).splitlines()]
        self.assertEqual(rows[0]["ticket"], "ABC-1001")


if __name__ == "__main__":
    unittest.main()
