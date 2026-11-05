"""Regression tests for the implementation review's findings F01-F14: one personal projection on every read surface,
assignment evidence bound to the confirmed Jira accounts, several accounts on one host, Jira scope pages that are
checked and merged without false absence, UAT evidence bound to the changes now in UAT, development versus
integration branches and PRs, canonical repository identity, literal git author filters, source status carried into
records / milestones / next steps, and one profile day for progress and releases. Synthetic data only: developer A
(developer@acme.test, Jira acct-a), developer B (colleague@acme.test, Jira acct-b), both displayed as "Dev Example";
ABC-… tickets; temp homes and temp git repositories."""
import copy
import datetime as dt
import json
import os
import shutil
import unittest
from unittest import mock

from tests.support import Isolated, write_config
from tests.test_profile import git, commit

ME, OTHER, LOOK = "developer@acme.test", "colleague@acme.test", "xdeveloper@acme.test"
A, B = "acct-a", "acct-b"
UTC = dt.timezone.utc
JIRA_A = {"provider": "jira", "instance": "acme.atlassian.net", "id": A}
JIRA_B = {"provider": "jira", "instance": "acme.atlassian.net", "id": B}


class Home(Isolated):
    def setUp(self):
        self.isolate()
        self.repo = os.path.join(self.home, "work", "service-api")
        os.makedirs(self.repo)
        self.cfg = {"watched_repos": [self.repo], "git_author_emails": [ME], "ticket_prefixes": ["ABC"],
                    "day_starts_at": "04:00",
                    "profile": {"display_name": "Dev Example", "accounts": [JIRA_A], "jira_projects": ["ABC"]}}
        write_config(self.home, self.cfg)

    def config(self, **kw):
        for k, v in kw.items():
            if k == "profile":
                self.cfg["profile"] = dict(self.cfg.get("profile") or {}, **v)
            else:
                self.cfg[k] = v
        write_config(self.home, self.cfg)
        from chitragupta import common
        return common.load_config()

    def ticket(self, key, rec):
        from chitragupta import common
        common.write_json(common.hp("data", "tickets", key + ".json"), dict({"id": key}, **rec))


# --------------------------------------------------------------------------- F01 / F03: one personal projection

