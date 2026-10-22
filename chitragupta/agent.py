"""Chitragupta agent. Run by the hidden scheduled task "Chitragupta" (at logon + every 5 min) through the launcher.

    agent tick [--force]   one pass: ingest -> extract -> sources -> close days -> render -> commit -> push
    agent loop             fallback launcher: tick every 5 minutes forever (single instance via locks/loop.lock)
    agent render [--all]   re-render markdown from data/
    agent status           print health.json
"""
import copy
import datetime as _dt
import json
import os
import re
import sys
import time
import traceback

from . import branches  # noqa: E402
from . import common  # noqa: E402
from . import configstore  # noqa: E402
from . import delivery  # noqa: E402
from . import gittrack  # noqa: E402
from . import extract  # noqa: E402
from . import features as featmod  # noqa: E402
from . import gitsync  # noqa: E402
from . import modelbudget  # noqa: E402
from . import render  # noqa: E402
from . import sources  # noqa: E402
from . import kbnorm  # noqa: E402
from . import sqlsent  # noqa: E402
from . import supersede  # noqa: E402
from . import store as storemod  # noqa: E402
from . import timesheet  # noqa: E402
from .redact import redact  # noqa: E402
from .store import Store  # noqa: E402
from .safety import paths  # noqa: E402

LOCK_STALE_S = 30 * 60
LOCK_WAIT_S = 300  # how long CLI commands that change state wait for a running tick
LOCK_POLL_S = 10
LOOP_INTERVAL_S = 300
BLOCK_GAP_MIN = 20  # the old block model (blocks_minutes), kept for reference
time_params = timesheet.time_params  # (turn tail, idle) minutes of the time model


# --------------------------------------------------------------------------- lock

class Lock:
    def __init__(self, name="agent.lock"):
        self.path = common.wl("locks", name)
        self.fd = None

    def acquire(self):
        common.ensure_dir(os.path.dirname(self.path))
        for _ in range(2):
            try:
                self.fd = paths.create_exclusive(self.path, ("%d %s" % (os.getpid(), common.now_iso())).encode())
                return True
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > LOCK_STALE_S:
                        paths.delete(self.path)
                        continue
                except OSError:
                    pass
                return False
        return False

    def acquire_wait(self, wait_s=None, poll_s=None):
        """acquire(), retrying for up to wait_s seconds (default LOCK_WAIT_S) while a tick holds the lock."""
        wait_s = LOCK_WAIT_S if wait_s is None else wait_s
        poll_s = LOCK_POLL_S if poll_s is None else poll_s
        deadline = time.time() + wait_s
        while True:
            if self.acquire():
                return True
            left = deadline - time.time()
            if left <= 0:
                return False
            time.sleep(max(0.01, min(poll_s, left)))

    def refresh(self):
        """Keep a long-held lock from looking stale."""
        try:
            paths.touch(self.path)
        except OSError:
            pass

    def release(self):
        try:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            paths.delete(self.path)
        except OSError:
            pass


# --------------------------------------------------------------------------- raw reading

def read_lines(path, start, end=None):
    """Complete JSON lines of a raw file between byte offsets, each with its 1-based line number in the file (`_ln`,
    for evidence refs "raw/<day>/<sid>.jsonl:<line>"). Returns (events, new_offset)."""
    try:
        with open(path, "rb") as f:
            ln = f.read(start).count(b"\n") if start else 0
            f.seek(start)
            data = f.read() if end is None else f.read(max(0, end - start))
    except OSError:
        return [], start
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], start
    evs = []
    for raw in data[: cut + 1].split(b"\n")[:-1]:
        ln += 1
        try:
            ev = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(ev, dict):
                ev["_ln"] = ln
                evs.append(ev)
        except ValueError:
            continue
    return evs, start + cut + 1


def raw_files():
    root = common.wl("raw")
    out = []
    try:
        days = sorted(os.listdir(root))
    except OSError:
        return out
    for d in days:
        dp = os.path.join(root, d)
        if not os.path.isdir(dp):
            continue
        for fn in sorted(os.listdir(dp)):
            if fn.endswith(".jsonl"):
                out.append((d, fn[:-6], os.path.join(dp, fn)))
    return out


def raw_key(day, sid):
    """The state key of a raw file (state.sessions.<sid>.files): "<day>/<sid>.jsonl", relative to .worklog/raw, so a
    copied or moved data home keeps its read and extracted offsets."""
    return "%s/%s.jsonl" % (day, sid)


def raw_path(key):
    return common.wl("raw", *key.split("/"))


def raw_day(key):
    return key.split("/")[0]


_ABS_RAW_KEY = re.compile(r"(?:^|[\\/])\.worklog[\\/]raw[\\/](\d{4}-\d{2}-\d{2})[\\/]([^\\/]+\.jsonl)$", re.I)


def migrate_raw_keys(dry_run, notes, backup):
    """Migration "raw-relative-keys": state.sessions.*.files keys written as absolute raw paths (any root, either
    separator) become "<day>/<sid>.jsonl"; two keys for the same file merge by their larger offsets."""
    spath = common.wl("state.json")
    st = common.read_json(spath, {})
    n = 0
    for s in (st.get("sessions") or {}).values() if isinstance(st, dict) else []:
        files = s.get("files") if isinstance(s, dict) else None
        if not isinstance(files, dict):
            continue
        new = {}
        for k, f in files.items():
            m = _ABS_RAW_KEY.search(k)
            nk = "%s/%s" % (m.group(1), m.group(2)) if m else k
            n += nk != k
            if nk in new and isinstance(f, dict):
                merged = dict(new[nk], **{x: max(int(new[nk].get(x) or 0), int(f.get(x) or 0)) for x in ("read", "extracted")})
                new[nk] = merged
            else:
                new[nk] = f
        s["files"] = new
    if not n:
        notes.append("state keys already relative")
        return
    backup([spath])
    notes.append("%d raw file key(s) made relative to .worklog/raw" % n)
    if not dry_run:
        common.write_json(spath, st)


def time_only_sids():
    """Session ids with time-only events (raw/<day>/time/<session>.jsonl)."""
    root = common.wl("raw")
    out = set()
    try:
        days = os.listdir(root)
    except OSError:
        return out
    for d in days:
        try:
            names = os.listdir(os.path.join(root, d, "time"))
        except OSError:
            continue
        out |= {fn[:-6] for fn in names if fn.endswith(".jsonl")}
    return out


def attribute(events, prefixes, cfg=None):
    """Ticket per event, decided per turn (a user prompt and every event of that session until its next prompt) by
    common.ticket_for_turn: the prompt's ticket -> the branch of the session's own repo (its first repo; a work branch
    only) -> the last ticket named earlier in the session. Sets ev["_t"] and ev["_src"]; returns `events` (same order).
    A parent issue (an Epic…) is never an event's ticket."""
    cfg = dict(cfg if cfg is not None else common.load_config(), ticket_prefixes=list(prefixes or []))
    turn, carry, own, own_branch, memo = {}, {}, {}, {}, {}
    for ev in sorted(events, key=lambda e: e.get("ts") or ""):
        sid = ev.get("session_id")
        repo = ev.get("repo_root")
        if repo and sid not in own:
            own[sid] = repo
        if ev.get("branch") and (not repo or common.norm_path(repo) == common.norm_path(own.get(sid))):
            own_branch[sid] = ev["branch"]
        if ev.get("event") == "user-prompt":
            if ev.get("_time"):  # time-only: the ticket its prompt named, no text
                t, src = (ev.get("ticket"), "prompt") if ev.get("ticket") else (None, None)
            else:
                t, src = common.prompt_ticket((ev.get("payload") or {}).get("prompt", ""), prefixes, cfg)
            turn[sid] = (t, src)
            if t:
                carry[sid] = t
        pt, ps = turn.get(sid) or (None, None)
        key = (pt, ps, own_branch.get(sid), own.get(sid), carry.get(sid))
        if key not in memo:
            memo[key] = common.ticket_for_turn(cfg, pt, ps, own_branch.get(sid), None, own.get(sid), carry.get(sid))
        ev["_t"], ev["_src"] = memo[key]
    return events


def fallback_keys(dd, sid):
    """The day's "AI summary pending" keys of one session (see Agent.set_fallback)."""
    return [k for k in dd.get("fallback") or {} if k == sid or k.startswith(sid + "|")]


