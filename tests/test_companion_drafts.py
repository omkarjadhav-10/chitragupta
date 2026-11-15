"""FLOW-04 audience drafts, FLOW-05 decisions, FLOW-06 impact suggestions, FLOW-07 attention presentation and the
Companion's day boundaries (A16, A17, A18, A19, A22).

Synthetic data only (ABC-…, acme.test), frozen clocks, isolated data homes. Every process start, model call, network
connection and MCP job is a tripwire: drafts are deterministic templates and nothing is ever sent.
"""
import datetime as dt
import io
import json
import os
import socket
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest import mock

from tests.support import write_config
from tests.test_companion_state import plan_body
from tests.test_companion_views import ViewBase, tree
from chitragupta import (brief, common, companion, companionattention as attention, companiondrafts as drafts,
                         companionimpact as impact, companionviews as views, extract, profile, progress, store,
                         supersede, workflowstore)
from chitragupta.dashboard import companion_api

UTC = dt.timezone.utc
CLAIMS = ("all tests pass", "deployed", "accepted")


class FallZone(dt.tzinfo):
    """A synthetic zone (no tz data needed): UTC-4, then UTC-5 from 2026-11-01 06:00 UTC (02:00 -> 01:00 local)."""
    SWITCH = dt.datetime(2026, 11, 1, 6, 0, tzinfo=UTC)

    def utcoffset(self, d):
        if d is None:
            return dt.timedelta(hours=-5)
        return dt.timedelta(hours=-4) if d.replace(tzinfo=None) < dt.datetime(2026, 11, 1, 1, 0) \
            else dt.timedelta(hours=-5)

    def dst(self, d):
        return self.utcoffset(d) - dt.timedelta(hours=-5)

    def tzname(self, d):
        return "SYN"

    def fromutc(self, d):
        off = dt.timedelta(hours=-4) if d.replace(tzinfo=UTC) < self.SWITCH else dt.timedelta(hours=-5)
        return (d + off).replace(tzinfo=self)


class DraftBase(ViewBase):
    def setUp(self):
        super().setUp()
        trips = ExitStack()
        self.addCleanup(trips.close)
        self.sent = []

        def no(name):
            def fail(*a, **k):
                self.sent.append(name)
                raise AssertionError("drafts performed " + name)
            return fail
        trips.enter_context(mock.patch.object(extract, "call_claude", no("model call")))
        trips.enter_context(mock.patch.object(socket.socket, "connect", no("network connection")))
        trips.enter_context(mock.patch.object(socket, "create_connection", no("network connection")))
        self.addCleanup(lambda: self.assertEqual(self.sent, [], "no model call or connection"))

    def drafts(self, rec, **args):
        return self.read("drafts", workflow=rec["id"], **args)

    def audience(self, rec, aud, **args):
        return next(a for a in self.drafts(rec, **args)["audiences"] if a["id"] == aud)

    def act(self, rec, name, **body):
        return companion.action(dict(body, action=name, workflow=rec["id"], revision=rec["revision"]))

    def started_with_evidence(self):
        rec = self.started(n=2)
        self.put(commits={"c1": {"sha": "c1", "subject": "Add the order filter", "date": self.stamp(),
                                 "repo_name": "scratch"}},
                 files={"src/orders/filter.py": {"path": "src/orders/filter.py", "changed": True, "last": "2026-11-08"}})
        self.captured_test(rec, "t1", "passed")
        return companion.get(rec["id"])

    def item(self, iid, kind="decision", title="Decision", date="2026-11-06", **extra):
        tk = self.tk()
        tk.setdefault("items", {})[iid] = dict({"id": iid, "kind": kind, "title": title, "detail": "", "date": date,
                                                "time": "10:00", "confidence": "likely"}, **extra)
        common.write_json(store.ticket_path("ABC-1001"), tk)


# ---------------------------------------------------------------- A18 / FLOW-04 drafts

