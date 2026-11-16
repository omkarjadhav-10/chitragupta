import copy
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from chitragupta import common, profile, store
from chitragupta import graphbuild as gb
from chitragupta import graphmodel as gm
from chitragupta import graphstore as gs
from tests import graphfixture as fx
from tests.support import PY, ROOT, Isolated

GB_PLAN = gb.plan_nodes

A, B = "ABC-1001", "ABC-1002"
REPO = "github:acme.test/shop/api"
SHA = "a" * 40


def graph(ticket=A, label="Add shop order total", extra=False):
    root = gm.node("ticket", ("", ticket), ticket, ticket)
    commit = gm.node("commit", (REPO, SHA), ticket, label, {"short": SHA[:7]}, repo_id=REPO,
                     observed_at="2026-01-05T10:00:00Z", source_ref="commit:" + SHA)
    nodes, edges = [root, commit], [gm.edge("HAS_EVIDENCE", root["id"], commit["id"], ticket, "observed",
                                            evidence_ref="commit:" + SHA, attrs={"how": "commit message"})]
    if extra:
        other = gm.node("commit", (REPO, "b" * 40), ticket, "Fix shop rounding")
        nodes.append(other)
        edges.append(gm.edge("IMPLEMENTED_BY", gm.node_id("plan", "w_" + "1" * 64, "p_" + "2" * 64), other["id"],
                             ticket, "inferred", confidence=0.5, workflow="w_" + "1" * 64))
    requirement = gm.node_id("requirement", root["id"], "d" * 64, 0)
    statuses = [{"requirement": requirement, "workflow": "", "location": "", "revision": "gv_" + "1" * 24,
                 "alias": "R1", "ordinal": 1, "status": "current", "plan": "Unverified",
                 "implementation": "Unverified association", "verification": "Unverified",
                 "detail": {"plan": {"basis": "unknown", "reason": "no plan"}}}]
    return {"nodes": nodes, "edges": edges, "statuses": statuses}


def tree(home):
    return sorted((os.path.join(d, f), os.path.getsize(os.path.join(d, f)))
                  for d, _, files in os.walk(home) for f in files) + sorted(d for d, _, _ in os.walk(home))


def plain(rows):
    return sorted(({k: v for k, v in row.items() if k != "generation"} for row in rows), key=lambda r: r["id"])


class StoreBase(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.addCleanup(gs.reset)

    def writer(self):
        con = gs.open_write()
        self.addCleanup(con.close)
        return con

    def put(self, con, key, g, **meta):
        with con:
            return gs.write_ticket(con, key, g, meta)


class Schema(StoreBase):
    def test_tables_keys_and_indexes(self):
        con = self.writer()
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(names, {"meta", "sources", "nodes", "edges", "requirements"})
        indexes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'index' "
                                             "AND name NOT LIKE 'sqlite%'")}
        self.assertEqual(indexes, {"nodes_type", "edges_type", "edges_src", "edges_dst"})
        self.assertEqual(gs.db_path(), os.path.join(self.home, ".worklog", "cache", "evidence-graph.sqlite"))
        m = gs.meta(con)
        self.assertEqual((m["schema"], m["builder"], m["generation"]), (str(gs.SCHEMA), str(gs.BUILDER), "0"))

    def test_schema_or_builder_bump_recreates_the_tables(self):
        for name in ("SCHEMA", "BUILDER"):
            con = self.writer()
            self.put(con, A, graph())
            self.assertIsNotNone(gs.open_read())   # a reader open on the file
            con.close()
            saved = getattr(gs, name)
            self.addCleanup(setattr, gs, name, saved)
            setattr(gs, name, saved + 1)
            self.assertIsNone(gs.open_read())      # another schema: not built, and nothing repaired by a read
            con = self.writer()
            self.assertEqual(gs.ticket_rows(con, A), ([], [], [], None))
            self.assertEqual(gs.meta(con)[name.lower()], str(saved + 1))
            self.assertEqual(gs.stats()["state"], "ok")
            con.close()
            setattr(gs, name, saved)

    def test_a_garbage_file_is_replaced(self):
        con = self.writer()
        self.put(con, A, graph())
        con.close()
        gs.reset()
        with open(gs.db_path(), "wb") as f:
            f.write(b"this is not a database" * 100)
        self.assertIsNone(gs.open_read())
        self.assertEqual(gs.stats()["state"], "not_built")
        con = self.writer()
        self.assertEqual(gs.ticket_rows(con, A), ([], [], [], None))
        self.assertTrue(self.put(con, A, graph())["changed"])
        self.assertEqual(len(gs.ticket_rows(con, A)[0]), 2)


class Readers(StoreBase):
    def test_a_missing_file_gives_none_and_writes_nothing(self):
        before = tree(self.home)
        self.assertIsNone(gs.open_read())
        self.assertEqual(gs.stats(), {"state": "not_built", "schema": gs.SCHEMA, "builder": gs.BUILDER})
        self.assertEqual(tree(self.home), before)
        self.assertFalse(os.path.exists(gs.db_path()))

    def test_reads_do_not_change_the_file(self):
        con = self.writer()
        self.put(con, A, graph())
        con.close()
        before = tree(self.home)
        reader = gs.open_read()
        self.assertIs(gs.open_read(), reader)   # cached per home
        self.assertEqual(len(gs.ticket_rows(reader, A)[0]), 2)
        stats = gs.stats()
        self.assertEqual((stats["state"], stats["tickets"], stats["nodes"], stats["edges"], stats["requirements"]),
                         ("ok", 1, 2, 1, 1))
        with self.assertRaises(Exception):
            reader.execute("DELETE FROM nodes")
        self.assertEqual(tree(self.home), before)

    def test_reset_closes_the_cached_connection(self):
        self.writer().close()
        reader = gs.open_read()
        gs.reset()
        with self.assertRaises(Exception):
            reader.execute("SELECT 1")
        self.assertIsNot(gs.open_read(), reader)


