"""Failed or sentence-shaped review directives must remain owner notes."""
from unittest import mock
from tests.test_kb_offticket import Base, InboxTests, T, D


class InboxSafety(Base):
    run_inbox = InboxTests.run_inbox

    def test_normal_keep_sentence_and_unresolved_id_are_recorded(self):
        from chitragupta import common
        lines = ["keep the old login message for disabled users", "keep deadbeef00", "move this check until tomorrow"]
        with mock.patch.object(common, "log") as log:
            a = self.run_inbox(*lines)
        outside = a.store.day(D)["outside"]["inbox"]
        self.assertEqual([n["text"] for n in outside], lines)
        self.assertEqual({i["title"] for i in a.store.ticket(T)["items"].values()}, set(lines))
        self.assertTrue(any("deadbeef00" in str(c) for c in log.call_args_list))

    def test_only_token_or_method_path_grammar_is_a_directive(self):
        from chitragupta import offticket
        for line in ("keep the old login message", "move the test later ABC-1002", "keep BAD /orders", "move work tomorrow"):
            self.assertIsNone(offticket.parse_directive(line), line)
        self.assertEqual(offticket.parse_directive("keep shop.orders"), ("keep", "shop.orders", None))
        self.assertEqual(offticket.parse_directive("move GET /api/orders ABC-1002"), ("move", "GET /api/orders", "ABC-1002"))

    def test_existing_record_commands_are_consumed_on_success_only(self):
        from chitragupta import offticket
        self.st.ticket(T)["apis"]["GET /api/orders"] = {"path": "/api/orders", "review": {"why": "check"}}
        a = self.run_inbox("keep shop.orders", "move GET /api/orders ABC-1002")
        self.assertFalse(a.store.day(D)["outside"]["inbox"])
        self.assertTrue(a.store.ticket(T)["tables"]["shop.orders"]["review_kept"])
        self.assertIn("GET /api/orders", a.store.ticket("ABC-1002")["apis"])
        ok, msg = offticket.apply_directive(a.store, T, ("keep", "deadbeef00", None), "2026-09-01", ["ABC"])
        self.assertFalse(ok)
