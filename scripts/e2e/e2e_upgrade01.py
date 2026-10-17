"""Replayable requests + DB flow with the REAL models (haiku extraction, sonnet curation) on a synthetic session
(opt-in: CHITRAGUPTA_E2E=1):

login 200, mistyped login 401, health check, failing payload 400, correct payload with "role": null 200, look-around
GET, plus reading an entity + service and running an UPDATE and a SELECT. Expected: exactly two saved requests (login,
then the null-role send) in Bruno, requests.md and replay.sh; replay passes against the mock with JSON null received;
db-flow.md shows is_locked false -> true with its condition, file:line and a verify-query link; a second tick adds
nothing. Reuses the unit suite's harness (temp home, mock server, tripwires on MCP/Slack).

    py -3 scripts/e2e/e2e_upgrade01.py        (KEEP=1 leaves the temp home)
"""
import json
import os
import re
import sys

from e2e_common import Checks, gate, paths_cfg

from tests import test_upgrade01 as T  # noqa: E402  (e2e_common put the repo root on sys.path)


def main():
    gate("e2e_upgrade01")
    c = T.ReplayTests("test_curation_replay_and_privacy")
    c.setUp()
    ck = Checks()
    try:
        from chitragupta import agent, common, extract, mermaid_check, store
        cfgp = os.path.join(c.home, ".worklog", "config.json")
        cfg = common.read_json(cfgp)
        cfg["paths"] = paths_cfg()
        cfg["models"] = {"extract": "haiku", "consolidate": "sonnet", "curate": "sonnet"}
        common.write_json(cfgp, cfg)
        s, port, repo = c.s, c.srv.server_port, c.repo
        with open(os.path.join(repo, "Customer.java"), "w") as f:
            f.write("@Entity @Table(name = \"customers\", schema = \"shop\")\n"
                    "class Customer { @Column(name = \"is_locked\") boolean isLocked; String role; }\n")
        with open(os.path.join(repo, "CustomerService.java"), "w") as f:
            f.write("\n" * 408 + "    if (StringUtils.isBlank(req.getRole())) { customer.setIsLocked(true); }  // line 409\n")
        s.prompt("%s: the partner send with role null locks the customer. Reproduce it and find where it happens." % T.TICKET)
        s.hook("post-tool", tool_name="Read", tool_input={"file_path": repo + "/Customer.java"})
        s.hook("post-tool", tool_name="Read", tool_input={"file_path": repo + "/CustomerService.java"})
        c.run_null_role_session(port)
        s.sid = "s-up01-db"
        s.prompt("%s: verify in the DB" % T.TICKET)
        s.bash("psql -h qa-db.acme.test -d shop -c \"UPDATE shop.customers SET is_locked = false WHERE id = 42\"", run=False)
        s.bash("psql -h qa-db.acme.test -d shop -c \"SELECT id, status, is_locked FROM shop.customers WHERE id = 42\"", run=False)
        s.stop("Confirmed. CustomerService.update (CustomerService.java:409) sets shop.customers.is_locked from false to true "
               "when the partner payload's role is null/blank (StringUtils.isBlank(req.getRole())). I reset it with the UPDATE "
               "and the SELECT after the partner send shows is_locked = true.\n"
               "Worklog note: partner update with role null sets shop.customers.is_locked=true (CustomerService.java:409).")
        s.end()
        extract.call_claude = c._real  # REAL models
        a = agent.Agent(force=True)
        a.tick()
        agent.save_state(a)
        cost = sum((a.health.get("cost_by_day") or {}).values())
        bdir = os.path.join(c.home, "bruno", T.TICKET)
        bru = sorted(f for f in os.listdir(bdir) if f != "folder.bru") if os.path.isdir(bdir) else []
        print("saved requests:", bru)
        ck(len(bru) == 2, "exactly two saved requests")
        if len(bru) == 2:
            ck(re.search(r"(?i)login|auth|token", bru[0]) is not None, "first is the login: " + bru[0])
            ck('"role": null' in T.rd(os.path.join(bdir, bru[1])), "second is the null-role send with JSON null")
        md = T.rd(os.path.join(c.home, "tickets", T.TICKET, "requests.md"))
        ck(md.count("\n### ") == 2, "requests.md has two steps")
        sh = T.rd(os.path.join(c.home, "tickets", T.TICKET, "replay.sh"))
        ck(sh.count("\nsend ") == 2, "replay.sh has two steps")
        c.fill_env()
        T.Mock.received = []
        r = c.replay()
        print(r.stdout[-800:], r.stderr[-400:])
        ck(r.returncode == 0, "replay.sh passes against the mock")
        ck(any(isinstance(x["body"], dict) and "role" in x["body"] and x["body"]["role"] is None for x in T.Mock.received),
           "mock received role as JSON null")
        ck(any(x["auth"] == "Bearer " + T.TOKEN for x in T.Mock.received), "token chained from the login")
        dfp = os.path.join(c.home, "tickets", T.TICKET, "db-flow.md")
        df = T.rd(dfp) if os.path.exists(dfp) else ""
        print(df[df.find("## Column changes"):df.find("## Relationships")])
        ck(bool(re.search(r"is_locked\S*\s*\|\s*false\s*(→|->)\s*true", df)), "column change is_locked: false -> true")
        ck("CustomerService.java:409" in df, "file:line in db-flow")
        ck(bool(re.search(r"(?i)role", df[df.find("## Column changes"):])), "condition mentions role")
        ck(bool(re.search(r"\]\(queries\.md#q-[0-9a-f]+\)", df)), "verify-query link")
        ck(df and mermaid_check.check(df) == [], "mermaid syntax ok")
        tk = store.Store().ticket(T.TICKET)
        n = (len(tk["api_calls"]), len(tk["data_flow"]), len(tk.get("artifacts") or {}))
        a = agent.Agent(force=True)
        a.tick()
        agent.save_state(a)
        tk = store.Store().ticket(T.TICKET)
        ck((len(tk["api_calls"]), len(tk["data_flow"]), len(tk.get("artifacts") or {})) == n, "second tick adds no duplicates")
        print("dropped:", json.dumps(tk["replay"].get("dropped"), indent=0)[:1500])
        print("model cost ~ $%.3f" % cost)
    finally:
        if os.environ.get("KEEP"):
            print("kept:", c.home)
            c.home = os.path.join(c.home, "__keep_nothing_to_delete__")  # restore() removes only this path
        c.tearDown()
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
