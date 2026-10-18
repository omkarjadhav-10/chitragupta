"""Team sharing (share.py): preview writes nothing; a share writes a redacted bundle only to the configured target and
only when the deny-list / secret scan passes; a git target commits and pushes shares/ only (never forced); team notes
are read back; the guard refuses everything else."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests import test_dashboard as td

from chitragupta import common

KEY = td.TICKET


def _git(*args, cwd=None):
    p = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)
    if p.returncode:
        raise AssertionError("git %s: %s" % (" ".join(args), p.stderr))
    return p.stdout


class Base(td.Base):
    def setUp(self):
        super().setUp()
        old = os.environ.get("GIT_ALLOW_PROTOCOL")
        os.environ["GIT_ALLOW_PROTOCOL"] = "file"  # nothing may leave the machine
        self.addCleanup(lambda: os.environ.pop("GIT_ALLOW_PROTOCOL", None) if old is None
                        else os.environ.__setitem__("GIT_ALLOW_PROTOCOL", old))
        self.folder = tempfile.mkdtemp(prefix="cg-share-folder-")
        self.addCleanup(shutil.rmtree, self.folder, True)

    def configure(self, *targets, **share):
        cfg = common.load_config()
        cfg["share"] = dict({"targets": list(targets)}, **share)
        common.save_config(cfg)
        from chitragupta.dashboard import api
        api._cache.clear()

    def add_finding(self, text):
        p = common.hp("data", "tickets", KEY + ".json")
        with open(p, encoding="utf-8") as f:
            tk = json.load(f)
        tk.setdefault("items", {})["zz"] = {"id": "zz", "kind": "root_cause", "title": "Planted", "detail": text,
                                            "date": self.days["day"]}
        td._w(p, tk)
        from chitragupta.dashboard import api
        api._cache.clear()

    def files_under(self, root):
        return sorted(os.path.relpath(os.path.join(d, f), root).replace("\\", "/")
                      for d, _, fs in os.walk(root) for f in fs if ".git" not in d.split(os.sep))


class Preview(Base):
    def test_preview_writes_nothing(self):
        from chitragupta import share
        self.configure({"name": "team", "kind": "folder", "path": self.folder})
        p = share.preview(KEY)
        self.assertEqual((p["key"], p["level"], p["blocked"]), (KEY, "team", False))
        self.assertIn("summary", p["sections"])
        self.assertNotIn("notes", p["sections"])  # the README is opt-in
        self.assertIn("My notes", p["never"])
        self.assertTrue(p["markdown"].startswith("<!-- cg-share "))
        self.assertEqual(self.files_under(self.folder), [])

    def test_my_notes_never_leave(self):
        from chitragupta import share
        self.configure({"name": "team", "kind": "folder", "path": self.folder})
        td._w(os.path.join(common.home(), "tickets", KEY, "README.md"),
              "# ABC-1001\n\nShared context.\n\n## My notes\n<!-- my-notes:start -->\nonly for me: ask about the refund\n"
              "<!-- my-notes:end -->\n")
        from chitragupta.dashboard import api
        api._cache.clear()
        p = share.preview(KEY, "notes,summary")
        self.assertIn("Shared context.", p["markdown"])
        self.assertNotIn("only for me", p["markdown"])
        self.assertIn("My notes", p["removed_text"]["notes"])

    def test_unknown_section_and_level(self):
        from chitragupta import share
        self.configure({"name": "team", "kind": "folder", "path": self.folder})
        with self.assertRaises(share.ShareError):
            share.preview(KEY, "everything")
        with self.assertRaises(share.ShareError):
            share.preview(KEY, level="world")
        with self.assertRaises(share.ShareError):
            share.preview("ABC-9999")


class FolderTarget(Base):
    def test_share_writes_the_bundle_only(self):
        from chitragupta import share
        self.configure({"name": "team", "kind": "folder", "path": self.folder}, author="dev1")
        r = share.share(KEY)
        self.assertTrue(r["ok"], r)
        files = self.files_under(self.folder)
        self.assertEqual(len(files), 2)
        self.assertTrue(all(f.startswith("shares/%s/" % KEY) for f in files), files)
        self.assertEqual({os.path.basename(f) for f in files}, {"README.md", "ticket.html"})
        notes = share.team_notes()
        self.assertEqual([(n["key"], n["level"], n["by"], n["mine"]) for n in notes], [(KEY, "team", "dev1", True)])
        got = share.read_note("team", notes[0]["dir"])
        self.assertIn(KEY, got["markdown"])
        self.assertIsNone(share.read_note("team", "../../etc"))

    def test_deny_list_hit_blocks(self):
        from chitragupta import share
        deny = os.path.join(common.home(), ".worklog", "denylist.txt")
        td._w(deny, "# names that must not leave\nzebra-ledger\ninternal:shop-api\n")
        self.configure({"name": "team", "kind": "folder", "path": self.folder})
        self.add_finding("The zebra-ledger job double-counts discounts in shop-api.")
        r = share.share(KEY)
        self.assertFalse(r["ok"])
        self.assertTrue(r["blocked"])
        self.assertTrue(any(h["rule"] == "zebra-ledger" for h in r["hits"]), r["hits"])
        self.assertFalse(any(h["rule"].endswith("shop-api") for h in r["hits"]))  # internal names are fine for the team
        self.assertEqual(self.files_under(self.folder), [])
        p = share.preview(KEY, "summary,branches", level="public")  # leave the finding out; public masks internal names
        self.assertFalse(p["blocked"], p["hits"])
        self.assertNotIn("shop-api", p["markdown"])

    def test_public_needs_a_deny_list(self):
        from chitragupta import share
        self.configure({"name": "team", "kind": "folder", "path": self.folder})
        p = share.preview(KEY, level="public")
        self.assertTrue(p["blocked"])
        self.assertIn("deny-list", p["problems"][0])

    def test_bad_targets(self):
        from chitragupta import share
        from chitragupta.safety.log import Blocked
        self.configure({"name": "gone", "kind": "folder", "path": os.path.join(self.folder, "nope")})
        with self.assertRaises(share.ShareError):
            share.share(KEY)
        self.configure({"name": "chat", "kind": "slack", "url": "https://hooks.slack.com/services/x"})
        with self.assertRaises((share.ShareError, Blocked)):
            share.share(KEY)
        self.configure()
        with self.assertRaises(share.ShareError):
            share.share(KEY)


class GitTarget(Base):
    def setUp(self):
        super().setUp()
        self.remote = os.path.join(tempfile.mkdtemp(prefix="cg-share-remote-"), "team.git")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.remote), True)
        _git("init", "-q", "--bare", "-b", "main", self.remote)
        self.configure({"name": "teamrepo", "kind": "git", "url": self.remote, "branch": "main"})

    def test_commit_and_push_shares_only(self):
        from chitragupta import share
        r = share.share(KEY)
        self.assertTrue(r["ok"] and r["committed"] and r["pushed"], r)
        changed = _git("log", "--name-only", "--format=", "main", cwd=self.remote).split()
        self.assertTrue(changed and all(f.startswith("shares/%s/" % KEY) for f in changed), changed)
        r2 = share.share(KEY, "findings")  # a second bundle: a new commit on top, history kept
        self.assertTrue(r2["pushed"], r2)
        self.assertEqual(len(_git("rev-list", "main", cwd=self.remote).split()), 2)
        self.assertEqual(len(share.team_notes("teamrepo")), 2)

    def test_guard(self):
        from chitragupta.safety import commands as c
        cfg = common.load_config()
        d = c.share_repo_dir("teamrepo")
        pre = ["git", "-C", d] + c.HARDEN
        t = c.share_git_targets(cfg)[0]
        ok = [["add", "--", "shares/ABC-1001/x/README.md"], ["commit", "-q", "-m", "m", "--", "shares/ABC-1001/x"],
              ["push", "-q", "-u", "origin", "main"], ["fetch", "-q", "origin", "main"]]
        for a in ok:
            self.assertIsNone(c.git_share_reason(a, t), a)
        bad = [["push", "--force", "origin", "main"], ["push", "-q", "origin", "main:other"], ["add", "-A"],
               ["add", "--", "README.md"], ["add", "--", "shares/../x"], ["commit", "-q", "-m", "m", "--", "docs"],
               ["reset", "--hard"], ["rebase", "origin/main"], ["remote", "set-url", "origin", "x"],
               ["config", "user.name", "x"], ["push", "-q", "upstream", "main"]]
        for a in bad:
            self.assertIsNotNone(c.git_share_reason(a, t), a)
        self.assertIsNotNone(c.reason(["git", "-C", common.home()] + c.HARDEN + ["status"], "git-share", cfg=cfg))
        self.assertIsNotNone(c.reason(pre + ["status"], "git-work", cfg=cfg))
        self.assertIsNotNone(c.share_url_reason("https://hooks.slack.com/services/T/B/x"))
        self.assertIsNotNone(c.share_url_reason("--upload-pack=evil"))


if __name__ == "__main__":
    unittest.main()
