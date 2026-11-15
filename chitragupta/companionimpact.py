"""FLOW-06 change-impact and test suggestions for one Companion task, from recorded links only.

Deterministic, in this order: explicit recorded relationships (this ticket's saved requests and captured tests that
call an API it recorded; direct links in a cached database impact result), historical links (other tickets visible in
your profile that recorded the same API or changed the same file), then inferred associations (inferred links in a
cached database impact result). Database impact is read from the cached dbimpact results only: never computed,
queried or refreshed here. Each suggestion names the impacted item, its supporting links, a confidence category and a
proposed validation step as text; nothing is ever run from it. With no links the answer is `No recorded impact links`,
never `No impact` or `Safe to change`, and the coverage limits are always stated.
"""
import os
import re

from . import companionviews as views
from .kbnorm import api_key

CONFIDENCE = ("explicit", "historical", "inferred")
CONFIDENCE_LABEL = {"explicit": "Explicit recorded link", "historical": "Historical link (other tickets)",
                    "inferred": "Inferred association"}
KIND_ORDER = ("request", "test", "database", "ticket")
NO_LINKS = "No recorded impact links"
NO_LINKS_NOTE = ("Nothing recorded links this ticket's changes to saved requests, tests, other tickets or database "
                 "objects. This is not evidence that the change is safe: unrecorded callers, services, reports and "
                 "jobs are not seen.")
LIMIT, LINKS, PEERS, DB_ITEMS = 10, 3, 5, 5


def _line(value, limit=200):
    return views.scrub(value, limit)


def _rel(path):
    p = str(path or "").replace("\\", "/")
    return p.rsplit("/", 1)[-1] if views._ABS.match(p) or re.match(r"^[A-Za-z]:/", p) else p


def _apis(tk):
    """{api key: record} this ticket recorded and still holds current (not archived, not waiting for review)."""
    return {k: v for k, v in (tk.get("apis") or {}).items()
            if isinstance(v, dict) and not v.get("archived") and not v.get("review")}


def _steps(tk):
    return [s for s in ((tk or {}).get("replay") or {}).get("final") or [] if isinstance(s, dict)]


def _calls(steps, key):
    """Saved requests whose method and path template match a recorded API key (ANY matches every method)."""
    method, _, shape = key.partition(" ")
    out = []
    for s in steps:
        path = re.sub(r"^\{\{[^}]+\}\}", "", re.sub(r"^https?://[^/]+", "", str(s.get("url") or "")))
        m, _, sh = api_key(s.get("method") or "GET", path).partition(" ")
        if sh == shape and (method == "ANY" or m == method):
            out.append(s)
    return out


def _runs(tests, step, ticket):
    """Captured test runs whose recorded command runs this saved request (its file, or the ticket's request folder)."""
    name = os.path.basename(str(step.get("file") or "").replace("\\", "/"))
    folder = re.compile(r"(^|[\\/\s])%s([\\/\s]|$)" % re.escape(ticket))
    out = []
    for tid, t in sorted(tests.items()):
        cmd = str(t.get("command") or "")
        whole = re.search(r"\bbru\s+run\b", cmd) and not re.search(r"\.bru\b", cmd) and folder.search(cmd)
        if (name and name in cmd) or whole:
            out.append((tid, t))
    return out


class _Out:
    def __init__(self):
        self.rows = {}

    def add(self, sid, item, kind, confidence, links, why, validation, stale=False):
        cur = self.rows.get(sid)
        links = [dict(ref=_line(x["ref"], 200), text=_line(x["text"], 240)) for x in links]
        if cur is not None:
            seen = {x["ref"] for x in cur["links"]}
            cur["links"] = (cur["links"] + [x for x in links if x["ref"] not in seen])[:LINKS]
            if CONFIDENCE.index(confidence) < CONFIDENCE.index(cur["confidence"]):
                cur.update(confidence=confidence, confidence_label=CONFIDENCE_LABEL[confidence])
            return
        self.rows[sid] = {"id": sid, "item": _line(item, 200), "kind": kind, "confidence": confidence,
                          "confidence_label": CONFIDENCE_LABEL[confidence], "links": links[:LINKS],
                          "why": _line(why, 300), "validation": {"text": _line(validation, 400), "executable": False},
                          "stale": bool(stale)}

    def ordered(self):
        return sorted(self.rows.values(), key=lambda r: (CONFIDENCE.index(r["confidence"]),
                                                         KIND_ORDER.index(r["kind"]), r["id"]))


