"""Team sharing: the ticket sections you pick, redacted, as a bundle your team can read.

    sections()                                           -> [{id, label, default}] the sections you can pick
    preview(key, sections=None, level=None)              -> the bundle's README, what was removed per section and the
                                                            scan result; writes nothing
    share(key, sections=None, level=None, target=None)   -> writes the bundle only when the scan passes
    team_notes(target=None)                              -> every bundle in the target(s), yours and your teammates'
    read_note(target, rel)                               -> one bundle's README (read-only)
    targets()                                            -> the configured targets, each with its problem (or None)
    refresh(target)                                      -> a git target's clone brought up to date (no push)

A bundle is shares/<KEY>/<YYYYMMDD-HHMM>-<level>/README.md (the chosen sections as Markdown) + ticket.html (the
ticket export, same level, same sections). Levels:

  team    (default) secrets, patient data, QA values and rows (QA trace, query result samples, request setup,
          headers and bodies), local paths and "My notes" are removed; internal service names, hosts, SQL text,
          branch names and PR links stay.
  public  export.shareable: the public redaction pass, no hosts, no PR links, no SQL text, plus the deny-list's
          `internal:` names -> <internal>. Needs a deny-list.

Never included at any level: .env files, raw captures, the local file list, Jira fields, "My notes".

The gate: the bundle (README and the ticket data inside ticket.html) is scanned with scanlib — the deny-list
(share.denylist, default <home>/.worklog/denylist.txt; at the team level its `internal:` names are allowed) and the
secret rules. Any hit blocks the share: nothing is written and the hits are returned.

Targets (config share.targets): {"name", "kind": "folder", "path"} writes under <path>/shares/ only;
{"name", "kind": "git", "url", "branch": "main"} clones the team repo to <home>/share/<name>, then adds, commits
and pushes shares/** only, never forced, never rewriting history. Both through chitragupta/safety (path root and
the git-share purpose). Sharing never posts to Slack.
"""
import datetime as _dt
import json
import os
import re

from . import __version__
from . import common
from . import export
from . import gitsync
from . import scanlib
from .safety import commands, log, paths

LEVELS = ("team", "public")
EXPORT_MODE = {"team": "team", "public": "shareable"}
SECTIONS = [  # (id, label, picked by default)
    ("summary", "Summary", True),
    ("findings", "Findings", True),
    ("requests", "Requests", True),
    ("queries", "Queries", True),
    ("dbflow", "DB flow", True),
    ("branches", "Branches & PRs", True),
    ("artifacts", "Artifacts", True),
    ("timeline", "Timeline", True),
    ("notes", "Ticket notes (README)", False),
]
SECTION_IDS = [s[0] for s in SECTIONS]
LABEL = {s[0]: s[1] for s in SECTIONS}
# The ticket fields (dashboard api.ticket) each section owns, and what they become when the section isn't picked.
OWN = {
    "summary": {"status": "", "first": "", "last": "", "minutes": 0, "tldr": "", "problem": "", "repos": [],
                "title_source": ""},
    "findings": {"findings": []},
    "requests": {"requests": {"steps": [], "setup": "", "scenarios": [], "dropped": 0, "captured": 0,
                              "bruno_folder": None}},
    "queries": {"queries": []},
    "dbflow": {"dbflow": {"markdown": "", "qa_trace": None}},
    "branches": {"branches": [], "git": {}, "alerts": []},
    "artifacts": {"artifacts": []},
    "timeline": {"timeline": []},
    "notes": {"readme": ""},
}
NEVER = ["raw captures", ".env files", "the local file list and paths", "Jira fields", "My notes"]
KIND_TITLE = {"root_cause": "Root cause", "finding": "Findings", "how_it_works": "How it works", "fix": "Fix",
              "decision": "Decisions", "config": "Configuration", "repro_test": "Repro and tests",
              "reference": "References", "blocker": "Blockers", "question": "Open questions", "todo": "To do"}
SHARE_DIR = paths.SHARE_DIR
BUNDLE_RE = re.compile(r"^\d{8}-\d{4}-(team|public)(?:-\d+)?$")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,6}$")
META = re.compile(r"^<!-- cg-share (\{.*?\}) -->")


