"""Branch tracking where it shows up: the 8 PM Delivery check block, the daily line, SessionStart, the ticket README,
the /cg:branches / /cg:branch commands and their inbox lines. Synthetic data only."""
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tests.support import rd
from tests.test_upgrade02 import AgentCase

LOST = {"kind": "lost", "text": "3 commits not in release/1.4 — pushed after PR #81234 merged.",
        "commits": ["a1b2c3d4e", "b2c3d4e5f", "c3d4e5f6a"], "target": "release"}
STALE = {"kind": "pr-stale", "text": "PR #81240 into qa has been open 5 days.", "commits": [], "pr": 81240}


def git_block(alerts):
    return {"repos": {"shop-api": {
        "repo": "shop-api", "strategy": "release-then-qa", "strategy_how": "repo default", "pr_source": "gh",
        "branches": [{"branch": "feature/ABC-1-sms", "parent": "release/1.4", "parent_role": "release", "parent_source": "reflog",
                      "status": "active", "suggested": None, "prs": [81234], "unpushed": 0, "own": 5, "behind": 2, "link": "branch name"},
                     {"branch": "feature/ABC-1-sms-qa", "parent": "qa", "parent_role": "qa", "parent_source": "command",
                      "status": "pr_open", "suggested": None, "prs": [81240], "unpushed": 0, "own": 1, "behind": 0, "link": "branch name"}],
        "prs": [{"number": 81234, "title": "ABC-1 sms", "source": "feature/ABC-1-sms", "target": "release/1.4", "state": "merged",
                 "review": "approved", "merged_at": "2026-10-15T10:00:00+05:30", "url": "https://git.acme.test/shop-api/pull/81234"},
                {"number": 81240, "title": "ABC-1 sms qa", "source": "feature/ABC-1-sms-qa", "target": "qa", "state": "open",
                 "review": "", "merged_at": None, "url": "https://git.acme.test/shop-api/pull/81240"}],
        "targets": [{"role": "release", "name": "release/1.4", "how": "PR #81234 target"}, {"role": "qa", "name": "qa", "how": "base"}],
        "rows": [{"id": "x1", "shas": ["a1b2c3d4e"], "subject": "ABC-1 add source", "date": "2026-10-15T11:00:00+05:30",
                  "branches": ["feature/ABC-1-sms"], "cells": {"release": {"s": "missing", "why": "pushed after PR #81234 merged"},
                                                                "qa": {"s": "open", "pr": 81240}}}],
        "alerts": alerts}},
        "alerts": [dict(a, repo="shop-api") for a in alerts],
        "summary": "Branches 2 · PRs 1 merged, 1 open · Delivery ⚠ 3 commits not in release/1.4" if alerts else
                   "Branches 2 · PRs 1 merged, 1 open · Delivery ✓"}


class DeliveryCheckSlack(AgentCase):
    def put_git(self, ticket, alerts):
        from chitragupta import common
        p = common.hp("data", "tickets", ticket + ".json")
        tk = common.read_json(p, {}) or {"id": ticket}
        tk["git"] = git_block(alerts)
        common.write_json(p, tk)

    def test_golden_block_and_jira_entries_unchanged(self):
        from chitragupta import jiraworklog
        d = "2026-10-16"
        self.raw(d, "ABC-1", 11, 0, n=20)
        self.raw(d, "ABC-2", 15, 0, n=20, sid="s2")
        self.clock(d, 20, 5)
        a = self.agent()
        a.ingest()
        a.store.save()
        plain = a.jwl.compose(d, d)["text"]  # the Jira part as composed without any alert
        self.put_git("ABC-1", [LOST, STALE])
        a = self.agent()
        a.ingest()
        a.jwl.run_day(d, "due")
        self.assertEqual(len(self.sent), 1)
        block = ("Delivery check\n"
                 "• ABC-1: 3 commits not in release/1.4 — pushed after PR #81234 merged. (a1b2c3d4e, b2c3d4e5f, c3d4e5f6a)\n"
                 "• ABC-1: PR #81240 into qa has been open 5 days.")
        self.assertEqual(self.sent[0], plain + "\n\n" + block)
        self.assertEqual(jiraworklog.load_sheet(d)["versions"][-1]["text"], plain + "\n\n" + block)

    def test_slack_drops_the_command_hint(self):
        from chitragupta import jiraworklog
        from chitragupta.store import Store
        wp = {"kind": "wrong-parent", "commits": [], "text": "feature/ABC-1-a was created from release/1.4, which qa-only "
              "doesn't use (qa). It looks superseded: /cg:branch abandon shop-api feature/ABC-1-a"}
        self.put_git("ABC-1", [wp])
        self.assertEqual(jiraworklog.delivery_block(Store(), ["ABC-1"]),
                         "Delivery check\n• ABC-1: feature/ABC-1-a was created from release/1.4, which qa-only doesn't use "
                         "(qa). It looks superseded.")

    def test_no_alerts_no_block(self):
        d = "2026-10-16"
        self.raw(d, "ABC-1", 11, 0, n=20)
        self.clock(d, 20, 5)
        self.put_git("ABC-1", [])
        a = self.agent()
        a.ingest()
        a.jwl.run_day(d, "due")
        self.assertNotIn("Delivery check", self.sent[0])


