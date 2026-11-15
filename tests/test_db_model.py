"""Database-flow records (dbmodel): identities (DBT-05), composite keys and cardinality (DBT-06), evidence that can't be
invented or overstated (DBT-07) and conflicts kept with their applicability (DBT-08). Synthetic data only."""
import os
import unittest

from tests.dbfixture import SERVER, Scenario
from tests.support import Isolated


def obj(conn, schema, name, env="qa", db="shop", engine="postgres", kind="table", quoted=True, cs=None):
    from chitragupta import dbmodel
    return dbmodel.db_node(SERVER, conn, env, db, schema, kind, name, engine, quoted, cs)


class Identities(unittest.TestCase):
    """DBT-05: same names across environments, connections, databases and schemas stay distinct; case follows the
    engine; IDs are opaque."""

    def test_same_name_elsewhere_is_another_object(self):
        ids = {obj("qa-main", "shop", "orders")["id"], obj("dev-main", "shop", "orders", env="dev")["id"],
               obj("qa-main", "shop_audit", "orders")["id"], obj("qa-main", "shop", "orders", db="shop2")["id"],
               obj("qa-billing", "shop", "orders", db="billing", engine="mssql")["id"],
               obj("qa-main", "shop", "orders", kind="view")["id"]}
        self.assertEqual(len(ids), 6)
        self.assertEqual(obj("qa-main", "shop", "orders")["id"], obj("qa-main", "shop", "orders")["id"])

    def test_postgres_quoted_names_keep_case(self):
        from chitragupta import dbmodel
        stored = obj("qa-main", "shop", "Orders")            # a catalog name as stored: created quoted
        lower = obj("qa-main", "shop", "orders")
        self.assertNotEqual(stored["id"], lower["id"])
        unquoted = obj("qa-main", "shop", "ORDERS", quoted=False)  # code that writes ORDERS unquoted means orders
        self.assertEqual(unquoted["id"], lower["id"])
        self.assertEqual(stored["display"]["name"], "Orders")
        self.assertEqual(dbmodel.norm_ident("postgres", "Orders", quoted=True), "Orders")
        self.assertEqual(dbmodel.norm_ident("postgres", "Orders", quoted=False), "orders")

    def test_mssql_is_case_insensitive_unless_marked(self):
        from chitragupta import dbmodel
        a = obj("qa-billing", "billing", "Refunds", db="billing", engine="mssql")
        b = obj("qa-billing", "billing", "refunds", db="billing", engine="mssql")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(a["display"]["collation"], "assumed_ci")
        c = obj("qa-billing", "billing", "Refunds", db="billing", engine="mssql", cs=True)
        d = obj("qa-billing", "billing", "refunds", db="billing", engine="mssql", cs=True)
        self.assertNotEqual(c["id"], d["id"])
        self.assertEqual(dbmodel.collation("mssql", True), "case_sensitive")

    def test_unknown_engines_keep_names_as_written(self):
        from chitragupta import dbmodel
        for eng in ("mongodb", "", "other", None):
            with self.subTest(engine=eng):
                self.assertEqual(dbmodel.norm_ident(eng, "Order Lines", quoted=False), "Order Lines")
        self.assertNotEqual(obj("qa-docs", "app", "Orders", engine="mongodb", kind="collection")["id"],
                            obj("qa-docs", "app", "orders", engine="mongodb", kind="collection")["id"])

    def test_names_are_never_stripped(self):
        from chitragupta import dbmodel
        for name in ("order-lines", " orders", "orders ", "o'rders", "1001"):
            self.assertEqual(dbmodel.norm_ident("postgres", name, quoted=True), name)

    def test_ids_are_opaque_and_validated(self):
        from chitragupta import dbmodel
        o = obj("qa-main", "shop", "orders")
        c = dbmodel.member_node(o, "id")
        n = dbmodel.code_node("path:/repo", "service", "shop-api")
        for rid, p in ((o["id"], "o"), (c["id"], "c"), (n["id"], "n")):
            self.assertTrue(dbmodel.valid_id(rid, p), rid)
            self.assertNotIn("orders", rid)
            self.assertNotIn("/", rid)
        self.assertNotEqual(c["id"], dbmodel.member_node(obj("qa-main", "shop_audit", "orders"), "id")["id"])
        self.assertEqual(c["parent"], o["id"])
        for bad in ("orders", "o_xyz", "../x", "o_" + "a" * 50, "O_abcdef12", "", None, 7):
            self.assertFalse(dbmodel.valid_id(bad), bad)
        self.assertTrue(dbmodel.valid_id(dbmodel.new_id("j")))
        self.assertEqual(dbmodel.new_id("f", "a", 1), dbmodel.new_id("f", "a", 1))
        with self.assertRaises(ValueError):
            dbmodel.new_id("z")

    def test_labels_cover_every_enum(self):
        from chitragupta import dbmodel
        for values, labels in ((dbmodel.BASIS, dbmodel.BASIS_LABEL), (dbmodel.COVERAGE, dbmodel.COVERAGE_LABEL),
                               (dbmodel.FRESHNESS, dbmodel.FRESHNESS_LABEL), (dbmodel.LIFECYCLE, dbmodel.LIFECYCLE_LABEL),
                               (dbmodel.STEP_STATUSES, dbmodel.STEP_LABEL), (dbmodel.JOB_STATES, dbmodel.JOB_LABEL),
                               (dbmodel.GAP_KINDS, dbmodel.GAP_LABEL), (dbmodel.EDGE_KINDS, dbmodel.EDGE_LABEL),
                               (dbmodel.TRANSPORTS, dbmodel.TRANSPORT_LABEL), (dbmodel.VALIDATION, dbmodel.VALIDATION_LABEL)):
            self.assertEqual(set(values), set(labels))
        self.assertEqual(dbmodel.BASIS_LABEL["user_confirmed"], "You confirmed (self-reported)")
        self.assertNotIn("verified", " ".join(dbmodel.JOB_LABEL.values()).lower())
        self.assertEqual(dbmodel.JOB_LABEL["completed"], "All planned reads completed")


