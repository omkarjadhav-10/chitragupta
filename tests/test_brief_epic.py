"""FX-20: an Epic (a jira.parent_issue_types card) gets no brief; its page is a summary of its children: each child's
key, title, status and one-line root cause or goal. Children are tickets whose Jira card names the Epic as parent, or
that a pasted breadcrumb put under it. Synthetic data only."""
import types
import unittest

from tests import test_brief as tb

DAY = tb.DAY


class Epic(tb.BriefBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from chitragupta import common, store
        common.write_json(store.ticket_path("ABC-1000"), {"id": "ABC-1000", "title": "Shop stock", "title_source": "jira",
                                                         "jira": {"type": "Epic", "status": "In Progress"}, "items": {}})
        tk = self.saved("ABC-1001")
        tk["jira"]["parent"] = "ABC-1000 (Shop stock, Epic)"
        common.write_json(store.ticket_path("ABC-1001"), tk)
        common.write_json(store.ticket_path("ABC-1002"), {"id": "ABC-1002", "title": "Show stock on the cart",
                                                         "title_source": "jira", "status_inferred": "investigating",
                                                         "jira": {"type": "Story", "status": "To Do"},
                                                         "crumb_parents": ["ABC-1000"],
                                                         "problem": "The cart should show live stock. It is cached now.",
                                                         "items": {}})
        common.write_json(store.ticket_path("ABC-1003"), {"id": "ABC-1003", "title": "Other", "title_source": "jira",
                                                         "jira": {"type": "Bug", "status": "Done", "parent": "ABC-2000"},
                                                         "items": {}})

    def test_epic_view_is_the_children_summary(self):
        from chitragupta import brief
        v = brief.view(self.saved("ABC-1000"))
        self.assertTrue(v["epic"])
        self.assertEqual(v["kind"], "epic")
        self.assertNotIn("sections", v)
        ch = {c["key"]: c for c in v["children"]}
        self.assertEqual(sorted(ch), ["ABC-1001", "ABC-1002"])
        self.assertEqual(ch["ABC-1001"]["title"], "Cancel keeps stock reserved")
        self.assertEqual(ch["ABC-1001"]["status"], "QA")
        self.assertEqual(ch["ABC-1001"]["line"], "Cancel never calls release() in CancelService.java")
        self.assertEqual(ch["ABC-1002"]["line"], "The cart should show live stock.")
        self.assertEqual(ch["ABC-1002"]["status"], "To Do")

    def test_a_childs_brief_gives_its_line(self):
        from chitragupta import brief
        self.gen("ABC-1001")
        ch = {c["key"]: c for c in brief.view(self.saved("ABC-1000"))["children"]}
        self.assertEqual(ch["ABC-1001"]["line"], "Cancel never releases the reservation.")

    def test_no_model_call_for_an_epic(self):
        res, _ = self.gen("ABC-1000")
        self.assertTrue(res.get("epic"))
        self.assertEqual(self.calls, [])
        self.assertNotIn("brief", self.saved("ABC-1000"))

    def test_breadcrumb_records_the_parent(self):
        from chitragupta import features, store
        st = store.Store()
        ag = types.SimpleNamespace(store=st, cfg={"ticket_prefixes": ["ABC"]}, state={},
                                   ensure_tslot=lambda day, t, repo=None, branch=None: st.day(day)["tickets"].setdefault(t, {}))
        ev = {"ts": DAY + "T10:00:00+05:30", "event": "user-prompt", "_t": "ABC-1004", "_src": "jira-title",
              "payload": {"prompt": "<pasted_content>Shop / ABC-1000 / ABC-1004\nAdd stock badge</pasted_content> why?"}}
        features.Features(ag).on_events(DAY, "sid12345", {}, [ev])
        self.assertEqual(st.ticket("ABC-1004")["crumb_parents"], ["ABC-1000"])


if __name__ == "__main__":
    unittest.main()
