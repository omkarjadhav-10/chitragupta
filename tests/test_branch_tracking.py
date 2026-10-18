"""Branch tracking: throwaway repos built with real git and fixed dates, PR data from a fake host.

Each scenario builds an `origin` (bare) and a clone `shop-api` with base branches main, release/1.0, qa, uat, staging,
then plays out one way of working and checks the delivery matrix and the alerts.
"""
import ast
import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest

from tests.support import ROOT, Isolated, rmtree

T = "ABC-1001"
ME = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@acme.test",
      "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@acme.test"}
OTHER = {"GIT_AUTHOR_NAME": "Other", "GIT_AUTHOR_EMAIL": "other@acme.test",
         "GIT_COMMITTER_NAME": "Other", "GIT_COMMITTER_EMAIL": "other@acme.test"}
TZ = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=TZ)


class Sandbox:
    """A clone of a bare origin; every git call advances a fixed clock by one minute."""

    def __init__(self, root):
        self.root = root
        self.origin = os.path.join(root, "origin.git")
        self.work = os.path.join(root, "shop-api")
        self.t = dt.datetime(2026, 10, 1, 9, 0, tzinfo=TZ)
        self.n = 0
        self.run(["init", "-q", "--bare", "-b", "main", self.origin], cwd=root)
        self.run(["clone", "-q", self.origin, self.work], cwd=root)
        self.commit("initial", who=OTHER)
        self.git("push", "-q", "-u", "origin", "main")
        for b in ("release/1.0", "qa", "uat", "staging"):
            self.git("checkout", "-q", "-b", b, "main")
            self.commit("base for " + b, who=OTHER)
            self.git("push", "-q", "-u", "origin", b)
        self.git("checkout", "-q", "main")

    def run(self, args, cwd=None, who=None, check=True):
        self.t += dt.timedelta(minutes=1)
        env = dict(os.environ)
        env.update(who or ME)
        env.update(GIT_AUTHOR_DATE=self.t.isoformat(), GIT_COMMITTER_DATE=self.t.isoformat(),
                   GIT_CONFIG_NOSYSTEM="1", GIT_EDITOR="true")
        p = subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
                            "-c", "core.autocrlf=false", "-c", "advice.detachedHead=false"] + args,
                           cwd=cwd or self.work, env=env, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise AssertionError("git %s failed: %s" % (" ".join(args), p.stderr))
        return p.stdout.strip()

    def git(self, *args, who=None, check=True):
        return self.run(list(args), who=who, check=check)

    def commit(self, msg, who=None, path=None, content=None):
        self.n += 1
        path = path or "f%d.txt" % self.n
        with open(os.path.join(self.work, path), "w", encoding="utf-8", newline="\n") as f:
            f.write(content if content is not None else "change %d: %s\n" % (self.n, msg))
        self.git("add", path, who=who)
        self.git("commit", "-q", "-m", msg, who=who)
        return self.git("rev-parse", "HEAD")

    def branch(self, name, start):
        self.git("checkout", "-q", "-b", name, start)

    def push(self, b):
        self.git("push", "-q", "-u", "origin", b)

    def tip(self, ref):
        return self.git("rev-parse", ref)

    def merge(self, src, target, squash=False, msg=None):
        """Merge `src` into `target` on origin, as a host would on PR completion. -> (merge commit, head at merge)."""
        head = self.tip(src)
        self.git("checkout", "-q", target)
        self.git("merge", "-q", "--ff-only", "origin/" + target)
        if squash:
            self.git("merge", "-q", "--squash", src)
            self.git("commit", "-q", "-m", msg or "squash %s" % src)
        else:
            self.git("merge", "-q", "--no-ff", "-m", msg or "Merge %s" % src, src)
        self.push(target)
        return self.tip("HEAD"), head

    def fetch(self):
        self.git("fetch", "-q", "origin")


def pr(number, source, target, state="merged", merge_commit=None, head=None, created="2026-10-01T10:00:00+05:30",
       review=""):
    return {"number": number, "title": "%s %s" % (T, source), "source": source, "target": target, "state": state,
            "review": review, "created_at": created, "merged_at": "2026-10-02T10:00:00+05:30" if state == "merged" else None,
            "merge_commit": merge_commit, "head": head, "head_at_merge": head if state == "merged" else None,
            "url": "https://git.acme.test/shop-api/pull/%d" % number, "src": "test"}