class A18Drafts(DraftBase):
    def test_three_templates_partial_evidence_missing_information_and_no_false_claims(self):
        rec = self.started_with_evidence()
        self.captured_test(rec, "t2", "failed")
        out = self.drafts(rec)
        self.assertEqual([a["id"] for a in out["audiences"]], ["pr", "qa", "session"])
        self.assertEqual([a["label"] for a in out["audiences"]], ["PR reviewer", "QA tester", "Next developer session"])
        for a in out["audiences"]:
            with self.subTest(audience=a["id"]):
                self.assertIsNone(a["current"])
                text = a["preview"]["text"]
                for section in ("## Purpose", "## Verified changes", "## Evidence", "## Unresolved risks and questions",
                                "## Next action", "## Missing information"):
                    self.assertIn(section, text)
                low = text.lower()
                for claim in CLAIMS:
                    self.assertNotIn(claim, low, "no %r without current evidence" % claim)
                self.assertIn("CI checks unavailable.", a["preview"]["missing"])
                self.assertIn("No current applicable verifying evidence for: ac1, ac2.", a["preview"]["missing"])
                self.assertIn("Whether UAT is required is not recorded.", a["preview"]["missing"])
                self.assertIn("ABC-1001", text)
                self.assertNotIn(self.repo, text, "no local absolute path")
                self.assertLessEqual(len(text.encode("utf-8")), drafts.TEXT_BYTES)
        qa = out["audiences"][1]["preview"]
        self.assertIn("## Expected outcomes", qa["text"])
        self.assertIn("No reproducible steps recorded (no saved requests).", qa["missing"])
        self.assertIn("## Where it stands", out["audiences"][2]["preview"]["text"])
        self.assertIn("## Decisions", out["audiences"][0]["preview"]["text"])
        # QA adds environments, reproducible steps and expected outcomes only when they are recorded
        self.put(replay={"final": [{"n": 1, "name": "List open orders", "method": "GET", "url":
                                    "{{ordersBaseUrl}}/orders?status=open", "expected": "200 with open orders only",
                                    "file": "01-list-orders.bru"}]})
        rec = self.observe(rec, environment="qa")
        qa = self.audience(rec, "qa")["preview"]
        self.assertIn("## Reproducible steps", qa["text"])
        self.assertIn("1. List open orders: GET /orders?status=open", qa["text"])
        self.assertIn("- Step 1: 200 with open orders only", qa["text"])
        self.assertIn("## Configured environments", qa["text"])
        self.assertIn("Developer-recorded environment (self-reported): qa", qa["text"])
        self.assertNotIn("No reproducible steps recorded (no saved requests).", qa["missing"])
        self.assertIn("Configured environments", qa["self_reported"], "self-reported sections are labelled")

    def test_deployment_and_acceptance_are_claimed_only_with_current_evidence(self):
        rec = self.started_with_evidence()
        uat = {"requirement": {"state": "required"}, "revision": {"rev": "r1", "at": self.stamp()},
               "deployed": {"state": "unknown", "source": "no evidence"}, "accepted": {"state": "unknown"}}
        self.put(release={"uat": uat, "reminders": [{"id": "uat-deployed", "kind": "uat-deployed", "repo": "",
                                                     "severity": "warn", "text": "UAT unverified"}]})
        text = self.audience(rec, "session")["preview"]["text"]
        self.assertIn("UAT deployment of the current revision is unverified.", text)
        self.assertNotIn("deployed", text.lower())
        uat["deployed"] = {"state": "yes", "rev": "r1", "source": "Jira status UAT"}
        self.put(release={"uat": uat, "reminders": []})
        text = self.audience(rec, "session")["preview"]["text"]
        self.assertIn("Deployed to UAT for the current revision (evidence: Jira status UAT).", text)
        self.assertNotIn("accepted", text.lower())
        uat["deployed"]["rev"] = "r0"  # evidence about an earlier revision never counts for the current one
        self.put(release={"uat": uat, "reminders": []})
        self.assertNotIn("deployed", self.audience(rec, "session")["preview"]["text"].lower())

    def test_generate_edit_regenerate_keeps_the_edit_and_marks_changed_sources(self):
        rec = self.started_with_evidence()
        rec = self.act(rec, "draft_generate", audience="pr")
        pr = self.audience(rec, "pr")
        v1 = pr["current"]
        self.assertEqual((v1["number"], v1["origin"], v1["stale"]), (1, "generated", False))
        same = self.act(rec, "draft_generate", audience="pr")
        self.assertEqual(same["revision"], rec["revision"], "an unchanged regeneration saves nothing")
        self.assertIn("already matches current evidence", same["notice"])
        mine = v1["text"].replace("## Next action", "## Reviewer focus (mine)\n- Check the rounding change\n\n"
                                                    "## Next action")
        rec = self.act(rec, "draft_save", audience="pr", text=mine, base=v1["id"])
        v2 = self.audience(rec, "pr")["current"]
        self.assertEqual((v2["number"], v2["origin"], v2["parent"], v2["text"]), (2, "manual", v1["id"], mine))
        # sources change: the text stays, the warning appears; nothing refreshes underneath
        self.put(commits={"c1": {"sha": "c1", "subject": "Add the order filter", "date": self.stamp(),
                                 "repo_name": "scratch"},
                          "c2": {"sha": "c2", "subject": "Round refunds", "date": self.stamp(minutes=5),
                                 "repo_name": "scratch"}})
        pr = self.audience(rec, "pr")
        self.assertTrue(pr["current"]["stale"])
        self.assertEqual(pr["current"]["stale_label"], "Source evidence changed")
        self.assertEqual(pr["current"]["text"], mine)
        rec = self.act(rec, "draft_generate", audience="pr")
        self.assertIn("Your edit stays current", rec["notice"])
        pr = self.audience(rec, "pr")
        self.assertEqual(pr["current"]["id"], v2["id"], "regeneration never replaces your edit")
        self.assertEqual(pr["current"]["text"], mine)
        self.assertEqual(pr["regenerated"]["number"], 3)
        self.assertIn("-## Reviewer focus (mine)", pr["regenerated"]["diff"]["text"])
        self.assertEqual([v["number"] for v in pr["versions"]], [3, 2, 1], "every version kept")
        again = self.act(rec, "draft_generate", audience="pr")  # a second click on unchanged sources piles nothing up
        self.assertEqual(again["revision"], rec["revision"])
        self.assertIn("Generated version 3 already matches current evidence; your edit stays current", again["notice"])
        saved = workflowstore.load(rec["id"])["audience_drafts"]["pr"]["versions"]
        self.assertEqual(saved[1]["text"], mine, "the edit is byte-identical in the record")
        # adopting the regenerated text is an explicit save from it
        regen = companion_api.read("draft_version", rec["id"], pr["regenerated"]["id"])["version"]
        self.assertEqual(regen["origin_label"], "Generated")
        rec = self.act(rec, "draft_save", audience="pr", text=regen["text"], base=regen["id"])
        self.assertEqual(self.audience(rec, "pr")["current"]["number"], 4)
        self.assertFalse(self.audience(rec, "pr")["current"]["stale"])

    def test_expected_revision_conflict_keeps_the_draft(self):
        rec = self.act(self.started_with_evidence(), "draft_generate", audience="qa")
        old = dict(rec, revision=rec["revision"] - 1)
        with self.assertRaises(workflowstore.Conflict) as caught:
            self.act(old, "draft_save", audience="qa", text="My QA notes")
        self.assertEqual(caught.exception.current["revision"], rec["revision"])
        status, body = companion_api.mutate({"action": "draft_save", "workflow": rec["id"], "revision": 0,
                                             "audience": "qa", "text": "My QA notes"})
        self.assertEqual((status, body["retain_draft"]), (409, True))
        self.assertEqual(len(workflowstore.load(rec["id"])["audience_drafts"]["qa"]["versions"]), 1)

    def test_copy_revalidates_access_and_passes_redaction(self):
        rec = self.act(self.started_with_evidence(), "draft_generate", audience="session")
        for bad in ("Ping dev@acme.test before release", "Logs are in %s" % os.path.join(self.repo, "out.log"),
                    "token ghp_" + "a" * 36, "bell \x07", "x" * 9000, "  "):
            with self.subTest(bad=bad[:20]):
                with self.assertRaises(ValueError):
                    self.act(rec, "draft_save", audience="session", text=bad)
        for bad in ({"audience": "manager"}, {"audience": "pr", "base": "d_../x", "text": "ok"},
                    {"audience": "pr", "flow": "f_; DROP"}):
            with self.subTest(body=str(bad)[:30]):
                with self.assertRaises(ValueError):
                    self.act(rec, "draft_generate" if "text" not in bad else "draft_save", **bad)
        self.assertEqual(drafts.export("Mail dev@acme.test, see C:\\work\\shop\\a.py\nnext line"),
                         "Mail [email], see [path]\nnext line")
        vid = self.audience(rec, "session")["current"]["id"]
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["repos"] = [self.repo_b]  # access revoked between showing and copying
        write_config(common.home(), cfg)
        with self.assertRaises(PermissionError):
            self.drafts(rec)
        with self.assertRaises(PermissionError):
            companion_api.read("draft_version", rec["id"], vid)
        status, _ = companion_api.mutate({"action": "draft_generate", "workflow": rec["id"],
                                          "revision": rec["revision"], "audience": "pr"})
        self.assertEqual(status, 403)

    def test_database_context_summary_without_rows_or_keys(self):
        rec = self.started_with_evidence()
        ctx = {"available": True, "flow_id": "f_0123456789ab", "operation_label": "Refund an order",
               "environment": "QA", "last_evidenced_step": "shop.refunds: observed write",
               "largest_gap": "No runtime events after the payment call", "next_action": "Check the ledger table",
               "coverage": "4 of 6 planned step(s) answered", "freshness": "current",
               "updated_at": "2026-11-08T09:00:00+00:00",
               "observations": [{"step": "shop.refunds", "status": "observed", "status_label": "Observed",
                                 "observed_at": "2026-11-08T08:40:00+00:00"},
                                {"step": "shop.orders", "status": "observed", "status_label": "Observed",
                                 "observed_at": "2026-11-08T08:55:00+00:00"}]}
        with mock.patch.object(views, "database_context", lambda cfg, args: dict(ctx) if args.get("flow") else
                               {"available": False, "reason": "No database flow or investigation selected"}):
            rec = self.act(rec, "draft_generate", audience="qa", flow="f_0123456789ab")
            qa = self.audience(rec, "qa", flow="f_0123456789ab")["current"]
            self.assertFalse(qa["stale"])
            self.assertEqual(qa["database"], {"flow": "f_0123456789ab"})
            for want in ("Flow: Refund an order (environment QA", "Code-supported path: 4 of 6 planned step(s)",
                         "Observed records: last evidenced step shop.refunds: observed write",
                         "Observation window: 2026-11-08T08:40:00+00:00 to 2026-11-08T08:55:00+00:00",
                         "Unknown boundary: No runtime events after the payment call",
                         "Next check: Check the ledger table", "Database observations: QA"):
                self.assertIn(want, qa["text"])
            # the read without a selection still judges the version by the selection it was generated from
            self.assertFalse(self.audience(rec, "qa")["current"]["stale"])
            ctx["last_evidenced_step"] = "shop.ledger: observed write"
            self.assertTrue(self.audience(rec, "qa")["current"]["stale"])

    def test_drafts_read_is_bounded_and_side_effect_free(self):
        rec = self.started_with_evidence()
        big = "\n".join("- Check case %d of the refund rounding change" % i for i in range(160))[:7900]
        for aud in ("pr", "qa", "session"):
            rec = self.act(rec, "draft_generate", audience=aud)
            cur = self.audience(rec, aud)["current"]
            rec = self.act(rec, "draft_save", audience=aud, text=big, base=cur["id"])
        self.put(commits={"c9": {"sha": "c9", "subject": "Later work", "date": self.stamp(), "repo_name": "scratch"}})
        for aud in ("pr", "qa", "session"):
            rec = self.act(rec, "draft_generate", audience=aud)
        before = tree(self.home, self.user_dir)
        stack, calls = self.side_effect_free()
        with stack:
            for _ in range(10):
                out = self.drafts(rec)
        self.assertEqual(calls, [])
        self.assertEqual(before, tree(self.home, self.user_dir), "reading drafts writes nothing")
        self.assertLessEqual(len(json.dumps(out, ensure_ascii=False).encode("utf-8")), views.MAX_BYTES)
        self.assertTrue(all(a["regenerated"] and a["current"]["stale"] for a in out["audiences"]))

    def test_cli_prints_a_draft_read_only(self):
        from chitragupta import cli
        rec = self.started_with_evidence()
        before = tree(self.home)

        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.main(["companion", "draft"] + list(argv))
            return code, buf.getvalue()
        code, text = run(rec["id"], "--audience", "qa")
        self.assertEqual(code, 0)
        self.assertIn("QA tester draft · preview (not saved)", text)
        self.assertIn("## Missing information", text)
        self.assertIn("nothing was sent", text)
        code, text = run(rec["id"])
        self.assertEqual(code, 0)
        self.assertIn("--audience pr|qa|session", text)
        self.assertEqual(before, tree(self.home), "the CLI never saves a draft")
        rec = self.act(rec, "draft_generate", audience="pr")
        code, text = run(rec["id"], "--audience", "pr")
        self.assertIn("PR reviewer draft · version 1 (Generated)", text)


