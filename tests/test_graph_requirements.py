import copy
import unittest

from chitragupta import brief, companion, companionjobs, graphbuild as gb, graphmodel as gm, workflowmodel as model
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


class WorkflowBase(fx.Workflows):
    def jira(self, *reads):
        """The requirement history after reading these criteria lists in turn."""
        req = {}
        for seq, texts in enumerate(reads, 1):
            rec = fx.card(KEY, texts)
            req = gb.requirement_snapshot(req, rec, rec["card"], (), "x", TICKET, seq)[0]
        return req

    def inputs(self, rec):
        return gb.workflow_inputs(companion.get(rec["id"]))

    def by_text(self, index):
        return {entry["text"]: (rid, entry) for rid, entry in index["requirements"].items()}

    def replan(self, rec, text="A second way to do it, checked the same way."):
        return companion.save_plan(rec["id"], rec["revision"], fx.plan_body(text=text))


class Baselines(WorkflowBase):
    def test_workflow_inputs_is_the_documented_subset(self):
        wf = self.inputs(self.workflow([FILTER]))
        self.assertEqual(set(wf), set(gb.WORKFLOW_FIELDS) | {"binding", "plan_order"})
        self.assertEqual(set(wf["binding"]), set(gb.BINDING_FIELDS))
        self.assertEqual((wf["binding"]["ticket"], wf["plan_order"]), (KEY, [wf["selected_plan"]]))
        self.assertNotIn("jobs", wf)

    def test_no_workflow_everything_is_unconfirmed_and_has_no_plan(self):
        index = gb.baseline_links(self.jira([FILTER, EXPORT]), [], TICKET)
        self.assertEqual({e["status"] for e in index["requirements"].values()}, {"unconfirmed"})
        self.assertEqual((index["baselines"], index["confirmed_in"]), ({}, {}))
        rows = gb.plan_coverage_rows(None, index)
        self.assertEqual(sorted(rows), sorted(index["requirements"]))
        for row in rows.values():
            self.assertEqual((row["state"], row["reason"], row["plan"]), ("Unverified", "no plan", None))

    def test_current_baseline_gives_alias_and_confirmation(self):
        wf = self.inputs(self.workflow([FILTER, EXPORT]))
        index = gb.baseline_links(self.jira([FILTER, EXPORT, EMPTY]), [wf], TICKET)
        found = self.by_text(index)
        rid, entry = found[EXPORT]
        self.assertEqual((entry["status"], entry["aliases"], entry["source"], entry["ambiguity"]),
                         ("current", {wf["id"]: "ac2"}, "jira", None))
        self.assertEqual(entry["confirmed"], {wf["id"]: entry["revision"]})
        self.assertEqual(found[EMPTY][1]["status"], "unconfirmed")   # in Jira, in no baseline
        mapping = index["baselines"][wf["id"]][wf["baseline"]]
        self.assertEqual((sorted(mapping), mapping["ac2"]["requirement"], mapping["ac2"]["excluded"]),
                         (["ac1", "ac2"], rid, False))
        self.assertEqual(index["confirmed_in"]["%s|%s" % (rid, entry["revision"])],
                         [{"workflow": wf["id"], "baseline": wf["baseline"], "current": True}])

    def test_a_criterion_jira_never_showed_is_a_local_requirement(self):
        wf = self.inputs(self.workflow([FILTER, "The filter choice survives a page reload"]))
        index = gb.baseline_links(self.jira([FILTER]), [wf], TICKET)
        rid, entry = self.by_text(index)["The filter choice survives a page reload"]
        self.assertEqual((entry["source"], entry["status"], entry["aliases"]), (gb.LOCAL, "current", {wf["id"]: "ac2"}))
        self.assertRegex(rid, gm.ID)
        self.assertEqual(len(index["requirements"]), 2)
        again = gb.baseline_links(self.jira([FILTER]), [wf], TICKET)
        self.assertEqual(again, index)

    def test_old_baseline_does_not_resurrect(self):
        wf = self.inputs(self.workflow([FILTER]))
        earlier = model.baseline(fx.criteria_rows([FILTER, EXPORT]))
        wf["baselines"][earlier["id"]] = earlier   # an earlier baseline of this workflow still named EXPORT
        index = gb.baseline_links(self.jira([FILTER, EXPORT], [FILTER]), [wf], TICKET)
        rid, entry = self.by_text(index)[EXPORT]
        self.assertEqual((entry["status"], entry["aliases"], entry["confirmed"]), ("removed", {}, {}))
        self.assertEqual(index["confirmed_in"]["%s|%s" % (rid, entry["revision"])],
                         [{"workflow": wf["id"], "baseline": earlier["id"], "current": False}])
        self.assertEqual(self.by_text(index)[FILTER][1]["status"], "current")
        self.assertEqual(len(index["requirements"]), 2)
        self.assertNotIn(rid, gb.plan_coverage_rows(wf, index))

    def test_an_earlier_baseline_cannot_introduce_a_requirement(self):
        wf = self.inputs(self.workflow([FILTER]))
        earlier = model.baseline(fx.criteria_rows([FILTER, "A clarification dropped later"]))
        wf["baselines"][earlier["id"]] = earlier
        index = gb.baseline_links(self.jira([FILTER]), [wf], TICKET)
        self.assertEqual(len(index["requirements"]), 1)
        self.assertEqual(sorted(index["baselines"][wf["id"]][earlier["id"]]), ["ac1"])

    def test_the_same_alias_in_two_workflows_is_kept_per_workflow(self):
        one = self.inputs(self.workflow([FILTER, EXPORT]))
        two = self.inputs(self.workflow([EXPORT, FILTER], repo=self.repo_b))
        self.assertNotEqual(one["id"], two["id"])
        index = gb.baseline_links(self.jira([FILTER, EXPORT]), [one, two], TICKET)
        found = self.by_text(index)
        self.assertEqual(found[FILTER][1]["aliases"], {one["id"]: "ac1", two["id"]: "ac2"})
        self.assertEqual(found[EXPORT][1]["aliases"], {one["id"]: "ac2", two["id"]: "ac1"})
        self.assertEqual({e["ambiguity"] for e in index["requirements"].values()}, {None})
        self.assertEqual(index, gb.baseline_links(self.jira([FILTER, EXPORT]), [two, one], TICKET))

    def test_baselines_that_confirm_different_wording_are_flagged(self):
        one = self.inputs(self.workflow([FILTER]))
        two = self.inputs(self.workflow([FILTER2], repo=self.repo_b))
        index = gb.baseline_links(self.jira([FILTER], [FILTER2]), [one, two], TICKET)
        self.assertEqual(len(index["requirements"]), 1)
        entry = list(index["requirements"].values())[0]
        self.assertEqual((entry["ambiguity"], len(set(entry["confirmed"].values())), entry["text"]),
                         (gb.BASELINES_DIFFER, 2, FILTER2))