class TrackingCase(Isolated, unittest.TestCase):
    strategy = "qa-only"

    def setUp(self):
        self.isolate()
        from chitragupta import hosts
        self._real_run = hosts._run

        def no_cli(*a, **k):
            self.forbidden.append("hosts._run")
            return 127, "", "tests: no host CLI"
        hosts._run = no_cli
        self.tmp = tempfile.mkdtemp(prefix="cg-git-")
        self.sb = Sandbox(self.tmp)
        os.makedirs(os.path.join(self.home, "data", "tickets"), exist_ok=True)
        for t in (T, "ABC-1002"):
            with open(os.path.join(self.home, "data", "tickets", t + ".json"), "w", encoding="utf-8") as f:
                json.dump({"id": t, "last": "2026-10-10"}, f)
        self.prs = []

    def tearDown(self):
        from chitragupta import hosts
        hosts._run = self._real_run
        rmtree(self.tmp)
        self.restore()

    def cfg(self, strategy=None, **repo):
        rc = {"strategy": strategy or self.strategy}
        rc.update(repo)
        return {"watched_repos": [self.sb.work.replace("\\", "/")], "git_author_emails": ["test@acme.test"],
                "ticket_prefixes": ["ABC"], "branching": {"repos": {"shop-api": rc}}}

    def track(self, strategy=None, fetch=False, ticket=T, **repo):
        from chitragupta import delivery, gittrack
        from chitragupta.store import Store
        cfg = self.cfg(strategy, **repo)
        self.store = Store()
        prs = self.prs
        tr = gittrack.Tracker(cfg, {}, self.store, now=NOW, pr_fetch=lambda r, b: [p for p in prs if p["source"] == b])
        self.rx = tr.refresh(fetch=fetch)
        delivery.update_ticket(self.store, ticket, self.rx, cfg, now=NOW, force=True)
        g = self.store.ticket(ticket)["git"]
        self.git = g
        return g["repos"]["shop-api"]

    @staticmethod
    def statuses(x, role):
        return [row["cells"][role]["s"] for row in x["rows"]]

    @staticmethod
    def kinds(x):
        return sorted(a["kind"] for a in x["alerts"])

    def branch(self, x, name):
        return next(b for b in x["branches"] if b["branch"] == name)


