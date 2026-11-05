"""Your own commits and PRs (chitragupta/activity.py): confirmed authors only, author never committer, merges out,
numstat with root commits, binary and excluded files, co-authors kept apart, rebase / cherry-pick equivalence, amended
and squashed history, future times, and author-filtered PR lists rechecked before they are stored. Synthetic data:
temp git repos with per-commit identities and dates, developer@acme.test, service-api, ABC-…"""
import datetime as dt
import json
import os
import subprocess
import unittest

from tests.test_profile import LOOKALIKE, ME, ME2, OTHER, Base

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
GH_REMOTE = "https://github.com/acme-test/service-api.git"


def g(repo, *args, author=("Dev Example", ME), committer=None, adate="2026-11-04T10:00:00+00:00", cdate=None):
    c = committer or author
    env = dict(os.environ, GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1], GIT_COMMITTER_NAME=c[0],
               GIT_COMMITTER_EMAIL=c[1], GIT_AUTHOR_DATE=adate, GIT_COMMITTER_DATE=cdate or adate)
    r = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return r.stdout.strip()


def put(repo, path, text, msg, **kw):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    g(repo, "add", path, **kw)
    g(repo, "commit", "-q", "-m", msg, **kw)
    return g(repo, "rev-parse", "HEAD")


def lines(n, tag="x"):
    return "".join("%s%d\n" % (tag, i) for i in range(n))


