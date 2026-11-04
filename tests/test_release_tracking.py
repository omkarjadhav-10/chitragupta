"""Release tracking: Jira release -> release branch per repo, stages to release / QA / UAT, reminders, the planning
brief and the monthly overview. Throwaway repos built with real git and fixed dates (two repos, service-api and
service-ui), PR data from a fake host, Jira data as files the jobs would have written. Nothing leaves the machine.
"""
import datetime as dt
import json
import os
import tempfile
import time
import unittest

from tests.support import Isolated, rmtree
from tests.test_branch_tracking import ME, OTHER, TZ, Sandbox, pr

NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=TZ)
DEC = "release/ABC-9000/7th-dec-release"   # ships the "FY99 December 14th Release" (dates differ on purpose)
NOV = "release/ABC-8000/9th-nov-release"
DEC2 = "release/ABC-9001/dec-hotfix"
V_DEC = {"id": "10001", "name": "FY99 December 14th Release", "releaseDate": "2026-12-14", "released": False}
V_NOV = {"id": "10002", "name": "FY99 November 9th Release", "releaseDate": "2026-11-09", "released": False}
UAT_FIELD = "customfield_20001"


class Box(Sandbox):
    """One repo (bare origin + clone) with main, qa, uat, staging and the given release branches."""

    def __init__(self, root, name, releases):  # noqa: super().__init__ builds a fixed shop-api layout
        self.root = root
        self.origin = os.path.join(root, name + ".git")
        self.work = os.path.join(root, name)
        self.t = dt.datetime(2026, 10, 1, 9, 0, tzinfo=TZ)
        self.n = 0
        self.run(["init", "-q", "--bare", "-b", "main", self.origin], cwd=root)
        self.run(["clone", "-q", self.origin, self.work], cwd=root)
        self.commit("initial", who=OTHER)
        self.git("push", "-q", "-u", "origin", "main")
        for b in ["qa", "uat", "staging"] + list(releases):
            self.git("checkout", "-q", "-b", b, "main")
            self.commit("base for " + b, who=OTHER)
            self.git("push", "-q", "-u", "origin", b)
        self.git("checkout", "-q", "main")
        self.fetch()  # FETCH_HEAD: the refs count as freshly fetched

    def dev(self, name, start, msgs, who=None, push=True):
        self.branch(name, "origin/" + start)
        shas = [self.commit(m, who=who) for m in msgs]
        if push:
            self.push(name)
        return shas

    def integrate(self, name, release, into="qa"):
        """The QA integration step: a new branch from qa that merges the whole release branch, merged into qa."""
        self.branch(name, "origin/" + into)
        self.git("merge", "-q", "--no-ff", "-m", "merge %s into %s" % (release, name), "origin/" + release)
        self.push(name)
        return self.merge(name, into)