class Writes(StoreBase):
    def test_round_trip(self):
        con = self.writer()
        g = graph(extra=True)
        res = self.put(con, A, g, sig="s1", profile_generation="pg1", snapshot=3, coverage={"jira": "ok"},
                       built_at="2026-01-05T10:00:00Z")
        nodes, edges, statuses, source = gs.ticket_rows(con, A)
        self.assertEqual(nodes, plain(g["nodes"]))
        self.assertEqual(plain(edges), plain(g["edges"]))
        self.assertEqual(statuses, g["statuses"])
        self.assertEqual({e["generation"] for e in edges}, {1})
        self.assertEqual(source, {"ticket": A, "sig": "s1", "content": gm.content(g["nodes"], g["edges"], g["statuses"]),
                                  "generation": 1, "built_at": "2026-01-05T10:00:00Z", "profile_generation": "pg1",
                                  "snapshot": "3", "coverage": {"jira": "ok"}})
        self.assertEqual(res, {"changed": True, "rows": 6, "generation": 1, "content": source["content"]})
        self.assertEqual(gm.content(nodes, edges, statuses), source["content"])

    def test_the_same_graph_twice_changes_no_row(self):
        con = self.writer()
        first = self.put(con, A, graph(), sig="s1", built_at="2026-01-05T10:00:00Z")
        rows = gs.ticket_rows(con, A)
        changes = con.total_changes
        second = self.put(con, A, graph(), sig="s1", built_at="2026-01-06T10:00:00Z")
        self.assertEqual(con.total_changes, changes)
        self.assertEqual(second, dict(first, changed=False, rows=0))
        self.assertEqual(gs.ticket_rows(con, A), rows)
        self.assertEqual(gs.meta(con)["generation"], "1")
        for table, count in (("nodes", 2), ("edges", 1), ("requirements", 1), ("sources", 1)):
            self.assertEqual(con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0], count)

    def test_only_changed_rows_move_to_the_new_generation(self):
        con = self.writer()
        self.put(con, A, graph(extra=True))
        res = self.put(con, A, graph(label="Add shop order total, with tax"))
        self.assertEqual((res["changed"], res["rows"], res["generation"]), (True, 3, 2))   # 1 changed, 2 removed
        stored = dict(con.execute("SELECT label, generation FROM nodes WHERE ticket = ?", (A,)).fetchall())
        self.assertEqual(stored, {A: 1, "Add shop order total, with tax": 2})
        self.assertEqual(len(gs.ticket_rows(con, A)[1]), 1)
        changed = graph()
        changed["statuses"][0]["verification"] = "Partial"
        self.assertEqual(self.put(con, A, changed)["rows"], 2)   # the node label back, the status row
        self.assertEqual(gs.ticket_rows(con, A)[2][0]["verification"], "Partial")
        self.assertEqual(gs.ticket_rows(con, A)[3]["generation"], 3)

    def test_a_new_signature_alone_keeps_the_generation(self):
        con = self.writer()
        self.put(con, A, graph(), sig="s1")
        res = self.put(con, A, graph(), sig="s2")
        self.assertEqual((res["changed"], res["generation"]), (False, 1))
        self.assertEqual(gs.ticket_rows(con, A)[3]["sig"], "s2")

    def test_a_shared_node_is_stored_per_ticket(self):
        con = self.writer()
        self.put(con, A, graph(A))
        self.put(con, B, graph(B))
        shared = gm.node_id("commit", REPO, SHA)
        self.assertEqual(con.execute("SELECT ticket FROM nodes WHERE id = ? ORDER BY ticket", (shared,)).fetchall(),
                         [(A,), (B,)])
        with con:
            self.assertEqual(gs.delete_ticket(con, A), 5)
        self.assertEqual(gs.ticket_rows(con, A), ([], [], [], None))
        nodes, edges, statuses, source = gs.ticket_rows(con, B)
        self.assertEqual((len(nodes), len(edges), len(statuses)), (2, 1, 1))
        self.assertIn(shared, [n["id"] for n in nodes])
        self.assertEqual(gs.meta(con)["generation"], "3")
        with con:
            self.assertEqual(gs.delete_ticket(con, A), 0)
        self.assertEqual(gs.meta(con)["generation"], "3")

    def test_rows_of_another_ticket_are_refused(self):
        con = self.writer()
        with self.assertRaises(ValueError):
            self.put(con, A, graph(B))
        self.assertEqual(gs.ticket_rows(con, A), ([], [], [], None))   # the caller's transaction rolled back
        twice = graph()
        twice["statuses"].append(dict(twice["statuses"][0], alias="R2"))
        with self.assertRaises(ValueError):
            self.put(con, A, twice)
        self.assertEqual(gs.ticket_rows(con, A), ([], [], [], None))

    def test_the_caller_owns_the_transaction(self):
        con = self.writer()
        gs.write_ticket(con, A, graph())
        con.rollback()
        self.assertEqual(gs.ticket_rows(con, A), ([], [], [], None))
        self.assertEqual(gs.meta(con)["generation"], "0")


AT = "2026-01-05T10:00:00Z"


class Boom(Exception):
    pass


class HistoryBase(StoreBase):
    def setUp(self):
        super().setUp()
        self.con = self.writer()
        self.history = gs.new_history(A)
        self.calls = 0

    def pub(self, g, **kw):
        out = gs.publish(self.con, A, g, self.history, kw.pop("sig", "s"), "pg1", AT, **kw)
        self.history = out["history"]
        return out

    def rebuild(self, g, **extra):
        def run(history):
            self.calls += 1
            return dict({"graph": g, "sig": "s", "profile_generation": "pg1", "at": AT}, **extra)
        return run

    def file(self):
        state, data = gs.load_history(A)
        self.assertEqual(state, "ok")
        return data

    def seqs(self):
        return [e["seq"] for e in self.file()["journal"]]

    def in_step(self):
        head, source = self.file()["head"], gs.ticket_rows(self.con, A)[3]
        self.assertEqual((source["snapshot"], source["content"]), (str(head["seq"]), head["content"]))
        return head


