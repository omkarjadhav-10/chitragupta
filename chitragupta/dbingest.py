"""Deterministic ingestion of what Chitragupta already holds into the canonical database-flow graph (database flow D1,
plan §3). No database query, no model call: it reads local files and writes data/db/ through dbstore.

Sources, each fingerprinted in data/db/sources.json (dbstore.source_fingerprint over the source's content and the
access policy generation): an unchanged source is skipped, re-running adds nothing (every ID is a hash of identity,
every time comes from the source), and one run handles at most MAX_CARDS changed cards and MAX_TICKETS changed
tickets in a stable order (the rest wait for the next run).

  card:<conn>/<schema.table>  schema cards (mcpjobs.schema_cards, dbflows.discover_metadata) of an approved connection
                              -> table node (env / engine / database from the approved scope entry, never guessed; a
                              card of another connection is skipped), column members (type, nullable, default, keys,
                              personal-data flag), foreign keys from `references` (declared_metadata), triggers and
                              catalog writer routines (`writes`, declared_metadata; missing_since -> historical), a
                              routine whose definition can't be read (claim routine_unreadable), and the per-source
                              state of describe / triggers / writers (last success, last attempt, error, rows,
                              truncated). A failed source keeps its earlier data marked stale: never "no writers".
  ticket:<KEY>                data_flow entries -> code_symbol / api_operation / event nodes and reads / writes / calls /
                              publishes / consumes / transforms edges with column mappings (lists: composite and tenant
                              keys). A table name resolves only when exactly one approved connection's scope holds its
                              schema; several -> claim unresolved_table (needs_resolution, candidates), none -> claim
                              unresolved_table (unresolved). An evidence string path:line / path:a-b is checked against
                              the selected repositories (a unique repo-relative match, else a unique file-name match):
                              -> source evidence (code_supported: repo, revision "worktree", content hash, path,
                              lines); anything else -> a session-record source evidence, unvalidated, basis inferred.
                              table_relations -> logical_association edges, inferred (legacy relations, DB-24). The
                              consolidated view (dbflow_view) is a model proposal: an entry naming a table or event the
                              ticket's own entries don't, or citing a file that doesn't check, is rejected; the rest is
                              kept as model_proposal evidence (at most inferred).
  linkmap                     data/db-schema/_linkmap.json -> observed_sample evidence on logical_association edges
                              ("m of n sampled values matched, QA, <date>"): never a foreign key, never "verified".
  mappings                    data/db/mappings.json (confirm_mapping) -> user_confirmed evidence on transforms edges; a
                              mapping between environments is a comparison claim (cross_environment_mapping), not a
                              flow edge.

Personal context: a record that comes from a ticket names it in `context.tickets` (evidence provenance names it for
session records); readers show a ticket only when it is inside the principal's personal scope, while a technical claim
with a validated code citation stays usable (DB-03). Records several sources share (a node, an evidence record cited
by several entries) are united on write (dbstore.upsert merge): applicability, subjects and ticket context are lists.
"""
import os
import re

from . import common
from . import dbaccess
from . import dbmodel
from . import dbstore

MAX_CARDS = 200
MAX_TICKETS = 50
MAX_REPO_FILES = 50_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target", ".idea", ".vs", ".tox"}
WRITE_OPS = ("insert", "update", "delete", "upsert", "merge")
UNKNOWN_TRANSFORMS = ("", "unknown", "?", "n/a", "none")
CITE_RX = re.compile(r"^\s*(?P<path>[^\s:()]+?\.[A-Za-z0-9]{1,8})\s*(?::|#L|#)\s*L?(?P<a>\d{1,7})"
                     r"(?:\s*[-–]\s*L?(?P<b>\d{1,7}))?\s*$")
COMPONENT_CITE_RX = re.compile(r"^(?P<name>.*?)\s*\((?P<cite>[^()]*?\.[A-Za-z0-9]{1,8}\s*:\s*\d[^()]*)\)\s*$")
REF_RX = re.compile(r"^[A-Za-z0-9_$#@][A-Za-z0-9_$#@ .:-]{0,199}$")
DB_KIND_OF = {"procedure": "procedure", "sql_stored_procedure": "procedure", "p": "procedure",
              "function": "function", "sql_scalar_function": "function", "sql_table_valued_function": "function",
              "sql_inline_table_valued_function": "function", "fn": "function", "tf": "function", "if": "function",
              "trigger": "trigger", "sql_trigger": "trigger", "tr": "trigger"}


class MappingError(ValueError):
    """A mapping that can't be confirmed (an unresolved or ambiguous side): nothing is written."""


# --------------------------------------------------------------------------- merging (several sources, one record)

def _union(a, b):
    return list(dict.fromkeys(list(a or []) + list(b or [])))


def _merge_app(old, new):
    oa, na = dict(old.get("applicability") or {}), dict(new.get("applicability") or {})
    out = dict(oa, **na)
    for k in ("repos", "connections"):
        if oa.get(k) or na.get(k):
            out[k] = sorted(set(oa.get(k) or []) | set(na.get(k) or []))
    return out


def _merge_context(old, new):
    oc, nc = old.get("context") or {}, new.get("context") or {}
    if not oc and not nc:
        return None
    return {"tickets": sorted(set(oc.get("tickets") or []) | set(nc.get("tickets") or []))}


def merge_record(old, new):
    """Unite what several sources contribute to one record (dbstore.upsert merge): applicability lists, subjects,
    ticket context, contributing sources, orders. A placeholder never replaces a described record."""
    if not old:
        return new
    o_ph = bool((old.get("attrs") or {}).get("placeholder"))
    n_ph = bool((new.get("attrs") or {}).get("placeholder"))
    out = dict(old) if (n_ph and not o_ph) else dict(old, **new)  # fields only the older source gave stay
    if "attrs" in old or "attrs" in new:
        attrs = dict(out.get("attrs") or {})
        attrs["from"] = sorted(set((old.get("attrs") or {}).get("from") or []) | set((new.get("attrs") or {}).get("from") or []))
        if not attrs["from"]:
            attrs.pop("from")
        out["attrs"] = attrs
    if old.get("applicability") is not None or new.get("applicability") is not None:
        out["applicability"] = _merge_app(old, new)
    ctx = _merge_context(old, new)
    if ctx is not None:
        out["context"] = ctx
    for k in ("subjects", "evidence", "orders"):
        if isinstance(old.get(k), list) or isinstance(new.get(k), list):
            if k == "orders":  # ticket step orders: kept per ticket and scenario
                seen, lst = set(), []
                for x in (old.get(k) or []) + (new.get(k) or []):
                    sig = repr(sorted(x.items())) if isinstance(x, dict) else repr(x)
                    if sig not in seen:
                        seen.add(sig)
                        lst.append(x)
                out[k] = lst
            else:
                out[k] = _union(old.get(k), new.get(k))
    return out


# --------------------------------------------------------------------------- one run

