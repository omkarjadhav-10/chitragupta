"""Replayable requests per ticket.

Captured HTTP calls (hooks, Bruno, inbox `pin`, worklog-requests blocks, extraction) are stored in
data/tickets/<T>.json["api_calls"]. A curator pass (prompts/curate.md) picks the minimal ordered replay set
per scenario. Generated from that, with "my edits win" ownership tracked in data/generated.json:

    bruno/<T>/NN <name>.bru        one Bruno collection for all tickets (<home>/bruno)
    bruno/<T>/folder.bru           ticket title, scenarios and run order (only if you don't have one)
    tickets/<T>/requests.md        the same steps as curl for Git Bash
    tickets/<T>/replay.sh          bash tickets/<T>/replay.sh qa
    tickets/<T>/payloads/NN-*.json request bodies (templated, no patient data)

The .bru files on disk are the source of truth for requests.md / replay.sh, so an edit made in Bruno
flows into both. A .bru you edit becomes yours (never overwritten); one you delete is tombstoned
(never recreated); one you add is adopted as pinned.
"""
import hashlib
import json
import os
import re
import shlex

from . import apicalls
from . import brufile
from . import common
from .redact import redact
from .safety import paths

HEALTH_RX = re.compile(r"(?i)/(?:actuator(?:/health)?|health(?:check|z)?|ping|status|ready|live|swagger[^/]*|v\d/api-docs|openapi[^/]*|favicon\.ico)(?:/|\?|$)")
CODES_RX = re.compile(r"\b([1-5]\d\d)\b")
ROLE_ORDER = {"auth": 0, "setup": 1, "trigger": 2, "verify": 3, "cleanup": 4, "exploratory": 5}


