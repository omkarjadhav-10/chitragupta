"""Item history retains full prior text and raw provenance with a bounded current history and additive archive."""
import unittest
from tests.test_item_merge_order import MergeOrder, item


class History(MergeOrder):
    def test_older_update_keeps_its_detail_and_ref(self):
        self.merge("2026-09-05", item("Fix is on qa", detail="Newer.", line=5))
        iid = self.only()["id"]
        self.merge("2026-09-02", item("Fix not on qa", detail="Older evidence.", line=2, updates=iid))
        self.assertTrue(any(h.get("detail") == "Older evidence." and h.get("ref", "").endswith(":2")
                            for h in self.only().get("history", [])))

    def test_history_keeps_first_and_newest_four_and_archives_overflow(self):
        from chitragupta import common, store
        self.merge("2026-09-09", item("Cancel skips release", detail="Current.", line=9))
        for n in range(1, 8):
            self.merge("2026-09-%02d" % n, item("Cancel skips release", detail="Historical %d." % n, line=n))
        self.st.save()
        h = self.only().get("history", [])
        self.assertEqual([v["detail"] for v in h], ["Historical %d." % n for n in (1, 4, 5, 6, 7)])
        import json
        archived = [json.loads(x)["version"] for x in common.read_text(common.hp("data", "history", "ABC-1001.jsonl")).splitlines()]
        self.assertEqual({v["detail"] for v in h + archived}, {"Historical %d." % n for n in range(1, 8)})
        self.assertTrue(all("evidence" not in v for v in h + archived))
        self.assertTrue(all(v.get("ref") and v.get("session") and v.get("time") for v in h + archived))

    def test_retry_dedupes_content_and_previous_version_stays_last(self):
        self.merge("2026-09-09", item("Cancel skips release", detail="Current.", line=9))
        for n in (1, 3, 2, 2):
            self.merge("2026-09-%02d" % n, item("Cancel skips release", detail="Historical %d." % n, line=n))
        h = self.only()["history"]
        self.assertEqual(len(h), 3)
        self.assertEqual(h[-1]["detail"], "Historical 3.")

    def test_history_migration_backs_up_before_compaction_and_is_idempotent(self):
        from chitragupta import common, migrate, store
        import json
        tk = self.st.ticket("ABC-1001")
        versions = [dict(item("Version %d" % n), date="2026-09-%02d" % n) for n in range(1, 9)]
        tk["items"]["sample"] = dict(item("Current"), id="sample", history=versions)
        common.write_json(store.ticket_path("ABC-1001"), tk)
        before = common.read_text(store.ticket_path("ABC-1001"))
        cfg = common.load_config()
        cfg["chitragupta_migrations"] = [x for x in migrate.all_ids() if x != "kb51-history-compact"]
        common.write_json(common.wl("config.json"), cfg)
        result = migrate._run(False)
        self.assertIn("kb51-history-compact", result["applied"])
        self.assertTrue(result["backups"])
        import os
        manifest = common.read_json(os.path.join(result["backups"][0], "manifest.json"), {})
        rel = os.path.join("data", "tickets", "ABC-1001.json")
        saved = common.read_text(os.path.join(result["backups"][0], manifest[rel]))
        self.assertEqual(saved, before)
        after = common.read_json(store.ticket_path("ABC-1001"), {})
        self.assertEqual(len(after["items"]["sample"]["history"]), 5)
        archive = common.read_text(common.hp("data", "history", "ABC-1001.jsonl"))
        self.assertTrue(migrate._run(False)["already"])
        self.assertEqual(common.read_text(common.hp("data", "history", "ABC-1001.jsonl")), archive)

    def test_replacing_a_version_keeps_its_full_detail(self):
        self.merge("2026-09-01", item("First cause", detail="Initial reasoning.", line=1))
        iid = self.only()["id"]
        self.merge("2026-09-05", item("Correct cause", detail="Correction.", line=5, updates=iid))
        self.assertEqual(self.only()["history"][0].get("detail"), "Initial reasoning.")
        self.assertNotIn("history", self.only()["history"][0])
