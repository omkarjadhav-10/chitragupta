import unittest
from unittest import mock

from chitragupta import graphbuild as gb, graphmodel as gm
from chitragupta.safety import commands
from tests import graphfixture as fx

KEY = "ABC-1001"
TICKET = gm.node_id("ticket", "", KEY)
CLONE_A, CLONE_B = "C:/work/shop", "D:/second/shop"
OTHER = "github:github.com/acme/shop-legacy"


class Base(unittest.TestCase):
    def setUp(self):
        self.started = []

        def no_process(*args, **kwargs):
            self.started.append(args[:1])
            raise AssertionError("the graph builder started a process")
        for name in ("run", "spawn"):
            trip = mock.patch.object(commands, name, no_process)
            trip.start()
            self.addCleanup(trip.stop)
        self.addCleanup(lambda: self.assertEqual(self.started, [], "no Git command or process"))

    def build(self, tk, identity=None):
        ident = identity or gb.repo_identity("shop", tk, [fx.ledger(CLONE_A)], [])
        out = gb.commit_nodes(tk, {"shop": ident}, TICKET)
        self.assertEqual(out, gb.commit_nodes(tk, {"shop": ident}, TICKET))   # deterministic
        return out

    def of(self, rows, kind):
        return [row for row in rows if row["type"] == kind]

    def commit_id(self, n, repo=fx.SHOP):
        return gm.node_id("commit", repo, fx.sha(n))


class RepoIdentity(Base):
    def test_a_ledger_at_the_recorded_location_gives_the_id(self):
        tk = fx.ticket_record(files=[fx.file_rec("src/orders/list.py", repo_root=CLONE_A)])
        got = gb.repo_identity("shop", tk, [fx.ledger(CLONE_A), fx.ledger("C:/work/other", "github:github.com/acme/x")],
                               [], fx.repo_json(CLONE_A))
        self.assertEqual((got["id"], got["identity_basis"], got["location"]), (fx.SHOP, "ledger", "c:/work/shop"))
        self.assertNotIn("candidates", got)

    def test_a_workflow_binding_is_next(self):
        got = gb.repo_identity("shop", fx.ticket_record(), [], [{"repo_id": fx.SHOP, "location": CLONE_A}])
        self.assertEqual((got["id"], got["identity_basis"]), (fx.SHOP, "binding"))

    def test_repo_json_is_adapted_never_passed_through(self):
        got = gb.repo_identity("shop", fx.ticket_record(), [], [], fx.repo_json(CLONE_A, remote_path="Acme/Shop"))
        self.assertEqual((got["id"], got["identity_basis"]), (fx.SHOP, "repo.json"))
        self.assertNotIn("work", got["id"])   # the local folder never becomes the identity
        azure = gb.repo_identity("shop", fx.ticket_record(), [], [],
                                 fx.repo_json(CLONE_A, host="azure", remote_path=None, org="acme", project="store",
                                              repo="shop"))
        self.assertEqual(azure["id"], "azure:dev.azure.com/acme/store/shop")

    def test_nothing_known_is_the_folder_name_said_plainly(self):
        for meta in (None, {}, fx.repo_json(CLONE_A, host=None, remote_path=None)):
            got = gb.repo_identity("shop", fx.ticket_record(), [], [], meta)
            self.assertEqual((got["id"], got["identity_basis"]), ("name:shop", "folder-name"))

    def test_one_remote_in_two_clones_is_one_identity(self):
        got = gb.repo_identity("shop", fx.ticket_record(), [fx.ledger(CLONE_A), fx.ledger(CLONE_B)], [],
                               fx.repo_json(CLONE_A))
        self.assertEqual((got["id"], got["identity_basis"]), (fx.SHOP, "ledger"))
        self.assertEqual(got["locations"], {"c:/work/shop": fx.SHOP, "d:/second/shop": fx.SHOP})
        self.assertNotIn("location", got)

    def test_two_remotes_with_one_folder_name_are_ambiguous_and_none_is_chosen(self):
        got = gb.repo_identity("shop", fx.ticket_record(), [fx.ledger(CLONE_A), fx.ledger(CLONE_B, OTHER)], [])
        self.assertEqual((got["id"], got["identity_basis"], got["candidates"]),
                         ("name:shop", "ambiguous", sorted([fx.SHOP, OTHER])))

    def test_a_recorded_location_settles_which_one(self):
        tk = fx.ticket_record(files=[fx.file_rec("src/orders/list.py", repo_root=CLONE_B)])
        got = gb.repo_identity("shop", tk, [fx.ledger(CLONE_A), fx.ledger(CLONE_B, OTHER)], [])
        self.assertEqual((got["id"], got["identity_basis"]), (OTHER, "ledger"))
        both = fx.ticket_record(files=[fx.file_rec("a.py", repo_root=CLONE_A), fx.file_rec("b.py", repo_root=CLONE_B)])
        got = gb.repo_identity("shop", both, [fx.ledger(CLONE_A), fx.ledger(CLONE_B, OTHER)], [])
        self.assertEqual(got["identity_basis"], "ambiguous")

    def test_other_folder_names_are_ignored(self):
        got = gb.repo_identity("shop", fx.ticket_record(), [fx.ledger("C:/work/shop-api", OTHER)],
                               [{"repo_id": OTHER, "location": "C:/work/api"}])
        self.assertEqual(got["identity_basis"], "folder-name")