def blocks_minutes(stamps):
    """Sum of activity blocks (gaps <= 20 min) in minutes; each block gets +1 min."""
    ts = sorted(set(stamps))
    if not ts:
        return 0
    total, start, prev = 0.0, ts[0], ts[0]
    for t in ts[1:]:
        if (t - prev).total_seconds() > BLOCK_GAP_MIN * 60:
            total += (prev - start).total_seconds() / 60 + 1
            start = t
        prev = t
    total += (prev - start).total_seconds() / 60 + 1
    return int(round(total))


# --------------------------------------------------------------------------- agent

class Agent:
    def __init__(self, force=False):
        self.force = force
        self.cfg = common.load_config()
        self.cfg_base = copy.deepcopy(self.cfg)  # what changed since load is all a save writes (see save_config)
        self.state = common.read_json(common.wl("state.json"))
        self.health = common.read_json(common.wl("health.json"))
        self.store = Store()
        self.session_ended = False
        self.day_closed = False
        self.extras = []  # commit-message extras
        self.cfg_dirty = False
        self.git_events = []  # post-tool hook events, for branch tracking (git commands, PR links)
        self.git_force = set()  # tickets whose branch picture must be recomputed (manual inbox decisions)
        self.features = featmod.Features(self)
        from . import jiraworklog
        from . import kbjobs
        self.jwl = jiraworklog.JiraWorklog(self)
        self.jobs = kbjobs.KBJobs(self)

    @property
    def prefixes(self):
        return self.cfg.get("ticket_prefixes") or []

    def ensure_tslot(self, day, t, repo=None, branch=None):
        storemod.touch_ticket_meta(self.store, t, day, repo, branch)
        dd = self.store.day(day)
        return dd["tickets"].setdefault(t, {"minutes": 0, "item_ids": [], "query_ids": [], "api_keys": [], "table_keys": [],
                                            "integration_keys": [], "files_changed": [], "what_i_did": []})

    # ------------------------------------------------------------------ safety net
    def safety_net(self, paused):
        tstate = self.state.setdefault("transcripts", {})
        recent = sources.list_recent_transcripts(48)
        first_run = not self.state.get("transcripts_baselined")
        hook_sids = {sid for sid, s in self.state.get("sessions", {}).items() if s.get("source") == "hook"}
        hook_sids |= {sid for _, sid, _ in raw_files()}
        hook_sids |= time_only_sids()  # data-home sessions the hooks recorded time-only
        sandbox = common.wl("sandbox")
        modes = {}
        for path, size, mtime in recent:
            key = common.norm_path(path)
            sid = os.path.splitext(os.path.basename(path))[0]
            st = tstate.get(key)
            if first_run or paused:
                tstate[key] = {"offset": size, "sid": sid}
                continue
            if st is None:
                st = tstate[key] = {"offset": 0, "sid": sid}
            if sid in hook_sids and not st.get("captured"):
                st["offset"] = size
                continue
            if size <= st["offset"]:
                continue
            # don't race the hooks: give a brand-new transcript 3 minutes for its SessionStart hook to land
            if not st.get("captured") and time.time() - mtime < 180 and st["offset"] == 0:
                continue
            events, new_off, meta = sources.transcript_events(path, st["offset"])
            cwd = meta.get("cwd") or st.get("cwd")
            st["cwd"] = cwd
            st["offset"] = new_off
            full = timed = 0
            turn, carry, own_repo = (None, None), None, None
            for e in events:
                ecwd = e.get("cwd") or cwd
                k = common.norm_path(ecwd)
                if k not in modes:
                    modes[k] = self._net_mode(ecwd, sandbox)
                mode, repo_root = modes[k]
                if not mode:
                    continue
                ts = common.parse_ts(e.get("ts")) or common.now()
                day = common.workday(ts, self.cfg)
                if mode == "time":  # like the hook: timestamp + the ticket mentioned, never content
                    if e["event"] not in ("user-prompt", "post-tool", "stop"):
                        continue
                    t = common.prompt_ticket((e.get("payload") or {}).get("prompt", ""), self.prefixes, self.cfg)[0] \
                        if e["event"] == "user-prompt" else None
                    row = {"ts": ts.isoformat(timespec="seconds"), "event": e["event"], "session_id": sid,
                           "source": "time-only", "ticket": t}
                    if common.is_home_folder(ecwd):
                        row["where"] = "home"
                    common.append_line(common.wl("raw", day, "time", sid + ".jsonl"), json.dumps(row))
                    timed += 1
                    continue
                ev = {"ts": ts.isoformat(timespec="seconds"), "event": e["event"], "session_id": sid, "source": "transcript",
                      "transcript_path": path, "cwd": ecwd, "repo_root": repo_root,
                      "branch": e.get("branch") or meta.get("branch"), "ticket": None, "agent_type": None,
                      "payload": e["payload"]}
                if e["event"] == "user-prompt":  # a turn starts: its prompt's ticket (common.ticket_for_turn)
                    turn = common.prompt_ticket((e.get("payload") or {}).get("prompt", ""), self.prefixes, self.cfg)
                    carry = turn[0] or carry
                own_repo = own_repo or repo_root
                ev["ticket"], ev["ticket_src"] = common.ticket_for_turn(self.cfg, turn[0], turn[1], ev["branch"], repo_root,
                                                                        own_repo, carry)
                common.append_line(common.wl("raw", day, sid + ".jsonl"), json.dumps(ev, ensure_ascii=False))
                full += 1
            if full or timed:
                st["captured"] = True
            if full:
                days = self.state.setdefault("safety_net_days", [])
                d = common.workday(common.now(), self.cfg)
                if d not in days:
                    days.append(d)
                    self.state["safety_net_days"] = days[-10:]
        self.state["transcripts_baselined"] = True
        # forget transcripts not touched in 7 days
        cutoff = common.now().timestamp() - 7 * 86400
        live = {common.norm_path(p) for p, _, _ in recent}
        for k in list(tstate):
            if k not in live:
                try:
                    if os.path.getmtime(k) < cutoff:
                        del tstate[k]
                except OSError:
                    del tstate[k]

    def _net_mode(self, cwd, sandbox):
        """(mode, repo root) for a transcript folder: the hook's rules (common.capture_mode), and as a fallback that
        cannot see who started a session, "full" only for work in a git repo or a configured include path."""
        if not cwd or common.path_within(cwd, sandbox):
            return None, None
        mode = common.capture_mode(cwd, self.cfg)
        if mode != "full":
            return mode, None
        repo_root = common.find_repo_root(cwd)
        inc = (self.cfg.get("capture_scope") or {}).get("include_paths") or []
        if not repo_root and not any(common.path_within(cwd, p) for p in inc):
            return None, None  # a plain folder, not a repo: who started the session is unknown
        return "full", repo_root

    # ------------------------------------------------------------------ ingest
    def ingest(self):
        sessions = self.state.setdefault("sessions", {})
        touched_days = set()
        recent = self.state.setdefault("recent_tickets", [])
        for day, sid, path in raw_files():
            s = sessions.setdefault(sid, {"files": {}, "pending_chars": 0, "first": None, "days": []})
            f = s["files"].setdefault(raw_key(day, sid), {"read": 0, "extracted": 0})
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size <= f["read"]:
                continue
            evs, new_off = self._read_turns(path, f["read"])
            f["read"] = new_off
            if not evs:
                continue
            touched_days.add(day)
            if day not in s["days"]:
                s["days"].append(day)
            try:
                self.features.on_events(day, sid, s, [dict(e) for e in evs if not e.get("_time")])
            except Exception:
                common.log("agent", "features failed: " + traceback.format_exc())
            for ev in evs:
                s["source"] = s.get("source") or ev.get("source")
                if ev.get("source") == "hook":
                    s["source"] = "hook"
                    self.health["last_hook_event"] = ev.get("ts")
                s["first"] = s.get("first") or ev.get("ts")
                s["last_event"] = ev.get("ts")
                for k in ("repo_root", "branch", "cwd", "transcript_path"):
                    if ev.get(k):
                        s[k] = ev[k]
                if ev.get("event") == "session-end":
                    s["ended"] = True
                    self.session_ended = True
                elif ev.get("event") in ("session-start", "user-prompt"):
                    s["ended"] = False
                s["pending_chars"] = s.get("pending_chars", 0) + extract.event_chars(ev)
                if ev.get("event") == "post-tool" and ev.get("repo_root"):
                    self.git_events.append(ev)
                self._learn(ev)
                t = ev.get("_t")  # the turn's ticket (common.ticket_for_turn)
                if t and not common.is_placeholder(t):  # raw lines written before a key was a placeholder
                    recent.append([ev.get("ts"), t])
            dd = self.store.day(day)
            meta = dd["sessions"].setdefault(sid, {})
            meta.update({"repo": os.path.basename((s.get("repo_root") or "").rstrip("/\\")), "branch": s.get("branch"),
                         "first": meta.get("first") or s.get("first"), "last": s.get("last_event"),
                         "source": s.get("source"), "extracted": meta.get("extracted", False)})
            self._apply_carry(day)
            self.store.touch_day(day)
        self.state["recent_tickets"] = recent[-400:]
        for d in touched_days:
            self.recompute_minutes(d)
        return touched_days

    def _learn(self, ev):
        """Auto-add new ticket prefixes (from branch names) and repos seen in sessions, only from captured work: an
        event whose folder is outside the capture scope, ignored, under the system temp folder or inside
        Chitragupta's own folders teaches nothing (common.learnable_path)."""
        repo = ev.get("repo_root")
        where = repo or ev.get("cwd")
        if not where or not self._learnable(where):
            return
        p = common.new_branch_prefix(ev.get("branch"), self.prefixes)
        if p:
            self.cfg["ticket_prefixes"] = sorted(set(self.prefixes) | {p})
            self.cfg_dirty = True
            common.log("agent", "learned ticket prefix %s from branch %s" % (p, ev.get("branch")))
        if repo:
            norm = [common.norm_path(r) for r in self.cfg.get("watched_repos") or []]
            if common.norm_path(repo) not in norm and os.path.exists(os.path.join(repo, ".git")):
                self.cfg.setdefault("watched_repos", []).append(repo.replace("\\", "/"))
                self.cfg_dirty = True
                common.log("agent", "watching new repo %s" % repo)

    def _read_turns(self, path, start, end=None, keep_time=True):
        """Captured events of a raw file between byte offsets, each with its turn's ticket (_t, _src). The file's
        earlier events are read as context, so a turn that began before `start` keeps its prompt's ticket.
        Returns (events, new_offset)."""
        before = self._captured(read_lines(path, 0, start)[0], keep_time) if start else []
        evs, off = read_lines(path, start, end)
        evs = self._captured(evs, keep_time)
        return attribute(before + evs, self.prefixes, self.cfg)[len(before):], off

    def _captured(self, evs, keep_time=True):
        """Raw events under today's capture rules (common.capture_mode, memoized per folder for this run): events
        whose folder is now ignored or out of scope are dropped; events from a now time-only folder keep only the
        timestamp and the ticket their prompt named (keep_time=False drops them too, for extraction and fallbacks).
        Rules only ever narrow what was written, so a removed marker simply lets the events back in."""
        memo = self.__dict__.setdefault("_capture_memo", {})
        out = []
        for ev in evs:
            where = ev.get("cwd") or ev.get("repo_root")
            if ev.get("_time") or not where:
                if keep_time or not ev.get("_time"):
                    out.append(ev)
                continue
            key = common.norm_path(where)
            if key not in memo:
                memo[key] = common.capture_mode(where, self.cfg)
            mode = memo[key]
            if mode == "full":
                out.append(ev)
            elif mode == "time" and keep_time:
                t = None
                if ev.get("event") == "user-prompt":
                    t = common.prompt_ticket((ev.get("payload") or {}).get("prompt", ""), self.prefixes, self.cfg)[0]
                out.append({"ts": ev.get("ts"), "event": ev.get("event"), "session_id": ev.get("session_id"),
                            "source": ev.get("source"), "ticket": t, "_time": True})
        return out

    def _learnable(self, path):
        memo = self.__dict__.setdefault("_learnable_memo", {})
        key = common.norm_path(path)
        if key not in memo:
            memo[key] = common.learnable_path(path, self.cfg)
        return memo[key]

    def _apply_carry(self, day):
        carry = self.state.get("carry_todos")
        if carry and carry.get("from", "") < day:
            dd = self.store.day(day)
            if not dd.get("carried_todos"):
                dd["carried_todos"] = carry.get("todos") or []
            self.state.pop("carry_todos", None)

    def day_events(self, day):
        evs = []
        dp = common.wl("raw", day)
        try:
            names = os.listdir(dp)
        except OSError:
            return evs
        for fn in names:
            if fn.endswith(".jsonl"):
                e, _ = read_lines(os.path.join(dp, fn), 0)
                evs += e
        return attribute(self._captured(evs), self.prefixes, self.cfg)

    def recompute_minutes(self, day):
        """Minutes per ticket (and per no-ticket repo/branch group) of a work day, from its raw events: turn intervals
        with a short tail (timesheet.active_intervals; session-start/end are not activity), unioned across sessions,
        overlapping time split evenly between the tickets active then (timesheet.union_minutes)."""
        evs = self.day_events(day)
        if not evs:
            return
        dd = self.store.day(day)
        per_t, where = {}, {}
        for ev in evs:
            ts = common.parse_ts(ev.get("ts"))
            t = ev.get("_t")
            if ts and t and ev.get("event") not in ("session-start", "session-end"):
                per_t.setdefault(t, []).append(ts)
                where.setdefault(t, set()).add(storemod.group_key(ev.get("repo_root"), ev.get("branch")))

        def key(e):  # a turn without a ticket counts for its repo/branch group (never for time-only rows)
            if e.get("_t") or e.get("_time"):
                return e.get("_t")
            return ("other", storemod.group_key(e.get("repo_root"), e.get("branch")))
        tail, idle = time_params(self.cfg)
        mins = timesheet.union_minutes(timesheet.active_intervals(evs, tail, idle, key=key))
        for t, slot in dd["tickets"].items():  # recomputed from raw: a ticket that lost its turns loses its time
            if t not in per_t and slot.get("minutes"):
                slot["minutes"] = 0
                tl = self.store.ticket(t)["timeline"].get(day)
                if tl:
                    tl["minutes"] = 0
                self.store.touch_ticket(t)
        for t, stamps in per_t.items():
            first = min(stamps)
            repo = next((e.get("repo_root") for e in evs if e.get("_t") == t and e.get("repo_root")), None)
            branch = next((e.get("branch") for e in evs if e.get("_t") == t and e.get("branch")), None)
            slot = self.ensure_tslot(day, t, repo, branch)
            slot["minutes"] = int(round(mins.get(t, 0)))
            slot["where"] = sorted(where.get(t, []))
            slot.setdefault("first_time", first.strftime("%H:%M"))
            tk = self.store.ticket(t)
            tl = tk["timeline"].setdefault(day, {"minutes": 0, "line": ""})
            tl["minutes"] = slot["minutes"]
            if not tl.get("line"):
                tl["line"] = "worked in " + ", ".join(slot["where"])
            self.store.touch_ticket(t)
        for k, m in mins.items():
            if isinstance(k, tuple) and m >= 1:
                slot = dd["other"].setdefault(k[1], {"minutes": 0, "items": {}, "queries": {}, "apis": {}, "tables": {},
                                                     "integrations": {}, "files_changed": [], "what_i_did": []})
                slot["minutes"] = int(round(m))
        self.store.touch_day(day)

    # ------------------------------------------------------------------ extraction
    def extraction_due(self, s, now):
        trig = self.cfg.get("extract_triggers") or {}
        if s.get("pending_chars", 0) <= 0:
            return False
        fail = s.get("fail") or {}
        if fail.get("next") and common.parse_ts(fail["next"]) > now and not self.force:
            return False
        if self.force:
            return True
        if s.get("pending_chars", 0) < trig.get("min_new_chars", 1500):
            return False
        last = common.parse_ts(s.get("last_event"))
        idle = (now - last).total_seconds() / 60 if last else 999
        le = common.parse_ts(s.get("last_extract")) or common.parse_ts(s.get("first"))
        since = (now - le).total_seconds() / 60 if le else 999
        return s.get("ended") or idle >= trig.get("idle_minutes", 10) or since >= trig.get("max_interval_minutes", 20)

    def run_extractions(self, only_day=None, any_size=False):
        now = common.now()
        gb = common.parse_ts(self.state.get("global_backoff_until"))
        if gb and gb > now and not self.force:
            return
        budget_s, t0 = 14 * 60, time.time()
        for sid, s in sorted(self.state.get("sessions", {}).items(), key=lambda kv: kv[1].get("last_event") or ""):
            if time.time() - t0 > budget_s:
                break
            due = self.extraction_due(s, now) or (any_size and s.get("pending_chars", 0) > 0 and
                                                   not (s.get("fail", {}).get("next") and common.parse_ts(s["fail"]["next"]) > now))
            if not due:
                continue
            if only_day and only_day not in s.get("days", []):
                continue
            ok = self.extract_session(sid, s)
            if not ok and self.state.get("global_backoff_until"):
                break

    def extract_session(self, sid, s):
        by_day = {}
        for key, f in sorted(s["files"].items()):
            if f["read"] > f["extracted"]:
                evs, _ = self._read_turns(raw_path(key), f["extracted"], f["read"], keep_time=False)
                for e in evs:
                    if e.get("_ln"):
                        e["_ref"] = "raw/%s:%d" % (key, e["_ln"])
                day = raw_day(key)
                by_day.setdefault(day, []).extend(evs)
        if not by_day:
            s["pending_chars"] = 0
            return True
        for day, evs in sorted(by_day.items()):
            evs.sort(key=lambda e: e.get("ts") or "")
            if not any(e.get("event") in ("user-prompt", "stop", "post-tool") for e in evs):
                continue
            enrich = extract.enrich_events(s.get("transcript_path"), evs) if s.get("source") == "hook" else []
            all_evs = sorted(evs + enrich, key=lambda e: e.get("ts") or "")
            cands = []
            for e in evs:
                if e.get("_t") and e["_t"] not in cands:
                    cands.append(e["_t"])
            last = evs[-1]
            ctx = {"day": day, "session": sid, "time": common.hhmm(evs[0].get("ts")), "repo": last.get("repo_root"),
                   "branch": last.get("branch"), "candidates": cands}
            ctx["edited"] = kbnorm.edited_files(evs)  # K6a: a file is "changed" only when a tool call wrote it
            ctx["sent"] = sqlsent.sent_statements(evs)  # K6b: a query counts as run only when one of these sent it
            already = storemod.already_recorded(self.store, cands)
            meta = {"session": sid, "day": day, "repo": ctx["repo"], "branch": ctx["branch"],
                    "lines": self.jwl.current_lines(day, cands, common.parse_ts(evs[0].get("ts")))}
            try:
                cost = 0.0
                for n, chunk in enumerate(extract.build_digests(all_evs, meta, cands, already)):
                    if n:  # K3: a later chunk sees what the earlier ones recorded
                        chunk = extract.with_already(chunk, storemod.already_recorded(self.store, cands))
                    res, c = extract.call_claude("extract", chunk, self.cfg)
                    cost += c
                    cctx = dict(ctx, default=chunk.default, turns=chunk.turns)  # untagged items: their turn's ticket
                    storemod.merge_extraction(self.store, res, cctx, self.prefixes)
                    self.features.after_extraction(res, cctx)
                    self.jwl.merge_extraction(day, res, evs)
                dd = self.store.day(day)
                dd["stats"]["cost_usd"] = round((dd["stats"].get("cost_usd") or 0) + cost, 4)
                dd["sessions"].setdefault(sid, {})["extracted"] = True
                for k in fallback_keys(dd, sid):
                    dd["fallback"].pop(k, None)
                self._add_cost(day, cost)
                self.store.touch_day(day)
                common.log("agent", "extracted %s day %s cands=%s cost=$%.4f" % (sid[:8], day, cands, cost))
            except extract.ExtractError as e:
                self._extraction_failed(sid, s, day, evs, ctx, str(e), e.limit)
                return False
            except Exception as e:  # never let one session wedge the agent
                self._extraction_failed(sid, s, day, evs, ctx, "unexpected: %r" % e, False)
                common.log("agent", traceback.format_exc())
                return False
        for f in s["files"].values():
            f["extracted"] = f["read"]
        s["pending_chars"] = 0
        s["last_extract"] = common.now_iso()
        s.pop("fail", None)
        return True

    def _extraction_failed(self, sid, s, day, evs, ctx, msg, limit):
        fail = s.setdefault("fail", {"n": 0})
        fail["n"] = fail.get("n", 0) + 1
        delay = min(5 * 2 ** (fail["n"] - 1), 360)
        fail["next"] = (common.now() + _dt.timedelta(minutes=delay)).isoformat(timespec="seconds")
        fail["error"] = msg[:300]
        if limit:
            self.state["global_backoff_until"] = (common.now() + _dt.timedelta(minutes=30)).isoformat(timespec="seconds")
        dd = self.store.day(day)
        dd["stats"]["errors"] = (dd["stats"].get("errors") or 0) + 1
        self.set_fallback(day, sid, evs, ctx, "extraction failed, retry %s" % common.hhmm(fail["next"]))
        self.health["errors_total"] = int(self.health.get("errors_total") or 0) + 1
        self.health["last_error"] = "%s extract %s: %s" % (common.now_iso(), sid[:8], msg[:200])
        common.log("agent", "extraction failed %s: %s" % (sid[:8], msg))

    def set_fallback(self, day, sid, evs, ctx, reason):
        """The "AI summary pending" entries of a session: one per turn ticket (key <sid>, or <sid>|<ticket> when the
        pending events span several tickets), so each ticket shows only its own turns' files, commands and SQL."""
        groups = {}
        for e in evs:
            groups.setdefault(e.get("_t"), []).append(e)
        recs = {}
        for t, gevs in groups.items():
            fb = extract.fallback_summary(gevs)
            if fb["files"] or fb["commands"] or fb["sql"]:
                recs[t] = dict(fb, session=sid, reason=reason, ticket=t,
                               group=storemod.group_key(ctx.get("repo"), ctx.get("branch")))
        new = {(sid if len(recs) == 1 else "%s|%s" % (sid, t or "-")): r for t, r in recs.items()}
        dd = self.store.day(day)
        old = {k: dd["fallback"][k] for k in fallback_keys(dd, sid)}
        if old != new:
            for k in old:
                dd["fallback"].pop(k, None)
            dd["fallback"].update(new)
            for t in recs:
                if t:
                    self.ensure_tslot(day, t, ctx.get("repo"), ctx.get("branch"))
            self.store.touch_day(day)

    def refresh_fallbacks(self):
        """Pending (not yet extracted) sessions show deterministic entries meanwhile."""
        for sid, s in self.state.get("sessions", {}).items():
            if s.get("pending_chars", 0) <= 0 or s.get("fail"):
                continue
            for key, f in s["files"].items():
                if f["read"] <= f["extracted"]:
                    continue
                day = raw_day(key)
                evs, _ = self._read_turns(raw_path(key), f["extracted"], f["read"], keep_time=False)
                if not evs:
                    continue
                cands = []
                for e in evs:
                    if e.get("_t") and e["_t"] not in cands:
                        cands.append(e["_t"])
                ctx = {"candidates": cands, "repo": evs[-1].get("repo_root"), "branch": evs[-1].get("branch")}
                fb = extract.fallback_summary(evs)
                if fb["files"] or fb["commands"] or fb["sql"]:
                    self.set_fallback(day, sid, evs, ctx, "waiting for extraction")

    def _add_cost(self, day, cost):
        cb = self.health.setdefault("cost_by_day", {})
        cb[day] = round((cb.get(day) or 0) + cost, 4)
        for k in sorted(cb)[:-30]:
            del cb[k]

    # ------------------------------------------------------------------ sources
    def active_ticket_at(self, ts):
        t0 = common.parse_ts(ts)
        if not t0:
            return None
        best = None
        for iso, t in self.state.get("recent_tickets", []):
            d = common.parse_ts(iso)
            if d and abs((t0 - d).total_seconds()) <= 30 * 60:
                if best is None or abs((t0 - d).total_seconds()) < best[0]:
                    best = (abs((t0 - d).total_seconds()), t)
        return best[1] if best else None

    def active_ticket_within(self, ts, seconds):
        """Most recent ticket active in Claude Code within `seconds` before ts."""
        t0 = common.parse_ts(ts)
        best = None
        for iso, t in self.state.get("recent_tickets", []):
            d = common.parse_ts(iso)
            if d and t0 and 0 <= (t0 - d).total_seconds() <= seconds and (best is None or d > best[0]):
                best = (d, t)
        return best[1] if best else None

    def run_sources(self):
        cfg, st = self.cfg, self.state
        # commits
        last = common.parse_ts(st.get("last_commit_scan"))
        if last:
            since = last - _dt.timedelta(days=1)
        else:
            since = common.day_bounds(common.workday(common.now(), cfg), cfg)[0]
        commits = sources.scan_commits(cfg, st, since)
        for c in commits:
            day = common.workday(c["date"], cfg)
            dd = self.store.day(day)
            rec = {k: c[k] for k in ("sha", "short", "repo_name", "date", "ref", "subject", "ticket")}
            rec["files_count"] = len(c["files"])
            if c["sha"] not in [x["sha"] for x in dd["commits"]]:
                dd["commits"].append(rec)
                self.store.touch_day(day)
            if c["ticket"]:
                slot = self.ensure_tslot(day, c["ticket"], c["repo"], c["ref"])
                tk = self.store.ticket(c["ticket"])
                tk["commits"][c["sha"]] = rec
                for fl in c["files"]:
                    old = tk["files"].get(fl["path"]) or {}
                    tk["files"][fl["path"]] = dict(old, path=fl["path"], role=old.get("role", ""), changed=True, last=day,
                                                   verified=True, verified_by="commit", repo=old.get("repo") or c["repo_name"])
                    if fl["path"] not in slot["files_changed"]:
                        slot["files_changed"].append(fl["path"])
                self.store.touch_ticket(c["ticket"])
        if commits:
            self.extras.append("+%d commit%s" % (len(commits), "" if len(commits) == 1 else "s"))
        sources.mark_commits_seen(st, commits)
        st["last_commit_scan"] = common.now_iso()

        # Bruno
        for b in sources.scan_bruno(cfg, st):
            day = common.workday(b["ts"], cfg)
            b["ticket"] = b.get("ticket") or self.active_ticket_at(b["ts"])
            dd = self.store.day(day)
            key = b["collection"] + "/" + b["path"]
            if (key, b["ts"]) not in [(x["collection"] + "/" + x["path"], x["ts"]) for x in dd["outside"]["bruno"]]:
                dd["outside"]["bruno"].append(b)
                self.store.touch_day(day)
            if b["ticket"]:
                self.ensure_tslot(day, b["ticket"])
                tk = self.store.ticket(b["ticket"])
                tk["bruno"][key] = {"method": b["method"], "url": b["url"], "name": b["name"], "collection": b["collection"], "last": day}

        # SQL scripts
        for q in sources.scan_sql(cfg, st):
            day = common.workday(q["ts"], cfg)
            q["ticket"] = q.get("ticket") or self.active_ticket_at(q["ts"])
            dd = self.store.day(day)
            dd["outside"]["sql"].append(q)
            self.store.touch_day(day)
            if q["ticket"]:
                slot = self.ensure_tslot(day, q["ticket"])
                tk = self.store.ticket(q["ticket"])
                qid = storemod.query_id(q["ticket"], q["text"])
                if qid not in tk["queries"]:
                    tk["queries"][qid] = {"id": qid, "date": day, "time": common.hhmm(q["ts"]), "dialect": q["dialect"],
                                          "text": q["text"], "purpose": "SQL script " + q["name"], "tables": [], "kind": "read",
                                          "revealed": "", "sent": None,  # a script you saved: unknown whether it ran
                                          "evidence": [q.get("path") or q["name"]]}
                    self.store.bump(q["ticket"], "queries")
                if qid not in slot["query_ids"]:
                    slot["query_ids"].append(qid)

        # inbox
        for n in sources.scan_inbox(cfg, st):
            if self.handle_branch_directive(n):
                continue  # branch-link: / branch-unlink: / branch-abandon: / branch-restore: / strategy: lines
            if self.jwl.handle_inbox(n):
                continue  # time: / leave: / half-day: / holiday: / workday: / resend: lines (jira worklog)
            day = common.workday(n["ts"], cfg)
            dd = self.store.day(day)
            dd["outside"]["inbox"].append(n)
            self.store.touch_day(day)
            if n["ticket"] and n["text"].lower().startswith("pin "):
                from . import replay
                if replay.pin_inbox(self.store, n["ticket"], n["text"], n["ts"], day):
                    self.ensure_tslot(day, n["ticket"])
                    self.store.ticket(n["ticket"]).setdefault("replay", {})["force"] = True
                    self.features.day_note(day, n["ticket"], "requests_changed", "pinned a request from inbox.md")
                continue
            if n["ticket"]:
                slot = self.ensure_tslot(day, n["ticket"])
                tk = self.store.ticket(n["ticket"])
                iid = common.stable_id(n["ticket"], "note", n["text"])
                if iid not in tk["items"]:
                    tk["items"][iid] = {"id": iid, "kind": "note", "title": common.truncate(n["text"], 90),
                                        "detail": n["text"] if len(n["text"]) > 90 else "", "evidence": ["inbox.md"],
                                        "confidence": "confirmed", "date": day, "time": common.hhmm(n["ts"]),
                                        "session": "inbox", "first_date": day}
                    self.store.bump(n["ticket"], "findings")
                if iid not in slot["item_ids"]:
                    slot["item_ids"].append(iid)

        # branches, commits and PRs per ticket; after the inbox, so manual links / abandons apply at once
        try:
            self.run_branches()
        except Exception:
            common.log("agent", "branch tracking failed: " + traceback.format_exc())

    def handle_branch_directive(self, n):
        d = gittrack.parse_directive(n.get("text") or "")
        if not d:
            return False
        ok, msg = gittrack.apply_directive(d, self.store, self.cfg, n.get("ts"))
        common.log("agent", "inbox branch directive: %s (%s)" % (msg, "ok" if ok else "ignored"))
        if d.get("ticket"):
            self.git_force.add(d["ticket"])
        self.store.branches_dirty = True
        return True

    def run_branches(self):
        cfg, st = self.cfg, self.state
        since_map = st.setdefault("branch_activity_since", {})
        baseline = st.setdefault("branch_baseline", {})
        tr = gittrack.Tracker(cfg, st, self.store, active_ticket=lambda ts: self.active_ticket_within(ts, 2 * 3600),
                              health=self.health)
        for ev in self.git_events:
            tr.add_session_event(ev)
        self.git_events = []
        before = {n: {b: dict(r) for b, r in gittrack.RepoData(n).branches.items()} for n in gittrack.all_repo_names()}
        rx = tr.refresh()
        self.store.reload_branches()
        self.health["pr_sources"] = gittrack.pr_source_rows(cfg, rx)  # Health / Settings: which PR source, last refresh
        for name, r in rx.items():
            key = common.norm_path(r.path)
            # first sight of a repo: record current branches, but only log activity from the start of today
            if key not in baseline:
                baseline[key] = common.day_bounds(common.workday(common.now(), cfg), cfg)[0].isoformat(timespec="seconds")
            base_ts = common.parse_ts(baseline[key])
            known = before.get(name) or {}
            for b, rec in r.data.branches.items():
                old = known.get(b)
                if old is None and rec.get("local"):
                    ct = common.parse_ts(rec.get("created_at"))
                    if ct and base_ts and ct >= base_ts:
                        self._branch_event(ct, {"kind": "create", "repo_name": name, "branch": b, "from": rec.get("parent"),
                                                "text": "created %s from %s" % (b, rec.get("parent") or "?")})
                elif old and old.get("local") and not rec.get("local"):
                    self._branch_event(common.now(), {"kind": "delete", "repo_name": name, "branch": b,
                                                      "text": "deleted local branch " + b})
                if old != rec:
                    self.store.branches_dirty = True
            since = since_map.get(key) or baseline[key]
            for a in branches.head_activity(cfg, r.path, since):
                self._branch_event(common.parse_ts(a["ts"]), a)
            since_map[key] = common.now_iso()
        # ticket pictures: tickets whose branches changed, recently active ones (targets move), manual decisions
        recent = tr.active_tickets(30)
        linked = {}
        for name, r in rx.items():
            for b, rec in r.data.branches.items():
                if rec.get("ticket"):
                    linked.setdefault(rec["ticket"], set()).add((name, b))
        for t in sorted(set(linked) & (recent | tr.changed_tickets | self.git_force) | (self.git_force & set(linked))):
            tk = self.store.ticket(t)
            for name, b in sorted(linked[t]):
                tk["branches"] = storemod._union(tk.get("branches"), [b])
                tk["repos"] = storemod._union(tk.get("repos"), [name])
            if delivery.update_ticket(self.store, t, rx, cfg, force=t in self.git_force):
                supersede.close_todos(tk)  # K3: git-action TODOs the new branch/PR data shows done
                self.store.touch_ticket(t)
                today = common.workday(common.now(), cfg)
                if t in (self.store.day(today).get("tickets") or {}):
                    self.store.touch_day(today)  # the daily file's "Branches & PRs" line
        self.git_force = set()

    def _branch_event(self, ts, rec):
        day = common.workday(ts, self.cfg)
        dd = self.store.day(day)
        rec = dict(rec, ts=ts.isoformat(timespec="seconds"))
        sig = (rec["ts"], rec.get("repo_name"), rec.get("text"))
        if sig not in [(x.get("ts"), x.get("repo_name"), x.get("text")) for x in dd["branch_activity"]]:
            dd["branch_activity"].append(rec)
            self.store.touch_day(day)

    # ------------------------------------------------------------------ day close
    def close_days(self):
        today = common.workday(common.now(), self.cfg)
        for day in Store.all_days():
            if day >= today:
                continue
            dd = self.store.day(day)
            if dd.get("closed"):
                continue
            pending = [sid for sid, s in self.state.get("sessions", {}).items()
                       if day in s.get("days", []) and s.get("pending_chars", 0) > 0]
            if pending:
                self.run_extractions(only_day=day, any_size=True)
                pending = [sid for sid in pending if self.state["sessions"][sid].get("pending_chars", 0) > 0]
            end = common.day_bounds(day, self.cfg)[1]
            if pending and (common.now() - end).total_seconds() < 6 * 3600 and not self.force:
                continue
            # run_sources() already ran this tick with a window reaching back past the previous scan,
            # so the day's commits are in before it is consolidated
            self.consolidate(day)

    def consolidation_input(self, day):
        dd = self.store.day(day)
        tickets = {}
        for t, slot in dd["tickets"].items():
            tk = self.store.ticket(t)
            items = [tk["items"][i] for i in slot.get("item_ids", []) if i in tk["items"]]
            tickets[t] = {
                "title": tk.get("title") or tk.get("title_hint"), "approx_minutes": slot.get("minutes"), "where": slot.get("where"),
                "activity": [w["time"] + " " + w["text"] for w in sorted(slot.get("what_i_did", []), key=lambda w: w.get("time", ""))],
                "items": [{"kind": i["kind"], "title": i["title"], "detail": i.get("detail"), "confidence": i.get("confidence")} for i in items],
                "queries": [{"purpose": tk["queries"][q].get("purpose"), "tables": tk["queries"][q].get("tables"),
                             "revealed": tk["queries"][q].get("revealed")} for q in slot.get("query_ids", []) if q in tk["queries"]],
                "apis": slot.get("api_keys", []), "files_changed": slot.get("files_changed", [])[:30],
                "commits": [c["subject"] for c in dd["commits"] if c.get("ticket") == t],
                "branches": [{"branch": b["branch"], "parent": b.get("parent"), "status": b.get("status")}
                             for x in ((tk.get("git") or {}).get("repos") or {}).values() for b in x.get("branches", [])],
                "delivery": (tk.get("git") or {}).get("summary"),
                "delivery_stage": (delivery.stage(tk.get("git") or {}) or {}).get("text"),
                "previous_tldr": tk.get("tldr"),
            }
        other = {g: {"activity": [w["text"] for w in s.get("what_i_did", [])],
                     "items": [i["title"] for i in s.get("items", {}).values()]} for g, s in dd["other"].items()}
        data = {"day": day, "tickets": tickets, "other_work": other,
                "outside_claude_code": {"bruno": [b["method"] + " " + b["url"] for b in dd["outside"]["bruno"]][:30],
                                        "sql_scripts": [s["name"] for s in dd["outside"]["sql"]][:20],
                                        "inbox": [n["text"] for n in dd["outside"]["inbox"]][:30]},
                "commits_no_ticket": [c["subject"] for c in dd["commits"] if not c.get("ticket")],
                "previous_open_todos": [supersede.todo_text(x) for x in dd.get("carried_todos") or []
                                        if not (isinstance(x, dict) and x.get("closed_by"))]}
        return redact(json.dumps(data, ensure_ascii=False, indent=1))[:150000]

    def consolidate(self, day):
        dd = self.store.day(day)
        has_content = dd["tickets"] or dd["other"] or dd["commits"] or any(dd["outside"].values())
        fails = int(dd.get("consolidate_fail") or 0)
        nxt = common.parse_ts(dd.get("consolidate_next"))
        if nxt and nxt > common.now() and not self.force:
            return
        res = None
        # K3: day TODOs are {text, ticket, closed_by}; carried git-action TODOs the branch/PR data shows done close here
        carried = [supersede.todo_obj(x, self.prefixes) for x in dd.get("carried_todos") or []]
        if carried:
            dd["carried_todos"] = carried
            supersede.close_day_todos(self.store, carried)
        if has_content and fails < 6:
            try:
                res, cost = extract.call_claude("consolidate", self.consolidation_input(day), self.cfg)
                dd["stats"]["cost_usd"] = round((dd["stats"].get("cost_usd") or 0) + cost, 4)
                self._add_cost(day, cost)
            except extract.ExtractError as e:
                dd["consolidate_fail"] = fails + 1
                dd["consolidate_next"] = (common.now() + _dt.timedelta(minutes=min(15 * 2 ** fails, 360))).isoformat(timespec="seconds")
                dd["stats"]["errors"] = (dd["stats"].get("errors") or 0) + 1
                self.store.touch_day(day)
                common.log("agent", "consolidation failed for %s: %s" % (day, e))
                return
        if res:
            dd["summary"] = res.get("day_summary") or dd.get("summary", "")
            dd["summary_final"] = True
            dd["standup"] = res.get("standup") or {}
            prev = {x["text"]: x for x in carried}
            dd["open_todos"] = [dict(prev[s]) if s in prev else supersede.todo_obj(s, self.prefixes, since=day)
                                for s in res.get("open_todos") or [] if isinstance(s, str) and s.strip()]
            for pt in res.get("per_ticket") or []:
                t = storemod.valid_ticket(pt.get("ticket"), None)
                if not t or t not in dd["tickets"]:
                    continue
                dd["consolidated"][t] = {"what_i_did": pt.get("what_i_did") or [], "next_steps": pt.get("next_steps") or [],
                                         "tldr": pt.get("tldr") or "", "status": pt.get("status_inferred") or ""}
                tk = self.store.ticket(t)
                if day >= (tk.get("tldr_date") or ""):
                    if pt.get("tldr"):
                        tk["tldr"], tk["tldr_date"] = pt["tldr"], day
                    if pt.get("status_inferred"):
                        tk["status_inferred"] = pt["status_inferred"]  # never the Jira status (jira.status)
                    if pt.get("problem") and not tk.get("problem"):
                        tk["problem"] = pt["problem"]
                tl = tk["timeline"].setdefault(day, {"minutes": dd["tickets"][t].get("minutes", 0), "line": ""})
                if pt.get("what_i_did"):
                    tl["line"] = common.truncate("; ".join(pt["what_i_did"][:2]), 200)
                self.store.touch_ticket(t)
        else:
            dd["standup"] = render.draft_standup(self.store, dd) if has_content else {}
            dd["open_todos"] = [dict(x) for x in carried]
        supersede.close_day_todos(self.store, dd["open_todos"])
        for t in dd["tickets"]:
            if supersede.close_todos(self.store.ticket(t)):
                self.store.touch_ticket(t)
        try:  # one supersession call per ticket with new items today, within today's model budget
            cost, _ = supersede.run_day(self.store, self.cfg, self.health, day)
            if cost:
                dd["stats"]["cost_usd"] = round((dd["stats"].get("cost_usd") or 0) + cost, 4)
                self._add_cost(day, cost)
        except Exception:
            common.log("agent", "supersession failed: " + traceback.format_exc())
        dd["closed"], dd["closed_at"] = True, common.now_iso()
        dd.pop("consolidate_next", None)
        still = [x for x in dd["open_todos"] if not x.get("closed_by")]  # done ones stay in the day, not carried
        if still:
            self.state["carry_todos"] = {"from": day, "todos": still}
            today = common.workday(common.now(), self.cfg)
            if os.path.exists(storemod.day_path(today)):
                self._apply_carry(today)
                self.store.touch_day(today)
        self.store.touch_day(day)
        self.day_closed = True
        self.extras.append("closed %s" % day)
        common.log("agent", "closed day %s (llm=%s)" % (day, bool(res)))

    # ------------------------------------------------------------------ health / prune
    def update_health(self):
        h = self.health
        warnings = []
        if h.get("privacy") == "public":
            warnings.append("Worklog recorder status: the GitHub repo is public, so pushes are blocked (commits continue locally).")
        if gitsync.privacy_warning(h):
            warnings.append("Worklog recorder status: " + gitsync.privacy_warning(h) + ".")
        pf = common.parse_ts(h.get("push_failing_since"))
        if pf and (common.now() - pf).total_seconds() > 6 * 3600:
            warnings.append("Worklog recorder status: pushes to GitHub have failed since %s." % pf.strftime("%Y-%m-%d %H:%M"))
        lh = h.get("last_hook_event") or ""
        sn_days = [d for d in self.state.get("safety_net_days", []) if d > lh[:10]]
        if len(sn_days) >= 3:
            warnings.append("Worklog recorder status: Claude Code hooks recorded nothing on %d recent work days; "
                            "capture is running from transcripts only." % len(sn_days))
        if h.get("held_back"):
            warnings.append("Worklog recorder status: not committed because they contain a literal password, token or person "
                            "value: %s. Replace the value with {{process.env.NAME}} (value in bruno/.env)." % ", ".join(h["held_back"][:5]))
        modelbudget.drain(h, self.cfg)
        mb = modelbudget.summary(h, self.cfg)
        if mb["over"]:
            warnings.append("Worklog recorder status: today's model spend $%.2f reached the $%.2f cap "
                            "(budget.model_daily_usd); supersession and briefs wait until tomorrow, extraction "
                            "continues." % (mb["usd"], mb["cap"]))
        h["warnings"] = warnings
        today = common.workday(common.now(), self.cfg)
        dd = self.store.day(today) if os.path.exists(storemod.day_path(today)) else None
        if dd:
            h["today"] = {"day": today, "sessions": len(dd["sessions"]),
                          "extracted": len([s for s in dd["sessions"].values() if s.get("extracted")]),
                          "cost_usd": dd["stats"].get("cost_usd"), "errors": dd["stats"].get("errors")}

    def prune(self):
        """Raw capture older than raw_retention_days goes once it is fully read and extracted, on every install: raw
        is never pushed, so there is no push to wait for. Raw not yet processed is never pruned."""
        days = int(self.cfg.get("raw_retention_days") or 30)
        cutoff = (common.now() - _dt.timedelta(days=days)).date().isoformat()
        sessions = self.state.get("sessions", {})
        for day, sid, path in raw_files():
            if day >= cutoff:
                continue
            s = sessions.get(sid) or {}
            f = (s.get("files") or {}).get(raw_key(day, sid))
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if f and f.get("extracted", 0) >= size and f.get("read", 0) >= size:
                paths.delete(path)
                s["files"].pop(raw_key(day, sid), None)
        root = common.wl("raw")
        for d in os.listdir(root) if os.path.isdir(root) else []:
            tdir = os.path.join(root, d, "time")  # time-only events of worklog-folder sessions
            if d < cutoff and os.path.isdir(tdir):
                for fn in os.listdir(tdir):
                    paths.delete(os.path.join(tdir, fn))
                paths.rmdir(tdir)
            dp = os.path.join(root, d)
            if os.path.isdir(dp) and not os.listdir(dp):
                paths.rmdir(dp)
        for sid in list(sessions):
            s = sessions[sid]
            if not s.get("files") and (s.get("last_event") or "") < cutoff:
                del sessions[sid]
        self.prune_tmp()
        from . import jiraworklog
        jiraworklog.prune_condense_cache(common.workday(common.now(), self.cfg))  # condensed lines keep 14 days

    @staticmethod
    def prune_tmp(max_age_s=2 * 86400):
        """<home>/.worklog/tmp (replay request bodies and run folders) keeps two days."""
        root = paths.tmp_dir()
        for name in os.listdir(root) if os.path.isdir(root) else []:
            p = os.path.join(root, name)
            try:
                if time.time() - os.path.getmtime(p) > max_age_s:
                    paths.rmtree(p) if os.path.isdir(p) else paths.delete(p)
            except OSError:
                continue

    # ------------------------------------------------------------------ main
    def tick(self):
        h = self.health
        h["last_tick"] = common.now_iso()
        paused = os.path.exists(common.wl("PAUSED"))
        gitsync.ensure_repo(self.cfg)
        self.safety_net(paused)
        if not paused:
            self.ingest()
            self.run_extractions()
            self.refresh_fallbacks()
            self.run_sources()
            try:
                if configstore.on(self.cfg, "artifacts.enabled"):  # off: queued files wait until it's back on
                    self.features.process_artifacts()
            except Exception:
                common.log("agent", "artifacts failed: " + traceback.format_exc())
            try:
                self.features.curate_and_generate(self.force)
            except Exception:
                common.log("agent", "replay failed: " + traceback.format_exc())
            try:
                from . import pathcheck
                pathcheck.run(self.store, self.cfg)  # model-mentioned file paths checked against git (read-only)
            except Exception:
                common.log("agent", "pathcheck failed: " + traceback.format_exc())
            self.close_days()
            try:
                self.jwl.tick()
            except Exception:
                common.log("agent", "jira worklog failed: " + traceback.format_exc())
            try:
                self.jobs.tick()
            except Exception:
                common.log("agent", "mcp jobs failed: " + traceback.format_exc())
        if self.cfg_dirty:
            common.save_config(self.cfg, base=self.cfg_base)
            self.cfg_base = copy.deepcopy(self.cfg)
        self.store.save()
        self.update_health()
        from . import kb5
        changed = render.render_all(self.store, self.cfg, h, days=self.store.dirty_days, tickets=self.store.dirty_tickets,
                                    everything=kb5.take_rerender())  # once after a data migration
        day = common.workday(common.now(), self.cfg)
        msg = gitsync.commit_message(day, self.store.delta, " ".join(self.extras + [self.features.commit_extras()]).strip())
        committed = gitsync.commit(self.cfg, msg, h)
        if committed:
            common.log("agent", "committed: " + msg)
        lp = common.parse_ts(h.get("last_push"))
        interval = int(self.cfg.get("push_interval_minutes") or 30) * 60
        due = self.force or self.session_ended or self.day_closed or not lp or (common.now() - lp).total_seconds() >= interval
        if not configstore.on(self.cfg, "push_enabled"):
            pass  # commits stay local until the push is switched back on
        elif due and gitsync.unpushed(self.cfg) > 0:
            if gitsync.push(self.cfg, h):
                common.log("agent", "pushed")
        else:
            gitsync.check_privacy(self.cfg, h)
        self.update_health()
        self.prune()
        h["last_tick_ok"] = common.now_iso()
        return changed