class Run:
    """One ingestion pass: the current graph, the access context, and the records it produces."""

    def __init__(self, cfg, ctx=None, graph=None, now=None):
        from . import mcpjobs
        self.cfg = cfg
        self.ctx = ctx or dbaccess.AccessContext(cfg)
        self.graph = graph if graph is not None else dbstore.load_graph()
        self.server = mcpjobs.server_name(cfg, "database")
        self.now = now or common.now_iso()
        self.out = {c: {} for c in dbstore.GRAPH}
        self.summary = {"cards": 0, "tickets": 0, "linkmap": 0, "mappings": 0, "skipped": [], "rejected": 0,
                        "unchanged": 0, "changed": {}, "sources": []}
        self.repo_check = dbmodel.repo_checker(self.ctx.repo_roots)
        self._file_index = {}
        self.source = ""

    # ------------------------------------------------------------------ records
    def add(self, coll, rec):
        rec = dict(rec)
        if self.source and coll == "nodes":
            rec["attrs"] = dict(rec.get("attrs") or {}, **{"from": [self.source]})
        cur = self.out[coll].get(rec["id"])
        self.out[coll][rec["id"]] = merge_record(cur, rec) if cur else rec
        return self.out[coll][rec["id"]]

    def node(self, nid):
        return self.out["nodes"].get(nid) or self.graph["nodes"].get(nid)

    def merged_graph(self):
        g = {}
        for c in dbstore.GRAPH:
            d = dict(self.graph.get(c) or {})
            for k, v in self.out[c].items():
                d[k] = merge_record(d.get(k), v) if d.get(k) else v
            g[c] = d
        return g

    # ------------------------------------------------------------------ identities
    def conn_entry(self, conn):
        return self.ctx.connections.get(conn)

    def _database(self, entry):
        dbs = entry.get("databases") or []
        return dbs[0] if len(dbs) == 1 else ""

    def _norm(self, entry, name, quoted):
        return dbmodel.norm_ident(entry.get("engine") or "", name, quoted, entry.get("case_sensitive"))

    def schema_ok(self, entry, schema):
        schemas = entry.get("schemas")
        if not schemas:
            return False  # schema list unknown: nothing is placed on this connection by name
        return self._norm(entry, schema, False) in {self._norm(entry, s, False) for s in schemas}

    def existing_object(self, conn, schema, name, quoted):
        """A DB object already in the graph (any kind) for (connection, schema, name) under the engine's rules."""
        entry = self.conn_entry(conn)
        want = (conn, self._norm(entry, schema, quoted), self._norm(entry, name, quoted))
        for src in (self.out["nodes"], self.graph["nodes"]):
            for n in src.values():
                if n.get("kind") in dbmodel.DB_KINDS and n["id"][0] == "o":
                    i = n.get("identity") or {}
                    if (i.get("connection"), i.get("schema"), i.get("name")) == want:
                        return n
        return None

    def object_node(self, conn, schema, name, kind="table", quoted=True, placeholder=False, attrs=None):
        entry = self.conn_entry(conn)
        n = dbmodel.db_node(self.server, conn, entry["env"], self._database(entry), schema, kind, name,
                            entry.get("engine") or "", quoted=quoted, case_sensitive=entry.get("case_sensitive"),
                            attrs=dict(attrs or {}, **({"placeholder": True} if placeholder else {})))
        if placeholder and self.node(n["id"]) is not None:
            return self.node(n["id"])
        return self.add("nodes", n)

    def column_node(self, obj, name, quoted=True, placeholder=False, attrs=None):
        entry = self.conn_entry((obj.get("display") or {}).get("connection")) or {}
        n = dbmodel.member_node(obj, name, entry.get("engine") or "", quoted=quoted,
                                case_sensitive=entry.get("case_sensitive"),
                                attrs=dict(attrs or {}, **({"placeholder": True} if placeholder else {})))
        if placeholder and self.node(n["id"]) is not None:
            return self.node(n["id"])
        return self.add("nodes", n)

    def resolve_table(self, ref, kind="table"):
        """('resolved', node) | ('needs_resolution', [candidates]) | ('unresolved', reason) for a table name from a
        ticket or mapping. Only a schema held by exactly one approved connection resolves; a bare name never does."""
        ref = str(ref or "").strip().strip('`"[]')
        conn_hint = None
        if ":" in ref:
            conn_hint, ref = ref.split(":", 1)
        parts = [p.strip('`"[] ') for p in ref.split(".") if p.strip('`"[] ')]
        if not parts or not REF_RX.match(ref):
            return "unresolved", "not a table name"
        if len(parts) == 1:
            cands = self.candidates_by_name(parts[0])
            return "needs_resolution", cands
        db, schema, name = (parts[-3] if len(parts) >= 3 else None), parts[-2], parts[-1]
        conns = []
        for c, e in sorted(self.ctx.connections.items()):
            if conn_hint and c != conn_hint and self.ctx.aliases.get(conn_hint) != c:
                continue
            if not self.schema_ok(e, schema):
                continue
            if db and e.get("databases") and self._norm(e, db, False) not in {self._norm(e, d, False)
                                                                             for d in e["databases"]}:
                continue
            conns.append(c)
        if len(conns) == 1:
            hit = self.existing_object(conns[0], schema, name, False)
            if hit is not None:
                return "resolved", hit
            return "resolved", self.object_node(conns[0], schema, name, kind, quoted=False, placeholder=True)
        if len(conns) > 1:
            cands = []
            for c in conns:
                hit = self.existing_object(c, schema, name, False)
                cands.append(hit["id"] if hit else "connection:" + c)
            return "needs_resolution", cands
        return "unresolved", "no approved connection holds schema %s" % schema

    def lookup_table(self, ref):
        """An object already recorded for a qualified name (nothing is created): a proposal may only name these."""
        parts = [p for p in str(ref or "").strip().split(".") if p]
        if len(parts) < 2:
            return None
        for c, e in sorted(self.ctx.connections.items()):
            if self.schema_ok(e, parts[-2]):
                hit = self.existing_object(c, parts[-2], parts[-1], False)
                if hit is not None and not (hit.get("attrs") or {}).get("placeholder"):
                    return hit
        return None

    def candidates_by_name(self, name):
        out = []
        for src in (self.graph["nodes"], self.out["nodes"]):
            for n in src.values():
                if n.get("kind") in ("table", "view") and str((n.get("display") or {}).get("name") or "").casefold() \
                        == str(name).casefold() and n["id"] not in out:
                    out.append(n["id"])
        return sorted(out)[:20]

    # ------------------------------------------------------------------ code citations
    def _index(self, root):
        if root in self._file_index:
            return self._file_index[root]
        idx, n = {}, 0
        for dp, dns, fns in os.walk(root):
            dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith("."))
            for f in fns:
                n += 1
                if n > MAX_REPO_FILES:
                    break
                rel = os.path.relpath(os.path.join(dp, f), root).replace("\\", "/")
                idx.setdefault(f.lower(), []).append(rel)
            if n > MAX_REPO_FILES:
                break
        self._file_index[root] = idx
        return idx

    def _repos_for(self, hint):
        roots = dict(self.ctx.repo_roots)
        if not hint:
            return roots
        h = str(hint).strip().lower().rstrip("/\\")
        pick = {r: p for r, p in roots.items() if r.lower() == h or os.path.basename(p.rstrip("/\\")).lower() == h
                or r.lower().endswith("/" + h)}
        return pick or roots

    def cite(self, text, repo_hint=None, ticket="", day=None):
        """-> (evidence record, repo id or "", validated). Never guesses between two matching files. `day`: when the
        citation was recorded (its entry's date), so the record is the same on every run."""
        day = str(day or self.now[:10])[:10]
        m = CITE_RX.match(str(text or ""))
        why = "not a file:line citation"
        if m:
            rel = m.group("path").replace("\\", "/")
            rel = rel[2:] if rel.startswith("./") else rel
            a = int(m.group("a"))
            b = int(m.group("b") or a)
            roots = self._repos_for(repo_hint)
            hits = [(r, rel) for r, root in sorted(roots.items())
                    if not dbmodel._rel_path_reason(rel) and os.path.isfile(os.path.join(root, rel))]
            if not hits:
                base = rel.rsplit("/", 1)[-1].lower()
                for r, root in sorted(roots.items()):
                    for cand in self._index(root).get(base, []):
                        if cand == rel or cand.endswith("/" + rel) or "/" not in rel:
                            hits.append((r, cand))
            if len(hits) == 1:
                r, rel = hits[0]
                chk = dbmodel.cite_code(self.ctx.repo_roots[r], rel, a, b)
                if chk["exists"] and chk["in_range"]:
                    ev = dbmodel.make_evidence(
                        "source", {"repo": r, "revision": "worktree", "content_hash": chk["content_hash"], "path": rel,
                                   "line_start": a, "line_end": b},
                        scope={"repo": r}, time={"collected_at": day},
                        quality={"coverage": "complete", "freshness": "current", "validation": "validated"})
                    return ev, r, True
                why = chk["reason"] or "lines out of range"
            elif len(hits) > 1:
                why = "matches %d files in the selected repositories" % len(hits)
            else:
                why = "no such file in the selected repositories"
        ev = dbmodel.make_evidence("source", {"type": "session_record", "ticket": ticket, "text": str(text)[:300],
                                              "reason": why},
                                   time={"collected_at": day}, basis="inferred",
                                   quality={"coverage": "not_checked", "validation": "unvalidated"})
        return ev, "", False


