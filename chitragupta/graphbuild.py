"""Evidence Graph builder: turns records Chitragupta already keeps into graph rows. Pure over its arguments:
no file, no clock, no process, no network.

This part versions the requirements read from the cached Jira card (design section 9).
"""
import copy
import difflib

from . import companionplanning as planning
from . import graphmodel
from .workflowmodel import digest

SIMILAR = planning.SIMILAR
MAX_SNAPSHOTS = 50
MAX_REVISIONS = 50      # per requirement: older wording keeps its digest and occurrences, not its text
MAX_REQUIREMENTS = 500
MAX_TEXT = 1000
NOT_REOBSERVED = "not re-observed; source partial"
TOO_MANY = "more than %d criteria" % MAX_REQUIREMENTS
EVENTS = ("added", "modified", "removed", "restored")


def _empty():
    return {"next_ordinal": 1, "source": {}, "logical": {}, "revisions": {}, "snapshots": []}


def text_fingerprint(digests):
    """Whether the requirements changed: the ordered row digests and nothing else (no summary, status or time)."""
    return digest(list(digests))[:16]


def _current(entry):
    return entry["history"][-1]["revision"]


def _source(rec, card, rows, gaps, proposed):
    failed = (rec or {}).get("error") or (card or {}).get("error")
    if (not rec and not card) or (card or {}).get("found") is False or (failed and not card):
        state = "inaccessible"
    else:
        state = "partial" if gaps else "current"
    # completeness: the Companion's own rule (companionplanning.propose)
    completeness = "unavailable" if state == "inaccessible" or not rows else "partial" if gaps else "complete"
    return {"state": state, "fetched_at": (rec or {}).get("fetched_at") or None,
            "error": planning.safe(failed, 200) if failed else None, "completeness": completeness,
            "gaps": [planning.safe(g, 300) for g in gaps], "comments_proposed": len(proposed)}


def _match(rows, logical):
    """Three whole passes → {row index: (requirement id, event or None)}."""
    taken, matched = set(), {}
    present = sorted((rid for rid, e in logical.items() if e["status"] != "removed"),
                     key=lambda rid: logical[rid]["ordinal"])
    everyone = sorted(logical, key=lambda rid: logical[rid]["ordinal"])

    def take(index, rid, event):
        matched[index] = (rid, event)
        taken.add(rid)

    # a1: the current wording of a requirement, exactly. Equal texts pair by source fragment, then by ordinal.
    for index, row in enumerate(rows):
        same = [rid for rid in present if rid not in taken and _current(logical[rid]) == row["digest"]]
        if same:
            here = (row["fragment"].get("field"), row["fragment"].get("index"))
            same.sort(key=lambda rid: ((logical[rid].get("fragment") or {}).get("field"),
                                       (logical[rid].get("fragment") or {}).get("index")) != here)
            take(index, same[0], None)
    # a2: an earlier wording, exactly: a removed requirement is back, or the text returned to what it was.
    for index, row in enumerate(rows):
        if index in matched:
            continue
        for rid in everyone:
            entry = logical[rid]
            if rid not in taken and any(h["revision"] == row["digest"] for h in entry["history"]):
                take(index, rid, "restored" if entry["status"] == "removed" else "modified")
                break
    # b: similar wording, best pair first. Every exact match is already taken, so rewording cannot steal one.
    pairs = sorted((-difflib.SequenceMatcher(None, planning.norm(row["text"]),
                                             planning.norm(logical[rid]["text"])).ratio(), index,
                    logical[rid]["ordinal"], rid)
                   for index, row in enumerate(rows) if index not in matched
                   for rid in present if rid not in taken)
    for ratio, index, _, rid in pairs:
        if -ratio >= SIMILAR and index not in matched and rid not in taken:
            take(index, rid, "modified")
    return matched


