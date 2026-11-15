"""Immutable, hash-validated workflow checkpoints under the existing data home.

Payload first, head last: the atomic head is the only publication point. Orphan or
incomplete payloads are never read; recovery quarantines them. No transcript or
second activity stream is collected here, and nothing here sends or dispatches.
"""
import copy
import os
import re

from . import common, workflowmodel as model
from .safety import paths, workflowio

_ID = re.compile(r"^w_[a-f0-9]{64}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")


class Conflict(Exception):
    def __init__(self, current, unsaved):
        super().__init__("workflow changed; review current state and keep your unsaved draft")
        self.current, self.unsaved = current, copy.deepcopy(unsaved)


class NotSaved(OSError):
    """Storage failed (disk full, permissions): the prior checkpoint stays current, nothing was sent."""


def root(wid):
    if not isinstance(wid, str) or not _ID.fullmatch(wid):
        raise ValueError("invalid workflow ID")
    return common.hp("data", "workflows", wid)


def identity(binding):
    return "w_" + model.digest(binding)


def _checkpoint(folder, digest):
    """A complete checkpoint, or None: its content must hash to its own name."""
    if not _HASH.fullmatch(str(digest or "")):
        return None
    record = common.read_json(os.path.join(folder, "checkpoints", digest + ".json"), None)
    if not isinstance(record, dict) or not record:
        return None
    try:
        return record if model.digest(record) == digest else None
    except (TypeError, ValueError):
        return None


def last_complete(wid):
    """(digest, revision) of the newest checkpoint whose parent chain is complete, or (None, 0)."""
    folder = root(wid)
    try:
        names = os.listdir(os.path.join(folder, "checkpoints"))
    except FileNotFoundError:
        return None, 0
    found = {}
    for name in names:
        if name.endswith(".json") and _checkpoint(folder, name[:-5]) is not None:
            found[name[:-5]] = common.read_json(os.path.join(folder, "checkpoints", name), {})
    best = (None, 0)
    for digest, record in found.items():
        node, seen = record, set()
        while node.get("parent") and node["parent"] in found and node["parent"] not in seen:
            seen.add(node["parent"])
            node = found[node["parent"]]
        if node.get("revision") == 1 and not node.get("parent") and record.get("revision", 0) > best[1]:
            best = (digest, record["revision"])
    return best


def _head(wid):
    folder = root(wid)
    head_path = os.path.join(folder, "head.json")
    if not os.path.exists(head_path):
        return None, None
    head = common.read_json(head_path, None)
    record = _checkpoint(folder, (head or {}).get("digest")) if isinstance(head, dict) else None
    if record is None:
        digest, revision = last_complete(wid)
        hint = (" Last complete checkpoint: %s (revision %d)." % (digest, revision)) if digest else ""
        raise ValueError("invalid or incomplete workflow head; recovery required." + hint)
    if record.get("schema_version", 0) > model.SCHEMA:
        raise ValueError("workflow saved by a newer version; read-only until upgrade")
    if record.get("id") != wid or record.get("revision") != head.get("revision"):
        raise ValueError("workflow checkpoint identity mismatch; recovery required")
    return head, record


def load(wid):
    """The published checkpoint, normalized for older records. Never a mixed or orphan version."""
    head, record = _head(wid)
    return model.normalize(record) if record else None


def save(record, expected_revision, authorize, timeout=5.0):
    """authorize runs inside the short lock with current config; never a provider call.

    Lock timeout raises TimeoutError and storage failure NotSaved: both say `Not saved`, keep the draft
    with the caller and leave the prior checkpoint current."""
    wid = record["id"]
    with workflowio.lock(timeout):
        head, current = _head(wid)
        if (current or {}).get("revision", 0) != expected_revision:
            raise Conflict(model.normalize(current) if current else None, record)
        authorize(record)
        out = copy.deepcopy(record)
        out.update(schema_version=model.SCHEMA, revision=expected_revision + 1,
                   updated_at=common.now_iso(), parent=head["digest"] if head else None)
        digest = model.digest(out)
        folder = root(wid)
        try:
            workflowio.durable_json(os.path.join(folder, "checkpoints", digest + ".json"), out)
            workflowio.durable_json(os.path.join(folder, "head.json"), {"digest": digest, "revision": out["revision"]})
        except OSError as exc:
            raise NotSaved("Not saved: %s. Your draft is kept, the previous checkpoint is unchanged and "
                           "nothing was sent." % (exc.strerror or exc.__class__.__name__)) from exc
        return model.normalize(out)


