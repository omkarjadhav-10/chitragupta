"""Queries and catalog/file records use their own turn and trusted raw time."""
from tests.test_attribution import AttrCase, D, ev


class RecordTurns(AttrCase):
    def merge_records(self, result, **extra):
        from chitragupta import store
        st = store.Store()
        ctx = {"day": D, "session": "session123", "time": "10:00", "repo": None, "branch": None,
               "default": "ABC-1001", "turns": [("10:00", "ABC-1001", "prompt", 1),
                                                   ("11:00", "ABC-1002", "prompt", 10)]}
        ctx.update(extra)
        store.merge_extraction(st, result, ctx, ["ABC"])
        return st

    def test_each_record_kind_uses_its_own_turn(self):
        st = self.merge_records({"queries": [{"text": "select id from shop.orders", "time": "11:01"}],
                                 "tables": [{"name": "shop.orders", "time": "11:02"}],
                                 "apis": [{"method": "GET", "path": "/orders", "time": "11:03"}],
                                 "files": [{"path": "src/orders.py", "time": "11:04"}]})
        for kind, when in (("queries", "11:01"), ("tables", "11:02"), ("apis", "11:03"), ("files", "11:04")):
            self.assertFalse(st.ticket("ABC-1001")[kind], kind)
            record = next(iter(st.ticket("ABC-1002")[kind].values()))
            self.assertEqual((record["time"], record["src"]), (when, "prompt"))

    def test_sent_sql_timestamp_wins_over_model_and_chunk_time(self):
        from chitragupta import sqlsent
        sent = sqlsent.sent_statements([ev("post-tool", 11, 5, _ln=12,
            payload={"tool": "mcp__database__query", "input": {"arguments": {"sql": "select id from shop.orders"}}})])
        st = self.merge_records({"queries": [{"text": "select id from shop.orders", "time": "10:02", "line": 12}]}, sent=sent)
        self.assertFalse(st.ticket("ABC-1001")["queries"])
        query = next(iter(st.ticket("ABC-1002")["queries"].values()))
        self.assertEqual(query["time"], "11:05")

    def test_raw_line_time_wins_over_a_model_time(self):
        st = self.merge_records({"tables": [{"name": "shop.orders", "time": "10:02", "line": 12}]},
                                line_times={12: "11:06"})
        self.assertFalse(st.ticket("ABC-1001")["tables"])
        self.assertEqual(st.ticket("ABC-1002")["tables"]["shop.orders"]["time"], "11:06")

    def test_same_sql_on_two_turns_uses_the_named_raw_line(self):
        from chitragupta import sqlsent
        events = [ev("post-tool", hour, 5, _ln=line,
            payload={"tool": "mcp__database__query", "input": {"arguments": {"sql": "select id from shop.orders"}}})
            for hour, line in ((10, 2), (11, 12))]
        st = self.merge_records({"queries": [{"text": "select id from shop.orders", "line": 12}]},
                                sent=sqlsent.sent_statements(events))
        self.assertFalse(st.ticket("ABC-1001")["queries"])
        self.assertEqual(next(iter(st.ticket("ABC-1002")["queries"].values()))["time"], "11:05")

    def test_provenance_migration_backs_up_and_keeps_unknown_time_unknown(self):
        import os
        from chitragupta import common, migrate, store
        tk = store.new_ticket("ABC-1001")
        tk["tables"]["shop.orders"] = {"name": "shop.orders", "purpose": "Orders"}
        common.write_json(store.ticket_path("ABC-1001"), tk)
        before = common.read_text(store.ticket_path("ABC-1001"))
        cfg = common.load_config()
        cfg["chitragupta_migrations"] = [m for m in migrate.all_ids() if m != "kb51-record-provenance"]
        common.write_json(common.wl("config.json"), cfg)
        result = migrate._run(False)
        self.assertIn("kb51-record-provenance", result["applied"])
        backup = result["backups"][0]
        manifest = common.read_json(os.path.join(backup, "manifest.json"), {})
        rel = os.path.join("data", "tickets", "ABC-1001.json")
        self.assertEqual(common.read_text(os.path.join(backup, manifest[rel])), before)
        table = common.read_json(store.ticket_path("ABC-1001"))["tables"]["shop.orders"]
        self.assertEqual((table["time"], table["src"]), ("", "legacy"))
        self.assertTrue(migrate._run(False)["already"])
