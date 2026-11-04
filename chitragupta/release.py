"""Release tracking: which Jira release a ticket is scheduled for, which release branch each affected repo ships it
from, and where the ticket's changes stand on the way to that release branch, QA and (when required) UAT.

Separate facts, never derived from one another:
  release identity   the Jira fix version (project + version id; the name only when no id is known)
  planned date       the version's release date, a configured Jira date field, or your override
  name date text     a date written in the release's name ("December 14th"): shown, used to rank suggestions, never a
                     planned date (and an "FY99" in a name never becomes a calendar year)
  release branch     per repo: your mapping, else an unambiguous structured link, else suggestions to confirm

Per affected repo the stages come from branch tracking's delivery matrix (delivery.py: ancestry, patch-id, squash
and revert evidence), pinned to the resolved release branch, plus a UAT target when UAT is required. Status and
freshness are kept apart: a stale fetch, a failed source or a PR source that can't see open PRs turns "missing" into
"unknown", never the other way round. Reminders are recomputed from evidence on every run; showing one never clears it.

Two phases per ticket, both in the background tick (and /cg:check): resolve() before the delivery check (it pins the
release branch and the UAT target), evaluate() after it. No model calls, no network; git reads go through gitro.
The ticket record gets `release` (additive). Config: "releases" in config.json (docs/releases.md).
"""
import copy
import datetime as _dt
import hashlib
import json
import re
import shlex

from . import common
from . import gitcmd

VERSION = 1
GOOD = ("in", "same", "squash")
STATES = ("present", "partial", "missing", "unknown", "not_started", "not_applicable")
UAT_STATES = ("required", "not_required", "unknown")
MAX_HISTORY = 10
MAX_SHARED_PRS = 40

DEFAULTS = {
    "enabled": False,
    "stale_hours": 24,
    "jira": {
        "scope_jql": "assignee = currentUser() AND fixVersion is not EMPTY AND updated >= -120d",
        "scope_refresh_hours": 12,
        "scope_limit": 50,
        "versions_refresh_hours": 24,
        "planned_date_field": "",
        "uat_field": "",
        "uat_values": {"required": ["yes", "required", "true", "y"],
                       "not_required": ["no", "not required", "not_required", "false", "n", "n/a", "na", "none"]},
        "uat_labels": {"required": [], "not_required": []},
        "uat_deployed_statuses": [],
        "uat_accepted_statuses": [],
        "exclude_statuses": ["cancelled", "canceled", "won't do", "wont do", "rejected", "duplicate"],
    },
    "scope": {"assigned": True, "branches": True, "worked": True},
    "naming": {"dev": "feature/{ticket}", "qa": "{dev}-qa", "uat": "{dev}-uat"},
    "repos": {},
}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|" \
       r"nov(?:ember)?|dec(?:ember)?)"
_MD = re.compile(r"(?i)\b%s\.?[\s_-]+(\d{1,2})(?:st|nd|rd|th)?\b" % _MON)
_DM = re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)?[\s_-]*(?:of[\s_-]+)?%s\b" % _MON)
_YMD = re.compile(r"(?<!\d)(20\d\d)[-_.](\d{1,2})[-_.](\d{1,2})(?!\d)")
_DMY = re.compile(r"(?<!\d)(\d{1,2})[-_.](\d{1,2})[-_.](20\d\d)(?!\d)")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


# --------------------------------------------------------------------------- settings

def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def settings(cfg):
    raw = (cfg or {}).get("releases")
    s = _merge(DEFAULTS, raw if isinstance(raw, dict) else {})
    s["enabled"] = bool(s.get("enabled"))
    try:
        s["stale_hours"] = max(1, int(s.get("stale_hours") or 24))
    except (TypeError, ValueError):
        s["stale_hours"] = 24
    j = s["jira"]
    for k in ("uat_deployed_statuses", "uat_accepted_statuses", "exclude_statuses"):
        j[k] = [str(x).strip().lower() for x in j.get(k) or [] if str(x).strip()]
    for k in ("uat_values", "uat_labels"):
        j[k] = {st: [str(x).strip().lower() for x in (j.get(k) or {}).get(st) or [] if str(x).strip()]
                for st in ("required", "not_required")}
    return s


def enabled(cfg):
    return settings(cfg)["enabled"]


def repo_settings(cfg, name):
    """releases.repos.<repo>: {branches: {release -> branch}, uat: {...} | None, naming: {...}}."""
    s = settings(cfg)
    rc = s["repos"].get(name) if isinstance(s["repos"].get(name), dict) else {}
    naming = dict(s["naming"])
    naming.update(rc.get("naming") or {})
    uat = rc.get("uat") if isinstance(rc.get("uat"), dict) else None
    return {"configured": name in s["repos"], "branches": dict(rc.get("branches") or {}), "uat": uat, "naming": naming}


def release_repos(cfg):
    """Repos that ship through release branches: listed under releases.repos, or with a strategy that starts from
    the release role (branching.repos)."""
    out = set(settings(cfg)["repos"])
    b = gitcmd.settings(cfg)
    for name, rc in (b["repos"] or {}).items():
        st = b["strategies"].get((rc or {}).get("strategy") or "") or {}
        if "release" in gitcmd.from_roles(st):
            out.add(name)
    return sorted(out)


# --------------------------------------------------------------------------- Jira facts

def _date(x):
    """'YYYY-MM-DD' from an ISO-like value, else None (display formats like '14/Dec/26' are never guessed)."""
    m = _ISO.match(str(x or "").strip())
    if not m:
        return None
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def _vals(x):
    """A Jira field value as a list of strings (option objects, user pickers, lists, booleans)."""
    if x is None:
        return []
    if isinstance(x, bool):
        return ["true" if x else "false"]
    if isinstance(x, (int, float)):
        return [str(x)]
    if isinstance(x, str):
        return [x] if x.strip() else []
    if isinstance(x, list):
        return [v for y in x for v in _vals(y)]
    if isinstance(x, dict):
        for k in ("value", "name", "displayName", "key"):
            if x.get(k) not in (None, ""):
                return _vals(x[k])
    return []


def _getter(iss):
    f = iss.get("fields") if isinstance(iss.get("fields"), dict) else {}

    def get(*names):
        for n in names:
            for src in (iss, f):
                if n in src and src[n] not in (None, ""):
                    return src[n]
        return None

    def has(*names):
        return any(n in iss or n in f for n in names)
    return get, has


