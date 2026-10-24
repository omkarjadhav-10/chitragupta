"""K3: what is still current in a ticket.

    close_todos       a TODO about a git action (rebase, push, open a PR, merge, cherry-pick to <target>) is done when the
                      ticket's branch and PR data shows it happened: status "done", closed_by "pr:<n>" / "branch:<name>"
    close_day_todos   the same for a day's open_todos ({text, ticket, closed_by})
    run_day           at day close, per ticket that got new items that day, model calls (purpose "supersede", each gated
                      by budget.model_daily_usd) that group items by subject and say which ones a newer item replaced;
                      a ticket with more candidates than one call holds gets several batches, so each is judged once

Nothing here rewrites an item's text or deletes one: only `status`, `subject`, `superseded_by` and `closed_by` are set.
Your own notes (inbox lines, kind "note") are never superseded by a model item.
"""
import json
import re

from . import common

STATUSES = ("current", "superseded", "hypothesis", "confirmed", "done")
SUPERSEDE_KINDS = ("root_cause", "finding", "decision", "fix", "how_it_works", "todo")
KEY_KINDS = ("root_cause", "decision", "fix")
INPUT_CAP = 60000  # chars of item JSON sent per call
MAX_ITEMS = 160  # items per call; a ticket with more candidates gets more calls (batches)
DETAIL_CHARS = 300


def is_user_note(it):
    return it.get("kind") == "note" or it.get("session") == "inbox" or it.get("src") == "user"


def is_current(it):
    """Shown in the current views: not archived, not superseded, not waiting for review (contract "current item")."""
    return isinstance(it, dict) and not it.get("archived") and it.get("status") != "superseded" and not it.get("review")


# --------------------------------------------------------------------------- deterministic TODO closure

_MR = re.compile(r"(?i)\bmerge request\b")
_ACTIONS = (("merge", re.compile(r"(?i)\bmerge\b")),
            ("cherry", re.compile(r"(?i)\bcherry[- ]?pick")),
            ("pr", re.compile(r"(?i)\b(open|raise|create|submit|file|put up)\b[^.;]{0,30}?\b(prs?|pull requests?|mrs?)\b")),
            ("push", re.compile(r"(?i)\bpush\b")),
            ("rebase", re.compile(r"(?i)\brebase\b")))
_TARGET = re.compile(r"(?i)\b(?:to|into)\s+(?:the\s+)?([A-Za-z0-9][A-Za-z0-9._/-]*)")
_NOT_TARGET = {"the", "a", "it", "remote", "origin", "github", "gitlab", "upstream", "be", "get", "do", "make", "review",
               "qa's"}


def git_actions(text):
    """{action: target or None} of a TODO's git actions ("merge", "cherry", "pr", "push", "rebase")."""
    s = _MR.sub("pull request", text or "")
    out = {}
    for name, rx in _ACTIONS:
        m = rx.search(s)
        if not m:
            continue
        tgt = None
        if name in ("merge", "cherry", "pr"):
            for t in _TARGET.finditer(s[m.start():]):
                w = t.group(1).rstrip(".,;:").lower()
                if w and w not in _NOT_TARGET:
                    tgt = w
                    break
        out[name] = tgt
    return out


def _norm_ref(x):
    x = (x or "").strip().lower()
    return x[len("origin/"):] if x.startswith("origin/") else x


def _target_ok(pr_target, want):
    if not want:
        return True
    pt = _norm_ref(pr_target)
    return pt == want or pt.startswith(want + "/") or pt.split("/")[-1] == want


def _facts(tk):
    repos = ((tk.get("git") or {}).get("repos")) or {}
    branches = [b for x in repos.values() for b in x.get("branches") or [] if isinstance(b, dict)]
    prs = [p for x in repos.values() for p in x.get("prs") or [] if isinstance(p, dict)]
    return branches, prs


def _when(p):
    return (p.get("merged_at") or p.get("created_at") or "")[:10]


