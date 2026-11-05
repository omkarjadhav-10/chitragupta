"""Gate 2: every brief surface renders each step once, the "Fixes made" rows, and a label once.

brief.view gives the before/after sections their steps; a renderer that also read them as claims printed every step
twice. A claim the model wrote as "Ruled out: …" printed the label twice. The real view (not a fixture) goes through
the hub README, the CLI text, MCP get_ticket section=brief and the SessionStart lines. Synthetic data only.
"""
import unittest

from tests import test_brief as tb

STEPS = ("Cancel sets the status and returns.", "Cancel releases the reservation, then sets the status.")


class RenderOnce(tb.BriefBase, unittest.TestCase):
    def view(self):
        from chitragupta import brief
        self.gen()
        return brief.view(self.saved(), tb.T, self.cfg())

    def surfaces(self, v):
        from chitragupta import brief, mcpserver, render
        tk = self.saved()
        return {"readme": "\n".join(render.render_brief(v, tk["items"])),
                "cli": "\n".join(brief.text_lines(v)),
                "mcp": "\n".join(mcpserver._brief_section({"key": tb.T, "brief": v, "findings": [], "queries": []})),
                "context": "\n".join(render.brief_context_lines(v))}

    def test_each_step_once_on_every_surface(self):
        v = self.view()
        by = {s["id"]: s for s in v["sections"]}
        for sid, text in (("before", STEPS[0]), ("after", STEPS[1])):
            self.assertEqual([c["text"] for c in by[sid]["steps"]], [text])
            self.assertFalse([c for c in by[sid].get("claims") or [] if c["text"] == text], "a step is not also a claim")
        for name, out in self.surfaces(v).items():
            if name == "context":
                continue  # the SessionStart lines carry root cause, fixes and cons only
            for text in STEPS:
                self.assertEqual(out.count(text), 1, "%s: %r" % (name, text))

    def test_ruled_out_label_once(self):
        v = self.view()
        fi = [s for s in v["sections"] if s["id"] == "findings"][0]
        ro = [c for c in fi["claims"] if c.get("label") == "ruled-out"][0]
        self.assertEqual(ro["text"], "The nightly job alone.")
        for name, out in self.surfaces(v).items():
            if name != "context":
                self.assertEqual(out.lower().count("ruled out"), 1, name)
                self.assertIn("The nightly job alone.", out)

    def test_fixes_made_rows_render(self):
        v = self.view()
        fm = [s for s in v["sections"] if s["id"] == "fixes_made"][0]
        self.assertEqual([r["cites"] for r in fm["list"]], [["commit:" + tb.SHA[:7]], ["pr:7"], []])
        for name in ("readme", "mcp", "context"):
            out = self.surfaces(v)[name]
            self.assertEqual(out.count("ABC-1001 release stock on cancel"), 1, name)
            self.assertIn("PR #7", out, name)

    def test_an_empty_section_says_not_recorded(self):
        from chitragupta import render
        self.output["proposed"] = {"claims": [], "rejected": tb.c("")}
        v = self.view()
        md = "\n".join(render.render_brief(v, self.saved()["items"]))
        sec = md.split("### Proposed fix", 1)[1].split("###", 1)[0]
        self.assertIn("_Not recorded yet._", sec)
        self.assertNotIn("True", md)


if __name__ == "__main__":
    unittest.main()
