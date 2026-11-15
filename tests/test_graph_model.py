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
                                    "scope", "generation", "confidence", "lifecycle", "attrs", "version",
                                    "workflow"})
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


class PlanCoverage(unittest.TestCase):
    def test_no_plan(self):
        got = gm.plan_coverage(False, True, {"state": "Covered", "origin": gm.DEVELOPER_ORIGIN}, True)
        self.assertEqual(got, {"state": "Unverified", "basis": "unknown", "reason": "no plan"})

    def test_requirement_changed_after_plan(self):
        got = gm.plan_coverage(True, False, {"state": "Covered", "origin": gm.DEVELOPER_ORIGIN}, True)
        self.assertEqual(got["state"], "Unverified")
        self.assertEqual(got["reason"], gm.PLAN_CHANGED)

    def test_developer_assessment_is_declared(self):
        got = gm.plan_coverage(True, True, {"state": "Partial", "origin": gm.DEVELOPER_ORIGIN}, True)
        self.assertEqual((got["state"], got["basis"]), ("Partial", "declared"))

    def test_jev_answer_is_inferred_and_confidence_is_never_used(self):
        low = gm.plan_coverage(True, True, {"state": "Covered", "origin": gm.JEV_ORIGIN, "confidence": 0.1}, False)
        high = gm.plan_coverage(True, True, {"state": "Covered", "origin": gm.JEV_ORIGIN, "confidence": 0.9}, False)
        self.assertEqual((low["state"], low["basis"]), ("Covered", "inferred"))
        self.assertEqual((high["state"], high["basis"]), ("Covered", "inferred"))
        self.assertIn("0.1", low["reason"])
        self.assertIn(gm.JEV_ORIGIN, low["reason"])

    def test_an_unnamed_origin_never_becomes_declared(self):
        for origin in (None, "", "someone else"):
            got = gm.plan_coverage(True, True, {"state": "Covered", "origin": origin}, True)
            self.assertEqual(got["basis"], "inferred")

    def test_mapping_without_evaluation(self):
        got = gm.plan_coverage(True, True, None, True)
        self.assertEqual(got, {"state": "Covered", "basis": "declared", "reason": "plan mapping; not evaluated"})

    def test_missing(self):
        self.assertEqual(gm.plan_coverage(True, True, None, False)["state"], "Missing")

    def test_unknown_classification_is_refused(self):
        with self.assertRaises(ValueError):
            gm.plan_coverage(True, True, {"state": "Done", "origin": gm.DEVELOPER_ORIGIN}, True)


class Implementation(unittest.TestCase):
    def setUp(self):
        plan = gm.node_id("plan", "w_" + "1" * 64, "p_" + "2" * 64)
        self.rev = gm.node_id("requirement_revision", "gr_" + "0" * 24, "d" * 64)
        self.s1, self.s2 = gm.node_id("plan_step", plan, "s1"), gm.node_id("plan_step", plan, "s2")
        self.commit = commit()["id"]

    def link(self, src, basis="declared", **kw):
        if basis == "inferred":
            kw["confidence"] = 0.5
        return gm.edge("IMPLEMENTED_BY", src, self.commit, TICKET, basis, **kw)

    def test_linked_when_every_step_has_a_link(self):
        edges = [self.link(self.s2), self.link(self.s1)]
        got = gm.implementation(edges, [self.s1, self.s2], True)
        self.assertEqual((got["state"], got["basis"]), ("Linked", "declared"))
        self.assertEqual(got["edges"], sorted(e["id"] for e in edges))

    def test_linked_without_steps_when_the_requirement_has_a_link(self):
        self.assertEqual(gm.implementation([self.link(self.rev)], [], False)["state"], "Linked")

    def test_partial(self):
        got = gm.implementation([self.link(self.s1)], [self.s1, self.s2], True)
        self.assertEqual(got["state"], "Partial")
        self.assertIn("1 of 2", got["reason"])
        self.assertEqual(gm.implementation([self.link(self.rev)], [self.s1], True)["state"], "Partial")

    def test_inferred_and_ambiguous_edges_never_count(self):
        edges = [self.link(self.s1, "inferred"), self.link(self.s1, attrs={"ambiguous": True}),
                 gm.edge("HAS_EVIDENCE", self.s1, self.commit, TICKET, "declared")]
        got = gm.implementation(edges, [self.s1], True)
        self.assertEqual((got["state"], got["edges"]), ("Unverified association", []))
        self.assertEqual(gm.implementation(edges, [self.s1], False)["state"], "None")

    def test_unverified_association_and_none(self):
        got = gm.implementation([], [self.s1], True)
        self.assertEqual((got["state"], got["basis"]), ("Unverified association", "observed"))
        self.assertEqual(got["reason"], "changes exist for the ticket; none is linked to this requirement")
        self.assertEqual(gm.implementation([], [], False)["state"], "None")


