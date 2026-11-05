"""The Profile and My progress pages' server side: /api/profile (masked unless revealed), /api/profile/preview (the
effect of a change, nothing written), /api/progress* (never an email or account id), the profile-discover and
reflection-save actions behind the usual write checks, and the change cursor.

add_progress_fixtures() is also what scripts/ui/run.py adds for the browser checks: a confirmed identity (Dev Example,
developer@acme.test, a github.com account, a Jira account), repo service-api with an activity ledger over several days
(a shared commit, a co-authored one, an excluded lockfile, a rebased copy, a future-dated commit), PRs (a draft, one
raised yesterday and merged today), an evening Jira status change, ABC-1002 included by hand, ABC-1004 reassigned away,
a reflection and identity suggestions (one a bot).
"""
import datetime as _dt
import json
import os
import re
import unittest
from unittest import mock

from tests import test_dashboard as td
from tests.test_dashboard import _w

from chitragupta import common

EMAIL = "developer@acme.test"
GH_ID = "70000001"
JIRA_ID = "557058:0f0e1d2c-dev-example"
REPO = "C:/src/service-api"


def add_progress_fixtures(home, days):
    """Synthetic profile, activity ledger, Jira facts, ownership, reflection and suggestions on the build_home() home."""
    from chitragupta import profile
    day, yday = days["day"], days["yday"]
    today = _dt.date.fromisoformat(day)

    def d(n):
        return (today - _dt.timedelta(days=n)).isoformat()

    def at(dd, hhmm):
        return _dt.datetime.fromisoformat("%sT%s:00" % (dd, hhmm)).astimezone().isoformat(timespec="seconds")

    p = os.path.join(home, ".worklog", "config.json")
    cfg = common.read_json(p, {})
    cfg.update(git_author_emails=[EMAIL], watched_repos=sorted(set((cfg.get("watched_repos") or []) + [REPO])))
    cfg["profile"] = {"display_name": "Dev Example", "email": EMAIL, "tracking_start": d(45),
                      "focus": "Checkout totals and the order export.",
                      "accounts": [{"provider": "github", "instance": "github.com", "id": GH_ID, "login": "dev-example",
                                    "source": "gh api user (the account gh is signed in as)", "confirmed_at": at(d(30), "09:00")},
                                   {"provider": "jira", "instance": "acme.atlassian.net", "id": JIRA_ID, "login": "",
                                    "source": "Jira user lookup for your primary email", "confirmed_at": at(d(30), "09:01")}],
                      "include_tickets": {"ABC-1002": "pairing on the checkout contract"}}
    _w(p, cfg)
    _w(os.path.join(home, "data", "git", "service-api", "repo.json"),
       {"host": "github", "org": "acme-test", "repo": "service-api", "path": "acme-test/service-api"})
    cfg = common.load_config()
    sig = profile.signature(cfg)
    window = profile.window_start(cfg, day)

    n = [0]

    def commit(dd, hhmm, subject, added, deleted, **kw):
        n[0] += 1
        sha = ("%02x" % n[0]) * 20
        it = {"sha": sha, "author_date": at(dd, hhmm), "committed_at": at(dd, hhmm), "role": "author", "coauthored": False,
              "subject": subject, "ticket": "ABC-1001", "added": added, "deleted": deleted, "files": 2, "binary_files": 0,
              "excluded": {"added": 0, "deleted": 0, "files": 0}, "patch_id": "p%02d" % n[0], "state": "counted",
              "equivalent_to": "", "integration_of": "", "reachable": True, "first_seen": at(dd, hhmm)}
        it.update(kw)
        return it
    items = [commit(day, "10:05", "ABC-1001 Apply the discount once", 120, 30),
             commit(day, "11:20", "ABC-1001 Add the order total test", 40, 12,
                    excluded={"added": 300, "deleted": 120, "files": 1}),
             commit(day, "11:40", "ABC-1001 Pair on rounding", 60, 20, coauthored=True),
             commit(day, "09:30", "ABC-1002 Checkout contract (co-authored)", 15, 5, role="coauthor", ticket="ABC-1002")]
    items.append(commit(day, "10:05", "ABC-1001 Apply the discount once", 120, 30, state="equivalent",
                        equivalent_to=items[0]["sha"], reachable=False))
    items += [commit(yday, "16:10", "ABC-1003 Refund report query", 25, 4, ticket="ABC-1003"),
              commit(yday, "17:45", "ABC-1003 Refund report totals", 18, 9, ticket="ABC-1003")]
    items += [commit(d(3), "%02d:15" % h, "ABC-1001 Order total step %d" % h, 10 * h, h) for h in (9, 10, 11, 14, 16)]
    items += [commit(d(10), "%02d:30" % h, "ABC-1002 Checkout timeout %d" % h, 5 * h, 2) for h in (10, 13, 15)]
    items.append(commit((today + _dt.timedelta(days=2)).isoformat(), "09:00", "ABC-1001 Clock skew", 3, 1, state="future"))

    def pr(num, title, state, created, merged=None, **kw):
        out = {"number": num, "title": title, "url": "https://github.com/acme-test/service-api/pull/%d" % num,
               "state": state, "draft": state == "draft", "review": "", "created_at": created, "merged_at": merged or "",
               "target": "main", "branch": "feature/ABC-1001-total", "ticket": "ABC-1001", "merge_commit": "", "head": ""}
        out.update(kw)
        return out
    prs = [pr(12, "ABC-1001 Apply the discount once", "merged", at(yday, "15:00"), at(day, "10:30")),
           pr(14, "ABC-1001 Order total test (draft)", "draft", at(day, "11:00")),
           pr(15, "ABC-1003 Review notes from " + EMAIL, "open", at(day, "11:45"), review="approved", ticket="ABC-1003"),
           pr(9, "ABC-1001 Order total step", "merged", at(d(4), "10:00"), at(d(3), "12:00")),
           pr(10, "ABC-1002 Checkout timeout", "merged", at(d(3), "09:40"), at(d(3), "17:00"), ticket="ABC-1002")]
    now_iso = common.now().isoformat(timespec="seconds")
    ledger = {"v": 1, "profile_sig": sig, "window_start": window,
              "repo": {"id": "github:github.com/acme-test/service-api", "path": REPO, "name": "service-api", "key": "service-api",
                       "provider": "github", "instance": "github.com",
                       "remote": {"host": "github", "org": "acme-test", "repo": "service-api", "path": "acme-test/service-api"}},
              "commits": {"status": "ok", "why": "", "scanned_at": now_iso, "full_at": now_iso, "since": window, "error": "",
                          "items": {it["sha"]: it for it in items}},
              "prs": {"status": "ok", "why": "", "source": "gh", "account": "github:github.com:" + GH_ID,
                      "fetched_at": now_iso, "error": "", "capped": False, "items": {str(x["number"]): x for x in prs}}}
    _w(os.path.join(home, "data", "activity", "service-api.json"), ledger)
    # Jira: ABC-1001 assigned to you (and moved to Done today), ABC-1004 reassigned away from you
    _w(os.path.join(home, "data", "jira", "_evening", day + ".json"), {
        "day": day, "fetched_at": now_iso, "jql": "key in (ABC-1001)", "changelogs": [],
        "facts": {"comments": [], "worklogs": [],
                  "status_changes": [{"ticket": "ABC-1001", "time": at(day, "11:50"), "from": "In Review", "to": "Done", "mine": True}]}})
    tp = os.path.join(home, "data", "tickets", "ABC-1001.json")
    tk = common.read_json(tp, {})
    tk["ownership"] = {"state": "you", "account": JIRA_ID, "checked_at": now_iso,
                       "history": [{"at": at(days["old"], "09:00"), "state": "you", "account": JIRA_ID}]}
    _w(tp, tk)
    _w(os.path.join(home, "data", "tickets", "ABC-1004.json"), {
        "id": "ABC-1004", "title": "Invoice rounding", "title_source": "jira", "first": d(20), "last": d(6),
        "timeline": {d(6): {"minutes": 50}},
        "ownership": {"state": "other", "account": JIRA_ID, "checked_at": now_iso,
                      "history": [{"at": at(d(20), "09:00"), "state": "you", "account": JIRA_ID},
                                  {"at": at(d(5), "09:00"), "state": "other", "account": JIRA_ID}]}})
    _w(os.path.join(home, "data", "reflections", yday[:7] + ".json"), {"days": {yday: {
        "text": "Paired with QA on the checkout contract; no code today.", "at": at(yday, "18:00"), "updated_at": at(yday, "18:00")}}})
    profile._merge_candidates([
        {"kind": "email", "value": EMAIL, "origin": "git", "seen_at": now_iso, "repos": ["service-api"], "bot": False,
         "source": "git config user.email in service-api"},
        {"kind": "email", "value": "dev.example@acme.test", "origin": "git", "seen_at": now_iso, "repos": ["shop-api"],
         "bot": False, "source": "git config user.email in shop-api"},
        {"kind": "email", "value": "ci-runner[bot]@acme.test", "origin": "git", "seen_at": now_iso, "repos": ["shop-api"],
         "bot": True, "source": "git config user.email in shop-api"},
        {"kind": "account", "provider": "github", "instance": "github.com", "id": GH_ID, "login": "dev-example",
         "handle": "Dev Example", "origin": "host:github", "seen_at": now_iso, "bot": False,
         "source": "gh api user (the account gh is signed in as)"}], {"git", "host:github"}, {}, now_iso)
    st = os.path.join(home, ".worklog", "state.json")
    state = common.read_json(st, {}) or {}
    state.update(profile_sig=sig, profile_aliases=[EMAIL], last_commit_scan=now_iso,
                 profile_reconciled={"signature": sig, "archived": 2, "restored": 0, "unverified": 0, "backup": None,
                                     "at": at(d(30), "09:05")})
    _w(st, state)
    return {"sig": sig, "window": window}


