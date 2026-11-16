import unittest
from unittest import mock

from chitragupta import graphbuild as gb, graphmodel as gm, hosts
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


BRANCH = "feature/ABC-1001-order-filter"


class PrBase(Base):
    def ident(self, tk=None, clone=CLONE_A, repo_id=fx.SHOP, **meta):
        return gb.repo_identity("shop", tk or fx.ticket_record(), [fx.ledger(clone, repo_id)], [],
                                fx.repo_json(clone, **meta))

    def record(self, prs, commits=(1, 2), ci=None, branches=None, shared=None):
        repo = {"repo": "shop", "prs": list(prs), "rows": [fx.delivery_row([fx.sha(n) for n in commits])],
                "branches": branches if branches is not None else [fx.branch_rec(BRANCH, [fx.sha(n) for n in commits])]}
        if ci:
            repo["ci"] = ci
        fields = {"release": {"repos": {"shop": {"shared": shared}}}} if shared else {}
        return fx.ticket_record(commits=[fx.commit_rec(n, "ABC-1001 change %d" % n) for n in commits],
                                repos={"shop": repo}, **fields)

    def prs(self, tk, ident=None):
        ident = ident or self.ident(tk)
        commits = gb.commit_nodes(tk, {"shop": ident}, TICKET)["nodes"]
        out = gb.pr_nodes(tk, {"shop": ident}, TICKET, commits)
        self.assertEqual(out, gb.pr_nodes(tk, {"shop": ident}, TICKET, commits))
        return out


class PullRequests(PrBase):
    def test_identity_is_provider_instance_repository_and_number(self):
        tk = self.record([fx.pull_request(7, head=fx.sha(2))])
        ident = self.ident(tk)
        self.assertEqual((ident["provider"], ident["instance"]), ("github", "github.com"))
        pr = self.of(self.prs(tk)["nodes"], "pull_request")[0]
        self.assertEqual(pr["id"], gm.node_id("pull_request", "github", "github.com", fx.SHOP, 7))
        self.assertEqual(pr["id"], gb.pull_request_id(ident, 7))
        attrs = pr["attrs"]
        self.assertEqual((attrs["number"], attrs["state"], attrs["head"], attrs["source"], attrs["target"],
                          attrs["shared"]), (7, "open", fx.sha(2), BRANCH, "main", False))
        self.assertEqual((pr["label"], pr["repo_id"]), ("#7 ABC-1001 Add the order filter", fx.SHOP))
        self.assertNotIn("author", attrs)

    def test_the_same_number_in_two_repositories_is_two_nodes(self):
        tk = self.record([fx.pull_request(7, head=fx.sha(2))])
        other = gb.repo_identity("shop", tk, [fx.ledger(CLONE_B, OTHER)], [],
                                 fx.repo_json(CLONE_B, remote_path="acme/shop-legacy"))
        a = self.of(self.prs(tk)["nodes"], "pull_request")[0]
        b = self.of(self.prs(tk, other)["nodes"], "pull_request")[0]
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual((a["attrs"]["number"], b["attrs"]["number"]), (7, 7))
        gitlab = self.ident(tk, host="gitlab", instance="git.acme.test")
        self.assertNotEqual(gb.pull_request_id(gitlab, 7), gb.pull_request_id(self.ident(tk), 7))

    def test_source_branch_commits_and_the_merge_commit_are_included(self):
        tk = self.record([fx.pull_request(7, "merged", merge_commit=fx.sha(9), head=fx.sha(2))])
        out = self.prs(tk)
        pr = self.of(out["nodes"], "pull_request")[0]["id"]
        included = {e["src"]: e["attrs"] for e in self.of(out["edges"], "INCLUDED_IN_PR")}
        self.assertEqual(included, {self.commit_id(1): {"at_head": False}, self.commit_id(2): {"at_head": True},
                                    self.commit_id(9): {"at_head": False, "merge": True}})
        self.assertEqual({e["dst"] for e in self.of(out["edges"], "INCLUDED_IN_PR")}, {pr})
        merge = self.of(out["nodes"], "commit")
        self.assertEqual([(c["id"], c["attrs"]["mine"]) for c in merge], [(self.commit_id(9), "unknown")])
        evidence = self.of(out["edges"], "HAS_EVIDENCE")
        self.assertEqual([(e["src"], e["dst"], e["attrs"]["how"]) for e in evidence], [(TICKET, pr, "branch link")])

    def test_commits_of_other_branches_are_not_included(self):
        tk = self.record([fx.pull_request(7, head=fx.sha(2))],
                         branches=[fx.branch_rec("feature/ABC-1001-other", [fx.sha(1), fx.sha(2)])])
        self.assertEqual(self.of(self.prs(tk)["edges"], "INCLUDED_IN_PR"), [])

    def test_a_shared_integration_pr_carries_no_author(self):
        shared = [{"number": 31, "title": "QA batch", "source": "integration/qa-batch", "target": "qa",
                   "state": "open", "created_at": "2026-01-06T09:00:00+00:00", "merged_at": None, "url": "",
                   "role": "qa", "shared": True, "carries": 2, "of": 2, "src": "gh"}]
        out = self.prs(self.record([fx.pull_request(7, head=fx.sha(2))], shared=shared))
        nodes = {p["attrs"]["number"]: p for p in self.of(out["nodes"], "pull_request")}
        attrs = nodes[31]["attrs"]
        self.assertEqual((attrs["shared"], attrs["carries"], attrs["of"], attrs["role"]), (True, 2, 2, "qa"))
        for name in ("author", "mine", "owner"):
            self.assertNotIn(name, attrs)
        self.assertEqual(attrs["ci"]["state"], "unavailable")
        self.assertEqual([e["dst"] for e in self.of(out["edges"], "INCLUDED_IN_PR")], [nodes[7]["id"]] * 2)
        how = {e["dst"]: e["attrs"]["how"] for e in self.of(out["edges"], "HAS_EVIDENCE")}
        self.assertEqual(how[nodes[31]["id"]], "shared integration pull request")

    def test_no_pull_requests(self):
        self.assertEqual(self.prs(fx.ticket_record()), {"nodes": [], "edges": []})


