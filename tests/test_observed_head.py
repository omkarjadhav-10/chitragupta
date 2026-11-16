"""The HEAD observed after a test run: the `.git` file resolver, the simple-command filter, the hook outcome and
the test record. `.git` layouts are built as plain files in a temp folder; no Git and no process is involved.
"""
import os
import time
import types
import unittest
from unittest import mock

from chitragupta import activity, common, hook, testruns
from chitragupta.redact import redact
from chitragupta.safety import commands
from tests.support import Isolated

A, B = "a" * 40, "b" * 40
LONG = "c" * 64
DAY, SID = "2026-09-01", "abcdef1234567890"
PASSED = {"stdout": "=== 3 passed in 0.5s ===", "stderr": "", "interrupted": False}


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(text if isinstance(text, bytes) else text.encode("utf-8"))


class Layouts(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.repo = os.path.join(self.home, "shop")
        self.git = os.path.join(self.repo, ".git")
        os.makedirs(self.git)
        self.started = []

        def no_process(*args, **kwargs):
            self.started.append(args[:1])
            raise AssertionError("reading HEAD started a process")
        for name in ("run", "spawn"):
            trip = mock.patch.object(commands, name, no_process)
            trip.start()
            self.addCleanup(trip.stop)
        self.addCleanup(lambda: self.assertEqual(self.started, []))

    def branch(self, name="main", sha=A, packed=False, git=None):
        git = git or self.git
        put(os.path.join(git, "HEAD"), "ref: refs/heads/%s\n" % name)
        if packed:
            put(os.path.join(git, "packed-refs"),
                "# pack-refs with: peeled fully-peeled sorted \n%s refs/heads/other\n%s refs/heads/%s\n^%s\n"
                % (B, sha, name, B))
        else:
            put(os.path.join(git, "refs", "heads", *name.split("/")), sha + "\n")

    def worktree(self, head="ref: refs/heads/feature/ABC-1001\n"):
        """A linked worktree of self.repo: its `.git` is a file, its HEAD is its own, its refs are shared."""
        tree = os.path.join(self.home, "shop-wt")
        own = os.path.join(self.git, "worktrees", "shop-wt")
        put(os.path.join(tree, ".git"), "gitdir: %s\n" % own)
        put(os.path.join(own, "HEAD"), head)
        put(os.path.join(own, "commondir"), "../..\n")
        return tree, own


class Resolver(Layouts):
    def test_a_branch_with_a_loose_ref(self):
        self.branch()
        self.assertEqual(common.read_head(self.repo), A)
        self.branch("feature/ABC-1001-order-filter", B)
        self.assertEqual(common.read_head(self.repo), B)

    def test_a_detached_head(self):
        put(os.path.join(self.git, "HEAD"), A + "\n")
        self.assertEqual(common.read_head(self.repo), A)
        put(os.path.join(self.git, "HEAD"), LONG + "\n")   # a sha-256 repository
        self.assertEqual(common.read_head(self.repo), LONG)

    def test_packed_refs(self):
        self.branch(packed=True)
        self.assertEqual(common.read_head(self.repo), A)
        put(os.path.join(self.git, "refs", "heads", "main"), B + "\n")   # a loose ref is newer than the packed one
        self.assertEqual(common.read_head(self.repo), B)

    def test_a_linked_worktree_with_shared_packed_refs(self):
        self.branch()   # the main checkout is on main at A
        tree, own = self.worktree()
        put(os.path.join(self.git, "packed-refs"), "%s refs/heads/feature/ABC-1001\n" % B)
        self.assertEqual(common.read_head(tree), B)
        self.assertEqual(common.read_head(self.repo), A)   # each location answers for itself
        put(os.path.join(self.git, "refs", "heads", "feature", "ABC-1001"), LONG + "\n")
        self.assertEqual(common.read_head(tree), LONG)      # a shared loose ref
        put(os.path.join(own, "HEAD"), A + "\n")
        self.assertEqual(common.read_head(tree), A)         # detached in the worktree

    def test_a_relative_gitdir_file(self):
        self.branch()
        nested = os.path.join(self.repo, "vendor", "kit")
        put(os.path.join(nested, ".git"), "gitdir: ../../.git/modules/kit\n")
        self.branch(sha=B, git=os.path.join(self.git, "modules", "kit"))
        self.assertEqual(common.read_head(nested), B)

    def test_head_that_moves_during_the_read_is_dropped(self):
        self.branch()
        real = common._resolve_ref

        def moving(git_dir, common_dir, ref):
            sha = real(git_dir, common_dir, ref)
            put(os.path.join(self.git, "HEAD"), B + "\n")   # something checked out another commit meanwhile
            return sha
        with mock.patch.object(common, "_resolve_ref", moving):
            self.assertIsNone(common.read_head(self.repo))
        self.assertEqual(common.read_head(self.repo), B)


class Refused(Layouts):
    def none(self, root=None):
        self.assertIsNone(common.read_head(root or self.repo))

    def test_ref_names_that_are_not_plain_refs(self):
        put(os.path.join(self.home, "outside"), A + "\n")
        put(os.path.join(self.git, "refs", "heads", "main"), A + "\n")
        for ref in ("refs/../../outside", "refs/heads/../../../outside", "refs\\heads\\main", "/etc/passwd",
                    "heads/main", "refs/heads/ma\x01in", "refs/heads/main.lock", "refs/heads/.invalid",
                    "refs/heads//main", "refs/heads/main/", "refs/heads/ma in", "refs/heads/m~1", "refs/heads/m:n",
                    os.path.join(self.home, "outside")):
            put(os.path.join(self.git, "HEAD"), "ref: %s\n" % ref)
            self.none()
        put(os.path.join(self.git, "HEAD"), "ref: refs/heads/main\n")
        self.assertEqual(common.read_head(self.repo), A)

    def test_wrong_hash_shapes(self):
        for value in (A[:39], A + "0", "g" * 40, A.upper(), "", "ref: refs/heads/next", A + " " + B):
            self.branch(sha=value)
            self.none()
            put(os.path.join(self.git, "HEAD"), value + "\n")
            self.none()

    def test_missing_files(self):
        self.none()                                         # an empty .git directory
        self.branch()
        os.remove(os.path.join(self.git, "refs", "heads", "main"))
        self.none()                                         # the ref is nowhere
        for root in (None, "", 7, os.path.join(self.home, "absent"), self.home):
            self.assertIsNone(common.read_head(root))

    def test_oversized_and_unreadable_files(self):
        self.branch()
        put(os.path.join(self.git, "HEAD"), "ref: refs/heads/main\n" + " " * 5000)
        self.none()
        self.branch()
        put(os.path.join(self.git, "refs", "heads", "main"), A + "\n" * 5000)
        self.none()                                         # an oversized loose ref does not fall back to packed
        self.branch()
        put(os.path.join(self.git, "HEAD"), b"\xff\xfe\x00ref")
        self.none()
        os.remove(os.path.join(self.git, "refs", "heads", "main"))
        self.branch(packed=True)
        self.assertEqual(common.read_head(self.repo), A)
        with mock.patch.object(common, "PACKED_REFS_MAX", 64):
            self.none()                                     # packed-refs over its bound: unknown
        put(os.path.join(self.git, "packed-refs"), "%s refs/heads/main\n" % A + "#" * (common.PACKED_REFS_MAX + 1))
        self.none()

    def test_malformed_worktree_files(self):
        self.branch()
        tree, own = self.worktree()
        put(os.path.join(self.git, "refs", "heads", "feature", "ABC-1001"), B + "\n")
        self.assertEqual(common.read_head(tree), B)
        for text in ("", "\n", "../../missing\n", "bad\x00dir\n"):
            put(os.path.join(own, "commondir"), text)
            self.none(tree)
        put(os.path.join(own, "commondir"), "../..\n")
        for text in ("", "gitdir:\n", "gitdir: %s\n" % os.path.join(self.home, "absent"), "not a git file\n",
                     "gitdir: bad\x01dir\n", "x" * 5000):
            put(os.path.join(tree, ".git"), text)
            self.none(tree)

    def test_a_symbolic_or_directory_ref(self):
        self.branch()
        put(os.path.join(self.git, "refs", "heads", "main"), "ref: refs/heads/other\n")
        self.none()
        os.remove(os.path.join(self.git, "refs", "heads", "main"))
        os.makedirs(os.path.join(self.git, "refs", "heads", "main"))   # `main` is a namespace, not a branch
        self.none()

    def test_nothing_is_ever_raised(self):
        self.branch()
        for target in ("_read_small", "_resolve_ref", "_ref_ok"):
            with mock.patch.object(common, target, side_effect=RuntimeError("boom")):
                self.assertIsNone(common.read_head(self.repo))

    def test_cost_of_200_reads(self):
        self.branch(packed=True)
        put(os.path.join(self.git, "packed-refs"),
            "".join("%040x refs/heads/b%d\n" % (i, i) for i in range(2000)) + "%s refs/heads/main\n" % A)
        started = time.perf_counter()
        for _ in range(200):
            self.assertEqual(common.read_head(self.repo), A)
        spent = time.perf_counter() - started
        print("\nread_head: 200 reads with 2,000 packed refs in %.1f ms (%.2f ms each)" % (spent * 1000, spent * 5))
        self.assertLess(spent, 5.0)


class SimpleCommand(unittest.TestCase):
    def test_plain_runner_commands_are_simple(self):
        for cmd in ("pytest", "pytest tests/test_orders.py -q", "python -m unittest discover -s tests -t .",
                    "py -3 -m pytest -c pytest.ini", "npm test", "go test ./...", "mvn -q test",
                    "pytest tests/test_git_commit.py", "pytest tests/checkout/test_cd.py && echo done",
                    "git status && pytest", "git log -1; pytest", "pytest --cov=shop -k 'merge or reset'",
                    "dotnet test Shop.sln"):
            self.assertTrue(testruns.simple_command(cmd), cmd)

    def test_a_directory_change_is_not_simple(self):
        for cmd in ("cd other; pytest", "cd other && pytest", "pushd api; npm test; popd", "(cd api && go test ./...)",
                    "Set-Location api; pytest", "set-location api; pytest", "pytest -C dir", "make -C build test",
                    "git -C ../other status; pytest", "git --git-dir=../x/.git log; pytest",
                    "git --work-tree ../x status && pytest", "chdir api & pytest", "pytest;cd ..", "cd"):
            self.assertFalse(testruns.simple_command(cmd), cmd)

    def test_anything_that_can_move_head_is_not_simple(self):
        for verb in ("checkout", "switch", "reset", "rebase", "merge", "pull", "commit", "cherry-pick", "revert", "stash",
                     "worktree", "bisect", "am"):
            for cmd in ("git %s x && pytest" % verb, "pytest; git %s" % verb, "git.exe %s x; pytest" % verb,
                        "git -c core.editor=true %s --continue && pytest" % verb, "GIT %s x; pytest" % verb.upper()):
                self.assertFalse(testruns.simple_command(cmd), cmd)

    def test_empty(self):
        for cmd in ("", "   ", None):
            self.assertFalse(testruns.simple_command(cmd))


class HookOutcome(Layouts):
    def outcome(self, cmd, root="repo"):
        return hook._outcome("Bash", {"command": cmd}, PASSED, self.repo, redact, self.repo if root == "repo" else root)

    def test_a_simple_test_command_records_the_head(self):
        self.branch()
        self.assertEqual(self.outcome("pytest tests"), {"test": {"status": "passed", "counts": {"passed": 3}, "head": A}})
        self.assertEqual(hook._outcome("PowerShell", {"command": "python -m unittest"}, PASSED, self.repo, redact,
                                       self.repo)["test"]["head"], A)

    def test_commands_that_change_directory_or_move_head_record_none(self):
        self.branch()
        for cmd in ("cd other; pytest", "git checkout x && pytest", "pytest -C dir"):
            self.assertEqual(self.outcome(cmd), {"test": {"status": "passed", "counts": {"passed": 3}}}, cmd)

    def test_no_repository_no_head(self):
        self.branch()
        for root in (None, "", os.path.join(self.home, "absent")):
            self.assertNotIn("head", self.outcome("pytest tests", root)["test"])
        self.assertEqual(hook._outcome("Bash", {"command": "pytest tests"}, PASSED, self.repo, redact),
                         {"test": {"status": "passed", "counts": {"passed": 3}}})   # the older call form
        self.assertEqual(self.outcome("git status"), {})

    def test_the_hook_path_raises_nothing_on_a_malformed_layout(self):
        layouts = (lambda: put(os.path.join(self.git, "HEAD"), b"\xff\xfe"),
                   lambda: put(os.path.join(self.git, "HEAD"), "ref: refs/../../x\n"),
                   lambda: put(os.path.join(self.git, "HEAD"), "ref: refs/heads/gone\n"),
                   lambda: put(os.path.join(self.git, "HEAD"), "x" * 9000),
                   lambda: os.remove(os.path.join(self.git, "HEAD")))
        for build in layouts:
            build()
            self.assertEqual(self.outcome("pytest tests"), {"test": {"status": "passed", "counts": {"passed": 3}}})
        for target, where in (("read_head", common), ("simple_command", testruns)):
            with mock.patch.object(where, target, side_effect=RuntimeError("boom")):
                self.assertEqual(self.outcome("pytest tests"), {"test": {"status": "passed", "counts": {"passed": 3}}})


class Record(Layouts):
    def record(self, outcome, repo_root="repo", ts="2026-09-01T10:05:00+05:30"):
        from chitragupta import store
        st = store.Store()
        ev = {"ts": ts, "event": "post-tool", "_ln": 9, "repo_root": self.repo if repo_root == "repo" else repo_root,
              "payload": {"tool": "Bash", "input": {"command": "pytest tests/test_orders.py"},
                          "outcome": {"test": outcome}}}
        return testruns.record(st, "ABC-1001", ev, DAY, SID), st

    def test_the_observed_head_is_stored_with_its_basis(self):
        rec, _ = self.record({"status": "passed", "counts": {"passed": 3}, "head": A})
        self.assertEqual(rec["context"], {"head": A, "location": os.path.realpath(self.repo)})
        self.assertEqual(rec["head_basis"], "observed after run")
        self.assertEqual((rec["status"], rec["counts"]), ("passed", {"passed": 3}))
        for name in ("worktree", "config", "environment"):   # still unknown: such a run never verifies alone
            self.assertNotIn(name, rec["context"])

    def test_repo_id_only_from_a_ledger_of_that_location(self):
        location = os.path.realpath(self.repo)
        common.write_json(activity.ledger_path(activity.ledger_key(location)),
                          {"v": activity.V, "repo": {"id": "github:github.com/acme/shop", "path": location}})
        rec, _ = self.record({"status": "passed", "head": A})
        self.assertEqual(rec["context"], {"head": A, "location": location, "repo_id": "github:github.com/acme/shop"})
        with mock.patch.object(activity, "load", side_effect=RuntimeError("boom")):
            rec, _ = self.record({"status": "passed", "head": A}, ts="2026-09-01T10:06:00+05:30")
        self.assertEqual(rec["context"], {"head": A, "location": location})

    def test_no_head_no_context(self):
        for outcome, root in (({"status": "passed"}, "repo"), ({"status": "passed", "head": A}, None),
                              ({"status": "passed", "head": A}, ""), ({"status": "passed", "head": "main"}, "repo"),
                              ({"status": "passed", "head": A[:12]}, "repo"), ({"status": "passed", "head": 7}, "repo")):
            rec, _ = self.record(outcome, root)
            self.assertNotIn("context", rec)
            self.assertNotIn("head_basis", rec)
            self.assertEqual(set(rec), {"id", "at", "day", "session", "command", "status", "ref"})   # the old shape

    def test_older_records_without_context_still_load(self):
        from chitragupta import companionevidence, features, store
        st = store.Store()
        old = {"id": "t1", "at": "2026-08-30T09:00:00+05:30", "day": "2026-08-30", "session": "abcdef12",
               "command": "pytest", "status": "passed"}
        st.ticket("ABC-1001")["tests"]["t1"] = old
        ag = types.SimpleNamespace(store=st, cfg={"ticket_prefixes": ["ABC"]}, state={},
                                   ensure_tslot=lambda day, t, repo=None, branch=None: st.day(day)["tickets"].setdefault(t, {}))
        ev = {"ts": "2026-09-01T10:05:00+05:30", "event": "post-tool", "_t": "ABC-1001", "_src": "prompt", "_ln": 9,
              "repo_root": self.repo,
              "payload": {"tool": "Bash", "input": {"command": "pytest tests"},
                          "outcome": {"test": {"status": "passed", "head": A}}}}
        features.Features(ag).on_events(DAY, SID, {}, [ev])
        tests = st.ticket("ABC-1001")["tests"]
        self.assertEqual(tests["t1"], old)
        new = [t for k, t in tests.items() if k != "t1"][0]
        self.assertIsNone(companionevidence.captured_context(old.get("context")))
        self.assertEqual(companionevidence.captured_context(new["context"]),
                         {"head": A, "location": os.path.realpath(self.repo)})


if __name__ == "__main__":
    unittest.main()