class ShareError(Exception):
    """A share that can't go ahead (unknown ticket, bad section or level, a target that isn't usable)."""


# --------------------------------------------------------------------------- config

def _conf(cfg=None):
    cfg = common.load_config() if cfg is None else cfg
    s = cfg.get("share")
    return s if isinstance(s, dict) else {}


def default_level(cfg=None):
    lv = _conf(cfg).get("default_level") or "team"
    return lv if lv in LEVELS else "team"


def denylist_path(cfg=None):
    p = _conf(cfg).get("denylist")
    return p.strip() if isinstance(p, str) and p.strip() else common.wl("denylist.txt")


def deny_rules(cfg=None):
    """The share deny-list's rules ([] when there is none)."""
    return scanlib.load_denylist(denylist_path(cfg))


def targets(cfg=None):
    """[{name, kind, path|url, branch, default_level, problem}] — problem None when the target can be used."""
    out = []
    for t in _conf(cfg).get("targets") or []:
        if not isinstance(t, dict):
            continue
        x = dict(t)
        x["problem"] = commands.share_target_reason(t)
        if x.get("kind") == "git":
            x["branch"] = str(t.get("branch") or "main")
            x["dir"] = commands.share_repo_dir(str(t.get("name") or "")) if not x["problem"] else None
        if not x["problem"] and x.get("kind") == "folder" and not paths.share_folder_roots({"share": {"targets": [t]}}):
            x["problem"] = "the folder %s doesn't exist (or isn't an absolute folder path)" % t.get("path")
        dl = t.get("default_level")
        if dl is not None and dl not in LEVELS and not x["problem"]:
            x["problem"] = "default_level must be team or public"
        out.append(x)
    return out


def resolve_target(name=None, cfg=None):
    """The target to share to: by name, else the first configured one. Raises ShareError, or Blocked (logged) for
    a target the safety layer refuses — e.g. anything that would post to Slack."""
    ts = targets(cfg)
    if not ts:
        raise ShareError("no share target configured (share.targets in config.json; see docs/sharing.md)")
    t = next((x for x in ts if x.get("name") == name), None) if name else ts[0]
    if t is None:
        raise ShareError("no share target named %r (configured: %s)" % (name, ", ".join(str(x.get("name")) for x in ts)))
    raw = {k: v for k, v in t.items() if k not in ("problem", "dir")}
    why = commands.share_target_reason(raw)
    if why:
        log.block("command", why, "share target %s" % json.dumps(raw, ensure_ascii=False)[:300], "share", "share")
    if t["problem"]:
        raise ShareError("share target %s: %s" % (t.get("name"), t["problem"]))
    return t


# --------------------------------------------------------------------------- picking sections

def sections():
    return [{"id": i, "label": lab, "default": d} for i, lab, d in SECTIONS]


def pick(selected=None):
    """(section ids in order, finding kinds or None) from None (the defaults), a list or "a,b,findings.root_cause"."""
    from .dashboard import api
    if selected is None or selected == "" or selected == []:
        return [i for i, _l, d in SECTIONS if d], None
    if isinstance(selected, str):
        selected = [x for x in re.split(r"[,\s]+", selected) if x]
    ids, kinds = set(), set()
    for s in selected:
        s = str(s).strip().lower()
        if s == "all":
            ids.update(SECTION_IDS)
        elif s.startswith("findings.") or s.startswith("findings:"):
            k = s.split(s[8], 1)[1]
            if k not in api.FINDING_KINDS:
                raise ShareError("unknown finding kind %r (one of %s)" % (k, ", ".join(api.FINDING_KINDS)))
            ids.add("findings")
            kinds.add(k)
        elif s in SECTION_IDS:
            ids.add(s)
            if s == "findings":
                kinds.add("*")
        else:
            raise ShareError("unknown section %r (one of %s, or findings.<kind>)" % (s, ", ".join(SECTION_IDS)))
    if not ids:
        raise ShareError("no section picked")
    return [i for i in SECTION_IDS if i in ids], (None if not kinds or "*" in kinds else kinds)


# --------------------------------------------------------------------------- what was removed

