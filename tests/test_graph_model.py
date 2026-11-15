import unittest

from chitragupta import graphmodel as gm

TICKET = "ABC-1001"
REPO = "github:acme.test/shop/api"
SHA = "a" * 40


def commit(sha=SHA, **kw):
    return gm.node("commit", (REPO, sha), TICKET, "Add shop order total", {"short": sha[:7]}, **kw)


def ticket():
    return gm.node("ticket", ("", TICKET), TICKET, TICKET)


class Vocabulary(unittest.TestCase):
    def test_types(self):
        self.assertEqual(len(gm.NODE_TYPES), 12)
        prefixes = list(gm.NODE_TYPES.values())
        self.assertEqual(len(set(prefixes)), 12)
        self.assertNotIn(gm.EDGE_PREFIX, prefixes)
        for prefix in prefixes:
            self.assertRegex(prefix, r"^g[a-z]_$")
        self.assertEqual(len(set(gm.EDGE_TYPES)), len(gm.EDGE_TYPES))
        for name in ("HAS_EVIDENCE", "EQUIVALENT_TO", "CHERRY_PICKED_FROM", "SQUASHED_INTO", "CHECKED_BY"):
            self.assertIn(name, gm.EDGE_TYPES)
        self.assertEqual(gm.BASIS, ("observed", "declared", "inferred", "unknown"))
        self.assertIn("source_missing", gm.LIFECYCLE)


class Identity(unittest.TestCase):
    def test_node_id_is_deterministic_and_well_formed(self):
        first, second = gm.node_id("commit", REPO, SHA), gm.node_id("commit", REPO, SHA)
        self.assertEqual(first, second)
        self.assertRegex(first, gm.ID)
        self.assertTrue(first.startswith("gm_"))

    def test_any_identity_element_changes_the_id(self):
        base = gm.node_id("commit", REPO, SHA)
        self.assertNotEqual(base, gm.node_id("commit", REPO + "2", SHA))
        self.assertNotEqual(base, gm.node_id("commit", REPO, "b" * 40))
        self.assertNotEqual(base, gm.node_id("commit", SHA, REPO))
        self.assertNotEqual(gm.node_id("test_case", TICKET, REPO, "pytest"), gm.node_id("test_run", TICKET, REPO, "pytest"))

    def test_edge_id(self):
        src, dst = ticket()["id"], commit()["id"]
        base = gm.edge_id("HAS_EVIDENCE", src, dst, "", "observed")
        self.assertRegex(base, gm.ID)
        self.assertTrue(base.startswith("ge_"))
        self.assertEqual(base, gm.edge_id("HAS_EVIDENCE", src, dst, "", "observed"))
        for other in (("IMPLEMENTED_BY", src, dst, "", "observed"), ("HAS_EVIDENCE", dst, src, "", "observed"),
                      ("HAS_EVIDENCE", src, dst, "r1", "observed"), ("HAS_EVIDENCE", src, dst, "", "declared")):
            self.assertNotEqual(base, gm.edge_id(*other))

    def test_unknown_values_are_refused(self):
        with self.assertRaises(ValueError):
            gm.node_id("decision", "x")
        with self.assertRaises(ValueError):
            gm.node_id("commit")
        with self.assertRaises(ValueError):
            gm.node_id("commit", ["nested"])
        with self.assertRaises(ValueError):
            gm.edge_id("RELATES_TO", "a", "b", "", "observed")
        with self.assertRaises(ValueError):
            gm.edge_id("HAS_EVIDENCE", "a", "b", "", "proven")


