"""FLOW-03 CI checks: one real read-only adapter (GitHub check runs through `gh api --method GET`) exercised with
synthetic recorded responses, capability states for the other hosts, the tick's PR-read step, the ticket projection
and the Companion evidence integration (A11 semantics, A13, A14, VERIFY-01).

Synthetic data only (ABC-…, acme.test, acme/shop-api). `run` is injected everywhere and every real process start is a
tripwire: no gh, glab, az, git, model or network call can happen here.
"""
import datetime as dt
import hashlib
import json
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.support import Isolated, write_config
from tests.test_companion_state import Base
from chitragupta import (activity, common, companion, companionevidence as evidence, delivery, gittrack, hosts,
                         profile, store, workflowstore)
from chitragupta.safety import Blocked, commands

R1, R2, R3, R4 = "a1" * 20, "b2" * 20, "c3" * 20, "d4" * 20
INFO = {"host": "github", "path": "acme/shop-api"}
ME = {"provider": "github", "instance": "github.com", "login": "dev-a", "node_id": "U_kgDOdeva"}
T = "2026-11-08T09:%02d:00Z"


def check(rid, name="unit", conclusion="success", status="completed", head=R1, app="github-actions", **extra):
    """One check run as GitHub returns it, with the fields that must never be stored (URLs, output, PR links)."""
    run = {"id": rid, "head_sha": head, "node_id": "CR_%d" % rid, "external_id": "ext-%d" % rid,
           "url": "https://api.github.com/repos/acme/shop-api/check-runs/%d" % rid,
           "html_url": "https://github.com/acme/shop-api/runs/%d" % rid,
           "details_url": "https://ci.acme.test/job/%d" % rid, "status": status,
           "conclusion": conclusion if status == "completed" else None, "started_at": T % (rid % 60),
           "completed_at": T % (rid % 60) if status == "completed" else None, "name": name,
           "output": {"title": "Build log", "summary": "secret build output", "text": "stack trace", "annotations_count": 1},
           "check_suite": {"id": 9}, "pull_requests": [{"number": 7, "url": "https://api.github.com/x"}],
           "app": {"id": 15368, "slug": app, "name": "GitHub Actions", "owner": {"login": "github"}}}
    run.update(extra)
    return run


def page(runs, total=None):
    return 0, json.dumps({"total_count": len(runs) if total is None else total, "check_runs": runs}), ""


def failed(se, rc=1, so=""):
    return rc, so, se


RATE = failed("gh: API rate limit exceeded for user ID 1. (HTTP 403)", so='{"message":"API rate limit exceeded"}')
SECONDARY = failed("gh: You have exceeded a secondary rate limit. (HTTP 429)")
DENIED = failed("gh: Resource not accessible by integration (HTTP 403)")
MISSING = failed("gh: No commit found for SHA (HTTP 404)")
TIMEOUT = failed("timeout after 30s", rc=124)


class Recorded:
    """Synthetic recorded `gh` responses keyed by (head, page), plus one `gh pr list` answer; keeps every argv."""

    def __init__(self, pages=None, prs=None):
        self.pages, self.prs, self.calls = dict(pages or {}), prs, []

    def __call__(self, argv, timeout=None, **kw):
        self.calls.append(list(argv))
        if argv[1:3] in (["pr", "list"], ["mr", "list"], ["repos", "pr"]):
            return self.prs
        m = re.search(r"commits/([0-9a-f]+)/check-runs\?filter=all&per_page=100&page=(\d+)$", argv[-1])
        return self.pages.get((m.group(1), int(m.group(2))) if m else None, failed("gh: unexpected (HTTP 500)"))

    def api(self):
        return [c for c in self.calls if c[1] == "api"]


def read(pr, rec, at="2026-11-08T10:00:00+00:00"):
    """One read of the PR's current head stored on the PR fact, as the tick does it."""
    return hosts.ci_record(pr, INFO, hosts.gh_check_runs({}, INFO, pr["head"], run=rec), at)


def open_pr(head=R1, number=7, **extra):
    return dict({"number": number, "state": "open", "review": "", "source": "feature/ABC-1001", "target": "main",
                 "title": "Order filter", "head": head, "src": "gh"}, **extra)


class Tripwires:
    def trip(self):
        self.processes = []

        def no_process(*args, **kwargs):
            self.processes.append(args[:1])
            raise AssertionError("CI code started a process")
        for name in ("run", "spawn"):
            p = mock.patch.object(commands, name, no_process)
            p.start()
            self.addCleanup(p.stop)
        exe = mock.patch.object(hosts, "cli_path", lambda cfg, name: name)
        exe.start()
        self.addCleanup(exe.stop)
        self.addCleanup(lambda: self.assertEqual(self.processes, [], "no gh, glab, az or git process"))