_STAT = {"public-qa-value": "qa", "public-uuid": "qa", "public-digits": "qa", "public-qa-host": "qahost",
         "public-sql-block": "sql", "public-sql": "sql", "public-payload-block": "payload", "public-payload": "payload",
         "local-path": "path", "host": "host", "internal-name": "internal", "request-setup": "setup",
         "request-body": "body", "query-sample": "sample", "qa-trace": "trace", "qa-example": "example",
         "pr-link": "pr", "my-notes": "notes"}
_KIND = [  # (key, singular, plural) in report order
    ("secret", "secret", "secrets"), ("patient", "patient-data value", "patient-data values"),
    ("qa", "QA value", "QA values"), ("qahost", "QA host", "QA hosts"), ("sample", "query result sample",
                                                                         "query result samples"),
    ("trace", "QA trace", "QA traces"), ("example", "QA worked example", "QA worked examples"),
    ("setup", "request setup", "request setups"), ("body", "request headers/body", "request headers/bodies"),
    ("payload", "payload", "payloads"), ("sql", "SQL text", "SQL texts"), ("path", "local path", "local paths"),
    ("host", "host", "hosts"), ("pr", "PR link", "PR links"), ("internal", "internal name", "internal names"),
    ("notes", "My notes", "My notes"),
]


def removed(stats):
    """[{kind, count}] from the redaction stats of one section."""
    from .redact import PATIENT_RULES
    agg = {}
    for name, n in (stats or {}).items():
        k = _STAT.get(name) or ("patient" if name in PATIENT_RULES else "secret")
        agg[k] = agg.get(k, 0) + int(n or 0)
    out = []
    for k, one, many in _KIND:
        if agg.get(k):
            out.append({"kind": one if agg[k] == 1 else many, "count": agg[k]})
    return out


def removed_text(items):
    """"3 QA values, 1 local path, My notes" ("" when nothing was removed)."""
    return ", ".join(x["kind"] if x["kind"] == "My notes" else "%d %s" % (x["count"], x["kind"]) for x in items)


# --------------------------------------------------------------------------- Markdown

def _fence(text, lang=""):
    n = max([3] + [len(m) + 1 for m in re.findall(r"`{3,}", text or "")])
    return "%s%s\n%s\n%s" % ("`" * n, lang, (text or "").rstrip("\n"), "`" * n)


def _demote(md, by=2):
    """Headings one or two levels down (outside code fences), so a file's own '# Title' sits under ours."""
    out, fence = [], None
    for line in (md or "").splitlines():
        m = re.match(r"^\s*(`{3,}|~{3,})", line)
        if m:
            fence = None if fence and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) else (fence or m.group(1))
        elif not fence:
            h = re.match(r"^(#{1,6})(\s.*)$", line)
            if h:
                line = "#" * min(6, len(h.group(1)) + by) + h.group(2)
        out.append(line)
    return "\n".join(out).strip("\n")


