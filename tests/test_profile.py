"""The developer profile and the one ownership policy: confirmed identities only, provider-scoped accounts, ticket
ownership with history, scope, the profile zone, settings validation, candidates and reconciliation. Synthetic data:
developer@acme.test, service-api, ABC-…; temp git repos with per-commit author / committer identities."""
import datetime as dt
import json
import os
import subprocess
import unittest
from unittest import mock

from tests.support import Isolated, write_config

ME, ME2, OTHER, LOOKALIKE = "developer@acme.test", "dev.alt@acme.test", "colleague@acme.test", "xdeveloper@acme.test"
UTC = dt.timezone.utc


def git(repo, *args, author=("Dev Example", ME), committer=None, date=None):
    c = committer or author
    env = dict(os.environ, GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1], GIT_COMMITTER_NAME=c[0],
               GIT_COMMITTER_EMAIL=c[1])
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    r = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return r.stdout.strip()


def commit(repo, path, text, msg, **kw):
    with open(os.path.join(repo, path), "w", encoding="utf-8") as f:
        f.write(text)
    git(repo, "add", path, **kw)
    git(repo, "commit", "-q", "-m", msg, **kw)
    return git(repo, "rev-parse", "HEAD")


class DstZone(dt.tzinfo):
    """A synthetic zone (no tz data needed): UTC-5, and UTC-4 from 2026-03-08 07:00 UTC (02:00 -> 03:00 local)."""
    SWITCH = dt.datetime(2026, 3, 8, 7, 0, tzinfo=UTC)

    def utcoffset(self, d):
        if d is None:
            return dt.timedelta(hours=-5)
        naive = d.replace(tzinfo=None)
        return dt.timedelta(hours=-4) if naive >= dt.datetime(2026, 3, 8, 3, 0) else dt.timedelta(hours=-5)

    def dst(self, d):
        return self.utcoffset(d) - dt.timedelta(hours=-5)

    def tzname(self, d):
        return "SYN"

    def fromutc(self, d):
        u = d.replace(tzinfo=UTC)
        off = dt.timedelta(hours=-4) if u >= self.SWITCH else dt.timedelta(hours=-5)
        return (d + off).replace(tzinfo=self)


class Base(Isolated):
    def setUp(self):
        self.isolate()
        self.repo = os.path.join(self.home, "work", "service-api")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self.cfg = {"watched_repos": [self.repo], "git_author_emails": [ME], "ticket_prefixes": ["ABC"],
                    "day_starts_at": "04:00"}
        write_config(self.home, self.cfg)

    def config(self, **kw):
        self.cfg.update(kw)
        write_config(self.home, self.cfg)
        from chitragupta import common
        return common.load_config()


