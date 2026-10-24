"""K12: `agent rebuild <KEY…> | --all` rebuilds tickets' knowledge from the raw events still on disk, under today's
attribution and capture rules. Old items are archived, never removed; your own notes stay current; minutes are
recomputed with the previous value kept; a parent issue (Epic) keeps no items and becomes its children summary.
A fake model stands in for claude. Synthetic data only (tickets ABC-…, *.acme.test)."""
import copy
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest

from tests.support import PY, RUN_PY, Isolated, rd, rmtree, write_config
from tests import test_brief as tb

D, D2 = "2026-10-14", "2026-10-15"
S1, S2 = "11111111-aaaa-bbbb-cccc-000000000001", "22222222-aaaa-bbbb-cccc-000000000002"
BRANCH = "feature/ABC-1001-cancel"
TURN = re.compile(r"=== TURN ticket=(\S+) \((\S+)\) at (\d\d:\d\d) ===")


def at(day, hh, mm):
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm)).astimezone().isoformat(timespec="seconds")


def item(iid, kind, title, day=D, session=S1[:8], **kw):
    rec = {"id": iid, "kind": kind, "title": title, "detail": "", "evidence": [], "confidence": "likely", "date": day,
           "time": "10:09", "session": session, "first_date": day}
    rec.update(kw)
    return rec


def slot(minutes, item_ids=()):
    return {"minutes": minutes, "item_ids": list(item_ids), "query_ids": [], "api_keys": [], "table_keys": [],
            "integration_keys": [], "files_changed": [], "what_i_did": []}