def sha(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def slug(s, n=60):
    s = re.sub(r"[^A-Za-z0-9]+", "-", (s or "").strip()).strip("-").lower()
    return s[:n].strip("-") or "request"


def safe_name(s, n=70):
    s = re.sub(r"[\\/:*?\"<>|]+", " ", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:n].rstrip(" .") or "request"


def bruno_root():
    return common.hp("bruno")


def rel(p):
    return os.path.relpath(p, common.home()).replace("\\", "/")


# --------------------------------------------------------------------------- registry (my edits win)

def registry_path():
    return common.hp("data", "generated.json")


def load_registry():
    r = common.read_json(registry_path(), None) or {}
    r.setdefault("schema_version", 1)
    r.setdefault("files", {})
    return r


def save_registry(r):
    common.write_json(registry_path(), r)


# --------------------------------------------------------------------------- environments

_LEARNED = {}


def learned_index(cfg):
    """URL-valued variables and {{var}}/path prefixes from the configured Bruno collections (read-only)."""
    key = tuple(cfg.get("bruno_collections") or [])
    if key in _LEARNED:
        return _LEARNED[key]
    envs, prefixes = {}, {}
    for root in list(key) + [bruno_root()]:
        ed = os.path.join(root, "environments")
        if os.path.isdir(ed):
            for fn in os.listdir(ed):
                if fn.endswith(".bru"):
                    vars_, _ = brufile.parse_env(common.read_text(os.path.join(ed, fn)))
                    e = envs.setdefault(fn[:-4].upper(), {})
                    for k, v in vars_:
                        if re.match(r"^https?://", v or "") and "{{" not in v:
                            e.setdefault(k, v.rstrip("/"))
        for dp, dn, fns in os.walk(root):
            dn[:] = [d for d in dn if d not in ("node_modules", ".git", "environments")]
            for fn in fns:
                if fn.endswith(".bru") and fn != "folder.bru":
                    m = re.search(r"(?m)^\s*url:\s*\{\{(\w+)\}\}/?([^?\s]*)", common.read_text(os.path.join(dp, fn)))
                    if m:
                        segs = "/".join([s for s in m.group(2).split("/") if s and "{{" not in s][:3])
                        for i in range(1, len(segs.split("/")) + 1):
                            k = m.group(1) + "|" + "/".join(segs.split("/")[:i])
                            prefixes[k] = prefixes.get(k, 0) + 1
    _LEARNED[key] = (envs, prefixes)
    return envs, prefixes


def own_envs():
    out = {}
    ed = os.path.join(bruno_root(), "environments")
    if os.path.isdir(ed):
        for fn in sorted(os.listdir(ed)):
            if fn.endswith(".bru"):
                vars_, _ = brufile.parse_env(common.read_text(os.path.join(ed, fn)))
                out[fn[:-4].upper()] = dict(vars_)
    return out


def env_index(cfg):
    envs, prefixes = learned_index(cfg)
    return apicalls.EnvIndex(own_envs(), envs, prefixes)


def apply_env_additions(idx, changed):
    """Add missing keys to bruno/environments/<ENV>.bru. Existing values are never changed."""
    ed = os.path.join(bruno_root(), "environments")
    for env, adds in sorted(idx.additions.items()):
        if env == "PROD" or not adds:
            continue
        path = None
        if os.path.isdir(ed):
            path = next((os.path.join(ed, fn) for fn in os.listdir(ed) if fn.lower() == env.lower() + ".bru"), None)
        path = path or os.path.join(ed, env + ".bru")
        vars_, secrets = brufile.parse_env(common.read_text(path))
        have = {k for k, _ in vars_}
        new = [(k, v) for k, v in sorted(adds.items()) if k not in have]
        if not new:
            continue
        if common.read_text(path):
            # append inside the existing vars block, keeping the file byte-for-byte otherwise
            txt = common.read_text(path)
            m = re.search(r"(?ms)^vars\s*\{\n(.*?)^\}", txt)
            if m:
                ins = "".join("  %s: %s\n" % (k, v) for k, v in new)
                txt = txt[:m.end(1)] + ins + txt[m.end(1):]
            else:
                txt = brufile.render_env(new, []) + txt
        else:
            txt = brufile.render_env(new, ["token"])
        if common.write_text(path, txt):
            changed.append(rel(path))


def ensure_dotenv(variables, changed):
    """Add missing keys only: empty for credentials/secrets, synthetic defaults for person fields.
    Never changes an existing value; never writes a captured value."""
    path = os.path.join(bruno_root(), ".env")
    ex_path = os.path.join(bruno_root(), ".env.example")
    cur = common.read_text(path)
    have = set(re.findall(r"(?m)^\s*([A-Za-z_]\w*)\s*=", cur))
    ex = common.read_text(ex_path) or "# Copy to .env (git-ignored) and fill in. Requests read these as {{process.env.NAME}}.\n"
    ex_have = set(re.findall(r"(?m)^\s*([A-Za-z_]\w*)\s*=", ex))
    add, add_ex = [], []
    for v in variables:
        if v["kind"] not in ("secret", "credential", "person"):
            continue
        if v["name"] not in have:
            default = v.get("default", "") if v["kind"] == "person" else ""
            add.append("%s=%s" % (v["name"], default))
            have.add(v["name"])
        if v["name"] not in ex_have:
            add_ex.append("%s=" % v["name"])
            ex_have.add(v["name"])
    if add:
        txt = (cur.rstrip("\n") + "\n" if cur.strip() else "") + "\n".join(add) + "\n"
        common.write_text(path, txt)  # git-ignored; never listed as a changed (committed) file
    if add_ex:
        common.write_text(ex_path, ex.rstrip("\n") + "\n" + "\n".join(add_ex) + "\n")
        changed.append(rel(ex_path))


# --------------------------------------------------------------------------- calls

def new_call(**kw):
    c = {"id": "", "ticket": None, "at": "", "day": "", "session": "", "source": "claude_session", "method": "GET",
         "url": "", "headers": [], "body": None, "form": [], "basic": None, "cookie": False, "status": None,
         "statuses": [], "response_keys": None, "token_path": None, "outcome": "unknown", "role": "", "scenario": "",
         "critical_fields": [], "expected": "", "auth_mechanism": "", "evidence": "", "pinned": False,
         "executed": True, "name": "", "purpose": ""}
    c.update(kw)
    if not c["id"]:
        body_h = hashlib.sha1(json.dumps([c.get("body"), c.get("form"), c.get("headers")], sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
        c["id"] = common.stable_id(c["ticket"] or "-", c["session"], c["at"], c["method"], c["url"], c.get("idx", 0), body_h, c.get("status"))
    c.pop("idx", None)
    return c


def calls_from_event(ev, ticket):
    """Deterministic calls from one captured post-tool event (Bash/PowerShell HTTP command)."""
    p = ev.get("payload") or {}
    ki = p.get("input") or {}
    cmd = ki.get("command") or ""
    if not cmd or not apicalls.is_http_command(cmd):
        return []
    oc = (p.get("outcome") or {}).get("http") or {}
    reqs = apicalls.parse_command(cmd, ev.get("cwd"), oc.get("files") or {})
    sts = oc.get("statuses") or []
    out = []
    for i, r in enumerate(reqs):
        st = sts[i] if len(sts) == len(reqs) else (sts[-1] if len(reqs) == 1 and sts else None)
        out.append(new_call(ticket=ticket, at=ev.get("ts") or "", session=(ev.get("session_id") or "")[:8],
                            source="claude_session", method=r["method"], url=r["url"], headers=r["headers"],
                            body=r["body"], form=r["form"], basic=r["basic"], cookie=r["cookie"], status=st, statuses=sts,
                            response_keys=oc.get("response_keys") if len(reqs) == 1 else None,
                            token_path=oc.get("token_path") if len(reqs) == 1 else None,
                            outcome=apicalls.outcome_of(st, oc.get("transport_error")) if oc else "unknown",
                            executed=True, evidence="command at %s" % common.hhmm(ev.get("ts")), idx=i))
    return out


def add_call(store, t, call, day=None):
    """Insert or update a call. Returns True when something new was recorded."""
    tk = store.ticket(t)
    calls = tk.setdefault("api_calls", {})
    call["ticket"], call["day"] = t, call.get("day") or day or common.workday(call.get("at") or common.now_iso(), common.load_config())
    if call["id"] in calls:
        old = calls[call["id"]]
        merged = dict(old)
        for k, v in call.items():
            if v not in (None, "", [], {}) and (old.get(k) in (None, "", [], {}) or k in ("pinned",) and v):
                merged[k] = v
        if merged != old:
            calls[call["id"]] = merged
            tk.setdefault("replay", {})["dirty"] = True
            store.touch_ticket(t)
        return False
    calls[call["id"]] = call
    tk.setdefault("replay", {})["dirty"] = True
    tk["replay"]["dirty_since"] = tk["replay"].get("dirty_since") or common.now_iso()
    store.touch_ticket(t)
    return True


def annotate_from_extraction(store, t, items, ctx):
    """Extraction api_calls[]: annotate matching captured calls (role, scenario, expected...), or add
    calls only the extractor saw (Bruno, text, http files)."""
    tk = store.ticket(t)
    calls = tk.setdefault("api_calls", {})
    n = 0
    for a in items or []:
        method = (a.get("method") or "GET").upper()
        shape = apicalls.path_shape(a.get("url") or "")
        if not shape and not a.get("url"):
            continue
        cands = [c for c in calls.values() if c["method"] == method and apicalls.path_shape(c["url"]) == shape
                 and c.get("source") in ("claude_session", "transcript")]
        akeys = _body_keys(a.get("body"))

        def score(c):
            sc = 0.0
            if isinstance(a.get("status"), int) and a["status"] == c.get("status"):
                sc += 4
            ck = _body_keys(c.get("body"))
            if akeys or ck:
                sc += 3 * len(akeys & ck) / max(1, len(akeys | ck))
            if a.get("at"):
                sc -= abs(_mins(common.hhmm(c.get("at"))) - _mins(a["at"])) / 30.0
            return sc
        cands.sort(key=score, reverse=True)
        ann = {k: a.get(k) for k in ("role", "scenario", "critical_fields", "expected", "auth_mechanism") if a.get(k)}
        if a.get("outcome") and a["outcome"] != "unknown":
            ann["outcome_llm"] = a["outcome"]
        if cands:
            c = cands[0]
            before = dict(c)
            c.update(ann)
            if a.get("pinned"):
                # a note naming a request pins the working variant of it, never a failed attempt
                good = [x for x in cands if x.get("outcome") == "worked"]
                target = c if c.get("outcome") == "worked" else (max(good, key=lambda x: x.get("at") or "") if good else None)
                if target and not target.get("pinned"):
                    target["pinned"] = True
                    tk.setdefault("replay", {})["dirty"] = True
            if c != before:
                tk.setdefault("replay", {})["dirty"] = True
                n += 1
        else:
            hdrs = a.get("headers") or {}
            hdrs = [[k, v] for k, v in hdrs.items()] if isinstance(hdrs, dict) else hdrs
            body = a.get("body")
            if body is not None and not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False)
            st = a.get("status") if isinstance(a.get("status"), int) else None
            c = new_call(ticket=t, at=_at(ctx, a.get("at")), session=ctx.get("session", "")[:8],
                         source=a.get("source") or "claude_session", method=method, url=a.get("url") or "",
                         headers=hdrs, body=body, status=st, outcome=a.get("outcome") or "unknown",
                         response_keys=a.get("response_keys") or None, executed=bool(st) or a.get("source") == "bruno",
                         evidence=a.get("evidence") or "", **ann)
            if add_call(store, t, c, ctx.get("day")):
                n += 1
    if n:
        store.touch_ticket(t)
    return n


def _body_keys(body):
    try:
        b = json.loads(body) if isinstance(body, str) else body
    except (ValueError, TypeError):
        return set()
    return set(b) if isinstance(b, dict) else set()


def _mins(hhmm):
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


def _at(ctx, hhmm):
    day = ctx.get("day") or ""
    if day and hhmm and re.match(r"^\d{1,2}:\d{2}$", hhmm):
        return "%sT%s:00" % (day, hhmm.zfill(5))
    return common.now_iso()


def pin_requests_block(store, obj, ctx):
    """worklog-requests block: requests are pinned, ingested as written (then templated for privacy)."""
    t = obj.get("ticket")
    scen = obj.get("scenario") or ""
    n = 0
    for i, r in enumerate(obj.get("requests") or []):
        hdrs = r.get("headers") or {}
        hdrs = [[k, v] for k, v in hdrs.items()] if isinstance(hdrs, dict) else hdrs
        body = r.get("body")
        if body is not None and not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False, indent=2)
        c = new_call(ticket=t, at=ctx.get("ts") or common.now_iso(), session=(ctx.get("session") or "")[:8],
                     source="block", method=(r.get("method") or "GET").upper(), url=r.get("url") or "", headers=hdrs,
                     body=body, scenario=r.get("scenario") or scen, expected=r.get("expected") or "",
                     name=r.get("name") or "", purpose=r.get("purpose") or "", critical_fields=r.get("critical_fields") or [],
                     role=r.get("role") or "", pinned=True, executed=bool(r.get("executed", True)), outcome="worked",
                     evidence="worklog-requests block", block_seq=i + 1, idx=i)
        if add_call(store, t, c, ctx.get("day")):
            n += 1
    return n


def pin_inbox(store, t, text, ts, day):
    """inbox line `ABC-1001: pin <curl ...>`."""
    m = re.match(r"(?i)^\s*pin\s+(.+)$", text or "", re.S)
    if not m:
        return 0
    n = 0
    for i, r in enumerate(apicalls.parse_command(m.group(1))):
        c = new_call(ticket=t, at=ts, session="inbox", source="inbox", method=r["method"], url=r["url"], headers=r["headers"],
                     body=r["body"], form=r["form"], basic=r["basic"], pinned=True, executed=False, outcome="unknown",
                     evidence="inbox.md pin", idx=i)
        n += add_call(store, t, c, day)
    return n


# --------------------------------------------------------------------------- curation

def prefilter(calls):
    """Deterministic noise removal before the curator: OPTIONS/HEAD, health checks, exact duplicates."""
    keep, dropped, seen = [], [], {}
    for c in sorted(calls, key=lambda c: c.get("at") or ""):
        if c.get("pinned"):
            keep.append(c)
            continue
        if c["method"] in ("OPTIONS", "HEAD"):
            dropped.append({"call_id": c["id"], "reason": "%s request" % c["method"]})
            continue
        if HEALTH_RX.search(re.sub(r"\?.*$", "", c["url"])):
            dropped.append({"call_id": c["id"], "reason": "health / docs check"})
            continue
        sig = (c["method"], c["url"], c.get("body") or "", c.get("status"))
        if sig in seen:
            dropped.append({"call_id": seen[sig]["id"], "reason": "duplicate of a later identical call"})
            keep.remove(seen[sig])
        seen[sig] = c
        keep.append(c)
    return keep, dropped


def curation_input(store, t, calls, idx):
    tk = store.ticket(t)
    finds = sorted([i for i in tk.get("items", {}).values() if i.get("kind") in ("root_cause", "finding", "repro_test", "how_it_works")],
                   key=lambda i: (i.get("date", ""), i.get("time", "")), reverse=True)[:15]
    view = []
    for c in calls:
        req, vs, mech, env = apicalls.template_call(c, idx, c.get("critical_fields") or ())
        view.append({"id": c["id"], "at": (c.get("at") or "")[:16], "source": c.get("source"), "method": req["method"],
                     "url": req["url"], "headers": req["headers"], "auth": req["auth"].get("mode"),
                     "body": common.truncate(req.get("body") or "", 3000) if req.get("body") else (req.get("form") or None),
                     "status": c.get("status"), "outcome": c.get("outcome"), "response_keys": json.dumps(c.get("response_keys"))[:600] if c.get("response_keys") else None,
                     "role_hint": c.get("role"), "scenario_hint": c.get("scenario"), "expected_hint": c.get("expected"),
                     "critical_fields": c.get("critical_fields") or [], "pinned": bool(c.get("pinned")), "executed": c.get("executed", True),
                     "name_hint": c.get("name"), "purpose_hint": c.get("purpose")})
    cur = [{"n": s.get("n"), "name": s.get("name"), "file": s.get("file"), "pinned": s.get("pinned"), "expected": s.get("expected")}
           for s in (tk.get("replay") or {}).get("final") or []]
    data = {"ticket": t, "title": tk.get("title"), "tldr": tk.get("tldr"),
            "findings": [{"kind": i["kind"], "title": i["title"], "detail": common.truncate(i.get("detail"), 300)} for i in finds],
            "queries": [{"id": q["id"], "purpose": q.get("purpose"), "tables": q.get("tables")} for q in list(tk.get("queries", {}).values())[-20:]],
            "current_replay_set": cur, "calls": view}
    return redact(json.dumps(data, ensure_ascii=False, indent=1))[:140000]


def curation_due(tk, cfg, recent_ticket_ts, force=False):
    rp = tk.get("replay") or {}
    if not rp.get("dirty"):
        return False
    if force:
        return True
    nxt = common.parse_ts(rp.get("curate_next"))
    if nxt and nxt > common.now():
        return False
    idle = int((cfg.get("extract_triggers") or {}).get("idle_minutes", 10))
    last = common.parse_ts(recent_ticket_ts)
    return not last or (common.now() - last).total_seconds() >= idle * 60


def curate(store, t, cfg, caller=None):
    """Run the curator for a ticket. Returns cost. Falls back to a deterministic pick when the call fails."""
    from . import extract
    tk = store.ticket(t)
    rp = tk.setdefault("replay", {})
    calls = list((tk.get("api_calls") or {}).values())
    keep, dropped = prefilter(calls)
    idx = env_index(cfg)
    cost = 0.0
    res = None
    if [c for c in keep if not c.get("pinned")] or (keep and not rp.get("scenarios")):
        try:
            res, cost = (caller or extract.call_claude)("curate", curation_input(store, t, keep, idx), cfg)
        except extract.ExtractError as e:
            fails = int(rp.get("curate_fail") or 0) + 1
            rp["curate_fail"] = fails
            import datetime as _dt
            rp["curate_next"] = (common.now() + _dt.timedelta(minutes=min(15 * 2 ** (fails - 1), 360))).isoformat(timespec="seconds")
            common.log("agent", "curation failed for %s: %s" % (t, e))
            res = fallback_curation(keep) if fails >= 3 else None
            if res is None:
                store.touch_ticket(t)
                return cost
    else:
        res = fallback_curation(keep)
    by_id = {c["id"]: c for c in calls}
    scenarios = []
    for sc in res.get("scenarios") or []:
        steps = []
        for st in sc.get("steps") or []:
            cid = st.get("call_id")
            if cid and cid not in by_id:
                cid = None
            if not cid and not st.get("request") and not st.get("repeat_of_step"):
                continue
            steps.append({"call_id": cid, "name": (st.get("name") or "").strip()[:70], "purpose": st.get("purpose") or "",
                          "why_needed": st.get("why_needed") or "", "expected": st.get("expected") or "",
                          "critical_fields": st.get("critical_fields") or [], "verify_query": st.get("verify_query") or "",
                          "repeat_of_step": st.get("repeat_of_step"), "request": st.get("request"),
                          "executed": bool(cid and by_id[cid].get("executed", True)), "role": st.get("role") or ""})
        if steps:
            scenarios.append({"name": sc.get("name") or "main", "summary": sc.get("summary") or "", "steps": steps})
    # pinned calls are always kept (rule 5)
    used = {s["call_id"] for sc in scenarios for s in sc["steps"] if s["call_id"]}
    for c in keep:
        if c.get("pinned") and c["id"] not in used:
            name = c.get("scenario") or "pinned"
            sc = next((s for s in scenarios if s["name"] == name), None)
            if not sc:
                sc = {"name": name, "summary": "", "steps": []}
                scenarios.append(sc)
            sc["steps"].append({"call_id": c["id"], "name": c.get("name") or "", "purpose": c.get("purpose") or "", "why_needed": "pinned",
                                "expected": c.get("expected") or "", "critical_fields": c.get("critical_fields") or [],
                                "verify_query": "", "repeat_of_step": None, "request": None, "executed": c.get("executed", True),
                                "role": c.get("role") or ""})
    rp.update({"scenarios": scenarios, "dropped": dropped + (res.get("dropped") or []), "dirty": False,
               "curated_at": common.now_iso(), "curated_calls": len(calls)})
    rp.pop("curate_fail", None)
    rp.pop("curate_next", None)
    rp.pop("dirty_since", None)
    store.touch_ticket(t)
    return cost


def fallback_curation(calls):
    """No-LLM pick: latest working call per method+path, auth first."""
    best = {}
    for c in sorted(calls, key=lambda c: c.get("at") or ""):
        if c.get("pinned") or c.get("outcome") == "worked":
            best[apicalls.signature({"method": c["method"], "url": c["url"], "body": c.get("body")})] = c
    picked = sorted(best.values(), key=lambda c: (0 if c.get("token_path") else 1, c.get("at") or ""))
    return {"scenarios": [{"name": "main", "summary": "", "steps": [
        {"call_id": c["id"], "name": c.get("name") or "", "purpose": c.get("purpose") or "", "why_needed": "latest working call",
         "expected": c.get("expected") or ("%s" % c["status"] if c.get("status") else ""), "critical_fields": c.get("critical_fields") or [],
         "verify_query": "", "repeat_of_step": None, "request": None} for c in picked]}] if picked else [], "dropped": []}


# --------------------------------------------------------------------------- folder parsing

def read_folder(t):
    d = os.path.join(bruno_root(), t)
    files = {}
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".bru"):
                continue
            p = os.path.join(d, fn)
            txt = common.read_text(p)
            if fn == "folder.bru":
                continue
            req = brufile.parse_request(txt)
            if req:
                files[fn] = {"path": p, "text": txt, "req": req}
    folder = brufile.parse_folder(common.read_text(os.path.join(d, "folder.bru"))) if os.path.exists(os.path.join(d, "folder.bru")) else None
    return files, folder