# --------------------------------------------------------------------------- the GitHub adapter

class GitHubAdapter(Tripwires, Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "profile": {"accounts": [ME]}})
        self.trip()

    def test_exact_get_request_passes_the_real_guard_and_nothing_else_does(self):
        rec = Recorded({(R1, 1): page([check(1)])})
        got = hosts.gh_check_runs({}, INFO, R1, run=rec)
        argv = ["gh", "api", "--method", "GET",
                "repos/acme/shop-api/commits/%s/check-runs?filter=all&per_page=100&page=1" % R1]
        self.assertEqual(rec.calls, [argv])
        self.assertEqual((got["coverage"], got["reason"], got["total"]), ("complete", "", 1))
        self.assertIsNone(commands.reason(argv, "host"), "the existing guard allows this GET unchanged")
        for bad in (argv[:2] + ["--method", "POST"] + argv[4:], argv + ["-f", "conclusion=success"],
                    ["gh", "run", "rerun", "1"], ["gh", "run", "cancel", "1"], ["gh", "run", "view", "1", "--log"]):
            self.assertIsNotNone(commands.reason(bad, "host"), bad)

    def test_mapping_keeps_minimal_sanitized_fields(self):
        cases = {"success": "passed", "failure": "failed", "cancelled": "interrupted", "timed_out": "interrupted",
                 "action_required": "action_required", "neutral": "neutral", "skipped": "skipped", "stale": "stale",
                 "something_new": "unknown"}
        runs = [check(10 + i, name="c-%s" % c, conclusion=c) for i, c in enumerate(cases)]
        runs += [check(30, name="queued", status="queued"), check(31, name="running", status="in_progress"),
                 check(32, name="unit \x1b[31mred ghp_" + "a" * 36, conclusion="success"),
                 check(33, name="other head", head=R2)]
        pr = open_pr()
        read(pr, Recorded({(R1, 1): page(runs)}))
        stored = {x["name"]: x for x in pr["ci"]["heads"][0]["runs"]}
        for c, outcome in cases.items():
            self.assertEqual(stored["c-" + c]["outcome"], outcome, c)
        self.assertEqual((stored["queued"]["outcome"], stored["running"]["outcome"]), ("running", "running"))
        self.assertEqual(stored["c-something_new"]["conclusion"], None, "unknown conclusions are not stored verbatim")
        self.assertNotIn("other head", stored, "a run of another commit is never filed under this head")
        self.assertEqual(pr["ci"]["heads"][0]["reason"], "unrecognised check runs skipped")
        self.assertEqual(set(stored["c-success"]), {"id", "name", "status", "conclusion", "outcome", "started_at",
                                                    "completed_at", "head_sha", "app", "attempt", "attempt_basis"})
        self.assertEqual((stored["c-success"]["head_sha"], stored["c-success"]["app"]), (R1, "github-actions"))
        blob = json.dumps(pr)
        for never in ("https://", "secret build output", "stack trace", "ghp_", "\x1b", "\\u001b", "CR_", "ext-"):
            self.assertNotIn(never, blob)

    def test_pagination_is_bounded_and_a_cut_is_partial(self):
        full = {(R1, n): page([check(n * 1000 + i, "c%d" % (n * 1000 + i)) for i in range(100)], total=350)
                for n in (1, 2, 3, 4)}
        rec = Recorded(full)
        got = hosts.gh_check_runs({}, INFO, R1, run=rec)
        self.assertEqual(len(rec.api()), hosts.CI_PAGES, "at most 1 + 2 further pages")
        self.assertEqual([c[-1][-6:] for c in rec.api()], ["page=1", "page=2", "page=3"])
        self.assertEqual((got["coverage"], got["reason"], len(got["runs"]), got["total"]),
                         ("partial", "pagination cut", 300, 350))
        pr = open_pr()
        hosts.ci_record(pr, INFO, got, "2026-11-08T10:00:00+00:00")
        state = hosts.ci_state(pr)
        self.assertEqual(state["state"], "partial", "300 passes of 350 checks are never 'passed'")
        self.assertIn("pagination cut", state["text"])
        two = Recorded({(R1, 1): page([check(i, "c%d" % i) for i in range(1, 101)], total=150),
                        (R1, 2): page([check(i, "c%d" % i) for i in range(101, 151)], total=150)})
        got = hosts.gh_check_runs({}, INFO, R1, run=two)
        self.assertEqual((len(two.api()), got["coverage"], len(got["runs"])), (2, "complete", 150))

    def test_rate_limit_and_permission_keep_the_last_complete_set(self):
        pr = open_pr()
        read(pr, Recorded({(R1, 1): page([check(1), check(2, name="lint")])}), at="2026-11-08T10:00:00+00:00")
        self.assertEqual(hosts.ci_state(pr)["state"], "passed")
        before = [x["id"] for x in pr["ci"]["heads"][0]["runs"]]
        for answer, why in ((RATE, "rate-limited"), (SECONDARY, "rate-limited"), (DENIED, "permission denied"),
                            (MISSING, "not found"), (TIMEOUT, "timeout")):
            read(pr, Recorded({(R1, 1): answer}), at="2026-11-08T11:00:00+00:00")
            block = pr["ci"]["heads"][0]
            self.assertEqual([x["id"] for x in block["runs"]], before, "never 'no checks'")
            self.assertEqual((block["coverage"], block["reason"], block["stale"]), ("partial", why, True))
            self.assertEqual(block["observed_at"], "2026-11-08T10:00:00+00:00", "observation time of the kept runs")
            self.assertEqual(block["attempted_at"], "2026-11-08T11:00:00+00:00")
            state = hosts.ci_state(pr)
            self.assertEqual(state["state"], "partial", "a stale complete set is no longer a complete pass")
            self.assertIn(why, state["text"])
            self.assertIn("earlier runs kept", state["text"])
        read(pr, Recorded({(R1, 1): page([check(1), check(2, name="lint")])}), at="2026-11-08T12:00:00+00:00")
        self.assertEqual((pr["ci"]["heads"][0]["stale"], hosts.ci_state(pr)["state"]), (False, "passed"))

    def test_a_failure_on_a_later_page_keeps_earlier_pages_and_uses_fresh_ones(self):
        pr = open_pr()
        first = [check(i, "c%d" % i) for i in range(1, 101)]
        read(pr, Recorded({(R1, 1): page(first, total=150),
                           (R1, 2): page([check(i, "c%d" % i) for i in range(101, 151)], total=150)}))
        self.assertEqual(hosts.ci_state(pr)["state"], "passed")
        fresh = [check(1, "c1", conclusion="failure")] + first[1:]
        read(pr, Recorded({(R1, 1): page(fresh, total=150), (R1, 2): RATE}))
        block = pr["ci"]["heads"][0]
        self.assertEqual((len(block["runs"]), block["coverage"], block["reason"], block["stale"]),
                         (150, "partial", "rate-limited", True))
        self.assertEqual(block["runs"][0]["outcome"], "failed", "fresh pages replace what they cover")
        self.assertEqual(hosts.ci_state(pr)["state"], "failing", "a known failure stays a failure")

    def test_first_read_failure_is_unavailable_and_zero_runs_is_missing(self):
        pr = open_pr()
        read(pr, Recorded({(R1, 1): DENIED}))
        state = hosts.ci_state(pr)
        self.assertEqual((state["state"], state["text"]), ("unavailable", "CI checks unavailable: permission denied"))
        read(pr, Recorded({(R1, 1): page([])}))
        state = hosts.ci_state(pr)
        self.assertEqual((state["state"], state["text"]), ("missing", "No CI checks reported for head %s" % R1[:7]))

    def test_two_attempts_stay_distinct_and_the_latest_decides(self):
        pr = open_pr()
        read(pr, Recorded({(R1, 1): page([check(205, "unit"), check(101, "unit", "failure"), check(150, "lint")])}))
        runs = {x["id"]: (x["name"], x["attempt"], x["attempt_basis"]) for x in pr["ci"]["heads"][0]["runs"]}
        self.assertEqual(runs, {101: ("unit", 1, "order"), 150: ("lint", 1, "order"), 205: ("unit", 2, "order")})
        self.assertEqual(hosts.ci_state(pr)["state"], "passed", "the re-run passed; attempt 1 stays on record")
        ids = sorted(row["id"] for row, _ in evidence.ci_rows("shop-api", pr))
        self.assertEqual(ids, ["ci:github:shop-api:101:1", "ci:github:shop-api:150:1", "ci:github:shop-api:205:2"])
        given = open_pr(head=R2)
        read(given, Recorded({(R2, 1): page([check(7, head=R2, run_attempt=3)])}))
        self.assertEqual(given["ci"]["heads"][0]["runs"][0]["attempt"], 3, "run_attempt when the provider states it")

    def test_replaying_the_same_response_is_idempotent(self):
        answer = {(R1, 1): page([check(1), check(2, "lint", "failure")])}
        pr = open_pr()
        read(pr, Recorded(answer), at="2026-11-08T10:00:00+00:00")
        first = json.loads(json.dumps(pr["ci"]))
        rows = evidence.ci_rows("shop-api", pr)
        read(pr, Recorded(answer), at="2026-11-08T11:00:00+00:00")
        self.assertEqual(pr["ci"]["sig"], first["sig"])
        self.assertEqual(len(pr["ci"]["heads"]), 1)
        self.assertEqual(pr["ci"]["heads"][0]["runs"], first["heads"][0]["runs"])
        self.assertEqual(evidence.ci_rows("shop-api", pr), rows, "same rows and version material")

    def test_head_changes_keep_history_that_never_counts_for_the_current_head(self):
        pr = open_pr(head=R1)
        read(pr, Recorded({(R1, 1): page([check(1), check(2, "lint")])}))
        pr["head"] = R2
        state = hosts.ci_state(pr)
        self.assertEqual(state["state"], "unavailable", "R1 passes say nothing about R2")
        self.assertEqual(state["text"], "CI checks unavailable: not read for head %s yet" % R2[:7])
        self.assertEqual(state["history"], [{"head": R1, "state": "passed", "observed_at": "2026-11-08T10:00:00+00:00"}])
        read(pr, Recorded({(R2, 1): page([check(3, head=R2, conclusion="failure")])}))
        state = hosts.ci_state(pr)
        self.assertEqual((state["state"], state["head"], state["counts"]), ("failing", R2, {"failed": 1}))
        self.assertEqual([h["head"] for h in pr["ci"]["heads"]], [R2, R1])
        for h, rid in ((R3, 4), (R4, 5)):
            pr["head"] = h
            read(pr, Recorded({(h, 1): page([check(rid, head=h)])}))
        self.assertEqual([h["head"] for h in pr["ci"]["heads"]], [R4, R3, R2], "at most CI_HEADS heads kept")

    def test_capability_states(self):
        text = "CI checks unavailable: not implemented for this host"
        for src in ("glab", "az"):
            self.assertEqual(hosts.ci_capability(src, R1, True)["text"], text)
            self.assertEqual(hosts.ci_state(open_pr(src=src))["text"], text)
        self.assertEqual(hosts.ci_capability("gh", R1, True)["state"], "available")
        for args, why in ((("gh", None, True), "head commit is not known"), (("gh", "HEAD", True), "head commit"),
                          (("gh", R1, None), "no confirmed GitHub account"), (("gh", R1, False), hosts.NOT_YOURS),
                          (("mcp:github", R1, True), "MCP server"), (("merge-commit", R1, True), "merge commits"),
                          (("", R1, True), "no PR source")):
            cap = hosts.ci_capability(*args)
            self.assertEqual(cap["state"], "unavailable", args)
            self.assertIn(why, cap["text"])
            self.assertTrue(cap["text"].startswith("CI checks unavailable: "))
        self.assertEqual(hosts.ci_state(open_pr())["text"], "CI checks unavailable: not read yet")
        self.assertEqual(hosts.ci_state(open_pr(state="merged"))["state"], "unavailable")
        self.assertEqual(set(hosts.CI_MATRIX), {"github", "gitlab", "azure", "bitbucket", "other"})

    def test_bad_inputs_and_guard_refusals_are_reasons_not_requests(self):
        rec = Recorded()
        for info, head in (({"path": "acme/shop api"}, R1), ({"path": "acme/shop-api;x"}, R1), (INFO, "HEAD"),
                           (INFO, R1[:12]), ({}, R1)):
            got = hosts.gh_check_runs({}, info, head, run=rec)
            self.assertEqual(got["coverage"], "unavailable")
        self.assertEqual(rec.calls, [], "nothing is asked with an unchecked repository or head")

        def refuse(argv, **kw):
            raise Blocked("commands", "api: only GET")
        got = hosts.gh_check_runs({}, INFO, R1, run=refuse)
        self.assertEqual((got["coverage"], got["reason"]), ("unavailable", "refused by the command guard"))
        got = hosts.gh_check_runs({}, INFO, R1, run=lambda argv, **kw: (0, "<html>", ""))
        self.assertEqual(got["reason"], "unreadable response")