class Commits(Base):
    def test_own_commits_files_and_ticket_evidence(self):
        tk = fx.ticket_record(commits=[fx.commit_rec(1, "ABC-1001 Add the order filter",
                                                     files=["src/orders/list.py", "tests/test_orders.py"]),
                                       fx.commit_rec(2, "Tidy the list query", files=["src/orders/list.py"]),
                                       fx.commit_rec(3, "Follow-up", ref="main")])
        out = self.build(tk)
        commits = {c["attrs"]["sha"]: c for c in self.of(out["nodes"], "commit")}
        first = commits[fx.sha(1)]
        self.assertEqual(first["id"], self.commit_id(1))
        self.assertEqual((first["repo_id"], first["label"], first["ticket"], first["observed_at"]),
                         (fx.SHOP, "ABC-1001 Add the order filter", KEY, "2026-01-05T09:00:00+00:00"))
        self.assertEqual((first["attrs"]["mine"], first["attrs"]["short"], first["attrs"]["identity_basis"],
                          first["attrs"]["sha_basis"]), (True, fx.sha(1)[:9], "ledger", "full"))
        how = {e["dst"]: e["attrs"]["how"] for e in self.of(out["edges"], "HAS_EVIDENCE")}
        self.assertEqual([how[self.commit_id(n)] for n in (1, 2, 3)],
                         ["commit message", "branch name", "recorded attribution"])
        for edge in self.of(out["edges"], "HAS_EVIDENCE"):
            self.assertEqual((edge["src"], edge["basis"]), (TICKET, "observed"))
        changes = self.of(out["nodes"], "code_change")
        self.assertEqual(len(changes), 3)   # the same path in two commits is two changes
        part = {(e["src"], e["dst"]) for e in self.of(out["edges"], "PART_OF_COMMIT")}
        self.assertEqual(part, {(gm.node_id("code_change", self.commit_id(n), p), self.commit_id(n))
                                for n, p in ((1, "src/orders/list.py"), (1, "tests/test_orders.py"),
                                             (2, "src/orders/list.py"))})
        self.assertEqual({c["attrs"]["state"] for c in changes}, {"committed"})

    def test_one_remote_in_two_clones_is_one_commit(self):
        tk = fx.ticket_record(commits=[fx.commit_rec(1, "ABC-1001 Add the order filter")])
        a = self.build(tk, gb.repo_identity("shop", tk, [fx.ledger(CLONE_A)], []))
        b = self.build(tk, gb.repo_identity("shop", tk, [fx.ledger(CLONE_B)], []))
        self.assertEqual([n["id"] for n in a["nodes"]], [n["id"] for n in b["nodes"]])

    def test_a_weak_identity_is_visible_on_the_node(self):
        tk = fx.ticket_record(commits=[fx.commit_rec(1, "ABC-1001 Add the order filter")])
        ident = gb.repo_identity("shop", tk, [fx.ledger(CLONE_A), fx.ledger(CLONE_B, OTHER)], [])
        node = self.of(self.build(tk, ident)["nodes"], "commit")[0]
        self.assertEqual((node["repo_id"], node["attrs"]["identity_basis"], node["attrs"]["candidates"]),
                         ("name:shop", "ambiguous", sorted([fx.SHOP, OTHER])))
        unknown = self.of(gb.commit_nodes(tk, {}, TICKET)["nodes"], "commit")[0]
        self.assertEqual((unknown["repo_id"], unknown["attrs"]["identity_basis"]), ("name:shop", "folder-name"))

    def test_uncommitted_edits_are_bound_to_their_clone(self):
        tk = fx.ticket_record(files=[fx.file_rec("src/orders/list.py", repo_root=CLONE_A),
                                     fx.file_rec("src/orders/list.py", repo_root=CLONE_B),
                                     fx.file_rec("docs/orders.md"),
                                     fx.file_rec("src/orders/done.py", committed=True, verified_by="commit"),
                                     fx.file_rec("src/orders/read.py", verified_by="")])
        ident = gb.repo_identity("shop", tk, [fx.ledger(CLONE_A), fx.ledger(CLONE_B)], [])
        out = self.build(tk, ident)
        changes = self.of(out["nodes"], "code_change")
        self.assertEqual(sorted((c["attrs"]["path"], c["location"]) for c in changes),
                         [("docs/orders.md", ""), ("src/orders/list.py", "c:/work/shop"),
                          ("src/orders/list.py", "d:/second/shop")])
        self.assertEqual(len({c["id"] for c in changes}), 3)
        self.assertEqual({c["attrs"]["state"] for c in changes}, {"uncommitted"})
        self.assertIn(gm.node_id("code_change", fx.SHOP, gb.UNKNOWN_LOCATION, "docs/orders.md", "uncommitted"),
                      [c["id"] for c in changes])
        self.assertEqual({(e["src"], e["attrs"]["how"]) for e in out["edges"]}, {(TICKET, "session edit")})
        self.assertEqual(self.of(out["edges"], "PART_OF_COMMIT"), [])

    def test_nothing_recorded(self):
        self.assertEqual(self.build(fx.ticket_record()), {"nodes": [], "edges": []})
        self.assertEqual(gb.commit_nodes({}, {}, TICKET), {"nodes": [], "edges": []})


