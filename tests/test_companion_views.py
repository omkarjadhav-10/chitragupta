"""ARCH-03 shared Companion projections, the read-only bridge and the text fallback (A27/A34/A42/A53/A76/A77).

Synthetic data only (ABC-…, acme.test), frozen clocks, isolated data homes; every process start, model call,
network connection, MCP job and workflow save is a tripwire during reads.
"""
import io
import json
import os
import socket
import statistics
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest import mock

from tests.support import Isolated, rmtree, write_config
from tests.test_companion_state import Base, JIRA, plan_body
from chitragupta import (common, companion, companion_mcp, companionevidence as evidence, companionjobs,
                         companionviews as views, extract, profile, store, workflowmodel as model, workflowstore)
from chitragupta.companionexecution import record_dispatch

READS = ("snapshot", "planner", "detail", "review", "plan", "changes", "verification", "jobs", "handoff", "database",
         "drafts", "graph_summary", "graph_trace", "graph_changes")


def graph_args(op, rec):
    """The graph operations take a ticket (and a requirement for the trace), not a workflow."""
    if not op.startswith("graph_"):
        return {}
    return dict({"ticket": rec["binding"]["ticket"]}, **({"requirement": "ac1"} if op == "graph_trace" else {}))


def tree(*roots):
    out = {}
    for root in roots:
        for dp, _, fns in os.walk(root):
            for f in fns:
                p = os.path.join(dp, f)
                st = os.stat(p)
                out[p] = (st.st_size, st.st_mtime_ns)
    return out


class ViewBase(Base):
    def setUp(self):
        super().setUp()
        views._SLIM.clear()
        views._JIRA.clear()

    def read(self, operation, **args):
        return views.read(dict(args, operation=operation))

    def handed_off(self):
        rec = self.approve(self.planned())
        return rec

    def side_effect_free(self):
        """Tripwires for every way a read could act: model, process (Base), network, MCP (Isolated), save, job."""
        calls = []

        def trip(name):
            def fail(*a, **k):
                calls.append(name)
                raise AssertionError("read performed " + name)
            return fail
        stack = ExitStack()
        stack.enter_context(mock.patch.object(extract, "call_claude", trip("model call")))
        stack.enter_context(mock.patch.object(socket.socket, "connect", trip("network connection")))
        stack.enter_context(mock.patch.object(socket, "create_connection", trip("network connection")))
        stack.enter_context(mock.patch.object(workflowstore, "save", trip("workflow save")))
        stack.enter_context(mock.patch.object(companionjobs, "enqueue", trip("job enqueue")))
        stack.enter_context(mock.patch.object(companionjobs, "spawn_runner", trip("runner spawn")))
        stack.enter_context(mock.patch.object(common, "write_json", trip("data-home write")))
        return stack, calls