class Scenarios(TrackingCase):
    def test_01_release_then_qa_happy_path(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-sms", "origin/release/1.0")
        sb.commit("ABC-1001 cancel pending SMS")
        sb.commit("ABC-1001 better reason")
        sb.push("feature/ABC-1001-sms")
        m1, h1 = sb.merge("feature/ABC-1001-sms", "release/1.0")
        sb.git("checkout", "-q", "release/1.0")
        sb.commit("release prep", who=OTHER)  # someone else's work on release: must not count for the ticket
        sb.push("release/1.0")
        sb.branch("feature/ABC-1001-sms-qa", "origin/qa")
        sb.git("merge", "-q", "--no-ff", "-m", "bring ABC-1001 to qa", "feature/ABC-1001-sms")
        sb.push("feature/ABC-1001-sms-qa")
        m2, h2 = sb.merge("feature/ABC-1001-sms-qa", "qa")
        self.prs = [pr(1, "feature/ABC-1001-sms", "release/1.0", merge_commit=m1, head=h1),
                    pr(2, "feature/ABC-1001-sms-qa", "qa", merge_commit=m2, head=h2)]
        x = self.track("release-then-qa")
        self.assertEqual(x["strategy"], "release-then-qa")
        self.assertEqual([t["name"] for t in x["targets"]], ["release/1.0", "qa"])
        self.assertEqual(len(x["rows"]), 2)
        self.assertEqual(self.statuses(x, "release"), ["in", "in"])
        self.assertEqual(self.statuses(x, "qa"), ["in", "in"])
        self.assertEqual(x["alerts"], [])
        b = self.branch(x, "feature/ABC-1001-sms")
        self.assertEqual((b["parent"], b["parent_source"], b["status"]), ("release/1.0", "reflog", "merged"))
        self.assertEqual(self.branch(x, "feature/ABC-1001-sms-qa")["parent"], "qa")
        self.assertIn("Delivery ✓", self.git["summary"])

    def test_02_lost_commits_pushed_after_release_pr_merged(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-sms", "origin/release/1.0")
        sb.commit("ABC-1001 cancel pending SMS")
        sb.commit("ABC-1001 better reason")
        sb.push("feature/ABC-1001-sms")
        m1, h1 = sb.merge("feature/ABC-1001-sms", "release/1.0")
        sb.git("checkout", "-q", "feature/ABC-1001-sms")
        lost = [sb.commit("ABC-1001 add source %d" % i) for i in range(3)]
        sb.push("feature/ABC-1001-sms")
        sb.branch("feature/ABC-1001-sms-qa", "origin/qa")
        sb.git("merge", "-q", "--no-ff", "-m", "bring ABC-1001 to qa", "feature/ABC-1001-sms")
        sb.push("feature/ABC-1001-sms-qa")
        m2, h2 = sb.merge("feature/ABC-1001-sms-qa", "qa")
        self.prs = [pr(1, "feature/ABC-1001-sms", "release/1.0", merge_commit=m1, head=h1),
                    pr(2, "feature/ABC-1001-sms-qa", "qa", merge_commit=m2, head=h2)]
        x = self.track("release-then-qa")
        self.assertEqual(self.statuses(x, "release"), ["in", "in", "missing", "missing", "missing"])
        self.assertEqual(self.statuses(x, "qa"), ["in"] * 5)
        whys = {row["cells"]["release"].get("why") for row in x["rows"][2:]}
        self.assertEqual(whys, {"pushed after PR #1 merged"})
        a = [a for a in x["alerts"] if a["kind"] == "lost"]
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["text"], "3 commits not in release/1.0 — pushed after PR #1 merged.")
        self.assertEqual(sorted(a[0]["commits"]), sorted(s[:9] for s in lost))
        self.assertNotIn("gap", self.kinds(x))  # the same commits are not reported twice
        self.assertIn("Delivery ⚠ 3 commits not in release/1.0", self.git["summary"])
        self.assertEqual(self.branch(x, "feature/ABC-1001-sms")["status"], "active")  # new commits after the merge

    def test_03_qa_branch_cut_before_the_last_commits(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-sms", "origin/release/1.0")
        sb.commit("ABC-1001 one")
        sb.commit("ABC-1001 two")
        sb.push("feature/ABC-1001-sms")
        sb.branch("feature/ABC-1001-sms-qa", "origin/qa")
        sb.git("merge", "-q", "--no-ff", "-m", "bring ABC-1001 to qa", "feature/ABC-1001-sms")
        sb.push("feature/ABC-1001-sms-qa")
        m2, h2 = sb.merge("feature/ABC-1001-sms-qa", "qa")
        sb.git("checkout", "-q", "feature/ABC-1001-sms")
        sb.commit("ABC-1001 three")
        sb.commit("ABC-1001 four")
        sb.push("feature/ABC-1001-sms")
        m1, h1 = sb.merge("feature/ABC-1001-sms", "release/1.0")
        self.prs = [pr(1, "feature/ABC-1001-sms", "release/1.0", merge_commit=m1, head=h1),
                    pr(2, "feature/ABC-1001-sms-qa", "qa", merge_commit=m2, head=h2)]
        x = self.track("release-then-qa")
        self.assertEqual(self.statuses(x, "release"), ["in"] * 4)
        self.assertEqual(self.statuses(x, "qa"), ["in", "in", "missing", "missing"])
        gap = [a for a in x["alerts"] if a["kind"] == "gap"]
        self.assertEqual([a["text"] for a in gap], ["2 commits in release/1.0 but not in qa."])

    def test_04_qa_only_open_pr(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-x", "origin/qa")
        sb.commit("ABC-1001 one")
        sb.commit("ABC-1001 two")
        sb.push("feature/ABC-1001-x")
        self.prs = [pr(3, "feature/ABC-1001-x", "qa", state="open", head=sb.tip("HEAD"), created="2026-10-09T10:00:00+05:30")]
        x = self.track("qa-only")
        self.assertEqual(self.statuses(x, "qa"), ["open", "open"])
        self.assertEqual(self.branch(x, "feature/ABC-1001-x")["status"], "pr_open")
        self.assertEqual(x["alerts"], [])

    def test_05_qa_cherry_pick_uat_done_staging_missing_and_conflict(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-x", "origin/qa")
        c1 = sb.commit("ABC-1001 one")
        c2 = sb.commit("ABC-1001 two", path="shared.txt", content="line A\n")
        sb.push("feature/ABC-1001-x")
        m, h = sb.merge("feature/ABC-1001-x", "qa")
        sb.branch("feature/ABC-1001-x-uat", "origin/uat")
        sb.git("cherry-pick", "-x", c1)
        sb.git("cherry-pick", "-x", "-n", c2)  # the conflict, resolved by hand: the patch changes
        with open(os.path.join(sb.work, "shared.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("line A (uat)\n")
        sb.git("add", "shared.txt")
        sb.git("commit", "-q", "-m", "ABC-1001 two\n\n(cherry picked from commit %s)" % c2)
        sb.push("feature/ABC-1001-x-uat")
        mu, hu = sb.merge("feature/ABC-1001-x-uat", "uat")
        self.prs = [pr(1, "feature/ABC-1001-x", "qa", merge_commit=m, head=h),
                    pr(2, "feature/ABC-1001-x-uat", "uat", merge_commit=mu, head=hu)]
        x = self.track("qa-cherry-pick")
        self.assertEqual(len(x["rows"]), 2)  # each copy joins its original: same patch-id, or a -x trailer
        r1, r2 = x["rows"]
        self.assertEqual(r1["subject"], "ABC-1001 one")
        self.assertEqual([r1["cells"][k]["s"] for k in ("qa", "uat", "staging")], ["in", "in", "missing"])
        self.assertEqual([r2["cells"][k]["s"] for k in ("qa", "uat", "staging")], ["in", "check", "missing"])
        self.assertEqual(r2["loose"], [hu[:9]])
        self.assertEqual([a["text"] for a in x["alerts"] if a["kind"] == "gap"], ["2 commits in qa but not in staging."])
        self.assertEqual([a["text"] for a in x["alerts"] if a["kind"] == "check"],
                         ["1 commit reached uat only with a changed patch — check it by hand."])

    def test_06_wrong_parent_superseded_and_empty(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-a", "origin/release/1.0")  # wrong for qa-only, never used
        sb.branch("feature/ABC-1001-b", "origin/qa")
        sb.commit("ABC-1001 the fix")
        sb.push("feature/ABC-1001-b")
        self.prs = [pr(4, "feature/ABC-1001-b", "qa", state="open", head=sb.tip("HEAD"), created="2026-10-09T10:00:00+05:30")]
        x = self.track("qa-only")
        a = self.branch(x, "feature/ABC-1001-a")
        self.assertEqual(a["parent"], "release/1.0")
        self.assertEqual(a["suggested"], "abandon (superseded by feature/ABC-1001-b)")
        self.assertEqual(len(x["rows"]), 1)
        self.assertEqual(self.statuses(x, "qa"), ["open"])
        wp = [a for a in x["alerts"] if a["kind"] == "wrong-parent"]
        self.assertEqual(len(wp), 1)
        self.assertIn("/cg:branch abandon shop-api feature/ABC-1001-a", wp[0]["text"])
        self.assertNotIn("only-copy", self.kinds(x))

    def test_07_wrong_parent_holds_the_only_copy(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-a", "origin/release/1.0")
        only = [sb.commit("ABC-1001 first try %d" % i) for i in range(2)]
        sb.push("feature/ABC-1001-a")
        sb.branch("feature/ABC-1001-b", "origin/qa")
        sb.commit("ABC-1001 the fix")
        sb.push("feature/ABC-1001-b")
        x = self.track("qa-only")
        self.assertTrue(self.branch(x, "feature/ABC-1001-a")["suggested"])
        oc = [a for a in x["alerts"] if a["kind"] == "only-copy"]
        self.assertEqual(len(oc), 1)
        self.assertEqual(oc[0]["text"], "2 commits exist only on feature/ABC-1001-a, which is superseded.")
        self.assertEqual(sorted(oc[0]["commits"]), sorted(s[:9] for s in only))
        self.assertEqual(len(x["rows"]), 1)

    def test_08_squash_merge_then_revert(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-x", "origin/qa")
        sb.commit("ABC-1001 one")
        sb.commit("ABC-1001 two")
        sb.push("feature/ABC-1001-x")
        sq, h = sb.merge("feature/ABC-1001-x", "qa", squash=True, msg="ABC-1001 the fix (#5)")
        self.prs = [pr(5, "feature/ABC-1001-x", "qa", merge_commit=sq, head=h)]
        x = self.track("qa-only")
        self.assertEqual(self.statuses(x, "qa"), ["squash", "squash"])
        self.assertEqual(x["rows"][0]["cells"]["qa"]["pr"], 5)
        sb.git("checkout", "-q", "qa")
        sb.git("revert", "--no-edit", sq)
        sb.push("qa")
        x = self.track("qa-only")
        self.assertEqual(self.statuses(x, "qa"), ["reverted", "reverted"])

    def test_09_qa_based_branch_with_pr_into_release(self):
        sb = self.sb
        sb.git("checkout", "-q", "qa")
        sb.commit("ABC-1002 qa-only work of someone else", who=OTHER)
        sb.push("qa")
        sb.branch("feature/ABC-1001-x", "origin/qa")
        sb.commit("ABC-1001 the fix")
        sb.push("feature/ABC-1001-x")
        self.prs = [pr(9, "feature/ABC-1001-x", "release/1.0", state="open", head=sb.tip("HEAD"),
                       created="2026-10-09T10:00:00+05:30")]
        x = self.track("release-then-qa")
        a = [a for a in x["alerts"] if a["kind"] == "pr-base"]
        self.assertEqual(len(a), 1)
        self.assertIn("PR #9 goes from a qa-based branch into release/1.0; release-then-qa expects qa.", a[0]["text"])
        self.assertIn("It would carry 2 commits from other work.", a[0]["text"])  # the qa base commit + someone's work
        self.assertEqual(len(a[0]["commits"]), 2)

    def test_10_unpushed_commits(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-x", "origin/qa")
        sb.commit("ABC-1001 one")
        sb.push("feature/ABC-1001-x")
        c2 = sb.commit("ABC-1001 two")
        x = self.track("qa-only")
        b = self.branch(x, "feature/ABC-1001-x")
        self.assertEqual(b["unpushed"], 1)
        a = [a for a in x["alerts"] if a["kind"] == "unpushed"]
        self.assertEqual([(a[0]["text"], a[0]["commits"])], [("1 commit on feature/ABC-1001-x not pushed.", [c2[:9]])])
        self.assertEqual(x["rows"][1]["cells"]["qa"]["why"], "not pushed")

    def test_11_cherry_pick_branch_without_the_key_linked_by_patch_id(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-x", "origin/qa")
        c1 = sb.commit("fix rounding")  # no key in the message either
        sb.push("feature/ABC-1001-x")
        sb.branch("uat-sync-rounding", "origin/uat")
        sb.git("cherry-pick", c1)
        sb.push("uat-sync-rounding")
        x = self.track("qa-cherry-pick")
        b = self.branch(x, "uat-sync-rounding")
        self.assertEqual((b["link"], b["parent"]), ("patch-id", "uat"))
        self.assertEqual(len(x["rows"]), 1)
        self.assertEqual(x["rows"][0]["branches"], ["feature/ABC-1001-x", "uat-sync-rounding"])

    def test_12_remote_branch_deleted_after_merge_keeps_history(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-x", "origin/qa")
        sb.commit("ABC-1001 one")
        sb.push("feature/ABC-1001-x")
        m, h = sb.merge("feature/ABC-1001-x", "qa")
        self.prs = [pr(7, "feature/ABC-1001-x", "qa", merge_commit=m, head=h)]
        x = self.track("qa-only", fetch=True)
        self.assertEqual(self.branch(x, "feature/ABC-1001-x")["status"], "merged")
        sb.git("push", "-q", "origin", "--delete", "feature/ABC-1001-x")
        sb.git("checkout", "-q", "main")
        sb.git("branch", "-q", "-D", "feature/ABC-1001-x")
        sb.git("update-ref", "-d", "refs/remotes/origin/feature/ABC-1001-x")  # as if the user pruned
        from chitragupta import gittrack
        gittrack.RepoData("shop-api").meta.clear()  # fetch is due again
        meta = gittrack.RepoData("shop-api")
        meta.meta["fetched_at"] = "2026-10-01T00:00:00+05:30"
        meta.touch("meta")
        meta.save()
        x = self.track("qa-only", fetch=True)
        b = self.branch(x, "feature/ABC-1001-x")
        self.assertEqual((b["status"], b["local"], b["on_remote"]), ("merged", False, False))
        self.assertEqual(self.statuses(x, "qa"), ["in"])
        self.assertEqual([p["number"] for p in x["prs"]], [7])


class ReadOnly(unittest.TestCase):
    def test_allowlist_refuses_mutating_calls(self):
        from chitragupta import gitro
        bad = [["checkout", "x"], ["switch", "x"], ["commit", "-m", "x"], ["merge", "x"], ["rebase", "x"],
               ["cherry-pick", "x"], ["reset", "--hard"], ["stash"], ["pull"], ["push"], ["tag", "v1"],
               ["branch", "-D", "x"], ["branch", "x"], ["branch", "--list", "-d", "x"], ["show", "HEAD"],
               ["show", "-s", "-p", "HEAD"], ["config", "user.name", "x"], ["remote", "add", "x", "y"],
               ["fetch", "origin"], ["fetch", "--no-tags", "--prune", "origin", "+refs/heads/a:refs/remotes/origin/a"],
               ["fetch", "--no-tags", "origin", "+refs/heads/a:refs/heads/a"],
               ["fetch", "--no-tags", "origin", "+refs/heads/a:refs/remotes/other/a"], ["reflog", "expire", "--all"],
               ["gc"], ["update-ref", "-d", "x"], ["worktree", "add", "x"], ["clean", "-fd"], ["restore", "."]]
        for args in bad:
            with self.assertRaises(gitro.NotAllowed, msg=" ".join(args)):
                gitro.check(args)
        good = [["log", "-g", "HEAD"], ["show", "-s", "--format=%H", "HEAD"], ["branch", "--list"],
                ["branch", "-a", "--contains", "abc"], ["config", "--get", "remote.origin.url"], ["remote", "get-url", "origin"],
                ["fetch", "--no-tags", "--quiet", "--no-auto-gc", "--no-auto-maintenance", "origin",
                 "+refs/heads/qa:refs/remotes/origin/qa", "+refs/heads/release/1.0:refs/remotes/origin/release/1.0"],
                ["ls-remote", "--heads", "origin"], ["patch-id", "--stable"], ["merge-base", "--is-ancestor", "a", "b"]]
        for args in good:
            self.assertTrue(gitro.check(args))

    def test_no_mutating_git_call_in_the_code(self):
        """Work repos are only reached through gitro. The data home's own repo (gitsync), the data home pre-commit
        check (redact) and setup's own sender folder are the exceptions."""
        exempt = {"gitsync.py", "redact.py", "setup.py", "gitro.py", "common.py"}
        forbidden = {"checkout", "switch", "commit", "merge", "rebase", "cherry-pick", "reset", "stash", "pull", "push",
                     "tag", "worktree", "clean", "restore", "update-ref", "gc", "prune", "am", "apply", "revert"}
        problems = []
        pkg = os.path.join(ROOT, "chitragupta")
        for dp, _, fns in os.walk(pkg):
            for fn in fns:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dp, fn)
                src = open(path, encoding="utf-8").read()
                rel = os.path.relpath(path, pkg)
                if rel.startswith("safety" + os.sep):
                    continue  # the command guard itself (allowlists and red-team samples)
                if fn not in exempt and "git_exe(" in src:
                    problems.append("%s runs git directly (use gitro)" % rel)
                for node in ast.walk(ast.parse(src)):
                    if not isinstance(node, ast.Call):
                        continue
                    for arg in node.args:
                        if isinstance(arg, ast.BinOp):
                            arg = arg.right if isinstance(arg.right, ast.List) else arg.left
                        if isinstance(arg, ast.List) and arg.elts and isinstance(arg.elts[0], ast.Constant) \
                                and arg.elts[0].value in forbidden and fn not in exempt:
                            problems.append("%s:%d git %s" % (rel, node.lineno, arg.elts[0].value))
        self.assertEqual(problems, [])

    def test_fetch_never_runs_while_git_holds_a_lock(self):
        from chitragupta import gitro
        d = tempfile.mkdtemp(prefix="cg-lock-")
        try:
            subprocess.run(["git", "init", "-q", d], check=True, capture_output=True)
            self.assertFalse(gitro.locked(d))
            open(os.path.join(d, ".git", "index.lock"), "w").close()
            self.assertTrue(gitro.locked(d))
            ok, msg = gitro.fetch({}, d, "origin", ["qa"])
            self.assertEqual((ok, msg), (False, "skipped: a git lock file exists"))
        finally:
            rmtree(d)


class Parsing(unittest.TestCase):
    def test_session_commands(self):
        from chitragupta import gittrack as g
        self.assertEqual(g.parse_command("git checkout -b feature/ABC-1-x origin/qa && git push -u origin feature/ABC-1-x"),
                         [{"kind": "create", "branch": "feature/ABC-1-x", "from": "origin/qa"},
                          {"kind": "push", "remote": "origin", "branch": "feature/ABC-1-x"}])
        self.assertEqual(g.parse_command('git -C "C:\\src\\shop api" switch -c fix/ABC-2 release/1.0'),
                         [{"kind": "create", "branch": "fix/ABC-2", "from": "release/1.0"}])
        self.assertEqual(g.parse_command("git branch fix/ABC-3 qa"), [{"kind": "create", "branch": "fix/ABC-3", "from": "qa"}])
        self.assertEqual(g.parse_command("git branch -D old"), [])
        self.assertEqual(g.parse_command("gh pr create --base qa --title x"),
                         [{"kind": "pr-create", "tool": "gh", "base": "qa", "head": None}])
        self.assertEqual(g.parse_command("az repos pr create --target-branch refs/heads/qa --source-branch fix/ABC-4")[0]["base"], "qa")
        self.assertEqual(g.parse_command("glab mr create -b uat")[0]["base"], "uat")
        self.assertEqual(g.parse_command("git cherry-pick -x abc123 def456"), [{"kind": "cherry-pick", "commits": ["abc123", "def456"]}])
        self.assertEqual(g.pr_urls("Created https://github.com/acme/shop/pull/12 ok"), ["https://github.com/acme/shop/pull/12"])
        self.assertEqual(g.pr_urls("https://dev.azure.com/acme/shop/_git/api/pullrequest/77"),
                         ["https://dev.azure.com/acme/shop/_git/api/pullrequest/77"])

    def test_host_detection_and_windows_paths(self):
        from chitragupta import gittrack, hosts
        self.assertEqual(hosts.detect("git@github.com:acme/shop.git"), {"host": "github", "path": "acme/shop"})
        self.assertEqual(hosts.detect("https://user:tok@github.com/acme/shop"), {"host": "github", "path": "acme/shop"})
        self.assertEqual(hosts.detect("git@ssh.dev.azure.com:v3/acme/Shop/shop-api"),
                         {"host": "azure", "org": "acme", "project": "Shop", "repo": "shop-api"})
        self.assertEqual(hosts.detect("acme@vs-ssh.visualstudio.com:v3/acme/Acme%20Shop/shop-api")["project"], "Acme Shop")
        self.assertEqual(hosts.detect("https://acme@dev.azure.com/acme/Shop/_git/shop-api")["repo"], "shop-api")
        self.assertEqual(hosts.detect("https://gitlab.com/acme/group/shop.git"), {"host": "gitlab", "path": "acme/group/shop"})
        self.assertEqual(gittrack.repo_key("C:\\src\\shop-api\\"), "shop-api")
        self.assertEqual(gittrack.repo_key("C:/src/shop api"), "shop_api")
        self.assertEqual(gittrack.repo_key("/home/u/src/shop-api"), "shop-api")

    def test_merge_commit_messages(self):
        from chitragupta import hosts
        self.assertEqual(hosts.MERGE_RX[0].match("Merged PR 812: fix(ABC-1): x").groups(), ("812", "fix(ABC-1): x"))
        m = hosts.MERGE_RX[1].match("Merge pull request #12 from acme/feature/ABC-1-x")
        self.assertEqual((m.group(1), m.group(2)), ("12", "feature/ABC-1-x"))

    def test_inbox_grammar(self):
        from chitragupta import gittrack as g
        self.assertEqual(g.parse_directive("branch-link: ABC-1001 shop-api uat-sync"),
                         {"kind": "branch-link", "ticket": "ABC-1001", "repo": "shop-api", "branch": "uat-sync"})
        self.assertEqual(g.parse_directive("- branch-unlink: abc-1001 shop-api x")["ticket"], "ABC-1001")
        self.assertEqual(g.parse_directive("branch-abandon: shop-api feature/ABC-1001-a"),
                         {"kind": "branch-abandon", "repo": "shop-api", "branch": "feature/ABC-1001-a"})
        self.assertEqual(g.parse_directive("branch-restore: shop-api x")["kind"], "branch-restore")
        self.assertEqual(g.parse_directive("strategy: shop-api qa-only"),
                         {"kind": "strategy", "repo": "shop-api", "strategy": "qa-only"})
        self.assertEqual(g.parse_directive("strategy: ABC-1001 release-then-qa"),
                         {"kind": "ticket-strategy", "ticket": "ABC-1001", "strategy": "release-then-qa"})
        self.assertEqual(g.parse_directive("branch-link: too few")["kind"], "invalid")
        self.assertIsNone(g.parse_directive("time: ABC-1001 1h"))


class Migration(TrackingCase):
    def test_branch_tracking_migration_backs_up_and_seeds(self):
        from chitragupta import common, gittrack, migrate
        sb = self.sb
        sb.branch("feature/ABC-1001-x", "origin/qa")
        sb.commit("ABC-1001 one")
        cfg = self.cfg("qa-only")
        cfg.pop("branching")
        cfg["branch_fetch"] = True
        cfg["chitragupta_migrations"] = ["database-mcp-rename"]
        os.makedirs(common.wl(), exist_ok=True)
        common.write_json(common.wl("config.json"), cfg)
        common.write_json(common.hp("data", "meta.json"), {"schema_version": 3})
        legacy = {"shop-api": {"feature/ABC-1001-x": {"branch": "feature/ABC-1001-x", "parent": "qa", "parent_ref": "origin/qa",
                                                      "parent_source": "reflog (HEAD)", "created_at": "2026-09-01T10:00:00+05:30",
                                                      "ticket": T, "envs": {"qa": {"status": "not merged"}}, "v": 2}}}
        common.write_json(common.hp("data", "branches.json"), legacy)
        dry = migrate.run(dry_run=True)
        self.assertEqual(dry["applied"], ["branch-tracking", "inbox-counts"])
        self.assertFalse(os.path.exists(gittrack.gdir("shop-api", "branches.json")))
        res = migrate.run(wait_s=0)
        self.assertTrue(os.path.isfile(os.path.join(res["backup"], "branches.json")))
        self.assertTrue(os.path.isfile(os.path.join(res["backup"], "meta.json")))
        rec = common.read_json(gittrack.gdir("shop-api", "branches.json"))["feature/ABC-1001-x"]
        self.assertEqual((rec["parent"], rec["parent_source"], rec["created_at"]), ("qa", "reflog", "2026-09-01T10:00:00+05:30"))
        self.assertNotIn("envs", rec)
        evs = gittrack.RepoData("shop-api").events()
        self.assertTrue(any(e["kind"] == "create" and e.get("branch") == "feature/ABC-1001-x" for e in evs))
        newcfg = common.read_json(common.wl("config.json"))
        self.assertEqual(newcfg["branching"]["guard_mode"], "warn")
        self.assertEqual(newcfg["chitragupta_migrations"], ["database-mcp-rename", "branch-tracking", "inbox-counts"])
        self.assertEqual(common.read_json(common.hp("data", "meta.json"))["schema_version"], 4)
        self.assertEqual(common.read_json(common.hp("data", "branches.json")), legacy)  # the old file is left as it was
        self.assertTrue(migrate.run(wait_s=0)["already"])
        # the kept parent survives the first tick even though the reflog would infer the same
        x = self.track("qa-only")
        self.assertEqual(self.branch(x, "feature/ABC-1001-x")["created_at"], "2026-09-01T10:00:00+05:30")


class Manual(TrackingCase):
    def test_link_unlink_abandon_restore_and_strategy(self):
        from chitragupta import gittrack
        from chitragupta.store import Store
        sb = self.sb
        sb.branch("feature/ABC-1001-a", "origin/release/1.0")
        sb.commit("ABC-1001 try")
        sb.branch("misc-work", "origin/qa")
        sb.commit("unrelated")
        x = self.track("qa-only")
        self.assertNotIn("misc-work", [b["branch"] for b in x["branches"]])
        st = Store()
        cfg = self.cfg("qa-only")
        for line in ("branch-link: ABC-1001 shop-api misc-work", "branch-abandon: shop-api feature/ABC-1001-a"):
            ok, _ = gittrack.apply_directive(gittrack.parse_directive(line), st, cfg)
            self.assertTrue(ok)
        st.save()
        x = self.track("qa-only")
        self.assertEqual(self.branch(x, "misc-work")["link"], "manual")
        self.assertEqual(self.branch(x, "feature/ABC-1001-a")["status"], "abandoned")
        ok, _ = gittrack.apply_directive(gittrack.parse_directive("strategy: ABC-1001 release-then-qa"), st, cfg)
        st.save()
        x = self.track("qa-only")
        self.assertEqual((x["strategy"], x["strategy_how"]), ("release-then-qa", "set for this ticket"))
        ok, _ = gittrack.apply_directive(gittrack.parse_directive("branch-restore: shop-api feature/ABC-1001-a"), st, cfg)
        gittrack.apply_directive(gittrack.parse_directive("branch-unlink: ABC-1001 shop-api misc-work"), st, cfg)
        x = self.track("qa-only")
        self.assertNotEqual(self.branch(x, "feature/ABC-1001-a")["status"], "abandoned")
        self.assertNotIn("misc-work", [b["branch"] for b in x["branches"]])

    def test_ignored_ticket_is_not_tracked(self):
        from chitragupta import gitro
        sb = self.sb
        sb.branch("feature/ABC-1001-x", "origin/qa")
        sb.commit("ABC-1001 one")
        x = self.track("qa-only")
        self.assertEqual(len(x["branches"]), 1)
        seen = []
        real = gitro.run

        def spy(cfg, repo, args, **kw):
            seen.append(" ".join(args))
            return real(cfg, repo, args, **kw)
        gitro.run = spy
        try:
            from chitragupta import delivery, gittrack
            from chitragupta.store import Store
            cfg = self.cfg("qa-only")
            cfg["branching"]["ignore_tickets"] = [T]
            st = Store()
            rx = gittrack.Tracker(cfg, {}, st, now=NOW).refresh(fetch=False)
            delivery.update_ticket(st, T, rx, cfg, now=NOW, force=True)
        finally:
            gitro.run = real
        self.assertNotIn("feature/ABC-1001-x", gittrack.RepoData("shop-api").branches)
        self.assertNotIn("git", st.ticket(T))
        self.assertFalse([c for c in seen if "ABC-1001" in c], "no git call names the ignored ticket's branch")

    def test_strategy_rule_by_jira_parent(self):
        sb = self.sb
        sb.branch("feature/ABC-1001-x", "origin/qa")
        sb.commit("ABC-1001 one")
        with open(os.path.join(self.home, "data", "tickets", T + ".json"), "w", encoding="utf-8") as f:
            json.dump({"id": T, "last": "2026-10-10", "jira": {"parent": "OPS-77"}}, f)
        x = self.track("release-then-qa", rules=[{"jira_parent": "OPS-*", "strategy": "qa-only"}])
        self.assertEqual((x["strategy"], x["strategy_how"]), ("qa-only", "Jira parent OPS-77"))
        self.assertNotIn("wrong-parent", self.kinds(x))


if __name__ == "__main__":
    unittest.main()
