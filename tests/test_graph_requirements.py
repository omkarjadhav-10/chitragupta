import copy
import unittest

from chitragupta import brief, graphbuild as gb, graphmodel as gm
from tests import graphfixture as fx
from tests.support import Isolated

KEY = "ABC-1001"
TICKET = gm.node_id("ticket", "", KEY)
FILTER = "Orders can be filtered by status"
EXPORT = "The filtered list can be exported as a spreadsheet"
EMPTY = "An empty result shows a clear message"
FILTER2 = "Orders can be filtered by status and by date"


class Base(unittest.TestCase):
    def setUp(self):
        self.req, self.seq = {}, 0

    def read(self, criteria, **kw):
        return self.take(fx.card(KEY, criteria, **kw))

    def take(self, rec):
        self.seq += 1
        before = copy.deepcopy(self.req)
        req, change = gb.requirement_snapshot(self.req, rec, (rec or {}).get("card"), (), "2026-01-09T09:00:00+00:00",
                                              TICKET, self.seq)
        self.assertEqual(self.req, before)   # the input is never modified
        self.req = req
        return change

    def ids(self):
        return {self.text(rid): rid for rid in self.req["logical"]}

    def text(self, rid):
        return self.req["revisions"][self.req["logical"][rid]["history"][-1]["revision"]]["text"]

    def events(self, rid):
        return [(h["seq"], h["event"]) for h in self.req["logical"][rid]["history"]]

    def quiet(self, change):
        self.assertEqual([change[n] for n in gb.EVENTS], [[], [], [], []])


class FirstRead(Base):
    def test_rows_become_unconfirmed_requirements(self):
        change = self.read([FILTER, EXPORT])
        ids = self.ids()
        self.assertEqual(change["added"], [ids[FILTER], ids[EXPORT]])
        self.assertEqual((change["source"], change["snapshot"], change["changed"]),
                         ({"from": None, "to": "current"}, True, True))
        for rid in ids.values():
            self.assertRegex(rid, gm.ID)
            self.assertTrue(rid.startswith("gr_"))
        first = self.req["logical"][ids[FILTER]]
        self.assertEqual((first["ordinal"], first["status"], first["first_seen"]), (1, "unconfirmed", fx.FETCHED))
        self.assertEqual(self.events(ids[FILTER]), [(1, "added")])
        self.assertEqual(self.req["next_ordinal"], 3)
        revision = self.req["revisions"][first["root"]]
        self.assertEqual((revision["text"], revision["source"], revision["fragment"]["index"]),
                         (FILTER, "Jira description, acceptance criteria item 1", 1))
        self.assertEqual(self.req["source"], {"state": "current", "fetched_at": fx.FETCHED, "error": None,
                                              "completeness": "complete", "gaps": [], "comments_proposed": 0})
        snapshot = self.req["snapshots"][0]
        self.assertEqual((snapshot["seq"], snapshot["at"], snapshot["order"], snapshot["reordered"]),
                         (1, fx.FETCHED, [ids[FILTER], ids[EXPORT]], False))

    def test_same_inputs_same_result(self):
        self.read([FILTER, EXPORT])
        other = gb.requirement_snapshot({}, fx.card(KEY, [FILTER, EXPORT]), fx.card(KEY, [FILTER, EXPORT])["card"],
                                        (), "x", TICKET, 1)[0]
        self.assertEqual(self.req, other)
        elsewhere = gb.requirement_snapshot({}, fx.card(KEY, [FILTER]), fx.card(KEY, [FILTER])["card"], (), "x",
                                            gm.node_id("ticket", "", "ABC-1002"), 1)[0]
        self.assertNotIn(self.ids()[FILTER], elsewhere["logical"])   # identity includes the ticket

    def test_identical_texts_are_two_requirements(self):
        self.read([FILTER, FILTER])
        self.assertEqual(len(self.req["logical"]), 2)
        self.assertEqual(len(self.req["revisions"]), 1)
        order = self.req["snapshots"][0]["order"]
        self.quiet(self.read([FILTER, FILTER]))
        self.assertEqual(len(self.req["snapshots"]), 1)
        self.assertEqual(len(set(order)), 2)

    def test_comments_never_become_requirements(self):
        self.read([FILTER], comments=["The list must also sort by date", "Acceptance criteria: - Add a pager"])
        self.assertEqual(list(self.ids()), [FILTER])
        self.assertEqual(self.req["source"]["comments_proposed"], 2)
        self.quiet(self.read([FILTER], comments=["One more wish: dark mode"]))
        self.assertEqual((len(self.req["logical"]), self.req["source"]["comments_proposed"]), (1, 1))


