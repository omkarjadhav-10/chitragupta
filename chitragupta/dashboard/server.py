"""The dashboard's HTTP server: 127.0.0.1 only, GET/HEAD only, Host checked, strict headers, read-only.

Routes
  / and /index.html      the page (app/index.html)
  /app/<path>            the page's own files: only files that exist under app/ with an allowed extension
                         (.js .css .svg .png .woff2 .webmanifest .json), looked up in an allowlist map
  /manifest.webmanifest  app/manifest.webmanifest
  /vendor/mermaid.min.js the vendored Mermaid build (third_party/mermaid)
  /vendor/preact.module.js, /vendor/hooks.module.js   the vendored Preact build (third_party/preact)
  /api/health            identity: {"app", "version", "pid", "home"} — the port is the lock, this says who holds it
  /api/today             time per ticket today + tonight's Jira-worklog preview (read-only, no model call)
  /api/day/<YYYY-MM-DD>  the same for any day (a past day's preview is composed whole)
  /api/calendar?from=&to=   minutes per day with leave / holidays / targets (default: the last 365 days)
  /api/knowledge         tables, relations, writers, APIs and integrations from the catalog and the QA link map
  /api/search?q=&type=&limit=   full-text search (in-memory index, see search.py)
  /api/setup             a read-only setup checklist (doctor's fast mode, cached 60 s)
  /api/events            Server-Sent Events: "cursor" events when /api/changes would move (max 16 streams)
  /api/tickets[?q=]      ticket list
  /api/tickets/<KEY>     one ticket: findings, requests (+ curl), DB flow, queries, artifacts, branches & PRs
  /api/tickets/<KEY>/export?mode=full|shareable   the ticket page as one self-contained HTML file (attachment;
                         built in memory, nothing is written). /api/ticket/<KEY>/export is the same.
  /api/timesheet?period=week|month[&limit=N]   hours per ticket per ISO week / month, leave and holidays
  /api/timesheet.csv?period=week|month         the same as CSV
  /api/status            health page data
  /api/changes           {"cursor"} — moves when any file the page reads changes
  /api/shutdown?token=T  stops the server (token from the user folder; used by `dashboard stop` / ensure)
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import select
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import __version__ as VERSION  # read once: a reinstall swaps files under a running server
from .. import APP
from .. import common
from ..safety import paths
from . import api

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "app")
PAGE = os.path.join(APP_DIR, "index.html")
MANIFEST = os.path.join(APP_DIR, "manifest.webmanifest")
# third_party sits next to the package in a checkout and inside it in the runtime copy
THIRD_PARTY = [os.path.join(os.path.dirname(common.PACKAGE_DIR), "third_party"), os.path.join(common.PACKAGE_DIR, "third_party")]
MERMAID_CANDIDATES = [os.path.join(d, "mermaid", "mermaid.min.js") for d in THIRD_PARTY]
VENDOR = {"/vendor/preact.module.js": ("preact", "preact.module.js"), "/vendor/hooks.module.js": ("preact", "hooks.module.js")}
STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".woff2": "font/woff2", ".webmanifest": "application/manifest+json", ".json": "application/json",
}
STATIC_RESCAN = 2.0  # seconds between walks of app/, so edits during development show up
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; manifest-src 'self'; worker-src 'none'; object-src 'none'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
SECURITY_HEADERS = [
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
]
TICKET_PATH = re.compile(r"^/api/tickets/([A-Za-z0-9-]{1,20})$")
EXPORT_PATH = re.compile(r"^/api/tickets?/([A-Za-z0-9-]{1,20})/export$")
DAY_PATH = re.compile(r"^/api/day/(\d{4}-\d{2}-\d{2})$")
MAX_STREAMS = 16
STREAM_POLL = 1.0       # seconds between change checks on an open event stream
STREAM_KEEPALIVE = 25.0  # seconds of silence before a ": keep-alive" comment


def _first_file(paths):
    for p in paths:
        if os.path.isfile(p):
            return p
    return None


def mermaid_path():
    return _first_file(MERMAID_CANDIDATES)


def vendor_path(sub, name):
    return _first_file([os.path.join(d, sub, name) for d in THIRD_PARTY])


_static = {"at": None, "map": {}}
_static_lock = threading.Lock()


def static_map():
    """{"/app/<relative path>": absolute path} of every servable file under app/ (re-walked at most every 2 s).
    A request is answered only by an exact key lookup here: the URL is never joined onto a folder."""
    now = time.monotonic()
    with _static_lock:
        if _static["at"] is not None and now - _static["at"] < STATIC_RESCAN:
            return _static["map"]
    out = {}
    for dp, dns, fns in os.walk(APP_DIR):
        dns[:] = [d for d in dns if not d.startswith(".")]
        for fn in fns:
            p = os.path.join(dp, fn)
            if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in STATIC_TYPES or os.path.islink(p):
                continue
            out["/app/" + os.path.relpath(p, APP_DIR).replace(os.sep, "/")] = p
    with _static_lock:
        _static.update(at=now, map=out)
    return out


def token_path(port):
    return os.path.join(common.user_dir(), "dashboard", "%d.token" % int(port))


def log(msg):
    """The dashboard's own log (stdout/stderr go to <home>/.worklog/logs/dashboard.log when spawned)."""
    try:
        sys.stderr.write("%s [%d] %s\n" % (common.now_iso(), os.getpid(), msg))
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # the port is the lock: never share it (SO_REUSEADDR on Windows would)
    request_queue_size = 32

    def __init__(self, port, cfg=None):
        self.cfg = cfg if cfg is not None else common.load_config()
        self.started_at = common.now_iso()
        self.token = secrets.token_urlsafe(24)
        self.home = common.home()
        self.stopping = threading.Event()  # set on shutdown: open event streams end within a poll
        self.streams = 0
        self.streams_lock = threading.Lock()
        super().__init__(("127.0.0.1", int(port)), Handler)

    def shutdown(self):
        self.stopping.set()
        super().shutdown()

    def server_close(self):
        self.stopping.set()
        super().server_close()

    def open_stream(self):
        """Count one more event stream; False when MAX_STREAMS are already open."""
        with self.streams_lock:
            if self.streams >= MAX_STREAMS:
                return False
            self.streams += 1
            return True

    def close_stream(self):
        with self.streams_lock:
            self.streams = max(0, self.streams - 1)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: refuse a second listener on the same port
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        elif os.name != "nt":  # POSIX SO_REUSEADDR only rebinds over TIME_WAIT; it never allows two listeners
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # TCPServer.server_bind, without HTTPServer's getfqdn() (slow on some Windows machines)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]

    @property
    def port(self):
        return self.server_address[1]

    def allowed_hosts(self):
        return {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port}

    def identity(self):
        return {"app": APP, "version": VERSION, "pid": os.getpid(), "home": self.home}

    def write_token(self):
        p = token_path(self.port)
        try:
            paths.write_text(p, json.dumps({"pid": os.getpid(), "token": self.token, "version": VERSION}))
        except OSError as e:
            log("could not write the shutdown token: %s" % e)

    def remove_token(self):
        p = token_path(self.port)
        try:
            with open(p, encoding="utf-8") as f:
                mine = json.load(f).get("token") == self.token
            if mine:
                paths.delete(p)
        except (OSError, ValueError):
            pass

    def handle_error(self, request, client_address):  # never print tracebacks per connection to a console
        log("connection error from %s: %s" % (client_address, traceback.format_exc().splitlines()[-1]))


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "Chitragupta"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    # Python < 3.13 treats a one-word request line as HTTP/0.9 and would answer a malformed
    # request without a status line; we never speak HTTP/0.9.
    default_request_version = "HTTP/1.0"
    timeout = 15  # a client that stalls mid-request is dropped
    error_content_type = "application/json; charset=utf-8"

    # ------------------------------------------------------------------ plumbing
    def version_string(self):
        return self.server_version

    def log_message(self, fmt, *args):  # quiet: only errors are logged
        pass

    def _drain_body(self, limit=65536):
        """Read (and drop) a small request body we are about to reject; larger ones are left unread."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < n <= limit:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    def handle_one_request(self):
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline, self.request_version, self.command = "", "HTTP/1.1", ""
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():  # sends its own 400/505
                return
            if getattr(self.headers, "defects", None):  # e.g. a header line without a colon
                self.send_error(400, "malformed headers")
                return
            if self.command not in ("GET", "HEAD"):
                self._drain_body()  # closing with unread bytes makes Windows send RST instead of our 405
                self._send(405, _json_bytes({"error": "method not allowed"}), "application/json; charset=utf-8",
                           [("Allow", "GET, HEAD")])
                self.close_connection = True
                return
            self.dispatch()
            self.wfile.flush()
        except (socket.timeout, TimeoutError, ConnectionError):
            self.close_connection = True
        except Exception:  # noqa: BLE001 — a bad request never takes the server down
            log("request failed: %s" % traceback.format_exc())
            self.close_connection = True
            try:
                self._send(500, _json_bytes({"error": "internal error"}), "application/json; charset=utf-8")
            except Exception:  # noqa: BLE001
                pass

    def send_error(self, code, message=None, explain=None):
        """Every error, including the stdlib's own (bad request line, bad version), gets our headers."""
        try:
            short = self.responses.get(code, ("Error",))[0]
        except Exception:  # noqa: BLE001
            short = "Error"
        self.close_connection = True
        try:
            self._send(code, _json_bytes({"error": message or short}), "application/json; charset=utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _send(self, code, body, ctype, extra=None, cache="no-store"):
        self.send_response(code)
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in extra or []:
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, code=200):
        self._send(code, _json_bytes(obj), "application/json; charset=utf-8")

    def send_file(self, path, ctype, cache):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self.send_json({"error": "not found"}, 404)
        etag = '"%s"' % hashlib.sha1(body).hexdigest()[:20]
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            for k, v in SECURITY_HEADERS:
                self.send_header(k, v)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, body, ctype, [("ETag", etag)], cache=cache)

    # ------------------------------------------------------------------ checks
    def host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts():
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            if origin.lower().rstrip("/") not in {"http://" + h for h in self.server.allowed_hosts()}:
                return False
        return True

    # ------------------------------------------------------------------ routes
    def dispatch(self):
        if not self.host_ok():
            self.close_connection = True
            return self.send_json({"error": "forbidden host"}, 403)
        try:
            u = urllib.parse.urlsplit(self.path)
        except ValueError:
            return self.send_json({"error": "bad url"}, 400)
        path = u.path
        try:
            qs = urllib.parse.parse_qs(u.query, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            return self.send_json({"error": "bad query"}, 400)

        def q(name, default=""):
            return (qs.get(name) or [default])[0]

        if path in ("/", "/index.html"):
            return self.send_file(PAGE, "text/html; charset=utf-8", "no-cache")
        if path.startswith("/app/"):
            p = static_map().get(path)
            if not p:
                return self.send_json({"error": "not found"}, 404)
            return self.send_file(p, STATIC_TYPES[os.path.splitext(p)[1].lower()], "no-cache")
        if path == "/manifest.webmanifest":
            return self.send_file(MANIFEST, STATIC_TYPES[".webmanifest"], "no-cache")
        if path == "/vendor/mermaid.min.js":
            p = mermaid_path()
            if not p:
                return self.send_json({"error": "mermaid is not installed"}, 404)
            return self.send_file(p, "text/javascript; charset=utf-8", "max-age=86400")
        if path in VENDOR:
            p = vendor_path(*VENDOR[path])
            if not p:
                return self.send_json({"error": "not installed"}, 404)
            return self.send_file(p, "text/javascript; charset=utf-8", "no-cache")
        if path == "/api/events":
            return self.stream_events()
        if path == "/api/health":
            return self.send_json(self.server.identity())
        if path == "/api/shutdown":
            given = q("token")
            if not given or not hmac.compare_digest(given.encode(), self.server.token.encode()):
                return self.send_json({"error": "bad token"}, 403)
            self.close_connection = True
            self.send_json({"ok": True, "pid": os.getpid()})
            self.server.stopping.set()  # end open event streams now, not after serve_forever notices
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        cfg = common.load_config()
        if path == "/api/today":
            return self.send_json(api.today(cfg))
        if path.startswith("/api/day/"):
            m = DAY_PATH.match(path)
            try:
                return self.send_json(api.day(m.group(1), cfg)) if m else self.send_json({"error": "bad date"}, 400)
            except ValueError:
                return self.send_json({"error": "bad date"}, 400)
        if path == "/api/calendar":
            try:
                return self.send_json(api.calendar(q("from") or None, q("to") or None, cfg))
            except ValueError as e:
                return self.send_json({"error": str(e)[:200]}, 400)
        if path == "/api/knowledge":
            return self.send_json(api.knowledge())
        if path == "/api/search":
            from . import search
            types = [t for t in re.split(r"[,\s]+", q("type")) if t] or None
            return self.send_json(search.search(q("q"), types, _int(q("limit"), 40)))
        if path == "/api/setup":
            return self.send_json(api.setup())
        if path == "/api/tickets":
            return self.send_json(api.tickets(q("q")[:200]))
        m = TICKET_PATH.match(path)
        if m:
            key = m.group(1).upper()
            if not api.TICKET_RE.match(key):
                return self.send_json({"error": "bad ticket key"}, 400)
            data = api.ticket(key)
            return self.send_json(data) if data else self.send_json({"error": "unknown ticket", "key": key}, 404)
        m = EXPORT_PATH.match(path)
        if m:
            key = m.group(1).upper()
            mode = q("mode", "full")
            if not api.TICKET_RE.match(key) or mode not in ("full", "shareable"):
                return self.send_json({"error": "bad ticket key or mode (full|shareable)"}, 400)
            from .. import export
            res = export.export_ticket(key, mode)
            if not res:
                return self.send_json({"error": "unknown ticket", "key": key}, 404)
            fname, html = res
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                              [("Content-Disposition", 'attachment; filename="%s"' % fname)])
        if path == "/api/timesheet":
            return self.send_json(api.timesheet(q("period", "week"), _int(q("limit"), 12), cfg))
        if path == "/api/timesheet.csv":
            period = "month" if q("period") == "month" else "week"
            body = api.timesheet_csv(period).encode("utf-8-sig")  # BOM: spreadsheet apps read UTF-8 correctly
            return self._send(200, body, "text/csv; charset=utf-8",
                              [("Content-Disposition", 'attachment; filename="chitragupta-timesheet-%s.csv"' % period)])
        if path == "/api/status":
            info = dict(self.server.identity(), port=self.server.port, started_at=self.server.started_at)
            return self.send_json(api.status(cfg, server=info))
        if path == "/api/changes":
            return self.send_json(api.changes(cfg))
        return self.send_json({"error": "not found"}, 404)

    def stream_events(self):
        """Server-Sent Events: a "cursor" event now and whenever the change cursor moves, a comment every 25 s.
        Ends when the client goes away (a failed write) or the server stops. Only writes: the socket timeout only
        drops a client that stops reading for that long."""
        srv = self.server
        self.close_connection = True
        if not srv.open_stream():
            return self._send(503, _json_bytes({"error": "too many live connections"}), "application/json; charset=utf-8",
                              [("Retry-After", "5")])
        try:
            self.send_response(200)
            for k, v in SECURITY_HEADERS:
                self.send_header(k, v)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return None
            self._event(b"retry: 3000\n\n")
            last, quiet = None, time.monotonic()
            while not srv.stopping.is_set():
                cur = api.changes(max_age=STREAM_POLL)["cursor"]
                if cur != last:
                    self._event(("event: cursor\ndata: %s\n\n" % json.dumps({"cursor": cur})).encode("utf-8"))
                    last, quiet = cur, time.monotonic()
                elif time.monotonic() - quiet >= STREAM_KEEPALIVE:
                    self._event(b": keep-alive\n\n")
                    quiet = time.monotonic()
                if srv.stopping.wait(STREAM_POLL) or self._client_gone():
                    break
        except (OSError, ValueError):  # the client went away (ValueError: write to a closed file)
            pass
        finally:
            srv.close_stream()
        return None

    def _event(self, data):
        self.wfile.write(data)
        self.wfile.flush()

    def _client_gone(self):
        """True once the client closed its end (the socket reads as EOF). Without this a closed tab would hold its
        slot until the next keep-alive write fails."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
        except (OSError, ValueError):
            return True


def _int(s, default):
    try:
        return max(1, min(200, int(s)))
    except (TypeError, ValueError):
        return default


def make_server(port, cfg=None):
    """Bind (raises OSError when the port is taken) and return the server, not yet serving."""
    return DashboardServer(port, cfg)


def serve(port, cfg=None, ready=None):
    """Serve until /api/shutdown or Ctrl+C. Returns 0, or 3 when the port is already taken."""
    deadline = time.monotonic() + 3.0  # a replaced server may still be releasing the port
    while True:
        try:
            srv = make_server(port, cfg)
            break
        except OSError as e:
            if time.monotonic() >= deadline:
                log("port %s is taken (%s); another dashboard or app holds it" % (port, e))
                return 3
            time.sleep(0.1)
    srv.write_token()
    log("serving %s %s on http://127.0.0.1:%d/ (pid %d, home %s)" % (APP, VERSION, srv.port, os.getpid(), srv.home))
    if ready:
        ready(srv)
    try:
        srv.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        srv.remove_token()
        srv.server_close()
        log("stopped")
    return 0


def start_in_thread(port=0, cfg=None):
    """For tests and the foreground CLI: a server on a background thread. Returns (server, thread)."""
    srv = make_server(port, cfg)
    srv.write_token()
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    return srv, t


def wait_until(pred, timeout, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()