def closed_by(tk, text, since=None):
    """"pr:<n>" / "branch:<name>" when the ticket's branch and PR data shows every git action of the TODO done (a PR
    covers rebase and push too), else None. A PR older than the TODO (`since`, YYYY-MM-DD) doesn't count."""
    acts = git_actions(text)
    if not acts:
        return None
    branches, prs = _facts(tk)
    low = (text or "").lower()
    named = [b.get("branch") for b in branches if b.get("branch") and b["branch"].lower() in low]
    prs = [p for p in prs if p.get("state") != "closed" and (not named or p.get("source") in named)
           and (not since or not _when(p) or _when(p) >= since)]
    prs.sort(key=_when, reverse=True)
    used = []
    for act, tgt in acts.items():
        if act == "merge":
            hit = next((p for p in prs if p.get("state") == "merged" and _target_ok(p.get("target"), tgt)), None)
        elif act in ("pr", "cherry", "rebase"):
            hit = next((p for p in prs if _target_ok(p.get("target"), tgt)
                        and (act != "cherry" or tgt or p.get("state") == "merged")), None)
        else:  # push: a PR, or the branch on the remote with nothing left to push
            hit = next(iter(prs), None) or next(
                ("branch:" + b["branch"] for b in branches if b.get("on_remote") and not int(b.get("unpushed") or 0)
                 and (not named or b.get("branch") in named) and b.get("status") != "abandoned"), None)
        if not hit:
            return None
        used.append(hit)
    pr = next((u for u in used if isinstance(u, dict)), None)
    return "pr:%s" % pr.get("number") if pr else next(u for u in used if isinstance(u, str))


def close_todos(tk):
    """Close the ticket's open TODO items that branch and PR data shows done. Returns how many it closed."""
    n = 0
    for it in (tk.get("items") or {}).values():
        if not isinstance(it, dict) or it.get("kind") != "todo" or it.get("archived") \
                or it.get("status") in ("done", "superseded"):
            continue
        by = closed_by(tk, "%s %s" % (it.get("title") or "", it.get("detail") or ""),
                       it.get("first_date") or it.get("date"))
        if by:
            it["status"], it["closed_by"] = "done", by
            n += 1
    return n


_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")


def todo_obj(x, prefixes=None, since=None):
    """A day TODO as {text, ticket, closed_by} (older files hold plain strings); `since` only on new ones."""
    if isinstance(x, dict):
        return x
    from . import store
    text = str(x or "")
    known = {p.upper() for p in prefixes} if prefixes else None
    t = next((k for k in (store.valid_ticket(m, known) for m in _KEY.findall(text)) if k), None)
    out = {"text": text, "ticket": t, "closed_by": None}
    if since:
        out["since"] = since
    return out


def todo_text(x):
    return x.get("text") or "" if isinstance(x, dict) else str(x or "")


def close_day_todos(store, todos):
    """Set closed_by on day TODOs whose ticket's branch and PR data shows them done. Returns how many it closed."""
    n = 0
    for x in todos or []:
        if not isinstance(x, dict) or x.get("closed_by") or not x.get("ticket"):
            continue
        by = closed_by(store.ticket(x["ticket"]), x.get("text"), x.get("since"))
        if by:
            x["closed_by"] = by
            n += 1
    return n


# --------------------------------------------------------------------------- per-ticket supersession (model)

def tickets_with_new_items(store, day):
    """The day's tickets that got an item first recorded that day."""
    dd = store.day(day)
    out = []
    for t, slot in sorted((dd.get("tickets") or {}).items()):
        items = store.ticket(t)["items"]
        if any((items.get(i) or {}).get("first_date") == day for i in slot.get("item_ids") or []):
            out.append(t)
    return out


def _candidates(tk):
    """The ticket's current candidates, newest first."""
    pool = [i for i in tk["items"].values() if isinstance(i, dict) and not i.get("archived") and not i.get("review")
            and (i.get("kind") in SUPERSEDE_KINDS or is_user_note(i)) and i.get("status") not in ("superseded", "done")]
    pool.sort(key=lambda i: (i.get("date") or "", i.get("time") or "", i.get("id")), reverse=True)
    return pool


def _is_key(i):
    return i.get("kind") in KEY_KINDS or is_user_note(i)


def _row(i):
    r = {"id": i["id"], "kind": i.get("kind"), "title": i.get("title") or "",
         "detail": common.truncate((i.get("detail") or "").strip(), DETAIL_CHARS), "day": i.get("date") or "",
         "status": i.get("status") or "current", "confidence": i.get("confidence") or ""}
    if is_user_note(i):
        r["by_user"] = True
    return r


def _size(r):
    return len(json.dumps(r, ensure_ascii=False)) + 2