class Identity(Base, unittest.TestCase):
    def scan(self):
        from chitragupta import common, sources
        return sources.scan_commits(common.load_config(), {}, common.now() - dt.timedelta(days=2))

    def test_same_display_name_and_lookalike_addresses_are_not_you(self):
        mine = commit(self.repo, "a.txt", "1", "ABC-1001 mine", author=("Dev Example", ME))
        commit(self.repo, "b.txt", "2", "ABC-1001 theirs", author=("Dev Example", OTHER))  # same display name
        commit(self.repo, "c.txt", "3", "ABC-1001 look-alike", author=("Dev Example", LOOKALIKE))  # --author matches it
        got = self.scan()
        self.assertEqual([c["sha"] for c in got], [mine])
        self.assertEqual(got[0]["ae"], ME)

    def test_several_confirmed_emails_match_exactly_in_any_case(self):
        self.config(git_author_emails=[ME, ME2])
        a = commit(self.repo, "a.txt", "1", "one", author=("Dev", "Developer@ACME.test"))
        b = commit(self.repo, "b.txt", "2", "two", author=("Dev", ME2))
        commit(self.repo, "c.txt", "3", "three", author=("Dev", "dev.alt+ci@acme.test"))  # never plus-stripped
        commit(self.repo, "d.txt", "4", "four", author=("Dev", "devalt@acme.test"))  # never dot-stripped
        self.assertEqual(sorted(c["sha"] for c in self.scan()), sorted([a, b]))

    def test_author_counts_never_the_committer(self):
        theirs = commit(self.repo, "a.txt", "1", "theirs, rebased by me", author=("Colleague", OTHER),
                        committer=("Dev Example", ME))
        mine = commit(self.repo, "b.txt", "2", "mine, applied by them", author=("Dev Example", ME),
                      committer=("Colleague", OTHER))
        got = [c["sha"] for c in self.scan()]
        self.assertIn(mine, got)
        self.assertNotIn(theirs, got)

    def test_no_confirmed_email_means_unknown_never_everyone(self):
        from chitragupta import delivery, profile
        cfg = self.config(git_author_emails=[])
        commit(self.repo, "a.txt", "1", "ABC-1001 work")
        self.assertEqual(self.scan(), [])
        pol = profile.Policy(cfg)
        self.assertIsNone(pol.commit_mine(ME))
        self.assertIsNone(pol.commit_role(ME))
        # the delivery check keeps commits of unknown authorship; "your commits" counts never include them
        r = mock.Mock()
        r.patch_ids.return_value = {}
        own = [{"sha": "a" * 40, "subject": "work", "date": "2026-11-02", "mine": None, "picked_from": None}]
        rows = delivery.rows_for(r, "ABC-1001", [{"branch": "feature/ABC-1001", "own": own}], cfg)
        self.assertEqual(len(rows), 1)
        self.assertFalse(own[0]["mine"] is True)

    def test_bot_identities_are_refused_everywhere(self):
        from chitragupta import profile, settings
        _, err = settings.validate({"git_author_emails": [ME, "dependabot[bot]@users.noreply.github.com"]})
        self.assertIn("bot", err["git_author_emails"])
        _, err = settings.validate({"git_author_emails": ["ci-bot@acme.test"]})
        self.assertIn("git_author_emails", err)
        _, err = settings.validate({"profile.accounts": [{"provider": "github", "instance": "github.com",
                                                          "id": "41898282", "login": "github-actions[bot]"}]})
        self.assertIn("bot", err["profile.accounts"])
        pol = profile.Policy(self.cfg)
        self.assertFalse(pol.pr_mine({"login": "renovate[bot]", "is_bot": True}, "github", "github.com"))
        self.assertIsNone(pol.commit_role("noreply@github.com"))

    def test_coauthor_role_is_separate_and_exact(self):
        from chitragupta import profile
        pol = profile.Policy(self.cfg)
        self.assertEqual(pol.commit_role(OTHER, [ME]), "coauthor")
        self.assertEqual(pol.commit_role(ME, [OTHER]), "author")
        self.assertIsNone(pol.commit_role(OTHER, [LOOKALIKE]))


