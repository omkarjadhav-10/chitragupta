"""K8: an item that may not belong to its ticket is flagged for review, never moved and never deleted.

At merge time a new item gets `review = {why, suggest, at}` when
  (a) it names another ticket key and not its own, unless its turn's prompt (or pasted Jira title) named this ticket
      or it shares a file, table, endpoint or title word with this ticket (then it says how the two relate, e.g.
      "ABC-1002's commit is the fix", and stays);
  (b) its ticket came from the branch, an earlier turn (carry) or the model, and it shares no file, table, endpoint or
      title word (4+ letters, common words left out) with the ticket's current items or its Jira title;
  (c) it is about the worklog tool itself (.worklog, /cg:, Chitragupta) while the ticket is work.
Flagged items are left out of the current views and listed under "Needs review". An inbox line resolves one:
"<KEY>: keep <id>" clears the flag; "<KEY>: move <id> <OTHER>" copies the item to OTHER (src "user") and archives the
original {reason: "moved to OTHER"}.
"""
import re

from . import common

STOP = set("""
about above after again against also among another because been before being below between both cannot change changed
changes check checked checks class code could data does doing done down during each either error errors false field
fields file files first found from have having here into item items just last like line lines made make many method
might more most must need needs never none note notes null only other over path paths query request response returns
same service should show shows since some still such table tables test tests than that their them then there these
they this those through time true under until update updated updates used uses using value values very want what when
where which while will with within without work worked would your ticket tickets issue issues fixed step steps flow
logic case cases call calls added adds java python json string number list result results run runs running
""".split())
_WORD = re.compile(r"[a-z][a-z0-9_]{3,}")
_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
_SELF = re.compile(r"(?i)\.worklog\b|/cg:|\bchitragupta\b")
SPECIFIC_SRC = ("prompt", "jira-title")
WEAK_SRC = ("branch", "carry", "model")


def words(text):
    return {w for w in _WORD.findall((text or "").lower()) if w not in STOP}


def _item_text(it):
    return " ".join([it.get("title") or "", it.get("detail") or ""] + [str(e) for e in it.get("evidence") or []])


def profile(tk):
    """Words that tie an item to the ticket: its Jira title, its current items' titles and evidence, and its files,
    tables and endpoints."""
    from . import store, supersede
    out = words(store.display_title(tk))
    for it in (tk.get("items") or {}).values():
        if supersede.is_current(it):
            out |= words(it.get("title")) | words(" ".join(str(e) for e in it.get("evidence") or []))
    for k in ("files", "tables"):
        for key in tk.get(k) or {}:
            name = str(key).rsplit(".", 1)[-1] if k == "tables" else str(key)
            out |= words(name.replace("/", " ").replace(".", " "))
    for a in (tk.get("apis") or {}).values():
        if isinstance(a, dict):
            out |= words(" ".join(a.get("paths") or [a.get("path") or ""]).replace("/", " "))
    return out


def _work_ticket(tk):
    from . import store
    return not _SELF.search("%s %s" % (store.display_title(tk), tk.get("title_hint") or "")) and \
        "worklog" not in (store.display_title(tk) or "").lower()


def check(tk, t, rec, prefixes):
    """The review flag for a new item `rec` of ticket `t`, or None."""
    from . import store
    text = _item_text(rec)
    known = {p.upper() for p in prefixes or []} or None
    keys = []
    for m in _KEY.findall(text):
        k = store.valid_ticket(m, known)
        if k and k not in keys:
            keys.append(k)
    others = [k for k in keys if k != t]
    src = rec.get("src")
    why, suggest, prof = None, None, None
    if others and t not in keys and src not in SPECIFIC_SRC:
        prof = profile(tk)
        if not (words(text) & prof):
            why, suggest = "names %s, not %s" % (", ".join(others[:3]), t), others[0]
    if not why and _SELF.search(text) and _work_ticket(tk):
        why = "about the worklog tool itself, not the ticket's work"
    elif not why and src in WEAK_SRC:
        prof = profile(tk) if prof is None else prof
        if prof and not (words(text) & prof):
            why = "shares no file, table, endpoint or title word with %s (ticket from %s)" % (t, src)
    if not why:
        return None
    return {"why": why, "suggest": suggest, "at": common.now_iso()}