class Projection(Home, unittest.TestCase):
    """A two-person dataset: A's own, reassigned, included and Epic-parent tickets beside a ticket that is only B's."""

    def setUp(self):
        super().setUp()
        self.ticket("ABC-1000", {"title": "Checkout epic", "title_source": "jira"})  # no work of A's: context only
        self.ticket("ABC-1001", {"title": "Apply the discount once", "title_source": "jira", "first": "2026-11-01",
                                 "last": "2026-11-05", "timeline": {"2026-11-05": {"minutes": 40}},
                                 "jira": {"parent": "ABC-1000"},
                                 "ownership": {"account": A, "state": "you", "checked_at": "2026-11-05T09:00:00+00:00",
                                               "history": [{"at": "2026-11-01T09:00:00+00:00", "state": "you", "account": A}]}})
        self.ticket("ABC-1500", {"title": "Invoice rounding", "title_source": "jira", "timeline": {"2026-10-20": {"minutes": 30}},
                                 "ownership": {"account": A, "state": "other",
                                               "history": [{"at": "2026-10-01T09:00:00+00:00", "state": "you", "account": A},
                                                           {"at": "2026-10-25T09:00:00+00:00", "state": "other", "account": A}]}})
        self.ticket("ABC-1600", {"title": "Checkout contract", "title_source": "jira",
                                 "ownership": {"account": A, "state": "other"}})  # included by A below, no work yet
        # B's ticket: B's commit only, the same display name, a marker that must never reach A's surfaces
        self.ticket("ABC-1999", {"title": "Another developer only", "title_source": "jira", "first": "2026-11-02",
                                 "last": "2026-11-04", "tldr": "OUTSIDER-SYNTHETIC-DETAIL",
                                 "ownership": {"account": A, "state": "other"},
                                 "commits": {"a" * 40: {"sha": "a" * 40, "ae": OTHER, "repo_name": "service-api",
                                                        "subject": "ABC-1999 OUTSIDER-SYNTHETIC-DETAIL"}},
                                 "items": {}})
        self.cfg_now = self.config(profile={"include_tickets": {"ABC-1600": "pairing on the contract"}})

    def test_list_and_detail_apis(self):
        from chitragupta.dashboard import api
        listed = {t["key"]: t for t in api.tickets()["tickets"]}
        self.assertEqual(sorted(listed), ["ABC-1000", "ABC-1001", "ABC-1500", "ABC-1600"])
        self.assertEqual(listed["ABC-1001"]["scope"]["basis"], "current")
        self.assertEqual(listed["ABC-1500"]["scope"]["basis"], "history")
        self.assertEqual(listed["ABC-1600"]["scope"]["basis"], "current")
        self.assertEqual(listed["ABC-1000"]["scope"]["basis"], "context")
        self.assertEqual(api.tickets()["outside"], 1)
        # asking by key, or by a search term, doesn't make it A's
        self.assertEqual(api.tickets("OUTSIDER")["tickets"], [])
        t = api.ticket("ABC-1999")
        self.assertTrue(t["outside"])
        self.assertNotIn("OUTSIDER", json.dumps(t))
        self.assertNotIn("Another developer", json.dumps(t))
        for section in api.DETAIL_SECTIONS:
            self.assertIsNone(api.ticket_detail("ABC-1999", section))
        self.assertEqual(api.ticket("ABC-1001")["scope"]["basis"], "current")

    def test_mcp_cli_search_export_share_and_alerts(self):
        import io
        from contextlib import redirect_stdout
        from chitragupta import cli, export, mcpserver, searchidx, share
        cfg = self.cfg_now
        with self.assertRaises(mcpserver.ToolError) as e:
            mcpserver.tool_get_ticket({"key": "ABC-1999"}, cfg)
        self.assertIn("outside your personal scope", str(e.exception))
        self.assertNotIn("OUTSIDER", str(e.exception))
        with self.assertRaises(mcpserver.ToolError):
            mcpserver.tool_get_branch_status({"key": "ABC-1999"}, cfg)
        listing = mcpserver.tool_list_tickets({}, cfg)
        self.assertNotIn("ABC-1999", listing)
        self.assertIn("ABC-1001", listing)
        self.assertNotIn("ABC-1999", mcpserver.tool_get_alerts({}, cfg))
        res = searchidx.search("OUTSIDER-SYNTHETIC-DETAIL")
        self.assertEqual(res["total"], 0)
        res = searchidx.search("OUTSIDER", ticket="ABC-1999")  # a ticket filter doesn't open it either
        self.assertEqual(res["total"], 0)
        self.assertGreater(searchidx.search("discount")["total"], 0)  # A's own work is still found
        self.assertIsNone(export.snapshot("ABC-1999"))
        with self.assertRaises(share.ShareError):
            share.build("ABC-1999", cfg=cfg)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["ticket", "ABC-1999"])
            cli.main(["branches", "ABC-1999"])
        self.assertIn("outside your personal scope", buf.getvalue())
        self.assertNotIn("OUTSIDER", buf.getvalue())

    def test_profile_changes_take_effect_on_the_next_read(self):
        from chitragupta.dashboard import api
        self.assertFalse(api.scope_of("ABC-1999")["visible"])
        self.config(profile={"include_tickets": {"ABC-1600": "pairing", "ABC-1999": "reviewing it together"}})
        self.assertTrue(api.scope_of("ABC-1999")["visible"])
        self.assertEqual(api.scope_of("ABC-1999")["basis"], "current")
        # including a collaborative ticket doesn't make its other author's commits A's
        from chitragupta import profile, common
        pol = profile.Policy(common.load_config())
        self.assertNotIn("your commits", pol.own_evidence(api._ticket_records()["ABC-1999"]))
        self.config(profile={"include_tickets": {}})
        self.assertFalse(api.scope_of("ABC-1999")["visible"])

    def test_legacy_unverifiable_commits_are_quarantined_with_a_backup(self):
        from chitragupta import common, profile
        from chitragupta.store import Store
        self.ticket("ABC-1001", dict(common.read_json(common.hp("data", "tickets", "ABC-1001.json")),
                                     commits={"b" * 40: {"sha": "b" * 40, "repo_name": "gone-repo", "subject": "old"}}))
        state = {"profile_sig": profile.signature(common.load_config()), "profile_policy": 1}
        st = Store()
        res = profile.reconcile(st, common.load_config(), state)
        st.save()
        tk = common.read_json(common.hp("data", "tickets", "ABC-1001.json"))
        self.assertNotIn("b" * 40, tk.get("commits") or {})
        self.assertIn("couldn't be verified", tk["commits_excluded"]["b" * 40]["excluded"]["reason"])
        self.assertTrue(res["backup"])
        self.assertIsNone(profile.reconcile(Store(), common.load_config(), state))  # idempotent


# --------------------------------------------------------------------------- F02: identity change

class IdentityChange(Home, unittest.TestCase):
    def test_old_account_evidence_is_unknown_until_read_again(self):
        from chitragupta import common, profile, release
        tk = {"id": "ABC-1001", "ownership": {"account": A, "state": "you"},
              "release": {"assignee_me": True, "assignee_binding": A, "v": 1}}
        legacy = {"id": "ABC-1002", "release": {"assignee_me": True, "v": 1}}  # an unbound answer from before
        self.assertEqual(profile.Policy(common.load_config()).ticket_state("ABC-1001", tk)[0], "assigned")
        self.assertNotEqual(profile.Policy(common.load_config()).ticket_state("ABC-1002", legacy)[0], "assigned")
        for prof in ({"accounts": [JIRA_B]}, {"accounts": []}):
            cfg = self.config(profile=prof)
            pol = profile.Policy(cfg)
            self.assertNotEqual(pol.ticket_state("ABC-1001", tk)[0], "assigned", prof)
            self.assertIsNone(pol.release_assignee(tk["release"]))
            self.assertFalse(release.mine(tk, cfg, key="ABC-1001")[0] and
                             release.mine(tk, cfg, key="ABC-1001")[1] == "assigned to you")

    def test_scope_rows_of_another_account_are_not_yours(self):
        from chitragupta import common, release
        cfg = self.config(releases={"enabled": True})
        v = {"id": "10", "name": "November", "releaseDate": "2026-11-15"}
        release.save_scope([{"key": "ABC-1005", "fields": {"fixVersions": [v], "assignee": {"accountId": A},
                                                           "status": {"name": "To Do"}}}], "x", cfg, "2026-11-05T09:00:00+00:00")
        now = dt.datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
        ov = release.month_overview("2026-11", cfg, now=now)
        self.assertIn("ABC-1005", [r["key"] for g in ov["groups"] for r in g["tickets"]])
        cfg = self.config(profile={"accounts": [JIRA_B]})
        self.assertEqual(release.jira_facts("ABC-1005", cfg)["assignee_me"], None)
        ov = release.month_overview("2026-11", cfg, now=now)
        self.assertNotIn("ABC-1005", [r["key"] for g in ov["groups"] for r in g["tickets"]])
        self.assertIn("other Jira accounts", ov["scope"]["partial_reason"])

    def test_adding_a_second_jira_account_keeps_your_evidence(self):
        from chitragupta import profile
        tk = {"ownership": {"account": A, "state": "you", "history": [{"at": "2026-10-01T00:00:00+00:00", "state": "you",
                                                                         "account": A}]}}
        pol = profile.Policy(self.config(profile={"accounts": [JIRA_A, JIRA_B]}))
        self.assertEqual(pol.ticket_state("ABC-1001", tk)[0], "assigned")  # {A} is still within your accounts
        other = {"ownership": {"account": A, "state": "other"}}
        self.assertEqual(pol.ticket_state("ABC-1001", other)[0], "unknown")  # B might be the assignee: read again
        self.assertTrue(pol.person_mine({"accountId": B}))