class Accounts(Base, unittest.TestCase):
    def acct(self, **kw):
        a = dict(provider="github", instance="github.com", id="1001", login="dev-example", node_id="U_dev1001")
        a.update(kw)
        return a

    def test_pr_author_compares_stable_ids_of_the_same_kind(self):
        from chitragupta import hosts, profile
        pol = profile.Policy(self.config(profile={"accounts": [self.acct()]}))
        gh = hosts.author_of("gh", {"id": "U_dev1001", "login": "dev-example", "is_bot": False})
        self.assertEqual(gh["node_id"], "U_dev1001")
        self.assertTrue(pol.pr_mine(gh, "github", "github.com"))
        renamed = hosts.author_of("gh", {"id": "U_dev1001", "login": "new-handle"})  # login changed, id didn't
        self.assertTrue(pol.pr_mine(renamed, "github", "github.com"))
        rest = hosts.author_of("mcp:github", {"id": 2002, "login": "dev-example"})  # same login, another account
        self.assertFalse(pol.pr_mine(rest, "github", "github.com"))
        self.assertIsNone(pol.pr_mine(gh, "github", "git.acme.test"))  # another instance: not configured
        self.assertIsNone(pol.pr_mine(None, "github", "github.com"))  # merge-commit source: no author

    def test_wrong_connected_account_is_only_a_suggestion(self):
        from chitragupta import common, hosts, profile
        os.makedirs(common.hp("data", "git", "service-api"))
        common.write_json(common.hp("data", "git", "service-api", "repo.json"), {"host": "github", "remote_path": "acme/service-api"})
        cfg = self.config(profile={"accounts": [self.acct()]})

        def run(argv, timeout=60):
            self.assertEqual(argv[1:], ["api", "user"])
            return 0, json.dumps({"id": 2002, "node_id": "U_other", "login": "someone-else", "type": "User"}), ""
        with mock.patch.object(hosts, "cli_path", return_value="gh"):
            res = profile.discover(cfg, hosts_too=True, run=run)
        c = [x for x in profile.read_candidates(cfg)["candidates"] if x["kind"] == "account"]
        self.assertEqual(len(c), 1)
        self.assertFalse(c[0]["confirmed"])
        self.assertIn("gh api user", c[0]["source"])
        self.assertFalse(profile.Policy(cfg).pr_mine({"id": "2002", "login": "someone-else"}, "github", "github.com"))
        self.assertEqual(res["errors"], {})

    def test_host_self_account_commands_are_read_only(self):
        from chitragupta.safety import commands
        self.assertIsNone(commands.host_reason("gh", ["api", "user"]))
        self.assertIsNone(commands.host_reason("glab", ["api", "user"]))
        self.assertIsNone(commands.host_reason("az", ["account", "show", "--output", "json"]))
        self.assertIsNotNone(commands.host_reason("gh", ["api", "user", "-X", "PATCH"]))
        self.assertIsNotNone(commands.host_reason("gh", ["api", "user", "-f", "name=x"]))
        self.assertIsNotNone(commands.host_reason("az", ["account", "set", "--subscription", "x", "--output", "json"]))

    def test_gh_pr_list_asks_for_the_author(self):
        from chitragupta import hosts
        seen = {}

        def run(argv, timeout=60):
            seen["argv"] = argv
            return 0, json.dumps([{"number": 7, "title": "x", "state": "OPEN", "isDraft": False, "headRefName": "f",
                                   "baseRefName": "main", "author": {"id": "U_dev1001", "login": "dev-example"}}]), ""
        with mock.patch.object(hosts, "cli_path", return_value="gh"):
            prs = hosts.gh_prs({}, {"path": "acme/service-api"}, "f", run=run)
        self.assertIn("author", seen["argv"][seen["argv"].index("--json") + 1].split(","))
        self.assertEqual(prs[0]["author"], {"id": "", "node_id": "U_dev1001", "login": "dev-example", "is_bot": False})

    def test_jira_person_prefers_the_confirmed_account_id(self):
        from chitragupta import mcpjobs, profile
        cfg = self.config(profile={"accounts": [{"provider": "jira", "instance": "acme.atlassian.net", "id": "acct-me"}]})
        pol = profile.Policy(cfg)
        self.assertTrue(pol.person_mine({"accountId": "acct-me"}))
        self.assertFalse(pol.person_mine({"accountId": "acct-other", "emailAddress": ME}))  # the id decides
        self.assertTrue(pol.person_mine({"emailAddress": ME}))  # no id on the record: an exact confirmed email
        self.assertIsNone(pol.person_mine({"displayName": "Dev Example"}))  # a name is never compared
        self.assertIn("assignee", mcpjobs.card_fields(cfg).split(","))
        self.assertNotIn("assignee", mcpjobs.card_fields(self.config(profile={})).split(","))

    def test_legacy_jira_account_id_still_counts(self):
        from chitragupta import profile
        pol = profile.Policy(self.config(mcp={"jira": {"account_id": "acct-legacy"}}))
        self.assertTrue(pol.person_mine({"account_id": "acct-legacy"}))
        self.assertTrue(pol.jira_account().get("legacy"))