class Relationships(Base):
    def repo(self, rows, prs=()):
        return {"shop": {"repo": "shop", "rows": rows, "prs": list(prs)}}

    def test_the_same_patch_is_equivalent(self):
        tk = fx.ticket_record(commits=[fx.commit_rec(1, "ABC-1001 Add the order filter")],
                              repos=self.repo([fx.delivery_row([fx.sha(1), fx.sha(2)])]))
        out = self.build(tk)
        commits = {c["attrs"]["sha"]: c for c in self.of(out["nodes"], "commit")}
        self.assertEqual(sorted(commits), [fx.sha(1), fx.sha(2)[:9]])   # the record keeps only a prefix of the copy
        copy = commits[fx.sha(2)[:9]]
        self.assertEqual((copy["attrs"]["mine"], copy["attrs"]["sha_basis"]), ("unknown", "prefix"))
        self.assertEqual(commits[fx.sha(1)]["attrs"]["mine"], True)   # resolved to the commit already known
        same = self.of(out["edges"], "EQUIVALENT_TO")
        self.assertEqual([(e["src"], e["dst"], e["basis"], e["attrs"]) for e in same],
                         [(copy["id"], self.commit_id(1), "observed", {"patch": "same"})])
        self.assertEqual(self.of(out["edges"], "CHERRY_PICKED_FROM") + self.of(out["edges"], "SQUASHED_INTO"), [])
        how = {e["dst"]: e["attrs"]["how"] for e in self.of(out["edges"], "HAS_EVIDENCE")}
        self.assertEqual(how, {self.commit_id(1): "commit message", copy["id"]: "delivery row"})

    def test_a_changed_cherry_pick_is_lineage_not_equivalence(self):
        tk = fx.ticket_record(commits=[fx.commit_rec(1, "ABC-1001 Add the order filter")],
                              repos=self.repo([fx.delivery_row([fx.sha(1)], loose=[fx.sha(5)])]))
        out = self.build(tk)
        picked = self.of(out["edges"], "CHERRY_PICKED_FROM")
        self.assertEqual(len(picked), 1)
        self.assertEqual((picked[0]["dst"], picked[0]["attrs"]), (self.commit_id(1), {"patch": "changed"}))
        self.assertEqual(self.of(out["edges"], "EQUIVALENT_TO"), [])
        self.assertEqual(len(self.of(out["nodes"], "commit")), 2)   # related commits stay separate nodes

    def test_a_multi_commit_squash_is_squashed_into_only(self):
        merge = fx.sha(9)
        pr = fx.pull_request(7, "merged", merge_commit=merge, head=fx.sha(2))
        cells = {"release": {"s": "squash", "ref": "origin/main", "pr": 7, "via": merge[:9]}}
        tk = fx.ticket_record(commits=[fx.commit_rec(1, "ABC-1001 Add the order filter"),
                                       fx.commit_rec(2, "ABC-1001 Cover the empty result")],
                              repos=self.repo([fx.delivery_row([fx.sha(1)], cells=cells, rid="a" * 12),
                                               fx.delivery_row([fx.sha(2)], cells=cells, rid="b" * 12)], [pr]))
        out = self.build(tk)
        squashed = self.of(out["edges"], "SQUASHED_INTO")
        self.assertEqual({(e["src"], e["dst"]) for e in squashed},
                         {(self.commit_id(1), self.commit_id(9)), (self.commit_id(2), self.commit_id(9))})
        self.assertEqual({e["attrs"]["pr"] for e in squashed}, {7})
        self.assertEqual(self.of(out["edges"], "EQUIVALENT_TO") + self.of(out["edges"], "CHERRY_PICKED_FROM"), [])
        merged = [c for c in self.of(out["nodes"], "commit") if c["id"] == self.commit_id(9)][0]
        self.assertEqual((merged["attrs"]["mine"], merged["attrs"]["sha_basis"]), ("unknown", "full"))
        self.assertNotIn(self.commit_id(9), [e["dst"] for e in self.of(out["edges"], "HAS_EVIDENCE")])

    def test_no_squash_edge_without_the_recorded_merge(self):
        cells = {"release": {"s": "squash", "pr": 7}}
        for pr in (fx.pull_request(7, "open", head=fx.sha(1)), fx.pull_request(7, "merged", head=fx.sha(1)),
                   fx.pull_request(8, "merged", merge_commit=fx.sha(9), head=fx.sha(1))):
            tk = fx.ticket_record(commits=[fx.commit_rec(1, "ABC-1001 Add the order filter")],
                                  repos=self.repo([fx.delivery_row([fx.sha(1)], cells=cells)], [pr]))
            self.assertEqual(self.of(self.build(tk)["edges"], "SQUASHED_INTO"), [])
        merged = {"release": {"s": "merged", "pr": 8}}   # reached by an ordinary merge: no squash
        tk = fx.ticket_record(commits=[fx.commit_rec(1, "ABC-1001 Add the order filter")],
                              repos=self.repo([fx.delivery_row([fx.sha(1)], cells=merged)],
                                              [fx.pull_request(8, "merged", merge_commit=fx.sha(9), head=fx.sha(1))]))
        self.assertEqual(self.of(self.build(tk)["edges"], "SQUASHED_INTO"), [])

    def test_names_that_resemble_the_ticket_create_no_requirement_edge(self):
        tk = fx.ticket_record(
            commits=[fx.commit_rec(1, "ABC-1001 Add the order filter", files=["src/ABC-1001_filter.py"],
                                   ref="feature/ABC-1001-ac1-orders-can-be-filtered-by-status")],
            files=[fx.file_rec("notes/ABC-1001-acceptance-criteria.md", repo_root=CLONE_A)],
            repos=self.repo([fx.delivery_row([fx.sha(1), fx.sha(2)], loose=[fx.sha(3)])]))
        out = self.build(tk)
        self.assertEqual({e["type"] for e in out["edges"]},
                         {"HAS_EVIDENCE", "PART_OF_COMMIT", "EQUIVALENT_TO", "CHERRY_PICKED_FROM"})
        for edge in out["edges"]:
            for end in (edge["src"], edge["dst"]):
                self.assertNotRegex(end, r"^g[rvps]_")   # no requirement, revision, plan or step is touched
            self.assertNotEqual(edge["basis"], "declared")
        self.assertEqual({n["type"] for n in out["nodes"]}, {"commit", "code_change"})


if __name__ == "__main__":
    unittest.main()