class EnvelopeContract(ViewBase):
    def test_snapshot_envelope_fields_and_bounds(self):
        rec = self.planned()
        out = self.read("snapshot", workflow=rec["id"])
        for field in ("schema_version", "profile_id", "profile_generation", "scope_generation", "snapshot_id",
                      "generated_at", "timezone", "context", "database_context", "workflow_context", "sources",
                      "focus", "next_action", "attention_summary", "capabilities", "cursor"):
            self.assertIn(field, out)
        self.assertEqual(out["schema_version"], 1)
        self.assertTrue(out["generated_at"].endswith("Z"))
        self.assertTrue(out["profile_id"].startswith("pf_"))
        self.assertEqual(out["profile_generation"], profile.signature(common.load_config()))
        self.assertEqual(out["context"]["state"], "explicit")
        self.assertEqual(out["workflow_context"]["stage"], "planned")
        self.assertEqual(out["next_action"]["label"], "Review saved plan")
        self.assertEqual(out["capabilities"]["jev"]["available"], False)
        self.assertFalse(out["capabilities"]["direct_execution"]["available"])
        self.assertEqual(out["database_context"], {"available": False,
                                                  "reason": "No database flow or investigation selected"})
        jev_src = [s for s in out["sources"] if s["id"] == "jev"][0]
        self.assertEqual(jev_src["state"], "Unavailable")
        self.assertNotIn("Connected", [s["state"] for s in out["sources"] if s["success_at"] is None])
        self.assertLessEqual(len(json.dumps(out).encode()), views.MAX_BYTES)
        again = self.read("snapshot", workflow=rec["id"])
        self.assertEqual(again["snapshot_id"], out["snapshot_id"], "same content, same view identity")

    def test_every_operation_is_bounded_and_validated(self):
        rec = self.handed_off()
        for op in READS:
            with self.subTest(op=op):
                out = self.read(op, workflow=rec["id"]) if op not in ("snapshot", "planner", "database") \
                    and not op.startswith("graph_") else self.read(op, **graph_args(op, rec))
                self.assertEqual(out["operation"], op)
                self.assertLessEqual(len(json.dumps(out).encode()), views.MAX_BYTES)
        for bad in ({"operation": "save_plan"}, {"operation": "detail", "workflow": "../../x"},
                    {"operation": "snapshot", "tool": "x"}, {"operation": "plan", "plan": "p_../x"},
                    {"operation": "planner", "group": "everything"}, {"operation": "snapshot", "privacy": "yes"},
                    {"operation": "database", "flow": "f_; DROP"}, {"operation": "snapshot", "pinned": ["x"] * 11},
                    {"operation": "snapshot", "ticket": "A" * 9000}, "snapshot",
                    {"operation": "graph_trace", "ticket": "ABC-1001", "requirement": "ac1; x"},
                    {"operation": "graph_changes", "ticket": "ABC-1001", "since": "1"},
                    {"operation": "snapshot", "since": 1}, {"operation": "detail", "requirement": "ac1"},
                    {"operation": "graph_summary"}):
            with self.subTest(bad=str(bad)[:60]):
                with self.assertRaises(ValueError):
                    views.read(bad)

    def test_unknown_or_hidden_workflow_reveals_nothing(self):
        rec = self.planned()
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["repos"] = [self.repo_b]
        write_config(common.home(), cfg)
        with self.assertRaises(PermissionError):
            self.read("detail", workflow=rec["id"])
        out = self.read("snapshot", workflow=rec["id"])
        self.assertEqual(out["context"]["state"], "absent")
        self.assertIsNone(out["workflow_context"])
        self.assertNotIn(rec["id"], json.dumps(out))


class SideEffects(ViewBase):
    """A27/A34/A53/A76: every read makes zero model, process, network, MCP or database calls, zero writes."""

    def test_every_read_is_side_effect_free(self):
        rec = self.handed_off()
        self.put(commits={"c1": {"sha": "c1", "subject": "Work", "date": self.stamp(), "repo_name": "scratch"}})
        rec = companion.checkpoint(rec["id"], rec["revision"])
        before = tree(self.home, self.user_dir)
        ack = dict(workflowstore.load(rec["id"])["acknowledged"])
        stack, calls = self.side_effect_free()
        with stack:
            for _ in range(10):  # reopening ten times: one view, no growth
                for op in READS:
                    if op.startswith("graph_"):
                        self.read(op, **graph_args(op, rec))
                    elif op in ("snapshot", "planner", "database"):
                        self.read(op)
                        self.read(op, workflow=rec["id"]) if op == "snapshot" else None
                    else:
                        self.read(op, workflow=rec["id"])
        self.assertEqual(calls, [])
        self.assertEqual(self.forbidden, [], "no MCP / Slack job")
        self.assertEqual(before, tree(self.home, self.user_dir), "zero writes under the data home")
        after = workflowstore.load(rec["id"])
        self.assertEqual(after["acknowledged"], ack, "reading never advances the acknowledged cursor")
        self.assertEqual(companionjobs.jobs(rec["id"]), [], "reading never enqueues a job")


