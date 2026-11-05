"""The developer profile: whose work Chitragupta tracks, and the one ownership policy every reader applies.

    settings(cfg) / progress_settings(cfg)   profile.* and progress.* with the defaults filled in
    load(cfg)                 the confirmed identity: email aliases, provider accounts, scope (repos, Jira projects)
    signature(cfg)            a hash of everything that decides what is "yours"; derived files carry it
    zone(name) / tzinfo(cfg) / day_of(ts, cfg) / day_bounds(day, cfg)   the profile's time zone and work days in it
    window_start(cfg) / coverage(cfg)        how far back history is collected, and how that is labelled
    Policy(cfg)               email_mine, commit_mine, commit_role, person_mine, pr_mine, ticket_state,
                              ticket_current, repo_in_scope, project_in_scope, is_bot
    status(cfg)               per provider (git, Jira, each Git host instance): ok | unconfigured | partial | error
    candidates / discover / confirm   identity suggestions with their provenance; never active until confirmed
    context_text(cfg)         the compact profile line for Claude (no emails, no account ids)
    reconcile(store, cfg, state)      after an identity or scope change: archive what is no longer yours, back-fill
    tick(store, cfg, state)   the background step: local candidates daily, reconcile, the context cache

Confirmed means written by you: `git_author_emails`, `profile.accounts` (and the legacy `mcp.jira.account_id` when the
profile has no Jira account). Nothing here matches display names, email domains, look-alike addresses (no dot or
plus stripping), or a provider's signed-in account that you haven't confirmed. With nothing confirmed for a provider
its personal data is "unconfigured": never everyone's.
"""
import datetime as _dt
import hashlib
import json
import os
import re

from . import common

PROVIDERS = ("jira", "github", "gitlab", "azure", "bitbucket")
HOST_CLI = {"github": "gh", "gitlab": "glab", "azure": "az"}
DEFAULT_BOTS = [r"\[bot\]", r"^noreply@github\.com$", r"^(?:dependabot|renovate|github-actions)\b", r"(?:^|[._+-])bot@"]
DEFAULTS = {
    "display_name": "", "email": "", "accounts": [], "repos": [], "jira_projects": [], "focus": "", "role": "",
    "technologies": [], "timezone": "", "tracking_start": "", "backfill_days": 90, "include_tickets": {},
    "bot_patterns": DEFAULT_BOTS, "celebrations": True, "animations": True, "reflection": True,
}
PROGRESS_DEFAULTS = {
    "exclude_globs": ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json", "poetry.lock",
                      "Pipfile.lock", "Cargo.lock", "composer.lock", "Gemfile.lock", "go.sum", "packages.lock.json",
                      "vendor/**", "node_modules/**", "dist/**", "build/**", "out/**", "target/**", "*.min.js",
                      "*.min.css", "*.map", "*.generated.*", "*.g.cs", "*.designer.cs", "*_pb2.py", "*.pb.go"],
    "done_statuses": ["done", "closed", "resolved"],
}
EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                   r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OFFSET = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.I)
CONTEXT_MAX = 600
HISTORY_MAX = 50
# Reconciliation rules version: a new one re-runs reconcile once (backed up) even when the profile didn't change.
# 2: commit records whose author can't be verified are quarantined instead of left in place.
POLICY_V = 2
LOCAL = object()  # day_of(..., tz=LOCAL): this machine's zone, whatever the profile says


# --------------------------------------------------------------------------- settings

def _coerce(default, v):
    """`v` when it has the default's type (bools and ints kept apart), else the default."""
    if isinstance(default, bool):
        return v if isinstance(v, bool) else default
    if isinstance(default, int):
        return v if isinstance(v, int) and not isinstance(v, bool) else default
    return v if isinstance(v, type(default)) else default


def settings(cfg):
    """profile.* with defaults; unknown keys and values of the wrong type fall back to the default."""
    raw = (cfg or {}).get("profile")
    raw = raw if isinstance(raw, dict) else {}
    s = json.loads(json.dumps(DEFAULTS))
    for k in s:
        if k in raw:
            s[k] = _coerce(s[k], raw[k])
    s["backfill_days"] = min(365, max(1, s["backfill_days"]))
    return s


def progress_settings(cfg):
    raw = (cfg or {}).get("progress")
    raw = raw if isinstance(raw, dict) else {}
    s = json.loads(json.dumps(PROGRESS_DEFAULTS))
    for k in s:
        if k in raw:
            s[k] = _coerce(s[k], raw[k])
    s["done_statuses"] = [str(x).strip().lower() for x in s["done_statuses"] if str(x).strip()]
    return s


def norm_email(e):
    """The comparable form of an address: trimmed and lower-cased whole. Never dot- or plus-stripped."""
    e = str(e or "").strip().lower()
    return e if EMAIL.match(e) else ""


def mask(value):
    """An email or id as you'd show it on screen by default: d•••@a•••.test, 7120•••0001. Never for matching."""
    v = str(value or "")
    if not v:
        return ""
    if "@" in v:
        local, _, dom = v.partition("@")
        tld = dom.rsplit(".", 1)[-1] if "." in dom else ""
        # the last letter too when the name is long enough, so two masked suggestions can be told apart
        return "%s•••%s@%s•••%s" % (local[:1], local[-1:] if len(local) >= 4 else "", dom[:1], "." + tld if tld else "")
    return v[:4] + "•••" + v[-4:] if len(v) > 10 else v[:1] + "•••"


def masked_candidates(c):
    out = []
    for x in c.get("candidates") or []:
        x = dict(x)
        if x.get("kind") == "email":
            x["value"] = mask(x.get("value"))
        if x.get("id"):
            x["id"] = mask(x["id"])
        x.pop("node_id", None)
        out.append(x)
    return dict(c, candidates=out)


def _accounts(rows):
    out = []
    for a in rows if isinstance(rows, list) else []:
        if not isinstance(a, dict):
            continue
        prov = str(a.get("provider") or "").strip().lower()
        aid, login = str(a.get("id") or "").strip(), str(a.get("login") or "").strip()
        if prov not in PROVIDERS or not (aid or login):
            continue
        out.append({"provider": prov, "instance": str(a.get("instance") or "").strip().lower().rstrip("/"),
                    "id": aid, "node_id": str(a.get("node_id") or "").strip(), "login": login,
                    "source": str(a.get("source") or ""), "confirmed_at": str(a.get("confirmed_at") or "")})
    return out


def account_key(a):
    """An account's identity: provider, instance and its stable id (else its login, case-insensitive). Two accounts
    of yours on the same instance differ here; the same account entered twice doesn't."""
    a = a if isinstance(a, dict) else {}
    ident = str(a.get("id") or "").strip() or "login:" + str(a.get("login") or "").strip().lower()
    return (str(a.get("provider") or "").strip().lower(), str(a.get("instance") or "").strip().lower().rstrip("/"), ident)


