"""Background MCP jobs: saved tool results, the Jira allowlist, server status, the daily budget, tick records and the
Slack sender's MCP config (stdlib unittest, synthetic data, no model / MCP / Slack calls)."""
import json
import os
import unittest
from unittest import mock

from tests.test_v010 import Case

JIRA = "mcp-atlassian"
DBS = "sqldb"


def stream(servers, calls=(), out=None):
    """A stream-json transcript: init with {server: status}, one tool_use + tool_result per (tool, result), a result."""
    ev = [{"type": "system", "subtype": "init", "tools": [], "mcp_servers": [
        dict({"name": n}, **(st if isinstance(st, dict) else {"status": st})) for n, st in servers.items()]}]
    for i, (tool, result) in enumerate(calls):
        ev.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t%d" % i, "name": tool,
                                                                 "input": {}}]}})
        ev.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t%d" % i,
                                                            "content": result}]}})
    ev.append({"type": "result", "subtype": "success", "result": json.dumps(out or {"done": True}),
               "usage": {"input_tokens": 10, "output_tokens": 2}, "total_cost_usd": 0.001})
    return "\n".join(json.dumps(e) for e in ev)


class Harness(Case):
    """mcpjobs.run for real; only the claude process is replaced at the command guard."""

    def setUp(self):
        super().setUp()
        from chitragupta import mcpjobs
        from chitragupta.safety import commands
        mcpjobs.run = self._real_mcp[0]  # restore() puts the tripwire back
        real_exe, real_run = mcpjobs.claude_exe, commands.run
        self.addCleanup(setattr, mcpjobs, "claude_exe", real_exe)
        self.addCleanup(setattr, commands, "run", real_run)
        mcpjobs.claude_exe = lambda cfg: ["claude"]
        mcpjobs.claude_json = lambda: {"mcpServers": {JIRA: {"type": "stdio", "command": "uvx"},
                                                      DBS: {"type": "http", "url": "https://db.acme.test/mcp"}}}
        self.runs, self.replies = [], []

        def fake_run(argv, purpose, cwd=None, input_text=None, timeout=120, env_extra=None, cfg=None):
            self.runs.append({"argv": list(argv), "reason": commands.reason(argv, purpose, cwd=cwd, cfg=cfg)})
            return self.replies.pop(0) if self.replies else (0, stream({}), "")
        commands.run = fake_run

    def jcfg(self, **mcp):
        from chitragupta import common
        cfg = json.loads(json.dumps(common.load_config()))
        cfg.setdefault("mcp", {}).setdefault("jira", {}).update(enabled=True)
        cfg["mcp"].setdefault("database", {}).update(enabled=True, server=DBS, connections={"qa": "qa-shop"})
        cfg["mcp"].update(mcp)
        return cfg

    def job(self, health, server=JIRA, reply=None, cfg=None):
        from chitragupta import mcpjobs
        if reply is not None:
            self.replies.append(reply)
        return mcpjobs.run("probe-job", {server: ["jira_get_issue"]}, [mcpjobs.mcp_tool(server, "jira_get_issue")], [],
                           "task", {"type": "object"}, cfg or self.jcfg(), health, day="2026-10-16")