class Plans(WorkflowBase):
    def build(self, rec, *reads):
        wf = self.inputs(rec)
        index = gb.baseline_links(self.jira(*(reads or ([FILTER, EXPORT],))), [wf], TICKET)
        return wf, index, gb.plan_nodes(wf, index)

    def of(self, rows, kind):
        return [row for row in rows if row["type"] == kind]

    def test_selected_never_approved_plan_is_labelled_so(self):
        wf, index, out = self.build(self.workflow([FILTER, EXPORT]))
        plan = self.of(out["nodes"], "plan")[0]
        self.assertEqual((out["label"], out["judged"]), (gb.NOT_APPROVED, wf["selected_plan"]))
        self.assertEqual((plan["label"], plan["lifecycle"], plan["workflow"], plan["ticket"]),
                         ("Plan v1", "current", wf["id"], KEY))
        attrs = plan["attrs"]
        self.assertEqual((attrs["version"], attrs["state"], attrs["approved"], attrs["selected"], attrs["judged"],
                          attrs["baseline"], attrs["evaluation"]),
                         (1, gb.NOT_APPROVED, False, True, True, wf["baseline"], None))
        self.assertEqual(plan["id"], gm.node_id("plan", wf["id"], wf["selected_plan"]))
        self.assertEqual((plan["repo_id"], plan["location"]), (wf["binding"]["repo_id"], wf["binding"]["location"]))

    def test_steps_and_their_edges(self):
        wf, index, out = self.build(self.workflow([FILTER, EXPORT]))
        plan = self.of(out["nodes"], "plan")[0]["id"]
        steps = {s["attrs"]["step"]: s for s in self.of(out["nodes"], "plan_step")}
        self.assertEqual(sorted(steps), ["one", "two"])
        self.assertEqual((steps["two"]["attrs"]["kind"], steps["two"]["attrs"]["plan"], steps["one"]["label"]),
                         ("verification", plan, "Implement behavior"))
        self.assertEqual(steps["one"]["id"], gm.node_id("plan_step", plan, "one"))
        rid, entry = self.by_text(index)[FILTER]
        addressed = self.of(out["edges"], "ADDRESSED_BY")
        self.assertEqual({(e["src"], e["dst"]) for e in addressed},
                         {(gb.revision_id(rid, entry["revision"]), steps[s]["id"]) for s in ("one", "two")})
        for edge in addressed:
            self.assertEqual((edge["basis"], edge["scope"], edge["workflow"], edge["attrs"], edge["lifecycle"]),
                             ("declared", plan, wf["id"], {"approved": False, "criterion": "ac1"}, "current"))
        depends = self.of(out["edges"], "DEPENDS_ON")
        self.assertEqual([(e["src"], e["dst"], e["scope"]) for e in depends],
                         [(steps["two"]["id"], steps["one"]["id"], plan)])
        self.assertEqual(self.of(out["edges"], "SUPERSEDES"), [])
        self.assertEqual(out, gb.plan_nodes(wf, index))

    def test_a_new_approved_plan_keeps_the_old_edges_superseded_and_scoped(self):
        first = self.workflow([FILTER, EXPORT], approve=True)
        second = self.approve(self.replan(first))
        wf, index, out = self.build(second)
        old, new = wf["plan_order"]
        self.assertEqual((wf["approved_plan"], out["label"]), (new, "approved"))
        plans = {p["attrs"]["version"]: p for p in self.of(out["nodes"], "plan")}
        self.assertEqual([(plans[n]["lifecycle"], plans[n]["attrs"]["state"]) for n in (1, 2)],
                         [("superseded", "superseded"), ("current", "approved")])
        addressed = self.of(out["edges"], "ADDRESSED_BY")
        by_scope = {scope: [e for e in addressed if e["scope"] == scope] for scope in (plans[1]["id"], plans[2]["id"])}
        self.assertEqual([len(v) for v in by_scope.values()], [2, 2])
        self.assertEqual({e["lifecycle"] for e in by_scope[plans[1]["id"]]}, {"superseded"})
        self.assertEqual({(e["lifecycle"], e["attrs"]["approved"]) for e in by_scope[plans[2]["id"]]},
                         {("current", True)})
        self.assertEqual(len({e["dst"] for e in addressed}), 4)   # step "one" of v1 is not step "one" of v2
        self.assertEqual({s["lifecycle"] for s in self.of(out["nodes"], "plan_step")
                          if s["attrs"]["plan"] == plans[1]["id"]}, {"superseded"})
        succession = [e for e in self.of(out["edges"], "SUPERSEDES") if e["attrs"]["how"] == "approval"]
        self.assertEqual([(e["src"], e["dst"], e["basis"]) for e in succession],
                         [(plans[2]["id"], plans[1]["id"], "observed")])
        self.assertTrue(succession[0]["scope"].startswith("a_"))
        if wf["plans"][new].get("parent") == old:
            parents = [e for e in self.of(out["edges"], "SUPERSEDES") if e["attrs"]["how"] == "parent"]
            self.assertEqual([(e["src"], e["dst"]) for e in parents], [(plans[2]["id"], plans[1]["id"])])

    def test_no_plan_gives_no_nodes(self):
        wf, index, out = self.build(self.workflow([FILTER], plan=False))
        self.assertEqual(out, {"nodes": [], "edges": [], "judged": None, "label": "no plan"})
        row = list(gb.plan_coverage_rows(wf, index).values())[0]
        self.assertEqual((row["state"], row["reason"], row["plan"], row["alias"]), ("Unverified", "no plan", None, "ac1"))


