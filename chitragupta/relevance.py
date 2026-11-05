"""Additive relevance and validated brief picks; canonical records are never removed."""
import re

from . import common

LEVELS = ("core", "supporting", "noise")
ROLES = ("cause", "fix-design", "verify", "context")
CAPS = {"queries": 8, "tables": 6, "apis": 5}
CATALOG = re.compile(r"(?i)information_schema|pg_catalog|\bsys\.\w|^\s*(?:show\b|\\d)|has_perms_by_name|table_constraints|key_column_usage|\bpg_\w+\(|sp_help|object_id\(")
SHELL = re.compile(r"(?i)psql(\.exe)?\b|PGPASSWORD|\bexport\s+\w+=|set -a")
NORES = re.compile(r"(?i)not (shown|recorded|described|stated) in|result not|not in the chunk|output is not|not yet run|not run yet|no conclusion|\(result not recorded\)|^\s*$|query template|will show")
TESTDATA = re.compile(r"(?i)bruno (request )?body|so the bruno|keeps? them|does not wipe|before the (local )?(upload )?test\b|suitable for .*test|pick (a|any) |find (a|the) (qa )?test (order|user|physician)|look up the qa (test )?order|by auth0 user id|test data")
NEG = re.compile(r"(?i)\b(0|zero|no)\s+(\w+\s+)?(rows?|matching|match|results?)\b|\bnever\b|\bnot (found|present)\b|does not exist|\bmissing\b")
TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def sql_shape(text):
    """Only literals and spacing fold; different projections remain distinct evidence."""
    s = re.sub(r"'([^']|'')*'", "?", text or "")
    s = re.sub(r"\b\d+(?:\.\d+)?\b|(?<!:):\w+", "?", s)
    s = re.sub(r"(?i)set default_transaction_read_only\s*=\s*on;", "", s)
    return re.sub(r"\s+", " ", s).lower().strip(" ;")


def cited_queries(tk):
    cited = set()
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "cites" and isinstance(v, list):
                    cited.update(c[6:] for c in v if isinstance(c, str) and c.startswith("query:"))
                else:
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(tk.get("brief") or {})
    return cited


def problem_tables(tk):
    return {n.lower() for n in re.findall(r"\b[A-Za-z_]\w*\.[A-Za-z_]\w*\b", tk.get("problem") or "")}


def flow_scenarios(tk):
    names = {e.get("scenario") or "main" for e in (tk.get("data_flow") or {}).values()}
    view = tk.get("dbflow_view") or {}
    names |= {e.get("scenario") or "main" for e in view.get("data_flow") or []}
    names |= {s["name"] for s in view.get("scenarios") or [] if s.get("name")}
    return sorted(names)


def _tag(rec, level, reason="", variant=""):
    rec.update(relevance=level, hidden_reason=reason, variant_of=variant)


def classify(tk, other_tables=()):
    """Conservative candidates until a validated brief chooses core. No arbitrary top-N ranker."""
    cited, key, mine = cited_queries(tk), tk.get("id"), problem_tables(tk)
    groups = {}
    for kind in ("queries", "tables", "apis", "files"):
        for rid, rec in (tk.get(kind) or {}).items():
            if not isinstance(rec, dict):
                continue
            # Model decisions survive repeated deterministic passes; rule decisions are recomputed.
            level = rec.get("relevance") if rec.get("relevance_source") == "brief" else "core"
            reason = rec.get("hidden_reason") if level != "core" else ""
            _tag(rec, level if level in LEVELS else "core", reason or "")
            if rec.get("archived"):
                _tag(rec, "noise", "archived")
            elif rec.get("review"):
                _tag(rec, "supporting", "needs review")
            elif kind == "queries":
                txt = rec.get("text") or ""
                qt = {x.lower() for x in rec.get("tables") or []}
                why = ""
                if rec.get("sent") is False:
                    why = "not sent"
                elif CATALOG.search(txt) or any(CATALOG.search(t) for t in qt):
                    why = "catalog"
                elif SHELL.search(txt):
                    why = "shell"
                elif rid not in cited and NORES.search(rec.get("revealed") or ""):
                    why = "no-result"
                elif rid not in cited and TESTDATA.search(rec.get("purpose") or ""):
                    why = "test-data"
                elif rid not in cited:
                    named = set(TICKET.findall((rec.get("purpose") or "") + " " + (rec.get("revealed") or ""))) - {key}
                    others = set(other_tables)
                    if named or (qt and qt <= others and not qt & mine):
                        # Review only; do not reassign the ticket or infer a destination by wording.
                        if not rec.get("review_kept"):
                            rec.setdefault("review", {"why": "other-ticket query", "suggest": sorted(named)[0] if named else None,
                                                      "at": common.now_iso()})
                            _tag(rec, "supporting", "needs review")
                if why:
                    _tag(rec, "noise", why)
                if not why and not rec.get("review"):
                    shape = (sql_shape(txt), tuple(sorted(qt)), bool(NEG.search(rec.get("revealed") or "")))
                    groups.setdefault(shape, []).append((rid, rec))
    for rows in groups.values():
        newest = max(rows, key=lambda x: (x[1].get("date") or "", x[1].get("time") or "", x[0]))[0]
        for rid, rec in rows:
            if rid != newest and rid not in cited:
                _tag(rec, "noise", "literal variant", newest)
    # Scenario metadata covers raw and consolidated entries without discarding either.
    tags = tk.setdefault("scenario_relevance", {})
    for name in flow_scenarios(tk):
        tags.setdefault(name, {"relevance": "core", "hidden_reason": "", "variant_of": ""})
    for rec in list((tk.get("data_flow") or {}).values()) + list((tk.get("dbflow_view") or {}).get("data_flow") or []):
        rec.update(tags.get(rec.get("scenario") or "main") or {})
    for sc in (tk.get("dbflow_view") or {}).get("scenarios") or []:
        sc.update(tags.get(sc.get("name")) or {})


