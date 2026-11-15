"""Daily Planner domain: requirement baselines from cached Jira cards, planner cards, the plan packet and
generated-plan validation (FLOW-12, PLAN-01, PLAN-02; A42, A43, A54, A55).

Synthetic data only (ABC-…, acme.test), frozen clock, isolated data homes, every process start a tripwire.
"""
import datetime as dt
import json
import os
import unittest
from unittest import mock

from tests.support import Isolated, write_config
from chitragupta import (common, companion, companionexecution as execution, companionplanning as planning,
                         mcpjobs, store, workflowmodel as model, workflowstore)
from chitragupta.safety import commands

T0 = dt.datetime(2026, 11, 8, 10, 0, tzinfo=dt.timezone.utc)
JIRA, JIRA2 = "https://jira.acme.test", "https://jira2.acme.test"
FIELD = "customfield_10300"


def adf(*items):
    return {"type": "doc", "content": [{"type": "bulletList", "content": [
        {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": t}]}]}
        for t in items]}]}


def capped(text):
    """What the card job keeps (mcpjobs.card_sources): whitespace collapsed, cut at 3,000 characters."""
    return common.truncate(" ".join(text.split()), 3000)


def output(criteria=("ac1",), files=None, **over):
    out = {"summary": "Add the order filter.", "approach": "Extend the order list query with a status filter.",
           "steps": [{"id": "s1", "text": "Add the filter parameter", "kind": "implementation", "depends_on": [],
                      "criteria": list(criteria)},
                     {"id": "s2", "text": "Check the filtered list", "kind": "verification", "depends_on": ["s1"],
                      "criteria": list(criteria)}],
           "files": files if files is not None else [{"path": "src/orders/list.py", "component": "order list",
                                                      "basis": "evidence"}],
           "database": "Not applicable: no schema change.",
           "criteria_mapping": [{"criterion": c, "steps": ["s1", "s2"]} for c in criteria],
           "edge_cases": ["Empty result"], "risks": ["Slow query on large accounts"],
           "tests": [{"scenario": "Filter by open", "expected": "Only open orders are listed"}],
           "verification": ["Run the order list tests"], "assumptions": ["Status values are stable"], "missing": []}
    out.update(over)
    return out


class Base(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.repo = os.path.realpath(os.path.join(common.home(), "scratch"))
        os.makedirs(self.repo)
        self.cfg = {"profile": {"accounts": [{"provider": "jira", "instance": JIRA, "id": "dev-a"},
                                             {"provider": "jira", "instance": JIRA2, "id": "dev-b"}],
                                "repos": [self.repo],
                                "include_tickets": {"ABC-1001": "Mine", "ABC-1002": "Mine", "ABC-1004": "Mine",
                                                    "ABC-1005": "Mine"}},
                    "watched_repos": [self.repo], "git_author_emails": ["dev@acme.test"],
                    "companion": {"criteria_fields": [FIELD]}}
        write_config(common.home(), self.cfg)
        for key in ("ABC-1001", "ABC-1002", "ABC-1003", "ABC-1004", "ABC-1005"):
            common.write_json(store.ticket_path(key), store.new_ticket(key))
        self.now = T0
        clock = mock.patch.object(common, "now", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.processes = []

        def no_process(*args, **kwargs):
            self.processes.append(args[:1])
            raise AssertionError("planning code started a process")
        for name in ("run", "spawn"):
            trip = mock.patch.object(commands, name, no_process)
            trip.start()
            self.addCleanup(trip.stop)
        self.addCleanup(lambda: self.assertEqual(self.processes, [], "no Jira, model or git process"))

    def jira(self, key="ABC-1001", description="", comments=(), raw_fields=None, error=None, instance=None,
             fields=None, priority="High"):
        rec = {"key": key, "fetched_at": "2026-11-08T09:00:00+00:00",
               "card": {"found": True, "title": "Order filter", "status": "In Progress", "priority": priority,
                        "summary": "Filter orders.", "description": description, "comments": list(comments)},
               "raw": json.dumps({"key": key, "fields": raw_fields or {}}), "error": error,
               "fields": fields or mcpjobs.CARD_FIELDS + "," + FIELD}
        if instance:
            rec["instance"] = instance
        common.write_json(common.hp("data", "jira", key + ".json"), rec)

    def put(self, key="ABC-1001", **fields):
        tk = common.read_json(store.ticket_path(key))
        tk.update(fields)
        common.write_json(store.ticket_path(key), tk)

    def create(self, key="ABC-1001", criteria=None, **kw):
        return companion.create(key, self.repo, JIRA, criteria or [{"id": "ac1", "text": "Orders can be filtered"}],
                                **kw)


LONG = ("Users need filtering. h3. Acceptance criteria * Orders can be filtered by status * The chosen filter "
        "persists across reloads * The filter badge shows " + "the active filter name " * 140 +
        "* A final criterion beyond the cap")


class A54ScopedCompleteness(Base):
    def test_truncated_description_custom_field_failed_page_and_two_instances(self):
        self.jira(description=capped(LONG), raw_fields={FIELD: adf("Export respects the filter",
                                                                    "Archived orders can be filtered")},
                  error="jira_get_issue: page 2 failed")
        prop = planning.propose("ABC-1001", JIRA)
        texts = [c["text"] for c in prop["criteria"]]
        self.assertEqual(texts, ["Export respects the filter", "Archived orders can be filtered",
                                 "Orders can be filtered by status", "The chosen filter persists across reloads"])
        self.assertNotIn("A final criterion beyond the cap", json.dumps(prop))
        self.assertEqual([c["id"] for c in prop["criteria"]], ["ac1", "ac2", "ac3", "ac4"])
        self.assertEqual(prop["criteria"][0]["fragment"]["field"], FIELD)
        self.assertEqual(prop["criteria"][2]["source"], "Jira description, acceptance criteria item 1")
        self.assertEqual((prop["completeness"], prop["state"]), ("partial", planning.INCOMPLETE))
        self.assertEqual(prop["overall_coverage"], "not claimed")
        gaps = " | ".join(prop["gaps"])
        for needle in ("last Jira read failed", "cut at 3000 characters", "criterion was cut",
                       "does not record its instance"):
            self.assertIn(needle, gaps)
        self.assertEqual(prop["coverage"]["fields"][FIELD], "collected")
        self.assertEqual(prop["coverage"]["description"], "truncated")
        self.assertFalse(prop["confirmed"])
        # confirmed by the developer -> the workflow baseline keeps its incompleteness; no overall claim
        ok = planning.confirmable(prop)
        rec = self.create(criteria=ok["criteria"], completeness=ok["completeness"], coverage=ok["coverage"])
        base = rec["baselines"][rec["baseline"]]
        self.assertEqual(base["coverage"]["state"], planning.INCOMPLETE)
        self.assertEqual(base["criteria"][2]["fragment"]["field"], "description")
        self.assertEqual(model.score(base, {})["label"], "Coverage of available criteria")

    def test_no_criteria_scope_and_instance_identity(self):
        self.jira(description="Make the list faster.")
        prop = planning.propose("ABC-1001", JIRA)
        self.assertEqual((prop["criteria"], prop["completeness"], prop["state"]),
                         ([], "unavailable", planning.NO_CRITERIA))
        self.assertIsNone(model.score(model.baseline([], "unavailable"), {})["percent"])  # N/A, never 100%
        self.jira("ABC-1003", description="h3. Acceptance criteria * Hidden work")
        with self.assertRaises(PermissionError):
            planning.propose("ABC-1003", JIRA)  # not in the confirmed profile: nothing exposed
        with self.assertRaises(PermissionError):
            planning.propose("ABC-1001", "https://other.acme.test")
        self.jira(description="h3. Acceptance criteria * Orders can be filtered", instance=JIRA2)
        with self.assertRaises(PermissionError):
            planning.propose("ABC-1001", JIRA)
        self.assertEqual(len(planning.propose("ABC-1001", JIRA2)["criteria"]), 1)

    def test_uncollected_field_and_missing_card_stay_gaps(self):
        prop = planning.propose("ABC-1001", JIRA)
        self.assertEqual(prop["state"], planning.NO_CRITERIA)
        self.assertIn("not cached", prop["gaps"][0])
        self.jira(description="h3. Acceptance criteria * One * Two", fields=mcpjobs.CARD_FIELDS)
        prop = planning.propose("ABC-1001", JIRA)
        self.assertEqual(prop["coverage"]["fields"][FIELD], "not collected")
        self.assertEqual(prop["completeness"], "partial")

    def test_comments_are_proposals_until_accepted_and_jira_is_never_written(self):
        self.jira(description="h3. Acceptance criteria * Orders can be filtered",
                  comments=[{"id": "501", "at": "2026-11-07", "text": "Also support the archived status"}])
        prop = planning.propose("ABC-1001", JIRA)
        self.assertEqual(len(prop["criteria"]), 1)
        self.assertEqual(prop["proposed"][0]["state"], "proposed")
        ok = planning.confirmable(prop)
        rec = self.create(criteria=ok["criteria"], completeness=ok["completeness"], coverage=ok["coverage"])
        jira_before = open(common.hp("data", "jira", "ABC-1001.json"), "rb").read()
        old = rec["baseline"]
        with self.assertRaises(ValueError):
            planning.accept_requirement(rec["id"], rec["revision"], ["q_unknown"], rationale="Agreed")
        rec = companion.action({"action": "accept_requirement", "workflow": rec["id"], "revision": rec["revision"],
                                "proposals": [prop["proposed"][0]["id"]], "rationale": "Agreed with the reporter"})
        base = rec["baselines"][rec["baseline"]]
        self.assertNotEqual(rec["baseline"], old)
        self.assertIn(old, rec["baselines"])  # history kept
        self.assertEqual([c["id"] for c in base["criteria"]], ["ac1", "ac2"])
        self.assertIn("accepted locally", base["criteria"][1]["source"])
        self.assertEqual(base["lineage"]["accepted"], ["ac2"])
        self.assertEqual(rec["decisions"][-1]["source"], "developer")
        self.assertEqual(open(common.hp("data", "jira", "ABC-1001.json"), "rb").read(), jira_before)
        self.assertEqual(self.forbidden, [])  # no MCP (Jira) job


class A55BaselineIdentityAndPlans(Base):
    def test_reorder_reword_split_keep_lineage_and_total_weight(self):
        prev = model.baseline([{"id": "ac1", "text": "Orders can be filtered by status"},
                               {"id": "ac2", "text": "The chosen filter persists across reloads"},
                               {"id": "ac3", "text": "Export and print respect the filter"}])
        rows = [{"text": "The chosen filter persists across reloads"},
                {"text": "Orders can be filtered by their status"},
                {"text": "A badge shows the active filter"}]
        out, lineage = planning.assign(rows, prev)
        self.assertEqual([r["id"] for r in out], ["ac2", "ac1", "ac4"])
        self.assertEqual(lineage["reworded"], ["ac1"])
        self.assertTrue(lineage["reordered"])
        self.assertEqual([r["id"] for r in lineage["removed"]], ["ac3"])
        rows, lineage = planning.revise(prev, [dict(c) for c in prev["criteria"]],
                                        {"ac3": ["Export respects the filter", "Print respects the filter"]})
        self.assertEqual([r["id"] for r in rows], ["ac1", "ac2", "ac3-1", "ac3-2"])
        self.assertEqual({r.get("parent") for r in rows if r["id"].startswith("ac3")}, {"ac3"})
        self.assertEqual(lineage["weight"], {"before": 3, "after": 3.0})
        self.assertEqual(lineage["removed"], [])
        self.assertEqual(lineage["split"][0]["children"], ["ac3-1", "ac3-2"])
        # persisted through the existing requirements action; parent links and total weight survive
        rec = self.create(criteria=[dict(c) for c in prev["criteria"]])
        rec = execution.requirements(rec["id"], rec["revision"], rows, "partial", "split compound criterion")
        base = rec["baselines"][rec["baseline"]]
        self.assertEqual(sum(c["weight"] for c in base["criteria"]), 3)
        self.assertEqual(base["criteria"][2]["parent"], "ac3")
        three = model.baseline([{"id": "ac1", "text": "Do it"}])
        rows, _ = planning.revise(three, [dict(c) for c in three["criteria"]], {"ac1": ["a", "b", "c"]})
        self.assertAlmostEqual(sum(r["weight"] for r in rows), 1.0, places=9)

    def test_generated_output_sections_mappings_unknowns_and_labels(self):
        base = model.baseline([{"id": "ac1", "text": "Orders can be filtered"},
                               {"id": "ac2", "text": "The filter persists"}])
        out = output(criteria=("ac1",), database="", files=[
            {"path": "src/orders/list.py", "component": "order list", "basis": "evidence"},
            {"path": "src/orders/badge.py", "component": "badge", "basis": "proposed new file"},
            {"path": "src/orders/claimed.py", "component": "claimed", "basis": "evidence"}])
        body = planning.validate_output(out, base, ["src/orders/list.py"], origin={"job": "cj_x"})
        plan = model.plan(body, base, source="claude")
        self.assertEqual(set(plan["sections"]), set(model.REQUIRED_SECTIONS))
        self.assertTrue(plan["sections"]["database"].startswith("Unknown"))  # a missing field stays unknown
        files = {f["path"]: f for f in plan["details"]["files"]}
        self.assertEqual(files["src/orders/badge.py"]["basis"], "proposed new file")
        self.assertEqual(files["src/orders/claimed.py"]["basis"], "proposed new file")  # relabeled
        self.assertEqual(plan["details"]["relabelled"], ["src/orders/claimed.py"])
        self.assertEqual(plan["details"]["unmapped"], ["ac2"])
        self.assertEqual(plan["details"]["mapping"][0], {"criterion": "ac1", "steps": ["s1", "s2"]})
        self.assertEqual(plan["details"]["origin"]["label"], "Claude suggestion")
        self.assertEqual(plan["assumptions"], ["Status values are stable"])
        self.assertEqual([c["id"] for c in base["criteria"]], ["ac1", "ac2"])  # an assumption is no requirement
        self.assertIn("Assumptions (not requirements)", plan["text"])
        for bad, why in ((output(criteria=("ac9",)), "unknown criterion"),
                         (output(files=[{"path": "C:/work/repo/a.py", "component": "x", "basis": "evidence"}]),
                          "absolute path"),
                         (output(approach="Use token ghp_" + "a" * 36), "secret"),
                         (output(risks=["Run claude with --dangerously-skip-permissions"]), "tools or permissions"),
                         (output(steps=[]), "incomplete"), ([], "no structured")):
            with self.assertRaisesRegex(planning.Incomplete, why):
                planning.validate_output(bad, base, [])

    def test_packet_is_bounded_delimited_and_relative(self):
        self.jira(description="h3. Acceptance criteria * Orders can be filtered")
        rec = self.create()
        self.put(files={"src/orders/list.py": {"path": "src/orders/list.py", "last": "2026-11-07", "changed": True},
                        "C:/Users/dev/secret.py": {"path": "C:/Users/dev/secret.py", "last": "2026-11-07"},
                        **{"src/gen/f%03d.py" % i: {"last": "2026-11-0%d" % (i % 7 + 1)} for i in range(400)}},
                 items={"d1": {"kind": "decision", "title": "Keep the old API", "date": "2026-11-07"},
                        "e1": {"kind": "error", "title": "Index hint failed", "date": "2026-11-07"}})
        text, data = planning.packet(rec, common.read_json(store.ticket_path("ABC-1001")), limit=8192)
        self.assertTrue(text.splitlines()[1].startswith("BEGIN UNTRUSTED SOURCE DATA"))
        self.assertLessEqual(len(text.encode("utf-8")), 8192 + 400)
        self.assertNotIn("C:/Users", text)
        self.assertTrue(any("not repository-relative" in o for o in data["omitted"]))
        self.assertTrue(any("files beyond" in o for o in data["omitted"]))
        self.assertEqual(data["requirements"]["confirmed"][0]["id"], "ac1")
        self.assertEqual(data["evidence"]["decisions"][0]["title"], "Keep the old API")
        self.assertEqual(data["evidence"]["failed_approaches"][0]["title"], "Index hint failed")
        self.assertEqual(data["evidence"]["database"]["available"], False)


class A42Planner(Base):
    def rec(self, key, stage, **extra):
        base = model.baseline([{"id": "ac1", "text": "Expected"}])
        out = {"id": "w_" + model.digest([key, stage])[:64].ljust(64, "0"), "stage": stage, "binding":
               {"location": self.repo, "ticket": key}, "baselines": {base["id"]: base}, "baseline": base["id"],
               "overlays": {}, "jobs": {}, "updated_at": "2026-11-08T09:00:00+00:00"}
        out.update(extra)
        return out

    def row(self, key, last="2026-11-01", **kw):
        return dict({"key": key, "title": "Task " + key, "jira_status": "In Progress", "last": last, "minutes": 30,
                     "branches": []}, **kw)

    def test_all_eight_groups_order_chooser_legacy_and_collapsed_completed(self):
        stages = ["new", "planned", "review_incomplete", "in_development", "paused", "awaiting_verification",
                  "pr_review", "completed"]
        rows = [self.row("ABC-%d" % (2000 + i), last="2026-11-0%d" % (i + 1)) for i in range(len(stages))]
        workflows = {r["key"]: [self.rec(r["key"], s)] for r, s in zip(rows, stages)}
        workflows["ABC-2004"][0]["overlays"] = {"stale": {"reason": "Jira requirements changed"}}
        rows.append(self.row("ABC-2100", last="2026-11-08"))  # legacy development, no workflow
        rows.append(self.row("ABC-2200", last="2026-10-01"))  # two attempts
        workflows["ABC-2200"] = [self.rec("ABC-2200", "paused"), self.rec("ABC-2200", "completed", id="w_" + "1" * 64)]
        jira = {"ABC-2001": {"card": {"priority": "Highest", "description": "h3. Acceptance criteria * A * B"},
                             "record": {"fetched_at": "2026-11-08T08:00:00+00:00"}}}
        tickets = {"ABC-2100": {"commits": {"c1": {"sha": "c1"}}}}
        out = planning.cards(rows, jira, workflows, tickets, pinned=["ABC-2002"])
        every = out["items"] + out["completed"]["items"]
        self.assertEqual({c["group"] for c in every}, set(planning.GROUPS))
        self.assertEqual(out["groups"]["completed"], 1)
        self.assertTrue(out["completed"]["collapsed"])
        order = [c["ticket"] for c in out["items"]]
        self.assertEqual(order[0], "ABC-2002")  # pinned (session-local input) first
        self.assertEqual(order[1], "ABC-2004")  # resumable with an actionable blocker
        self.assertIn("Blocker: Jira requirements changed", out["items"][1]["why"])
        legacy = next(c for c in every if c["ticket"] == "ABC-2100")
        self.assertEqual((legacy["action"], legacy["group"], legacy["stage"]), ("Resume Task", "in_development", "legacy"))
        self.assertIn("reconstruct", legacy["reconstruction"])
        chooser = next(c for c in every if c["ticket"] == "ABC-2200")
        self.assertEqual((chooser["action"], chooser["workflow"], len(chooser["attempts"])), ("Choose attempt", None, 2))
        self.assertEqual(chooser["group"], "paused")  # the unfinished attempt is never hidden by the completed one
        new = next(c for c in every if c["ticket"] == "ABC-2000")
        self.assertEqual(new["action"], "Generate Plan")
        partial = next(c for c in every if c["ticket"] == "ABC-2003")
        self.assertEqual((partial["priority"], partial["due"], partial["dependencies"]["state"],
                          partial["freshness"]["state"]), ("Unavailable",) * 4)
        planned = next(c for c in every if c["ticket"] == "ABC-2001")
        self.assertEqual(planned["criteria"]["basis"], "confirmed baseline")
        self.assertEqual(next(c for c in every if c["ticket"] == "ABC-2005")["stage"], "awaiting_verification")
        self.assertTrue(all(c["why"] for c in every))

    def test_real_planner_is_scoped_partial_and_selection_never_regenerates(self):
        self.jira("ABC-1001", description="h3. Acceptance criteria * Orders can be filtered", priority="High",
                  fields=mcpjobs.CARD_FIELDS, raw_fields={"issuelinks": [
                      {"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "ABC-1002", "fields": {"status": {"name": "Open"}}}},
                      {"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "ABC-1003", "fields": {"status": {"name": "Open"}}}},
                      {"type": {"inward": "is blocked by"}, "inwardIssue": {"key": "ABC-1004", "fields": {"status": {"name": "Done"}}}}]})
        self.put("ABC-1001", last="2026-11-07T10:00:00+00:00")
        self.put("ABC-1002", last="2026-11-06T10:00:00+00:00")
        self.put("ABC-1003", last="2026-11-08T10:00:00+00:00",  # someone else's work: outside the profile
                 commits={"c9": {"sha": "c9", "ae": "other@acme.test"}})
        rec = self.create("ABC-1002")
        companion.save_plan(rec["id"], rec["revision"], {"text": "Manual plan", **{s: "Detail" for s in model.REQUIRED_SECTIONS},
                                                         "steps": [{"id": "one", "text": "Do", "criteria": ["ac1"]}]})
        files = sorted(os.path.join(dp, f) for dp, _, fs in os.walk(common.home()) for f in fs)
        heads = workflowstore.load(rec["id"])
        reads = [planning.planner(pinned=[]) for _ in range(3)]
        self.assertEqual(reads[0], reads[2])
        self.assertEqual(files, sorted(os.path.join(dp, f) for dp, _, fs in os.walk(common.home()) for f in fs))
        self.assertEqual(heads, workflowstore.load(rec["id"]))
        tickets = [c["ticket"] for c in reads[0]["items"]]
        self.assertNotIn("ABC-1003", tickets)
        first = next(c for c in reads[0]["items"] if c["ticket"] == "ABC-1001")
        self.assertEqual(first["dependencies"], {"state": "known", "unresolved": [{"ticket": "ABC-1002", "status": "Open"}],
                                                 "outside": 1})
        self.assertEqual(first["criteria"]["basis"], "proposed from cached Jira fields")
        self.assertIn("Requirements incomplete", first["criteria"]["label"])  # FIELD configured, not collected: gap
        self.assertEqual(next(c for c in reads[0]["items"] if c["ticket"] == "ABC-1002")["group"], "planned")
        other = next(c for c in reads[0]["items"] if c["ticket"] == "ABC-1004")
        self.assertEqual(other["criteria"]["label"], "Unavailable")


class A43Versions(Base):
    def test_versions_are_immutable_and_reads_never_regenerate(self):
        rec = self.create()
        body = {"text": "Manual plan", **{s: "Detail" for s in model.REQUIRED_SECTIONS},
                "steps": [{"id": "one", "text": "Do", "criteria": ["ac1"]}]}
        rec = companion.save_plan(rec["id"], rec["revision"], body)
        first = json.dumps(rec["plans"], sort_keys=True)
        rec = companion.save_plan(rec["id"], rec["revision"], dict(body, text="Manual plan, edited"))
        self.assertEqual(len(rec["plans"]), 2)
        old = json.loads(first)
        for pid, version in old.items():
            self.assertEqual(rec["plans"][pid], version)  # the earlier version is unchanged
        edited = rec["plans"][rec["selected_plan"]]
        self.assertEqual(edited["parent"], list(old)[0])
        before = json.dumps(workflowstore.load(rec["id"]), sort_keys=True)
        for _ in range(3):
            companion.resume(rec["id"])
            planning.planner()
            companion.get(rec["id"])
        self.assertEqual(before, json.dumps(workflowstore.load(rec["id"]), sort_keys=True))
        self.assertFalse(os.path.isdir(os.path.join(workflowstore.root(rec["id"]), "jobs")))


class CriteriaFieldRead(unittest.TestCase):
    def test_card_job_reads_only_validated_criteria_fields(self):
        base = mcpjobs.card_fields({}).split(",")
        fields = mcpjobs.card_fields({"companion": {"criteria_fields": [FIELD, "bad;drop", "x" * 80]}}).split(",")
        self.assertEqual(fields, base + [FIELD])


if __name__ == "__main__":
    unittest.main()