def requirement_snapshot(req_history, rec, card, fields, at, ticket, seq):
    """One read of the cached card against the `requirements` object of the history file.

    rec / card  `data/jira/<KEY>.json` and its card (either may be missing); fields: the configured criteria fields
    at          the time recorded when the card carries no `fetched_at`
    ticket      the ticket node id (part of each requirement's identity); seq: the journal sequence this run
                will publish under
    → (new requirements object, change). `req_history` is not modified. change = {added, modified, removed,
    restored: [gr ids], reordered, unseen, source: {from, to} or None, snapshot: bool, changed: bool}."""
    req = copy.deepcopy(req_history) if req_history else _empty()
    for name, value in _empty().items():
        req.setdefault(name, value)
    logical, revisions = req["logical"], req["revisions"]
    change = dict({name: [] for name in EVENTS}, reordered=False, unseen=[], source=None, snapshot=False)
    found, gaps, _, proposed = planning.extract(rec, card, fields)
    gaps = list(gaps)
    when = (rec or {}).get("fetched_at") or at

    rows = []
    for row in found:
        fragment = dict(row.get("fragment") or {})
        text = str(row["text"])[:MAX_TEXT]
        rows.append({"text": text, "source": row.get("source") or "", "fragment": fragment,
                     "digest": fragment.get("digest") or planning.fingerprint(text)})
    source = _source(rec, card, rows, gaps, proposed)
    matched = {}
    if source["state"] != "inaccessible":   # an inaccessible source says nothing about any requirement
        known = {rid: dict(e, text=(revisions.get(_current(e)) or {}).get("text") or "")
                 for rid, e in logical.items()}
        matched = _match(rows, known)
        new = [i for i in range(len(rows)) if i not in matched]
        over = len(logical) + len(new) - MAX_REQUIREMENTS
        if over > 0:   # further rows are a source gap, never a requirement
            cut = set(new[len(new) - over:])
            rows = [row for i, row in enumerate(rows) if i not in cut]
            gaps.append(TOO_MANY)
            source = _source(rec, card, rows, gaps, proposed)
            matched = _match(rows, known)

    previous = (req["source"] or {}).get("state")
    if previous != source["state"]:
        change["source"] = {"from": previous, "to": source["state"]}
    req["source"] = source
    if source["state"] == "inaccessible":
        change["changed"] = bool(change["source"])
        return req, change

    order = []
    for index, row in enumerate(rows):
        revision = revisions.setdefault(row["digest"], {"text": row["text"], "source": row["source"],
                                                        "fragment": row["fragment"], "first_seen": when})
        revision["last_seen"] = when
        if not revision.get("text"):   # wording whose text was dropped by the bound is back
            revision["text"] = row["text"]
            revision.pop("text_dropped", None)
        rid, event = matched.get(index, (None, "added"))
        if rid is None:
            same_root = sum(1 for e in logical.values() if e["root"] == row["digest"])
            rid = graphmodel.node_id("requirement", ticket, row["digest"], same_root)
            logical[rid] = {"ordinal": req["next_ordinal"], "root": row["digest"], "first_seen": when,
                            "status": "unconfirmed", "history": []}
            req["next_ordinal"] += 1
        entry = logical[rid]
        if event:
            entry["history"].append({"seq": seq, "at": when, "event": event, "revision": row["digest"]})
            change[event].append(rid)
            if event == "restored":
                entry["status"] = "unconfirmed"
        entry["fragment"] = {"field": row["fragment"].get("field"), "index": row["fragment"].get("index")}
        entry.pop("status_note", None)
        order.append(rid)

    seen = set(order)
    for rid in sorted(logical, key=lambda r: logical[r]["ordinal"]):
        entry = logical[rid]
        if rid in seen or entry["status"] == "removed":
            continue
        if source["completeness"] == "complete":
            entry["history"].append({"seq": seq, "at": when, "event": "removed", "revision": _current(entry)})
            entry["status"] = "removed"
            entry.pop("status_note", None)
            change["removed"].append(rid)
        else:   # no false absence: a partial read cannot remove a requirement
            entry["status_note"] = NOT_REOBSERVED
            change["unseen"].append(rid)

    fingerprint = text_fingerprint(row["digest"] for row in rows)
    last = req["snapshots"][-1] if req["snapshots"] else {"fingerprint": text_fingerprint([]), "order": []}
    events = any(change[name] for name in EVENTS)
    if not events:
        both = set(order) & set(last["order"])
        change["reordered"] = [r for r in order if r in both] != [r for r in last["order"] if r in both]
    if events or change["reordered"] or fingerprint != last["fingerprint"]:
        req["snapshots"].append(dict({name: list(change[name]) for name in EVENTS}, seq=seq, at=when,
                                     fingerprint=fingerprint, order=order, reordered=change["reordered"],
                                     unseen=list(change["unseen"])))
        req["snapshots"] = req["snapshots"][-MAX_SNAPSHOTS:]
        change["snapshot"] = True
    _drop_old_text(req)
    change["changed"] = bool(change["snapshot"] or change["source"])
    return req, change


def _drop_old_text(req):
    """Above the per-requirement revision bound the oldest wording loses its text; digests and occurrences stay."""
    logical, revisions = req["logical"], req["revisions"]
    current = {_current(e) for e in logical.values()}
    for entry in logical.values():
        seen = []
        for occurrence in entry["history"]:
            if occurrence["revision"] not in seen:
                seen.append(occurrence["revision"])
        for old in seen[:-MAX_REVISIONS] if len(seen) > MAX_REVISIONS else []:
            if old not in current and revisions.get(old, {}).get("text"):
                revisions[old]["text"] = ""
                revisions[old]["text_dropped"] = True