class KeysAndCardinality(unittest.TestCase):
    """DBT-06: composite / tenant keys are lists, one-to-many mappings keep every target, cardinality needs a
    declaration."""

    def setUp(self):
        from chitragupta import dbmodel
        self.o = obj("qa-main", "shop", "orders")
        self.r = obj("qa-billing", "billing", "refunds", db="billing", engine="mssql")
        self.c = {n: dbmodel.member_node(self.o, n)["id"] for n in ("tenant_id", "id")}
        self.rc = {n: dbmodel.member_node(self.r, n, engine="mssql")["id"] for n in ("tenant_code", "order_ref")}

    def test_composite_tenant_mapping(self):
        from chitragupta import dbmodel
        e = dbmodel.make_edge("transforms", self.o["id"], self.r["id"],
                              column_mapping=[{"from": [self.c["tenant_id"]], "to": [self.rc["tenant_code"]],
                                               "transform": "tenants.code_for", "transform_known": True},
                                              {"from": [self.c["id"]], "to": [self.rc["order_ref"]],
                                               "transform": "str()", "transform_known": True}],
                              condition=[{"column": self.c["tenant_id"], "op": "=", "param": "tenant"}],
                              transport="event", applicability={"connections": ["qa-main", "qa-billing"], "repo": "r"})
        self.assertEqual(e["layer"], "flow")
        self.assertEqual([m["from"] for m in e["column_mapping"]], [[self.c["tenant_id"]], [self.c["id"]]])
        self.assertIsInstance(e["condition"], list)
        self.assertEqual(e["applicability"]["connections"], ["qa-billing", "qa-main"])
        self.assertIsNone(e["cardinality"])
        for bad in ([{"from": self.c["id"], "to": [self.rc["order_ref"]]}], [{"from": [], "to": [self.rc["order_ref"]]}],
                    [{"from": [self.o["id"]], "to": [self.rc["order_ref"]]}]):
            with self.subTest(mapping=bad):
                with self.assertRaises(dbmodel.RecordError):
                    dbmodel.make_edge("transforms", self.o["id"], self.r["id"], column_mapping=bad)
        with self.assertRaises(dbmodel.RecordError):
            dbmodel.make_edge("transforms", self.o["id"], self.r["id"], condition="tenant_id = 7")

    def test_one_to_many_keeps_every_target(self):
        from chitragupta import dbmodel
        ev = obj("qa-main", "shop", "order_events")
        a, b = dbmodel.member_node(ev, "order_id")["id"], dbmodel.member_node(ev, "tenant_id")["id"]
        e = dbmodel.make_edge("foreign_key", ev["id"], self.o["id"],
                              column_mapping=[{"from": [b, a], "to": [self.c["tenant_id"], self.c["id"]]}],
                              cardinality="many_to_one", basis="declared_metadata")
        self.assertEqual(e["column_mapping"][0]["from"], [b, a])
        self.assertEqual(e["layer"], "structure")

    def test_cardinality_needs_a_declaration(self):
        from chitragupta import dbmodel
        for basis in ("observed_sample", "inferred"):
            with self.subTest(basis=basis):
                with self.assertRaises(dbmodel.RecordError):
                    dbmodel.make_edge("logical_association", self.o["id"], self.r["id"], cardinality="one_to_one",
                                      basis=basis)
        dbmodel.make_edge("foreign_key", self.o["id"], self.r["id"], cardinality="one_to_many", basis="declared_metadata")
        with self.assertRaises(dbmodel.RecordError):
            dbmodel.make_edge("foreign_key", self.o["id"], self.r["id"], cardinality="lots", basis="declared_metadata")

    def test_enum_values_are_checked(self):
        from chitragupta import dbmodel
        for kw in ({"transport": "carrier pigeon"}, {"basis": "verified"}, {"lifecycle": "forever"}):
            with self.subTest(kw=kw):
                with self.assertRaises(dbmodel.RecordError):
                    dbmodel.make_edge("reads", self.o["id"], self.r["id"], **kw)
        with self.assertRaises(dbmodel.RecordError):
            dbmodel.make_edge("joins", self.o["id"], self.r["id"])
        with self.assertRaises(dbmodel.RecordError):
            dbmodel.make_evidence("rumour", {})