def _batch(tk, sent):
    """One call's rows: the current root causes, decisions, fixes and notes of yours first (few, and what the rest is
    judged against; any that don't fit in half a call wait their turn with the rest), then the newest candidates not
    sent yet in this run (`sent`), within MAX_ITEMS and INPUT_CAP. [] when every candidate was sent."""
    pool = _candidates(tk)
    rows, size = [], 0
    for i in (i for i in pool if _is_key(i)):
        r = _row(i)
        if len(rows) >= MAX_ITEMS // 2 or size + _size(r) > INPUT_CAP // 2:
            break
        rows.append(r)
        size += _size(r)
    have = {r["id"] for r in rows}
    rest = [i for i in pool if i["id"] not in sent and i["id"] not in have]
    if not rest and have <= sent:
        return []
    for i in rest:
        r = _row(i)
        if len(rows) >= MAX_ITEMS or size + _size(r) > INPUT_CAP:
            break
        rows.append(r)
        size += _size(r)
    return rows


def supersede_input(store, t, day, sent=None):
    """One call's input for ticket `t`: its key items (root causes, decisions, fixes, your notes) and the newest of
    the candidates not in `sent`, each with id, kind, title, short detail, day and status."""
    tk = store.ticket(t)
    from . import store as S
    return {"ticket": t, "title": S.display_title(tk) or tk.get("title_hint") or "", "day": day,
            "items": _batch(tk, set(sent or ()))}


def _chain_reaches(items, start, target):
    seen = set()
    cur = start
    while cur and cur not in seen:
        if cur == target:
            return True
        seen.add(cur)
        nxt = items.get(cur) or {}
        cur = nxt.get("superseded_by") if nxt.get("status") == "superseded" else None
    return False


def apply(tk, out):
    """Apply a supersession answer: only ids that exist; text is never touched; a user note is never superseded by a
    model item; no cycles. Returns how many items changed."""
    items = tk.get("items") or {}
    n = 0
    for r in (out or {}).get("items") or []:
        iid = r.get("id") if isinstance(r, dict) else None
        it = items.get(iid)
        st = r.get("status") if it else None
        if not it or it.get("archived") or st not in STATUSES:
            continue
        before = (it.get("status"), it.get("superseded_by"), it.get("subject"))
        if r.get("subject"):
            it["subject"] = str(r["subject"])[:80]
        if st == "superseded":
            by = r.get("superseded_by")
            rep = items.get(by)
            if not rep or by == iid or rep.get("archived") or (is_user_note(it) and not is_user_note(rep)) \
                    or _chain_reaches(items, by, iid):
                if (it.get("status"), it.get("superseded_by"), it.get("subject")) != before:
                    n += 1
                continue
            it["status"], it["superseded_by"] = "superseded", by
        elif st == "done" and it.get("kind") not in ("todo", "question", "blocker"):
            continue
        else:
            it["status"], it["superseded_by"] = st, None
        if (it.get("status"), it.get("superseded_by"), it.get("subject")) != before:
            n += 1
    return n


def run_day(store, cfg, health, day, tickets=None):
    """One supersession call per ticket that got new items on `day`, while today's model budget allows. Returns
    (cost_usd, {ticket: changed items}). The call is recorded in the model-spend ledger by extract.call_claude."""
    from . import extract, modelbudget
    cost, done = 0.0, {}
    for t in tickets if tickets is not None else tickets_with_new_items(store, day):
        sent, batch = set(), 0
        while True:  # one call per batch, until every current candidate was sent once
            data = supersede_input(store, t, day, sent)
            ids = {r["id"] for r in data["items"]}
            if len(ids) < 2 or (batch and not ids - sent):
                break  # nothing (more) to compare
            if modelbudget.over_budget(health, cfg, "supersede"):
                if batch:
                    common.log("agent", "supersession %s: the budget stopped it after %d batch(es)" % (t, batch))
                return cost, done  # logged and counted under model_usage.<day>.skipped; the rest wait for tomorrow
            try:
                out, c = extract.call_claude("supersede", json.dumps(data, ensure_ascii=False), cfg)
            except extract.ExtractError as e:
                common.log("agent", "supersession failed for %s: %s" % (t, e))
                break
            batch += 1
            sent |= ids
            cost += c
            n = apply(store.ticket(t), out)
            done[t] = done.get(t, 0) + n
            if n:
                store.touch_ticket(t)
            common.log("agent", "supersession %s day %s batch %d: %d item(s) changed, cost=$%.4f" % (
                t, day, batch, n, c))
    return cost, done