# --------------------------------------------------------------------------- F14: several accounts on one host

class MultiAccount(Home, unittest.TestCase):
    def test_two_accounts_on_github_com_coexist_and_a_third_person_stays_out(self):
        from chitragupta import activity, common, profile, settings
        accts = [{"provider": "github", "instance": "github.com", "id": "101", "login": "dev-work"},
                 {"provider": "github", "instance": "github.com", "id": "102", "login": "dev-old"}]
        clean, err = settings.validate({"profile.accounts": accts + [JIRA_A]})
        self.assertEqual(err, {})
        cfg = self.config(profile={"accounts": accts + [JIRA_A]})
        pol = profile.Policy(cfg)
        self.assertEqual(len(pol.accounts("github", "github.com")), 2)
        self.assertTrue(pol.pr_mine({"id": "101"}, "github", "github.com"))
        self.assertTrue(pol.pr_mine({"login": "Dev-Old"}, "github", "github.com"))
        self.assertFalse(pol.pr_mine({"id": "103", "login": "colleague"}, "github", "github.com"))
        # provider requests: one author-filtered request per confirmed account, each result rechecked
        asked = []

        def authored(cfg_, info, src, acct, since, run=None):
            asked.append(acct["id"])
            return [{"number": int(acct["id"]), "title": "ABC-1001 x", "state": "open", "created_at": "2026-11-05T10:00:00Z",
                     "author": {"id": acct["id"], "login": acct["login"]}},
                    {"number": 999, "title": "someone else's", "state": "open", "created_at": "2026-11-05T10:00:00Z",
                     "author": {"id": "103", "login": "colleague"}}], False
        prs = activity.new_ledger()["prs"]
        repo = {"provider": "github", "instance": "github.com", "path": self.repo, "key": "service-api",
                "remote": {"host": "github", "path": "acme-test/service-api"}}
        with mock.patch("chitragupta.hosts.source_for", return_value="gh"), \
                mock.patch("chitragupta.hosts.authored_prs", side_effect=authored):
            activity.collect_prs(cfg, pol, repo, prs, "2026-10-01")
        self.assertEqual(sorted(asked), ["101", "102"])
        self.assertEqual(sorted(prs["items"]), ["101", "102"])
        self.assertEqual(prs["status"], "ok")

    def test_removing_one_account_previews_only_its_prs(self):
        from chitragupta import activity, common, profile
        from chitragupta.dashboard import api
        a1 = {"provider": "github", "instance": "github.com", "id": "101", "login": "dev-work"}
        a2 = {"provider": "github", "instance": "github.com", "id": "102", "login": "dev-old"}
        cfg = self.config(profile={"accounts": [a1, a2, JIRA_A]})
        led = activity.new_ledger({"id": "github:github.com/acme-test/service-api", "path": self.repo.replace("\\", "/"),
                                   "provider": "github", "instance": "github.com", "name": "service-api"})
        led["prs"]["items"] = {"1": {"number": 1, "by": list(profile.account_key(a1))},
                               "2": {"number": 2, "by": list(profile.account_key(a2))},
                               "3": {"number": 3, "by": list(profile.account_key(a2))}}
        common.write_json(activity.ledger_path(activity.ledger_key(self.repo)), led)
        res, code = api.profile_preview(json.dumps({"profile.accounts": [a1, JIRA_A]}), cfg)
        self.assertEqual(code, 200)
        self.assertEqual(res["leaving"]["prs"], 2)
        self.assertEqual([a["login"] for a in res["accounts"]["removed"]], ["dev-old"])
        self.assertEqual(res["accounts"]["added"], [])

    def test_confirming_a_second_account_adds_it(self):
        from chitragupta import common, profile
        first = {"provider": "github", "instance": "github.com", "id": "101", "login": "dev-work"}
        self.config(profile={"accounts": [first, JIRA_A]})
        profile._merge_candidates([{"kind": "account", "provider": "github", "instance": "github.com", "id": "102",
                                    "login": "dev-old", "origin": "host:github", "source": "gh api user", "bot": False}],
                                  {"host:github"}, {}, common.now_iso())
        cid = next(c["cid"] for c in profile.read_candidates()["candidates"] if c.get("id") == "102")
        profile.confirm(cid, source="test")
        ids = sorted(a["id"] for a in profile.load(common.load_config())["accounts"] if a["provider"] == "github")
        self.assertEqual(ids, ["101", "102"])


