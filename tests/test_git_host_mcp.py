"""PR data through a Git host's read-only MCP server (hostmcp.py): adapters, source order, the one budgeted job per
repo, setup's classification, and the branch tracker using it.

Fixtures in tests/fixtures/git_host_mcp/ are hand-built stream-json transcripts in the shape the official servers
return (microsoft/azure-devops-mcp, github/github-mcp-server), with synthetic data only (acme.test, ABC-1001).
Nothing here runs a model or reaches a server: the `claude -p` process is replaced at the command guard.
"""
import json
import os
import unittest

from tests.support import ROOT, Isolated, rd, write_config
from tests import test_branch_tracking as tb

FIX = os.path.join(ROOT, "tests", "fixtures", "git_host_mcp")
ADO_INFO = {"host": "azure", "org": "acme", "project": "Shop", "repo": "shop-api"}
GH_INFO = {"host": "github", "path": "acme-test/shop-api"}
B1, B2, B3 = "feature/ABC-1001-x", "feature/ABC-1002-y", "feature/ABC-1003-z"
SERVERS = {"ado": {"type": "stdio", "command": "npx", "args": ["-y", "@azure-devops/mcp", "acme"]},
           "github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/",
                      "headers": {"Authorization": "Bearer ${GITHUB_PAT}"}},
           "sqldb": {"type": "http", "url": "https://db.acme.test/mcp"}}
CFG = {"mcp": {"git_host": {"azure-devops": {"server": "ado", "enabled": True},
                            "github": {"server": "github", "enabled": True}}}}


def stream(provider):
    return rd(os.path.join(FIX, provider + ".stream.jsonl"))


def expected(provider):
    return json.loads(rd(os.path.join(FIX, provider + ".expected.json")))


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.addCleanup(self.restore)
        from chitragupta import mcpjobs
        mcpjobs.claude_json = lambda: {"mcpServers": json.loads(json.dumps(SERVERS))}

    def cfg(self, extra=None):
        c = json.loads(json.dumps(CFG))
        c.update(extra or {})
        write_config(self.home, c)
        from chitragupta import common
        return common.load_config()


class Adapters(Case):
    def test_fixtures_map_to_the_cli_pr_record(self):
        from chitragupta import hostmcp, mcpjobs
        for provider, info in (("azure-devops", ADO_INFO), ("github", GH_INFO)):
            with self.subTest(provider=provider):
                res = mcpjobs.parse_stream(stream(provider))
                got = {r["number"]: r for r in hostmcp.records(provider, res, info) if r["source"] in (B1, B2)}
                exp = {r["number"]: r for r in expected(provider)}
                self.assertEqual(got, exp)
                keys = {"number", "title", "source", "target", "state", "review", "created_at", "merged_at",
                        "merge_commit", "head", "head_at_merge", "url", "src"}
                for r in got.values():
                    self.assertEqual(set(r), keys)
                    self.assertEqual(r["src"], "mcp:" + provider)
                # the failed list call (B3) is not counted as checked
                self.assertEqual(hostmcp.covered(provider, res, info, [B1, B2, B3]), [B1, B2])

    def test_payload_shapes(self):
        from chitragupta import hostmcp
        one = {"pullRequestId": 7}
        for raw in (json.dumps([one]), json.dumps({"result": json.dumps([one])}), [{"type": "text", "text": json.dumps(one)}],
                    "<external-content>\n%s\n</external-content>" % json.dumps(one), json.dumps({"value": [one]}),
                    "2 pull requests:\n" + json.dumps([one])):
            with self.subTest(raw=str(raw)[:40]):
                self.assertEqual(hostmcp.payload(raw), [one])
        self.assertEqual(hostmcp.payload("not json at all"), [])

    def test_ado_status_codes_and_github_states(self):
        from chitragupta import hostmcp
        for st, want in ((1, "open"), ("active", "open"), (2, "closed"), ("abandoned", "closed"), (3, "merged"),
                         ("completed", "merged")):
            self.assertEqual(hostmcp.ado_pr({"pullRequestId": 1, "status": st}, ADO_INFO)["state"], want)
        self.assertEqual(hostmcp.ado_pr({"pullRequestId": 1, "status": 1, "isDraft": True}, ADO_INFO)["state"], "draft")
        self.assertEqual(hostmcp.gh_pr({"number": 1, "state": "open", "draft": True}, GH_INFO)["state"], "draft")
        self.assertEqual(hostmcp.gh_pr({"number": 1, "state": "closed", "merged_at": "2026-10-09T08:15:00Z"},
                                       GH_INFO)["state"], "merged")