class EvidenceCase(Isolated, unittest.TestCase):
    def setUp(self):
        from chitragupta import dbmodel
        self.isolate()
        self.sc = Scenario(self.home).build()
        self.rid = self.sc.repo_id()
        self.repos = {self.rid: self.sc.repo}
        self.o = obj("qa-main", "shop", "orders")
        self.col = dbmodel.member_node(self.o, "status")
        self.svc = dbmodel.code_node(self.rid, "service", "shop-api")
        self.graph = {"nodes": {n["id"]: n for n in (self.o, self.col, self.svc)}, "edges": {}, "evidence": {},
                      "claims": {}}

    def add(self, ev):
        self.graph["evidence"][ev["id"]] = ev
        return ev["id"]

    def code_ev(self, rel="shop-api/orders/cancel.py", needle="UPDATE shop.orders", subjects=None, **over):
        from chitragupta import dbmodel
        a, b, h = self.sc.citation(rel, needle)
        prov = dict({"repo": self.rid, "revision": "worktree", "content_hash": h, "path": rel, "line_start": a,
                     "line_end": b, "symbol": "cancel"}, **over)
        return dbmodel.make_evidence("source", prov, scope={"repo": self.rid}, subjects=subjects or [self.o["id"]])

    def catalog_ev(self, subjects=None, conn="qa-main"):
        from chitragupta import dbmodel
        return dbmodel.make_evidence("catalog", {"source": "describe_table", "connection": conn},
                                     scope={"env": "qa", "connection": conn}, subjects=subjects or [self.col["id"]])


