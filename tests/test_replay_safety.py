"""K11 replay safety: steps that change a shared record are flagged in requests.md and gated in replay.sh (--yes for
any env but local); a restore step only when the before-state was captured; repeat steps get their own engine-owned
.bru file so `bru run` and replay.sh run the same steps; payloads are the .bru body; hard-coded literals are linted.
Synthetic acme.test requests; nothing is run against a server."""
import json
import os
import re
import shutil
import subprocess
import unittest

from tests.support import Isolated, rd, write_config, wr

T = "ABC-1001"
BASH = next((p for p in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash") or "") if p and os.path.exists(p)), None)
FOLDER = """meta {
  name: ABC-1001 order status
  seq: 1
}

docs {
  Order status check.

  Run in order:
    1. Login             200
    2. Update order      200
    1. Login again       200
}
"""
LOGIN = """meta {
  name: 1. Login
  type: http
  seq: 1
}

post {
  url: {{baseUrl}}/auth/login
  body: json
  auth: none
}

body:json {
  {
    "email": "{{process.env.ABC_EMAIL}}",
    "password": "{{process.env.ABC_PASSWORD}}"
  }
}

script:post-response {
  const b = res.getBody() || {};
  if (b.token) bru.setVar("token", b.token);
}
"""
UPDATE = """meta {
  name: 2. Update order
  type: http
  seq: 2
}

put {
  url: {{baseUrl}}/shop/orders/{{orderId}}
  body: json
  auth: bearer
}

auth:bearer {
  token: {{token}}
}

body:json {
  {
    "status": "OPEN",
    "firstName": "Test",
    "storeName": "Acme Downtown Clinic"
  }
}

vars:pre-request {
  orderId: 7
}
"""
BEFORE = {"id": 7, "status": "CLOSED", "firstName": "Test", "storeName": "Acme Downtown Clinic", "total": 5}


class ReplaySafety(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate()
        write_config(self.home, {"worklog_home": self.home, "ticket_prefixes": ["ABC"]})
        b = os.path.join(self.home, "bruno")
        for env, url in (("QA", "https://qa.acme.test"), ("LOCAL", "http://127.0.0.1:9")):
            os.makedirs(os.path.join(b, "environments"), exist_ok=True)
            wr(os.path.join(b, "environments", env + ".bru"), "vars {\n  baseUrl: %s\n}\n" % url)
        d = os.path.join(b, T)
        os.makedirs(d)
        wr(os.path.join(d, "folder.bru"), FOLDER)
        wr(os.path.join(d, "1. Login.bru"), LOGIN)
        wr(os.path.join(d, "2. Update order.bru"), UPDATE)
        self.bdir = d

    def generate(self, before_body=True):
        from chitragupta import common, replay, store
        st = store.Store()
        tk = st.ticket(T)
        get = replay.new_call(ticket=T, at="2026-10-20T10:00:00+05:30", session="s1", method="GET",
                              url="https://qa.acme.test/shop/orders/7", status=200, outcome="worked",
                              response_body=json.dumps({"data": BEFORE}) if before_body else None,
                              response_keys=["data"])
        put = replay.new_call(ticket=T, at="2026-10-20T10:05:00+05:30", session="s1", method="PUT",
                              url="https://qa.acme.test/shop/orders/7", status=200, outcome="worked",
                              body=json.dumps({"status": "OPEN", "firstName": "Test", "storeName": "Acme Downtown Clinic"}))
        tk["api_calls"] = {get["id"]: get, put["id"]: put}
        replay.generate(st, T, common.load_config())
        st.save()
        return st

    def md(self):
        return rd(os.path.join(self.home, "tickets", T, "requests.md"))

    def sh(self):
        return rd(os.path.join(self.home, "tickets", T, "replay.sh"))

    def test_mutating_step_warned_and_confirmed(self):
        self.generate()
        md = self.md()
        rows = [ln for ln in md.splitlines() if re.match(r"^\| \d+ \|", ln)]
        self.assertIn("changes a shared record", rows[1])
        self.assertNotIn("changes a shared record", rows[0])  # the login POST is an auth call
        self.assertNotIn("changes a shared record", rows[2])
        self.assertIn("PUT /shop/orders/7", md)
        sh = self.sh()
        gate = sh.index("\nconfirm_mutation ")
        self.assertLess(sh.index("# --- 1. Login"), gate)
        self.assertLess(gate, sh.index("# --- 2. Update order"))
        self.assertEqual(sh.count("\nconfirm_mutation "), 1)
        self.assertIn("PUT /shop/orders/7", sh[gate:sh.index("\n", gate + 1)])

    @unittest.skipUnless(BASH, "bash not available")
    def test_gate_needs_yes_except_local(self):
        self.generate()
        sh = self.sh()
        start = sh.index('ENV=""')
        end = sh.index("\n}\n", sh.index("confirm_mutation() {")) + 3
        snippet = sh[start:end] + 'confirm_mutation "PUT /shop/orders/7"\necho SENT\n'

        def run(*args, **env):
            e = dict(os.environ, **env)
            return subprocess.run([BASH, "-c", snippet, "replay"] + list(args), capture_output=True, text=True,
                                  timeout=60, env=e)
        r = run("qa")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertNotIn("SENT", r.stdout)
        self.assertIn("--yes", r.stdout)
        self.assertIn("SENT", run("qa", "--yes").stdout)
        self.assertIn("SENT", run("--yes", "qa").stdout)
        self.assertIn("SENT", run("qa", REPLAY_YES="1").stdout)
        self.assertIn("SENT", run("local").stdout)

    def test_repeat_has_bru_file(self):
        from chitragupta import brufile, replay
        self.generate()
        again = os.path.join(self.bdir, "03 Login (again).bru")
        self.assertTrue(os.path.exists(again), os.listdir(self.bdir))
        req = brufile.parse_request(rd(again))
        self.assertEqual(req["seq"], 3)
        self.assertEqual(req["url"], "{{baseUrl}}/auth/login")
        self.assertIn("Repeat of step 1", req["docs"])
        reg = replay.load_registry()["files"]
        self.assertEqual(reg["bruno/%s/03 Login (again).bru" % T]["owner"], "engine")
        bru = [f for f in os.listdir(self.bdir) if f.endswith(".bru") and f != "folder.bru"]
        self.assertEqual(len(bru), self.sh().count("\n# --- "))
        # the user's own files are never edited
        self.assertEqual(rd(os.path.join(self.bdir, "2. Update order.bru")), UPDATE)
        self.assertEqual(rd(os.path.join(self.bdir, "folder.bru")), FOLDER)

    def test_second_generate_changes_nothing(self):
        from chitragupta import common, replay, store
        self.generate()
        snap = {fn: rd(os.path.join(self.bdir, fn)) for fn in os.listdir(self.bdir)}
        md, sh = self.md(), self.sh()
        st = store.Store()
        changed, _ = replay.generate(st, T, common.load_config())
        self.assertEqual(changed, [])
        self.assertEqual({fn: rd(os.path.join(self.bdir, fn)) for fn in os.listdir(self.bdir)}, snap)
        self.assertEqual((self.md(), self.sh()), (md, sh))

    def test_payload_matches_bru(self):
        self.generate()
        p = os.path.join(self.home, "tickets", T, "payloads", "02-update-order.json")
        self.assertEqual(json.loads(rd(p)), {"status": "OPEN", "firstName": "Test", "storeName": "Acme Downtown Clinic"})

    def test_restore_when_before_known(self):
        from chitragupta import brufile
        self.generate()
        restore = [f for f in os.listdir(self.bdir) if "Restore" in f]
        self.assertEqual(len(restore), 1, os.listdir(self.bdir))
        req = brufile.parse_request(rd(os.path.join(self.bdir, restore[0])))
        self.assertEqual(req["method"], "PUT")
        self.assertEqual(req["url"], "{{baseUrl}}/shop/orders/{{orderId}}")
        body = json.loads(req["body"])
        self.assertEqual(set(body), {"status", "firstName", "storeName"})
        self.assertEqual(body["status"], "CLOSED")
        self.assertEqual(req["seq"], 4)
        md = self.md()
        self.assertIn("Restore", md)
        self.assertNotIn("no restore: before-state unknown", md)
        self.assertIn("# --- 4. Restore", self.sh())

    def test_no_restore_without_before_body(self):
        self.generate(before_body=False)
        self.assertFalse([f for f in os.listdir(self.bdir) if "Restore" in f])
        self.assertIn("no restore: before-state unknown", self.md())

    def test_hard_coded_literals_linted(self):
        self.generate()
        md = self.md()
        line = next(ln for ln in md.splitlines() if "hard-coded" in ln.lower())
        self.assertIn("firstName", line)
        self.assertIn("storeName", line)
        self.assertNotIn("Acme Downtown Clinic", line)  # names the keys, never the values
        self.assertNotIn("status", line.split("hard-coded", 1)[1].split("—")[0].replace("first", ""))

    def test_captured_response_body_kept_on_the_call(self):
        from chitragupta import replay
        ev = {"ts": "2026-10-20T10:00:00+05:30", "session_id": "s1", "cwd": self.home,
              "payload": {"tool": "Bash", "input": {"command": "curl -s https://qa.acme.test/shop/orders/7"},
                          "outcome": {"http": {"statuses": [200], "response_keys": ["id"], "body": '{"id": 7}'}}}}
        calls = replay.calls_from_event(ev, T)
        self.assertEqual(calls[0]["response_body"], '{"id": 7}')


if __name__ == "__main__":
    unittest.main()