# --------------------------------------------------------------------------- schema cards

def _ts(s):
    return common.parse_ts(s) if isinstance(s, str) and s else None


def source_state(src, card_at, now, has_data, max_age_days=7):
    """Normalised state of one card source: {state: ok | failed | unsupported | not_checked, coverage, freshness,
    last_success, last_attempt, error, rows, truncated, page}."""
    if not isinstance(src, dict):
        return {"state": "not_checked", "coverage": "not_checked", "freshness": "unknown", "last_success": "",
                "last_attempt": "", "error": "", "rows": None, "truncated": False, "page": ""}
    err = str(src.get("error") or "")
    at = str(src.get("fetched_at") or "")
    out = {"last_attempt": str(card_at or at or ""), "error": err[:200], "rows": src.get("rows"),
           "truncated": bool(src.get("truncated")), "page": str(src.get("page") or "")}
    if err.startswith("not supported"):
        out.update(state="unsupported", coverage="unavailable", freshness="unknown", last_success="")
    elif err:
        out.update(state="failed", coverage="partial" if has_data else "unavailable", freshness="stale",
                   last_success=at if has_data else "")
    else:
        t, n = _ts(at), _ts(now)
        fresh = "current" if t and n and (n - t).total_seconds() <= max_age_days * 86400 else "stale" if t else "unknown"
        partial = out["truncated"] or out["page"] == "partial"
        out.update(state="ok", coverage="partial" if partial else "complete", freshness=fresh, last_success=at)
    return out


