"""Repository invariants: versions agree, names are fixed, the scanner works."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VersionTests(unittest.TestCase):
    def test_versions_agree(self):
        versions = load_script("bump_version").current()
        self.assertEqual(len(set(versions.values())), 1, versions)


class ManifestTests(unittest.TestCase):
    def test_names(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "cg")
        self.assertEqual(plugin["displayName"], "Chitragupta")
        self.assertEqual(plugin["license"], "MIT")
        self.assertEqual(market["name"], "chitragupta")
        entry = market["plugins"][0]
        self.assertEqual((entry["name"], entry["displayName"], entry["source"]), ("cg", "Chitragupta", "./"))


class ScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scan = load_script("scan")

    def test_secret_rules_catch_fakes(self):
        rules = self.scan.secret_rules()
        # Assembled at runtime so this file never contains a literal token.
        fakes = ["gh" + "p_" + "a" * 36, "xox" + "b-" + "1234567890-abc", "AKIA" + "Z" * 16,
                 "-----BEGIN " + "RSA PRIVATE KEY-----", 'password = "' + "hunter2hunter2" + '"']
        for fake in fakes:
            self.assertTrue(self.scan.scan_text("t", fake, [], rules), fake)

    def test_placeholders_pass(self):
        rules = self.scan.secret_rules()
        ok = ['password = "{{process.env.APP_PASSWORD}}"', "token: ${JIRA_TOKEN}", 'api_key = "<your key>"']
        for line in ok:
            self.assertEqual(self.scan.scan_text("t", line, [], rules), [], line)

    def test_denylist_plain_and_regex(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "deny.txt"
            p.write_text("# comment\nacmecorp\nre:\\bXYZ-\\d+\n", encoding="utf-8")
            deny = self.scan.load_denylist(p)
        self.assertEqual(len(deny), 2)
        self.assertTrue(self.scan.scan_text("t", "Welcome to AcmeCorp", deny, []))
        self.assertTrue(self.scan.scan_text("t", "fixes XYZ-42", deny, []))
        self.assertFalse(self.scan.scan_text("t", "fixes ABC-1001", deny, []))


if __name__ == "__main__":
    unittest.main()
