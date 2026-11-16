"""Synthetic inputs for the Evidence Graph tests (ABC-…, acme.test, shop). Nothing here is real data."""
import json
import os

from chitragupta import companion, companionplanning as planning
from tests.test_companion_state import JIRA, Base as _CompanionBase, plan_body

FETCHED = "2026-01-05T10:00:00+00:00"


def criteria_rows(texts):
    """Confirmed criteria as the Companion keeps them when they come from the card: acN with the cited fragment."""
    return [{"id": "ac%d" % i, "text": text,
             "fragment": {"field": "description", "index": i, "digest": planning.fingerprint(text)}}
            for i, text in enumerate(texts, 1)]


class Workflows(_CompanionBase):
    """Real workflow records in a temp home (frozen clock, every process start a tripwire)."""

    def workflow(self, texts, key="ABC-1001", repo=None, steps=None, plan=True, approve=False):
        rec = companion.create(key, repo or self.repo, JIRA, criteria_rows(texts))
        if plan:
            rec = companion.save_plan(rec["id"], rec["revision"], plan_body(steps))
        if plan and approve:
            rec = self.approve(rec)
        return rec


def card(key, criteria, truncated=False, error=None, found=True, comments=(), fetched_at=FETCHED):
    """A `data/jira/<KEY>.json` record whose description lists `criteria` under an acceptance-criteria heading.

    truncated  the description ran into the card's cap: one more criterion is cut off mid-sentence
    error      the last read failed; the earlier card content is kept, as the card job keeps it
    found      False: Jira said the issue does not exist"""
    description = "The shop order list needs a filter. Acceptance criteria:"
    description += "".join(" - %s" % c for c in criteria)
    if truncated:
        description += " - The export keeps the chosen fil… [truncated]"
    body = {"found": bool(found), "summary": "Shop order list filter", "status": "In Progress"}
    if found:
        body.update(description=description,
                    comments=[{"id": str(i), "text": text, "at": fetched_at} for i, text in enumerate(comments, 1)])
    return {"key": key, "fetched_at": fetched_at, "card": body, "raw": None, "error": error,
            "fields": "summary,status,description,comment"}


def write_card(home, key, criteria, **kw):
    """Write that record where the card job would (tests pass their temp home); → the record."""
    rec = card(key, criteria, **kw)
    path = os.path.join(home, "data", "jira", key + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(rec, f, indent=2, sort_keys=True)
    return rec