def ingest_card(run, conn, rec):
    """One schema card (mcpjobs.card_record's format) -> nodes, members, edges, evidence, claims (module doc).
    Returns a skip reason or ""."""
    entry = run.conn_entry(conn)
    if entry is None:
        return "connection %s is not approved" % conn
    table = str(rec.get("table") or "")
    parts = [p for p in table.split(".") if p]
    if len(parts) < 2:
        return "card without a schema (%s)" % table
    schema, name = parts[-2], parts[-1]
    if not run.schema_ok(entry, schema):
        return "schema outside the approved scope"
    env = entry["env"]
    card = rec.get("card") if isinstance(rec.get("card"), dict) else {}
    srcs = rec.get("sources") if isinstance(rec.get("sources"), dict) else {}
    rows = [r for r in rec.get("describe") or [] if isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"]] \
        if isinstance(rec.get("describe"), list) else []
    at = str(rec.get("fetched_at") or "")
    states = {"describe": source_state(srcs.get("describe"), at, run.now, bool(rows)),
              "triggers": source_state(srcs.get("triggers"), at, run.now, bool(card.get("triggers"))),
              "writers": source_state(srcs.get("writers"), at, run.now,
                                      bool(card.get("writers") or card.get("references")))}
    hidden = [w for w in card.get("references") or [] if isinstance(w, dict) and w.get("definition") == "unavailable"]
    if hidden and states["writers"]["state"] == "ok":
        states["writers"]["coverage"] = "partial"
    obj = run.object_node(conn, schema, name, "table", quoted=True, attrs={
        "described": bool(rows), "found": bool(card.get("found", bool(rows))), "card": "%s/%s" % (conn, table),
        "fetched_at": at, "sources": states, "engine_label": rec.get("type") or ""})
    if rec.get("missing_since"):  # a complete listing no longer shows it: kept as history, out of current lookups
        obj = run.add("nodes", dict(obj, lifecycle="historical",
                                    attrs=dict(obj.get("attrs") or {}, missing_since=str(rec["missing_since"]))))
    oid = obj["id"]
    scope = {"env": env, "connection": conn}
    cols, fks = [], {}
    for i, r in enumerate(rows):
        if any(c["display"].get("member") == r["name"] for c in cols):
            continue  # some describes list a column once per constraint
        dflt = next((r.get(k) for k in ("default", "columnDefault", "column_default", "defaultValue") if k in r), None)
        c = run.column_node(obj, r["name"], quoted=True, attrs={
            "data_type": str(r.get("dataType") or r.get("data_type") or r.get("type") or ""),
            "nullable": r.get("nullable") if isinstance(r.get("nullable"), bool) else None,
            "default": None if dflt is None else str(dflt)[:200], "primary_key": bool(r.get("isPrimaryKey")),
            "foreign_key": bool(r.get("isForeignKey")), "references": str(r.get("references") or "")[:300],
            "sensitive": bool(r.get("isPhiField")), "position": i + 1})
        cols.append(c)
        ref = r.get("references")
        if isinstance(ref, str) and ref.strip():
            rp = [p for p in ref.strip().split(".") if p]
            if len(rp) == 4 and run._database(entry) and \
                    run._norm(entry, rp[0], True) != run._norm(entry, run._database(entry), True):
                rp = []  # another database: not followed
            rp = rp[-3:] if len(rp) >= 3 else []
            if rp and run.schema_ok(entry, rp[0]):
                fks.setdefault((rp[0], rp[1]), []).append((c, rp[2]))
            else:
                fks.setdefault(None, []).append((c, ""))
    edges, claims = [], []
    for target, pairs in sorted(fks.items(), key=lambda x: str(x[0])):
        if target is None:
            continue
        tobj = run.existing_object(conn, target[0], target[1], True) or \
            run.object_node(conn, target[0], target[1], "table", quoted=True, placeholder=True)
        tcols = [run.column_node(tobj, tc, quoted=True, placeholder=True) for _, tc in pairs]
        edges.append(dbmodel.make_edge("foreign_key", oid, tobj["id"], column_mapping=[
            {"from": [c["id"] for c, _ in pairs], "to": [t["id"] for t in tcols], "transform": "",
             "transform_known": True}], transport="unknown", applicability=scope, basis="declared_metadata"))
    outside = len(fks.get(None) or [])
    if rows:
        ev = dbmodel.make_evidence(
            "catalog", {"source": "describe_table", "connection": conn, "object": table}, scope=scope,
            time={"source_time": states["describe"]["last_success"], "collected_at": at or run.now},
            quality={"coverage": states["describe"]["coverage"], "freshness": states["describe"]["freshness"],
                     "as_of": states["describe"]["last_success"], "validation": "validated",
                     "unsupported": (["%d reference(s) outside the approved scope or unqualified" % outside]
                                     if outside else [])},
            subjects=[oid] + [c["id"] for c in cols] + [e["id"] for e in edges])
        run.add("evidence", ev)
        for e in edges:
            e["evidence"] = [ev["id"]]
            run.add("edges", e)
    # triggers and writer routines: catalog evidence of their own source
    tsub, tedges = [], []
    for tg in card.get("triggers") or []:
        if not isinstance(tg, dict) or not isinstance(tg.get("name"), str) or not tg["name"]:
            continue
        life = "historical" if tg.get("missing_since") else "current"
        tn = run.object_node(conn, schema, tg["name"], "trigger", quoted=True, attrs={
            "event": str(tg.get("event") or "")[:120], "missing_since": tg.get("missing_since") or "",
            "definition": "available"})
        tsub.append(tn["id"])
        e = dbmodel.make_edge("triggers", oid, tn["id"], transport="trigger", applicability=scope,
                              basis="declared_metadata", lifecycle=life)
        tedges.append(e)
        sets = [run.column_node(obj, cn, quoted=True, placeholder=True)["id"] for cn in tg.get("sets_columns") or []
                if isinstance(cn, str) and cn]
        if sets:
            w = dbmodel.make_edge("writes", tn["id"], oid, transport="trigger", applicability=scope,
                                  basis="declared_metadata", lifecycle=life)
            w["ops"], w["columns"] = [], {"written": sets, "read": []}
            tedges.append(w)
    if tedges:
        ev = dbmodel.make_evidence(
            "catalog", {"source": "triggers", "connection": conn, "object": table}, scope=scope,
            time={"source_time": states["triggers"]["last_success"], "collected_at": at or run.now},
            quality={"coverage": states["triggers"]["coverage"], "freshness": states["triggers"]["freshness"],
                     "as_of": states["triggers"]["last_success"], "validation": "validated"},
            subjects=[oid] + tsub + [e["id"] for e in tedges])
        run.add("evidence", ev)
        for e in tedges:
            e["evidence"] = [ev["id"]]
            run.add("edges", e)
    wsub, wedges = [], []
    for w in list(card.get("writers") or []) + list(card.get("references") or []):
        if not isinstance(w, dict) or not isinstance(w.get("name"), str) or "." not in w["name"]:
            continue
        rs, rn = w["name"].rsplit(".", 1)
        if not run.schema_ok(entry, rs):
            continue
        kind = DB_KIND_OF.get(str(w.get("kind") or "").strip().lower(), "procedure")
        unreadable = w.get("definition") == "unavailable"
        life = "historical" if w.get("missing_since") else "current"
        rn_node = run.object_node(conn, rs, rn, kind, quoted=True, attrs={
            "catalog_kind": str(w.get("kind") or "")[:60], "missing_since": w.get("missing_since") or "",
            "definition": "unavailable" if unreadable else "available"})
        wsub.append(rn_node["id"])
        if unreadable:
            claims.append(dbmodel.make_claim(oid, "routine_unreadable", rn_node["id"], basis="declared_metadata",
                                             scope=scope, lifecycle=life))
            continue
        ops = [o for o in w.get("ops") or [] if o in WRITE_OPS]
        e = dbmodel.make_edge("writes" if ops else "reads", rn_node["id"], oid, transport="direct_sql",
                              applicability=scope, basis="declared_metadata" if ops else "inferred", lifecycle=life)
        if not ops:
            e["basis_cap"] = "inferred"  # it names the table; that it reads it is our reading
        if ops:
            e["ops"] = ops
            e["columns"] = {"written": [run.column_node(obj, cn, quoted=True, placeholder=True)["id"]
                                        for cn in w.get("writes") or [] if isinstance(cn, str) and cn], "read": []}
        wedges.append(e)
    if wedges or claims:
        ev = dbmodel.make_evidence(
            "catalog", {"source": "writers", "connection": conn, "object": table}, scope=scope,
            time={"source_time": states["writers"]["last_success"], "collected_at": at or run.now},
            quality={"coverage": states["writers"]["coverage"], "freshness": states["writers"]["freshness"],
                     "as_of": states["writers"]["last_success"], "validation": "validated",
                     "unsupported": ["definition of %s unavailable" % w["name"] for w in hidden]},
            subjects=[oid] + wsub + [e["id"] for e in wedges])
        run.add("evidence", ev)
        for e in wedges:
            e["evidence"] = [ev["id"]]
            run.add("edges", e)
        for c in claims:
            c["evidence"] = [ev["id"]]
            run.add("claims", c)
    return ""


# --------------------------------------------------------------------------- tickets

def _split_component(comp):
    m = COMPONENT_CITE_RX.match(comp or "")
    if m and m.group("name").strip():
        return m.group("name").strip(), m.group("cite").strip()
    return (comp or "").strip(), ""


class TicketIngest:
    def __init__(self, run, key, tk):
        self.run, self.key, self.tk = run, key, tk
        raw = [e for e in (tk.get("data_flow") or {}).values() if isinstance(e, dict)]
        self.raw = raw
        self.known_tables = {str(e.get("table") or "").strip().lower() for e in raw if e.get("table")}
        self.known_events = {str(e.get("event") or "") for e in raw if e.get("event")}
        self.comp_repo = {}  # component -> the repo its own entries cite (a proposal reuses it)

    def ctx(self, rec):
        rec["context"] = {"tickets": [self.key]}
        return rec

    def evidence_for(self, e, extra=(), proposal=False):
        """[(evidence, repo, validated)] for an entry's citations; for a proposal: the citations that check
        (others rejected) and one model_proposal record."""
        comp, ccite = _split_component(str(e.get("component") or ""))
        cites = [str(x) for x in list(e.get("evidence") or []) + ([ccite] if ccite else []) + list(extra) if x]
        out = []
        for c in dict.fromkeys(cites):
            ev, repo, ok = self.run.cite(c, e.get("repo"), self.key, e.get("date"))
            if proposal and not ok:
                if CITE_RX.match(c):
                    return None  # an invented citation: the proposal is rejected
                continue
            out.append((ev, repo, ok))
        if proposal:
            ev = dbmodel.make_evidence("model_proposal", {"type": "consolidation", "ticket": self.key,
                                                          "citations": sorted(x[0]["provenance"].get("path", "") + ":%s" %
                                                                              x[0]["provenance"].get("line_start", "")
                                                                              for x in out if x[2])},
                                       time={"collected_at": self.run.now[:10]},
                                       quality={"coverage": "not_checked", "validation": "validated"})
            repo = next((x[1] for x in out if x[2]), "")
            return [(ev, repo, False)]
        return out

    def entry(self, e, proposal=False):
        run = self.run
        comp, _ = _split_component(str(e.get("component") or ""))
        if not comp:
            return
        table, event = str(e.get("table") or "").strip(), str(e.get("event") or "").strip()
        if proposal:
            if table and table.lower() not in self.known_tables and run.lookup_table(table) is None:
                run.summary["rejected"] += 1
                return
            if event and event not in self.known_events:
                run.summary["rejected"] += 1
                return
        evs = self.evidence_for(e, proposal=proposal)
        if evs is None:
            run.summary["rejected"] += 1
            return
        repo = next((r for _, r, ok in evs if ok), "") or next((r for _, r, _ok in evs if r), "")
        if proposal and not repo:
            repo = self.comp_repo.get(comp, "")
        elif not proposal and repo:
            self.comp_repo.setdefault(comp, repo)
        basis = "code_supported" if any(ok for _, _, ok in evs) else "inferred"
        op = str(e.get("operation") or "select").lower()
        anchors = {"repos": sorted({r for _, r, ok in evs if ok and r}), "connections": []}
        cnode = dbmodel.code_node(repo, "code_symbol", comp)
        subjects = [cnode["id"]]
        edges, claims = [], []
        app = {"repo": repo, "revision": str(e.get("revision") or "")[:80]}
        order = {"ticket": self.key, "scenario": e.get("scenario") or "main", "step": e.get("step")} \
            if isinstance(e.get("step"), int) else None
        extra = {"path": e.get("path") or "", "path_conditions": list(e.get("path_conditions") or []),
                 "transaction": str(e.get("transaction") or "")[:80], "via": str(e.get("via") or "")[:120]}

        def edge(kind, src, dst, transport, conns=(), cond=None, mapping=None, async_=False, ops=None, columns=None,
                 changes=None):
            a = dict(app)
            conns = sorted(set(c for c in conns if c))
            if len(conns) == 1:
                a["connection"] = conns[0]
                a["env"] = run.ctx.connections[conns[0]]["env"]
            elif conns:
                a["connections"] = conns
            ed = dbmodel.make_edge(kind, src, dst, column_mapping=mapping, condition=cond, transport=transport,
                                   applicability=a, basis=basis)
            ed.update({"async": bool(async_), "path": extra["path"], "transaction": extra["transaction"],
                       "via": extra["via"], "orders": [order] if order else []})
            if ops:
                ed["ops"] = ops
            if columns:
                ed["columns"] = columns
            if changes:
                ed["changes"] = changes
            edges.append(self.ctx(ed))
            return ed

        tnode = None
        if table and op not in ("publish", "consume"):
            state, val = run.resolve_table(table, "procedure" if op == "proc" else "table")
            if state == "resolved":
                tnode = val
                conn = (tnode.get("display") or {}).get("connection")
                anchors["connections"].append(conn)
                subjects.append(tnode["id"])
                cr = [run.column_node(tnode, c, quoted=False, placeholder=True)["id"]
                      for c in e.get("columns_read") or [] if isinstance(c, str) and c]
                cw, changes = [], []
                for c in e.get("columns_written") or []:
                    if isinstance(c, dict) and c.get("column"):
                        cid = run.column_node(tnode, c["column"], quoted=False, placeholder=True)["id"]
                        cw.append(cid)
                        if c.get("change") or c.get("condition"):
                            changes.append({"column": cid, "change": str(c.get("change") or "")[:200],
                                            "condition": str(c.get("condition") or "")[:200]})
                conds = list(dict.fromkeys([ch["condition"] for ch in changes if ch["condition"]] +
                                           extra["path_conditions"]))
                if op == "select":
                    edge("reads", cnode["id"], tnode["id"], "direct_sql", [conn], columns={"read": cr, "written": []})
                elif op == "proc":
                    edge("calls", cnode["id"], tnode["id"], "direct_sql", [conn])
                else:
                    edge("writes", cnode["id"], tnode["id"], "direct_sql", [conn], cond=conds or None,
                         ops=[op if op in WRITE_OPS else "write"], columns={"read": cr, "written": cw}, changes=changes)
            else:
                claims.append(dbmodel.make_claim(cnode["id"], "unresolved_table", {
                    "ref": table[:200], "operation": op, "state": state,
                    "candidates": val if isinstance(val, list) else [], "reason": val if isinstance(val, str) else ""},
                    basis="inferred", scope={"repo": repo}))
        enode = None
        if event:
            enode = dbmodel.code_node("", "event", event)
            subjects.append(enode["id"])
            trig = str(e.get("trigger") or "").strip()
            if op == "publish":
                edge("publishes", cnode["id"], enode["id"], "event", async_=e.get("async", True))
            elif op == "consume" or (tnode is not None and trig == event):
                edge("consumes", cnode["id"], enode["id"], "event", async_=e.get("async", True))
        trig = str(e.get("trigger") or "").strip()
        anode = None
        if trig and trig != event:
            anode = dbmodel.code_node(repo, "api_operation", trig)
            subjects.append(anode["id"])
            edge("calls", anode["id"], cnode["id"], "http")
        groups = {}  # one transforms edge per (source, destination): its column mappings are a list
        for m in e.get("mapping") or []:
            self.mapping(m, enode, groups, claims, cnode, anchors, subjects, repo)
        for (so, do), g in sorted(groups.items()):
            edge("transforms", so, do, "event" if g["event"] else "unknown", g["conns"], mapping=g["maps"],
                 async_=g["event"])
        if not edges and not claims:
            return
        # nodes, with the anchors that make them visible (approved connections, selected repos)
        conns = sorted(set(c for c in anchors["connections"] if c))
        for n in (cnode, anode, enode):
            if n is None:
                continue
            n = dict(n, applicability={"repos": anchors["repos"], "connections": conns})
            run.add("nodes", n)
        # evidence: about the entry's nodes and every edge it made
        eids = [ed["id"] for ed in edges]
        ev_ids = []
        for ev, r, ok in evs:
            if ok and r and r != repo:
                continue  # an edge carries evidence of its own repository only
            ev = dict(ev, subjects=sorted(set(subjects + eids)))
            if not ok:
                ev = self.ctx(ev)
            else:
                ev["context"] = {"tickets": [self.key]}
            ev_ids.append(run.add("evidence", ev)["id"])
        for ed in edges:
            ed["evidence"] = list(ev_ids)
            run.add("edges", ed)
        for c in claims:
            c = self.ctx(dict(c, evidence=list(ev_ids)))
            c["anchors"] = {"connections": conns, "repos": anchors["repos"]}
            run.add("claims", c)

    def ref(self, text, enode):
        """One mapping side -> ('column' | 'field', owner node, member ID) or ('unresolved', reason, None)."""
        run = self.run
        t = str(text or "").strip()
        if enode is not None:
            ename = (enode.get("display") or {}).get("name") or ""
            if t.startswith(ename + ".") and len(t) > len(ename) + 1:
                f = dbmodel.field_node(enode, t[len(ename) + 1:])
                run.add("nodes", f)
                return "field", enode, f["id"]
        ev_name = t.split(".", 1)[0]
        if ev_name in self.known_events and "." in t:
            en = dbmodel.code_node("", "event", ev_name)
            f = dbmodel.field_node(en, t.split(".", 1)[1])
            run.add("nodes", f)
            return "field", en, f["id"]
        parts = t.split(".")
        if len(parts) < 3:
            return "unresolved", "%s needs schema.table.column" % t[:80], None
        state, val = run.resolve_table(".".join(parts[:-1]))
        if state != "resolved":
            return "unresolved", "%s: %s" % (t[:80], "several approved connections hold it" if state ==
                                             "needs_resolution" else val), None
        col = run.column_node(val, parts[-1], quoted=False, placeholder=True)
        return "column", val, col["id"]

    def mapping(self, m, enode, groups, claims, cnode, anchors, subjects, repo=""):
        run = self.run
        src = [self.ref(x, enode) for x in m.get("from") or []]
        dst = [self.ref(x, enode) for x in m.get("to") or []]
        transform = str(m.get("transform") or "")
        bad = [r for r in src + dst if r[0] == "unresolved"]
        owners_s = {r[1]["id"] for r in src if r[0] != "unresolved"}
        owners_d = {r[1]["id"] for r in dst if r[0] != "unresolved"}
        if bad or len(owners_s) != 1 or len(owners_d) != 1:
            claims.append(dbmodel.make_claim(cnode["id"], "mapping_unresolved", {
                "from": m.get("from"), "to": m.get("to"), "transform": transform[:200],
                "reason": "; ".join(r[1] for r in bad)[:300] or "each side must name one object"}, basis="inferred",
                scope={"repo": repo}))
            return
        so, do = src[0][1], dst[0][1]
        envs = {(n.get("display") or {}).get("environment") for n in (so, do) if n["id"][0] == "o"}
        conns = [(n.get("display") or {}).get("connection") for n in (so, do) if n["id"][0] == "o"]
        if len(envs) > 1:
            claims.append(dbmodel.make_claim(so["id"], "cross_environment_mapping", {
                "to": do["id"], "from_columns": [r[2] for r in src], "to_columns": [r[2] for r in dst],
                "transform": transform[:200]}, basis="inferred", scope={"repo": repo}))
            return
        known = transform.strip().lower() not in UNKNOWN_TRANSFORMS
        is_event = any(n["id"][0] == "n" for n in (so, do))
        for n in (so, do):
            if n["id"] not in subjects:
                subjects.append(n["id"])
        anchors["connections"] += [c for c in conns if c]
        g = groups.setdefault((so["id"], do["id"]), {"event": is_event, "conns": [c for c in conns if c], "maps": []})
        g["maps"].append({"from": [r[2] for r in src], "to": [r[2] for r in dst], "transform": transform[:300],
                          "transform_known": known})

    def relation(self, r):
        run = self.run
        a, b = str(r.get("from") or ""), str(r.get("to") or "")
        sa, sb = self.ref(a, None), self.ref(b, None)
        evs = []
        if r.get("evidence"):  # a legacy relation: its note stays a session record, inferred (DB-24)
            evs = [(dbmodel.make_evidence("source", {"type": "legacy_relation", "ticket": self.key,
                                                     "text": str(r["evidence"])[:300]},
                                          time={"collected_at": str(r.get("date") or run.now)[:10]}, basis="inferred",
                                          quality={"coverage": "not_checked", "validation": "unvalidated"}), "", False)]
        if sa[0] != "column" or sb[0] != "column":
            return
        if (sa[1].get("display") or {}).get("environment") != (sb[1].get("display") or {}).get("environment"):
            return
        conns = sorted({(sa[1].get("display") or {}).get("connection"), (sb[1].get("display") or {}).get("connection")})
        app = {"connection": conns[0], "env": (sa[1].get("display") or {}).get("environment")} if len(conns) == 1 \
            else {"connections": conns}
        ed = dbmodel.make_edge("logical_association", sa[1]["id"], sb[1]["id"], column_mapping=[
            {"from": [sa[2]], "to": [sb[2]], "transform": "", "transform_known": True}], transport="unknown",
            applicability=app, basis="inferred")
        ed["claimed_kind"] = str(r.get("kind") or "logical")[:20]
        ids = []
        for ev, repo, ok in evs:
            ev = dict(ev, subjects=[ed["id"], sa[1]["id"], sb[1]["id"]])
            ev["context"] = {"tickets": [self.key]}
            ids.append(run.add("evidence", ev)["id"])
        ed["evidence"] = ids
        run.add("edges", self.ctx(ed))


# --------------------------------------------------------------------------- link map and confirmed mappings

def ingest_linkmap(run, lm):
    res = (lm or {}).get("result") if isinstance(lm, dict) else None
    at = str((lm or {}).get("fetched_at") or "")
    for ln in (res or {}).get("links") or [] if isinstance(res, dict) else []:
        if not isinstance(ln, dict):
            continue
        conn = ln.get("connection")
        try:
            s, m = int(ln.get("sampled") or 0), int(ln.get("matched") or 0)
        except (TypeError, ValueError):
            continue
        if not s or conn not in run.ctx.connections or ln.get("checked") is False:
            continue
        ends = []
        for side in ("from", "to"):
            parts = str(ln.get(side) or "").split(".")
            if len(parts) < 3:
                break
            st, obj = run.resolve_table("%s:%s.%s" % (conn, parts[-3], parts[-2]))
            if st != "resolved":
                break
            ends.append((obj, run.column_node(obj, parts[-1], quoted=False, placeholder=True)))
        if len(ends) != 2:
            continue
        env = run.ctx.connections[conn]["env"]
        scope = {"env": env, "connection": conn}
        ed = dbmodel.make_edge("logical_association", ends[0][0]["id"], ends[1][0]["id"], column_mapping=[
            {"from": [ends[0][1]["id"]], "to": [ends[1][1]["id"]], "transform": "", "transform_known": True}],
            transport="unknown", applicability=scope, basis="observed_sample")
        label = "%d of %d sampled values matched, %s, %s" % (m, s, env.upper(), at[:10] or "date unknown")
        ev = dbmodel.make_evidence(
            "observation", {"source": "link_map", "connection": conn, "from": str(ln.get("from")),
                            "to": str(ln.get("to")), "sampled": s, "matched": m, "fetched_at": at, "label": label},
            scope=scope, time={"source_time": at, "collected_at": at or run.now},
            quality={"coverage": "partial", "freshness": "current", "as_of": at, "validation": "validated"},
            subjects=[ed["id"], ends[0][0]["id"], ends[1][0]["id"]])
        run.add("evidence", ev)
        ed["evidence"] = [ev["id"]]
        run.add("edges", ed)
        run.summary["linkmap"] += 1


def mapping_records():
    m = dbstore.read_file("mappings").get("mappings")
    return m if isinstance(m, dict) else {}


def ingest_mappings(run):
    for mid, m in sorted(mapping_records().items()):
        if not isinstance(m, dict):
            continue
        ti = TicketIngest(run, "", {})
        via = str(m.get("via") or "")
        hops = [(m.get("from"), m.get("to"))]
        enode = None
        if via and "." in via:
            ename = via.split(".", 1)[0]
            ti.known_events.add(ename)
            enode = dbmodel.code_node("", "event", ename)
            hops = [(m.get("from"), [via]), ([via], m.get("to"))]
        for frm, to in hops:
            src = [ti.ref(x, enode) for x in frm or []]
            dst = [ti.ref(x, enode) for x in to or []]
            if any(r[0] == "unresolved" for r in src + dst) or not src or not dst:
                continue
            so, do = src[0][1], dst[0][1]
            if any(r[1]["id"] != so["id"] for r in src) or any(r[1]["id"] != do["id"] for r in dst):
                continue
            envs = {(n.get("display") or {}).get("environment") for n in (so, do) if n["id"][0] == "o"}
            conns = sorted({(n.get("display") or {}).get("connection") for n in (so, do) if n["id"][0] == "o"})
            ev = dbmodel.make_evidence("user_note", {"source": "mappings", "mapping": mid, "note": str(m.get("note") or "")[:300],
                                                     "at": str(m.get("at") or "")},
                                       time={"source_time": str(m.get("at") or ""), "collected_at": str(m.get("at") or run.now)},
                                       quality={"coverage": "not_checked", "validation": "validated"})
            transform = str(m.get("transform") or "")
            if len(envs) > 1:  # a cross-environment mapping is for comparison, never a flow edge
                c = dbmodel.make_claim(so["id"], "cross_environment_mapping", {
                    "to": do["id"], "from_columns": [r[2] for r in src], "to_columns": [r[2] for r in dst],
                    "transform": transform[:200]}, basis="user_confirmed")
                ev = dict(ev, subjects=[so["id"], do["id"]])
                c["evidence"] = [run.add("evidence", ev)["id"]]
                c["anchors"] = {"connections": conns, "repos": []}
                run.add("claims", c)
                continue
            app = {"connection": conns[0], "env": run.ctx.connections[conns[0]]["env"]} if len(conns) == 1 else \
                {"connections": conns}
            ed = dbmodel.make_edge("transforms", so["id"], do["id"], column_mapping=[
                {"from": [r[2] for r in src], "to": [r[2] for r in dst], "transform": transform[:300],
                 "transform_known": transform.strip().lower() not in UNKNOWN_TRANSFORMS}],
                transport="event" if enode is not None else "unknown", applicability=app, basis="user_confirmed")
            ed["async"] = enode is not None
            ev = dict(ev, subjects=[ed["id"], so["id"], do["id"]])
            ed["evidence"] = [run.add("evidence", ev)["id"]]
            run.add("edges", ed)
            if enode is not None:
                run.add("nodes", dict(enode, applicability={"repos": [], "connections": conns}))
        run.summary["mappings"] += 1


def confirm_mapping(cfg, frm, to, via="", transform="", note="", at=None):
    """Record a mapping you confirmed (validated: every side must resolve to one object now) in
    data/db/mappings.json; it becomes user_confirmed evidence on the next ingestion. Raises MappingError."""
    frm = [frm] if isinstance(frm, str) else list(frm or [])
    to = [to] if isinstance(to, str) else list(to or [])
    for x in frm + to + ([via] if via else []):
        if not isinstance(x, str) or not REF_RX.match(x.strip()) or len(x) > 200:
            raise MappingError("%r is not a column or event-field name" % (x,))
    if not frm or not to or len(frm) > 8 or len(to) > 8:
        raise MappingError("name 1 to 8 columns on each side")
    run = Run(cfg)
    ti = TicketIngest(run, "", {})
    enode = None
    if via:
        if "." not in via:
            raise MappingError("via names an event field, like OrderCancelled.orderRef")
        ti.known_events.add(via.split(".", 1)[0])
        enode = dbmodel.code_node("", "event", via.split(".", 1)[0])
    for x in frm + to:
        r = ti.ref(x.strip(), enode)
        if r[0] == "unresolved":
            raise MappingError(r[1])
    rec = {"from": [x.strip() for x in frm], "to": [x.strip() for x in to], "via": via.strip(),
           "transform": str(transform or "")[:300], "note": str(note or "")[:300], "at": at or common.now_iso(),
           "principal": run.ctx.principal_id or ""}
    mid = dbmodel.new_id("x", "mapping", rec["from"], rec["to"], rec["via"])
    with dbstore.lock():
        cur = dbstore.read_file("mappings")
        maps = cur.get("mappings") if isinstance(cur.get("mappings"), dict) else {}
        maps[mid] = rec
        dbstore.write_file("mappings", {"schema_version": dbstore.SCHEMA_VERSION, "mappings": maps})
    return dict(rec, id=mid)


# --------------------------------------------------------------------------- the run

def _cards():
    """[(source key, connection folder, path)] of every schema card, in a stable order."""
    root = common.hp("data", "db-schema")
    out = []
    try:
        conns = sorted(os.listdir(root))
    except OSError:
        return out
    for c in conns:
        d = os.path.join(root, c)
        if c.startswith("_") or not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".json") and not fn.startswith("_"):
                out.append(("card:%s/%s" % (c, fn[:-5]), c, os.path.join(d, fn)))
    return out


