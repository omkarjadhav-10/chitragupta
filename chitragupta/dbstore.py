"""Storage of the database-flow records (plan §2.6): JSON under <home>/data/db, schema_version 1.

    data/db/meta.json                 {schema_version, migrated: [...]}
    data/db/graph/<nodes|edges|evidence|claims>.json   {id: record}  (scoped technical context)
    data/db/capabilities.json         connector capability records (dbconnector)
    data/db/quarantine.json           (connection, object) hidden after a permission-denied answer (dbaccess)
    data/db/revocations.json          connections you revoked (dbaccess)
    data/db/inventory.json            the connector's own connection listing: recorded, never scope
    data/db/sources.json              ingestion fingerprints per source (dbingest): an unchanged source is skipped
    data/db/mappings.json             mappings you confirmed (dbingest.confirm_mapping): user_confirmed evidence
    data/db/refresh.json              queued metadata discovery requests (dbflows.queue_refresh; agent db-discover)
    data/db/reconcile.json            drift state (dbimpact.reconcile): generations, per-source last success / last
                                      attempt / failure, stable event IDs, the last run
    data/db/comparisons.json          object pairs you confirmed for a comparison (dbimpact.confirm_pair)
    data/db/aliases.json              legacy table names that answer to exactly one approved object (the migration
                                      db1-canonical-graph; dbflows.resolve_start reads them)
    data/db/snapshots/<conn>/<ts>.json  metadata snapshots of one connection (dbimpact.take_snapshot after a metadata
                                      refresh; at most SNAPSHOT_KEEP per connection, an unchanged one is not repeated)
    data/db/<flows|investigations|plans|jobs|impact|compare>/<id>.json   one record per file, `revision` checked
    data/db/checkpoints/<source>.json resumable discovery cursors
    .worklog/cache/db-jobs/           a job's pin, call ledger, cancel flag, transient key and raw buffer, and a reviewed
                                      plan's transient key (<p_id>.key.json, kept at most PLAN_KEY_TTL_S): deleted with
                                      the job's files, never backed up (kbjobs.backup skips the folder), indexed or shared

Every write is atomic (common.write_json through the path guard) and happens under lock() (a file lock in
.worklog/locks/). save() checks `revision`: a record saved from an older revision raises Conflict carrying the caller's
unsaved data, so a note being typed is never lost or silently overwritten. Paths are built only from validated IDs
and names, never from caller-supplied paths.
"""
import os
import re
import time

from . import common
from . import dbmodel
from .safety import paths

SCHEMA_VERSION = 1
GRAPH = ("nodes", "edges", "evidence", "claims")
RECORDS = {"flows": "f", "investigations": "i", "plans": "p", "jobs": "j", "impact": None, "compare": None}
FILES = ("meta", "capabilities", "quarantine", "revocations", "inventory", "sources", "mappings", "refresh",
         "reconcile", "comparisons", "aliases")
JOB_SUFFIXES = (".pin.json", ".calls.jsonl", ".cancel", ".raw.json", ".key.json")
PLAN_KEY_TTL_S = 3600  # a reviewed plan's key waits this long for its run, then it is deleted
JOB_DIR = ("cache", "db-jobs")
PAGE_MAX = 50
LOCK_NAME = "db.lock"
LOCK_STALE_S = 60.0  # held for one read-modify-write; an older lock is a crashed writer's
LOCK_WAIT_S = 10.0
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
SNAPSHOT_RX = re.compile(r"^\d{8}T\d{6}(?:-\d{1,3})?$")
SNAPSHOT_KEEP = 20


class Conflict(Exception):
    """A save from an older revision. `current` is what is stored now, `unsaved` the caller's record (kept for it)."""

    def __init__(self, current, unsaved):
        super().__init__("record changed since it was read (revision %s, saving %s)" % (
            (current or {}).get("revision"), (unsaved or {}).get("revision")))
        self.current, self.unsaved = current, unsaved


class Busy(Exception):
    """The database-flow store is locked by another writer for longer than LOCK_WAIT_S."""


# --------------------------------------------------------------------------- paths

def root():
    return common.hp("data", "db")


def file_path(name):
    if name not in FILES:
        raise ValueError("unknown store file %r" % name)
    return os.path.join(root(), name + ".json")


def graph_path(collection):
    if collection not in GRAPH:
        raise ValueError("unknown graph collection %r" % collection)
    return os.path.join(root(), "graph", collection + ".json")


def record_path(kind, rec_id):
    """data/db/<kind>/<id>.json for a validated record ID (its prefix must match the kind)."""
    if kind not in RECORDS:
        raise ValueError("unknown record kind %r" % kind)
    want = RECORDS[kind]
    if not dbmodel.valid_id(rec_id, want):
        raise ValueError("invalid %s id" % kind)
    return os.path.join(root(), kind, rec_id + ".json")


