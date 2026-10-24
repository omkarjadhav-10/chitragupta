"""Which ticket a prompt, an event and an extracted item belong to (K2 pasted breadcrumbs and parent issue types).

Synthetic data only (tickets ABC-…, schema `shop`).
"""
import datetime as dt
import json
import os
import tempfile
import unittest

from tests.support import Isolated, rd, rmtree, run_hook, write_config

D = "2026-10-14"


def at(hh, mm, day=D):
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm)).astimezone()


def ev(event, hh, mm, sid="s1", branch=None, prompt=None, repo=None, **kw):
    e = {"ts": at(hh, mm).isoformat(timespec="seconds"), "event": event, "session_id": sid, "source": "hook",
         "branch": branch, "repo_root": repo, "cwd": repo, "payload": {}}
    if prompt is not None:
        e["payload"] = {"prompt": prompt}
    e.update(kw)
    return e


class AttrCase(Isolated, unittest.TestCase):
    def setUp(self):
        h = tempfile.mkdtemp(prefix="cg-test-")
        write_config(h, {"ticket_prefixes": ["ABC"]})
        self.isolate(h)
        self.work = tempfile.mkdtemp(prefix="cg-work-")
        self.addCleanup(rmtree, self.work)

    def card(self, key, type_, parent=""):
        from chitragupta import common
        common.write_json(common.hp("data", "jira", key + ".json"),
                          {"key": key, "card": {"found": True, "title": "t", "status": "Open", "type": type_,
                                                "parent": parent}})

    def repo(self, name, branch):
        d = os.path.join(self.work, name)
        os.makedirs(os.path.join(d, ".git"))
        with open(os.path.join(d, ".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/%s\n" % branch)
        return d


# --------------------------------------------------------------------------- K2 breadcrumbs and pasted text

class PromptTicketTests(AttrCase):
    def pt(self, text):
        from chitragupta import common
        return common.prompt_ticket(text, ["ABC"], common.load_config())

    def test_pasted_jira_breadcrumb_gives_the_last_id(self):
        paste = "<pasted_content>\nSpaces\nShop\n\nABC-1000\n\nABC-1001\n\nOrders not linked\n</pasted_content>"
        self.assertEqual(self.pt(paste), ("ABC-1001", "jira-title"))
        self.assertEqual(self.pt("ABC-1000 / ABC-1001"), ("ABC-1001", "jira-title"))
        self.assertEqual(self.pt("ABC-1000 > ABC-1001"), ("ABC-1001", "jira-title"))
        self.assertEqual(self.pt("Spaces / Shop / ABC-1000 / ABC-1001\nOrders not linked\nwhy?"), ("ABC-1001", "jira-title"))

    def test_own_words_beat_a_pasted_block(self):
        self.assertEqual(self.pt("fix ABC-1002 the same way:\n<pasted_content>\nABC-1001 stack trace\n</pasted_content>"),
                         ("ABC-1002", "prompt"))
        self.assertEqual(self.pt("ABC-1000 / ABC-1001\nnow compare with ABC-1003"), ("ABC-1003", "prompt"))
        self.assertEqual(self.pt("look at ABC-1002 and ABC-1003"), ("ABC-1002", "prompt"))
        self.assertEqual(self.pt("no ticket here"), (None, None))

    def test_parent_issue_types_never_win(self):
        self.card("ABC-1000", "Epic")
        self.assertEqual(self.pt("ABC-1000 overview"), (None, None))
        self.assertEqual(self.pt("ABC-1000 overview, then ABC-1001"), ("ABC-1001", "prompt"))
        self.card("ABC-1005", "Story", parent="ABC-1004 (Shop rework)")
        self.assertEqual(self.pt("ABC-1004 and ABC-1005"), ("ABC-1005", "prompt"))  # named as another one's parent
        self.card("ABC-1000", "Bug")
        self.assertEqual(self.pt("ABC-1000 overview"), ("ABC-1000", "prompt"))

    def test_parent_types_are_configurable(self):
        from chitragupta import common
        self.card("ABC-1000", "Feature")
        cfg = common.load_config()
        self.assertEqual(cfg["jira"]["parent_issue_types"], ["Epic", "Initiative"])
        self.assertEqual(common.prompt_ticket("ABC-1000", ["ABC"], cfg), ("ABC-1000", "prompt"))
        cfg["jira"]["parent_issue_types"] = ["feature"]
        self.assertEqual(common.prompt_ticket("ABC-1000", ["ABC"], cfg), (None, None))
        from chitragupta import settings
        clean, errors = settings.validate({"jira.parent_issue_types": ["Epic", "Initiative", "Feature Set"]})
        self.assertEqual(errors, {})
        self.assertIn("jira.parent_issue_types", settings.validate({"jira.parent_issue_types": ["<b>"]})[1])


class ParentNeverGetsWorkTests(AttrCase):
    def test_breadcrumb_attribution_and_minutes(self):
        from chitragupta import agent
        self.card("ABC-1000", "Epic")
        evs = [ev("user-prompt", 10, 0, branch="qa", prompt="ABC-1000 / ABC-1001\nOrders not linked"),
               ev("post-tool", 10, 5, branch="qa"), ev("stop", 10, 9, branch="qa"),
               ev("user-prompt", 11, 0, sid="s2", branch="qa", prompt="ABC-1000 overview please"),
               ev("post-tool", 11, 5, sid="s2", branch="qa")]
        out = agent.attribute([dict(e) for e in evs], ["ABC"])
        self.assertEqual([e["_t"] for e in out], ["ABC-1001"] * 3 + [None, None])

    def test_recompute_minutes_creates_no_parent_slot(self):
        from chitragupta import agent, common
        self.card("ABC-1000", "Epic")
        p = common.wl("raw", D, "s1.jsonl")
        os.makedirs(os.path.dirname(p))
        with open(p, "w", encoding="utf-8") as f:
            for e in (ev("user-prompt", 10, 0, branch="qa", prompt="ABC-1000 overview"), ev("post-tool", 10, 5, branch="qa"),
                      ev("stop", 10, 9, branch="qa")):
                f.write(json.dumps(e) + "\n")
        a = agent.Agent()
        a.recompute_minutes(D)
        self.assertNotIn("ABC-1000", a.store.day(D)["tickets"])

    def test_model_item_for_a_parent_goes_to_the_turn_ticket(self):
        from chitragupta import store as storemod
        from chitragupta.store import Store
        self.card("ABC-1000", "Epic")
        st = Store()
        res = {"items": [{"kind": "finding", "title": "Orders lose their link on save", "ticket": "ABC-1000"}],
               "queries": [{"text": "select 1 from shop.orders", "ticket": "ABC-1000"}]}
        storemod.merge_extraction(st, res, {"day": D, "session": "s1", "time": "10:00", "repo": None, "branch": "qa",
                                            "candidates": ["ABC-1001"]}, ["ABC"])
        self.assertNotIn("ABC-1000", st.day(D)["tickets"])
        self.assertEqual(len(st.ticket("ABC-1001")["items"]), 1)
        self.assertEqual(len(st.ticket("ABC-1001")["queries"]), 1)

    def test_merge_only_into_selected_tickets(self):
        """A rebuild of some tickets (K12) merges nothing into the others, or into no ticket; it counts what it left."""
        from chitragupta import store as storemod
        from chitragupta.store import Store
        st = Store()
        res = {"activity_summary": "looked at refunds",
               "items": [{"kind": "finding", "title": "Cancel keeps stock", "ticket": "ABC-1001"},
                         {"kind": "finding", "title": "Refund rounds down", "ticket": "ABC-1002"},
                         {"kind": "finding", "title": "Untagged", "time": "09:00"}],
               "queries": [{"text": "select 1 from shop.refunds", "ticket": "ABC-1002"}],
               "tables": [{"name": "shop.refunds", "ticket": "ABC-1002"}],
               "apis": [{"method": "GET", "path": "/refunds", "ticket": "ABC-1002"}],
               "files": [{"path": "src/Refund.java", "ticket": "ABC-1002"}]}
        ctx = {"day": D, "session": "s1", "time": "10:00", "repo": None, "branch": "qa", "candidates": ["ABC-1001"],
               "default": None, "turns": [("10:00", "ABC-1001", "prompt")]}
        storemod.merge_extraction(st, res, ctx, ["ABC"], only={"ABC-1001"})
        self.assertEqual(sorted(i["title"] for i in st.ticket("ABC-1001")["items"].values()), ["Cancel keeps stock"])
        self.assertNotIn("ABC-1002", st.day(D)["tickets"])
        self.assertEqual(st.day(D)["other"], {})
        self.assertEqual(ctx["ignored"], {"ABC-1002": 5, "-": 1})
        self.assertNotIn("shop.refunds", st.catalog()["tables"])

    def test_hook_files_the_breadcrumb_child(self):
        repo = self.repo("shop-api", "qa")
        run_hook("user-prompt", {"session_id": "h1", "cwd": repo, "user_prompt": "ABC-1000 / ABC-1001\nwhy?"},
                 self.child_env())
        from chitragupta import common
        rows = [json.loads(l) for l in rd(common.wl("raw", common.workday(common.now(), common.load_config()),
                                                     "h1.jsonl")).splitlines()]
        self.assertEqual(rows[0]["ticket"], "ABC-1001")


# --------------------------------------------------------------------------- K1 one ticket per turn

class TurnAttributionTests(AttrCase):
    def att(self, evs):
        from chitragupta import agent
        out = agent.attribute([dict(e) for e in evs], ["ABC"])
        return [e["_t"] for e in out], [e["_src"] for e in out]

    def test_prompt_beats_branch_then_branch_returns(self):
        b = "fix/ABC-1001-x"
        t, src = self.att([ev("user-prompt", 10, 0, branch=b, prompt="look at ABC-1002 login"),
                           ev("post-tool", 10, 2, branch=b), ev("stop", 10, 5, branch=b),
                           ev("user-prompt", 10, 19, branch=b, prompt="continue"), ev("post-tool", 10, 20, branch=b)])
        self.assertEqual(t, ["ABC-1002"] * 3 + ["ABC-1001"] * 2)
        self.assertEqual(src, ["prompt"] * 3 + ["branch"] * 2)

    def test_release_and_base_branches_never_win(self):
        b = "release/ABC-1003/nov"
        t, src = self.att([ev("user-prompt", 10, 0, branch=b, prompt="RCA of ABC-1002"), ev("post-tool", 10, 2, branch=b),
                           ev("user-prompt", 10, 30, branch=b, prompt="how to reproduce"), ev("post-tool", 10, 31, branch=b)])
        self.assertEqual(t, ["ABC-1002"] * 4)
        self.assertEqual(src, ["prompt", "prompt", "carry", "carry"])
        t, _ = self.att([ev("user-prompt", 11, 0, sid="s3", branch="qa", prompt="go"), ev("post-tool", 11, 1, sid="s3", branch="qa")])
        self.assertEqual(t, [None, None])

    def test_another_repos_branch_is_not_taken(self):
        x, y = os.path.join(self.work, "shop-api"), os.path.join(self.work, "shop-ui")
        t, src = self.att([ev("user-prompt", 10, 0, branch="fix/ABC-1001-x", repo=x, prompt="go on"),
                           ev("post-tool", 10, 1, branch="fix/ABC-1001-x", repo=x),
                           ev("post-tool", 10, 2, branch="fix/ABC-1368-ui", repo=y),
                           ev("stop", 10, 3, branch="fix/ABC-1001-x", repo=x)])
        self.assertEqual(t, ["ABC-1001"] * 4)
        self.assertEqual(src, ["branch"] * 4)

    def test_ticket_for_turn_order(self):
        from chitragupta import common
        cfg = common.load_config()
        self.assertEqual(common.ticket_for_turn(cfg, "ABC-1002", "prompt", "fix/ABC-1001", carry="ABC-1009"),
                         ("ABC-1002", "prompt"))
        self.assertEqual(common.ticket_for_turn(cfg, None, None, "fix/ABC-1001", carry="ABC-1009"), ("ABC-1001", "branch"))
        self.assertEqual(common.ticket_for_turn(cfg, None, None, "release/ABC-1003/x", carry="ABC-1009"),
                         ("ABC-1009", "carry"))
        self.assertEqual(common.ticket_for_turn(cfg, None, None, "uat"), (None, None))
        self.assertEqual(common.ticket_for_turn(cfg, None, None, "fix/ABC-1001", repo="/w/ui", own_repo="/w/api",
                                                carry="ABC-1009"), ("ABC-1009", "carry"))

    def test_hook_writes_ticket_src(self):
        from chitragupta import common
        repo = self.repo("shop-api", "fix/ABC-1001-x")
        env = self.child_env()
        run_hook("user-prompt", {"session_id": "h2", "cwd": repo, "user_prompt": "look at ABC-1002"}, env)
        run_hook("post-tool", {"session_id": "h2", "cwd": repo, "tool_name": "Read", "tool_input": {"file_path": "a.py"}}, env)
        rows = [json.loads(l) for l in rd(common.wl("raw", common.workday(common.now(), common.load_config()),
                                                     "h2.jsonl")).splitlines()]
        self.assertEqual([(r["ticket"], r["ticket_src"]) for r in rows], [("ABC-1002", "prompt"), ("ABC-1001", "branch")])


class TurnExtractionTests(AttrCase):
    def setUp(self):
        super().setUp()
        from unittest import mock
        from chitragupta import common, extract
        self.calls = []
        self.answers = []

        def fake(kind, stdin_text, cfg, timeout=300):
            self.calls.append(stdin_text)
            return (self.answers.pop(0) if self.answers else {}), 0.0
        for p in (mock.patch.object(extract, "call_claude", fake),
                  mock.patch.object(common, "now", lambda: at(23, 0))):
            p.start()
            self.addCleanup(p.stop)
        self.repo_dir = self.repo("shop-api", "fix/ABC-1001-x")

    def two_turns(self, big=0):
        b, r = "fix/ABC-1001-x", self.repo_dir
        return [ev("user-prompt", 10, 0, branch=b, repo=r, prompt="look at ABC-1002 orders"),
                ev("post-tool", 10, 1, branch=b, repo=r, payload={"tool": "Edit", "input": {"file_path": "shop/orders.py"}}),
                ev("stop", 10, 5, branch=b, repo=r, payload={"message": "Orders lose the link. " + "a" * big}),
                ev("user-prompt", 10, 20, branch=b, repo=r, prompt="now the login part"),
                ev("post-tool", 10, 21, branch=b, repo=r, payload={"tool": "Edit", "input": {"file_path": "shop/login.py"}}),
                ev("stop", 10, 25, branch=b, repo=r, payload={"message": "Login checks the lock first. " + "b" * big})]

    def write(self, evs, sid="s1"):
        from chitragupta import common
        p = common.wl("raw", D, sid + ".jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            for e in evs:
                f.write(json.dumps(dict(e, session_id=sid)) + "\n")

    def test_items_follow_their_turn(self):
        from chitragupta import agent
        self.write(self.two_turns(big=1000))
        self.answers = [{"items": [{"kind": "finding", "title": "Orders lose their link", "ticket": None, "time": "10:05"},
                                   {"kind": "finding", "title": "Login checks the lock first", "ticket": None, "time": "10:25"}]}]
        a = agent.Agent()
        a.ingest()
        a.run_extractions(any_size=True)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("TURN ticket=ABC-1002 (prompt)", self.calls[0])
        self.assertIn("TURN ticket=ABC-1001 (branch)", self.calls[0])
        i2 = list(a.store.ticket("ABC-1002")["items"].values())
        i1 = list(a.store.ticket("ABC-1001")["items"].values())
        self.assertEqual([(i["title"], i["src"]) for i in i2], [("Orders lose their link", "prompt")])
        self.assertEqual([(i["title"], i["src"]) for i in i1], [("Login checks the lock first", "branch")])
        dd = a.store.day(D)
        self.assertGreater(dd["tickets"]["ABC-1002"]["minutes"], 0)
        self.assertGreater(dd["tickets"]["ABC-1001"]["minutes"], 0)

    def test_chunks_split_at_turn_boundaries(self):
        from unittest import mock
        from chitragupta import agent, extract
        evs = agent.attribute([dict(e, session_id="s1") for e in self.two_turns(big=3000)], ["ABC"])
        with mock.patch.object(extract, "DIGEST_CAP", 5000):
            chunks = extract.build_digests(evs, {"session": "s1", "day": D}, ["ABC-1002", "ABC-1001"], {})
        self.assertEqual(len(chunks), 2)
        self.assertEqual([c.default for c in chunks], ["ABC-1002", "ABC-1001"])
        self.assertIn("TURN ticket=ABC-1002 (prompt)", chunks[0])
        self.assertNotIn("now the login part", chunks[0])
        self.assertIn("now the login part", chunks[1])
        self.assertNotIn("ABC-1002 (prompt)", chunks[1])

    def test_a_long_turn_still_splits_and_keeps_its_ticket(self):
        from unittest import mock
        from chitragupta import agent, extract
        b, r = "fix/ABC-1001-x", self.repo_dir
        evs = [ev("user-prompt", 10, 0, branch=b, repo=r, prompt="look at ABC-1002")] + \
              [ev("stop", 10, i, branch=b, repo=r, payload={"message": "c" * 2000}) for i in range(1, 6)]
        evs = agent.attribute([dict(e, session_id="s1") for e in evs], ["ABC"])
        with mock.patch.object(extract, "DIGEST_CAP", 5000):
            chunks = extract.build_digests(evs, {"session": "s1", "day": D}, ["ABC-1002"], {})
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.default == "ABC-1002" and "TURN ticket=ABC-1002 (prompt)" in c for c in chunks))

    def test_chunk_default_is_used_for_untimed_items(self):
        from unittest import mock
        from chitragupta import agent, extract
        self.write(self.two_turns(big=3000))
        self.answers = [{"items": [{"kind": "finding", "title": "First chunk finding", "ticket": None}]},
                        {"items": [{"kind": "finding", "title": "Second chunk finding", "ticket": None}]}]
        with mock.patch.object(extract, "DIGEST_CAP", 5000):
            a = agent.Agent()
            a.ingest()
            a.run_extractions(any_size=True)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([i["title"] for i in a.store.ticket("ABC-1002")["items"].values()], ["First chunk finding"])
        self.assertEqual([i["title"] for i in a.store.ticket("ABC-1001")["items"].values()], ["Second chunk finding"])

    def test_summary_pending_fallback_keeps_turn_tickets(self):
        from chitragupta import agent
        self.write(self.two_turns())
        a = agent.Agent()
        a.ingest()
        a.refresh_fallbacks()
        fb = {r["ticket"]: r["files"] for r in a.store.day(D)["fallback"].values()}
        self.assertEqual(fb, {"ABC-1002": ["shop/orders.py"], "ABC-1001": ["shop/login.py"]})
        self.answers = [{"items": []}]
        a.run_extractions(any_size=True)
        self.assertEqual(a.store.day(D)["fallback"], {})  # extraction clears every turn's entry

    def test_recent_tickets_follow_the_turn(self):
        from chitragupta import agent
        self.write(self.two_turns())
        a = agent.Agent()
        a.ingest()
        self.assertEqual([t for _, t in a.state["recent_tickets"]], ["ABC-1002"] * 3 + ["ABC-1001"] * 3)