def run_order(folder_docs, files):
    """'Run in order:' list in folder.bru docs -> [(file, expected_text)], repeats allowed."""
    if not folder_docs or "Run in order" not in folder_docs:
        return []
    lines = folder_docs.split("Run in order", 1)[1].split("\n")[1:]
    out = []
    by_seq = {}
    for fn, f in files.items():
        if f["req"].get("seq") is not None:
            by_seq.setdefault(f["req"]["seq"], fn)
        m = re.match(r"^(\d+)", fn)
        if m:
            by_seq.setdefault(int(m.group(1)), fn)
    for l in lines:
        if not l.strip():
            if out:
                break
            continue
        m = re.match(r"^\s*(\d+)\.\s+(.*?)(?:\s{2,}(.*))?$", l)
        if m and int(m.group(1)) in by_seq:
            out.append([by_seq[int(m.group(1))], (m.group(3) or "").strip(), m.group(2).strip()])
        elif out and l.startswith((" ", "\t")) and not re.match(r"^\s*\d+\.", l):
            out[-1][1] = (out[-1][1] + " " + l.strip()).strip()
        else:
            break
    return out


def expected_codes(text, req=None):
    for k, v in (req or {}).get("asserts") or []:
        if k.strip() == "res.status":
            m = re.match(r"^(eq|in)\s+(.+)$", v.strip())
            if m:
                return [int(x) for x in re.findall(r"\d{3}", m.group(2))]
    t = re.sub(r"(?i)\bbug\b[^0-9]{0,20}\d{3}[^/;,\n]*", "", text or "")  # 'bug: 423 ...' is what we are checking against
    return [int(x) for x in CODES_RX.findall(t)][:4]