def save_state(agent):
    modelbudget.drain(agent.health, agent.cfg)  # model calls made since the last health update
    common.write_json(common.wl("state.json"), agent.state)
    common.write_json(common.wl("health.json"), agent.health)


def cmd_tick(force=False):
    from . import ticklog
    t0 = time.time()
    if os.path.exists(common.wl("locks", "upgrade.lock")):
        ticklog.record("skipped-upgrade")
        return 0  # an upgrade is in progress; hooks keep capturing, processing resumes afterwards
    lock = Lock()
    if not lock.acquire():
        ticklog.record("skipped-busy")  # another run holds the lock (and health.json): recorded only here
        return 0
    agent = None
    outcome = "error"
    try:
        try:
            from . import migrate
            migrate.run_in_tick()  # forward data migrations of a new version (backed up first), before config is loaded
        except Exception:
            common.log("agent", "migration failed: " + traceback.format_exc())
        agent = Agent(force=force)
        agent.tick()
        outcome = "ok"
        return 0
    except Exception:
        common.log("agent", "tick failed: " + traceback.format_exc())
        if agent:
            agent.health["last_error"] = common.now_iso() + " tick: " + traceback.format_exc().splitlines()[-1][:200]
        return 1
    finally:
        if agent:
            try:
                save_state(agent)
            except Exception:
                common.log("agent", "state save failed: " + traceback.format_exc())
        ticklog.record(outcome, (time.time() - t0) * 1000, trim=True)
        lock.release()


