import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