def checkpoint_path(source):
    if not isinstance(source, str) or not _NAME.match(source) or source.startswith("."):
        raise ValueError("invalid checkpoint source name")
    return os.path.join(root(), "checkpoints", source + ".json")


def snapshot_dir(conn):
    if not isinstance(conn, str) or not _NAME.match(conn) or conn.startswith("."):
        raise ValueError("invalid connection name")
    return os.path.join(root(), "snapshots", conn)


def snapshot_path(conn, snap_id):
    """data/db/snapshots/<conn>/<YYYYMMDDTHHMMSS[-n]>.json (both parts validated: never a caller's path)."""
    if not isinstance(snap_id, str) or not SNAPSHOT_RX.match(snap_id):
        raise ValueError("invalid snapshot id")
    return os.path.join(snapshot_dir(conn), snap_id + ".json")


def list_snapshots(conn):
    """Snapshot IDs of one connection, oldest first."""
    try:
        names = os.listdir(snapshot_dir(conn))
    except (OSError, ValueError):
        return []
    return sorted(f[:-5] for f in names if f.endswith(".json") and SNAPSHOT_RX.match(f[:-5]))


def jobs_dir():
    return common.wl(*JOB_DIR)


def job_file(job_id, suffix):
    """<home>/.worklog/cache/db-jobs/<job><suffix> (pin, ledger, cancel flag, transient key, raw buffer)."""
    if not dbmodel.valid_id(job_id, "j") or suffix not in JOB_SUFFIXES:
        raise ValueError("invalid job file")
    return os.path.join(jobs_dir(), job_id + suffix)


def plan_key_file(plan_id):
    """<home>/.worklog/cache/db-jobs/<plan>.key.json: a reviewed plan's start key until its run takes it (transient)."""
    if not dbmodel.valid_id(plan_id, "p"):
        raise ValueError("invalid plan id")
    return os.path.join(jobs_dir(), plan_id + ".key.json")


# --------------------------------------------------------------------------- lock

class lock:
    """`with dbstore.lock():` one writer at a time across processes (agent.Lock under .worklog/locks/db.lock). A lock
    older than LOCK_STALE_S belonged to a crashed writer and is taken over; Busy after LOCK_WAIT_S."""

    def __init__(self, wait_s=None):
        self.wait_s = LOCK_WAIT_S if wait_s is None else wait_s
        self._lock = None

    def __enter__(self):
        from .agent import Lock
        self._lock = Lock(LOCK_NAME)
        end = time.monotonic() + self.wait_s
        while True:
            try:
                if time.time() - os.path.getmtime(self._lock.path) > LOCK_STALE_S:
                    paths.delete(self._lock.path, caller="dbstore.lock")
            except OSError:
                pass
            if self._lock.acquire():
                return self
            if time.monotonic() >= end:
                raise Busy("the database-flow store is busy; try again in a moment")
            time.sleep(0.05)

    def __exit__(self, *exc):
        if self._lock is not None:
            self._lock.release()


# --------------------------------------------------------------------------- single files

def read_file(name, default=None):
    v = common.read_json(file_path(name), None)
    return v if isinstance(v, dict) else ({} if default is None else default)


def write_file(name, obj):
    common.write_json(file_path(name), obj)


def ensure_meta():
    m = read_file("meta")
    if m.get("schema_version") != SCHEMA_VERSION:
        m = {"schema_version": SCHEMA_VERSION, "migrated": list(m.get("migrated") or [])}
        write_file("meta", m)
    return m


# --------------------------------------------------------------------------- graph collections

def load_graph():
    """{nodes, edges, evidence, claims}: each {id: record} (empty when not written yet)."""
    out = {}
    for c in GRAPH:
        v = common.read_json(graph_path(c), None)
        out[c] = v if isinstance(v, dict) else {}
    return out


def get(collection, rec_id):
    if not dbmodel.valid_id(rec_id):
        return None
    return (common.read_json(graph_path(collection), None) or {}).get(rec_id)


def upsert(collection, records, merge=None):
    """Insert or replace records by ID (same ID = same identity: evidence lists are united, never dropped).
    `merge(old, new) -> record` (optional) combines a stored record with the new one under the lock, so lists that
    several sources contribute to (subjects, applicability) are united against what is stored now.
    Returns the number of records that changed."""
    with lock():
        cur = common.read_json(graph_path(collection), None)
        cur = cur if isinstance(cur, dict) else {}
        n = 0
        for r in records:
            if not isinstance(r, dict) or not dbmodel.valid_id(r.get("id")):
                raise ValueError("record without a valid id")
            old = cur.get(r["id"])
            new = dict(r)
            if old and merge is not None:
                new = merge(old, new)
            if old and isinstance(old.get("evidence"), list) and isinstance(new.get("evidence"), list):
                new["evidence"] = list(dict.fromkeys(old["evidence"] + new["evidence"]))
            if old != new:
                cur[r["id"]] = new
                n += 1
        if n:
            ensure_meta()
            common.write_json(graph_path(collection), cur)
        return n