def cmd_branch_check(key=None, wait_s=None):
    """/cg:check [KEY]: fetch, refresh PRs and recompute the delivery check now (under the agent lock).
    -> {"tickets": [...], "repos": [...], "fetched": [repos actually fetched]} or {"skipped": why}."""
    lock = Lock()
    if not lock.acquire_wait(LOCK_WAIT_S if wait_s is None else wait_s):
        return {"skipped": "the background agent is busy; try again in a few minutes"}
    agent = None
    try:
        agent = Agent(force=True)
        cfg = agent.cfg
        repos = cfg.get("watched_repos") or []
        if key:
            names = set(agent.store.ticket(key).get("repos") or [])
            repos = [p for p in repos if gittrack.repo_key(p) in names] or repos
        tr = gittrack.Tracker(cfg, agent.state, agent.store, force_fetch=True, only_tickets=[key] if key else None,
                              health=agent.health)
        started = common.now_iso()
        rx = tr.refresh(repos, fetch=True, force_pr=True)
        agent.store.reload_branches()
        if key:
            tickets = [key]
        else:
            recent = tr.active_tickets(14)
            tickets = sorted({b.get("ticket") for r in rx.values() for b in r.data.branches.values()} & recent - {None})
        for t in tickets:
            delivery.update_ticket(agent.store, t, rx, cfg, force=True)
        agent.store.branches_dirty = True
        agent.store.save()
        render.render_all(agent.store, cfg, agent.health, days=set(), tickets=set(t for t in tickets if t in agent.store.tickets))
        errors = {n: (r.data.meta.get("fetch_error") or r.data.meta.get("pr_error")) for n, r in rx.items()
                  if r.data.meta.get("fetch_error") or r.data.meta.get("pr_error")}
        fetched = sorted(n for n, r in rx.items() if (r.data.meta.get("fetched_at") or "") >= started)
        return {"tickets": tickets, "repos": sorted(rx), "fetched": fetched, "errors": errors}
    finally:
        if agent:
            try:
                save_state(agent)
            except Exception:
                common.log("agent", "state save failed: " + traceback.format_exc())
        lock.release()


