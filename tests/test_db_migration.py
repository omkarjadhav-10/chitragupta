"""The compatible migration (KB step db1-canonical-graph, dbingest.migrate_legacy; DB-24, DBT-36): a dry run writes
nothing; a real run backs data/db/ up before its first write, builds the canonical graph from the legacy DB flows,
leaves an ambiguous legacy name needs_resolution with its candidates and legacy relations inferred, keeps every legacy
file byte-identical (ticket JSON, db-flow.md, schema cards, the link map, /cg:trace), preserves your notes, and a
re-run adds nothing (no duplicate flow, no inflated metric)."""
import contextlib
import io
import os
import unittest
from unittest import mock

from tests.dbfixture import Scenario, write_linkmap, write_ticket
from tests.support import Isolated, write_config

STEP = "db1-canonical-graph"


class Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build()
        write_ticket(self.home)
        write_linkmap(self.home)
        self.md = os.path.join(self.home, "tickets", "ABC-1001", "db-flow.md")
        os.makedirs(os.path.dirname(self.md), exist_ok=True)
        with open(self.md, "w", encoding="utf-8", newline="\n") as f:
            f.write("# ABC-1001 DB flow\n\n| table | change |\n|---|---|\n| shop.orders | status open -> cancelled |\n")
        self.pending()

    def pending(self):
        from chitragupta import migrate
        cfg = self.sc.config()
        cfg["chitragupta_migrations"] = [m for m in migrate.all_ids() if m != STEP]
        write_config(self.home, cfg)

    def cfg(self):
        return self.sc.cfg()

    def ctx(self):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg())

    def legacy(self):
        out = {}
        for top in (os.path.join(self.home, "data", "tickets"), os.path.join(self.home, "data", "db-schema"),
                    os.path.join(self.home, "tickets")):
            for dp, _, fns in os.walk(top):
                for fn in fns:
                    with open(os.path.join(dp, fn), "rb") as f:
                        out[os.path.join(dp, fn)] = f.read()
        return out

    def trace(self):
        from chitragupta import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["trace", "ABC-1001"])
        return buf.getvalue()

    def metrics(self):
        from chitragupta import common, progress
        return progress.records(progress.Ctx(None, common.now()))

    def db_files(self):
        out = {}
        for dp, _, fns in os.walk(os.path.join(self.home, "data", "db")):
            for fn in fns:
                if fn == "reconcile.json" or dp.endswith("checkpoints"):
                    continue  # the reconciliation's own run record (its time changes on every pass)
                with open(os.path.join(dp, fn), "rb") as f:
                    out[os.path.relpath(os.path.join(dp, fn), self.home)] = f.read()
        return out


class Migration(Case):
    """DBT-36."""

    def test_dry_run_writes_nothing(self):
        from chitragupta import migrate
        before = self.legacy()
        res = migrate.run(dry_run=True)
        self.assertIn(STEP, res["applied"])
        notes = " ".join(n for n in res["changes"] if n.startswith(STEP))
        self.assertIn("left needs_resolution", notes)
        self.assertIn("orders", notes)
        self.assertFalse(os.path.exists(os.path.join(self.home, "data", "db")))
        self.assertEqual(self.legacy(), before)

    def test_migration_builds_the_graph_and_leaves_legacy_bytes_alone(self):
        from chitragupta import common, dbflows, dbingest, dbstore, migrate
        before, trace, metrics = self.legacy(), self.trace(), self.metrics()
        seen = []
        real = dbingest.run

        def spy(*a, **k):  # the backup folder exists before the migration's first write
            seen.append(sorted(f for f in os.listdir(common.wl("backups")) if STEP in f))
            return real(*a, **k)
        with mock.patch.object(dbingest, "run", spy):
            res = migrate.run(wait_s=0)
        self.assertFalse(res.get("error"), res)
        self.assertIn(STEP, res["applied"])
        self.assertTrue(seen and seen[0], seen)
        self.assertIn(STEP, common.load_config()["chitragupta_migrations"])
        g = dbstore.load_graph()
        un = [c for c in g["claims"].values() if c["predicate"] == "unresolved_table" and
              c["value"]["ref"] == "orders"]
        self.assertEqual(un[0]["value"]["state"], "needs_resolution")
        self.assertEqual(len(un[0]["value"]["candidates"]), 2)  # shop.orders and shop_audit.orders: none chosen
        legacy = [e for e in g["edges"].values() if e.get("claimed_kind")]
        self.assertTrue(legacy)
        for e in legacy:  # the legacy "fk" stays an association; only the QA sample's evidence lifts it to a sample
            self.assertEqual(e["kind"], "logical_association")
            kinds = {g["evidence"][v]["kind"] for v in e["evidence"]}
            self.assertEqual(e["basis"], "observed_sample" if "observation" in kinds else "inferred", kinds)
        flows = dbstore.list_ids("flows")
        self.assertEqual(len(flows), 1)
        self.assertEqual(dbflows.resolve_start(self.ctx(), "table", "orders")["state"], "ambiguous")
        self.assertEqual(self.legacy(), before)  # ticket JSON, db-flow.md, schema cards, link map
        self.assertEqual(self.trace(), trace)  # /cg:trace unchanged
        self.assertEqual(self.metrics(), metrics)  # database records never feed progress
        db1, rev = self.db_files(), dbstore.load("flows", flows[0])["revision"]
        self.pending()  # run it again (as if the marker were lost)
        res = migrate.run(wait_s=0)
        self.assertFalse(res.get("error"), res)
        self.assertEqual(dbstore.list_ids("flows"), flows)  # no duplicate flow
        self.assertEqual(dbstore.load("flows", flows[0])["revision"], rev)
        self.assertEqual(self.db_files(), db1)  # nothing added
        self.assertEqual(self.metrics(), metrics)

    def test_replay_preserves_notes_and_backs_up_first(self):
        from chitragupta import common, dbflows, dbingest, dbstore, migrate
        dbingest.run(self.cfg())
        fid = dbflows.build_flow(self.ctx(), {"kind": "ticket", "key": "ABC-1001"}, save=True)["id"]
        rec = dbstore.load("flows", fid)
        dbstore.save("flows", dict(rec, notes="checked with the billing team"))
        res = migrate.run(wait_s=0)
        self.assertFalse(res.get("error"), res)
        self.assertEqual(dbstore.load("flows", fid)["notes"], "checked with the billing team")
        b = [p for p in res["backups"] if STEP in p][0]
        manifest = common.read_json(os.path.join(b, "manifest.json"))
        copied = [k for k in manifest if k.replace("\\", "/").endswith("data/db/flows/%s.json" % fid)]
        self.assertTrue(copied, manifest)
        with open(os.path.join(b, manifest[copied[0]]), encoding="utf-8") as f:
            self.assertIn("checked with the billing team", f.read())

    def test_a_home_without_database_scope_is_left_alone(self):
        from chitragupta import migrate
        cfg = self.sc.config()
        cfg["mcp"]["database"].pop("scope")
        cfg["mcp"]["database"].pop("connections")
        cfg["chitragupta_migrations"] = [m for m in migrate.all_ids() if m != STEP]
        write_config(self.home, cfg)
        res = migrate.run(wait_s=0)
        self.assertIn(STEP, res["applied"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "data", "db")))
        self.assertFalse([p for p in res.get("backups") or [] if STEP in p])


if __name__ == "__main__":
    unittest.main()
