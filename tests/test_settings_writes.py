"""Settings and dashboard writes: the one config write path (lock, backup, history, undo, no lost updates), the
settings schema's validation and locked items, and every write-protection check of the dashboard.

Synthetic home from tests/test_dashboard.py; ephemeral ports; nothing outside temp folders is touched.
"""
import json
import os
import re
import threading
import unittest

from tests import test_dashboard as td

from chitragupta import common


def raw_cfg():
    with open(common.wl("config.json"), encoding="utf-8") as f:
        return json.load(f)


class ConfigStoreTests(td.Base):
    def test_update_backup_history_and_secret_masking(self):
        from chitragupta import configstore
        res = configstore.update({"jira_worklog.send_at": "20:30", "mcp.jira.api_token": "s3cr3t"}, "dashboard")
        self.assertEqual(sorted(d["key"] for d in res["changes"]), ["jira_worklog.send_at", "mcp.jira.api_token"])
        self.assertTrue(res["backup"] and os.path.isfile(res["backup"]))
        self.assertEqual(raw_cfg()["jira_worklog"]["send_at"], "20:30")
        hist = configstore.history()
        self.assertEqual({h["key"] for h in hist}, {"jira_worklog.send_at", "mcp.jira.api_token"})
        with open(configstore.history_path(), encoding="utf-8") as f:
            self.assertNotIn("s3cr3t", f.read())
        self.assertEqual(configstore.update({"jira_worklog.send_at": "20:30"}, "dashboard")["changes"], [], "no-op change")

    def test_undo_and_refusals(self):
        from chitragupta import configstore
        configstore.update({"jira_worklog.send_at": "21:00"}, "dashboard")
        configstore.undo("dashboard")
        self.assertNotIn("send_at", raw_cfg().get("jira_worklog", {}))
        with self.assertRaises(configstore.ConfigError):
            configstore.undo("dashboard")  # the undo itself is not undone again
        configstore.update({"jira_worklog.send_at": "21:15"}, "dashboard")
        configstore.update({"jira_worklog.send_at": "21:45"}, "setup")  # someone else changed it since
        with self.assertRaises(configstore.ConfigError):
            configstore.undo("dashboard")
        self.assertEqual(raw_cfg()["jira_worklog"]["send_at"], "21:45")

    def test_engine_save_never_overwrites_a_newer_change(self):
        from chitragupta import configstore
        import copy
        cfg = common.load_config()
        base = copy.deepcopy(cfg)
        configstore.update({"jira_worklog.send_at": "19:30"}, "dashboard")  # the page saves while a tick runs
        cfg["ticket_prefixes"] = list(cfg.get("ticket_prefixes") or []) + ["XYZ"]  # what the tick learned
        common.save_config(cfg, base=base)
        now = raw_cfg()
        self.assertEqual(now["jira_worklog"]["send_at"], "19:30")
        self.assertIn("XYZ", now["ticket_prefixes"])

    def test_lock_serialises_writers(self):
        from chitragupta import configstore
        errors = []

        def bump(i):
            try:
                configstore.update(lambda c, i=i: c.setdefault("ticket_prefixes", []).append("T%02d" % i), "test")
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=bump, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len([p for p in raw_cfg()["ticket_prefixes"] if p.startswith("T")]), 8)
        self.assertFalse(os.path.exists(common.wl("config.lock")))

    def test_setup_goes_through_the_store(self):
        from chitragupta import configstore, setup
        raw, _ = setup.read_raw_config()
        new = dict(raw, push_interval_minutes=45)
        self.assertTrue(setup.write_raw_config(new, raw))
        self.assertTrue(any(h["key"] == "push_interval_minutes" and h["source"] == "setup" for h in configstore.history()))
        with self.assertRaises(configstore.ConfigError):  # a stale view is refused, not merged blindly
            setup.write_raw_config(dict(raw, push_interval_minutes=60), raw)