class Changes(Base):
    def test_reorder_keeps_ids_and_creates_no_revision(self):
        self.read([FILTER, EXPORT, EMPTY])
        ids, revisions = self.ids(), copy.deepcopy(self.req["revisions"])
        change = self.read([EMPTY, FILTER, EXPORT])
        self.quiet(change)
        self.assertEqual((change["reordered"], change["snapshot"]), (True, True))
        self.assertEqual(self.ids(), ids)
        self.assertEqual(sorted(self.req["revisions"]), sorted(revisions))
        self.assertEqual([len(e["history"]) for e in self.req["logical"].values()], [1, 1, 1])
        self.assertEqual(self.req["snapshots"][-1]["order"], [ids[EMPTY], ids[FILTER], ids[EXPORT]])
        self.assertEqual(self.req["logical"][ids[EMPTY]]["fragment"]["index"], 1)

    def test_rewording_is_a_new_revision_of_the_same_requirement(self):
        self.read([FILTER, EXPORT])
        ids = self.ids()
        change = self.read([FILTER2, EXPORT])
        self.assertEqual((change["modified"], change["added"], change["removed"]), ([ids[FILTER]], [], []))
        self.assertEqual(self.text(ids[FILTER]), FILTER2)
        self.assertEqual(self.events(ids[FILTER]), [(1, "added"), (2, "modified")])
        self.assertEqual(len(self.req["revisions"]), 3)
        self.assertEqual(self.req["revisions"][self.req["logical"][ids[FILTER]]["root"]]["text"], FILTER)

    def test_exact_before_fuzzy(self):
        self.read([FILTER])
        rid = self.ids()[FILTER]
        change = self.read([FILTER2, FILTER])   # the reworded-looking row comes first
        self.assertEqual((change["modified"], len(change["added"])), ([], 1))
        self.assertEqual(self.text(rid), FILTER)
        self.assertEqual(self.events(rid), [(1, "added")])
        self.assertEqual(self.text(change["added"][0]), FILTER2)

    def test_unrelated_text_is_added_not_matched(self):
        self.read([FILTER])
        change = self.read([FILTER, EMPTY])
        self.assertEqual((len(change["added"]), change["modified"]), (1, []))

    def test_a_b_a_is_three_occurrences_with_one_digest_reused(self):
        self.read([FILTER])
        rid = self.ids()[FILTER]
        self.read([FILTER2])
        change = self.read([FILTER])
        self.assertEqual(change["modified"], [rid])
        history = self.req["logical"][rid]["history"]
        self.assertEqual([(h["seq"], h["event"]) for h in history], [(1, "added"), (2, "modified"), (3, "modified")])
        self.assertEqual(history[0]["revision"], history[2]["revision"])
        self.assertNotEqual(history[0]["revision"], history[1]["revision"])
        self.assertEqual((len(self.req["logical"]), len(self.req["revisions"])), (1, 2))

    def test_a_refresh_alone_changes_nothing_that_is_journaled(self):
        self.read([FILTER])
        change = self.read([FILTER], fetched_at="2026-01-06T10:00:00+00:00")
        self.quiet(change)
        self.assertEqual((change["source"], change["snapshot"], change["changed"], change["reordered"]),
                         (None, False, False, False))
        self.assertEqual(len(self.req["snapshots"]), 1)
        self.assertEqual(self.req["source"]["fetched_at"], "2026-01-06T10:00:00+00:00")


