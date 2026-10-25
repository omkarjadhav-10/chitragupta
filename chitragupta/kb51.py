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
