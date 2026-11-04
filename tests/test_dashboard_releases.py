"""Release tracking in the dashboard: the /api/releases month overview and a ticket's release plan, on the synthetic
home of test_dashboard plus synthetic release records (repos service-api, service-ui, shop-api; releases FY99 …).

add_release_fixtures() is also what scripts/ui/run.py adds for the browser checks. Its planned dates are relative to
the fixture's "today", so the current month always has a release group, an overdue ticket and an unresolved one.
"""
import datetime as _dt
import json
import os
import unittest

from tests.test_dashboard import TICKET, _w
from tests.test_dashboard_v2 import V2Base, add_v2_fixtures  # noqa: F401 - add_v2_fixtures documents the order

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December"]
UAT_FIELD = "customfield_20001"


def _ordinal(n):
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def _release(vid, date, **kw):
    """One fix version as release.resolve() stores it (enriched, keyed, with its planned date and the name's date)."""
    name = "FY99 %s %s Release" % (MONTHS[date.month - 1], _ordinal(date.day))
    out = {"id": vid, "name": name, "project": "ABC", "release_date": date.isoformat(), "released": False,
           "archived": False, "description": "", "key": "ABC:" + vid,
           "name_date": {"month": date.month, "day": date.day, "text": "%s %s" % (MONTHS[date.month - 1], _ordinal(date.day))},
           "planned_date": date.isoformat(), "date_source": "Jira version release date"}
    out.update(kw)
    return out


def _branch_name(date, key):
    return "release/ABC-%d/%s-%s-release" % (key, _ordinal(date.day), MONTHS[date.month - 1][:3].lower())