def ledger(status, *cited):
    return {"criterion": "ac1", "status": status, "excluded": False,
            "evidence": [{"id": "e_%d" % i, "applicability": a, "outcome": o, "basis": b}
                         for i, (a, o, b) in enumerate(cited)]}


class Verification(unittest.TestCase):
    R1, R2 = "gv_" + "1" * 24, "gv_" + "2" * 24

    def test_ledger_states_map(self):
        cases = (("Verified", "Verified within recorded scope", ("current", "passed", "captured"), "observed"),
                 ("Partial", "Partial", ("current", "passed", "self-reported"), "declared"),
                 ("Missing", "Failed", ("current", "failed", "captured"), "observed"))
        for status, state, cited, basis in cases:
            got = gm.verification(ledger(status, cited), self.R1, self.R1)
            self.assertEqual((got["state"], got["basis"]), (state, basis))

    def test_historical_only_needs_a_historical_pass(self):
        got = gm.verification(ledger("Unverified", ("historical", "passed", "captured")), self.R1, self.R1)
        self.assertEqual((got["state"], got["basis"]), ("Historical only", "observed"))
        for cited in (("historical", "failed", "captured"), ("unknown", "passed", "captured")):
            got = gm.verification(ledger("Unverified", cited), self.R1, self.R1)
            self.assertEqual((got["state"], got["basis"]), ("Unverified", "unknown"))
        self.assertEqual(gm.verification(ledger("Unverified"), self.R1, self.R1)["state"], "Unverified")

    def test_a_newer_revision_does_not_inherit_verified(self):
        got = gm.verification(ledger("Verified", ("current", "passed", "captured")), self.R1, self.R2)
        self.assertEqual(got, {"state": "Unverified", "basis": "unknown", "reason": gm.VERIFY_CHANGED})

    def test_no_confirming_workflow(self):
        self.assertEqual(gm.verification(None, None, self.R1)["state"], "Unverified")
        self.assertEqual(gm.verification(ledger("Verified"), None, self.R1)["state"], "Unverified")


class Combine(unittest.TestCase):
    def test_least_favourable_wins_in_every_dimension(self):
        for dimension, order in gm.ORDER.items():
            for worst in range(len(order)):
                states = list(order[worst:])
                self.assertEqual(gm.combine(dimension, states), order[worst])
                self.assertEqual(gm.combine(dimension, states[::-1]), order[worst])
            self.assertIsNone(gm.combine(dimension, []))

    def test_named_orders(self):
        self.assertEqual(gm.ORDER["verification"][0], "Failed")
        self.assertEqual(gm.ORDER["plan_coverage"][0], "Missing")
        self.assertEqual(gm.ORDER["implementation"][0], "None")
        self.assertEqual(gm.combine("verification", ["Verified within recorded scope", "Historical only", "Partial"]),
                         "Historical only")

    def test_unknown_values_are_refused(self):
        with self.assertRaises(ValueError):
            gm.combine("delivery", ["Covered"])
        with self.assertRaises(ValueError):
            gm.combine("plan_coverage", ["Covered", "Linked"])


def req(name, plan="Covered", impl="Linked", verify="Verified within recorded scope", **kw):
    reasons = kw.pop("reasons", {})
    states = {"plan_coverage": plan, "implementation": impl, "verification": verify}
    return dict({"id": "gr_" + "0" * 24, "aliases": [name],
                 "statuses": {d: {"state": s, "reason": reasons.get(d, "")} for d, s in states.items()}}, **kw)