class Cursors(ViewBase):
    def make_tasks(self, n):
        incl = {"ABC-%d" % (2000 + i): "Task %d" % i for i in range(n)}
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["include_tickets"].update(incl)
        write_config(common.home(), cfg)
        self.cfg = cfg
        for key in incl:
            common.write_json(store.ticket_path(key), store.new_ticket(key))

    def test_planner_pages_and_refuses_stale_forged_or_foreign_cursors(self):
        self.make_tasks(70)
        first = self.read("planner")
        self.assertEqual(len(first["items"]), views.PAGE_SIZE)
        self.assertTrue(first["more"])
        second = self.read("planner", cursor=first["cursor"])
        self.assertEqual(len(second["items"]), 72 - views.PAGE_SIZE)
        self.assertFalse({c["ticket"] for c in first["items"]} & {c["ticket"] for c in second["items"]})
        body, mac = first["cursor"].rsplit(".", 1)
        for bad in (body + "." + "0" * 32, "x.y", first["cursor"] + "a", "A" * 2000):
            with self.assertRaisesRegex(ValueError, "reload"):
                self.read("planner", cursor=bad)
        with self.assertRaisesRegex(ValueError, "reload"):
            self.read("planner", cursor=first["cursor"], group="new")  # another view
        common.write_json(store.ticket_path("ABC-3999"), store.new_ticket("ABC-3999"))
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["include_tickets"]["ABC-3999"] = "Inserted"
        write_config(common.home(), cfg)  # profile generation + content change
        with self.assertRaisesRegex(ValueError, "reload"):
            self.read("planner", cursor=first["cursor"])

    def test_snapshot_cursor_continues_in_the_planner(self):
        self.make_tasks(30)
        snap = self.read("snapshot")
        self.assertTrue(snap["more"])
        rest = self.read("planner", cursor=snap["cursor"])
        self.assertEqual(len(snap["items"]) + len(rest["items"]), 32)

    def test_generation_change_during_read_publishes_nothing(self):
        rec = self.planned()
        real = profile.signature
        calls = []

        def drifting(cfg):
            calls.append(1)
            return real(cfg) if len(calls) < 3 else "changed-generation"
        with mock.patch.object(profile, "signature", drifting):
            with self.assertRaisesRegex(PermissionError, "profile changed"):
                self.read("planner")
        self.assertTrue(self.read("detail", workflow=rec["id"]))


class PrivacyAndStates(ViewBase):
    def test_overview_has_no_emails_paths_accounts_or_urls_and_privacy_masks_titles(self):
        rec = self.planned()
        self.put(title="Secret customer migration", title_hint="Secret customer migration",
                 release={"reminders": [{"id": "map:scratch", "kind": "mapping", "repo": "scratch", "severity": "warn",
                                         "text": "release branch for https://jira.acme.test/x not confirmed at %s"
                                                 % self.repo}]})
        for privacy in (False, True):
            out = self.read("snapshot", privacy=privacy)
            planner = self.read("planner", privacy=privacy)
            text = json.dumps([out, planner])
            for needle in ("dev@acme.test", "dev-a", "https://", self.repo, self.repo.replace("\\", "\\\\")):
                self.assertNotIn(needle, text, needle)
            if privacy:
                self.assertNotIn("Secret customer migration", text)
                self.assertEqual(out["items"][0]["title"], views.HIDDEN)
        self.assertTrue(any(a["label"] == "Resolve release mapping" for a in out["attention_summary"]["items"]))
        sel = self.read("snapshot", workflow=rec["id"])
        self.assertEqual(sel["next_action"]["label"], "Resolve release mapping")

    def test_unconfirmed_identity_shows_setup_and_no_cached_activity(self):
        self.planned()
        cfg = {"profile": {"repos": [self.repo], "include_tickets": {"ABC-1001": "My task"}},
               "watched_repos": [self.repo]}
        write_config(common.home(), cfg)
        out = self.read("snapshot")
        self.assertEqual(out["state"], "identity_unconfirmed")
        self.assertEqual(out["items"], [])
        self.assertEqual(out["next_action"]["label"], "Open profile setup")
        self.assertNotIn("ABC-1001", json.dumps(out))
        self.assertEqual(self.read("planner")["items"], [])

    def test_new_developer_sees_no_recorded_work_yet(self):
        for key in ("ABC-1001", "ABC-1002"):
            os.remove(store.ticket_path(key))
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["include_tickets"] = {}
        write_config(common.home(), cfg)
        out = self.read("snapshot")
        self.assertEqual(out["state"], "empty")
        self.assertIn("No recorded work yet", out["empty"])
        self.assertEqual(out["next_action"]["label"], "Open setup")


