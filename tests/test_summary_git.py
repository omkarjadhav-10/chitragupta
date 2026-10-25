"""Summary change evidence groups identical patches and excludes stash snapshots."""
import datetime as dt
import os
import unittest
from unittest import mock
from tests.test_brief import BriefBase, T, SHA, ticket, good_output, c


class LogicalChanges(BriefBase, unittest.TestCase):
    def test_cherry_picked_copies_are_one_change_with_citation_aliases(self):
        from chitragupta import brief
        tk = ticket()
        copysha = "b" * 40
        tk["commits"][copysha] = dict(tk["commits"][SHA], sha=copysha, ref="qa")
        tk["git"]["repos"]["shop-api"]["rows"][0]["shas"] = [SHA, copysha]
        tk["commits"]["c"*40] = {"sha": "c"*40, "subject": "index on feature/ABC-1001: snapshot", "ref": "refs/stash"}
        commits = brief._commits(tk)
        self.assertEqual(len(commits), 1)
        self.assertIn(copysha[:7], commits[0]["aliases"])
        self.assertEqual(len([r for r in brief.fixes_made(tk) if r["kind"] == "commit"]), 1)
        output = good_output()
        output["fixes_made"]["summary"] = c("The commit releases stock on cancel.", "commit:" + copysha[:7])
        sections, dropped = brief.validate(output, T, tk, "bug", {"found": True})
        self.assertIsNotNone(sections["fixes_made"]["summary"])

    def test_commit_scan_excludes_stash_ref_and_snapshot_subjects(self):
        from chitragupta import sources
        repo = os.path.join(self.home, "shop-api")
        os.makedirs(os.path.join(repo, ".git"))
        cfg = dict(self.cfg(), watched_repos=[repo], git_author_emails=["test@acme.test"])
        def row(sha, ref, subject):
            return sources._SEP_REC + sources._SEP_FLD.join([sha, "2026-09-01T10:00:00Z", ref, subject, "", "M\tsrc/Cancel.java"])
        output = row("a"*40,"refs/heads/feature/ABC-1001", "ABC-1001 release stock") + row("b"*40,"refs/stash", "ABC-1001 snapshot") + row("c"*40,"refs/heads/feature/ABC-1001", "WIP on feature/ABC-1001: temporary")
        with mock.patch.object(sources.gitro, "run", return_value=(0, output, "")) as run:
            got = sources.scan_commits(cfg, {}, dt.datetime(2026,9,1, tzinfo=dt.timezone.utc))
        self.assertEqual([c["sha"] for c in got], ["a"*40])
        args = run.call_args.args[2]
        self.assertLess(args.index("--exclude=refs/stash"), args.index("--all"))
