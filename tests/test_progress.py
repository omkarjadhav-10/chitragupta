"""Personal progress (chitragupta/progress.py): days in the profile zone (DST included), zero kept apart from unknown /
partial / stale, records with ties and coverage, factual and repeatable milestone sentences, no next actions on a day
off, tickets completed only from Jira status history while the ticket was yours, weekly wins, reflections, and the
surfaces (CLI, MCP get_progress, the SessionStart line). Synthetic ledgers: developer@acme.test, service-api, ABC-…"""
import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.support import child_env, rmtree, run_hook
from tests.test_profile import ME, Base, DstZone

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 3, 10, 15, 0, tzinfo=UTC)  # a Tuesday
JIRA = {"provider": "jira", "instance": "acme.atlassian.net", "id": "acct-1001", "login": ""}


def commit(sha, when, added=1, deleted=0, role="author", coauthored=False, state="counted", **kw):
    return dict({"sha": sha, "author_date": when, "committed_at": when, "role": role, "coauthored": coauthored,
                 "subject": "ABC-1001 change", "ticket": "ABC-1001", "added": added, "deleted": deleted, "files": 1,
                 "binary_files": 0, "excluded": {"added": 0, "deleted": 0, "files": 0}, "patch_id": "p" + sha,
                 "state": state, "reachable": True}, **kw)


def pr(n, created, merged=None, state="open", review="", draft=False):
    return {"number": n, "title": "ABC-1001 change %d" % n, "url": "https://github.com/acme-test/service-api/pull/%d" % n,
            "state": "merged" if merged else state, "draft": draft, "review": review, "created_at": created,
            "merged_at": merged or "", "target": "main", "branch": "feature/ABC-1001", "ticket": "ABC-1001",
            "merge_commit": "", "head": ""}


