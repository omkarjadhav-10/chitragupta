"""Chitragupta's local, read-only dashboard: daemon control.

The port is the lock (no pid file). `GET /api/health` on 127.0.0.1:<port> says who holds it:
  down     nothing listens (connection refused)
  ours     {"app": "chitragupta", "version": str, "pid": int, "home": str}
  foreign  anything else, a timeout included — never touched

ensure(): down -> spawn ours ("started"); ours with the same home and version >= ours -> "running";
ours but older -> stop it, spawn ours ("replaced"); a different home or a foreign app -> "other", left alone.
Never downgrades, so a dev checkout and the stable runtime do not replace each other back and forth.
"""
import http.client
import json
import os
import subprocess
import sys
import time

from .. import __version__ as VERSION
from .. import APP
from .. import common

DEFAULT_PORT = 7419
PROBE_BODY_MAX = 4096
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
_spawned = []


def port(cfg=None):
    """$CHITRAGUPTA_DASHBOARD_PORT, else config `dashboard.port`, else 7419."""
    v = common.env("DASHBOARD_PORT")
    if not v:
        try:
            v = ((cfg if cfg is not None else common.load_config()).get("dashboard") or {}).get("port")
        except Exception:  # noqa: BLE001
            v = None
    try:
        p = int(v)
        if 0 < p < 65536:
            return p
    except (TypeError, ValueError):
        pass
    return DEFAULT_PORT


def url(p=None, route=""):
    p = p or port()
    route = (route or "").lstrip("#/")
    return "http://127.0.0.1:%d/%s" % (p, ("#/" + route) if route else "")


def vtuple(v):
    out = []
    for part in str(v or "").split("+")[0].split("-")[0].split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out + [0] * (3 - len(out)))


def _same_home(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (TypeError, ValueError, OSError):
        return False


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


def probe(p=None, timeout=0.15):
    """{"state": "down"|"ours"|"foreign", "port", "health"?, "detail"?}."""
    p = p or port()
    if port_free(p):
        return {"state": "down", "port": p}
    conn = http.client.HTTPConnection("127.0.0.1", p, timeout=timeout)
    try:
        conn.request("GET", "/api/health", headers={"Host": "127.0.0.1:%d" % p, "Accept": "application/json"})
        r = conn.getresponse()
        body = r.read(PROBE_BODY_MAX + 1)
    except ConnectionRefusedError:
        return {"state": "down", "port": p}
    except OSError as e:  # timeouts, resets: something holds the port but does not answer like us
        return {"state": "foreign", "port": p, "detail": "%s: %s" % (e.__class__.__name__, e)}
    except Exception as e:  # noqa: BLE001 — garbage on the port
        return {"state": "foreign", "port": p, "detail": "%s: %s" % (e.__class__.__name__, e)}
    finally:
        conn.close()
    if r.status != 200 or len(body) > PROBE_BODY_MAX:
        return {"state": "foreign", "port": p, "detail": "HTTP %s" % r.status}
    try:
        h = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"state": "foreign", "port": p, "detail": "not JSON"}
    if (isinstance(h, dict) and h.get("app") == APP and isinstance(h.get("version"), str)
            and isinstance(h.get("pid"), int) and not isinstance(h.get("pid"), bool) and isinstance(h.get("home"), str)):
        return {"state": "ours", "port": p, "health": h}
    return {"state": "foreign", "port": p, "detail": "another app answers on this port"}


def log_path():
    return common.wl("logs", "dashboard.log")


def _runtime_version():
    return common.read_text(os.path.join(common.user_dir(), "runtime", "current")).strip()


def serve_command(p, cfg=None):
    """argv + extra env for a detached server. The stable launcher when it runs this same version (so the server
    survives plugin updates), else this package straight from where it is."""
    if os.path.isfile(common.launcher()) and _runtime_version() == VERSION:
        return common.self_command("dashboard", "serve", "--port", p, cfg=cfg, windowless=True), {}
    root = os.path.dirname(common.PACKAGE_DIR)
    env = {"PYTHONPATH": root + ((os.pathsep + os.environ["PYTHONPATH"]) if os.environ.get("PYTHONPATH") else "")}
    return [common.python_exe(cfg, windowless=True), "-m", "chitragupta", "dashboard", "serve", "--port", str(p)], env


