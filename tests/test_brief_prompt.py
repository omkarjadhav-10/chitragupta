"""Gate 2: the brief prompt keeps the model's sentences consistent with the facts it is given.

A brief said "no code change was needed" while its own Fixes made listed the merged commits, and gave the root cause
as two claims led by the second layer. The prompt now says the Fixes made list, the delivery stage and the Jira status
are facts no sentence may contradict, and asks for one root-cause statement that leads with the original cause.
"""
import unittest

from tests.support import Isolated


class BriefPrompt(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()

    def test_facts_are_never_contradicted_and_one_root_cause(self):
        from chitragupta import common
        p = common.read_text(common.prompt_path("brief.md"))
        self.assertIn("the Fixes made list, the delivery stage and the Jira status are facts", p)
        self.assertIn("never write a sentence that contradicts them", p)
        self.assertIn("one combined statement", p)
        self.assertIn("lead with the original cause, then the layer that undid a fix", p)
        self.assertLess(len(p.split()), 700, "the prompt stays short")


if __name__ == "__main__":
    unittest.main()