class HistoryFile(HistoryBase):
    def test_path_and_key_check(self):
        self.assertEqual(gs.history_path(A), os.path.join(self.home, "data", "graph", A + ".json"))
        for bad in ("../ABC-1001", "abc-1", "ABC", "", None, "ABC-1/x"):
            with self.assertRaises(ValueError):
                gs.history_path(bad)

    def test_missing_ok_and_corrupt_are_told_apart(self):
        self.assertEqual(gs.load_history(A), ("missing", None))
        self.pub(graph())
        state, data = gs.load_history(A)
        self.assertEqual((state, data["ticket"], data["next_seq"], data["head"]["seq"]), ("ok", A, 2, 1))
        good = dict(data)
        for bad in (b"{not json", b"", b"[]", b"\xff\xfe\x00", None):
            with open(gs.history_path(A), "wb") as f:
                f.write(bad if bad is not None else b"{}")
            self.assertEqual(gs.load_history(A), ("corrupt", None))
        for change in ({"schema_version": 2}, {"ticket": B}, {"next_seq": 1}, {"next_seq": "2"}, {"journal": {}},
                       {"requirements": None}, {"head": {"seq": 1}}, {"journal": [{"seq": 1}]}):
            common_write(gs.history_path(A), dict(good, **change))
            self.assertEqual(gs.load_history(A), ("corrupt", None), change)
        common_write(gs.history_path(A), good)
        self.assertEqual(gs.load_history(A)[0], "ok")

    def test_state_leaves_out_code_changes_and_non_evidence(self):
        g = graph(extra=True)
        change = gm.node("code_change", (g["nodes"][1]["id"], "shop/orders.py"), A, "shop/orders.py")
        g["nodes"].append(change)
        state = gs.state_of(g)
        commits = [n for n in g["nodes"] if n["type"] == "commit"]
        self.assertEqual(sorted(state["items"]), sorted(n["id"] for n in commits))
        first = commits[0]
        self.assertEqual(state["items"][first["id"]], ["commit", first["version"], "", ""])
        row = g["statuses"][0]
        self.assertEqual(state["requirements"], {row["requirement"] + "|": [
            row["revision"], "Unverified", "Unverified association", "Unverified"]})


class Delta(HistoryBase):
    def test_added_removed_changed_and_status(self):
        old = gs.state_of(graph(extra=True))
        changed = graph(label="Add shop order total, with tax")
        changed["statuses"][0]["verification"] = "Partial"
        d = gs.delta(old, gs.state_of(changed), {"modified": ["gr_x"]}, {"from": "current", "to": "partial"})
        kept, gone = gm.node_id("commit", REPO, SHA), gm.node_id("commit", REPO, "b" * 40)
        self.assertEqual(d["evidence"], {"added": [], "removed": [[gone, "commit", "", ""]],
                                         "changed": [[kept, "commit", "", ""]]})
        self.assertEqual(d["status"], [{"requirement": changed["statuses"][0]["requirement"], "workflow": "",
                                        "dimension": "verification", "from": "Unverified", "to": "Partial"}])
        self.assertEqual(d["requirements"], {"added": [], "modified": ["gr_x"], "removed": [], "restored": []})
        self.assertEqual((d["source"], d["more"]), ({"from": "current", "to": "partial"}, 0))
        same = gs.delta(old, old)
        self.assertEqual((same["evidence"], same["status"], same["source"], same["more"]),
                         ({"added": [], "removed": [], "changed": []}, [], None, 0))

    def test_first_entry_lists_what_is_present_without_transitions(self):
        d = gs.delta(None, gs.state_of(graph()))
        self.assertEqual(len(d["evidence"]["added"]), 1)
        self.assertEqual(d["status"], [])

    def test_lists_are_capped_with_more(self):
        nodes = [gm.node("commit", (REPO, "%040x" % i), A, "c%d" % i) for i in range(150)]
        d = gs.delta({"requirements": {}, "items": {}}, gs.state_of({"nodes": nodes}),
                     {"added": ["gr_%d" % i for i in range(103)]})
        self.assertEqual((len(d["evidence"]["added"]), len(d["requirements"]["added"]), d["more"]), (100, 100, 53))
        self.assertEqual(d["evidence"]["added"], sorted(d["evidence"]["added"]))

    def test_coarse_above_the_item_bound(self):
        saved = gs.MAX_ITEMS
        self.addCleanup(setattr, gs, "MAX_ITEMS", saved)
        gs.MAX_ITEMS = 1
        state = gs.state_of(graph(extra=True))
        self.assertEqual((state["items"], state["coarse"], state["counts"]), ({}, True, {"commit": 2}))
        d = gs.delta(gs.state_of(graph()), state)
        self.assertEqual((d["coarse"], d["counts"]["to"], d["evidence"]["added"]), (True, {"commit": 2}, []))
        out = self.pub(graph(extra=True))
        self.assertTrue(out["history"]["head"]["state"]["coarse"])
        self.assertEqual(gs.load_history(A)[0], "ok")


class Publication(HistoryBase):
    def test_publish_writes_both_in_step(self):
        out = self.pub(graph(), coverage={"jira": "ok"})
        self.assertEqual((out["seq"], out["changed"], out["dropped"]), (1, True, 0))
        head = self.in_step()
        self.assertEqual((head["seq"], head["profile_generation"]), (1, "pg1"))
        data = self.file()
        self.assertEqual(data["next_seq"], 2)
        entry = data["journal"][0]
        self.assertEqual((entry["seq"], entry["prev"], entry["at"], entry["content"]), (1, None, AT, head["content"]))
        self.assertNotIn("baseline", entry)
        self.assertEqual(gs.ticket_rows(self.con, A)[3]["coverage"], {"jira": "ok"})
        self.assertEqual(self.history, data)

    def test_a_b_a_gives_three_sequences_two_with_equal_content(self):
        a, b = graph(), graph(extra=True)
        for g in (a, b, a):
            self.pub(g)
        journal = self.file()["journal"]
        self.assertEqual([(e["seq"], e["prev"]) for e in journal], [(1, None), (2, 1), (3, 2)])
        self.assertEqual(journal[0]["content"], journal[2]["content"])
        self.assertNotEqual(journal[0]["content"], journal[1]["content"])
        self.assertEqual(len(journal[1]["delta"]["evidence"]["added"]), 1)
        self.assertEqual(len(journal[2]["delta"]["evidence"]["removed"]), 1)
        self.assertEqual(self.in_step()["seq"], 3)

    def test_requirements_part_travels_in_the_same_write(self):
        self.history["requirements"] = {"next_ordinal": 2, "snapshots": [{"seq": i} for i in range(60)]}
        self.pub(graph(), requirements_change={"added": ["gr_1"]})
        data = self.file()
        self.assertEqual(data["requirements"]["next_ordinal"], 2)
        self.assertEqual([s["seq"] for s in data["requirements"]["snapshots"]], list(range(10, 60)))
        self.assertEqual(data["journal"][0]["delta"]["requirements"]["added"], ["gr_1"])

    def test_entry_201_drops_the_oldest(self):
        a, b = graph(), graph(extra=True)
        for i in range(201):
            self.pub(a if i % 2 else b)
        seqs = self.seqs()
        self.assertEqual((len(seqs), seqs[0], seqs[-1]), (200, 2, 201))
        self.assertEqual(self.file()["next_seq"], 202)
        self.assertEqual(self.in_step()["seq"], 201)

    def test_file_size_bound_drops_the_oldest_and_reports_it(self):
        for g in (graph(), graph(extra=True), graph(), graph(extra=True)):
            self.pub(g)
        saved = gs.MAX_BYTES
        self.addCleanup(setattr, gs, "MAX_BYTES", saved)
        gs.MAX_BYTES = 1500
        out = self.pub(graph())
        self.assertGreater(out["dropped"], 0)
        self.assertEqual(self.seqs()[-1], 5)
        self.assertLess(len(self.seqs()), 5)
        self.assertIn("size bound", gs.meta(self.con)["last_error"])
        self.assertEqual(self.in_step()["seq"], 5)


