"""K12: rebuild tickets' knowledge from the raw events still on disk.

    agent rebuild <KEY> [<KEY>...] | --all  [--dry-run] [--cap-usd N] [--no-brief]

Scope: every raw session-day whose turns, under today's attribution (agent.attribute) and capture rules (O5), touch a
selected ticket (--all: every session-day), plus the session-days that produced the selected tickets' existing items.

A dry run writes nothing: it prices the run (chunks x the ledger's recent average per extraction call, else
rebuild.est_usd_per_chunk; plus one supersession and one brief call per ticket that is not a parent issue) and returns
{tickets, sessions, days, chunks, est_usd, model, cap_usd, over_cap}. A real run takes the agent lock and:
  1. refuses, changing nothing, when the estimate is over the cap (rebuild.cap_usd, default 8; --cap-usd)
  2. backs up the selected tickets' records and hub folders, the affected days, catalog.json and state.json
     (.worklog/backups/chitragupta-rebuild-<timestamp>/, paths relative to the home)
  3. archives every extracted item of a selected ticket (archived {at, reason "rebuild <timestamp>"}): kept, out of
     every current view. Items you wrote stay current: inbox notes, copies you moved, items whose review you resolved.
     A parent issue (jira.parent_issue_types) has its items archived as its children's work and gets no new ones.
  4. re-extracts the session-days chunk by chunk through the normal extraction (purpose "extract", the same prompts,
     TURN markers and per-chunk default ticket; CHITRAGUPTA_LLM_CACHE answers are reused), merging only into the
     selected tickets (--all: every ticket). An item re-extracted with the id of one this run archived is current
     again. When the spend reaches the cap it stops cleanly, keeps what was merged and records its progress in
     state.json "rebuild_progress", so the same command continues where it stopped.
  5. recomputes the affected days' minutes with the current time model (the whole day, as a tick does), keeping each
     row's earlier value once as minutes_before_rebuild
  6. runs supersession and (unless --no-brief) the brief for each selected ticket, within budget.model_daily_usd
  7. re-renders the tickets' hubs and the INDEX; daily files are left as they are
Requests, replay, artifacts, test runs, Bruno files, git data, the Jira card, titles and "My notes" are untouched; the
day's activity lines and worklog lines are not re-derived. Each run is recorded in health "rebuilds" and agent.log.
"""
import json
import os
import sys
import traceback

from . import common
from . import configstore
from . import extract
from . import kbnorm
from . import sqlsent
from . import store as S

DEFAULT_CAP_USD = 8.0
DEFAULT_PER_CHUNK = 0.02
DEFAULT_SUPERSEDE = 0.06
DEFAULT_BRIEF = 0.10
LEDGER_DAYS = 7  # the most recent ledger days with calls of a purpose make its average
KEEP_RUNS = 10
PARENT_REASON = "parent issue: work belongs to its child tickets"
CONTENT_EVENTS = ("user-prompt", "stop", "post-tool")


def user_written(it):
    """Items you wrote or decided on, which a rebuild never archives: inbox notes, copies you moved to a ticket,
    items whose review you resolved, anything titled by you."""
    return (it.get("src") == "user" or it.get("session") == "inbox" or bool(it.get("review_kept"))
            or bool(it.get("moved_from")) or it.get("title_source") == "user")


def _num(cfg, key, default):
    try:
        return float(configstore.get(cfg or {}, key, default))
    except (TypeError, ValueError):
        return default


def avg_cost(health, purpose, default):
    """Dollars per call of `purpose` over the ledger's most recent LEDGER_DAYS days that have such calls, else
    `default`."""
    led = (health or {}).get("model_usage") or {}
    calls, usd, n = 0, 0.0, 0
    for day in sorted(led, reverse=True):
        bp = (((led.get(day) or {}).get("by_purpose")) or {}).get(purpose) or {}
        if not int(bp.get("calls") or 0):
            continue
        calls += int(bp["calls"])
        usd += float(bp.get("usd") or 0)
        n += 1
        if n >= LEDGER_DAYS:
            break
    return round(usd / calls, 6) if calls and usd > 0 else default


