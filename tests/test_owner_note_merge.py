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

    def test_extractor_note_can_be_updated(self):
        self.merge("2026-09-01", item("Recorded note", kind="note", detail="Initial detail."))
        iid = self.only()["id"]
        self.merge("2026-09-02", item("Corrected note", kind="note", detail="Corrected detail.", updates=iid))
        self.assertEqual(self.only()["detail"], "Corrected detail.")

    def test_identical_owner_note_retry_is_not_an_update(self):
        iid = self.owner_note()
        self.st.delta.clear()
        for n in range(12):
            self.merge("2026-09-02", item("Owner correction", kind="note",
                       detail="The original cause is a missing release.", updates=iid, evidence=[str(n)]))
        self.assertLessEqual(len(self.only()["evidence"]), 10)
        self.assertEqual(self.st.delta.get("ABC-1001", {}).get("updates", 0), 0)
        self.assertFalse(self.only().get("history"))
