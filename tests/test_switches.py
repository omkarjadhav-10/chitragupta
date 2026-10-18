"""On/off switches the Settings page can change, and the engine honours: Jira cards and evening facts, QA trace and
link map, the daily push, requests and artifacts. Absent means on, so existing configs keep working; only an explicit
false turns one off. Also the per-repo base and relation settings and the read-only relations-detect action."""
import json
import unittest
from unittest import mock

from tests.test_v010 import Case, DB, at


class Switch(unittest.TestCase):
    def test_absent_means_on(self):
        from chitragupta import configstore
        self.assertTrue(configstore.on({}, "mcp.jira.cards"))
        self.assertTrue(configstore.on({"mcp": {"jira": {"cards": None}}}, "mcp.jira.cards"))
        self.assertTrue(configstore.on({"mcp": {"jira": {"cards": True}}}, "mcp.jira.cards"))
        self.assertFalse(configstore.on({"mcp": {"jira": {"cards": False}}}, "mcp.jira.cards"))


class JobSwitches(Case):
    def _tick(self, **switches):
        from chitragupta import mcpjobs
        self.clock("2026-10-16", 10, 0)
        a = self.agent()
        a.cfg["mcp"]["jira"]["enabled"] = True
        a.cfg["mcp"]["database"].update(enabled=True, server=DB)
        for k, v in switches.items():
            a.cfg["mcp"][k.split("_")[0]][k.split("_")[1]] = v
        ran = []
        a.jobs.jira_cards = lambda today, limit=2, force=None: ran.append("cards") or 0
        a.jobs.schema_cards = lambda today, tables=None: ran.append("schema") or 0
        a.jobs.qa_traces = lambda today, tickets=None, force=False: ran.append("trace") or 0
        a.jobs.link_map = lambda today: ran.append("linkmap") or True
        a.jobs._offline = lambda what: False
        with mock.patch.object(mcpjobs, "budget_left", return_value=20), \
                mock.patch.object(mcpjobs, "database_conn", return_value="qa-shop"), \
                mock.patch.object(mcpjobs, "ensure_files"):
            a.jobs.tick()
        return ran

    def test_all_on_by_default(self):
        self.assertEqual(self._tick(), ["cards", "schema", "trace", "linkmap"])

    def test_each_switch(self):
        self.assertEqual(self._tick(jira_cards=False), ["schema", "trace", "linkmap"])
        self.assertEqual(self._tick(database_trace=False), ["cards", "schema", "linkmap"])
        self.assertEqual(self._tick(database_linkmap=False), ["cards", "schema", "trace"])

    def test_evening_facts_switch(self):
        from chitragupta import mcpjobs
        self.clock("2026-10-16", 20, 0)
        self.raw("2026-10-16", "ABC-1001", 10, 0)
        for on, want in ((True, 1), (False, 0)):
            a = self.agent()
            a.run_extractions = lambda *x, **k: None
            a.cfg["mcp"]["jira"].update(enabled=True, evening=on)
            calls = []
            with mock.patch.object(mcpjobs, "jira_evening", side_effect=lambda *x, **k: calls.append(1) or None):
                a.jwl.compose("2026-10-16", describe=False)
            self.assertEqual(len(calls), want, "evening=%s" % on)


class PushAndCurationSwitches(Case):
    def test_push_off_keeps_commits_local(self):
        from chitragupta import gitsync
        self.clock("2026-10-16", 10, 0)
        a = self.agent()
        a.force = True
        a.cfg["push_enabled"] = False
        with mock.patch.object(gitsync, "unpushed", return_value=3), mock.patch.object(gitsync, "push") as push, \
                mock.patch.object(gitsync, "commit", return_value=False), mock.patch.object(gitsync, "ensure_repo"), \
                mock.patch.object(gitsync, "check_privacy"):
            a.tick()
            push.assert_not_called()
            a.cfg["push_enabled"] = True
            a.tick()
            push.assert_called_once()

    def test_requests_off_skips_curation(self):
        from chitragupta import replay
        self.clock("2026-10-16", 10, 0)
        a = self.agent()
        a.cfg["requests"] = {"enabled": False}
        f = a.features
        f.cfg = a.cfg
        tk = f.store.ticket("ABC-1001")
        tk["replay"] = {"dirty": True, "force": True}
        with mock.patch.object(replay, "curate") as curate, mock.patch.object(replay, "generate") as gen, \
                mock.patch.object(f, "bruno_changes", return_value=["ABC-1001"]):
            f.curate_and_generate(force=True)
            curate.assert_not_called()
            gen.assert_not_called()