class Rebuild:
    def __init__(self, agent, keys, all_=False, cap_usd=None, brief=True, lock=None):
        from . import brief as briefmod
        self.a, self.cfg, self.store, self.lock = agent, agent.cfg, agent.store, lock
        self.all = bool(all_)
        if self.all:
            keys = [t for t in S.Store.all_ticket_ids() if S.valid_ticket(t, None)]
        self.keys = sorted(dict.fromkeys(k.strip().upper() for k in keys))
        self.parents = [t for t in self.keys if briefmod.kind_of(self.store.ticket(t), self.cfg) == "epic"]
        self.cap = float(cap_usd) if cap_usd is not None else _num(self.cfg, "rebuild.cap_usd", DEFAULT_CAP_USD)
        self.brief = brief
        self.usd, self.calls, self.ignored, self.failed = 0.0, 0, {}, []

    @property
    def scope_key(self):
        return "all" if self.all else ",".join(self.keys)

    # ------------------------------------------------------------------ scope
    def scope(self):
        """[{key, day, sid, evs}] of the session-days to re-extract, in order (day, first event)."""
        from . import agent as agentmod
        sel = set(self.keys)
        made = {}  # day -> session ids (8 chars) that produced the selected tickets' extracted items
        for t in self.keys:
            for it in (self.store.ticket(t).get("items") or {}).values():
                if isinstance(it, dict) and it.get("session") and it.get("date") and not user_written(it):
                    made.setdefault(it["date"], set()).add(it["session"])
        out = []
        for day, sid, path in agentmod.raw_files():
            evs = self.a._read_turns(path, 0, keep_time=False)[0]  # today's capture rules and attribution
            if not any(e.get("event") in CONTENT_EVENTS for e in evs):
                continue
            if not (self.all or any(e.get("_t") in sel for e in evs) or any(sid.startswith(p) for p in made.get(day, ()))):
                continue
            key = agentmod.raw_key(day, sid)
            for e in evs:
                if e.get("_ln"):
                    e["_ref"] = "raw/%s:%d" % (key, e["_ln"])
            evs.sort(key=lambda e: e.get("ts") or "")
            out.append({"key": key, "day": day, "sid": sid, "evs": evs})
        out.sort(key=lambda x: (x["day"], x["evs"][0].get("ts") or "", x["sid"]))
        return out

    def days(self, scope):
        """Days whose data the run changes: the scope's, plus the selected tickets' days whose raw is still on disk."""
        out = {sd["day"] for sd in scope}
        for t in self.keys:
            for d in (self.store.ticket(t).get("timeline") or {}):
                if os.path.isdir(common.wl("raw", d)):
                    out.add(d)
        return sorted(out)

    def prepare(self, sd):
        """(ctx, chunks) of one session-day, exactly as Agent.extract_session builds them."""
        evs, sid, day = sd["evs"], sd["sid"], sd["day"]
        s = (self.a.state.get("sessions") or {}).get(sid) or {}
        enrich = extract.enrich_events(s.get("transcript_path"), evs) if s.get("source") == "hook" else []
        all_evs = sorted(evs + enrich, key=lambda e: e.get("ts") or "")
        cands = []
        for e in evs:
            if e.get("_t") and e["_t"] not in cands:
                cands.append(e["_t"])
        last = evs[-1]
        ctx = {"day": day, "session": sid, "time": common.hhmm(evs[0].get("ts")), "repo": last.get("repo_root"),
               "branch": last.get("branch"), "candidates": cands, "edited": kbnorm.edited_files(evs),
               "sent": sqlsent.sent_statements(evs),
               "line_times": {e["_ln"]: common.hhmm(e.get("ts")) for e in evs if e.get("_ln")}}
        meta = {"session": sid, "day": day, "repo": ctx["repo"], "branch": ctx["branch"],
                "lines": self.a.jwl.current_lines(day, cands, common.parse_ts(evs[0].get("ts")))}
        return ctx, extract.build_digests(all_evs, meta, cands, S.already_recorded(self.store, cands))

    def estimate(self, todo):
        h = self.a.health
        per_chunk = avg_cost(h, "extract", _num(self.cfg, "rebuild.est_usd_per_chunk", DEFAULT_PER_CHUNK))
        per_sup = avg_cost(h, "supersede", DEFAULT_SUPERSEDE)
        per_brief = avg_cost(h, "brief", DEFAULT_BRIEF)
        chunks = sum(len(self.prepare(sd)[1]) for sd in todo)
        n = len(self.keys) - len(self.parents)
        est = chunks * per_chunk + n * per_sup + (n * per_brief if self.brief else 0)
        return chunks, round(est, 4)

    # ------------------------------------------------------------------ the steps
    def backup(self, days):
        from . import migrate
        paths = [S.ticket_path(t) for t in self.keys] + [S.day_path(d) for d in days]
        paths += [S.catalog_path(), common.wl("state.json")] + [common.hp("tickets", t) for t in self.keys]
        return migrate._backup_tree(paths, "rebuild")

    def archive(self, reason):
        """Archive every extracted item of the selected tickets (never one you wrote). Returns how many."""
        n, at = 0, common.now_iso()
        for t in self.keys:
            why = PARENT_REASON if t in self.parents else reason
            tk = self.store.ticket(t)
            k = 0
            for it in (tk.get("items") or {}).values():
                if isinstance(it, dict) and not it.get("archived") and not user_written(it):
                    it["archived"] = {"at": at, "reason": why}
                    k += 1
            if k:
                self.store.touch_ticket(t)
            n += k
        return n

    def hold(self):
        """Items a rebuild archived, set aside while re-extracting, so one extracted again (same id) comes back as a
        current item instead of merging into the archived record."""
        held = {}
        for t in self.keys:
            if t in self.parents:
                continue
            items = self.store.ticket(t)["items"]
            for iid in [i for i, it in items.items() if isinstance(it, dict)
                        and str((it.get("archived") or {}).get("reason") or "").startswith("rebuild ")]:
                held[(t, iid)] = items.pop(iid)
        return held

    def restore(self, held):
        """Put the set-aside items back. One the run extracted again stays current, keeping the older record's first
        day, evidence and history. Returns how many came back current."""
        n = 0
        for (t, iid), old in held.items():
            items = self.store.ticket(t)["items"]
            new = items.get(iid)
            if new is None:
                items[iid] = old
                continue
            new["evidence"] = S._union(new.get("evidence"), old.get("evidence"))[:10]
            firsts = [x for x in (old.get("first_date") or old.get("date"), new.get("first_date")) if x]
            if firsts:
                new["first_date"] = min(firsts)
            if old.get("history") or new.get("history"):
                new["history"] = S.union_history(old.get("history"), new.get("history"))
                S.compact_history(new, t)
            self.store.touch_ticket(t)
            n += 1
        return n

    def extract_one(self, sd):
        """Re-extract one session-day. None when done, else why it stopped ("cap" / "limit" / "error")."""
        ctx, chunks = self.prepare(sd)
        only = None if self.all else set(self.keys)
        try:
            for n, chunk in enumerate(chunks):
                if self.usd >= self.cap:
                    return "cap"
                if n:  # a later chunk sees what the earlier ones recorded
                    chunk = extract.with_already(chunk, S.already_recorded(self.store, ctx["candidates"]))
                res, c = extract.call_claude("extract", chunk, self.cfg)
                self.usd += float(c or 0)
                self.calls += 1
                res = dict(res)
                res.pop("activity_summary", None)  # the day's activity lines stay as they were written
                cctx = dict(ctx, default=chunk.default, turns=chunk.turns)
                S.merge_extraction(self.store, res, cctx, self.a.prefixes, only=only)
                for k, v in (cctx.get("ignored") or {}).items():
                    self.ignored[k] = self.ignored.get(k, 0) + v
        except extract.ExtractError as e:
            self.failed.append(sd["key"])
            common.log("agent", "rebuild: extraction failed for %s: %s" % (sd["key"], e))
            return "limit" if e.limit else "error"
        except Exception:  # noqa: BLE001 - one session-day never stops the rest
            self.failed.append(sd["key"])
            common.log("agent", "rebuild: extraction failed for %s: %s" % (sd["key"], traceback.format_exc()))
            return "error"
        return None

    def breadcrumbs(self, scope):
        """An Epic's page lists the tickets a pasted breadcrumb put under it (tk.crumb_parents, additive)."""
        sel = set(self.keys)
        for sd in scope:
            for e in sd["evs"]:
                if e.get("event") != "user-prompt":
                    continue
                for child, parents in common.breadcrumb_parents((e.get("payload") or {}).get("prompt") or "",
                                                                self.a.prefixes).items():
                    if not (self.all or child in sel or sel & set(parents)):
                        continue
                    tk = self.store.ticket(child)
                    if any(x not in (tk.get("crumb_parents") or []) for x in parents):
                        tk["crumb_parents"] = S._union(tk.get("crumb_parents"), parents)
                        self.store.touch_ticket(child)

    def minutes(self, days):
        """Recompute each day with the current time model; every row keeps its earlier value once."""
        out = {}
        for d in days:
            dd = self.store.day(d)
            before = {t: int(s.get("minutes") or 0) for t, s in dd["tickets"].items()}
            for t, s in dd["tickets"].items():
                s.setdefault("minutes_before_rebuild", before[t])
            self.a.recompute_minutes(d)
            for t, s in dd["tickets"].items():
                s.setdefault("minutes_before_rebuild", 0)
                out.setdefault(d, {})[t] = {"before": before.get(t, 0), "after": int(s.get("minutes") or 0)}
            self.store.touch_day(d)
        return out

    def finish(self, keys):
        """Supersession, then the brief, per ticket. Returns why it stopped, or None."""
        from . import brief as briefmod
        from . import supersede
        today = common.workday(common.now(), self.cfg)
        for t in keys:
            if self.usd >= self.cap:
                return "cap"
            c, done = supersede.run_day(self.store, self.cfg, self.a.health, today, tickets=[t])
            self.usd += float(c or 0)
            self.calls += len(done)
        if not self.brief:
            return None
        for t in keys:
            if self.usd >= self.cap:
                return "cap"
            try:
                r = briefmod.generate(t, store=self.store, cfg=self.cfg, health=self.a.health)
            except Exception:  # noqa: BLE001
                common.log("agent", "rebuild: brief %s failed: %s" % (t, traceback.format_exc()))
                continue
            if r.get("ok") and not r.get("skipped"):
                self.calls += 1
                self.usd += float((self.store.ticket(t).get("brief") or {}).get("cost_usd") or 0)
        return None

    # ------------------------------------------------------------------ run
    def run(self, dry_run):
        from . import extract as ex
        progs = self.a.state.get("rebuild_progress") or {}
        prog = progs.get(self.scope_key)
        scope = self.scope()
        done = set((prog or {}).get("done") or [])
        todo = [sd for sd in scope if sd["key"] not in done]
        days = self.days(scope)
        chunks, est = self.estimate(todo)
        out = {"tickets": self.keys, "parents": self.parents, "sessions": len(todo), "days": days, "chunks": chunks,
               "est_usd": est, "model": ex.model_for("extract", self.cfg), "cap_usd": self.cap, "over_cap": est > self.cap}
        if prog:
            out["continues"] = prog.get("at")
        if dry_run:
            out["dry_run"] = True
            return out
        if out["over_cap"]:
            out["refused"] = "the estimate $%.2f is over the $%.2f cap (rebuild.cap_usd, --cap-usd)" % (est, self.cap)
            common.log("agent", "rebuild %s refused: %s" % (self.scope_key, out["refused"]))
            return out
        at = (prog or {}).get("at") or common.now_iso()
        reason = (prog or {}).get("reason") or "rebuild " + at
        out["backup"] = self.backup(days)
        out["archived"] = 0 if prog else self.archive(reason)
        before = {t: set(self.store.ticket(t)["items"]) for t in self.keys}
        self.breadcrumbs(scope)
        prog = self.a.state.setdefault("rebuild_progress", {}).setdefault(
            self.scope_key, {"at": at, "reason": reason, "tickets": "all" if self.all else self.keys, "done": []})
        held = self.hold()
        stopped = None
        try:
            for sd in todo:
                why = self.extract_one(sd)
                if why in ("cap", "limit"):
                    stopped = ("the $%.2f spend cap was reached" % self.cap) if why == "cap" else "the model's usage limit"
                    break
                if why is None:
                    prog["done"].append(sd["key"])
                if self.lock:
                    self.lock.refresh()
        finally:
            out["restored"] = self.restore(held)
        if self.ignored:
            common.log("agent", "rebuild %s: results for other tickets left out: %s" % (self.scope_key, json.dumps(self.ignored)))
        out["minutes"] = self.minutes(days)
        keys = list(self.keys)
        if self.all:  # a ticket the re-extraction created
            keys += sorted(t for t in self.store.dirty_tickets if t not in self.keys and self.store.ticket(t).get("items"))
        if not stopped:
            stopped = self.finish([t for t in keys if t not in self.parents])
        new = 0
        for t in keys:
            new += len(set(self.store.ticket(t)["items"]) - before.get(t, set()))
        if not stopped and not self.failed:
            self.a.state["rebuild_progress"].pop(self.scope_key, None)
            if not self.a.state["rebuild_progress"]:
                self.a.state.pop("rebuild_progress")
        self.store.save()
        from . import render
        render.render_all(self.store, self.cfg, self.a.health, days=set(), tickets=set(keys) | set(self.store.dirty_tickets))
        self.a._add_cost(common.workday(common.now(), self.cfg), self.usd)
        out.update(sessions=len(todo), calls=self.calls, usd=round(self.usd, 4), new_items=new, ignored=self.ignored,
                   failed=self.failed, stopped=stopped)
        rec = {"at": common.now_iso(), "started": at, "scope": self.scope_key, "sessions": len(todo),
               "chunks": chunks, "calls": self.calls, "usd": out["usd"], "archived": out["archived"],
               "new_items": new, "backup": out["backup"], "stopped": stopped}
        h = self.a.health
        h["rebuilds"] = (h.get("rebuilds") or [])[-(KEEP_RUNS - 1):] + [rec]
        common.log("agent", "rebuild %s" % json.dumps(rec))
        return out


