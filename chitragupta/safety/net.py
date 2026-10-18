"""Network guard: the only direct HTTP calls Chitragupta makes. GET only, to an allowlisted host.

  api.github.com   /repos/<owner>/<repo>  (the data-home repo's privacy check)
  127.0.0.1        Chitragupta's own dashboard (health probe, shutdown with its token)
Everything else on the network happens inside git / gh / glab / az / claude, under the command guard.
"""
import http.client
import re
import urllib.error
import urllib.parse
import urllib.request

from .log import block

LAYER = "network"
_GITHUB = re.compile(r"^/repos/[^/]+/[^/]+$")


def github_status(url, headers=None, timeout=15):
    """HTTP status of a GET to api.github.com/repos/<o>/<r> (404 and other errors returned as status), or None."""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.hostname != "api.github.com" or not _GITHUB.match(u.path) or u.query:
        block(LAYER, "only GET https://api.github.com/repos/<owner>/<repo>", url, "github")
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return None


def local_get(port, path, host_header=None, timeout=0.5, max_body=None):
    """GET http://127.0.0.1:<port><path> -> (status, body bytes). Raises OSError like http.client."""
    if not path.startswith("/api/"):
        block(LAYER, "dashboard calls are GET /api/*", path, "dashboard")
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": host_header or "127.0.0.1:%d" % int(port), "Accept": "application/json"})
        r = conn.getresponse()
        return r.status, (r.read(max_body) if max_body else r.read())
    finally:
        conn.close()


def port_free(p):
    """True when nothing listens on the port. Checked by binding (and closing at once) because on Windows a
    connect() to a closed local port is refused only after ~2 s of SYN retries — far past a 150 ms probe."""
    import socket
    for host in ("127.0.0.1", "0.0.0.0"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((host, p))
        except OSError:
            return False
        finally:
            s.close()
    return True