def name_date(text):
    """{'month', 'day', 'year'?, 'text'} written in a release or branch name, else None. A year is kept only when
    the name spells one out (2026-12-14); "FY99" is a fiscal label, not a year."""
    t = text or ""
    m = _YMD.search(t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return {"month": mo, "day": d, "year": y, "text": m.group(0)}
    m = _DMY.search(t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return {"month": mo, "day": d, "year": y, "text": m.group(0)}
    for rx, order in ((_MD, "md"), (_DM, "dm")):
        m = rx.search(t)
        if m:
            mon, day = (m.group(1), m.group(2)) if order == "md" else (m.group(2), m.group(1))
            mo = next(i + 1 for i, n in enumerate(MONTHS) if n.startswith(mon.lower()[:3]))
            d = int(day)
            if 1 <= d <= 31:
                return {"month": mo, "day": d, "text": m.group(0).strip()}
    return None


def parse_versions(raw, project):
    out = []
    for v in raw if isinstance(raw, list) else []:
        if isinstance(v, str):
            v = {"name": v}
        if not isinstance(v, dict):
            continue
        name = str(v.get("name") or "").strip()
        vid = str(v.get("id") or "").strip()
        if not name and not vid:
            continue
        out.append({"id": vid, "name": name, "project": project,
                    "release_date": _date(v.get("releaseDate") or v.get("release_date")),
                    "released": bool(v.get("released")), "archived": bool(v.get("archived")),
                    "description": common.truncate(str(v.get("description") or ""), 300)})
    return out


def _me(cfg):
    emails = {e.lower() for e in (cfg or {}).get("git_author_emails") or [] if e}
    acct = (((cfg or {}).get("mcp") or {}).get("jira") or {}).get("account_id")
    return emails, acct


def assignee_is_me(a, cfg):
    """True / False when the assignee can be compared with you (git_author_emails, mcp.jira.account_id), else None."""
    if a is None:
        return False
    if not isinstance(a, dict):
        return None
    emails, acct = _me(cfg)
    em = str(a.get("email") or a.get("emailAddress") or "").lower()
    aid = a.get("account_id") or a.get("accountId")
    if (em and em in emails) or (acct and aid == acct):
        return True
    if (em and emails) or (aid and acct):
        return False
    return None


def issue_facts(iss, cfg, key=None, requested=None):
    """Release facts of one Jira issue (a jira_get_issue or jira_search result, REST or simplified shape).
    `requested`: the fields the job asked for; a field asked for but absent is empty, one never asked for is unknown."""
    s = settings(cfg)["jira"]
    get, has = _getter(iss if isinstance(iss, dict) else {})
    key = key or str((iss or {}).get("key") or "")
    project = key.split("-")[0] if "-" in key else ""
    asked = {f.strip().lower() for f in (requested or "").split(",") if f.strip()}
    vkeys = ("fixVersions", "fix_versions", "fixversions")
    if has(*vkeys) or "fixversions" in asked:
        versions = parse_versions(get(*vkeys) or [], project)
    else:
        versions = None  # never asked for (a card from before release tracking): unknown, not "no release"
    planned = None
    if s["planned_date_field"]:
        planned = next((d for d in map(_date, _vals(get(s["planned_date_field"]))) if d), None)
    uat_vals, uat_seen = [], False
    if s["uat_field"]:
        uat_vals = [common.truncate(v, 60) for v in _vals(get(s["uat_field"]))][:5]
        uat_seen = has(s["uat_field"]) or s["uat_field"].lower() in asked
    labels = [str(x) for x in (get("labels") or []) if isinstance(x, str)][:30]
    st = _vals(get("status"))
    a = get("assignee")
    return {"versions": versions, "planned_date": planned, "uat_values": uat_vals, "uat_seen": uat_seen,
            "labels": labels, "status": st[0] if st else "", "summary": common.truncate(" ".join(_vals(get("summary"))), 200),
            "assignee_me": assignee_is_me(a, cfg) if (a is not None or has("assignee") or "assignee" in asked) else None}


def card_fields(cfg):
    """The extra fields the Jira card job asks for when release tracking is on (no extra call)."""
    j = settings(cfg)["jira"]
    out = ["fixVersions", "assignee"]
    out += [f for f in (j["planned_date_field"], j["uat_field"]) if f]
    return out


def scope_fields(cfg):
    j = settings(cfg)["jira"]
    return ",".join(["summary", "status", "assignee", "fixVersions", "labels", "issuetype"] +
                    [f for f in (j["planned_date_field"], j["uat_field"]) if f])


def _unwrap(x):
    from .mcpjobs import unwrap
    return unwrap(x)


def scope_path():
    return common.hp("data", "jira", "_releases", "scope.json")


def versions_path(project):
    return common.hp("data", "jira", "_versions", re.sub(r"[^A-Za-z0-9_-]", "_", project or "_") + ".json")


_FILES = {}


def _read_cached(path):
    """A small JSON file, re-read only when its modification time changes (one tick reads it for every ticket)."""
    import os
    try:
        mt = os.path.getmtime(path)
    except OSError:
        _FILES.pop(path, None)
        return {}
    hit = _FILES.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    data = common.read_json(path, {}) or {}
    _FILES[path] = (mt, data)
    return data


def catalog(project):
    return _read_cached(versions_path(project))


def merge_catalog(old, versions, now_iso):
    """A project's versions catalog after a fresh read: a version whose name changed keeps its id and remembers
    the old name (so a mapping or card that used it still matches)."""
    prev = {v.get("id"): v for v in (old or {}).get("versions") or [] if v.get("id")}
    out = []
    for v in versions:
        o = prev.get(v.get("id")) or {}
        names = list(o.get("previous_names") or [])
        if o.get("name") and v.get("name") and o["name"] != v["name"] and o["name"] not in names:
            names.append(o["name"])
        out.append(dict(v, previous_names=names) if names else dict(v))
    return {"versions": out, "fetched_at": now_iso, "error": None}


def save_scope(issues, jql, cfg, now_iso, error=None):
    """data/jira/_releases/scope.json from one jira_search: only the release facts of each issue are kept."""
    path = scope_path()
    old = common.read_json(path, {}) or {}
    if error:
        old.update(error=str(error)[:300], failed_at=now_iso)
        common.write_json(path, old)
        return old
    req = scope_fields(cfg)
    out = {}
    for iss in issues or []:
        if not isinstance(iss, dict) or not iss.get("key"):
            continue
        f = issue_facts(iss, cfg, iss["key"], req)
        out[iss["key"]] = {k: f[k] for k in ("versions", "planned_date", "uat_values", "uat_seen", "labels", "status",
                                             "summary", "assignee_me")}
    rec = {"fetched_at": now_iso, "jql": jql, "issues": out, "error": None,
           "mine_only": "assignee=currentuser()" in re.sub(r"\s+", "", (jql or "").lower())}
    common.write_json(path, rec)
    return rec


def jira_facts(key, cfg):
    """The newest release facts for a ticket: its Jira card (data/jira/<KEY>.json) or the scope search, whichever was
    read last. -> facts + {source, fetched_at, error} (source None: nothing read yet)."""
    out = {"source": None, "fetched_at": None, "error": None, "versions": None, "planned_date": None, "uat_values": [],
           "uat_seen": False, "labels": [], "status": "", "summary": "", "assignee_me": None}
    rec = common.read_json(common.hp("data", "jira", key + ".json"), {}) or {}
    card_at = rec.get("fetched_at") if rec.get("raw") else None
    sc = _read_cached(scope_path())
    sc_iss = (sc.get("issues") or {}).get(key)
    sc_at = sc.get("fetched_at") if sc_iss else None
    if card_at and (not sc_at or card_at >= sc_at):
        iss = _unwrap(rec.get("raw"))
        if isinstance(iss, dict):
            out.update(issue_facts(iss, cfg, key, rec.get("fields")))
            out.update(source="card", fetched_at=card_at)
    elif sc_iss:
        out.update({k: sc_iss.get(k) for k in ("versions", "planned_date", "uat_values", "uat_seen", "labels", "status",
                                               "summary", "assignee_me") if k in sc_iss})
        out.update(source="scope", fetched_at=sc_at)
    out["error"] = rec.get("error") if out["source"] == "card" else (sc.get("error") if out["source"] == "scope" else
                                                                     rec.get("error") or None)
    return out


def enrich(versions, cache=None):
    """Fill each version from the project's versions catalog (data/jira/_versions/<P>.json): id, release date,
    released / archived, and the names it had before (a rename keeps its id)."""
    cache = {} if cache is None else cache
    out = []
    for v in versions or []:
        v = dict(v)
        cat = cache.setdefault(v.get("project") or "", catalog(v.get("project") or ""))
        hit = None
        for c in cat.get("versions") or []:
            if v.get("id") and c.get("id") == v["id"]:
                hit = c
                break
        if not hit and v.get("name"):
            low = v["name"].lower()
            for c in cat.get("versions") or []:
                if (c.get("name") or "").lower() == low or low in [n.lower() for n in c.get("previous_names") or []]:
                    hit = c
                    break
        if hit:
            if hit.get("name") and v.get("name") and hit["name"] != v["name"]:
                v.setdefault("previous_names", []).append(v["name"])
            v.update(id=v.get("id") or hit.get("id") or "", name=hit.get("name") or v.get("name"),
                     release_date=v.get("release_date") or hit.get("release_date"),
                     released=bool(v.get("released") or hit.get("released")),
                     archived=bool(v.get("archived") or hit.get("archived")),
                     description=v.get("description") or hit.get("description") or "")
            for n in hit.get("previous_names") or []:
                if n not in v.setdefault("previous_names", []):
                    v["previous_names"].append(n)
            v["catalog_at"] = cat.get("fetched_at")
        out.append(v)
    return out


def rel_key(v):
    if v.get("id"):
        return "%s:%s" % (v.get("project") or "", v["id"])
    return "%s:name:%s" % (v.get("project") or "", (v.get("name") or "").lower())


def _names(v):
    return [n for n in [v.get("id"), v.get("name"), rel_key(v)] + list(v.get("previous_names") or []) if n]


def _matches(v, ident):
    low = str(ident or "").strip().lower()
    return bool(low) and any(low == str(n).lower() for n in _names(v))


# --------------------------------------------------------------------------- inbox directives

DIRECTIVE = re.compile(r"^(release-map|release-unmap|release-pick|release-branch|release-repos|uat)\s*:\s*(.+)$", re.I)
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,9}-\d{1,6}$")


def parse_directive(text):
    """inbox.md line -> dict or None.
      release-map: <repo> "<release>" <branch>      release-unmap: <repo> "<release>"
      release-pick: <TICKET> "<release>"|-          release-branch: <TICKET> <repo> <branch>|-
      release-repos: <TICKET> <repo>...|-           uat: <TICKET> required|not_required|unknown|-"""
    m = DIRECTIVE.match((text or "").strip().lstrip("-* ").strip())
    if not m:
        return None
    kind = m.group(1).lower()
    try:
        args = shlex.split(m.group(2))
    except ValueError:
        return {"kind": "invalid", "text": text}
    bad = {"kind": "invalid", "text": text}
    if kind == "release-map" and len(args) == 3:
        return {"kind": kind, "repo": args[0], "release": args[1], "branch": args[2]}
    if kind == "release-unmap" and len(args) == 2:
        return {"kind": kind, "repo": args[0], "release": args[1]}
    if not args or not _KEY.match(args[0]):
        return bad
    t = args[0].upper()
    if kind == "release-pick" and len(args) == 2:
        return {"kind": kind, "ticket": t, "release": None if args[1] == "-" else args[1]}
    if kind == "release-branch" and len(args) == 3:
        return {"kind": kind, "ticket": t, "repo": args[1], "branch": None if args[2] == "-" else args[2]}
    if kind == "release-repos" and len(args) >= 2:
        return {"kind": kind, "ticket": t, "repos": [] if args[1:] == ["-"] else args[1:]}
    if kind == "uat" and len(args) == 2 and args[1].lower().replace("-", "_") in UAT_STATES + ("_",):
        v = args[1].lower().replace("-", "_")
        return {"kind": kind, "ticket": t, "state": None if v in ("_", "unknown") else v}
    return bad


def directive_line(kind, args):
    """The inbox line for `cli branch <kind> …` (arguments with spaces are quoted)."""
    return "%s: %s" % (kind, " ".join('"%s"' % a.replace("\\", "\\\\").replace('"', '\\"')
                                      if re.search(r"[\s'\"]", a) else a for a in args))


def apply_directive(d, store, cfg, now_iso=None):
    """Record a manual release decision. -> (ok, message, config_changed). Mappings go to the config (the caller
    saves it through the config write path); per-ticket choices to the ticket's `release_override`."""
    now_iso = now_iso or common.now_iso()
    if d["kind"] == "invalid":
        return False, "not understood: " + d["text"], False
    if d["kind"] in ("release-map", "release-unmap"):
        repo = _match_repo(cfg, d["repo"])
        rc = cfg.setdefault("releases", {}).setdefault("repos", {}).setdefault(repo, {})
        br = rc.setdefault("branches", {})
        if d["kind"] == "release-map":
            br[d["release"]] = d["branch"]
            return True, "%s: %s ships from %s" % (repo, d["release"], d["branch"]), True
        hit = [k for k in br if k.lower() == d["release"].lower()]
        for k in hit:
            del br[k]
        return bool(hit), "%s: mapping for %s %s" % (repo, d["release"], "removed" if hit else "not found"), bool(hit)
    tk = store.ticket(d["ticket"])
    ov = tk.setdefault("release_override", {})
    if d["kind"] == "release-pick":
        _set(ov, "pick", d["release"])
    elif d["kind"] == "release-branch":
        b = ov.setdefault("branches", {})
        if d["branch"]:
            b[_match_repo(cfg, d["repo"])] = d["branch"]
        else:
            b.pop(_match_repo(cfg, d["repo"]), None)
    elif d["kind"] == "release-repos":
        _set(ov, "repos", [_match_repo(cfg, r) for r in d["repos"]] or None)
    elif d["kind"] == "uat":
        _set(ov, "uat", d["state"])
    ov["at"] = now_iso
    tk.setdefault("release", {})["sig"] = None
    store.touch_ticket(d["ticket"])
    return True, "%s %s" % (d["kind"], d["ticket"]), False


def _set(d, k, v):
    if v in (None, "", []):
        d.pop(k, None)
    else:
        d[k] = v


def _match_repo(cfg, name):
    for p in (cfg or {}).get("watched_repos") or []:
        if gitcmd.repo_key(p).lower() == str(name).lower():
            return gitcmd.repo_key(p)
    for n in list(settings(cfg)["repos"]) + list(gitcmd.settings(cfg)["repos"]):
        if n.lower() == str(name).lower():
            return n
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(name))