def _one_line(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _md_summary(t):
    L = []
    when = " → ".join(x for x in (t.get("first"), t.get("last")) if x)
    bits = [x for x in ("Status: " + t["status"] if t.get("status") else "", "worked " + when if when else "",
                        "≈ " + common.fmt_minutes(t.get("minutes")) if t.get("minutes") else "") if x]
    if bits:
        L.append("- " + " · ".join(bits))
    if t.get("repos"):
        L.append("- Repos: " + ", ".join(t["repos"]))
    if t.get("tldr"):
        L += ["", "**TL;DR:** " + _one_line(t["tldr"])]
    if t.get("problem"):
        L += ["", "**Problem:** " + t["problem"].strip()]
    return L


def _md_findings(t):
    L, by = [], {}
    for f in t.get("findings") or []:
        by.setdefault(f.get("kind") or "finding", []).append(f)
    for kind, items in by.items():
        L += ["### " + KIND_TITLE.get(kind, kind), ""]
        for f in items:
            meta = ", ".join(x for x in (f.get("confidence"), f.get("date")) if x)
            line = "- **%s**%s" % (_one_line(f.get("title")), " (%s)" % meta if meta else "")
            if f.get("detail"):
                line += " — " + _one_line(f["detail"])
            L.append(line)
            ev = [e for e in (f.get("evidence") or []) if isinstance(e, str) and e]
            if ev:
                L.append("  Evidence: " + "; ".join("`%s`" % e.replace("`", "'") for e in ev))
        L.append("")
    return L


def _md_requests(t):
    rq = t.get("requests") or {}
    L = []
    for s in rq.get("steps") or []:
        call = " ".join(x for x in (s.get("method"), s.get("url")) if x)
        line = "%s. **%s**" % (s.get("n") or len(L) + 1, _one_line(s.get("name")) or "request")
        if call:
            line += " — `%s`" % call.replace("`", "'")
        if s.get("expected"):
            line += " → expected " + _one_line(s["expected"])
        L.append(line)
    names = [sc.get("name") for sc in rq.get("scenarios") or [] if sc.get("name")]
    if names:
        L += ["", "Scenarios: " + ", ".join(names)]
    return L


def _md_queries(t):
    L = []
    for q in t.get("queries") or []:
        meta = ", ".join(x for x in (q.get("dialect"), q.get("kind")) if x)
        L.append("### %s%s" % (_one_line(q.get("purpose")) or q.get("id") or "query", " (%s)" % meta if meta else ""))
        if q.get("tables"):
            L.append("Tables: " + ", ".join("`%s`" % x for x in q["tables"]))
        L += ["", _fence(q.get("text") or "", "sql"), ""]
    return L


def _md_dbflow(t):
    md = ((t.get("dbflow") or {}).get("markdown") or "").strip()
    return [_demote(md)] if md else []


def _md_branches(t):
    L = []
    g = t.get("git") or {}
    if g.get("summary"):
        L += [_one_line(g["summary"]), ""]
    for name, e in (g.get("repos") or {}).items():
        L += ["### %s%s" % (name, " (%s)" % e["strategy"] if e.get("strategy") else ""), ""]
        for b in e.get("branches") or []:
            line = "- Branch `%s`" % b.get("branch")
            if b.get("parent"):
                line += " from `%s`" % b["parent"]
            if b.get("status"):
                line += " — " + str(b["status"])
            L.append(line)
        for p in e.get("prs") or []:
            line = "- PR #%s %s: `%s` → `%s` · %s" % (p.get("number"), _one_line(p.get("title")), p.get("source"),
                                                       p.get("target"), p.get("state") or "")
            if p.get("url"):
                line += " · [link](%s)" % p["url"]
            L.append(line)
        L.append("")
    if not (g.get("repos")) and t.get("branches"):
        L += ["- Branch `%s`" % b for b in t["branches"]] + [""]
    alerts = g.get("alerts") or t.get("alerts") or []
    if alerts:
        L += ["**Alerts**", ""] + ["- %s%s" % ("%s: " % a["repo"] if a.get("repo") else "", _one_line(a.get("text")))
                                   for a in alerts]
    return L


def _md_artifacts(t):
    L = []
    for a in t.get("artifacts") or []:
        line = "- %s" % (a.get("name") or a.get("id"))
        if a.get("kind"):
            line += " (%s)" % a["kind"]
        if a.get("purpose"):
            line += " — " + _one_line(a["purpose"])
        if a.get("last") or a.get("date"):
            line += " · " + str(a.get("last") or a.get("date"))
        L.append(line)
    if L:
        L += ["", "_The files stay with the author; ask for them._"]
    return L


def _md_timeline(t):
    rows = t.get("timeline") or []
    if not rows:
        return []
    L = ["| Day | Time | What |", "|---|---|---|"]
    for r in rows:
        L.append("| %s | %s | %s |" % (r.get("day"), common.fmt_minutes(r.get("minutes")) if r.get("minutes") else "",
                                      _one_line(r.get("line")).replace("|", "\\|")))
    return L


def _md_notes(t):
    md = (t.get("readme") or "").strip()
    return [_demote(md)] if md else []


_MD = {"summary": _md_summary, "findings": _md_findings, "requests": _md_requests, "queries": _md_queries,
       "dbflow": _md_dbflow, "branches": _md_branches, "artifacts": _md_artifacts, "timeline": _md_timeline,
       "notes": _md_notes}

_LEVEL_NOTE = {
    "team": "Redacted for the team: no secrets, patient data, QA values or rows, request bodies, local paths or "
            "personal notes.",
    "public": "Redacted for sharing outside the team: also no hosts, links, SQL text or internal names.",
}


def _header(meta):
    blob = json.dumps(meta, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
    return ["<!-- cg-share %s -->" % blob, "# %s%s" % (meta["key"], " — " + meta["title"] if meta.get("title") else ""),
            "", "_Shared with Chitragupta · %s · level **%s** · sections: %s%s_" % (
                meta["at"].replace("T", " "), meta["level"], ", ".join(LABEL[s] for s in meta["sections"]),
                " · by " + meta["by"] if meta.get("by") else ""),
            "", "_%s_" % _LEVEL_NOTE[meta["level"]], ""]


# --------------------------------------------------------------------------- building a bundle

def _redact(part, level, stats, deny):
    if level == "public":
        return export.shareable(part, stats, deny)
    return export.team(part, stats)


def _counts(t, orig):
    return {"findings": sum(1 for f in t.get("findings") or [] if f.get("kind") not in ("todo", "question")),
            "queries": len(t.get("queries") or []), "requests": len((t.get("requests") or {}).get("steps") or []),
            "artifacts": len(t.get("artifacts") or []),
            "dbflow": (orig.get("counts") or {}).get("dbflow", 0) if (t.get("dbflow") or {}).get("markdown") else 0}


def build(key, sections=None, level=None, cfg=None, now=None):
    """Everything a preview or a share needs; writes nothing. Raises ShareError."""
    from .dashboard import api
    cfg = common.load_config() if cfg is None else cfg
    level = level or default_level(cfg)
    if level not in LEVELS:
        raise ShareError("level must be team or public")
    key = (key or "").strip().upper()
    if not KEY_RE.match(key):
        raise ShareError("not a ticket key: %r" % key)
    picked, kinds = pick(sections)
    t = api.ticket(key)
    if not t:
        raise ShareError("nothing recorded for %s" % key)
    t = json.loads(json.dumps(t, default=str))  # a private copy: api results may be cached
    now = now or common.now()
    deny_path = denylist_path(cfg)
    deny = scanlib.load_denylist(deny_path)
    gate = deny if level == "public" else [r for r in deny if not r.internal]
    secrets = scanlib.secret_rules()

    head_stats = {}
    shared = _redact({"key": key, "title": t.get("title") or ""}, level, head_stats, deny)
    parts, rem = {}, {}
    for sid in SECTION_IDS:
        own = OWN[sid]
        if sid not in picked:
            shared.update(json.loads(json.dumps(own)))
            continue
        part = {k: t.get(k, json.loads(json.dumps(v))) for k, v in own.items()}
        if sid == "findings" and kinds:
            part["findings"] = [f for f in part["findings"] or [] if f.get("kind") in kinds]
        stats = dict(head_stats) if sid == "summary" else {}
        part = _redact(part, level, stats, deny)
        shared.update(part)
        parts[sid] = part
        rem[sid] = removed(stats)
    if "summary" not in picked and head_stats:
        rem["title"] = removed(head_stats)
    shared["counts"] = _counts(shared, t)
    meta = {"v": 1, "key": key, "title": shared["title"], "level": level, "sections": picked,
            "kinds": sorted(kinds) if kinds else None, "at": now.isoformat(timespec="minutes"),
            "by": str(_conf(cfg).get("author") or "").strip()[:80], "tool": "Chitragupta " + __version__}
    shared["export"] = {"at": meta["at"], "mode": level, "sections": picked}

    chunks = {"_header": "\n".join(_header(meta))}
    for sid in picked:
        body = _MD[sid](shared)
        chunks[sid] = "\n".join(["## " + LABEL[sid], ""] + (body or ["_Nothing recorded._"])).rstrip() + "\n"
    readme = chunks["_header"] + "\n" + "\n".join(chunks[s] for s in picked)

    hits, problems = [], []
    if level == "public" and not deny:
        problems.append("public sharing needs a deny-list with your internal names (%s)" % deny_path)
    for sid, text in chunks.items():
        for no, kind, rule in scanlib.find(text, gate, secrets):
            hits.append({"section": sid.strip("_"), "file": "README.md", "line": no, "kind": kind, "rule": rule})
    for sid, part in list(parts.items()) + [("header", {"title": shared["title"]})]:
        blob = json.dumps(part, ensure_ascii=False, indent=1, sort_keys=True)
        for no, kind, rule in scanlib.find(blob, gate, secrets):
            hits.append({"section": sid, "file": "ticket.html", "line": no, "kind": kind, "rule": rule})
    return {"key": key, "title": shared["title"], "level": level, "sections": picked, "meta": meta,
            "markdown": readme, "snapshot": shared, "removed": rem,
            "removed_text": {s: removed_text(v) for s, v in rem.items()}, "never": list(NEVER),
            "hits": hits, "problems": problems, "blocked": bool(hits or problems),
            "deny_list": {"path": deny_path, "found": bool(deny), "rules": len(deny)}}


def _public_view(b):
    return {k: v for k, v in b.items() if k not in ("snapshot",)}


def preview(key, sections=None, level=None, cfg=None, now=None):
    """The bundle as it would be shared — {key, title, level, sections, markdown, removed: {section: [{kind,
    count}]}, removed_text, never, blocked, hits: [{section, file, line, kind, rule}], problems, deny_list} —
    without writing anything."""
    return _public_view(build(key, sections, level, cfg, now))


# --------------------------------------------------------------------------- writing

def _stamp(now):
    return now.strftime("%Y%m%d-%H%M")


def _bundle_dir(root, key, level, now):
    base = os.path.join(root, SHARE_DIR, key)
    name = "%s-%s" % (_stamp(now), level)
    d, n = os.path.join(base, name), 2
    while os.path.exists(d):
        d, n = os.path.join(base, "%s-%d" % (name, n)), n + 1
    return d


def _git_runner(t, cfg):
    return gitsync.share_runner(commands.share_repo_dir(t["name"]), cfg)


def ensure_clone(t, cfg=None):
    """The git target's clone at <home>/share/<name> (cloned on first use), on the configured branch.
    Returns (folder, run). Raises ShareError."""
    cfg = common.load_config() if cfg is None else cfg
    d = commands.share_repo_dir(t["name"])
    if not os.path.isdir(os.path.join(d, ".git")):
        paths.makedirs(os.path.dirname(d), caller="share")
        rc, so, se = gitsync.share_clone(t["url"], d, cfg)
        if rc != 0:
            raise ShareError("could not clone the team repo: %s" % (se or so).strip()[:300])
    run = _git_runner(t, cfg)
    rc, cur, _ = run(["remote", "get-url", "origin"])
    if rc != 0 or cur.strip() != t["url"]:
        raise ShareError("the clone at %s does not point at %s" % (d, t["url"]))
    if run(["rev-parse", "--verify", "-q", "HEAD"])[0] != 0:  # an empty team repo: our first commit starts the branch
        run(["symbolic-ref", "HEAD", "refs/heads/" + t["branch"]])
    else:
        head = run(["symbolic-ref", "--short", "HEAD"])[1].strip()
        if head != t["branch"]:
            raise ShareError("the clone is on branch %r, the target says %r (set \"branch\" in the target)"
                             % (head, t["branch"]))
    return d, run


def share(key, sections=None, level=None, target=None, cfg=None, now=None):
    """Write the bundle to the target — only when the scan passes. Returns the preview fields plus {ok, target,
    path (the bundle folder), files, committed, pushed, push_error}. Raises ShareError (and Blocked for a target the
    safety layer refuses)."""
    cfg = common.load_config() if cfg is None else cfg
    t = resolve_target(target, cfg)
    level = level or t.get("default_level") or default_level(cfg)
    now = now or common.now()
    b = build(key, sections, level, cfg, now)
    res = dict(_public_view(b), ok=False, target=t["name"], kind=t["kind"], path=None, files=[], committed=False,
               pushed=False, push_error=None)
    if b["blocked"]:
        return res  # nothing written
    html = export.render(b["snapshot"])
    if t["kind"] == "folder":
        root, run = os.path.abspath(t["path"]), None
    else:
        root, run = ensure_clone(t, cfg)
    d = _bundle_dir(root, b["key"], level, now)
    files = [os.path.join(d, "README.md"), os.path.join(d, "ticket.html")]
    paths.write_text(files[0], b["markdown"], caller="share")
    paths.write_text(files[1], html, caller="share")
    res.update(ok=True, path=d, files=files)
    if run:
        rel = os.path.relpath(d, root).replace("\\", "/")
        gitsync.stage(cfg, [rel + "/README.md", rel + "/ticket.html"], [], run=run)
        msg = "share(%s): %s · %s" % (b["key"], level, ", ".join(b["sections"]))
        rc, so, se = gitsync.commit_paths(run, msg, [rel])
        if rc != 0:
            res.update(ok=False, push_error="commit failed: " + (se or so).strip()[:300])
            return res
        res["committed"] = True
        err = gitsync.integrate_and_push(run, t["branch"], rebase=False, label="share")
        res.update(pushed=err is None, push_error=err)
        if err:
            common.log("agent", "share push failed (%s): %s" % (t["name"], err.strip()[:300]))
    common.log("agent", "shared %s (%s, %s) to %s" % (b["key"], level, ",".join(b["sections"]), t["name"]))
    return res


def refresh(target=None, cfg=None):
    """Bring a git target's clone up to date with the team (fetch + merge, never a rebase, no push).
    None when done (or nothing to do), else the error."""
    cfg = common.load_config() if cfg is None else cfg
    t = resolve_target(target, cfg)
    if t["kind"] != "git":
        return None
    _d, run = ensure_clone(t, cfg)
    return gitsync.integrate(run, t["branch"], rebase=False, label="share")


# --------------------------------------------------------------------------- team notes (read-only)

def _root(t):
    if t["kind"] == "folder":
        return os.path.join(os.path.abspath(t["path"]), SHARE_DIR)
    return os.path.join(commands.share_repo_dir(t["name"]), SHARE_DIR)


def _read_head(p, limit=4096):
    try:
        with open(p, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _listdir(p):
    try:
        return sorted(os.listdir(p))
    except OSError:
        return []


def team_notes(target=None, cfg=None):
    """Every bundle in the target(s) — yours and your teammates' — newest first:
    [{target, key, title, level, sections, at, by, mine, dir, readme, html}]. Reads only (a git target is read as
    its clone is now; refresh() brings it up to date)."""
    cfg = common.load_config() if cfg is None else cfg
    me = str(_conf(cfg).get("author") or "").strip()
    out = []
    for t in targets(cfg):
        if t["problem"] or (target and t.get("name") != target):
            continue
        root = _root(t)
        for key in _listdir(root):
            if not KEY_RE.match(key):
                continue
            for bun in _listdir(os.path.join(root, key)):
                m = BUNDLE_RE.match(bun)
                d = os.path.join(root, key, bun)
                readme = os.path.join(d, "README.md")
                if not m or not os.path.isfile(readme):
                    continue
                meta = {}
                mm = META.match(_read_head(readme))
                if mm:
                    try:
                        meta = json.loads(mm.group(1))
                    except ValueError:
                        meta = {}
                at = meta.get("at") or _dt.datetime.strptime(bun[:13], "%Y%m%d-%H%M").isoformat(timespec="minutes")
                html = os.path.join(d, "ticket.html")
                by = str(meta.get("by") or "")
                out.append({"target": t["name"], "key": key, "title": meta.get("title") or "", "level": m.group(1),
                            "sections": meta.get("sections") or [], "at": at, "by": by, "mine": bool(me) and by == me,
                            "dir": "%s/%s/%s" % (SHARE_DIR, key, bun), "readme": readme,
                            "html": html if os.path.isfile(html) else None})
    out.sort(key=lambda n: (n["at"], n["key"]), reverse=True)
    return out


def read_note(target, rel, cfg=None):
    """{markdown, html} of one bundle (`rel` = a team_notes() "dir"); None when it isn't one. Reads only."""
    cfg = common.load_config() if cfg is None else cfg
    t = next((x for x in targets(cfg) if x.get("name") == target and not x["problem"]), None)
    parts = (rel or "").replace("\\", "/").strip("/").split("/")
    if not t or len(parts) != 3 or parts[0] != SHARE_DIR or not KEY_RE.match(parts[1]) or not BUNDLE_RE.match(parts[2]):
        return None
    d = os.path.join(_root(t), parts[1], parts[2])
    readme = os.path.join(d, "README.md")
    if not os.path.isfile(readme):
        return None
    html = os.path.join(d, "ticket.html")
    return {"markdown": common.read_text(readme), "html": html if os.path.isfile(html) else None}
