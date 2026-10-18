"""Base-branch relations: config cleaning, read-only detection on a throwaway repo, the graph drawn from real fork
points with merge flows, and delivery explanations that use relations without changing any verdict."""
import unittest

from tests.test_branch_tracking import T, TrackingCase, pr

REL = [{"from": "release/*", "rel": "cut_from", "to": "main"},
       {"from": "release/*", "rel": "merges_into", "to": ["qa", "staging"]},
       {"from": "uat", "rel": "syncs_from", "to": "main"}]


class Model(unittest.TestCase):
    def test_normalize(self):
        from chitragupta import relations
        rels, warnings = relations.normalize(REL + [{"from": "qa", "rel": "flows", "to": "x"}, {"rel": "cut_from"}, "x"])
        self.assertEqual(len(rels), 3)
        self.assertEqual(rels[0]["to"], ["main"])
        self.assertEqual(len(warnings), 3)
        self.assertEqual(relations.normalize(None), ([], []))
        self.assertEqual(relations.normalize("x")[1], ["relations must be a list"])

    def test_queries_and_order(self):
        from chitragupta import relations
        rels, _ = relations.normalize(REL)
        self.assertEqual(relations.cut_from("release/2.0", rels), "main")
        self.assertIsNone(relations.cut_from("qa", rels))
        self.assertEqual(relations.explain("release/2.0", "qa", rels), "reaches qa when release/2.0 is merged into qa")
        self.assertEqual(relations.explain("main", "uat", rels), "reaches uat at its next sync from main")
        self.assertIsNone(relations.explain("qa", "release/2.0", rels))
        self.assertEqual(relations.order(["release/2.0", "qa", "main"], rels), ["main", "release/2.0", "qa"])

    def test_repo_conf_carries_relations_and_warnings(self):
        from chitragupta import gitcmd
        cfg = {"branching": {"repos": {"shop-api": {"strategy": "qa-only", "relations": REL + [{"from": "x"}]}}}}
        conf = gitcmd.repo_conf(cfg, "shop-api")
        self.assertEqual(len(conf["relations"]), 3)
        self.assertEqual(len(conf["relation_warnings"]), 1)
        self.assertEqual(gitcmd.repo_conf({}, "other")["relations"], [])


class Graph(unittest.TestCase):
    def entry(self, rels):
        return {"branches": [{"branch": "fix/ABC-1001-total", "parent": "release/1.0",
                              "commits": [{"sha": "aaaaaaa1", "subject": "fix total", "date": "2026-10-02T10:00:00+05:30"}]}],
                "prs": [{"number": 7, "source": "fix/ABC-1001-total", "target": "release/1.0", "state": "merged",
                         "merged_at": "2026-10-02T12:00:00+05:30"}],
                "targets": [{"role": "release", "name": "release/1.0"}, {"role": "qa", "name": "qa"}],
                "rows": [{"shas": ["aaaaaaa1"], "cells": {"release": {"s": "in"}, "qa": {"s": "in"}}}],
                "relations": rels}

    def test_bases_fork_from_their_parent_and_merge_flows_are_drawn(self):
        from chitragupta import gitview, relations
        rels, _ = relations.normalize(REL)
        g = gitview.graph(self.entry(rels))
        src = g["source"]
        self.assertIsNotNone(src, g["why"])
        self.assertIn('"mainBranchName": "main"', src)  # the release's parent became a lane, and the first one
        lines = [l.strip() for l in src.splitlines()]
        self.assertLess(lines.index('branch "release/1.0"'), lines.index('branch "qa"'))
        self.assertTrue(any(l.startswith('merge "release/1.0"') and "into qa" in l for l in lines), src)
        self.assertEqual(gitview.check(src), [])

    def test_without_relations_the_old_drawing_stays(self):
        from chitragupta import gitview
        src = gitview.graph(self.entry([]))["source"]
        self.assertIn('"mainBranchName": "release/1.0"', src)
        self.assertNotIn("into qa", src)