def _requests(out, key, tk, unlinked):
    steps = _steps(tk)
    tests = {tid: t for tid, t in (tk.get("tests") or {}).items() if isinstance(t, dict) and not t.get("archived")}
    for k in sorted(_apis(tk)):
        recorded = {"ref": "apis:" + k, "text": "%s recorded %s" % (key, k)}
        calls = _calls(steps, k)
        for s in calls:
            n, name = s.get("n"), _line(s.get("name"), 100) or "Saved request"
            out.add("request:%s" % n, "Saved request %s: %s (%s)" % (n, name, k), "request", "explicit",
                    [recorded, {"ref": "replay:%s" % n, "text": "Saved request %s calls %s" % (n, k)}],
                    "It calls an API this ticket recorded: a change to %s can change its response." % k,
                    "Replay saved request %s (%s) and compare its status and response keys with %s." % (
                        n, name, ("the recorded expectation: " + _line(s.get("expected"), 160)) if s.get("expected")
                        else "its last recorded outcome"))
            for tid, t in _runs(tests, s, key):
                out.add("test:%s" % tid, "Test run %s: %s" % (tid, _line(t.get("command"), 120)), "test", "explicit",
                        [{"ref": "tests:" + tid, "text": "Its recorded command runs saved request %s" % n},
                         {"ref": "replay:%s" % n, "text": "Saved request %s calls %s" % (n, k)}],
                        "Its recorded command runs a saved request for %s." % k,
                        "Run the recorded test command yourself and compare with its last outcome (%s)." % (
                            _line(t.get("status"), 20) or "unknown"))
        if not calls:
            unlinked.append("%s: no saved request or captured test of this ticket calls it" % k)


def _peers(out, rec, tk, cfg, repo):
    """Historical links: other tickets visible in your profile that recorded the same API (their records or the
    knowledge catalog's API entry) or changed the same file. A ticket outside your profile is never named."""
    from . import store
    from .dashboard import api
    key = rec["binding"]["ticket"]
    scope = api.scope_index(cfg)
    others = {k: t for k, t in api._ticket_records().items()
              if k != key and isinstance(t, dict) and (scope.get(k) or {}).get("visible")}
    catalog = (api.read_json_cached(store.catalog_path(), {}) or {}).get("apis") or {}
    for k in sorted(_apis(tk)):
        listed = set((catalog.get(k) or {}).get("tickets") or []) if isinstance(catalog.get(k), dict) else set()
        mine = {o for o, otk in others.items() if k in (otk.get("apis") or {})}
        for o in sorted((listed | mine) & set(others))[:PEERS]:
            calls = _calls(_steps(others[o]), k)
            first = {"ref": "tickets:%s" % o, "text": "%s also recorded %s" % (o, k)} if o in mine else \
                {"ref": "catalog:apis:%s" % k, "text": "The API catalog lists %s under %s" % (k, o)}
            out.add("ticket:%s" % o, "Ticket %s" % o, "ticket", "historical",
                    [first] + [{"ref": "tickets:%s:replay:%s" % (o, s.get("n")), "text": "Its saved request %s calls %s"
                                % (s.get("n"), k)} for s in calls[:1]],
                    "Earlier work on %s used the same API." % o,
                    "Check that %s's recorded behaviour of %s still holds%s." % (
                        o, k, ": replay its saved request %s" % calls[0].get("n") if calls else ""))
    changed = sorted(p for p, f in (tk.get("files") or {}).items() if isinstance(f, dict)
                     and (f.get("changed") or f.get("committed")) and (not f.get("repo") or f["repo"] == repo))
    for path in changed[:50]:
        hits = [o for o, otk in sorted(others.items()) if isinstance((otk.get("files") or {}).get(path), dict)
                and (otk["files"][path].get("repo") or repo) == repo]
        for o in hits[:PEERS]:
            out.add("ticket:%s" % o, "Ticket %s" % o, "ticket", "historical",
                    [{"ref": "files:" + _rel(path), "text": "%s also changed %s" % (o, _rel(path))}],
                    "Earlier work on %s changed the same file." % o,
                    "Review %s's recorded behaviour around %s before relying on it." % (o, _rel(path)))