def add_release_fixtures(home, days):
    """Release records as release.resolve() + evaluate() leave them, written onto the synthetic tickets:
    ABC-1001 ships this month from two repos (service-api complete through QA and in UAT, service-ui with its release
    PR open and a PR into the wrong release branch; UAT required, deployment and acceptance unverified; a shared QA
    integration PR; one candidate repo), ABC-1003 was planned last month and isn't done (stale refs, a failed Jira
    read), ABC-1002 has two fix versions (date unresolved), and the Jira scope search has ABC-1005, not started.
    Turns release tracking on in the home's config."""
    day, yday, old = days["day"], days["yday"], days["old"]
    today = _dt.date.fromisoformat(day)
    first = today.replace(day=1)
    end = (first + _dt.timedelta(days=32)).replace(day=1) - _dt.timedelta(days=1)   # planned: this month's last day
    before = first - _dt.timedelta(days=5)                                         # planned last month: overdue
    after = (end + _dt.timedelta(days=1)).replace(day=14)                          # a second fix version, next month

    def ts(s):
        return _dt.datetime.fromisoformat(s).astimezone().isoformat(timespec="seconds")

    computed, fetched, jira_at = ts(yday + "T18:00:00"), ts(yday + "T17:30:00"), ts(yday + "T17:00:00")
    RB, OLD, HOTFIX = _branch_name(end, 9000), _branch_name(before, 8000), "release/ABC-9001/hotfix"
    cur, prev, nxt = _release("20001", end), _release("20000", before), _release("20002", after)
    fresh = {"fetched_at": fetched, "stale": False, "errors": [], "pr_source": "test", "pr_refreshed_at": fetched,
             "open_prs_visible": True, "why_stale": ""}

    def branch(name, source, state="confirmed"):
        return {"name": name, "state": state, "source": source, "confidence": "high", "exists": "remote",
                "verified_at": fetched, "override": False, "candidates": [], "missing": ""}

    def pr(repo, n, source, target, state, role, key=TICKET, **kw):
        out = {"number": n, "title": "%s %s" % (key, source), "source": source, "target": target, "state": state,
               "created_at": ts(old + "T10:00:00"), "merged_at": ts(yday + "T15:00:00") if state == "merged" else None,
               "url": "https://git.acme.test/%s/pull/%d" % (repo, n), "src": "test", "role": role}
        out.update(kw)
        return out

    def stage(state, target, detail, n_in=0, n=1, opened=()):
        return {"target": target, "counts": {"in": n_in, "of": n, "check": 0, "reverted": 0, "open": len(opened)},
                "pending_prs": list(opened), "why": [] if state == "present" else ["no PR carries it"], "state": state,
                "detail": detail}

    wrong = {"kind": "wrong-target", "pr": 40, "text": "PR #40 targets %s; the release ships from %s: retarget it in "
                                                       "the Git host." % (HOTFIX, RB)}
    uat_req = {"state": "required", "source": "Jira %s = Yes" % UAT_FIELD}
    ui_next = "Get release PR #41 merged into %s." % RB
    api_repo = {
        "affected": {"how": "has branches"}, "candidate": False,
        "branch": branch(RB, 'your mapping releases.repos.service-api.branches["20001"]'),
        "strategy": "release-then-qa-sync",
        "uat_conf": {"branch": "uat", "promotion": {"kind": "pr", "from": "uat", "brings": "cherry-pick"},
                     "source": "releases.repos.service-api.uat"},
        "freshness": fresh,
        "dev": [{"branch": "feature/ABC-1001-order-total", "parent": RB, "parent_source": "reflog", "status": "merged",
                 "base": "ok", "note": "created from %s (reflog)" % RB}],
        "prs": {"release": [pr("service-api", 31, "feature/ABC-1001-order-total", RB, "merged", "release")],
                "uat": [pr("service-api", 33, "feature/ABC-1001-order-total-uat", "uat", "merged", "uat")]},
        "shared": [pr("service-api", 32, "qa-sync/%s-release" % MONTHS[end.month - 1][:3].lower(), "qa", "merged", "qa",
                      title="Sync %s into qa" % RB, shared=True, carries=2, of=2)],
        "complete_at": {"release": computed, "qa": computed, "uat": computed},
        "stages": {"release": stage("present", RB, "all 2 commits in %s" % RB, 2, 2),
                   "qa": stage("present", "qa", "all 2 commits in qa", 2, 2),
                   "uat": stage("present", "uat", "all 2 commits in uat", 2, 2)},
        "flags": [], "next": "In UAT branch uat: verify the UAT deployment and acceptance."}
    ui_repo = {
        "affected": {"how": "has branches or PRs"}, "candidate": False,
        "branch": branch(RB, 'your mapping releases.repos.service-ui.branches["%s"]' % cur["name"]),
        "strategy": "release-then-qa-sync",
        "uat_conf": {"branch": "uat", "promotion": {"kind": "unspecified"}, "source": "releases.repos.service-ui.uat"},
        "freshness": fresh,
        "dev": [{"branch": "feature/ABC-1001-total-label", "parent": RB, "parent_source": "reflog", "status": "pr_open",
                 "base": "ok", "note": "created from %s (reflog)" % RB}],
        "prs": {"release": [pr("service-ui", 40, "feature/ABC-1001-total-label", HOTFIX, "open", "release", wrong_target=True),
                            pr("service-ui", 41, "feature/ABC-1001-total-label", RB, "open", "release")]},
        "shared": [], "complete_at": {},
        "stages": {"release": stage("missing", RB, "none of 1 commit in %s; PR #41 open" % RB, opened=[41]),
                   "qa": stage("missing", "qa", "none of 1 commit in qa"),
                   "uat": stage("missing", "uat", "none of 1 commit in uat")},
        "flags": [wrong], "next": ui_next}
    batch_repo = {
        "affected": None, "candidate": True,
        "branch": {"name": None, "state": "suggested", "source": "", "confidence": "low", "exists": None,
                   "verified_at": fetched, "override": False, "missing": "confirm which branch ships %r in service-batch" % cur["name"],
                   "candidates": [{"name": RB, "why": "branch name date: same date as the planned %s; month of %r" % (
                       end.isoformat(), cur["name_date"]["text"]), "score": 4},
                       {"name": HOTFIX, "why": "PR target of ABC-1003 (same release)", "score": 3}]},
        "strategy": None, "uat_conf": None, "freshness": fresh, "dev": [], "prs": {}, "shared": [], "complete_at": {},
        "stages": {r: {"state": "not_started", "detail": "no branches for this ticket here"} for r in ("release", "qa")},
        "flags": [], "next": "If %s changes service-batch: confirm which branch ships %r in service-batch" % (TICKET, cur["name"])}
    batch_repo["stages"]["uat"] = {"state": "not_started", "detail": "no branches for this ticket here"}
    rel_1001 = {
        "v": 1, "jira": {"source": "card", "fetched_at": jira_at, "error": None, "status": "In Progress"},
        "releases": [cur], "release_keys": [cur["key"]], "selected": cur["key"], "selection": {"how": "the ticket's fix version"},
        "history": [{"at": ts(old + "T10:00:00"), "releases": [{k: prev[k] for k in ("key", "name", "planned_date")}],
                     "branches": {"service-api": OLD}, "to": [cur["name"]]}],
        "uat": {"requirement": uat_req,
                "deployed": {"state": "unknown", "source": "no deployed evidence source configured "
                                                           "(releases.jira.uat_deployed_statuses)"},
                "accepted": {"state": "unknown", "source": "Jira status 'In Progress' doesn't say accepted"}},
        "repos": {"service-api": api_repo, "service-ui": ui_repo, "service-batch": batch_repo},
        "pins": {"service-api": {"release": RB, "uat": "uat"}, "service-ui": {"release": RB, "uat": "uat"}},
        "assignee_me": True, "summary": "Order total is wrong for discounted items",
        "reminders": [{"id": "wrong-target:service-ui:40", "kind": "wrong-target", "repo": "service-ui", "severity": "alert",
                       "text": "service-ui: " + wrong["text"]},
                      {"id": "uat-missing:service-ui", "kind": "uat-missing", "repo": "service-ui", "severity": "alert",
                       "text": "service-ui: UAT required — none of 1 commit in uat. " + ui_next}],
        "flags": [dict(wrong, repo="service-ui")],
        "readiness": {"ready": False, "missing": ["service-ui: release missing", "service-ui: qa missing",
                                                  "service-ui: uat missing", "service-ui: 1 flag", "UAT deployed unverified",
                                                  "UAT accepted unverified"]},
        "computed_at": computed}

    stale_at = ts(before.isoformat() + "T09:00:00")
    stale_why = "last fetched %s" % stale_at[:16].replace("T", " ")
    rel_1003 = {
        "v": 1, "jira": {"source": "card", "fetched_at": ts(before.isoformat() + "T08:00:00"),
                         "error": "jira_get_issue timed out after 60 s (synthetic)", "status": "In Progress"},
        "releases": [prev], "release_keys": [prev["key"]], "selected": prev["key"], "selection": {"how": "the ticket's fix version"},
        "history": [],
        "uat": {"requirement": {"state": "not_required", "source": "Jira %s = No" % UAT_FIELD},
                "deployed": {"state": "unknown", "source": "no deployed evidence source configured "
                                                           "(releases.jira.uat_deployed_statuses)"},
                "accepted": {"state": "unknown", "source": "no accepted evidence source configured "
                                                           "(releases.jira.uat_accepted_statuses)"}},
        "repos": {"service-api": {
            "affected": {"how": "has branches"}, "candidate": False,
            "branch": branch(OLD, 'your mapping releases.repos.service-api.branches["20000"]'),
            "strategy": "release-then-qa-sync", "uat_conf": None,
            "freshness": {"fetched_at": stale_at, "stale": True, "errors": [], "pr_source": "merge-commit",
                          "pr_refreshed_at": stale_at, "open_prs_visible": False, "why_stale": stale_why},
            "dev": [{"branch": "feature/ABC-1003-refund-report", "parent": OLD, "parent_source": "inferred",
                     "status": "merged", "base": "unproven",
                     "note": "shares history with %s (inferred: the original start can't be proven from ancestry)" % OLD}],
            "prs": {"release": [pr("service-api", 21, "feature/ABC-1003-refund-report", OLD, "merged", "release", key="ABC-1003")]},
            "shared": [], "complete_at": {"release": computed},
            "stages": {"release": stage("present", OLD, "all 1 commit in %s" % OLD, 1, 1),
                       "qa": stage("unknown", "qa", "none of 1 commit in qa as of the local refs (%s), so not confirmed "
                                                    "missing" % stale_why),
                       "uat": {"state": "not_applicable", "detail": "UAT not required (Jira %s = No)" % UAT_FIELD}},
            "flags": [],
            "next": "QA next: create a QA integration branch from qa (`git switch -c feature/ABC-1003-refund-report-qa "
                    "origin/qa`), merge %s into it (`git merge origin/%s`), resolve conflicts there, push, then PR "
                    "feature/ABC-1003-refund-report-qa → qa (open PRs aren't visible from merge commits: skip this if it's "
                    "already open)." % (OLD, OLD)}},
        "pins": {"service-api": {"release": OLD, "uat": None}}, "assignee_me": True, "summary": "Refund report",
        "reminders": [], "flags": [],
        "readiness": {"ready": False, "missing": ["service-api: qa unknown", "service-api: refs " + stale_why]},
        "computed_at": computed}

    amb = "the ticket has 2 fix versions (%s, %s): choose the one that counts" % (cur["name"], nxt["name"])
    rel_1002 = {
        "v": 1, "jira": {"source": "card", "fetched_at": jira_at, "error": None, "status": "In Progress"},
        "releases": [cur, nxt], "release_keys": [cur["key"], nxt["key"]], "selected": None,
        "selection": {"how": "ambiguous", "candidates": [cur["name"], nxt["name"]], "missing": amb},
        "history": [],
        "uat": {"requirement": {"state": "unknown", "source": "Jira %s is empty" % UAT_FIELD},
                "deployed": {"state": "unknown", "source": "no deployed evidence source configured "
                                                           "(releases.jira.uat_deployed_statuses)"},
                "accepted": {"state": "unknown", "source": "no accepted evidence source configured "
                                                           "(releases.jira.uat_accepted_statuses)"}},
        "repos": {"shop-api": {
            "affected": {"how": "has branches or PRs"}, "candidate": False,
            "branch": {"name": None, "state": "unresolved", "source": "", "confidence": "none", "exists": None,
                       "verified_at": fetched, "override": False, "candidates": [], "missing": "no release selected for the ticket"},
            "strategy": "release-then-qa-sync", "uat_conf": None, "freshness": fresh,
            "dev": [{"branch": "feature/ABC-1002-timeout", "parent": None, "parent_source": None, "status": "active",
                     "base": "unknown", "note": "start not recorded"}],
            "prs": {}, "shared": [], "complete_at": {},
            "stages": {"release": {"state": "unknown", "target": None, "detail": "no release target in this repo's strategy"},
                       "qa": {"state": "not_started", "target": "qa", "detail": "no commits yet"},
                       "uat": {"state": "unknown", "detail": "UAT requirement unknown (Jira %s is empty)" % UAT_FIELD}},
            "flags": [],
            "next": "Resolve the release branch first: no release selected for the ticket. No development base is "
                    "recommended until then."}},
        "pins": {"shop-api": {"release": None, "uat": None}}, "assignee_me": True, "summary": "Checkout timeout",
        "reminders": [{"id": "release-ambiguous", "kind": "release-ambiguous", "repo": "", "severity": "warn",
                       "text": "ABC-1002: %s (`/cg:branch release-pick ABC-1002 \"<release>\"`)." % amb},
                      {"id": "map:shop-api", "kind": "mapping", "repo": "shop-api", "severity": "warn",
                       "text": "shop-api: release branch for the release not confirmed — no release selected for the ticket."}],
        "flags": [],
        "readiness": {"ready": False, "missing": [amb, "UAT requirement unknown", "shop-api: release branch not confirmed",
                                                  "shop-api: release unknown", "shop-api: qa not started"]},
        "computed_at": computed}

    for key, rel in ((TICKET, rel_1001), ("ABC-1003", rel_1003), ("ABC-1002", rel_1002)):
        p = os.path.join(home, "data", "tickets", key + ".json")
        with open(p, encoding="utf-8") as f:
            tk = json.load(f)
        tk["release"] = rel
        _w(p, tk)
    _w(os.path.join(home, "data", "jira", "_releases", "scope.json"), {
        "fetched_at": jira_at, "jql": "assignee = currentUser() AND fixVersion is not EMPTY AND updated >= -120d",
        "mine_only": True, "error": None, "issues": {"ABC-1005": {
            "versions": [{"id": cur["id"], "name": cur["name"], "project": "ABC", "release_date": end.isoformat()}],
            "planned_date": None, "uat_values": [], "uat_seen": False, "labels": [], "status": "To Do",
            "summary": "Export refunds as CSV", "assignee_me": True}}})
    p = os.path.join(home, ".worklog", "config.json")
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["releases"] = {"enabled": True, "jira": {"uat_field": UAT_FIELD}}
    _w(p, cfg)
    return {"month": day[:7], "release": cur["name"], "planned": end.isoformat(), "release_branch": RB}