class Detection(TrackingCase):
    def build(self):
        sb = self.sb
        sb.git("checkout", "-q", "release/1.0")
        sb.commit("release work", who=None)
        sb.push("release/1.0")
        sb.merge("release/1.0", "qa")
        sb.merge("release/1.0", "staging")
        sb.git("checkout", "-q", "main")
        sb.commit("main moves on")
        sb.push("main")
        sb.branch("release/1.1", "origin/main")
        sb.commit("next release")
        sb.push("release/1.1")
        sb.merge("release/1.1", "qa")
        sb.git("checkout", "-q", "main")
        sb.git("merge", "-q", "--ff-only", "origin/main")
        sb.commit("hotfix on main")
        sb.push("main")
        sb.merge("main", "uat")
        sb.fetch()

    def test_detect_proposes_the_real_flows_read_only(self):
        from chitragupta import gitcmd, relations
        from chitragupta.safety import commands
        self.build()
        cfg = self.cfg()
        calls = []
        real = commands.run

        def spy(argv, purpose, *a, **k):
            calls.append((purpose, argv))
            return real(argv, purpose, *a, **k)
        commands.run = spy
        try:
            res = relations.detect(cfg, self.sb.work, gitcmd.repo_conf(cfg, "shop-api")["bases"])
        finally:
            commands.run = real
        props = {(p["from"], p["rel"]): p for p in res["proposals"]}
        self.assertEqual(props[("release/*", "cut_from")]["to"], "main")
        self.assertEqual(set(props[("release/*", "merges_into")]["to"]), {"qa", "staging"})
        self.assertEqual(props[("uat", "syncs_from")]["to"], "main")
        self.assertTrue(calls)
        for purpose, argv in calls:
            self.assertEqual(purpose, "git-work")
            sub = argv[3 + len(commands.HARDEN)]
            self.assertIn(sub, ("for-each-ref", "rev-list", "log"), argv)

    def test_cli_set_and_show(self):
        import io
        import contextlib
        import json
        from chitragupta import cli, common, setup
        common.save_config(self.cfg(strategy="release-then-qa"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["relations", "set", "shop-api", "--json", json.dumps(REL)])
        self.assertEqual(rc, 0, buf.getvalue())
        raw, _ = setup.read_raw_config()
        self.assertEqual(len(raw["branching"]["repos"]["shop-api"]["relations"]), 3)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["relations", "set", "shop-api", "--json", '[{"from": "qa", "rel": "nope", "to": "x"}]'])
        self.assertEqual(rc, 2)
        self.assertIn("Not saved", buf.getvalue())


class Explanations(TrackingCase):
    strategy = "release-then-qa"

    def test_missing_cell_says_how_it_will_arrive(self):
        sb = self.sb
        sb.branch("fix/ABC-1001-total", "origin/release/1.0")
        sb.commit("ABC-1001 fix total")
        sb.push("fix/ABC-1001-total")
        mc, head = sb.merge("fix/ABC-1001-total", "release/1.0")
        sb.fetch()
        self.prs = [pr(7, "fix/ABC-1001-total", "release/1.0", merge_commit=mc, head=head)]
        plain = self.track()
        with_rel = self.track(relations=[{"from": "release/*", "rel": "merges_into", "to": "qa"}])
        self.assertEqual(self.statuses(plain, "qa"), self.statuses(with_rel, "qa"), "verdicts don't change")
        self.assertEqual(self.statuses(with_rel, "qa"), ["missing"])
        why = with_rel["rows"][0]["cells"]["qa"]["why"]
        self.assertTrue(why.startswith("in release/1.0; reaches qa when release/1.0 is merged into qa"), why)
        self.assertEqual(with_rel["relations"][0]["to"], ["qa"])


if __name__ == "__main__":
    unittest.main()