def cmd_backfill(llm=True, sids=None):
    """One-off backfill for upgrade 01 (replay requests, DB flow, artifacts) from retained raw logs."""
    lock = Lock()
    if not lock.acquire():
        print("agent busy; try again")
        return 1
    agent = None
    try:
        agent = Agent(force=True)
        cost = agent.features.backfill(sids=set(sids) if sids else None, llm=llm)
        agent.features.process_artifacts()
        agent.features.curate_and_generate(force=True)
        agent.store.save()
        render.render_all(agent.store, agent.cfg, agent.health, days=Store.all_days(), tickets=Store.all_ticket_ids(), everything=True)
        agent._add_cost(common.workday(common.now(), agent.cfg), cost)
        print("backfill cost $%.2f" % cost)
        return 0
    finally:
        if agent:
            save_state(agent)
        lock.release()


def cmd_render(everything):
    cfg = common.load_config()
    st = Store()
    health = common.read_json(common.wl("health.json"))
    days = Store.all_days()
    changed = render.render_all(st, cfg, health, days=days, tickets=Store.all_ticket_ids(), everything=everything)
    print("\n".join(changed) or "no changes")


def slack_email(cfg):
    """Email to look up in the Slack workspace: jira_worklog.slack.email, else the last git_author_emails entry
    (the work address; personal addresses come first)."""
    email = ((cfg.get("jira_worklog") or {}).get("slack") or {}).get("email")
    emails = cfg.get("git_author_emails") or []
    return email or (emails[-1] if emails else "")


