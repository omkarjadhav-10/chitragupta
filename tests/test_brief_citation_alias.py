"""Only an exact current item may resolve through the item: namespace."""
import unittest
from tests.test_brief import BriefBase, T, c, ticket
from chitragupta import brief


class CitationAlias(BriefBase, unittest.TestCase):
    def validate(self, iid):
        return brief.validate({"root_cause": {"confidence": "confirmed", "claims": [c("Cancel never releases stock.", iid)]}}, T, ticket(), "bug")

    def test_current_item_namespace_resolves_to_its_exact_id(self):
        sec, dropped = self.validate("item:rc00000002")
        self.assertEqual(sec["root_cause"]["claims"], [c("Cancel never releases stock.", "rc00000002")])
        self.assertEqual(dropped, [])

    def test_old_or_missing_item_namespace_does_not_resolve(self):
        for iid in ("item:rc00000001", "item:doesnotexist", "item:RC00000002", "item:rc0000000", "item:item:rc00000002"):
            with self.subTest(iid=iid):
                sec, dropped = self.validate(iid)
                self.assertEqual(sec["root_cause"]["claims"], [])
                self.assertTrue(dropped)

    def test_other_namespaces_cannot_alias_items(self):
        for iid in ("fact:rc00000002", "query:rc00000002"):
            sec, dropped = self.validate(iid)
            self.assertEqual(sec["root_cause"]["claims"], [])
            self.assertTrue(dropped)