def eligible(rec):
    return isinstance(rec, dict) and not rec.get("archived") and not rec.get("review")


def core(tk, kind, cap=None):
    rows = [(k, r) for k, r in (tk.get(kind) or {}).items() if eligible(r) and r.get("relevance", "core") == "core"
            and not r.get("variant_of") and (kind != "queries" or r.get("sent") is not False)]
    cited = cited_queries(tk) if kind == "queries" else set()
    rows.sort(key=lambda x: x[0])
    rows.sort(key=lambda x: (x[1].get("date") or x[1].get("last") or "", x[1].get("time") or ""), reverse=True)
    def priority(pair):
        rid, row = pair
        role = row.get("role")
        rank = ROLES.index(role) if row.get("relevance_source") == "brief" and role in ROLES else len(ROLES)
        return rank, rid not in cited, row.get("sent") is not True
    rows.sort(key=priority)
    return [r for _, r in (rows[:cap] if cap is not None else rows)]


def ranked(tk, kind):
    return any(r.get("relevance_source") == "brief" for r in (tk.get(kind) or {}).values() if isinstance(r, dict))


def heading(tk, kind):
    if not ranked(tk, kind):
        return "Recorded %s (not yet ranked)" % {"queries": "queries", "tables": "tables", "apis": "APIs"}[kind]
    return {"queries": "Key queries", "tables": "Core tables", "apis": "Core APIs"}[kind]


def query_groups(tk):
    """Default query views: five key picks plus three context picks, or eight unranked."""
    rows = core(tk, "queries")
    if not ranked(tk, "queries"):
        return [(heading(tk, "queries"), rows[:8])]
    key = [q for q in rows if q.get("role") != "context"][:5]
    context = [q for q in rows if q.get("role") == "context"][:3]
    return [("Key queries", key)] + ([("Also used", context)] if context else [])


def listed_queries(tk):
    return [q for _, rows in query_groups(tk) for q in rows]


def candidates(tk):
    """Small, identified records for the existing brief call; no second model call."""
    data = {}
    fields = {"queries": ("purpose", "revealed", "tables", "date", "sent"), "tables": ("purpose",), "apis": ("purpose", "path")}
    for kind in CAPS:
        data[kind] = [dict(id=k, **{f: r.get(f) for f in fields[kind]}) for k, r in (tk.get(kind) or {}).items()
                      if eligible(r) and r.get("relevance") != "noise"]
    for row in data["queries"]:
        row["sql"] = " ".join(((tk["queries"][row["id"]].get("text")) or "").split())[:160]
    data["scenarios"] = flow_scenarios(tk)
    return data