def load(cfg):
    """The confirmed identity and scope. Reads only the config."""
    cfg = cfg or {}
    s = settings(cfg)
    aliases = sorted({norm_email(e) for e in cfg.get("git_author_emails") or [] if norm_email(e)})
    accounts = _accounts(s["accounts"])
    jiras = [a for a in accounts if a["provider"] == "jira"]
    jira = jiras[0] if jiras else None
    legacy = str((((cfg.get("mcp") or {}).get("jira") or {}).get("account_id")) or "").strip()
    if jira is None and legacy:
        jira = {"provider": "jira", "instance": "", "id": legacy, "login": "", "legacy": True,
                "source": "mcp.jira.account_id (the older setting)", "confirmed_at": ""}
        jiras = [jira]
    watched = [r for r in cfg.get("watched_repos") or [] if isinstance(r, str) and r.strip()]
    sel = [r for r in s["repos"] if isinstance(r, str) and r.strip()]
    if sel:
        by = {common.norm_path(r): r for r in watched}
        repos = [by[common.norm_path(r)] for r in sel if common.norm_path(r) in by]
        missing = [r for r in sel if common.norm_path(r) not in by]
    else:
        repos, missing = watched, []
    projects = [p for p in (s["jira_projects"] or cfg.get("ticket_prefixes") or []) if isinstance(p, str) and p]
    include = {str(k).upper(): str(v) for k, v in (s["include_tickets"] or {}).items() if str(v).strip()}
    return {"display_name": s["display_name"].strip(), "email": norm_email(s["email"]), "aliases": aliases,
            "accounts": accounts, "jira": jira, "jira_accounts": jiras, "repos": repos, "repos_missing": missing,
            "repos_selected": bool(sel), "projects": projects, "include": include, "settings": s}