class ContextSelection(ViewBase):
    def test_explicit_evidenced_ambiguous_and_branch_lookalike(self):
        self.put(branches=["feature/ABC-1002-hotfix"])
        self.assertEqual(self.read("snapshot")["context"]["state"], "absent", "a branch name is not proof")
        a = self.started()
        snap = self.read("snapshot")
        self.assertEqual((snap["context"]["state"], snap["context"]["workflow"]), ("evidenced", a["id"]))
        self.assertIn("Observed execution activity", snap["context"]["evidence"])
        b = self.started(key="ABC-1002", repo=self.repo_b, sid="sess-2")
        amb = self.read("snapshot")
        self.assertEqual(amb["context"]["state"], "ambiguous")
        self.assertEqual({c["workflow"] for c in amb["context"]["candidates"]}, {a["id"], b["id"]})
        self.assertIsNone(amb["workflow_context"], "never picks one silently")
        self.assertEqual(amb["next_action"]["label"], "Choose a task")
        exp = self.read("snapshot", ticket="ABC-1002")
        self.assertEqual((exp["context"]["state"], exp["context"]["workflow"]), ("explicit", b["id"]))

    def test_several_attempts_need_a_choice(self):
        self.planned()
        self.planned(repo=self.repo_b)
        out = self.read("snapshot", ticket="ABC-1001")
        self.assertEqual(out["context"]["state"], "ambiguous")
        self.assertEqual(len(out["context"]["candidates"]), 2)
        card = [c for c in self.read("planner")["items"] if c["ticket"] == "ABC-1001"][0]
        self.assertEqual(card["action"], "Choose attempt")
        self.assertEqual(len(card["attempts"]), 2)


