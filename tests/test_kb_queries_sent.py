"""K6b: queries.md lists only SQL that was actually sent (database MCP or a SQL command line), each with the raw line
it came from; SQL the model only wrote or reworded is kept in the data, marked sent:false, and not rendered.

Synthetic data only (tickets ABC-…, schema `shop`).
"""
import datetime as dt
import json
import os
import tempfile
import unittest

from tests.support import Isolated, rmtree, write_config

D = "2026-10-14"
SID = "s1"
SENT = "SELECT id, status FROM shop.orders WHERE id = 42 LIMIT 1"


def at(hh, mm, day=D):
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm)).astimezone()


def ev(event, hh, mm, repo, **kw):
    e = {"ts": at(hh, mm).isoformat(timespec="seconds"), "event": event, "session_id": SID, "source": "hook",
         "branch": "fix/ABC-1001-x", "repo_root": repo, "cwd": repo, "payload": {}}
    e.update(kw)
    return e


def db_call(hh, mm, repo, sql, key="sql"):
    args = json.dumps({"connectionId": "qa", key: sql})
    return ev("post-tool", hh, mm, repo, payload={"tool": "mcp__db__run_query", "input": {"arguments": args}})


class Base(Isolated, unittest.TestCase):
    def setUp(self):
        h = tempfile.mkdtemp(prefix="cg-test-")
        write_config(h, {"ticket_prefixes": ["ABC"]})
        self.isolate(h)
        self.work = tempfile.mkdtemp(prefix="cg-work-")
        self.addCleanup(rmtree, self.work)
        d = os.path.join(self.work, "shop-api")
        os.makedirs(os.path.join(d, ".git"))
        with open(os.path.join(d, ".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/fix/ABC-1001-x\n")
        self.r = d

    def write_raw(self, evs, day=D):
        from chitragupta import common
        p = common.wl("raw", day, SID + ".jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            for e in evs:
                f.write(json.dumps(e) + "\n")
        return p


class SentMatchTests(Base):
    def test_read_lines_carries_line_numbers(self):
        from chitragupta import agent
        p = self.write_raw([ev("user-prompt", 10, 0, self.r, payload={"prompt": "look at ABC-1001"}),
                            ev("stop", 10, 1, self.r, payload={"message": "ok"}),
                            db_call(10, 2, self.r, SENT)])
        evs, off = agent.read_lines(p, 0)
        self.assertEqual([e["_ln"] for e in evs], [1, 2, 3])
        with open(p, "rb") as f:
            first = len(f.readline())
        later, _ = agent.read_lines(p, first)
        self.assertEqual([e["_ln"] for e in later], [2, 3])

    def test_placeholders_are_wildcards(self):
        from chitragupta import sqlsent
        sent = sqlsent.sent_statements([dict(db_call(10, 2, self.r, SENT), _ref="raw/x.jsonl:3")])
        self.assertEqual(len(sent), 1)
        hit = sqlsent.find("select id,status from shop.orders where id = :order_id limit 1;", sent)
        self.assertEqual(hit["ref"], "raw/x.jsonl:3")
        self.assertIsNotNone(sqlsent.find("SELECT id, status FROM shop.orders WHERE id = ? LIMIT 1", sent))
        self.assertIsNone(sqlsent.find("SELECT * FROM shop.customers WHERE id = :id", sent))
        self.assertIsNone(sqlsent.find("SELECT id, status FROM shop.orders WHERE id = 43 LIMIT 1", sent))

    def test_postgres_casts_are_not_placeholders(self):
        from chitragupta import sqlsent
        sql = "select updated_at::text from shop.orders where id = 7"
        sent = sqlsent.sent_statements([db_call(10, 2, self.r, sql, key="query")])
        self.assertIsNotNone(sqlsent.find("select updated_at::text from shop.orders where id = :id", sent))
        self.assertIsNone(sqlsent.find("select created_at::text from shop.orders where id = :id", sent))

    def test_sql_in_a_command_line_counts(self):
        from chitragupta import sqlsent
        cmd = 'psql -h db.acme.test -c "SELECT count(*) FROM shop.orders WHERE status = \'NEW\'"'
        sent = sqlsent.sent_statements([ev("post-tool", 10, 2, self.r, payload={"tool": "Bash", "input": {"command": cmd}})])
        self.assertIsNotNone(sqlsent.find("select count(*) from shop.orders where status = :status", sent))


class ExtractionTests(Base):
    def setUp(self):
        super().setUp()
        from unittest import mock
        from chitragupta import common, extract
        self.answers = []

        def fake(kind, stdin_text, cfg, timeout=300):
            return (self.answers.pop(0) if self.answers else {}), 0.0
        for p in (mock.patch.object(extract, "call_claude", fake), mock.patch.object(common, "now", lambda: at(23, 0))):
            p.start()
            self.addCleanup(p.stop)

    def test_only_sent_sql_rendered(self):
        from chitragupta import agent, render
        from chitragupta.dashboard import api
        self.write_raw([ev("user-prompt", 10, 0, self.r, payload={"prompt": "check ABC-1001 orders"}),
                        ev("stop", 10, 1, self.r, payload={"message": "looking"}),
                        db_call(10, 2, self.r, SENT),
                        ev("stop", 10, 5, self.r, payload={"message": "Order 42 is NEW."})])
        self.answers = [{"queries": [
            {"text": "SELECT id, status FROM shop.orders WHERE id = :order_id LIMIT 1", "purpose": "order status",
             "dialect": "postgres", "ticket": "ABC-1001"},
            {"text": "SELECT * FROM shop.customers WHERE email = :email", "purpose": "customer lookup",
             "dialect": "postgres", "ticket": "ABC-1001"}]}]
        a = agent.Agent()
        a.ingest()
        a.run_extractions(any_size=True)
        qs = {q["purpose"]: q for q in a.store.ticket("ABC-1001")["queries"].values()}
        self.assertEqual((qs["order status"]["sent"], qs["order status"]["ref"]), (True, "raw/%s/%s.jsonl:3" % (D, SID)))
        self.assertIs(qs["customer lookup"]["sent"], False)
        md = render.render_queries(a.store, "ABC-1001")
        self.assertIn("shop.orders", md)
        self.assertIn("raw/%s/%s.jsonl:3" % (D, SID), md)
        self.assertNotIn("shop.customers", md)
        self.assertIn("1 more", md)
        hub = render.render_ticket(a.store, "ABC-1001")
        self.assertIn("1 query — see [queries.md]", hub)
        dq = {q["purpose"]: q for q in api._queries(a.store.ticket("ABC-1001"))}
        self.assertEqual((dq["order status"]["sent"], dq["customer lookup"]["sent"]), (True, False))

    def test_saved_sql_scripts_stay_unknown_with_their_file(self):
        from chitragupta import agent, sources
        from unittest import mock
        q = {"ts": at(11, 0).isoformat(), "ticket": "ABC-1001", "dialect": "postgres", "name": "ABC-1001-check.sql",
             "path": "C:/sql/ABC-1001-check.sql", "text": "select 1 from shop.orders"}
        with mock.patch.object(sources, "scan_sql", lambda cfg, st: [q]), \
                mock.patch.object(sources, "scan_commits", lambda cfg, st, since: []), \
                mock.patch.object(sources, "scan_bruno", lambda cfg, st: []), \
                mock.patch.object(sources, "scan_inbox", lambda cfg, st: []), \
                mock.patch.object(agent.Agent, "run_branches", lambda self: None):
            a = agent.Agent()
            a.run_sources()
        rec = list(a.store.ticket("ABC-1001")["queries"].values())[0]
        self.assertIsNone(rec["sent"])
        self.assertEqual(rec["evidence"], ["C:/sql/ABC-1001-check.sql"])


class Migration(Base):
    def setUp(self):
        super().setUp()
        from chitragupta import common, migrate
        cfg = common.read_json(common.wl("config.json"), {})
        cfg["chitragupta_migrations"] = [m for m in migrate.all_ids() if m != "kb5-queries-sent"]
        common.write_json(common.wl("config.json"), cfg)
        self.write_raw([ev("user-prompt", 10, 0, self.r, payload={"prompt": "ABC-1001"}), db_call(10, 2, self.r, SENT)])
        qs = {"q1": {"id": "q1", "date": D, "text": "SELECT id, status FROM shop.orders WHERE id = :id LIMIT 1"},
              "q2": {"id": "q2", "date": D, "text": "SELECT * FROM shop.customers"},
              "q3": {"id": "q3", "date": "2026-09-01", "text": "SELECT 2 FROM shop.items"},
              "q4": {"id": "q4", "date": D, "text": "select 3", "purpose": "SQL script ABC-1001.sql"}}
        common.write_json(common.hp("data", "tickets", "ABC-1001.json"), {"id": "ABC-1001", "queries": qs})

    def test_marks_existing_queries_from_raw_on_disk(self):
        from chitragupta import common, migrate, store
        res = migrate.run(wait_s=0)
        self.assertIn("kb5-queries-sent", res["applied"], res)
        q = common.read_json(store.ticket_path("ABC-1001"))["queries"]
        self.assertEqual((q["q1"]["sent"], q["q1"]["ref"]), (True, "raw/%s/%s.jsonl:2" % (D, SID)))
        self.assertIs(q["q2"]["sent"], False)
        self.assertIsNone(q["q3"]["sent"])  # its raw is gone: unknown, still shown
        self.assertIsNone(q["q4"]["sent"])  # a saved script
        self.assertTrue(os.path.isfile(os.path.join(res["backups"][-1], "data", "tickets", "ABC-1001.json")))


if __name__ == "__main__":
    unittest.main()