class RebuildBase(Isolated):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": {"python": PY},
                                 "brief": {"daily_cap": 8}, "budget": {"model_daily_usd": 3}})
        self.work = tempfile.mkdtemp(prefix="cg-work-")
        self.addCleanup(rmtree, self.work)
        self.repo = os.path.join(self.work, "shop-api")
        os.makedirs(os.path.join(self.repo, ".git"))
        from chitragupta import common, extract, modelbudget, render, store
        # raw sessions: a pasted breadcrumb on the ABC-1001 branch, then a prompt that names ABC-1002 (same branch)
        self.raw(D, S1, [("user-prompt", 10, 0, {"prompt": "ABC-1000 / ABC-1001\nOrders not linked on cancel"}),
                         ("post-tool", 10, 5, {"tool": "Bash", "input": {"command": "grep -rn cancel src"}}),
                         ("stop", 10, 9, {"message": "Cancel never releases the reservation."}),
                         ("user-prompt", 11, 0, {"prompt": "now look at ABC-1002, the refund rounding"}),
                         ("post-tool", 11, 5, {"tool": "Bash", "input": {"command": "grep -rn refund src"}}),
                         ("stop", 11, 10, {"message": "Refund rounds down twice."})])
        self.raw(D2, S2, [("user-prompt", 9, 0, {"prompt": "continue the cancel fix"}),
                          ("post-tool", 9, 5, {"tool": "Bash", "input": {"command": "grep -rn release src"}}),
                          ("stop", 9, 20, {"message": "Release the reservation before saving."})])
        # the Epic: its card says so; it holds items and time that were the child's work
        common.write_json(common.hp("data", "jira", "ABC-1000.json"),
                          {"key": "ABC-1000", "card": {"found": True, "title": "Shop orders", "type": "Epic",
                                                       "status": "Draft"}})
        t0 = store.new_ticket("ABC-1000")
        t0.update(jira={"type": "Epic", "status": "Draft"}, first=D, last=D,
                  items={"ep00000001": item("ep00000001", "finding", "Orders are not linked on cancel")},
                  timeline={D: {"minutes": 66, "line": "analysis"}})
        common.write_json(store.ticket_path("ABC-1000"), t0)
        t1 = store.new_ticket("ABC-1001")
        t1.update(title="Cancel keeps stock reserved", title_source="user", jira={"type": "Bug", "status": "QA"},
                  first=D, last=D2, bruno={"shop/cancel.bru": {"method": "POST", "url": "https://api.acme.test/cancel"}},
                  git={"summary": "Branches 1"}, timeline={D: {"minutes": 120, "line": "x"}},
                  items={"mf00000001": item("mf00000001", "finding", "Refund rounds down twice"),  # ABC-1002's, misfiled
                         "ol00000001": item("ol00000001", "finding", "An old guess about the cancel path"),
                         "nt00000001": item("nt00000001", "note", "Ask QA about partial cancels", session="inbox",
                                            evidence=["inbox.md"], confidence="confirmed"),
                         "mv00000001": item("mv00000001", "finding", "Moved here by you", src="user",
                                            moved_from="ABC-1003/aaaaaaaaaa")})
        common.write_json(store.ticket_path("ABC-1001"), t1)
        dd = store.new_day(D)
        dd["closed"] = True
        dd["tickets"] = {"ABC-1000": slot(66, ["ep00000001"]), "ABC-1001": slot(120, ["mf00000001", "ol00000001"])}
        common.write_json(store.day_path(D), dd)
        common.write_json(store.catalog_path(), {"tables": {}, "apis": {}, "integrations": {}})
        st = {"sessions": {S1: {"files": {"%s/%s.jsonl" % (D, S1): self.offsets(D, S1)}, "days": [D], "source": "hook"},
                           S2: {"files": {"%s/%s.jsonl" % (D2, S2): self.offsets(D2, S2)}, "days": [D2],
                                "source": "hook"}}}
        common.write_json(common.wl("state.json"), st)
        common.write_json(common.wl("health.json"), {"model_usage": {D: {"calls": 10, "usd": 0.5, "by_purpose": {
            "extract": {"calls": 10, "in": 0, "out": 0, "usd": 0.5}}}}})
        self.readme = os.path.join(self.home, "tickets", "ABC-1001", "README.md")
        os.makedirs(os.path.dirname(self.readme))
        with open(self.readme, "w", encoding="utf-8") as f:
            f.write("# ABC-1001\n\n## My notes\n\n%s\nkeep this: check partial cancels\n%s\n" % (render.NOTES_START,
                                                                                            render.NOTES_END))
        self.daily = os.path.join(self.home, *render.daily_rel(D).split("/"))
        os.makedirs(os.path.dirname(self.daily))
        with open(self.daily, "w", encoding="utf-8") as f:
            f.write("# %s (already pushed)\n" % D)

        self.calls, self.extract_usd = [], 0.01
        real = extract.call_claude
        modelbudget._PENDING.clear()

        def fake(kind, text, cfg, timeout=300):
            self.calls.append((kind, text))
            if kind == "extract":
                items = []
                for t, src, hhmm in TURN.findall(text):
                    if t == "ABC-1001":
                        items.append({"kind": "root_cause", "title": "Cancel never releases the reservation",
                                      "ticket": "ABC-1001", "time": hhmm, "confidence": "confirmed"})
                        items.append({"kind": "finding", "title": "The epic knows the order flow", "ticket": "ABC-1000",
                                      "time": hhmm})  # a parent issue never gets it: the turn's ticket does
                    elif t == "ABC-1002":
                        items.append({"kind": "finding", "title": "Refund rounds down twice", "ticket": "ABC-1002",
                                      "time": hhmm})
                usd = self.extract_usd
                out = {"items": items, "activity_summary": "re-extracted", "queries": [], "tables": [], "apis": []}
            elif kind == "supersede":
                usd, out = 0.02, {"items": []}
            else:
                usd, out = 0.05, tb.good_output()
            modelbudget.note(kind, 1000, 100, usd, cfg)
            return copy.deepcopy(out), usd

        extract.call_claude = fake
        self.addCleanup(setattr, extract, "call_claude", real)
        self.addCleanup(modelbudget._PENDING.clear)

    def raw(self, day, sid, rows):
        from chitragupta import common
        p = common.wl("raw", day, sid + ".jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for event, hh, mm, payload in rows:
                f.write(json.dumps({"ts": at(day, hh, mm), "event": event, "session_id": sid, "source": "hook",
                                    "cwd": self.repo, "repo_root": self.repo, "branch": BRANCH, "payload": payload}) + "\n")

    def offsets(self, day, sid):
        from chitragupta import common
        n = os.path.getsize(common.wl("raw", day, sid + ".jsonl"))
        return {"read": n, "extracted": n}

    def tk(self, key):
        from chitragupta import common, store
        return common.read_json(store.ticket_path(key), None)

    def day(self, d=D):
        from chitragupta import common, store
        return common.read_json(store.day_path(d))

    def current(self, key):
        return {i["title"]: i for i in ((self.tk(key) or {}).get("items") or {}).values() if not i.get("archived")}

    def snapshot(self):
        out = {}
        for dp, _, fns in os.walk(self.home):
            if os.sep + "logs" in dp or os.sep + "locks" in dp:
                continue
            for f in fns:
                p = os.path.join(dp, f)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, self.home)] = hashlib.sha1(fh.read()).hexdigest()
        return out

    def kinds(self):
        return [k for k, _ in self.calls]

    def rebuild(self, keys=("ABC-1000", "ABC-1001", "ABC-1002"), **kw):
        from chitragupta import rebuild
        return rebuild.run(list(keys), wait_s=0, **kw)