class EvidenceRules(EvidenceCase):
    """DBT-07: invented nodes and citations, unrelated or out-of-scope evidence, and inference promoted to fact are
    rejected; a changed file needs review."""

    def claim(self, subject, predicate, value, evidence, basis, scope=None, proposal=None):
        from chitragupta import dbmodel
        return dbmodel.make_claim(subject, predicate, value, evidence, basis, scope=scope, proposal=proposal)

    def check(self, c):
        from chitragupta import dbmodel
        return dbmodel.validate_claim(c, self.graph, self.repos)

    def test_code_supported_claim_with_a_real_citation(self):
        v = self.add(self.code_ev())
        r = self.check(self.claim(self.o["id"], "written_by", self.svc["id"], [v], "code_supported",
                                  scope={"repo": self.rid}))
        self.assertEqual((r["state"], r["basis"]), ("validated", "code_supported"), r)

    def test_invented_ids_are_rejected(self):
        from chitragupta import dbmodel
        v = self.add(self.code_ev())
        ghost = dbmodel.new_id("o", "ghost")
        r = self.check(self.claim(ghost, "written_by", "x", [v], "inferred"))
        self.assertEqual(r["state"], "rejected")
        self.assertIn("invented", r["reasons"][0])
        r = self.check(self.claim(self.o["id"], "written_by", "x", [dbmodel.new_id("v", "never")], "inferred"))
        self.assertEqual(r["state"], "rejected")

    def test_nonexistent_or_out_of_range_citations_are_rejected(self):
        bad = [self.code_ev(path="shop-api/orders/missing.py"), self.code_ev(line_start=400, line_end=401)]
        for ev in bad:
            with self.subTest(path=ev["provenance"]["path"], line=ev["provenance"]["line_start"]):
                v = self.add(ev)
                r = self.check(self.claim(self.o["id"], "written_by", "x", [v], "code_supported", scope={"repo": self.rid}))
                self.assertEqual(r["state"], "rejected", r)
        from chitragupta import dbmodel
        for path in ("../outside.py", "/etc/passwd", "C:/x.py", "a/../../b.py"):
            with self.subTest(path=path):
                with self.assertRaises(dbmodel.RecordError):
                    self.code_ev(path=path)
        with self.assertRaises(dbmodel.RecordError):  # never a line number alone
            dbmodel.make_evidence("source", {"repo": self.rid, "path": "shop-api/orders/cancel.py", "line_start": 8,
                                             "line_end": 8}, subjects=[self.o["id"]])

    def test_a_citation_from_an_unselected_repo_is_rejected(self):
        from chitragupta import dbmodel
        a, b, h = self.sc.citation("shop-api/orders/cancel.py", "UPDATE")
        ev = dbmodel.make_evidence("source", {"repo": "path:/elsewhere", "revision": "worktree", "content_hash": h,
                                              "path": "shop-api/orders/cancel.py", "line_start": a, "line_end": b},
                                   scope={"repo": "path:/elsewhere"}, subjects=[self.o["id"]])
        v = self.add(ev)
        r = self.check(self.claim(self.o["id"], "written_by", "x", [v], "code_supported", scope={"repo": "path:/elsewhere"}))
        self.assertEqual(r["state"], "rejected")
        self.assertIn("not selected", r["reasons"][0])

    def test_a_changed_file_needs_review(self):
        v = self.add(self.code_ev())
        with open(os.path.join(self.sc.repo, "shop-api", "orders", "cancel.py"), "a", encoding="utf-8") as f:
            f.write("# edited later\n")
        r = self.check(self.claim(self.o["id"], "written_by", self.svc["id"], [v], "code_supported",
                                  scope={"repo": self.rid}))
        self.assertEqual(r["state"], "needs_review", r)
        self.assertFalse(r["ok"])
        from chitragupta import dbmodel
        stored = dbmodel.apply_validation(self.claim(self.o["id"], "written_by", self.svc["id"], [v], "code_supported"), r)
        self.assertEqual(stored["lifecycle"], "needs_review")

    def test_unrelated_evidence_is_rejected(self):
        v = self.add(self.code_ev(subjects=[self.svc["id"]]))  # about the service, not the table
        r = self.check(self.claim(self.o["id"], "written_by", "x", [v], "code_supported", scope={"repo": self.rid}))
        self.assertEqual(r["state"], "rejected")
        self.assertIn("something else", r["reasons"][0])

    def test_a_claim_wider_than_its_evidence_is_rejected(self):
        v = self.add(self.catalog_ev())
        wide = self.check(self.claim(self.col["id"], "type", "varchar(32)", [v], "declared_metadata"))  # every env
        self.assertEqual(wide["state"], "rejected")
        other = self.check(self.claim(self.col["id"], "type", "varchar(32)", [v], "declared_metadata",
                                      scope={"env": "dev", "connection": "qa-main"}))
        self.assertEqual(other["state"], "rejected")
        ok = self.check(self.claim(self.col["id"], "type", "varchar(32)", [v], "declared_metadata",
                                   scope={"env": "qa", "connection": "qa-main"}))
        self.assertEqual(ok["state"], "validated", ok)

    def test_model_proposals_never_promote(self):
        from chitragupta import dbmodel
        with self.assertRaises(dbmodel.RecordError):  # a model proposal can't carry a stronger basis
            dbmodel.make_evidence("model_proposal", {"model": "haiku"}, basis="code_supported", subjects=[self.o["id"]])
        v = self.add(dbmodel.make_evidence("model_proposal", {"model": "haiku", "run": "r1"}, subjects=[self.o["id"]]))
        r = self.check(self.claim(self.o["id"], "written_by", self.svc["id"], [v], "code_supported",
                                  proposal={"confidence": 0.99}))
        self.assertEqual(r["state"], "rejected")
        r = self.check(self.claim(self.o["id"], "written_by", self.svc["id"], [v], "inferred",
                                  proposal={"confidence": 0.99}))
        self.assertEqual((r["state"], r["basis"]), ("validated", "inferred"))
        # a basis with no evidence at all is not a fact either
        self.assertEqual(self.check(self.claim(self.o["id"], "purpose", "orders", [], "declared_metadata"))["state"],
                         "rejected")

    def test_a_user_note_stays_self_reported(self):
        from chitragupta import dbmodel
        v = self.add(dbmodel.make_evidence("user_note", {"note": "n1"}, subjects=[self.o["id"]]))
        self.assertEqual(self.check(self.claim(self.o["id"], "purpose", "Customer orders", [v], "user_confirmed"))["state"],
                         "validated")
        self.assertEqual(self.check(self.claim(self.o["id"], "purpose", "Customer orders", [v], "code_supported"))["state"],
                         "rejected")
        with self.assertRaises(dbmodel.RecordError):
            dbmodel.make_evidence("user_note", {"note": "n2"}, basis="observed_sample", subjects=[self.o["id"]])

    def test_a_sample_supports_only_the_sampled_relationship(self):
        from chitragupta import dbmodel
        ev = obj("qa-main", "shop", "order_events")
        self.graph["nodes"][ev["id"]] = ev
        sample = dbmodel.make_evidence("observation", {"query": "q1", "sampled": 20, "matched": 20},
                                       scope={"env": "qa", "connection": "qa-main"}, subjects=[])
        fk = dbmodel.make_edge("foreign_key", ev["id"], self.o["id"], basis="observed_sample",
                               applicability={"env": "qa", "connection": "qa-main"})
        assoc = dbmodel.make_edge("logical_association", ev["id"], self.o["id"], basis="observed_sample",
                                  applicability={"env": "qa", "connection": "qa-main"})
        for e in (fk, assoc):
            s = dict(sample, subjects=[e["id"]], id=dbmodel.evidence_id("observation", {"query": e["id"]}))
            self.add(s)
            e["evidence"] = [s["id"]]
            self.graph["edges"][e["id"]] = e
        self.assertEqual(dbmodel.validate_edge(fk, self.graph)["state"], "rejected")
        r = dbmodel.validate_edge(assoc, self.graph)
        self.assertEqual((r["state"], r["basis"]), ("validated", "observed_sample"), r)
        self.assertIsNone(dbmodel.strongest_basis([s], structural=True))

    def test_strongest_basis_order(self):
        from chitragupta import dbmodel
        evs = [dbmodel.make_evidence(k, {"k": k, "type": "definition"} if k == "source" else {"k": k},
                                     subjects=[self.o["id"]]) for k in dbmodel.EVIDENCE_KINDS]
        self.assertEqual(dbmodel.strongest_basis(evs), "declared_metadata")
        self.assertEqual(dbmodel.strongest_basis([e for e in evs if e["kind"] != "catalog"]), "code_supported")
        self.assertEqual(dbmodel.strongest_basis([e for e in evs if e["kind"] in ("user_note", "observation")]),
                         "user_confirmed")
        self.assertEqual(dbmodel.strongest_basis([e for e in evs if e["kind"] == "model_proposal"]), "inferred")
        forged = dict(evs[-1], basis="declared_metadata")  # a stored model proposal edited to say more
        self.assertEqual(dbmodel.strongest_basis([forged]), "inferred")
        stale = dict(evs[0], lifecycle="superseded")
        self.assertIsNone(dbmodel.strongest_basis([stale]))

    def test_edges_with_invented_columns_are_rejected(self):
        from chitragupta import dbmodel
        other = dbmodel.member_node(obj("qa-main", "shop_audit", "orders"), "id")
        e = dbmodel.make_edge("reads", self.svc["id"], self.o["id"], basis="inferred")
        self.assertEqual(dbmodel.validate_edge(e, self.graph)["state"], "validated")
        ghost = dbmodel.make_edge("reads", dbmodel.node_id(self.rid, "service", "ghost"), self.o["id"])
        self.assertEqual(dbmodel.validate_edge(ghost, self.graph)["state"], "rejected")
        self.graph["nodes"][other["id"]] = other
        mapped = dbmodel.make_edge("copies", self.o["id"], self.o["id"],
                                   column_mapping=[{"from": [self.col["id"]], "to": [other["id"]]}])
        r = dbmodel.validate_edge(mapped, self.graph)
        self.assertEqual(r["state"], "rejected")  # a same-named id of an unrelated table isn't this table's column
        self.assertIn("does not belong", r["reasons"][0])


