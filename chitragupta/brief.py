"""The ticket brief (FX-20): what a ticket's surfaces render first.

# stub: replaced by the brief engine
Only view(tk) lives here, so the dashboard, the hub README, the export, the SessionStart context and the MCP server can
render the brief before the engine (generation, current_items, stale, fixes_made) lands. view() returns the shape the
renderers read:

    {"key", "has_brief": bool, "epic": bool, "kind": "bug" | "story" | "epic",
     "header": {"key", "title", "jira_type", "jira_status", "status_inferred", "stage": {"kind", "text"} | None,
                "repo", "branch", "pr": {"number", "state", "target", "url"} | None, "updated": iso | ""},
     "stale": {"since": iso, "new": n} | None,
     "sections": [{"id", "heading", ...}],   # in order; ids root_cause, findings, before, after, proposed,
                                             # fixes_made, cons (see the renderers for each one's fields)
     "fallback": {"root_cause": {"text", "confidence", "cites"} | None, "stage", "tldr", "problem"} | None,
     "children": [{"key", "title", "status", "line"}]}      # an Epic: a roll-up instead of prose

A cite is an evidence id ("c607fa7d13", "commit:a1b2c3d", "query:<id>", …) or {"id", "label", "kind"}.
"""
import os

from . import store as _store

PARENT_TYPES = ("epic", "initiative")


def _header(tk):
    from . import delivery
    git = tk.get("git") or {}
    st = delivery.stage(git)
    prs = [p for x in (git.get("repos") or {}).values() for p in x.get("prs") or [] if isinstance(p, dict)]
    pr = sorted(prs, key=lambda p: (p.get("merged_at") or p.get("created_at") or "", str(p.get("number"))))[-1] if prs else None
    branches = [b.get("branch") for x in (git.get("repos") or {}).values() for b in x.get("branches") or [] if b.get("branch")]
    branches = branches or [b for b in tk.get("branches") or [] if b and not str(b).startswith("refs/")]
    repos = [os.path.basename(str(r).rstrip("/\\")) for r in tk.get("repos") or [] if r]
    jira = tk.get("jira") if isinstance(tk.get("jira"), dict) else {}
    return {"key": tk.get("id") or "", "title": _store.display_title(tk), "jira_type": jira.get("type") or "",
            "jira_status": _store.jira_status(tk), "status_inferred": tk.get("status_inferred") or "",
            "stage": {"kind": st["kind"], "text": st["text"]} if st else None,
            "repo": repos[0] if repos else "", "branch": branches[0] if branches else "",
            "pr": {"number": pr.get("number"), "state": pr.get("state") or "", "target": pr.get("target") or "",
                   "url": pr.get("url") or ""} if pr else None,
            "updated": ((tk.get("brief") or {}).get("generated_at") or "") if isinstance(tk.get("brief"), dict) else ""}


def _fallback_root_cause(tk):
    rank = {"confirmed": 0, "likely": 1, "hypothesis": 2}
    rcs = [i for i in (tk.get("items") or {}).values() if isinstance(i, dict) and i.get("kind") == "root_cause"
           and not i.get("archived") and (i.get("status") or "current") != "superseded" and not i.get("review")]
    if not rcs:
        return None
    rcs.sort(key=lambda i: (i.get("date") or "", i.get("time") or ""), reverse=True)
    rcs.sort(key=lambda i: rank.get(i.get("confidence"), 3))
    i = rcs[0]
    text = (i.get("title") or "").strip()
    if i.get("detail"):
        text += ": " + i["detail"].strip()
    return {"text": text, "confidence": i.get("confidence") or "none", "cites": [i.get("id")] if i.get("id") else []}


def view(tk, key=None):
    """The brief ready to render (see the module docstring). Pure: reads only `tk`."""
    tk = tk or {}
    if isinstance(tk.get("_brief_view"), dict):
        return tk["_brief_view"]
    head = _header(tk)
    if key:
        head["key"] = key
    epic = (head["jira_type"] or "").lower() in PARENT_TYPES
    out = {"key": head["key"], "has_brief": False, "epic": epic, "kind": "epic" if epic else "bug", "header": head,
           "stale": None, "sections": [], "children": [],
           "fallback": None if epic else {"root_cause": _fallback_root_cause(tk), "stage": head["stage"],
                                          "tldr": tk.get("tldr") or "", "problem": tk.get("problem") or ""}}
    return out