class ReleaseCase(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        from chitragupta import hosts
        self._real_run = hosts._run
        hosts._run = lambda *a, **k: (127, "", "tests: no host CLI")
        self.tmp = tempfile.mkdtemp(prefix="cg-rel-")
        self.api = Box(self.tmp, "service-api", [DEC, NOV, DEC2])
        self._ui = None
        os.makedirs(os.path.join(self.home, "data", "tickets"), exist_ok=True)
        for t in ("ABC-1001", "ABC-1002", "ABC-1003"):
            self.ticket(t, {"id": t, "last": "2026-10-10", "first": "2026-10-01" if t != "ABC-1002" else ""})
        self.prs = {"service-api": [], "service-ui": []}
        self.over = {}

    @property
    def ui(self):
        if self._ui is None:
            self._ui = Box(self.tmp, "service-ui", [DEC, NOV])
        return self._ui

    def tearDown(self):
        from chitragupta import hosts
        hosts._run = self._real_run
        rmtree(self.tmp)

    # ---- fixtures
    def ticket(self, key, rec):
        with open(os.path.join(self.home, "data", "tickets", key + ".json"), "w", encoding="utf-8") as f:
            json.dump(rec, f)

    def jira(self, key, versions=None, uat=None, status="In Progress", assignee=True, simplified=False, error=None,
             fields="summary,status,fixVersions,assignee,labels," + UAT_FIELD, labels=None):
        """data/jira/<KEY>.json as the card job writes it (raw = the jira_get_issue result)."""
        if error:
            rec = {"key": key, "fetched_at": "2026-10-10T09:00:00+05:30", "card": {}, "raw": None, "error": error}
        else:
            a = {"emailAddress": "test@acme.test" if assignee else "other@acme.test"}
            if simplified:
                iss = {"key": key, "summary": "Synthetic " + key, "status": {"name": status},
                       "fix_versions": [v["name"] for v in versions or []], "assignee": {"email": a["emailAddress"]},
                       "labels": labels or []}
                if uat is not None:
                    iss[UAT_FIELD] = {"value": {"value": uat}}
            else:
                f = {"summary": "Synthetic " + key, "status": {"name": status}, "fixVersions": versions or [],
                     "assignee": a, "labels": labels or []}
                if uat is not None:
                    f[UAT_FIELD] = {"value": uat, "id": "1"}
                iss = {"key": key, "fields": f}
            rec = {"key": key, "fetched_at": "2026-10-10T09:00:00+05:30", "card": {"found": True, "status": status},
                   "raw": json.dumps(iss), "error": None, "fields": fields}
        os.makedirs(os.path.join(self.home, "data", "jira"), exist_ok=True)
        with open(os.path.join(self.home, "data", "jira", key + ".json"), "w", encoding="utf-8") as f:
            json.dump(rec, f)

    def cfg(self, **rel):
        r = {"enabled": True, "jira": {"uat_field": UAT_FIELD},
             "repos": {"service-api": {"branches": {"10001": DEC, "10002": NOV},
                                       "uat": {"branch": "uat", "promotion": {"kind": "pr", "from": "uat",
                                                                               "brings": "cherry-pick"}}},
                       "service-ui": {"branches": {"FY99 December 14th Release": DEC, "10002": NOV},
                                      "uat": {"branch": "uat", "promotion": "unspecified"}}}}
        for k, v in rel.items():
            r[k] = v
        r.update(self.over)
        repos = [self.api.work] + ([self._ui.work] if self._ui is not None else [])
        return {"watched_repos": [w.replace("\\", "/") for w in repos],
                "git_author_emails": ["test@acme.test"], "ticket_prefixes": ["ABC"],
                "branching": {"repos": {"service-api": {"strategy": "release-then-qa-sync"},
                                        "service-ui": {"strategy": "release-then-qa-sync"}}},
                "releases": r}

    def track(self, keys=("ABC-1001",), cfg=None, now=NOW, store=None):
        from chitragupta import delivery, gittrack, release
        from chitragupta.store import Store
        cfg = cfg or self.cfg()
        self.store = store or Store()
        prs = self.prs
        tr = gittrack.Tracker(cfg, {}, self.store, now=now, pr_fetch=lambda r, b: [p for p in prs.get(r.name, []) if p["source"] == b])
        self.rx = tr.refresh(fetch=False)
        release.refresh(self.store, self.rx, cfg, list(keys), now=now, phase="resolve")
        for t in keys:
            delivery.update_ticket(self.store, t, self.rx, cfg, now=now, force=True)
        release.refresh(self.store, self.rx, cfg, list(keys), now=now, phase="evaluate")
        self.store.save()
        return {k: self.store.ticket(k).get("release") for k in keys}

    def rel(self, key="ABC-1001"):
        return self.store.ticket(key)["release"]

    def repo(self, name, key="ABC-1001"):
        return self.rel(key)["repos"][name]

    def brief(self, key="ABC-1001"):
        from chitragupta import release
        return "\n".join(release.brief_lines(self.store.ticket(key), key, now=NOW))


class JiraFacts(ReleaseCase):
    def test_rest_and_simplified_shapes_catalog_and_rename(self):
        from chitragupta import release
        cfg = self.cfg()
        self.jira("ABC-1001", [V_DEC], uat="Yes")
        f = release.jira_facts("ABC-1001", cfg)
        self.assertEqual([(v["id"], v["name"], v["release_date"]) for v in f["versions"]],
                         [("10001", "FY99 December 14th Release", "2026-12-14")])
        self.assertEqual(f["uat_values"], ["Yes"])
        self.assertTrue(f["assignee_me"])
        # the simplified MCP shape gives names only: the versions catalog supplies the id and the date
        self.jira("ABC-1002", [{"name": "FY99 December 14th (old name)"}], simplified=True)
        f2 = release.jira_facts("ABC-1002", cfg)
        self.assertEqual([(v["id"], v["release_date"]) for v in f2["versions"]], [("", None)])
        cat = release.merge_catalog({"versions": [{"id": "10001", "name": "FY99 December 14th (old name)"}]},
                                    release.parse_versions([V_DEC], "ABC"), "2026-10-10T09:00:00+05:30")
        os.makedirs(os.path.dirname(release.versions_path("ABC")), exist_ok=True)
        with open(release.versions_path("ABC"), "w", encoding="utf-8") as fh:
            json.dump(cat, fh)
        v = release.enrich(f2["versions"])[0]
        self.assertEqual((v["id"], v["name"], v["release_date"]), ("10001", "FY99 December 14th Release", "2026-12-14"))
        self.assertIn("FY99 December 14th (old name)", v["previous_names"])
        self.assertTrue(release._matches(v, "FY99 December 14th (old name)"))  # a mapping by the old name still holds

    def test_name_date_is_a_separate_fact_and_fy_is_not_a_year(self):
        from chitragupta import release
        nd = release.name_date("FY99 December 14th Release")
        self.assertEqual((nd["month"], nd["day"]), (12, 14))
        self.assertNotIn("year", nd)
        self.assertEqual(release.name_date("release/ABC-9000/7th-dec-release")["day"], 7)
        self.assertEqual(release.name_date("release/RC_2026_06_07")["year"], 2026)
        self.assertIsNone(release.name_date("release/ABC-9001/dec-hotfix"))

    def test_field_asked_but_absent_is_empty_never_asked_is_unknown(self):
        from chitragupta import release
        cfg = self.cfg()
        iss = {"key": "ABC-1001", "fields": {"status": {"name": "Open"}}}
        self.assertEqual(release.issue_facts(iss, cfg, "ABC-1001", "status,fixVersions")["versions"], [])
        self.assertIsNone(release.issue_facts(iss, cfg, "ABC-1001", "status")["versions"])
        from chitragupta import mcpjobs
        self.assertIn("fixVersions", mcpjobs.card_fields(cfg))
        self.assertIn(UAT_FIELD, mcpjobs.card_fields(cfg))
        self.assertNotIn("fixVersions", mcpjobs.card_fields({}))  # off: the card job asks for what it always did
        self.assertIn("jira_get_project_versions", mcpjobs.JIRA_ALLOW_DEFAULT["read"])

    def test_uat_requirement_is_tri_state(self):
        from chitragupta import release
        cfg = self.cfg()
        base = {"source": "card", "labels": []}
        self.assertEqual(release.uat_requirement(dict(base, uat_values=["Yes"]), {}, cfg)["state"], "required")
        self.assertEqual(release.uat_requirement(dict(base, uat_values=["No"]), {}, cfg)["state"], "not_required")
        self.assertEqual(release.uat_requirement(dict(base, uat_values=[]), {}, cfg)["state"], "unknown")  # absent
        self.assertEqual(release.uat_requirement(dict(base, uat_values=["Maybe"]), {}, cfg)["state"], "unknown")
        self.assertEqual(release.uat_requirement(dict(base, uat_values=[]), {"uat": "not_required"}, cfg)["state"],
                         "not_required")
        self.assertEqual(release.uat_requirement({"source": None}, {}, cfg)["state"], "unknown")
        nofield = self.cfg(jira={})
        self.assertEqual(release.uat_requirement(dict(base, uat_values=["Yes"]), {}, nofield)["state"], "unknown")

    def test_directives_round_trip(self):
        from chitragupta import release
        from chitragupta.store import Store
        line = release.directive_line("release-map", ["service-api", "FY99 December 14th Release", DEC])
        d = release.parse_directive(line)
        self.assertEqual((d["repo"], d["release"], d["branch"]), ("service-api", "FY99 December 14th Release", DEC))
        cfg, store = self.cfg(), Store()
        ok, _, changed = release.apply_directive(d, store, cfg)
        self.assertTrue(ok and changed)
        self.assertEqual(cfg["releases"]["repos"]["service-api"]["branches"]["FY99 December 14th Release"], DEC)
        for text, want in (("release-pick: ABC-1001 10001", ("pick", "10001")), ("uat: ABC-1001 not_required", ("uat", "not_required")),
                           ("release-repos: ABC-1001 service-ui", ("repos", ["service-ui"])),
                           ("release-branch: ABC-1001 service-api %s" % DEC2, ("branches", {"service-api": DEC2}))):
            ok, _, changed = release.apply_directive(release.parse_directive(text), store, cfg)
            self.assertTrue(ok)
            self.assertFalse(changed)
            self.assertEqual(store.ticket("ABC-1001")["release_override"][want[0]], want[1])
        self.assertEqual(release.parse_directive("uat: nonsense")["kind"], "invalid")
        self.assertIsNone(release.parse_directive("branch-link: ABC-1001 service-api x"))


class Scenarios(ReleaseCase):
    def test_01_date_mismatch_resolved_by_explicit_mapping(self):
        self.jira("ABC-1001", [V_DEC], uat="No")
        self.api.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])
        self.track()
        r = self.rel()
        v = r["releases"][0]
        self.assertEqual((v["name"], v["planned_date"], v["date_source"]),
                         ("FY99 December 14th Release", "2026-12-14", "Jira version release date"))
        self.assertEqual((v["name_date"]["month"], v["name_date"]["day"]), (12, 14))  # the name's date: shown only
        b = self.repo("service-api")["branch"]
        self.assertEqual((b["name"], b["state"], b["confidence"]), (DEC, "confirmed", "high"))
        self.assertIn("your mapping", b["source"])
        self.assertTrue(b["verified_at"])
        t = self.store.ticket("ABC-1001")["git"]["repos"]["service-api"]["targets"]
        self.assertEqual((t[0]["name"], t[0]["pinned"]), (DEC, True))  # the delivery check uses the mapped branch
        self.assertEqual(self.repo("service-api")["dev"][0]["base"], "ok")
        txt = self.brief()
        self.assertIn("Development base: %s" % DEC, txt)
        self.assertIn("Development PR target: %s" % DEC, txt)
        self.assertIn("planned 2026-12-14", txt)

    def test_02_two_repositories_are_tracked_independently(self):
        self.jira("ABC-1001", [V_DEC], uat="No")
        sb = self.api
        sb.dev("feature/ABC-1001-api", DEC, ["ABC-1001 api"])
        m1, h1 = sb.merge("feature/ABC-1001-api", DEC)
        m2, h2 = sb.integrate("qa-sync/dec", DEC)
        self.prs["service-api"] = [pr(11, "feature/ABC-1001-api", DEC, merge_commit=m1, head=h1),
                                   pr(12, "qa-sync/dec", "qa", merge_commit=m2, head=h2)]
        ui = self.ui
        head = ui.dev("feature/ABC-1001-ui", DEC, ["ABC-1001 ui"])[-1]
        self.prs["service-ui"] = [pr(21, "feature/ABC-1001-ui", DEC, state="open", head=head)]
        self.track()
        api, uix = self.repo("service-api"), self.repo("service-ui")
        self.assertEqual((api["stages"]["release"]["state"], api["stages"]["qa"]["state"]), ("present", "present"))
        self.assertEqual(uix["stages"]["release"]["state"], "missing")
        self.assertEqual(uix["stages"]["release"]["pending_prs"], [21])
        self.assertEqual(uix["next"], "Get release PR #21 merged into %s." % DEC)
        self.assertEqual(api["next"], "Nothing left for service-api.")
        rd = self.rel()["readiness"]
        self.assertFalse(rd["ready"])
        self.assertTrue(any(m.startswith("service-ui: release") for m in rd["missing"]))
        self.assertFalse(any(m.startswith("service-api:") for m in rd["missing"]))

    def test_03_ambiguity_is_visible_and_no_base_is_recommended(self):
        self.jira("ABC-1001", [V_DEC, V_NOV], uat="No")
        self.api.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])
        self.track()
        r = self.rel()
        self.assertIsNone(r["selected"])
        self.assertEqual(r["selection"]["how"], "ambiguous")
        self.assertIn("2 fix versions", r["selection"]["missing"])
        e = self.repo("service-api")
        self.assertEqual(e["branch"]["state"], "unresolved")
        self.assertNotIn("git switch", e["next"])
        self.assertIn("No development base is recommended", e["next"])
        self.assertNotIn("Development base: release/", self.brief())
        self.assertIn("release-ambiguous", [x["kind"] for x in r["reminders"]])
        # one fix version, no mapping, two branches that could be it: suggestions only
        self.jira("ABC-1001", [V_DEC], uat="No")
        cfg = self.cfg()
        cfg["releases"]["repos"]["service-api"]["branches"] = {}
        self.track(cfg=cfg)
        b = self.repo("service-api")["branch"]
        self.assertEqual(b["state"], "suggested")
        self.assertGreaterEqual(len(b["candidates"]), 2)
        self.assertEqual(b["candidates"][0]["name"], DEC)  # its creation parent and its name month rank it first
        self.assertFalse(self.store.ticket("ABC-1001")["git"]["repos"]["service-api"]["targets"][0].get("pinned"))
        self.assertNotIn("git switch", self.repo("service-api")["next"])

    def test_04_release_change_keeps_history_and_flags_old_targets(self):
        self.jira("ABC-1001", [V_NOV], uat="No")
        head = self.api.dev("feature/ABC-1001-x", NOV, ["ABC-1001 one"])[-1]
        self.prs["service-api"] = [pr(31, "feature/ABC-1001-x", NOV, state="open", head=head)]
        self.track()
        self.assertEqual(self.repo("service-api")["branch"]["name"], NOV)
        self.assertEqual(self.repo("service-api")["flags"], [])
        self.jira("ABC-1001", [V_DEC], uat="No")  # moved to the December release
        self.track()
        r = self.rel()
        self.assertEqual(r["history"][-1]["releases"][0]["name"], "FY99 November 9th Release")
        self.assertEqual(r["history"][-1]["branches"], {"service-api": NOV})
        e = self.repo("service-api")
        self.assertEqual(e["branch"]["name"], DEC)
        kinds = sorted(f["kind"] for f in e["flags"])
        self.assertEqual(kinds, ["old-release-base", "old-release-pr"])
        self.assertEqual([p["number"] for p in e["prs"]["release"]], [31])  # the PR stays on record
        self.assertIn("Moved from FY99 November 9th Release", self.brief())

    def test_05_development_pr_into_the_wrong_release_is_flagged(self):
        self.jira("ABC-1001", [V_DEC], uat="No")
        head = self.api.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])[-1]
        self.prs["service-api"] = [pr(41, "feature/ABC-1001-x", NOV, state="open", head=head)]
        self.track()
        e = self.repo("service-api")
        self.assertEqual([f["kind"] for f in e["flags"]], ["wrong-target"])
        self.assertIn("PR #41 targets %s" % NOV, e["flags"][0]["text"])
        self.assertEqual(e["next"], "Retarget PR #41 from %s to %s in the Git host (or open a new PR "
                                    "feature/ABC-1001-x → %s)." % (NOV, DEC, DEC))
        self.assertIn("⚠ PR #41 targets", self.brief())
        self.assertIn("wrong-target", [x["kind"] for x in self.rel()["reminders"]])
        # the guard says so before the PR is created (file reads only, no git)
        from chitragupta import gitro, guard
        real = gitro.run
        gitro.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the guard ran git"))
        try:
            msgs = guard.check("gh pr create --base %s --head feature/ABC-1001-x" % NOV, self.api.work, self.cfg())
            msgs2 = guard.check("git checkout -b feature/ABC-1001-y origin/%s" % NOV, self.api.work, self.cfg())
        finally:
            gitro.run = real
        self.assertTrue(any("ships from %s" % DEC in m for m in msgs), msgs)
        self.assertTrue(any("ships from %s" % DEC in m for m in msgs2), msgs2)

    def test_06_release_merged_qa_missing_explains_the_integration_branch(self):
        self.jira("ABC-1001", [V_DEC], uat="No")
        self.api.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])
        m, h = self.api.merge("feature/ABC-1001-x", DEC)
        self.prs["service-api"] = [pr(51, "feature/ABC-1001-x", DEC, merge_commit=m, head=h)]
        self.track()
        e = self.repo("service-api")
        self.assertEqual((e["stages"]["release"]["state"], e["stages"]["qa"]["state"]), ("present", "missing"))
        nxt = e["next"]
        self.assertIn("create a QA integration branch from qa", nxt)
        self.assertIn("`git switch -c feature/ABC-1001-x-qa origin/qa`", nxt)
        self.assertIn("merge %s into it" % DEC, nxt)
        self.assertIn("resolve conflicts there", nxt)
        self.assertIn("PR feature/ABC-1001-x-qa → qa", nxt)

    def test_07_one_integration_pr_is_linked_to_each_evidenced_ticket(self):
        for t in ("ABC-1001", "ABC-1002", "ABC-1003"):
            self.jira(t, [V_NOV if t == "ABC-1003" else V_DEC], uat="No", assignee=t != "ABC-1002")
        sb = self.api
        sb.dev("feature/ABC-1001-a", DEC, ["ABC-1001 a"])
        m1, h1 = sb.merge("feature/ABC-1001-a", DEC)
        sb.dev("feature/ABC-1002-b", DEC, ["ABC-1002 b"], who=OTHER)
        m2, h2 = sb.merge("feature/ABC-1002-b", DEC)
        sb.dev("feature/ABC-1003-c", NOV, ["ABC-1003 c"])
        m3, h3 = sb.merge("feature/ABC-1003-c", NOV)
        mi, hi = sb.integrate("qa-sync/dec-release", DEC)
        self.prs["service-api"] = [pr(61, "feature/ABC-1001-a", DEC, merge_commit=m1, head=h1),
                                   pr(62, "feature/ABC-1002-b", DEC, merge_commit=m2, head=h2),
                                   pr(63, "feature/ABC-1003-c", NOV, merge_commit=m3, head=h3),
                                   pr(64, "qa-sync/dec-release", "qa", merge_commit=mi, head=hi)]
        keys = ("ABC-1001", "ABC-1002", "ABC-1003")
        self.track(keys)

        def qa_prs(t):
            e = self.repo("service-api", t)
            return sorted({p["number"] for p in (e["prs"].get("qa") or []) + [p for p in e["shared"] if p["role"] == "qa"]})
        self.assertEqual(qa_prs("ABC-1001"), [64])
        self.assertEqual(qa_prs("ABC-1002"), [64])
        self.assertEqual(qa_prs("ABC-1003"), [])  # its change isn't in that PR: no invented link
        # linked by its commit messages to one of the two tickets; for the other it is a shared PR that carries it
        sh = [p for t in ("ABC-1001", "ABC-1002") for p in self.repo("service-api", t)["shared"] if p["number"] == 64]
        self.assertEqual([(p["carries"], p["of"], p["shared"]) for p in sh], [(1, 1, True)])
        self.assertEqual(self.repo("service-api", "ABC-1002")["stages"]["qa"]["state"], "present")
        # the shared PR doesn't make a teammate's ticket mine
        from chitragupta import release
        ov = release.month_overview("2026-12", self.cfg(), now=NOW, store=self.store)
        keys_in = [r["key"] for g in ov["groups"] for r in g["tickets"]]
        self.assertIn("ABC-1001", keys_in)
        self.assertNotIn("ABC-1002", keys_in)

    def test_08_squash_follow_up_and_revert_evidence(self):
        self.jira("ABC-1001", [V_DEC], uat="No")
        sb = self.api
        sb.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one", "ABC-1001 two"])
        m1, h1 = sb.merge("feature/ABC-1001-x", DEC, squash=True, msg="ABC-1001 squashed (#71)")
        sb.git("checkout", "-q", "feature/ABC-1001-x")
        sb.commit("ABC-1001 follow-up")
        sb.push("feature/ABC-1001-x")
        m2, h2 = sb.merge("feature/ABC-1001-x", DEC)
        self.prs["service-api"] = [pr(71, "feature/ABC-1001-x", DEC, merge_commit=m1, head=h1),
                                   dict(pr(72, "feature/ABC-1001-x", DEC, merge_commit=m2, head=h2),
                                        created="2026-10-02T10:00:00+05:30", merged_at="2026-10-03T10:00:00+05:30")]
        self.track()
        e = self.repo("service-api")
        self.assertEqual(e["stages"]["release"]["state"], "present")
        rel_prs = e["prs"]["release"]
        self.assertEqual([p["number"] for p in rel_prs], [71, 72])
        self.assertEqual(rel_prs[0].get("note"), "followed by #72")
        # a revert on the release branch takes the follow-up back out
        sb.git("checkout", "-q", DEC)
        sb.git("pull", "-q", "--ff-only", "origin", DEC)
        follow = sb.git("log", "-1", "--format=%H", "--no-merges", "feature/ABC-1001-x")
        sb.git("revert", "--no-edit", follow)
        sb.push(DEC)
        sb.fetch()
        self.track()
        st = self.repo("service-api")["stages"]["release"]
        self.assertEqual(st["state"], "partial")
        self.assertEqual(st["counts"]["reverted"], 1)
        self.assertIn("reverted there", st["detail"])

    def test_09_required_uat_missing_keeps_reminding(self):
        self.jira("ABC-1001", [V_DEC], uat="Yes")
        sb = self.api
        sb.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])
        m1, h1 = sb.merge("feature/ABC-1001-x", DEC)
        mi, hi = sb.integrate("qa-sync/dec", DEC)
        self.prs["service-api"] = [pr(81, "feature/ABC-1001-x", DEC, merge_commit=m1, head=h1),
                                   pr(82, "qa-sync/dec", "qa", merge_commit=mi, head=hi)]
        self.track()
        e = self.repo("service-api")
        self.assertEqual(self.rel()["uat"]["requirement"]["state"], "required")
        self.assertEqual(e["stages"]["uat"]["state"], "missing")
        self.assertIn("UAT next", e["next"])
        self.assertIn("from uat", e["next"])
        ids = [x["id"] for x in self.rel()["reminders"]]
        self.assertIn("uat-missing:service-api", ids)
        self.assertIn("Reminders: service-api: UAT required", self.brief())  # shown...
        self.track()                                                        # ...and still there next run
        self.assertIn("uat-missing:service-api", [x["id"] for x in self.rel()["reminders"]])
        from chitragupta.dashboard import api
        self.assertIn("uat-missing", [x["kind"] for x in api.ticket_summary("ABC-1001", self.store.ticket("ABC-1001"))["reminders"]])

    def test_10_uat_unknown_asks_not_required_stays_quiet(self):
        self.jira("ABC-1001", [V_DEC], uat=None)
        self.api.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])
        self.track()
        r = self.rel()
        self.assertEqual(r["uat"]["requirement"]["state"], "unknown")
        self.assertEqual(r["reminders"][0]["kind"], "uat-unknown")
        self.assertFalse(r["readiness"]["ready"])
        self.jira("ABC-1001", [V_DEC], uat="No")
        self.track()
        r = self.rel()
        self.assertEqual(r["uat"]["requirement"]["state"], "not_required")
        self.assertEqual(self.repo("service-api")["stages"]["uat"]["state"], "not_applicable")
        self.assertFalse([x for x in r["reminders"] if x["kind"].startswith("uat")])

    def test_11_uat_branch_has_changes_but_deployment_is_unverified(self):
        self.jira("ABC-1001", [V_DEC], uat="Yes")
        sb = self.api
        sb.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])
        m1, h1 = sb.merge("feature/ABC-1001-x", DEC)
        mi, hi = sb.integrate("qa-sync/dec", DEC)
        c = sb.git("log", "-1", "--format=%H", "--no-merges", "feature/ABC-1001-x")
        sb.branch("feature/ABC-1001-x-uat", "origin/uat")
        sb.git("cherry-pick", c)
        sb.push("feature/ABC-1001-x-uat")
        mu, hu = sb.merge("feature/ABC-1001-x-uat", "uat")
        self.prs["service-api"] = [pr(91, "feature/ABC-1001-x", DEC, merge_commit=m1, head=h1),
                                   pr(92, "qa-sync/dec", "qa", merge_commit=mi, head=hi),
                                   pr(93, "feature/ABC-1001-x-uat", "uat", merge_commit=mu, head=hu)]
        self.track()
        e = self.repo("service-api")
        self.assertEqual(e["stages"]["uat"]["state"], "present")
        r = self.rel()
        self.assertEqual((r["uat"]["deployed"]["state"], r["uat"]["accepted"]["state"]), ("unknown", "unknown"))
        kinds = [x["kind"] for x in r["reminders"]]
        self.assertIn("uat-deployed", kinds)
        self.assertIn("uat-accepted", kinds)
        self.assertNotIn("uat-missing", kinds)
        self.assertFalse(r["readiness"]["ready"])
        # explicit evidence settles acceptance only
        self.over = {"jira": {"uat_field": UAT_FIELD, "uat_accepted_statuses": ["UAT Done"]}}
        self.jira("ABC-1001", [V_DEC], uat="Yes", status="UAT Done")
        self.track()
        r = self.rel()
        self.assertEqual((r["uat"]["deployed"]["state"], r["uat"]["accepted"]["state"]), ("unknown", "yes"))

    def test_12_later_changes_need_promotion_again(self):
        self.jira("ABC-1001", [V_DEC], uat="No")
        sb = self.api
        sb.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])
        m1, h1 = sb.merge("feature/ABC-1001-x", DEC)
        mi, hi = sb.integrate("qa-sync/dec", DEC)
        self.prs["service-api"] = [pr(101, "feature/ABC-1001-x", DEC, merge_commit=m1, head=h1),
                                   pr(102, "qa-sync/dec", "qa", merge_commit=mi, head=hi)]
        self.track()
        self.assertEqual(self.repo("service-api")["stages"]["qa"]["state"], "present")
        sb.git("checkout", "-q", "feature/ABC-1001-x")
        sb.commit("ABC-1001 later fix")
        sb.push("feature/ABC-1001-x")
        m3, h3 = sb.merge("feature/ABC-1001-x", DEC)
        self.prs["service-api"].append(dict(pr(103, "feature/ABC-1001-x", DEC, merge_commit=m3, head=h3),
                                            created="2026-10-05T10:00:00+05:30"))
        self.track()
        e = self.repo("service-api")
        self.assertEqual(e["stages"]["release"]["state"], "present")
        self.assertEqual(e["stages"]["qa"]["state"], "partial")
        self.assertIn("was complete", e["stages"]["qa"]["detail"])
        self.assertIn("later commits are in %s but not in qa" % DEC, e["next"])
        self.assertFalse(self.rel()["readiness"]["ready"])

    def test_13_stale_or_unavailable_sources_never_become_missing(self):
        self.jira("ABC-1001", [V_DEC], uat="No")
        self.api.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])
        old = time.mktime((2026, 9, 1, 0, 0, 0, 0, 0, -1))
        fh = os.path.join(self.api.work, ".git", "FETCH_HEAD")
        os.utime(fh, (old, old))
        self.track(now=dt.datetime.now().astimezone())
        st = self.repo("service-api")["stages"]["release"]
        self.assertEqual(st["state"], "unknown")
        self.assertIn("not confirmed missing", st["detail"])
        self.assertIn("Uncertainty:", self.brief())
        self.assertIn("Refresh first", self.repo("service-api")["next"])
        os.utime(fh, None)  # fresh again for the PR-source check below
        # Jira unreadable: the release is unknown, not "no release"
        self.jira("ABC-1002", error="tool error: 401")
        self.track(("ABC-1002",))
        r = self.rel("ABC-1002")
        self.assertEqual(r["selection"]["how"], "unknown")
        self.assertNotIn("no fix version", json.dumps(r["selection"]))
        # merge commits only (no host CLI or MCP): open PRs can't be seen, so the next step is hedged
        from chitragupta import release
        self.rx["service-api"].data.meta["pr_source"] = "merge-commit"
        release.refresh(self.store, self.rx, self.cfg(), ["ABC-1001"], now=dt.datetime.now().astimezone(), phase="evaluate")
        e = self.repo("service-api")
        self.assertFalse(e["freshness"]["open_prs_visible"])
        self.assertIn("open PRs aren't visible", e["next"])
        self.assertIn("open PRs not visible", self.brief())

    def test_14_month_overview_boundary_overdue_and_partial_repos(self):
        from chitragupta import release
        cfg = self.cfg()

        def rec(key, planned, ready=False, repos=None, first="2026-10-01", status=""):
            v = {"key": "ABC:%s" % key, "name": "Release " + (planned or "?"), "planned_date": planned}
            r = {"v": 1, "releases": [v], "selected": v["key"], "selection": {"how": "the ticket's fix version"},
                 "repos": repos or {}, "readiness": {"ready": ready, "missing": [] if ready else ["service-ui: qa missing"]},
                 "uat": {"requirement": {"state": "not_required"}}, "reminders": [], "jira": {"source": "card"}}
            self.ticket(key, {"id": key, "first": first, "last": "2026-11-20", "release": r,
                              "jira": {"status": status} if status else {}})
        done = {"stages": {"release": {"state": "present"}, "qa": {"state": "present"}}, "branch": {"name": DEC, "state": "confirmed"}}
        half = {"stages": {"release": {"state": "present"}, "qa": {"state": "missing"}}, "branch": {"name": DEC, "state": "confirmed"}}
        rec("ABC-2001", "2026-12-14", repos={"service-api": done, "service-ui": half})
        rec("ABC-2002", "2026-11-30")            # last day of November: overdue in December, not in its group
        rec("ABC-2003", None)                    # no planned date: its own group
        rec("ABC-2004", "2027-01-03")            # next month: not shown
        rec("ABC-2005", "2026-12-01", status="Cancelled")
        rec("ABC-2006", "2026-12-14", first="")  # not mine: no work, no assignment, no branch
        os.makedirs(os.path.dirname(release.scope_path()), exist_ok=True)
        with open(release.scope_path(), "w", encoding="utf-8") as f:
            json.dump({"fetched_at": "2026-12-03T08:00:00+05:30", "mine_only": True, "issues": {
                "ABC-2007": {"versions": [{"id": "", "name": "Release 2026-12-14", "project": "ABC",
                                           "release_date": "2026-12-14"}], "summary": "Not started yet",
                             "status": "To Do", "assignee_me": None}}}, f)
        ov = release.month_overview(None, cfg, now=dt.datetime(2026, 12, 3, 10, 0, tzinfo=TZ))
        self.assertEqual(ov["month"], "2026-12")
        dec = {r["key"]: r for g in ov["groups"] for r in g["tickets"]}
        self.assertEqual(sorted(dec), ["ABC-2001", "ABC-2007"])
        self.assertFalse(dec["ABC-2001"]["ready"])
        self.assertEqual([x["repo"] for x in dec["ABC-2001"]["repos"]], ["service-api", "service-ui"])
        self.assertEqual(dec["ABC-2001"]["repos"][1]["qa"]["state"], "missing")
        self.assertEqual(dec["ABC-2007"]["missing"], ["not started: no branches or worklog yet"])
        self.assertEqual([r["key"] for r in ov["overdue"]], ["ABC-2002"])
        self.assertEqual([r["key"] for r in ov["unresolved"]], ["ABC-2003"])
        nov = release.month_overview("2026-11", cfg, now=dt.datetime(2026, 12, 3, 10, 0, tzinfo=TZ))
        self.assertEqual([r["key"] for g in nov["groups"] for r in g["tickets"]], ["ABC-2002"])  # stays in its month
        text = "\n".join(release.overview_lines(ov))
        self.assertIn("## Overdue", text)
        self.assertIn("## Release date unresolved", text)
        with self.assertRaises(ValueError):
            release.month_overview("2026-13", cfg)

    def test_15_session_context_hook_and_read_only(self):
        self.jira("ABC-1001", [V_DEC], uat="Yes")
        self.api.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])
        self.track()
        from chitragupta import render
        ctx = render.render_context(self.store, "ABC-1001")
        self.assertIn("Release plan for ABC-1001", ctx)
        self.assertIn("Repository service-api:", ctx)
        self.assertIn("Next:", ctx)
        # SessionStart only reads the cached file and says how old it is
        from tests.support import child_env, run_hook, write_config
        cfgd = self.cfg()
        cfgd["setup_done"] = True
        write_config(self.home, cfgd)
        os.makedirs(os.path.join(self.home, ".worklog", "cache", "context"), exist_ok=True)
        with open(os.path.join(self.home, ".worklog", "cache", "context", "ABC-1001.md"), "w", encoding="utf-8") as f:
            f.write(ctx)
        env = child_env(self.home, self.user_dir)
        p = run_hook("session-start", {"session_id": "s1", "cwd": self.api.work, "source": "startup"}, env)
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout.decode())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Release plan for ABC-1001", out)
        self.assertIn("Development base: %s" % DEC, out)
        self.assertIn("Cached worklog context, written 0 min ago", out)

    def test_16_disabled_changes_nothing(self):
        self.jira("ABC-1001", [V_DEC], uat="Yes")
        self.api.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])
        cfg = self.cfg(enabled=False)
        self.track(cfg=cfg)
        tk = self.store.ticket("ABC-1001")
        self.assertNotIn("release", tk)
        self.assertNotIn("pinned", tk["git"]["repos"]["service-api"]["targets"][0])


