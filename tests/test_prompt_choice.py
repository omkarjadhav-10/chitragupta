"""Prompt choice preserves first own words and prefers a named work-branch key."""
from tests.test_attribution import AttrCase, ev


class PromptChoice(AttrCase):
    def test_first_own_words_ticket_wins_without_a_named_branch_ticket(self):
        from chitragupta import common
        self.assertEqual(common.prompt_ticket("compare ABC-1001 and ABC-1002", ["ABC"], common.load_config()),
                         ("ABC-1001", "prompt"))

    def test_named_branch_ticket_wins_a_multi_ticket_prompt(self):
        from chitragupta import common
        self.assertEqual(common.prompt_ticket("compare ABC-1001 and ABC-1002", ["ABC"], common.load_config(),
                                             branch_ticket="ABC-1001"), ("ABC-1001", "prompt"))

    def test_own_words_still_beat_a_pasted_ticket(self):
        from chitragupta import common
        self.assertEqual(common.prompt_ticket("fix ABC-1002 <pasted_content>ABC-1001</pasted_content>",
                                             ["ABC"], common.load_config(), branch_ticket="ABC-1001"),
                         ("ABC-1002", "prompt"))

    def test_configured_hotfix_base_containing_a_key_gives_no_branch_ticket(self):
        from chitragupta import common, gitcmd
        cfg = common.load_config()
        cfg.setdefault("branching", {}).setdefault("repos", {})["shop-api"] = {"bases": {"release": ["*-hotfix"]}}
        repo = self.repo("shop-api", "fix/ABC-1001-oct-hotfix")
        bases = gitcmd.repo_conf(cfg, "shop-api")["bases"]
        self.assertEqual(gitcmd.role_of("fix/ABC-1001-oct-hotfix", bases), "release")
        self.assertIsNone(common.work_branch_ticket("fix/ABC-1001-oct-hotfix", cfg, repo))
        self.assertIsNone(common.work_branch_ticket("release/ABC-1001/oct", cfg, repo))
        self.assertIsNone(common.work_branch_ticket("detached@abc123", cfg, repo))
        self.assertIsNone(common.work_branch_ticket("qa", cfg, repo))

    def test_event_attribution_prefers_a_named_branch_key_in_the_middle(self):
        from chitragupta import agent
        events = [ev("user-prompt", 10, 0, branch="fix/ABC-1001",
                     prompt="compare ABC-1003, ABC-1001 and ABC-1002")]
        self.assertEqual(agent.attribute(events, ["ABC"])[0]["_t"], "ABC-1001")
