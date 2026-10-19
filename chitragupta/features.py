"""Upgrade 01 pipeline: replayable requests, DB flow maps, artifacts. Called from agent.py.

    on_events(day, sid, session, events)   per newly ingested raw chunk (deterministic, no LLM)
    after_extraction(res, ctx)             extraction api_calls / data_flow / table_relations / artifacts
    process_artifacts()                    queue, untracked repo files, _drop/, watch folders, ticket folders
    curate_and_generate(force)             curator (LLM) when due, then bruno/requests.md/replay.sh
    backfill(...)                          one-off: replay retained raw logs + transcripts for the new fields
"""
import hashlib
import json
import os
import re
import time
import traceback

from . import apicalls
from . import artifacts as artmod
from . import blocks
from . import common
from . import configstore
from . import dbflow
from . import extract
from . import replay
from . import store as storemod
from .redact import redact

SCHEMA_VERSION = 2


class Features:
    def __init__(self, agent):
        self.a = agent
        self.store = agent.store
        self.cfg = agent.cfg
        self.state = agent.state
        self.collector = artmod.Collector(self.store, self.cfg, self.state)
        self.extras = {"requests": 0, "artifacts": 0, "dbflow": 0}
        self.generated = set()

    @property
    def prefixes(self):
        return self.cfg.get("ticket_prefixes") or []

    # ------------------------------------------------------------------ helpers
    def ticket_of(self, ev, session):
        if ev.get("event") == "user-prompt":
            found = common.find_tickets((ev.get("payload") or {}).get("prompt", ""), self.prefixes)
            if found:
                session["sticky"] = found[0]
        return ev.get("_t") or common.ticket_from_branch(ev.get("branch"), self.prefixes) or ev.get("ticket") or session.get("sticky")

    def slot(self, day, t):
        dd = self.store.day(day)
        if t in dd["tickets"]:
            return dd["tickets"][t]
        return self.a.ensure_tslot(day, t)

    def day_note(self, day, t, key, text):
        s = self.slot(day, t)
        lst = s.setdefault(key, [])
        if text not in lst:
            lst.append(text)
            self.store.touch_day(day)

    # ------------------------------------------------------------------ per event
    def on_events(self, day, sid, session, events):
        for ev in sorted(events, key=lambda e: e.get("ts") or ""):
            try:
                self._on_event(day, sid, session, ev)
            except Exception:
                common.log("agent", "features.on_event failed: " + traceback.format_exc())

    def _on_event(self, day, sid, session, ev):
        t = self.ticket_of(ev, session)
        p = ev.get("payload") or {}
        e = ev.get("event")
        if e == "post-tool":
            tool, ki = p.get("tool") or "", p.get("input") or {}
            if tool in ("Bash", "PowerShell") and t:
                for c in replay.calls_from_event(ev, t):
                    if replay.add_call(self.store, t, c, day):
                        self.extras["requests"] += 0  # counted when saved
            elif tool in ("Write", "Edit", "MultiEdit") and ki.get("file_path") and not artmod.excluded(ki["file_path"], self.cfg):
                self.queue_artifact(ki["file_path"], t, day, sid, ev.get("ts"), ki.get("content") if ki.get("content_mode") == "full" else None,
                                    ev.get("repo_root"), session.get("first") or ev.get("ts"))
            elif tool == "Artifact":
                for url in ((p.get("outcome") or {}).get("artifact_links") or []) + ([ki["url"]] if ki.get("url") else []):
                    self.queue_link(url, ki.get("title") or "", t, day, sid, ki.get("file_path"))
        elif e == "stop":
            msg = p.get("message") or ""
            found = [(b["kind"], b["text"]) for b in p.get("blocks") or []] or [(k, raw) for k, raw, _ in blocks.find(msg)]
            for kind, raw in found:
                self.ingest_block(kind, raw, ev, day, sid, t)
            for url in artmod_links(msg):
                self.queue_link(url, "", t, day, sid, None)

    def ingest_block(self, kind, raw, ev, day, sid, t):
        h = hashlib.sha1((kind + raw).encode("utf-8")).hexdigest()[:16]
        seen = self.state.setdefault("blocks_seen", [])
        if h in seen:
            return
        obj, errs = blocks.parse(kind, raw)
        if errs:
            common.log("agent", "invalid worklog-%s block in %s: %s" % (kind, sid[:8], "; ".join(errs)))
            bad = self.state.setdefault("blocks_invalid", [])
            if h not in bad:
                bad.append(h)
                self.state["blocks_invalid"] = bad[-200:]
            return
        tt = storemod.valid_ticket(obj.get("ticket"), {p.upper() for p in self.prefixes}) or t
        if not tt:
            common.log("agent", "worklog-%s block without a known ticket in %s" % (kind, sid[:8]))
            return
        obj["ticket"] = tt
        if kind == "requests":
            n = replay.pin_requests_block(self.store, obj, {"ts": ev.get("ts"), "session": sid, "day": day})
            if n:
                self.day_note(day, tt, "requests_changed", "pinned %d request%s from a worklog-requests block" % (n, "" if n == 1 else "s"))
                self.store.ticket(tt).setdefault("replay", {})["force"] = True
        else:
            for f in obj.get("data_flow") or []:
                f.setdefault("ticket", tt)
            nn, nu = dbflow.merge(self.store, tt, obj.get("data_flow"), obj.get("table_relations"), day, "worklog-dbflow block",
                                  overview=obj.get("overview"))
            if nn or nu:
                self.day_note(day, tt, "dbflow_updates", "deep trace: %d new, %d updated flow entries" % (nn, nu))
                self.extras["dbflow"] += nn + nu
        seen.append(h)
        self.state["blocks_seen"] = seen[-500:]

    # ------------------------------------------------------------------ after extraction
    def after_extraction(self, res, ctx):
        known = {p.upper() for p in self.prefixes}
        default_t = ctx["candidates"][0] if len(ctx.get("candidates") or []) == 1 else None
        day = ctx["day"]
        by_t = {}
        for kind in ("api_calls", "data_flow", "table_relations", "artifacts"):
            for it in res.get(kind) or []:
                t = storemod.valid_ticket(it.get("ticket"), known) or default_t
                if t:
                    by_t.setdefault(t, {}).setdefault(kind, []).append(it)
        for t, parts in by_t.items():
            if parts.get("api_calls"):
                for a in parts["api_calls"]:
                    if a.get("headers") and isinstance(a["headers"], list) and a["headers"] and isinstance(a["headers"][0], dict):
                        a["headers"] = [[h.get("name"), h.get("value")] for h in a["headers"]]
                replay.annotate_from_extraction(self.store, t, parts["api_calls"], ctx)
            if parts.get("data_flow") or parts.get("table_relations"):
                nn, nu = dbflow.merge(self.store, t, parts.get("data_flow"), parts.get("table_relations"), day, "extraction")
                if nn or nu:
                    self.day_note(day, t, "dbflow_updates", "%d new, %d updated flow entries" % (nn, nu))
                    self.extras["dbflow"] += nn + nu
            if parts.get("artifacts"):
                self.collector.set_purposes(t, parts["artifacts"])
                pend = self.state.setdefault("artifact_purposes", {})
                for it in parts["artifacts"]:
                    if it.get("purpose") and it.get("path"):
                        pend[os.path.basename(it["path"].replace("\\", "/")).lower()] = [t, it["purpose"]]

    # ------------------------------------------------------------------ artifacts
    def queue_artifact(self, path, t, day, sid, ts, content, repo, session_start):
        q = self.state.setdefault("artifact_queue", {})
        key = common.norm_path(path)
        cur = q.get(key) or {}
        q[key] = {"path": path, "ticket": t or cur.get("ticket"), "day": day, "session": sid, "ts": ts,
                  "repo": repo, "session_start": cur.get("session_start") or session_start,
                  "content": content if content is not None else cur.get("content")}

    def queue_link(self, url, title, t, day, sid, local_file):
        q = self.state.setdefault("link_queue", {})
        q[url] = {"url": url, "title": title or (q.get(url) or {}).get("title", ""), "ticket": t, "day": day,
                  "session": sid, "file": local_file}

    def process_artifacts(self):
        c = self.collector
        q = self.state.get("artifact_queue") or {}
        purposes = self.state.get("artifact_purposes") or {}
        for key, it in list(q.items()):
            try:
                if artmod.excluded(it.get("path"), self.cfg):  # queued before its folder was ignored
                    del q[key]
                    continue
                if not it.get("ticket"):
                    self._unfiled(it["day"], it["path"], "Claude Code session " + (it.get("session") or "")[:8])
                    del q[key]
                    continue
                if it.get("repo") and artmod.tracked_before(self.cfg, it["repo"], it["path"], it.get("session_start")):
                    del q[key]  # an existing tracked file: that's a code change, not an artifact
                    continue
                ext = os.path.splitext(it["path"])[1].lower()
                if ext in artmod.SCRIPT_EXT and it.get("repo") and not self._untracked(it["repo"], it["path"]):
                    del q[key]
                    continue
                pp = purposes.get(os.path.basename(it["path"].replace("\\", "/")).lower())
                rec = c.file(it["ticket"], it["path"], it["day"], "claude_session", it.get("session") or "",
                             content=it.get("content"), purpose=pp[1] if pp and pp[0] == it["ticket"] else "")
                if rec:
                    self._new_artifact_note(it["day"], it["ticket"], rec)
                    known = self.state.setdefault("artifact_sources", {})
                    known[key] = {"ticket": it["ticket"], "seen": common.now_iso()}
                del q[key]
            except Exception:
                common.log("agent", "artifact failed %s: %s" % (it.get("path"), traceback.format_exc()))
                del q[key]
        lq = self.state.get("link_queue") or {}
        for url, it in list(lq.items()):
            t = it.get("ticket")
            if t:
                rec = c.link(t, url, it.get("title"), it["day"], it.get("session") or "", it.get("file"))
                if rec:
                    self._new_artifact_note(it["day"], t, rec)
            else:
                self._unfiled(it["day"], url, "published link")
            del lq[url]
        self._untracked_scan()
        self._watched()
        self._ticket_folders()
        self._recopy()

    def _new_artifact_note(self, day, t, rec):
        self.day_note(day, t, "new_artifacts", "%s — %s" % (rec.get("name"), rec.get("storage")))
        self.extras["artifacts"] += 1

    def _unfiled(self, day, path, source):
        dd = self.store.day(day)
        lst = dd.setdefault("unfiled_artifacts", [])
        if not any(x.get("path") == path for x in lst):
            lst.append({"path": path.replace("\\", "/"), "source": source, "time": common.hhmm(common.now())})
            self.store.touch_day(day)

    def _untracked(self, repo, path):
        try:
            return common.norm_path(path) in {common.norm_path(p) for p in artmod.untracked(self.cfg, repo)}
        except Exception:
            return False

    def _untracked_scan(self):
        """Untracked, non-ignored files modified during a session that has gone idle or ended."""
        done = self.state.setdefault("untracked_scanned", {})
        idle = int((self.cfg.get("extract_triggers") or {}).get("idle_minutes", 10))
        now = common.now()
        for sid, s in (self.state.get("sessions") or {}).items():
            repo = s.get("repo_root")
            last = common.parse_ts(s.get("last_event"))
            if not repo or not last or done.get(sid) == s.get("last_event"):
                continue
            if not (s.get("ended") or (now - last).total_seconds() >= idle * 60):
                continue
            done[sid] = s.get("last_event")
            first = common.parse_ts(s.get("first")) or last
            t = s.get("sticky") or common.ticket_from_branch(s.get("branch"), self.prefixes)
            try:
                files = artmod.untracked(self.cfg, repo)
            except Exception:
                files = []
            for p in files:
                if artmod.excluded(p, self.cfg):
                    continue
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if first.timestamp() - 60 <= m <= last.timestamp() + idle * 60:
                    day = common.workday(last, self.cfg)
                    if t:
                        rec = self.collector.file(t, p, day, "untracked file", sid)
                        if rec:
                            self._new_artifact_note(day, t, rec)
                    else:
                        self._unfiled(day, p, "untracked in " + os.path.basename(repo))
        for k in list(done):
            if k not in (self.state.get("sessions") or {}):
                del done[k]

    def _watched(self):
        acfg = self.cfg.get("artifacts") or {}
        roots = [(common.hp("_drop"), True)] + [(d, False) for d in acfg.get("watch_dirs") or []]
        seen = self.state.setdefault("watched_files", {})
        common.ensure_dir(common.hp("_drop"))
        for root, move in roots:
            if not os.path.isdir(root):
                continue
            for dp, dn, fns in os.walk(root):
                dn[:] = [d for d in dn if d.lower() not in artmod.SKIP_PARTS and not d.startswith(".")]
                for fn in fns:
                    p = os.path.join(dp, fn)
                    if fn.startswith((".", "~$")) or os.path.splitext(fn)[1].lower() not in artmod.ALL_EXT:
                        continue
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    key = common.norm_path(p)
                    sig = "%d:%d" % (st.st_size, int(st.st_mtime))
                    if seen.get(key) == sig and not move:
                        continue
                    if move and time.time() - st.st_mtime < 5:
                        continue  # still being written
                    found = common.find_tickets(fn.replace("_", " ").replace(".", " "), self.prefixes) or \
                        common.find_tickets(os.path.relpath(p, root).replace("\\", "/"), self.prefixes)
                    t = found[0] if found else self.a.active_ticket_within(common.now_iso(), 2 * 3600)
                    day = common.workday(common.now(), self.cfg)
                    if not t:
                        if seen.get(key) != sig:
                            self._unfiled(day, p, "_drop" if move else "watched folder " + root)
                        seen[key] = sig
                        continue
                    rec = self.collector.file(t, p, day, "_drop" if move else "watched folder", "", move=move, origin=p)
                    if rec:
                        self._new_artifact_note(day, t, rec)
                    seen[key] = sig
        for k in list(seen):
            if not os.path.exists(k):
                del seen[k]

    def _ticket_folders(self):
        """Files you put straight into tickets/<T>/ are indexed where they are."""
        root = common.hp("tickets")
        if not os.path.isdir(root):
            return
        day = common.workday(common.now(), self.cfg)
        for t in os.listdir(root):
            d = os.path.join(root, t)
            if not os.path.isdir(d) or not storemod.TICKET_RX.match(t):
                continue
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if os.path.isfile(p) and fn not in artmod.GENERATED_IN_TICKET and not fn.endswith(".tmp"):
                    before = (self.store.ticket(t).get("artifacts") or {}).get(common.stable_id(common.norm_path(p)))
                    rec = self.collector.note_in_folder(t, p, day)
                    if rec and rec is not before and (not before or before.get("sha") != rec.get("sha")):
                        self._new_artifact_note(day, t, rec)

    def _recopy(self):
        """Re-copy known artifact sources when they change (last 14 days)."""
        known = self.state.setdefault("artifact_sources", {})
        cutoff = common.now().timestamp() - 14 * 86400
        for key, info in list(known.items()):
            ts = common.parse_ts(info.get("seen"))
            if not ts or ts.timestamp() < cutoff or artmod.excluded(key, self.cfg):
                del known[key]
                continue
            if os.path.isfile(key):
                rec = self.collector.file(info["ticket"], key, common.workday(common.now(), self.cfg), "claude_session")
                if rec and rec.get("last") == common.workday(common.now(), self.cfg) and rec.get("versions", 1) > 1 and \
                        rec.get("sha") != info.get("sha"):
                    info["sha"] = rec.get("sha")

    # ------------------------------------------------------------------ replay
    def bruno_changes(self):
        """Tickets whose bruno/<T>/ folder changed on disk (my edits, adds, deletes)."""
        root = replay.bruno_root()
        mt = self.state.setdefault("bruno_folder_sig", {})
        out = []
        if not os.path.isdir(root):
            return out
        for t in os.listdir(root):
            d = os.path.join(root, t)
            if not os.path.isdir(d) or not storemod.TICKET_RX.match(t):
                continue
            sig = folder_sig(d)
            if mt.get(t) != sig:
                out.append(t)
                mt[t] = sig
        return out

    def curate_and_generate(self, force=False):
        requests_on = configstore.on(self.cfg, "requests.enabled")  # off: no curation and no Bruno files; DB flow still runs
        todo = set(self.bruno_changes()) if requests_on else set()
        last_seen = {}
        for iso, t in self.state.get("recent_tickets", []):
            if iso and (t not in last_seen or iso > last_seen[t]):
                last_seen[t] = iso
        for t in storemod.Store.all_ticket_ids() + list(self.store.tickets):
            tk = self.store.ticket(t)
            rp = tk.get("replay") or {}
            if requests_on and rp.get("dirty") and replay.curation_due(tk, self.cfg, last_seen.get(t), force or rp.get("force")):
                try:
                    cost = replay.curate(self.store, t, self.cfg)
                    self.a._add_cost(common.workday(common.now(), self.cfg), cost)
                    rp.pop("force", None)
                    todo.add(t)
                except Exception:
                    common.log("agent", "curation crashed for %s: %s" % (t, traceback.format_exc()))
        for t in storemod.Store.all_ticket_ids() + list(self.store.tickets):
            tk = self.store.ticket(t)
            if dbflow.consolidation_due(tk, self.cfg, last_seen.get(t), force):
                try:
                    cost = dbflow.consolidate(self.store, t, self.cfg)
                    self.a._add_cost(common.workday(common.now(), self.cfg), cost)
                    self.day_note(common.workday(common.now(), self.cfg), t, "dbflow_updates", "DB flow map consolidated")
                except Exception:
                    common.log("agent", "db flow consolidation crashed for %s: %s" % (t, traceback.format_exc()))
        for t in sorted(todo):
            if not storemod.TICKET_RX.match(t):
                continue
            try:
                changed, events = replay.generate(self.store, t, self.cfg)
                self.generated.add(t)
                d = os.path.join(replay.bruno_root(), t)
                if os.path.isdir(d):  # our own writes are not "your edits"
                    self.state.setdefault("bruno_folder_sig", {})[t] = folder_sig(d)
                if any(p.endswith(".bru") for p in changed):
                    n = len([p for p in changed if p.endswith(".bru") and not p.endswith("folder.bru")])
                    self.extras["requests"] += n
                day = common.workday(common.now(), self.cfg)
                for e in events:
                    self.day_note(day, t, "requests_changed", e)
            except Exception:
                common.log("agent", "replay generation failed for %s: %s" % (t, traceback.format_exc()))

    def commit_extras(self):
        bits = []
        if self.extras["requests"]:
            bits.append("+%d request%s" % (self.extras["requests"], "" if self.extras["requests"] == 1 else "s"))
        if self.extras["artifacts"]:
            bits.append("+%d artifact%s" % (self.extras["artifacts"], "" if self.extras["artifacts"] == 1 else "s"))
        if self.extras["dbflow"]:
            bits.append("DB flow")
        return " ".join(bits)

    # ------------------------------------------------------------------ backfill
    def backfill(self, sids=None, llm=True, log=None, workers=4):
        """Replay retained raw logs (+ transcripts for HTTP outcomes and written contents) through the new pipeline."""
        from . import agent as agentmod
        from . import sources
        done = self.state.setdefault("upgrade01_backfill", {})
        cost = 0.0
        jobs, pending_sids = [], []
        log = log or (lambda m: print(m, flush=True))
        files = agentmod.raw_files()
        by_sid = {}
        for day, sid, path in files:
            by_sid.setdefault(sid, []).append((day, path))
        for sid, parts in sorted(by_sid.items()):
            if sids and sid not in sids:
                continue
            if done.get(sid) == "done":
                continue
            evs = []
            for day, path in parts:
                e, _ = agentmod.read_lines(path, 0)
                evs += e
            evs = agentmod.attribute(evs, self.prefixes)
            evs.sort(key=lambda e: e.get("ts") or "")
            if not evs:
                done[sid] = "done"
                continue
            s = (self.state.get("sessions") or {}).get(sid) or {}
            tp = s.get("transcript_path") or next((e.get("transcript_path") for e in evs if e.get("transcript_path")), None)
            enriched = transcript_tool_events(tp, evs) if tp and os.path.isfile(tp) else []
            sess = {"first": evs[0].get("ts")}
            # deterministic: calls + artifacts (+ blocks/links in messages)
            stream = [e for e in evs if e.get("event") != "post-tool"] + enriched if enriched else evs
            stream.sort(key=lambda e: e.get("ts") or "")
            for ev in stream:
                day = common.workday(ev.get("ts") or common.now_iso(), self.cfg)
                try:
                    self._on_event(day, sid, sess, ev)
                except Exception:
                    common.log("agent", "backfill event failed: " + traceback.format_exc())
            # LLM jobs (run below, several at a time): only the new arrays
            cands = []
            for e in stream:
                if e.get("_t") and e["_t"] not in cands:
                    cands.append(e["_t"])
            # sessions not yet extracted get the new fields from their normal extraction (new schema) - no double call
            if llm and cands and not s.get("pending_chars"):
                for day in sorted({common.workday(e.get("ts"), self.cfg) for e in stream if e.get("ts")}):
                    devs = [e for e in stream if common.workday(e.get("ts") or "", self.cfg) == day]
                    if not any(e.get("event") in ("post-tool", "stop") for e in devs):
                        continue
                    meta = {"session": sid, "day": day, "repo": s.get("repo_root"), "branch": s.get("branch")}
                    already = {t: {"title": self.store.ticket(t).get("title", "")} for t in cands}
                    ctx = {"day": day, "session": sid, "time": common.hhmm(devs[0].get("ts")), "repo": s.get("repo_root"),
                           "branch": s.get("branch"), "candidates": cands}
                    for chunk in extract.build_digests(devs, meta, cands, already):
                        jobs.append((sid, ctx, chunk))
            pending_sids.append(sid)
            log("captured %s (%s) tickets=%s enriched=%d" % (sid[:8], parts[0][0], cands, len(enriched)))
        # model calls in parallel, merged in a fixed order so the result is deterministic
        from concurrent.futures import ThreadPoolExecutor

        def call(job):
            try:
                return extract.call_claude("extract_delta", job[2], self.cfg), None
            except extract.ExtractError as e:
                return None, e
        failed = set()
        log("%d extraction calls, %d at a time" % (len(jobs), workers))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(call, jobs))
        for (sid, ctx, _), (r, err) in zip(jobs, results):
            if err is not None:
                failed.add(sid)
                log("  extraction failed for %s: %s" % (sid[:8], err))
                continue
            res, c = r
            cost += c
            self.after_extraction(res, ctx)
        for sid in pending_sids:
            if sid not in failed:
                done[sid] = "done"
        log("backfill: %d sessions, %d calls, %d failed, cost $%.2f" % (len(pending_sids), len(jobs), len(failed), cost))
        return cost


