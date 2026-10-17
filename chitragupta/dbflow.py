"""DB flow map per ticket: where each change originates, which columns change, from what to what, under
which condition. Built from extraction `data_flow[]` / `table_relations[]` and `worklog-dbflow` blocks.

data/tickets/<T>.json:  data_flow{key: entry}, table_relations{key: rel}, dbflow_conflicts[], dbflow_overview
Rendered: tickets/<T>/db-flow.md and knowledge/db-map.md.
"""
import json
import re

from . import common

RANK = {"confirmed": 3, "likely": 2, "hypothesis": 1}
OPS = ("select", "insert", "update", "delete", "upsert", "merge", "proc")


def flow_key(t, e):
    return common.stable_id(t, e.get("scenario") or "", e.get("component") or "", (e.get("table") or "").lower(), e.get("operation") or "")


def rel_key(r):
    return common.stable_id((r.get("from") or "").lower(), (r.get("to") or "").lower(), r.get("kind") or "")


def _norm_entry(e, day, source):
    cw = []
    for c in e.get("columns_written") or []:
        if isinstance(c, dict) and c.get("column"):
            cw.append({"column": c["column"], "change": c.get("change") or "", "condition": c.get("condition") or ""})
        elif isinstance(c, str) and c:
            cw.append({"column": c, "change": "", "condition": ""})
    op = (e.get("operation") or "").lower()
    return {"scenario": e.get("scenario") or "main", "step": e.get("step") if isinstance(e.get("step"), int) else None,
            "trigger": e.get("trigger") or "", "component": e.get("component") or "", "operation": op if op in OPS else (op or "select"),
            "table": e.get("table") or "", "columns_read": [c for c in e.get("columns_read") or [] if c],
            "columns_written": cw, "via": e.get("via") or "", "transaction": e.get("transaction") or "",
            "evidence": [x for x in e.get("evidence") or [] if x][:8], "confidence": e.get("confidence") if e.get("confidence") in RANK else "likely",
            "date": day, "source": source}


def merge(store, t, flows, relations, day, source="extraction", overview=None):
    """Merge by ticket+scenario+component+table+operation; updates replace; contradictions keep both."""
    tk = store.ticket(t)
    df = tk.setdefault("data_flow", {})
    tr = tk.setdefault("table_relations", {})
    conflicts = tk.setdefault("dbflow_conflicts", [])
    n_new = n_upd = 0
    for e in flows or []:
        if not e.get("table") or not e.get("component"):
            continue
        rec = _norm_entry(e, day, source)
        k = flow_key(t, rec)
        old = df.get(k)
        if not old:
            rec["first_date"] = day
            df[k] = rec
            n_new += 1
            continue
        oc = {c["column"]: c["change"] for c in old.get("columns_written") or [] if c.get("change")}
        diff = [c for c in rec["columns_written"] if c["change"] and c["column"] in oc and oc[c["column"]] != c["change"]]
        if diff and RANK[rec["confidence"]] < RANK.get(old.get("confidence"), 2):
            for c in diff:
                _conflict(conflicts, rec, c["column"], oc[c["column"]], old.get("confidence"), c["change"], rec["confidence"], day)
            continue
        for c in diff:
            _conflict(conflicts, rec, c["column"], c["change"], rec["confidence"], oc[c["column"]], old.get("confidence"), day)
        merged = dict(rec)
        merged["evidence"] = list(dict.fromkeys(rec["evidence"] + (old.get("evidence") or [])))[:10]
        merged["columns_read"] = list(dict.fromkeys(rec["columns_read"] + (old.get("columns_read") or [])))
        keep_cols = {c["column"] for c in rec["columns_written"]}
        merged["columns_written"] = rec["columns_written"] + [c for c in old.get("columns_written") or [] if c["column"] not in keep_cols]
        merged["first_date"] = old.get("first_date") or old.get("date")
        if {k2: v for k2, v in merged.items() if k2 != "date"} != {k2: v for k2, v in old.items() if k2 != "date"}:
            df[k] = merged
            n_upd += 1
    for r in relations or []:
        if not r.get("from") or not r.get("to"):
            continue
        rec = {"from": r["from"], "to": r["to"], "kind": r.get("kind") if r.get("kind") in ("fk", "join", "logical") else "logical",
               "evidence": r.get("evidence") or "", "date": day}
        k = rel_key(rec)
        if k not in tr:
            tr[k] = rec
            n_new += 1
    if overview:
        tk["dbflow_overview"] = overview.strip()
    if n_new or n_upd:
        tk["dbflow_updated"] = day
        tk["dbflow_dirty"] = True
        tk["dbflow_dirty_since"] = tk.get("dbflow_dirty_since") or common.now_iso()
        store.touch_ticket(t)
        _catalog(store, t, tk)
    return n_new, n_upd