class SavedToolResult(Case):
    """FX-14: Claude Code moved a large tool result to a file; a refused delete must not lose the text."""

    def test_text_survives_a_refused_delete(self):
        from chitragupta import mcpjobs
        from chitragupta.safety import paths
        from chitragupta.safety.log import Blocked
        p = os.path.join(self.home, "saved-result.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"issues": [{"key": "ABC-1001"}]}')
        content = "Output too large. Full output saved to: %s" % p
        with mock.patch.object(paths, "delete", side_effect=Blocked("path", "outside the write roots")):
            self.assertEqual(mcpjobs._persisted(content), '{"issues": [{"key": "ABC-1001"}]}')
        self.assertTrue(os.path.exists(p))
        self.assertEqual(mcpjobs._persisted(content), '{"issues": [{"key": "ABC-1001"}]}')  # deleted this time
        self.assertFalse(os.path.exists(p))


class JiraAllowlist(Case):
    """FX-13: the Jira entry is filed under the configured server name and refreshed from the code every day."""

    def cfg(self, server=None):
        from chitragupta import common
        cfg = json.loads(json.dumps(common.load_config()))
        cfg.setdefault("mcp", {}).setdefault("jira", {}).update(enabled=True)
        if server:
            cfg["mcp"]["jira"]["server"] = server
        return cfg

    def test_renamed_server_gets_the_reads(self):
        from chitragupta import common, mcpjobs
        common.write_json(common.wl("mcp", "allow.json"),
                          {"servers": {"mcp-atlassian": {"read": ["jira_get_issue"], "write": []}}})
        allow = mcpjobs.ensure_files(self.cfg("jira-acme"))
        self.assertEqual(allow["servers"]["jira-acme"]["read"], mcpjobs.JIRA_ALLOW_DEFAULT["read"])
        self.assertIn("jira_transition_issue", allow["servers"]["jira-acme"]["write"])
        self.assertIn("jira-acme", mcpjobs.allow_default(self.cfg("jira-acme"))["servers"])

    def test_existing_file_gets_new_reads_and_keeps_extra_writes(self):
        from chitragupta import common, mcpjobs
        common.write_json(common.wl("mcp", "allow.json"), {"servers": {"mcp-atlassian": {
            "read": ["jira_get_issue"], "write": ["jira_some_new_write"]}}})
        allow = mcpjobs.ensure_files(self.cfg())
        e = allow["servers"]["mcp-atlassian"]
        self.assertEqual(e["read"], mcpjobs.JIRA_ALLOW_DEFAULT["read"])
        self.assertIn("jira_some_new_write", e["write"])
        self.assertIn("jira_add_worklog", e["write"])

    def test_read_only_mode_under_the_configured_name(self):
        from chitragupta import common, mcpjobs
        mcpjobs.claude_json = lambda: {"mcpServers": {"jira-acme": {"type": "stdio", "command": "uvx",
                                                                    "env": {"JIRA_URL": "https://jira.acme.test"}}}}
        path, _ = mcpjobs.job_config("jira-acme", ["jira_get_issue"], "jira-card-ABC-1001", self.cfg("jira-acme"))
        env = common.read_json(path)["mcpServers"]["jira-acme"]["env"]
        self.assertEqual((env["READ_ONLY_MODE"], env["ENABLED_TOOLS"]), ("true", "jira_get_issue"))

    def test_get_write_request_is_not_a_read(self):
        from chitragupta import common, mcpjobs
        self.assertNotIn("get_write_request", mcpjobs.DATABASE_ALLOW_DEFAULT["read"])
        common.write_json(common.wl("mcp", "allow.json"), {"servers": {"sqldb": {
            "read": ["run_query", "get_write_request"], "write": [], "query_tools": ["run_query"]}}})
        cfg = self.cfg()
        cfg["mcp"].setdefault("database", {}).update(enabled=True, server="sqldb")
        allow = mcpjobs.ensure_files(cfg)
        self.assertEqual(allow["servers"]["sqldb"]["read"], ["run_query"])


class ServerStatus(Harness):
    """FX-10: unreachable / signed out / missing are told apart, keep their error text, and a later success clears them."""

    def test_unreachable_then_signed_out_then_cleared(self):
        from chitragupta import mcpjobs
        h = {"mcp": {"skip": {"jira": "2026-10-16T09:00:00+05:30"}, "last_skip_reason": {"jira": "x"},
                     "offline": {"jira-card-ABC-1001": "missing env JIRA_TOKEN"}}}
        with self.assertRaises(mcpjobs.JobError) as cm:
            self.job(h, reply=(1, stream({JIRA: "failed"}), "Error: connect ETIMEDOUT 10.0.0.1:443\n"))
        self.assertIn("unreachable", str(cm.exception))
        st = h["mcp"]["server_state"][JIRA]
        self.assertEqual(st["state"], "unreachable")
        self.assertIn("ETIMEDOUT", st["error"])
        self.assertTrue(st["at"])
        self.assertIn(JIRA, h["mcp"]["offline"])
        self.assertIn("probe-job", h["mcp"]["job_errors"])
        with self.assertRaises(mcpjobs.JobError) as cm:
            self.job(h, reply=(0, stream({JIRA: "needs-auth"}), ""))
        self.assertIn("signed-out", str(cm.exception))
        self.assertEqual(h["mcp"]["server_state"][JIRA]["state"], "signed-out")
        self.job(h, reply=(0, stream({JIRA: "connected"}, [("mcp__%s__jira_get_issue" % JIRA, "{}")]), ""))
        m = h["mcp"]
        self.assertNotIn(JIRA, m["offline"])
        self.assertNotIn("jira-card-ABC-1001", m["offline"])  # an older version's job-keyed entry goes too
        self.assertNotIn(JIRA, m["server_state"])
        self.assertNotIn("jira", m["skip"])
        self.assertNotIn("jira", m["last_skip_reason"])
        self.assertNotIn("probe-job", m["job_errors"])

    def test_missing_env_is_a_job_error_not_a_server_key(self):
        from chitragupta import mcpjobs
        mcpjobs.claude_json = lambda: {"mcpServers": {JIRA: {"type": "http", "url": "https://jira.acme.test/mcp",
                                                             "headers": {"Authorization": "Bearer ${ACME_JIRA_PAT}"}}}}
        os.environ.pop("ACME_JIRA_PAT", None)
        h = {}
        with mock.patch.object(mcpjobs, "user_env", return_value=None), self.assertRaises(mcpjobs.JobError):
            self.job(h)
        self.assertEqual(self.runs, [])
        self.assertEqual(list(h["mcp"]["offline"]), [JIRA])  # keyed by server only
        self.assertEqual(h["mcp"]["server_state"][JIRA]["state"], "signed-out")
        self.assertIn("ACME_JIRA_PAT", h["mcp"]["job_errors"]["probe-job"]["error"])

    def test_mcp_list_errors_and_old_cache(self):
        from chitragupta import doctor, setup
        from tests.test_setup import MCP_LIST
        self.assertEqual(setup.parse_mcp_list(MCP_LIST)["sqldb"], "unreachable")
        self.assertEqual(setup.parse_mcp_errors(MCP_LIST)["sqldb"], "Failed to connect")
        self.assertEqual(setup.parse_mcp_errors(MCP_LIST)["claude.ai Slack"], "Needs authentication")
        old = {"slack": {"status": "missing"}, "jira": {"status": "offline", "server": JIRA, "detail": "server (user scope)"},
               "database": {"status": "signed out", "server": DBS, "detail": "server (user scope)",
                            "error": "Needs authentication", "error_at": "2026-10-16T09:00:00+05:30"}}
        health = {"mcp": {"server_state": {JIRA: {"state": "unreachable", "error": "connect ETIMEDOUT",
                                                  "at": "2026-10-16T08:00:00+05:30"}}}}
        rows = {r["name"]: r for r in doctor.mcp_rows(old, self.jcfg(), health)}
        self.assertFalse(rows["Jira"]["ok"])
        self.assertTrue(rows["Jira"]["detail"].startswith("unreachable: "))
        self.assertIn("connect ETIMEDOUT", rows["Jira"]["detail"])
        self.assertIn("Needs authentication (09:00)", rows["database MCP"]["detail"])
        self.assertIn("/mcp", rows["database MCP"]["detail"])

    def test_status_cache_keeps_the_error(self):
        from chitragupta import plugin, setup
        from tests.test_setup import MCP_LIST
        os.makedirs(os.path.dirname(plugin.claude_json_path()), exist_ok=True)
        with open(plugin.claude_json_path(), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {DBS: {"type": "http", "url": "https://db.acme.test/mcp"}}}, f)
        with mock.patch.object(setup, "_claude_exe", return_value=["claude"]), \
                mock.patch.object(setup, "_run", return_value=(0, MCP_LIST, "")):
            st = setup.mcp_status({"mcp": {"database": {"server": DBS, "enabled": True}}}, probe=True)
        self.assertEqual((st["database"]["status"], st["database"]["error"]), ("unreachable", "Failed to connect"))
        self.assertEqual(st["database"]["error_at"], st["checked"])

    def test_dashboard_reads_old_and_new_health(self):
        from chitragupta.dashboard import api
        m = api._mcp({"mcp": {"offline": {"jira-card-ABC-1001": "missing env X", JIRA: "2026-10-16T09:00:00+05:30"},
                              "jobs": {"jira-card-ABC-1001": {"at": "2026-10-16T09:00:00+05:30",
                                                              "servers": {JIRA: "failed"}}},
                              "server_state": {JIRA: {"state": "unreachable", "error": "connect ETIMEDOUT",
                                                      "at": "2026-10-16T09:00:00+05:30"}}}}, self.jcfg(), "2026-10-16")
        self.assertEqual(list(m["offline"]), [JIRA])
        srv = m["servers"][0]
        self.assertEqual((srv["state"], srv["error"]), ("unreachable", "connect ETIMEDOUT"))
        self.assertEqual(api._mcp({"mcp": {"offline": {"a,b": "2026-10-16T09:00:00+05:30"}}}, self.jcfg(),
                                  "2026-10-16")["offline"], {"a,b": "2026-10-16T09:00:00+05:30"})