# --------------------------------------------------------------------------- the tick's PR-read step

class TickStep(Tripwires, Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "profile": {"accounts": [ME]}})
        self.trip()
        self.data = SimpleNamespace(
            meta={"host": "github", "remote_path": "acme/shop-api"}, prs={}, touch=lambda what: None,
            branches={"feature/ABC-1001": {"on_remote": True, "remote_tip": R2, "ticket": "ABC-1001"},
                      "feature/ABC-1002": {"on_remote": True, "remote_tip": R3, "ticket": "ABC-1002"}})
        self.repo = SimpleNamespace(data=self.data, conf={}, all_base_refs=lambda: {}, path="synthetic",
                                    sha=lambda ref: "", name="shop-api")

    def gh_list(self, *rows):
        return 0, json.dumps(list(rows)), ""

    def pr_row(self, number, branch, head, login, node, state="OPEN", name="Dev A"):
        return {"number": number, "title": "ABC work", "state": state, "baseRefName": "main", "headRefName": branch,
                "headRefOid": head, "mergedAt": None, "mergeCommit": None, "isDraft": False, "reviewDecision": "",
                "url": "https://github.com/acme/shop-api/pull/%d" % number, "createdAt": "2026-11-07T09:00:00Z",
                "author": {"id": node, "login": login, "name": name, "is_bot": False}}

    def refresh(self, rec, now=None, src="gh"):
        tr = gittrack.Tracker(common.load_config(), {}, store.Store(), now=now or common.now(), pr_run=rec)
        tr.active_tickets = lambda days=None: {"ABC-1001", "ABC-1002"}
        with mock.patch.object(hosts, "source_for", return_value=src):
            tr.refresh_prs(self.repo)
        return tr

    def test_reads_checks_only_for_your_open_prs_never_a_similar_name(self):
        rec = Recorded({(R2, 1): page([check(1, head=R2)])}, prs=self.gh_list(
            self.pr_row(7, "feature/ABC-1001", R2, "dev-a", ME["node_id"]),
            self.pr_row(8, "feature/ABC-1002", R3, "dev-a1", "U_kgDOother"),          # same display name, another person
            self.pr_row(6, "feature/ABC-1001", R1, "dev-a", ME["node_id"], state="MERGED")))
        self.refresh(rec)
        self.assertEqual([c[-1].split("/")[4] for c in rec.api()], [R2], "one read: your open PR's head")
        for argv in rec.calls:
            self.assertIsNone(commands.reason(argv, "host"), argv)
        prs = self.data.prs
        self.assertEqual(hosts.ci_state(prs["7"])["state"], "passed")
        self.assertEqual((prs["8"]["ci"]["capability"]["reason"], prs["8"]["ci"]["heads"]), (hosts.NOT_YOURS, []))
        self.assertNotIn("ci", prs["6"], "closed and merged PRs are not read")
        self.assertEqual(set(self.data.meta["pr_checked"]), {"feature/ABC-1001", "feature/ABC-1002"})

    def test_ci_reads_follow_the_pr_read_interval_only(self):
        rows = self.gh_list(self.pr_row(7, "feature/ABC-1001", R2, "dev-a", ME["node_id"]))
        rec = Recorded({(R2, 1): page([check(1, head=R2)])}, prs=rows)
        self.refresh(rec)
        self.refresh(rec)
        self.assertEqual((len(rec.calls), len(rec.api())), (2, 1), "no PR read due: no CI read either")
        self.refresh(rec, now=common.now() + dt.timedelta(hours=2))
        self.assertEqual((len(rec.calls), len(rec.api())), (4, 2), "the hourly PR read brings one CI read")

    def test_a_failed_pr_read_makes_no_ci_read_and_ci_failure_never_breaks_the_pr_read(self):
        rec = Recorded({(R2, 1): page([check(1, head=R2)])}, prs=failed("gh: HTTP 502 (HTTP 502)"))
        self.refresh(rec)
        self.assertEqual(rec.api(), [])
        self.assertIn("pr_error", self.data.meta)
        rows = self.gh_list(self.pr_row(7, "feature/ABC-1001", R2, "dev-a", ME["node_id"]))
        self.refresh(Recorded({(R2, 1): RATE}, prs=rows))
        self.assertEqual(self.data.prs["7"]["ci"]["heads"][0]["coverage"], "unavailable")
        self.assertNotIn("pr_error", self.data.meta)
        for broken in ("gh_check_runs", "ci_refresh"):  # one PR's read, then the whole CI step
            self.data.prs.clear()
            self.data.meta.pop("pr_checked")
            with mock.patch.object(hosts, broken, side_effect=RuntimeError("synthetic")):
                self.refresh(Recorded(prs=rows))
            self.assertEqual(self.data.prs["7"]["state"], "open", "the PR itself is still recorded")
            self.assertIn("feature/ABC-1001", self.data.meta["pr_checked"])
            self.assertNotIn("pr_error", self.data.meta)

    def test_gitlab_and_azure_make_no_ci_request(self):
        for src, rows in (("glab", [{"iid": 7, "source_branch": "feature/ABC-1001", "state": "opened", "sha": R2,
                                     "author": {"id": 1, "username": "dev-a"}}]),
                          ("az", [{"pullRequestId": 7, "sourceRefName": "refs/heads/feature/ABC-1001",
                                   "status": "active", "lastMergeSourceCommit": {"commitId": R2},
                                   "createdBy": {"id": "g-1", "uniqueName": "dev-a"}}])):
            self.data.prs.clear()
            self.data.meta.pop("pr_checked", None)
            self.data.meta.update(org="acme", project="shop", repo="shop-api")
            rec = Recorded(prs=(0, json.dumps(rows), ""))
            self.refresh(rec, src=src)
            self.assertEqual((len(rec.calls), rec.api()), (1, []), src)
            self.assertEqual(hosts.ci_state(self.data.prs["7"])["text"],
                             "CI checks unavailable: not implemented for this host")