class Case(Base, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.config(profile={"timezone": "+00:00", "tracking_start": "2026-10-01"})

    def tick(self, force=True, run=None, now=NOW):
        from chitragupta import activity, common
        return activity.tick(None, common.load_config(), {}, now=now, run=run, force=force)

    def ledger(self):
        from chitragupta import activity
        return activity.load(self.repo)

    def items(self):
        return self.ledger()["commits"]["items"]

    def counted(self):
        return sorted(s for s, it in self.items().items() if it["state"] == "counted")

    def day(self, d="2026-11-04"):
        from chitragupta import progress
        ctx = progress.Ctx(None, NOW)
        return progress.day_metrics(ctx, d, evidence=True)


class Commits(Case):
    def test_author_counts_never_committer_or_lookalike_or_same_display_name(self):
        base = put(self.repo, "a.txt", lines(1), "ABC-1001 base", author=("Colleague", OTHER))
        mine = put(self.repo, "b.txt", lines(2), "ABC-1001 mine", committer=("Colleague", OTHER))
        put(self.repo, "c.txt", lines(3), "theirs, I rebased it", author=("Colleague", OTHER), committer=("Dev Example", ME))
        put(self.repo, "d.txt", lines(4), "look-alike address", author=("Dev Example", LOOKALIKE))
        put(self.repo, "e.txt", lines(5), "same display name, other person", author=("Dev Example", OTHER))
        self.tick()
        self.assertEqual(sorted(self.items()), [mine])
        self.assertNotIn(base, self.items())
        it = self.items()[mine]
        self.assertEqual((it["role"], it["added"], it["ticket"]), ("author", 2, "ABC-1001"))
        self.assertNotIn(OTHER, json.dumps(self.ledger()))

    def test_several_confirmed_emails(self):
        self.cfg = self.config(git_author_emails=[ME, ME2])
        a = put(self.repo, "a.txt", lines(1), "one")
        b = put(self.repo, "b.txt", lines(1), "two", author=("Dev", ME2.upper()))
        self.tick()
        self.assertEqual(sorted(self.items()), sorted([a, b]))

    def test_root_commit_lines_merges_out_and_refs_counted_once(self):
        root = put(self.repo, "src/app.py", lines(3), "root")
        g(self.repo, "checkout", "-q", "-b", "feature/ABC-1001")
        feat = put(self.repo, "src/feature.py", lines(4), "ABC-1001 feature")
        g(self.repo, "checkout", "-q", "main")
        put(self.repo, "src/main.py", lines(1), "main moves on")
        g(self.repo, "merge", "-q", "--no-ff", "-m", "Merge feature", "feature/ABC-1001")
        g(self.repo, "branch", "copy-of-feature", "feature/ABC-1001")
        self.tick()
        items = self.items()
        self.assertEqual(items[root]["added"], 3)  # the root commit's whole tree, not zero
        self.assertEqual(len(items), 3)  # root, feature, main move: no merge commit, the feature once
        self.assertIn(feat, items)
        self.assertEqual(self.day()["commits"]["value"], 3)

    def test_binary_lockfile_and_generated_files_are_apart(self):
        os.makedirs(os.path.join(self.repo, "img"))
        with open(os.path.join(self.repo, "img", "logo.png"), "wb") as f:
            f.write(bytes(range(256)) * 40)
        g(self.repo, "add", "img/logo.png")
        put(self.repo, "package-lock.json", lines(100), "deps")
        it = self.items_after()
        self.assertEqual(it["added"], 0)
        self.assertEqual(it["excluded"], {"added": 100, "deleted": 0, "files": 1})
        self.assertEqual(it["binary_files"], 1)
        put(self.repo, "dist/bundle.min.js", lines(50), "build")
        put(self.repo, "src/real.py", lines(7), "real")
        self.tick()
        m = self.day()
        self.assertEqual((m["lines"]["added"], m["excluded"]["added"], m["binary_files"]), (7, 150, 1))

    def items_after(self):
        self.tick()
        return next(iter(self.items().values()))

    def test_coauthors_are_shared_or_coauthored_never_headline(self):
        put(self.repo, "a.txt", lines(10), "pair work\n\nCo-authored-by: Colleague <%s>" % OTHER)
        theirs = put(self.repo, "b.txt", lines(20), "their work\n\nCo-authored-by: Dev Example <%s>" % ME.upper(),
                     author=("Colleague", OTHER))
        put(self.repo, "c.txt", lines(30), "look-alike co-author\n\nCo-authored-by: X <%s>" % LOOKALIKE,
            author=("Colleague", OTHER))
        self.tick()
        self.assertEqual(len(self.items()), 2)
        self.assertEqual(self.items()[theirs]["role"], "coauthor")
        m = self.day()
        self.assertEqual(m["commits"]["value"], 1)
        self.assertEqual(m["lines"]["added"], 0)  # the shared commit's diff is never "your" lines
        self.assertEqual(m["shared"], {"commits": 1, "added": 10, "deleted": 0})
        self.assertEqual(m["coauthored"], {"commits": 1, "added": 20, "deleted": 0})

    def test_cherry_pick_counted_once(self):
        put(self.repo, "a.txt", lines(1), "base")
        g(self.repo, "checkout", "-q", "-b", "feature/ABC-1001")
        orig = put(self.repo, "b.txt", lines(5), "ABC-1001 fix", adate="2026-11-03T10:00:00+00:00")
        g(self.repo, "checkout", "-q", "main")
        g(self.repo, "cherry-pick", "-x", orig, adate="2026-11-03T10:00:00+00:00", cdate="2026-11-04T09:00:00+00:00")
        self.tick()
        states = sorted(it["state"] for it in self.items().values())
        self.assertEqual(states, ["counted", "counted", "equivalent"])
        self.assertEqual(self.day("2026-11-03")["commits"]["value"], 1)
        ev = self.day("2026-11-03")["evidence"]["not_counted"]
        self.assertEqual(len(ev), 1)
        self.assertIn("counted once", ev[0]["why"])

    def test_amended_commit_leaves_the_counts(self):
        first = put(self.repo, "a.txt", lines(2), "draft")
        self.tick()
        self.assertEqual(self.counted(), [first])
        with open(os.path.join(self.repo, "a.txt"), "a", encoding="utf-8") as f:
            f.write("more\n")
        g(self.repo, "add", "a.txt")
        g(self.repo, "commit", "-q", "--amend", "-m", "final")
        new = g(self.repo, "rev-parse", "HEAD")
        self.tick()
        self.assertEqual(self.counted(), [new])
        self.assertEqual(self.items()[first]["state"], "unreachable")
        self.assertEqual(self.day()["commits"]["value"], 1)

    def test_future_author_time_is_never_today(self):
        put(self.repo, "a.txt", lines(1), "clock skew", adate="2026-11-09T10:00:00+00:00")
        self.tick()
        self.assertEqual(next(iter(self.items().values()))["state"], "future")
        from chitragupta import progress
        t = progress.today(None, NOW)
        self.assertEqual(t["metrics"]["commits"]["value"], 0)
        self.assertEqual(len(t["metrics"]["unresolved"]), 1)
        self.assertIn("future", t["metrics"]["unresolved"][0]["why"])

    def test_no_confirmed_email_collects_nothing(self):
        self.cfg = self.config(git_author_emails=[])
        put(self.repo, "a.txt", lines(1), "anything")
        self.tick()
        led = self.ledger()
        self.assertEqual(led["commits"]["status"], "unconfigured")
        self.assertEqual(led["commits"]["items"], {})
        self.assertEqual(self.day()["commits"]["status"], "unknown")

    def test_refresh_is_idempotent(self):
        put(self.repo, "a.txt", lines(3), "one")
        put(self.repo, "b.txt", lines(4), "two")
        self.tick()
        first = {s: {k: v for k, v in it.items() if k != "first_seen"} for s, it in self.items().items()}
        m1 = self.day()
        from chitragupta import progress
        c1 = progress.celebrations(progress.Ctx(None, NOW), "2026-11-04")
        self.tick()
        self.tick(force=False)
        second = {s: {k: v for k, v in it.items() if k != "first_seen"} for s, it in self.items().items()}
        self.assertEqual(first, second)
        self.assertEqual(self.day(), m1)
        self.assertEqual(progress.celebrations(progress.Ctx(None, NOW), "2026-11-04"), c1)

    def test_alias_removal_rebuilds_the_ledger(self):
        mine = put(self.repo, "a.txt", lines(1), "mine")
        alt = put(self.repo, "b.txt", lines(1), "alt", author=("Dev", ME2))
        self.cfg = self.config(git_author_emails=[ME, ME2])
        self.tick()
        self.assertEqual(sorted(self.items()), sorted([mine, alt]))
        self.cfg = self.config(git_author_emails=[ME])
        self.assertEqual(self.day()["commits"]["status"], "unknown")  # old ledger: never shown under the new profile
        self.tick()
        self.assertEqual(sorted(self.items()), [mine])
        self.assertEqual(self.day()["commits"]["value"], 1)


class PRs(Case):
    def setUp(self):
        super().setUp()
        g(self.repo, "remote", "add", "origin", GH_REMOTE)
        gh = os.path.join(self.home, "gh.exe")
        with open(gh, "w") as f:
            f.write("")
        self.cfg = self.config(paths={"gh": gh}, profile={
            "timezone": "+00:00", "tracking_start": "2026-10-01",
            "accounts": [{"provider": "github", "instance": "github.com", "id": "", "node_id": "MDQ6VXNlcjEwMDE=",
                          "login": "dev-example"}]})
        self.calls = []

    def stub(self, rows):
        def run(argv, timeout=60):
            self.calls.append(argv)
            return 0, json.dumps(rows), ""
        return run

    def pr(self, n, state="OPEN", draft=False, created="2026-11-02T10:00:00Z", merged=None, login="dev-example",
           node="MDQ6VXNlcjEwMDE=", base="main", head="feature/ABC-1001", oid="1" * 40, merge_oid=None):
        return {"number": n, "title": "ABC-1001 change %d" % n, "state": state, "isDraft": draft, "baseRefName": base,
                "headRefName": head, "headRefOid": oid, "mergedAt": merged, "mergeCommit": {"oid": merge_oid} if merge_oid else None,
                "reviewDecision": "", "url": "https://github.com/acme-test/service-api/pull/%d" % n, "createdAt": created,
                "author": {"id": node, "login": login, "is_bot": False}}

    def test_author_filtered_request_and_recheck(self):
        theirs = self.pr(9, base="qa", head="integration/qa", login="colleague", node="MDQ6VXNlcjIwMDI=")
        same_login_other_node = self.pr(8, node="MDQ6VXNlcjMwMDM=")
        self.tick(run=self.stub([self.pr(1, draft=True), theirs, same_login_other_node]))
        argv = self.calls[0]
        self.assertEqual(argv[argv.index("--author") + 1], "dev-example")
        self.assertNotIn("@me", argv)
        self.assertTrue(argv[argv.index("--search") + 1].startswith("updated:2026-"))
        from chitragupta.safety import commands
        self.assertIsNone(commands.reason(["gh"] + argv[1:], "host"))
        prs = self.ledger()["prs"]
        self.assertEqual(sorted(prs["items"]), ["1"])  # someone else's integration PR is never stored
        self.assertEqual(prs["status"], "ok")
        m = self.day("2026-11-02")
        self.assertEqual((m["prs_raised"]["value"], m["prs_raised"]["drafts"]), (1, 1))
        self.assertNotIn("colleague", json.dumps(self.ledger()))

    def test_wrong_connected_account_stores_nothing(self):
        self.tick(run=self.stub([self.pr(3, login="someone-else", node="MDQ6VXNlcjQwMDQ=")]))
        self.assertEqual(self.ledger()["prs"]["items"], {})
        self.assertEqual(self.day("2026-11-02")["prs_raised"]["status"], "zero")

    def test_reopen_and_ready_are_the_same_pr_and_dates_are_separate(self):
        self.tick(run=self.stub([self.pr(5, draft=True)]))
        self.tick(run=self.stub([self.pr(5, state="MERGED", merged="2026-11-04T15:00:00Z")]))
        items = self.ledger()["prs"]["items"]
        self.assertEqual(list(items), ["5"])
        self.assertEqual(self.day("2026-11-02")["prs_raised"]["value"], 1)
        self.assertEqual(self.day("2026-11-02")["prs_merged"]["value"], 0)
        self.assertEqual(self.day("2026-11-04")["prs_merged"]["value"], 1)
        self.assertEqual(self.day("2026-11-04")["prs_raised"]["value"], 0)

    def test_full_page_is_partial(self):
        rows = [self.pr(i) for i in range(1, 201)]
        self.tick(run=self.stub(rows))
        prs = self.ledger()["prs"]
        self.assertEqual((prs["status"], prs["capped"]), ("partial", True))
        self.assertEqual(self.day("2026-11-02")["prs_raised"]["status"], "partial")

    def test_no_account_or_no_author_source_is_unknown_never_zero(self):
        self.cfg = self.config(profile={"timezone": "+00:00", "tracking_start": "2026-10-01"})
        self.tick(run=self.stub([self.pr(1)]))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger()["prs"]["status"], "unconfigured")
        self.assertEqual(self.day("2026-11-02")["prs_raised"]["status"], "unknown")
        self.assertIsNone(self.day("2026-11-02")["prs_raised"]["value"])

    def test_squash_merge_of_your_pr_is_not_counted_twice(self):
        put(self.repo, "a.txt", lines(1), "base")
        g(self.repo, "checkout", "-q", "-b", "feature/ABC-1001")
        head = put(self.repo, "b.txt", lines(5), "ABC-1001 one")
        g(self.repo, "checkout", "-q", "main")
        g(self.repo, "merge", "-q", "--squash", "feature/ABC-1001")
        squash = put(self.repo, "c.txt", lines(1), "ABC-1001 change (#7)", committer=("Host", "noreply@acme.test"))
        self.tick(run=self.stub([self.pr(7, state="MERGED", merged="2026-11-04T11:00:00Z", oid=head, merge_oid=squash)]))
        self.assertEqual(self.items()[squash]["state"], "integration")
        self.assertEqual(self.items()[head]["state"], "counted")
        self.assertEqual(self.day()["commits"]["value"], 2)  # base and the PR's own commit; the squash once


if __name__ == "__main__":
    unittest.main()