class Crashes(HistoryBase):
    def setUp(self):
        super().setUp()
        self.a, self.b = graph(), graph(extra=True)
        self.pub(self.a)

    def previous_is_readable(self, seq=1):
        nodes, edges, statuses, source = gs.ticket_rows(self.con, A)
        self.assertEqual((len(nodes), len(edges), len(statuses), source["snapshot"]), (2, 1, 1, str(seq)))

    def test_crash_before_publish_1(self):
        with mock.patch.object(gs.common, "write_json", side_effect=Boom):
            with self.assertRaises(Boom):
                self.pub(self.b)
        self.assertEqual(self.seqs(), [1])
        self.previous_is_readable()
        self.assertEqual(gs.recover(self.con, A, self.rebuild(self.b))["action"], "none")
        self.assertEqual(self.pub(self.b)["seq"], 2)
        self.assertEqual(self.seqs(), [1, 2])
        self.assertEqual(self.in_step()["seq"], 2)

    def test_crash_between_publish_1_and_2(self):
        with mock.patch.object(gs, "write_ticket", side_effect=Boom):
            with self.assertRaises(Boom):
                self.pub(self.b)
        self.assertEqual(self.seqs(), [1, 2])    # the file is ahead
        self.previous_is_readable()              # readers still get the previous complete rows
        res = gs.recover(self.con, A, self.rebuild(self.b))
        self.assertEqual((res["action"], res["seq"]), ("index", 2))
        self.assertEqual(self.seqs(), [1, 2])    # not lost, not duplicated
        self.assertEqual(self.in_step()["seq"], 2)
        self.assertEqual(len(gs.ticket_rows(self.con, A)[0]), 3)
        self.assertEqual(gs.recover(self.con, A, self.rebuild(self.b))["action"], "none")
        self.assertEqual(self.calls, 1)          # nothing is rebuilt once consistent

    def test_crash_inside_the_sql_transaction(self):
        real, seen = gs._sync, []

        def failing(con, table, *rest):
            seen.append(table)
            if table == "edges":
                raise Boom()
            return real(con, table, *rest)

        with mock.patch.object(gs, "_sync", side_effect=failing):
            with self.assertRaises(Boom):
                self.pub(self.b)
        self.assertEqual(seen, ["nodes", "edges"])
        self.previous_is_readable()              # the node rows written before the failure were rolled back
        self.assertEqual(gs.recover(self.con, A, self.rebuild(self.b))["action"], "index")
        self.assertEqual(self.seqs(), [1, 2])
        self.assertEqual(self.in_step()["seq"], 2)

    def test_inputs_moved_on_while_the_file_was_ahead(self):
        with mock.patch.object(gs, "write_ticket", side_effect=Boom):
            with self.assertRaises(Boom):
                self.pub(self.b)
        res = gs.recover(self.con, A, self.rebuild(graph(label="Add shop order total, with tax")))
        self.assertEqual((res["action"], res["seq"]), ("publish", 3))
        journal = self.file()["journal"]
        self.assertEqual([(e["seq"], e["prev"]) for e in journal], [(1, None), (2, 1), (3, 2)])
        self.assertEqual(len(journal[2]["delta"]["evidence"]["removed"]), 1)   # against head.state, not SQLite rows
        self.assertEqual(self.in_step()["seq"], 3)

    def test_crash_inside_recovery(self):
        with mock.patch.object(gs, "write_ticket", side_effect=Boom):
            with self.assertRaises(Boom):
                self.pub(self.b)
            before = gs.common.read_text(gs.history_path(A))
            with self.assertRaises(Boom):
                gs.recover(self.con, A, self.rebuild(self.b))
        self.assertEqual(gs.common.read_text(gs.history_path(A)), before)
        self.previous_is_readable()
        self.assertEqual(gs.recover(self.con, A, self.rebuild(self.b))["action"], "index")
        self.assertEqual(self.seqs(), [1, 2])
        self.assertEqual(self.in_step()["seq"], 2)


