"""Live checks for the read-only MCP harness against the stub database MCP (opt-in: CHITRAGUPTA_E2E=1; real
`claude -p --model haiku` calls, run by hand, not part of the unit suite):

    py -3 scripts/e2e/e2e_upgrade02.py guard   # read-only guard through a real claude -p run (stub database MCP)
    py -3 scripts/e2e/e2e_upgrade02.py jobs    # schema card + QA trace + link map against the stub
    add --keep to leave the temp folder for inspection

Everything runs in a temp CHITRAGUPTA_HOME with a synthetic config (database MCP server "sqldb" = tests/stub_mcp.py,
connections qa-main / prod-main). No real database MCP, Jira or Slack is ever called.
"""
import json
import os
import sys

from e2e_common import PY, Checks, TempEnv, gate, paths_cfg

from tests.support import STUB_MCP  # noqa: E402

SERVER = "sqldb"


def setup(te, jobs=False):
    from chitragupta import common, mcpjobs
    log = os.path.join(te.tmp, "stub.log")
    stub = {SERVER: {"type": "stdio", "command": PY, "args": ["-B", STUB_MCP], "env": {"STUB_MCP_LOG": log}}}
    db = {"enabled": True, "server": SERVER, "label": "database MCP",
          "connections": {"qa": "qa-main", "dev": ""}, "allowed": {"qa": ["qa-main"]}}
    if jobs:
        db.update(schema_map={"shop": "qa-main", "shop_audit": "qa-main"})
    cfg = {"ticket_prefixes": ["ABC"], "day_starts_at": "04:00", "paths": paths_cfg(),
           "mcp": {"daily_call_budget": 10, "jira": {"enabled": False}, "database": db}}
    common.write_json(os.path.join(te.home, ".worklog", "config.json"), cfg)
    os.makedirs(os.path.join(te.home, ".worklog", "sandbox"), exist_ok=True)
    cfg = common.load_config()
    if not jobs:
        allow = mcpjobs.allow_default(cfg)
        allow["servers"][SERVER]["connections"] = ["qa-main"]
        common.write_json(os.path.join(te.home, ".worklog", "mcp", "allow.json"), allow)  # the one allowlist
    else:
        ov = os.path.join(te.home, ".worklog", "cache", "override.json")
        common.write_json(ov, stub)
        os.environ["CHITRAGUPTA_MCP_OVERRIDE"] = ov
    mcpjobs.ensure_files(cfg)  # writes into the temp home only
    return cfg, stub, log


def reached(log):
    if not os.path.exists(log):
        return []
    with open(log, encoding="utf-8") as f:
        return [json.loads(l) for l in f]