def _tickets():
    d = common.hp("data", "tickets")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    return [(fn[:-5], os.path.join(d, fn)) for fn in names if fn.endswith(".json") and common.ticket_key(fn[:-5])]


def run(cfg=None, force=False, max_cards=MAX_CARDS, max_tickets=MAX_TICKETS, only=None, now=None):
    """One bounded ingestion pass (module doc). `only`: source keys to take (others untouched). Returns a summary:
    {cards, tickets, linkmap, mappings, unchanged, rejected, skipped: [(source, why)], changed: {collection: n},
    sources: [keys ingested]}."""
    from . import dbviews, mcpjobs
    cfg = common.load_config() if cfg is None else cfg
    r = Run(cfg, now=now)
    fps = dbstore.read_file("sources").get("sources")
    fps = fps if isinstance(fps, dict) else {}
    gen = r.ctx.policy_generation
    new_fps = {}

    def due(key, payload):
        if only is not None and key not in only:
            return None
        fp = dbstore.source_fingerprint(key, {"payload": payload, "policy": gen})
        if not force and (fps.get(key) or {}).get("fingerprint") == fp:
            r.summary["unchanged"] += 1
            return None
        return fp

    for key, conn_dir, p in _cards():
        if r.summary["cards"] >= max_cards:
            break
        rec = common.read_json(p, None)
        if not isinstance(rec, dict):
            continue
        fp = due(key, rec)
        if fp is None:
            continue
        conn = str(rec.get("connection") or conn_dir)
        r.source = key
        why = "card folder and connection disagree" if mcpjobs._safe(conn) != conn_dir else ingest_card(r, conn, rec)
        if why:
            r.summary["skipped"].append((key, why))
        else:
            r.summary["cards"] += 1
        new_fps[key] = {"fingerprint": fp, "at": r.now, "ok": not why, "why": why}
    for key, p in _tickets():
        if r.summary["tickets"] >= max_tickets:
            break
        tk = common.read_json(p, None)
        if not isinstance(tk, dict):
            continue
        payload = {"data_flow": tk.get("data_flow") or {}, "table_relations": tk.get("table_relations") or {},
                   "view": (tk.get("dbflow_view") or {}).get("data_flow") or []}
        fp = due("ticket:" + key, payload)
        if fp is None:
            continue
        r.source = "ticket:" + key
        ti = TicketIngest(r, key, tk)
        for e in ti.raw:
            ti.entry(e)
        for rel in (tk.get("table_relations") or {}).values():
            if isinstance(rel, dict):
                ti.relation(rel)
        for e in payload["view"]:
            if isinstance(e, dict):
                ti.entry(e, proposal=True)
        r.summary["tickets"] += 1
        new_fps["ticket:" + key] = {"fingerprint": fp, "at": r.now, "ok": True, "why": ""}
    lm = common.read_json(common.hp("data", "db-schema", "_linkmap.json"), None)
    if isinstance(lm, dict):
        fp = due("linkmap", lm)
        if fp is not None:
            r.source = "linkmap"
            ingest_linkmap(r, lm)
            new_fps["linkmap"] = {"fingerprint": fp, "at": r.now, "ok": True, "why": ""}
    maps = mapping_records()
    fp = due("mappings", maps) if maps else None
    if fp is not None:
        r.source = "mappings"
        ingest_mappings(r)
        new_fps["mappings"] = {"fingerprint": fp, "at": r.now, "ok": True, "why": ""}
    r.summary["changed"] = write(r)
    r.summary["sources"] = sorted(new_fps)
    if new_fps:
        with dbstore.lock():
            cur = dbstore.read_file("sources")
            srcs = cur.get("sources") if isinstance(cur.get("sources"), dict) else {}
            srcs.update(new_fps)
            dbstore.write_file("sources", {"schema_version": dbstore.SCHEMA_VERSION, "sources": srcs})
        dbviews.ensure_secret()
    return r.summary