class Recovery(HistoryBase):
    def test_nothing_built_yet(self):
        res = gs.recover(self.con, A, self.rebuild(graph()))
        self.assertEqual((res["action"], res["reason"], self.calls), ("none", "not built", 0))
        self.assertFalse(os.path.exists(gs.history_path(A)))

    def test_a_deleted_index_is_rebuilt_without_a_journal_entry(self):
        self.pub(graph())
        self.pub(graph(extra=True))
        before = gs.common.read_text(gs.history_path(A))
        self.con.close()
        gs.reset()
        os.remove(gs.db_path())
        self.con = self.writer()
        self.assertEqual(gs.ticket_rows(self.con, A), ([], [], [], None))
        res = gs.recover(self.con, A, self.rebuild(graph(extra=True)))
        self.assertEqual((res["action"], res["seq"]), ("index", 2))
        self.assertEqual(gs.common.read_text(gs.history_path(A)), before)
        self.assertEqual(self.in_step()["seq"], 2)

    def test_an_unreadable_file_is_kept_aside_and_reported(self):
        self.pub(graph())
        self.pub(graph(extra=True))
        with open(gs.history_path(A), "wb") as f:
            f.write(b"{broken")
        res = gs.recover(self.con, A, self.rebuild(graph(extra=True)))
        self.assertEqual((res["action"], res["reason"], res["seq"]), ("reset", "history file unreadable", 3))
        folder = os.path.dirname(gs.history_path(A))
        aside = [n for n in os.listdir(folder) if n.startswith(A + ".json.corrupt-")]
        self.assertEqual(len(aside), 1)
        with open(os.path.join(folder, aside[0]), "rb") as f:
            self.assertEqual(f.read(), b"{broken")
        self.assertIn(aside[0], gs.meta(self.con)["last_error"])
        self.assertEqual(res["error"], gs.meta(self.con)["last_error"])
        journal = self.file()["journal"]
        self.assertEqual([(e["seq"], e["prev"], e.get("baseline")) for e in journal], [(3, None, "reset")])
        self.assertEqual(journal[0]["delta"]["evidence"], {"added": [], "removed": [], "changed": []})
        self.assertEqual(self.in_step()["seq"], 3)
        self.assertEqual(gs.recover(self.con, A, self.rebuild(graph(extra=True)))["action"], "none")

    def test_a_crash_after_moving_the_file_still_ends_in_a_reset(self):
        self.pub(graph())
        self.con.close()
        gs.reset()
        os.remove(gs.db_path())
        self.con = self.writer()
        with open(gs.history_path(A), "wb") as f:
            f.write(b"{broken")

        def crash(history):
            raise Boom()

        with self.assertRaises(Boom):
            gs.recover(self.con, A, crash)
        self.assertEqual(gs.load_history(A), ("missing", None))   # moved aside, no index row either
        res = gs.recover(self.con, A, self.rebuild(graph()))
        self.assertEqual((res["action"], res["reason"]), ("reset", "history file missing"))
        self.assertEqual(self.file()["journal"][0]["baseline"], "reset")

    def test_a_missing_file_with_index_rows_resets(self):
        self.pub(graph())
        self.pub(graph(extra=True))
        os.remove(gs.history_path(A))
        res = gs.recover(self.con, A, self.rebuild(graph(extra=True)))
        self.assertEqual((res["action"], res["reason"], res["seq"]), ("reset", "history file missing", 3))
        self.assertEqual([(e["seq"], e.get("baseline")) for e in self.file()["journal"]], [(3, "reset")])
        self.assertEqual(self.in_step()["seq"], 3)

    def test_an_older_file_than_the_index_resets_without_reusing_a_sequence(self):
        self.pub(graph())
        older = gs.common.read_text(gs.history_path(A))
        self.pub(graph(extra=True))
        self.pub(graph())
        with open(gs.history_path(A), "w", encoding="utf-8", newline="") as f:
            f.write(older)   # restored from a backup taken at sequence 1
        res = gs.recover(self.con, A, self.rebuild(graph()))
        self.assertEqual((res["action"], res["reason"], res["seq"]), ("reset", "index ahead of the file", 4))
        journal = self.file()["journal"]
        self.assertEqual([(e["seq"], e["prev"], e.get("baseline")) for e in journal],
                         [(1, None, None), (4, None, "reset")])
        self.assertEqual(self.in_step()["seq"], 4)
        self.assertEqual(gs.recover(self.con, A, self.rebuild(graph()))["action"], "none")

    def test_rebuild_may_replace_the_requirements_part(self):
        self.pub(graph())
        os.remove(gs.history_path(A))
        gs.recover(self.con, A, self.rebuild(graph(), requirements={"next_ordinal": 7},
                                             requirements_change={"added": ["gr_1"]}, coverage={"jira": "ok"}))
        data = self.file()
        self.assertEqual(data["requirements"], {"next_ordinal": 7})
        self.assertEqual(data["journal"][0]["delta"]["requirements"]["added"], ["gr_1"])
        self.assertEqual(gs.ticket_rows(self.con, A)[3]["coverage"], {"jira": "ok"})


FILTER, EXPORT = "Orders can be filtered by status", "The filtered list can be exported as a spreadsheet"
STEPS = [{"id": "one", "text": "Add the order filter", "criteria": ["ac1"]},
         {"id": "two", "text": "Add the export", "criteria": ["ac2"]},
         {"id": "check", "text": "Verify both", "kind": "verification", "criteria": ["ac1", "ac2"],
          "depends_on": ["one", "two"]}]


def everything(home):
    """Every file under the home with its bytes: 'byte-identical' means this is equal."""
    out = {}
    for d, _, files in os.walk(home):
        for f in files:
            with open(os.path.join(d, f), "rb") as fh:
                out[os.path.join(d, f)] = fh.read()
    return out


class SyncBase(fx.Workflows):
    """A synthetic home: a card, a ticket record with a commit and test runs, one approved workflow with a pin."""

    def setUp(self):
        super().setUp()
        self.addCleanup(gs.reset)
        fx.write_card(common.home(), A, [FILTER, EXPORT])
        self.rec = self.observe(self.associate(self.workflow([FILTER, EXPORT], steps=STEPS, approve=True)))
        self.captured_test(self.rec, "t1")
        self.captured_test(self.rec, "t2", status="failed")
        self.rec = self.map(self.rec, evidence="test:t1", criteria=["ac1"], coverage="complete")
        self.put(A, commits={c["sha"]: c for c in [fx.commit_rec(1, "ABC-1001 Add the order filter", repo="scratch",
                                                                 files=["src/orders/list.py"])]})
        self.cfg = common.load_config()

    def sync(self, **kw):
        return gs.sync(kw.pop("cfg", self.cfg), **kw)

    def rows(self, key=A):
        con = gs.open_write()
        try:
            return gs.ticket_rows(con, key)
        finally:
            con.close()

    def by_type(self, nodes, kind):
        return [n for n in nodes if n["type"] == kind]

    def ticket_level(self, statuses, text):
        index = {n["id"]: n for n in self.rows()[0]}
        for row in statuses:
            if row["workflow"] == "" and index[gb.revision_id(row["requirement"], row["revision"])]["attrs"]["text"] == text:
                return row
        raise AssertionError("no ticket-level row for %r" % text)