class ReleasesApi(V2Base):
    def setUp(self):
        super().setUp()
        self.rel = add_release_fixtures(self.home, self.days)

    def test_month_overview_groups_overdue_unresolved_and_scope(self):
        from chitragupta.dashboard import api
        ov = api.releases(None)
        self.assertEqual(ov["month"], self.rel["month"])
        self.assertTrue(ov["enabled"])
        self.assertEqual([g["release"] for g in ov["groups"]], [self.rel["release"]])
        rows = {r["key"]: r for r in ov["groups"][0]["tickets"]}
        self.assertEqual(sorted(rows), ["ABC-1001", "ABC-1005"])
        self.assertFalse(rows["ABC-1001"]["ready"])
        self.assertEqual([x["repo"] for x in rows["ABC-1001"]["repos"]], ["service-api", "service-ui"])  # no candidates
        self.assertEqual(rows["ABC-1005"]["missing"], ["not started: no branches or worklog yet"])
        self.assertEqual([r["key"] for r in ov["overdue"]], ["ABC-1003"])
        self.assertEqual([r["key"] for r in ov["unresolved"]], ["ABC-1002"])

    def test_ticket_summary_carries_reminders_and_release_line(self):
        from chitragupta.dashboard import api
        t = {x["key"]: x for x in api.tickets()["tickets"]}
        self.assertEqual([r["kind"] for r in t[TICKET]["reminders"]], ["wrong-target", "uat-missing"])
        self.assertFalse(t[TICKET]["release"]["ready"])
        self.assertIn(self.rel["release"], t[TICKET]["release"]["label"])