class SourceOrder(Case):
    def source(self, info, pref=None, cli=True, configured=True):
        from chitragupta import hosts
        real = hosts.cli_path
        hosts.cli_path = lambda cfg, name: ("C:/tools/%s.exe" % name) if cli else None
        try:
            return hosts.source_for(self.cfg() if configured else {}, info, pref)
        finally:
            hosts.cli_path = real

    def test_order(self):
        self.assertEqual(self.source(ADO_INFO), "az")                                    # CLI present -> CLI
        self.assertEqual(self.source(ADO_INFO, cli=False), "mcp:azure-devops")           # no CLI + MCP -> MCP
        self.assertEqual(self.source(GH_INFO, cli=False), "mcp:github")
        self.assertEqual(self.source(ADO_INFO, cli=False, configured=False), "merge-commit")  # neither -> git
        self.assertEqual(self.source({"host": "gitlab", "path": "acme/x"}, cli=False), "merge-commit")

    def test_explicit_choices(self):
        self.assertEqual(self.source(ADO_INFO, "mcp"), "mcp:azure-devops")
        self.assertEqual(self.source(ADO_INFO, "mcp", configured=False), "merge-commit")
        self.assertEqual(self.source(ADO_INFO, "cli", cli=False), "merge-commit")
        self.assertEqual(self.source(ADO_INFO, "cli"), "az")
        for p in ("git", "merge-commit", "none"):
            self.assertEqual(self.source(ADO_INFO, p), "merge-commit")
        self.assertEqual(self.source(ADO_INFO, "auto"), self.source(ADO_INFO, ""))

    def test_server_missing_from_claude_code(self):
        from chitragupta import mcpjobs
        mcpjobs.claude_json = lambda: {"mcpServers": {}}
        self.assertEqual(self.source(ADO_INFO, cli=False), "merge-commit")


class OneJob(Case):
    """hostmcp.fetch_prs through the real harness (mcpjobs.run); only the claude process is faked."""

    def setUp(self):
        super().setUp()
        from chitragupta import mcpjobs
        from chitragupta.safety import commands
        mcpjobs.run = self._real_mcp[0]  # the real harness for these tests; restore() puts the tripwire back
        real_exe, real_run = mcpjobs.claude_exe, commands.run
        self.addCleanup(setattr, mcpjobs, "claude_exe", real_exe)
        self.addCleanup(setattr, commands, "run", real_run)
        mcpjobs.claude_exe = lambda cfg: ["claude"]
        self.runs = []
        self.reply = None

        def fake_run(argv, purpose, cwd=None, input_text=None, timeout=120, env_extra=None, cfg=None):
            self.runs.append({"argv": list(argv), "purpose": purpose, "cwd": cwd, "env": dict(env_extra or {}),
                              "reason": commands.reason(argv, purpose, cwd=cwd, cfg=cfg)})
            return 0, self.reply, ""
        commands.run = fake_run

    def fetch(self, provider, info, health=None, branches=(B1, B2, B3), known=()):
        from chitragupta import hostmcp
        self.reply = stream(provider)
        self.health = {} if health is None else health
        if "GITHUB_PAT" not in os.environ:  # the server definition reads it; the faked claude never sends it anywhere
            os.environ["GITHUB_PAT"] = "test-token"
            self.addCleanup(os.environ.pop, "GITHUB_PAT", None)
        return hostmcp.fetch_prs(self.cfg(), info, provider, list(branches), list(known), self.health, day="2026-10-10")

    def test_one_guarded_call_per_repo(self):
        from chitragupta import common
        os.environ["GITHUB_PAT"] = "test-token"
        self.addCleanup(os.environ.pop, "GITHUB_PAT", None)
        for provider, info, srv in (("azure-devops", ADO_INFO, "ado"), ("github", GH_INFO, "github")):
            with self.subTest(provider=provider):
                self.runs = []
                got = self.fetch(provider, info, known=[812] if provider == "azure-devops" else [41])
                self.assertEqual(len(self.runs), 1)  # all due branches in ONE job
                run = self.runs[0]
                self.assertEqual(run["purpose"], "claude")
                self.assertIsNone(run["reason"])  # the command guard accepts the call as built
                a = run["argv"]
                self.assertIn("--strict-mcp-config", a)
                conf = common.read_json(a[a.index("--mcp-config") + 1])
                self.assertEqual(list(conf["mcpServers"]), [srv])  # only the host's server
                allowed = a[a.index("--allowedTools") + 1].split(",")
                from chitragupta.safety.mcp import GIT_HOST_READ
                self.assertEqual(sorted(t.split("__")[-1] for t in allowed), sorted(GIT_HOST_READ[provider]))
                self.assertIn('"mcp"]', a[a.index("--settings") + 1])  # the fail-closed guard, profile mcp
                denied = a[a.index("--disallowedTools") + 1]
                self.assertIn("mcp__%s__%s" % (srv, "repo_pull_request_write" if provider == "azure-devops"
                                               else "merge_pull_request"), denied)
                prompt = a[a.index("-p") + 1]
                for b in (B1, B2, B3):
                    self.assertIn(b, prompt)
                self.assertEqual(got["branches"], [B1, B2])
                self.assertEqual(sorted(p["number"] for ps in got["prs"].values() for p in ps),
                                 sorted(p["number"] for p in expected(provider)))
                self.assertEqual(self.health["mcp"]["calls"]["2026-10-10"], 1)
                allow = common.read_json(common.wl("mcp", "allow.json"))
                self.assertEqual(allow["servers"][srv]["role"], "git-host")
                if provider == "github":  # the hosted server also runs in its own read-only mode
                    self.assertEqual(conf["mcpServers"]["github"]["headers"]["X-MCP-Readonly"], "true")

    def test_budget_keeps_the_evening_reserve(self):
        from chitragupta import hostmcp
        from chitragupta.kbjobs import EVENING_RESERVE
        budget = 10
        with self.assertRaises(hostmcp.Deferred):
            self.fetch("github", GH_INFO, health={"mcp": {"calls": {"2026-10-10": budget - EVENING_RESERVE}}})
        self.assertEqual(self.runs, [])
        self.assertEqual(self.health["mcp"]["calls"]["2026-10-10"], budget - EVENING_RESERVE)
        self.fetch("github", GH_INFO, health={"mcp": {"calls": {"2026-10-10": budget - EVENING_RESERVE - 1}}})
        self.assertEqual(len(self.runs), 1)

    def test_not_configured_and_bad_names_never_call(self):
        from chitragupta import hostmcp
        with self.assertRaises(hostmcp.Deferred):
            self.fetch("github", {"host": "github", "path": "acme-test/shop api;x"})
        from chitragupta import mcpjobs
        mcpjobs.claude_json = lambda: {"mcpServers": {}}
        with self.assertRaises(hostmcp.Deferred):
            self.fetch("azure-devops", ADO_INFO)
        self.assertEqual(self.runs, [])