def _conflict(conflicts, rec, column, kept, kept_conf, other, other_conf, day):
    sig = (rec["table"], rec["component"], column, kept, other)
    if any((c["table"], c["component"], c["column"], c["kept"], c["other"]) == sig for c in conflicts):
        return
    conflicts.append({"table": rec["table"], "component": rec["component"], "column": column, "kept": kept,
                      "kept_confidence": kept_conf, "other": other, "other_confidence": other_conf, "date": day})


def entries(tk):
    """What the views show: the consolidated map when there is one, else the raw entries."""
    v = tk.get("dbflow_view") or {}
    return v["data_flow"] if v.get("data_flow") else list(tk.get("data_flow", {}).values())


def relations(tk):
    v = tk.get("dbflow_view") or {}
    return v["table_relations"] if v.get("data_flow") else list(tk.get("table_relations", {}).values())


def consolidation_input(t, tk):
    rp = tk.get("replay") or {}
    data = {"ticket": t, "title": tk.get("title"),
            "replay_scenarios": sorted({s.get("scenario") for s in rp.get("final") or [] if s.get("scenario")} |
                                       {sc.get("name") for sc in rp.get("scenarios") or [] if sc.get("name")}),
            "findings": [{"kind": i["kind"], "title": i["title"]} for i in sorted(tk.get("items", {}).values(),
                         key=lambda i: (i.get("date", ""), i.get("time", "")), reverse=True)
                         if i.get("kind") in ("root_cause", "finding", "how_it_works", "fix")][:25],
            "queries": [{"purpose": q.get("purpose"), "tables": q.get("tables")} for q in list(tk.get("queries", {}).values())[-25:]],
            "data_flow": [{k: e.get(k) for k in ("scenario", "step", "trigger", "component", "operation", "table", "columns_read",
                                                 "columns_written", "via", "evidence", "confidence", "source")}
                          for e in tk.get("data_flow", {}).values()],
            "table_relations": list(tk.get("table_relations", {}).values()),
            "recorded_conflicts": tk.get("dbflow_conflicts") or []}
    from .redact import redact
    return redact(json.dumps(data, ensure_ascii=False, indent=1))[:140000]


def consolidate(store, t, cfg, caller=None):
    """One clean map per ticket (prompts/dbflow.md). Raw entries stay as evidence. Returns cost."""
    from . import extract
    tk = store.ticket(t)
    if not tk.get("data_flow"):
        tk["dbflow_dirty"] = False
        return 0.0
    try:
        res, cost = (caller or extract.call_claude)("dbflow", consolidation_input(t, tk), cfg)
    except extract.ExtractError as e:
        common.log("agent", "db flow consolidation failed for %s: %s" % (t, e))
        import datetime as _dt
        tk["dbflow_next"] = (common.now() + _dt.timedelta(minutes=30)).isoformat(timespec="seconds")
        store.touch_ticket(t)
        return 0.0
    flows = [_norm_entry(e, tk.get("dbflow_updated") or "", "consolidated") for e in res.get("data_flow") or []
             if e.get("table") and e.get("component")]
    if flows:
        tk["dbflow_view"] = {"overview": res.get("overview") or "", "scenarios": res.get("scenarios") or [],
                             "data_flow": flows,
                             "table_relations": [dict(r, kind=r.get("kind") if r.get("kind") in ("fk", "join", "logical") else "logical")
                                                 for r in res.get("table_relations") or [] if r.get("from") and r.get("to")],
                             "conflicts": res.get("conflicts") or [], "unknowns": res.get("unknowns") or [],
                             "at": common.now_iso()}
        _catalog(store, t, tk)
    tk["dbflow_dirty"] = False
    tk.pop("dbflow_dirty_since", None)
    tk.pop("dbflow_next", None)
    store.touch_ticket(t)
    return cost