class Nodes(unittest.TestCase):
    def test_row_has_every_column(self):
        row = commit(repo_id=REPO, observed_at="2026-01-05T10:00:00Z")
        self.assertEqual(set(row), {"id", "type", "ticket", "label", "attrs", "version", "lifecycle", "repo_id",
                                    "location", "workflow", "source_ref", "observed_at"})
        self.assertEqual(row["id"], gm.node_id("commit", REPO, SHA))
        self.assertEqual(row["lifecycle"], "current")
        self.assertEqual(row["repo_id"], REPO)
        self.assertEqual(row, commit(repo_id=REPO, observed_at="2026-01-05T10:00:00Z"))

    def test_attribute_changes_only_the_version(self):
        one = commit()
        two = gm.node("commit", (REPO, SHA), TICKET, "Add shop order total", {"short": "bbbbbbb"})
        three = commit(lifecycle="historical")
        self.assertEqual(one["id"], two["id"])
        self.assertEqual(one["id"], three["id"])
        self.assertEqual(len({one["version"], two["version"], three["version"]}), 3)
        self.assertRegex(one["version"], r"^[0-9a-f]{16}$")

    def test_labels_and_string_attrs_are_made_safe(self):
        row = gm.node("plan_step", ("gp_" + "0" * 24, "s1"), TICKET, "line\x00one\n" + "x" * 500,
                      {"text": "tab\there", "nested": {"items": ["a\x1fb", 3, None, True]}})
        self.assertNotRegex(row["label"], r"[\x00-\x1f]")
        self.assertLessEqual(len(row["label"]), 300)
        self.assertEqual(row["attrs"]["text"], "tab here")
        self.assertEqual(row["attrs"]["nested"]["items"], ["a b", 3, None, True])

    def test_attrs_are_copied(self):
        attrs = {"list": ["a"]}
        row = gm.node("commit", (REPO, SHA), TICKET, "x", attrs)
        row["attrs"]["list"].append("b")
        self.assertEqual(attrs, {"list": ["a"]})

    def test_bad_input_is_refused(self):
        with self.assertRaises(ValueError):
            gm.node("decision", ("x",), TICKET, "x")
        with self.assertRaises(ValueError):
            commit(lifecycle="deleted")
        with self.assertRaises(ValueError):
            commit(colour="red")
        with self.assertRaises(ValueError):
            gm.node("commit", (REPO, SHA), TICKET, "x", {"raw": b"bytes"})
        with self.assertRaises(ValueError):
            gm.node("commit", (REPO, SHA), TICKET, "x", {1: "x"})


class Edges(unittest.TestCase):
    def setUp(self):
        self.src, self.dst = ticket()["id"], commit()["id"]

    def test_row_has_every_column(self):
        row = gm.edge("HAS_EVIDENCE", self.src, self.dst, TICKET, "observed", evidence_ref="commit:" + SHA,
                      attrs={"how": "commit message"}, generation=4)
        self.assertEqual(set(row), {"id", "type", "src", "dst", "ticket", "basis", "evidence_ref", "observed_at",
                                    "scope", "generation", "confidence", "lifecycle", "attrs", "version"})
        self.assertEqual(row["id"], gm.edge_id("HAS_EVIDENCE", self.src, self.dst, "", "observed"))
        self.assertIsNone(row["confidence"])
        self.assertEqual(row["generation"], 4)

    def test_generation_does_not_change_the_version(self):
        one = gm.edge("HAS_EVIDENCE", self.src, self.dst, TICKET, "observed", generation=1)
        two = gm.edge("HAS_EVIDENCE", self.src, self.dst, TICKET, "observed", generation=2)
        three = gm.edge("HAS_EVIDENCE", self.src, self.dst, TICKET, "observed", attrs={"how": "branch name"})
        self.assertEqual(one["version"], two["version"])
        self.assertEqual(one["version"], gm.version(two))
        self.assertEqual(one["id"], three["id"])
        self.assertNotEqual(one["version"], three["version"])

    def test_confidence_belongs_to_inferred_edges_only(self):
        plan = gm.node_id("plan", "w_" + "1" * 64, "p_" + "2" * 64)
        row = gm.edge("IMPLEMENTED_BY", plan, self.dst, TICKET, "inferred", confidence=0.5)
        self.assertEqual(row["confidence"], 0.5)
        with self.assertRaises(ValueError):
            gm.edge("IMPLEMENTED_BY", plan, self.dst, TICKET, "inferred")
        for bad in (True, "0.5", 1.5, -0.1):
            with self.assertRaises(ValueError):
                gm.edge("IMPLEMENTED_BY", plan, self.dst, TICKET, "inferred", confidence=bad)
        for basis in ("observed", "declared", "unknown"):
            with self.assertRaises(ValueError):
                gm.edge("IMPLEMENTED_BY", plan, self.dst, TICKET, basis, confidence=0.5)

    def test_inferred_and_declared_are_two_edges(self):
        plan = gm.node_id("plan", "w_" + "1" * 64, "p_" + "2" * 64)
        inferred = gm.edge("IMPLEMENTED_BY", plan, self.dst, TICKET, "inferred", confidence=0.5)
        declared = gm.edge("IMPLEMENTED_BY", plan, self.dst, TICKET, "declared")
        self.assertNotEqual(inferred["id"], declared["id"])

    def test_bad_input_is_refused(self):
        with self.assertRaises(ValueError):
            gm.edge("RELATES_TO", self.src, self.dst, TICKET, "observed")
        with self.assertRaises(ValueError):
            gm.edge("HAS_EVIDENCE", self.src, self.dst, TICKET, "proven")
        with self.assertRaises(ValueError):
            gm.edge("HAS_EVIDENCE", self.src, "commit:" + SHA, TICKET, "observed")
        with self.assertRaises(ValueError):
            gm.edge("HAS_EVIDENCE", self.src, self.dst, TICKET, "observed", lifecycle="gone")
        with self.assertRaises(ValueError):
            gm.edge("HAS_EVIDENCE", self.src, self.dst, TICKET, "observed", weight=2)