class Sync(SyncBase):
    def test_the_graph_holds_every_part(self):
        report = self.sync()
        self.assertEqual((sorted(report["built"]), report["errors"], report["purged"]), ([A, B], [], True))
        nodes, edges, statuses, source = self.rows()
        types = {n["type"] for n in nodes}
        self.assertTrue({"ticket", "requirement", "requirement_revision", "plan", "plan_step", "commit", "code_change",
                         "test_case", "test_run"} <= types, types)
        self.assertEqual(len(self.by_type(nodes, "requirement")), 2)
        have = {n["id"] for n in nodes}
        self.assertEqual([e["id"] for e in edges if e["src"] not in have or e["dst"] not in have], [])
        self.assertEqual(len([e for e in edges if e["type"] == "HAS_REQUIREMENT"]), 2)
        self.assertEqual(len([e for e in edges if e["type"] == "SUPPORTED_BY"]), 1)
        self.assertEqual({(r["workflow"] == "") for r in statuses}, {True, False})
        self.assertEqual(len(statuses), 4)   # two requirements, each with a workflow row and a ticket-level row
        self.assertEqual(self.ticket_level(statuses, FILTER)["verification"], "Verified within recorded scope")
        self.assertEqual(self.ticket_level(statuses, EXPORT)["verification"], "Unverified")
        self.assertEqual(source["coverage"]["workflows"], [self.rec["id"]])
        self.assertEqual(gs.load_history(A)[1]["head"]["content"], source["content"])

    def test_two_builds_of_the_same_home_are_identical(self):   # EG-A01
        first = gb.ticket_graph(gb.load_inputs(A, self.cfg))
        second = gb.ticket_graph(gb.load_inputs(A, self.cfg))
        self.assertEqual(first, second)
        self.sync()
        before = self.rows()
        journal = gs.load_history(A)[1]["journal"]
        gs.reset()
        os.remove(gs.db_path())   # the index is derived: build it again from the same home
        report = self.sync()
        self.assertEqual(sorted(report["recovered"]), [A, B])
        after = self.rows()
        self.assertEqual([plain(before[0]), plain(before[1]), before[2]], [plain(after[0]), plain(after[1]), after[2]])
        self.assertEqual(before[3]["content"], after[3]["content"])
        self.assertEqual(gs.load_history(A)[1]["journal"], journal)   # no new journal entry

    def test_second_sync_writes_nothing(self):
        self.sync()
        before = everything(common.home())
        report = self.sync()
        self.assertEqual(everything(common.home()), before)
        self.assertEqual((report["built"], report["recovered"], report["unchanged"], report["deleted"]), ([], [], [], []))
        report = self.sync(keys=[A])   # a dirty ticket whose inputs did not change: checked, not rewritten
        self.assertEqual(everything(common.home()), before)
        self.assertEqual(report["built"], [])

    def test_changing_only_the_criteria_fields_triggers_a_rebuild(self):
        self.sync()
        sig = self.rows()[3]["sig"]
        cfg = dict(self.cfg, companion=dict(self.cfg.get("companion") or {}, criteria_fields=["customfield_10001"]))
        self.assertEqual(gs.common.load_config().get("companion"), self.cfg.get("companion"))
        report = self.sync(cfg=cfg)
        self.assertFalse(report["purged"])
        self.assertIn(A, report["unchanged"] + report["built"])
        self.assertNotEqual(self.rows()[3]["sig"], sig)
        self.assertEqual(self.sync(cfg=cfg)["unchanged"] + self.sync(cfg=cfg)["built"], [])

    def test_a_ticket_that_left_the_scope_has_no_rows(self):
        self.sync()
        self.assertTrue(self.rows(B)[0])
        os.remove(store.ticket_path(B))   # no record of your work on it: outside the personal projection
        report = self.sync()
        self.assertEqual(report["deleted"], [B])
        self.assertEqual(self.rows(B), ([], [], [], None))
        self.assertTrue(self.rows(A)[0])
        self.assertTrue(os.path.exists(gs.history_path(B)))   # the canonical file stays

    def test_a_profile_change_purges_everything_first(self):
        self.sync()
        cfg = copy.deepcopy(self.cfg)
        cfg["profile"]["include_tickets"]["ABC-1003"] = "Third task"
        self.assertNotEqual(profile.signature(cfg), profile.signature(self.cfg))
        report = self.sync(cfg=cfg, limit=0)
        self.assertEqual((report["purged"], report["more"], report["built"]), (True, True, []))
        stats = gs.stats()
        self.assertEqual((stats["tickets"], stats["nodes"], stats["edges"], stats["requirements"]), (0, 0, 0, 0))
        self.assertEqual(gs.meta(gs.open_read())["profile_generation"], profile.signature(cfg))
        report = self.sync(cfg=cfg)
        self.assertFalse(report["purged"])
        self.assertEqual(sorted(report["recovered"] + report["built"]), [A, B])
        self.assertEqual(self.rows()[3]["coverage"]["workflows"], [])   # the workflow is bound to the old profile

    def test_a_pruned_unpinned_test_record_leaves_no_node_and_no_dangling_edge(self):   # EG-A23
        self.sync()
        nodes = self.rows()[0]
        self.assertEqual(sorted(n["attrs"]["evidence"] for n in self.by_type(nodes, "test_run")), ["test:t1", "test:t2"])
        tk = self.tk(A)
        del tk["tests"]["t2"], tk["tests"]["t1"]   # t1 is pinned by the workflow, t2 is not
        common.write_json(store.ticket_path(A), tk)
        self.assertEqual(self.sync()["built"], [A])
        nodes, edges, _, _ = self.rows()
        runs = {n["attrs"]["evidence"]: n for n in self.by_type(nodes, "test_run")}
        self.assertEqual(sorted(runs), ["test:t1"])
        self.assertEqual((runs["test:t1"]["lifecycle"], runs["test:t1"]["attrs"]["note"]),
                         ("source_missing", gb.PINNED_COPY))
        have = {n["id"] for n in nodes}
        self.assertEqual([e["id"] for e in edges if e["src"] not in have or e["dst"] not in have], [])

    def test_dropped_edges_are_counted(self):
        inputs = gb.load_inputs(A, self.cfg)
        real = gb.ticket_graph
        with mock.patch.object(gb, "plan_nodes", lambda w, i: dict(gb_plan(w, i), nodes=[])):
            built = real(inputs)
        self.assertGreater(built["dropped"], 0)
        self.assertEqual(built["coverage"]["dropped_edges"], built["dropped"])
        ids = {n["id"] for n in built["nodes"]}
        self.assertTrue(all(e["src"] in ids and e["dst"] in ids for e in built["edges"]))

    def test_no_process_network_or_model_call(self):
        def no_socket(*args, **kwargs):
            raise AssertionError("the graph step opened a connection")
        with mock.patch("socket.socket", no_socket), mock.patch("socket.create_connection", no_socket):
            report = gs.tick(self.cfg, {A})
        self.assertEqual(sorted(report["built"]), [A, B])
        self.assertEqual(self.processes, [])

    def test_nothing_happens_while_paused(self):
        common.write_text(common.wl("PAUSED"), "")
        self.assertEqual(gs.tick(self.cfg, {A}), {"paused": True})
        self.assertFalse(os.path.exists(gs.db_path()))
        self.assertFalse(os.path.exists(gs.history_path(A)))

    def test_the_bound_and_the_rotating_cursor(self):
        report = self.sync(limit=1)
        self.assertEqual((report["built"], report["more"]), ([A], True))
        self.assertEqual(gs.meta(gs.open_read())["cursor"], A)
        report = self.sync(limit=1)   # starts after the cursor; stops before A, which it has not looked at
        self.assertEqual((report["built"], report["more"]), ([B], True))
        self.assertEqual(gs.meta(gs.open_read())["cursor"], B)
        report = self.sync(limit=1)   # nothing left to build: the whole round fits and the cursor clears
        self.assertEqual((report["built"], report["more"]), ([], False))
        self.assertEqual(gs.meta(gs.open_read())["cursor"], "")

    def test_a_failing_ticket_does_not_stop_the_others(self):
        real = gb.ticket_graph

        def broken(inputs):
            if inputs["key"] == A:
                raise RuntimeError("synthetic failure")
            return real(inputs)
        with mock.patch.object(gb, "ticket_graph", broken):
            report = self.sync()
        self.assertEqual((report["errors"], report["built"]), ([A], [B]))
        self.assertEqual(self.sync()["built"], [A])