# --------------------------------------------------------------------------- generation

def _token_needed_after(steps, i):
    return any("{{token}}" in json.dumps(s.get("req") or {}) for s in steps[i + 1:])


def build_request(call, step, idx, seq):
    req, variables, mech, env = apicalls.template_call(call, idx, step.get("critical_fields") or call.get("critical_fields") or ())
    name = step.get("name") or call.get("name") or default_name(call)
    req["name"] = "%02d %s" % (seq, safe_name(name))
    req["seq"] = seq
    codes = expected_codes(step.get("expected") or call.get("expected") or "")
    if not codes and call.get("status") and call.get("outcome") == "worked":
        codes = [call["status"]]
    if len(codes) == 1:
        req["asserts"] = [["res.status", "eq %d" % codes[0]]]
    elif len(codes) > 1:
        req["asserts"] = [["res.status", "in [%s]" % ", ".join(str(c) for c in codes)]]
    docs = []
    if step.get("purpose") or call.get("purpose"):
        docs.append("Purpose: " + (step.get("purpose") or call.get("purpose")))
    if step.get("why_needed"):
        docs.append("Why needed: " + step["why_needed"])
    if step.get("expected") or call.get("expected"):
        docs.append("Expected: " + (step.get("expected") or call.get("expected")))
    crit = step.get("critical_fields") or call.get("critical_fields") or []
    if crit:
        docs.append("Critical field: " + "; ".join(crit) + " (keep exactly as written; JSON null stays null)")
    src = "captured call, %s %s" % ((call.get("at") or "")[:10], common.hhmm(call.get("at"))) if call.get("executed", True) \
        else "reconstructed from %s - not yet executed" % (call.get("source") or "code/docs")
    docs.append("Source: " + src + ".")
    req["docs"] = "\n".join(docs)
    return req, variables, mech