def signature(cfg):
    """Changes whenever what counts as yours could change: identity, scope, zone, day boundary, history range,
    exclusions, completion statuses. Derived files store it; a reader ignores one whose signature differs."""
    p, pr = load(cfg), progress_settings(cfg)
    s = p["settings"]
    basis = {
        "aliases": p["aliases"],
        "accounts": sorted([a["provider"], a["instance"], a["id"], a["node_id"], a["login"].lower()] for a in p["accounts"]),
        "jira": [(p["jira"] or {}).get("instance", ""), (p["jira"] or {}).get("id", "")] if len(p["jira_accounts"]) < 2
        else sorted([a.get("instance", ""), a.get("id", "")] for a in p["jira_accounts"]),
        "repos": sorted(common.norm_path(r) for r in p["repos"]), "projects": sorted(p["projects"]),
        "include": sorted(p["include"]), "timezone": s["timezone"], "day_starts_at": (cfg or {}).get("day_starts_at") or "04:00",
        "tracking_start": s["tracking_start"], "backfill_days": s["backfill_days"], "bots": s["bot_patterns"],
        "exclude_globs": pr["exclude_globs"], "done_statuses": sorted(pr["done_statuses"]),
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- time zone and days

def zone(name):
    """tzinfo for a profile zone: None for "" / "local" (this machine's zone, DST included), a fixed offset for
    "+05:30" / "UTC-04:00", else an IANA name this machine has data for. ValueError when it can't be resolved."""
    n = str(name or "").strip()
    if not n or n.lower() == "local":
        return None
    m = _OFFSET.match(n)
    if m:
        hh, mm = int(m.group(2)), int(m.group(3) or 0)
        if hh > 14 or mm > 59:
            raise ValueError("%s isn't a UTC offset (use something like +05:30)" % n)
        off = _dt.timedelta(hours=hh, minutes=mm) * (-1 if m.group(1) == "-" else 1)
        return _dt.timezone(off, "UTC%s%02d:%02d" % (m.group(1), hh, mm))
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover - Python 3.9+ always has it
        raise ValueError("this Python has no time zone database")
    try:
        return ZoneInfo(n)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError("this machine has no time zone data for %s; leave it empty for this machine's zone, or use a "
                         "UTC offset such as +05:30" % n)


def tzinfo(cfg):
    """The profile's zone, or None (this machine's) when it is empty or can't be resolved here (status says so)."""
    try:
        return zone(settings(cfg)["timezone"])
    except ValueError:
        return None


def _aware(ts):
    if isinstance(ts, _dt.datetime):
        return ts if ts.tzinfo else ts.astimezone()
    if not ts:
        return None
    try:
        d = _dt.datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def in_zone(ts, tz):
    """An aware moment shown in `tz` (None: this machine's zone, with its own DST rules for that date)."""
    d = _aware(ts)
    if d is None:
        return None
    return d.astimezone(tz) if tz is not None else d.astimezone()


def day_of(ts, cfg, tz=LOCAL):
    """The work day (YYYY-MM-DD) a moment belongs to in the profile zone: work before day_starts_at counts as the day
    before. The boundary is wall-clock time in that zone, so a DST change moves it with the clock."""
    tz = tzinfo(cfg) if tz is LOCAL else tz
    d = in_zone(ts, tz)
    if d is None:
        return None
    return (d.replace(tzinfo=None) - common.day_offset(cfg)).date().isoformat()


def day_bounds(day, cfg, tz=LOCAL):
    """[start, end) of a work day in the profile zone as aware datetimes; a DST day is 23 or 25 hours long."""
    tz = tzinfo(cfg) if tz is LOCAL else tz
    d = _dt.date.fromisoformat(day)
    off = common.day_offset(cfg)

    def at(day_):
        wall = _dt.datetime.combine(day_, _dt.time()) + off
        return wall.replace(tzinfo=tz) if tz is not None else wall.astimezone()
    return at(d), at(d + _dt.timedelta(days=1))


def today(cfg, now=None):
    return day_of(now or common.now(), cfg)


# --------------------------------------------------------------------------- history range

def tracking_start(cfg):
    """profile.tracking_start, else the first tick's day (health.json), else None."""
    s = settings(cfg)["tracking_start"]
    if DATE.match(s or ""):
        return s
    af = str(((common.read_json(common.wl("health.json"), {}) or {}).get("jira_worklog") or {}).get("active_from") or "")
    return af[:10] if DATE.match(af[:10]) else None


def window_start(cfg, day=None):
    """The first day history is collected for: the later of tracking start and today - backfill_days."""
    day = day or today(cfg)
    back = (_dt.date.fromisoformat(day) - _dt.timedelta(days=settings(cfg)["backfill_days"])).isoformat()
    ts = tracking_start(cfg)
    return max(back, ts) if ts else back


def coverage(cfg, day=None):
    """{"start", "tracking_start", "imported", "label"}: records say "since tracking began" only when the window
    really starts there; otherwise "within imported history since …". Never "all-time"."""
    start, ts = window_start(cfg, day), tracking_start(cfg)
    imported = not ts or start != ts
    label = ("within imported history since %s" % start) if imported else ("since tracking began %s" % start)
    return {"start": start, "tracking_start": ts, "imported": imported, "label": label}


# --------------------------------------------------------------------------- the ownership policy

def host_instance(info):
    """(provider, instance) of a remote described by hosts.detect / repo.json; (None, None) for unknown hosts."""
    info = info or {}
    h = info.get("host")
    if h == "github":
        return "github", "github.com"
    if h == "azure" and info.get("org"):
        return "azure", ("dev.azure.com/" + str(info["org"])).lower()
    if h == "gitlab":
        return "gitlab", str(info.get("instance") or "gitlab.com").lower()
    if h == "bitbucket":
        return "bitbucket", "bitbucket.org"
    return None, None


class Policy:
    """The one place that decides whether a commit, PR, Jira person or ticket is yours. Every surface (tick, dashboard,
    MCP, CLI, alerts, exports) asks it; none filters on its own."""

    def __init__(self, cfg, prof=None):
        self.cfg = cfg or {}
        self.p = prof or load(self.cfg)
        self.emails = set(self.p["aliases"])
        self._bots = []
        for rx in self.p["settings"]["bot_patterns"]:
            try:
                self._bots.append(re.compile(rx, re.I))
            except (re.error, TypeError):
                continue
        self._repos = {common.norm_path(r) for r in self.p["repos"]}
        self._projects = set(self.p["projects"])
        self._jira_ids = {str(a.get("id") or "").strip() for a in self.p.get("jira_accounts") or [] if a.get("id")}

    # ---- people
    def is_bot(self, value):
        v = str(value or "").strip()
        return bool(v) and any(rx.search(v) for rx in self._bots)

    def email_mine(self, email):
        e = norm_email(email)
        return bool(e) and e in self.emails and not self.is_bot(e)

    def commit_mine(self, email):
        """True / False, or None when no alias is confirmed (unknown: never counted as yours)."""
        if not self.emails:
            return None
        return self.email_mine(email)

    def commit_role(self, author_email, coauthor_emails=()):
        """"author" when you wrote it, "coauthor" when you are only an exact Co-authored-by, else None."""
        if self.email_mine(author_email):
            return "author"
        if any(self.email_mine(e) for e in coauthor_emails or ()):
            return "coauthor"
        return None

    def jira_account(self):
        return self.p["jira"]

    def person_mine(self, person):
        """A Jira person (assignee, comment or worklog author): True / False, None when it can't be compared. The
        confirmed account id decides when both sides have one; else an exact confirmed email."""
        if not isinstance(person, dict):
            return None
        aid = str(person.get("account_id") or person.get("accountId") or "").strip()
        if self._jira_ids and aid:
            return aid in self._jira_ids
        em = norm_email(person.get("email") or person.get("emailAddress"))
        if em and self.emails:
            return em in self.emails and not self.is_bot(em)
        return None

    def assignee_state(self, assignee, present=True):
        """you | other | unassigned, or None when the field wasn't read or can't be compared with you."""
        if not present:
            return None
        if not assignee:
            return "unassigned"
        m = self.person_mine(assignee)
        return None if m is None else ("you" if m else "other")

    def accounts(self, provider, instance=None):
        """Every confirmed account of yours on a provider instance: one person may have several on the same host (a
        work and an older account). Jira: the confirmed Jira accounts (or the legacy id)."""
        prov, inst = str(provider or "").lower(), str(instance or "").lower().rstrip("/")
        if prov == "jira":
            return list(self.p.get("jira_accounts") or ([self.p["jira"]] if self.p["jira"] else []))
        return [a for a in self.p["accounts"] if a["provider"] == prov and a["instance"] and a["instance"] == inst]

    def account(self, provider, instance=None):
        """The first confirmed account on a provider instance (see accounts() for all of them), or None."""
        got = self.accounts(provider, instance)
        return got[0] if got else None

    @staticmethod
    def _pr_match(acct, aid, nid, login):
        if acct["id"] and aid:
            return acct["id"] == aid
        if acct.get("node_id") and nid:
            return acct["node_id"] == nid
        if acct["login"] and login:
            return acct["login"].lower() == login.lower()
        return None

    def pr_mine(self, author, provider, instance):
        """True / False, None when no account is confirmed for that instance or the source gave no author. Every
        confirmed account on the instance is compared: stable ids decide when both sides have the same kind (REST id,
        or GitHub's GraphQL node id); else the exact login. True only on a positive match."""
        accts = self.accounts(provider, instance)
        if not accts or not isinstance(author, dict):
            return None
        aid, nid = str(author.get("id") or "").strip(), str(author.get("node_id") or "").strip()
        login = str(author.get("login") or author.get("username") or author.get("uniqueName") or "").strip()
        if not (aid or nid or login):
            return None
        if author.get("is_bot") or self.is_bot(login):
            return False
        verdicts = [self._pr_match(a, aid, nid, login) for a in accts]
        if True in verdicts:
            return True
        return None if None in verdicts else False

    # ---- tickets
    def jira_ids(self):
        return set(self._jira_ids)

    def jira_binding(self):
        """The confirmed Jira account set as one comparable string (one id stays exactly that id, so records written
        before several accounts were possible still match). Assignment evidence is stored with it."""
        return ",".join(sorted(self._jira_ids))

    def jira_id(self):
        return self.jira_binding()

    def bound(self, account, state):
        """Does an assignee state recorded against `account` (a binding string) still say something about you? "you"
        holds while every account it was compared with is still yours; "unassigned" holds whatever was compared;
        "other" only against exactly the accounts confirmed now (a newly added account could be the assignee)."""
        ids = {x for x in str(account or "").split(",") if x}
        if not ids or not self._jira_ids:
            return False
        if state == "you":
            return ids <= self._jira_ids
        if state == "unassigned":
            return True
        return ids == self._jira_ids

    def ownership_of(self, tk):
        """(state, history) of a ticket's recorded Jira assignee, as compared with the Jira accounts confirmed now. A
        state or history entry recorded against other accounts (before an account was changed or removed) says
        nothing about you, so it is left out: the ticket is unknown until its card is read again."""
        own = (tk or {}).get("ownership") if isinstance((tk or {}).get("ownership"), dict) else {}
        if not self._jira_ids:
            return None, []
        hist = [h for h in own.get("history") or [] if isinstance(h, dict) and self.bound(h.get("account"), h.get("state"))]
        state = own.get("state") if self.bound(own.get("account"), own.get("state")) else None
        return state, hist

    def release_assignee(self, rel):
        """Release tracking's cached assignee answer, only when it was computed against the Jira accounts confirmed
        now (assignee_binding); an unbound or other-account answer is unknown (None) until Jira is read again."""
        rel = rel if isinstance(rel, dict) else {}
        am = rel.get("assignee_me")
        if am not in (True, False) or not self._jira_ids:
            return None
        return am if self.bound(rel.get("assignee_binding"), "you" if am else "other") else None

    def ticket_state(self, key, tk):
        """(state, reason). assigned: Jira assigns it to your confirmed account now (in a Jira project of your scope);
        included: you chose it (your reason); reassigned: it was yours, now someone else's (your own activity on it is
        kept); other: assigned to someone else; unassigned; outside: assigned to you in a project outside your scope;
        unknown: no confirmed Jira account or no assignee read yet."""
        tk = tk or {}
        state, hist = self.ownership_of(tk)
        was_you = any(h.get("state") == "you" for h in hist)
        if state is None:  # no card with an assignee yet: release tracking's view of the same field, if still bound
            am = self.release_assignee(tk.get("release"))
            state = {True: "you", False: "other"}.get(am)
        k = str(key or tk.get("id") or "").upper()
        inc = self.p["include"].get(k)
        if state == "you" and not inc and not self.project_in_scope(k):
            return "outside", "assigned to you in Jira, but %s isn't one of your profile's Jira projects" % k.split("-")[0]
        if state == "you":
            return "assigned", "assigned to you in Jira"
        if inc:
            return "included", "included by you: %s" % inc
        if state == "other":
            if was_you:
                return "reassigned", "reassigned in Jira; your earlier work on it is kept"
            return "other", "assigned to someone else in Jira"
        if state == "unassigned":
            return "unassigned", "unassigned in Jira" + ("; it was assigned to you before" if was_you else "")
        if not self.p["jira"]:
            return "unknown", "no Jira account is confirmed in your profile"
        return "unknown", "no Jira assignee recorded yet"

    def ticket_current(self, key, tk):
        return self.ticket_state(key, tk)[0] in ("assigned", "included")

    def ticket_ownership(self, key, tk):
        st, why = self.ticket_state(key, tk)
        own = (tk or {}).get("ownership") if isinstance((tk or {}).get("ownership"), dict) else {}
        _, hist = self.ownership_of(tk)
        return {"state": st, "reason": why, "current": st in ("assigned", "included"),
                "checked_at": (own.get("checked_at") or "") if self.bound(own.get("account"), own.get("state")) else "",
                "history": [{"at": h.get("at"), "state": h.get("state")} for h in hist][-10:]}

    # ---- the personal projection: what of a ticket record may appear on your surfaces
    def own_evidence(self, tk):
        """Why a ticket record carries your own work, independent of who Jira assigns it to now: time from your Claude
        Code sessions or your own logged time, findings / queries / requests extracted from your sessions and notes,
        commits whose recorded author is one of your confirmed emails, your own commits on a ticket branch, or a
        recorded Jira assignment to you. -> [reasons] (empty: nothing of yours is recorded)."""
        tk = tk or {}
        out = []
        if any(int((v or {}).get("minutes") or 0) > 0 for v in (tk.get("timeline") or {}).values() if isinstance(v, dict)):
            out.append("time from your own sessions")
        if tk.get("items") or tk.get("queries") or tk.get("api_calls") or ((tk.get("replay") or {}).get("final")):
            out.append("findings from your own sessions or notes")
        if any(isinstance(c, dict) and self.email_mine(c.get("ae")) for c in (tk.get("commits") or {}).values()):
            out.append("your commits")
        repos = ((tk.get("git") or {}).get("repos")) if isinstance(tk.get("git"), dict) else None
        for x in (repos.values() if isinstance(repos, dict) else []):
            branches = (x or {}).get("branches") if isinstance(x, dict) else None
            # a branch record's `own` is its list of commits; the delivery view keeps counts (`own`, `mine`)
            def mine_on(b):
                if isinstance(b.get("own"), list):
                    return any(isinstance(c, dict) and c.get("mine") is True for c in b["own"])
                return isinstance(b.get("mine"), int) and not isinstance(b.get("mine"), bool) and b["mine"] > 0
            bl = [b for b in (branches if isinstance(branches, list) else []) if isinstance(b, dict)]
            if any(mine_on(b) for b in bl):
                out.append("your commits on a ticket branch")
                break
            if any(b.get("local") and (b.get("link") if isinstance(b.get("link"), str) else (b.get("link") or {}).get("how"))
                   in ("branch name", "session", "manual") for b in bl):
                out.append("a branch for it in your own clone")
                break
        _, hist = self.ownership_of(tk)
        if any(h.get("state") == "you" for h in hist):
            out.append("it was assigned to you")
        if not out and (tk.get("first") or tk.get("last")):
            # worked on: first / last come from your own sessions, notes and confirmed-email commits; a record whose
            # commits are all someone else's (or couldn't be verified) doesn't count through them
            recs = [c for part in ("commits", "commits_excluded") for c in ((tk.get(part) or {}).values()
                                                                         if isinstance(tk.get(part), dict) else [])]
            if not any(isinstance(c, dict) and not self.email_mine(c.get("ae")) for c in recs):
                out.append("you worked on it")
        return out

    def ticket_scope(self, key, tk):
        """Where a ticket stands in your personal projection. -> {visible, basis, current, reason, own}
        basis: current (assigned to you / included by you) | history (your own earlier or collaborative work: only
        that is yours) | outside (nothing of yours is recorded: another person's work, never shown as yours).
        A parent kept as context for one of your tickets is decided by the caller (it needs every ticket)."""
        st, why = self.ticket_state(key, tk)
        own = self.own_evidence(tk)
        if st in ("assigned", "included"):
            return {"visible": True, "basis": "current", "current": True, "reason": why, "own": own, "state": st}
        if own:
            lead = {"reassigned": "reassigned in Jira", "other": "assigned to someone else in Jira",
                    "outside": why, "unassigned": "unassigned in Jira"}.get(st, "no current Jira assignment to you")
            return {"visible": True, "basis": "history", "current": False, "state": st,
                    "reason": "%s; your own work on it is kept: %s" % (lead, ", ".join(own)), "own": own}
        return {"visible": False, "basis": "outside", "current": False, "state": st, "own": [],
                "reason": "none of your own work is recorded on it (%s)" % why}

    # ---- scope
    def repo_in_scope(self, path):
        return common.norm_path(path) in self._repos

    def project_in_scope(self, key):
        if not self._projects:
            return True
        return str(key or "").split("-")[0].upper() in self._projects


def projection(pol, records):
    """{key: ticket_scope} for every ticket record ({key: record}), plus parents kept as context: a ticket with nothing
    of yours on it is still shown, labelled "context", when it is the Jira parent (an Epic, say) of one of your visible
    tickets. Context is never counted as your work."""
    out = {k: pol.ticket_scope(k, tk) for k, tk in records.items()}
    parents = {}
    for k, tk in records.items():
        if out[k]["visible"]:
            p = str(((tk or {}).get("jira") or {}).get("parent") or "").strip().upper()
            if p and p != k:
                parents.setdefault(p, k)
    for p, child in parents.items():
        s = out.get(p)
        if s is not None and not s["visible"]:
            out[p] = dict(s, visible=True, basis="context",
                          reason="the parent of %s, kept as context; none of your own work is recorded on it" % child)
    return out


def ticket_scope_of(key, cfg=None, tk=None):
    """The personal projection of one ticket, for the readers that get a key directly (MCP, CLI, export, sharing):
    the same answer the ticket list gives (see dashboard.api.scope_index)."""
    from .dashboard import api
    return api.scope_of(str(key or "").upper(), cfg)


def record_ownership(tk, state, at, account):
    """Record the Jira assignee state seen on a card (you | other | unassigned), compared with `account` (your
    confirmed Jira account id). History grows only on a change, never names the other person, and keeps entries made
    against an earlier account (labelled with it; Policy ignores them). Returns True when the state changed."""
    if state not in ("you", "other", "unassigned") or not account:
        return False
    own = tk.get("ownership") if isinstance(tk.get("ownership"), dict) else {}
    if own.get("account") != account:  # recorded against another account: start over for this one
        own.pop("state", None)
        own["account"] = account
    own["checked_at"] = at
    changed = own.get("state") != state
    if changed:
        own["state"] = state
        own["history"] = ([h for h in own.get("history") or [] if isinstance(h, dict)] +
                          [{"at": at, "state": state, "account": account}])[-HISTORY_MAX:]
    tk["ownership"] = own
    return changed


def owner_from_card(pol, tk, rec):
    """Ticket ownership from a Jira card record (data/jira/<KEY>.json), when the card job asked for the assignee.
    Used by the card job and, after the Jira account changes, by reconcile on the cached card. -> changed."""
    from . import release
    asked = {f.strip().lower() for f in str((rec or {}).get("fields") or "").split(",") if f.strip()}
    raw = (rec or {}).get("raw") if isinstance((rec or {}).get("raw"), dict) else {}
    get, has = release._getter(raw)
    if "assignee" not in asked and not has("assignee"):
        return False
    return record_ownership(tk, pol.assignee_state(get("assignee")), rec.get("fetched_at") or common.now_iso(),
                            pol.jira_id())


# --------------------------------------------------------------------------- status

def repo_hosts(cfg, repos):
    """{(provider, instance): [repo names]} from the cached remote info branch tracking keeps (no git call)."""
    from . import gitcmd
    out = {}
    for r in repos:
        meta = common.read_json(common.hp("data", "git", gitcmd.repo_key(r), "repo.json"), {}) or {}
        prov, inst = host_instance(meta)
        if prov:
            out.setdefault((prov, inst), []).append(os.path.basename(str(r).rstrip("/\\")))
    return out


CONN_STALE_S = 3 * 3600


def _conn(rows, now):
    """One source's connection from its reads [(status, at, why)]: failing | partial | stale | ok | never. Being
    configured says nothing here: only a read that happened, and how it went."""
    if not rows:
        return {"state": "never", "why": "Not read yet.", "at": ""}
    bad = [r for r in rows if r[0] == "error"]
    if bad:
        return {"state": "failing", "why": "The last read failed: %s" % (bad[0][2] or "error")[:200], "at": bad[0][1] or ""}
    ats = [common.parse_ts(r[1]) for r in rows if r[1]]
    oldest = min([a for a in ats if a] or [None]) if any(ats) else None
    if oldest is None:
        return {"state": "never", "why": "Not read yet.", "at": ""}
    at = oldest.isoformat(timespec="seconds")
    if (now - oldest).total_seconds() > CONN_STALE_S:
        return {"state": "stale", "why": "Last read %s." % at[:16].replace("T", " "), "at": at}
    part = [r for r in rows if r[0] == "partial"]
    if part:
        return {"state": "partial", "why": (part[0][2] or "partly collected")[:200], "at": at}
    return {"state": "ok", "why": "Last read %s." % at[:16].replace("T", " "), "at": at}


def connections(cfg, p=None, state=None, now=None):
    """How each source's last refresh went, apart from whether an identity is confirmed: git (the commit scans of the
    in-scope repositories), each Git host instance (their PR reads) and Jira (the MCP's last answer). The same answer
    Profile, My progress and Health show."""
    from . import activity, mcpjobs
    p = p or load(cfg)
    now = now or common.now()
    commits, hosts_ = [], {}
    for r in p["repos"]:
        led = activity.load(r) or {}
        c, pr = led.get("commits") or {}, led.get("prs") or {}
        if c.get("status") and c["status"] not in ("unconfigured",):
            commits.append((c["status"], c.get("scanned_at") or "", c.get("error") or c.get("why") or ""))
        rp = (led.get("repo") or {})
        if rp.get("provider") and pr.get("status") not in (None, "unconfigured", "unavailable"):
            hosts_.setdefault((rp["provider"], rp.get("instance") or ""), []).append(
                (pr["status"], pr.get("fetched_at") or "", pr.get("error") or pr.get("why") or ""))
    out = {"git": _conn(commits, now), "hosts": {k: _conn(v, now) for k, v in hosts_.items()}}
    h = common.read_json(common.wl("health.json"), {}) or {}
    srv = mcpjobs.server_name(cfg, "jira")
    off = ((h.get("mcp") or {}).get("offline") or {}).get(srv)
    ok_at = str((h.get("mcp") or {}).get("jira_auth_ok") or "")
    if not mcpjobs.enabled(cfg, "jira"):
        out["jira"] = {"state": "off", "why": "The Jira MCP is off.", "at": ""}
    elif off and (not ok_at or str(off) >= ok_at):
        out["jira"] = {"state": "failing", "why": "Jira was unreachable or signed out at %s." % str(off)[:16].replace("T", " "),
                       "at": str(off)}
    elif ok_at:
        out["jira"] = _conn([("ok", ok_at, "")], now) if common.parse_ts(ok_at) else {"state": "never", "why": "", "at": ""}
        if out["jira"]["state"] == "stale":
            out["jira"]["why"] = "Last answered %s." % ok_at[:16].replace("T", " ")
    else:
        out["jira"] = {"state": "never", "why": "No Jira read has succeeded yet.", "at": ""}
    return out


def status(cfg, state=None):
    """Per provider: {"state": ok | unconfigured | partial | error, "why", ...}. One provider's gap never hides the
    others; nothing here is a reason to collect anyone else's work."""
    from . import hosts, mcpjobs
    cfg = cfg or {}
    p = load(cfg)
    pol = Policy(cfg, p)
    st = state if state is not None else (common.read_json(common.wl("state.json"), {}) or {})
    names = [os.path.basename(str(r).rstrip("/\\")) for r in p["repos"]]
    git = {"aliases": len(p["aliases"]), "repos": names, "last_scan": st.get("last_commit_scan") or ""}
    if not p["aliases"]:
        git.update(state="unconfigured", why="No commit email is confirmed, so no commit is counted as yours.")
    elif not p["repos"]:
        git.update(state="unconfigured", why="No repository is in scope.")
    elif p["repos_missing"]:
        git.update(state="partial", why="Selected but not watched: %s." % ", ".join(
            os.path.basename(str(r).rstrip("/\\")) for r in p["repos_missing"]))
    else:
        git.update(state="ok", why="Commits authored by %d confirmed email%s in %d repositor%s." % (
            len(p["aliases"]), "" if len(p["aliases"]) == 1 else "s", len(names), "y" if len(names) == 1 else "ies"))
    jira = {"enabled": bool(mcpjobs.enabled(cfg, "jira")), "instance": (p["jira"] or {}).get("instance", "")}
    if not p["jira"]:
        jira.update(state="unconfigured", why="No Jira account is confirmed, so ticket ownership is unknown.")
    elif not jira["enabled"]:
        jira.update(state="partial", why="Your Jira account is confirmed, but the Jira MCP is off: assignees aren't read.")
    else:
        jira.update(state="ok", why="Tickets assigned to your confirmed Jira account%s." % (
            " (from the older mcp.jira.account_id setting)" if p["jira"].get("legacy") else ""))
    out_hosts = []
    for (prov, inst), repos in sorted(repo_hosts(cfg, p["repos"]).items()):
        cli = HOST_CLI.get(prov)
        h = {"provider": prov, "instance": inst, "repos": sorted(repos), "cli": bool(cli and hosts.cli_path(cfg, cli))}
        if pol.account(prov, inst):
            h.update(state="ok", why="PRs you authored on %s." % inst)
        else:
            h.update(state="unconfigured", why="No confirmed %s account for %s, so PRs aren't counted for %s." % (
                prov, inst, ", ".join(sorted(repos))))
        out_hosts.append(h)
    conn = connections(cfg, p, st)
    git["connection"] = conn["git"]
    jira["connection"] = conn["jira"]
    for h in out_hosts:
        h["connection"] = conn["hosts"].get((h["provider"], h["instance"])) or \
            {"state": "never", "why": "No PR read yet for these repositories.", "at": ""}
    tz = {"name": p["settings"]["timezone"] or "", "state": "ok", "why": "This machine's time zone."}
    if tz["name"]:
        try:
            zone(tz["name"])
            tz["why"] = "Days follow %s." % tz["name"]
        except ValueError as e:
            tz.update(state="error", why=str(e)[0].upper() + str(e)[1:] + "; this machine's zone is used meanwhile.")
    return {"git": git, "jira": jira, "hosts": out_hosts, "timezone": tz,
            "scope": {"repos": names, "missing": p["repos_missing"], "selected": p["repos_selected"],
                      "projects": p["projects"], "included_tickets": p["include"]},
            "signature": signature(cfg), "coverage": coverage(cfg)}


# --------------------------------------------------------------------------- candidates

def candidates_path():
    return common.wl("cache", "profile-candidates.json")


def _cid(c):
    basis = "|".join(str(c.get(k) or "").lower() for k in ("kind", "provider", "instance", "value", "id", "login"))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def read_candidates(cfg=None):
    """The cached suggestions, each marked confirmed or not against the config as it is now."""
    cfg = cfg if cfg is not None else common.load_config()
    pol = Policy(cfg)
    data = common.read_json(candidates_path(), {}) or {}
    out = []
    for c in data.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        c = dict(c)
        if c.get("kind") == "email":
            c["confirmed"] = norm_email(c.get("value")) in pol.emails
        else:
            c["confirmed"] = any((c.get("id") and a.get("id") == c.get("id")) or
                                 (not c.get("id") and c.get("login") and a.get("login", "").lower() == str(c.get("login")).lower())
                                 for a in pol.accounts(c.get("provider"), c.get("instance")))
        c["bot"] = bool(c.get("bot")) or pol.is_bot(c.get("value") or c.get("login"))
        out.append(c)
    return {"at": data.get("at") or "", "candidates": out, "errors": data.get("errors") or {}}


def _merge_candidates(found, sources, errors, now_iso):
    """Replace the candidates of the sources just looked at; keep the rest."""
    old = common.read_json(candidates_path(), {}) or {}
    keep = [c for c in old.get("candidates") or [] if isinstance(c, dict) and c.get("origin") not in sources]
    by = {}
    for c in keep + found:
        c["cid"] = _cid(c)
        by[c["cid"]] = c
    errs = {k: v for k, v in (old.get("errors") or {}).items() if k not in sources}
    errs.update(errors)
    rec = {"at": now_iso, "candidates": sorted(by.values(), key=lambda c: (c.get("kind", ""), c["cid"])), "errors": errs}
    common.write_json(candidates_path(), rec)
    return rec


def local_candidates(cfg):
    """`git config --get user.email` in each watched repo (read-only, local). A repo's configured email is only a
    suggestion: it becomes yours when you confirm it."""
    from . import gitro
    pol = Policy(cfg)
    found, errors = {}, {}
    now = common.now_iso()
    for r in cfg.get("watched_repos") or []:
        if not isinstance(r, str) or not os.path.isdir(r):
            continue
        try:
            rc, so, se = gitro.run(cfg, r, ["config", "--get", "user.email"], timeout=15)
        except Exception as e:  # noqa: BLE001 - a blocked or failed read is one repo's gap
            errors["git:" + os.path.basename(r.rstrip("/\\"))] = str(e)[:200]
            continue
        e = norm_email(so.strip()) if rc == 0 else ""
        if not e:
            continue
        name = os.path.basename(r.rstrip("/\\"))
        c = found.setdefault(e, {"kind": "email", "value": e, "origin": "git", "seen_at": now, "repos": [],
                                 "bot": pol.is_bot(e)})
        c["repos"].append(name)
    for c in found.values():
        c["source"] = "git config user.email in %s" % ", ".join(sorted(c["repos"]))
    return list(found.values()), errors


def _host_self(cfg, prov, insts, run):
    """One read of the account a host CLI is signed in as -> candidates. Commands: gh api user, glab api user,
    az account show --output json (all on the command guard's read-only allowlist)."""
    from . import hosts
    cli = HOST_CLI[prov]
    exe = hosts.cli_path(cfg, cli)
    if not exe:
        return [], None
    argv = {"github": [exe, "api", "user"], "gitlab": [exe, "api", "user"],
            "azure": [exe, "account", "show", "--output", "json"]}[prov]
    rc, so, se = run(argv, timeout=30)
    if rc != 0:
        return [], "%s: %s" % (cli, (se or so or "failed").strip()[:200])
    try:
        d = json.loads(so or "{}")
    except ValueError:
        return [], "%s: unexpected output" % cli
    now, out = common.now_iso(), []
    for inst in sorted(insts):
        if prov == "github":
            c = {"id": str(d.get("id") or ""), "node_id": str(d.get("node_id") or ""), "login": str(d.get("login") or ""),
                 "handle": str(d.get("name") or ""),
                 "bot": str(d.get("type") or "").lower() == "bot" or str(d.get("login") or "").endswith("[bot]")}
            src = "gh api user (the account gh is signed in as)"
        elif prov == "gitlab":
            c = {"id": str(d.get("id") or ""), "login": str(d.get("username") or ""), "handle": str(d.get("name") or ""),
                 "bot": bool(d.get("bot"))}
            src = "glab api user (the account glab is signed in as)"
        else:
            u = d.get("user") or {}
            c = {"id": "", "login": str(u.get("name") or ""), "handle": "",
                 "bot": str(u.get("type") or "").lower() not in ("", "user")}
            src = "az account show (the account az is signed in as)"
        if not (c["id"] or c["login"]):
            continue
        c.update(kind="account", provider=prov, instance=inst, origin="host:" + prov, seen_at=now, source=src)
        out.append(c)
    return out, None


def discover(cfg, hosts_too=False, jira=False, health=None, run=None):
    """Refresh the suggestions: local git config always; the host CLIs' signed-in accounts with hosts_too (one GET
    each); the Jira account for profile.email with jira (one manual, budgeted MCP job). Never confirms anything."""
    from . import hosts
    found, errors = local_candidates(cfg)
    sources = {"git"}
    if hosts_too:
        p = load(cfg)
        by_prov = {}
        for (prov, inst) in repo_hosts(cfg, p["repos"]):
            if prov in HOST_CLI:
                by_prov.setdefault(prov, set()).add(inst)
        for prov, insts in sorted(by_prov.items()):
            sources.add("host:" + prov)
            try:
                got, err = _host_self(cfg, prov, insts, run or hosts._run)
            except Exception as e:  # noqa: BLE001 - one host's failure leaves the others
                got, err = [], "%s: %s" % (HOST_CLI[prov], str(e)[:200])
            found += got
            if err:
                errors["host:" + prov] = err
    if jira:
        sources.add("jira")
        try:
            found += jira_candidates(cfg, health if health is not None else {})
        except Exception as e:  # noqa: BLE001
            errors["jira"] = str(e)[:200]
    return _merge_candidates(found, sources, errors, common.now_iso())


def jira_candidates(cfg, health):
    """One read-only MCP job: jira_get_user_profile for profile.email -> the account Jira has for that address."""
    from . import mcpjobs
    from urllib.parse import urlparse
    email = load(cfg)["email"]
    if not email:
        raise ValueError("set your primary work email first: the Jira lookup uses it")
    if not mcpjobs.enabled(cfg, "jira"):
        raise ValueError("the Jira MCP is off")
    raw = mcpjobs.jira_user(email, cfg, health)
    u = raw if isinstance(raw, dict) else {}
    u = u.get("user") if isinstance(u.get("user"), dict) else u
    aid = str(u.get("account_id") or u.get("accountId") or "").strip()
    if not aid:
        return []
    inst = urlparse(str(u.get("self") or "")).hostname or ""
    return [{"kind": "account", "provider": "jira", "instance": inst.lower(), "id": aid, "login": "",
             "handle": str(u.get("display_name") or u.get("displayName") or ""), "origin": "jira",
             "seen_at": common.now_iso(), "bot": str(u.get("accountType") or "").lower() == "app",
             "source": "Jira user lookup for your primary email"}]


def confirm(cid, cfg=None, source="cli"):
    """Confirm one suggestion: the same validation and write path as the Settings page (configstore: lock, backup,
    history). Returns the configstore result; raises ValueError with the reason when it can't be confirmed."""
    from . import configstore, settings as S
    cfg = cfg if cfg is not None else common.load_config()
    c = next((x for x in read_candidates(cfg)["candidates"] if x.get("cid") == cid), None)
    if c is None:
        raise ValueError("no suggestion %s (list them with: profile candidates)" % cid)
    if c.get("bot"):
        raise ValueError("a bot or service identity can't be one of yours")
    if c.get("confirmed"):
        raise ValueError("already confirmed")
    if c["kind"] == "email":
        cur = [e for e in cfg.get("git_author_emails") or [] if e]
        changes = {"git_author_emails": cur + [c["value"]]}
    else:
        # added next to any other account on the same instance (one person may have several); only the same
        # account confirmed again is replaced
        cur = [a for a in settings(cfg)["accounts"] if isinstance(a, dict) and account_key(a) != account_key(c)]
        acct = {"provider": c["provider"], "instance": c["instance"], "id": c.get("id") or "",
                "login": c.get("login") or "", "source": c.get("source") or "", "confirmed_at": common.now_iso()}
        if c.get("node_id"):
            acct["node_id"] = c["node_id"]
        changes = {"profile.accounts": cur + [acct]}
    clean, errors = S.validate(changes)
    if errors:
        raise ValueError("; ".join("%s: %s" % kv for kv in errors.items()))
    return configstore.update(clean, source, note="confirmed: %s" % (c.get("source") or c["kind"]))


# --------------------------------------------------------------------------- context for Claude

def context_text(cfg):
    """The profile in at most CONTEXT_MAX characters: who, focus, scope and the ownership rule. No email, no id."""
    p = load(cfg)
    s = p["settings"]
    if not (p["display_name"] or p["aliases"] or p["accounts"] or p["jira"] or s["focus"]):
        return ""
    parts =["Developer profile: %s." % (p["display_name"] or "(no name set)")]
    if s["focus"].strip():
        parts.append("Focus: %s." % " ".join(s["focus"].split())[:200])
    if s["role"].strip():
        parts.append("Role: %s." % s["role"].strip()[:60])
    names = [os.path.basename(str(r).rstrip("/\\")) for r in p["repos"]]
    scope = []
    if names:
        scope.append("repos " + ", ".join(names[:6]) + (" +%d" % (len(names) - 6) if len(names) > 6 else ""))
    if p["projects"]:
        scope.append("Jira " + ", ".join(p["projects"][:6]))
    if scope:
        parts.append("Scope: %s." % "; ".join(scope))
    rule = []
    if p["jira"]:
        rule.append("Jira tickets assigned to your confirmed account" +
                    (" (+%d you included)" % len(p["include"]) if p["include"] else ""))
    if p["aliases"]:
        rule.append("commits authored by your %d confirmed email%s" % (len(p["aliases"]), "" if len(p["aliases"]) == 1 else "s"))
    hosts_ok = sorted({a["instance"] for a in p["accounts"] if a["provider"] != "jira"})
    if hosts_ok:
        rule.append("PRs you authored on " + ", ".join(hosts_ok))
    if rule:
        parts.append("Yours: " + "; ".join(rule) + ".")
    missing = [w for w, ok in (("Jira account", p["jira"]), ("commit email", p["aliases"])) if not ok]
    if missing:
        parts.append("Not confirmed: %s (/cg:setup or the Profile page)." % ", ".join(missing))
    return common.truncate(" ".join(parts), CONTEXT_MAX)


def context_path():
    return common.wl("cache", "profile-context.md")


# --------------------------------------------------------------------------- reconcile

def _sent(day):
    from . import jiraworklog
    return jiraworklog.load_sheet(day).get("status") in ("sent", "nudged")


def _verify_authors(cfg, by_repo):
    """{sha: author email} read from git for records that don't carry one: one batched, read-only `log --no-walk`
    per repo (missing objects ignored)."""
    from . import gitro
    repos = {os.path.basename(str(r).rstrip("/\\")): r for r in cfg.get("watched_repos") or [] if r}
    out = {}
    for name, shas in by_repo.items():
        path = repos.get(name)
        if not path or not os.path.isdir(path):
            continue
        shas = sorted(shas)
        for i in range(0, len(shas), 200):
            try:
                rc, so, _ = gitro.run(cfg, path, ["log", "--no-walk=unsorted", "--ignore-missing",
                                                  "--format=%H%x1f%ae"] + shas[i:i + 200], timeout=60)
            except Exception:  # noqa: BLE001 - unverifiable records stay where they are
                continue
            if rc != 0:
                continue
            for line in so.splitlines():
                bits = line.split("\x1f")
                if len(bits) == 2 and bits[0].strip():
                    out[bits[0].strip()] = bits[1].strip().lower()
    return out


def reconcile(store, cfg, state, now=None):
    """After an identity or scope change (or the first run with a profile): commit records no longer authored by a
    confirmed email move to a `commits_excluded` sidecar (kept, labelled), records that are yours again move back,
    and the commit scan restarts at the history window so newly confirmed emails are back-filled. Ticket records
    always; day records only for today and days whose evening worklog wasn't sent (a sent message is history).
    Files are backed up before the first change. Idempotent. -> summary dict, or None when nothing was due."""
    sig = signature(cfg)
    if state.get("profile_sig") == sig and state.get("profile_policy") == POLICY_V:
        return None
    now = now or common.now()
    at = now.isoformat(timespec="seconds")
    pol = Policy(cfg)
    day_now = common.workday(now, cfg)
    tickets = [t for t in store.all_ticket_ids()]
    days = [d for d in store.all_days() if d == day_now or not _sent(d)]
    # 1. what needs an author from git: an archived record keeps no author email (saved records are redacted, only
    # your own addresses survive), so it is always re-read; a kept record carries `ae` from the scan, older ones don't
    need = {}

    def want(rec, archived=False):
        if (archived or not rec.get("ae")) and rec.get("sha") and rec.get("repo_name"):
            need.setdefault(rec["repo_name"], set()).add(rec["sha"])
    for t in tickets:
        tk = store.ticket(t)
        for rec in (tk.get("commits") or {}).values():
            want(rec)
        for rec in (tk.get("commits_excluded") or {}).values():
            want(rec, True)
    for d in days:
        dd = store.day(d)
        for rec in dd.get("commits") or []:
            want(rec)
        for rec in dd.get("commits_excluded") or []:
            want(rec, True)
    authors = _verify_authors(cfg, need) if need else {}

    def author(rec):
        if rec.get("excluded"):
            return authors.get(rec.get("sha") or "")
        return norm_email(rec.get("ae")) or authors.get(rec.get("sha") or "")

    def scope_ok(rec):
        name = rec.get("repo_name")
        return not pol.p["repos_selected"] or any(os.path.basename(str(r).rstrip("/\\")) == name for r in pol.p["repos"])

    def verdict(rec):
        """True keep, False archive, None unverifiable: quarantined like an archived record (kept apart, labelled, never
        shown as yours) and moved back once git confirms a confirmed email wrote it."""
        a = author(rec)
        if not a:
            return None
        return pol.email_mine(a) and scope_ok(rec)
    # 2. plan the moves
    t_moves, d_moves, unverified = {}, {}, 0
    for t in tickets:
        tk = store.ticket(t)
        out_ = [s for s, rec in (tk.get("commits") or {}).items() if verdict(rec) is not True]
        back = [s for s, rec in (tk.get("commits_excluded") or {}).items() if verdict(rec) is True]
        unverified += sum(1 for rec in (tk.get("commits") or {}).values() if verdict(rec) is None)
        if out_ or back:
            t_moves[t] = (out_, back)
    for d in days:
        dd = store.day(d)
        out_ = [i for i, rec in enumerate(dd.get("commits") or []) if verdict(rec) is not True]
        back = [i for i, rec in enumerate(dd.get("commits_excluded") or []) if verdict(rec) is True]
        unverified += sum(1 for rec in dd.get("commits") or [] if verdict(rec) is None)
        if out_ or back:
            d_moves[d] = (out_, back)
    # ticket ownership when the Jira account changed: recomputed from each cached card (no Jira call); until a card
    # is read for the new account, a ticket's ownership is unknown (Policy ignores what another account recorded)
    jira_id = pol.jira_id()
    owners = []
    if jira_id and state.get("profile_jira", "") != jira_id:
        for t in tickets:
            rec = common.read_json(common.hp("data", "jira", t + ".json"), None)
            if isinstance(rec, dict) and isinstance(rec.get("raw"), dict):
                owners.append((t, rec))
    backup = None
    if t_moves or d_moves or owners:
        from . import migrate, store as S
        paths = [S.ticket_path(t) for t in sorted(set(t_moves) | {t for t, _ in owners})] + \
            [S.day_path(d) for d in d_moves]
        backup = migrate._backup_tree([p_ for p_ in paths if os.path.exists(p_)], "profile")
    owned = 0
    for t, rec in owners:
        if owner_from_card(pol, store.ticket(t), rec):
            store.touch_ticket(t)
            owned += 1
    # 3. apply
    def excluded(rec):
        a = author(rec)
        why = "its author couldn't be verified from git (quarantined until it can be)" if not a else \
            "its repository is outside your profile's scope" if pol.email_mine(a) else \
            "not authored by a confirmed email in your profile"
        return dict({k: v for k, v in rec.items() if k != "ae"}, excluded={"reason": why, "at": at})

    def included(rec):
        return dict({k: v for k, v in rec.items() if k != "excluded"}, ae=author(rec))
    archived = restored = 0
    for t, (out_, back) in t_moves.items():
        tk = store.ticket(t)
        com, exc = tk.setdefault("commits", {}), tk.setdefault("commits_excluded", {})
        for s in out_:
            exc[s] = excluded(com.pop(s))
            archived += 1
        for s in back:
            com[s] = included(exc.pop(s))
            restored += 1
        if not exc:
            tk.pop("commits_excluded", None)
        store.touch_ticket(t)
    for d, (out_, back) in d_moves.items():
        dd = store.day(d)
        com, exc = list(dd.get("commits") or []), list(dd.get("commits_excluded") or [])
        moved_out = [excluded(com[i]) for i in out_]
        moved_back = [included(exc[i]) for i in back]
        com = [r for i, r in enumerate(com) if i not in set(out_)] + moved_back
        exc = [r for i, r in enumerate(exc) if i not in set(back)] + moved_out
        com.sort(key=lambda r: str(r.get("date") or ""))
        dd["commits"] = com
        if exc:
            dd["commits_excluded"] = exc
        else:
            dd.pop("commits_excluded", None)
        archived += len(moved_out)
        restored += len(moved_back)
        store.touch_day(d)
    # 4. back-fill commits of newly confirmed emails (bounded: the history window, at most 30 days for day records)
    prev = set(state.get("profile_aliases") or [])
    added = [e for e in pol.p["aliases"] if e not in prev]
    if added and state.get("profile_sig"):
        start = max(window_start(cfg, day_now), (_dt.date.fromisoformat(day_now) - _dt.timedelta(days=30)).isoformat())
        cur = common.parse_ts(state.get("last_commit_scan"))
        begin = common.day_bounds(start, cfg)[0]
        if cur is None or begin < cur:
            state["last_commit_scan"] = begin.isoformat(timespec="seconds")
            # forget what was seen since, so the rescan can record the newly yours commits (records dedupe by sha)
            seen = state.get("commits_seen") or {}
            for sha in list(seen):
                d = common.parse_ts(seen[sha])
                if d and d >= begin:
                    seen.pop(sha, None)
    state["profile_aliases"] = pol.p["aliases"]
    state["profile_jira"] = jira_id
    state["profile_sig"] = sig
    state["profile_policy"] = POLICY_V
    res = {"signature": sig, "archived": archived, "restored": restored, "unverified": unverified, "backup": backup,
           "ownership": owned, "at": at}
    state["profile_reconciled"] = res
    if archived or restored:
        common.log("agent", "profile: %d commit record(s) archived, %d restored%s" % (
            archived, restored, " (backup %s)" % backup if backup else ""))
    return res


# --------------------------------------------------------------------------- the background step

def tick(store, cfg, state, now=None):
    """Local suggestions at most daily (git config only: no network), reconcile on a changed profile, and the cached
    context line SessionStart appends (the hook never computes it)."""
    now = now or common.now()
    day = common.workday(now, cfg)
    if state.get("profile_candidates_day") != day:
        try:
            found, errors = local_candidates(cfg)
            _merge_candidates(found, {"git"}, errors, now.isoformat(timespec="seconds"))
        except Exception as e:  # noqa: BLE001 - suggestions are optional
            common.log("agent", "profile candidates failed: %s" % e)
        state["profile_candidates_day"] = day
    res = reconcile(store, cfg, state, now=now)
    text = context_text(cfg)
    p = context_path()
    if text:
        common.write_text(p, text + "\n")
    elif os.path.exists(p):
        common.write_text(p, "")
    return res
