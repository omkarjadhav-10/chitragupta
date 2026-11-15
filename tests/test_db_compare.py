"""Metadata comparison (dbimpact.compare, DB-14): DBT-24 (explicit mappings only, a same-named object is an unconfirmed
candidate, a denied or partial listing never turns into "removed", engine-aware type normalisation, the column-type
difference and the hidden procedure definition of the fixture's second environment, snapshots of one connection) and
DBT-25 (a side whose environment is not permitted blocks the comparison with the reason; the configuration is
unchanged and nothing is relabelled). Metadata only; reads never recompute."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.dbfixture import FakeTransport, Scenario, write_ticket
from tests.support import Isolated


class Case(Isolated, unittest.TestCase):
    dev = True

    def setUp(self):
        self.isolate()
        self.sc = Scenario(self.home).build(dev=self.dev)
        write_ticket(self.home)
        from chitragupta import dbingest
        dbingest.run(self.cfg())
        self.fake = FakeTransport(self.sc)

    def cfg(self):
        return self.sc.cfg()

    def ctx(self):
        from chitragupta import dbaccess
        return dbaccess.AccessContext(self.cfg())

    def discover(self, page_size=2):
        from chitragupta import dbflows
        self.fake.page_size = page_size
        return dbflows.discover_metadata(self.ctx(), ["qa-main:shop.orders", "qa-main:shop.order_events",
                                                      "dev-main:shop.orders"], self.fake, self.cfg(), {},
                                         inventory=[("qa-main", "shop"), ("dev-main", "shop")])

    def cli(self, *argv):
        from chitragupta import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    @staticmethod
    def by_key(r):
        return {it["key"]: it for it in r["items"]}


class Mapping(Case):
    """DBT-24."""

    def test_same_name_is_a_candidate_until_confirmed(self):
        from chitragupta import dbimpact
        self.discover()
        r = dbimpact.compare(self.ctx(), "qa-main", "dev-main")
        self.assertEqual(r["state"], "ok")
        it = self.by_key(r)
        self.assertEqual((it["shop.orders"]["category"], it["shop.orders"]["candidate"]), ("not_comparable", True))
        self.assertIn("--confirm shop.orders", it["shop.orders"]["confirm"])
        self.assertEqual(it["shop.order_events"]["category"], "removed")  # dev-main's listing of shop is complete
        self.assertIn("complete listing", it["shop.order_events"]["reason"])
        self.assertEqual(it["shop_audit.orders"]["category"], "unavailable")  # outside dev-main's scope: unknown
        proc = it["shop.cancel_order"]  # dev-main's procedure whose definition the account may not read
        self.assertEqual(proc["category"], "not_comparable")
        self.assertIn("definition of shop.cancel_order is unavailable on dev-main", proc["reason"])
        dbimpact.confirm_pair(self.ctx(), "qa-main", "dev-main", "shop.orders")
        r = dbimpact.compare(self.ctx(), "qa-main", "dev-main")
        o = self.by_key(r)["shop.orders"]
        self.assertEqual((o["category"], o["mapping"]), ("changed", "confirmed"))
        self.assertEqual([(d["column"], d["field"], d["left"], d["right"]) for d in o["differences"]],
                         [("status", "type", "varchar(32)", "varchar(20)")])
        self.assertEqual(r["categories"]["changed"], 1)
        notes = " ".join(r["notes"])
        for want in ("Snapshot times: qa-main", "dev-main", "says nothing about data, configuration, deployed code",
                     "Row-by-row comparison is not offered", "never says the environments are synchronised"):
            self.assertIn(want, notes)

    def test_a_cross_environment_mapping_you_confirmed_pairs_them(self):
        from chitragupta import dbimpact, dbingest
        self.discover()
        dbingest.confirm_mapping(self.cfg(), "qa-main:shop.orders.id", "dev-main:shop.orders.id")
        dbingest.run(self.cfg(), only={"mappings"})
        o = self.by_key(dbimpact.compare(self.ctx(), "dev-main", "qa-main"))["shop.orders"]
        self.assertEqual((o["category"], o["mapping"]), ("changed", "confirmed"))
        self.assertEqual(o["differences"][0]["left"], "varchar(20)")

    def test_denied_or_partial_listings_never_become_removed(self):
        from chitragupta import dbaccess, dbimpact, dbmodel, dbstore
        r = dbimpact.compare(self.ctx(), "qa-main", "dev-main", save=False)  # no listing taken yet
        self.assertEqual(r["categories"]["removed"] + r["categories"]["added"], 0, r["items"])
        self.assertIn("never taken", self.by_key(r)["shop.order_events"]["reason"])
        self.discover()
        dbstore.save_checkpoint("inventory-%s" % dbmodel.fingerprint(["dev-main", "shop"])[:16], "shop.orders",
                                ["orders"], done=False)  # dev-main's last listing stopped at a more_available page
        r = dbimpact.compare(self.ctx(), "qa-main", "dev-main", save=False)
        self.assertEqual(r["categories"]["removed"] + r["categories"]["added"], 0, r["items"])
        self.assertEqual(self.by_key(r)["shop.order_events"]["category"], "unavailable")
        self.assertIn("partial", self.by_key(r)["shop.order_events"]["reason"])
        self.discover()
        dbaccess.quarantine("dev-main", None, reason="permission denied")  # denied at the last check
        r = dbimpact.compare(self.ctx(), "qa-main", "dev-main")
        self.assertEqual(r["categories"]["removed"], 0, r["items"])
        self.assertTrue(r["right"]["denied"])

    def test_engine_aware_type_normalisation(self):
        from chitragupta import dbimpact
        same = [("postgres", "int4", "mssql", "int"), ("postgres", "integer", "postgres", "INT"),
                ("postgres", "character varying(50)", "postgres", "varchar(50)"), ("mssql", "datetime2(7)", "mssql",
                                                                                  "datetime2"),
                ("postgres", "numeric(10)", "postgres", "numeric(10, 0)"), ("postgres", "timestamp with time zone",
                                                                           "postgres", "timestamptz"),
                ("mssql", "float", "postgres", "double precision"), ("postgres", "bool", "postgres", "boolean")]
        for e1, t1, e2, t2 in same:
            self.assertEqual(dbimpact.norm_type(e1, t1), dbimpact.norm_type(e2, t2), (t1, t2))
        differ = [("postgres", "varchar(50)", "postgres", "varchar(100)"), ("postgres", "numeric(10,2)", "postgres",
                                                                          "numeric(12,2)"),
                  ("mssql", "timestamp", "postgres", "timestamp"), ("postgres", "int8", "postgres", "int4"),
                  ("mssql", "nvarchar(32)", "mssql", "varchar(32)")]
        for e1, t1, e2, t2 in differ:
            self.assertNotEqual(dbimpact.norm_type(e1, t1), dbimpact.norm_type(e2, t2), (t1, t2))
        self.assertEqual(dbimpact.norm_default("((0))"), dbimpact.norm_default("0"))
        self.assertEqual(dbimpact.norm_default("'open'::character varying"), "'open'")

    def test_two_snapshots_of_one_connection(self):
        from chitragupta import dbimpact, dbingest
        res = self.discover()
        first = [s for s in res["snapshots"] if s["connection"] == "dev-main"][0]
        p = os.path.join(self.home, "data", "db-schema", "dev-main", "shop.orders.json")
        with open(p, encoding="utf-8") as f:
            card = json.load(f)
        card["describe"] = [dict(c, dataType="varchar(40)") if c["name"] == "status" else c for c in card["describe"]]
        with open(p, "w", encoding="utf-8") as f:
            json.dump(card, f)
        dbingest.run(self.cfg(), only={"card:dev-main/shop.orders"}, force=True)
        second = dbimpact.take_snapshot(self.ctx(), "dev-main")
        self.assertFalse(second["unchanged"])
        self.assertTrue(dbimpact.take_snapshot(self.ctx(), "dev-main")["unchanged"])  # not repeated
        r = dbimpact.compare(self.ctx(), "dev-main@" + first["id"], "dev-main@" + second["id"])
        o = self.by_key(r)["shop.orders"]
        self.assertEqual((o["category"], o["mapping"]), ("changed", "same object"))
        self.assertEqual([(d["field"], d["left"], d["right"]) for d in o["differences"]],
                         [("type", "varchar(20)", "varchar(40)")])
        self.assertEqual((r["left"]["source"], r["right"]["source"]), ("snapshot", "snapshot"))
        self.assertTrue(r["left"]["taken_at"] and r["right"]["taken_at"])
        code, out = self.cli("db", "snapshots", "dev-main")
        self.assertEqual(code, 0, out)
        self.assertIn("dev-main@" + second["id"], out)


class Blocked(Case):
    """DBT-25 (and DBT-02 for the blocked side): no comparison with a side that is not permitted; nothing widened."""

    dev = False

    def files(self):
        out = {}
        for rel in (os.path.join(".worklog", "config.json"),):
            with open(os.path.join(self.home, rel), "rb") as f:
                out[rel] = f.read()
        from chitragupta import mcpjobs
        p = os.path.join(mcpjobs.mcp_dir(), "allow.json")
        out["allow"] = open(p, "rb").read() if os.path.isfile(p) else None
        d = os.path.join(self.home, "data", "db", "compare")
        out["compare"] = sorted(os.listdir(d)) if os.path.isdir(d) else []
        return out

    def test_disallowed_environment_blocks_with_the_reason(self):
        from chitragupta import dbaccess, dbimpact, dbviews, mcpserver
        before = self.files()
        cases = {"uat-main": "environment uat is not in qa_data.allowed_envs",
                 "dev-main": "environment dev is not in qa_data.allowed_envs",
                 "prod-main": "not in the approved scope", "qa-reporting": "not in the approved scope"}
        for conn, why in cases.items():
            r = dbimpact.compare(self.ctx(), "qa-main", conn)
            self.assertEqual((r["state"], r["side"], r["connection"]), ("blocked", "right", conn))
            self.assertIn(why, r["reason"])
            self.assertIn("nothing here widens or relabels them", r["text"])
            self.assertFalse(r["published"])
            v = dbviews.compare_view(self.ctx(), "qa-main", conn)
            self.assertEqual((v["state"], v["data"]["blocked"]["side"]), ("blocked_comparison", "right"))
            t = mcpserver.call_tool("get_db_comparison", {"left": conn, "right": "qa-main"})["content"][0]["text"]
            self.assertIn("Comparison blocked: the left side (%s) is not permitted" % conn, t)
            code, out = self.cli("db", "compare", "qa-main", conn, "--refresh")
            self.assertEqual(code, 0, out)
            self.assertIn("Comparison blocked", out)
        self.assertEqual(self.files(), before)  # config, guard file and caches untouched
        ctx = dbaccess.AccessContext(self.cfg())
        self.assertNotIn("uat-main", ctx.connections)
        self.assertEqual(ctx.scope["blocked"]["uat-main"], "environment uat is not in qa_data.allowed_envs")
        self.assertEqual(self.cfg()["qa_data"]["allowed_envs"], ["qa"])


class Cache(Case):
    def test_reads_never_recompute(self):
        from chitragupta import dbimpact, dbviews, mcpserver
        self.discover()
        code, out = self.cli("db", "compare", "qa-main", "dev-main")
        self.assertEqual(code, 0, out)
        self.assertIn("# Compare qa-main with dev-main", out)
        self.assertIn("Confirm the pair: cli db compare qa-main dev-main --confirm shop.orders", out)
        with mock.patch.object(dbimpact, "compare", side_effect=AssertionError("a read recomputed")):
            v = dbviews.compare_view(self.ctx(), "qa-main", "dev-main")
            self.assertEqual((v["kind"], v["state"]), ("compare", "ok"))
            self.assertEqual(v["data"]["categories"]["not_comparable"], 2)
            res = mcpserver.call_tool("get_db_comparison", {"id": v["id"]})
            self.assertFalse(res["isError"], res)
            self.assertIn("same name on both connections", res["content"][0]["text"])
            self.assertIn(v["id"], mcpserver.call_tool("get_db_comparison", {})["content"][0]["text"])
            self.assertEqual(dbviews.compare_view(self.ctx(), "dev-main", "qa-main")["state"], "not_computed")
        code, out = self.cli("db", "compare", "qa-main", "dev-main", "--confirm", "shop.orders")
        self.assertEqual(code, 0, out)
        self.assertIn("status type: varchar(32) -> varchar(20)", out)


if __name__ == "__main__":
    unittest.main()