class Budget(Harness):
    """FX-9: a unit only for a run that made an MCP call; offline servers get one probe per OFFLINE_PROBE_S; a Slack
    retry reuses the evening facts fetched minutes before."""

    def test_a_run_without_mcp_calls_costs_no_unit(self):
        from chitragupta import mcpjobs
        h = {}
        self.job(h, reply=(0, stream({JIRA: "connected"}), ""))
        self.assertEqual(int((h["mcp"].get("calls") or {}).get("2026-10-16") or 0), 0)
        self.assertEqual(h["mcp"]["empty"]["2026-10-16"], 1)
        with self.assertRaises(mcpjobs.JobError):
            self.job(h, reply=(1, stream({JIRA: "failed"}), "connect ECONNREFUSED"))
        self.assertEqual(int((h["mcp"].get("calls") or {}).get("2026-10-16") or 0), 0)
        self.job(h, reply=(0, stream({JIRA: "connected"}, [("mcp__%s__jira_get_issue" % JIRA, "{}")]), ""))
        self.assertEqual(h["mcp"]["calls"]["2026-10-16"], 1)
        self.assertEqual(h["mcp"]["jobs"]["probe-job"]["calls"], 1)

    def test_empty_runs_have_their_own_cap(self):
        from chitragupta import mcpjobs
        cfg = self.jcfg(daily_call_budget=10)
        h = {"mcp": {"empty": {"2026-10-16": 4}}}
        self.assertEqual(mcpjobs.budget_left(h, cfg, "2026-10-16"), 6)
        h = {"mcp": {"empty": {"2026-10-16": 10}, "calls": {"2026-10-16": 1}}}
        self.assertEqual(mcpjobs.budget_left(h, cfg, "2026-10-16"), 0)