def _database(out, cfg, key):
    """Cached database impact results whose recorded flows touch this ticket, re-projected through today's access.
    Returns coverage notes. Never computes, queries or refreshes an impact."""
    from . import dbstore
    found = []
    for rec in dbstore.iter_records("impact", limit=200):
        items = rec.get("items") or []
        if rec.get("kind") == "impact" and any((it.get("relation") == "ticket" and it.get("key") == key) or
                                               key in (it.get("tickets") or []) for it in items):
            found.append(rec)
    if not found:
        return ["Database impact: no cached result names this ticket's recorded flows (cli db impact computes one; "
                "the Companion never does)"]
    from . import dbaccess, dbflows, dbimpact, dbviews
    ctx = dbaccess.AccessContext(cfg)
    vis = dbflows.Visible(ctx, cfg=cfg)
    notes, used = [], 0
    for rec in sorted(found, key=lambda r: str(r.get("computed_at") or ""), reverse=True):
        if (rec.get("target") or {}).get("id") not in vis.nodes or \
                any(c not in ctx.connections for c in rec.get("connections") or []):
            continue
        state = dbimpact.cache_state(ctx, rec)
        if state["state"] == "scope_changed":
            notes.append("A cached database impact was computed under another access scope: not shown")
            continue
        target = _line((rec.get("target") or {}).get("label"), 120) or "a database object"
        cite = {"ref": "impact:%s" % rec.get("id"), "text": "Cached impact of %s (%s, computed %s)" % (
            target, _line(rec.get("change_label"), 40) or "any change", _line(rec.get("computed_at"), 25))}
        if state["state"] == "stale":
            notes.append("Cached impact of %s is stale: %s" % (target, "; ".join(state["reasons"][:2])))
        notes += [_line(n, 200) for n in (rec.get("coverage") or [])[:2]]
        for it in dbviews._impact_items(vis, rec):
            if used >= DB_ITEMS:
                break
            if it.get("relation") == "ticket":
                if it.get("key") == key:
                    continue
                out.add("ticket:%s" % it["key"], "Ticket %s" % it["key"], "ticket", "historical",
                        [cite, {"ref": "tickets:%s" % it["key"], "text": "Its recorded flow touches %s" % target}],
                        it.get("why") or "Its recorded flow touches the same object.",
                        (it.get("validation") or {}).get("text") or "Check its saved requests and tests.")
                used += 1
                continue
            out.add("database:%s" % _line(it.get("id"), 80), "%s %s" % (_line(it.get("kind_label"), 30),
                                                                         _line(it.get("label"), 120)), "database",
                    "explicit" if it.get("basis") == "direct" else "inferred",
                    [cite, {"ref": "edge:%s" % _line(it.get("edge"), 80), "text": "%s via %s" % (
                        _line(it.get("relation_label"), 60), " > ".join(_line(p, 60) for p in it.get("path") or []))}],
                    it.get("why") or "", (it.get("validation") or {}).get("text") or "Review it against the change.",
                    stale=bool(it.get("stale")) or state["state"] == "stale")
            used += 1
    return list(dict.fromkeys(notes))[:5]


def suggestions(rec, tk, cfg, limit=LIMIT):
    """The bounded `impact` field of the Companion detail view (FLOW-06)."""
    from .companionevidence import repo_key
    out, unlinked = _Out(), []
    repo = repo_key(rec["binding"]["location"])
    key = rec["binding"]["ticket"]
    _requests(out, key, tk, unlinked)
    _peers(out, rec, tk, cfg, repo)
    try:
        notes = _database(out, cfg, key)
    except Exception:  # noqa: BLE001 - database impact is optional; the rest of the answer stands
        notes = ["Database impact unavailable"]
    rows = out.ordered()
    by = {c: sum(1 for r in rows if r["confidence"] == c) for c in CONFIDENCE}
    return {"state": "links" if rows else "none",
            "label": "%d suggestion%s from recorded links" % (len(rows), "" if len(rows) == 1 else "s") if rows
            else NO_LINKS, "note": "" if rows else NO_LINKS_NOTE, "items": rows[:limit], "total": len(rows),
            "more": len(rows) > limit, "by_confidence": by, "unlinked": [_line(u, 200) for u in unlinked[:5]],
            "coverage": "partial",
            "limits": ["Saved requests and tests are matched to recorded APIs by method and path template only",
                       "Only tickets visible in your profile are compared; files outside %s are not considered" %
                       views._repo(rec["binding"]["location"]),
                       "Database impact comes only from cached results; the Companion never computes or queries"]
            + notes,
            "basis": "Recorded links only; validation steps are text, nothing runs from here"}
