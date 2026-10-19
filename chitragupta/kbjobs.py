"""Knowledge-base jobs that read through the read-only MCP harness, plus their rendering:
Jira ticket cards, database-MCP schema cards, the QA trace (worked example) and the link map; nightly local backup.

All of it stays local (data/jira, data/db-schema, data/qa-trace, tickets/*/db-flow.md, knowledge/db-map.md).
"""
import datetime as _dt
import json
import os
import re
import traceback

from . import common
from . import configstore
from . import envmap
from . import mcpjobs
from .redact import redact
from .safety import paths

OFFLINE_BACKOFF_S = 2 * 3600  # after a skipped job, that role's jobs wait this long
OFFLINE_PROBE_S = 2 * 3600  # while a server is known offline, at most one probe job this often
EVENING_RESERVE = 2  # budget kept for the 20:00 Jira call (and one retry)


class KBJobs:
    def __init__(self, agent):
        self.a = agent
        self.cfg = agent.cfg

    @property
    def h(self):
        return self.a.health.setdefault("mcp", {})

    def _offline(self, key):
        t = common.parse_ts((self.h.get("skip") or {}).get(key))
        return bool(t and (common.now() - t).total_seconds() < OFFLINE_BACKOFF_S)

    def _down(self, role):
        """The role's server is known offline: a job found it unreachable or signed out (health mcp.offline), or the
        last `claude mcp list` check did, after the last job that reached it (mcp.last_ok)."""
        srv = mcpjobs.server_name(self.cfg, role)
        if not srv:
            return False
        if mcpjobs.offline_since(self.a.health, srv):
            return True
        from . import setup
        st = setup.cached_mcp_status() or {}
        row = st.get(role) if isinstance(st.get(role), dict) else {}
        if row.get("server") != srv or row.get("status") not in setup.UNREACHABLE + ("signed out",):
            return False
        checked = common.parse_ts(row.get("error_at") or st.get("checked"))
        ok = common.parse_ts((self.h.get("last_ok") or {}).get(srv))
        return bool(checked and (not ok or checked > ok))

    def _may_run(self, role):
        """A job of this role may start now: not backing off after a skip, and the server isn't known offline, or a
        probe is due (one at most every OFFLINE_PROBE_S while it is)."""
        if self._offline(role):
            return False
        if not self._down(role):
            return True
        t = common.parse_ts((self.h.get("probe") or {}).get(role))
        return not (t and (common.now() - t).total_seconds() < OFFLINE_PROBE_S)

    def _job(self, role, fn, *args, **kw):
        """Run one job function when _may_run allows it. A job that tried (did work or recorded a skip) while the
        server was known offline was that interval's probe."""
        if not self._may_run(role):
            return 0
        down, before = self._down(role), (self.h.get("skip") or {}).get(role)
        r = fn(*args, **kw)
        if down and (r or (self.h.get("skip") or {}).get(role) != before):
            self.h.setdefault("probe", {})[role] = common.now_iso()
        return r

    def _skipped(self, key, ex):
        self.h.setdefault("skip", {})[key] = common.now_iso()
        self.h.setdefault("last_skip_reason", {})[key] = str(ex)[:200]
        common.log("mcp", "%s skipped: %s" % (key, ex))

    def tick(self):
        today = common.workday(common.now(), self.cfg)
        st = self.a.state.setdefault("kbjobs", {})
        if st.get("files_day") != today:
            if mcpjobs.configured(self.cfg):  # guard files only when an MCP job can run (mcpjobs.run makes them too)
                mcpjobs.ensure_files(self.cfg)
            envmap.build(self.cfg)
            self.qa_values()
            st["files_day"] = today
        self.backup(today, st)  # local, no MCP: independent of NO_MCP_JOBS
        if common.env("NO_MCP_JOBS"):
            return
        def left():  # re-read after every job: only runs that made an MCP call spend a unit
            return mcpjobs.budget_left(self.a.health, self.cfg, today)
        if mcpjobs.enabled(self.cfg, "jira") and configstore.on(self.cfg, "mcp.jira.cards") and left() > EVENING_RESERVE:
            self._job("jira", lambda: self.jira_cards(today, limit=min(2, left() - EVENING_RESERVE)))
        if mcpjobs.enabled(self.cfg, "database") and mcpjobs.server_name(self.cfg, "database") \
                and mcpjobs.database_conn(self.cfg):  # each job re-checks: one failure stops the rest of the tick
            if left() > EVENING_RESERVE:
                self._job("database", self.schema_cards, today)
            if left() > EVENING_RESERVE and configstore.on(self.cfg, "mcp.database.trace"):
                self._job("database", self.qa_traces, today)
            week_ago = (_dt.date.fromisoformat(today) - _dt.timedelta(days=7)).isoformat()
            if left() > EVENING_RESERVE and configstore.on(self.cfg, "mcp.database.linkmap") \
                    and st.get("linkmap_day", "") < week_ago:
                if self._job("database", self.link_map, today):
                    st["linkmap_day"] = today

    # ------------------------------------------------------------------ Jira cards
    def jira_cards(self, today, limit=2, force=None):
        dd = self.a.store.day(today)
        want = force or sorted(dd.get("tickets") or {})
        n = 0
        for t in want:
            if n >= limit:
                break
            rec = common.read_json(common.hp("data", "jira", t + ".json"), {}) or {}
            if not force and rec.get("fetched_at", "")[:10] >= today:
                continue
            try:
                rec = mcpjobs.jira_card(t, self.cfg, self.a.health)
                n += 1
            except mcpjobs.JobError as ex:
                self._skipped("jira", ex)
                break
            self.apply_card(t, rec)
        return n

    def apply_card(self, t, rec):
        card = rec.get("card") or {}
        tk = self.a.store.ticket(t)
        if rec.get("raw") and card.get("found"):
            if card.get("title") and tk.get("title_source") not in ("user",):
                tk["title"], tk["title_source"] = card["title"], "jira"
            if card.get("summary") and (not tk.get("problem") or tk.get("problem_source") == "jira"):
                tk["problem"], tk["problem_source"] = card["summary"], "jira"
            tk["jira"] = {k: card.get(k) for k in ("type", "status", "priority", "sprint", "parent")}
            tk["jira"]["fetched"] = rec.get("fetched_at")
            tk["status"] = card.get("status") or tk.get("status")
            tk.pop("jira_missing", None)
        elif card.get("found") is False and self.h.get("jira_auth_ok"):
            tk["jira_missing"] = rec.get("fetched_at")  # a look-alike key: reading works, but Jira has no such issue
        self.a.store.touch_ticket(t)

    # ------------------------------------------------------------------ database-MCP schema cards
    def flow_tables(self, tk):
        """Schema-qualified tables of the ticket's DB flow (consolidated view first), catalogs excluded."""
        view = ((tk.get("dbflow_view") or {}).get("data_flow")) or []
        tables = {e.get("table") for e in view if isinstance(e, dict) and e.get("table")}
        if tables:
            return {t for t in tables if _real_table(t)}
        for e in (tk.get("data_flow") or {}).values():
            if e.get("table"):
                tables.add(e["table"])
        for q in (tk.get("queries") or {}).values():
            for x in q.get("tables") or []:
                tables.add(x)
        return {t for t in tables if _real_table(t)}

    def cached(self, table):
        conn = mcpjobs.database_conn(self.cfg, "qa", table)
        rec = common.read_json(common.hp("data", "db-schema", mcpjobs._safe(conn), mcpjobs._safe(table) + ".json"), {}) or {}
        return rec if rec.get("fetched_at", "") >= (common.now() - _dt.timedelta(days=7)).isoformat() else None

    def schema_cards(self, today, tables=None):
        if tables is None:
            want = []
            for t in sorted(self.a.store.day(today).get("tickets") or {}):
                for tb in sorted(self.flow_tables(self.a.store.ticket(t))):
                    if tb not in want and not self.cached(tb):
                        want.append(tb)
            tables = want
        if not tables:
            return 0
        try:
            mcpjobs.schema_cards(tables[:10], self.cfg, self.a.health)
        except mcpjobs.JobError as ex:
            self._skipped("database", ex)
            return 0
        for t in self.a.store.day(today).get("tickets") or {}:
            self.a.store.touch_ticket(t)
        return 1

    # ------------------------------------------------------------------ QA trace
    def start_ids(self, t):
        """Real ids used in the ticket's QA requests and queries (local files keep QA values literally)."""
        txt = []
        for rel in ("requests.md", "queries.md"):
            txt.append(common.read_text(common.hp("tickets", t, rel))[:6000])
        bdir = common.hp("bruno", t)
        for fn in sorted(os.listdir(bdir)) if os.path.isdir(bdir) else []:
            if fn.endswith(".bru"):
                txt.append(common.read_text(os.path.join(bdir, fn))[:3000])
        blob = "\n".join(txt)
        ids = []
        for m in re.finditer(r"(?i)\b([a-z_]*id|email|username)\b[\"'\s:=]+\"?([\w.@+-]{3,64})", blob):
            v = m.group(2)
            if v.startswith("{{") or v.lower() in ("null", "true", "false") or "*" in v:
                continue
            # data values only: numbers, emails, UUIDs, codes with digits - not query anchors or column names
            if v.startswith("q-") or not (re.fullmatch(r"\d{3,}", v) or "@" in v or re.fullmatch(r"[0-9a-f-]{32,36}", v)
                                          or (re.search(r"\d", v) and re.search(r"[A-Za-z]", v) and "_" not in v
                                              and "." not in v)):
                continue
            pair = "%s=%s" % (m.group(1), v)
            if pair not in ids:
                ids.append(pair)
        for m in re.finditer(r"/(\d{3,12})(?=[/?\s\"']|$)", blob):
            pair = "path_id=%s" % m.group(1)
            if pair not in ids:
                ids.append(pair)
        return ids[:12]

    def qa_traces(self, today, tickets=None, force=False):
        st = self.a.state.setdefault("kbjobs", {}).setdefault("traced", {})
        n = 0
        cands = tickets or sorted(self.a.store.day(today).get("tickets") or {})
        for t in cands:
            tk = self.a.store.ticket(t)
            view = tk.get("dbflow_view") or {}
            sig = common.stable_id(json.dumps(view.get("tables") or sorted(self.flow_tables(tk)), sort_keys=True))
            if not force and (st.get(t, {}).get("day") == today or st.get(t, {}).get("sig") == sig):
                continue
            if not force and not self._idle(t):
                continue
            ids = self.start_ids(t)
            tables = self.flow_tables(tk)
            if not ids or not tables:
                continue
            flow = {tb: sorted({c.get("column") for e in (tk.get("data_flow") or {}).values() if e.get("table") == tb
                                for c in (e.get("columns_written") or []) + [{"column": x} for x in e.get("columns_read") or []]
                                if c.get("column")}) for tb in sorted(tables)}
            before = (common.read_json(common.hp("data", "qa-trace", t + ".before.json"), {}) or {}).get("values")
            try:
                mcpjobs.qa_trace(t, ids, flow, self.cfg, self.a.health, before=before)
            except mcpjobs.JobError as ex:
                self._skipped("database", ex)
                break
            st[t] = {"day": today, "sig": sig}
            self.a.store.touch_ticket(t)
            n += 1
            break  # one trace per tick
        return n

    def _idle(self, t, minutes=20):
        last = None
        for s in (self.a.state.get("sessions") or {}).values():
            if t in (s.get("tickets") or []) or t == s.get("ticket"):
                ts = common.parse_ts(s.get("last_event"))
                last = max(last, ts) if last and ts else (ts or last)
        return not last or (common.now() - last).total_seconds() > minutes * 60

    # ------------------------------------------------------------------ link map
    def link_map(self, today):
        cat = self.a.store.catalog()
        rels = list((cat.get("db_relations") or {}).values())
        cands = [(r["from"], r["to"], "code/queries") for r in rels if r.get("from") and r.get("to")]
        tables = set()
        for t in self.a.store.all_ticket_ids():
            tables |= self.flow_tables(self.a.store.ticket(t))
        cols = {}
        for p in self._schema_files():
            rec = common.read_json(p, {}) or {}
            tbl = rec.get("table") or ""
            for c in rec.get("describe") if isinstance(rec.get("describe"), list) else []:
                ref = c.get("references") if isinstance(c, dict) else None
                if ref and tbl.count(".") >= 1:
                    ref = ref if ref.count(".") >= 2 else "%s.%s" % (tbl.rsplit(".", 1)[0].rsplit(".", 1)[0], ref)
                    cands.insert(0, ("%s.%s" % (tbl, c.get("name")), ref, "foreign key"))
            for c in _columns(rec.get("describe")):
                cols.setdefault(c.lower(), set()).add(rec.get("table"))
        for c, tbs in cols.items():
            if c.endswith("_id") and len(tbs) > 1:
                base = c[:-3]
                owner = [x for x in tbs if x.lower().split(".")[-1].rstrip("s") == base]
                for o in owner:
                    for x in tbs - {o}:
                        cands.append(("%s.%s" % (x, c), "%s.id" % o, "matching column name"))
        if not cands:
            return False
        try:
            mcpjobs.link_map(cands[:12], tables, self.cfg, self.a.health)
        except mcpjobs.JobError as ex:
            self._skipped("database", ex)
            return False
        return True

    def _schema_files(self):
        root = common.hp("data", "db-schema")
        for dp, _, fns in os.walk(root):
            for fn in fns:
                if fn.endswith(".json") and not fn.startswith("_"):
                    yield os.path.join(dp, fn)

    # ------------------------------------------------------------------ QA values for the public pass
    def qa_values(self):
        vals = set()
        for p in [os.path.join(common.hp("data", "qa-trace"), f) for f in os.listdir(common.hp("data", "qa-trace"))] \
                if os.path.isdir(common.hp("data", "qa-trace")) else []:
            rec = common.read_json(p, {}) or {}
            for s in (rec.get("trace") or {}).get("steps") or []:
                for m in re.finditer(r"=\s*'?([\w.@+-]{4,64})", (s.get("key") or "") + " " + (s.get("values") or "")):
                    v = m.group(1)
                    if not re.fullmatch(r"(?i)true|false|null|none|active|inactive|approved", v):
                        vals.add(v)
            for kv in rec.get("start_ids") or []:
                v = kv.split("=", 1)[-1]
                if len(v) >= 4:
                    vals.add(v)
        common.write_json(common.wl("cache", "qa_values.json"), sorted(vals))

    # ------------------------------------------------------------------ backup
    def backup(self, today, st):
        dest = ((self.cfg.get("storage") or {}).get("local_backup_dir") or "").strip()
        if not dest or st.get("backup_day") == today or common.now().hour < 21:
            return
        try:
            paths.makedirs(dest)
            path = os.path.join(dest, "worklog-kb-%s.zip" % today)
            h = common.home()
            with paths.zip_writer(path + ".tmp") as z:
                for dp, dns, fns in os.walk(h):
                    rel = os.path.relpath(dp, h).replace("\\", "/")
                    dns[:] = [d for d in dns if d not in (".git", "raw", "sandbox", "logs", "__pycache__", "locks")]
                    for fn in fns:
                        if fn == ".env":
                            continue  # credentials never leave their file
                        z.write(os.path.join(dp, fn), os.path.join(rel, fn))
            paths.replace(path + ".tmp", path)
            zips = sorted(f for f in os.listdir(dest) if f.startswith("worklog-kb-") and f.endswith(".zip"))
            for f in zips[:-7]:
                paths.delete(os.path.join(dest, f))
            st["backup_day"] = today
        except Exception:
            common.log("agent", "backup failed: " + traceback.format_exc())