def default_name(call):
    path = re.sub(r"\?.*$", "", re.sub(r"^https?://[^/]+|\{\{[^}]+\}\}", "", call.get("url") or ""))
    segs = [s for s in path.split("/") if s and not re.match(r"^(api|v\d+|\d+|\{\{.*\}\}|:\w+)$", s)]
    verb = {"GET": "Get", "POST": "Post", "PUT": "Put", "PATCH": "Patch", "DELETE": "Delete"}.get(call.get("method"), call.get("method"))
    if call.get("token_path") or re.search(r"(?i)login|token|auth", path):
        return "Login" if re.search(r"(?i)login|signin", path) else "Get token"
    return "%s %s" % (verb, " ".join(segs[-2:]) or "request")


def generate(store, t, cfg):
    """Write bruno/<T>/*.bru, folder.bru, requests.md, replay.sh, payloads/. Returns changed (committed) paths."""
    tk = store.ticket(t)
    rp = tk.setdefault("replay", {})
    reg = load_registry()
    changed = []
    bdir = os.path.join(bruno_root(), t)
    files, folder = read_folder(t)
    regf = reg["files"]
    prefix = "bruno/%s/" % t
    events = []

    # 1. ownership: adopt files I added, mark files I edited, tombstone files I deleted
    for fn, f in files.items():
        key = prefix + fn
        r = regf.get(key)
        if r is None:
            regf[key] = {"owner": "user", "ticket": t, "sha": sha(f["text"]), "adopted": common.now_iso(),
                         "signature": apicalls.signature(f["req"])}
            events.append("adopted %s" % fn)
        elif r.get("owner") == "engine" and r.get("sha") != sha(f["text"]):
            r.update(owner="user", edited=common.now_iso(), sha=sha(f["text"]))
            events.append("you edited %s - now yours" % fn)
        elif r.get("owner") == "user":
            r["sha"] = sha(f["text"])
    for key, r in list(regf.items()):
        if key.startswith(prefix) and key != prefix + "folder.bru" and not r.get("tombstone") and key[len(prefix):] not in files:
            r.update(tombstone=True, deleted=common.now_iso())
            events.append("you deleted %s - will not be recreated" % key[len(prefix):])
    tomb_sigs = {r.get("signature") for k, r in regf.items() if k.startswith(prefix) and r.get("tombstone")}

    # 2. curated steps -> files
    idx = env_index(cfg)
    folder_mine = os.path.exists(os.path.join(bdir, "folder.bru")) and (regf.get(prefix + "folder.bru") or {}).get("owner") != "engine" \
        and bool(run_order((folder or {}).get("docs"), files))
    calls = tk.get("api_calls") or {}
    variables = []
    sig_to_file = {apicalls.signature(f["req"]): fn for fn, f in files.items()}
    steps = []
    next_seq = max([f["req"].get("seq") or 0 for f in files.values()] + [0]) + 1
    for sc in rp.get("scenarios") or []:
        for st in sc["steps"]:
            s = {"scenario": sc["name"], "name": st.get("name"), "purpose": st.get("purpose"), "why_needed": st.get("why_needed"),
                 "expected": st.get("expected"), "critical_fields": st.get("critical_fields") or [], "verify_query": st.get("verify_query"),
                 "executed": st.get("executed", True), "file": None, "call_id": st.get("call_id"), "role": st.get("role")}
            if st.get("repeat_of_step"):
                if not st.get("call_id"):
                    continue  # a repeat is kept only when a captured call shows it was actually made
                k = int(st["repeat_of_step"]) - 1
                if 0 <= k < len(steps) and steps[k].get("file"):
                    s.update(file=steps[k]["file"], name=st.get("name") or steps[k]["name"], repeat=True)
                    steps.append(s)
                continue
            call = calls.get(st.get("call_id")) if st.get("call_id") else None
            if call is None and st.get("request"):
                r = st["request"]
                hdrs = r.get("headers") or []
                hdrs = [[h.get("name"), h.get("value")] if isinstance(h, dict) else h for h in hdrs]
                call = new_call(ticket=t, method=(r.get("method") or "GET").upper(), url=r.get("url") or "", headers=hdrs,
                                body=r.get("body"), executed=False, source="reconstructed", expected=st.get("expected") or "")
            if call is None:
                continue
            if folder_mine and not (call.get("pinned") and call.get("source") in ("block", "inbox")):
                continue  # your folder.bru run order is the replay set; only requests you pin explicitly are added
            req, vs, mech = build_request(call, st, idx, next_seq)
            base = re.match(r"^\{\{(\w+)\}\}", req.get("url") or "")
            if not call.get("executed", True) and (not re.match(r"^\{\{\w+\}\}", req.get("url") or "") or
                                                   any(v["name"] == base.group(1) and v["kind"] == "runtime" for v in vs)):
                continue  # reconstructed request whose base URL is unknown: it could not be replayed
            sig = apicalls.signature(req)
            if call.get("block_seq") and not st.get("name") and call.get("name"):
                s["name"] = call["name"]
            if sig in sig_to_file:
                s["file"] = sig_to_file[sig]
                variables += vs
                steps.append(s)
                continue
            if sig in tomb_sigs:
                continue
            fn = "%s.bru" % req["name"]
            key = prefix + fn
            if regf.get(key, {}).get("tombstone"):
                continue
            s["file"], s["req_new"] = fn, req
            sig_to_file[sig] = fn
            variables += vs
            steps.append(s)
            next_seq += 1

    # token chaining: the step before the first {{token}} user that returns a token stores it
    for i, s in enumerate(steps):
        req = s.get("req_new")
        if not req:
            continue
        call = calls.get(s.get("call_id")) or {}
        if (call.get("token_path") or s.get("role") == "auth" or call.get("role") == "auth") and \
                any("{{token}}" in json.dumps(x.get("req_new") or (files.get(x["file"]) or {}).get("req") or {}) for x in steps[i + 1:]):
            req["script_post"] = brufile.token_script("token", call.get("token_path") or "auto")

    # 3. write new / engine-owned files (never touch files that are mine)
    for s in steps:
        req = s.pop("req_new", None)
        if not req:
            continue
        fn, key = s["file"], prefix + s["file"]
        text = brufile.render_request(req)
        r = regf.get(key)
        p = os.path.join(bdir, fn)
        if os.path.exists(p) and (not r or r.get("owner") != "engine"):
            continue
        if common.write_text(p, text):
            changed.append(rel(p))
            events.append(("updated %s" if r else "saved %s") % fn)
        regf[key] = {"owner": "engine", "ticket": t, "sha": sha(text), "signature": apicalls.signature(req),
                     "generated": (r or {}).get("generated") or common.now_iso()}
        files[fn] = {"path": p, "text": text, "req": brufile.parse_request(text)}

    # 3b. engine files no longer in the replay set are removed (only when you haven't edited them)
    wanted = {s["file"] for s in steps if s.get("file")}
    for fn in list(files):
        key = prefix + fn
        r = regf.get(key) or {}
        if r.get("owner") == "engine" and fn not in wanted and sha(files[fn]["text"]) == r.get("sha"):
            try:
                paths.delete(files[fn]["path"])
            except OSError:
                continue
            del files[fn]
            regf.pop(key, None)
            changed.append(prefix + fn)
            events.append("removed %s (no longer needed)" % fn)

    # 4. final order: my folder.bru 'Run in order' > curated order > seq
    final = []
    order = run_order((folder or {}).get("docs"), files) if folder_mine else []
    if order:
        for fn, exp, label in order:
            meta = next((dict(x) for x in steps if x.get("file") == fn), {})
            meta.update({"file": fn, "expected": exp or meta.get("expected", ""), "name": meta.get("name") or files[fn]["req"]["name"],
                         "scenario": meta.get("scenario", ""), "repeat": any(x["file"] == fn for x in final)})
            final.append(meta)
    elif steps:
        final = [dict(s) for s in steps if s.get("file") in files]
    used = {s["file"] for s in final}
    for fn in sorted(files, key=lambda f: (files[f]["req"].get("seq") or 999, f)):
        if fn not in used:
            pos = len(final)
            seq = files[fn]["req"].get("seq") or 999
            for k, s in enumerate(final):
                if (files[s["file"]]["req"].get("seq") or 999) > seq:
                    pos = k
                    break
            final.insert(pos, {"file": fn, "name": files[fn]["req"]["name"], "expected": "", "scenario": "", "pinned": True})
    for i, s in enumerate(final):
        s["n"] = i + 1
        req = files[s["file"]]["req"]
        s["name"] = display_name(s.get("name") or req["name"])
        s["owner"] = (regf.get(prefix + s["file"]) or {}).get("owner", "user")
        if not s.get("expected"):
            m = re.search(r"(?im)^\s*Expected:\s*(.+)$", req.get("docs") or "")
            s["expected"] = m.group(1).strip() if m else ""
        s["codes"] = expected_codes(s.get("expected"), req) if not s.get("repeat") else expected_codes(s.get("expected"))
        if not s["codes"] and not s.get("repeat"):
            s["codes"] = expected_codes("", req)

    # 5. folder.bru (only when I don't have my own)
    fkey = prefix + "folder.bru"
    fpath = os.path.join(bdir, "folder.bru")
    fr = regf.get(fkey)
    if final and (not os.path.exists(fpath) or (fr and fr.get("owner") == "engine")):
        if os.path.exists(fpath) and fr and sha(common.read_text(fpath)) != fr.get("sha"):
            fr["owner"] = "user"
        else:
            txt = brufile.render_folder("%s %s" % (t, tk.get("title") or ""), None, folder_docs(t, tk, rp, final))
            if common.write_text(fpath, txt):
                changed.append(rel(fpath))
            regf[fkey] = {"owner": "engine", "ticket": t, "sha": sha(txt)}
    elif os.path.exists(fpath) and not fr:
        regf[fkey] = {"owner": "user", "ticket": t, "sha": sha(common.read_text(fpath))}

    # 6. environments + .env keys
    for fn, f in files.items():
        for m in re.finditer(r"\{\{process\.env\.(\w+)\}\}", f["text"]):
            if not any(v["name"] == m.group(1) for v in variables):
                variables.append({"name": m.group(1), "kind": "secret" if re.search(r"KEY|SECRET|TOKEN|PASSWORD", m.group(1)) else "credential", "default": ""})
    apply_env_additions(idx, changed)
    ensure_dotenv(variables, changed)

    # 7. requests.md, replay.sh, payloads (from the .bru files on disk: my version wins)
    tdir = common.hp("tickets", t)
    if final:
        changed += write_payloads(t, final, files)
        envs = sorted(own_envs().keys()) or ["QA"]
        if common.write_text(os.path.join(tdir, "requests.md"), render_requests_md(t, tk, final, files, envs, rp)):
            changed.append("tickets/%s/requests.md" % t)
        if common.write_text(os.path.join(tdir, "replay.sh"), render_replay_sh(t, final, files, envs)):
            changed.append("tickets/%s/replay.sh" % t)
    rp["final"] = [{"n": s["n"], "file": s["file"], "name": s["name"], "expected": s.get("expected") or "",
                    "scenario": s.get("scenario") or "", "owner": s.get("owner"), "repeat": bool(s.get("repeat")),
                    "method": files[s["file"]]["req"]["method"], "url": files[s["file"]]["req"]["url"],
                    "executed": s.get("executed", True), "verify_query": s.get("verify_query") or "",
                    "critical_fields": s.get("critical_fields") or []} for s in final]
    if events:
        rp.setdefault("events", []).extend({"at": common.now_iso(), "text": e} for e in events)
        rp["events"] = rp["events"][-40:]
    save_registry(reg)
    store.touch_ticket(t)
    return changed, events


