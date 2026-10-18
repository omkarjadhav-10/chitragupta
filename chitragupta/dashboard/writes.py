"""Write protection for the dashboard's few write routes, and the routes themselves.

Reads stay GET/HEAD. A write is answered only when every check passes, in this order:
  1. the exact path and method are in WRITES (anything else: 404 / 405)
  2. Host is 127.0.0.1:<port> or localhost:<port> (checked by the server before this)
  3. Origin is present and is this server (a missing or "null" Origin is refused: sandboxed frames send "null")
  4. X-Chitragupta-Token equals the token minted when the server started; the page gets it in a <meta> tag. Other
     sites can't read the page (no CORS), so they can't learn it, and a custom header forces a preflight we never allow
  5. Content-Type is application/json, Content-Length is given and within the route's cap (bytes actually read are
     capped too)
  6. at most RATE_LIMIT writes per minute
  7. the body is a JSON object
Then the handler runs; whatever it changes goes through the safety layer (configstore / safety.paths), and every
write is recorded in the settings history.
"""
import collections
import hmac
import json
import threading
import time

from .. import common
from .. import configstore

TOKEN_HEADER = "X-Chitragupta-Token"
RATE_LIMIT = 30          # writes per minute, across all routes
DEFAULT_CAP = 16 * 1024  # bytes
ACTIONS = {}             # name -> (handler(body) -> (status, obj), cap)


class WriteError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def action(name, cap=DEFAULT_CAP):
    """Register an allowlisted dashboard action (POST /api/actions/<name>)."""
    def wrap(fn):
        ACTIONS[name] = (fn, cap)
        return fn
    return wrap


# --------------------------------------------------------------------------- settings routes

def _settings_patch(body):
    changes = body.get("changes")
    try:
        from .. import settings
        res = settings.apply(changes, "dashboard")
    except ValueError as e:
        errors = e.args[0] if e.args and isinstance(e.args[0], dict) else {"": str(e)}
        return 422, {"error": "Some values aren't allowed; nothing was saved.", "fields": errors}
    except configstore.ConfigError as e:
        return 409, {"error": str(e)}
    return 200, {"ok": True, "id": res["id"], "changes": res["changes"]}


def _settings_undo(body):
    try:
        res = configstore.undo("dashboard")
    except configstore.ConfigError as e:
        return 409, {"error": "Nothing was undone: %s." % e}
    return 200, {"ok": True, "id": res["id"], "changes": res["changes"]}


ROUTES = {
    ("PATCH", "/api/settings"): (_settings_patch, DEFAULT_CAP),
    ("POST", "/api/settings/undo"): (_settings_undo, 1024),
}


def route(method, path):
    """(handler, cap) for an allowed write, or raise WriteError(404/405)."""
    if path.startswith("/api/actions/"):
        name = path[len("/api/actions/"):]
        if name not in ACTIONS:
            raise WriteError(404, "unknown action")
        if method != "POST":
            raise WriteError(405, "use POST")
        return ACTIONS[name]
    hit = ROUTES.get((method, path))
    if hit:
        return hit
    if any(p == path for _, p in ROUTES):
        raise WriteError(405, "method not allowed")
    raise WriteError(404, "not found")


def is_write_path(path):
    return path.startswith("/api/actions/") or any(p == path for _, p in ROUTES)


# --------------------------------------------------------------------------- the checks

class Limiter:
    def __init__(self, per_minute=RATE_LIMIT):
        self.per_minute = per_minute
        self.hits = collections.deque()
        self.lock = threading.Lock()

    def allow(self):
        now = time.monotonic()
        with self.lock:
            while self.hits and now - self.hits[0] > 60:
                self.hits.popleft()
            if len(self.hits) >= self.per_minute:
                return False
            self.hits.append(now)
            return True


def check(headers, allowed_hosts, token, limiter, cap):
    """Raise WriteError unless the request may write. Returns the declared body length."""
    origin = (headers.get("Origin") or "").strip().lower().rstrip("/")
    if not origin or origin == "null" or origin not in {"http://" + h for h in allowed_hosts}:
        raise WriteError(403, "this request didn't come from the dashboard page")
    given = headers.get(TOKEN_HEADER) or ""
    if not given or not hmac.compare_digest(given.encode(), token.encode()):
        raise WriteError(403, "missing or wrong page token; reload the page")
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise WriteError(415, "send JSON")
    try:
        n = int(headers.get("Content-Length") or "")
    except ValueError:
        raise WriteError(411, "Content-Length is required")
    if n < 0 or n > cap:
        raise WriteError(413, "request too large")
    if not limiter.allow():
        raise WriteError(429, "too many changes in a minute; wait a moment")
    return n


def parse(raw):
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        raise WriteError(400, "the body isn't valid JSON")
    if not isinstance(body, dict):
        raise WriteError(400, "the body must be a JSON object")
    return body


def record_action(name, ok, detail=""):
    """Actions are written to the settings history too, so every dashboard write is on record."""
    try:
        from ..safety import paths
        row = {"at": common.now_iso(), "id": "act-" + time.strftime("%Y%m%d%H%M%S"), "source": "dashboard",
               "kind": "action", "key": "action:" + name, "old": None, "new": "ok" if ok else "failed"}
        if detail:
            row["note"] = str(detail)[:300]
        paths.append_text(configstore.history_path(), json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n",
                          caller="dashboard")
    except Exception:  # noqa: BLE001 — the history line must never break the action itself
        pass