class Tickets(Base, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from chitragupta import profile
        self.cfg_j = self.config(profile={"accounts": [{"provider": "jira", "instance": "acme.atlassian.net", "id": "acct-me"}],
                                          "include_tickets": {"ABC-1004": "pairing on the API contract"}})
        self.pol = profile.Policy(self.cfg_j)

    def own(self, *states):
        from chitragupta import profile
        tk = {"id": "ABC-1001"}
        for i, s in enumerate(states):
            profile.record_ownership(tk, s, "2026-11-0%dT10:00:00+00:00" % (i + 1), "acct-me")
        return tk

    def test_states_and_history(self):
        self.assertEqual(self.pol.ticket_state("ABC-1001", self.own("you"))[0], "assigned")
        tk = self.own("you", "you", "other")
        self.assertEqual(self.pol.ticket_state("ABC-1001", tk)[0], "reassigned")
        self.assertEqual([h["state"] for h in tk["ownership"]["history"]], ["you", "other"])  # only changes
        self.assertNotIn("other_person", json.dumps(tk))
        self.assertEqual(self.pol.ticket_state("ABC-1002", self.own("other"))[0], "other")
        self.assertEqual(self.pol.ticket_state("ABC-1003", self.own("unassigned"))[0], "unassigned")
        self.assertEqual(self.pol.ticket_state("ABC-1005", {})[0], "unknown")
        st, why = self.pol.ticket_state("ABC-1004", self.own("other"))
        self.assertEqual(st, "included")
        self.assertIn("pairing on the API contract", why)
        self.assertTrue(self.pol.ticket_current("ABC-1004", self.own("other")))
        self.assertFalse(self.pol.ticket_current("ABC-1001", tk))

    def test_card_assignee_is_recorded_without_the_other_persons_identity(self):
        from chitragupta import kbjobs
        jobs = kbjobs.KBJobs.__new__(kbjobs.KBJobs)
        jobs.cfg = self.cfg_j
        tk = {"id": "ABC-1001"}
        raw = {"key": "ABC-1001", "fields": {"assignee": {"accountId": "acct-me", "displayName": "Dev Example"}}}
        jobs.record_owner(tk, {"raw": raw, "fields": "summary,assignee", "fetched_at": "2026-11-01T10:00:00+00:00"})
        raw2 = {"key": "ABC-1001", "fields": {"assignee": {"accountId": "acct-colleague", "displayName": "Dev Example"}}}
        jobs.record_owner(tk, {"raw": raw2, "fields": "summary,assignee", "fetched_at": "2026-11-03T10:00:00+00:00"})
        self.assertEqual(tk["ownership"]["state"], "other")
        self.assertNotIn("acct-colleague", json.dumps(tk))
        self.assertEqual(self.pol.ticket_state("ABC-1001", tk)[0], "reassigned")
        jobs.record_owner(tk, {"raw": {"fields": {"summary": "x"}}, "fields": "summary", "fetched_at": "x"})
        self.assertEqual(tk["ownership"]["state"], "other")  # not asked for: nothing recorded

    def test_release_overview_drops_reassigned_and_keeps_included(self):
        from chitragupta import release
        worked = {"first": "2026-10-01"}
        self.assertEqual(release.mine(dict(self.own("you", "other"), **worked), self.cfg_j, key="ABC-1001"), (False, ""))
        ok, why = release.mine(dict(self.own("other"), **worked), self.cfg_j, key="ABC-1004")
        self.assertTrue(ok)
        self.assertIn("included by you", why)
        self.assertTrue(release.mine(dict(self.own("you"), **worked), self.cfg_j, key="ABC-1001")[0])

    def test_mcp_lists_your_tickets_and_scopes_alerts(self):
        from chitragupta import common, mcpserver, store as S
        def tk(key, state, alert):
            rec = dict(S.new_ticket(key), ownership=self.own(state)["ownership"], last="2026-11-0%s" % key[-1],
                       git={"alerts": [{"kind": "stale-pr", "text": "PR open on %s" % key, "repo": "service-api"}]} if alert else {})
            common.write_json(common.hp("data", "tickets", key + ".json"), rec)
        os.makedirs(common.hp("data", "tickets"))
        tk("ABC-1001", "you", True)
        tk("ABC-1002", "other", True)
        tk("ABC-1004", "other", False)
        out = mcpserver.tool_list_tickets({"mine": True}, self.cfg_j)
        self.assertIn("ABC-1001", out)
        self.assertIn("ABC-1004", out)
        self.assertNotIn("ABC-1002", out)
        self.assertIn("included by you", out)
        alerts = mcpserver.tool_get_alerts({}, self.cfg_j)
        self.assertIn("PR open on ABC-1001", alerts)
        self.assertNotIn("PR open on ABC-1002", alerts)
        self.assertIn("Left out: alerts of 1 ticket(s) assigned to someone else", alerts)
        # a record with none of your work on it (no session, note or commit of yours): outside your scope, not named
        common.write_json(common.hp("data", "tickets", "ABC-1009.json"), dict(
            S.new_ticket("ABC-1009"), ownership=self.own("other")["ownership"],
            git={"alerts": [{"kind": "stale-pr", "text": "PR open on ABC-1009", "repo": "service-api"}]}))
        alerts = mcpserver.tool_get_alerts({}, self.cfg_j)
        self.assertNotIn("ABC-1009", alerts)
        self.assertIn("Not shown: 1 ticket record(s) with none of your own work", alerts)
        self.assertNotIn("ABC-1009", mcpserver.tool_list_tickets({}, self.cfg_j))


class SettingsAndScope(Base, unittest.TestCase):
    def test_validation(self):
        from chitragupta import settings
        bad = {
            "profile.focus": "ship it, token ghp_" + "A1b2" * 10,
            "profile.display_name": "line one\nline two",
            "profile.timezone": "Nowhere/Never",
            "profile.tracking_start": "2999-01-01",
            "profile.include_tickets": {"ABC-1002": ""},
            "profile.repos": [os.path.join(self.home, "elsewhere")],
            "profile.accounts": [{"provider": "github", "instance": "github.com", "id": "1"},
                                 {"provider": "github", "instance": "github.com", "id": "1", "login": "dev"}],
            "profile.email": "not-an-email",
        }
        _, err = settings.validate(bad)
        self.assertEqual(sorted(err), sorted(bad))
        self.assertIn("secret", err["profile.focus"])
        self.assertIn("listed twice", err["profile.accounts"])
        # two different accounts of yours on the same instance are fine (a work and an older account)
        _, err = settings.validate({"profile.accounts": [{"provider": "github", "instance": "github.com", "id": "1"},
                                                         {"provider": "github", "instance": "github.com", "id": "2"}]})
        self.assertEqual(err, {})
        good = {"profile.display_name": "Dev Example", "profile.timezone": "+05:30", "profile.focus": "Payments API v2",
                "profile.include_tickets": {"ABC-1004": "pairing"}, "profile.repos": [self.repo],
                "profile.accounts": [{"provider": "jira", "instance": "acme.atlassian.net",
                                      "id": "712020:3f1c2d4e-0000-4000-8000-000000000001"}],
                "git_author_emails": [ME, ME2], "profile.tracking_start": "2026-01-05", "profile.backfill_days": 30}
        clean, err = settings.validate(good)
        self.assertEqual(err, {})
        settings.apply(good)
        from chitragupta import common, configstore
        cfg = common.load_config()
        self.assertEqual(cfg["profile"]["display_name"], "Dev Example")
        self.assertTrue(any(r["key"] == "profile.accounts" for r in configstore.history(20)))

    def test_signature_follows_identity_scope_and_zone_only(self):
        from chitragupta import profile
        base = profile.signature(self.config())
        self.assertEqual(base, profile.signature(self.config(profile={"display_name": "Dev Example"})))
        self.assertNotEqual(base, profile.signature(self.config(git_author_emails=[ME, ME2], profile={"display_name": "Dev Example"})))
        s2 = profile.signature(self.config())
        self.assertNotEqual(s2, profile.signature(self.config(profile={"timezone": "+05:30"})))
        both = profile.signature(self.config(profile={}, watched_repos=[self.repo, self.home]))
        self.assertNotEqual(both, profile.signature(self.config(profile={"repos": [self.repo]})))

    def test_doctor_row_reports_each_provider_without_identities(self):
        from chitragupta import doctor
        row = doctor.profile_row(self.config())
        self.assertTrue(row["ok"])  # an unconfirmed Jira account isn't a fault: its tickets just aren't counted as yours
        self.assertIn("commits ok", row["detail"])
        self.assertIn("Jira unconfigured", row["detail"])
        self.assertIn("Profile page", row["detail"])
        self.assertNotIn(ME, row["detail"])
        bad = doctor.profile_row(self.config(profile={"timezone": "Not/AZone"}))
        self.assertFalse(bad["ok"])
        self.assertIn("time zone", bad["detail"])

    def test_scope_limits_the_commit_scan(self):
        from chitragupta import common, sources
        other = os.path.join(self.home, "work", "web-app")
        os.makedirs(other)
        git(other, "init", "-q", "-b", "main")
        commit(other, "a.txt", "1", "mine elsewhere")
        a = commit(self.repo, "a.txt", "1", "mine here")
        self.config(watched_repos=[self.repo, other], profile={"repos": [self.repo]})
        got = sources.scan_commits(common.load_config(), {}, common.now() - dt.timedelta(days=1))
        self.assertEqual([c["sha"] for c in got], [a])


class Zone(Base, unittest.TestCase):
    def test_day_boundary_follows_dst_in_the_profile_zone(self):
        from chitragupta import profile
        z = DstZone()
        # 07:30 UTC on the switch day is 03:30 local (after the jump): before 04:00, so the day before
        self.assertEqual(profile.day_of("2026-03-08T07:30:00+00:00", self.cfg, tz=z), "2026-03-07")
        self.assertEqual(profile.day_of("2026-03-08T08:30:00+00:00", self.cfg, tz=z), "2026-03-08")
        # a fixed -05:00 would have put 08:30 UTC at 03:30, the day before
        self.assertEqual(profile.day_of("2026-03-08T08:30:00+00:00", self.cfg, tz=profile.zone("-05:00")), "2026-03-07")
        start, end = profile.day_bounds("2026-03-07", self.cfg, tz=z)
        self.assertEqual((end - start), dt.timedelta(hours=24))  # wall-clock arithmetic in one tzinfo
        self.assertEqual(end.astimezone(UTC).isoformat(), "2026-03-08T08:00:00+00:00")
        self.assertEqual(start.astimezone(UTC).isoformat(), "2026-03-07T09:00:00+00:00")

    def test_named_zone_when_this_machine_has_data(self):
        from chitragupta import profile
        try:
            z = profile.zone("America/New_York")
        except ValueError:
            self.skipTest("no time zone data on this machine")
        self.assertEqual(profile.day_of("2026-03-08T07:30:00+00:00", self.cfg, tz=z), "2026-03-07")
        self.assertEqual(profile.day_of("2026-03-08T08:30:00+00:00", self.cfg, tz=z), "2026-03-08")
        s, e = profile.day_bounds("2026-03-08", self.cfg, tz=z)
        self.assertEqual((e.astimezone(UTC) - s.astimezone(UTC)), dt.timedelta(hours=23))

    def test_offsets_and_window(self):
        from chitragupta import profile
        self.assertEqual(profile.zone("+05:30").utcoffset(None), dt.timedelta(hours=5, minutes=30))
        self.assertIsNone(profile.zone(""))
        with self.assertRaises(ValueError):
            profile.zone("+25:00")
        cfg = self.config(profile={"tracking_start": "2026-10-20", "backfill_days": 30})
        self.assertEqual(profile.window_start(cfg, "2026-11-05"), "2026-10-20")
        cov = profile.coverage(cfg, "2026-11-05")
        self.assertEqual(cov["label"], "since tracking began 2026-10-20")
        cfg = self.config(profile={"tracking_start": "2026-01-01", "backfill_days": 30})
        self.assertEqual(profile.coverage(cfg, "2026-11-05")["label"], "within imported history since 2026-10-06")


class Candidates(Base, unittest.TestCase):
    def test_git_config_email_is_a_suggestion_until_confirmed(self):
        from chitragupta import common, configstore, profile
        git(self.repo, "config", "user.email", ME2)
        cfg = self.config()
        res = profile.discover(cfg)
        c = next(x for x in res["candidates"] if x["kind"] == "email")
        self.assertEqual(c["value"], ME2)
        self.assertIn("git config user.email in service-api", c["source"])
        self.assertNotIn(ME2, common.load_config()["git_author_emails"])
        cur = profile.read_candidates()
        self.assertFalse(next(x for x in cur["candidates"] if x["cid"] == c["cid"])["confirmed"])
        profile.confirm(c["cid"])
        self.assertIn(ME2, common.load_config()["git_author_emails"])
        self.assertTrue(any("confirmed" in (r.get("note") or "") for r in configstore.history(5)))
        with self.assertRaises(ValueError):
            profile.confirm(c["cid"])  # already yours
        masked = profile.masked_candidates(profile.read_candidates())
        self.assertNotIn(ME2, json.dumps(masked))

    def test_bot_suggestion_cant_be_confirmed(self):
        from chitragupta import profile
        git(self.repo, "config", "user.email", "build-bot@acme.test")
        res = profile.discover(self.config())
        c = next(x for x in res["candidates"] if x["kind"] == "email")
        self.assertTrue(profile.read_candidates()["candidates"][0]["bot"])
        with self.assertRaises(ValueError):
            profile.confirm(c["cid"])

    def test_context_text_has_no_email_or_id(self):
        from chitragupta import profile
        self.assertEqual(profile.context_text(self.config(git_author_emails=[])), "")
        cfg = self.config(git_author_emails=[ME], profile={
            "display_name": "Dev Example", "focus": "Payments API v2 migration",
            "accounts": [{"provider": "jira", "instance": "acme.atlassian.net", "id": "acct-me"},
                         {"provider": "github", "instance": "github.com", "id": "1001", "login": "dev-example"}]})
        text = profile.context_text(cfg)
        self.assertIn("Dev Example", text)
        self.assertIn("Payments API v2", text)
        self.assertIn("service-api", text)
        for secret in (ME, "acct-me", "1001", "dev-example"):
            self.assertNotIn(secret, text)
        self.assertLessEqual(len(text), profile.CONTEXT_MAX)


class Reconcile(Base, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from chitragupta import common
        self.today = common.workday(common.now(), self.cfg)
        self.sent = (dt.date.fromisoformat(self.today) - dt.timedelta(days=3)).isoformat()
        common.write_json(common.hp("data", "timesheets", self.sent + ".json"), {"status": "sent"})

    def rec(self, sha, ae, repo="service-api"):
        r = {"sha": sha, "short": sha[:8], "repo_name": repo, "date": "2026-11-01T10:00:00+00:00", "ref": "main",
             "subject": "work", "ticket": "ABC-1001"}
        if ae:
            r["ae"] = ae
        return r

    def seed(self):
        from chitragupta import common, store as S
        mine = commit(self.repo, "x.txt", "x", "my commit", author=("Dev Example", ME))
        theirs = commit(self.repo, "y.txt", "y", "a colleague's commit", author=("Colleague", OTHER))
        legacy = commit(self.repo, "z.txt", "z", "an older colleague's commit", author=("Colleague", OTHER))
        tk = S.new_ticket("ABC-1001")
        tk["commits"] = {mine: self.rec(mine, ME), theirs: self.rec(theirs, OTHER), legacy: self.rec(legacy, None)}
        common.write_json(S.ticket_path("ABC-1001"), tk)
        for d in (self.today, self.sent):
            dd = S.new_day(d)
            dd["commits"] = [self.rec(mine, ME), self.rec(theirs, OTHER)]
            common.write_json(S.day_path(d), dd)
        return mine, theirs, legacy

    def run_(self, state):
        from chitragupta import common, profile
        from chitragupta.store import Store
        st = Store()
        res = profile.reconcile(st, common.load_config(), state)
        st.save()
        return st, res

    def test_archives_restores_and_is_idempotent(self):
        from chitragupta import common, store as S
        mine, theirs, legacy = self.seed()
        state = {}
        st, res = self.run_(state)
        self.assertEqual(res["archived"], 3)  # theirs + legacy (verified through git) on the ticket, theirs today
        tk = common.read_json(S.ticket_path("ABC-1001"))
        self.assertEqual(sorted(tk["commits"]), [mine])
        self.assertEqual(sorted(tk["commits_excluded"]), sorted([theirs, legacy]))
        self.assertIn("not authored by a confirmed email", tk["commits_excluded"][theirs]["excluded"]["reason"])
        self.assertNotIn(OTHER, json.dumps(tk))  # an archived record keeps no one else's address
        self.assertEqual([c["sha"] for c in common.read_json(S.day_path(self.today))["commits"]], [mine])
        self.assertEqual(len(common.read_json(S.day_path(self.sent))["commits"]), 2)  # a sent day is history
        self.assertTrue(res["backup"] and os.path.isdir(res["backup"]))
        self.assertTrue(os.path.exists(os.path.join(res["backup"], "data", "tickets", "ABC-1001.json")))
        self.assertIsNone(self.run_(state)[1])  # same profile: nothing to do
        # confirming the colleague's address (say it was really yours) brings the records back
        self.config(git_author_emails=[ME, OTHER])
        _, res = self.run_(state)
        self.assertEqual(res["restored"], 3)
        tk = common.read_json(S.ticket_path("ABC-1001"))
        self.assertEqual(sorted(tk["commits"]), sorted([mine, theirs, legacy]))
        self.assertNotIn("commits_excluded", tk)
        self.assertNotIn("excluded", tk["commits"][theirs])

    def test_removing_an_alias_archives_and_adding_one_backfills(self):
        from chitragupta import common
        mine, _, _ = self.seed()
        state = {"last_commit_scan": common.now_iso(), "commits_seen": {"c" * 40: common.now_iso()}}
        self.run_(state)
        self.config(git_author_emails=[ME, ME2])
        self.run_(state)
        self.assertLess(common.parse_ts(state["last_commit_scan"]), common.now() - dt.timedelta(days=1))
        self.assertNotIn("c" * 40, state["commits_seen"])  # rescanned, records dedupe by sha
        self.config(git_author_emails=[ME2])
        _, res = self.run_(state)
        from chitragupta import store as S
        tk = common.read_json(S.ticket_path("ABC-1001"))
        self.assertNotIn(mine, tk["commits"])
        self.assertIn(mine, tk["commits_excluded"])

    def test_tick_writes_the_context_cache(self):
        from chitragupta import common, profile
        from chitragupta.store import Store
        cfg = self.config(profile={"display_name": "Dev Example"})
        state = {}
        profile.tick(Store(), cfg, state)
        self.assertIn("Dev Example", common.read_text(profile.context_path()))
        self.assertEqual(state["profile_candidates_day"], common.workday(common.now(), cfg))

    def test_ownership_follows_the_confirmed_jira_account(self):
        """Ownership recorded against one Jira account says nothing once that account is removed or replaced; after a
        change the cached card is compared again with the new account (no Jira call), and the old history is kept but
        never read as yours."""
        from chitragupta import common, profile, store as S
        def acct(i):
            return {"profile": {"accounts": [{"provider": "jira", "instance": "acme.atlassian.net", "id": i}]}}
        common.write_json(S.ticket_path("ABC-1001"), S.new_ticket("ABC-1001"))
        common.write_json(common.hp("data", "jira", "ABC-1001.json"), {
            "key": "ABC-1001", "fetched_at": "2026-11-01T10:00:00+00:00", "fields": "summary,assignee",
            "raw": {"key": "ABC-1001", "fields": {"assignee": {"accountId": "acct-me", "displayName": "Dev Example"}}}})
        state = {}
        self.config(**acct("acct-me"))
        st, res = self.run_(state)
        self.assertEqual(res["ownership"], 1)
        tk = common.read_json(S.ticket_path("ABC-1001"))
        self.assertEqual(profile.Policy(common.load_config()).ticket_state("ABC-1001", tk)[0], "assigned")
        # removed: unknown, never "assigned" from what the old account recorded
        self.config(profile={})
        self.assertEqual(profile.Policy(common.load_config()).ticket_state("ABC-1001", tk)[0], "unknown")
        # replaced by another account (the card's assignee isn't it): other, and the old "you" isn't history of it
        self.config(**acct("acct-new"))
        pol = profile.Policy(common.load_config())
        self.assertEqual(pol.ticket_state("ABC-1001", tk)[0], "unknown")  # before the recompute
        st, res = self.run_(state)
        tk = common.read_json(S.ticket_path("ABC-1001"))
        self.assertEqual(pol.ticket_state("ABC-1001", tk)[0], "other")  # not "reassigned": "you" was another account
        self.assertEqual([h["account"] for h in tk["ownership"]["history"]], ["acct-me", "acct-new"])
        self.assertEqual(pol.ticket_ownership("ABC-1001", tk)["history"], [{"at": "2026-11-01T10:00:00+00:00", "state": "other"}])
        self.assertIsNotNone(res["backup"])
        self.assertIsNone(self.run_(state)[1])  # nothing changed: nothing to do


if __name__ == "__main__":
    unittest.main()