def display_name(name):
    return re.sub(r"^\s*\d+\s*[.)-]?\s+", "", name or "").strip() or (name or "")


def folder_docs(t, tk, rp, final):
    L = [tk.get("title") or t, ""]
    for sc in rp.get("scenarios") or []:
        if sc.get("summary"):
            L.append("Scenario %s: %s" % (sc["name"], sc["summary"]))
    L += ["", "Run in order:"]
    for s in final:
        L.append("  %d. %-34s %s" % (s["n"], s["name"][:34], s.get("expected") or ""))
    L += ["", "Generated by the worklog. Edit any request in Bruno and it becomes yours."]
    return "\n".join(L)


def payload_name(s):
    return "%02d-%s.json" % (s["n"], slug(display_name(s["name"])))


def write_payloads(t, final, files):
    out = []
    pdir = common.hp("tickets", t, "payloads")
    want = set()
    for s in final:
        req = files[s["file"]]["req"]
        if s.get("repeat") or req.get("body") is None or req.get("body_mode") not in ("json", "text", "xml"):
            continue
        name = payload_name(s)
        want.add(name)
        if common.write_text(os.path.join(pdir, name), redact(req["body"].rstrip("\n") + "\n")):
            out.append("tickets/%s/payloads/%s" % (t, name))
    if os.path.isdir(pdir):
        for fn in os.listdir(pdir):
            if fn not in want and re.match(r"^\d{2}-.*\.json$", fn):
                paths.delete(os.path.join(pdir, fn))
                out.append("tickets/%s/payloads/%s" % (t, fn))
    return out


