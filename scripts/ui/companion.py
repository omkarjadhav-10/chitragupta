"""Browser walkthrough of the Companion page against an isolated synthetic home (dev only; never a real one).

    python scripts/ui/companion.py [SHOTS_DIR]

Builds tasks in every planner stage through the backend's own actions (frozen clock, synthetic ABC-… tickets,
acme.test), serves the dashboard from this checkout on a free port and runs scripts/ui/companion-check.js. Generate
Plan and Improve Plan run through the real job code with an injected stub caller in this process: no model, no
provider, no Jira or database call, and every other process start is a tripwire. Needs scripts/ui/node_modules
(playwright-core driving the installed Chrome or Edge); nothing is installed here.
"""
import datetime as dt
import os
import subprocess
import sys
import threading
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from tests.support import write_config  # noqa: E402
from tests.test_companion_state import Base, JIRA  # noqa: E402
from chitragupta import common, companion, companionjobs, store, workflowstore  # noqa: E402
from chitragupta.dashboard import server  # noqa: E402

CALLS = []


def stub_caller(prompt, text, cfg, timeout=None):
    """Stands in for the constrained Claude call: a fixed, valid plan output. Counted, never a real call."""
    CALLS.append(prompt)
    out = {"summary": "Filter orders by status and keep the choice.",
           "approach": "Add a status filter to the order list and persist the selection per user.",
           "steps": [{"id": "filter", "text": "Add the status filter to the order list", "criteria": ["ac1"]},
                     {"id": "persist", "text": "Persist the chosen filter", "criteria": ["ac2"], "depends_on": ["filter"]},
                     {"id": "check", "text": "Verify both criteria", "kind": "verification", "criteria": ["ac1", "ac2"],
                      "depends_on": ["persist"]}],
           "files": [{"path": "src/orders/filter.py", "component": "order filter", "basis": "proposed"}],
           "database": "Not applicable: no schema change.",
           "risks": ["Saved filters from older sessions may hold removed statuses"],
           "tests": [{"scenario": "Filter by open status", "expected": "Only open orders are listed"}],
           "verification": ["Run the order list tests"], "assumptions": ["Status names are stable"], "missing": []}
    return out, 0.01


class Fixture(Base):
    def runTest(self):  # instantiated directly, never collected
        pass

    def build(self):
        self.setUp()
        extra = {}
        for name in ("repo-c", "repo-d", "repo-e", "repo-f", "repo-g"):
            extra[name] = os.path.realpath(os.path.join(common.home(), name))
            os.makedirs(extra[name])
        keys = {"ABC-1001": "Filter orders by status", "ABC-1002": "Export order history",
                "ABC-1003": "Show a badge for the active filter", "ABC-1004": "Refund rounding fix",
                "ABC-1005": "Paused: invoice totals", "ABC-1006": "Testing: shipping estimate",
                "ABC-1007": "Open PR: order search", "ABC-1008": "Completed: cart cleanup"}
        repos = [self.repo, self.repo_b] + list(extra.values())
        self.cfg = {"profile": {"accounts": [{"provider": "jira", "instance": JIRA, "id": "dev-a"}], "repos": repos,
                                "include_tickets": keys},
                    "watched_repos": repos, "git_author_emails": ["dev@acme.test"],
                    "budget": {"companion_call_usd": 0.05}}
        write_config(common.home(), self.cfg)
        for key, title in keys.items():
            tk = store.new_ticket(key)
            tk["title_hint"] = title
            common.write_json(store.ticket_path(key), tk)
        common.write_json(common.hp("data", "jira", "ABC-1003.json"), {
            "instance": JIRA, "fetched_at": self.stamp(), "card": {
                "key": "ABC-1003", "summary": keys["ABC-1003"], "status": "To Do", "priority": "High",
                "description": "Users lose track of filters. Acceptance criteria: - A badge shows the active filter "
                               "- The badge clears with the filter - Screen readers announce the badge"}})
        a = self.planned("ABC-1001", n=2)
        self.create("ABC-1002", self.repo_b, n=2)
        self.started("ABC-1004", extra["repo-c"], sid="sess-4")
        p = self.started("ABC-1005", extra["repo-d"], sid="sess-5")
        companion.pause(p["id"], p["revision"], "Stopped after the rounding helper")
        t = self.started("ABC-1006", extra["repo-e"], sid="sess-6")
        companion.request_verification(t["id"], t["revision"], ["Run the shipping estimate tests"])
        self.started("ABC-1007", extra["repo-f"], sid="sess-7")
        self.pr(number=41, state="open", key="ABC-1007", repo="repo-f")
        self.tick("ABC-1007")
        # attention: one required delivery reminder (UAT, current revision r1) and three branch alerts
        tk = self.tk("ABC-1007")
        tk["release"] = {"uat": {"requirement": {"state": "required"}, "revision": {"rev": "r1", "at": self.stamp()}},
                         "reminders": [{"id": "uat-deployed", "kind": "uat-deployed", "repo": "", "severity": "warn",
                                        "text": "UAT branch has the changes, but the UAT deployment of them is "
                                                "unverified (no evidence)."}]}
        common.write_json(store.ticket_path("ABC-1007"), tk)
        for key, repo in (("ABC-1001", "scratch"), ("ABC-1004", "repo-c"), ("ABC-1005", "repo-d")):
            tk = self.tk(key)
            tk["git"] = dict(tk.get("git") or {"repos": {}}, alerts=[
                {"kind": "behind", "text": "feature/%s is behind main" % key, "repo": repo}])
            common.write_json(store.ticket_path(key), tk)
        done = self.planned("ABC-1008", extra["repo-g"])
        rec = workflowstore.load(done["id"])
        rec.update(stage="completed", next_action="View completion evidence")  # fixture shortcut: a finished task
        workflowstore.save(rec, rec["revision"], lambda r: None)
        return a


def main(argv):
    shots = os.path.abspath(argv[0]) if argv else os.path.join(ROOT, "notes", "local-tests", "companion-review-assets")
    fx = Fixture()
    srv = None
    try:
        fx.build()
        cfg = common.load_config()

        def spawn(jid, cfg=None):  # the runner, in this process, with the stub caller
            threading.Timer(1.0, lambda: companionjobs.run(jid, common.load_config(), caller=stub_caller)).start()
        patch = mock.patch.object(companionjobs, "spawn_runner", spawn)
        patch.start()
        fx.addCleanup(patch.stop)
        srv, _thread = server.start_in_thread(0)
        env = dict(os.environ)
        args = ["node", os.path.join(ROOT, "scripts", "ui", "companion-check.js"), "http://127.0.0.1:%d/" % srv.port,
                shots]
        code = subprocess.call(args, cwd=os.path.join(ROOT, "scripts", "ui"), env=env, stdin=subprocess.DEVNULL,
                               timeout=900)
        print("stub Claude calls: %d (no real model, provider or Jira call)" % len(CALLS))
        del cfg
        return code
    finally:
        if srv:
            srv.shutdown()
            srv.server_close()
        fx.doCleanups()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
