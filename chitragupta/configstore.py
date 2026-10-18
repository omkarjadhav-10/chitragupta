"""The one way Chitragupta changes its config file: locked, backed up, written atomically, recorded.

    update(changes, source)      changes = {"dotted.key": value, …} (None deletes the key) or a callable(cfg) that edits
                                 the raw config in place; -> {"id", "changes": [{"key","old","new"}], "backup"}
    write(cfg, before, source)   replace the whole raw config (what setup builds); same lock, backup and history
    history(limit) / undo(source="dashboard")   the change log and the inverse of the newest change from `source`

Every caller (setup, the dashboard's Settings page, the engine's own updates) goes through here, so the lock, the
backup, the history and the path guard can't be skipped. History: `<user dir>/settings-history.jsonl`, one line per
changed key; values of secret-looking keys are never written there.
"""
import copy
import json
import os
import re
import secrets
import time

from . import common
from .safety import paths

LOCK_STALE_S = 30.0
LOCK_WAIT_S = 10.0
SECRET_KEY = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|credential|client_secret)")
HISTORY_FILE = "settings-history.jsonl"


class ConfigError(Exception):
    """The config file can't be read, or the change can't be applied (the file is left as it was)."""


def config_path():
    return common.wl("config.json")


def history_path():
    return os.path.join(common.user_dir(), HISTORY_FILE)


# --------------------------------------------------------------------------- read

def read_raw():
    """(cfg, None) or (None, error). A missing file is an empty config."""
    p = config_path()
    if not os.path.exists(p):
        return {}, None
    try:
        with open(p, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        return None, "%s does not parse: %s" % (p, e)
    if not isinstance(cfg, dict):
        return None, "%s is not a JSON object" % p
    return cfg, None


def get(cfg, key, default=None):
    cur = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_key(cfg, key, value):
    """Set (or with value None, delete) a dotted key in a nested dict, creating objects on the way."""
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            if value is None:
                return
            nxt = cur[part] = {}
        cur = nxt
    if value is None:
        cur.pop(parts[-1], None)
    else:
        cur[parts[-1]] = value


def flat_diff(before, after, prefix=""):
    """[{"key","old","new"}] for every leaf that differs (dicts are walked; lists and scalars compare whole)."""
    out = []
    keys = sorted(set(before or {}) | set(after or {}))
    for k in keys:
        a, b = (before or {}).get(k), (after or {}).get(k)
        dk = prefix + k
        if isinstance(a, dict) or isinstance(b, dict):
            if isinstance(a, dict) == isinstance(b, dict) or a is None or b is None:  # a new or removed section: per key
                out.extend(flat_diff(a if isinstance(a, dict) else {}, b if isinstance(b, dict) else {}, dk + "."))
            else:
                out.append({"key": dk, "old": a, "new": b})
        elif a != b:
            out.append({"key": dk, "old": a, "new": b})
    return out


# --------------------------------------------------------------------------- lock

class _Lock:
    def __init__(self):
        self.path = common.wl("config.lock")
        self.held = False

    def __enter__(self):
        end = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                os.close(paths.create_exclusive(self.path, ("%d %s" % (os.getpid(), common.now_iso())).encode(),
                                                caller="configstore"))
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > LOCK_STALE_S:  # a crashed writer: take it over
                        paths.delete(self.path, caller="configstore")
                        continue
                except OSError:
                    continue
                if time.monotonic() >= end:
                    raise ConfigError("the config is being changed by another process; try again in a moment")
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.held:
            try:
                paths.delete(self.path, caller="configstore")
            except OSError:
                pass


# --------------------------------------------------------------------------- write

def _backup(source):
    p = config_path()
    if not os.path.exists(p):
        return None
    dest = common.wl("backups", "chitragupta-%s-%s" % (re.sub(r"[^a-z0-9-]", "-", source.lower()), time.strftime("%Y%m%d-%H%M%S")))
    common.ensure_dir(dest)
    target = os.path.join(dest, "config.json")
    paths.copy(p, target, caller="configstore")
    return target


def _shown(key, value):
    return "(hidden)" if SECRET_KEY.search(key) else value


def _record(change_id, source, diff, note="", undo_of=None):
    at = common.now_iso()
    lines = []
    for d in diff:
        row = {"at": at, "id": change_id, "source": source, "key": d["key"], "old": _shown(d["key"], d["old"]),
               "new": _shown(d["key"], d["new"])}
        if note:
            row["note"] = note
        if undo_of:
            row["undo_of"] = undo_of
        lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
    if lines:
        common.ensure_dir(os.path.dirname(history_path()))
        paths.append_text(history_path(), "\n".join(lines) + "\n", caller="configstore")


def _commit(before, after, source, note="", undo_of=None):
    diff = flat_diff(before, after)
    if not diff:
        return {"id": None, "changes": [], "backup": None}
    backup = _backup(source)
    common.write_json(config_path(), after)
    change_id = time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    _record(change_id, source, diff, note, undo_of)
    return {"id": change_id, "changes": diff, "backup": backup}


def update(changes, source, note="", undo_of=None):
    """Apply `changes` (dict of dotted keys, or a callable editing the raw config) under the lock."""
    with _Lock():
        raw, err = read_raw()
        if raw is None:
            raise ConfigError(err)
        after = copy.deepcopy(raw)
        if callable(changes):
            changes(after)
        else:
            for k, v in changes.items():
                set_key(after, k, v)
        return _commit(raw, after, source, note, undo_of)


def write(cfg, before, source):
    """Replace the whole raw config with `cfg` (setup's merged config). `before` is what the caller read; if the file
    changed since, the caller's view is stale and nothing is written."""
    with _Lock():
        raw, err = read_raw()
        if raw is None:
            raise ConfigError(err)
        if before is not None and raw != before:
            raise ConfigError("the config changed while this was being prepared; run it again")
        return _commit(raw, cfg, source)


# --------------------------------------------------------------------------- history / undo

def history(limit=50, source=None):
    """Newest first, one row per changed key."""
    try:
        with open(history_path(), encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []
    if source:
        rows = [r for r in rows if r.get("source") == source]
    return rows[::-1][:limit]


def undo(source="dashboard"):
    """Revert the newest change made by `source` that hasn't been undone. Refused when any of its keys changed since
    (someone else edited them), so an undo never overwrites a newer change."""
    rows = history(limit=2000)
    undone = {r.get("undo_of") for r in rows if r.get("undo_of")}
    target = next((r["id"] for r in rows if r.get("source") == source and r["id"] not in undone and not r.get("undo_of")), None)
    if not target:
        raise ConfigError("there is no change to undo")
    keys = [r for r in rows if r["id"] == target]
    if any(r.get("old") == "(hidden)" or r.get("new") == "(hidden)" for r in keys):
        raise ConfigError("that change touched a hidden value; change it back with /cg:setup")
    raw, err = read_raw()
    if raw is None:
        raise ConfigError(err)
    stale = [r["key"] for r in keys if get(raw, r["key"]) != r.get("new")]
    if stale:
        raise ConfigError("%s changed again since; undo would overwrite that" % ", ".join(stale))
    return update({r["key"]: r.get("old") for r in keys}, source, note="undo", undo_of=target)