class WorkflowViews(ViewBase):
    def test_planner_groups_why_and_collapsed_completed(self):
        self.planned()
        out = self.read("planner")
        self.assertEqual([g["id"] for g in out["groups"]], list(views.GROUP_LABELS))
        card = [c for c in out["items"] if c["ticket"] == "ABC-1001"][0]
        self.assertEqual((card["group"], card["action"]), ("planned", "Resume Task"))
        self.assertTrue(card["why"])
        self.assertTrue(out["completed"]["collapsed"])
        self.assertEqual(self.read("planner", group="planned")["items"][0]["ticket"], "ABC-1001")

    def test_detail_shows_stage_appropriate_actions_only(self):
        rec = self.planned()
        out = self.read("detail", workflow=rec["id"])
        names = [a["name"] for a in out["actions"]]
        self.assertIn("Execute Plan", names)
        self.assertNotIn("Resume Execution", names)
        self.assertNotIn("Complete", names)
        self.assertEqual(out["approved_label"], "No approved plan")
        self.assertNotIn(self.repo, json.dumps(out), "detail overview carries no absolute path")
        started = self.started(key="ABC-1002", repo=self.repo_b, sid="sess-2")
        paused = companion.pause(started["id"], started["revision"], "Switching")
        out = self.read("detail", workflow=paused["id"])
        names = [a["name"] for a in out["actions"]]
        self.assertIn("Resume Execution", names)
        self.assertNotIn("Execute Plan", names)
        self.assertNotIn("Pause", names)

    def test_review_offers_both_choices_with_one_recommendation(self):
        rec = self.planned(n=2)
        out = self.read("review", workflow=rec["id"])
        choices = {c["action"]: c for c in out["choices"]}
        self.assertEqual(set(choices), {"Improve Plan", "Execute Plan"})
        self.assertEqual(sum(1 for c in out["choices"] if c["recommended"]), 1)
        self.assertEqual(out["evaluation"]["label"], "Evaluation unavailable")
        self.assertIsNone(out["evaluation"]["percent"])
        self.assertTrue(choices["Improve Plan"]["recommended"], "ac2 is not mapped by the plan")
        self.assertFalse(choices["Improve Plan"]["modes"]["claude"]["available"])
        self.assertIn("companion_call_usd", choices["Improve Plan"]["modes"]["claude"]["reason"])
        self.assertEqual(out["execute"]["digest"], model.digest(companion.get(rec["id"])["plans"][rec["selected_plan"]]))
        rec = companionjobs.manual_assessment(rec["id"], rec["revision"], rec["selected_plan"],
                                              {"ac1": "Covered", "ac2": "Covered"}, "Reviewed by hand")
        out = self.read("review", workflow=rec["id"])
        self.assertEqual(out["assessment"]["origin"], "developer assessment")
        self.assertEqual(out["evaluation"]["label"], "Evaluation unavailable", "an assessment is never Jev")
        approved = self.approve(rec)
        out = self.read("review", workflow=approved["id"])
        choices = {c["action"]: c for c in out["choices"]}
        self.assertFalse(choices["Execute Plan"]["available"])
        self.assertIn("copy the prepared handoff", choices["Execute Plan"]["reason"])
        self.assertEqual(sum(1 for c in out["choices"] if c["recommended"]), 1)

    def test_handoff_is_prepared_not_submitted_with_the_exact_version(self):
        rec = self.handed_off()
        out = self.read("handoff", workflow=rec["id"])
        self.assertEqual(out["label"], "Prepared; not submitted")
        self.assertEqual(out["plan"], rec["approved_plan"])
        self.assertIn(plan_body()["text"], out["text"])
        self.assertEqual(out["plan_digest"], model.digest(companion.get(rec["id"])["plans"][rec["approved_plan"]]))
        rec = record_dispatch(rec["id"], rec["revision"], out["attempt"], "copied")
        self.assertEqual(self.read("handoff", workflow=rec["id"])["dispatch"], "prepared")
        self.assertEqual(self.read("handoff", workflow=self.planned(key="ABC-1002")["id"])["state"], "unavailable")

    def test_plan_pages_exact_text_with_digest(self):
        rec = self.create()
        long = "Exact plan line.\n" * 2000
        rec = companion.save_plan(rec["id"], rec["revision"], plan_body(text=long))
        pages, out = [], self.read("plan", workflow=rec["id"])
        pages.append(out["text"])
        while out["cursor"]:
            out = self.read("plan", workflow=rec["id"], cursor=out["cursor"])
            pages.append(out["text"])
        self.assertEqual("".join(pages), long)
        self.assertEqual(out["digest"], model.digest(companion.get(rec["id"])["plans"][rec["selected_plan"]]))

    def test_changes_have_provenance_page_and_never_acknowledge(self):
        rec = self.planned()
        self.put(commits={"c%d" % i: {"sha": "c%d" % i, "subject": "Work %d" % i, "date": self.stamp(minutes=i),
                                      "repo_name": "scratch"} for i in range(60)})
        rec = companion.checkpoint(rec["id"], rec["revision"])
        out = self.read("changes", workflow=rec["id"])
        self.assertEqual(out["total"], 60)
        self.assertTrue(out["more"])
        self.assertTrue(all(i["provenance"].startswith("commits:") for i in out["items"]))
        nxt = self.read("changes", workflow=rec["id"], cursor=out["cursor"])
        self.assertEqual(len(out["items"]) + len(nxt["items"]), 60)
        self.assertEqual(workflowstore.load(rec["id"])["acknowledged"], {})
        rec = companion.acknowledge(rec["id"], rec["revision"], out["unread"] + nxt["unread"])
        self.assertEqual(self.read("changes", workflow=rec["id"])["total"], 0)

    def test_verification_keeps_three_measures_apart(self):
        rec = self.started(n=2)
        out = self.read("verification", workflow=rec["id"])
        self.assertEqual(set(out["measures"]), {"plan_coverage", "implementation_progress", "functional_verification"})
        self.assertEqual(out["measures"]["functional_verification"]["percent"], 0.0)
        self.assertEqual(out["measures"]["plan_coverage"]["state"], "unavailable")
        self.assertTrue(all(r["applicability"] for r in out["rows"]))
        self.assertEqual({r["status"] for r in out["rows"]}, {"Unverified"})

    def test_jobs_are_local_state_and_database_context_is_bounded(self):
        rec = self.planned()
        out = self.read("jobs", workflow=rec["id"])
        self.assertEqual((out["active"], out["jobs"], out["poll_after_s"]), ([], [], 15))
        db = self.read("database", flow="f_0123456789ab")
        self.assertFalse(db["database_context"]["available"])
        self.assertEqual(self.forbidden, [], "no database MCP job")


