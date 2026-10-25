"""Forward-only, backed-up knowledge migrations for 0.5.1."""
from . import common, store as S


def history_compact(dry_run, notes, backup):
    keys = S.Store.all_ticket_ids()
    paths = [S.ticket_path(t) for t in keys]
    if not keys:
        return
    backup(paths + [common.hp("data", "history")])
    for key in keys:
        tk = common.read_json(S.ticket_path(key), {})
        changed = False
        for it in (tk.get("items") or {}).values():
            if not isinstance(it, dict) or not it.get("history"):
                continue
            hist = S.union_history(it["history"])
            if hist != it["history"] or len(hist) > 5:
                changed = True
                if not dry_run:
                    S.compact_history(it, key)
        if changed:
            notes.append("%s: compact history; overflow retained in data/history" % key)
            if not dry_run:
                common.write_json(S.ticket_path(key), tk)


def record_provenance(dry_run, notes, backup):
    """Older unknown origins remain explicitly legacy; never invent a time or silently move records."""
    keys = S.Store.all_ticket_ids()
    paths = [S.ticket_path(t) for t in keys] + [S.catalog_path()]
    backup(paths)
    for path in paths:
        data = common.read_json(path, {})
        changed = False
        for kind in ("queries", "tables", "apis", "files"):
            for record in (data.get(kind) or {}).values():
                if not isinstance(record, dict):
                    continue
                for field, default in (("time", ""), ("src", "legacy")):
                    if field not in record:
                        record[field] = default
                        changed = True
        if changed:
            notes.append("%s: explicit legacy provenance for older records" % path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
            if not dry_run:
                common.write_json(path, data)


def record_review(dry_run, notes, backup):
    """Flag suspected record routing additively; owner decisions and previous flags survive."""
    from . import offticket
    keys = S.Store.all_ticket_ids()
    backup([S.ticket_path(t) for t in keys])
    prefixes = common.load_config().get("ticket_prefixes") or []
    for t in keys:
        tk = common.read_json(S.ticket_path(t), {})
        changed = False
        for kind in ("queries", "tables", "apis"):
            for key, rec in (tk.get(kind) or {}).items():
                if not isinstance(rec, dict) or rec.get("review"):
                    continue
                rv = offticket.record_check(tk, t, kind, key, rec, prefixes)
                if rv:
                    rec["review"] = rv
                    changed = True
        if changed:
            notes.append("%s: off-ticket record review flags; no records moved" % t)
            if not dry_run:
                common.write_json(S.ticket_path(t), tk)