class OfflineProbe(Case):
    def _agent(self, health_mcp=None):
        a = self.agent()
        a.cfg["mcp"]["database"].update(enabled=True, server="sqldb", connections={"qa": "qa-shop"})
        a.cfg["mcp"]["jira"]["enabled"] = False
        a.health["mcp"] = health_mcp or {}
        self.ran = []

        def job(name):
            def go(*x, **k):
                self.ran.append(name)
                a.jobs._skipped("database", "server sqldb unreachable: connect ECONNREFUSED")
                from chitragupta import mcpjobs
                mcpjobs.server_down(a.health, "sqldb", "unreachable", "connect ECONNREFUSED")
                return 0
            return go
        a.jobs.schema_cards = job("schema")
        a.jobs.qa_traces = job("trace")
        a.jobs.link_map = job("linkmap")
        return a

    def tick(self, a):
        from chitragupta import mcpjobs
        with mock.patch.object(mcpjobs, "ensure_files"):
            a.jobs.tick()

    def test_one_failure_stops_the_rest_of_the_tick(self):
        self.clock("2026-10-16", 10, 0)
        a = self._agent()
        self.tick(a)
        self.assertEqual(self.ran, ["schema"])

    def test_offline_server_gets_one_probe_per_interval(self):
        from chitragupta import kbjobs
        t = self.clock("2026-10-16", 10, 0)
        a = self._agent({"offline": {"sqldb": "2026-10-16T09:30:00" + t.isoformat()[19:]}})
        self.tick(a)
        self.assertEqual(self.ran, ["schema"])  # the probe
        self.ran.clear()
        a.health["mcp"].pop("skip", None)  # even without the skip mark, the probe interval holds
        self.clock("2026-10-16", 11, 0)
        self.tick(a)
        self.assertEqual(self.ran, [])
        self.clock("2026-10-16", 10 + kbjobs.OFFLINE_PROBE_S // 3600, 1)
        a.health["mcp"].pop("skip", None)
        self.tick(a)
        self.assertEqual(self.ran, ["schema"])

    def test_cached_status_offline_skips_until_a_job_succeeds_later(self):
        from chitragupta import common, plugin
        self.clock("2026-10-16", 10, 0)
        common.write_json(plugin.mcp_status_cache(), {"checked": "2026-10-16T09:50:00+05:30", "database": {
            "status": "unreachable", "server": "sqldb", "error": "Failed to connect",
            "error_at": common.now_iso()}})
        a = self._agent({"probe": {"database": common.now_iso()}})
        self.tick(a)
        self.assertEqual(self.ran, [])  # cached offline and probed just now
        a.health["mcp"]["last_ok"] = {"sqldb": "2026-10-16T10:30:00+05:30"}  # a later job reached it
        self.clock("2026-10-16", 10, 40)
        self.tick(a)
        self.assertEqual(self.ran, ["schema"])


class SlackRetry(Case):
    def test_retry_reuses_recent_facts_and_is_labelled(self):
        from chitragupta import common, jiraworklog, mcpjobs
        self.clock("2026-10-16", 20, 0)
        self.raw("2026-10-16", "ABC-1001", 10, 0)
        a = self.agent()
        a.run_extractions = lambda *x, **k: None
        a.cfg["mcp"]["jira"].update(enabled=True)
        a.cfg["jira_worklog"] = {"slack": {"self_user_id": "U000TEST", "send_tool": "mcp__slack__slack_send_message"}}
        calls = []

        def evening(day, tickets, cfg, health):
            calls.append(common.now_iso())
            facts = {"comments": [], "worklogs": [], "status_changes": []}
            common.write_json(common.hp("data", "jira", "_evening", day + ".json"),
                              {"day": day, "fetched_at": common.now_iso(), "facts": facts})
            return facts
        jiraworklog.slack.send = lambda cfg, text, health: {"ok": False, "error": "slack_send_message tool is not available"}
        with mock.patch.object(mcpjobs, "jira_evening", side_effect=evening):
            a.jwl.tick()
            self.clock("2026-10-16", 20, 5)
            a.jwl.tick()
            self.clock("2026-10-16", 20, 50)
            a.jwl.tick()
        self.assertEqual(len(calls), 2)  # 20:00 and 20:50; the 20:05 retry reused the 20:00 facts
        sh = jiraworklog.load_sheet("2026-10-16")
        self.assertEqual([v["why"] for v in sh["versions"]], ["first", "retry", "retry"])
        self.assertTrue(sh["versions"][1]["jira_facts"])
        self.assertEqual(sh["attempts"], 3)


class TickRecords(Case):
    """FX-11: every background run leaves a record, busy skips included; a stale-tick warning when events arrive
    but nothing processes them."""

    def test_busy_skip_and_ok_runs_are_recorded(self):
        from chitragupta import agent, ticklog
        self.hold_agent_lock()
        self.assertEqual(agent.cmd_tick(), 0)
        self.assertEqual([r["outcome"] for r in ticklog.recent()], ["skipped-busy"])
        self._held.pop().release()
        with open(ticklog.path(), "a", encoding="utf-8") as f:
            for _ in range(ticklog.KEEP + 30):
                f.write(json.dumps({"at": "2026-10-16T09:00:00+05:30", "outcome": "ok", "ms": 1}) + "\n")
        with mock.patch.object(agent.Agent, "tick", return_value=None):
            self.assertEqual(agent.cmd_tick(), 0)
        rec = ticklog.recent(1000)
        self.assertEqual(len(rec), ticklog.KEEP)  # trimmed by the run that held the lock
        self.assertEqual(rec[-1]["outcome"], "ok")
        self.assertIn("ms", rec[-1])
        with mock.patch.object(agent.Agent, "tick", side_effect=RuntimeError("boom")):
            self.assertEqual(agent.cmd_tick(), 1)
        self.assertEqual(ticklog.recent()[-1]["outcome"], "error")

    def _stale_setup(self, ok_minutes_ago):
        import datetime as dt
        from chitragupta import common, ticklog
        now = common.now()
        raw = common.wl("raw", now.date().isoformat(), "s1.jsonl")
        os.makedirs(os.path.dirname(raw), exist_ok=True)
        with open(raw, "w", encoding="utf-8") as f:
            f.write("{}\n")
        ok = (now - dt.timedelta(minutes=ok_minutes_ago)).isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(ticklog.path()), exist_ok=True)
        for m in (20, 10):
            with open(ticklog.path(), "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": (now - dt.timedelta(minutes=m)).isoformat(timespec="seconds"),
                                    "outcome": "skipped-busy", "ms": 0}) + "\n")
        return {"last_tick_ok": ok, "last_tick": ok}

    def test_stale_warning_everywhere(self):
        from chitragupta import common, doctor, hook, ticklog
        from chitragupta.dashboard import api
        h = self._stale_setup(45)
        w = ticklog.stale_warning(h)
        self.assertIn("no background run has succeeded since", w)
        self.assertIn("skipped-busy x2", w)
        self.assertIn(w, hook._health_warning(h, common))
        self.assertFalse(doctor.tick_row(h)["ok"])
        common.write_json(common.wl("health.json"), h)
        st = api.status()
        self.assertIn(w, st["warnings"])
        self.assertEqual(st["tick"]["last_hour"], {"skipped-busy": 2})

    def test_no_warning_after_a_recent_good_run(self):
        from chitragupta import doctor, ticklog
        h = self._stale_setup(10)
        self.assertEqual(ticklog.stale_warning(h), "")
        row = doctor.tick_row(h)
        self.assertTrue(row["ok"])
        self.assertIn("2 run(s) skipped in the last hour", row["detail"])


if __name__ == "__main__":
    unittest.main()
