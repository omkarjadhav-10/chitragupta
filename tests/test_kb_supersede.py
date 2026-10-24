"""K3: the extractor sees every candidate ticket's recent items (and earlier chunks'), git-action TODOs close from
branch and PR data, one per-ticket supersession call at day close marks replaced items, and the hub shows only
current items with the rest collapsed and labelled.

Synthetic data only (tickets ABC-…, schema `shop`).
"""
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.support import Isolated, rd, rmtree, write_config

D = "2026-09-01"
T = "ABC-1001"
BR = "feature/ABC-1001-x"


def at(hh, mm, day=D):
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm)).astimezone()


def git_merged(n=7, target="qa", merged="2026-09-02T10:00:00Z"):
    return {"repos": {"shop-api": {
        "branches": [{"branch": BR, "on_remote": True, "unpushed": 0, "own": 2, "status": "merged"}],
        "prs": [{"number": n, "source": BR, "target": target, "state": "merged", "created_at": "2026-09-02T09:00:00Z",
                 "merged_at": merged}]}}}


class Base(Isolated, unittest.TestCase):
    def setUp(self):
        h = tempfile.mkdtemp(prefix="cg-test-")
        write_config(h, {"ticket_prefixes": ["ABC"]})
        self.isolate(h)
        from chitragupta import common, modelbudget
        modelbudget._PENDING.clear()
        p = mock.patch.object(common, "now", lambda: at(23, 0, "2026-09-03"))
        p.start()
        self.addCleanup(p.stop)

    def item(self, tk, iid, kind, title, date=D, time="10:00", **kw):
        tk["items"][iid] = dict({"id": iid, "kind": kind, "title": title, "detail": "", "evidence": [], "date": date,
                                 "time": time, "first_date": date, "confidence": "likely", "session": "s1"}, **kw)