class ProgressBase(td.Base):
    def setUp(self):
        super().setUp()
        self.fx = add_progress_fixtures(self.home, self.days)

    def blob(self, obj):
        return json.dumps(obj, ensure_ascii=False)


class ProfileView(ProgressBase):
    def test_masked_unless_revealed(self):
        from chitragupta.dashboard import api
        d = api.profile_view()
        b = self.blob(d)
        self.assertNotIn(EMAIL, b)
        self.assertNotIn(GH_ID, b)
        self.assertNotIn(JIRA_ID, b)
        self.assertNotIn("accounts_raw", d)
        self.assertEqual(d["display_name"], "Dev Example")
        self.assertEqual(len(d["aliases"]), 1)
        self.assertEqual(d["status"]["git"]["state"], "ok")
        self.assertEqual(d["status"]["hosts"][0]["instance"], "github.com")
        self.assertEqual(d["collected"][0]["state"], "current")
        cands = {c["source"] + (c.get("value") or c.get("login") or ""): c for c in d["candidates"]["candidates"]}
        self.assertTrue(any(c["bot"] for c in cands.values()))
        self.assertTrue(any(c["confirmed"] for c in cands.values()))
        r = api.profile_view(reveal=True)
        self.assertIn(EMAIL, self.blob(r))
        self.assertEqual(r["accounts_raw"][0]["id"], GH_ID)

    def test_http_routes(self):
        from chitragupta.dashboard import server
        srv, _t = server.start_in_thread(0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.port = srv.port
        get = td.TestHttp.get
        r, body = get(self, "/api/profile")
        self.assertEqual(r.status, 200)
        self.assertNotIn(EMAIL.encode(), body)
        r, body = get(self, "/api/profile?reveal=1")
        self.assertIn(EMAIL.encode(), body)
        for path in ("/api/progress", "/api/progress?date=" + self.days["yday"], "/api/progress/week",
                     "/api/progress/calendar", "/api/progress/records", "/api/progress/wins"):
            r, body = get(self, path)
            self.assertEqual(r.status, 200, path)
            self.assertNotIn(EMAIL.encode(), body, path)
            self.assertNotIn(JIRA_ID.encode(), body, path)
        for path in ("/api/progress?date=2026-13-40", "/api/progress?date=yesterday", "/api/progress/calendar?month=2026-13"):
            self.assertEqual(get(self, path)[0].status, 400, path)
        r, body = get(self, "/api/profile/preview?changes=%7Bnope")
        self.assertEqual(r.status, 400)


class ProgressAnswers(ProgressBase):
    def test_today_counts_and_evidence(self):
        from chitragupta.dashboard import api
        t = api.progress("day")
        m = t["metrics"]
        self.assertEqual((m["commits"]["value"], m["commits"]["status"]), (3, "known"))
        self.assertEqual((m["lines"]["added"], m["lines"]["deleted"]), (160, 42))  # solo commits only
        self.assertEqual(m["shared"]["commits"], 1)
        self.assertEqual(m["coauthored"]["commits"], 1)
        self.assertEqual(m["excluded"], {"added": 300, "deleted": 120, "files": 1})
        self.assertEqual((m["prs_raised"]["value"], m["prs_raised"]["drafts"]), (2, 1))
        self.assertEqual(m["prs_merged"]["value"], 1)
        self.assertEqual(m["tickets_completed"]["value"], 1)
        self.assertEqual(len(m["evidence"]["not_counted"]), 1)
        self.assertEqual(len(m["unresolved"]), 1)  # the future-dated commit: never today
        mine = t["tickets"]
        self.assertIn("ABC-1002", [x["key"] for x in mine["current"]])
        self.assertEqual([x["key"] for x in mine["reassigned"]], ["ABC-1004"])
        self.assertTrue(t["celebrations"])

    def test_no_identity_in_any_progress_answer(self):
        from chitragupta.dashboard import api
        for kind in ("day", "week", "calendar", "records", "wins"):
            b = self.blob(api.progress(kind))
            self.assertNotIn(EMAIL, b, kind)
            self.assertNotIn(JIRA_ID, b, kind)
        wins = api.progress("wins")
        self.assertIn("#12", wins["copy_text"])
        self.assertNotIn(EMAIL, wins["copy_text"])

    def test_records_and_calendar(self):
        from chitragupta.dashboard import api
        recs = {r["id"]: r for r in api.progress("records")["records"]}
        self.assertEqual(recs["commits"]["value"], 5)
        self.assertIn("since tracking began", recs["commits"]["coverage"])
        self.assertEqual(recs["prs_merged"]["value"], 2)
        cal = api.progress("calendar")
        cells = [c for w in cal["weeks"] for c in w if c]
        self.assertTrue(any(c["date"] == self.days["day"] and c["activity"] for c in cells))
        early = [c for c in cells if c["date"] < self.fx["window"]]
        self.assertTrue(all(c["activity"] is None for c in early), "before the history window: unknown, not zero")


class Preview(ProgressBase):
    def test_preview_never_writes_and_reports_removal(self):
        from chitragupta.dashboard import api
        before = td.snapshot(self.home)
        res, st = api.profile_preview(json.dumps({"git_author_emails": []}))
        self.assertEqual(st, 200)
        self.assertTrue(res["ok"])
        self.assertTrue(res["signature_changes"])
        self.assertEqual(len(res["aliases"]["removed"]), 1)
        self.assertNotIn(EMAIL, self.blob(res))
        self.assertEqual(res["leaving"]["commits"], 14)  # every counted commit, the co-authored one too
        res, _ = api.profile_preview(json.dumps({"profile.accounts": []}))
        self.assertEqual(res["leaving"]["prs"], 5)
        self.assertEqual(len(res["accounts"]["removed"]), 2)
        self.assertNotIn(GH_ID, self.blob(res))
        res, _ = api.profile_preview(json.dumps({"profile.include_tickets": {}}))
        self.assertEqual(res["tickets"]["leaving"], ["ABC-1002"])
        res, _ = api.profile_preview(json.dumps({"git_author_emails": [EMAIL, "dev.example@acme.test"]}))
        self.assertTrue(any("next background run" in n for n in res["notes"]))
        bad, st = api.profile_preview(json.dumps({"git_author_emails": ["ci-runner[bot]@acme.test"],
                                                  "profile.repos": ["C:/elsewhere/repo"]}))
        self.assertEqual(st, 200)
        self.assertFalse(bad["ok"])
        self.assertEqual(sorted(bad["fields"]), ["git_author_emails", "profile.repos"])
        for raw in ("", "{nope", "[]", "{}", "x" * 9000):
            self.assertEqual(api.profile_preview(raw)[1], 400, raw[:10])
        self.assertEqual(td.snapshot(self.home), before, "a preview writes nothing")


class Actions(ProgressBase):
    def test_reflection_save(self):
        from chitragupta import progress, settings
        from chitragupta.dashboard import actions
        day = self.days["day"]
        st, body = actions.reflection_save({"date": day, "text": "Sketched the export design with QA."})
        self.assertEqual(st, 200, body)
        self.assertEqual(progress.read_reflection(day)["text"], "Sketched the export design with QA.")
        future = (_dt.date.fromisoformat(day) + _dt.timedelta(days=3)).isoformat()
        for bad in ({"date": "05-11-2026", "text": "x"}, {"date": day}, {"date": day, "text": 5},
                    {"date": future, "text": "x"}, {"date": day, "text": "x" * 1001}):
            self.assertEqual(actions.reflection_save(bad)[0], 400, bad)
        self.assertFalse(os.path.exists(common.hp("daily", day[:4], day[5:7], day + ".md")) and
                         "Sketched" in common.read_text(common.hp("daily", day[:4], day[5:7], day + ".md")))
        settings.apply({"profile.reflection": False})
        self.assertEqual(actions.reflection_save({"date": day, "text": "x"})[0], 403)

    def test_profile_discover(self):
        from chitragupta import profile
        from chitragupta.dashboard import actions
        self.assertEqual(actions.profile_discover({"hosts": "yes"})[0], 400)
        with mock.patch.object(profile, "discover", return_value={"candidates": [{}, {}], "errors": {"host:github": "gh: no"}}) as dv:
            st, body = actions.profile_discover({"hosts": True})
        self.assertEqual((st, body["ok"], body["candidates"]), (200, True, 2))
        dv.assert_called_once()
        self.assertTrue(dv.call_args.kwargs["hosts_too"])
        with mock.patch.object(profile, "discover") as dv:
            st, body = actions.profile_discover({"jira": True})  # the Jira MCP is off in this home
        self.assertEqual((st, body["ok"]), (200, False))
        self.assertIn("Jira is off", body["skipped"])
        dv.assert_not_called()
        from chitragupta import configstore
        self.assertIn("action:profile-discover", [h["key"] for h in configstore.history()])


class Writes(ProgressBase):
    get = td.TestHttp.get

    def setUp(self):
        super().setUp()
        from chitragupta.dashboard import server
        self.srv, self.thread = server.start_in_thread(0)
        self.port = self.srv.port
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def token(self):
        r, body = self.get("/")
        return re.search(r'<meta name="cg-token" content="([^"]+)">', body.decode("utf-8")).group(1)

    def write(self, path, payload, method="POST", token=None, origin="default", ctype="application/json", raw=None):
        headers = {"Content-Type": ctype} if ctype else {}
        if origin == "default":
            origin = "http://127.0.0.1:%d" % self.port
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers["X-Chitragupta-Token"] = token
        r, out = self.get(path, method=method, headers=headers, body=raw if raw is not None else json.dumps(payload).encode())
        return r.status, (json.loads(out) if out else {})

    def test_new_actions_keep_every_check(self):
        tok = self.token()
        good = {"date": self.days["day"], "text": "Reviewed the refund report with the team."}
        for name, kw, want in (("no token", {}, 403), ("foreign Origin", dict(token=tok, origin="http://evil.test"), 403),
                               ("null Origin", dict(token=tok, origin="null"), 403),
                               ("form post", dict(token=tok, ctype="application/x-www-form-urlencoded"), 415),
                               ("oversized", dict(token=tok, raw=json.dumps({"date": self.days["day"], "text": "x" * 3000}).encode()), 413)):
            self.assertEqual(self.write("/api/actions/reflection-save", good, **kw)[0], want, name)
        self.assertEqual(self.write("/api/actions/reflection-save", good, token=tok)[0], 200)
        self.assertEqual(self.write("/api/actions/profile-discover", {"hosts": False}, token="x" * 43)[0], 403)

    def test_settings_patch_confirms_a_suggestion_and_refuses_a_bot(self):
        from chitragupta import profile
        tok = self.token()
        st, out = self.write("/api/settings", {"changes": {"git_author_emails": [EMAIL, "dev.example@acme.test"]}},
                             method="PATCH", token=tok)
        self.assertEqual(st, 200, out)
        conf = {c["value"]: c["confirmed"] for c in profile.read_candidates()["candidates"] if c["kind"] == "email"}
        self.assertTrue(conf["dev.example@acme.test"])
        st, out = self.write("/api/settings", {"changes": {"git_author_emails": [EMAIL, "ci-runner[bot]@acme.test"]}},
                             method="PATCH", token=tok)
        self.assertEqual(st, 422)
        self.assertIn("git_author_emails", out["fields"])


class Plumbing(ProgressBase):
    def test_watch_paths_and_export_stubs(self):
        from chitragupta import export
        from chitragupta.dashboard import api
        paths = {os.path.normcase(p) for p in api._watch_paths(common.load_config())}
        for p in (common.hp("data", "activity", "service-api.json"), common.wl("config.json"),
                  common.hp("data", "reflections", self.days["yday"][:7] + ".json"),
                  common.wl("cache", "profile-candidates.json")):
            self.assertIn(os.path.normcase(p), paths)
        before = api.changes(max_age=0)["cursor"]
        from chitragupta import progress
        progress.save_reflection(self.days["day"], "A note.")
        self.assertNotEqual(api.changes(max_age=0)["cursor"], before)
        self.assertTrue({"/app/pages/profile.js", "/app/pages/progress.js"} <= export._STUBBED)


if __name__ == "__main__":
    unittest.main()