def attach_ticket(record, timeout=5.0):
    """Backed additive reference migration. Store.save shares this short writer lock.

    The immutable head remains authoritative if attachment is interrupted. Recovery
    calls this again; no approved text or ticket evidence is reconstructed.
    Returns True when the reference was added.
    """
    from . import store
    with workflowio.lock(timeout):
        return _attach(record, store.ticket_path(record["binding"]["ticket"]))


def _attach(record, path):
    tk = common.read_json(path, {}) or {}
    refs = tk.get("workflows") or {"schema_version": 1, "ids": []}
    if refs.get("schema_version") != 1:
        raise ValueError("unsupported ticket workflow-reference schema")
    if record["id"] in refs["ids"]:
        return False
    backup = common.hp("data", "workflows", "backups", record["id"] + ".ticket.json")
    if os.path.exists(path) and not os.path.exists(backup):
        paths.copy(path, backup, caller="workflow reference migration")
    tk["workflows"] = {"schema_version": 1, "ids": sorted(set(refs["ids"]) | {record["id"]})}
    common.write_json(path, tk)
    return True


def recover(wid, restore=None):
    """Idempotent repair under the writer lock; never sends or dispatches anything.

    Validates head -> checkpoint digest and parent chain, quarantines orphan, incomplete and temp
    files (never exposed), and re-attaches the ticket reference. An invalid head is not guessed:
    the report names the last complete checkpoint; `restore` republishes exactly that one."""
    from . import store
    folder = root(wid)
    report = {"workflow": wid, "state": "ok", "quarantined": [], "chain": "complete", "ticket_reference": None}
    with workflowio.lock():
        try:
            head, record = _head(wid)
        except ValueError as exc:
            digest, revision = last_complete(wid)
            if not restore or restore != digest:
                report.update(state="recovery_required", reason=str(exc), last_complete=digest,
                              last_revision=revision)
                return report
            bad = os.path.join(folder, "head.json")
            workflowio.move(bad, os.path.join(folder, "quarantine", "head.%s.json" % common.now().strftime("%Y%m%d%H%M%S")))
            workflowio.durable_json(bad, {"digest": digest, "revision": revision})
            head, record = _head(wid)
            report["restored"] = digest
        keep = set()
        node, digest = record, (head or {}).get("digest")
        while node is not None and digest:
            keep.add(digest)
            digest = node.get("parent")
            node = _checkpoint(folder, digest) if digest else None
            if digest and node is None:
                report["chain"] = "missing parent %s" % digest
        cdir = os.path.join(folder, "checkpoints")
        for name in sorted(os.listdir(cdir)) if os.path.isdir(cdir) else []:
            if name.endswith(".json") and name[:-5] in keep:
                continue
            workflowio.move(os.path.join(cdir, name), os.path.join(folder, "quarantine", name))
            report["quarantined"].append("checkpoints/" + name)
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if name.endswith(".tmp"):
                workflowio.move(os.path.join(folder, name), os.path.join(folder, "quarantine", name))
                report["quarantined"].append(name)
        if record is not None:
            report.update(head=head["digest"], revision=record["revision"])
            path = store.ticket_path(record["binding"]["ticket"])
            report["ticket_reference"] = "repaired" if _attach(record, path) else "present"
        else:
            report["state"] = "no_published_checkpoint"
    return report


def ids():
    try:
        return sorted(x for x in os.listdir(common.hp("data", "workflows")) if _ID.fullmatch(x))
    except FileNotFoundError:
        return []