class Declarations(unittest.TestCase):
    def edge(self):
        rev = gm.node_id("requirement_revision", "gr_" + "0" * 24, "d" * 64)
        return gm.edge("IMPLEMENTED_BY", rev, commit()["id"], TICKET, "declared")

    def test_both_are_kept_in_time_order(self):
        row = self.edge()
        before = row["version"]
        gm.add_declaration(row, "dev-b", "2026-01-06T09:00:00Z", "e_2")
        gm.add_declaration(row, "dev-a", "2026-01-05T09:00:00Z", "e_1")
        self.assertEqual([d["ref"] for d in row["attrs"]["declarations"]], ["e_1", "e_2"])
        self.assertEqual(row["attrs"]["declarations"][0], {"by": "dev-a", "at": "2026-01-05T09:00:00Z", "ref": "e_1"})
        self.assertNotEqual(row["version"], before)
        self.assertEqual(row["version"], gm.version(row))

    def test_no_duplicates_and_order_independent(self):
        one, two = self.edge(), self.edge()
        gm.add_declaration(one, "dev-a", "2026-01-05T09:00:00Z", "e_1")
        gm.add_declaration(one, "dev-b", "2026-01-06T09:00:00Z", "e_2")
        gm.add_declaration(one, "dev-a", "2026-01-05T09:00:00Z", "e_1")
        gm.add_declaration(two, "dev-b", "2026-01-06T09:00:00Z", "e_2")
        gm.add_declaration(two, "dev-a", "2026-01-05T09:00:00Z", "e_1")
        self.assertEqual(len(one["attrs"]["declarations"]), 2)
        self.assertEqual(one, two)
        self.assertEqual(one["id"], self.edge()["id"])


class Content(unittest.TestCase):
    def graph(self, generation=1):
        nodes = [ticket(), commit(), commit("b" * 40)]
        edges = [gm.edge("HAS_EVIDENCE", nodes[0]["id"], n["id"], TICKET, "observed", generation=generation)
                 for n in nodes[1:]]
        statuses = [{"requirement": "gr_" + "0" * 24, "implementation": "unknown"},
                    {"requirement": "gr_" + "1" * 24, "implementation": "unknown"}]
        return nodes, edges, statuses

    def test_same_rows_same_digest_in_any_order(self):
        nodes, edges, statuses = self.graph()
        base = gm.content(nodes, edges, statuses)
        self.assertRegex(base, r"^[0-9a-f]{64}$")
        self.assertEqual(base, gm.content(*self.graph()))
        self.assertEqual(base, gm.content(nodes[::-1], edges[::-1], statuses[::-1]))
        self.assertEqual(base, gm.content(*self.graph(generation=9)))

    def test_any_change_changes_the_digest(self):
        nodes, edges, statuses = self.graph()
        base = gm.content(nodes, edges, statuses)
        self.assertNotEqual(base, gm.content(nodes[:-1], edges, statuses))
        self.assertNotEqual(base, gm.content(nodes, edges[:-1], statuses))
        self.assertNotEqual(base, gm.content(nodes, edges, [dict(statuses[0], implementation="done"), statuses[1]]))
        self.assertNotEqual(base, gm.content(nodes, edges, []))


if __name__ == "__main__":
    unittest.main()