SYNC_CHILD = r"""
import json, os, sys, time
from chitragupta import common, graphstore
folder, tag = sys.argv[1], sys.argv[2]
open(os.path.join(folder, "ready-" + tag), "w").close()
deadline = time.monotonic() + 60  # never outlive a failed parent
while not os.path.exists(os.path.join(folder, "go")):
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.005)
reports = []
for _ in range(200):   # a busy run is skipped; try again until this process has done one real run
    out = graphstore.sync(common.load_config())
    reports.append(out)
    if not out["busy"]:
        break
    time.sleep(0.01)
print(json.dumps(reports))
"""

LOCK_CHILD = r"""
import os, sys, time
from chitragupta.agent import Lock
folder = sys.argv[1]
lock = Lock()
if not lock.acquire():
    sys.exit(4)
open(os.path.join(folder, "held"), "w").close()
deadline = time.monotonic() + 60
while not os.path.exists(os.path.join(folder, "done")) and time.monotonic() < deadline:
    time.sleep(0.01)
lock.release()
"""
LEDGER = {"Verified": "Verified within recorded scope", "Partial": "Partial", "Missing": "Failed",
          "Unverified": "Unverified"}


class Boomed(Exception):
    pass


class Rebuild(SyncBase):
    def seqs(self, key=A):
        return [(e["seq"], e.get("baseline")) for e in gs.load_history(key)[1]["journal"]]

    def consistent(self, key=A):
        head, source = gs.load_history(key)[1]["head"], self.rows(key)[3]
        self.assertEqual((source["snapshot"], source["content"]), (str(head["seq"]), head["content"]))

    def signals(self):
        folder = os.path.join(self.user_dir, "signals-%d" % len(os.listdir(self.user_dir)))
        os.makedirs(folder)
        return folder

    def child(self, script, *args):
        from tests.test_companion_state import kill
        proc = subprocess.Popen([PY, "-c", script] + list(args), cwd=ROOT, env=self.child_env(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(kill, proc)   # hard deadline: no child outlives the test
        return proc

    def wait_for(self, path, procs):
        deadline = time.monotonic() + 60
        while not os.path.exists(path):
            self.assertLess(time.monotonic(), deadline, "child did not start")
            self.assertTrue(all(p.poll() is None for p in procs), "a child exited early")
            time.sleep(0.02)

    def test_rebuild_replaces_the_tables_and_keeps_every_journal(self):
        self.sync()
        rows, journals = self.rows(), {k: self.seqs(k) for k in (A, B)}
        report = gs.rebuild(self.cfg)
        self.assertEqual((report["skipped"], sorted(report["recovered"]), report["errors"]), (None, [A, B], []))
        after = self.rows()
        self.assertEqual([plain(rows[0]), plain(rows[1]), rows[2], rows[3]["content"]],
                         [plain(after[0]), plain(after[1]), after[2], after[3]["content"]])
        self.assertEqual({k: self.seqs(k) for k in (A, B)}, journals)   # re-indexed at their own sequence
        self.assertTrue(gs.stats()["full_at"])
        self.assertFalse(os.path.exists(common.wl("locks", "agent.lock")))
        self.assertFalse(os.path.exists(common.wl("locks", gs.GRAPH_LOCK)))

    def test_a_rebuild_after_each_crash_point_keeps_the_journal(self):   # EG-A17
        self.sync()
        failing_sync = gs._sync

        def in_transaction(con, table, *rest):
            if table == "edges":
                raise Boomed()
            return failing_sync(con, table, *rest)

        points = {"before publish 1": lambda: mock.patch.object(gs.common, "write_json", side_effect=Boomed),
                  "between publish 1 and 2": lambda: mock.patch.object(gs, "write_ticket", side_effect=Boomed),
                  "inside the SQL transaction": lambda: mock.patch.object(gs, "_sync", side_effect=in_transaction),
                  "inside recovery": lambda: mock.patch.object(gs, "write_ticket", side_effect=Boomed)}
        for n, (point, crash) in enumerate(points.items(), 2):
            with self.subTest(point):
                kept = self.seqs()
                commits = dict(self.tk(A)["commits"])
                extra = fx.commit_rec(n, "ABC-1001 Step %d" % n, repo="scratch", files=["src/orders/step%d.py" % n])
                commits[extra["sha"]] = extra
                self.put(A, commits=commits)   # the inputs moved: the next sync publishes
                with crash():
                    self.assertIn(A, self.sync()["errors"])
                    if point == "inside recovery":   # the repair itself crashes once more
                        self.assertIn(A, gs.rebuild(self.cfg)["errors"])
                report = gs.rebuild(self.cfg)
                self.assertEqual((report["skipped"], report["errors"]), (None, []))
                self.assertIn(A, report["recovered"] + report["built"])
                now = self.seqs()
                self.assertEqual(now[:len(kept)], kept, "a retained journal position was lost or rewritten")
                self.assertEqual([b for _, b in now], [None] * len(now), "no reset baseline")
                self.assertEqual([s for s, _ in now], sorted({s for s, _ in now}))
                self.assertEqual(len(now), len(kept) + 1)   # exactly one entry for the change
                self.consistent(A)
                self.consistent(B)
                self.assertEqual(self.sync()["built"], [])

    def test_a_pinned_pruned_source_stays_labelled_and_counts_as_the_ledger_counts_it(self):   # EG-A23
        self.sync()

        def workflow_row():
            for row in self.rows()[2]:
                if row["workflow"] == self.rec["id"] and row["alias"] == "ac1":
                    return row
            raise AssertionError("no workflow row for ac1")

        before = workflow_row()
        run = next(n for n in self.rows()[0] if n["type"] == "test_run" and n["attrs"]["evidence"] == "test:t1")
        tk = self.tk(A)
        del tk["tests"]["t1"]   # pinned by the workflow: the record is gone, the pin keeps its copy
        common.write_json(store.ticket_path(A), tk)
        self.assertEqual(self.sync()["built"], [A])
        for when in ("after sync", "after rebuild"):
            with self.subTest(when):
                nodes, edges, _, _ = self.rows()
                copy_ = next(n for n in nodes if n["id"] == run["id"])   # the same identity as the live source
                self.assertEqual((copy_["lifecycle"], copy_["attrs"]["note"], copy_["attrs"]["pinned"]),
                                 ("source_missing", gb.PINNED_COPY, True))
                self.assertTrue([e for e in edges if e["type"] == "SUPPORTED_BY" and e["dst"] == run["id"]])
                row = workflow_row()
                ledger = row["detail"]["verification"]["ledger"]
                self.assertEqual(ledger, before["detail"]["verification"]["ledger"])
                self.assertEqual(row["verification"], LEDGER[ledger])
                delta = gs.load_history(A)[1]["journal"][-1]["delta"]["evidence"]
                self.assertNotIn(run["id"], [r[0] for r in delta["added"]])   # never a new observation
                self.assertIn(run["id"], [r[0] for r in delta["changed"]])
                self.assertEqual(gs.rebuild(self.cfg)["errors"], [])

    def test_with_the_lock_held_by_another_process_rebuild_is_skipped_and_writes_nothing(self):
        self.sync()
        folder = self.signals()
        proc = self.child(LOCK_CHILD, folder)
        self.wait_for(os.path.join(folder, "held"), [proc])
        before = everything(common.home())
        report = gs.rebuild(self.cfg, wait_s=0)
        self.assertEqual(report["skipped"], gs.SKIPPED)
        self.assertEqual((report["built"], report["recovered"]), ([], []))
        self.assertEqual(everything(common.home()), before)
        open(os.path.join(folder, "done"), "w").close()
        proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0)

    def test_a_held_graph_lock_skips_sync_and_rebuild(self):
        from chitragupta.agent import Lock
        self.sync()
        held = Lock(gs.GRAPH_LOCK)
        self.assertTrue(held.acquire())
        self.addCleanup(held.release)
        self.put(A, title="changed")
        before = everything(common.home())
        self.assertTrue(self.sync()["busy"])
        self.assertEqual(gs.rebuild(self.cfg, wait_s=0)["skipped"], gs.SKIPPED)
        self.assertEqual(everything(common.home()), before)
        self.assertFalse(os.path.exists(common.wl("locks", "agent.lock")))   # rebuild let its own lock go

    def test_two_processes_syncing_the_same_home(self):   # EG-A16
        folder = self.signals()
        procs = [self.child(SYNC_CHILD, folder, tag) for tag in ("one", "two")]
        for tag in ("one", "two"):
            self.wait_for(os.path.join(folder, "ready-" + tag), procs)
        open(os.path.join(folder, "go"), "w").close()
        reports = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err.decode())
            reports += json.loads(out.decode().strip().splitlines()[-1])
        built = [k for r in reports for k in r["built"] + r["recovered"]]
        self.assertEqual(sorted(built), [A, B], "every ticket built exactly once")
        self.assertEqual([k for r in reports for k in r["errors"]], [])
        for key in (A, B):
            self.assertEqual(self.seqs(key), [(1, None)])
            self.consistent(key)
        con = gs.open_write()
        try:
            for table in ("nodes", "edges"):
                dupes = con.execute("SELECT ticket, id, COUNT(*) FROM %s GROUP BY ticket, id HAVING COUNT(*) > 1"
                                    % table).fetchall()
                self.assertEqual(dupes, [])
        finally:
            con.close()
        expected = gb.ticket_graph(gb.load_inputs(A, self.cfg))
        nodes, edges, statuses, source = self.rows()
        self.assertEqual(source["content"], expected["content"])   # no lost row
        self.assertEqual((len(nodes), len(edges), len(statuses)),
                         (len(expected["nodes"]), len(expected["edges"]), len(expected["statuses"])))

    def test_stats_report_rows_errors_and_how_far_behind(self):
        self.assertEqual(gs.stats(self.cfg)["behind"], 2)   # nothing built yet
        self.sync()
        stats = gs.stats(self.cfg)
        self.assertEqual((stats["state"], stats["tickets"], stats["behind"], stats["last_error"], stats["full_at"]),
                         ("ok", 2, 0, "", ""))
        self.assertEqual(stats["rows"], stats["nodes"] + stats["edges"] + stats["requirements"])
        self.assertTrue(stats["generation"] > 0 and stats["built_at"])
        self.assertNotIn("behind", gs.stats())   # only computed on request
        self.put(A, title="changed")
        self.assertEqual(gs.stats(self.cfg)["behind"], 1)
        gs.rebuild(self.cfg)
        stats = gs.stats(self.cfg)
        self.assertEqual(stats["behind"], 0)
        self.assertTrue(stats["full_at"])
        self.assertGreater(stats["generation"], 0)


def gb_plan(workflow, index):
    return GB_PLAN(workflow, index)


def common_write(path, obj):
    gs.common.write_json(path, obj)


if __name__ == "__main__":
    unittest.main()
