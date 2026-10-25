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


_RECORD_KEY = re.compile(r"(?:[^\s]+|(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|ANY) /[^\s]*)", re.I)
_DIRECTIVE = re.compile(r"(?i)^\s*(keep|move)\s+(.+?)\s*$")


def parse_directive(text):
    """Keep/move accepts an item/query id, qualified table key or complete API key."""
    m = _DIRECTIVE.fullmatch(text or "")
    if not m:
        return None
    verb, key = m.group(1).lower(), m.group(2).strip()
    if verb == "keep":
        key = key.rstrip(".")
        return (verb, key, None) if _RECORD_KEY.fullmatch(key) else None
    dest = re.fullmatch(r"(.+?)\s+(?:to\s+)?([A-Za-z][A-Za-z0-9]{1,9}-\d{1,6})\s*\.?", key, re.I)
    if not dest or not _RECORD_KEY.fullmatch(dest.group(1).strip()):
        return None
    return verb, dest.group(1).strip(), dest.group(2).upper()


def record_reviews(tk):
    """Review rows share the existing finding UI; the id is the exact inbox record key."""
    out = []
    for kind in ("queries", "tables", "apis"):
        for key, rec in (tk.get(kind) or {}).items():
            if isinstance(rec, dict) and rec.get("review") and not rec.get("archived"):
                out.append(dict(rec, id=key, kind=kind[:-1], title=rec.get("purpose") or rec.get("name") or key,
                                detail=rec.get("text") or rec.get("path") or "", date=rec.get("date") or rec.get("last") or ""))
    return out


def _resolve_record(st, t, kind, iid, rec, verb, key, ts, prefixes):
    import copy
    from . import store as S
    if verb == "keep":
        rec.update(review=None, review_kept=ts)
        st.touch_ticket(t)
        return True, "kept %s in %s" % (iid, t)
    dest = S.valid_ticket(key, {p.upper() for p in prefixes} if prefixes else None)
    if not dest or dest == t:
        return False, "not a ticket to move to: %s" % key
    if rec.get("archived"):
        return False, "%s in %s was already archived" % (iid, t)
    row = copy.deepcopy({k: v for k, v in rec.items() if k not in ("review", "review_kept", "archived", "relevance", "hidden_reason", "variant_of")})
    nid = S.query_id(dest, row.get("text")) if kind == "queries" else iid
    if kind == "queries":
        row["id"] = nid
    row.update(src="user", review=None, review_kept=ts, moved_from="%s/%s" % (t, iid))
    S.touch_ticket_meta(st, dest, rec.get("date") or rec.get("last") or ts[:10])
    records = st.ticket(dest)[kind]
    records[nid] = S._merge_rec(records.get(nid), row)
    records[nid].update(review=None, review_kept=ts)
    rec["archived"] = {"at": ts, "reason": "moved to %s" % dest}
    if kind in ("tables", "apis"):
        S._merge_catalog(st.catalog()[kind], nid, row, dest, ts[:10])
        st.catalog_dirty = True
    st.touch_ticket(t)
    st.touch_ticket(dest)
    return True, "moved %s from %s to %s as %s" % (iid, t, dest, nid)


def apply_directive(store, t, d, ts, prefixes):
    """Resolve one review: keep clears the flag; move copies the item to the other ticket and archives the original.
    Returns (ok, message); failed directives fall through to normal inbox notes."""
    verb, iid, key = d
    tk = store.ticket(t)
    it = (tk.get("items") or {}).get(iid)
    if not it:
        matches = [(kind, k, row) for kind in ("queries", "tables", "apis")
                   for k, row in (tk.get(kind) or {}).items() if k.casefold() == iid.casefold()]
        if len(matches) == 1:
            kind, actual, row = matches[0]
            return _resolve_record(store, t, kind, actual, row, verb, key, ts, prefixes)
        return False, "no unambiguous item or record %s in %s" % (iid, t)
    if verb == "keep":
        it["review"], it["review_kept"] = None, ts
        store.touch_ticket(t)
        return True, "kept %s in %s" % (iid, t)
    from . import store as S
    dest = S.valid_ticket(key, {p.upper() for p in prefixes} if prefixes else None)
    if not dest or dest == t:
        return False, "not a ticket to move to: %s" % key
    if it.get("archived"):
        return False, "%s in %s was already archived" % (iid, t)
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
    return True, "moved %s from %s to %s as %s" % (iid, t, dest, nid)
