"""Release notes must have unambiguous sections and resolvable README documentation links."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ReleaseNotes(unittest.TestCase):
    def test_each_release_has_unique_category_headings(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for release in re.split(r"(?m)^## ", text)[1:]:
            headings = re.findall(r"(?m)^### (.+)$", release)
            self.assertEqual(len(headings), len(set(headings)), release.splitlines()[0])

    def test_readme_documentation_links_resolve(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for target in re.findall(r"\]\((docs/[^)#]+)(?:#[^)]*)?\)", text):
            self.assertTrue((ROOT / target).is_file(), target)