def run(keys, all_=False, dry_run=False, cap_usd=None, brief=True, wait_s=None):
    """The rebuild (see the module docstring). A real run waits for a running tick like migrate.run, else returns
    {"skipped": why}."""
    from . import agent as agentmod
    if dry_run:
        return Rebuild(agentmod.Agent(force=True), keys, all_, cap_usd, brief).run(True)
    lock = agentmod.Lock()
    if not lock.acquire_wait(wait_s):
        return {"skipped": "the agent is busy (a tick holds the lock); run the rebuild again in a few minutes"}
    try:
        a = agentmod.Agent(force=True)
        res = Rebuild(a, keys, all_, cap_usd, brief, lock).run(False)
        if not res.get("refused"):
            agentmod.save_state(a)
        return res
    finally:
        lock.release()


USAGE = "usage: agent rebuild <KEY> [<KEY>...] | --all  [--dry-run] [--cap-usd N] [--no-brief]"


def main(argv):
    keys, cap, i = [], None, 0
    while i < len(argv):
        a = argv[i]
        if a == "--cap-usd" and i + 1 < len(argv):
            cap, i = argv[i + 1], i + 2
            continue
        if a.startswith("--cap-usd="):
            cap = a.split("=", 1)[1]
        elif not a.startswith("--"):
            keys.append(a)
        i += 1
    all_ = "--all" in argv
    try:
        cap = float(cap) if cap is not None else None
    except ValueError:
        cap = -1
    bad = [k for k in keys if not S.valid_ticket(k, None)]
    if bool(keys) == all_ or bad or (cap is not None and cap < 0):
        print(USAGE + ("\nnot a ticket key: " + ", ".join(bad) if bad else ""), file=sys.stderr)
        return 2
    res = run(keys, all_=all_, dry_run="--dry-run" in argv, cap_usd=cap, brief="--no-brief" not in argv)
    print(json.dumps(res, indent=1))
    return 3 if res.get("skipped") else (1 if res.get("refused") else 0)
