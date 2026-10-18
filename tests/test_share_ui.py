"""The dashboard's share and scheduler surfaces: GET /api/share, /api/share/preview, /api/team-notes(/note) and
/api/scheduler read only; the share-ticket and scheduler-repair actions do what the CLI does."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import test_dashboard as td

from chitragupta import common

KEY = td.TICKET


class ShareRoutes(td.Base):
    get = td.TestHttp.get  # borrowed, not inherited

    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp(prefix="cg-share-ui-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        cfg = common.load_config()
        cfg["share"] = {"targets": [{"name": "team", "kind": "folder", "path": self.folder}], "author": "dev1"}
        common.save_config(cfg)
        from chitragupta.dashboard import api, server
        api._cache.clear()
        self.srv, self.thread = server.start_in_thread(0)
        self.port = self.srv.port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def json(self, path):
        r, body = self.get(path)
        return r.status, json.loads(body)

    def test_reads_write_nothing(self):
        st, info = self.json("/api/share")
        self.assertEqual((st, info["default_level"], info["targets"][0]["problem"]), (200, "team", None))
        self.assertIn("findings", [s["id"] for s in info["sections"]])
        st, pv = self.json("/api/share/preview?key=%s&sections=summary,findings&level=team" % KEY)
        self.assertEqual((st, pv["blocked"], pv["sections"]), (200, False, ["summary", "findings"]))
        st, bad = self.json("/api/share/preview?key=%s&sections=nope" % KEY)
        self.assertEqual(st, 400)
        self.assertIn("unknown section", bad["error"])
        st, notes = self.json("/api/team-notes")
        self.assertEqual((st, notes["notes"], notes["targets"]), (200, [], ["team"]))
        self.assertEqual(os.listdir(self.folder), [])
        st, sch = self.json("/api/scheduler")
        self.assertEqual(st, 200)
        self.assertIn("installed", sch)

    def test_share_action_then_team_notes(self):
        from chitragupta.dashboard import actions
        st, body = actions.share_ticket({"key": KEY, "sections": ["summary"], "level": "team", "target": "team"})
        self.assertEqual((st, body["ok"], body["blocked"]), (200, True, False))
        self.assertTrue(body["path"].startswith(os.path.join(self.folder, "shares")))
        st, notes = self.json("/api/team-notes")
        n = notes["notes"][0]
        self.assertEqual((n["key"], n["by"], n["mine"], n["sections"]), (KEY, "dev1", True, ["summary"]))
        st, note = self.json("/api/team-notes/note?target=team&dir=" + n["dir"])
        self.assertEqual(st, 200)
        self.assertIn(KEY, note["markdown"])
        self.assertEqual(self.get("/api/team-notes/note?target=team&dir=../x")[0].status, 404)
        self.assertEqual(actions.share_ticket({"key": KEY, "sections": "summary"})[0], 400)
        self.assertEqual(actions.share_ticket({"key": KEY, "sections": ["nope"]})[0], 400)

    def test_scheduler_repair(self):
        from chitragupta import scheduler
        from chitragupta.dashboard import actions
        with mock.patch.object(scheduler, "repair", return_value={"ok": True, "changed": True, "method": "task",
                                                                  "detail": "re-registered"}) as rp:
            st, body = actions.scheduler_repair({})
        rp.assert_called_once()
        self.assertEqual((st, body["changed"]), (200, True))
        self.assertIn("installed", body["status"])


class ShareSettings(td.Base):
    def test_targets_validation(self):
        from chitragupta import settings
        folder = tempfile.mkdtemp(prefix="cg-share-set-")
        self.addCleanup(shutil.rmtree, folder, True)
        k = "share.targets"
        ok = [{"name": "team", "kind": "folder", "path": folder},
              {"name": "repo", "kind": "git", "url": "https://example.test/team/notes.git", "branch": "main"}]
        self.assertEqual(settings.validate({k: ok})[1], {})
        for bad in ([{"name": "a b", "kind": "folder", "path": folder}],
                    [{"name": "x", "kind": "folder", "path": os.path.join(folder, "missing")}],
                    [{"name": "x", "kind": "slack", "url": "https://hooks.slack.com/services/x"}],
                    [{"name": "x", "kind": "git", "url": "https://hooks.slack.com/x"}],
                    [{"name": "x", "kind": "folder", "path": folder}, {"name": "x", "kind": "folder", "path": folder}],
                    [{"name": "x", "kind": "folder", "path": folder, "post_to": "everyone"}], "team"):
            self.assertIn(k, settings.validate({k: bad})[1], bad)
        self.assertEqual(settings.validate({"share.author": "Dev One"})[1], {})
        self.assertIn("share.author", settings.validate({"share.author": "a\nb"})[1])
        self.assertIn("share.default_level", settings.validate({"share.default_level": "world"})[1])


if __name__ == "__main__":
    unittest.main()