class SafetyNetTurnTests(AttrCase):
    def test_safety_net_uses_the_prompt_ticket(self):
        import time
        from chitragupta import agent, common
        tdir = os.path.join(self.work, "transcripts")
        os.environ["CHITRAGUPTA_TRANSCRIPTS_DIR"] = tdir
        repo = self.repo("shop-api", "fix/ABC-1001-x")
        ts = common.now_iso()
        recs = [{"type": "user", "cwd": repo, "sessionId": "t1", "timestamp": ts, "gitBranch": "fix/ABC-1001-x",
                 "message": {"role": "user", "content": "check ABC-1002 first"}},
                {"type": "assistant", "cwd": repo, "sessionId": "t1", "timestamp": ts, "gitBranch": "fix/ABC-1001-x",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}}]
        p = os.path.join(tdir, "proj", "t1.jsonl")
        os.makedirs(os.path.dirname(p))
        with open(p, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))
        old = time.time() - 600
        os.utime(p, (old, old))
        a = agent.Agent()
        a.state["transcripts_baselined"] = True
        a.safety_net(False)
        rows = [json.loads(l) for l in rd(common.wl("raw", common.workday(common.now(), a.cfg), "t1.jsonl")).splitlines()]
        self.assertEqual({(r["ticket"], r["ticket_src"]) for r in rows}, {("ABC-1002", "prompt")})


if __name__ == "__main__":
    unittest.main()
