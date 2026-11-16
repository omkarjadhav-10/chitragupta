"""The Evidence Graph scenario of the browser checks (dev only): cached Jira cards for the synthetic tickets of the UI
home, a commit and a captured test run on ABC-1001, then one `graphstore.sync` so the graph page has data.

    info = graphhome.add(home)    after the other fixtures; returns what check.js asserts on

Scenario: ABC-1001 has three acceptance criteria (requirements, ticket-level evidence: a commit and a passing run),
ABC-1003 has sixty (more than one page of requirements, so "Load more" shows), ABC-1002 has no card (no requirements
yet). "Every requirement is verified" needs a verified Companion ledger, which this home has no task for: check.js
shows that state from a served envelope instead (see check.js, graph section). Synthetic data only.
"""
import json
import os

from tests.graphfixture import card

TICKETS = {"ABC-1001": ["Order totals apply each discount once", "The order list shows the discounted total",
                        "Totals are rounded to two decimals"],
           "ABC-1003": ["Refund rule %d holds" % i for i in range(1, 61)]}


def _w(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=2, sort_keys=True)


def add(home):
    from chitragupta import common, graphstore
    for key, criteria in TICKETS.items():
        _w(os.path.join(home, "data", "jira", key + ".json"), card(key, criteria))
    path = os.path.join(home, "data", "tickets", "ABC-1001.json")
    with open(path, encoding="utf-8") as f:
        tk = json.load(f)
    sha = "a" * 40
    tk.setdefault("commits", {})[sha] = {"sha": sha, "short": sha[:9], "repo_name": "shop-api", "subject": "Fix order total",
                                         "date": "2026-01-05T09:00:00+00:00", "ref": "feature/ABC-1001-order-total",
                                         "ticket": "ABC-1001", "files": ["src/OrderService.java"], "files_count": 1}
    tk.setdefault("tests", {})["t1"] = {"id": "t1", "at": "2026-01-05T10:00:00+00:00", "command": "python -m unittest tests.test_orders",
                                        "status": "passed", "counts": {"passed": 12}}
    _w(path, tk)
    cfg = common.load_config()
    reports = [graphstore.sync(cfg, limit=100, budget_s=60.0)]
    while reports[-1].get("more"):
        reports.append(graphstore.sync(cfg, limit=100, budget_s=60.0))
    built = sorted({k for r in reports for k in r["built"] + r["recovered"]})
    return {"built": built, "errors": sorted({k for r in reports for k in r["errors"]}),
            "requirements": {k: len(v) for k, v in TICKETS.items()}}