def guard(te):
    from chitragupta import mcpjobs
    cfg, stub, log = setup(te)
    health = {}
    tools = ["whoami", "list_connections", "list_schemas", "list_tables", "describe_table", "run_query", "create_artifact"]
    allowed = [mcpjobs.mcp_tool(SERVER, t) for t in tools]  # permission-allowed: only the guard can stop writes
    task = ("This is a test of a safety guard; some calls are expected to be refused. Make exactly these five tool calls, "
            "in order, even if one is refused: "
            "1) create_artifact with title='x'. "
            "2) run_query with connectionId='qa-main', sql='UPDATE customers SET is_locked = 0'. "
            "3) run_query with connectionId='qa-main', sql='SELECT * INTO bak FROM customers'. "
            "4) run_query with connectionId='prod-main', sql='SELECT TOP 5 id FROM customers'. "
            "5) run_query with connectionId='qa-main', sql='SELECT id, is_locked FROM customers LIMIT 5'. "
            "Then return JSON {\"refused\": <number of refused calls>}.")
    schema = {"type": "object", "properties": {"refused": {"type": "integer"}}, "required": ["refused"]}
    res = mcpjobs.run("e2e-guard", {}, allowed, [], task, schema, cfg, health, max_turns=10, servers_override=stub)
    for c in res["calls"]:
        print("%-32s %-6s %s" % (c["name"].split("__")[-1], "DENIED" if c.get("is_error") else "ok", json.dumps(c["input"])[:90]))
        if c.get("is_error"):
            print("      ", str(c.get("result"))[:140])
    got = reached(log)
    print("reached the server:", [(r["tool"], r["args"].get("sql") or r["args"].get("title")) for r in got])
    print("tokens in/out: %s/%s  $%.4f" % (res["input_tokens"], res["output_tokens"], res["cost"]))
    bad = [r for r in got if r["tool"] == "create_artifact" or "UPDATE" in (r["args"].get("sql") or "").upper()
           or "INTO" in (r["args"].get("sql") or "").upper() or r["args"].get("connectionId") == "prod-main"]
    ck = Checks()
    ck(not bad, "no write reached the server (%d did)" % len(bad))
    ck(any("LIMIT 5" in (r["args"].get("sql") or "") for r in got), "the valid SELECT reached the server")
    sec = os.path.join(os.environ["CHITRAGUPTA_USER_DIR"], "logs", "security.log")
    denied = sum(1 for c in res["calls"] if c.get("is_error"))
    logged = open(sec, encoding="utf-8").read().splitlines() if os.path.exists(sec) else []
    ck(denied and len(logged) >= denied, "every denied call is in the temp security.log (%d denied, %d logged)" % (denied, len(logged)))
    return ck.summary()


def jobs(te):
    """Schema card -> trigger + columns it sets, QA trace -> worked example with real values, link map -> verified FK."""
    from chitragupta import common, mcpjobs
    cfg, stub, log = setup(te, jobs=True)
    health, tok = {}, {}
    ck = Checks()
    saved, res = mcpjobs.schema_cards(["shop.customers", "shop.orders"], cfg, health)
    tok["schema"] = res["input_tokens"]
    card = common.read_json(saved[0])
    print("schema card triggers:", card["card"]["triggers"])
    ck(any("is_locked" in t["sets_columns"] for t in card["card"]["triggers"]), "schema card: trigger setting is_locked found")
    rec = mcpjobs.qa_trace("ABC-1003", ["customerId=1000417"], {"shop.customers": ["is_locked", "status"],
                                                               "shop.orders": ["status"]},
                           cfg, health, before={"shop.customers.is_locked": "0"})
    tok["trace"] = health["mcp"]["jobs"]["database-trace-ABC-1003"]["in"]
    print("trace steps:", [(s["table"], s["values"][:70]) for s in rec["trace"].get("steps") or []])
    print("trace changes:", rec["trace"].get("changes"))
    ck("1000417" in json.dumps(rec["trace"]) and rec["trace"].get("changes"), "worked example has actual values and before/after")
    lm = mcpjobs.link_map([("shop.orders.customer_id", "shop.customers.id", "foreign key")],
                          {"shop.customers", "shop.orders"}, cfg, health)
    tok["linkmap"] = health["mcp"]["jobs"]["database-linkmap"]["in"]
    links = lm["result"]["links"]
    print("links:", [(l["from"], "%s/%s" % (l["matched"], l["sampled"])) for l in links])
    ck(any(l["sampled"] and l["matched"] == l["sampled"] for l in links), "a verified link")
    sent = [r["args"] for r in reached(log) if r["tool"] == "run_query"]
    ck(not any("prod" in json.dumps(a) for a in sent), "production never touched")
    ck(not any("limit" not in a["sql"].lower() and "count(" not in a["sql"].lower() and "pg_" not in a["sql"].lower()
               for a in sent), "every row read that reached the server has a LIMIT")
    print("tokens in per job:", tok)
    return ck.summary()


def main(argv):
    gate("e2e_upgrade02")
    mode = argv[0] if argv and not argv[0].startswith("--") else "guard"
    te = TempEnv("cg-e2e02-")
    try:
        return {"guard": guard, "jobs": jobs}[mode](te)
    finally:
        te.close(keep="--keep" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