def write(r):
    """Validate the run's edges and claims against the merged graph, drop rejected ones, write everything."""
    g = r.merged_graph()
    changed = {}
    for n in list(r.out["nodes"].values()):  # a field is as visible as its event (or API operation)
        if n.get("kind") == "field" and n.get("parent") in g["nodes"]:
            n["applicability"] = dict(g["nodes"][n["parent"]].get("applicability") or {})
            g["nodes"][n["id"]] = merge_record(g["nodes"].get(n["id"]), n)
    edges, claims = [], []
    for e in r.out["edges"].values():
        e = g["edges"][e["id"]]
        evs = [g["evidence"][v] for v in e.get("evidence") or [] if v in g["evidence"]]
        best = dbmodel.strongest_basis(evs, structural=e.get("kind") == "foreign_key" or e.get("cardinality") is not None)
        best = best or "inferred"
        cap = e.get("basis_cap")
        if cap in dbmodel.BASIS_RANK and dbmodel.BASIS_RANK[cap] < dbmodel.BASIS_RANK[best]:
            best = cap
        e = dict(e, basis=best)
        res = dbmodel.validate_edge(e, g, r.repo_check)
        if res["state"] == "rejected":
            r.summary["rejected"] += 1
            continue
        edges.append(dbmodel.apply_validation(e, res))
    for c in r.out["claims"].values():
        c = g["claims"][c["id"]]
        res = dbmodel.validate_claim(c, g, r.repo_check)
        if res["state"] == "rejected":
            r.summary["rejected"] += 1
            continue
        claims.append(dbmodel.apply_validation(c, res))
    for coll, recs in (("nodes", list(r.out["nodes"].values())), ("evidence", list(r.out["evidence"].values())),
                       ("edges", edges), ("claims", claims)):
        if recs:
            changed[coll] = dbstore.upsert(coll, recs, merge=merge_record)
    return changed