# --------------------------------------------------------------------------- rendering helpers

def _real_table(t):
    t = (t or "").strip()
    return bool(re.fullmatch(r"[\w\[\]\".]+", t)) and "." in t and not re.match(r"(?i)(information_schema|pg_|sys)\.", t)


def _columns(desc):
    """Column names from a describe_table result of unknown shape."""
    out = []
    if isinstance(desc, dict):
        for k in ("columns", "fields", "Columns"):
            if isinstance(desc.get(k), list):
                desc = desc[k]
                break
    if isinstance(desc, list):
        for c in desc:
            if isinstance(c, dict):
                n = c.get("name") or c.get("column_name") or c.get("COLUMN_NAME") or c.get("column")
                if n:
                    out.append(str(n))
    elif isinstance(desc, str):
        out = re.findall(r"(?m)^\s*\|?\s*([A-Za-z_][\w]*)\s*\|", desc)
    return out


def _mid(s):
    return re.sub(r"[^A-Za-z0-9_]", "_", s)[:40]


def database_label(cfg=None):
    """How rendered headings name the database MCP (mcp.database.label)."""
    cfg = cfg if cfg is not None else common.load_config()
    return (((cfg.get("mcp") or {}).get("database") or {}).get("label")) or "database MCP"


def dbflow_qa_section(t, cfg=None):
    """'Worked example (QA)' with actual values (local file; secrets-only masking) + schema facts filling Unknowns."""
    L = []
    rec = common.read_json(common.hp("data", "qa-trace", t + ".json"), {}) or {}
    tr = rec.get("trace") or {}
    if tr.get("steps"):
        L += ["## Worked example (QA)", "",
              "_Traced on %s (%s, connection `%s`) from the ids used in this ticket's requests: %s._" % (
                  rec.get("fetched_at", "")[:10], rec.get("env", "qa").upper(), rec.get("connection"),
                  ", ".join("`%s`" % x for x in rec.get("start_ids") or [])), ""]
        if tr.get("summary"):
            L += [tr["summary"], ""]
        L += ["| Step | Table | Found by | Values | Note |", "|---|---|---|---|---|"]
        for i, s in enumerate(tr["steps"], 1):
            L.append("| %d | `%s` | %s | %s | %s |" % (i, s.get("table"), _c(s.get("key")), _c(s.get("values")), _c(s.get("note"))))
        L.append("")
        if tr.get("changes"):
            L += ["**What changed** (before → after the replay)", ""]
            L += ["- `%s.%s`: %s → %s" % (c.get("table"), c.get("column"), c.get("before"), c.get("after")) for c in tr["changes"]]
            L.append("")
        L += ["```mermaid", "flowchart LR"]
        prev = None
        for i, s in enumerate(tr["steps"], 1):
            nid = "s%d" % i
            L.append('  %s["%s<br/>%s"]' % (nid, _m(s.get("table")), _m(s.get("key"))))
            if prev:
                L.append("  %s --> %s" % (prev, nid))
            prev = nid
        L += ["```", ""]
    cards = []
    conn = None
    for p in KBJobs._schema_files(None):
        r = common.read_json(p, {}) or {}
        cards.append(r)
        conn = conn or r.get("connection")
    tk_tables = set()
    tkj = common.read_json(common.hp("data", "tickets", t + ".json"), {}) or {}
    for e in (tkj.get("data_flow") or {}).values():
        if e.get("table"):
            tk_tables.add(e["table"].lower().split(".")[-1])
    facts = []
    for r in cards:
        if (r.get("table") or "").lower().split(".")[-1] not in tk_tables:
            continue
        card = r.get("card") or {}
        for tg in card.get("triggers") or []:
            facts.append("- `%s`: trigger `%s` (%s) sets %s" % (r["table"], tg.get("name"), tg.get("event"),
                                                               ", ".join("`%s`" % c for c in tg.get("sets_columns") or []) or "—"))
        for w in card.get("writers") or []:
            if w.get("writes"):
                facts.append("- `%s`: %s `%s` writes %s" % (r["table"], w.get("kind"), w.get("name"),
                                                          ", ".join("`%s`" % c for c in w["writes"])))
        if card.get("found") and not card.get("triggers") and not any(w.get("writes") for w in card.get("writers") or []):
            readers = [w.get("name") for w in card.get("writers") or []]
            facts.append("- `%s`: no database trigger, procedure or function writes it, so its column changes come from "
                         "application code%s" % (r["table"], (" (read by %s)" % ", ".join("`%s`" % x for x in readers[:5]))
                                                 if readers else ""))
    if facts:
        L += ["## Schema facts (%s, %s)" % (database_label(cfg), conn or "qa"), "",
              "_Read from the database catalog; these answer Unknowns above where they name the same columns._", ""] + facts + [""]
    return "\n".join(L)


