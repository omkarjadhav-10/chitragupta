import os
import unittest
from unittest import mock

from chitragupta import graphmodel as gm
from chitragupta import graphstore as gs
from tests.support import Isolated

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


def common_write(path, obj):
    gs.common.write_json(path, obj)


if __name__ == "__main__":
    unittest.main()