class NextAction(unittest.TestCase):
    PRS = [{"number": 12, "state": "open"}, {"number": 7, "state": "open"}, {"number": 3, "state": "merged"}]

    def test_order_first_match_wins(self):
        ladder = [
            (req("ac1", verify="Failed"), "failed verification: ac1"),
            (req("ac2", plan="Unverified", reasons={"plan_coverage": gm.PLAN_CHANGED}),
             "requirement changed after plan or baseline: ac2"),
            (req("ac3", plan="Missing"), "requirement missing from the plan: ac3"),
            (req("ac4", verify="Historical only"), "review or rerun the applicable validation: ac4"),
            (req("ac5", impl="Unverified association"), "no linked implementation: ac5"),
            (req("ac6", verify="Unverified"), "unverified: ac6"),
            (req("ac7", verify="Partial"), "partially verified: ac7"),
        ]
        rows = [row for row, _ in ladder]
        for index, (_, line) in enumerate(ladder):
            self.assertEqual(gm.next_action(rows[index:][::-1], self.PRS), line)

    def test_changed_baseline_counts_as_changed(self):
        row = req("ac1", verify="Unverified", reasons={"verification": gm.VERIFY_CHANGED})
        self.assertEqual(gm.next_action([row], []), "requirement changed after plan or baseline: ac1")

    def test_recorded_pull_request_then_nothing(self):
        done = [req("ac1")]
        self.assertEqual(gm.next_action(done, self.PRS), "review the recorded pull request #7")
        self.assertEqual(gm.next_action(done, [{"number": 3, "state": "MERGED"}]),
                         "nothing outstanding in recorded evidence")
        self.assertEqual(gm.next_action(done, []), "nothing outstanding in recorded evidence")
        self.assertEqual(gm.next_action(done, None), "nothing outstanding in recorded evidence")

    def test_excluded_rows_are_skipped(self):
        rows = [req("ac1", verify="Failed", excluded=True), req("ac2")]
        self.assertEqual(gm.next_action(rows, []), "nothing outstanding in recorded evidence")

    def test_no_requirements(self):
        self.assertEqual(gm.next_action([], self.PRS), "no requirements read from Jira yet")
        self.assertEqual(gm.next_action([], [], source="never"), "no requirements read from Jira yet (source: never)")

    def test_never_proposes_creating_a_pull_request(self):
        rows = [req("ac%d" % i, plan=p, impl=m, verify=v) for i, (p, m, v) in enumerate(
            (p, m, v) for p in gm.ORDER["plan_coverage"] for m in gm.ORDER["implementation"]
            for v in gm.ORDER["verification"])]
        for row in rows:
            for prs in ([], self.PRS):
                line = gm.next_action([row], prs).lower()
                self.assertNotRegex(line, r"\b(create|open|raise|submit)\b")
                if "pull request" in line:
                    self.assertTrue(prs)
                    self.assertEqual(line, "review the recorded pull request #7")


class Proposal(unittest.TestCase):
    def offer(self, **kw):
        plan = gm.node_id("plan", "w_" + "1" * 64, "p_" + "2" * 64)
        change = gm.node_id("code_change", commit()["id"], "shop/orders.py")
        return dict({"schema": 1, "origin": "exact-path", "type": "IMPLEMENTED_BY", "src": plan, "dst": change,
                     "confidence": 0.5, "rationale": "plan file path equals a changed path"}, **kw)

    def test_gives_an_inferred_edge(self):
        row = gm.proposal(self.offer(), ticket=TICKET)
        self.assertEqual((row["basis"], row["confidence"], row["ticket"]), ("inferred", 0.5, TICKET))
        self.assertEqual(row["attrs"], {"origin": "exact-path", "rationale": "plan file path equals a changed path"})
        self.assertEqual(row["id"], gm.edge_id("IMPLEMENTED_BY", row["src"], row["dst"], "", "inferred"))
        self.assertEqual(row, gm.proposal(self.offer(), ticket=TICKET))

    def test_cannot_produce_anything_but_inferred(self):
        with self.assertRaises(ValueError):
            gm.proposal(self.offer(basis="declared"))
        with self.assertRaises(ValueError):
            gm.proposal(self.offer(confidence=None))
        for confidence in (0, 0.01, 1):
            self.assertEqual(gm.proposal(self.offer(confidence=confidence))["basis"], "inferred")

    def test_invalid_proposals_are_refused(self):
        good = self.offer()
        edge_id = gm.edge_id("HAS_EVIDENCE", good["src"], good["dst"], "", "observed")
        bad = [{"schema": 2}, {"schema": True}, {"type": "RELATES_TO"}, {"src": "shop/orders.py"}, {"dst": edge_id},
               {"dst": good["src"]}, {"confidence": 1.5}, {"confidence": "high"}, {"origin": ""}, {"rationale": " "}]
        for change in bad:
            with self.assertRaises(ValueError, msg=change):
                gm.proposal(self.offer(**change))
        missing = self.offer()
        del missing["rationale"]
        for obj in (missing, None, []):
            with self.assertRaises(ValueError):
                gm.proposal(obj)


if __name__ == "__main__":
    unittest.main()