class AlreadyRecordedTests(Base):
    def test_budget_is_split_per_ticket_newest_first(self):
        from chitragupta import extract, store
        st = store.Store()
        for t in ("ABC-1001", "ABC-1002"):
            tk = st.ticket(t)
            for n in range(100):
                self.item(tk, "%s-%03d" % (t[-1], n), "finding", "%s finding number %03d about shop.orders %s" % (t, n, "x" * 120),
                          date="2026-08-%02d" % (1 + n % 28), time="%02d:%02d" % (n // 60, n % 60))
        already = store.already_recorded(st, ["ABC-1001", "ABC-1002"])
        self.assertLessEqual(len(json.dumps(already, ensure_ascii=False)), 20000)
        for t in ("ABC-1001", "ABC-1002"):
            self.assertTrue(already[t]["items"], t)
            dates = [i["id"] for i in already[t]["items"]]
            self.assertEqual(already[t]["items"][0]["id"], max(st.ticket(t)["items"].values(),
                                                               key=lambda i: (i["date"], i["time"]))["id"], dates[:3])
        head = extract.build_digests([{"ts": at(10, 0).isoformat(), "event": "stop", "payload": {"message": "m"}}],
                                     {"session": "s1", "day": D}, ["ABC-1001", "ABC-1002"], already)[0]
        self.assertIn('"ABC-1002"', head)

    def test_superseded_and_archived_items_are_left_out(self):
        from chitragupta import store
        st = store.Store()
        tk = st.ticket(T)
        self.item(tk, "a", "root_cause", "Old cause", status="superseded", superseded_by="b")
        self.item(tk, "b", "root_cause", "New cause")
        self.item(tk, "c", "finding", "Moved away", archived={"at": "x", "reason": "moved to ABC-1002"})
        self.assertEqual([i["id"] for i in store.already_recorded(st, [T])[T]["items"]], ["b"])


class ChunkRefreshTests(Base):
    def test_a_later_chunk_sees_the_earlier_chunks_items(self):
        from chitragupta import agent, common, extract
        work = tempfile.mkdtemp(prefix="cg-work-")
        self.addCleanup(rmtree, work)
        repo = os.path.join(work, "shop-api")
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/%s\n" % BR)
        p = common.wl("raw", D, "s1.jsonl")
        os.makedirs(os.path.dirname(p))
        evs = [{"ts": at(10, 0).isoformat(), "event": "user-prompt", "session_id": "s1", "source": "hook", "branch": BR,
                "repo_root": repo, "cwd": repo, "payload": {"prompt": "look at ABC-1001"}}]
        evs += [{"ts": at(10, i).isoformat(), "event": "stop", "session_id": "s1", "source": "hook", "branch": BR,
                 "repo_root": repo, "cwd": repo, "payload": {"message": "m%d " % i + "c" * 2500}} for i in range(1, 5)]
        with open(p, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(e) + "\n" for e in evs))
        seen = []

        def fake(kind, text, cfg, timeout=300):
            seen.append(text)
            return ({"items": [{"kind": "finding", "title": "Chunk one found the orders link", "ticket": T}]}
                    if len(seen) == 1 else {}), 0.0
        with mock.patch.object(extract, "call_claude", fake), mock.patch.object(extract, "DIGEST_CAP", 6000):
            a = agent.Agent()
            a.ingest()
            a.run_extractions(any_size=True)
        self.assertGreater(len(seen), 1)
        self.assertNotIn("Chunk one found the orders link", seen[0])
        self.assertIn("Chunk one found the orders link", seen[1].split("CURRENT_WORKLOG_LINES")[0])


class TodoClosureTests(Base):
    def test_push_todo_closes_when_the_pr_merged(self):
        from chitragupta import render, store, supersede
        st = store.Store()
        tk = st.ticket(T)
        self.item(tk, "t1", "todo", "Push %s and open the PR" % BR)
        self.item(tk, "t2", "todo", "Ask QA to retest the cancel flow")
        self.item(tk, "t3", "todo", "Cherry-pick the fix to release/2026.09")
        tk["git"] = git_merged()
        self.assertEqual(supersede.close_todos(tk), 1)
        self.assertEqual((tk["items"]["t1"]["status"], tk["items"]["t1"]["closed_by"]), ("done", "pr:7"))
        self.assertNotIn("status", tk["items"]["t2"])
        self.assertNotIn("status", tk["items"]["t3"])  # its target was never reached
        md = render.render_ticket(st, T)
        sec = md[md.index("## Open questions"):md.index("## Code map")]
        self.assertIn("Ask QA to retest", sec)
        self.assertIn("Done (1)", sec)
        self.assertLess(sec.index("Done (1)"), sec.index("Push %s" % BR))
        self.assertIn("pr:7", sec)

    def test_a_todo_written_after_the_pr_stays_open(self):
        from chitragupta import store, supersede
        st = store.Store()
        tk = st.ticket(T)
        self.item(tk, "t1", "todo", "Rebase onto qa, push, and open the PR", date="2026-09-05", first_date="2026-09-05")
        tk["git"] = git_merged()  # that PR merged before the TODO was written
        self.assertEqual(supersede.close_todos(tk), 0)

    def test_day_todos_become_objects_and_closed_ones_are_not_carried(self):
        from chitragupta import agent, extract, store
        st = store.Store()
        tk = st.ticket(T)
        tk["git"] = git_merged()
        st.day(D)["tickets"][T] = {"minutes": 30, "item_ids": []}
        st.touch_ticket(T)
        st.touch_day(D)
        st.save()

        def fake(kind, text, cfg, timeout=300):
            return {"day_summary": "x", "standup": {}, "per_ticket": [],
                    "open_todos": ["ABC-1001: rebase, push %s and open the PR" % BR, "ABC-1002: ask QA about stock"]}, 0.0
        with mock.patch.object(extract, "call_claude", fake):
            a = agent.Agent()
            a.consolidate(D)
        todos = a.store.day(D)["open_todos"]
        self.assertEqual([(x["ticket"], x["closed_by"]) for x in todos], [(T, "pr:7"), ("ABC-1002", None)])
        self.assertEqual([x["text"] for x in a.state["carry_todos"]["todos"]], ["ABC-1002: ask QA about stock"])


class SupersessionTests(Base):
    def setUp(self):
        super().setUp()
        from chitragupta import extract
        from chitragupta.safety import commands
        self.calls = []
        self.reply = {"items": []}

        def run(args, purpose, **kw):
            kind = "supersede" if any("supersede.md" in str(a) for a in args) else "consolidate"
            self.calls.append((kind, kw.get("input_text")))
            out = self.reply if kind == "supersede" else {"day_summary": "x", "standup": {}, "open_todos": [],
                                                           "per_ticket": []}
            return 0, json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.02,
                                  "usage": {"input_tokens": 900, "output_tokens": 80}, "structured_output": out}), ""
        for p in (mock.patch.object(extract, "resolve_claude", lambda cfg=None: ["claude.exe"]),
                  mock.patch.object(commands, "run", run)):
            p.start()
            self.addCleanup(p.stop)

    def day_with_causes(self):
        from chitragupta import store
        st = store.Store()
        tk = st.ticket(T)
        self.item(tk, "rc1", "root_cause", "Cancel skips the stock release", time="09:00")
        self.item(tk, "rc2", "root_cause", "Cancel releases stock only for paid orders", time="11:00")
        self.item(tk, "rc3", "root_cause", "Cancel checks the order lock before payment state", time="15:00",
                  confidence="confirmed")
        self.item(tk, "n1", "note", "Stock is released by the nightly job", session="inbox", evidence=["inbox.md"])
        st.day(D)["tickets"][T] = {"minutes": 60, "item_ids": ["rc1", "rc2", "rc3", "n1"]}
        st.touch_ticket(T)
        st.touch_day(D)
        st.save()

    def test_three_conflicting_root_causes_leave_one_current(self):
        from chitragupta import agent, modelbudget, render
        self.day_with_causes()
        self.reply = {"items": [
            {"id": "rc1", "subject": "cancel stock release", "status": "superseded", "superseded_by": "rc3"},
            {"id": "rc2", "subject": "cancel stock release", "status": "superseded", "superseded_by": "rc3"},
            {"id": "rc3", "subject": "cancel stock release", "status": "confirmed", "superseded_by": None},
            {"id": "n1", "subject": "cancel stock release", "status": "superseded", "superseded_by": "rc3"},
            {"id": "nope", "subject": "x", "status": "superseded", "superseded_by": "rc3"}]}
        a = agent.Agent()
        a.consolidate(D)
        kinds = [k for k, _ in self.calls]
        self.assertEqual(kinds, ["consolidate", "supersede"])
        sent = json.loads(self.calls[1][1])
        self.assertEqual(sent["ticket"], T)
        self.assertEqual([i["id"] for i in sent["items"] if i["id"].startswith("rc")], ["rc3", "rc2", "rc1"])  # newest first
        self.assertTrue(next(i for i in sent["items"] if i["id"] == "n1")["by_user"])
        it = a.store.ticket(T)["items"]
        self.assertEqual([(it[i].get("status"), it[i].get("superseded_by")) for i in ("rc1", "rc2", "rc3")],
                         [("superseded", "rc3"), ("superseded", "rc3"), ("confirmed", None)])
        self.assertEqual(it["rc1"]["title"], "Cancel skips the stock release")  # text never rewritten
        self.assertNotEqual(it["n1"].get("status"), "superseded")  # your note is never replaced by a model item
        self.assertNotIn("nope", it)
        self.assertEqual(it["rc1"]["subject"], "cancel stock release")
        modelbudget.drain(a.health, a.cfg)
        usage = a.health["model_usage"]["2026-09-03"]["by_purpose"]["supersede"]
        self.assertEqual((usage["calls"], usage["usd"]), (1, 0.02))
        md = render.render_ticket(a.store, T)
        sec = md[md.index("## Root cause"):md.index("## How it works")]
        self.assertIn("Cancel checks the order lock", sec)
        self.assertIn("Superseded (2)", sec)
        cur = sec[:sec.index("Superseded (2)")]
        self.assertNotIn("Cancel skips the stock release", cur)
        self.assertIn("replaced by [Cancel checks the order lock before payment state](#i-rc3)", sec)
        self.assertIn('<a id="i-rc3"></a>', md)
        from chitragupta.dashboard import api
        f = {i["id"]: i for i in api._items(a.store.ticket(T))}
        self.assertEqual((f["rc1"]["status"], f["rc1"]["superseded_by"]), ("superseded", "rc3"))

    def test_over_budget_makes_no_call_and_logs_it(self):
        from chitragupta import agent, common, modelbudget
        self.day_with_causes()
        a = agent.Agent()
        modelbudget.record(a.health, "2026-09-03", "extract", 0, 0, 5.0)
        a.consolidate(D)
        self.assertEqual([k for k, _ in self.calls], ["consolidate"])
        self.assertEqual(a.health["model_usage"]["2026-09-03"]["skipped"]["supersede"], 1)
        self.assertIn("supersede skipped", rd(common.wl("logs", "agent.log")))
        self.assertNotIn("status", a.store.ticket(T)["items"]["rc1"])

    def test_tickets_without_new_items_are_not_sent(self):
        from chitragupta import agent
        self.day_with_causes()
        a = agent.Agent()
        for i in a.store.ticket(T)["items"].values():
            i["first_date"] = "2026-08-01"
        a.consolidate(D)
        self.assertEqual([k for k, _ in self.calls], ["consolidate"])