def _sh_var(m):
    name = m.group(1)
    if name.startswith("process.env."):
        name = name[len("process.env."):]
    return "${%s}" % re.sub(r"\W", "_", name)


def shell_tpl(s):
    return re.sub(r"\{\{\s*([\w.$-]+)\s*\}\}", _sh_var, s or "")


def sq(s):
    return "'" + (s or "").replace("'", "'\\''") + "'"


def req_headers(req):
    """All headers a request sends, auth included, as templates."""
    hs = [[k, v] for k, v in req.get("headers") or []]
    a = req.get("auth") or {}
    if a.get("mode") == "bearer":
        hs.append(["Authorization", "Bearer " + (a.get("token") or "{{token}}")])
    elif a.get("mode") == "apikey" and (a.get("placement") or "header") == "header":
        hs.append([a.get("key", "x-api-key"), a.get("value", "")])
    return hs


def req_url(req):
    url = req.get("url") or ""
    a = req.get("auth") or {}
    if a.get("mode") == "apikey" and a.get("placement") == "queryparams":
        url += ("&" if "?" in url else "?") + "%s=%s" % (a.get("key"), a.get("value"))
    return url


def all_vars(req):
    s = json.dumps([req.get("url"), req.get("headers"), req.get("auth"), req.get("body"), req.get("form")])
    envs = sorted(set(re.findall(r"\{\{process\.env\.(\w+)\}\}", s)))
    return envs


def capture_spec(req):
    c = req.get("capture") or []
    return [(x["var"], x["path"]) for x in c]


def _tools():
    """(python, launcher) as forward-slash paths: generated scripts call `"<python>" "<launcher>" bruenv ...`."""
    return common.python_exe().replace("\\", "/"), common.launcher().replace("\\", "/")


def render_requests_md(t, tk, final, files, envs, rp):
    home = common.home().replace("\\", "/")
    folder = os.path.basename(home.rstrip("/")) or "home"
    py, launcher = _tools()
    msys = re.sub(r"^([A-Za-z]):", lambda m: "/" + m.group(1).lower(), home)
    env0 = (envs[0] if "QA" not in envs else "QA").lower()
    need = sorted({n for s in final for n in all_vars(files[s["file"]]["req"])})
    L = ["# %s — Replay requests" % t, "",
         "_Generated from [`bruno/%s`](../../bruno/%s) — edit the requests in Bruno, not here (your edits win and flow into this file)._" % (t, t), "",
         "**Run it any of three ways**", "",
         "1. **Bruno** — open the collection `%s/bruno` once; this ticket is the folder `%s`. Pick an environment (%s) and run the folder." % (folder, t, ", ".join(envs)),
         "2. **Bruno CLI** — `cd %s/bruno && bru run %s --env %s` (needs `npm i -g @usebruno/cli`)." % (msys, t, envs[0] if "QA" not in envs else "QA"),
         "3. **replay.sh** — `bash tickets/%s/replay.sh %s` from the %s folder: runs every step, prints status vs expected, stops at the first failure." % (t, env0, folder), ""]
    if need:
        L += ["Needs these in `bruno/.env` (git-ignored; fill once): " + ", ".join("`%s`" % n for n in need), ""]
    L += ["## Steps", "", "| # | Request | Expected | Source |", "|---|---|---|---|"]
    for s in final:
        src = "yours" if s.get("owner") == "user" else ("generated" + ("" if s.get("executed", True) else " · not yet executed"))
        L.append("| %d | %s%s | %s | %s |" % (s["n"], s["name"], " _(again)_" if s.get("repeat") else "", (s.get("expected") or "—").replace("|", "\\|"), src))
    dropped = len(rp.get("dropped") or [])
    if dropped:
        L += ["", "_%d other captured call%s left out (failed attempts, duplicates, health checks, look-arounds)._" % (dropped, "" if dropped == 1 else "s")]
    L += ["", "## curl (Git Bash)", "", "```bash", "cd %s" % msys, "ENV=%s" % env0,
          'eval "$("%s" "%s" bruenv shell $ENV)"   # base URLs + bruno/.env into this shell' % (py, launcher), "```", ""]
    for s in final:
        req = files[s["file"]]["req"]
        L.append("### %d. %s%s" % (s["n"], s["name"], " (again)" if s.get("repeat") else ""))
        purpose = re.search(r"(?im)^\s*Purpose:\s*(.+)$", req.get("docs") or "")
        if purpose:
            L.append("Purpose: " + purpose.group(1).strip() + "  ")
        L.append("Expected: " + (s.get("expected") or "2xx"))
        if s.get("critical_fields"):
            L.append("  \nCritical: " + "; ".join("`%s`" % c for c in s["critical_fields"]))
        L += ["", "```bash"]
        for k, v in req.get("vars_pre") or []:
            L.append("export %s=%s" % (re.sub(r"\W", "_", k), sq(v)))
        args = ["curl -sS -i -X %s %s" % (req["method"], '"%s"' % shell_tpl(req_url(req)))]
        for k, v in req_headers(req):
            args.append('-H "%s: %s"' % (k, shell_tpl(v).replace('"', '\\"')))
        a = req.get("auth") or {}
        if a.get("mode") == "basic":
            args.append('-u "%s:%s"' % (shell_tpl(a.get("username")), shell_tpl(a.get("password"))))
        pre = []
        mode = req.get("body_mode")
        if req.get("body") is not None and mode in ("json", "text", "xml"):
            pre.append('B=$("%s" "%s" bruenv body $ENV tickets/%s/payloads/%s)' % (py, launcher, t, payload_name(dict(s, n=_first_n(final, s)))))
            args.append('--data-binary @"$B"')
        for k, v in req.get("form") or []:
            if mode == "multipartForm":
                fm = re.match(r"^@file\((.*)\)$", v)
                args.append('-F "%s=%s"' % (k, ("@" + fm.group(1)) if fm else shell_tpl(v)))
            else:
                args.append('--data-urlencode "%s=%s"' % (k, shell_tpl(v)))
        caps = capture_spec(req)
        L += pre
        if caps:
            var, path = caps[0]
            args[0] = args[0].replace("curl -sS -i", "curl -sS")
            L.append("export %s=$(%s \\\n  | \"%s\" -c \"%s\")" % (re.sub(r"\W", "_", var), " \\\n  ".join(args), py, token_oneliner(path)))
        else:
            L.append(" \\\n  ".join(args))
        L += ["```", ""]
    return "\n".join(L).rstrip("\n") + "\n"