def cmd_worklog(cmd, args):
    """worklog-show [day] [--jira] [--exact] | worklog-send <day> [--correction] | slack-discover | jira-card KEY... |
    schema-card TABLE... | qa-trace KEY | link-map | mcp-ensure"""
    from . import jiraworklog
    from . import kbjobs
    from . import mcpjobs
    from . import slack
    pos = [a for a in args if not a.startswith("--")]
    if cmd != "worklog-send":
        os.environ["CHITRAGUPTA_MCP_MANUAL"] = os.environ["WORKLOG_MCP_MANUAL"] = "1"
    if cmd == "worklog-show":  # the sender's builder: cached Jira facts and condensed lines, nothing saved or sent
        cfg = common.load_config()
        day = pos[0] if pos else common.workday(common.now(), cfg)
        if "--jira" in args or "--exact" in args:  # the two asked-for calls: under the agent lock, never beside a tick
            r = jiraworklog.refresh_facts(day) if "--jira" in args else {"ok": True}
            if r.get("ok") and "--exact" in args:
                r = jiraworklog.preview_exact(day)
            if r.get("skipped") == "the agent is busy; try again in a minute":
                print("agent busy; try again")
                return 1
            if r.get("skipped"):
                print("(%s)" % r["skipped"])
        pv = jiraworklog.preview(day)
        print(pv["text"] or "(no message for %s: %s)" % (day, pv.get("reason")))
        notes = [pv["facts_as_of"]] if pv.get("facts_as_of") else []
        n = sum(1 for e in pv["entries"] if e.get("pending_condense"))
        if n:
            notes.append("%d description(s) condensed at %s (--exact condenses now)" % (n, pv.get("send_at") or "20:00"))
        if notes:
            print("\n(" + "; ".join(notes) + ")")
        return 0
    lock = Lock()
    if not lock.acquire_wait():
        print("agent busy; try again")
        return 1
    a = None
    try:
        a = Agent(force=True)
        if cmd == "worklog-send":
            sh = a.jwl.run_day(pos[0], "correction" if "--correction" in args else "resend")
            print(json.dumps({k: sh.get(k) for k in ("status", "slack_ts", "last_error", "attempts")}, indent=1))
            print((sh.get("versions") or [{}])[-1].get("text"))
        elif cmd == "slack-discover":
            email = pos[0] if pos else slack_email(a.cfg)
            r = slack.discover(a.cfg, email)
            print(json.dumps(r, indent=1))
            if r.get("self_user_id") and r.get("send_tool"):
                a.cfg.setdefault("jira_worklog", {}).setdefault("slack", {}).update(
                    self_user_id=r["self_user_id"], send_tool=r["send_tool"], mode=r["mode"], tools=r["tools"])
                common.save_config(a.cfg, base=a.cfg_base)
        elif cmd == "jira-card":
            for k in pos:
                rec = mcpjobs.jira_card(k, a.cfg, a.health)
                a.jobs.apply_card(k, rec)
                print(json.dumps({"key": k, "card": rec.get("card"), "error": rec.get("error")}, indent=1))
        elif cmd == "schema-card":
            saved, res = mcpjobs.schema_cards(pos, a.cfg, a.health)
            print("\n".join(saved), "\ntokens in/out: %s/%s" % (res["input_tokens"], res["output_tokens"]))
        elif cmd == "qa-trace":
            print(a.jobs.qa_traces(common.workday(common.now(), a.cfg), tickets=pos, force=True), "trace(s)")
        elif cmd == "link-map":
            print(a.jobs.link_map(common.workday(common.now(), a.cfg)))
        elif cmd == "mcp-ensure":
            mcpjobs.ensure_files(a.cfg)
            slack.ensure_config(a.cfg)
            from . import envmap
            print(json.dumps(envmap.build(a.cfg), indent=1))
        a.store.save()
        render.render_all(a.store, a.cfg, a.health, days=a.store.dirty_days, tickets=a.store.dirty_tickets)
        return 0
    except mcpjobs.JobError as e:
        print("skipped: %s" % e)
        return 3
    finally:
        if a:
            save_state(a)
        lock.release()