class Rendered(AgentCase):
    def store_with(self, alerts):
        from chitragupta.store import Store
        st = Store()
        tk = st.ticket("ABC-1")
        tk.update(title="SMS reminders", first="2026-10-15", last="2026-10-16", git=git_block(alerts))
        return st

    def test_daily_line_has_no_urls(self):
        from chitragupta import render
        st = self.store_with([LOST])
        dd = st.day("2026-10-16")
        dd["tickets"]["ABC-1"] = {"minutes": 30, "item_ids": [], "what_i_did": []}
        md = render.render_day(st, "2026-10-16", {}, {})
        self.assertIn("- **Branches & PRs** — Branches 2 · PRs 1 merged, 1 open · Delivery ⚠ 3 commits not in release/1.4", md)
        self.assertNotIn("git.acme.test", md)

    def test_sessionstart_line_and_ticket_readme(self):
        from chitragupta import render
        st = self.store_with([LOST, STALE])
        ctx = render.render_context(st, "ABC-1")
        self.assertTrue(ctx.startswith("ABC-1: 3 commits not in release/1.4 — pushed after PR #81234 merged. (+1 more on the ticket page)"))
        md = render.render_ticket(st, "ABC-1")
        self.assertIn("## Branches & PRs", md)
        self.assertIn("- ⚠ 3 commits not in release/1.4 — pushed after PR #81234 merged.", md)
        self.assertIn("| `a1b2c3d4e` | ABC-1 add source | ✗ pushed after PR #81234 merged | ⏳ PR #81240 |", md)
        self.assertIn("[#81234](https://git.acme.test/shop-api/pull/81234)", md)
        self.assertEqual(render.alert_line("ABC-1", st.ticket("ABC-2")), "")

    def test_sessionstart_hook_prints_the_alert(self):
        from chitragupta import common, render
        st = self.store_with([LOST])
        os.makedirs(common.wl("cache", "context"), exist_ok=True)
        common.write_text(common.wl("cache", "context", "ABC-1.md"), render.render_context(st, "ABC-1"))
        import subprocess
        import tempfile
        from tests.support import rmtree, run_hook
        repo = tempfile.mkdtemp(prefix="cg-ss-")
        try:
            subprocess.run(["git", "init", "-q", "-b", "feature/ABC-1-sms", repo], check=True, capture_output=True)
            p = run_hook("session-start", {"session_id": "s9", "cwd": repo, "source": "startup"}, self.child_env())
            ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertTrue(ctx.startswith("ABC-1: 3 commits not in release/1.4 — pushed after PR #81234 merged."))
        finally:
            rmtree(repo)


class Commands(AgentCase):
    def cli(self, *args):
        from chitragupta import cli
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            rc = cli.main(list(args))
        return rc, o.getvalue(), e.getvalue()

    def test_branch_commands_write_inbox_lines(self):
        from chitragupta import common
        self.assertEqual(self.cli("branch", "link", "shop-api", "uat-sync", "abc-1")[0], 0)
        self.assertEqual(self.cli("branch", "abandon", "shop-api", "feature/ABC-1-old")[0], 0)
        self.assertEqual(self.cli("branch", "strategy", "ABC-1", "qa-only")[0], 0)
        rc, _, err = self.cli("branch", "strategy", "shop-api", "no-such")
        self.assertEqual(rc, 2)
        self.assertIn("unknown strategy", err)
        self.assertEqual(self.cli("branch", "link", "shop-api")[0], 2)
        rc, out, _ = self.cli("branch", "abandon", "shop-api", "feature/ABC-1-old")
        self.assertIn("Already in inbox.md", out)
        lines = [l for l in rd(common.hp("inbox.md")).splitlines() if l.strip()]
        self.assertEqual(lines[-3:], ["branch-link: ABC-1 shop-api uat-sync", "branch-abandon: shop-api feature/ABC-1-old",
                                      "strategy: ABC-1 qa-only"])

    def test_inbox_directives_reach_the_agent(self):
        from chitragupta import gittrack
        a = self.agent()
        self.assertTrue(a.handle_branch_directive({"text": "branch-abandon: shop-api feature/ABC-1-old", "ts": "2026-10-16T10:00:00+05:30"}))
        self.assertTrue(a.handle_branch_directive({"text": "strategy: ABC-1 qa-only", "ts": "2026-10-16T10:00:00+05:30"}))
        self.assertFalse(a.handle_branch_directive({"text": "time: 2026-10-16 1h ABC-1 Sync.", "ts": "2026-10-16T10:00:00+05:30"}))
        self.assertIn("feature/ABC-1-old", gittrack.RepoData("shop-api").meta["manual"]["abandoned"])
        self.assertEqual(a.store.ticket("ABC-1")["git_override"]["strategy"], "qa-only")
        self.assertIn("ABC-1", a.git_force)

    def test_branches_report(self):
        from chitragupta import common
        common.write_json(common.hp("data", "tickets", "ABC-1.json"), {"id": "ABC-1", "git": git_block([LOST])})
        rc, out, _ = self.cli("branches", "ABC-1")
        self.assertEqual(rc, 0)
        self.assertIn("## shop-api · strategy release-then-qa (repo default) · PRs from gh", out)
        self.assertIn("- feature/ABC-1-sms ← release/1.4 · active · PR #81234", out)
        self.assertIn("- release/1.4: ✗ 1", out)
        self.assertIn("- ⚠ 3 commits not in release/1.4 — pushed after PR #81234 merged. (a1b2c3d4e, b2c3d4e5f, c3d4e5f6a)", out)
        rc, out, _ = self.cli("branches", "ABC-9")
        self.assertIn("No branches recorded for ABC-9", out)


if __name__ == "__main__":
    unittest.main()
