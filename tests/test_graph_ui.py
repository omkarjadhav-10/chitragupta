"""Evidence graph page and its read-only route: /api/graph, the #/graph page registration, its states and its
stylesheet (synthetic data only)."""
import http.client
import json
import os
import re
import unittest

from chitragupta import export, graphstore as gs, graphviews
from tests.support import ROOT
from tests.test_graph_store import SyncBase, everything

APP = os.path.join(ROOT, "chitragupta", "dashboard", "app")


def read(rel):
    with open(os.path.join(APP, rel), encoding="utf-8") as f:
        return f.read()


class Route(SyncBase):
    def setUp(self):
        super().setUp()
        from chitragupta.dashboard import server
        self.srv, self.thread = server.start_in_thread(0)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        try:
            c.putrequest("GET", path, skip_host=True)
            c.putheader("Host", "127.0.0.1:%d" % self.srv.port)
            c.endheaders()
            r = c.getresponse()
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
        finally:
            c.close()

    def test_the_route_returns_the_views_envelope_and_writes_nothing(self):
        self.sync()
        gs.reset()
        before = everything(self.home)
        status, env = self.get("/api/graph?view=summary&ticket=ABC-1001")
        self.assertEqual(status, 200)
        self.assertEqual(env, graphviews.read({"view": "summary", "ticket": "ABC-1001"}))
        self.assertEqual(env["graph"]["state"], "ok")
        status, trace = self.get("/api/graph?view=trace&ticket=ABC-1001&requirement=ac1&history=1&basis=declared")
        self.assertEqual((status, trace["requirement"]["alias"], trace["filters"]), (200, "ac1",
                                                                                     {"basis": "declared", "history": True}))
        status, changes = self.get("/api/graph?view=changes&ticket=ABC-1001&since=0")
        self.assertEqual((status, changes["baseline"]), (200, "ok"))
        self.assertEqual(self.get("/api/graph?ticket=ABC-1001&privacy=1")[1]["privacy"], True)
        gs.reset()
        self.assertEqual(everything(self.home), before, "the route wrote into the data home")

    def test_hidden_ticket_is_403_with_revoke_and_bad_input_is_400(self):
        self.sync()
        os.remove(os.path.join(self.home, "data", "tickets", "ABC-1002.json"))
        status, body = self.get("/api/graph?view=summary&ticket=ABC-1002")
        self.assertEqual((status, body.get("revoke")), (403, True))
        self.assertNotIn("rows", body)
        for bad in ("/api/graph?view=summary&ticket=abc", "/api/graph?view=everything&ticket=ABC-1001",
                    "/api/graph?ticket=ABC-1001&since=soon", "/api/graph?ticket=ABC-1001&basis=guess",
                    "/api/graph?view=trace&ticket=ABC-1001&requirement=ac9", "/api/graph"):
            with self.subTest(bad=bad):
                status, body = self.get(bad)
                self.assertEqual(status, 400)
                self.assertTrue(body.get("error"))

    def test_not_built_is_a_state_not_an_error(self):
        status, env = self.get("/api/graph?view=summary&ticket=ABC-1001")
        self.assertEqual((status, env["graph"]["state"]), (200, "not_built"))


class Page(unittest.TestCase):
    def test_registered_with_key_v_under_more(self):
        self.assertIn("'graph'", re.search(r"export const PAGES = \[[^\]]*\]", read("lib/core.js")).group(0))
        self.assertRegex(read("lib/shell.js"), r"\{ id: 'graph', label: 'Evidence graph', icon: '\w+', key: 'v' \}")
        main = read("main.js")
        self.assertIn("import { GraphPage } from './pages/graph.js';", main)
        self.assertIn("case 'graph': return html`<${GraphPage} route=${r} />`;", main)
        self.assertIn("['g then v', 'Evidence graph']", main)
        primary = re.search(r"const primary = \[([^\]]*)\]", main).group(1)
        self.assertNotIn("graph", primary)   # More menu, not the primary navigation
        self.assertIn('<link rel="stylesheet" href="/app/graph.css">', read("index.html"))
        self.assertIn("/app/pages/graph.js", export._STUBBED)   # an export shows only the ticket page
        self.assertTrue(any("graph.css" in tag for tag in export._PAGE_TAGS))

    def test_reads_only_through_api_and_never_off_the_machine(self):
        js = read("pages/graph.js")
        self.assertNotIn("fetch(", js)
        self.assertNotIn("runAction", js)
        self.assertNotIn("write(", js)
        self.assertEqual(sorted(set(re.findall(r"'(/api/[a-z/]+)", js))), ["/api/graph", "/api/tickets"])
        for text in (js, read("graph.css")):
            self.assertEqual(re.findall(r"(?i)(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", text), [])

    def test_every_state_has_its_own_text(self):
        js = read("pages/graph.js")
        for state in ("not_built", "rebuilding", "revoked"):
            self.assertRegex(js, r"\b%s: \['[^']+', '[^']+'\]" % state, state)
        for text in ("Behind: the inputs changed", "No requirements yet", "Every requirement is verified",
                     "Load more", "Choose a ticket", "Removed from Jira", "Not confirmed in a task baseline"):
            self.assertIn(text, js)
        self.assertIn("res.error.status === 403", js)   # a 403 clears the page, as on the Companion page
        self.assertIn('data-egx-trace="placeholder"', js)   # the trace panel arrives in the next dashboard step

    def test_every_status_has_an_icon_and_a_word(self):
        js = read("pages/graph.js")
        from chitragupta import graphmodel
        for dim, states in graphmodel.ORDER.items():
            block = re.search(r"%s: \{([^}]*)\}" % dim, js).group(1)
            for state in states:
                self.assertIn("'%s'" % state if " " in state else "%s:" % state, block, (dim, state))

    def test_stylesheet_uses_existing_tokens_and_its_own_prefix(self):
        css, app = read("graph.css"), read("app.css")
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", app))
        used = set(re.findall(r"var\((--[a-z0-9-]+)\)", css))
        self.assertTrue(used)
        self.assertEqual(sorted(used - defined), [])
        for selector in re.findall(r"(?m)^([^\s@/{}][^{]*)\{", css):
            for part in selector.split(","):
                self.assertTrue(part.strip().startswith(".egx-"), part)


if __name__ == "__main__":
    unittest.main()
