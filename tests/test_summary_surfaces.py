"""Summary defaults, preserved Full brief, Epic answers and reviewed golden artifacts."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from tests import support
from tests.test_brief import BriefBase, T, c

FIXTURES = Path(support.ROOT) / "tests/fixtures/summary"


class SummarySurfaces(BriefBase, unittest.TestCase):
    def prepared(self):
        from chitragupta import brief, common, store
        self.output["summary"] = {"unknowns": [c("Partial cancels still need checking.", "rk00000001")],
                                  "next_action": c("Run the partial cancel check in QA.", "rk00000001")}
        with mock.patch.object(common, "now_iso", return_value="2026-09-01T12:00:00+05:30"):
            self.gen()
        st = store.Store()
        return st, brief.view(st.ticket(T),T,self.cfg())

    def test_six_questions_precede_collapsed_full_brief(self):
        from chitragupta import render, summary
        st, v = self.prepared()
        md = render.render_ticket(st,T)
        self.assertIn("<details><summary>Full brief</summary>",md)
        end = md.index("<details><summary>Full brief</summary>")
        for heading in summary.HEADINGS.values():
            self.assertLess(md.index(heading),end)
        self.assertIn("How it works today",md)
        self.assertIn("What was actually verified?",md)
        self.assertIn("Run the partial cancel check",md)

    def test_readme_export_and_sessionstart_goldens(self):
        from chitragupta import export, render
        st,v = self.prepared()
        md = render.render_ticket(st,T)
        self.assertIn("## Ticket summary",md)
        context = render.render_context(st,T).replace(self.home,"<HOME>").replace(chr(92),"/")
        snapshot = export.snapshot(T)
        outputs = {"ABC-1001.README.md": md,
                   "ABC-1001.SessionStart.txt": context,
                   "ABC-1001.export-summary.json": json.dumps(snapshot["brief"]["summary"],indent=2,ensure_ascii=False)+"\n"}
        if os.environ.get("CG_RECORD_SUMMARY_GOLDENS"):
            (Path(tempfile.gettempdir()) / "summary-goldens.json").write_text(json.dumps(outputs,ensure_ascii=False),encoding="utf-8")
            self.skipTest("Golden artifacts staged in isolated temp for review after the runner exits")
        for name,text in outputs.items():
            self.assertEqual((FIXTURES/name).read_text(encoding="utf-8"),text,name)
        self.assertLessEqual(len(context),4000)

    def test_empty_sections_render_and_replay_plan_proves_nothing(self):
        from chitragupta import brief, render, summary
        tk = {"id":T,"items":{},"replay":{"final":[{"n":1,"expected":"200","executed":False}]}}
        sv = summary.view(tk,T,self.cfg(),card={})
        md = "\n".join(render.render_summary({"summary":sv}))
        for heading in summary.HEADINGS.values():
            self.assertIn(heading,md)
        self.assertEqual(md.count("Not recorded yet"),5)
        self.assertIn("Nothing verified yet",md)

    def test_epic_child_answers_all_six_without_a_model_call(self):
        from chitragupta import brief, common, store
        tk = self.saved()
        tk["jira"]["parent"] = "ABC-1000"
        common.write_json(store.ticket_path(T),tk)
        epic = {"id":"ABC-1000","title":"Shop stock","jira":{"type":"Epic"},"items":{}}
        common.write_json(store.ticket_path("ABC-1000"),epic)
        v = brief.view(epic,"ABC-1000",self.cfg())
        child = v["children"][0]
        self.assertEqual(len(child["summary"]["sections"]),6)
        for word in ("Problem:","Cause", "Changed:", "Verified:","Unknown:","Next:"):
            self.assertIn(word,child["summary_line"])
        from chitragupta import render
        rollups = "\n".join(render.render_brief(v)) + "\n".join(brief.text_lines(v))
        for word in ("Problem:","Changed:","Verified:","Unknown:","Next:"):
            self.assertIn(word,rollups)
        self.assertEqual(self.calls,[])
