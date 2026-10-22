"""K10 time model: turn intervals with a short tail, unioned across sessions, overlap split between tickets.

Synthetic data only (tickets ABC-…, schema `shop`).
"""
import datetime as dt
import json
import os
import tempfile
import unittest

from tests.support import Isolated, write_config

D = "2026-10-14"  # a Tuesday


def at(hh, mm, day=D):
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm)).astimezone()


def ev(event, hh, mm, sid, branch, prompt=None):
    e = {"ts": at(hh, mm).isoformat(timespec="seconds"), "event": event, "session_id": sid, "source": "hook",
         "branch": branch, "payload": {}}
    if prompt is not None:
        e["payload"] = {"prompt": prompt}
    return e


def sessions(end_at=(10, 40)):
    """time.md §4: session A on the ABC-1001 branch, two turns and a late session-end; session B names ABC-1002."""
    a, b = "fix/ABC-1001/x", "main"
    return [ev("session-start", 9, 50, "A", a),
            ev("user-prompt", 10, 0, "A", a, "go"), ev("post-tool", 10, 2, "A", a), ev("stop", 10, 5, "A", a),
            ev("user-prompt", 10, 19, "A", a, "next"), ev("post-tool", 10, 20, "A", a), ev("stop", 10, 22, "A", a),
            ev("session-end", end_at[0], end_at[1], "A", a),
            ev("user-prompt", 10, 3, "B", b, "check ABC-1002 in shop.orders"), ev("stop", 10, 7, "B", b)]


class PureModelTests(unittest.TestCase):
    def minutes(self, evs, **kw):
        from chitragupta import agent, timesheet as T
        attributed = agent.attribute([dict(e) for e in evs], ["ABC"], {"ticket_prefixes": ["ABC"]})
        return T.union_minutes(T.active_intervals(attributed, **kw))

    def test_turns_tail_and_split_overlap(self):
        m = self.minutes(sessions())
        self.assertAlmostEqual(m["ABC-1001"], 14.5)  # 3 alone + 3.5 shared + 8; the old blocks model gave 41
        self.assertAlmostEqual(m["ABC-1002"], 5.5)
        self.assertAlmostEqual(sum(m.values()), 20.0)  # = the wall union 10:00-10:12 + 10:19-10:27

    def test_session_end_and_start_change_nothing(self):
        self.assertEqual(self.minutes(sessions()), self.minutes(sessions(end_at=(10, 59))))

    def test_idle_gap_splits_a_turn(self):
        a = "fix/ABC-1001/x"
        evs = [ev("user-prompt", 10, 0, "A", a, "go"), ev("post-tool", 10, 2, "A", a),
               ev("post-tool", 10, 40, "A", a), ev("stop", 10, 41, "A", a)]
        self.assertAlmostEqual(self.minutes(evs)["ABC-1001"], (2 + 5) + (1 + 5))
        self.assertAlmostEqual(self.minutes(evs, tail=0, idle=60)["ABC-1001"], 41)

    def test_old_model_for_reference(self):
        from chitragupta import agent
        stamps = [at(10, 0), at(10, 2), at(10, 5), at(10, 19), at(10, 20), at(10, 22), at(10, 40)]
        self.assertEqual(agent.blocks_minutes(stamps), 41)


class AgentTimeTests(Isolated, unittest.TestCase):
    def setUp(self):
        h = tempfile.mkdtemp(prefix="cg-test-")
        write_config(h, {"ticket_prefixes": ["ABC"]})
        self.isolate(h)

    def write(self, evs):
        from chitragupta import common
        for e in evs:
            p = common.wl("raw", D, e["session_id"] + ".jsonl")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(e) + "\n")

    def test_recompute_minutes_uses_turn_intervals(self):
        from chitragupta import agent
        self.write(sessions())
        a = agent.Agent()
        a.recompute_minutes(D)
        t = a.store.day(D)["tickets"]
        self.assertEqual((t["ABC-1001"]["minutes"], t["ABC-1002"]["minutes"]), (14, 6))  # 14.5 and 5.5, rounded
        self.assertEqual(a.store.ticket("ABC-1001")["timeline"][D]["minutes"], 14)

    def test_tail_and_idle_are_configurable(self):
        from chitragupta import agent, common
        self.write(sessions())
        cfg = common.read_json(common.wl("config.json"))
        cfg["jira_worklog"] = {"turn_tail_minutes": 0, "block_gap_minutes": 30}  # idle falls back to block_gap_minutes
        common.write_json(common.wl("config.json"), cfg)
        a = agent.Agent()
        self.assertEqual(agent.time_params(a.cfg), (0, 30))
        a.recompute_minutes(D)
        self.assertEqual(a.store.day(D)["tickets"]["ABC-1002"]["minutes"], 3)  # 10:03-10:07, no tail, 10:03-10:05 shared

    def test_commit_counts_the_15_minutes_before_it(self):
        from chitragupta import agent
        a = agent.Agent()
        dd = a.store.day(D)
        dd["commits"].append({"sha": "a" * 40, "short": "aaaaaaa", "repo_name": "shop-api", "date": at(18, 47).isoformat(),
                              "ref": "fix/ABC-1001/x", "subject": "Fix orders", "ticket": "ABC-1001"})
        bl = a.jwl.blocks(D)
        self.assertEqual([(b["ticket"], b["start"], b["end"], round(b["minutes"])) for b in bl],
                         [("ABC-1001", at(18, 32), at(18, 47), 15)])

    def test_commit_inside_a_session_is_not_counted_twice(self):
        from chitragupta import agent
        self.write(sessions())
        a = agent.Agent()
        a.store.day(D)["commits"].append({"sha": "b" * 40, "short": "bbbbbbb", "repo_name": "shop-api",
                                          "date": at(10, 20).isoformat(), "ref": "fix/ABC-1001/x", "subject": "x",
                                          "ticket": "ABC-1001"})
        tracked = {}
        for b in a.jwl.blocks(D):
            tracked[b["ticket"]] = tracked.get(b["ticket"], 0) + b["minutes"]
        # the commit covers 10:05-10:20: only the gap 10:12-10:19 is new time; where it overlaps the sessions it is
        # counted once (10:05-10:12 stays split with ABC-1002, 10:19-10:20 is ABC-1001 already)
        self.assertAlmostEqual(tracked["ABC-1001"], 22.5)
        self.assertAlmostEqual(tracked["ABC-1002"], 4.5)
        self.assertAlmostEqual(sum(tracked.values()), 27)  # the wall union, 10:00-10:27

    def test_bruno_file_times_never_count_for_past_days(self):
        from chitragupta import agent, common
        d = os.path.join(self.home, "bruno", "ABC-1005")
        os.makedirs(d)
        p = os.path.join(d, "01 Login.bru")
        with open(p, "w", encoding="utf-8") as f:
            f.write("meta {\n  name: Login\n}\n")
        os.utime(p, (at(11, 0).timestamp(), at(11, 0).timestamp()))  # last changed during D
        a = agent.Agent()
        self.assertEqual(a.jwl.blocks(D), [])  # D is in the past: a file's last-change time is not a record of it
        os.utime(p, None)
        today = common.workday(common.now(), a.cfg)
        self.assertEqual({b["ticket"] for b in a.jwl.blocks(today)}, {"ABC-1005"})


if __name__ == "__main__":
    unittest.main()
