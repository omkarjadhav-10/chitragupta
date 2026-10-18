"""`python -m chitragupta dashboard <command>`

  serve [--port N]      run the server in the foreground (what the detached daemon runs); exit 3 if the port is taken
  ensure [--json]       start it when it is not running (or replace an older version); prints the URL
  stop [--json]         stop our server on the port (never a foreign app)
  status [--json]       who answers on the port: down | ours (version, pid, home) | foreign
  url [ROUTE]           the URL, e.g. `url tickets/ABC-1001`
  open [ROUTE] [--no-browser]   ensure, then open the page (or ROUTE) in the default browser
  shortcut [--desktop]  (re)write the Start menu shortcut (and one on the Desktop)
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


def day_arg(s):
    """(YYYY-MM-DD, None) for YYYY-MM-DD, today, yesterday or -N (days back), else (None, a message saying what works).
    Days follow the work-day boundary (day_starts_at)."""
    import datetime as _dt
    from .. import common
    s = (s or "").strip().lower()
    today = _dt.date.fromisoformat(common.workday(common.now(), common.load_config()))
    if s in ("", "today"):
        return today.isoformat(), None
    if s == "yesterday":
        return (today - _dt.timedelta(days=1)).isoformat(), None
    if s.startswith("-") and s[1:].isdigit() and int(s[1:]) <= 3650:
        return (today - _dt.timedelta(days=int(s[1:]))).isoformat(), None
    try:
        return _dt.date.fromisoformat(s).isoformat(), None
    except ValueError:
        return None, "Not a day: %r. Use YYYY-MM-DD, today, yesterday or -N (for example -3)." % s


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
    if cmd == "open":
        pos = [a for a in argv[1:] if not a.startswith("--") or (a[1:].isdigit())]  # "-3" is a day, not a flag
        route = "/".join(pos[:2]) if pos[:1] in (["day"], ["ticket"], ["tickets"]) else (pos[0] if pos else "")
        if route == "ticket" or route.startswith("ticket/"):
            route = "tickets" + route[len("ticket"):]
        if route.startswith("day"):
            day, err = day_arg(route[3:].lstrip("/ ") or "today")
            if err:
                print(err)
                return 2
            route = "day/" + day
        r = ensure(timeout=1.5, wait=6.0)
        if r.get("status") not in ("started", "running", "replaced"):
            print("Dashboard not available: %s%s" % (r.get("status"), (" (%s)" % r["detail"]) if r.get("detail") else ""))
            return 1
        link = url(port(), route)
        opened = False
        if "--no-browser" not in argv:
            import webbrowser
            try:
                opened = webbrowser.open(link)
            except Exception:  # noqa: BLE001 — printing the address is enough
                opened = False
        print("%s: %s" % ("Opened" if opened else "Dashboard", link))
        return 0
    if cmd == "shortcut":
        from .. import install
        r = install.write_shortcuts(url(port(), "today"), desktop="--desktop" in argv)
        print(r["detail"])
        return 0 if r["ok"] else 1
    if cmd == "url":
        route = next((a for a in argv[1:] if not a.startswith("--")), "")
        print(url(port(), route))
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