def tick(cfg, state=None, max_flows=10):
    """The background step: one bounded ingestion, then the saved flows of the tickets it changed are rebuilt
    (deterministic, no model, no query), then one bounded drift reconciliation (dbimpact.tick: review marks, cached
    results of revoked connections removed). Fail-open: the agent logs any error and goes on. Without an approved
    connection there is nothing to place on a database, so the step does nothing (the explicit `cli db ingest` still
    runs)."""
    from . import dbflows
    if not dbaccess.approved_scope(cfg)["connections"]:
        return None
    s = run(cfg)
    keys = [k.split(":", 1)[1] for k in s["sources"] if k.startswith("ticket:")][:max_flows]
    if keys:
        ctx = dbaccess.AccessContext(cfg)
        for k in keys:
            try:
                dbflows.build_flow(ctx, {"kind": "ticket", "key": k}, save=True, cfg=cfg)
            except dbflows.FlowError:
                continue
    if isinstance(state, dict):
        state["db_ingest"] = {"at": common.now_iso(), "cards": s["cards"], "tickets": s["tickets"],
                              "rejected": s["rejected"], "flows": len(keys)}
    try:  # drift: one bounded reconciliation pass (no query, no model); fail-open like the step itself
        from . import dbimpact
        dbimpact.tick(cfg, state)
    except Exception as e:  # noqa: BLE001 - logged; ingestion's result stands
        common.log("agent", "db reconcile failed: %s: %s" % (e.__class__.__name__, str(e)[:200]))
    return s


