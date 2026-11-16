"""Evidence Graph in Resume: the checkpoint a task records and the bounded delta since it (synthetic data only)."""
import ast
import json
import os
import unittest
from unittest import mock

from chitragupta import (common, companion, companioncli, companionviews as cv, graphstore as gs, graphviews as gv,
                         workflowmodel)
from tests import graphfixture as fx
from tests.test_companion_state import HEAD1, HEAD2
from tests.test_graph_store import EXPORT, FILTER, SyncBase

A = "ABC-1001"
NEW_CRITERION = "The export names the chosen filter in its file name"
PLAIN = ("graph", "snapshot_id", "generated_at", "generated_local")


class ResumeBase(SyncBase):
    def wid(self):
        return self.rec["id"]

    def fresh(self):
        self.rec = companion.get(self.wid())
        return self.rec

    def checkpoint(self):
        self.rec = companion.checkpoint(self.wid(), self.fresh()["revision"], "stopped after the filter")
        return self.rec

    def delta(self, **kw):
        return gv.resume(A, self.wid(), self.fresh().get("graph_checkpoint"), self.cfg, **kw)

    def add_commit(self, n, subject):
        commits = dict(self.tk(A)["commits"])
        extra = fx.commit_rec(n, subject, repo="scratch", files=["src/orders/c%d.py" % n])
        commits[extra["sha"]] = extra
        self.put(A, commits=commits)
        return extra

    def mapped_run(self, tid, status, head=HEAD1):
        """A captured run of the export check at `head`, mapped to ac2 (the export criterion); the graph catches up."""
        if head != self.fresh()["context"].get("head"):
            self.rec = self.observe(self.fresh(), head=head)   # the fix was committed: the working tree moved on
        self.captured_test(self.fresh(), tid, status=status, head=head)
        self.rec = self.map(self.fresh(), evidence="test:" + tid, criteria=["ac2"], coverage="complete")
        self.sync()

    def plain(self, d):
        return {k: v for k, v in d.items() if k not in PLAIN}


class Checkpoint(ResumeBase):
    def test_pause_checkpoint_and_acknowledge_record_the_journal_position(self):
        self.sync()
        self.checkpoint()
        mark = self.rec["graph_checkpoint"]
        self.assertEqual(set(mark), {"seq", "at", "profile_generation"})
        self.assertEqual(mark["seq"], gs.load_history(A)[1]["journal"][-1]["seq"])
        self.add_commit(2, "ABC-1001 Add the order export")
        self.sync()
        rec = companion.acknowledge(self.wid(), self.fresh()["revision"], [])
        self.assertEqual(rec["graph_checkpoint"]["seq"], mark["seq"] + 1)
        self.sync()
        rec = companion.pause(self.wid(), self.fresh()["revision"], "lunch")
        self.assertEqual(rec["graph_checkpoint"]["seq"], gs.load_history(A)[1]["journal"][-1]["seq"])

    def test_no_graph_or_a_failure_leaves_the_field_unchanged(self):
        self.checkpoint()   # nothing built yet
        self.assertIsNone(self.rec["graph_checkpoint"])
        self.sync()
        self.checkpoint()
        mark = self.rec["graph_checkpoint"]
        with mock.patch.object(gv, "checkpoint", side_effect=RuntimeError("index unreadable")):
            rec = companion.pause(self.wid(), self.fresh()["revision"], "lunch")
        self.assertEqual(rec["stage"], "paused")
        self.assertEqual(rec["graph_checkpoint"], mark)

    def test_a_record_saved_before_this_change_loads(self):
        old = dict(self.fresh())
        old.pop("graph_checkpoint", None)
        loaded = workflowmodel.normalize(old)
        self.assertIsNone(loaded["graph_checkpoint"])
        self.assertEqual({k: v for k, v in loaded.items() if k != "graph_checkpoint"}, old)