def dbmap_section():
    rec = common.read_json(common.hp("data", "db-schema", "_linkmap.json"), {}) or {}
    res = rec.get("result") or {}
    if not res:
        return ""
    L = ["## Databases, schemas and tables (QA, %s)" % rec.get("fetched_at", "")[:10], ""]
    hier = res.get("hierarchy") or []
    for hrow in hier:
        L.append("- **%s** · `%s`: %s" % (hrow.get("database") or "(connection not mapped)", hrow.get("schema"),
                                         ", ".join("`%s`" % x for x in hrow.get("tables") or [])))
    L += ["", "```mermaid", "flowchart TB"]
    for i, hrow in enumerate(hier):
        L.append('  db%d["%s"] --> sc%d["%s"]' % (i, _m(hrow.get("database")), i, _m(hrow.get("schema"))))
    L += ["```", "", "## Verified links", "", "| From | To | Match | Status | Evidence |", "|---|---|---|---|---|"]
    for ln in res.get("links") or []:
        s, m = int(ln.get("sampled") or 0), int(ln.get("matched") or 0)
        status = "verified" if s and m / s >= 0.8 else ("likely" if s and m / s >= 0.3 else "unverified")
        ln["status"] = status
        L.append("| `%s` | `%s` | %d/%d | %s | %s |" % (ln.get("from"), ln.get("to"), m, s, status, _c(ln.get("evidence"))))
    L.append("")
    by = {}
    for ln in res.get("links") or []:
        sch = ".".join(ln["from"].split(".")[:-2]) or "default"
        by.setdefault(sch, []).append(ln)
    for sch, lns in sorted(by.items()):
        L += ["### %s" % sch, "```mermaid", "erDiagram"]
        for ln in lns:
            a, b = ln["from"].rsplit(".", 1)[0], ln["to"].rsplit(".", 1)[0]
            L.append('  %s }o--|| %s : "%s %s"' % (_mid(a), _mid(b), _m(ln["from"].rsplit(".", 1)[-1]), ln.get("status")))
        L += ["```", ""]
    return "\n".join(L)


def _c(s):
    return str(s or "").replace("|", "\\|").replace("\n", " ")[:300]


def _m(s):
    return re.sub(r"[\"<>{}\[\]]", "", str(s or ""))[:60]