# --------------------------------------------------------------------------- F03 / F05: the Jira scope search

def page(issues, total, start=0):
    return ({"start_at": start}, {"issues": issues, "total": total, "startAt": start, "maxResults": 2})


def iss(key, acct=A, version="November"):
    return {"key": key, "fields": {"assignee": {"accountId": acct} if acct else None, "status": {"name": "To Do"},
                                   "fixVersions": [{"id": "10", "name": version, "releaseDate": "2026-11-15"}]}}


class ScopeSearch(Home, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg_r = self.config(releases={"enabled": True})

    def test_query_is_narrowed_to_confirmed_accounts_and_projects(self):
        from chitragupta import release
        jql, _ = release.scope_query(self.cfg_r)
        self.assertIn('assignee in ("%s")' % A, jql)
        self.assertIn("project in (ABC)", jql)
        self.assertNotIn("currentUser", jql)
        cfg = self.config(releases={"enabled": True, "jira": {"scope_jql": "assignee = currentUser() AND fixVersion is not EMPTY ORDER BY updated"}})
        jql, _ = release.scope_query(cfg)
        self.assertNotIn("currentUser", jql)
        self.assertTrue(jql.endswith("ORDER BY key ASC"))
        cfg = self.config(profile={"accounts": []})
        self.assertEqual(release.scope_query(cfg)[0], None)  # unconfirmed identity: nothing, never everyone's

    def test_rows_are_rechecked_and_pages_merged_without_false_absence(self):
        from chitragupta import release
        now = "2026-11-05T09:00:00+00:00"
        rec = release.merge_scope({}, [page([iss("ABC-1"), iss("ABC-2", acct=B)], 5),
                                       page([iss("XYZ-3"), iss("ABC-1")], 5, start=2)], self.cfg_r, now, "q", 2)
        self.assertEqual(sorted(rec["issues"]), ["ABC-1"])
        self.assertEqual(rec["rejected"], {"not_yours": 1, "outside_projects": 1})
        self.assertFalse(rec["complete"])
        self.assertEqual(rec["cycle"]["next"], 4)
        # the last page arrives on the next run: complete, still deduplicated
        rec2 = release.merge_scope(rec, [page([iss("ABC-4")], 5, start=4)], self.cfg_r, now, "q", 2, cycle=rec["cycle"])
        self.assertTrue(rec2["complete"])
        self.assertEqual(sorted(rec2["issues"]), ["ABC-1", "ABC-4"])
        # a new pass that only gets its first page: a ticket not seen yet is kept (stale), never dropped
        rec3 = release.merge_scope(rec2, [page([iss("ABC-4")], 3)], self.cfg_r, now, "q", 2,
                                   cycle=release.scope_cycle({}, "q", A, dt.datetime(2026, 11, 6, tzinfo=UTC)))
        self.assertIn("ABC-1", rec3["issues"])
        self.assertTrue(rec3["issues"]["ABC-1"]["stale"])
        self.assertFalse(rec3["complete"])
        # only a complete pass may drop it
        rec4 = release.merge_scope(rec3, [page([iss("ABC-5")], 3, start=2)], self.cfg_r, now, "q", 2, cycle=rec3["cycle"])
        self.assertTrue(rec4["complete"])
        self.assertNotIn("ABC-1", rec4["issues"])
        self.assertEqual([r["key"] for r in rec4["removed"]], ["ABC-1"])

    def test_error_objects_and_failures_keep_the_last_good_rows(self):
        from chitragupta import release
        now = "2026-11-05T09:00:00+00:00"
        good = release.merge_scope({}, [page([iss("ABC-1")], 1)], self.cfg_r, now, "q", 50)
        self.assertTrue(good["complete"])
        for bad in ({"errorMessages": ["You do not have permission"]}, {"error": "timeout"}, "Internal error", None):
            rec = release.merge_scope(good, [({}, bad)], self.cfg_r, "2026-11-05T10:00:00+00:00", "q", 50)
            self.assertIn("ABC-1", rec["issues"], bad)
            self.assertFalse(rec["complete"])
            self.assertTrue(rec["error"])
            self.assertEqual(rec["fetched_at"], now)  # the last good read, not the failed one

    def test_the_job_pages_within_its_budget_and_resumes(self):
        from chitragupta import common, mcpjobs, release
        cfg = self.config(releases={"enabled": True, "jira": {"scope_limit": 50, "scope_max_pages": 2}},
                          mcp={"jira": {"enabled": True}})
        rows = [iss("ABC-%d" % (2000 + i)) for i in range(120)]
        calls = []

        def run(job, *a, **k):
            calls.append(job)
            return {"calls": [], "result": ""}

        def results(res, suffix):
            start = 0 if len(calls) == 1 else 100
            pages = [rows[s:s + 50] for s in (start, start + 50)]
            return [({"start_at": start + 50 * i}, {"issues": p, "total": 120, "startAt": start + 50 * i})
                    for i, p in enumerate(pages) if p]
        with mock.patch.object(mcpjobs, "run", side_effect=run), mock.patch.object(mcpjobs, "tool_results", side_effect=results):
            rec = mcpjobs.jira_release_scope(cfg, {})
            self.assertFalse(rec["complete"])
            self.assertEqual(rec["collected"], 100)
            self.assertIn("100 of 120", rec["partial_reason"])
            rec = mcpjobs.jira_release_scope(cfg, {})
        self.assertTrue(rec["complete"])
        self.assertEqual(len(rec["issues"]), 120)
        self.assertEqual(calls, ["jira-release-scope", "jira-release-scope"])

    def test_a_scope_only_overdue_ticket_stays_visible_while_the_search_fails(self):
        from chitragupta import common, release
        good = release.merge_scope({}, [page([iss("ABC-7", version="October")], 1)], self.cfg_r,
                                   "2026-10-20T09:00:00+00:00", release.scope_query(self.cfg_r)[0], 50)
        good["issues"]["ABC-7"]["versions"][0]["release_date"] = "2026-10-15"
        common.write_json(release.scope_path(), good)
        release.save_scope(None, "q", self.cfg_r, "2026-11-05T09:00:00+00:00", error="timed out")
        ov = release.month_overview("2026-11", self.cfg_r, now=dt.datetime(2026, 11, 5, 12, tzinfo=UTC))
        self.assertEqual([r["key"] for r in ov["overdue"]], ["ABC-7"])
        self.assertFalse(ov["scope"]["complete"])
        self.assertIn("timed out", ov["scope"]["error"])


# --------------------------------------------------------------------------- F04: UAT evidence per revision

class UatRevision(Home, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg_r = self.config(releases={"enabled": True, "jira": {"uat_deployed_statuses": ["Deployed to UAT", "Accepted"],
                                                                     "uat_accepted_statuses": ["Accepted"]}})

    def ev(self, kind, proms, obs, facts=None):
        from chitragupta import release
        cur = proms[-1]
        return release.uat_evidence(kind, cur, len(proms) > 1, obs, self.cfg_r, {}, "ABC-1001",
                                    facts or {"source": "card", "status": obs[-1]["status"] if obs else ""})

    def test_revision_a_then_follow_up_b(self):
        from chitragupta import release
        a = {"rev": "rev-a", "at": "2026-11-01T10:00:00+00:00"}
        obs = [{"at": "2026-11-01T09:00:00+00:00", "status": "In QA"},
               {"at": "2026-11-01T12:00:00+00:00", "status": "Accepted"}]
        self.assertEqual(self.ev("accepted", [a], obs)["state"], "yes")
        # B reaches UAT later; Jira still says Accepted (A's acceptance): not B's
        b = {"rev": "rev-b", "at": "2026-11-03T10:00:00+00:00"}
        obs.append({"at": "2026-11-03T11:00:00+00:00", "status": "Accepted"})
        for kind in ("deployed", "accepted"):
            e = self.ev(kind, [a, b], obs)
            self.assertEqual(e["state"], "unknown", kind)
            self.assertTrue(e.get("carried"))
        # B deployed: a change into a deployed status after B reached UAT
        obs.append({"at": "2026-11-03T15:00:00+00:00", "status": "Deployed to UAT"})
        self.assertEqual(self.ev("deployed", [a, b], obs)["state"], "yes")
        self.assertEqual(self.ev("accepted", [a, b], obs)["state"], "unknown")
        # B accepted
        obs.append({"at": "2026-11-04T09:00:00+00:00", "status": "Accepted"})
        acc = self.ev("accepted", [a, b], obs)
        self.assertEqual((acc["state"], acc["rev"]), ("yes", "rev-b"))

    def test_a_failed_or_stale_read_is_not_an_observation(self):
        from chitragupta import release
        obs = release.uat_observations([], {"source": "card", "fetched_at": "2026-11-04T09:00:00+00:00",
                                            "status": "Accepted", "error": "disconnected"})
        self.assertEqual(obs, [])
        e = release.uat_evidence("accepted", {"rev": "rev-b", "at": "2026-11-03T10:00:00+00:00"}, True, obs, self.cfg_r,
                                 {}, "ABC-1001", {"source": "card", "status": "Accepted", "error": "disconnected"})
        self.assertEqual(e["state"], "unknown")
        self.assertIn("failed", e["source"])

    def test_readiness_needs_evidence_for_the_revision_in_uat(self):
        from chitragupta import release
        stages = {x: {"state": "present"} for x in ("release", "qa", "uat")}
        rel = {"selected": "v1", "repos": {"service-api": {"branch": {"name": "release/ABC-9000/nov", "state": "confirmed"},
                                                           "stages": stages, "freshness": {"stale": False}}},
               "uat": {"requirement": {"state": "required"}, "revision": {"rev": "rev-b"},
                       "deployed": {"state": "yes", "rev": "rev-a"}, "accepted": {"state": "yes", "rev": "rev-a"}}}
        rd = release.readiness(rel)
        self.assertFalse(rd["ready"])
        self.assertIn("UAT accepted unverified for the current changes", rd["missing"])
        rel["uat"]["deployed"]["rev"] = rel["uat"]["accepted"]["rev"] = "rev-b"
        self.assertTrue(release.readiness(rel)["ready"])
        # the review's repro: a status read with an error and no revision never makes it ready
        dep = release._status_evidence({"status": "Accepted", "fetched_at": "2026-10-01T12:00:00Z", "error": "disconnected"},
                                       self.cfg_r, "deployed")
        self.assertEqual(dep["state"], "unknown")
        rel["uat"].update(revision=None)
        self.assertFalse(release.readiness(rel)["ready"])

    def test_a_cached_ready_expires_with_time(self):
        from chitragupta import release
        row = {"key": "ABC-1001", "ready": True, "missing": [], "computed_at": "2026-11-01T10:00:00+00:00"}
        late = release.current_readiness(row, dt.datetime(2026, 11, 5, tzinfo=UTC), 24)
        self.assertFalse(late["ready"])
        self.assertIn("/cg:check ABC-1001", late["missing"][-1])
        self.assertTrue(release.current_readiness(row, dt.datetime(2026, 11, 1, 12, tzinfo=UTC), 24)["ready"])

    def test_your_confirmation_counts_for_the_current_changes_only(self):
        from chitragupta import release
        from chitragupta.store import Store
        st = Store()
        tk = st.ticket("ABC-1001")
        tk["release"] = {"v": 1, "uat": {"revision": {"rev": "rev-b", "at": "2026-11-03T10:00:00+00:00"}}}
        ok, msg, _ = release.apply_directive(release.parse_directive("uat-accepted: ABC-1001"), st, self.cfg_r,
                                             "2026-11-04T09:00:00+00:00")
        self.assertTrue(ok, msg)
        ov = st.ticket("ABC-1001")["release_override"]
        cur = {"rev": "rev-b", "at": "2026-11-03T10:00:00+00:00"}
        self.assertEqual(release.uat_evidence("accepted", cur, True, [], self.cfg_r, ov, "ABC-1001", {})["state"], "yes")
        later = {"rev": "rev-c", "at": "2026-11-05T10:00:00+00:00"}
        self.assertEqual(release.uat_evidence("accepted", later, True, [], self.cfg_r, ov, "ABC-1001", {})["state"], "unknown")


# --------------------------------------------------------------------------- F06: development versus integration

class Guidance(Home, unittest.TestCase):
    RB = "release/ABC-9000/nov"

    def view(self):
        from chitragupta import release
        v = release.RepoView("service-api", {}, meta={})
        roles = {"main": "main", "qa": "qa", "uat": "uat", self.RB: "release", "release/ABC-8000/oct": "release"}
        v.role = lambda b: roles.get(b)
        return v

    def test_development_bases_from_any_role_are_checked(self):
        from chitragupta import release
        x = {"branches": [
            {"branch": "feature/ABC-1001-a", "parent": "main", "parent_source": "command"},
            {"branch": "feature/ABC-1001-b", "parent": "qa", "parent_source": "reflog"},
            {"branch": "feature/ABC-1001-c", "parent": "spike/tmp", "parent_source": "reflog"},
            {"branch": "feature/ABC-1001-d", "parent": "release/ABC-8000/oct", "parent_source": "reflog"},
            {"branch": "feature/ABC-1001-e", "parent": self.RB, "parent_source": "reflog"},
            {"branch": "feature/ABC-1001-qa", "parent": "qa", "parent_source": "reflog"},   # the QA integration branch
            {"branch": "qa-merge-nov", "parent": "qa", "link": "commit message"}],
            "prs": [{"number": 7, "source": "feature/ABC-1001-qa", "target": "qa", "state": "open"}]}
        dev, flags = release._dev_branches(x, self.view(), self.RB, {"release/ABC-8000/oct"})
        by = {d["branch"]: d for d in dev}
        self.assertEqual(sorted(by), ["feature/ABC-1001-a", "feature/ABC-1001-b", "feature/ABC-1001-c",
                                      "feature/ABC-1001-d", "feature/ABC-1001-e"])
        self.assertEqual([by["feature/ABC-1001-%s" % k]["base"] for k in "abcde"], ["wrong", "wrong", "unproven", "wrong", "ok"])
        self.assertEqual(sorted(f["kind"] for f in flags), ["old-release-base", "wrong-base", "wrong-base"])
        self.assertTrue(any(self.RB in f["text"] for f in flags))

    def test_development_prs_into_main_are_flagged_and_integration_prs_are_not(self):
        from chitragupta import release
        v = self.view()
        x = {"branches": [{"branch": "feature/ABC-1001", "parent": self.RB},
                          {"branch": "feature/ABC-1001-qa", "parent": "qa"}],
             "prs": [{"number": 1, "source": "feature/ABC-1001", "target": "main", "state": "open"},
                     {"number": 2, "source": "feature/ABC-1001-qa", "target": "qa", "state": "open"},
                     {"number": 3, "source": self.RB, "target": "main", "state": "merged"},
                     {"number": 4, "source": "feature/ABC-1001", "target": "release/ABC-8000/oct", "state": "open"}]}
        prs = release._prs_by_role(x, v)
        flags = release._pr_flags([p for lst in prs.values() for p in lst], self.RB, {"release/ABC-8000/oct"}, v, x)
        self.assertEqual(sorted(f["pr"] for f in flags), [1, 4])
        main = next(f for f in flags if f["pr"] == 1)
        self.assertEqual((main["kind"], main["resolved_target"]), ("wrong-target", self.RB))
        self.assertIn("main", main["text"])
        e = {"branch": {"name": self.RB, "state": "confirmed"}, "dev": release._dev_branches(x, v, self.RB, set())[0],
             "prs": prs, "stages": {"release": {"state": "missing"}, "qa": {"state": "missing"}},
             "freshness": {"stale": False, "open_prs_visible": True}}
        strat = {"steps": [{"pr_into": "release"}, {"pr_into": "qa", "of_role": "release"}]}
        step = release.next_step("ABC-1001", "service-api", e, strat, "not_required", {})
        self.assertIn("Retarget PR #1 from main to %s" % self.RB, step)


# --------------------------------------------------------------------------- F07: canonical repository identity

class RepoIdentity(Home, unittest.TestCase):
    def test_same_basename_repositories_get_their_own_ledgers(self):
        from chitragupta import activity, common
        other = os.path.join(self.home, "elsewhere", "service-api")
        os.makedirs(other)
        self.assertNotEqual(activity.ledger_key(self.repo), activity.ledger_key(other))
        # an older folder-name ledger written for the other folder never answers for this one
        led = activity.new_ledger({"id": "path:x", "path": other.replace("\\", "/"), "name": "service-api", "key": "service-api"})
        common.write_json(activity.ledger_path("service-api"), led)
        self.assertIsNone(activity.load(self.repo))
        self.assertIsNotNone(activity.load(other))

    def test_two_locations_of_one_repository_count_once(self):
        from tests.test_dashboard import build_home
        from tests.test_dashboard_progress import add_progress_fixtures, REPO
        from chitragupta import activity, common, profile, progress
        now = dt.datetime(2026, 11, 5, 12, 0).astimezone()
        with mock.patch.object(common, "now", return_value=now):
            days = build_home(self.home, now)
            add_progress_fixtures(self.home, days)
            cfg = common.load_config()
            base = progress.day_metrics(progress.Ctx(cfg, now), days["day"])
            twin = REPO + "-copy"
            cfg2 = copy.deepcopy(cfg)
            cfg2["watched_repos"] = cfg["watched_repos"] + [twin]
            write_config(self.home, cfg2)
            cfg2 = common.load_config()
            led = common.read_json(activity.ledger_path("service-api"))
            led["profile_sig"] = profile.signature(cfg2)
            common.write_json(activity.ledger_path("service-api"), led)
            twin_led = copy.deepcopy(led)
            twin_led["repo"]["path"] = twin
            common.write_json(activity.ledger_path(activity.ledger_key(twin)), twin_led)
            m = progress.day_metrics(progress.Ctx(cfg2, now), days["day"])
        got = (m["commits"]["value"], m["lines"]["added"], m["lines"]["deleted"], m["prs_raised"]["value"], m["prs_merged"]["value"])
        self.assertEqual(got, (3, 160, 42, 2, 1))
        self.assertEqual(got, (base["commits"]["value"], base["lines"]["added"], base["lines"]["deleted"],
                               base["prs_raised"]["value"], base["prs_merged"]["value"]))


# --------------------------------------------------------------------------- F08: literal aliases through real git

class LiteralAliases(Home, unittest.TestCase):
    def test_plus_case_punctuation_lookalikes_and_coauthors(self):
        from chitragupta import activity, common, profile, sources
        git(self.repo, "init", "-q", "-b", "main")
        mine = "developer+work@acme.test"
        dot = "dev.x+y@acme.test"
        commit(self.repo, "a.txt", "a\n", "ABC-1001 plus alias", author=("Dev Example", mine), date="2026-11-05T10:00:00Z")
        commit(self.repo, "b.txt", "b\n", "ABC-1001 case", author=("Dev Example", "Developer+Work@ACME.test"),
               date="2026-11-05T10:05:00Z")
        commit(self.repo, "c.txt", "c\n", "ABC-1001 look-alike", author=("Dev Example", "x" + mine), date="2026-11-05T10:10:00Z")
        commit(self.repo, "d.txt", "d\n", "ABC-1001 dot is not a wildcard", author=("Dev Example", "devAx+y@acme.test"),
               date="2026-11-05T10:15:00Z")
        commit(self.repo, "e.txt", "e\n", "ABC-1001 punctuation", author=("Dev Example", dot), date="2026-11-05T10:20:00Z")
        commit(self.repo, "f.txt", "f\n", "ABC-1001 pair\n\nCo-authored-by: Dev <%s>" % mine,
               author=("Colleague", OTHER), date="2026-11-05T10:25:00Z")
        cfg = self.config(git_author_emails=[mine, dot])
        ok, items, err = activity.scan(cfg, profile.Policy(cfg), self.repo, "2026-11-01T00:00:00Z", [], ["ABC"])
        self.assertTrue(ok, err)
        subjects = sorted(i["subject"] for i in items.values())
        self.assertEqual(subjects, ["ABC-1001 case", "ABC-1001 pair", "ABC-1001 plus alias", "ABC-1001 punctuation"])
        self.assertEqual({i["subject"]: i["role"] for i in items.values()}["ABC-1001 pair"], "coauthor")
        got = sources.scan_commits(cfg, {}, dt.datetime(2026, 11, 1, tzinfo=UTC))
        self.assertEqual(sorted(c["subject"] for c in got), ["ABC-1001 case", "ABC-1001 plus alias", "ABC-1001 punctuation"])


# --------------------------------------------------------------------------- F09: uncertainty carried through

class Uncertainty(Home, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from tests.test_dashboard import build_home
        from tests.test_dashboard_progress import add_progress_fixtures
        from chitragupta import common
        self.now = dt.datetime(2026, 11, 5, 12, 0).astimezone()
        self.clock = mock.patch.object(common, "now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.days = build_home(self.home, self.now)
        add_progress_fixtures(self.home, self.days)

    def ledger(self, **prs):
        from chitragupta import activity, common
        p = activity.ledger_path("service-api")
        led = common.read_json(p)
        led["prs"].update(prs)
        common.write_json(p, led)

    def test_partial_counts_never_become_records_or_highest_day_claims(self):
        from chitragupta import progress
        self.ledger(status="partial", why="The host returned its maximum page; older PRs may be missing.")
        ctx = progress.Ctx(None, self.now)
        m = progress.day_metrics(ctx, self.days["day"])
        self.assertEqual(m["prs_raised"]["status"], "partial")
        rec = next(r for r in progress.records(ctx)["records"] if r["id"] == "prs_raised")
        self.assertNotEqual(rec["date"], self.days["day"])
        self.assertTrue(rec.get("qualified"))
        self.assertEqual(rec["partial_days"][0]["date"], self.days["day"])
        text = " ".join(progress.celebrations(ctx))
        self.assertNotIn("highest PR-opening day", text)
        self.assertIn("at least 2 PRs", text)
        self.assertIn("not compared with your records", text)
        wins = progress.weekly_wins(None, None, self.now)["markdown"]
        self.assertIn("at least", wins)

    def test_disconnected_pr_source_never_says_merge_it(self):
        from chitragupta import profile, progress, common
        self.ledger(status="error", error="gh: authentication failed", why="The last PR read failed; earlier PRs are kept.")
        out = progress.today(None, self.now)
        texts = [a["text"] for a in out["next_actions"]["actions"]]
        self.assertFalse(any("merge it" in t for t in texts), texts)
        self.assertTrue(any("was approved when last read" in t and "check its current state" in t for t in texts), texts)
        self.assertTrue(any(a["kind"] == "recover" for a in out["next_actions"]["actions"]))
        self.assertEqual(out["status"]["state"], "attention")
        st = profile.status(common.load_config())
        host = next(h for h in st["hosts"] if h["provider"] == "github")
        self.assertEqual(host["state"], "ok")  # the identity is confirmed ...
        self.assertEqual(host["connection"]["state"], "failing")  # ... but the last read failed

    def test_recovery_is_consistent_once_the_source_is_back(self):
        from chitragupta import progress, common
        self.ledger(status="error", error="timeout")
        self.assertEqual(progress.today(None, self.now)["status"]["state"], "attention")
        self.ledger(status="ok", error="", why="", fetched_at=self.now.isoformat(timespec="seconds"))
        out = progress.today(None, self.now)
        self.assertEqual(out["status"]["state"], "ok")
        self.assertTrue(any("merge it" in a["text"] for a in out["next_actions"]["actions"]))


# --------------------------------------------------------------------------- F10: one profile day

class ProfileDay(Home, unittest.TestCase):
    def test_month_and_today_agree_across_zones_and_boundaries(self):
        from chitragupta import profile, release
        cases = [("+14:00", dt.datetime(2026, 10, 31, 15, 0, tzinfo=UTC), "2026-11-01"),
                 ("-10:00", dt.datetime(2026, 11, 1, 5, 0, tzinfo=UTC), "2026-10-31"),
                 ("+14:00", dt.datetime(2026, 12, 31, 15, 0, tzinfo=UTC), "2027-01-01"),  # 05:00 on 1 January
                 ("+14:00", dt.datetime(2026, 12, 31, 12, 0, tzinfo=UTC), "2026-12-31"),  # 02:00: before day_starts_at
                 ("+00:00", dt.datetime(2026, 11, 1, 3, 30, tzinfo=UTC), "2026-10-31")]  # before day_starts_at 04:00
        for tz, now, want in cases:
            cfg = self.config(profile={"timezone": tz}, releases={"enabled": True})
            self.assertEqual(profile.today(cfg, now), want, tz)
            ov = release.month_overview(None, cfg, now=now)
            self.assertEqual((ov["today"], ov["month"]), (want, want[:7]), tz)

    def test_a_completion_near_midnight_counts_once_on_the_profile_day(self):
        from chitragupta import common, progress
        cfg = self.config(profile={"timezone": "+14:00"})
        when = "2026-11-01T15:30:00+00:00"  # 2026-11-02 05:30 at +14:00 (after day_starts_at): profile day 2026-11-02
        ch = {"ticket": "ABC-1001", "time": when, "from": "In Review", "to": "Done", "mine": True}
        for day in ("2026-11-01", "2026-11-02"):  # the same change filed under two machine days
            common.write_json(common.hp("data", "jira", "_evening", day + ".json"),
                              {"day": day, "changelogs": [], "facts": {"status_changes": [], "status_history": [ch]}})
        self.ticket("ABC-1001", {"ownership": {"account": A, "state": "you",
                                               "history": [{"at": "2026-10-01T00:00:00+00:00", "state": "you", "account": A}]}})
        now = dt.datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
        ctx = progress.Ctx(cfg, now)
        self.assertEqual(progress._completed(ctx, "2026-11-02")[0], 1)
        self.assertEqual(progress._completed(ctx, "2026-11-01")[0], 0)


if __name__ == "__main__":
    unittest.main()