def ensure_dashboard():
    """Start the read-only dashboard if it isn't running (respects dashboard.autostart / CHITRAGUPTA_DASHBOARD=0).
    A cheap port probe when it is already up; never raises."""
    try:
        from . import dashboard
        dashboard.ensure()
    except Exception as e:  # noqa: BLE001
        common.log("agent", "dashboard ensure failed: %s" % e)


def cmd_loop(max_ticks=None, interval=None, sleep=time.sleep):
    """Fallback launcher (Startup folder): tick every 5 minutes. Only one loop runs: a second one exits at once."""
    loop = Lock("loop.lock")
    if not loop.acquire():
        print("an agent loop is already running")
        return 0
    interval = LOOP_INTERVAL_S if interval is None else interval
    try:
        n = 0
        while max_ticks is None or n < max_ticks:
            loop.refresh()
            cmd_tick()  # takes the agent lock itself
            if max_ticks is None:  # the real Startup-folder loop, not a test
                ensure_dashboard()
            n += 1
            if max_ticks is None or n < max_ticks:
                sleep(interval)
        return 0
    finally:
        loop.release()


def main(argv):
    cmd = argv[0] if argv else "tick"
    if cmd in ("worklog-show", "worklog-send", "slack-discover", "jira-card", "schema-card", "qa-trace", "link-map",
               "mcp-ensure"):
        return cmd_worklog(cmd, argv[1:])
    if cmd == "tick":
        rc = cmd_tick(force="--force" in argv)
        if "--from-task" in argv:  # the scheduled task (logon + every 5 min) also keeps the dashboard up
            ensure_dashboard()
        return rc
    if cmd == "loop":
        return cmd_loop()
    if cmd == "render":
        cmd_render("--all" in argv)
        return 0
    if cmd == "backfill-upgrade01":
        return cmd_backfill("--no-llm" not in argv, [a for a in argv[1:] if not a.startswith("--")])
    if cmd == "status":
        print(json.dumps(common.read_json(common.wl("health.json")), indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass  # pythonw has no console
    sys.exit(main(sys.argv[1:]))
