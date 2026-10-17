"""Branch guard (PreToolUse): warn / ask / off, fits and misfits, PR bases, fail-open, latency."""
import json
import os
import statistics
import subprocess
import tempfile
import time
import unittest

from tests.support import Isolated, rmtree, run_hook


class GuardCase(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.work = tempfile.mkdtemp(prefix="cg-guard-")
        self.repo = os.path.join(self.work, "shop-api")
        os.makedirs(os.path.join(self.repo, ".git"))
        self.head("release/1.0")

    def tearDown(self):
        rmtree(self.work)
        self.restore()

    def head(self, branch):
        with open(os.path.join(self.repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/%s\n" % branch)

    def config(self, strategy="qa-only", mode="warn", **extra):
        from chitragupta import common
        b = {"guard_mode": mode, "repos": {"shop-api": {"strategy": strategy}}}
        b.update(extra)
        cfg = {"ticket_prefixes": ["ABC"], "watched_repos": [self.repo.replace("\\", "/")], "branching": b}
        os.makedirs(common.wl(), exist_ok=True)
        common.write_json(common.wl("config.json"), cfg)
        return cfg

    def records(self, recs):
        from chitragupta import common
        common.write_json(common.hp("data", "git", "shop-api", "branches.json"), recs)

    def ask(self, cmd):
        from chitragupta import common, guard
        return guard.run({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": self.repo,
                          "hook_event_name": "PreToolUse"}) if common.load_config() else None


class Guard(GuardCase):
    def test_warn_on_a_parent_the_strategy_does_not_use(self):
        self.config("qa-only")
        out = self.ask("git checkout -b feature/ABC-1001-x origin/release/1.0")
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", hso)  # warn never changes the permission flow
        self.assertIn("new branch feature/ABC-1001-x (ABC-1001) is being cut from release/1.0 (release), but qa-only "
                      "for ABC-1001 starts from qa", hso["additionalContext"])
        self.assertIn("Tell the user", hso["additionalContext"])

    def test_ask_and_off(self):
        self.config("qa-only", mode="ask")
        hso = self.ask("git switch -c feature/ABC-1001-x release/1.0")["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "ask")
        self.assertIn("release/1.0", hso["permissionDecisionReason"])
        self.assertNotIn("additionalContext", hso)
        self.config("qa-only", mode="off")
        self.assertIsNone(self.ask("git switch -c feature/ABC-1001-x release/1.0"))

    def test_fits_and_unrelated_commands_are_silent(self):
        self.config("qa-only")
        self.assertIsNone(self.ask("git checkout -b feature/ABC-1001-x origin/qa"))
        self.assertIsNone(self.ask("git status && git log -3"))
        self.assertIsNone(self.ask("git checkout qa"))
        self.assertIsNone(self.ask("git checkout -b scratch-no-ticket origin/release/1.0"))

    def test_current_branch_is_the_parent_when_none_is_given(self):
        self.config("qa-only")
        self.head("release/1.0")
        self.assertIsNotNone(self.ask("git checkout -b feature/ABC-1001-x"))
        self.head("qa")
        self.assertIsNone(self.ask("git checkout -b feature/ABC-1001-x"))

    def test_branch_name_suffix_hint(self):
        self.config("qa-cherry-pick")
        out = self.ask("git checkout -b feature/ABC-1001-x-uat origin/qa")
        self.assertIn("feature/ABC-1001-x-uat's name says it goes to uat, but it is cut from qa (qa)",
                      out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.ask("git checkout -b feature/ABC-1001-x-uat origin/uat"))

    def test_pr_into_an_unexpected_base(self):
        self.config("release-then-qa")
        self.records({"feature/ABC-1001-x": {"branch": "feature/ABC-1001-x", "ticket": "ABC-1001", "parent": "qa",
                                             "parent_role": "qa"}})
        self.head("feature/ABC-1001-x")
        out = self.ask("gh pr create --base release/1.0 --title fix")
        self.assertIn("PR from feature/ABC-1001-x (cut from qa) into release/1.0: release-then-qa for ABC-1001 expects a "
                      "qa-based branch to go into qa", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.ask("gh pr create --base qa --title fix"))
        out = self.ask('az repos pr create --source-branch feature/ABC-1001-x --target-branch refs/heads/release/1.0')
        self.assertIsNotNone(out)

    def test_strategy_rule_and_ignored_tickets(self):
        from chitragupta import common
        self.config("release-then-qa", ignore_tickets=["ABC-1002"])
        cfg = common.load_config()
        cfg["branching"]["repos"]["shop-api"]["rules"] = [{"jira_parent": "OPS-*", "strategy": "qa-only"}]
        common.write_json(common.wl("config.json"), cfg)
        common.write_json(common.hp("data", "tickets", "ABC-1001.json"), {"id": "ABC-1001", "jira": {"parent": "OPS-9"}})
        out = self.ask("git checkout -b feature/ABC-1001-x origin/release/1.0")
        self.assertIn("qa-only for ABC-1001 starts from qa (strategy: Jira parent OPS-9)", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.ask("git checkout -b feature/ABC-1002-x origin/uat"))

    def test_unconfigured_repo_is_silent(self):
        from chitragupta import common
        common.write_json(common.wl("config.json"), {"ticket_prefixes": ["ABC"], "branching": {"repos": {}}})
        self.assertIsNone(self.ask("git checkout -b feature/ABC-1001-x origin/release/1.0"))


class GuardHook(GuardCase):
    def payload(self, cmd):
        return {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": self.repo,
                "tool_input": {"command": cmd}}

    def test_hook_end_to_end_and_fail_open(self):
        self.config("qa-only")
        p = run_hook("pre-tool", self.payload("git checkout -b feature/ABC-1001-x origin/release/1.0"), self.child_env())
        self.assertEqual(p.returncode, 0)
        self.assertIn("additionalContext", json.loads(p.stdout)["hookSpecificOutput"])
        # nothing is recorded by the guard
        self.assertFalse(os.path.isdir(os.path.join(self.home, ".worklog", "raw")))
        # broken input, broken config: exit 0, no output
        p = run_hook("pre-tool", None, self.child_env(), raw_input=b"{not json")
        self.assertEqual((p.returncode, p.stdout), (0, b""))
        with open(os.path.join(self.home, ".worklog", "config.json"), "w", encoding="utf-8") as f:
            f.write("{broken")
        p = run_hook("pre-tool", self.payload("git checkout -b feature/ABC-1001-x origin/release/1.0"), self.child_env())
        self.assertEqual((p.returncode, p.stdout), (0, b""))

    def test_latency_under_200ms(self):
        self.config("qa-only")
        self.records({"feature/ABC-%d-x" % i: {"branch": "feature/ABC-%d-x" % i, "parent_role": "qa"} for i in range(300)})
        times = []
        for i in range(12):
            t = time.perf_counter()
            p = run_hook("pre-tool", self.payload("git checkout -b feature/ABC-1001-x origin/release/1.0"), self.child_env())
            times.append((time.perf_counter() - t) * 1000)
            self.assertEqual(p.returncode, 0)
        times = sorted(times[2:])  # the first runs warm the disk cache
        p95 = times[int(len(times) * 0.95) - 1]
        print("\nbranch guard p95 = %.0f ms (median %.0f ms)" % (p95, statistics.median(times)))
        self.assertLess(p95, 200)


if __name__ == "__main__":
    unittest.main()