def consolidation_due(tk, cfg, last_ticket_ts, force=False):
    if not tk.get("dbflow_dirty"):
        return False
    if force:
        return True
    nxt = common.parse_ts(tk.get("dbflow_next"))
    if nxt and nxt > common.now():
        return False
    idle = int((cfg.get("extract_triggers") or {}).get("idle_minutes", 10))
    last = common.parse_ts(last_ticket_ts)
    return not last or (common.now() - last).total_seconds() >= idle * 60


def _catalog(store, t, tk):
    cat = store.catalog()
    rels = cat.setdefault("db_relations", {})
    writers = cat.setdefault("db_writers", {})
    # this ticket's rows are rebuilt from its current view
    for k in list(rels):
        if rels[k].get("tickets") == [t]:
            del rels[k]
        elif t in rels[k].get("tickets", []):
            rels[k]["tickets"].remove(t)
    for w in writers.values():
        for sect in ("writes", "reads"):
            for k in list(w[sect]):
                if t in w[sect][k]["tickets"]:
                    w[sect][k]["tickets"].remove(t)
                    if not w[sect][k]["tickets"]:
                        del w[sect][k]
    for k in [k for k, w in writers.items() if not w["writes"] and not w["reads"]]:
        del writers[k]
    for r in relations(tk):
        k = rel_key(r)
        cur = rels.setdefault(k, dict(r, tickets=[]))
        if t not in cur["tickets"]:
            cur["tickets"].append(t)
    for e in entries(tk):
        tbl = e["table"].lower()
        w = writers.setdefault(tbl, {"table": e["table"], "writes": {}, "reads": {}})
        for c in e.get("columns_written") or []:
            kk = "%s|%s" % (e["component"], c["column"])
            cur = w["writes"].setdefault(kk, {"component": e["component"], "column": c["column"], "change": c["change"], "tickets": []})
            cur["change"] = c["change"] or cur["change"]
            if t not in cur["tickets"]:
                cur["tickets"].append(t)
        if e["operation"] == "select" or e.get("columns_read"):
            cur = w["reads"].setdefault(e["component"], {"component": e["component"], "tickets": []})
            if t not in cur["tickets"]:
                cur["tickets"].append(t)
    store.catalog_dirty = True


# --------------------------------------------------------------------------- mermaid helpers

def mlabel(s, n=70):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    s = s if len(s) <= n else s[: n - 1] + "…"
    return s.replace("#", "#35;").replace('"', "#quot;").replace("|", "#124;")


def mtext(s, n=80):
    """sequenceDiagram message / alias text: no ; # or newlines."""
    s = re.sub(r"\s+", " ", str(s or "")).strip().replace(";", ",").replace("#", "#35;")
    return s if len(s) <= n else s[: n - 1] + "…"


def ent(name):
    s = re.sub(r"[^A-Za-z0-9_]+", "_", name or "").strip("_")
    return s if s and s[0].isalpha() else "T_" + s


def col_name(c):
    s = re.sub(r"[^A-Za-z0-9_]+", "_", c or "").strip("_")
    return s if s and (s[0].isalpha() or s[0] == "_") else "c_" + s


def _cols_text(e, n=3):
    cw = [c["column"] for c in e.get("columns_written") or []]
    return ", ".join(cw[:n]) + ("…" if len(cw) > n else "")


def scenarios(tk):
    out = {}
    for e in entries(tk):
        out.setdefault(e["scenario"], []).append(e)
    for k in out:
        out[k].sort(key=lambda e: (e.get("step") if e.get("step") is not None else 999, e.get("date", ""), e["component"]))
    return out


def flowchart(entries):
    ids, L = {}, ["flowchart LR"]

    def nid(kind, name):
        key = (kind, name)
        if key not in ids:
            ids[key] = "%s%d" % (kind, len([k for k in ids if k[0] == kind]) + 1)
        return ids[key]
    defs, edges = [], []
    for e in entries:
        c = nid("C", e["component"])
        d = nid("D", e["table"])
        if e.get("trigger"):
            tr = nid("T", e["trigger"])
            edge = '    %s -->|"calls"| %s' % (tr, c)
            if edge not in edges:
                edges.append(edge)
        if e["operation"] == "select":
            lab = ("read " + ", ".join(e["columns_read"][:3])) if e.get("columns_read") else "select"
            edge = '    %s -.->|"%s"| %s' % (d, mlabel(lab, 60), c)
        else:
            lab = e["operation"] + (": " + _cols_text(e) if e.get("columns_written") else "")
            edge = '    %s -->|"%s"| %s' % (c, mlabel(lab, 60), d)
        if edge not in edges:
            edges.append(edge)
    for (kind, name), i in ids.items():
        if kind == "T":
            defs.append('    %s(["%s"])' % (i, mlabel(name)))
        elif kind == "C":
            defs.append('    %s["%s"]' % (i, mlabel(name)))
        else:
            defs.append('    %s[("%s")]' % (i, mlabel(name)))
    return "\n".join(L + defs + edges)