class Checks(PrBase):
    def moved(self):
        """A PR whose head moved from commit 1 to commit 2; only the first head's run passed and was read."""
        pr = fx.pull_request(7, head=fx.sha(1))
        fx.read_ci(dict(pr), [])
        first = dict(pr)
        hosts.ci_record(first, {"path": "acme/shop"},
                        {"runs": [fx.check_run(101)], "coverage": "complete", "reason": "", "total": 1},
                        "2026-01-05T10:05:00+00:00")
        first["head"] = fx.sha(2)
        return first

    def test_runs_cases_and_checked_by(self):
        pr = fx.pull_request(7, head=fx.sha(2))
        ci = fx.read_ci(pr, [fx.check_run(101), fx.check_run(102, "lint", "failed")])
        out = self.prs(self.record([pr], ci={"7": ci}))
        node = self.of(out["nodes"], "pull_request")[0]
        self.assertEqual((node["attrs"]["ci"]["state"], node["attrs"]["ci"]["head"], node["attrs"]["ci"]["coverage"]),
                         ("failing", fx.sha(2), "complete"))
        cases = {c["label"]: c for c in self.of(out["nodes"], "test_case")}
        self.assertEqual(sorted(cases), ["lint", "unit tests"])
        self.assertEqual(cases["lint"]["id"], gm.node_id("test_case", "github", fx.SHOP, "lint"))
        self.assertEqual(cases["lint"]["attrs"]["test_type"], "ci-check")
        runs = {r["attrs"]["evidence"]: r for r in self.of(out["nodes"], "test_run")}
        self.assertEqual(sorted(runs), ["ci:github:shop:101:1", "ci:github:shop:102:1"])
        run = runs["ci:github:shop:102:1"]
        self.assertEqual(run["id"], gm.node_id("test_run", "github", fx.SHOP, "102", 1))
        self.assertEqual((run["attrs"]["status"], run["attrs"]["source"], run["attrs"]["head"], run["lifecycle"],
                          run["attrs"]["applicability"]), ("failed", "ci", fx.sha(2), "current", "current head"))
        checked = self.of(out["edges"], "CHECKED_BY")
        self.assertEqual({(e["src"], e["scope"], e["lifecycle"]) for e in checked},
                         {(node["id"], fx.sha(2), "current")})
        self.assertEqual({(e["src"], e["dst"]) for e in self.of(out["edges"], "EXECUTED_IN")},
                         {(cases["lint"]["id"], run["id"]),
                          (cases["unit tests"]["id"], runs["ci:github:shop:101:1"]["id"])})

    def test_after_the_head_moves_earlier_runs_are_historical_and_never_pass_the_new_head(self):
        pr = self.moved()
        ci = pr.pop("ci")
        out = self.prs(self.record([pr], ci={"7": ci}))
        node = self.of(out["nodes"], "pull_request")[0]
        self.assertEqual(node["attrs"]["ci"]["head"], fx.sha(2))
        self.assertNotEqual(node["attrs"]["ci"]["state"], "passed")   # the new head was never read
        run = self.of(out["nodes"], "test_run")[0]
        self.assertEqual((run["attrs"]["status"], run["attrs"]["head"], run["lifecycle"], run["attrs"]["applicability"]),
                         ("passed", fx.sha(1), "historical", "earlier head"))
        checked = self.of(out["edges"], "CHECKED_BY")[0]
        self.assertEqual((checked["scope"], checked["lifecycle"]), (fx.sha(1), "historical"))

    def test_the_state_comes_from_the_new_head_once_it_is_read(self):
        pr = self.moved()
        hosts.ci_record(pr, {"path": "acme/shop"},
                        {"runs": [fx.check_run(201, outcome="failed")], "coverage": "complete", "reason": "", "total": 1},
                        "2026-01-05T11:00:00+00:00")
        ci = pr.pop("ci")
        out = self.prs(self.record([pr], ci={"7": ci}))
        self.assertEqual(self.of(out["nodes"], "pull_request")[0]["attrs"]["ci"]["state"], "failing")
        life = {r["attrs"]["head"]: (r["lifecycle"], r["attrs"]["status"]) for r in self.of(out["nodes"], "test_run")}
        self.assertEqual(life, {fx.sha(1): ("historical", "passed"), fx.sha(2): ("current", "failed")})
        self.assertEqual(len(self.of(out["nodes"], "test_case")), 1)   # one check, two runs

    def test_an_unread_or_partial_read_is_never_passed(self):
        pr = fx.pull_request(7, head=fx.sha(2))
        unread = self.of(self.prs(self.record([pr]))["nodes"], "pull_request")[0]["attrs"]["ci"]
        self.assertEqual(unread["state"], "unavailable")
        partial = fx.read_ci(pr, [fx.check_run(101)], coverage="partial", reason="rate-limited")
        got = self.of(self.prs(self.record([pr], ci={"7": partial}))["nodes"], "pull_request")[0]["attrs"]["ci"]
        self.assertEqual((got["coverage"], got["reason"]), ("partial", "rate-limited"))
        self.assertNotEqual(got["state"], "passed")