def record_text(rec):
    return " ".join(str(rec.get(k) or "") for k in
                    ("text", "purpose", "revealed", "name", "path", "host_or_service", "tables"))


def record_check(tk, t, kind, key, rec, prefixes):
    """K8 for records. Neither a record itself nor other suspect records establish overlap."""
    old = (tk.get(kind) or {}).get(key) or {}
    if old.get("review_kept") or old.get("archived"):
        return None
    view = dict(tk)
    for family in ("queries", "tables", "apis"):
        good = {}
        for rid, row in (tk.get(family) or {}).items():
            if not isinstance(row, dict) or (family == kind and rid == key) or row.get("review"):
                continue
            keys = set(_KEY.findall(record_text(row)))
            suspect = keys and t not in keys and row.get("src") not in SPECIFIC_SRC and not row.get("review_kept")
            if not suspect:
                good[rid] = row
        view[family] = good
    proxy = dict(rec, title=record_text(rec), detail="")
    if proxy.get("src") in (None, "legacy", "carry-recent"):
        proxy["src"] = "carry"
    return check(view, t, proxy, prefixes)


def recheck(tk, t, prefixes, at=None):
    """Clear the review flag of items that check() would no longer flag (kb5-review-recheck). Never touches an item
    you resolved (kept, or archived by a move). Returns the ids cleared."""
    out = []
    for iid, it in sorted((tk.get("items") or {}).items()):
        if not isinstance(it, dict) or not it.get("review") or it.get("review_kept") or it.get("archived"):
            continue
        if check(tk, t, it, prefixes) is None:
            it["review"] = None
            it["review_cleared"] = {"at": at or common.now_iso(),
                                    "reason": "recheck: shares a file, table, endpoint or title word with %s" % t}
            out.append(iid)
    return out


_DIRECTIVE = re.compile(r"(?i)^\s*(keep|move)\s+([0-9a-f]{10})(?:\s+(?:to\s+)?([A-Za-z][A-Za-z0-9]{1,9}-\d{1,6}))?\s*\.?\s*$")


def parse_directive(text):
    """("keep", id, None) / ("move", id, KEY) for an inbox line's text after "<KEY>:", else None."""
    m = _DIRECTIVE.match(text or "")
    if not m:
        return None
    verb, iid, key = m.group(1).lower(), m.group(2).lower(), (m.group(3) or "").upper() or None
    if (verb == "move") != bool(key):
        return None
    return verb, iid, key


def apply_directive(store, t, d, ts, prefixes):
    """Resolve one review: keep clears the flag; move copies the item to the other ticket and archives the original.
    Returns a short message (logged)."""
    verb, iid, key = d
    tk = store.ticket(t)
    it = (tk.get("items") or {}).get(iid)
    if not it:
        return "no item %s in %s" % (iid, t)
    if verb == "keep":
        it["review"], it["review_kept"] = None, ts
        store.touch_ticket(t)
        return "kept %s in %s" % (iid, t)
    from . import store as S
    dest = S.valid_ticket(key, {p.upper() for p in prefixes} if prefixes else None)
    if not dest or dest == t:
        return "not a ticket to move to: %s" % key
    if it.get("archived"):
        return "%s in %s was already archived" % (iid, t)
    copy = {k: v for k, v in it.items() if k not in ("review", "review_kept", "status", "superseded_by", "subject",
                                                     "closed_by", "history", "archived")}
    nid = common.stable_id(dest, copy.get("kind"), copy.get("title"))
    copy.update(id=nid, src="user", moved_from="%s/%s" % (t, iid), review=None)
    S.touch_ticket_meta(store, dest, it.get("date") or (ts or "")[:10])
    items = store.ticket(dest)["items"]
    if nid in items:
        items[nid]["evidence"] = S._union(items[nid].get("evidence"), copy.get("evidence"))[:10]
    else:
        items[nid] = copy
    it["archived"] = {"at": ts, "reason": "moved to %s" % dest}
    store.touch_ticket(t)
    store.touch_ticket(dest)
    return "moved %s from %s to %s as %s" % (iid, t, dest, nid)
