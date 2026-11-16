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


SHOP = "github:github.com/acme/shop"


def sha(n):
    """A full 40-character commit id that is easy to tell apart: sha(1) starts 0000001…"""
    return "%07x" % n + "c" * 33


def commit_rec(n, subject, repo="shop", files=(), ref="feature/ABC-1001-order-filter", date="2026-01-05T09:00:00+00:00"):
    """One `tk.commits` entry, as the agent writes it (own commits only)."""
    full = sha(n)
    return {"sha": full, "short": full[:9], "repo_name": repo, "date": date, "ref": ref, "subject": subject,
            "ticket": "ABC-1001", "files_count": len(files), "files": list(files)}


def file_rec(path, repo="shop", repo_root=None, committed=False, verified_by="edit"):
    """One `tk.files` entry; `repo_root` is the clone it was edited in, when the record has one."""
    rec = {"path": path, "role": "", "changed": True, "last": "2026-01-05", "verified": True,
           "verified_by": verified_by, "committed": committed, "repo": repo}
    if repo_root:
        rec["repo_root"] = repo_root
    return rec


def delivery_row(shas, loose=(), cells=None, subject="ABC-1001 Add the order filter", rid="p" * 12):
    """One stored delivery row: the record keeps 9-character commit prefixes."""
    return {"id": rid, "shas": [s[:9] for s in shas], "loose": [s[:9] for s in loose], "subject": subject,
            "date": "2026-01-05T09:00:00+00:00", "branches": ["feature/ABC-1001-order-filter"], "cells": cells or {}}


def pull_request(number, state="open", merge_commit=None, head=None, source="feature/ABC-1001-order-filter",
                 target="main"):
    return {"number": number, "title": "ABC-1001 Add the order filter", "source": source, "target": target,
            "state": state, "review": "", "created_at": "2026-01-05T09:30:00+00:00",
            "merged_at": "2026-01-06T09:00:00+00:00" if state == "merged" else None, "merge_commit": merge_commit,
            "head_at_merge": head if state == "merged" else None, "url": "https://github.com/acme/shop/pull/%d" % number,
            "src": "gh", "head": head}


def ticket_record(key="ABC-1001", commits=(), files=(), repos=None, **fields):
    """A `data/tickets/<KEY>.json` record with the parts the graph reads."""
    from chitragupta import store
    tk = store.new_ticket(key)
    tk["commits"] = {c["sha"]: c for c in commits}
    tk["files"] = {"%s|%s" % (f.get("repo_root") or "", f["path"]) if f.get("repo_root") else f["path"]: f for f in files}
    if repos is not None:
        tk["git"] = {"repos": repos}
    tk.update(fields)
    return tk


def repo_json(path, host="github", remote_path="acme/shop", **extra):
    """`data/git/<repo_key>/repo.json`: `path` is the LOCAL folder, `remote_path` the owner/repo."""
    meta = {"path": path.replace("\\", "/"), "host": host}
    if remote_path:
        meta["remote_path"] = remote_path
    meta.update(extra)
    return meta


def ledger(path, repo_id=SHOP):
    """An activity ledger of one repository location, cut to what the graph reads."""
    return {"v": 1, "repo": {"id": repo_id, "path": path}, "events": []}


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