class RepoSettings(Case):
    config = dict(Case.config, watched_repos=["C:/src/shop-api"],
                  branching={"repos": {"shop-api": {"strategy": "release-then-qa"}}})

    def items(self):
        from chitragupta import settings
        return {i["key"]: i for g in settings.describe()["groups"] for i in g["items"]}

    def test_effective_values_and_new_switches(self):
        it = self.items()
        self.assertEqual(it["branching.repos.shop-api.bases"]["value"]["main"], ["main", "master"])
        self.assertEqual(it["branching.repos.shop-api.relations"]["value"], [])
        self.assertEqual(it["branching.repos.shop-api.pr_source"]["value"], "")
        for k in ("mcp.jira.cards", "mcp.jira.evening", "mcp.database.trace", "mcp.database.linkmap", "push_enabled",
                  "requests.enabled", "artifacts.enabled"):
            self.assertIs(it[k]["value"], True, k)
            self.assertTrue(it[k]["editable"], k)

    def test_validation(self):
        from chitragupta import settings
        key = "branching.repos.shop-api.relations"
        ok = [{"from": "release/*", "rel": "cut_from", "to": "main"}, {"from": "*-hotfix", "rel": "merges_into", "to": ["main"]}]
        self.assertEqual(settings.validate({key: ok})[1], {})
        for bad in ([{"from": "qa", "rel": "eats", "to": ["main"]}], [{"from": "qa", "rel": "syncs_from", "to": ["qa"]}],
                    [{"from": "qa; rm", "rel": "syncs_from", "to": ["main"]}], "qa", [{"from": "qa", "rel": "syncs_from"}]):
            self.assertIn(key, settings.validate({key: bad})[1], bad)
        bkey = "branching.repos.shop-api.bases"
        self.assertEqual(settings.validate({bkey: {"qa": ["qa", "test"], "release": ["release/*"]}})[1], {})
        for bad in ({"qa": []}, {"nope": ["x"]}, {"qa": ["$(x)"]}, ["qa"]):
            self.assertIn(bkey, settings.validate({bkey: bad})[1], bad)

    def test_apply_stores_relations_as_the_engine_reads_them(self):
        from chitragupta import common, gitcmd, settings
        settings.apply({"branching.repos.shop-api.relations": [{"from": "release/*", "rel": "cut_from", "to": "main"}]})
        raw = common.load_config()["branching"]["repos"]["shop-api"]
        self.assertEqual(raw["relations"], [{"from": "release/*", "rel": "cut_from", "to": ["main"]}])
        self.assertEqual(raw["strategy"], "release-then-qa")  # the rest of the repo's settings are kept
        self.assertEqual(gitcmd.repo_conf(common.load_config(), "shop-api")["relations"][0]["to"], ["main"])

    def test_detect_action(self):
        from chitragupta import relations
        from chitragupta.dashboard import actions
        self.assertEqual(actions.relations_detect({"repo": "other"})[0], 404)
        self.assertEqual(actions.relations_detect({"repo": ["shop-api"]})[0], 404)
        got = {}

        def fake(cfg, repo, bases, remote="origin"):
            got.update(repo=repo, remote=remote)
            return {"proposals": [{"from": "qa", "rel": "syncs_from", "to": "main", "evidence": "3 merges"}], "evidence": {}}
        with mock.patch.object(relations, "detect", side_effect=fake):
            st, body = actions.relations_detect({"repo": "shop-api"})
        self.assertEqual((st, body["proposals"][0]["rel"]), (200, "syncs_from"))
        self.assertEqual(got, {"repo": "C:/src/shop-api", "remote": "origin"})


if __name__ == "__main__":
    unittest.main()