class AgentAndCli(ReleaseCase):
    def setUp(self):
        super().setUp()
        from tests.support import write_config
        write_config(self.home, self.cfg())

    def test_check_runs_resolve_delivery_evaluate_and_renders_the_brief(self):
        self.jira("ABC-1001", [V_DEC], uat="Yes")
        self.api.dev("feature/ABC-1001-x", DEC, ["ABC-1001 one"])
        from chitragupta import agent, common
        res = agent.cmd_branch_check("ABC-1001", wait_s=5)  # fetches the local bare origin only
        self.assertEqual(res["tickets"], ["ABC-1001"])
        with open(os.path.join(self.home, "data", "tickets", "ABC-1001.json"), encoding="utf-8") as f:
            tk = json.load(f)
        e = tk["release"]["repos"]["service-api"]
        self.assertEqual(e["branch"]["state"], "confirmed")
        self.assertEqual(tk["git"]["repos"]["service-api"]["targets"][0]["name"], DEC)
        self.assertEqual([t["role"] for t in tk["git"]["repos"]["service-api"]["targets"]], ["release", "qa", "uat"])
        ctx = common.read_text(common.wl("cache", "context", "ABC-1001.md"))
        self.assertIn("Release plan for ABC-1001", ctx)
        self.assertNotIn(" ago", ctx.split("Release plan", 1)[1].split("\n")[0])  # cached text keeps absolute times
        readme = common.read_text(os.path.join(self.home, "tickets", "ABC-1001", "README.md"))
        self.assertIn("## Release plan", readme)

    def test_inbox_directive_and_cli(self):
        import contextlib
        import io
        from chitragupta import agent, cli, common
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["branch", "release-map", "service-api", "FY99 December 14th Release", DEC2]), 0)
        self.assertIn('release-map: service-api "FY99 December 14th Release" %s' % DEC2,
                      common.read_text(os.path.join(self.home, "inbox.md")))
        a = agent.Agent(force=True)
        self.assertTrue(a.handle_release_directive({"text": 'release-map: service-api "FY99 December 14th Release" %s' % DEC2,
                                                    "ts": "2026-10-10T10:00:00+05:30"}))
        self.assertTrue(a.cfg_dirty)
        self.assertEqual(a.cfg["releases"]["repos"]["service-api"]["branches"]["FY99 December 14th Release"], DEC2)
        self.assertTrue(a.handle_release_directive({"text": "uat: ABC-1001 required", "ts": "2026-10-10T10:00:00+05:30"}))
        self.assertEqual(a.store.ticket("ABC-1001")["release_override"]["uat"], "required")
        self.assertIn("ABC-1001", a.git_force)
        self.assertFalse(a.handle_release_directive({"text": "branch-link: ABC-1001 service-api x"}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["branches"]), 0)  # no key: the month overview
        self.assertIn("# Releases — ", buf.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["branch", "uat", "ABC-1001", "maybe"]), 2)

    def test_shareable_export_drops_release_pr_links(self):
        from chitragupta import export
        url = "https://git.acme.test/service-api/pull/7"
        t = {"release_plan": {"repos": {"service-api": {
            "prs": {"release": [{"number": 7, "url": url}], "qa": []},
            "shared": [{"number": 8, "url": url, "shared": True}]}}}}
        out = export.drop_local(json.loads(json.dumps(t)), "shareable")
        e = out["release_plan"]["repos"]["service-api"]
        self.assertEqual((e["prs"]["release"][0]["url"], e["shared"][0]["url"]), (None, None))
        self.assertEqual(export.drop_local(json.loads(json.dumps(t)), "team")["release_plan"]["repos"]["service-api"]
                         ["prs"]["release"][0]["url"], url)  # a team export keeps links, like branch tracking's

    def test_mcp_guard_allows_the_versions_read_and_still_blocks_writes(self):
        from chitragupta import mcpjobs
        from chitragupta.safety import mcp
        allow = mcpjobs.allow_default(self.cfg())
        self.assertIsNone(mcp.mcp_reason("mcp__mcp-atlassian__jira_get_project_versions", {"project_key": "ABC"}, allow))
        self.assertTrue(mcp.mcp_reason("mcp__mcp-atlassian__jira_update_version", {"version_id": "1"}, allow))
        self.assertTrue(mcp.mcp_reason("mcp__mcp-atlassian__jira_create_version", {"name": "x"}, allow))