# --------------------------------------------------------------------------- the ticket projection

class TicketProjection(Tripwires, Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"]})
        self.trip()

    def repo(self, pr):
        data = SimpleNamespace(branches={"feature/ABC-1001": {"branch": "feature/ABC-1001", "ticket": "ABC-1001",
                                                              "own": [], "on_remote": True}},
                               prs={"7": pr}, meta={})
        return SimpleNamespace(name="shop-api", data=data, conf={"bases": {}}, all_base_refs=lambda: {},
                               sha=lambda ref: "")

    def legacy_sig(self, r, cfg):
        """delivery.sig_of as it was before CI checks: a PR without `ci` must keep exactly this signature."""
        names = {b["branch"] for b in r.data.branches.values() if b.get("ticket") == "ABC-1001"}
        branches = sorted((b["branch"], b.get("tip"), b.get("remote_tip"), b.get("parent"), b.get("abandoned"),
                           b.get("unpushed"), b.get("deleted_at"), b.get("remote_gone"))
                          for b in r.data.branches.values() if b.get("ticket") == "ABC-1001")
        prs = sorted((p["number"], p.get("state"), p.get("review"), p.get("head"), p.get("merge_commit"))
                     for p in r.data.prs.values() if p.get("source") in names)
        blob = json.dumps([branches, prs, [], None, None, None, gittrack.settings(cfg), r.conf, gittrack.ALGO],
                          sort_keys=True, default=str)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]

    def test_ticket_record_carries_head_and_checks_and_the_signature_moves_only_with_them(self):
        cfg = common.load_config()
        pr = open_pr(head=R2)
        r = self.repo(pr)
        self.assertEqual(delivery.sig_of(r, "ABC-1001", {}, cfg), self.legacy_sig(r, cfg), "no churn without CI")
        read(pr, Recorded({(R2, 1): page([check(1, head=R2)])}), at="2026-11-08T10:00:00+00:00")
        with_ci = delivery.sig_of(r, "ABC-1001", {}, cfg)
        self.assertNotEqual(with_ci, self.legacy_sig(r, cfg))
        read(pr, Recorded({(R2, 1): page([check(1, head=R2)])}), at="2026-11-08T11:00:00+00:00")
        self.assertEqual(delivery.sig_of(r, "ABC-1001", {}, cfg), with_ci, "a replayed read changes nothing")
        read(pr, Recorded({(R2, 1): page([check(1, head=R2, conclusion="failure")])}))
        self.assertNotEqual(delivery.sig_of(r, "ABC-1001", {}, cfg), with_ci)
        with mock.patch.object(delivery, "strategy_for", return_value=(None, None)):
            out = delivery.compute(r, "ABC-1001", {}, cfg)
        self.assertEqual((out["prs"][0]["head"], out["ci"]), (R2, {"7": pr["ci"]}))
        self.assertNotIn("ci", out["prs"][0], "PR copies (release roles, exports) stay small")
        self.assertEqual(hosts.ci_state(evidence.with_ci(out, out["prs"][0]))["state"], "failing")
        del r.data.prs["7"]["ci"]
        with mock.patch.object(delivery, "strategy_for", return_value=(None, None)):
            self.assertNotIn("ci", delivery.compute(r, "ABC-1001", {}, cfg), "no key without checks")