# --------------------------------------------------------------------------- the compatible migration (DB-24)

MIGRATE_FLOWS = 500


def _legacy_names():
    """{lower-cased legacy table name: [ticket keys]} from every ticket's data_flow (as stored: never rewritten)."""
    out = {}
    for key, p in _tickets():
        tk = common.read_json(p, None)
        if not isinstance(tk, dict):
            continue
        for e in (tk.get("data_flow") or {}).values():
            if isinstance(e, dict) and isinstance(e.get("table"), str) and e["table"].strip():
                out.setdefault(e["table"].strip().lower(), []).append(key)
    return {k: sorted(set(v)) for k, v in out.items()}


def migrate_legacy(dry_run, notes, backup):
    """KB step "db1-canonical-graph": the existing worklog-dbflow data (ticket data_flow / table_relations, schema
    cards, the link map) into the canonical graph through the ordinary ingestion, forward-only. Backs data/db/ up
    before the first write; a dry run only reports. Ticket JSON, blocks, rendered db-flow.md, schema cards and
    /cg:trace stay byte-identical (only data/db/ is written). A legacy name that several approved connections hold
    stays needs_resolution with its candidates; a legacy relation stays inferred; a legacy name becomes an explicit
    alias (data/db/aliases.json) only when exactly one approved, visible object answers to it. Saved flows are built
    for the tickets with a DB flow (one per ticket and environment: the same ID, a new version only on change) and one
    reconciliation pass updates review marks, the search index and the cached results. Re-running adds nothing."""
    from . import dbflows, dbimpact
    cfg = common.load_config()
    if not dbaccess.approved_scope(cfg)["connections"]:
        notes.append("no approved database connection: nothing to place yet (the tick ingests once one is approved)")
        return
    names = _legacy_names()
    r = Run(cfg)
    states = {n: r.resolve_table(n) for n in sorted(names)}
    amb = sorted(n for n, (st, _v) in states.items() if st == "needs_resolution")
    tickets = sorted({k for ks in names.values() for k in ks})
    notes.append("%d ticket(s) with a DB flow, %d legacy table name(s), %d schema card(s)" % (
        len(tickets), len(names), len(_cards())))
    if amb:
        notes.append("left needs_resolution (several approved connections or objects answer to them): %s" %
                     ", ".join(amb[:20]))
    if dry_run:
        return
    backup([common.hp("data", "db")])
    s = run(cfg, max_cards=100000, max_tickets=100000)
    notes.append("ingested %d card(s), %d ticket(s); %d unchanged, %d rejected" % (
        s["cards"], s["tickets"], s["unchanged"], s["rejected"]))
    ctx = dbaccess.AccessContext(cfg)
    vis = dbflows.Visible(ctx, cfg=cfg)
    with dbstore.lock():
        cur = dbstore.read_file("aliases")
        al = cur.get("aliases") if isinstance(cur.get("aliases"), dict) else {}
        added = 0
        for n, (st, v) in states.items():
            oid = v.get("id") if st == "resolved" and isinstance(v, dict) else None
            if oid and oid in vis.nodes and n not in al and n != vis.label(oid, False).lower():
                al[n] = {"object": oid, "connection": vis.conn_of(oid) or "", "source": "db1-canonical-graph",
                         "at": common.now_iso()}
                added += 1
        if added:
            dbstore.write_file("aliases", {"schema_version": dbstore.SCHEMA_VERSION, "aliases": al})
    built = 0
    for k in tickets[:MIGRATE_FLOWS]:
        try:
            dbflows.build_flow(ctx, {"kind": "ticket", "key": k}, save=True, cfg=cfg)
            built += 1
        except dbflows.FlowError:
            continue
    rc = dbimpact.reconcile(ctx, cfg)
    try:
        from . import searchidx
        searchidx.update()
    except Exception:  # noqa: BLE001 - the index is a rebuildable cache
        pass
    notes.append("%d alias(es) recorded, %d flow(s) saved or unchanged, reconciliation %s" % (added, built, rc["state"]))