def spawn(p, cfg=None):
    """Start a detached, windowless server; never waits for it. Returns the Popen pid (or None)."""
    argv, extra = serve_command(p, cfg)
    env = dict(os.environ)
    env.update(extra)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for k in common.INTERNAL_ENV:
        env.pop(k, None)
    cwd = common.home() if os.path.isdir(common.home()) else common.user_dir()
    if not os.path.isdir(cwd):
        cwd = None
    logf = None
    try:
        common.ensure_dir(os.path.dirname(log_path()))
        logf = open(log_path(), "w", encoding="utf-8")  # truncated on every start
        logf.write("%s starting: %s\n" % (common.now_iso(), " ".join(argv)))
        logf.flush()
    except OSError:
        logf = None
    kw = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=logf or subprocess.DEVNULL,
              stderr=subprocess.STDOUT if logf else subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kw["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kw)
        _spawned.append(proc)  # detached and never waited on; kept so Popen.__del__ does not warn
        return proc.pid
    except OSError as e:
        if logf:
            logf.write("spawn failed: %s\n" % e)
        return None
    finally:
        if logf:
            logf.close()


def _wait_state(p, want, timeout, probe_timeout=0.5):
    end = time.monotonic() + timeout
    st = probe(p, probe_timeout)
    while st["state"] not in want and time.monotonic() < end:
        time.sleep(0.1)
        st = probe(p, probe_timeout)
    return st


def _token_for(p, pid):
    from .server import token_path
    try:
        with open(token_path(p), encoding="utf-8") as f:
            rec = json.load(f)
        return rec.get("token") if rec.get("pid") == pid else None
    except (OSError, ValueError):
        return None


def _terminate(pid):
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x0001, False, int(pid))  # PROCESS_TERMINATE
            if not h:
                return False
            try:
                return bool(k.TerminateProcess(h, 1))
            finally:
                k.CloseHandle(h)
        import signal
        os.kill(int(pid), signal.SIGTERM)
        return True
    except Exception:  # noqa: BLE001
        return False


def stop(p=None, timeout=1.5):
    """Stop OUR server on the port: the token-guarded /api/shutdown, else terminate exactly the pid /api/health just
    reported. Foreign apps are never touched. -> {"stopped": bool, "state", "detail"}."""
    p = p or port()
    st = probe(p, timeout)
    if st["state"] == "down":
        return {"stopped": False, "state": "down", "detail": "not running"}
    if st["state"] != "ours":
        return {"stopped": False, "state": st["state"], "detail": st.get("detail") or "not ours; left alone"}
    pid = st["health"]["pid"]
    how = None
    tok = _token_for(p, pid)
    if tok:
        conn = http.client.HTTPConnection("127.0.0.1", p, timeout=timeout)
        try:
            conn.request("GET", "/api/shutdown?token=" + tok, headers={"Host": "127.0.0.1:%d" % p})
            if conn.getresponse().status == 200:
                how = "shutdown"
        except OSError:
            pass
        finally:
            conn.close()
    if how and _wait_state(p, ("down",), 2.0)["state"] == "down":
        return {"stopped": True, "state": "down", "detail": "stopped pid %d" % pid}
    if pid != os.getpid() and _terminate(pid):
        how = "terminated"
    st = _wait_state(p, ("down",), 2.0)
    if st["state"] == "down":
        return {"stopped": True, "state": "down", "detail": "%s pid %d" % (how or "stopped", pid)}
    return {"stopped": False, "state": st["state"], "detail": "pid %d still answers" % pid}


def enabled(cfg):
    if (common.env("DASHBOARD") or "").lower() in ("0", "off", "false", "no"):
        return False
    return ((cfg.get("dashboard") or {}).get("autostart", True)) is not False


def ensure(cfg=None, timeout=0.15, wait=0.0):
    """Make sure our dashboard (this version or newer) answers on the configured port.
    -> {"status": started|running|replaced|other|disabled|failed, "url", "port", "version"?, "pid"?, "detail"?}.
    `wait` > 0 polls that long for a freshly spawned server to answer."""
    try:
        cfg = cfg if cfg is not None else common.load_config()
        p = port(cfg)
        out = {"port": p, "url": url(p)}
        if not enabled(cfg):
            return dict(out, status="disabled", detail="dashboard autostart is off")
        st = probe(p, timeout)
        if st["state"] == "foreign":
            return dict(out, status="other", detail=st.get("detail") or "another app holds the port")
        status = "started"
        if st["state"] == "ours":
            h = st["health"]
            if not _same_home(h.get("home"), common.home()):
                return dict(out, status="other", version=h["version"], pid=h["pid"],
                            detail="a Chitragupta dashboard for another data home (%s) holds the port" % h.get("home"))
            if vtuple(h["version"]) >= vtuple(VERSION):
                return dict(out, status="running", version=h["version"], pid=h["pid"])
            r = stop(p, max(timeout, 1.5))
            if not r["stopped"]:
                return dict(out, status="other", version=h["version"], pid=h["pid"],
                            detail="older dashboard %s could not be stopped: %s" % (h["version"], r["detail"]))
            status = "replaced"
        pid = spawn(p, cfg)
        if pid is None:
            return dict(out, status="failed", detail="could not start the server (see %s)" % log_path())
        out.update(status=status, pid=pid, version=VERSION)
        if wait > 0:
            st = _wait_state(p, ("ours", "foreign"), wait)
            if st["state"] == "ours":
                out["pid"] = st["health"]["pid"]
            elif st["state"] == "down":
                out.update(status="failed", detail="the server did not come up (see %s)" % log_path())
            else:
                out.update(status="other", detail=st.get("detail") or "another app took the port")
        return out
    except Exception as e:  # noqa: BLE001 — called from SessionStart maintenance: never raise
        return {"status": "failed", "detail": "%s: %s" % (e.__class__.__name__, e)}


__all__ = ["ensure", "stop", "probe", "port", "url", "spawn", "DEFAULT_PORT", "VERSION"]