# --------------------------------------------------------------------------- Companion evidence (A13, A14, VERIFY-01)

class CompanionEvidence(Base):
    def setUp(self):
        super().setUp()
        exe = mock.patch.object(hosts, "cli_path", lambda cfg, name: name)
        exe.start()
        self.addCleanup(exe.stop)
        # the clone's host identity, as the activity scan records it: the CI checkout is of this repository
        led = activity.new_ledger({"id": "github:github.com/acme/shop-api", "path": self.repo.replace("\\", "/")})
        led["profile_sig"] = profile.signature(common.load_config())
        common.write_json(activity.ledger_path(activity.ledger_key(self.repo)), led)
        self.pr_fact = open_pr(head=R1)

    def checks(self, *runs, other=None):
        """One recorded read of the PR's current head (none without runs), projected onto the ticket as
        delivery.compute writes it."""
        if runs:
            read(self.pr_fact, Recorded({(self.pr_fact["head"], 1): page(list(runs))}), at=self.stamp())

        def projected(pr):
            return {"branches": [], "prs": [{k: v for k, v in pr.items() if k != "ci"}],
                    "ci": {str(pr["number"]): json.loads(json.dumps(pr["ci"]))} if pr.get("ci") else {}}
        tk = self.tk()
        repos = {"scratch": projected(self.pr_fact)}
        if other:
            repos["other"] = projected(other)
        tk["git"] = {"repos": repos, "checked_at": self.stamp()}
        common.write_json(store.ticket_path("ABC-1001"), tk)

    def ci_ids(self, rows):
        return sorted(s for s in rows if s.startswith("ci:"))

    def test_ci_rows_reach_the_delta_with_provenance_and_are_never_acknowledged_or_mapped(self):
        rec = self.started(n=2)
        foreign = open_pr(head=R1, number=9)
        read(foreign, Recorded({(R1, 1): page([check(901)])}))
        self.checks(check(101), check(102, "lint", "failure"), other=foreign)
        self.assertEqual(self.tick(), 1)
        rec = workflowstore.load(rec["id"])
        items = {i["id"]: i for i in companion.resume(rec["id"])["delta"]["items"]}
        self.assertEqual(self.ci_ids(items), ["ci:github:scratch:101:1", "ci:github:scratch:102:1"],
                         "another repository's PR brings no rows")
        row = items["ci:github:scratch:102:1"]
        self.assertEqual((row["kind"], row["outcome"], row["provenance"], row["change"], row["state"]),
                         ("ci", "failed", "git:scratch:pr:7:ci:102", "new", "available"))
        self.assertIn("ci:github:scratch:101:1", rec["ingested"])
        self.assertEqual(self.ci_ids(rec["acknowledged"]), [], "ingestion never acknowledges")
        self.assertEqual(rec["evidence"], [], "no run is mapped to a criterion automatically")
        self.assertEqual(self.tick(), 0, "replay without a new source event")
        self.checks(check(101), check(102, "lint", "failure"))  # the same response read again
        self.assertEqual(self.tick(), 0, "replayed read: stable IDs, same versions")
        self.checks(check(101), check(102, "lint", "failure"), check(202, "lint"))  # a re-run: attempt 2
        self.assertEqual(self.tick(), 1)
        items = {i["id"]: i for i in companion.resume(rec["id"])["delta"]["items"]}
        self.assertEqual(items["ci:github:scratch:202:2"]["change"], "new")
        self.assertEqual(items["ci:github:scratch:102:1"]["outcome"], "failed", "attempt 1 stays distinct")
        rec = workflowstore.load(rec["id"])
        rec = companion.verify(rec["id"], rec["revision"])
        self.assertEqual(self.ledger(rec), {"ac1": "Unverified", "ac2": "Unverified"},
                         "a green suite verifies nothing without the developer's mapping")
        rec = companion.acknowledge(rec["id"], rec["revision"], ["ci:github:scratch:101:1"])
        self.assertNotIn("ci:github:scratch:101:1", companion.resume(rec["id"])["unread"])
        self.assertIn("ci:github:scratch:202:2", companion.resume(rec["id"])["unread"])

    def test_a14_a_success_on_r1_never_clears_a_failing_or_missing_check_on_r2(self):
        rec = self.started(n=2)
        rec = self.observe(rec, R1)
        self.checks(check(101, "unit"), check(102, "e2e"))
        self.tick()
        rec = workflowstore.load(rec["id"])
        rec = self.map(rec, evidence="ci:github:scratch:101:1", criteria=["ac1"], coverage="complete")
        rec = self.map(rec, evidence="ci:github:scratch:102:1", criteria=["ac2"], coverage="complete")
        pinned = {e["id"]: e for e in rec["evidence"]}["ci:github:scratch:101:1"]
        self.assertEqual((pinned["head"], pinned["attempt"], pinned["checkout"], pinned["mapped_by"]),
                         (R1, 1, evidence.CI_CHECKOUT, "developer"))
        # the PR head moves to R2; its checks are not read yet
        self.pr_fact["head"] = R2
        self.checks()
        self.tick()
        rec = workflowstore.load(rec["id"])
        rec = self.observe(rec, R2)
        cand = companion.resume(rec["id"])
        tk = self.tk()
        summary = evidence.ci_summary(tk, rec["binding"])
        self.assertEqual([(s["pr"], s["state"], s["head"]) for s in summary], [("pr:scratch:7", "unavailable", R2)])
        self.assertEqual(summary[0]["history"][0]["state"], "passed", "R1's success is history only")
        self.assertEqual(cand["prs"][0]["head"], R2)
        self.assertEqual(cand["continuation"]["head_gaps"], [], "the PR is on the observed head")
        self.assertEqual(cand["measures"]["functional_verification"]["gaps"], ["ac1", "ac2"])
        # R2 is read: unit fails, e2e is not reported at all
        self.checks(check(301, "unit", "failure", head=R2))
        self.tick()
        summary = evidence.ci_summary(self.tk(), rec["binding"])[0]
        self.assertEqual((summary["state"], summary["counts"]), ("failing", {"failed": 1}))
        rec = workflowstore.load(rec["id"])
        rec = companion.verify(rec["id"], rec["revision"])
        self.assertEqual(self.ledger(rec), {"ac1": "Unverified", "ac2": "Unverified"})
        rows = {r["criterion"]: r for r in rec["verifications"][-1]["rows"]}
        self.assertEqual(rows["ac1"]["evidence"][0]["applicability"], "historical")
        self.assertIn("head", rows["ac1"]["invalidation"])
        self.assertEqual(rows["ac2"]["evidence"][0]["revision"], R1)
        cand = companion.resume(rec["id"])
        self.assertEqual(cand["measures"]["functional_verification"]["percent"], 0.0)
        self.assertEqual(cand["measures"]["functional_verification"]["gaps"], ["ac1", "ac2"])
        # a re-run passes on R2: GitHub states no config / environment / location, so it stays applicability unknown
        self.checks(check(301, "unit", "failure", head=R2), check(302, "unit", head=R2))
        self.tick()
        state = evidence.ci_summary(self.tk(), rec["binding"])[0]
        self.assertEqual((state["state"], state["counts"], state["absent"]), ("missing", {"passed": 1}, ["e2e"]),
                         "the re-run passed, but R1's e2e success never stands in for e2e missing on R2")
        self.assertIn("missing on head %s" % R2[:7], state["text"])
        rec = workflowstore.load(rec["id"])
        rec = self.map(rec, evidence="ci:github:scratch:302:2", criteria=["ac1"], coverage="complete")
        rec = companion.verify(rec["id"], rec["revision"])
        row = {r["criterion"]: r for r in rec["verifications"][-1]["rows"]}["ac1"]
        self.assertEqual(row["status"], "Unverified")
        cited = {c["id"]: c for c in row["evidence"]}["ci:github:scratch:302:2"]
        self.assertEqual(cited["applicability"], "unknown")
        self.assertEqual(cited["reason"], "not captured: location, config, environment")

    def test_a_complete_read_with_no_checks_on_the_current_head_is_missing_not_passing(self):
        rec = self.started()
        self.checks(check(101))
        self.pr_fact["head"] = R2
        self.checks()
        self.assertEqual(evidence.ci_summary(self.tk(), rec["binding"])[0]["state"], "unavailable")
        read(self.pr_fact, Recorded({(R2, 1): page([])}))  # a complete read that reports no checks at all
        self.checks()
        state = evidence.ci_summary(self.tk(), rec["binding"])[0]
        self.assertEqual((state["state"], state["history"][0]["state"]), ("missing", "passed"))


if __name__ == "__main__":
    unittest.main()
