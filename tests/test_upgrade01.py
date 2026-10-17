"""Replayable requests, DB flow maps, artifacts (stdlib unittest).

Uses a real http.server mock, real curl, the real hook launcher and real `bash replay.sh`; LLM calls are stubbed
(deterministic). MCP jobs are off in the config and tripwired (tests.support), so nothing reaches Jira or a database.
scripts/e2e/e2e_upgrade01.py runs the same curation/extraction rules against the real models (opt-in).
Synthetic fixtures only: ticket ABC-1001, schema `shop`, an account-lock scenario on a local mock server.
"""
import base64
import glob
import http.server
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from tests.support import PY, Isolated, child_env, install_launcher, rd, rdb, rmtree, run_hook, write_config

BASH = next((p for p in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash") or "") if p and os.path.exists(p)), None)
CURL = shutil.which("curl") or "curl"
TICKET = "ABC-1001"
VAR = "ABC1001"  # ticket-scoped variable prefix
EMAIL = "dev.test@acme.test"
PASS_VALUE = "pw-Correct-123!"
TOKEN = "tok-abc-123-SECRETVALUE"
KEY_VALUE = "key-9f8e7d6c5b4a-SECRET"

# --------------------------------------------------------------------------- mock server


class Mock(http.server.BaseHTTPRequestHandler):
    received = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode() or "null")
        except ValueError:
            return raw.decode()

    def do_GET(self):
        if self.path.startswith("/actuator/health"):
            return self._send(200, {"status": "UP"})
        if self.path.startswith("/api/v1/customers/"):
            return self._send(200, {"id": 42, "isLocked": False, "firstName": "Jane", "email": "jane.doe@acme.test"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        if self.path == "/api/v1/auth/login":
            if isinstance(body, dict) and body.get("password") == PASS_VALUE:
                return self._send(200, {"data": {"accessToken": TOKEN, "expiresIn": 3600}})
            return self._send(401, {"error": "bad credentials"})
        if self.path == "/api/v1/partner/customers/update":
            Mock.received.append({"auth": self.headers.get("Authorization"), "body": body})
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                return self._send(401, {"error": "no token"})
            if not isinstance(body, dict) or "role" not in body:
                return self._send(400, {"error": "role is required"})
            return self._send(200, {"isLocked": body["role"] is None})
        self._send(404, {"error": "not found"})


def start_mock():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------- harness

def make_home():
    h = tempfile.mkdtemp(prefix="cg-up01-")
    write_config(h, {"ticket_prefixes": ["ABC", "XYZ"], "day_starts_at": "00:00", "github_remote": "",
                     "capture_scope": {"include_paths": [], "exclude_paths": []}, "paths": {"python": PY},
                     "git_author_emails": [], "watched_repos": [], "bruno_collections": [],
                     "artifacts": {"watch_dirs": [], "push_binary_types": []},
                     # MCP jobs stay off (the legacy suite had no `mcp` key and reached a real Jira from tick())
                     "mcp": {"daily_call_budget": 10, "jira": {"enabled": False}, "database": {"enabled": False}}})
    return h


def make_repo():
    r = tempfile.mkdtemp(prefix="cg-repo-")
    for a in (["init", "-q", "-b", "main"], ["config", "user.email", "t@acme.test"], ["config", "user.name", "t"],
              ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", "-C", r] + a, check=True, capture_output=True)
    with open(os.path.join(r, "README.md"), "w") as f:
        f.write("# repo\n" * 10)
    subprocess.run(["git", "-C", r, "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", r, "commit", "-q", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", r, "checkout", "-q", "-b", "feature/%s-null-role" % TICKET], check=True, capture_output=True)
    return r


class Session:
    """Feeds the real hook launcher the same JSON Claude Code would."""

    def __init__(self, home, user_dir, repo, sid="s-up01"):
        self.home, self.repo, self.sid = home, repo, sid
        self.env = child_env(home, user_dir)

    def hook(self, event, **inp):
        inp.setdefault("session_id", self.sid)
        inp.setdefault("cwd", self.repo)
        inp.setdefault("transcript_path", "")
        p = run_hook(event, inp, self.env)
        assert p.returncode == 0
        return p.stdout.decode()

    def bash(self, cmd, run=True):
        out = ""
        if run:
            p = subprocess.run([BASH or "bash", "-c", cmd], capture_output=True, text=True, cwd=self.repo, timeout=30)
            out = p.stdout + p.stderr
        self.hook("post-tool", tool_name="Bash", tool_input={"command": cmd}, tool_response={"stdout": out, "stderr": ""})
        return out

    def write(self, path, content):
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        self.hook("post-tool", tool_name="Write", tool_input={"file_path": path, "content": content}, tool_response={"success": True})

    def prompt(self, text):
        self.hook("user-prompt", prompt=text)

    def stop(self, msg):
        self.hook("stop", last_assistant_message=msg)

    def end(self):
        self.hook("session-end", reason="exit")


def fake_llm(extract_result=None):
    """call_claude stub: extraction returns `extract_result` (+ empty arrays); curation picks the latest working
    login and the working trigger, like the curate.md rules ask for."""
    from chitragupta import common, extract

    def fake(kind, stdin_text, cfg, timeout=300):
        if kind in ("extract", "extract_delta"):
            with open(common.prompt_path("schemas/%s.json" % kind), encoding="utf-8") as f:
                schema = json.load(f)
            out = extract.coerce(dict(extract_result or {}), schema)
            return out, 0.0
        if kind == "curate":
            data = json.loads(stdin_text)
            calls = data["calls"]
            login = [c for c in calls if c["url"].endswith("/auth/login") and c["status"] == 200]
            trig = [c for c in calls if c["url"].endswith("/partner/customers/update") and c["status"] == 200]
            pinned = [c for c in calls if c["pinned"]]
            steps = []
            if login:
                steps.append({"call_id": login[-1]["id"], "name": "Login to v2", "purpose": "get a token", "why_needed": "auth",
                              "expected": "200 with accessToken", "critical_fields": [], "role": "auth", "verify_query": "",
                              "repeat_of_step": None, "request": None})
            if trig:
                steps.append({"call_id": trig[-1]["id"], "name": "Partner send - null role locks user", "purpose": "trigger",
                              "why_needed": "reproduces the lock", "expected": "200; is_locked -> true", "critical_fields": ["role = null"],
                              "role": "trigger", "verify_query": "", "repeat_of_step": None, "request": None})
            for c in pinned:
                if c["id"] not in [s["call_id"] for s in steps]:
                    steps.append({"call_id": c["id"], "name": c.get("name_hint") or "", "purpose": "", "why_needed": "pinned",
                                  "expected": c.get("expected_hint") or "", "critical_fields": [], "role": "verify",
                                  "verify_query": "", "repeat_of_step": None, "request": None})
            keep = {s["call_id"] for s in steps}
            return {"scenarios": [{"name": "null role locks user", "summary": "", "steps": steps}] if steps else [],
                    "dropped": [{"call_id": c["id"], "reason": "noise"} for c in calls if c["id"] not in keep]}, 0.0
        return {}, 0.0
    return fake


class Up01Case(Isolated, unittest.TestCase):
    def setUp(self):
        self.isolate(make_home())
        self.repo = make_repo()
        self.tx = tempfile.mkdtemp(prefix="cg-tx-")
        os.environ["CHITRAGUPTA_TRANSCRIPTS_DIR"] = self.tx
        from chitragupta import extract
        self._real = extract.call_claude
        self.s = Session(self.home, self.user_dir, self.repo)
        Mock.received = []

    def tearDown(self):
        from chitragupta import extract
        extract.call_claude = self._real
        for d in (self.repo, self.tx):
            rmtree(d)
        self.restore()

    def tick(self, extract_result=None):
        from chitragupta import agent, extract
        extract.call_claude = fake_llm(extract_result)
        a = agent.Agent(force=True)
        a.tick()
        agent.save_state(a)
        return a

    def raw_text(self):
        return "\n".join(rd(p) for p in glob.glob(os.path.join(self.home, ".worklog", "raw", "*", "*.jsonl")))

    def all_files_text(self, root=None):
        out = []
        for dp, dn, fns in os.walk(root or self.home):
            if ".git" in dp.split(os.sep):
                continue
            for fn in fns:
                try:
                    with open(os.path.join(dp, fn), encoding="utf-8", errors="ignore") as f:
                        out.append((os.path.join(dp, fn), f.read()))
                except OSError:
                    pass
        return out

    def run_null_role_session(self, port):
        b = "http://127.0.0.1:%d" % port
        s = self.s
        s.prompt("%s: reproduce null role locking the user" % TICKET)
        s.bash("curl -s -i -X POST %s/api/v1/auth/login -H 'Content-Type: application/json' -d '{\"email\":\"%s\",\"password\":\"%s\"}'"
               % (b, EMAIL, PASS_VALUE))
        s.bash("curl -s -i -X POST %s/api/v1/auth/login -H 'Content-Type: application/json' -d '{\"email\":\"%s\",\"password\":\"pw-wrong\"}'"
               % (b, EMAIL))
        s.bash("curl -s -i %s/actuator/health" % b)
        s.bash("curl -s -i -X POST %s/api/v1/partner/customers/update -H 'Authorization: Bearer %s' -H 'Content-Type: application/json' "
               "-d '{\"customerId\": 42, \"firstName\": \"Jane\"}'" % (b, TOKEN))
        s.bash("curl -s -i -X POST %s/api/v1/partner/customers/update -H 'Authorization: Bearer %s' -H 'Content-Type: application/json' "
               "-d '{\"customerId\": 42, \"firstName\": \"Jane\", \"role\": null}'" % (b, TOKEN))
        s.bash("curl -s -i %s/api/v1/customers/42 -H 'Authorization: Bearer %s'" % (b, TOKEN))
        s.stop("The null role send locks the user: CustomerService.update sets is_locked=true (CustomerService.java:409).")
        s.end()


# --------------------------------------------------------------------------- tests

class CaptureTests(Up01Case):
    def test_curl_status_and_skeleton_no_values(self):
        srv = start_mock()
        try:
            self.s.bash("curl -s -i -X POST http://127.0.0.1:%d/api/v1/auth/login -H 'Content-Type: application/json' "
                        "-d '{\"email\":\"%s\",\"password\":\"%s\"}'" % (srv.server_port, EMAIL, PASS_VALUE))
        finally:
            srv.shutdown()
            srv.server_close()
        ev = [json.loads(l) for l in self.raw_text().splitlines() if l.strip()][-1]
        http = ev["payload"]["outcome"]["http"]
        self.assertEqual(http["statuses"], [200])
        self.assertEqual(http["response_keys"], {"data": {"accessToken": "string", "expiresIn": "number"}})
        self.assertEqual(http["token_path"], "data.accessToken")
        raw = self.raw_text()
        for v in (TOKEN, PASS_VALUE, EMAIL, "3600"):
            self.assertNotIn(v, raw)

    def test_write_bru_content_redacted(self):
        p = os.path.join(self.repo, "login.bru")
        self.s.write(p, 'post {\n  url: http://x/login\n}\n\nbody:json {\n  {"email": "jane.doe@acme.test", "password": "%s"}\n}\n'
                     % PASS_VALUE)
        ev = [json.loads(l) for l in self.raw_text().splitlines() if l.strip()][-1]
        c = ev["payload"]["input"]["content"]
        self.assertIn("body:json", c)
        self.assertNotIn(PASS_VALUE, c)
        self.assertNotIn("jane.doe@acme.test", c)


@unittest.skipUnless(BASH, "Git Bash not found")
class ReplayTests(Up01Case):
    def setUp(self):
        super().setUp()
        self.srv = start_mock()
        # replay.sh calls the stable launcher (<user>/runtime/run.py), as in a real install
        install_launcher(self.user_dir)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def fill_env(self):
        p = os.path.join(self.home, "bruno", ".env")
        txt = rd(p)
        txt = re.sub(r"(?m)^%s_EMAIL=.*$" % VAR, "%s_EMAIL=%s" % (VAR, EMAIL), txt)
        txt = re.sub(r"(?m)^%s_PASSWORD=.*$" % VAR, "%s_PASSWORD=%s" % (VAR, PASS_VALUE), txt)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(txt)

    def replay(self, env="local"):
        sh = os.path.join(self.home, "tickets", TICKET, "replay.sh")
        return subprocess.run([BASH, sh, env], capture_output=True, text=True, timeout=120, cwd=self.home,
                              env=self.child_env())

    def test_curation_replay_and_privacy(self):
        self.run_null_role_session(self.srv.server_port)
        self.tick()
        bdir = os.path.join(self.home, "bruno", TICKET)
        bru = sorted(f for f in os.listdir(bdir) if f != "folder.bru")
        # exactly two saved requests: login, then the null-role send
        self.assertEqual(len(bru), 2, bru)
        self.assertTrue(bru[0].startswith("01 Login"), bru)
        self.assertTrue(bru[1].startswith("02 Partner send"), bru)
        trig = rd(os.path.join(bdir, bru[1]))
        self.assertIn('"role": null', trig)
        self.assertIn("auth: bearer", trig)
        login = rd(os.path.join(bdir, bru[0]))
        self.assertIn('bru.setVar("token"', login)
        self.assertNotIn("setEnvVar", login)
        md = rd(os.path.join(self.home, "tickets", TICKET, "requests.md"))
        self.assertIn("### 1. Login to v2", md)
        self.assertIn("### 2. Partner send - null role locks user", md)
        self.assertNotIn("### 3.", md)
        self.assertIn("4 other captured calls left out", md)
        sh = rdb(os.path.join(self.home, "tickets", TICKET, "replay.sh"))
        self.assertNotIn(b"\r\n", sh)
        self.assertEqual(sh.count(b"\nsend "), 2)
        payload = json.loads(rd(os.path.join(self.home, "tickets", TICKET, "payloads", "02-partner-send-null-role-locks-user.json")))
        self.assertIn("role", payload)
        self.assertIsNone(payload["role"])
        # README hub
        hub = rd(os.path.join(self.home, "tickets", TICKET, "README.md"))
        self.assertIn("[Requests](requests.md)", hub.split("\n")[1])
        self.assertIn("## Replay requests", hub)
        # replay works: token chained, mock receives JSON null
        r = self.replay()
        self.assertNotEqual(r.returncode, 0)  # credentials not filled yet -> stops before sending anything
        self.assertIn("%s_PASSWORD" % VAR, r.stderr + r.stdout)
        self.fill_env()
        Mock.received = []
        r = self.replay()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("All 2 steps passed", r.stdout)
        self.assertEqual(len(Mock.received), 1)
        self.assertEqual(Mock.received[0]["auth"], "Bearer " + TOKEN)
        self.assertIn("role", Mock.received[0]["body"])
        self.assertIsNone(Mock.received[0]["body"]["role"])
        # privacy: Authorization value and password are in no file (bruno/.env aside, which the user filled)
        for path, txt in self.all_files_text():
            if path.endswith(os.path.join("bruno", ".env")):
                continue
            self.assertNotIn(TOKEN, txt, path)
            self.assertNotIn(PASS_VALUE, txt, path)
        ign = subprocess.run(["git", "-C", self.home, "check-ignore", "-q", "bruno/.env"])
        self.assertEqual(ign.returncode, 0, "bruno/.env must be git-ignored")
        tracked = subprocess.run(["git", "-C", self.home, "ls-files"], capture_output=True, text=True).stdout
        self.assertNotIn("bruno/.env\n", tracked + "\n")
        self.assertNotIn("bruno/", tracked)  # bruno/ is local-only
        self.assertTrue(os.path.exists(os.path.join(self.home, "bruno", ".env.example")))
        # idempotency: another tick changes nothing
        before = {p: t for p, t in self.all_files_text(os.path.join(self.home, "bruno"))}
        before.update({p: t for p, t in self.all_files_text(os.path.join(self.home, "tickets"))})
        self.tick()
        after = {p: t for p, t in self.all_files_text(os.path.join(self.home, "bruno"))}
        after.update({p: t for p, t in self.all_files_text(os.path.join(self.home, "tickets"))})
        self.assertEqual(before, after)
        from chitragupta import store
        tk = store.Store().ticket(TICKET)
        self.assertEqual(len(tk["api_calls"]), 6)

    def test_my_edits_win(self):
        self.run_null_role_session(self.srv.server_port)
        self.tick()
        bdir = os.path.join(self.home, "bruno", TICKET)
        bru = sorted(f for f in os.listdir(bdir) if f != "folder.bru")
        trig = os.path.join(bdir, bru[1])
        # edit a generated file in "Bruno"
        txt = rd(trig).replace('"customerId": 42', '"customerId": 43')
        with open(trig, "w", encoding="utf-8", newline="\n") as f:
            f.write(txt)
        # force a re-curation with a new capture of the same trigger
        self.s.sid = "s-up01-b"
        self.s.bash("curl -s -i -X POST http://127.0.0.1:%d/api/v1/partner/customers/update -H 'Authorization: Bearer %s' "
                    "-H 'Content-Type: application/json' -d '{\"customerId\": 42, \"firstName\": \"Jane\", \"role\": null}'"
                    % (self.srv.server_port, TOKEN))
        self.s.end()
        self.tick()
        self.assertIn('"customerId": 43', rd(trig))
        self.assertIn("43", rd(os.path.join(self.home, "tickets", TICKET, "payloads", "02-partner-send-null-role-locks-user.json")))
        from chitragupta import replay
        reg = replay.load_registry()["files"]
        self.assertEqual(reg["bruno/%s/%s" % (TICKET, bru[1])]["owner"], "user")
        # delete the login -> never recreated
        os.remove(os.path.join(bdir, bru[0]))
        self.s.sid = "s-up01-c"
        self.s.bash("curl -s -i -X POST http://127.0.0.1:%d/api/v1/auth/login -H 'Content-Type: application/json' "
                    "-d '{\"email\":\"%s\",\"password\":\"%s\"}'" % (self.srv.server_port, EMAIL, PASS_VALUE))
        self.s.end()
        self.tick()
        self.assertFalse(any(f.startswith("01") or "Login" in f for f in os.listdir(bdir)), os.listdir(bdir))
        self.assertTrue(replay.load_registry()["files"]["bruno/%s/%s" % (TICKET, bru[0])]["tombstone"])
        # add one -> adopted
        mine = os.path.join(bdir, "09 My check.bru")
        with open(mine, "w", encoding="utf-8", newline="\n") as f:
            f.write("meta {\n  name: 09 My check\n  type: http\n  seq: 9\n}\n\nget {\n  url: {{local%dBaseUrl}}/api/v1/customers/42\n"
                    "  body: none\n  auth: none\n}\n" % self.srv.server_port)
        self.tick()
        self.assertEqual(replay.load_registry()["files"]["bruno/%s/09 My check.bru" % TICKET]["owner"], "user")
        md = rd(os.path.join(self.home, "tickets", TICKET, "requests.md"))
        self.assertIn("My check", md)
        self.assertNotIn("Login to v2", md)


class PrivacyTests(Up01Case):
    def test_person_values_become_variables(self):
        from chitragupta import apicalls, replay
        idx = apicalls.EnvIndex({"QA": {"svcBaseUrl": "https://svc-qa.acme.test"}})
        call = {"ticket": "ABC-1", "method": "POST", "url": "https://svc-qa.acme.test/api/patients",
                "headers": [["Authorization", "Bearer " + TOKEN], ["X-Api-Key", KEY_VALUE]],
                "body": json.dumps({"firstName": "John", "lastName": "Smith", "dob": "1980-01-02", "phone": "(713) 555-0100",
                                    "mrn": "00123456", "role": None, "active": True, "testCode": "1800"})}
        req, vs, mech, env = apicalls.template_call(call, idx)
        body = json.loads(req["body"])
        for k in ("firstName", "lastName", "dob", "phone", "mrn"):
            self.assertTrue(body[k].startswith("{{process.env.ABC1_"), (k, body[k]))
        self.assertIsNone(body["role"])
        self.assertIs(body["active"], True)
        self.assertEqual(body["testCode"], "1800")
        self.assertEqual(req["url"], "{{svcBaseUrl}}/api/patients")
        defaults = {v["name"]: v["default"] for v in vs}
        self.assertEqual(defaults["ABC1_DOB"], "1990-01-01")
        self.assertEqual(defaults["ABC1_FIRST_NAME"], "Test")
        changed = []
        replay.ensure_dotenv(vs, changed)
        env_txt = rd(os.path.join(self.home, "bruno", ".env"))
        self.assertIn("ABC1_DOB=1990-01-01", env_txt)
        self.assertIn("ABC1_MRN=MRN0000001", env_txt)
        self.assertNotIn("John", env_txt)
        self.assertNotIn("00123456", env_txt)
        ex = rd(os.path.join(self.home, "bruno", ".env.example"))
        self.assertIn("ABC1_DOB=\n", ex)
        # an existing value is never changed
        txt = env_txt.replace("ABC1_DOB=1990-01-01", "ABC1_DOB=1975-05-05")
        with open(os.path.join(self.home, "bruno", ".env"), "w", encoding="utf-8") as f:
            f.write(txt)
        replay.ensure_dotenv(vs, changed)
        self.assertIn("ABC1_DOB=1975-05-05", rd(os.path.join(self.home, "bruno", ".env")))
        dumped = json.dumps(req)
        self.assertNotIn(TOKEN, dumped)
        self.assertNotIn(KEY_VALUE, dumped)

    def test_scanner_keeps_variable_references(self):
        from chitragupta.redact import redact
        t = '{"email": "{{process.env.ABC1003_EMAIL}}", "password": "{{process.env.ABC1003_PASSWORD}}", "url": "{{orderSvcBaseUrl}}/x"}'
        self.assertEqual(redact(t), t)
        self.assertEqual(redact('"email": "a.b@acme.test"'), '"email": "x.x@xxxx.xxxx"')

    def test_bruno_file_with_literal_is_held_not_rewritten(self):
        from chitragupta import common, gitsync
        gitsync.ensure_repo(common.load_config())
        d = os.path.join(self.home, "bruno", "ABC-1")
        os.makedirs(d)
        p = os.path.join(d, "1. Login.bru")
        txt = 'post {\n  url: {{x}}/login\n}\n\nbody:json {\n  {"email": "real.person@acme.test", "password": "{{process.env.P}}"}\n}\n'
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(txt)
        h = {}
        gitsync.commit(common.load_config(), "test", h)
        self.assertEqual(rd(p), txt)  # the user's file is untouched
        tracked = subprocess.run(["git", "-C", self.home, "ls-files"], capture_output=True, text=True).stdout
        self.assertNotIn("bruno/ABC-1/1. Login.bru", tracked)
        self.assertFalse(h.get("held_back"))  # bruno/ is never pushed, so nothing needs holding back


class DbFlowTests(Up01Case):
    FLOW = {"data_flow": [
        {"ticket": TICKET, "scenario": "null role locks user", "step": 1, "trigger": "POST /api/v1/partner/customers/update",
         "component": "CustomerService.update (CustomerService.java:409)", "operation": "update", "table": "shop.customers",
         "columns_read": ["role"], "columns_written": [{"column": "is_locked", "change": "false → true", "condition": "payload.role IS NULL"}],
         "via": "JPA/EF entity", "transaction": "", "evidence": ["CustomerService.java:409"], "confidence": "confirmed"},
        {"ticket": TICKET, "scenario": "null role locks user", "step": 2, "trigger": "", "component": "psql verify",
         "operation": "select", "table": "shop.customers", "columns_read": ["is_locked"], "columns_written": [],
         "via": "native SQL", "transaction": "", "evidence": ["SELECT is_locked FROM shop.customers"], "confidence": "confirmed"}],
        "table_relations": [{"ticket": TICKET, "from": "shop.orders.customer_id", "to": "shop.customers.id",
                             "kind": "fk", "evidence": "Order.java:22"}],
        "queries": [{"ticket": TICKET, "dialect": "postgresql", "text": "SELECT id, is_locked FROM shop.customers WHERE id = :id",
                     "purpose": "is_locked after partner send", "tables": ["shop.customers"], "kind": "read", "revealed": "is_locked true"},
                    {"ticket": TICKET, "dialect": "postgresql", "text": "UPDATE shop.customers SET is_locked = false WHERE id = :id",
                     "purpose": "unlock test customer", "tables": ["shop.customers"], "kind": "data_fix", "revealed": ""}]}

    def test_db_flow_rendered(self):
        from chitragupta import mermaid_check, store
        s = self.s
        s.prompt("%s: how does a null role lock the user?" % TICKET)
        s.hook("post-tool", tool_name="Read", tool_input={"file_path": self.repo + "/Customer.java"})
        s.hook("post-tool", tool_name="Read", tool_input={"file_path": self.repo + "/CustomerService.java"})
        s.bash("psql -c \"UPDATE shop.customers SET is_locked = false WHERE id = 42\"", run=False)
        s.bash("psql -c \"SELECT id, is_locked FROM shop.customers WHERE id = 42\"", run=False)
        s.stop("CustomerService.update sets is_locked = true when the payload role is null (CustomerService.java:409).")
        s.end()
        self.tick(self.FLOW)
        md = rd(os.path.join(self.home, "tickets", TICKET, "db-flow.md"))
        self.assertIn("flowchart LR", md)
        self.assertIn("sequenceDiagram", md)
        self.assertIn("erDiagram", md)
        self.assertIn("| `shop.customers.is_locked` | false → true | CustomerService.update (CustomerService.java:409) | payload.role IS NULL |", md)
        self.assertRegex(md, r"\[is_locked after partner send\]\(queries\.md#q-[0-9a-f]+\)")
        self.assertEqual(mermaid_check.check(md), [])
        hub = rd(os.path.join(self.home, "tickets", TICKET, "README.md"))
        self.assertIn("## Scenarios", hub)
        self.assertIn("`is_locked`: false → true", hub)
        dm = rd(os.path.join(self.home, "knowledge", "db-map.md"))
        self.assertIn("shop.customers", dm)
        self.assertEqual(mermaid_check.check(dm), [])
        ctx = rd(os.path.join(self.home, ".worklog", "cache", "context", TICKET + ".md"))
        self.assertIn("DB flow: tables shop.customers", ctx)
        # idempotent
        n = len(store.Store().ticket(TICKET)["data_flow"])
        self.s.sid = "s2"
        self.s.prompt("%s: again" % TICKET)
        self.s.stop("same as before")
        self.s.end()
        self.tick(self.FLOW)
        self.assertEqual(len(store.Store().ticket(TICKET)["data_flow"]), n)

    def test_mermaid_checker_rejects_bad(self):
        from chitragupta import mermaid_check
        good = "```mermaid\nflowchart LR\n    A[\"x\"] -->|\"y\"| B[(\"z\")]\n```\n```mermaid\nsequenceDiagram\n    participant A as Client\n" \
               "    participant B as API\n    A->>B: GET /x\n```\n" \
               "```mermaid\nerDiagram\n    CUSTOMER ||--o{ ORDER : places\n    ORDER {\n        string id PK\n    }\n```"
        self.assertEqual(mermaid_check.check(good), [])
        self.assertTrue(mermaid_check.check("```mermaid\nflowchart LR\n    A[\"a \"quoted\" b\"] --> B\n```"))
        self.assertTrue(mermaid_check.check("```mermaid\nsequenceDiagram\n    A->>B: x\n```"))
        self.assertTrue(mermaid_check.check("```mermaid\nerDiagram\n    a.b ||--o{ c : x\n```"))


class BlockTests(Up01Case):
    def test_blocks_ingested_exactly_once(self):
        from chitragupta import extract, store
        req_block = {"ticket": TICKET, "scenario": "null role locks user", "requests": [
            {"name": "Login to v2", "method": "POST", "url": "http://127.0.0.1:9/api/v1/auth/login",
             "headers": {"Content-Type": "application/json"},
             "body": {"email": "{{process.env.%s_EMAIL}}" % VAR, "password": "{{process.env.%s_PASSWORD}}" % VAR}, "expected": "200"},
            {"name": "Partner send - null role", "method": "POST", "url": "http://127.0.0.1:9/api/v1/partner/customers/update",
             "headers": {"Authorization": "Bearer {{token}}"}, "body": {"customerId": 42, "role": None},
             "expected": "200; is_locked -> true", "critical_fields": ["role = null"]}]}
        flow_block = {"ticket": TICKET, "overview": "Null role locks the customer.", "data_flow": [
            {"scenario": "null role locks user", "step": 1, "trigger": "POST /api/v1/partner/customers/update",
             "component": "CustomerService.update (CustomerService.java:409)", "operation": "update", "table": "shop.customers",
             "columns_read": ["role"], "columns_written": [{"column": "is_locked", "change": "false -> true", "condition": "role is null"}],
             "via": "JPA/EF entity", "evidence": ["CustomerService.java:409"], "confidence": "hypothesis"}],
            "table_relations": []}
        msg = ("Here is the trace." + " filler" * 3000 + "\n\n```worklog-requests\n" + json.dumps(req_block) + "\n```\n\n"
               "```worklog-dbflow\n" + json.dumps(flow_block) + "\n```\n")
        self.assertGreater(len(msg), 12000)
        self.s.prompt("%s: save the login and the partner null-role send to the worklog, and map the DB flow" % TICKET)
        self.s.stop(msg)
        self.s.end()
        seen = {}
        real_build = extract.build_digests

        def spy(events, meta, cands, already):
            out = real_build(events, meta, cands, already)
            seen["digest"] = "\n".join(out)
            return out
        extract.build_digests = spy
        try:
            self.tick()
        finally:
            extract.build_digests = real_build
        tk = store.Store().ticket(TICKET)
        calls = list(tk["api_calls"].values())
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["pinned"] for c in calls))
        e = list(tk["data_flow"].values())
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["confidence"], "hypothesis")  # kept exactly as written
        self.assertEqual(e[0]["source"], "worklog-dbflow block")
        self.assertEqual(tk["dbflow_overview"], "Null role locks the customer.")
        self.assertIn("[worklog-requests block: ingested separately]", seen["digest"])
        self.assertNotIn('"customerId": 42', seen["digest"])
        bdir = os.path.join(self.home, "bruno", TICKET)
        self.assertEqual(sorted(f for f in os.listdir(bdir) if f != "folder.bru"),
                         ["01 Login to v2.bru", "02 Partner send - null role.bru"])
        self.assertIn('"role": null', rd(os.path.join(bdir, "02 Partner send - null role.bru")))
        # again: no duplicates
        self.s.sid = "s-again"
        self.s.stop(msg)
        self.tick()
        tk = store.Store().ticket(TICKET)
        self.assertEqual(len(tk["api_calls"]), 2)
        self.assertEqual(len(tk["data_flow"]), 1)

    def test_invalid_block_is_logged_and_left_as_text(self):
        from chitragupta import store
        self.s.prompt("%s: save it" % TICKET)
        self.s.stop("ok\n```worklog-requests\n{not json}\n```\n")
        self.s.end()
        self.tick()
        self.assertEqual(store.Store().ticket(TICKET).get("api_calls") or {}, {})
        log = rd(os.path.join(self.home, ".worklog", "logs", "agent.log"))
        self.assertIn("invalid worklog-requests block", log)


class ArtifactTests(Up01Case):
    def test_artifacts_filed(self):
        from chitragupta import store
        s = self.s
        s.prompt("%s: write up the analysis" % TICKET)
        s.write(os.path.join(self.repo, "partner-lock-analysis.md"), "# Partner lock analysis\n\nNull role locks the user.\n" + "detail\n" * 20)
        img = base64.b64encode(b"\x89PNG" + b"0" * 200).decode()
        s.write(os.path.join(self.repo, "lock-report.html"),
                "<html><head><title>Lock report</title></head><body><h1>Report</h1><img src=\"data:image/png;base64,%s\"></body></html>" % img)
        s.write(os.path.join(self.repo, "locked-users.csv"), "id,email,is_locked\n1,a@acme.test,true\n2,b@acme.test,true\n3,c@acme.test,false\n")
        png = os.path.join(self.repo, "screenshot-login.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + os.urandom(400))
        s.bash("echo screenshot taken", run=False)
        s.end()
        drop = os.path.join(self.home, "_drop")
        os.makedirs(drop, exist_ok=True)
        dp = os.path.join(drop, "%s_claude-ai-notes.md" % TICKET)
        with open(dp, "w", encoding="utf-8") as f:
            f.write("# Notes from claude.ai\n\n" + "line\n" * 20)
        os.utime(dp, (time.time() - 60, time.time() - 60))
        extraction = {"artifacts": [{"path": png, "ticket": TICKET, "kind": "screenshot", "purpose": "Login error after the lock"},
                                    {"path": os.path.join(self.repo, "locked-users.csv"), "ticket": TICKET, "kind": "export",
                                     "purpose": "Locked customers export"}]}
        self.tick(extraction)
        adir = os.path.join(self.home, "tickets", TICKET, "artifacts")
        names = os.listdir(adir)
        md = [n for n in names if n.endswith("partner-lock-analysis.md")]
        html = [n for n in names if n.endswith("lock-report.html")]
        self.assertTrue(md and html, names)
        pushed_html = rd(os.path.join(adir, html[0]))
        self.assertNotIn("data:image/png", pushed_html)
        self.assertTrue(os.path.exists(os.path.join(adir, "local", html[0])))
        self.assertFalse([n for n in names if n.endswith("locked-users.csv")])
        summ = rd(os.path.join(adir, [n for n in names if n.endswith("locked-users.csv.summary.md")][0]))
        self.assertIn("Rows: 3", summ)
        self.assertIn("`is_locked`", summ)
        self.assertNotIn("a@acme.test", summ)
        self.assertFalse([n for n in names if n.endswith(".png")])
        self.assertTrue([n for n in os.listdir(os.path.join(adir, "local")) if n.endswith("screenshot-login.png")])
        idx = rd(os.path.join(adir, "INDEX.md"))
        for want in ("Partner lock analysis", "Lock report", "Locked customers export", "Login error after the lock", "Notes from claude.ai"):
            self.assertIn(want, idx)
        self.assertIn("local-only", idx)
        self.assertFalse(os.path.exists(dp), "dropped file should be moved")
        self.assertTrue([n for n in names if n.endswith("claude-ai-notes.md")])
        ign = subprocess.run(["git", "-C", self.home, "check-ignore", "-q", "tickets/%s/artifacts/local/x.png" % TICKET])
        self.assertEqual(ign.returncode, 0)
        daily = glob.glob(os.path.join(self.home, "daily", "*", "*", "*.md"))[0]
        self.assertIn("**New artifacts**", rd(daily))
        # idempotent
        n = len(store.Store().ticket(TICKET)["artifacts"])
        self.tick(extraction)
        self.assertEqual(len(store.Store().ticket(TICKET)["artifacts"]), n)
        self.assertEqual(n, 5)


class BruFileTests(unittest.TestCase):
    def test_roundtrip_and_run_order(self):
        from chitragupta import brufile, replay
        txt = ('meta {\n  name: 2. Disable\n  type: http\n  seq: 2\n}\n\nput {\n  url: {{u}}/x/{{id}}\n  body: json\n  auth: apikey\n}\n\n'
               'auth:apikey {\n  key: authorization\n  value: api-key {{process.env.K}}\n  placement: header\n}\n\n'
               'body:json {\n  {\n    "role": null\n  }\n}\n\nvars:pre-request {\n  id: 5\n}\n\ndocs {\n  hello\n}\n')
        r = brufile.parse_request(txt)
        self.assertEqual(r["method"], "PUT")
        self.assertEqual(r["auth"]["value"], "api-key {{process.env.K}}")
        self.assertEqual(json.loads(r["body"]), {"role": None})
        self.assertEqual(brufile.parse_request(brufile.render_request(r))["body"], r["body"])
        files = {"1. Login.bru": {"req": {"seq": 1}}, "2. Disable.bru": {"req": {"seq": 2}}}
        docs = "x\n\nRun in order:\n  1. Login     should return 200\n  2. Disable   role null\n  1. Login again   fix: 403 denied\n                  bug: 423 locked\n"
        order = replay.run_order(docs, files)
        self.assertEqual([o[0] for o in order], ["1. Login.bru", "2. Disable.bru", "1. Login.bru"])
        self.assertEqual(replay.expected_codes(order[2][1]), [403])


if __name__ == "__main__":
    unittest.main()
