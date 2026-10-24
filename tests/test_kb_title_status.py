"""K4: a ticket's title comes only from its Jira card or the user; Jira status and the inferred status are kept apart."""
import os
import types
import unittest

from tests.support import PY, Isolated, write_config, backup_file

DAY = "2026-09-01"


def ctx():
    return {"day": DAY, "session": "abcdef123456", "time": "10:00", "repo": "C:/w/shop-api",
            "branch": "feature/ABC-1001-cancel-order", "candidates": ["ABC-1001"]}


HINT = {"ticket_links": [{"ticket": "ABC-1001", "title_hint": "Guessed cancel title", "evidence": "prompt"}],
        "items": [{"kind": "finding", "ticket": "ABC-1001", "title": "Cancel keeps the reservation", "detail": "",
                   "evidence": [], "confidence": "likely", "time": "10:00", "updates": None}]}


class TitleStatus(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY}})

    def jobs(self, st):
        from chitragupta import kbjobs
        return kbjobs.KBJobs(types.SimpleNamespace(cfg={}, store=st, health={"mcp": {"jira_auth_ok": True}}))

    def card(self, st, status="QA COMPLETED"):
        self.jobs(st).apply_card("ABC-1001", {"raw": "x", "fetched_at": DAY + "T09:00:00+05:30",
                                              "card": {"found": True, "title": "Cancel order keeps stock reserved",
                                                       "type": "Bug", "status": status}})

    def test_no_card_title_is_the_key_and_the_hint_is_labelled(self):
        from chitragupta import render, store
        st = store.Store()
        store.merge_extraction(st, HINT, ctx(), ["ABC"])
        tk = st.ticket("ABC-1001")
        self.assertEqual(tk["title"], "")
        self.assertEqual(tk["title_hint"], "Guessed cancel title")
        md = render.render_ticket(st, "ABC-1001")
        self.assertTrue(md.startswith("# ABC-1001\n"), md[:80])
        self.assertIn("hint: Guessed cancel title", md)
        st.save()
        row = [r for r in render.render_index(st).splitlines() if r.startswith("| [ABC-1001]")][0]
        self.assertEqual(row.split(" | ")[1], "_hint: Guessed cancel title_")  # labelled, never the title
        day = render.render_day(st, DAY, {"day_starts_at": "04:00"}, {})
        self.assertIn("### ABC-1001\n", day)

    def test_branch_slug_is_only_a_hint(self):
        from chitragupta import store
        st = store.Store()
        store.touch_ticket_meta(st, "ABC-1001", DAY, "C:/w/shop-api", "feature/ABC-1001-cancel-order")
        tk = st.ticket("ABC-1001")
        self.assertEqual((tk["title"], tk["title_hint"]), ("", "cancel order"))

    def test_card_sets_title_type_status_and_day_close_keeps_it(self):
        from chitragupta import agent, common, extract, render, store
        st = store.Store()
        store.merge_extraction(st, HINT, ctx(), ["ABC"])
        self.card(st)
        tk = st.ticket("ABC-1001")
        self.assertEqual((tk["title"], tk["title_source"]), ("Cancel order keeps stock reserved", "jira"))
        self.assertEqual((tk["jira"]["type"], tk["jira"]["status"]), ("Bug", "QA COMPLETED"))
        st.save()
        real = extract.call_claude
        self.addCleanup(setattr, extract, "call_claude", real)
        extract.call_claude = lambda kind, text, cfg, timeout=300: ({
            "day_summary": "x", "standup": {}, "open_todos": [],
            "per_ticket": [{"ticket": "ABC-1001", "what_i_did": ["looked"], "tldr": "Cancel keeps stock.",
                            "problem": "", "status_inferred": "in_progress", "next_steps": []}]}, 0.0)
        common.load_config()
        a = agent.Agent()
        a.consolidate(DAY)
        tk = a.store.ticket("ABC-1001")
        self.assertEqual(tk["jira"]["status"], "QA COMPLETED")
        self.assertEqual(tk["status_inferred"], "in_progress")
        a.store.save()
        self.assertEqual(common.read_json(store.ticket_path("ABC-1001"))["status"], "QA COMPLETED")  # computed, for old readers
        md = render.render_ticket(a.store, "ABC-1001")
        self.assertIn("Jira: QA COMPLETED · Inferred: in_progress", md)
        self.card(a.store, "Done")  # a later card never touches the inferred status
        self.assertEqual(tk["status_inferred"], "in_progress")

    def test_dashboard_summary_keeps_both(self):
        from chitragupta import store
        from chitragupta.dashboard import api
        st = store.Store()
        store.merge_extraction(st, HINT, ctx(), ["ABC"])
        st.ticket("ABC-1001")["status_inferred"] = "blocked"
        s = api.ticket_summary("ABC-1001", st.ticket("ABC-1001"))
        self.assertEqual((s["title"], s["title_hint"], s["jira_status"], s["status_inferred"]),
                         ("", "Guessed cancel title", "", "blocked"))


class Migration(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        from chitragupta import common, migrate
        write_config(self.home, {"ticket_prefixes": ["ABC"]})
        cfg = common.read_json(common.wl("config.json"), {})
        cfg["chitragupta_migrations"] = [m for m in migrate.all_ids() if m != "kb5-title-status"]
        common.write_json(common.wl("config.json"), cfg)
        common.write_json(common.hp("data", "tickets", "ABC-1001.json"),
                          {"id": "ABC-1001", "title": "Guessed", "title_source": "hint", "status": "fix_ready"})
        common.write_json(common.hp("data", "tickets", "ABC-1002.json"),
                          {"id": "ABC-1002", "title": "Real", "title_source": "jira", "status": "in_progress",
                           "jira": {"status": "QA COMPLETED", "type": "Bug"}})
        common.write_json(common.hp("data", "tickets", "ABC-1003.json"),
                          {"id": "ABC-1003", "title": "Mine", "title_source": "user", "status": "Done", "jira": {"status": "Done"}})

    def test_moves_hints_and_splits_status(self):
        from chitragupta import common, migrate, store
        res = migrate.run(wait_s=0)
        self.assertIn("kb5-title-status", res["applied"], res)
        rd = lambda t: common.read_json(store.ticket_path(t))
        a, b, c = rd("ABC-1001"), rd("ABC-1002"), rd("ABC-1003")
        self.assertEqual((a["title"], a["title_hint"], a["status_inferred"], a["status"]), ("", "Guessed", "fix_ready", "fix_ready"))
        self.assertEqual((b["title"], b["status_inferred"], b["status"]), ("Real", "in_progress", "QA COMPLETED"))
        self.assertEqual((c["title"], c.get("status_inferred", ""), c["status"]), ("Mine", "", "Done"))
        self.assertTrue(os.path.isfile(backup_file(res["backups"], "kb5-title-status", "data", "tickets", "ABC-1001.json")))


if __name__ == "__main__":
    unittest.main()