class Absence(Base):
    def test_deleted_on_a_complete_source_is_removed(self):
        self.read([FILTER, EXPORT])
        ids = self.ids()
        change = self.read([FILTER])
        self.assertEqual((change["removed"], change["unseen"]), ([ids[EXPORT]], []))
        self.assertEqual(self.req["logical"][ids[EXPORT]]["status"], "removed")
        self.assertEqual(self.events(ids[EXPORT]), [(1, "added"), (2, "removed")])
        self.quiet(self.read([FILTER]))   # stays removed, is not removed twice

    def test_missing_from_a_truncated_read_is_unseen_not_removed(self):
        self.read([FILTER, EXPORT])
        ids = self.ids()
        change = self.read([FILTER], truncated=True)
        self.assertEqual((change["removed"], change["unseen"]), ([], [ids[EXPORT]]))
        self.assertEqual(change["source"], {"from": "current", "to": "partial"})
        entry = self.req["logical"][ids[EXPORT]]
        self.assertEqual((entry["status"], entry["status_note"]), ("unconfirmed", gb.NOT_REOBSERVED))
        self.assertEqual(self.req["source"]["completeness"], "partial")
        self.assertTrue(self.req["source"]["gaps"])
        self.assertEqual(self.req["snapshots"][-1]["unseen"], [ids[EXPORT]])
        back = self.read([FILTER, EXPORT])   # seen again: the note goes, nothing else happened
        self.quiet(back)
        self.assertNotIn("status_note", self.req["logical"][ids[EXPORT]])
        self.assertEqual(self.events(ids[EXPORT]), [(1, "added")])

    def test_unseen_becomes_removed_once_a_complete_read_confirms_it(self):
        self.read([FILTER, EXPORT])
        ids = self.ids()
        self.read([FILTER], truncated=True)
        change = self.read([FILTER])
        self.assertEqual(change["removed"], [ids[EXPORT]])
        self.assertTrue(change["snapshot"])

    def test_missing_from_a_failed_read_is_unseen(self):
        self.read([FILTER, EXPORT])
        ids = self.ids()
        change = self.read([FILTER], error="tool call failed")
        self.assertEqual((change["removed"], change["unseen"]), ([], [ids[EXPORT]]))
        self.assertEqual((self.req["source"]["state"], self.req["source"]["error"]), ("partial", "tool call failed"))

    def test_removed_then_restored_keeps_the_id(self):
        self.read([FILTER, EXPORT])
        ids = self.ids()
        self.read([FILTER])
        change = self.read([FILTER, EXPORT])
        self.assertEqual((change["restored"], change["added"]), ([ids[EXPORT]], []))
        self.assertEqual(self.events(ids[EXPORT]), [(1, "added"), (2, "removed"), (3, "restored")])
        self.assertEqual(self.req["logical"][ids[EXPORT]]["status"], "unconfirmed")
        self.assertEqual(len(self.req["logical"]), 2)

    def test_everything_gone_follows_the_companion_rule(self):
        self.read([FILTER])
        change = self.read([])   # no rows: completeness is "unavailable", so absence is not asserted
        self.assertEqual((change["removed"], len(change["unseen"])), ([], 1))
        self.assertEqual(self.req["source"]["completeness"], "unavailable")