def sequence(entries):
    parts, L = {}, ["sequenceDiagram"]

    def pid(name, prefix):
        if name not in parts:
            parts[name] = "%s%d" % (prefix, len(parts) + 1)
        return parts[name]
    msgs = []
    client = pid("Client", "P")
    for e in entries:
        trig = e.get("trigger") or "request"
        api = pid("API: " + trig, "P") if e.get("trigger") else client
        comp = pid(e["component"], "P")
        db = pid("DB: " + e["table"], "P")
        if e.get("trigger"):
            m = "    %s->>%s: %s" % (client, api, mtext(trig))
            if m not in msgs:
                msgs.append(m)
            m = "    %s->>%s: handle" % (api, comp)
            if m not in msgs:
                msgs.append(m)
        cond = [c["condition"] for c in e.get("columns_written") or [] if c.get("condition")]
        changes = ", ".join(("%s %s" % (c["column"], c["change"])).strip() for c in (e.get("columns_written") or [])[:3])
        text = e["operation"].upper() + (" " + changes if changes else "") + (" when " + cond[0] if cond else "")
        msgs.append("    %s->>%s: %s" % (comp, db, mtext(text, 110)))
    for name, i in parts.items():
        L.append("    participant %s as %s" % (i, mtext(name, 60)))
    return "\n".join(L + msgs)


def er(tables, relations, entries):
    L = ["erDiagram"]
    cols = {}
    for e in entries:
        s = cols.setdefault(ent(e["table"]), [])
        for c in [x["column"] for x in e.get("columns_written") or []] + list(e.get("columns_read") or []):
            c = col_name(c.split(".")[-1])
            if c not in s:
                s.append(c)
    names = {ent(t) for t in tables}
    rel_lines = []
    for r in relations:
        a, b = ent(r["from"].rsplit(".", 1)[0]), ent(r["to"].rsplit(".", 1)[0])
        names |= {a, b}
        for side, full in ((a, r["from"]), (b, r["to"])):
            c = col_name(full.rsplit(".", 1)[-1]) if "." in full else None
            if c and c not in cols.setdefault(side, []):
                cols[side].append(c)
        card = "}o--||" if r["kind"] == "fk" else "}o..o{"
        rel_lines.append('    %s %s %s : "%s"' % (a, card, b, mlabel("%s %s" % (r["kind"], r["from"].rsplit(".", 1)[-1]), 40)))
    for n in sorted(names):
        cs = cols.get(n) or []
        if cs:
            L.append("    %s {" % n)
            L += ["        col %s" % c for c in cs[:12]]
            L.append("    }")
        else:
            L.append("    %s" % n)
    return "\n".join(L + rel_lines)


# --------------------------------------------------------------------------- views

def verify_links(tk, entry):
    """Queries in queries.md that read this entry's table (and ideally a written column)."""
    out = []
    tbl = (entry.get("table") or "").lower()
    short = tbl.split(".")[-1]
    wcols = [c["column"].lower() for c in entry.get("columns_written") or []]
    for q in sorted(tk.get("queries", {}).values(), key=lambda q: (q.get("date", ""), q.get("time", "")), reverse=True):
        text = (q.get("text") or "").lower()
        tabs = [x.lower() for x in q.get("tables") or []]
        if any(x == tbl or x.split(".")[-1] == short for x in tabs) or re.search(r"\b%s\b" % re.escape(short), text):
            score = sum(1 for c in wcols if c in text)
            out.append((score, q))
    out.sort(key=lambda x: -x[0])
    return [q for _, q in out[:2]]


def qlink(q, prefix="queries.md"):
    return "[%s](%s#q-%s)" % ((q.get("purpose") or "query").replace("|", "\\|").replace("]", ")")[:70], prefix, q["id"])