# --------------------------------------------------------------------------- phase 1: release and branch

def select(rels, override):
    """(release or None, {how, candidates?, missing?}). Several fix versions are never narrowed down silently."""
    pick = (override or {}).get("pick")
    if pick:
        hit = [v for v in rels if _matches(v, pick)]
        if len(hit) == 1:
            return hit[0], {"how": "chosen by you"}
        return None, {"how": "pick not found", "candidates": [v.get("name") for v in rels],
                      "missing": "your choice %r isn't one of the ticket's fix versions" % pick}
    if len(rels) == 1:
        return rels[0], {"how": "the ticket's fix version"}
    if len(rels) > 1:
        return None, {"how": "ambiguous", "candidates": [v.get("name") for v in rels],
                      "missing": "the ticket has %d fix versions (%s): choose the one that counts" % (
                          len(rels), ", ".join(v.get("name") or v.get("id") for v in rels))}
    return None, {"how": "none", "missing": "no fix version in Jira"}


class RepoView:
    """What resolution needs from one repo: its branch names (remote-tracking first) and fetch time. Built from a
    gittrack.Repo during a refresh, or from the stored remote heads when none is at hand (no git)."""

    def __init__(self, name, cfg, repo=None, meta=None):
        self.name = name
        self.conf = gitcmd.repo_conf(cfg, name)
        self.remote = self.conf["remote"]
        self.repo = repo
        self.meta = meta if meta is not None else (repo.data.meta if repo is not None else {})
        self.remote_names, self.local_names, self.dates = set(), set(), {}
        if repo is not None:
            for short, ref in repo.refs.items():
                if ref["kind"] == "remote":
                    n = repo.remote_name(short)
                    if n:
                        self.remote_names.add(n)
                        self.dates[n] = ref.get("date") or ""
                else:
                    self.local_names.add(short)
                    self.dates.setdefault(short, ref.get("date") or "")
        else:
            self.remote_names = set(self.meta.get("_heads") or [])
        m = dict(self.meta or {})
        if repo is not None and not m.get("path"):
            m["path"] = repo.path
        self.fetched_at = _last_fetch(m)

    @property
    def available(self):
        return self.repo is not None or bool(self.remote_names)

    def exists(self, branch):
        """'remote' | 'local' | None (not found) | 'unknown' (no refs to look at)."""
        if not self.available:
            return "unknown"
        if branch in self.remote_names:
            return "remote"
        if branch in self.local_names:
            return "local"
        return None

    def role(self, branch):
        return gitcmd.role_of(branch, self.conf["bases"])

    def release_branches(self):
        return sorted(b for b in self.remote_names | self.local_names if self.role(b) == "release")


def _last_fetch(meta):
    """The newest of Chitragupta's own fetch and any other fetch of the repo (.git/FETCH_HEAD's time): a stat, no git."""
    import os
    best = meta.get("fetched_at")
    path = meta.get("path")
    if path:
        try:
            from . import gitro
            fh = os.path.join(gitro.git_dir(path), "FETCH_HEAD")
            ts = _dt.datetime.fromtimestamp(os.path.getmtime(fh)).astimezone().isoformat(timespec="seconds")
            if not best or (common.parse_ts(ts) and common.parse_ts(ts) > common.parse_ts(best)):
                best = ts
        except (OSError, ValueError, TypeError):
            pass
    return best


def _freshness(view, cfg, now):
    meta = dict(view.meta or {})
    if view.repo is not None and not meta.get("path"):
        meta["path"] = view.repo.path
    stale_h = settings(cfg)["stale_hours"]
    meta["fetched_at"] = _last_fetch(meta)
    fetched = common.parse_ts(meta.get("fetched_at"))
    errors = []
    ferr = str(meta.get("fetch_error") or "")
    ferr_at = common.parse_ts(ferr.split(" ", 1)[0]) if ferr else None
    failed = bool(ferr) and (not fetched or not ferr_at or ferr_at >= fetched)  # a later fetch elsewhere supersedes it
    if failed:
        errors.append("last fetch failed: " + ferr[:160])
    if meta.get("pr_error"):
        errors.append("PR source failed: " + str(meta["pr_error"])[:160])
    old = bool(fetched) and (now - fetched).total_seconds() > stale_h * 3600
    stale = failed or not fetched or old
    src = meta.get("pr_source")
    why = "never fetched" if not fetched else "last fetch failed" if failed else \
        "last fetched %s" % meta["fetched_at"][:16].replace("T", " ") if old else ""
    return {"fetched_at": meta.get("fetched_at"), "stale": stale, "errors": errors, "pr_source": src,
            "pr_refreshed_at": meta.get("pr_refreshed_at"), "open_prs_visible": bool(src) and src != "merge-commit",
            "why_stale": why}


_MONTH_ONLY = re.compile(r"(?i)(?<![a-z])%s(?![a-z])" % _MON)


def _date_score(branch, rel):
    """How well a branch name's date matches the release: planned date month+day 3, month 1; name date +1."""
    nd = name_date(branch)
    if not nd:
        m = _MONTH_ONLY.search(branch.rsplit("/", 1)[-1] if "/" in branch else branch)
        if not m:
            return 0, ""
        nd = {"month": next(i + 1 for i, n in enumerate(MONTHS) if n.startswith(m.group(1).lower()[:3])), "day": None}
    score, why = 0, []
    pd = rel.get("planned_date")
    if pd:
        d = _dt.date.fromisoformat(pd)
        if nd["month"] == d.month and nd["day"] == d.day:
            score, why = 3, ["same date as the planned %s" % pd]
        elif nd["month"] == d.month:
            score, why = 1, ["same month as the planned %s" % pd]
    rn = name_date(rel.get("name"))
    if rn and rn["month"] == nd["month"]:
        score += 1
        why.append("month of %r" % rn["text"])
    return score, "; ".join(why)