class TodoMigration(Base):
    def setUp(self):
        super().setUp()
        from chitragupta import common, migrate, store
        cfg = common.read_json(common.wl("config.json"), {})
        cfg["chitragupta_migrations"] = [m for m in migrate.all_ids() if m != "kb5-todo-objects"]
        common.write_json(common.wl("config.json"), cfg)
        dd = store.new_day(D)
        dd.update(open_todos=["ABC-1001: push the branch", "check the stock job"], carried_todos=["ABC-1002: ask QA"])
        common.write_json(store.day_path(D), dd)
        common.write_json(common.wl("state.json"), {"carry_todos": {"from": D, "todos": ["ABC-1001: push the branch"]}})

    def test_strings_become_objects_nothing_dropped(self):
        from chitragupta import common, migrate, store
        res = migrate.run(wait_s=0)
        self.assertIn("kb5-todo-objects", res["applied"], res)
        dd = common.read_json(store.day_path(D))
        self.assertEqual(dd["open_todos"], [{"text": "ABC-1001: push the branch", "ticket": T, "closed_by": None},
                                            {"text": "check the stock job", "ticket": None, "closed_by": None}])
        self.assertEqual(dd["carried_todos"], [{"text": "ABC-1002: ask QA", "ticket": "ABC-1002", "closed_by": None}])
        st = common.read_json(common.wl("state.json"))
        self.assertEqual(st["carry_todos"]["todos"][0]["ticket"], T)
        self.assertTrue(os.path.isfile(os.path.join(res["backups"][-1], "data", "days", D[:4], D + ".json")))
        from chitragupta import render
        self.assertIn("- [ ] ABC-1002: ask QA", render.render_day(store.Store(), D, {"day_starts_at": "04:00"}, {}))


if __name__ == "__main__":
    unittest.main()