class Setup(Case):
    def cj(self, servers):
        from chitragupta import plugin
        os.makedirs(os.path.dirname(plugin.claude_json_path()), exist_ok=True)
        with open(plugin.claude_json_path(), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f)

    def test_detected_then_chosen(self):
        from chitragupta import common, setup
        from chitragupta.safety.mcp import GIT_HOST_READ
        self.cj(SERVERS)
        write_config(self.home, {"mcp": {"jira": {"enabled": True}}})
        st = setup.mcp_status(common.load_config(), listing={"ado": "connected", "github": "connected"})
        self.assertEqual(st["git_host"]["status"], "detected")
        self.assertEqual({(r["server"], r["provider"]) for r in st["git_host"]["servers"]},
                         {("ado", "azure-devops"), ("github", "github")})
        self.assertEqual(st["database"]["candidates"], ["sqldb"])  # Git-host servers aren't database candidates
        # an existing allow.json (Jira only) gets the host entry added, with exactly the classified reads
        common.write_json(common.wl("mcp", "allow.json"), {"servers": {"mcp-atlassian": {"read": ["jira_get_issue"]},
                                                                       "ado": {"read": ["repo_file", "repo_pull_request"]}}})
        res = setup.use_git_host("ado")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["provider"], "azure-devops")
        cfg = common.load_config()
        self.assertEqual(cfg["mcp"]["git_host"]["azure-devops"], {"server": "ado", "enabled": True})
        allow = common.read_json(common.wl("mcp", "allow.json"))
        self.assertEqual(allow["servers"]["mcp-atlassian"]["read"], ["jira_get_issue"])
        e = allow["servers"]["ado"]
        self.assertEqual((e["role"], e["provider"]), ("git-host", "azure-devops"))
        self.assertEqual(sorted(e["read"]), sorted(GIT_HOST_READ["azure-devops"]))
        self.assertNotIn("repo_file", e["read"])  # not a classified PR read: dropped
        self.assertIn("repo_pull_request_write", e["write"])
        st = setup.mcp_status(cfg, listing={"ado": "connected"})
        self.assertEqual(st["git_host"]["status"], "connected")

    def test_unknown_server_needs_a_provider(self):
        from chitragupta import setup
        self.cj({"mystery": {"type": "stdio", "command": "x"}})
        write_config(self.home, {})
        self.assertFalse(setup.use_git_host("mystery")["ok"])
        self.assertFalse(setup.use_git_host("absent")["ok"])
        self.assertTrue(setup.use_git_host("mystery", provider="github")["ok"])

    def test_detect_provider(self):
        from chitragupta import hostmcp
        self.assertEqual(hostmcp.detect_provider("x", {"command": "docker", "args": [
            "run", "-i", "--rm", "ghcr.io/github/github-mcp-server"]}), "github")
        self.assertEqual(hostmcp.detect_provider("x", {"command": "npx", "args": ["-y", "@azure-devops/mcp", "acme"]}),
                         "azure-devops")
        self.assertIsNone(hostmcp.detect_provider("tools", {"url": "https://tools.acme.test/mcp"}))