class Coverage(WorkflowBase):
    def rows(self, wf, *reads):
        index = gb.baseline_links(self.jira(*(reads or ([FILTER, EXPORT],))), [wf], TICKET)
        return {entry["text"]: gb.plan_coverage_rows(wf, index).get(rid) for rid, entry in index["requirements"].items()}

    def test_without_an_evaluation_coverage_comes_from_the_plan_mapping(self):
        wf = self.inputs(self.workflow([FILTER, EXPORT]))
        rows = self.rows(wf)
        self.assertEqual((rows[FILTER]["state"], rows[FILTER]["basis"], rows[FILTER]["reason"]),
                         ("Covered", "declared", "plan mapping; not evaluated"))
        self.assertEqual((rows[FILTER]["judged"], rows[FILTER]["alias"], rows[FILTER]["excluded"]),
                         (gb.NOT_APPROVED, "ac1", False))
        self.assertEqual(rows[FILTER]["plan"], gm.node_id("plan", wf["id"], wf["selected_plan"]))
        self.assertEqual((rows[EXPORT]["state"], rows[EXPORT]["basis"]), ("Missing", "declared"))   # no step lists ac2

    def test_a_developer_assessment_is_declared(self):
        rec = self.workflow([FILTER, EXPORT])
        rec = companionjobs.manual_assessment(rec["id"], rec["revision"], rec["selected_plan"],
                                              {"ac1": "Partial", "ac2": "Covered"})
        wf = self.inputs(rec)
        rows = self.rows(wf)
        self.assertEqual((rows[FILTER]["state"], rows[FILTER]["basis"]), ("Partial", "declared"))
        self.assertEqual((rows[EXPORT]["state"], rows[EXPORT]["basis"]), ("Covered", "declared"))
        index = gb.baseline_links(self.jira([FILTER, EXPORT]), [wf], TICKET)
        plan = [n for n in gb.plan_nodes(wf, index)["nodes"] if n["type"] == "plan"][0]
        self.assertEqual(plan["attrs"]["evaluation"], list(wf["evaluations"])[0])

    def jev(self, wf, answers, at="2026-11-08T11:00:00+00:00", **over):
        body = dict({"id": "e_" + "1" * 16, "plan": wf["selected_plan"], "baseline": wf["baseline"],
                     "state": "available", "at": at,
                     "answers": {cid: {"status": status, "origin": "Jev classification", "confidence": 0.9}
                                 for cid, status in answers.items()}}, **over)
        wf["evaluations"][body["id"]] = body

    def test_a_jev_evaluation_shows_as_inferred(self):
        wf = self.inputs(self.workflow([FILTER, EXPORT]))
        self.jev(wf, {"ac1": "Covered", "ac2": "Partial"})
        rows = self.rows(wf)
        self.assertEqual((rows[FILTER]["state"], rows[FILTER]["basis"]), ("Covered", "inferred"))
        self.assertEqual((rows[EXPORT]["state"], rows[EXPORT]["basis"]), ("Partial", "inferred"))
        self.assertIn("Jev classification", rows[EXPORT]["reason"])

    def test_an_unavailable_evaluation_classifies_nothing(self):
        wf = self.inputs(self.workflow([FILTER, EXPORT]))
        self.jev(wf, {"ac1": "Missing"}, state="unavailable")
        self.assertEqual(self.rows(wf)[FILTER]["reason"], "plan mapping; not evaluated")

    def test_the_newest_evaluation_that_classifies_wins(self):
        rec = self.workflow([FILTER, EXPORT])
        rec = companionjobs.manual_assessment(rec["id"], rec["revision"], rec["selected_plan"], {"ac1": "Partial"})
        wf = self.inputs(rec)
        self.jev(wf, {"ac1": "Covered", "ac2": "Covered"}, at="2020-01-01T00:00:00+00:00")   # older than the assessment
        rows = self.rows(wf)
        self.assertEqual((rows[FILTER]["state"], rows[FILTER]["basis"]), ("Partial", "declared"))
        self.assertEqual((rows[EXPORT]["state"], rows[EXPORT]["basis"]), ("Covered", "inferred"))

    def test_requirement_changed_after_the_plan_is_unverified(self):
        wf = self.inputs(self.workflow([FILTER, EXPORT]))
        self.jev(wf, {"ac1": "Covered"})
        rows = self.rows(wf, [FILTER, EXPORT], [FILTER2, EXPORT])   # Jira reworded ac1 after the baseline
        self.assertEqual((rows[FILTER2]["state"], rows[FILTER2]["reason"]), ("Unverified", gm.PLAN_CHANGED))
        self.assertEqual(rows[EXPORT]["state"], "Missing")

    def test_an_excluded_criterion_keeps_its_rationale(self):
        wf = self.inputs(self.workflow([FILTER, EXPORT]))
        base = wf["baselines"][wf["baseline"]]
        base["criteria"][1].update(excluded=True, rationale="Export moved to ABC-1002")
        row = self.rows(wf)[EXPORT]
        self.assertEqual((row["excluded"], row["rationale"]), (True, "Export moved to ABC-1002"))


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