def page(items, cursor=None, limit=PAGE_MAX):
    """One bounded page of an {id: record} mapping in ID order: {items, more_available, next}. `cursor` is the last
    ID of the previous page; limit is capped at PAGE_MAX."""
    limit = max(1, min(int(limit or PAGE_MAX), PAGE_MAX))
    ids = sorted(k for k in items if cursor is None or k > cursor)
    chunk = ids[:limit]
    more = len(ids) > limit
    return {"items": [items[k] for k in chunk], "more_available": more, "next": chunk[-1] if more and chunk else None}


# --------------------------------------------------------------------------- one record per file

def load(kind, rec_id, default=None):
    v = common.read_json(record_path(kind, rec_id), None)
    return v if isinstance(v, dict) else default


def save(kind, rec, expected_revision=None):
    """Write `rec` (which carries its `id`) and return it with revision + 1. `expected_revision` (default: the
    record's own `revision`, 0 for a new one) must equal the stored revision, else Conflict."""
    with lock():
        return save_locked(kind, rec, expected_revision)


def save_locked(kind, rec, expected_revision=None):
    """save() for a caller that already holds lock() (several records published as one step)."""
    rid = rec.get("id")
    p = record_path(kind, rid)
    cur = common.read_json(p, None)
    have = int((cur or {}).get("revision") or 0) if isinstance(cur, dict) else 0
    want = int(rec.get("revision") or 0) if expected_revision is None else int(expected_revision)
    if have != want:
        raise Conflict(cur, rec)
    out = dict(rec, schema_version=rec.get("schema_version") or SCHEMA_VERSION, revision=have + 1,
               updated_at=common.now_iso())
    ensure_meta()
    common.write_json(p, out)
    return out


def delete_record(kind, rec_id):
    """Remove one derived record (a cached impact or comparison) through the path guard. True when it existed."""
    p = record_path(kind, rec_id)
    if not os.path.isfile(p):
        return False
    paths.delete(p, caller="dbstore.record")
    return True


def list_ids(kind, limit=PAGE_MAX, after=None):
    """Record IDs of one kind in ID order, at most `limit` (a bounded listing, never the whole folder at once)."""
    if kind not in RECORDS:
        raise ValueError("unknown record kind %r" % kind)
    d = os.path.join(root(), kind)
    try:
        names = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))
    except OSError:
        return []
    ids = [i for i in names if dbmodel.valid_id(i) and (after is None or i > after)]
    return ids[:max(1, min(int(limit or PAGE_MAX), 1000))]


def iter_records(kind, limit=PAGE_MAX, after=None):
    for rid in list_ids(kind, limit, after):
        rec = load(kind, rid)
        if rec is not None:
            yield rec


# --------------------------------------------------------------------------- checkpoints and fingerprints

def load_checkpoint(source):
    v = common.read_json(checkpoint_path(source), None)
    return v if isinstance(v, dict) else {"source": source, "cursor": None, "seen": [], "done": False}


def save_checkpoint(source, cursor, seen=(), done=False):
    """Resumable discovery: where a source's listing stopped and the IDs already taken (deduplication on resume)."""
    rec = {"source": source, "cursor": cursor, "seen": sorted(set(seen)), "done": bool(done), "at": common.now_iso()}
    with lock():
        common.write_json(checkpoint_path(source), rec)
    return rec


def sources_revision():
    """One fingerprint of every ingested source's fingerprint: it changes whenever ingestion took in a change."""
    s = read_file("sources").get("sources")
    return dbmodel.fingerprint({k: (v or {}).get("fingerprint") for k, v in (s if isinstance(s, dict) else {}).items()
                                if isinstance(v, dict)})


def source_fingerprint(kind, payload):
    """Fingerprint of one source's content (a describe result, a definition, a code file): unchanged fingerprint =
    nothing to refresh."""
    return dbmodel.fingerprint({"kind": kind, "payload": payload})


# --------------------------------------------------------------------------- transient job files

def delete_job_files(job_id, keep=()):
    """Remove a job's transient files (raw buffer, pin, claims, cancel flag) except the suffixes in `keep`."""
    d = jobs_dir()
    if not dbmodel.valid_id(job_id, "j") or not os.path.isdir(d):
        return 0
    n = 0
    for f in os.listdir(d):
        if not f.startswith(job_id + "."):
            continue
        if any(f.endswith(s) for s in keep):
            continue
        try:
            paths.delete(os.path.join(d, f), caller="dbstore.job")
            n += 1
        except OSError:
            pass
    return n