class Case(Base, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.config(profile={"timezone": "+00:00", "tracking_start": "2026-03-01", "accounts": [JIRA]})

    def ledger(self, commits=(), prs=(), path=None, status="ok", scanned=None, pr_status="ok", sig=None):
        from chitragupta import activity, common, gitcmd, profile
        path = path or self.repo
        at = (scanned or (NOW - dt.timedelta(minutes=10))).isoformat(timespec="seconds")
        led = activity.new_ledger({"id": "path:" + path, "name": os.path.basename(path), "path": path})
        led.update(profile_sig=sig or profile.signature(common.load_config()), window_start="2026-03-01")
        led["commits"].update(status=status, scanned_at=at, full_at=at, items={c["sha"]: c for c in commits})
        led["prs"].update(status=pr_status, fetched_at=at, source="gh", items={str(p["number"]): p for p in prs})
        common.write_json(activity.ledger_path(gitcmd.repo_key(path)), led)

    def ctx(self, tz=None):
        from chitragupta import progress
        return progress.Ctx(None, NOW, **({"tz": tz} if tz else {}))

    def day(self, d, tz=None, evidence=False):
        from chitragupta import progress
        return progress.day_metrics(self.ctx(tz), d, evidence=evidence)


class Days(Case):
    def test_dst_boundary_in_the_profile_zone(self):
        self.ledger([commit("a", "2026-03-08T07:30:00+00:00"),   # 03:30 local after the switch: still the 7th
                     commit("b", "2026-03-08T08:30:00+00:00"),   # 04:30 local: the 8th
                     commit("c", "2026-03-09T08:30:00+00:00")])
        z = DstZone()
        self.assertEqual(self.day("2026-03-07", z)["commits"]["value"], 1)
        self.assertEqual(self.day("2026-03-08", z)["commits"]["value"], 1)
        self.assertEqual(self.day("2026-03-09", z)["commits"]["value"], 1)
        # the same instants in a fixed UTC-5 zone fall on other days: the zone decides, not the machine
        self.assertEqual(self.day("2026-03-07", dt.timezone(dt.timedelta(hours=-5)))["commits"]["value"], 2)

    def test_zero_unknown_partial_and_stale(self):
        from chitragupta import activity, common, gitcmd
        self.ledger([commit("a", "2026-03-09T10:00:00+00:00")])
        self.assertEqual(self.day("2026-03-08")["commits"]["status"], "zero")
        self.assertEqual(self.day("2026-03-09")["commits"]["status"], "known")
        self.assertEqual(self.day("2026-02-20")["commits"]["status"], "unknown")  # before the history window
        self.assertEqual(self.day("2026-03-11")["commits"]["status"], "unknown")  # not yet
        self.assertIsNone(self.day("2026-02-20")["commits"]["value"])
        # a second repository whose scan failed: partial, never a confident number
        other = os.path.join(self.home, "work", "web-app")
        os.makedirs(other)
        self.cfg = self.config(watched_repos=[self.repo, other])
        self.ledger([commit("a", "2026-03-09T10:00:00+00:00")])
        self.ledger([], path=other, status="error")
        led = common.read_json(activity.ledger_path(gitcmd.repo_key(other)))
        led["commits"]["scanned_at"] = ""  # its scan never succeeded
        common.write_json(activity.ledger_path(gitcmd.repo_key(other)), led)
        m = self.day("2026-03-09")
        self.assertEqual((m["commits"]["value"], m["commits"]["status"]), (1, "partial"))
        self.assertIn("web-app", m["commits"]["why"])
        # a scan that failed after an earlier one succeeded: the earlier results stay, labelled "not current" from the
        # day of that read on, with the failure; days that read finished are complete
        led["commits"].update(scanned_at=(NOW - dt.timedelta(hours=26)).isoformat(timespec="seconds"), error="git log: synthetic")
        common.write_json(activity.ledger_path(gitcmd.repo_key(other)), led)
        m = self.day("2026-03-10")
        self.assertEqual(m["commits"]["status"], "stale")
        self.assertIn("synthetic", m["commits"]["why"])
        self.assertEqual(self.day("2026-03-07")["commits"]["status"], "zero")
        # never collected: unknown
        os.remove(activity.ledger_path(gitcmd.repo_key(other)))
        self.cfg = self.config(watched_repos=[other])
        self.assertEqual(self.day("2026-03-09")["commits"]["status"], "unknown")
        # read hours ago: today's numbers are stale
        self.cfg = self.config(watched_repos=[self.repo])
        self.ledger([], scanned=NOW - dt.timedelta(hours=5))
        self.assertEqual(self.day("2026-03-10")["commits"]["status"], "stale")
        # a ledger from another profile: updating, not shown
        self.ledger([commit("a", "2026-03-09T10:00:00+00:00")], sig="0" * 16)
        m = self.day("2026-03-09")
        self.assertEqual((m["commits"]["status"], m["commits"]["value"]), ("unknown", None))
        self.assertIn("updating", m["commits"]["why"])
        self.assertTrue(common.read_json(activity.ledger_path(gitcmd.repo_key(self.repo))))

    def test_lines_shared_and_coauthored_and_not_counted(self):
        self.ledger([commit("a", "2026-03-09T10:00:00+00:00", added=420, deleted=180),
                     commit("b", "2026-03-09T11:00:00+00:00", added=10, coauthored=True),
                     commit("c", "2026-03-09T12:00:00+00:00", added=20, role="coauthor"),
                     commit("d", "2026-03-09T13:00:00+00:00", added=99, state="equivalent", equivalent_to="a")])
        m = self.day("2026-03-09", evidence=True)
        self.assertEqual(m["commits"]["value"], 2)
        self.assertEqual((m["lines"]["added"], m["lines"]["deleted"]), (420, 180))
        self.assertEqual(m["shared"]["commits"], 1)
        self.assertEqual(m["coauthored"]["added"], 20)
        self.assertEqual([c["sha"] for c in m["evidence"]["not_counted"]], ["d"])


class Records(Case):
    def test_ties_coverage_and_recalculation(self):
        from chitragupta import progress
        self.ledger([commit(s, "2026-03-03T10:00:00+00:00") for s in "abc"] +
                    [commit(s, "2026-03-05T10:00:00+00:00") for s in "def"],
                    [pr(1, "2026-03-05T10:00:00Z"), pr(2, "2026-03-05T11:00:00Z")])
        r = {x["id"]: x for x in progress.records(self.ctx())["records"]}
        self.assertEqual((r["commits"]["value"], r["commits"]["date"], r["commits"]["ties"]), (3, "2026-03-03", 1))
        self.assertEqual(r["commits"]["coverage"], "since tracking began 2026-03-01")
        self.assertEqual((r["activity"]["date"], r["activity"]["value"]), ("2026-03-05", 5))
        self.assertEqual(r["activity"]["breakdown"]["prs_raised"], 2)
        self.assertIn("not a score", r["activity"]["definition"])
        self.assertNotIn("all-time", json.dumps(r).lower())
        # imported history that doesn't reach the tracking start says so
        self.cfg = self.config(profile={"timezone": "+00:00", "tracking_start": "2025-01-01", "backfill_days": 30,
                                        "accounts": [JIRA]})
        self.ledger([commit("a", "2026-03-03T10:00:00+00:00")])
        self.assertTrue(progress.records(self.ctx())["coverage"]["label"].startswith("within imported history since"))
        # an identity change makes the old ledger invisible until it is rebuilt: records recalculate
        self.cfg = self.config(git_author_emails=["dev.alt@acme.test"])
        r = {x["id"]: x for x in progress.records(self.ctx())["records"]}
        self.assertIsNone(r["commits"]["value"])

    def test_milestones_are_factual_repeatable_and_optional(self):
        from chitragupta import progress
        self.ledger([], [pr(i, "2026-03-05T1%d:00:00Z" % i) for i in range(4)] +
                    [pr(10 + i, "2026-03-10T1%d:00:00Z" % i) for i in range(3)] +
                    [pr(20, "2026-03-04T10:00:00Z", merged="2026-03-10T12:00:00Z"),
                     pr(21, "2026-03-04T11:00:00Z", merged="2026-03-10T13:00:00Z")])
        first = progress.celebrations(self.ctx())
        self.assertEqual(first[0], "You opened 3 PRs today and merged 2. Your highest PR-opening day since tracking "
                                   "began 2026-03-01 was 4.")
        self.assertEqual(progress.celebrations(self.ctx()), first)
        self.config(profile={"timezone": "+00:00", "tracking_start": "2026-03-01", "accounts": [JIRA],
                             "celebrations": False})
        self.assertEqual(progress.celebrations(self.ctx()), [])

    def test_empty_day_note_never_blames(self):
        from chitragupta import progress
        self.ledger([])
        t = progress.today(None, NOW)
        self.assertIn("reviews", t["note"])
        self.assertNotIn("nothing", t["note"].lower())


class Tickets(Case):
    def owner(self, key, history):
        from chitragupta import common, store
        common.write_json(store.ticket_path(key), {"id": key, "ownership": {
            "state": history[-1]["state"], "account": JIRA["id"], "history": [dict(h, account=JIRA["id"]) for h in history]}})

    def evening(self, changes, changelogs=True):
        from chitragupta import common
        common.write_json(common.hp("data", "jira", "_evening", "2026-03-10.json"),
                          {"day": "2026-03-10", "facts": {"status_changes": changes}, "changelogs": [] if changelogs else None})

    def ch(self, key, time, to):
        return {"ticket": key, "time": time, "from": "In Progress", "to": to, "mine": True}

    def test_completed_only_while_yours_and_once_per_day(self):
        self.ledger([])
        self.cfg = self.config(profile={"timezone": "+00:00", "tracking_start": "2026-03-01", "accounts": [JIRA],
                                        "include_tickets": {"ABC-1003": "pairing on the API contract"}})
        self.owner("ABC-1001", [{"at": "2026-03-01T09:00:00+00:00", "state": "you"}])
        self.owner("ABC-1002", [{"at": "2026-03-01T09:00:00+00:00", "state": "you"},
                                {"at": "2026-03-09T09:00:00+00:00", "state": "other"}])
        self.evening([self.ch("ABC-1001", "2026-03-10T10:00:00+00:00", "Done"),
                      self.ch("ABC-1001", "2026-03-10T11:00:00+00:00", "In Progress"),
                      self.ch("ABC-1001", "2026-03-10T12:00:00+00:00", "DONE"),
                      self.ch("ABC-1002", "2026-03-10T10:00:00+00:00", "Done"),
                      self.ch("ABC-1003", "2026-03-10T10:00:00+00:00", "Closed")])
        m = self.day("2026-03-10", evidence=True)
        self.assertEqual((m["tickets_completed"]["value"], m["tickets_completed"]["status"]), (2, "known"))
        self.assertEqual(sorted(t["key"] for t in m["evidence"]["tickets_completed"]), ["ABC-1001", "ABC-1003"])
        # a completion whose owner at the time isn't recorded: partial, never guessed
        self.evening([self.ch("ABC-1004", "2026-03-10T10:00:00+00:00", "Done")])
        m = self.day("2026-03-10")
        self.assertEqual((m["tickets_completed"]["value"], m["tickets_completed"]["status"]), (0, "partial"))

    def test_without_status_history_or_account_it_is_unknown(self):
        self.ledger([])
        self.evening([self.ch("ABC-1001", "2026-03-10T10:00:00+00:00", "Done")], changelogs=False)
        self.assertEqual(self.day("2026-03-10")["tickets_completed"]["status"], "unknown")
        self.cfg = self.config(profile={"timezone": "+00:00", "tracking_start": "2026-03-01"})
        self.evening([self.ch("ABC-1001", "2026-03-10T10:00:00+00:00", "Done")])
        m = self.day("2026-03-10")
        self.assertEqual(m["tickets_completed"]["status"], "unknown")
        self.assertIn("Jira account", m["tickets_completed"]["why"])


class Actions(Case):
    TICKETS = [{"key": "ABC-1001", "ownership": {"current": True, "state": "assigned", "reason": "assigned to you in Jira"},
                "reminders": [{"text": "UAT promotion is needed"}], "alerts": []},
               {"key": "ABC-1002", "ownership": {"current": False, "state": "reassigned", "reason": "reassigned"},
                "reminders": [{"text": "someone else's release step"}], "alerts": [{"text": "not yours"}]}]

    def test_open_prs_and_your_tickets_only(self):
        from chitragupta import progress
        self.ledger([], [pr(1, "2026-03-02T10:00:00Z", review="approved"), pr(2, "2026-03-01T10:00:00Z", state="draft",
                                                                              draft=True)])
        with mock.patch.object(progress.Ctx, "tickets", lambda self: Actions.TICKETS):
            na = progress.next_actions(self.ctx())
        texts = [a["text"] for a in na["actions"]]
        self.assertTrue(texts[0].endswith("PR #1 is approved: merge it when you're ready."))
        self.assertIn("ABC-1001: UAT promotion is needed", texts)
        self.assertFalse(any("ABC-1002" in t for t in texts))
        self.assertEqual(len(texts), 3)
        self.assertFalse(any(w in json.dumps(na).lower() for w in ("stuck", "blocked", "inactive")))

    def test_day_off_has_no_actions_and_no_streaks(self):
        from chitragupta import common, progress
        common.write_json(common.hp("data", "calendar.json"), {"holiday": {"2026-03-10": "Synthetic holiday"}})
        self.ledger([], [pr(1, "2026-03-02T10:00:00Z", review="approved")])
        t = progress.today(None, NOW)
        self.assertTrue(t["next_actions"]["day_off"])
        self.assertEqual(t["next_actions"]["actions"], [])
        self.assertNotIn("streak", json.dumps(t).lower())


class WinsAndNotes(Case):
    def test_weekly_wins_are_factual_and_shareable(self):
        from chitragupta import common, progress, store
        self.ledger([commit("a", "2026-03-09T10:00:00+00:00", added=5)],
                    [pr(3, "2026-03-04T10:00:00Z", merged="2026-03-09T12:00:00Z")])
        common.write_json(store.day_path("2026-03-09"), {"date": "2026-03-09", "summary": "Traced the cancel flow."})
        progress.save_reflection("2026-03-10", "Helped a teammate debug the order sync.", now=NOW)
        w = progress.weekly_wins(None, "2026-03-10", NOW)
        md = w["markdown"]
        self.assertIn("## Merged PRs", md)
        self.assertIn("https://github.com/acme-test/service-api/pull/3", md)
        self.assertIn("Traced the cancel flow.", md)
        self.assertIn("## Your notes (self-reported)", md)
        self.assertNotIn("@acme.test", w["copy_text"])
        self.assertEqual(progress.weekly_wins(None, "2026-03-10", NOW)["markdown"], md)

    def test_reflection_is_local_bounded_and_masked(self):
        from chitragupta import common, progress
        with self.assertRaises(ValueError):
            progress.save_reflection("2026-03-10", "x" * 1001)
        with self.assertRaises(ValueError):
            progress.save_reflection("10/03/2026", "ok")
        key = "AKIA" + "IOSFODNN7" + "EXAMPLE"  # built here so no key-shaped literal sits in the repo
        rec = progress.save_reflection("2026-03-10", "Rotated key %s with the team" % key, now=NOW)
        self.assertNotIn(key, rec["text"])
        self.assertTrue(os.path.exists(common.hp("data", "reflections", "2026-03.json")))
        self.assertFalse(os.path.exists(common.hp("daily")) and any(
            "Rotated" in open(os.path.join(r, f), encoding="utf-8").read()
            for r, _, fs in os.walk(common.hp("daily")) for f in fs))
        again = progress.save_reflection("2026-03-10", "Updated note", now=NOW + dt.timedelta(hours=1))
        self.assertEqual(again["at"], rec["at"])
        self.assertEqual(progress.read_reflection("2026-03-10")["text"], "Updated note")


class Surfaces(Case):
    def test_mcp_progress_has_no_email_unless_asked(self):
        from chitragupta import common, mcpserver
        self.ledger([commit("a", "2026-03-09T10:00:00+00:00")])
        cfg = common.load_config()
        with mock.patch.object(common, "now", lambda: NOW):
            text = mcpserver.HANDLERS["get_progress"]({"period": "today"}, cfg)
            self.assertNotIn(ME, text)
            self.assertNotIn("acct-1001", text)
            self.assertIn(ME, mcpserver.HANDLERS["get_progress"]({"period": "today", "include_ids": True}, cfg))
            for period in ("week", "month", "records", "wins"):
                self.assertNotIn(ME, mcpserver.HANDLERS["get_progress"]({"period": period}, cfg))
        with self.assertRaises(mcpserver.ToolError):
            mcpserver.HANDLERS["get_progress"]({"period": "forever"}, cfg)
        self.assertIn("get_progress", mcpserver.TOOL_NAMES)

    def test_cli_progress_json_and_reflect(self):
        from chitragupta import cli, common, progress
        self.ledger([commit("a", "2026-03-09T10:00:00+00:00")])
        buf = io.StringIO()
        with mock.patch.object(common, "now", lambda: NOW), contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["progress", "today", "--json", "--date", "2026-03-09"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["metrics"]["commits"]["value"], 1)
        self.assertNotIn(ME, buf.getvalue())
        with mock.patch.object(common, "now", lambda: NOW), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["progress", "reflect", "Paired on the release plan", "--date", "2026-03-10"]), 0)
        self.assertEqual(progress.read_reflection("2026-03-10")["text"], "Paired on the release plan")

    def test_session_start_appends_only_the_cached_profile_line(self):
        from chitragupta import common
        env = child_env(self.home, self.user_dir)
        cwd = tempfile.mkdtemp(prefix="cg-cwd-")
        self.addCleanup(rmtree, cwd)
        common.write_text(common.wl("cache", "profile-context.md"),
                          "Developer profile: Dev Example. Focus: the order service.\n")
        p = run_hook("session-start", {"session_id": "s1", "cwd": cwd, "source": "startup"}, env)
        self.assertEqual(p.returncode, 0)
        ctx = json.loads(p.stdout.decode())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Developer profile: Dev Example.", ctx)
        os.remove(common.wl("cache", "profile-context.md"))
        os.makedirs(common.wl("cache", "profile-context.md"))  # unreadable: the hook still exits 0
        p = run_hook("session-start", {"session_id": "s2", "cwd": cwd, "source": "startup"}, env)
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