class Jobs(ReleaseCase):
    def test_release_jobs_are_bounded_and_due_based(self):
        from chitragupta import kbjobs, mcpjobs, release
        calls = []
        real = (mcpjobs.jira_release_scope, mcpjobs.jira_versions)

        def scope(cfg, health):
            calls.append("scope")
            return release.save_scope([{"key": "ABC-1001", "fields": {"fixVersions": [V_DEC], "status": {"name": "To Do"}}}],
                                      "assignee = currentUser()", cfg, common_now())

        def versions(p, cfg, health):
            calls.append("versions:" + p)
            rec = release.merge_catalog({}, release.parse_versions([V_DEC], p), common_now())
            with open(release.versions_path(p), "w", encoding="utf-8") as f:
                json.dump(rec, f)
            return rec
        from chitragupta import common

        def common_now():
            return common.now_iso()
        mcpjobs.jira_release_scope, mcpjobs.jira_versions = scope, versions
        try:
            os.makedirs(os.path.dirname(release.versions_path("ABC")), exist_ok=True)

            class A:
                cfg = self.cfg()
                health = {}
                state = {}
            jobs = kbjobs.KBJobs(A())
            self.assertEqual(jobs.release_jobs({}, limit=2), 2)
            self.assertEqual(calls, ["scope", "versions:ABC"])
            self.assertEqual(jobs.release_jobs({}, limit=2), 0)  # both fresh: nothing is due
        finally:
            mcpjobs.jira_release_scope, mcpjobs.jira_versions = real
        with open(release.scope_path(), encoding="utf-8") as f:
            sc = json.load(f)
        self.assertTrue(sc["mine_only"])
        self.assertEqual(sc["issues"]["ABC-1001"]["versions"][0]["name"], "FY99 December 14th Release")
        self.assertNotIn("fields", sc["issues"]["ABC-1001"])  # only release facts are kept


if __name__ == "__main__":
    unittest.main()