class CiteCode(EvidenceCase):
    def test_reads_only_inside_the_repo(self):
        from chitragupta import dbmodel
        r = dbmodel.cite_code(self.sc.repo, "shop-api/orders/cancel.py", 1, 2)
        self.assertTrue(r["exists"] and r["in_range"], r)
        self.assertEqual(len(r["content_hash"]), 64)
        for rel in ("../x", "/abs", "C:\\x", "", "a/../../x", "shop-api\\..\\..\\x"):
            with self.subTest(rel=rel):
                self.assertFalse(dbmodel.cite_code(self.sc.repo, rel, 1, 1)["exists"])
        self.assertFalse(dbmodel.cite_code(self.sc.repo, "shop-api/orders/cancel.py", 0, 1)["in_range"])
        self.assertFalse(dbmodel.cite_code(self.sc.repo, "shop-api/orders/cancel.py", 2, 1)["in_range"])
        self.assertFalse(dbmodel.cite_code(self.sc.repo, "nope.py", 1, 1)["exists"])

    @unittest.skipUnless(os.name == "nt", "directory junctions are a Windows feature")
    def test_a_junction_out_of_the_repo_is_refused(self):
        import _winapi
        from chitragupta import dbmodel
        outside = os.path.join(self.home, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "secret.py"), "w") as f:
            f.write("x = 1\n")
        j = os.path.join(self.sc.repo, "link")
        _winapi.CreateJunction(outside, j)
        self.addCleanup(lambda: os.path.isdir(j) and os.rmdir(j))
        r = dbmodel.cite_code(self.sc.repo, "link/secret.py", 1, 1)
        self.assertFalse(r["exists"])
        self.assertEqual(r["reason"], "outside the repository")