class SettingsSchemaTests(td.Base):
    def test_validation_messages(self):
        from chitragupta import settings
        clean, errors = settings.validate({
            "jira_worklog.send_at": "25:00", "jira_worklog.workdays": [], "ticket_prefixes": ["abc"],
            "mcp.daily_call_budget": 500, "branching.guard_mode": "maybe", "jira_worklog.hours_per_day": 7.5,
            "github_remote": "https://example.invalid/x", "nope.key": 1, "safety.rules": "off"})
        self.assertEqual(clean, {"jira_worklog.hours_per_day": 7.5})
        self.assertIn("24-hour", errors["jira_worklog.send_at"])
        self.assertIn("at least one day", errors["jira_worklog.workdays"])
        self.assertIn("uppercase", errors["ticket_prefixes"])
        self.assertIn("0 to 100", errors["mcp.daily_call_budget"])
        self.assertIn("/cg:setup", errors["github_remote"])
        self.assertIn("/cg:setup", errors["safety.rules"])
        self.assertIn("doesn't exist", errors["nope.key"])

    def test_envs_can_only_narrow(self):
        from chitragupta import configstore, settings
        configstore.update({"qa_data.allowed_envs": ["qa", "uat"]}, "setup")
        _, errors = settings.validate({"qa_data.allowed_envs": ["qa", "prod"]})
        self.assertIn("/cg:setup", errors["qa_data.allowed_envs"])
        clean, errors = settings.validate({"qa_data.allowed_envs": ["qa"]})
        self.assertEqual((clean, errors), ({"qa_data.allowed_envs": ["qa"]}, {}))

    def test_describe_lists_locked_items_without_secrets(self):
        from chitragupta import settings
        d = settings.describe()
        items = {i["key"]: i for g in d["groups"] for i in g["items"]}
        for k in ("safety.rules", "safety.mcp_tools", "github_remote", "worklog_home", "mcp.database.server"):
            self.assertFalse(items[k]["editable"], k)
        self.assertTrue(items["jira_worklog.send_at"]["editable"])
        self.assertEqual(items["jira_worklog.send_at"]["value"], "20:00")
        blob = json.dumps(d)
        self.assertNotRegex(blob, r"(?i)\"[^\"]*(token|secret|password)[^\"]*\": \"[^\"(]")