class ReleasesHttp(ReleasesApi):
    def setUp(self):
        super().setUp()
        from chitragupta.dashboard import server
        self.srv, self.thread = server.start_in_thread(0)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def get(self, path):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        try:
            c.request("GET", path, headers={"Host": "127.0.0.1:%d" % self.srv.port, "Accept": "application/json"})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        finally:
            c.close()

    def test_releases_route(self):
        st, ov = self.get("/api/releases")
        self.assertEqual(st, 200)
        self.assertEqual(ov["month"], self.rel["month"])
        self.assertEqual(len(ov["groups"]), 1)
        st, ov = self.get("/api/releases?month=" + self.rel["month"])
        self.assertEqual((st, [g["release"] for g in ov["groups"]]), (200, [self.rel["release"]]))
        st, later = self.get("/api/releases?month=2099-01")  # overdue work stays in view on later months
        self.assertEqual((st, later["groups"], [r["key"] for r in later["overdue"]]), (200, [], ["ABC-1003"]))
        st, earlier = self.get("/api/releases?month=2000-01")
        self.assertEqual((st, earlier["groups"], earlier["overdue"]), (200, [], []))
        for bad in ("2026-13", "2026-1", "next"):
            st, err = self.get("/api/releases?month=" + bad)
            self.assertEqual(st, 400, bad)
            self.assertIn("YYYY-MM", err["error"])

    def test_ticket_includes_release_plan(self):
        st, t = self.get("/api/tickets/" + TICKET)
        self.assertEqual(st, 200)
        plan = t["release_plan"]
        self.assertEqual(plan["selected"], "ABC:20001")
        self.assertEqual(sorted(plan["repos"]), ["service-api", "service-batch", "service-ui"])
        self.assertNotIn("pins", plan)
        self.assertIn(self.rel["release"], plan["label"])
        self.assertTrue(any(x.startswith("Release plan for " + TICKET) for x in t["release_brief"]))
        st, t = self.get("/api/tickets/ABC-1003")
        self.assertEqual((st, t["release_plan"]["readiness"]["ready"]), (200, False))


class NoReleaseRecord(V2Base):
    def test_ticket_without_a_record_has_no_plan(self):
        from chitragupta.dashboard import api
        t = api.ticket(TICKET)
        self.assertIsNone(t["release_plan"])
        self.assertEqual(t["release_brief"], [])
        ov = api.releases(None)
        self.assertFalse(ov["enabled"])
        self.assertEqual((ov["overdue"], ov["unresolved"]), ([], []))


if __name__ == "__main__":
    unittest.main()
