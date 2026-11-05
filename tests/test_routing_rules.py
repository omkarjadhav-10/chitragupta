"""A follow-up continues only the immediately previous eligible turn."""
from tests.test_attribution import AttrCase, ev


class TurnContinuation(AttrCase):
    def test_system_notification_does_not_change_the_prompt_ticket(self):
        from chitragupta import agent
        events = [ev("user-prompt", 10, 0, branch="fix/ABC-1001", prompt="check ABC-1002"),
                  ev("user-prompt", 10, 5, branch="fix/ABC-1001",
                     prompt="<task-notification>Agent cites old dependency ABC-1003</task-notification>"),
                  ev("user-prompt", 10, 6, branch="fix/ABC-1001", prompt="continue")]
        out = agent.attribute(events, ["ABC"])
        self.assertEqual([e["_t"] for e in out], ["ABC-1002"] * 3)
        self.assertEqual([e["_src"] for e in out], ["prompt", "carry-recent", "carry-recent"])

    def test_pasted_ticket_never_becomes_recent_own_words_memory(self):
        from chitragupta import agent
        events = [ev("user-prompt", 10, 0, branch="fix/ABC-1001", prompt="<pasted_content>ABC-1002 details</pasted_content>"),
                  ev("user-prompt", 10, 1, branch="fix/ABC-1001", prompt="continue")]
        out = agent.attribute(events, ["ABC"])
        self.assertEqual((out[1]["_t"], out[1]["_src"]), ("ABC-1001", "branch"))

    def test_only_short_unnamed_prompts_qualify_at_the_twelve_word_boundary(self):
        from chitragupta import agent
        for words, ticket, source in ((12, "ABC-1002", "carry-recent"), (13, "ABC-1001", "branch")):
            with self.subTest(words=words):
                events = [ev("user-prompt", 10, 0, branch="fix/ABC-1001", prompt="check ABC-1002"),
                          ev("user-prompt", 10, 1, branch="fix/ABC-1001", prompt=" ".join(["continue"] * words))]
                out = agent.attribute(events, ["ABC"])
                self.assertEqual((out[1]["_t"], out[1]["_src"]), (ticket, source))

    def test_long_task_notification_still_qualifies_for_recent_carry(self):
        from chitragupta import agent
        events = [ev("user-prompt", 10, 0, branch="fix/ABC-1001", prompt="check ABC-1002"),
                  ev("user-prompt", 10, 1, branch="fix/ABC-1001",
                     prompt="<task-notification>" + " background result" * 20 + "</task-notification>")]
        out = agent.attribute(events, ["ABC"])
        self.assertEqual((out[1]["_t"], out[1]["_src"]), ("ABC-1002", "carry-recent"))

    def test_notification_chain_inherits_pasted_origin_through_notifications(self):
        from chitragupta import agent
        events = [ev("user-prompt", 10, 0, branch="fix/ABC-1001", prompt="<pasted_content>ABC-1002 details</pasted_content>"),
                  ev("user-prompt", 10, 29, branch="fix/ABC-1001", prompt="<task-notification>done</task-notification>"),
                  ev("user-prompt", 10, 58, branch="fix/ABC-1001", prompt="<task-notification>done again</task-notification>"),
                  ev("user-prompt", 10, 59, branch="fix/ABC-1001", prompt="continue")]
        out = agent.attribute(events, ["ABC"])
        self.assertEqual([(e["_t"], e["_src"]) for e in out],
                         [("ABC-1002", "jira-title"), ("ABC-1002", "carry-recent"),
                          ("ABC-1002", "carry-recent"), ("ABC-1001", "branch")])

    def test_short_prompt_after_branch_decided_long_prompt_keeps_branch(self):
        from chitragupta import agent
        events = [ev("user-prompt", 10, 0, branch="fix/ABC-1001", prompt="check ABC-1002"),
                  ev("user-prompt", 10, 1, branch="fix/ABC-1001", prompt=" ".join(["newquestion"] * 13)),
                  ev("user-prompt", 10, 2, branch="fix/ABC-1001", prompt="continue")]
        out = agent.attribute(events, ["ABC"])
        self.assertEqual([(e["_t"], e["_src"]) for e in out],
                         [("ABC-1002", "prompt"), ("ABC-1001", "branch"), ("ABC-1001", "branch")])

    def test_short_prompt_after_pasted_selection_does_not_revive_older_own_words(self):
        from chitragupta import agent
        events = [ev("user-prompt", 10, 0, branch="fix/ABC-1001", prompt="check ABC-1002"),
                  ev("user-prompt", 10, 5, branch="fix/ABC-1001", prompt="<pasted_content>ABC-1003 details</pasted_content>"),
                  ev("user-prompt", 10, 6, branch="fix/ABC-1001", prompt="continue")]
        out = agent.attribute(events, ["ABC"])
        self.assertEqual((out[2]["_t"], out[2]["_src"]), ("ABC-1001", "branch"))

    def test_window_moves_with_each_own_words_continuation_turn(self):
        from chitragupta import agent
        events = [ev("user-prompt", 10, 0, branch="fix/ABC-1001", prompt="check ABC-1002"),
                  ev("user-prompt", 10, 30, branch="fix/ABC-1001", prompt="continue"),
                  ev("user-prompt", 11, 0, branch="fix/ABC-1001", prompt="continue again"),
                  ev("user-prompt", 11, 31, branch="fix/ABC-1001", prompt="continue once more")]
        out = agent.attribute(events, ["ABC"])
        self.assertEqual([(e["_t"], e["_src"]) for e in out],
                         [("ABC-1002", "prompt"), ("ABC-1002", "carry-recent"),
                          ("ABC-1002", "carry-recent"), ("ABC-1001", "branch")])

    def test_window_uses_previous_turn_start_not_its_last_tool(self):
        from chitragupta import agent
        events = [ev("user-prompt", 10, 0, branch="fix/ABC-1001", prompt="check ABC-1002"),
                  ev("user-prompt", 10, 30, branch="fix/ABC-1001", prompt="continue"),
                  ev("post-tool", 11, 0, branch="fix/ABC-1001"),
                  ev("user-prompt", 11, 1, branch="fix/ABC-1001", prompt="continue again")]
        out = agent.attribute(events, ["ABC"])
        self.assertEqual((out[2]["_t"], out[2]["_src"]), ("ABC-1002", "carry-recent"))
        self.assertEqual((out[3]["_t"], out[3]["_src"]), ("ABC-1001", "branch"))

    def test_general_carry_does_not_create_an_own_words_continuation_chain(self):
        from chitragupta import agent
        events = [ev("user-prompt", 10, 0, branch="qa", prompt="check ABC-1002"),
                  ev("user-prompt", 10, 1, branch="qa", prompt=" ".join(["newquestion"] * 13)),
                  ev("user-prompt", 10, 2, branch="fix/ABC-1001", prompt="continue")]
        out = agent.attribute(events, ["ABC"])
        self.assertEqual((out[1]["_t"], out[1]["_src"]), ("ABC-1002", "carry"))
        self.assertEqual((out[2]["_t"], out[2]["_src"]), ("ABC-1001", "branch"))