class Source(Base):
    def test_failed_then_good_read_is_two_transitions_and_no_requirement_change(self):
        self.read([FILTER, EXPORT])
        snapshots = len(self.req["snapshots"])
        failed = self.read([FILTER, EXPORT], error="tool call failed")
        good = self.read([FILTER, EXPORT])
        self.assertEqual(failed["source"], {"from": "current", "to": "partial"})
        self.assertEqual(good["source"], {"from": "partial", "to": "current"})
        for change in (failed, good):
            self.quiet(change)
            self.assertEqual((change["unseen"], change["snapshot"], change["changed"]), ([], False, True))
        self.assertEqual(len(self.req["snapshots"]), snapshots)
        self.assertEqual([len(e["history"]) for e in self.req["logical"].values()], [1, 1])

    def test_an_inaccessible_source_changes_nothing(self):
        self.read([FILTER, EXPORT])
        logical, snapshots = copy.deepcopy(self.req["logical"]), copy.deepcopy(self.req["snapshots"])
        for rec in (None, fx.card(KEY, [], found=False), {"key": KEY, "fetched_at": fx.FETCHED, "card": None,
                                                           "raw": None, "error": "no access", "fields": ""}):
            change = self.take(rec)
            self.quiet(change)
            self.assertEqual((change["unseen"], change["snapshot"]), ([], False))
            self.assertEqual((self.req["source"]["state"], self.req["source"]["completeness"]),
                             ("inaccessible", "unavailable"))
            self.assertEqual((self.req["logical"], self.req["snapshots"]), (logical, snapshots))
        self.assertEqual(self.read([FILTER, EXPORT])["source"], {"from": "inaccessible", "to": "current"})

    def test_first_read_of_nothing(self):
        change = self.take(None)
        self.assertEqual((change["source"], change["snapshot"], change["changed"]),
                         ({"from": None, "to": "inaccessible"}, False, True))
        self.assertEqual((self.req["logical"], self.req["snapshots"], self.req["next_ordinal"]), ({}, [], 1))

    def test_fingerprint_is_over_ordered_digests_only(self):
        self.assertEqual(gb.text_fingerprint(["a", "b"]), gb.text_fingerprint(iter(["a", "b"])))
        self.assertNotEqual(gb.text_fingerprint(["a", "b"]), gb.text_fingerprint(["b", "a"]))
        self.read([FILTER])
        first = self.req["snapshots"][-1]["fingerprint"]
        rec = fx.card(KEY, [FILTER], fetched_at="2026-02-01T10:00:00+00:00")
        rec["card"].update(summary="A different model-written summary", status="Done")
        self.assertFalse(self.take(rec)["snapshot"])
        self.assertEqual(self.req["snapshots"][-1]["fingerprint"], first)


class Bounds(Base):
    def patch(self, name, value):
        self.addCleanup(setattr, gb, name, getattr(gb, name))
        setattr(gb, name, value)

    def test_snapshots_keep_the_newest(self):
        self.patch("MAX_SNAPSHOTS", 3)
        for i in range(5):
            self.read([FILTER, EXPORT] if i % 2 else [EXPORT, FILTER])
        self.assertEqual([s["seq"] for s in self.req["snapshots"]], [3, 4, 5])
        self.assertEqual(len(self.req["logical"]), 2)

    def test_rows_beyond_the_requirement_limit_are_a_gap(self):
        self.patch("MAX_REQUIREMENTS", 2)
        change = self.read([FILTER, EXPORT, EMPTY])
        self.assertEqual((len(change["added"]), len(self.req["logical"])), (2, 2))
        self.assertIn(gb.TOO_MANY, self.req["source"]["gaps"])
        self.assertEqual((self.req["source"]["state"], self.req["source"]["completeness"]), ("partial", "partial"))
        self.assertNotIn(EMPTY, self.ids())
        self.quiet(self.read([FILTER, EXPORT, EMPTY]))

    def test_old_wording_loses_its_text_not_its_occurrences(self):
        self.patch("MAX_REVISIONS", 1)
        self.read([FILTER])
        rid = self.ids()[FILTER]
        self.read([FILTER2])
        old = self.req["logical"][rid]["history"][0]["revision"]
        self.assertEqual((self.req["revisions"][old]["text"], self.req["revisions"][old]["text_dropped"]), ("", True))
        self.assertEqual(self.text(rid), FILTER2)
        self.assertEqual(len(self.req["logical"][rid]["history"]), 2)
        self.read([FILTER])   # the old wording returns: same requirement, text kept again
        self.assertEqual(self.events(rid), [(1, "added"), (2, "modified"), (3, "modified")])
        self.assertEqual(self.text(rid), FILTER)

    def test_text_is_capped(self):
        long = "Orders " + "x" * 1200
        self.read([FILTER, long])   # the extractor reports an over-long criterion as a gap, not a requirement
        self.assertEqual(list(self.ids()), [FILTER])
        self.assertEqual(self.req["source"]["state"], "partial")


class Fixture(Isolated, unittest.TestCase):
    def test_write_card_puts_the_record_where_the_card_reader_looks(self):
        self.isolate()
        rec = fx.write_card(self.home, KEY, [FILTER, EXPORT], comments=["A wish"])
        card = brief.jira_card(KEY)
        self.assertEqual(card, rec["card"])
        req, change = gb.requirement_snapshot({}, rec, card, (), "x", TICKET, 1)
        self.assertEqual((len(change["added"]), req["source"]["comments_proposed"]), (2, 1))


if __name__ == "__main__":
    unittest.main()