class Conflicts(EvidenceCase):
    """DBT-08: conflicting metadata / code claims are both kept, with their applicability."""

    def test_both_claims_stay_with_their_sources(self):
        from chitragupta import dbmodel
        cat = self.add(self.catalog_ev())
        code = self.add(self.code_ev(subjects=[self.col["id"]], needle="status"))
        a = dbmodel.make_claim(self.col["id"], "type", "varchar(32)", [cat], "declared_metadata",
                               scope={"env": "qa", "connection": "qa-main"}, time={"valid_from": "2026-11-06"})
        b = dbmodel.make_claim(self.col["id"], "type", "varchar(20)", [code], "code_supported",
                               scope={"repo": self.rid}, time={"valid_from": "2026-01-01", "valid_to": "2026-06-30"})
        self.assertEqual(dbmodel.record_conflict(self.graph, a), [])
        self.assertEqual(dbmodel.record_conflict(self.graph, b), [a["id"]])
        claims = self.graph["claims"]
        self.assertEqual(set(claims), {a["id"], b["id"]})
        self.assertEqual(claims[a["id"]]["quality"]["conflicts"], [b["id"]])
        self.assertEqual(claims[b["id"]]["quality"]["conflicts"], [a["id"]])
        self.assertEqual(claims[a["id"]]["scope"]["connection"], "qa-main")
        self.assertEqual(claims[b["id"]]["time"]["valid_to"], "2026-06-30")
        self.assertEqual(claims[a["id"]]["evidence"], [cat])
        # the same claim again: evidence accumulates, no new conflict, nothing dropped
        cat2 = self.add(dbmodel.make_evidence("catalog", {"source": "describe_table", "run": 2},
                                              scope={"env": "qa", "connection": "qa-main"}, subjects=[self.col["id"]]))
        again = dbmodel.make_claim(self.col["id"], "type", "varchar(32)", [cat2], "declared_metadata",
                                   scope={"env": "qa", "connection": "qa-main"})
        self.assertEqual(dbmodel.record_conflict(self.graph, again), [b["id"]])
        self.assertEqual(claims[a["id"]]["evidence"], [cat, cat2])
        self.assertEqual(len(claims), 2)
        for c in claims.values():
            self.assertEqual(dbmodel.validate_claim(c, self.graph, self.repos)["state"], "validated")


if __name__ == "__main__":
    unittest.main()