def _first_n(final, s):
    for x in final:
        if x["file"] == s["file"]:
            return x["n"]
    return s["n"]


def token_oneliner(path):
    if path and path != "auto":
        expr = "b" + "".join(".get(%r, {})" % p for p in path.split(".")[:-1]) + ".get(%r, '')" % path.split(".")[-1]
        return "import sys,json; b=json.load(sys.stdin); print(%s)" % expr.replace('"', '\\"')
    return ("import sys,json; b=json.load(sys.stdin); d=b.get('data') or {}; "
            "print(b.get('token') or b.get('accessToken') or b.get('access_token') or d.get('token') or d.get('accessToken') or d.get('access_token') or '')")


REPLAY_HEAD = r'''#!/usr/bin/env bash
# Replay %(t)s — generated by the worklog from bruno/%(t)s (edit the requests in Bruno; your edits win).
# Usage: bash tickets/%(t)s/replay.sh <env>      envs: %(envs)s
# Base URLs come from bruno/environments/<env>.bru, secrets from bruno/.env (git-ignored).
set -u
ENV="${1:-%(env0)s}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WL="$(cd "$HERE/../.." && pwd)"
PY="%(py)s"
LAUNCHER="%(launcher)s"
"$PY" -c "import sys" >/dev/null 2>&1 || { echo "Python 3 not found ($PY)"; exit 2; }
[ -f "$LAUNCHER" ] || { echo "Chitragupta launcher not found ($LAUNCHER)"; exit 2; }
TMP="$WL/.worklog/tmp/replay-$$"; mkdir -p "$TMP"; VARS="$TMP/vars.json"; JAR="$TMP/cookies.txt"; echo '{}' > "$VARS"
trap 'rm -rf "$TMP"' EXIT
wp() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%%s' "$1"; fi; }
bruenv() { CHITRAGUPTA_HOME="$(wp "$WL")" "$PY" "$LAUNCHER" bruenv "$@"; }
s() { bruenv subst "$ENV" "$(wp "$VARS")" "$1"; }
die() { echo "  ✗ $1"; exit 1; }
setvar() { bruenv setvar "$(wp "$VARS")" "$1" "$2"; }
body() { bruenv file "$ENV" "$(wp "$VARS")" "$(wp "$1")" "$(wp "$TMP/body")" || die "could not fill $1"; BODY="$(wp "$TMP/body")"; }
send() {  # send METHOD URL [curl args...]
  local m="$1" u="$2"; shift 2
  CODE=$(curl -sS -o "$(wp "$TMP/resp")" -w '%%{http_code}' -c "$(wp "$JAR")" -b "$(wp "$JAR")" -X "$m" "$u" "$@") || die "curl failed for $m $u"
}
expect() {  # expect "<codes>" "<expected text>"
  local ok=0
  if [ -z "$1" ]; then case "$CODE" in 2??) ok=1;; esac; else for c in $1; do [ "$CODE" = "$c" ] && ok=1; done; fi
  if [ "$ok" = 1 ]; then echo "  ✓ HTTP $CODE   (expected: ${2:-2xx})"; else
    echo "  ✗ HTTP $CODE   (expected: ${2:-2xx})"; head -c 600 "$TMP/resp"; echo; exit 1; fi
}
capture() { bruenv capture "$(wp "$VARS")" "$(wp "$TMP/resp")" "$1" "$2" || die "no $1 in the response"; }
'''


def render_replay_sh(t, final, files, envs):
    env0 = (envs[0] if "QA" not in envs else "QA").lower()
    need = sorted({n for s in final for n in all_vars(files[s["file"]]["req"])})
    py, launcher = _tools()
    L = [REPLAY_HEAD % {"t": t, "envs": ", ".join(e.lower() for e in envs), "env0": env0, "py": py, "launcher": launcher}]
    if need:
        L.append('bruenv check "$ENV" %s || exit 2' % " ".join(need))
    L.append('echo "Replaying %s on $ENV"' % t)
    for s in final:
        req = files[s["file"]]["req"]
        exp = (s.get("expected") or "").replace("\n", " ")
        L += ["", "# --- %d. %s" % (s["n"], s["name"]), 'echo "%d. %s"' % (s["n"], s["name"].replace('"', "'"))]
        for k, v in req.get("vars_pre") or []:
            L.append("setvar %s %s" % (sq(k), sq(v)))
        L.append("URL=$(s %s) || die 'missing variable'" % sq(req_url(req)))
        hargs = []
        for i, (k, v) in enumerate(req_headers(req)):
            L.append("H%d=$(s %s) || die 'missing variable'" % (i, sq("%s: %s" % (k, v))))
            hargs.append('-H "$H%d"' % i)
        a = req.get("auth") or {}
        if a.get("mode") == "basic":
            L.append("U=$(s %s) || die 'missing variable'" % sq("%s:%s" % (a.get("username", ""), a.get("password", ""))))
            hargs.append('-u "$U"')
        mode = req.get("body_mode")
        if req.get("body") is not None and mode in ("json", "text", "xml"):
            L.append('body "$HERE/payloads/%s"' % payload_name(dict(s, n=_first_n(final, s))))
            hargs.append('--data-binary "@$BODY"')
        for i, (k, v) in enumerate(req.get("form") or []):
            fm = re.match(r"^@file\((.*)\)$", v)
            if fm:
                hargs.append("-F %s" % sq("%s=@%s" % (k, fm.group(1))))
            else:
                L.append("F%d=$(s %s) || die 'missing variable'" % (i, sq("%s=%s" % (k, v))))
                hargs.append(('-F "$F%d"' if mode == "multipartForm" else '--data-urlencode "$F%d"') % i)
        L.append('send %s "$URL" %s' % (req["method"], " ".join(hargs)))
        L.append('expect "%s" %s' % (" ".join(str(c) for c in s.get("codes") or []), sq(exp)))
        for var, path in capture_spec(req):
            L.append("capture %s %s" % (sq(var), sq(path)))
    L += ["", 'echo "All %d steps passed."' % len(final)]
    return "\n".join(L) + "\n"