class WriteProtectionTests(td.Base):
    get = td.TestHttp.get

    def setUp(self):
        super().setUp()
        from chitragupta.dashboard import server
        self.srv, self.thread = server.start_in_thread(0)
        self.port = self.srv.port

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def token(self):
        r, body = self.get("/")
        m = re.search(r'<meta name="cg-token" content="([^"]+)">', body.decode("utf-8"))
        self.assertTrue(m, "the page carries the write token")
        return m.group(1)

    def write(self, path, payload, method="PATCH", token=None, origin="default", ctype="application/json", host=None,
              raw=None):
        headers = {"Content-Type": ctype} if ctype else {}
        if origin == "default":
            origin = "http://127.0.0.1:%d" % self.port
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers["X-Chitragupta-Token"] = token
        body = raw if raw is not None else json.dumps(payload).encode()
        r, out = self.get(path, method=method, host=host, headers=headers, body=body)
        return r.status, (json.loads(out) if out else {})

    def test_settings_roundtrip_and_undo(self):
        tok = self.token()
        st, out = self.write("/api/settings", {"changes": {"jira_worklog.send_at": "20:45"}}, token=tok)
        self.assertEqual(st, 200, out)
        self.assertEqual(raw_cfg()["jira_worklog"]["send_at"], "20:45")
        r, body = self.get("/api/settings")
        items = {i["key"]: i for g in json.loads(body)["groups"] for i in g["items"]}
        self.assertEqual(items["jira_worklog.send_at"]["value"], "20:45")
        st, out = self.write("/api/settings/undo", {}, method="POST", token=tok)
        self.assertEqual(st, 200, out)
        self.assertNotIn("send_at", raw_cfg().get("jira_worklog", {}))
        st, out = self.write("/api/settings", {"changes": {"jira_worklog.send_at": "nope"}}, token=tok)
        self.assertEqual(st, 422)
        self.assertIn("jira_worklog.send_at", out["fields"])
        st, out = self.write("/api/settings", {"changes": {"github_remote": "x"}}, token=tok)
        self.assertEqual(st, 422)

    def test_every_check_rejects(self):
        tok = self.token()
        before = open(common.wl("config.json"), "rb").read()
        good = {"changes": {"jira_worklog.send_at": "20:10"}}
        cases = [
            ("no token", dict(token=None), 403),
            ("wrong token", dict(token="x" * 43), 403),
            ("no Origin", dict(token=tok, origin=None), 403),
            ("null Origin", dict(token=tok, origin="null"), 403),
            ("foreign Origin", dict(token=tok, origin="http://evil.test"), 403),
            ("other port Origin", dict(token=tok, origin="http://127.0.0.1:1"), 403),
            ("wrong Host", dict(token=tok, host="evil.test"), 403),
            ("form post", dict(token=tok, ctype="application/x-www-form-urlencoded"), 415),
            ("no content type", dict(token=tok, ctype=None), 415),
            ("not JSON", dict(token=tok, raw=b"{nope"), 400),
            ("JSON array", dict(token=tok, raw=b"[1,2]"), 400),
            ("oversized", dict(token=tok, raw=b'{"changes": {"x": "' + b"a" * 20000 + b'"}}'), 413),
        ]
        for name, kw, want in cases:
            st, _ = self.write("/api/settings", good, **kw)
            self.assertEqual(st, want, name)
        self.assertEqual(open(common.wl("config.json"), "rb").read(), before, "nothing was written")
        st, _ = self.write("/api/settings", good, method="PUT", token=tok)
        self.assertEqual(st, 405)
        st, _ = self.write("/api/actions/rm-rf", {}, method="POST", token=tok)
        self.assertEqual(st, 404)
        r, _ = self.get("/api/settings", method="OPTIONS", headers={"Origin": "http://evil.test"})
        self.assertEqual(r.status, 405)
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))

    def test_rate_limit(self):
        tok = self.token()
        self.srv.limiter.per_minute = 3
        codes = [self.write("/api/actions/capture-pause", {}, method="POST", token=tok)[0] for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])

    def test_actions_pause_resume_recorded(self):
        from chitragupta import configstore
        tok = self.token()
        self.assertEqual(self.write("/api/actions/capture-pause", {}, method="POST", token=tok)[0], 200)
        self.assertTrue(os.path.exists(common.wl("PAUSED")))
        self.assertEqual(self.write("/api/actions/capture-resume", {}, method="POST", token=tok)[0], 200)
        self.assertFalse(os.path.exists(common.wl("PAUSED")))
        keys = [h["key"] for h in configstore.history()]
        self.assertIn("action:capture-pause", keys)
        self.assertIn("action:capture-resume", keys)

    def test_live_mcp_check_is_doctors_check(self):
        from chitragupta import setup
        from chitragupta.dashboard import api
        calls = []
        real = setup.mcp_status

        def fake(cfg, probe=False):
            calls.append(probe)
            return {"jira": {"status": "connected", "server": "mcp-atlassian"}, "checked": common.now_iso(), "probed": probe}
        setup.mcp_status = fake
        try:
            st, out = self.write("/api/actions/mcp-check", {}, method="POST", token=self.token())
        finally:
            setup.mcp_status = real
        self.assertEqual(st, 200, out)
        self.assertEqual(calls, [True], "the same probe /cg:doctor runs")
        self.assertIsNotNone(out["seconds"])
        api._cache.clear()
        self.assertIsNotNone(api.mcp_live() is None or api.mcp_live().get("seconds") is not None)

    def test_token_changes_per_server_start(self):
        from chitragupta.dashboard import server
        other, _ = server.start_in_thread(0)
        try:
            self.assertNotEqual(other.page_token, self.srv.page_token)
        finally:
            other.shutdown()
            other.server_close()


if __name__ == "__main__":
    unittest.main()