def folder_sig(d):
    sig = []
    for fn in sorted(os.listdir(d)):
        try:
            st = os.stat(os.path.join(d, fn))
            sig.append("%s:%d:%d" % (fn, st.st_size, int(st.st_mtime)))
        except OSError:
            pass
    return hashlib.sha1("|".join(sig).encode()).hexdigest()


def artmod_links(text):
    from . import capture
    return capture.artifact_links(text or "")


def transcript_tool_events(path, raw_events):
    """Rebuild post-tool events from a transcript with the new capture rules (HTTP outcome, file contents)."""
    from . import capture
    from . import hook
    out = []
    pending = {}
    first = raw_events[0] if raw_events else {}
    try:
        with open(path, "rb") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for raw in lines:
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if rec.get("type") == "assistant" and b.get("type") == "tool_use":
                pending[b.get("id")] = (b.get("name") or "", b.get("input") or {}, rec.get("timestamp"), rec.get("cwd"), rec.get("gitBranch"))
            elif rec.get("type") == "user" and b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                name, ti, ts, cwd, br = pending.pop(b["tool_use_id"])
                if name not in ("Bash", "PowerShell", "Write", "Edit", "MultiEdit", "Artifact"):
                    continue
                cwd = cwd or first.get("cwd")
                repo = common.find_repo_root(cwd) if cwd else None
                ki = hook._key_input(name, ti, common, redact, repo)
                resp = b.get("content")
                if isinstance(resp, list):
                    resp = "\n".join(x.get("text", "") for x in resp if isinstance(x, dict))
                try:
                    oc = hook._outcome(name, ti, resp, cwd, redact)
                except Exception:
                    oc = {}
                tsx = common.parse_ts(ts)
                ev = {"ts": tsx.isoformat(timespec="seconds") if tsx else first.get("ts"), "event": "post-tool",
                      "session_id": first.get("session_id"), "source": "transcript", "cwd": cwd, "repo_root": repo,
                      "branch": br or first.get("branch"), "ticket": None, "payload": {"tool": name, "input": ki}}
                if oc:
                    ev["payload"]["outcome"] = oc
                out.append(ev)
    # ticket of the nearest raw event (same session)
    stamps = [(common.parse_ts(e.get("ts")), e.get("_t")) for e in raw_events if e.get("ts")]
    for ev in out:
        t0 = common.parse_ts(ev["ts"])
        best = None
        for ts, t in stamps:
            if ts and t and t0 and (best is None or abs((ts - t0).total_seconds()) < best[0]):
                best = (abs((ts - t0).total_seconds()), t)
        ev["_t"] = best[1] if best else None
    return out