def overview(t, tk, scs):
    if (tk.get("dbflow_view") or {}).get("overview"):
        return tk["dbflow_view"]["overview"]
    if tk.get("dbflow_overview"):
        return tk["dbflow_overview"]
    bits = []
    for name, es in scs.items():
        tables = list(dict.fromkeys(e["table"] for e in es))
        chg = ["`%s.%s` %s" % (e["table"].split(".")[-1], c["column"], c["change"]) for e in es for c in e.get("columns_written") or [] if c.get("change")]
        trig = next((e["trigger"] for e in es if e.get("trigger")), "")
        s = "Scenario **%s**%s touches %s." % (name, " (triggered by %s)" % code(trig) if trig else "", ", ".join(code(x) for x in tables))
        if chg:
            s += " Key changes: " + "; ".join(chg[:4]) + "."
        bits.append(s)
    return " ".join(bits[:3])


def code(s):
    return "`%s`" % str(s).replace("`", "'")


def cell(s):
    return re.sub(r"\s+", " ", str(s or "")).replace("|", "\\|").strip()


DEFAULT_SOURCES_LABEL = "entities, repositories, SQL, procs, integration channels"


def sources_label(cfg=None):
    """What the db-flow header says the map is built from (config dbflow.sources_label)."""
    cfg = cfg if cfg is not None else common.load_config()
    return ((cfg.get("dbflow") or {}).get("sources_label")) or DEFAULT_SOURCES_LABEL


def render_ticket(t, tk, notes_block, cfg=None):
    scs = scenarios(tk)
    L = ["# %s — DB flow" % t, "",
         "_Generated from what Claude Code sessions read, changed and ran (%s). "
         "Ask \"map the DB flow for %s\" in a session for a deep trace. Edit only inside \"My notes\"._" % (
             sources_label(cfg), t), ""]
    if not scs:
        L += ["_No DB flow traced yet._", "", "## My notes", notes_block, ""]
        return "\n".join(L)
    allent = [e for es in scs.values() for e in es]
    main = max(scs, key=lambda k: len(scs[k]))
    L += ["## Overview", overview(t, tk, scs), ""]
    L += ["## Flow", "```mermaid", flowchart(allent), "```", ""]
    L += ["## Sequence — %s" % main, "```mermaid", sequence(scs[main]), "```", ""]
    L += ["## Table map", "", "| Step | Table | Operation | Columns read | Columns written (change) | Condition | Where | Confidence |",
          "|---|---|---|---|---|---|---|---|"]
    for name, es in scs.items():
        for i, e in enumerate(es):
            cw = "; ".join(("%s %s" % (c["column"], c["change"])).strip() for c in e.get("columns_written") or [])
            cond = "; ".join(dict.fromkeys(c["condition"] for c in e.get("columns_written") or [] if c.get("condition")))
            where = e["component"] + ((" — " + e["evidence"][0]) if e.get("evidence") else "")
            L.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
                cell("%s %s" % (name, e["step"] if e.get("step") is not None else i + 1)), code(e["table"]), e["operation"],
                cell(", ".join(e.get("columns_read") or [])) or "—", cell(cw) or "—", cell(cond) or "—", cell(where), e.get("confidence")))
    L.append("")
    changes = [(e, c) for e in allent for c in e.get("columns_written") or [] if c.get("change")]
    L += ["## Column changes", ""]
    if changes:
        L += ["| Table.column | Before → After | Set by | Condition | Evidence | Verify |", "|---|---|---|---|---|---|"]
        for e, c in changes:
            v = verify_links(tk, e)
            L.append("| %s | %s | %s | %s | %s | %s |" % (code("%s.%s" % (e["table"], c["column"])), cell(c["change"]), cell(e["component"]),
                                                       cell(c.get("condition")) or "always", cell("; ".join(e.get("evidence") or [])[:200]) or "—",
                                                       ", ".join(qlink(q) for q in v) or "—"))
    else:
        L.append("_No column changes traced yet._")
    L.append("")
    tables = list(dict.fromkeys(e["table"] for e in allent))
    rels = relations(tk)
    L += ["## Relationships", "```mermaid", er(tables, rels, allent), "```", ""]
    if rels:
        L += ["| From | To | Kind | Evidence |", "|---|---|---|---|"]
        L += ["| %s | %s | %s | %s |" % (code(r["from"]), code(r["to"]), r["kind"], cell(r.get("evidence"))) for r in rels]
        L.append("")
    L += ["## Verify", ""]
    seen = set()
    for e in allent:
        for q in verify_links(tk, e):
            if q["id"] not in seen:
                seen.add(q["id"])
                L.append("- %s — tables %s" % (qlink(q), ", ".join(code(x) for x in q.get("tables") or []) or "—"))
    if not seen:
        L.append("_No matching query in [queries.md](queries.md) yet._")
    L.append("")
    L += ["## Unknowns & conflicts", ""]
    unk = ["- %s %s on %s — _%s_%s" % (e["operation"], code(e["table"]), cell(e["component"]), e["confidence"],
                                       (" (" + "; ".join(e["evidence"][:2]) + ")") if e.get("evidence") else "")
           for e in allent if e.get("confidence") == "hypothesis"]
    unk += ["- %s.%s in %s: recorded %s (%s) — also seen %s (%s), %s" % (code(c["table"]), c["column"], cell(c["component"]), cell(c["kept"]),
                                                                        c.get("kept_confidence"), cell(c["other"]), c.get("other_confidence"), c["date"])
            for c in tk.get("dbflow_conflicts") or []]
    view = tk.get("dbflow_view") or {}
    for c in view.get("conflicts") or []:
        vs = "; ".join("%s%s (%s%s)" % (cell(v.get("change")), (" when " + cell(v.get("condition"))) if v.get("condition") else "",
                                         v.get("confidence") or "", (", " + cell(v.get("evidence"))[:80]) if v.get("evidence") else "")
                       for v in c.get("versions") or [])
        unk.append("- %s: %s" % (code("%s.%s" % (c.get("table"), c.get("column"))), vs))
    unk += ["- " + cell(u) for u in view.get("unknowns") or []]
    unk += ["- %s %s has no columns traced" % (e["operation"], code(e["table"])) for e in allent
            if e["operation"] != "select" and not e.get("columns_written")]
    L += unk or ["_Nothing open._"]
    L += ["", "## My notes", notes_block, ""]
    return "\n".join(L)