def apply_picks(tk, picks):
    """Validate every id/role before applying; never promote reviewed, archived or deterministic noise."""
    dropped = []
    if not isinstance(picks, dict):
        return dropped
    for kind, cap in CAPS.items():
        if kind not in picks or not isinstance(picks[kind], list):
            continue
        selected = {}
        for pick in picks[kind]:
            rid = pick.get("id") if isinstance(pick, dict) else pick
            row = (tk.get(kind) or {}).get(rid) if isinstance(rid, str) else None
            role = pick.get("role") if isinstance(pick, dict) else None
            if not eligible(row) or row.get("relevance") == "noise" or (kind == "queries" and role not in ROLES):
                dropped.append({"kind": kind, "id": rid, "why": "unknown or ineligible id/role"})
                continue
            if rid in selected:
                continue
            role_cap = 3 if role == "context" else 5
            same_group = sum((r == "context") == (role == "context") for r in selected.values())
            if len(selected) >= cap or (kind == "queries" and same_group >= role_cap):
                dropped.append({"kind": kind, "id": rid, "why": "pick cap"})
                continue
            selected[rid] = role
        # Entirely invalid answers cannot demote the ticket's existing candidates.
        if picks[kind] and not selected:
            continue
        for rid, row in (tk.get(kind) or {}).items():
            if not eligible(row) or row.get("relevance") == "noise":
                continue
            _tag(row, "core" if rid in selected else "supporting", "" if rid in selected else "recorded context")
            row["relevance_source"] = "brief"
            if kind == "queries":
                row["role"] = selected.get(rid) or ""
    tags = tk.setdefault("scenario_relevance", {})
    valid = set(flow_scenarios(tk))
    for pick in picks.get("scenarios") or []:
        name, level = pick.get("id"), pick.get("relevance")
        if name not in valid or level not in LEVELS:
            dropped.append({"kind": "scenarios", "id": name, "why": "unknown id/relevance"})
            continue
        tags[name] = {"relevance": level, "hidden_reason": "" if level == "core" else "outside the problem's main flow", "variant_of": ""}
    classify(tk)
    return dropped


def focused(tk):
    """Legacy unmigrated files keep their old view until additive classification runs."""
    return "scenario_relevance" in tk or any("relevance" in r for kind in ("queries", "tables", "apis", "files")
                                            for r in (tk.get(kind) or {}).values() if isinstance(r, dict))


def catalog_view(cat, lookup):
    """Project the derived catalog from eligible owning-ticket records without rewriting evidence."""
    import copy
    tickets = {t: lookup(t) or {} for kind in ("tables", "apis") for r in (cat.get(kind) or {}).values()
               for t in r.get("tickets") or []}
    for w in (cat.get("db_writers") or {}).values():
        for section in ("reads", "writes"):
            for r in (w.get(section) or {}).values():
                for t in r.get("tickets") or []:
                    if t not in tickets:
                        tickets[t] = lookup(t) or {}
    if not any(focused(tk) for tk in tickets.values()):
        return cat
    out = copy.deepcopy(cat)
    def allowed(t, kind, key):
        tk = tickets.get(t) or {}
        if not focused(tk):
            return True
        rows = tk.get(kind) or {}
        r = next((r for k, r in rows.items() if k.casefold() == key.casefold()), None)
        if r is not None:
            return eligible(r) and r.get("relevance", "core") in ("core", "supporting")
        if kind == "tables":
            from . import dbflow
            return any((e.get("table") or "").casefold() == key.casefold() for e in dbflow.entries(tk, ("core", "supporting")))
        return False
    for kind in ("tables", "apis"):
        rows = {}
        for key, r in (out.get(kind) or {}).items():
            owners = [t for t in r.get("tickets") or [] if allowed(t, kind, key)]
            if owners:
                r["tickets"] = owners
                rows[key] = r
        out[kind] = rows
    from . import dbflow
    for t, tk in tickets.items():
        if focused(tk):
            for e in dbflow.entries(tk, ("core", "supporting")):
                name = e.get("table") or ""
                if name and allowed(t, "tables", name):
                    out.setdefault("tables", {}).setdefault(name, {"name": name, "tickets": [t]})
    good_tables = set(k.casefold() for k in out.get("tables") or {})
    def good_ref(ref):
        return any(str(ref).casefold() == t or str(ref).casefold().startswith(t + ".") for t in good_tables)
    out["db_relations"] = {k: r for k, r in (out.get("db_relations") or {}).items()
                            if good_ref(r.get("from")) and good_ref(r.get("to"))}
    writers = {}
    for key, w in (out.get("db_writers") or {}).items():
        table = w.get("table") or key
        if table.casefold() not in good_tables:
            continue
        for section in ("reads", "writes"):
            rows = {}
            for rid, row in (w.get(section) or {}).items():
                owners = [t for t in row.get("tickets") or [] if allowed(t, "tables", table)]
                if owners:
                    row["tickets"] = owners
                    rows[rid] = row
            w[section] = rows
        if w["reads"] or w["writes"]:
            writers[key] = w
    out["db_writers"] = writers
    out["relevance_filtered"] = True
    return out


def source_records(tk):
    """Hash all generation facts without feeding relevance output back into freshness."""
    out = {}
    excluded = {"relevance", "relevance_source", "hidden_reason", "variant_of"}
    for kind in ("queries", "tables", "apis", "files", "data_flow"):
        out[kind] = {key:{k:v for k,v in row.items() if k not in excluded and not (kind == "queries" and k == "role")}
                     for key,row in (tk.get(kind) or {}).items() if isinstance(row,dict)}
    out["scenarios"] = flow_scenarios(tk)
    return out