class DryRun(RebuildBase, unittest.TestCase):
    def test_dry_run_writes_nothing_and_estimates(self):
        before = self.snapshot()
        res = self.rebuild(dry_run=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])
        self.assertEqual(res["tickets"], ["ABC-1000", "ABC-1001", "ABC-1002"])
        self.assertEqual(res["sessions"], 2)
        self.assertEqual(res["days"], [D, D2])
        self.assertEqual(res["chunks"], 2)
        # 2 chunks at the ledger's $0.05 per extraction, plus one supersede ($0.06) and one brief ($0.10) default per
        # ticket that is not a parent issue (2)
        self.assertAlmostEqual(res["est_usd"], 0.42, places=4)
        self.assertEqual(res["cap_usd"], 8.0)
        self.assertFalse(res["over_cap"])
        self.assertEqual(res["model"], "haiku")
        res = self.rebuild(dry_run=True, brief=False, cap_usd=0.2)
        self.assertAlmostEqual(res["est_usd"], 0.22, places=4)
        self.assertTrue(res["over_cap"])

    def test_default_cost_per_chunk_without_a_ledger(self):
        from chitragupta import common
        common.write_json(common.wl("health.json"), {})
        cfg = common.read_json(common.wl("config.json"))
        cfg["rebuild"] = {"est_usd_per_chunk": 0.5, "cap_usd": 1}
        common.write_json(common.wl("config.json"), cfg)
        res = self.rebuild(["ABC-1001"], dry_run=True)
        self.assertAlmostEqual(res["est_usd"], 2 * 0.5 + 0.06 + 0.10, places=4)
        self.assertEqual(res["cap_usd"], 1.0)
        self.assertTrue(res["over_cap"])

    def test_cap_refusal_changes_nothing(self):
        before = self.snapshot()
        res = self.rebuild(cap_usd=0.3)
        self.assertTrue(res["refused"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_cli_and_launcher(self):
        from chitragupta import agent
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = agent.main(["rebuild", "ABC-1001", "--dry-run", "--cap-usd", "0.1"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["tickets"], ["ABC-1001"])
        self.assertTrue(out["over_cap"])
        p = subprocess.run([PY, "-B", RUN_PY, "agent", "rebuild", "--all", "--dry-run"], capture_output=True,
                           env=self.child_env(), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout.decode("utf-8"))
        self.assertEqual(out["sessions"], 2)
        self.assertIn("ABC-1001", out["tickets"])


class RealRun(RebuildBase, unittest.TestCase):
    def test_rebuild_files_items_under_the_right_ticket_and_keeps_everything(self):
        from chitragupta import brief, common
        old1 = self.tk("ABC-1001")
        res = self.rebuild()
        self.assertFalse(res.get("refused"))
        self.assertFalse(res.get("stopped"))
        # backup of the old files, relative paths kept
        b = res["backup"]
        self.assertTrue(os.path.isdir(b))
        saved = common.read_json(os.path.join(b, "data", "tickets", "ABC-1001.json"))
        self.assertEqual(saved["items"], old1["items"])
        for rel in (("data", "tickets", "ABC-1000.json"), ("data", "days", "2026", D + ".json"), ("data", "catalog.json"),
                    ("tickets", "ABC-1001", "README.md"), (".worklog", "state.json")):
            self.assertTrue(os.path.isfile(os.path.join(b, *rel)), rel)
        # old items archived, never removed; your own items stay current
        t1 = self.tk("ABC-1001")
        for iid in ("mf00000001", "ol00000001"):
            self.assertTrue(t1["items"][iid]["archived"]["reason"].startswith("rebuild "))
        for iid in ("nt00000001", "mv00000001"):
            self.assertNotIn("archived", t1["items"][iid])
        # the re-extracted items land under the ticket their turn names
        self.assertIn("Cancel never releases the reservation", self.current("ABC-1001"))
        self.assertIn("The epic knows the order flow", self.current("ABC-1001"))
        self.assertNotIn("Refund rounds down twice", self.current("ABC-1001"))
        self.assertEqual(self.current("ABC-1002")["Refund rounds down twice"]["src"], "prompt")
        # the user's things are untouched
        for k in ("title", "title_source", "bruno", "jira", "git"):
            self.assertEqual(t1[k], old1[k], k)
        self.assertIn("keep this: check partial cancels", rd(self.readme))
        self.assertEqual(rd(self.daily), "# %s (already pushed)\n" % D)  # pushed daily files are left alone
        # the Epic: no items of its own any more, its page lists its child
        t0 = self.tk("ABC-1000")
        self.assertEqual(self.current("ABC-1000"), {})
        self.assertEqual(t0["items"]["ep00000001"]["archived"]["reason"],
                         "parent issue: work belongs to its child tickets")
        v = brief.view(t0, "ABC-1000")
        self.assertTrue(v["epic"])
        self.assertEqual([c["key"] for c in v["children"]], ["ABC-1001"])
        self.assertIn("[ABC-1001](../ABC-1001/README.md)", rd(os.path.join(self.home, "tickets", "ABC-1000", "README.md")))
        # minutes recomputed with the current time model, the old value kept once
        rows = self.day()["tickets"]
        self.assertEqual(rows["ABC-1001"]["minutes_before_rebuild"], 120)
        self.assertNotEqual(rows["ABC-1001"]["minutes"], 120)
        self.assertEqual((rows["ABC-1000"]["minutes"], rows["ABC-1000"]["minutes_before_rebuild"]), (0, 66))
        self.assertGreater(rows["ABC-1002"]["minutes"], 0)
        self.assertEqual(rows["ABC-1002"]["minutes_before_rebuild"], 0)
        self.assertEqual(res["minutes"][D]["ABC-1001"], {"before": 120, "after": rows["ABC-1001"]["minutes"]})
        # supersession and a brief per ticket that is not a parent issue; recorded in health
        self.assertEqual(self.kinds().count("extract"), 2)
        self.assertEqual(self.kinds().count("supersede"), 1)  # ABC-1002 has one item: nothing to compare
        self.assertEqual(self.kinds().count("brief"), 2)
        self.assertEqual(res["archived"], 3)
        self.assertEqual(res["calls"], 2 + 1 + 2)
        self.assertAlmostEqual(res["usd"], 0.02 + 0.02 + 0.10, places=4)
        h = common.read_json(common.wl("health.json"))
        self.assertEqual(h["rebuilds"][-1]["backup"], b)
        self.assertIn("rebuild", rd(common.wl("logs", "agent.log")))

    def test_no_brief(self):
        self.rebuild(brief=False)
        self.assertNotIn("brief", self.kinds())

    def test_per_ticket_mode_ignores_other_tickets_results(self):
        res = self.rebuild(["ABC-1001"])
        self.assertEqual((self.tk("ABC-1002") or {}).get("items") or {}, {})  # its time is the day's, its items wait
        self.assertEqual(res["ignored"].get("ABC-1002"), 1)
        self.assertIn("Cancel never releases the reservation", self.current("ABC-1001"))
        self.assertTrue(self.tk("ABC-1000")["items"]["ep00000001"].get("archived") is None)  # not selected: untouched

    def test_second_run_is_idempotent(self):
        self.rebuild()
        first = {k: sorted(self.current(k)) for k in ("ABC-1000", "ABC-1001", "ABC-1002")}
        n_items = len(self.tk("ABC-1001")["items"])
        res = self.rebuild()
        self.assertEqual({k: sorted(self.current(k)) for k in first}, first)
        self.assertEqual(len(self.tk("ABC-1001")["items"]), n_items)
        self.assertEqual(res["new_items"], 0)
        self.assertEqual(self.day()["tickets"]["ABC-1001"]["minutes_before_rebuild"], 120)  # only the first time

    def test_spend_cap_mid_run_stops_cleanly_and_a_rerun_continues(self):
        self.extract_usd = 0.5  # far above the ledger's $0.05 estimate
        res = self.rebuild(cap_usd=0.45)
        self.assertTrue(res["stopped"])
        self.assertEqual(self.kinds(), ["extract"])
        from chitragupta import common
        prog = common.read_json(common.wl("state.json"))["rebuild_progress"]
        self.assertEqual(len(list(prog.values())[0]["done"]), 1)
        self.assertIn("Cancel never releases the reservation", self.current("ABC-1001"))  # what was merged is kept
        self.assertTrue(self.tk("ABC-1001")["items"]["ol00000001"]["archived"])
        self.extract_usd = 0.01
        res = self.rebuild(cap_usd=8)
        self.assertFalse(res.get("stopped"))
        self.assertEqual(self.kinds().count("extract"), 2)  # only the session-day left over
        self.assertEqual(res["archived"], 0)  # the first run archived them already
        self.assertNotIn("rebuild_progress", common.read_json(common.wl("state.json")) or {})


if __name__ == "__main__":
    unittest.main()
