"""K8: an item that looks like it belongs to another ticket (or to the worklog tool itself) is flagged for review at
merge time, never moved or deleted; inbox lines keep it or copy it to the right ticket.

Synthetic data only (tickets ABC-…, schema `shop`).
"""
import datetime as dt
import tempfile
import unittest
from unittest import mock

from tests.support import Isolated, write_config

D = "2026-09-01"
T = "ABC-1001"


def at(hh, mm, day=D):
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm)).astimezone()


def ctx(src="branch"):
    return {"day": D, "session": "abcdef123456", "time": "09:00", "repo": None, "branch": "feature/ABC-1001-cancel",
            "candidates": [T], "turns": [("09:00", T, src)], "default": T}


class Base(Isolated, unittest.TestCase):
    def setUp(self):
        h = tempfile.mkdtemp(prefix="cg-test-")
        write_config(h, {"ticket_prefixes": ["ABC"]})
        self.isolate(h)
        from chitragupta import common, store
        common.load_config()
        self.st = store.Store()
        tk = self.st.ticket(T)
        tk.update(title="Cancel order keeps stock reserved", title_source="jira")
        tk["tables"]["shop.orders"] = {"name": "shop.orders"}

    def merge(self, title, src="branch", detail=""):
        from chitragupta import store
        store.merge_extraction(self.st, {"items": [{"kind": "finding", "title": title, "detail": detail, "time": "10:00"}]},
                               ctx(src), ["ABC"])
        return next(i for i in self.st.ticket(T)["items"].values() if i["title"] == title)


class FlagTests(Base):
    def test_item_naming_only_another_ticket_is_flagged(self):
        from chitragupta import render
        it = self.merge("ABC-1002: login lockout reset never clears failed_attempts")
        self.assertEqual(it["review"]["suggest"], "ABC-1002")
        self.assertIn("ABC-1002", it["review"]["why"])
        self.assertTrue(it["review"]["at"])
        md = render.render_ticket(self.st, T)
        rc = md[md.index("## Root cause"):md.index("## How it works")]
        self.assertNotIn("login lockout", rc)
        nr = md[md.index("Needs review (1)"):]
        self.assertIn("login lockout", nr)
        self.assertIn("`ABC-1001: move %s ABC-1002`" % it["id"], nr)
        from chitragupta.dashboard import api
        self.assertEqual(api._items(self.st.ticket(T))[0]["review"]["suggest"], "ABC-1002")

    def test_a_turn_that_named_this_ticket_keeps_it(self):
        it = self.merge("ABC-1002 half fix explains the stock reservation", src="prompt")
        self.assertFalse(it.get("review"))

    def test_naming_its_own_ticket_too_is_fine(self):
        self.assertFalse(self.merge("ABC-1001 cancel path differs from ABC-1002 refund path").get("review"))

    def test_branch_item_sharing_nothing_is_flagged_and_one_sharing_a_word_is_not(self):
        it = self.merge("Login page font is blurry on retina screens")
        self.assertIn("shares no", it["review"]["why"])
        self.assertIsNone(it["review"]["suggest"])
        self.assertFalse(self.merge("Reservation row stays after cancel").get("review"))
        self.assertFalse(self.merge("Unrelated topic from a prompt turn", src="prompt").get("review"))

    def test_notes_about_the_worklog_itself_are_flagged(self):
        it = self.merge("Ran /cg:today to check the stock notes", detail="saved under .worklog by mistake")
        self.assertIn("worklog tool", it["review"]["why"])

    def test_a_flagged_item_updated_in_place_keeps_its_review_state(self):
        from chitragupta import store
        it = self.merge("ABC-1002: login lockout reset never clears failed_attempts")
        it["review"], it["review_kept"] = None, "2026-09-01T10:00:00"
        store.merge_extraction(self.st, {"items": [{"kind": "finding", "title": "ABC-1002 lockout reset also skips audit",
                                                    "updates": it["id"], "time": "11:00"}]}, ctx(), ["ABC"])
        new = self.st.ticket(T)["items"][it["id"]]
        self.assertFalse(new.get("review"))
        self.assertEqual(new["review_kept"], "2026-09-01T10:00:00")


class InboxTests(Base):
    def run_inbox(self, *lines):
        from chitragupta import agent, sources
        notes = [{"hash": str(n), "ts": at(12, n).isoformat(), "ticket": T, "text": x} for n, x in enumerate(lines)]
        self.st.touch_ticket(T)
        self.st.save()
        with mock.patch.object(sources, "scan_sql", lambda cfg, st: []), \
                mock.patch.object(sources, "scan_commits", lambda cfg, st, since: []), \
                mock.patch.object(sources, "scan_bruno", lambda cfg, st: []), \
                mock.patch.object(sources, "scan_inbox", lambda cfg, st: notes), \
                mock.patch.object(agent.Agent, "run_branches", lambda self: None):
            a = agent.Agent()
            a.run_sources()
        return a

    def test_keep_clears_the_review(self):
        iid = self.merge("ABC-1002: login lockout reset never clears failed_attempts")["id"]
        a = self.run_inbox("keep %s" % iid)
        it = a.store.ticket(T)["items"][iid]
        self.assertIsNone(it["review"])
        self.assertTrue(it["review_kept"])
        self.assertEqual([i for i in a.store.ticket(T)["items"].values() if i["kind"] == "note"], [])  # not a note

    def test_move_copies_and_archives_never_deletes(self):
        from chitragupta import render
        iid = self.merge("ABC-1002: login lockout reset never clears failed_attempts")["id"]
        a = self.run_inbox("move %s ABC-1002" % iid)
        orig = a.store.ticket(T)["items"][iid]
        self.assertEqual(orig["archived"]["reason"], "moved to ABC-1002")
        self.assertEqual(orig["title"], "ABC-1002: login lockout reset never clears failed_attempts")
        copy = [i for i in a.store.ticket("ABC-1002")["items"].values()]
        self.assertEqual(len(copy), 1)
        self.assertEqual((copy[0]["src"], copy[0]["moved_from"], copy[0].get("review")), ("user", "%s/%s" % (T, iid), None))
        self.assertEqual(copy[0]["title"], orig["title"])
        md = render.render_ticket(a.store, T)
        self.assertNotIn("login lockout", md)
        self.assertIn("login lockout", render.render_ticket(a.store, "ABC-1002"))


class PageTests(unittest.TestCase):
    def test_dashboard_ticket_page_collapses_review_superseded_and_unsent(self):
        import os
        from chitragupta.dashboard import server
        from tests.support import rd
        js = rd(os.path.join(os.path.dirname(server.PAGE), "pages", "tickets.js"))
        for hook in ("Needs review (${list.length})", "Superseded (${sup.length})", "Done (${done.length})",
                     "Not run (${notRun.length})", "': keep '", "': move '"):
            self.assertIn(hook, js)


if __name__ == "__main__":
    unittest.main()
