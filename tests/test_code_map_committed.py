"""Gate 2: the code map tells committed files from files only edited in a session.

Files an abandoned attempt edited were shown as "Changed: yes" next to the files the merged commit changed. Now a
file changed only by Edit/Write events that no commit of the ticket contains reads "edited, not committed"; committed
ones stay "yes". Commit file lists come from the commit scan (recorded on the commit) or, for older commits, from the
read-only `git log --no-walk --name-only` the pathcheck step runs. Synthetic data only (ABC-1001, shop-api).
"""
import os
import unittest

from tests.support import PY, Isolated, rmtree, write_config
from tests.test_kb_pathcheck import T, git, make_repo


def rec(path, by="edit", **kw):
    d = {"path": path, "role": "", "changed": True, "verified": True, "verified_by": by, "repo": "shop-api"}
    d.update(kw)
    return d


def row(md, path):
    return next(line for line in md if "`%s`" % path in line)


class Synthetic(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "paths": {"python": PY}})

    def test_edit_only_files_no_commit_contains(self):
        from chitragupta import render
        tk = {"files": {p: rec(p) for p in ("src/Kept.java", "src/Dropped.java")},
              "commits": {"a" * 40: {"sha": "a" * 40, "repo_name": "shop-api", "files": ["src/Kept.java", "src/Other.java"]}}}
        tk["files"]["src/Other.java"] = rec("src/Other.java", by="commit")
        md = render.code_map(tk)
        self.assertTrue(row(md, "src/Kept.java").endswith("| yes |"))
        self.assertTrue(row(md, "src/Other.java").endswith("| yes |"))
        self.assertTrue(row(md, "src/Dropped.java").endswith("| edited, not committed |"))

    def test_no_commits_at_all(self):
        from chitragupta import render
        md = render.code_map({"files": {"src/A.java": rec("src/A.java")}, "commits": {}})
        self.assertTrue(row(md, "src/A.java").endswith("| edited, not committed |"))


class FromGit(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.repo = make_repo()
        self.addCleanup(rmtree, os.path.dirname(self.repo))
        self.cfg = {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY},
                    "watched_repos": [self.repo]}
        write_config(self.home, self.cfg)
        from chitragupta import common
        common.load_config()

    def test_pathcheck_reads_older_commits_files(self):
        from chitragupta import pathcheck, render, store
        sha = git(self.repo, "rev-parse", "feature/%s-cancel" % T)  # its commit adds src/Branch.java
        st = store.Store()
        tk = st.ticket(T)
        tk["repos"] = ["shop-api"]
        tk["commits"] = {sha: {"sha": sha, "short": sha[:8], "repo_name": "shop-api", "subject": "%s branch file" % T}}
        tk["files"] = {p: rec(p, repo_root=self.repo) for p in ("src/Branch.java", "src/Real.java")}
        st.touch_ticket(T)
        pathcheck.run(st, self.cfg)
        f = st.ticket(T)["files"]
        self.assertEqual((f["src/Branch.java"]["committed"], f["src/Real.java"]["committed"]), (True, False))
        md = render.code_map(st.ticket(T))
        self.assertTrue(row(md, "src/Branch.java").endswith("| yes |"))
        self.assertTrue(row(md, "src/Real.java").endswith("| edited, not committed |"))

    def test_the_commit_scan_records_its_files(self):
        from chitragupta import agent, sources
        from unittest import mock
        commit = {"sha": "b" * 40, "short": "b" * 8, "repo": self.repo, "repo_name": "shop-api",
                  "date": "2026-09-01T10:00:00+05:30", "ref": "feature/%s-cancel" % T, "subject": "%s fix" % T,
                  "ticket": T, "files": [{"status": "M", "path": "src/Real.java"}]}
        with mock.patch.object(sources, "scan_commits", lambda cfg, st, since: [commit]), \
                mock.patch.object(sources, "scan_bruno", lambda cfg, st: []), \
                mock.patch.object(sources, "scan_inbox", lambda cfg, st: []), \
                mock.patch.object(sources, "scan_sql", lambda cfg, st: []), \
                mock.patch.object(agent.Agent, "run_branches", lambda self: None):
            a = agent.Agent()
            a.run_sources()
        tk = a.store.ticket(T)
        self.assertEqual(tk["commits"]["b" * 40]["files"], ["src/Real.java"])
        self.assertTrue(tk["files"]["src/Real.java"]["committed"])


if __name__ == "__main__":
    unittest.main()