# ---------------------------------------------------------------- A16 / FLOW-05 decisions

class A16Decisions(DraftBase):
    def test_later_decision_supersedes_consistently_with_history_and_conflicts(self):
        rec = self.started_with_evidence()
        self.item("d1", title="Keep the filter in the browser session", subject="filter storage")
        d = self.read("detail", workflow=rec["id"])["knowledge"]
        self.assertEqual([r["id"] for r in d["relevant"]], ["d1"])
        rec = self.act(rec, "draft_generate", audience="session")
        self.assertIn("Keep the filter in the browser session", self.audience(rec, "session")["current"]["text"])
        # a confirmed later decision supersedes it through the existing supersession model
        self.item("d2", title="Store the filter per user in the database", date="2026-11-07", status="confirmed",
                  subject="filter storage")
        tk = self.tk()
        self.assertEqual(supersede.apply(tk, {"items": [{"id": "d1", "status": "superseded",
                                                         "superseded_by": "d2"}]}), 1)
        common.write_json(store.ticket_path("ABC-1001"), tk)
        d = self.read("detail", workflow=rec["id"])["knowledge"]
        self.assertEqual([r["id"] for r in d["relevant"]], ["d2"])
        self.assertEqual(d["relevant"][0]["status_label"], "Confirmed")
        self.assertEqual([s["id"] for s in d["relevant"][0]["supersedes"]], ["d1"])
        self.assertEqual([(h["id"], h["superseded_by"]) for h in d["history"]], [("d1", "d2")], "history kept")
        self.assertNotIn("d1", [r["id"] for r in d["current"]])
        session = self.audience(rec, "session")["current"]
        self.assertTrue(session["stale"], "new decision evidence marks the old draft")
        rec = self.act(rec, "draft_generate", audience="session")
        text = self.audience(rec, "session")["current"]["text"]
        self.assertIn('[Confirmed] Store the filter per user in the database (decision, 2026-11-07); supersedes: '
                      '"Keep the filter in the browser session"', text)
        self.assertIn("Earlier advice kept as history: Keep the filter in the browser session (superseded)", text)
        self.assertNotIn("[Recorded (captured; not confirmed)] Keep the filter", text)
        recall = [i["id"] for i in brief.current_items(self.tk())]  # the recall surfaces read the same items
        self.assertIn("d2", recall)
        self.assertNotIn("d1", recall)
        self.assertEqual(self.tk()["items"]["d1"]["title"], "Keep the filter in the browser session",
                         "the earlier item is never rewritten or deleted")
        # conflicting current evidence is shown for review, not merged
        self.item("d3", title="Keep the filter only in the URL", date="2026-11-08", subject="Filter  Storage")
        d = self.read("detail", workflow=rec["id"])["knowledge"]
        self.assertEqual(len(d["conflicts"]), 1)
        self.assertEqual(sorted(i["id"] for i in d["conflicts"][0]["items"]), ["d2", "d3"])
        rec = self.act(rec, "draft_generate", audience="pr")
        self.assertIn("Conflicting evidence: review before relying on either on filter storage",
                      self.audience(rec, "pr")["current"]["text"])

    def test_rejected_approach_in_one_repository_or_environment_does_not_block_another(self):
        here = self.started_with_evidence()
        here = self.observe(here, environment="local")
        tk = self.tk()
        tk["tests"]["t-cache"] = {"id": "t-cache", "at": self.stamp(), "status": "failed", "command": "pytest",
                                  "context": {"location": self.repo, "environment": "local"}}
        tk["tests"]["t-qa"] = {"id": "t-qa", "at": self.stamp(), "status": "failed", "command": "pytest",
                               "context": {"location": self.repo, "environment": "qa"}}
        common.write_json(store.ticket_path("ABC-1001"), tk)
        self.item("e1", kind="error", title="Caching the order list in memory failed", evidence=["test:t-cache"])
        self.item("e2", kind="error", title="Batch refunds timed out", evidence=["test:t-qa"])
        mine = self.read("detail", workflow=here["id"])["knowledge"]
        states = {r["id"]: r["applies"]["state"] for r in mine["rejected"]}
        self.assertEqual(states, {"e1": "here", "e2": "other_environment"})
        self.assertEqual([r["id"] for r in mine["rejected_here"]], ["e1"])
        self.assertIn("not a prohibition", next(r for r in mine["rejected"] if r["id"] == "e2")["applies"]["label"])
        other = self.create("ABC-1001", self.repo_b, n=2)  # a second attempt of the ticket in another repository
        theirs = self.read("detail", workflow=other["id"])["knowledge"]
        self.assertEqual({r["applies"]["state"] for r in theirs["rejected"]}, {"other_repository"})
        self.assertEqual(theirs["rejected_here"], [])
        self.assertIn("Recorded in another repository (scratch); context only, not a prohibition here",
                      theirs["rejected"][0]["applies"]["label"])
        planned = companion.save_plan(other["id"], other["revision"], plan_body())
        approved = self.approve(planned)  # the failed approach elsewhere blocks nothing here
        self.assertEqual(approved["stage"], "approved")
        rec = self.act(approved, "draft_generate", audience="session")
        self.assertIn("Rejected approach: Caching the order list in memory failed. Recorded in another repository",
                      self.audience(rec, "session")["current"]["text"])