def _structured(rel, view):
    """Branches a structured link names: a branch written in the version's description, or the one release branch
    carrying a ticket key the version's name or description mentions (a release coordination ticket)."""
    text = "%s %s" % (rel.get("name") or "", rel.get("description") or "")
    out = []
    branches = view.release_branches()
    for b in branches:
        if b and re.search(r"(?<![\w/.-])%s(?![\w/-])" % re.escape(b), rel.get("description") or ""):
            out.append((b, "named in the Jira version's description"))
    if not out:
        for k in re.findall(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b", text):
            hit = [b for b in branches if re.search(r"(?<![A-Za-z0-9])%s(?!\d)" % re.escape(k), b)]
            if len(hit) == 1:
                out.append((hit[0], "the only release branch carrying %s (named by the Jira version)" % k))
    seen, uniq = set(), []
    for b, why in out:
        if b not in seen:
            seen.add(b)
            uniq.append((b, why))
    return uniq


def resolve_branch(rel, view, cfg, override, observed=None, siblings=None):
    """Where `rel` ships from in this repo. Precedence: your per-ticket choice, your mapping
    (releases.repos.<repo>.branches), an unambiguous structured link, then suggestions you confirm.
    -> {name, state, source, confidence, exists, verified_at, override, candidates, missing}
    state: confirmed | mapped-missing | mapped-unverified | linked | suggested | unresolved."""
    out = {"name": None, "state": "unresolved", "source": "", "confidence": "none", "exists": None,
           "verified_at": view.fetched_at, "override": False, "candidates": [], "missing": ""}
    rname = (rel or {}).get("name") or (rel or {}).get("id") or ""
    if not rel:
        out["missing"] = "no release selected for the ticket"
        return out
    b = ((override or {}).get("branches") or {}).get(view.name)
    if b:
        out.update(source="set for this ticket (release-branch)", override=True)
    else:
        mapping = repo_settings(cfg, view.name)["branches"]
        for k, v in mapping.items():
            if _matches(rel, k):
                b = v
                out["source"] = "your mapping releases.repos.%s.branches[%s]" % (view.name, json.dumps(k))
                if str(k).lower() in [n.lower() for n in rel.get("previous_names") or []]:
                    out["source"] += " (the release was renamed since)"
                break
    if b:
        ex = view.exists(b)
        out.update(name=b, exists=ex)
        if ex == "remote":
            out.update(state="confirmed", confidence="high")
        elif ex == "local":
            out.update(state="confirmed", confidence="medium",
                       missing="%s exists only as a local branch here; it isn't among the fetched remote branches" % b)
        elif ex == "unknown":
            out.update(state="mapped-unverified", confidence="medium",
                       missing="%s isn't checked out or fetched on this machine, so it can't be verified" % view.name)
        else:
            out.update(state="mapped-missing", confidence="low",
                       missing="%s is mapped but no such branch exists in %s (as of %s)" % (
                           b, view.name, (out["verified_at"] or "the local refs")[:16].replace("T", " ")))
        return out
    links = [(x, why) for x, why in _structured(rel, view) if view.exists(x) in ("remote", "local")]
    if len(links) == 1:
        out.update(name=links[0][0], state="linked", source=links[0][1], confidence="high", exists=view.exists(links[0][0]))
        return out
    cands = {}
    for x, why in links:
        cands.setdefault(x, [0, []])
        cands[x][0] += 3
        cands[x][1].append(why)
    for x, why in (observed or []):
        if view.exists(x) in ("remote", "local"):
            cands.setdefault(x, [0, []])
            cands[x][0] += 2
            cands[x][1].append(why)
    for x, why in (siblings or []):
        if view.exists(x) in ("remote", "local"):
            cands.setdefault(x, [0, []])
            cands[x][0] += 3
            cands[x][1].append(why)
    for x in view.release_branches():
        sc, why = _date_score(x, rel)
        if sc:
            cands.setdefault(x, [0, []])
            cands[x][0] += sc
            cands[x][1].append("branch name date: " + why)
    ranked = sorted(cands.items(), key=lambda kv: (-kv[1][0], kv[0] not in view.remote_names, -_neg_date(view.dates.get(kv[0]))))
    out["candidates"] = [{"name": x, "why": "; ".join(dict.fromkeys(w)), "score": s} for x, (s, w) in ranked[:5]]
    if out["candidates"]:
        out.update(state="suggested", confidence="low",
                   missing="confirm which branch ships %r in %s" % (rname, view.name))
    else:
        out["missing"] = "no release branch in %s matches %r; map it" % (view.name, rname) if view.available else \
            "%s has no local refs to look at" % view.name
    return out


def _neg_date(s):
    try:
        return int(re.sub(r"\D", "", (s or "")[:19]) or 0)
    except ValueError:
        return 0


def pinned(b):
    """The resolved branch the delivery check may treat as the release that counts (verified by current refs)."""
    return b.get("name") if b.get("state") in ("confirmed", "linked") else None


def affected_repos(tk, cfg, override, key=None, views=None):
    """({repo: how}, [candidate repos]). Evidence: branches linked to the ticket in a repo (branch tracking's records,
    so a repo counts from the first run), its delivery record, the repos it was worked in; your release-repos choice
    replaces it. Repos without evidence are only candidates: no ticket is assumed to touch every repo."""
    if (override or {}).get("repos"):
        return {r: "set by you" for r in override["repos"]}, []
    out = {}
    for name, v in sorted((views or {}).items()):
        if key and v.repo is not None and any(b.get("ticket") == key for b in v.repo.data.branches.values()):
            out[name] = "has branches"
    for name in sorted(((tk.get("git") or {}).get("repos") or {})):
        out.setdefault(name, "has branches or PRs")
    for p in tk.get("repos") or []:
        out.setdefault(gitcmd.repo_key(p), "has branches or PRs")
    cands = [r for r in release_repos(cfg) if r not in out]
    return out, cands


def _uat_conf(cfg, name, strat):
    """The UAT destination of a repo: releases.repos.<repo>.uat, else a strategy step into the uat role.
    -> {branch, promotion: {kind, from?, brings?, sync_from?}, source} or None (not configured)."""
    rc = repo_settings(cfg, name)["uat"]
    if rc and (rc.get("branch") or rc.get("promotion")):
        p = rc.get("promotion")
        p = dict(p) if isinstance(p, dict) else {"kind": p} if isinstance(p, str) else {}
        p.setdefault("kind", "unspecified")
        for k in ("from", "brings", "sync_from"):
            if rc.get(k) and k not in p:
                p[k] = rc[k]
        return {"branch": rc.get("branch"), "promotion": p, "source": "releases.repos.%s.uat" % name}
    for st in (strat or {}).get("steps") or []:
        if st.get("pr_into") == "uat":
            conf = gitcmd.repo_conf(cfg, name)
            b = conf["bases"].get("uat")
            return {"branch": b if isinstance(b, str) else None, "source": "strategy step into uat",
                    "promotion": {"kind": "pr", "from": st.get("from"), "brings": st.get("brings") or "cherry-pick"}}
    return None


def uat_requirement(facts, override, cfg):
    """{state: required | not_required | unknown, source}. An absent field is unknown, never "not required"."""
    if (override or {}).get("uat") in UAT_STATES:
        return {"state": override["uat"], "source": "set by you"}
    j = settings(cfg)["jira"]
    if not facts.get("source"):
        return {"state": "unknown", "source": "no Jira data read for this ticket yet"}
    labels = {x.lower() for x in facts.get("labels") or []}
    for st in ("required", "not_required"):
        hit = sorted(labels & set(j["uat_labels"][st]))
        if hit:
            return {"state": st, "source": "Jira label %s" % hit[0]}
    if not j["uat_field"]:
        return {"state": "unknown", "source": "no UAT field configured (releases.jira.uat_field)"}
    vals = [v.strip().lower() for v in facts.get("uat_values") or []]
    if not vals:
        return {"state": "unknown", "source": "Jira %s is empty%s" % (
            j["uat_field"], "" if facts.get("uat_seen") else " or wasn't read yet")}
    for st in ("required", "not_required"):
        if any(v in j["uat_values"][st] for v in vals):
            return {"state": st, "source": "Jira %s = %s" % (j["uat_field"], facts["uat_values"][0])}
    return {"state": "unknown", "source": "Jira %s = %r isn't listed in releases.jira.uat_values" % (
        j["uat_field"], facts["uat_values"][0])}


def _status_evidence(facts, cfg, kind):
    j = settings(cfg)["jira"]
    lst = j["uat_%s_statuses" % kind]
    st = (facts.get("status") or "").strip()
    if not lst:
        return {"state": "unknown", "source": "no %s evidence source configured (releases.jira.uat_%s_statuses)" % (
            kind, kind)}
    if st and st.lower() in lst:
        return {"state": "yes", "source": "Jira status %r" % st, "at": facts.get("fetched_at")}
    return {"state": "unknown", "source": "Jira status %r doesn't say %s" % (st or "?", kind)}


def sibling_index(store, views):
    """{(release key, repo): {release branch: {tickets}}}: the release branches tickets of each release targeted
    with PRs, built once per refresh."""
    idx = {}
    for t, tk in list((store.tickets if store is not None else {}).items()):
        rk = (tk.get("release") or {}).get("selected")
        if not rk:
            continue
        for name, x in (((tk.get("git") or {}).get("repos")) or {}).items():
            v = views.get(name)
            for p in x.get("prs") or []:
                tgt = p.get("target") or ""
                role = v.role(tgt) if v is not None else gitcmd.role_of(tgt, gitcmd.repo_conf({}, name)["bases"])
                if p.get("state") != "closed" and role == "release":
                    idx.setdefault((rk, name), {}).setdefault(tgt, set()).add(t)
    return idx


def _siblings(idx, rel, key, view):
    """Release branches other tickets of the same release targeted with PRs (a team signal, never proof)."""
    if not rel:
        return []
    out = []
    for b, ts in sorted(((idx or {}).get((rel_key(rel), view.name)) or {}).items()):
        others = sorted(set(ts) - {key})
        if others:
            out.append((b, "PR target of %s (same release)" % ", ".join(others[:3])))
    return out


def _observed(tk, name, view):
    out = []
    x = ((tk.get("git") or {}).get("repos") or {}).get(name) or {}
    for p in x.get("prs") or []:
        if p.get("state") != "closed" and view.role(p.get("target") or "") == "release":
            out.append((p["target"], "this ticket's PR #%s targets it" % p.get("number")))
    for b in x.get("branches") or []:
        if b.get("parent") and view.role(b["parent"]) == "release" and b.get("parent_source") != "inferred":
            out.append((b["parent"], "%s was created from it" % b["branch"]))
    return out


def resolve(store, key, views, cfg, now=None, sib=None):
    """Phase 1 (before the delivery check): the ticket's releases, the one that counts, the affected repos and each
    repo's release branch. Writes tk['release'] (keeps the evaluation of the last run until phase 2 replaces it)
    and tk['release']['pins'] = {repo: {release, uat}} for delivery.compute. -> True when the pins changed."""
    now = now or common.now()
    tk = store.ticket(key)
    old = tk.get("release") if isinstance(tk.get("release"), dict) else {}
    if not enabled(cfg) or gitcmd.ignored(cfg, key):
        if old:
            tk.pop("release", None)
            store.touch_ticket(key)
            return True
        return False
    ov = tk.get("release_override") or {}
    facts = jira_facts(key, cfg)
    vers = facts.get("versions")
    rels = enrich(vers) if vers is not None else None
    if rels is not None:
        for v in rels:
            v["key"] = rel_key(v)
            v["name_date"] = name_date(v.get("name"))
            if v.get("release_date"):
                v["planned_date"], v["date_source"] = v["release_date"], "Jira version release date"
            elif facts.get("planned_date"):
                v["planned_date"], v["date_source"] = facts["planned_date"], "Jira field %s" % settings(cfg)["jira"]["planned_date_field"]
            else:
                v["planned_date"], v["date_source"] = None, ""
            od = (ov.get("dates") or {}).get(v["key"])
            if od and _date(od):
                v["planned_date"], v["date_source"] = _date(od), "set by you"
    else:
        rels = old.get("releases") if old.get("releases") is not None else None  # keep what we knew; marked unknown
    sel, how = select(rels or [], ov) if rels is not None else (None, {"how": "unknown", "missing": "the ticket's fix "
                                                                     "versions haven't been read from Jira yet"})
    hist = list(old.get("history") or [])
    prev_keys = old.get("release_keys")
    cur_keys = sorted(v["key"] for v in rels or []) if vers is not None else prev_keys
    if vers is not None and prev_keys is not None and sorted(prev_keys) != cur_keys and old.get("releases"):
        hist.append({"at": now.isoformat(timespec="seconds"),
                     "releases": [{k: v.get(k) for k in ("key", "name", "planned_date")} for v in old["releases"]],
                     "branches": {n: (r.get("branch") or {}).get("name") for n, r in (old.get("repos") or {}).items()
                                  if (r.get("branch") or {}).get("name") and not r.get("candidate")},
                     "to": [v.get("name") for v in rels or []]})
        hist = hist[-MAX_HISTORY:]
    affected, cands = affected_repos(tk, cfg, ov, key, views)
    repos = {}
    pins = {}
    b = gitcmd.settings(cfg)
    for name in list(affected) + cands:
        view = views.get(name) or RepoView(name, cfg, meta=_meta(name))
        br = resolve_branch(sel, view, cfg, ov, _observed(tk, name, view), _siblings(sib, sel, key, view))
        oldr = (old.get("repos") or {}).get(name) or {}
        strat_name = _strategy_name(cfg, key, tk, name)
        strat = b["strategies"].get(strat_name) if strat_name else None
        uat = _uat_conf(cfg, name, strat)
        repos[name] = dict(oldr, affected=affected.get(name) and {"how": affected[name]}, candidate=name not in affected,
                           branch=br, strategy=strat_name, uat_conf=uat, freshness=_freshness(view, cfg, now))
        if name in affected:
            pins[name] = {"release": pinned(br), "uat": None}
    req = uat_requirement(facts, ov, cfg)
    for name, p in pins.items():
        u = repos[name]["uat_conf"]
        if req["state"] == "required" and u and u.get("branch"):
            p["uat"] = u["branch"]
    new = dict(old, v=VERSION, jira={k: facts.get(k) for k in ("source", "fetched_at", "error", "status")},
               releases=rels, release_keys=cur_keys, selected=sel["key"] if sel else None, selection=how,
               history=hist, uat=dict(old.get("uat") or {}, requirement=req), repos=repos, pins=pins,
               assignee_me=facts.get("assignee_me"), summary=facts.get("summary") or old.get("summary") or "")
    changed_pins = old.get("pins") != pins
    if _strip(old) != _strip(new):
        tk["release"] = new
        store.touch_ticket(key)
    return changed_pins


def _strip(r):
    return json.dumps({k: v for k, v in (r or {}).items() if k not in ("computed_at", "sig")}, sort_keys=True, default=str)


def _meta(name):
    return common.read_json(common.hp("data", "git", name, "repo.json"), {}) or {}


def _strategy_name(cfg, key, tk, name):
    try:
        from . import delivery
        conf = gitcmd.repo_conf(cfg, name)
        return gitcmd.strategy_for(cfg, conf, _meta(name).get("manual"), key, tk, delivery.jira_parent(tk, key))[0]
    except Exception:  # noqa: BLE001 - no strategy is a valid answer
        return None


# --------------------------------------------------------------------------- phase 2: stages, flags, next steps

def _cells(x, role):
    t = next((t for t in x.get("targets") or [] if t.get("role") == role), None)
    if not t:
        return None, []
    return t, [((row.get("cells") or {}).get(role) or {}) for row in x.get("rows") or []]


def stage(x, role, fresh, pinned_name=None, open_visible=True):
    """One destination's inclusion from the delivery matrix. -> {state, target, detail, counts, pending_prs, why}.
    present: every commit of the ticket is there (ancestry, same patch or squash PR); partial: some; missing: none
    (and the refs are fresh); unknown: no target, stale refs, or a release target that isn't the resolved branch."""
    t, cells = _cells(x, role)
    if t is None:
        return {"state": "unknown", "target": None, "detail": "no %s target in this repo's strategy" % role}
    n = len(cells)
    if not n:
        return {"state": "not_started", "target": t.get("name"), "detail": "no commits yet"}
    good = sum(1 for c in cells if c.get("s") in GOOD)
    opened = sorted({c.get("pr") for c in cells if c.get("s") == "open" and c.get("pr")}, key=str)
    checks = sum(1 for c in cells if c.get("s") == "check")
    reverted = sum(1 for c in cells if c.get("s") == "reverted")
    whys = sorted({c.get("why") for c in cells if c.get("s") == "missing" and c.get("why")})
    out = {"target": t.get("name"), "counts": {"in": good, "of": n, "check": checks, "reverted": reverted,
                                               "open": sum(1 for c in cells if c.get("s") == "open")},
           "pending_prs": opened, "why": whys[:3]}
    if good == n:
        out.update(state="present", detail="all %d commit%s in %s" % (n, "" if n == 1 else "s", t.get("name")))
    elif good == 0 and not checks:
        out.update(state="missing", detail="none of %d commit%s in %s" % (n, "" if n == 1 else "s", t.get("name")))
    else:
        out.update(state="partial", detail="%d of %d commits in %s" % (good, n, t.get("name")))
    if checks:
        out["detail"] += "; %d reached it only with a changed patch (check by hand)" % checks
    if reverted:
        out["detail"] += "; %d reverted there" % reverted
    if opened:
        out["detail"] += "; PR %s open" % ", ".join("#%s" % p for p in opened)
    if role == "release" and pinned_name and t.get("name") != pinned_name:
        out.update(state="unknown", detail="checked against %s, not the release branch %s" % (t.get("name"), pinned_name))
    elif role == "release" and not pinned_name:
        out.update(state="unknown" if out["state"] != "not_started" else out["state"],
                   detail=out["detail"] + " (%s; the Jira release's branch isn't confirmed)" % (t.get("how") or "target"))
    if out["state"] == "missing" and fresh["stale"]:
        out.update(state="unknown", detail=out["detail"] + " as of the local refs (%s), so not confirmed missing" % fresh["why_stale"])
    elif out["state"] == "partial" and fresh["stale"]:
        out["detail"] += " (refs %s)" % fresh["why_stale"]
    return out


def _prs_by_role(x, view):
    out = {}
    for p in x.get("prs") or []:
        role = view.role(p.get("target") or "") or "other"
        out.setdefault(role, []).append(dict(p, role=role))
    for role, lst in out.items():
        lst.sort(key=lambda p: p.get("created_at") or p.get("merged_at") or "")
        for i, p in enumerate(lst):
            later = [q for q in lst[i + 1:] if q.get("target") == p.get("target") and q.get("state") != "closed"]
            if p.get("state") == "closed":
                p["note"] = "closed unmerged" + ("; superseded by #%s" % later[0]["number"] if later else "")
            elif p.get("state") == "merged" and any(q.get("state") == "merged" for q in later):
                p["note"] = "followed by #%s" % next(q["number"] for q in later if q.get("state") == "merged")
    return out


def shared_prs(x, repo, roles, cache):
    """PRs into a qa / uat target from branches that aren't this ticket's, whose head carries the ticket's commits
    (same sha, or same patch-id with the same subject). A shared integration PR is linked to every ticket it
    carries, never credited with ownership. `cache` {pr@head@rows: [row ids]} avoids re-reading heads."""
    rows = x.get("rows") or []
    used = cache.setdefault("_used", [])
    del used[:]
    if not rows or repo is None:
        return []
    own = {b.get("branch") for b in x.get("branches") or []}
    since = min(r.get("date") or "" for r in rows)[:10]
    rsig = hashlib.sha1("|".join(sorted(r.get("id") or "" for r in rows)).encode()).hexdigest()[:8]
    out = []
    cands = [p for p in repo.data.prs.values() if p.get("source") not in own and repo.role(p.get("target") or "") in roles
             and (p.get("merged_at") or p.get("created_at") or "9999")[:10] >= since]
    cands.sort(key=lambda p: p.get("merged_at") or p.get("created_at") or "", reverse=True)
    for p in cands[:MAX_SHARED_PRS]:
        rec = repo.data.branches.get(p.get("source") or "") or {}
        head = p.get("head_at_merge") or p.get("head") or rec.get("remote_tip") or rec.get("tip")
        if not head:
            continue
        ck = "%s@%s@%s" % (p.get("number"), head[:12], rsig)
        if ck not in cache:
            cache[ck] = _carried(repo, head, rows, since)
        used.append(ck)
        carried = cache[ck]
        if carried:
            out.append({"number": p.get("number"), "title": p.get("title"), "source": p.get("source"),
                        "target": p.get("target"), "state": p.get("state"), "created_at": p.get("created_at"),
                        "merged_at": p.get("merged_at"), "url": p.get("url"), "role": repo.role(p.get("target") or ""),
                        "shared": True, "carries": len(carried), "of": len(rows), "src": p.get("src")})
    return out


def _carried(repo, head, rows, since):
    d = common.parse_ts(since + "T00:00:00")
    lo = (d - _dt.timedelta(days=3)).date().isoformat() if d else since
    log = repo.commits(["--since=" + lo, head], merges=False, limit=1000)
    have = {c["sha"][:9] for c in log}
    got = [r["id"] for r in rows if any((s or "")[:9] in have for s in r.get("shas") or [])]
    rest = [r for r in rows if r["id"] not in got]
    if rest:
        subj = {r.get("subject") for r in rest}
        near = [c for c in log if c["subject"] in subj]
        pids = repo.patch_ids([c["sha"] for c in near]) if near else {}
        for r in rest:
            if any(c["subject"] == r.get("subject") and (pids.get(c["sha"]) or "")[:12] == r["id"] for c in near):
                got.append(r["id"])
    return got


def _dev_branches(x, view, rb, prev_branches):
    """The ticket's development branches (started from a release branch, or recorded as such) and how their start
    fits the resolved release branch. A shared history alone never proves where a branch started."""
    out, flags = [], []
    for b in x.get("branches") or []:
        if b.get("status") == "abandoned":
            continue
        par, src = b.get("parent"), b.get("parent_source")
        prole = b.get("parent_role") or (view.role(par) if par else None)
        if prole not in ("release", None):
            continue
        e = {"branch": b["branch"], "parent": par, "parent_source": src, "status": b.get("status"), "base": "unknown"}
        if not par:
            e["note"] = "start not recorded"
        elif src == "inferred" or b.get("role_guessed"):
            e["base"] = "unproven"
            e["note"] = "shares history with %s (inferred: the original start can't be proven from ancestry)" % par
        elif rb and par == rb:
            e["base"] = "ok"
            e["note"] = "created from %s (%s)" % (par, src or "recorded")
        elif rb and prole == "release":
            e["base"] = "wrong"
            old = par in prev_branches
            e["note"] = "created from %s%s, but the release ships from %s" % (par, " (the previous release)" if old else "", rb)
            flags.append({"kind": "old-release-base" if old else "wrong-base", "branch": b["branch"],
                          "text": "%s was created from %s%s; the release branch is %s." % (
                              b["branch"], par, " (the ticket's previous release)" if old else "", rb)})
        out.append(e)
    return out, flags


def _pr_flags(prs, rb, prev_branches, view):
    flags = []
    for p in prs:
        if p.get("role") != "release" or not rb or p.get("target") == rb:
            continue
        old = p.get("target") in prev_branches
        what = "the ticket's previous release branch %s" % p["target"] if old else p.get("target")
        if p.get("state") in ("open", "draft"):
            txt = "PR #%s targets %s; the release ships from %s: retarget it in the Git host." % (p["number"], what, rb)
        elif p.get("state") == "merged":
            txt = "PR #%s was merged into %s, not %s: those changes still have to reach %s." % (p["number"], what, rb, rb)
        else:
            continue
        p["wrong_target"] = True
        flags.append({"kind": "old-release-pr" if old else "wrong-target", "pr": p["number"], "text": txt})
    return flags


def _fmt(pattern, **kw):
    try:
        return pattern.format(**kw)
    except (KeyError, IndexError, ValueError):
        return ""


def suggest(cfg, name, which, ticket, dev=None, release_branch=None):
    """A suggested branch name from releases.naming (placeholders {ticket} {dev} {release_branch} {release_tail}
    {release_key}); a suggestion to review, never created by Chitragupta."""
    n = repo_settings(cfg, name)["naming"]
    rb = release_branch or ""
    kw = {"ticket": ticket, "release_branch": rb, "release_tail": rb.rstrip("/").split("/")[-1],
          "release_key": (re.findall(r"\b[A-Z][A-Z0-9]{1,9}-\d+", rb) or [""])[0]}
    devname = dev or _fmt(n.get("dev") or "feature/{ticket}", dev="", **kw) or "feature/" + ticket
    return _fmt(n.get(which) or "", dev=devname, **kw) or devname


def next_step(key, name, e, strat, uat_req, cfg):
    """One plain next action for the repo, from where the evidence says the ticket is. Commands are suggestions to
    review, and only when the base is certain."""
    br = e["branch"]
    rb = pinned(br)
    remote = gitcmd.repo_conf(cfg, name)["remote"]
    st = e["stages"]
    fresh = e["freshness"]
    hedge = "" if fresh.get("open_prs_visible") else " (open PRs aren't visible from merge commits: skip this if it's already open)"
    if not rb:
        if br.get("state") == "mapped-missing":
            return "The mapped release branch %s doesn't exist in %s: fix the mapping or fetch (/cg:check %s)." % (br["name"], name, key)
        if br.get("state") == "mapped-unverified":
            return "Fetch %s to verify %s before branching (/cg:check %s)." % (name, br["name"], key)
        c = ", ".join(x["name"] for x in (br.get("candidates") or [])[:3])
        return "Resolve the release branch first: %s%s. No development base is recommended until then." % (
            br.get("missing") or "map the release", " (candidates: %s)" % c if c else "")
    if not strat:
        return ("Start from %s and PR into it; set a branching strategy for %s (`/cg:branch strategy %s "
                "release-then-qa-sync`) so release and QA can be checked." % (rb, name, name))
    if fresh.get("stale") and any((st.get(r) or {}).get("state") == "unknown" for r in ("release", "qa")):
        return "Refresh first: %s's refs are %s, so what's missing can't be confirmed (/cg:check %s)." % (
            name, fresh.get("why_stale") or "stale", key)
    devs = [d for d in e["dev"] if d.get("base") != "wrong"]
    rel_prs = [p for p in e["prs"].get("release") or [] if not p.get("wrong_target")]
    s_rel, s_qa = st.get("release") or {}, st.get("qa") or {}
    if not e["dev"] and not rel_prs and s_rel.get("state") in ("not_started", None):
        dev = suggest(cfg, name, "dev", key, release_branch=rb)
        return "Start from %s: `git switch -c %s %s/%s` (review the name), then PR %s → %s." % (rb, dev, remote, rb, dev, rb)
    if any(d.get("base") == "wrong" for d in e["dev"]) and not devs:
        return "The development branch starts from the wrong release; start again from %s or bring the commits over." % rb
    open_rel = [p for p in rel_prs if p.get("state") in ("open", "draft")]
    wrong_open = [p for p in e["prs"].get("release") or [] if p.get("wrong_target") and p.get("state") in ("open", "draft")]
    if wrong_open and not open_rel and s_rel.get("state") != "present":
        p = wrong_open[0]
        return "Retarget PR #%s from %s to %s in the Git host (or open a new PR %s → %s)." % (
            p["number"], p.get("target"), rb, p.get("source"), rb)
    if s_rel.get("state") in ("not_started",) and devs:
        return "Commit on %s, then open a PR %s → %s." % (devs[0]["branch"], devs[0]["branch"], rb)
    if s_rel.get("state") in ("missing", "partial", "unknown") and not open_rel:
        d = devs[0]["branch"] if devs else suggest(cfg, name, "dev", key, release_branch=rb)
        part = " the remaining commits" if s_rel.get("state") == "partial" else ""
        return "Open a PR %s → %s%s%s." % (d, rb, part and " for" + part, hedge)
    if open_rel and s_rel.get("state") != "present":
        return "Get release PR %s merged into %s." % (", ".join("#%s" % p["number"] for p in open_rel), rb)
    qstep = next((x for x in (strat or {}).get("steps") or [] if x.get("pr_into") == "qa"), None)
    if s_qa.get("state") in ("missing", "partial", "unknown") and qstep:
        open_qa = [p for p in e["prs"].get("qa") or [] if p.get("state") in ("open", "draft")] + \
                  [p for p in e.get("shared") or [] if p.get("role") == "qa" and p.get("state") in ("open", "draft")]
        if open_qa:
            return "Get QA PR %s merged into qa." % ", ".join("#%s" % p["number"] for p in open_qa)
        qa = (gitcmd.repo_conf(cfg, name)["bases"].get("qa") or "qa")
        qa = qa if isinstance(qa, str) else qa[0]
        later = " (later commits are in %s but not in %s)" % (rb, qa) if s_qa.get("state") == "partial" else ""
        if qstep.get("of_role") == "release":
            ib = suggest(cfg, name, "qa", key, dev=devs[0]["branch"] if devs else None, release_branch=rb)
            return ("QA next%s: create a QA integration branch from %s (`git switch -c %s %s/%s`), merge %s into it "
                    "(`git merge %s/%s`), resolve conflicts there, push, then PR %s → %s%s." % (
                        later, qa, ib, remote, qa, rb, remote, rb, ib, qa, hedge))
        src = devs[0]["branch"] if devs else "your release-based branch"
        ib = suggest(cfg, name, "qa", key, dev=devs[0]["branch"] if devs else None, release_branch=rb)
        how = "cherry-pick" if qstep.get("brings") == "cherry-pick" else "merge"
        return "QA next%s: create %s from %s, %s %s into it, then PR %s → %s%s." % (later, ib, qa, how, src, ib, qa, hedge)
    u = e.get("uat_conf")
    if uat_req == "required":
        su = st.get("uat") or {}
        if not u or not u.get("branch"):
            return "UAT is required but no UAT destination is configured for %s (releases.repos.%s.uat)." % (name, name)
        if su.get("state") != "present":
            return uat_action(u, name, key, cfg, remote, devs, su)
        return "In UAT branch %s: verify the UAT deployment and acceptance." % u["branch"]
    if uat_req == "unknown":
        return "Confirm whether %s needs UAT (`/cg:branch uat %s required|not_required`)." % (key, key)
    return "Nothing left for %s." % name


def uat_action(u, name, key, cfg, remote, devs, su):
    p = u.get("promotion") or {}
    kind = p.get("kind") or "unspecified"
    later = " (%s)" % su.get("detail") if su.get("state") == "partial" else ""
    if kind == "pr":
        src = p.get("from") or u["branch"]
        b = suggest(cfg, name, "uat", key, dev=devs[0]["branch"] if devs else None)
        return "UAT next%s: create %s from %s (`git switch -c %s %s/%s`), %s the changes into it, then PR %s → %s." % (
            later, b, src, b, remote, src, p.get("brings") or "cherry-pick", b, u["branch"])
    if kind == "sync":
        return "UAT next%s: %s receives %s at its next sync; check again after it." % (later, u["branch"], p.get("sync_from") or "its source")
    return "UAT next%s: promote the changes to %s by your team's process (the path isn't configured); this updates " \
           "when %s contains them." % (later, u["branch"], u["branch"])


def evaluate(store, key, rx, cfg, now=None):
    """Phase 2 (after the delivery check): per repo dev branches, PRs per destination (shared integration PRs
    included), stages, flags, next step, freshness; then UAT facts, reminders and readiness. -> True if changed."""
    now = now or common.now()
    tk = store.ticket(key)
    rel = tk.get("release")
    if not isinstance(rel, dict) or not enabled(cfg):
        return False
    before = _strip(rel)
    rel = copy.deepcopy(rel)
    sel = next((v for v in rel.get("releases") or [] if v.get("key") == rel.get("selected")), None)
    prev_branches = {b for h in rel.get("history") or [] for b in (h.get("branches") or {}).values() if b}
    facts = jira_facts(key, cfg)
    req = (rel.get("uat") or {}).get("requirement") or {"state": "unknown", "source": ""}
    git = (tk.get("git") or {}).get("repos") or {}
    reminders, all_flags = [], []
    for name, e in (rel.get("repos") or {}).items():
        repo = rx.get(name) if rx else None
        view = RepoView(name, cfg, repo=repo, meta=None if repo is not None else _meta(name))
        e["freshness"] = _freshness(view, cfg, now)
        x = git.get(name) or {}
        br = e.get("branch") or {}
        rb = pinned(br)
        if br.get("state") == "confirmed" and br.get("name") and view.exists(br["name"]) is None:
            br.update(state="mapped-missing", confidence="low", missing="%s no longer exists in %s" % (br["name"], name))
        strat = gitcmd.settings(cfg)["strategies"].get(e.get("strategy") or "") if e.get("strategy") else None
        e["dev"], flags = _dev_branches(x, view, rb, prev_branches - ({rb} if rb else set()))
        e["prs"] = _prs_by_role(x, view)
        flags += _pr_flags(e["prs"].get("release") or [], rb, prev_branches - ({rb} if rb else set()), view)
        cache = dict(e.get("_shared") or {})
        if repo is not None:
            try:
                e["shared"] = shared_prs(x, repo, ("qa", "uat"), cache)
                keep = set(cache.get("_used") or [])
                e["_shared"] = {k: v for k, v in cache.items() if k in keep}  # heads still in play, carrying or not
            except Exception:  # noqa: BLE001 - a git hiccup leaves the shared PRs as they were
                e["shared"] = e.get("shared") or []
        else:
            e["shared"] = e.get("shared") or []
        fresh = e["freshness"]
        stages = {"release": stage(x, "release", fresh, rb), "qa": stage(x, "qa", fresh)}
        if req["state"] == "required" and (e.get("uat_conf") or {}).get("branch"):
            stages["uat"] = stage(x, "uat", fresh)
        elif req["state"] == "not_required":
            stages["uat"] = {"state": "not_applicable", "detail": "UAT not required (%s)" % req.get("source")}
        elif req["state"] == "required":
            stages["uat"] = {"state": "unknown", "detail": "UAT required, but no UAT destination is configured for %s" % name}
        else:
            stages["uat"] = {"state": "unknown", "detail": "UAT requirement unknown (%s)" % req.get("source")}
        if not x and e.get("candidate"):
            for s in stages.values():
                if s.get("state") in ("unknown",) and "requirement" not in (s.get("detail") or ""):
                    s.update(state="not_started", detail="no branches for this ticket here")
        hist = e.get("complete_at") or {}
        for role, s in stages.items():
            if s.get("state") == "present":
                hist[role] = hist.get(role) or now.isoformat(timespec="seconds")
            elif s.get("state") == "partial" and hist.get(role):
                s["detail"] += "; was complete %s, later commits need promoting" % hist[role][:10]
        e["complete_at"] = hist
        e["stages"] = stages
        e["flags"] = flags
        e["next"] = next_step(key, name, e, strat, req["state"], cfg) if not e.get("candidate") else (
            "If %s changes %s: start from %s." % (key, name, rb) if rb else "If %s changes %s: %s" % (
                key, name, br.get("missing") or "resolve its release branch"))
        all_flags += [dict(f, repo=name) for f in flags]
        if e.get("candidate"):
            continue
        if not rb:
            reminders.append({"id": "map:%s" % name, "kind": "mapping", "repo": name, "severity": "warn",
                              "text": "%s: release branch for %s not confirmed — %s." % (
                                  name, (sel or {}).get("name") or "the release", br.get("missing") or "map it")})
        for f in flags:
            reminders.append({"id": "%s:%s:%s" % (f["kind"], name, f.get("pr") or f.get("branch")), "kind": f["kind"],
                              "repo": name, "severity": "alert", "text": "%s: %s" % (name, f["text"])})
        su = stages.get("uat") or {}
        if req["state"] == "required":
            if su.get("state") in ("missing", "partial"):
                qa_done = stages["qa"].get("state") == "present"  # before QA, the next step is QA's: no UAT advice yet
                reminders.append({"id": "uat-missing:%s" % name, "kind": "uat-missing", "repo": name,
                                  "severity": "alert" if qa_done else "warn",
                                  "text": "%s: UAT required — %s%s" % (name, su.get("detail"),
                                                                       ". " + e["next"] if qa_done else " yet.")})
            elif su.get("state") in ("unknown", "not_started") and stages["qa"].get("state") == "present":
                reminders.append({"id": "uat-verify:%s" % name, "kind": "uat-verify", "repo": name, "severity": "warn",
                                  "text": "%s: UAT required — %s; verify it reaches UAT." % (name, su.get("detail"))})
    dep = _status_evidence(facts, cfg, "deployed")
    acc = _status_evidence(facts, cfg, "accepted")
    rel["uat"] = {"requirement": req, "deployed": dep, "accepted": acc}
    live = [n for n, e in (rel.get("repos") or {}).items() if not e.get("candidate")]
    if req["state"] == "required" and live and all(((rel["repos"][n].get("stages") or {}).get("uat") or {}).get("state")
                                                   == "present" for n in live):
        for kind, ev in (("deployed", dep), ("accepted", acc)):
            if ev["state"] != "yes":
                reminders.append({"id": "uat-%s" % kind, "kind": "uat-%s" % kind, "repo": "", "severity": "warn",
                                  "text": "UAT branch has the changes, but %s is unverified (%s)." % (
                                      "the UAT deployment" if kind == "deployed" else "UAT acceptance", ev["source"])})
    if req["state"] == "unknown" and sel:
        reminders.insert(0, {"id": "uat-unknown", "kind": "uat-unknown", "repo": "", "severity": "info",
                             "text": "Does %s need UAT? %s (`/cg:branch uat %s required|not_required`)." % (
                                 key, req.get("source"), key)})
    if not sel and (rel.get("selection") or {}).get("how") == "ambiguous":
        reminders.insert(0, {"id": "release-ambiguous", "kind": "release-ambiguous", "repo": "", "severity": "warn",
                             "text": "%s: %s (`/cg:branch release-pick %s \"<release>\"`)." % (
                                 key, rel["selection"].get("missing"), key)})
    seen, uniq = set(), []
    for r in reminders:
        if r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    rel["reminders"] = uniq
    rel["flags"] = all_flags
    rel["readiness"] = readiness(rel)
    rel["computed_at"] = now.isoformat(timespec="seconds")
    if _strip(rel) == before:
        return False
    tk["release"] = rel
    store.touch_ticket(key)
    return True


def readiness(rel):
    """{ready: bool, missing: [what stops it]}: ready only when every affected repo has every required stage
    confirmed present on fresh refs, the release and its branches are resolved and UAT is settled."""
    miss = []
    if not rel.get("selected"):
        miss.append((rel.get("selection") or {}).get("missing") or "no release")
    req = ((rel.get("uat") or {}).get("requirement") or {}).get("state")
    if req == "unknown":
        miss.append("UAT requirement unknown")
    live = {n: e for n, e in (rel.get("repos") or {}).items() if not e.get("candidate")}
    if not live:
        miss.append("no affected repository known yet")
    for n, e in sorted(live.items()):
        if not pinned(e.get("branch") or {}):
            miss.append("%s: release branch not confirmed" % n)
        for role in ("release", "qa") + (("uat",) if req == "required" else ()):
            s = (e.get("stages") or {}).get(role) or {}
            if s.get("state") != "present":
                miss.append("%s: %s %s" % (n, role, (s.get("state") or "unknown").replace("_", " ")))
        if (e.get("freshness") or {}).get("stale"):
            miss.append("%s: refs %s" % (n, e["freshness"].get("why_stale")))
        if e.get("flags"):
            miss.append("%s: %d flag%s" % (n, len(e["flags"]), "" if len(e["flags"]) == 1 else "s"))
    if req == "required":
        for kind in ("deployed", "accepted"):
            if ((rel.get("uat") or {}).get(kind) or {}).get("state") != "yes":
                miss.append("UAT %s unverified" % kind)
    return {"ready": not miss, "missing": miss}


# --------------------------------------------------------------------------- the tick

def refresh(store, rx, cfg, keys=None, now=None, phase="resolve"):
    """Run one phase for the tickets release tracking cares about. -> the tickets whose pins changed (resolve; their
    delivery check must be recomputed) or whose record changed (evaluate)."""
    if not enabled(cfg):
        return set()
    from .store import Store
    keys = set(keys if keys is not None else candidates(store, cfg, now))
    views = {n: RepoView(n, cfg, repo=r) for n, r in (rx or {}).items()}
    sib = sibling_index(store, views) if phase == "resolve" else None
    out = set()
    for t in sorted(keys):
        if common.is_placeholder(t) or gitcmd.ignored(cfg, t):
            continue
        if t not in store.tickets and t not in set(Store.all_ticket_ids()):
            continue
        try:
            ch = resolve(store, t, views, cfg, now, sib) if phase == "resolve" else evaluate(store, t, rx, cfg, now)
        except Exception:  # noqa: BLE001 - one ticket never stops the rest
            import traceback
            common.log("agent", "release tracking %s failed: %s" % (t, traceback.format_exc()))
            continue
        if ch:
            out.add(t)
    return out


def candidates(store, cfg, now=None, days=60):
    """Tickets worth a release record: worked on in the last `days` days, with a release in Jira (card or scope
    search), or already tracked."""
    from .store import Store
    now = now or common.now()
    cutoff = (now - _dt.timedelta(days=days)).date().isoformat()
    out = set()
    sc = (common.read_json(scope_path(), {}) or {}).get("issues") or {}
    for t in Store.all_ticket_ids():
        tk = store.ticket(t)
        if (tk.get("last") or "") >= cutoff or tk.get("release") or t in sc:
            out.add(t)
    return out


# --------------------------------------------------------------------------- reading: brief, report, overview

def _ago(ts, now):
    t = common.parse_ts(ts)
    if not t:
        return "never"
    m = int((now - t).total_seconds() // 60)
    return "%d min ago" % m if m < 120 else "%d h ago" % (m // 60) if m < 2880 else "%d days ago" % (m // 1440)


def release_label(rel):
    sel = next((v for v in rel.get("releases") or [] if v.get("key") == rel.get("selected")), None)
    if sel:
        d = sel.get("planned_date")
        return "%s (planned %s%s)" % (sel.get("name") or sel.get("id"), d or "date unknown",
                                       ", " + sel["date_source"] if d and sel.get("date_source") else "")
    how = rel.get("selection") or {}
    return "unresolved — %s" % (how.get("missing") or how.get("how") or "no release")


def _promotion_text(p):
    p = p or {}
    k = p.get("kind")
    if k == "pr":
        return "PR from a branch cut from %s" % (p.get("from") or "the UAT branch")
    if k == "sync":
        return "synced from %s" % (p.get("sync_from") or "its source")
    return "promotion path not configured"


def _stage_text(s):
    if not s:
        return "unknown"
    return "%s (%s)" % ((s.get("state") or "unknown").replace("_", " "), s.get("detail") or "")


def brief_lines(tk, key, now=None, limit_repos=4, relative=True):
    """The compact branching brief for session context and recall, one block per repo. `relative` adds "N min ago"
    (live views); files written for later (session context, README) keep absolute times only."""
    rel = tk.get("release")
    if not isinstance(rel, dict) or not rel.get("v"):
        return []
    now = now or common.now()

    def when(ts):
        stamp = (ts or "?")[:16].replace("T", " ") if ts else "never"
        return "%s, %s" % (stamp, _ago(ts, now)) if relative and ts else stamp
    L = ["Release plan for %s (computed %s; refresh with /cg:check %s):" % (key, when(rel.get("computed_at")), key),
         "Release: " + release_label(rel)]
    sel = next((v for v in rel.get("releases") or [] if v.get("key") == rel.get("selected")), None)
    if sel and sel.get("name_date"):
        L[-1] += "; name says %s (shown only, never the planned date)" % sel["name_date"]["text"]
    if rel.get("history"):
        h = rel["history"][-1]
        L.append("Moved from %s on %s." % (", ".join(x.get("name") or "?" for x in h.get("releases") or []), h["at"][:10]))
    req = (rel.get("uat") or {}).get("requirement") or {}
    repos = rel.get("repos") or {}
    for name, e in sorted(repos.items(), key=lambda kv: (bool(kv[1].get("candidate")), kv[0]))[:limit_repos]:
        br = e.get("branch") or {}
        rb = pinned(br)
        tag = " (candidate: no branches yet)" if e.get("candidate") else ""
        L.append("Repository %s%s:" % (name, tag))
        if rb:
            L.append("  Development base: %s (%s; %s; refs fetched %s)" % (rb, br.get("state"), br.get("source"),
                                                                          when(br.get("verified_at"))))
            L.append("  Development PR target: %s" % rb)
        else:
            c = "; ".join("%s (%s)" % (x["name"], x["why"]) for x in (br.get("candidates") or [])[:3])
            L.append("  Development base: not certain — %s%s" % (br.get("missing") or "unresolved", "; suggestions: " + c if c else ""))
        if e.get("candidate"):
            continue
        obs = ["%s ← %s [%s]" % (d["branch"], d.get("parent") or "?", d.get("base")) for d in e.get("dev") or []][:3]
        prs = ["PR #%s %s→%s %s%s" % (p.get("number"), p.get("source"), p.get("target"), p.get("state"),
                                      " (shared, carries %s/%s)" % (p.get("carries"), p.get("of")) if p.get("shared") else "")
               for role in ("release", "qa", "uat") for p in ((e.get("prs") or {}).get(role) or []) +
               [q for q in e.get("shared") or [] if q.get("role") == role]][:5]
        L.append("  Observed: %s" % ("; ".join(obs + prs) or "no branches or PRs yet"))
        st = e.get("stages") or {}
        L.append("  Release: %s · QA: %s" % (_stage_text(st.get("release")), _stage_text(st.get("qa"))))
        u = e.get("uat_conf") or {}
        L.append("  UAT: %s (%s)%s; %s" % (req.get("state", "unknown").replace("_", " "), req.get("source") or "",
                                           " → %s (%s)" % (u.get("branch"), _promotion_text(u.get("promotion")))
                                           if u.get("branch") else "", _stage_text(st.get("uat"))))
        for f in e.get("flags") or []:
            L.append("  ⚠ " + f["text"])
        L.append("  Next: " + (e.get("next") or "?"))
        fr = e.get("freshness") or {}
        unc = ([fr["why_stale"]] if fr.get("stale") else []) + (fr.get("errors") or [])[:1] + \
            ([] if fr.get("open_prs_visible") else ["open PRs not visible (merge commits only)"])
        if unc:
            L.append("  Uncertainty: " + "; ".join(unc))
    if len(repos) > limit_repos:
        L.append("(+%d more repositories on the ticket page)" % (len(repos) - limit_repos))
    j = rel.get("jira") or {}
    if not j.get("source"):
        L.append("Uncertainty: no Jira data read for %s yet (needs the Jira MCP and a ticket card)." % key)
    elif j.get("error"):
        L.append("Uncertainty: last Jira read failed: %s" % str(j["error"])[:160])
    else:
        L.append("Jira read %s." % when(j.get("fetched_at")))
    rems = [r for r in rel.get("reminders") or [] if r.get("kind") in ("uat-missing", "uat-verify", "uat-unknown",
                                                                        "release-ambiguous", "uat-deployed", "uat-accepted")]
    if rems:
        L.append("Reminders: " + " | ".join(r["text"] for r in rems[:4]))
    return L


def view(tk, key, now=None):
    """The ticket page's release data: the record without internal caches, plus the brief text."""
    rel = tk.get("release")
    if not isinstance(rel, dict) or not rel.get("v"):
        return None
    out = copy.deepcopy(rel)
    for e in (out.get("repos") or {}).values():
        e.pop("_shared", None)
    out.pop("pins", None)
    out["label"] = release_label(rel)
    return out


def _excluded(tk, cfg, facts_status=""):
    st = (((tk.get("jira") or {}).get("status")) or facts_status or "").strip().lower()
    return st in settings(cfg)["jira"]["exclude_statuses"]


def mine(tk, cfg, scope_issue=None):
    """(bool, why) — is this one of my tickets for the overview? Jira assignment, my own development branches
    (my commits on a branch linked to the ticket), or my Claude Code work on it. Authoring a shared integration PR
    alone never makes a ticket mine."""
    s = settings(cfg)["scope"]
    rel = tk.get("release") or {}
    if s.get("assigned") and (rel.get("assignee_me") or (scope_issue or {}).get("assigned_scope")):
        return True, "assigned to you"
    if s.get("branches"):
        for x in ((tk.get("git") or {}).get("repos") or {}).values():
            # a branch named for the ticket (or made in your session / linked by you) with your own commits on it;
            # a branch linked only through commit messages or patches (an integration branch) never counts
            if any(b.get("mine", b.get("own")) and b.get("status") != "abandoned" and
                   b.get("link") in ("branch name", "session", "manual") for b in x.get("branches") or []):
                return True, "your development branch"
    if s.get("worked") and tk.get("first"):
        return True, "you worked on it"
    return False, ""


def month_overview(month=None, cfg=None, now=None, store=None):
    """My tickets by planned release date for one month (YYYY-MM, default: this month in the configured day
    boundary), plus overdue tickets and tickets whose release date is unresolved, each with per-repo state."""
    from .store import Store
    cfg = cfg if cfg is not None else common.load_config()
    now = now or common.now()
    today = common.workday(now, cfg)
    month = month or today[:7]
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise ValueError("month must be YYYY-MM")
    sc = common.read_json(scope_path(), {}) or {}
    issues = sc.get("issues") or {}
    groups, overdue, unresolved = {}, [], []
    keys = set(Store.all_ticket_ids()) | set(issues)
    for t in sorted(keys):
        if common.is_placeholder(t) or gitcmd.ignored(cfg, t):
            continue
        tk = (store.ticket(t) if store is not None and t in store.tickets else
              common.read_json(common.hp("data", "tickets", t + ".json"), {})) or {}
        iss = issues.get(t)
        rel = tk.get("release") if isinstance(tk.get("release"), dict) and tk["release"].get("v") else None
        if tk.get("jira_missing") or _excluded(tk, cfg, (iss or {}).get("status")):
            continue
        # in the scope search: yours when Jira says so, or when the search itself only returns your tickets
        assigned = bool(iss) and (iss.get("assignee_me") is True or (iss.get("assignee_me") is None and sc.get("mine_only")))
        ok, why = mine(tk, cfg, {"assigned_scope": assigned})
        if not ok:
            continue
        row = _row(t, tk, rel, iss, why, today)
        if row is None:
            continue
        pd = row["planned_date"]
        if row["unresolved"]:
            unresolved.append(row)
        elif pd[:7] == month:
            g = groups.setdefault(row["release"], {"release": row["release"], "planned_date": pd, "tickets": []})
            g["tickets"].append(row)
        elif pd < today and not row["ready"] and month >= today[:7]:
            overdue.append(row)
    out = {"month": month, "today": today, "groups": sorted(groups.values(), key=lambda g: (g["planned_date"], g["release"])),
           "overdue": sorted(overdue, key=lambda r: r["planned_date"]), "unresolved": unresolved,
           "scope": {"fetched_at": sc.get("fetched_at"), "error": sc.get("error"), "jql": sc.get("jql")},
           "enabled": enabled(cfg)}
    for g in out["groups"]:
        g["tickets"].sort(key=lambda r: (r["ready"], r["key"]))
    return out


def _row(t, tk, rel, iss, why, today):
    title = tk.get("title") or (iss or {}).get("summary") or ""
    if rel:
        sel = next((v for v in rel.get("releases") or [] if v.get("key") == rel.get("selected")), None)
        repos = []
        for name, e in sorted((rel.get("repos") or {}).items()):
            if e.get("candidate"):
                continue
            st = e.get("stages") or {}
            repos.append({"repo": name, "branch": pinned(e.get("branch") or {}), "branch_state": (e.get("branch") or {}).get("state"),
                          "release": st.get("release"), "qa": st.get("qa"), "uat": st.get("uat"), "next": e.get("next"),
                          "prs": [dict(p) for role in ("release", "qa", "uat") for p in (e.get("prs") or {}).get(role) or []] +
                                 [dict(p) for p in e.get("shared") or []],
                          "flags": e.get("flags") or [], "freshness": e.get("freshness") or {}})
        rd = rel.get("readiness") or readiness(rel)
        return {"key": t, "title": title, "why": why, "release": (sel or {}).get("name") or "", "planned_date": (sel or {}).get("planned_date") or "",
                "date_source": (sel or {}).get("date_source") or "", "unresolved": not sel or not sel.get("planned_date"),
                "unresolved_why": (rel.get("selection") or {}).get("missing") if not sel else
                ("the release has no planned date in Jira" if not sel.get("planned_date") else ""),
                "repos": repos, "uat": rel.get("uat") or {}, "reminders": rel.get("reminders") or [],
                "ready": bool(rd.get("ready")), "missing": rd.get("missing") or [], "computed_at": rel.get("computed_at"),
                "jira": rel.get("jira") or {}, "overdue": False}
    if not iss:
        return None
    vers = iss.get("versions") or []
    v = vers[0] if len(vers) == 1 else None
    if v:
        v = (enrich([v]) or [v])[0]
    pd = (v or {}).get("release_date") or iss.get("planned_date") or ""
    return {"key": t, "title": title, "why": why, "release": (v or {}).get("name") or "", "planned_date": pd,
            "date_source": "Jira version release date" if (v or {}).get("release_date") else ("Jira field" if pd else ""),
            "unresolved": not v or not pd,
            "unresolved_why": ("%d fix versions" % len(vers) if len(vers) > 1 else "no planned date" if v else "no fix version"),
            "repos": [], "uat": {}, "reminders": [], "ready": False,
            "missing": ["not started: no branches or worklog yet"], "computed_at": None, "jira": {"source": "scope"},
            "overdue": False}


def overview_lines(ov):
    L = ["# Releases — %s" % ov["month"]]
    if not ov.get("enabled"):
        L.append("Release tracking is off (releases.enabled in config.json; docs/releases.md).")
    sc = ov.get("scope") or {}
    L.append("Jira scope search: %s%s" % (sc.get("fetched_at") or "not run yet", " · error: %s" % sc["error"] if sc.get("error") else ""))

    def rows(lst):
        for r in lst:
            state = "ready" if r["ready"] else "in progress"
            L.append("- %s%s · %s · %s" % (r["key"], " — " + r["title"] if r["title"] else "", state, r["why"]))
            for x in r["repos"]:
                L.append("  - %s: base %s · release %s · QA %s · UAT %s" % (
                    x["repo"], x["branch"] or "unresolved", (x["release"] or {}).get("state", "?"),
                    (x["qa"] or {}).get("state", "?"), (x["uat"] or {}).get("state", "?")))
                if x.get("next"):
                    L.append("    next: " + x["next"])
            if r["missing"] and not r["ready"]:
                L.append("  missing: " + "; ".join(r["missing"][:6]))
    for g in ov["groups"]:
        L += ["", "## %s — planned %s" % (g["release"], g["planned_date"])]
        rows(g["tickets"])
    if ov["overdue"]:
        L += ["", "## Overdue (planned before today, not complete)"]
        rows(ov["overdue"])
    if ov["unresolved"]:
        L += ["", "## Release date unresolved"]
        for r in ov["unresolved"]:
            L.append("- %s%s · %s" % (r["key"], " — " + r["title"] if r["title"] else "", r.get("unresolved_why") or ""))
    if not (ov["groups"] or ov["overdue"] or ov["unresolved"]):
        L.append("No tickets of yours are scheduled for %s." % ov["month"])
    return L