class Delta(ResumeBase):
    def test_new_commit_runs_and_jira_change_each_listed_with_applicability(self):   # EG-A22
        self.sync()
        self.checkpoint()
        self.sync()
        start = self.rec["graph_checkpoint"]["seq"]
        extra = self.add_commit(2, "ABC-1001 Add the order export")
        self.sync()
        self.mapped_run("t5", "failed")
        self.mapped_run("t6", "passed", head=HEAD2)
        fx.write_card(common.home(), A, [FILTER, EXPORT, NEW_CRITERION])
        self.sync()
        g = self.delta()
        self.assertEqual((g["state"], g["checkpoint"]["state"], g["checkpoint"]["seq"]), ("ok", "ok", start))
        self.assertGreater(g["to"], start)
        nodes = {n["id"]: n for n in self.rows()[0]}

        commits = g["new"]["commits"]["items"]
        self.assertEqual([c["label"] for c in commits], ["ABC-1001 Add the order export"])
        self.assertEqual(commits[0]["applicability"], nodes[commits[0]["id"]]["lifecycle"])
        self.assertTrue(extra["sha"])

        runs = {nodes[i["id"]]["attrs"]["status"]: i for i in g["new"]["tests"]["items"] if i["kind"] == "test_run"}
        self.assertEqual(set(runs), {"failed", "passed"})   # the failing run and the passing one, both listed
        for status, item in runs.items():
            self.assertEqual(item["outcome"], status)
            self.assertEqual(item["applicability"], nodes[item["id"]]["attrs"]["applicability"]["state"])

        done = [i for i in g["completed"]["items"] if i["kind"] == "verification"]
        self.assertEqual([(i["label"], i["to"]) for i in done], [("ac2", "Verified within recorded scope")])
        self.assertNotIn("ac2", [i["label"] for i in g["outstanding"]["items"]
                                 if i["statuses"]["verification"] != "Verified within recorded scope"])

        jira = g["new"]["jira"]["items"]
        added = [i for i in jira if i["kind"] == "requirement added"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["label"], "R3")   # read from Jira; no task baseline confirms it yet
        self.assertEqual(nodes[nodes[added[0]["id"]]["attrs"]["revision"]]["attrs"]["text"], NEW_CRITERION)
        self.assertIsInstance(g["next_action"], str)
        for part in [g["completed"], g["invalidated"], g["outstanding"]] + list(g["new"].values()):
            self.assertLessEqual(len(part["items"]), gv.RESUME_CAP)
            self.assertGreaterEqual(part["total"], len(part["items"]))

    def test_invalidated_evidence_is_listed(self):
        self.sync()
        self.checkpoint()
        self.sync()
        tk = self.tk(A)
        tk["tests"].pop("t1")   # the run ac1 was verified by is gone
        common.write_json(_ticket_path(A), tk)
        self.sync()
        g = self.delta()
        self.assertTrue(g["invalidated"]["total"])
        kinds = {i["kind"] for i in g["invalidated"]["items"]}
        self.assertTrue(kinds & {"test_run", "verification no longer complete"}, kinds)

    def test_no_or_foreign_checkpoint_counts_as_absent(self):
        self.sync()
        g = gv.resume(A, self.wid(), None, self.cfg)
        self.assertEqual((g["state"], g["checkpoint"]["state"]), ("ok", "absent"))
        self.assertIn("no graph checkpoint", g["checkpoint"]["reason"])
        self.assertEqual(g["completed"]["total"] + sum(p["total"] for p in g["new"].values()), 0)
        self.assertTrue(g["outstanding"]["total"])   # still says what is left, and the next action
        self.assertTrue(g["next_action"])
        mark = {"seq": 1, "at": "2026-01-05T10:00:00Z", "profile_generation": "another"}
        g = gv.resume(A, self.wid(), mark, self.cfg)
        self.assertEqual(g["checkpoint"]["state"], "absent")
        self.assertIn("another profile generation", g["checkpoint"]["reason"])
        g = gv.resume(A, self.wid(), dict(mark, seq=99, profile_generation=gv.profile.signature(self.cfg)), self.cfg)
        self.assertEqual((g["checkpoint"]["state"], g["checkpoint"]["reason"]),
                         ("absent", "the checkpoint is not in the graph history"))

    def test_privacy_mode_hides_free_text(self):
        self.sync()
        self.checkpoint()
        self.sync()
        self.add_commit(2, "ABC-1001 Add the order export")
        self.sync()
        g = self.delta(privacy=True)
        self.assertNotIn("Add the order export", json.dumps(g))
        self.assertEqual(g["new"]["commits"]["total"], 1)


class View(ResumeBase):
    def detail(self):
        return cv.detail(self.wid())

    def test_detail_and_cli_show_the_delta(self):
        self.sync()
        self.checkpoint()
        self.sync()
        self.add_commit(2, "ABC-1001 Add the order export")
        self.sync()
        d = self.detail()
        self.assertEqual(d["graph"]["state"], "ok")
        self.assertEqual(d["graph"]["new"]["commits"]["total"], 1)
        text = "\n".join(companioncli.detail_text(d))
        self.assertIn("Evidence graph since your checkpoint", text)
        self.assertIn("New commits (1):", text)
        self.assertIn("Graph next action:", text)

    def test_unbuilt_behind_or_corrupt_renders_as_before(self):
        with mock.patch.object(cv, "resume_graph", return_value=None):
            before = self.plain(self.detail())
        d = self.detail()   # not built
        self.assertEqual((d["graph"]["state"], self.plain(d)), ("unavailable", before))
        self.assertIn("not built", d["graph"]["reason"])
        self.sync()
        self.add_commit(2, "ABC-1001 Add the order export")   # inputs moved: behind
        with mock.patch.object(cv, "resume_graph", return_value=None):
            before = self.plain(self.detail())
        d = self.detail()
        self.assertEqual((d["graph"]["state"], self.plain(d)), ("unavailable", before))
        self.assertIn("behind", d["graph"]["reason"])
        self.sync()
        with mock.patch.object(gs, "ticket_rows", side_effect=gs.sqlite3.DatabaseError("file is not a database")):
            d = self.detail()   # corrupt
        self.assertEqual(d["graph"], {"state": "unavailable", "reason": "the evidence graph could not be read (DatabaseError)"})
        with mock.patch.object(cv, "resume_graph", return_value=None):
            self.assertEqual(self.plain(d), self.plain(self.detail()))
        self.assertIn("Evidence graph: unavailable", "\n".join(companioncli.detail_text(d)))

    def test_the_resume_candidate_is_unchanged_by_the_graph(self):
        first = companion.resume(self.wid())["id"]
        self.sync()
        self.assertEqual(companion.resume(self.wid())["id"], first)
        self.assertEqual(self.detail()["resume_id"], first)

    def test_graphviews_never_calls_resume_or_detail(self):
        path = os.path.join(os.path.dirname(gv.__file__), "graphviews.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertNotIn("detail", called)
        self.assertFalse({n.func.value.id for n in ast.walk(tree) if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Attribute) and n.func.attr == "resume"
                          and isinstance(n.func.value, ast.Name)} & {"companion", "cv"})
        self.sync()
        self.checkpoint()
        self.sync()
        with mock.patch.object(companion, "resume", side_effect=AssertionError("companion.resume called")), \
                mock.patch.object(cv, "detail", side_effect=AssertionError("detail called")):
            self.assertEqual(self.delta()["state"], "ok")


def _ticket_path(key):
    from chitragupta import store
    return store.ticket_path(key)


if __name__ == "__main__":
    unittest.main()
