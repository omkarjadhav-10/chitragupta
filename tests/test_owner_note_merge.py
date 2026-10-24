"""An extractor may add evidence/history but never replace the owner's note."""
from tests.test_item_merge_order import MergeOrder, item


class NoteAuthority(MergeOrder):
    def owner_note(self):
        self.merge("2026-09-01", item("Owner correction", kind="note", detail="The original cause is a missing release."))
        note = self.only()
        note.update(session="inbox", src="user")
        return note["id"]

    def test_a_model_update_cannot_reword_an_owner_note(self):
        iid = self.owner_note()
        self.merge("2026-09-05", item("Model hypothesis", updates=iid, detail="A different explanation."))
        self.assertEqual(self.only()["title"], "Owner correction")
        self.assertEqual(self.only()["session"], "inbox")

    def test_a_same_id_model_note_cannot_replace_the_owners_detail(self):
        self.owner_note()
        self.merge("2026-09-05", item("Owner correction", kind="note", detail="The model adds a longer, different explanation."))
        self.assertEqual(self.only()["detail"], "The original cause is a missing release.")