class Release(PrBase):
    def record(self, stages, uat=None, cells=None, loose=(), freshness=None, candidate=False):
        rows = [fx.delivery_row([fx.sha(1)], loose=[fx.sha(n) for n in loose], cells=cells or {})]
        release = {"repos": {"shop": {"stages": stages, "freshness": freshness or {"stale": False},
                                      "candidate": candidate}}}
        if uat is not None:
            release["uat"] = uat
        return fx.ticket_record(commits=[fx.commit_rec(1, "ABC-1001 Add the order filter")],
                                repos={"shop": {"repo": "shop", "rows": rows, "prs": []}}, release=release)

    def facts(self, tk):
        ident = self.ident(tk)
        commits = gb.commit_nodes(tk, {"shop": ident}, TICKET)["nodes"]
        out = gb.release_nodes(tk, {"shop": ident}, TICKET, commits)
        self.assertEqual(out, gb.release_nodes(tk, {"shop": ident}, TICKET, commits))
        self.assertEqual({n["type"] for n in out["nodes"]} - {"release_evidence"}, set())
        return out, {(n["attrs"]["fact"], n["attrs"]["environment"], n["lifecycle"]): n for n in out["nodes"]}

    def test_one_inclusion_node_per_stage(self):
        out, facts = self.facts(self.record({"release": fx.stage_rec("present"),
                                             "qa": fx.stage_rec("partial", "qa", {"in": 1, "of": 2}),
                                             "uat": fx.stage_rec("missing", "uat")}))
        self.assertEqual(sorted(k[1] for k in facts), ["qa", "release", "uat"])
        qa = facts[("inclusion", "qa", "current")]
        self.assertEqual(qa["id"], gm.node_id("release_evidence", TICKET, fx.SHOP, "inclusion", "qa"))
        self.assertEqual((qa["attrs"]["state"], qa["attrs"]["target"], qa["attrs"]["counts"], qa["attrs"]["freshness"],
                          qa["label"], qa["repo_id"]), ("partial", "qa", {"in": 1, "of": 2}, "fresh", "qa: partial",
                                                        fx.SHOP))
        edges = self.of(out["edges"], "HAS_RELEASE_EVIDENCE")
        self.assertEqual({(e["src"], e["dst"]) for e in edges}, {(TICKET, n["id"]) for n in out["nodes"]})

    def test_a_stale_or_unpinned_stage_stays_unknown(self):
        unpinned = fx.stage_rec("unknown", detail="all 1 commit in main (the Jira release's branch isn't confirmed)")
        stale = fx.stage_rec("unknown", "qa", detail="none of 1 commit in qa as of the local refs, so not confirmed missing")
        out, facts = self.facts(self.record({"release": unpinned, "qa": stale}, freshness={"stale": True}))
        for role in ("release", "qa"):
            node = facts[("inclusion", role, "current")]
            self.assertEqual((node["attrs"]["state"], node["attrs"]["freshness"]), ("unknown", "stale"))
        self.assertIn("isn't confirmed", facts[("inclusion", "release", "current")]["attrs"]["detail"])
        self.assertEqual(len(out["nodes"]), 2)   # no stage, no node: nothing is invented for uat

    def test_deployment_and_acceptance_are_separate_nodes_bound_to_their_revision(self):
        uat = {"deployed": {"state": "yes", "source": "confirmed by you 2026-01-07 09:00", "at": "2026-01-07T09:00:00",
                            "rev": "r2"},
               "accepted": {"state": "unknown", "rev": "r2", "carried": True,
                            "source": "Jira still says 'Accepted' from before the latest changes reached UAT"},
               "revision": {"rev": "r2", "at": "2026-01-06T09:00:00"},
               "history": [{"kind": "accepted", "rev": "r1", "at": "2026-01-03T09:00:00", "source": "Jira status"}]}
        out, facts = self.facts(self.record({"uat": fx.stage_rec("present", "uat")}, uat=uat))
        deployed = facts[("deployment", "uat", "current")]
        accepted = facts[("acceptance", "uat", "current")]
        earlier = facts[("acceptance", "uat", "historical")]
        self.assertEqual(len({deployed["id"], accepted["id"], earlier["id"],
                              facts[("inclusion", "uat", "current")]["id"]}), 4)
        self.assertEqual((deployed["attrs"]["state"], deployed["attrs"]["revision"]), ("yes", "r2"))
        self.assertEqual((accepted["attrs"]["state"], accepted["attrs"]["revision"], accepted["attrs"]["carried"]),
                         ("unknown", "r2", True))   # what Jira said about r1 does not accept r2
        self.assertEqual((earlier["attrs"]["state"], earlier["attrs"]["revision"]), ("yes", "r1"))
        self.assertEqual(deployed["id"], gm.node_id("release_evidence", TICKET, "", "deployment", "r2"))
        moved = dict(uat, deployed=dict(uat["deployed"], rev="r3"))
        _, later = self.facts(self.record({"uat": fx.stage_rec("present", "uat")}, uat=moved))
        self.assertNotEqual(later[("deployment", "uat", "current")]["id"], deployed["id"])

    def test_no_uat_revision_is_unknown(self):
        uat = {"deployed": {"state": "unknown", "source": "the current changes aren't confirmed in the UAT branch yet"},
               "accepted": {"state": "unknown", "source": "the current changes aren't confirmed in the UAT branch yet"}}
        _, facts = self.facts(self.record({}, uat=uat))
        self.assertEqual({(k[0], n["attrs"]["state"], n["attrs"]["revision"]) for k, n in facts.items()},
                         {("deployment", "unknown", None), ("acceptance", "unknown", None)})

    def test_commit_evidence_follows_the_same_patch_only(self):
        cells = {"release": {"s": "ancestor", "ref": "origin/release/2026.01"}, "qa": {"s": "missing"}}
        out, facts = self.facts(self.record({"release": fx.stage_rec("present"), "qa": fx.stage_rec("missing", "qa")},
                                            cells=cells, loose=[5]))
        linked = [e for e in out["edges"] if e["src"] != TICKET]
        self.assertEqual({(e["src"], e["dst"], e["attrs"]["state"]) for e in linked},
                         {(self.commit_id(1), facts[("inclusion", "release", "current")]["id"], "ancestor"),
                          (self.commit_id(1), facts[("inclusion", "qa", "current")]["id"], "missing")})
        self.assertNotIn(gm.node_id("commit", fx.SHOP, fx.sha(5)[:9]), [e["src"] for e in out["edges"]])

    def test_a_candidate_repository_and_no_release_give_nothing(self):
        out, _ = self.facts(self.record({"release": fx.stage_rec("not_started")}, candidate=True))
        self.assertEqual(out, {"nodes": [], "edges": []})
        self.assertEqual(gb.release_nodes(fx.ticket_record(), {}, TICKET), {"nodes": [], "edges": []})


if __name__ == "__main__":
    unittest.main()