class Tracker(tb.TrackingCase):
    """The branch tracker with the GitHub MCP as the PR source: one job per repo per refresh."""

    def setUp(self):
        super().setUp()
        from chitragupta import mcpjobs
        mcpjobs.claude_json = lambda: {"mcpServers": json.loads(json.dumps(SERVERS))}
        # The remote must *look* like GitHub (host detection reads `remote get-url`), but nothing may leave the
        # machine: pushes go to the local bare origin, and git refuses every protocol but file.
        old = os.environ.get("GIT_ALLOW_PROTOCOL")
        os.environ["GIT_ALLOW_PROTOCOL"] = "file"
        self.addCleanup(lambda: os.environ.pop("GIT_ALLOW_PROTOCOL", None) if old is None
                        else os.environ.__setitem__("GIT_ALLOW_PROTOCOL", old))
        self.sb.git("remote", "set-url", "origin", "https://github.com/acme-test/shop-api.git")
        self.sb.git("remote", "set-url", "--push", "origin", self.sb.origin)
        self.jobs = []
        self.fail = False

        def fake_run(job, servers, reads, writes, task, schema, cfg, health, **k):
            self.jobs.append({"job": job, "servers": servers, "task": task})
            if self.fail:
                raise mcpjobs.JobError("server offline or signed out: github", skip=True)
            return mcpjobs.parse_stream(stream("github"))
        mcpjobs.run = fake_run

    def cfg(self, strategy=None, **repo):
        c = super().cfg(strategy, **repo)
        c["mcp"] = json.loads(json.dumps(CFG["mcp"]))
        return c

    def refresh(self, health):
        from chitragupta import gittrack
        from chitragupta.store import Store
        tr = gittrack.Tracker(self.cfg("qa-only", pr_source="mcp"), {}, Store(), now=tb.NOW, health=health)
        return tr.refresh(fetch=False)

    def test_one_job_per_repo_and_status_rows(self):
        sb = self.sb
        for b in (B1, B2):
            sb.branch(b, "origin/qa")
            sb.commit(b.split("/")[1][:8] + " work")
            sb.push(b)
        health = {}
        rx = self.refresh(health)
        self.assertEqual(len(self.jobs), 1)
        self.assertIn(B1, self.jobs[0]["task"])
        self.assertIn(B2, self.jobs[0]["task"])
        data = rx["shop-api"].data
        self.assertEqual(data.meta["pr_source"], "mcp:github")
        self.assertEqual(sorted(int(k) for k, p in data.prs.items() if p.get("src") == "mcp:github"), [37, 38, 41])
        self.assertEqual(data.prs["41"]["state"], "open")
        self.assertEqual(sorted(data.meta["pr_checked"]), [B1, B2])
        self.refresh(health)  # within pr_refresh_minutes: no second job
        self.assertEqual(len(self.jobs), 1)
        from chitragupta import gittrack
        row = next(r for r in gittrack.pr_source_rows(self.cfg("qa-only", pr_source="mcp")) if r["repo"] == "shop-api")
        self.assertEqual((row["pref"], row["source"]), ("mcp", "mcp:github"))
        self.assertTrue(row["refreshed_at"])
        self.assertEqual(row["mcp"]["server"], "github")
        self.assertIn("read-only MCP", row["label"])

    def test_failed_job_backs_off(self):
        self.sb.branch(B1, "origin/qa")
        self.sb.commit("ABC-1001 work")
        self.sb.push(B1)
        self.fail = True
        rx = self.refresh({})
        self.assertEqual(len(self.jobs), 1)
        self.assertIn("offline", rx["shop-api"].data.meta["pr_error"])
        self.refresh({})  # backing off: no new job
        self.assertEqual(len(self.jobs), 1)

    def test_without_the_agent_no_job(self):
        self.sb.branch(B1, "origin/qa")
        self.sb.commit("ABC-1001 work")
        self.sb.push(B1)
        rx = self.refresh(None)
        self.assertEqual(self.jobs, [])
        self.assertIn("background tick", rx["shop-api"].data.meta["pr_error"])


if __name__ == "__main__":
    unittest.main()