# ---------------------------------------------------------------- A19 / FLOW-07 attention

class A19Attention(DraftBase):
    def reminders(self, rev="r1", text="UAT branch has the changes, but the UAT deployment of them is unverified."):
        self.put(release={"uat": {"requirement": {"state": "required"}, "revision": {"rev": rev, "at": self.stamp()}},
                          "reminders": [{"id": "uat-deployed", "kind": "uat-deployed", "repo": "", "severity": "warn",
                                         "text": text}]})

    def summary(self, **args):
        return self.read("snapshot", **args)["attention_summary"]

    def uat(self, summary):
        return next(i for i in summary["items"] if i["id"] == "release:ABC-1001:View-UAT-evidence")

    def act_on(self, name, item, **extra):
        return companion.action(dict(extra, action=name, item=item["id"], generation=item["generation"]))

    def test_acknowledge_hides_until_new_evidence_and_never_clears_delivery(self):
        rec = self.started()
        rec = companion.request_verification(rec["id"], rec["revision"], ["Run the order tests"])
        self.reminders()
        first = self.summary()
        item = self.uat(first)
        self.assertEqual((first["total"], first["recorded"], item["required"]), (1, 1, True))
        self.assertEqual(self.summary(), first, "repeated refresh never multiplies items")
        ticket_before = open(store.ticket_path("ABC-1001"), "rb").read()
        head_before = workflowstore.load(rec["id"])["revision"]
        out = self.act_on("attention_ack", item)
        self.assertEqual(out["attention"]["state"], "acknowledged")
        self.act_on("attention_ack", item)  # a second session acknowledging the same item
        saved = common.read_json(attention.path())
        pid = self.read("snapshot")["profile_id"]
        self.assertEqual(list(saved["profiles"][pid]["items"]), [item["id"]], "one entry, never multiplied")
        s = self.summary()
        self.assertEqual((s["total"], s["recorded"], s["hidden"]["acknowledged"], s["hidden"]["listed"]), (0, 1, 1, 1))
        self.assertEqual(self.uat(s)["presentation"]["state"], "acknowledged")
        self.assertTrue(self.uat(s)["text"].startswith("Acknowledged; returns when its evidence changes."))
        # presentation only: the ticket record, the workflow and the completion gate are untouched
        self.assertEqual(open(store.ticket_path("ABC-1001"), "rb").read(), ticket_before)
        self.assertEqual(workflowstore.load(rec["id"])["revision"], head_before)
        self.tick()
        rec = companion.get(rec["id"])
        with self.assertRaisesRegex(ValueError, "release: UAT branch has the changes"):
            companion.complete(rec["id"], rec["revision"], "Done", pr_not_required="Config-only change")
        # a new commit reopening UAT is a new state even after the acknowledgement
        self.reminders(rev="r2", text="UAT branch has the latest changes, but the UAT deployment of them is unverified.")
        s = self.summary()
        again = self.uat(s)
        self.assertEqual((s["total"], again["presentation"]["state"], again["presentation"]["changed"]),
                         (1, "active", True))
        self.assertNotEqual(again["generation"], item["generation"])
        with self.assertRaisesRegex(ValueError, "changed since you loaded it"):
            self.act_on("attention_ack", item)  # the old generation
        with self.assertRaisesRegex(ValueError, "no longer recorded"):
            companion.action({"action": "attention_ack", "item": "release:ABC-1002:View-UAT-evidence",
                              "generation": again["generation"]})
        with self.assertRaisesRegex(ValueError, "invalid attention item"):
            companion.action({"action": "attention_ack", "item": "../x", "generation": "0" * 16})

    def test_snooze_is_bounded_keeps_required_items_listed_and_expires(self):
        self.started()
        self.reminders()
        self.put(git={"repos": {}, "alerts": [{"kind": "behind", "text": "feature/ABC-1001 is behind main",
                                               "repo": "scratch"}]})
        s = self.summary()
        self.assertEqual([i["kind"] for i in s["items"]], ["release", "branch"], "required items first")
        self.assertEqual(s["initial"], 3)
        self.assertEqual([x["id"] for x in s["snooze"]], ["1h", "1d", "1w"])
        item, alert = self.uat(s), s["items"][1]
        with self.assertRaisesRegex(ValueError, "1h, 1d or 1w"):
            self.act_on("attention_snooze", item, duration="2h")
        out = self.act_on("attention_snooze", item, duration="1h")
        self.assertEqual(out["attention"]["until"], self.stamp(hours=1))
        self.act_on("attention_snooze", alert, duration="1d")
        s = self.summary()
        self.assertEqual((s["total"], s["hidden"]["snoozed"], s["hidden"]["listed"]), (0, 2, 1))
        self.assertEqual([i["id"] for i in s["items"]], [item["id"]], "a snoozed required item stays in the full list")
        self.assertTrue(self.uat(s)["text"].startswith("Snoozed until " + self.stamp(hours=1)))
        self.advance(minutes=61)
        s = self.summary()
        self.assertEqual(self.uat(s)["presentation"]["state"], "active", "returns when the snooze expires")
        self.assertEqual(s["hidden"]["snoozed"], 1)
        self.act_on("attention_snooze", self.uat(s), duration="1w")
        self.reminders(text="UAT branch has the latest changes, but the UAT deployment of them is unverified.")
        self.assertEqual(self.uat(self.summary())["presentation"]["state"], "active",
                         "materially new evidence reopens a snoozed item")
        before = tree(common.hp("data"))
        out = self.act_on("attention_unsnooze", alert)
        self.assertEqual((out["attention"]["state"], out["notice"]), ("active", "Shown again."))
        after = tree(common.hp("data"))
        changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
        self.assertEqual(changed, {attention.path()}, "only the presentation file changes")
        self.assertEqual(self.summary()["total"], 2)