class BatchAuthorization(ViewBase):
    """views.visibility applies exactly companion.authorize's rules, once per read."""

    def test_same_answer_as_companion_authorize(self):
        recs = [self.planned(), self.planned(key="ABC-1002", repo=self.repo_b)]
        variants = [self.cfg]
        for change in ("repos", "account", "include"):
            cfg = json.loads(json.dumps(self.cfg))
            if change == "repos":
                cfg["profile"]["repos"] = [self.repo]
            elif change == "account":
                cfg["profile"]["accounts"][0]["instance"] = "https://other.acme.test"
            else:
                cfg["profile"]["include_tickets"] = {"ABC-1001": "My task"}
            variants.append(cfg)
        for cfg in variants:
            write_config(common.home(), cfg)
            loaded = common.load_config()
            ok = views.visibility(loaded)
            for rec in recs:
                try:
                    companion.authorize(rec, loaded)
                    expected = True
                except (PermissionError, ValueError):
                    expected = False
                self.assertEqual(ok(rec["binding"]), expected, (cfg["profile"], rec["binding"]["ticket"]))
        write_config(common.home(), variants[1])  # a scope edit changes the generation: both need reconciliation
        hidden = {}
        self.assertEqual(views.index(common.load_config(), hidden), {})
        self.assertEqual(hidden, {"ABC-1001": 1, "ABC-1002": 1}, "counted, never shown")


class Bridge(ViewBase):
    def call(self, server, name, arguments):
        return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})

    def test_bridge_exposes_one_read_only_tool_and_refuses_everything_else(self):
        rec = self.planned()
        server = companion_mcp.Server()
        tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["companion_read"])
        self.assertTrue(tools[0]["annotations"]["readOnlyHint"])
        self.assertEqual(tools[0]["inputSchema"]["properties"]["operation"]["enum"], list(views.OPERATIONS))
        ok = self.call(server, "companion_read", {"operation": "detail", "workflow": rec["id"]})["result"]
        self.assertFalse(ok["isError"])
        self.assertEqual(ok["structuredContent"]["workflow"], rec["id"])
        for name, args in (("companion_write", {}), ("companion_read", {"operation": "approve"}),
                           ("get_ticket", {"key": "ABC-1001"}), ("companion_read", {"operation": "snapshot", "x": 1})):
            reply = self.call(server, name, args)
            self.assertTrue("error" in reply or reply["result"]["isError"], name)
        big = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "companion_read", "arguments": {"operation": "snapshot",
                                                                             "cursor": "x" * 9000}}})
        self.assertEqual(companion_mcp.handle_line(server, big)["error"]["message"], "Request too large")
        self.assertEqual(workflowstore.load(rec["id"])["revision"], rec["revision"])


class Parity(ViewBase):
    """A77: the same generation yields the same stage, coverage and evidence on every surface."""

    def test_dashboard_cli_and_bridge_agree(self):
        from chitragupta.dashboard import companion_api
        rec = self.started()
        direct = self.read("detail", workflow=rec["id"])
        dash = companion_api.read("detail", rec["id"])
        bridge = companion_mcp.Server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "companion_read", "arguments": {"operation": "detail", "workflow": rec["id"]}}})
        bridged = bridge["result"]["structuredContent"]
        for view in (dash, bridged):
            for k in ("stage", "situation", "measures", "verification", "profile_generation", "resume_id"):
                self.assertEqual(view[k], direct[k], k)


