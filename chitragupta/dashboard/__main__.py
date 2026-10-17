"""`python -m chitragupta dashboard <command>`

  serve [--port N]      run the server in the foreground (what the detached daemon runs); exit 3 if the port is taken
  ensure [--json]       start it when it is not running (or replace an older version); prints the URL
  stop [--json]         stop our server on the port (never a foreign app)
  status [--json]       who answers on the port: down | ours (version, pid, home) | foreign
  url [ROUTE]           the URL, e.g. `url tickets/ABC-1001`
"""
import json
import sys

from . import ensure, port, probe, stop, url


def _arg(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def main(argv):
    cmd = argv[0] if argv else "status"
    as_json = "--json" in argv
    if cmd == "serve":
        from .server import serve
        p = _arg(argv, "--port")
        try:
            p = int(p) if p is not None else port()
        except ValueError:
            print("--port needs a number", file=sys.stderr)
            return 2
        return serve(p)
    if cmd == "ensure":
        r = ensure(timeout=1.5, wait=6.0)
        if as_json:
            print(json.dumps(r))
        else:
            msg = {"started": "Dashboard started", "running": "Dashboard running", "replaced": "Dashboard updated",
                   "disabled": "Dashboard autostart is off", "other": "Port taken", "failed": "Dashboard failed to start"}
            print("%s: %s%s" % (msg.get(r.get("status"), r.get("status")), r.get("url", ""),
                                (" (%s)" % r["detail"]) if r.get("detail") else ""))
        return 0 if r.get("status") in ("started", "running", "replaced") else 1
    if cmd == "stop":
        r = stop(timeout=1.5)
        print(json.dumps(r) if as_json else r["detail"])
        return 0 if r["stopped"] or r["state"] == "down" else 1
    if cmd == "status":
        r = probe(timeout=1.5)
        r["url"] = url(r["port"])
        if as_json:
            print(json.dumps(r))
        elif r["state"] == "ours":
            h = r["health"]
            print("running: %s (version %s, pid %s, home %s)" % (r["url"], h["version"], h["pid"], h["home"]))
        elif r["state"] == "down":
            print("not running (port %d)" % r["port"])
        else:
            print("port %d is held by another app: %s" % (r["port"], r.get("detail", "")))
        return 0
    if cmd == "url":
        route = next((a for a in argv[1:] if not a.startswith("--")), "")
        print(url(port(), route))
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