# ---------------------------------------------------------------- A17 / FLOW-06 impact suggestions

class A17Impact(DraftBase):
    def linked(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["include_tickets"]["ABC-1004"] = "Order export"
        write_config(common.home(), cfg)
        self.cfg = cfg
        rec = self.started_with_evidence()
        api_rec = {"method": "GET", "path": "/orders", "purpose": "List orders", "last": "2026-11-08"}
        self.put(apis={"GET /orders": api_rec},
                 replay={"final": [{"n": 1, "name": "List open orders", "method": "GET", "file": "01-list-orders.bru",
                                    "url": "{{ordersBaseUrl}}/orders?status=open",
                                    "expected": "200 with open orders only"},
                                   {"n": 2, "name": "Health", "method": "GET", "file": "02-health.bru",
                                    "url": "{{ordersBaseUrl}}/health"}]},
                 tests={"t-bru": {"id": "t-bru", "at": self.stamp(), "status": "passed",
                                  "command": "bru run bruno/ABC-1001/01-list-orders.bru --env QA"}})
        self.put("ABC-1002", apis={"GET /orders": api_rec},
                 replay={"final": [{"n": 3, "name": "Orders page", "method": "GET", "url": "{{base}}/orders"}]},
                 files={"src/orders/filter.py": {"path": "src/orders/filter.py", "changed": True}})
        for key in ("ABC-1003", "ABC-1004"):  # ABC-1003: a record with nothing of yours, outside your profile
            common.write_json(store.ticket_path(key), store.new_ticket(key))
        common.write_json(store.catalog_path(), {"apis": {"GET /orders": dict(api_rec, tickets=[
            "ABC-1001", "ABC-1002", "ABC-1003", "ABC-1004", "ABC-1009"])}})
        return rec

    def test_linked_api_yields_its_request_and_test_suggestion_with_citations(self):
        rec = self.linked()
        stack, calls = self.side_effect_free()
        with stack:
            imp = self.read("detail", workflow=rec["id"])["impact"]
        self.assertEqual(calls, [])
        rows = {r["id"]: r for r in imp["items"]}
        req = rows["request:1"]
        self.assertEqual((req["confidence"], req["kind"]), ("explicit", "request"))
        self.assertEqual([x["ref"] for x in req["links"]], ["apis:GET /orders", "replay:1"])
        self.assertIn("Replay saved request 1 (List open orders)", req["validation"]["text"])
        self.assertIn("200 with open orders only", req["validation"]["text"])
        self.assertFalse(req["validation"]["executable"], "a proposed step is text, never an action")
        self.assertEqual(rows["test:t-bru"]["confidence"], "explicit")
        self.assertIn("tests:t-bru", [x["ref"] for x in rows["test:t-bru"]["links"]])
        peer = rows["ticket:ABC-1002"]
        self.assertEqual(peer["confidence"], "historical")
        self.assertEqual([x["ref"] for x in peer["links"]], ["tickets:ABC-1002", "tickets:ABC-1002:replay:3",
                                                             "files:src/orders/filter.py"])
        self.assertEqual([x["ref"] for x in rows["ticket:ABC-1004"]["links"]], ["catalog:apis:GET /orders"])
        self.assertNotIn("request:2", rows, "the unrelated health request is never a dependency")
        for outside in ("ABC-1003", "ABC-1009"):
            self.assertNotIn(outside, json.dumps(imp), "a ticket outside your profile is never cited")
        self.assertEqual([r["confidence"] for r in imp["items"]], ["explicit", "explicit", "historical", "historical"])
        self.assertEqual(imp["coverage"], "partial")
        self.assertTrue(any("method and path template only" in x for x in imp["limits"]))

    def test_no_links_says_no_recorded_impact_links_never_safe(self):
        rec = self.planned()
        self.put(apis={"POST /refunds": {"method": "POST", "path": "/refunds"}})
        imp = self.read("detail", workflow=rec["id"])["impact"]
        self.assertEqual((imp["state"], imp["label"], imp["items"]), ("none", "No recorded impact links", []))
        self.assertIn("not evidence that the change is safe", imp["note"])
        self.assertEqual(imp["unlinked"], ["POST /refunds: no saved request or captured test of this ticket calls it"])
        text = json.dumps(imp).lower()
        self.assertNotIn("safe to change", text)
        self.assertNotIn('"no impact"', text)

    def test_database_impact_comes_from_the_cache_only(self):
        from chitragupta import dbaccess, dbflows, dbimpact, dbstore, dbviews
        rec = self.planned()
        cached = {"id": "x_0123456789abcdef", "kind": "impact", "target": {"id": "o_aaaaaaaaaaaa", "label": "shop.orders"},
                  "change_label": "Type change", "computed_at": "2026-11-08T09:00:00+00:00", "connections": ["qa-main"],
                  "coverage": ["Writers not checked yet on qa-main (shop.orders)"],
                  "items": [{"id": "o_bbbbbbbbbbbb", "label": "shop.order_report", "kind_label": "View",
                             "relation_label": "Reads it", "path": ["shop.orders", "shop.order_report"],
                             "edge": "e_1", "basis": "direct", "why": "It reads the changed column.",
                             "validation": {"text": "Query the report view after the change and compare totals."},
                             "tickets": ["ABC-1001"]},
                            {"id": "o_cccccccccccc", "label": "nightly export", "kind_label": "Job",
                             "relation_label": "Associated", "path": ["shop.orders", "nightly export"], "edge": "e_2",
                             "basis": "inferred", "why": "Named near it in a recorded note.",
                             "validation": {"text": "Check the next export file for the changed column."}},
                            {"id": None, "key": "ABC-1001", "relation": "ticket", "label": "ABC-1001"}]}

        class Ctx:
            connections = {"qa-main": {}}
            policy_generation = "pg"

        class Vis:
            nodes = {"o_aaaaaaaaaaaa": {}, "o_bbbbbbbbbbbb": {}, "o_cccccccccccc": {}}

        def computed(*a, **k):
            raise AssertionError("impact computed on a read")
        with mock.patch.object(dbstore, "iter_records", lambda kind, limit=50: iter([cached])), \
                mock.patch.object(dbaccess, "AccessContext", lambda cfg=None: Ctx()), \
                mock.patch.object(dbflows, "Visible", lambda ctx, cfg=None: Vis()), \
                mock.patch.object(dbimpact, "cache_state", lambda ctx, r: {"state": "current", "reasons": []}), \
                mock.patch.object(dbviews, "_impact_items", lambda vis, r: r["items"]), \
                mock.patch.object(dbimpact, "impact", computed):
            imp = impact.suggestions(rec, self.tk(), common.load_config())
        rows = {r["id"]: r for r in imp["items"]}
        view = rows["database:o_bbbbbbbbbbbb"]
        self.assertEqual(view["confidence"], "explicit")
        self.assertEqual(view["links"][0]["ref"], "impact:x_0123456789abcdef")
        self.assertEqual(view["validation"]["text"], "Query the report view after the change and compare totals.")
        self.assertEqual(rows["database:o_cccccccccccc"]["confidence"], "inferred")
        self.assertEqual([r["confidence"] for r in imp["items"]], ["explicit", "inferred"])
        self.assertIn("Writers not checked yet on qa-main (shop.orders)", imp["limits"])


# ---------------------------------------------------------------- A22 day boundaries

class A22DayBoundaries(DraftBase):
    def day_of_views(self):
        snap, plan = self.read("snapshot"), self.read("planner")
        return snap, plan, progress.today(None, self.now)["date"], profile.day_of(self.now, common.load_config())

    def test_planner_and_snapshot_days_follow_the_profile_zone_at_utc_midnight_and_dst(self):
        self.planned()
        with mock.patch.object(profile, "tzinfo", lambda cfg: FallZone()):
            # across a UTC midnight: two UTC dates, one profile day (18:30 and 19:30 local on the 8th)
            for when in (dt.datetime(2026, 11, 8, 23, 30, tzinfo=UTC), dt.datetime(2026, 11, 9, 0, 30, tzinfo=UTC)):
                self.now = when
                snap, plan, today, day = self.day_of_views()
                self.assertEqual((snap["day"], plan["day"], today, day), ("2026-11-08",) * 4)
                self.assertEqual(snap["generated_at"], when.strftime("%Y-%m-%dT%H:%M:%SZ"))
                self.assertTrue(snap["generated_local"].endswith("-05:00"))
                self.assertEqual(snap["snapshot_id"], self.read("snapshot")["snapshot_id"])
            # the DST change: 08:30 UTC is 03:30 local after the fall-back, still the work day of the 31st
            self.now = dt.datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
            snap, plan, today, day = self.day_of_views()
            self.assertEqual((snap["day"], plan["day"], today, day), ("2026-10-31",) * 4)
            self.assertEqual(snap["day_bounds"], {"start": "2026-10-31T08:00:00Z", "end": "2026-11-01T09:00:00Z",
                                                  "hours": 25.0})
            self.now = dt.datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
            snap, plan, today, day = self.day_of_views()
            self.assertEqual((snap["day"], plan["day"], today, day), ("2026-11-01",) * 4)
            self.assertEqual(snap["day_bounds"]["hours"], 24.0)
        # a fixed zone from the profile setting puts the same instant on another day: the zone decides
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["timezone"] = "-04:00"
        write_config(common.home(), cfg)
        self.now = dt.datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
        snap, plan, today, day = self.day_of_views()
        self.assertEqual((snap["day"], plan["day"], today, day, snap["timezone"]), ("2026-11-01",) * 4 + ("-04:00",))


class Allowlist(DraftBase):
    def test_new_actions_are_allowlisted_and_others_still_refused(self):
        rec = self.create()
        for name in ("draft_send", "attention_clear", "draft_delete"):
            with self.assertRaisesRegex(ValueError, "unknown"):
                companion.action({"action": name, "workflow": rec["id"], "revision": rec["revision"]})
        self.assertIn("drafts", views.OPERATIONS)
        with self.assertRaises(ValueError):
            views.read({"operation": "drafts", "workflow": rec["id"], "audience": "pr"})


if __name__ == "__main__":
    unittest.main()