class CliText(ViewBase):
    """MOD-07 text fallback: same projections, read-only, never the global capture resume."""

    def cli(self, *argv):
        from chitragupta import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["companion", *argv])
        return code, buf.getvalue()

    def test_text_commands_read_only_and_never_global_resume(self):
        from chitragupta import cli
        rec = self.handed_off()
        self.planned(key="ABC-1002", repo=self.repo_b)
        before = tree(self.home, self.user_dir)
        stack, calls = self.side_effect_free()
        with stack, mock.patch.object(cli, "cmd_resume", lambda a: calls.append("global resume")), \
                mock.patch.object(cli, "cmd_pause", lambda a: calls.append("global pause")):
            code, text = self.cli("planner")
            self.assertEqual(code, 0)
            self.assertIn("Daily Planner", text)
            self.assertIn("ABC-1001", text)
            self.assertIn("Why:", text)
            code, text = self.cli("resume", "ABC-1001")
            self.assertEqual(code, 0)
            self.assertIn("does not resume capture", text)
            self.assertIn("Next action: Copy approved plan", text)
            self.assertIn("Functional verification", text)
            code, text = self.cli("review", rec["id"])
            self.assertIn("Execute Plan: unavailable", text)
            self.assertIn("Evaluation unavailable", text)
            code, text = self.cli("handoff", rec["id"])
            self.assertIn("Prepared; not submitted", text)
            self.assertIn(plan_body()["text"], text)
            code, text = self.cli("verification", rec["id"])
            self.assertIn("Functional verification", text)
            code, text = self.cli("resume", "ABC-9999")
            self.assertEqual(code, 2)
            self.assertIn("Not available", text)
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(common.wl("PAUSED")))
        self.assertEqual(before, tree(self.home, self.user_dir))

    def test_several_attempts_are_listed_never_picked(self):
        self.planned()
        self.planned(repo=self.repo_b)
        code, text = self.cli("resume", "ABC-1001")
        self.assertEqual(code, 0)
        self.assertIn("2 task attempts; which one?", text)
        self.assertNotIn("Next action", text)


# ---------------------------------------------------------------- A76 bounded scale