def summary_line(tk):
    """One line for SessionStart context."""
    es = entries(tk)
    if not es:
        return ""
    tables = list(dict.fromkeys(e["table"] for e in es))
    chg = ["%s.%s %s%s" % (e["table"].split(".")[-1], c["column"], c["change"], (" when " + c["condition"]) if c.get("condition") else "")
           for e in es for c in e.get("columns_written") or [] if c.get("change")]
    return "DB flow: tables %s%s." % (", ".join(tables[:6]), ("; key changes: " + "; ".join(chg[:3])) if chg else "")


def render_db_map(store):
    cat = store.catalog()
    writers = cat.get("db_writers") or {}
    rels = list((cat.get("db_relations") or {}).values())
    L = ["# DB map", "", "Every table seen in ticket DB flows, how they relate, and which components write which columns.",
         "Per-ticket detail lives in `tickets/<TICKET>/db-flow.md`.", ""]
    if not writers and not rels:
        return "\n".join(L + ["_Nothing traced yet._", ""])
    tables = sorted({w["table"] for w in writers.values()} | {r[k].rsplit(".", 1)[0] for r in rels for k in ("from", "to")})
    by_schema = {}
    for tb in tables:
        by_schema.setdefault(tb.split(".")[0] if tb.count(".") >= 1 else "(default)", []).append(tb)
    split = len(tables) > 25
    groups = by_schema.items() if split else [("all", tables)]
    for name, tbs in groups:
        rs = [r for r in rels if r["from"].rsplit(".", 1)[0] in tbs or r["to"].rsplit(".", 1)[0] in tbs]
        L += (["## Relationships — %s" % name] if split else ["## Relationships"]) + ["```mermaid", er(tbs, rs, []), "```", ""]
    L += ["## Writers per table", ""]
    for key in sorted(writers):
        w = writers[key]
        L += ["### %s" % code(w["table"]), ""]
        if w["writes"]:
            L += ["| Column | Change | Written by | Tickets |", "|---|---|---|---|"]
            for x in sorted(w["writes"].values(), key=lambda x: (x["column"], x["component"])):
                L.append("| %s | %s | %s | %s |" % (code(x["column"]), cell(x.get("change")) or "—", cell(x["component"]),
                                                   ", ".join("[%s](../tickets/%s/db-flow.md)" % (t, t) for t in x["tickets"])))
        if w["reads"]:
            L.append("")
            L.append("Read by: " + "; ".join("%s (%s)" % (cell(x["component"]), ", ".join(x["tickets"])) for x in w["reads"].values()))
        L.append("")
    return "\n".join(L) + "\n"