class Scale(unittest.TestCase):
    """1,000 tasks, 100 plan versions each, written through workflowstore once for the class."""
    TASKS, VERSIONS = 1000, 100

    @classmethod
    def setUpClass(cls):
        cls.saved = {k: v for k, v in os.environ.items() if k.startswith(("CHITRAGUPTA_", "WORKLOG_"))}
        cls.home = tempfile.mkdtemp(prefix="cg-scale-")
        cls.user = tempfile.mkdtemp(prefix="cg-scale-user-")
        os.environ.update(CHITRAGUPTA_HOME=cls.home, CHITRAGUPTA_USER_DIR=cls.user,
                          CHITRAGUPTA_CLAUDE_DIR=os.path.join(cls.user, "claude"), CHITRAGUPTA_NO_TASK="1")
        views._SLIM.clear()
        views._JIRA.clear()
        repo = os.path.realpath(os.path.join(cls.home, "scratch"))
        os.makedirs(repo)
        keys = ["ABC-%d" % (10000 + i) for i in range(cls.TASKS)]
        cfg = {"profile": {"accounts": [{"provider": "jira", "instance": JIRA, "id": "dev-a"}], "repos": [repo],
                           "include_tickets": {k: "Task" for k in keys}},
               "watched_repos": [repo], "git_author_emails": ["dev@acme.test"]}
        write_config(cls.home, cfg)
        t0 = time.perf_counter()
        sig = profile.signature(common.load_config())
        base = model.baseline([{"id": "ac%d" % i, "text": "Expected behavior %d" % i} for i in range(1, 4)])
        stages = ["planned", "review_incomplete", "plan_reviewed", "new", "paused", "completed", "in_development",
                  "awaiting_verification", "pr_review"]
        plans, parent = {}, None
        for v in range(cls.VERSIONS):  # one version chain, stored in every task (each record holds 100 versions)
            p = model.plan(plan_body(text="Version %d of the plan, with its exact reviewed wording." % v), base, parent)
            plans[p["id"]] = p
            parent = p["id"]
        for n, key in enumerate(keys):
            common.write_json(store.ticket_path(key), store.new_ticket(key))
            binding = {"ticket": key, "jira_instance": JIRA, "issue": key, "repo_id": "repo:scratch",
                       "location": repo, "profile_generation": sig}
            stage = stages[n % len(stages)]
            rec = model.normalize({"id": workflowstore.identity(binding), "binding": binding, "stage": stage,
                                   "revision": 0, "plans": plans, "evaluations": {}, "selected_plan": parent,
                                   "approved_plan": None, "approvals": [], "evidence": [], "decisions": [],
                                   "jobs": {}, "acknowledged": {}, "ingested": {}, "summary": "Synthetic",
                                   "next_action": "Review", "reason": "fixture", "session": None,
                                   "baseline": base["id"], "baselines": {base["id"]: base}, "context": {},
                                   "resume_stage": "in_development" if stage == "paused" else None})
            workflowstore.save(rec, 0, lambda r: None)
        cls.build_s = time.perf_counter() - t0
        cls.wids = workflowstore.ids()

    @classmethod
    def tearDownClass(cls):
        for k in [k for k in os.environ if k.startswith(("CHITRAGUPTA_", "WORKLOG_"))]:
            del os.environ[k]
        os.environ.update(cls.saved)
        views._SLIM.clear()
        rmtree(cls.home)
        rmtree(cls.user)

    def timed(self, fn, n):
        out, times = None, []
        for _ in range(n):
            t = time.perf_counter()
            out = fn()
            times.append((time.perf_counter() - t) * 1000)
        times.sort()
        return out, statistics.median(times), times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))]

    def test_scale_bounds_and_timings(self):
        self.assertEqual(len(self.wids), self.TASKS)
        t = time.perf_counter()
        views.read({"operation": "snapshot"})
        cold = (time.perf_counter() - t) * 1000
        snap, s50, s95 = self.timed(lambda: views.read({"operation": "snapshot"}), 20)
        plan, p50, p95 = self.timed(lambda: views.read({"operation": "planner"}), 20)
        wid = self.wids[len(self.wids) // 2]
        det, d50, d95 = self.timed(lambda: views.read({"operation": "detail", "workflow": wid}), 3)
        rev, r50, r95 = self.timed(lambda: views.read({"operation": "review", "workflow": wid}), 10)
        for out in (snap, plan, det, rev):
            self.assertLessEqual(len(json.dumps(out).encode()), views.MAX_BYTES)
        # Regression guard, not the 500 ms target: a per-workflow full load in resume() took ~13 s here.
        for name, p95_ms in (("snapshot", s95), ("planner", p95), ("detail", d95), ("review", r95)):
            self.assertLess(p95_ms, 2000, "%s p95 %.0f ms at %d tasks" % (name, p95_ms, self.TASKS))
        self.assertLessEqual(len(snap["items"]), views.PAGE_SIZE)
        self.assertLessEqual(len(plan["items"]), views.PAGE_SIZE)
        self.assertTrue(plan["more"] and plan["cursor"])
        self.assertEqual(det["plan_versions_total"], self.VERSIONS)
        self.assertLessEqual(len(det["plan_versions"]), 20)
        seen, cur, pages = set(), None, 0
        while True:
            page = views.read(dict({"operation": "planner"}, **({"cursor": cur} if cur else {})))
            seen |= {c["ticket"] for c in page["items"]}
            pages += 1
            cur = page["cursor"]
            if not cur:
                break
        completed = plan["completed"]["count"]
        self.assertEqual(len(seen) + completed, self.TASKS)
        report = ("A76 scale (%d tasks x %d plan versions, fixture built in %.1f s): snapshot cold %.0f ms, "
                  "p50 %.0f / p95 %.0f ms; planner p50 %.0f / p95 %.0f ms (%d pages); detail p50 %.0f / p95 %.0f ms; "
                  "review p50 %.0f / p95 %.0f ms" % (self.TASKS, self.VERSIONS, self.build_s, cold, s50, s95, p50, p95,
                                                     pages, d50, d95, r50, r95))
        print("\n" + report)
        path = os.environ.get("CG_SCALE_REPORT")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(report + "\n")


if __name__ == "__main__":
    unittest.main()
