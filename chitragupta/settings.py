"""The settings the dashboard can show and change: one schema, one validator, one write path.

    describe()                 -> {"groups": [{"id","label","items":[{key,label,help,type,…,value,default,editable}]}]}
    validate(changes, cfg)     -> (clean dict, {key: error}) — the same rules the page shows next to each field
    apply(changes, source)     validate, then write through configstore (lock, backup, history); -> configstore result

Everything not in the schema, and every item with `editable: False`, is refused here whatever the request says: the
safety rules, MCP tool lists and their read/write classification, secrets, the data folder, the GitHub repo, paths,
the Slack recipient, push paths and redaction patterns are only ever shown. /cg:setup is how those change.
"""
import datetime as _dt
import fnmatch
import os
import re

from . import common
from . import configstore

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MODELS = ["haiku", "sonnet", "opus"]
MODEL_ID = re.compile(r"^claude-[a-z0-9][a-z0-9.-]{1,60}$")  # an explicit model id pins the version
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PREFIX = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{0,39}$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9._/*-]{0,99}$")  # a branch name or an fnmatch pattern


def _defaults():
    from . import gitcmd, timesheet
    d = common.load_config()
    d["jira_worklog"] = timesheet.settings(d)
    b = gitcmd.settings(d)
    d["branching"] = dict(d.get("branching") or {}, guard_mode=b["guard_mode"], stale_pr_days=b["stale_pr_days"],
                          fetch=b["fetch"])
    from . import release
    d["releases"] = release.settings(d)
    from . import profile
    d["profile"] = dict(profile.settings(d), accounts=(d.get("profile") or {}).get("accounts") or [])
    d["progress"] = profile.progress_settings(d)
    return d


# --------------------------------------------------------------------------- schema
# type: bool | int | number | time | choice | days | prefixes | names | paths | envs_narrow | bases | relations | text
def _i(key, label, help_, type_, group, **kw):
    return dict(key=key, label=label, help=help_, type=type_, group=group, editable=kw.pop("editable", True), **kw)


GROUPS = [("profile", "Developer profile"), ("capture", "Capture"), ("worklog", "Daily worklog & Slack"), ("jira", "Jira"), ("database", "Database MCP"),
          ("branching", "Branches & PRs"), ("releases", "Releases & UAT"), ("artifacts", "Requests & artifacts"), ("push", "GitHub daily push"),
          ("models", "Models & budget"), ("data", "Data"), ("share", "Team sharing"), ("dashboard", "Dashboard"),
          ("safety", "Locked")]

STATIC = [
    _i("profile.display_name", "Display name", "How summaries greet you. Never used to match your work.", "line",
       "profile", maxlen=80, default_value=""),
    _i("profile.email", "Primary work email", "Shown to you only; the Jira lookup uses it. It isn't proof of authorship "
       "by itself: commit emails are confirmed below.", "email", "profile", default_value=""),
    _i("git_author_emails", "Confirmed commit emails", "Commits whose author email is exactly one of these are yours "
       "(one per line). Never inferred: a repo's configured email is only a suggestion until you confirm it.", "emails",
       "profile", affects="Which commits count as yours everywhere: worklog, tickets, progress"),
    _i("profile.accounts", "Confirmed accounts", "Your Jira account id and Git host accounts, each scoped to its "
       "instance. Tokens and passwords never go here.", "accounts", "profile", default_value=[],
       affects="Which tickets and PRs count as yours"),
    _i("profile.repos", "Repositories in scope", "Watched repositories whose work is yours to track. Empty: every "
       "watched repository.", "repo_paths", "profile", default_value=[]),
    _i("profile.jira_projects", "Jira projects in scope", "Empty: the ticket prefixes.", "prefixes", "profile",
       default_value=[]),
    _i("profile.focus", "Current focus", "Projects, responsibilities or what you are driving now. Given to Claude as "
       "planning context.", "longtext", "profile", maxlen=400, default_value=""),
    _i("profile.role", "Role", "Optional planning context, not ownership evidence.", "line", "profile", maxlen=80,
       default_value=""),
    _i("profile.technologies", "Preferred technologies", "Optional, one per line.", "tags", "profile", default_value=[]),
    _i("profile.timezone", "Time zone", "Days and reminders follow it. Empty: this machine's zone. Or a UTC offset such "
       "as +05:30, or a zone name this machine has data for.", "timezone", "profile", default_value=""),
    _i("profile.tracking_start", "Tracking starts", "History before this day isn't collected. Empty: the day "
       "Chitragupta first ran.", "date", "profile", default_value=""),
    _i("profile.backfill_days", "Import history (days)", "At most this many days back, whatever the start.", "int",
       "profile", min=1, max=365, default_value=90),
    _i("profile.include_tickets", "Collaborative tickets", "Tickets assigned to someone else that you count as yours, "
       "each with a reason. Only your own activity on them is counted.", "ticket_reasons", "profile", default_value={}),
    _i("profile.bot_patterns", "Bot and service identities", "Patterns (one per line) that can never be one of your "
       "identities.", "regexes", "profile"),
    _i("profile.celebrations", "Milestone messages", "Short factual notes when you reach a personal record.", "bool",
       "profile", default_value=True),
    _i("profile.animations", "Animations", "Motion on the progress page (reduced motion is always respected).", "bool",
       "profile", default_value=True),
    _i("profile.reflection", "Daily reflection", "A short optional note on what moved forward.", "bool", "profile",
       default_value=True),
    _i("progress.exclude_globs", "Left out of line counts", "Lockfiles, vendored and generated files (glob patterns, "
       "one per line). Shown separately, never hidden.", "globs", "profile"),
    _i("progress.done_statuses", "Jira statuses that mean done", "A move into one of these (any case) counts as a "
       "completed ticket that day.", "names", "profile"),
    _i("ticket_prefixes", "Ticket prefixes", "Jira project keys that count as tickets, for example ABC.", "prefixes", "capture",
       affects="Which branch names and messages are filed under a ticket"),
    _i("capture_scope.include_paths", "Capture only under", "If set, sessions are recorded only under these folders. "
       "Empty means everywhere.", "paths", "capture", affects="What gets recorded"),
    _i("capture_scope.exclude_paths", "Never capture under", "Folders whose sessions are never recorded.", "paths", "capture",
       affects="What gets recorded"),
    _i("day_starts_at", "Day starts at", "Work before this time counts as the previous day.", "time", "capture"),
    _i("jira_worklog.enabled", "Compose the evening worklog", "Turn the 8 PM Jira worklog off entirely.", "bool", "worklog"),
    _i("jira_worklog.slack.enabled", "Send it to me on Slack", "Off keeps the message in the daily file only.", "bool",
       "worklog", needs="slack"),
    _i("jira_worklog.send_at", "Send time", "When the message is composed and sent.", "time", "worklog",
       min="16:00", max="23:30"),
    _i("jira_worklog.workdays", "Workdays", "Normal working days. Working Saturdays go in the calendar.", "days", "worklog"),
    _i("jira_worklog.day_start", "First entry starts at", "Start time of the first Jira entry.", "time", "worklog",
       min="06:00", max="13:00"),
    _i("jira_worklog.lunch.minutes", "Lunch length (minutes)", "The gap left for lunch.", "int", "worklog", min=0, max=120),
    _i("jira_worklog.lunch.preferred", "Preferred lunch start", "Lunch starts here when the day allows.", "time", "worklog",
       min="11:00", max="16:00"),
    _i("jira_worklog.hours_per_day", "Hours per day", "A full day's target; half days are half of it.", "number", "worklog",
       min=1, max=12),
    _i("jira_worklog.turn_tail_minutes", "Time after a turn (minutes)", "Counted after the last event of each turn, for "
       "reading the answer.", "int", "worklog", min=0, max=30, default_value=5),
    _i("jira_worklog.idle_minutes", "Idle after (minutes)", "A longer gap inside a turn is not counted.", "int",
       "worklog", min=5, max=60, default_value=15),
    _i("mcp.jira.enabled", "Use the Jira MCP", "Ticket cards and the evening facts the worklog subtracts.", "bool", "jira",
       affects="Background Jira reads (within the daily budget)"),
    _i("jira.parent_issue_types", "Parent issue types", "Jira issue types that only group work (one per line). They "
       "never get time or findings; the child ticket named with them, or the branch's ticket, does.", "names", "jira",
       affects="Which ticket a prompt's time and findings go to"),
    _i("mcp.jira.cards", "Ticket cards", "Read each day's tickets from Jira (title, status, summary) for the ticket "
       "pages.", "bool", "jira", default_value=True),
    _i("mcp.jira.evening", "Evening facts", "Before the 8 PM message, read what's already logged in Jira that day so it "
       "isn't logged twice.", "bool", "jira", default_value=True),
    _i("mcp.database.enabled", "Use the database MCP", "Schema cards, the QA trace and the link map.", "bool", "database",
       needs="database"),
    _i("mcp.database.trace", "QA trace", "Follow a ticket's records through QA with read-only queries.", "bool",
       "database", default_value=True),
    _i("mcp.database.linkmap", "Link map", "Once a week, sample QA rows to check which table links hold.", "bool",
       "database", default_value=True),
    _i("qa_data.allowed_envs", "Environments it may read", "Only narrowing is possible here; adding an environment "
       "takes /cg:setup, which checks it isn't production.", "envs_narrow", "database"),
    _i("branching.guard_mode", "Branch guard", "What happens when a new branch or PR doesn't fit the repo's strategy.",
       "choice", "branching", choices=[["warn", "Warn"], ["ask", "Ask before"], ["off", "Off"]]),
    _i("branching.fetch.enabled", "Fetch watched repos", "Refresh remote branches so merge status is exact. Fetch only "
       "updates remote-tracking refs.", "bool", "branching"),
    _i("branching.stale_pr_days", "Stale PR after (days)", "An open PR older than this raises an alert.", "int",
       "branching", min=1, max=60),
    _i("releases.enabled", "Track releases", "Which Jira release each ticket ships in, its release branch per repo, and "
       "whether the changes reached release, QA and (when required) UAT. Local; the Jira reads use the daily budget.",
       "bool", "releases", default_value=False, affects="Release plan, monthly Releases page, UAT reminders"),
    _i("releases.stale_hours", "Refs stale after (hours)", "Older remote branches make a missing change 'unknown', "
       "never 'missing'.", "int", "releases", min=1, max=336, default_value=24),
    _i("releases.jira.uat_field", "Jira field: UAT required", "The custom field that says whether a ticket needs UAT, "
       "for example customfield_10100. Empty: the requirement stays unknown unless you set it per ticket.", "name",
       "releases", pattern=r"(customfield_\d{1,10})?", pattern_help="Use a custom field id such as customfield_10100, or "
       "leave it empty.", default_value=""),
    _i("releases.jira.planned_date_field", "Jira field: planned release date", "Optional custom field with a planned "
       "date, used when the fix version has no release date.", "name", "releases", pattern=r"(customfield_\d{1,10})?",
       pattern_help="Use a custom field id such as customfield_10200, or leave it empty.", default_value=""),
    _i("releases.jira.uat_deployed_statuses", "Jira statuses meaning deployed to UAT", "Only these count as deployment "
       "evidence (one per line). Empty: deployment stays unverified.", "names", "releases", default_value=[]),
    _i("releases.jira.uat_accepted_statuses", "Jira statuses meaning UAT accepted", "Only these count as acceptance "
       "evidence (one per line).", "names", "releases", default_value=[]),
    _i("releases.jira.scope_refresh_hours", "Re-read my scheduled tickets every (hours)", "One Jira search for the "
       "monthly overview.", "int", "releases", min=1, max=168, default_value=12),
    _i("releases.scope.assigned", "My tickets: assigned to me in Jira", "Tickets Jira assigns to you count as yours on "
       "the Releases page.", "bool", "releases", default_value=True),
    _i("releases.scope.branches", "My tickets: my development branches", "A branch named for the ticket with my "
       "commits. A shared integration PR alone never counts.", "bool", "releases", default_value=True),
    _i("releases.scope.worked", "My tickets: worked on in Claude Code", "Tickets with recorded Claude Code work count "
       "as yours.", "bool", "releases", default_value=True),
    _i("requests.enabled", "Requests", "Curate the API calls you make into Bruno requests per ticket. Off: nothing is "
       "curated or written to the Bruno folder.", "bool", "artifacts", default_value=True),
    _i("artifacts.enabled", "Artifacts", "Copy files that name a ticket into its folder. Off: they wait until it's on "
       "again.", "bool", "artifacts", default_value=True),
    _i("artifacts.watch_dirs", "Watched folders", "New files here are copied in when they name a ticket.", "paths",
       "artifacts", must_exist=True),
    _i("push_enabled", "Push the daily files", "Off keeps the commits on this machine until you switch it on.", "bool",
       "push", default_value=True),
    _i("push_interval_minutes", "Push at most every (minutes)", "Minimum gap between pushes of the daily files.", "int",
       "push", min=5, max=1440),
    _i("models.extract", "Model: session extraction", "An alias (haiku, sonnet, opus) or an explicit model id such as "
       "claude-haiku-4-5, which pins the version so extraction doesn't change when the alias moves.", "model", "models",
       choices=MODELS),
    _i("models.consolidate", "Model: day summary", "", "model", "models", choices=MODELS),
    _i("models.curate", "Model: request curation", "", "model", "models", choices=MODELS),
    _i("models.dbflow", "Model: DB flow", "", "model", "models", choices=MODELS),
    _i("models.worklog_condense", "Model: worklog condensing", "", "model", "models", choices=MODELS,
       default_value="sonnet"),
    _i("models.brief", "Model: ticket briefs", "Not set: the day-summary model.", "model", "models", choices=MODELS,
       optional=True),
    _i("budget.model_daily_usd", "Daily model spend cap (USD)", "When today's model spend reaches this, supersession "
       "and ticket briefs wait until tomorrow. Extraction and the day summary always run.", "number", "models",
       min=0, max=100, default_value=3.0),
    _i("brief.daily_cap", "Ticket briefs per day", "At most this many brief rebuilds a day.", "int", "models",
       min=0, max=50, default_value=8),
    _i("budget.companion_call_usd", "Companion planning: spend ceiling per call (USD)", "0 keeps Generate Plan and "
       "Improve Plan off. Each explicit call first reserves this much of the daily model spend cap; nothing runs when "
       "the reservation doesn't fit.", "number", "models", min=0, max=5, default_value=0),
    _i("brief.verified_statuses", "Jira statuses that mean QA verified", "A brief shows \"Verified by QA\" for a "
       "ticket in one of these Jira statuses (one per line, any case) when no passing test or replay proves it.",
       "names", "models", default_value=["QA COMPLETED", "QA PASSED", "READY FOR RELEASE", "DONE", "CLOSED",
                                         "RESOLVED"]),
    _i("mcp.daily_call_budget", "Daily MCP job budget", "Background MCP jobs per day. Runs you ask for count separately.",
       "int", "models", min=0, max=100),
    _i("extract_timeout_s", "Extraction timeout (seconds)", "How long one extraction call may take before it is retried "
       "later. Large chunks can need more than 5 minutes.", "int", "models", min=120, max=1800, default_value=600),
    _i("raw_retention_days", "Keep raw events (days)", "Raw capture files older than this are pruned once processed.",
       "int", "data", min=7, max=365),
    _i("reminders.mcp", "Weekly connect reminder", "A line at session start when Slack or Jira isn't connected.", "bool",
       "data"),
    _i("share.targets", "Share targets", "Where /cg:share writes bundles: an existing folder (bundles go under its "
       "shares/ folder) or a team Git repo (only shares/ is committed and pushed, never forced). Never Slack.",
       "share_targets", "share", default_value=[]),
    _i("share.default_level", "Default level", "team keeps SQL, hosts, internal names and PR links; public removes "
       "them too and needs a deny-list.", "choice", "share", choices=[["team", "team"], ["public", "public"]],
       default_value="team"),
    _i("share.author", "Your name on shared notes", "Shown as \"by\" on your bundles; Team notes uses it to tell yours "
       "apart.", "name", "share", default_value=""),
    _i("dashboard.preview_buttons", "Preview buttons", "Show Refresh from Jira and Preview exactly on Today. Each "
       "press is one read-only call within the daily MCP budget.", "bool", "dashboard", default_value=True),
    _i("dashboard.autostart", "Start the dashboard automatically", "From the scheduled task and session start.", "bool",
       "dashboard"),
    # shown, never editable here
    _i("github_remote", "GitHub repo for the daily push", "Set with /cg:setup, which checks the repo is private.", "text",
       "push", editable=False),
    _i("worklog_home", "Data folder", "Moving it is a /cg:setup step.", "text", "data", editable=False),
    _i("storage.local_backup_dir", "Backup folder", "Set with /cg:setup.", "text", "data", editable=False),
    _i("mcp.database.server", "Database MCP server", "Chosen by /cg:setup, which classifies its tools as read or write.",
       "text", "database", editable=False),
    _i("mcp.jira.server", "Jira MCP server", "Chosen by /cg:setup.", "text", "jira", editable=False),
    # the database flow's approved scope and connector mapping: validated (check / check_locked), never edited here
    _i("mcp.database.scope", "Approved database scope", "Environments, connections, databases, schemas and "
       "repositories the database flow may read. Set with /cg:setup; production is never in scope.", "db_scope",
       "database", editable=False),
    _i("mcp.database.arguments", "Database MCP argument names", "How the server names its connection, schema, table, "
       "statement and limit arguments. Set with /cg:setup.", "db_arguments", "database", editable=False),
    _i("mcp.database.authorization_max_age_days", "Access confirmation goes stale after (days)", "Cached database "
       "context older than this is labelled with the date its access was last confirmed.", "int", "database",
       editable=False, min=1, max=90, default_value=7),
    _i("safety.rules", "Safety rules", "What Chitragupta may run, write and reach. They have no off switch and can't be "
       "changed from here.", "text", "safety", editable=False, fixed="Always on"),
    _i("safety.mcp_tools", "MCP tools and their read/write class", "Only /cg:setup adds MCP tools.", "text", "safety",
       editable=False, fixed="Managed by /cg:setup"),
    _i("safety.secrets", "Secrets", "Never shown or stored by the dashboard.", "text", "safety", editable=False,
       fixed="Not shown"),
]


def _repo_items(cfg):
    """Per-repo branching items for every configured or watched repo."""
    from . import gitcmd
    names = set(((cfg.get("branching") or {}).get("repos") or {}).keys())
    names |= {os.path.basename(str(r).rstrip("/\\")) for r in cfg.get("watched_repos") or [] if r}
    strategies = sorted(gitcmd.settings(cfg)["strategies"])
    out = []
    for n in sorted(x for x in names if x):
        out.append(_i("branching.repos.%s.strategy" % n, "%s: strategy" % n, "How this repo's changes reach their targets.",
                      "choice", "branching", choices=[["", "None (no delivery check)"]] + [[s, s] for s in strategies],
                      repo=n))
        out.append(_i("branching.repos.%s.bases" % n, "%s: base branches" % n, "The branch names (or patterns such as "
                      "release/*) that play each role.", "bases", "branching", repo=n, roles=list(gitcmd.repo_conf(cfg, n)["bases"])))
        out.append(_i("branching.repos.%s.relations" % n, "%s: how the bases relate" % n, "Which base is cut from, merges "
                      "into or syncs from which. Shapes the branch graph and the delivery wording; the check itself "
                      "always uses real merges.", "relations", "branching", repo=n,
                      kinds=[["cut_from", "is cut from"], ["merges_into", "merges into"], ["syncs_from", "syncs from"]]))
        out.append(_i("branching.repos.%s.pr_source" % n, "%s: PR information from" % n, "Where open and merged PRs are "
                      "read.", "choice", "branching", repo=n,
                      choices=[["", "Automatic (host CLI, else the host's MCP, else merge commits)"],
                               ["cli", "The host's CLI only"], ["mcp", "The host's read-only MCP only"],
                               ["git", "Merge commits only"]]))
    return out


def _effective_repo(cfg, it):
    """Per-repo values as the engine sees them (defaults filled in), so the page shows what is in effect."""
    from . import gitcmd
    rc = gitcmd.repo_conf(cfg, it["repo"])
    field = it["key"].rsplit(".", 1)[1]
    if field == "bases":
        return {r: (v if isinstance(v, list) else [v]) for r, v in rc["bases"].items()}
    if field == "relations":
        return rc["relations"]
    return configstore.get(cfg, it["key"], "")


def schema(cfg=None):
    cfg = cfg if cfg is not None else common.load_config()
    return STATIC + _repo_items(cfg)


def _by_key(cfg):
    out = {}
    for it in schema(cfg):
        out[it["key"]] = it
    return out


# --------------------------------------------------------------------------- validation (mirrored by the page)

_TOKEN_SHAPE = re.compile(r"(?<![A-Za-z0-9_./+=-])(?=[A-Za-z0-9_+/=-]*\d)(?=[A-Za-z0-9_+/=-]*[A-Za-z])"
                          r"[A-Za-z0-9_+/=-]{32,}(?![A-Za-z0-9_./+=-])")
_INSTANCE = re.compile(r"^[a-z0-9.-]{1,100}(?:/[A-Za-z0-9._ -]{1,80})?$")
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9:._@+=/-]{1,128}$")
_TICKET = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,7}$")
_ACCOUNT_KEYS = {"provider", "instance", "id", "node_id", "login", "source", "confirmed_at"}


def secret_like(text, free_text=True):
    """A reason when `text` looks like a credential (the share scan's secret rules; in free text also a long
    token-shaped run), else None. The profile is never a place for tokens or passwords."""
    from . import scanlib
    for _, kind, name in scanlib.find(str(text or ""), [], scanlib.secret_rules()):
        return "That looks like a secret (%s). Tokens and passwords never go in the profile." % name
    if free_text and _TOKEN_SHAPE.search(str(text or "")):
        return "That looks like a token. Tokens and passwords never go in the profile."
    return None


def _bot(value):
    from . import profile
    for p in profile.settings(common.load_config())["bot_patterns"]:
        try:
            if re.search(p, str(value or ""), re.I):
                return True
        except re.error:
            continue
    return False


def _line(value, maxlen):
    if not isinstance(value, str) or len(value) > maxlen or any(ord(c) < 32 for c in value):
        return "Use one line of at most %d characters." % maxlen
    return secret_like(value)


def _check_account(i, a):
    from . import profile
    if not isinstance(a, dict):
        return "Account %d: give provider, instance and id or login." % i
    extra = set(a) - _ACCOUNT_KEYS
    if extra:
        return "Account %d: unknown fields %s." % (i, ", ".join(sorted(extra)))
    prov = a.get("provider")
    if prov not in profile.PROVIDERS:
        return "Account %d: the provider must be one of %s." % (i, ", ".join(profile.PROVIDERS))
    inst = a.get("instance")
    if not isinstance(inst, str) or not _INSTANCE.match(inst):
        return "Account %d: give the instance in lower case, for example github.com or acme.atlassian.net." % i
    aid, nid, login = a.get("id", ""), a.get("node_id", ""), a.get("login", "")
    if any(not isinstance(v, str) for v in (aid, nid, login)):
        return "Account %d: id and login are text." % i
    if not (aid or login):
        return "Account %d: give the account id (preferred) or the login." % i
    for v in (aid, nid):
        if v and not _ACCOUNT_ID.match(v):
            return "Account %d: the id may use letters, digits and : . _ @ + = / - only." % i
    if login and (len(login) > 100 or any(ord(c) < 33 for c in login)):
        return "Account %d: the login is one word of at most 100 characters." % i
    if login and _bot(login):
        return "Account %d: %s is a bot or service identity; it can't be yours." % (i, login)
    for k in ("source", "confirmed_at"):
        if not isinstance(a.get(k, ""), str) or len(a.get(k, "")) > 200:
            return "Account %d: %s is text of at most 200 characters." % (i, k)
    for v in (aid, nid, login, a.get("source", "")):
        why = secret_like(v, free_text=False)
        if why:
            return "Account %d: %s" % (i, why)
    return None


def check(it, value, current):
    """None when `value` is acceptable for item `it`, else a sentence saying what's wrong and what is allowed."""
    t = it["type"]
    if t == "line":
        return _line(value, it.get("maxlen", 80))
    if t == "longtext":
        if not isinstance(value, str) or len(value) > it.get("maxlen", 400) or \
                any(ord(c) < 32 and c not in "\n\t" for c in value):
            return "Use at most %d characters." % it.get("maxlen", 400)
        return secret_like(value)
    if t == "email":
        from . import profile
        if value == "":
            return None
        if not isinstance(value, str) or not profile.norm_email(value):
            return "Use an email address such as developer@acme.test, or leave it empty."
        return "That is a bot or service address." if _bot(value) else None
    if t == "emails":
        from . import profile
        if not isinstance(value, list) or len(value) > 20:
            return "Use a list of at most 20 email addresses."
        seen = set()
        for e in value:
            n = profile.norm_email(e) if isinstance(e, str) else ""
            if not n:
                return "%s isn't an email address." % (e if isinstance(e, str) else "A value")
            if n in seen:
                return "%s is listed twice." % e
            if _bot(n):
                return "%s is a bot or service identity; it can't be one of your aliases." % e
            seen.add(n)
        return None
    if t == "accounts":
        if not isinstance(value, list) or len(value) > 20:
            return "Use a list of at most 20 accounts."
        from . import profile
        seen = set()
        for i, a in enumerate(value, 1):
            why = _check_account(i, a)
            if why:
                return why
            k = profile.account_key(a)  # several accounts of yours may share an instance; the same one twice may not
            if k in seen:
                return "Account %d: this %s account for %s is listed twice." % (i, a["provider"], a["instance"])
            seen.add(k)
        return None
    if t == "repo_paths":
        cfg = common.load_config()
        watched = {common.norm_path(r) for r in cfg.get("watched_repos") or [] if isinstance(r, str)}
        if not isinstance(value, list) or any(not isinstance(p, str) or not p.strip() for p in value):
            return "Enter one repository folder per line."
        for p in value:
            if common.norm_path(p) not in watched:
                return "%s isn't a watched repository." % p
        return None
    if t == "tags":
        if not isinstance(value, list) or len(value) > 20:
            return "Use at most 20 entries."
        for v in value:
            why = _line(v, 40) if isinstance(v, str) and v.strip() else "Use one name per line."
            if why:
                return why
        return None
    if t == "timezone":
        from . import profile
        if not isinstance(value, str) or len(value) > 64:
            return "Use a zone name or an offset such as +05:30."
        try:
            profile.zone(value)
        except ValueError as e:
            return str(e)[0].upper() + str(e)[1:] + "."
        return None
    if t == "date":
        if value == "":
            return None
        try:
            d = _dt.date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
        except ValueError:
            d = None
        if d is None:
            return "Use a date such as 2026-01-31, or leave it empty."
        if d > common.now().date():
            return "Use a day that isn't in the future."
        return None
    if t == "ticket_reasons":
        if not isinstance(value, dict) or len(value) > 50:
            return "Use at most 50 tickets, each with a reason."
        for k, v in value.items():
            if not _TICKET.match(str(k)):
                return "%s isn't a ticket key such as ABC-1001." % k
            if not isinstance(v, str) or not v.strip():
                return "%s: say why it counts as yours." % k
            why = _line(v, 120)
            if why:
                return "%s: %s" % (k, why)
        return None
    if t == "regexes":
        if not isinstance(value, list) or len(value) > 20:
            return "Use at most 20 patterns."
        for v in value:
            if not isinstance(v, str) or not v or len(v) > 200:
                return "Use one pattern of at most 200 characters per line."
            try:
                re.compile(v)
            except re.error as e:
                return "%s isn't a valid pattern: %s." % (v, e)
        return None
    if t == "globs":
        if not isinstance(value, list) or len(value) > 100:
            return "Use at most 100 patterns."
        for v in value:
            if not isinstance(v, str) or not v.strip() or len(v) > 200 or any(ord(c) < 32 for c in v):
                return "Use one pattern of at most 200 characters per line."
        return None
    if t == "bool":
        return None if isinstance(value, bool) else "Choose on or off."
    if t in ("int", "number"):
        ok = isinstance(value, int) and not isinstance(value, bool) if t == "int" else \
            isinstance(value, (int, float)) and not isinstance(value, bool)
        if not ok:
            return "Enter a whole number." if t == "int" else "Enter a number."
        if value < it.get("min", value) or value > it.get("max", value):
            return "Use a value from %s to %s." % (it["min"], it["max"])
        return None
    if t == "time":
        if not isinstance(value, str) or not TIME.match(value):
            return "Use 24-hour time, for example 20:00."
        if (it.get("min") and value < it["min"]) or (it.get("max") and value > it["max"]):
            return "Use a time from %s to %s." % (it["min"], it["max"])
        return None
    if t == "choice":
        return None if value in [c[0] for c in it["choices"]] else "Pick one of the listed options."
    if t == "model":
        if value == "" and it.get("optional"):
            return None  # not set: the fallback model
        if isinstance(value, str) and (value in it["choices"] or MODEL_ID.match(value)):
            return None
        return "Use %s, or a model id such as claude-haiku-4-5." % ", ".join(it["choices"])
    if t == "days":
        if not isinstance(value, list) or not value or any(d not in DAYS for d in value) or len(set(value)) != len(value):
            return "Pick at least one day."
        return None
    if t == "prefixes":
        if not isinstance(value, list) or any(not isinstance(p, str) or not PREFIX.match(p) for p in value):
            return "Use uppercase project keys of 2 to 10 letters or digits, starting with a letter."
        return None
    if t == "names":
        if not isinstance(value, list) or len(value) > 20 or any(not isinstance(n, str) or not NAME.match(n) for n in value):
            return "Use one name per line: letters, digits, spaces or -, at most 40 characters."
        return None
    if t == "paths":
        if not isinstance(value, list) or any(not isinstance(p, str) or not p.strip() for p in value):
            return "Enter one folder per line."
        for p in value:
            if not os.path.isabs(p):
                return "%s isn't a full path." % p
            if it.get("must_exist") and not os.path.isdir(p):
                return "%s doesn't exist." % p
            from . import plugin
            cd = os.path.normcase(os.path.realpath(plugin.claude_dir()))
            if os.path.normcase(os.path.realpath(p)).startswith(cd):
                return "Claude Code's own folder can't be used here."
        return None
    if t == "bases":
        if not isinstance(value, dict) or not value:
            return "Give at least one base branch."
        for role, pats in value.items():
            if role not in it["roles"]:
                return "Unknown role %s." % role
            if not isinstance(pats, list) or not pats or any(not isinstance(p, str) or not BRANCH_PATTERN.match(p) for p in pats):
                return "%s: use branch names or patterns (letters, digits, . _ / * -), separated by commas." % role
        return None
    if t == "relations":
        from . import relations
        if not isinstance(value, list) or len(value) > 30:
            return "Use a list of at most 30 relations."
        clean, warnings = relations.normalize(value)
        if warnings:
            return warnings[0][0].upper() + warnings[0][1:] + "."
        for r in clean:
            for p in [r["from"]] + r["to"]:
                if not BRANCH_PATTERN.match(p):
                    return "%s isn't a branch name or pattern." % p
            if r["from"] in r["to"]:
                return "%s can't relate to itself." % r["from"]
        return None
    if t == "name":
        if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 for c in value):
            return "Use one line of at most 80 characters."
        if it.get("pattern") and not re.fullmatch(it["pattern"], value):
            return it.get("pattern_help") or "That value isn't allowed here."
        return None
    if t == "share_targets":
        from .safety import commands, paths as sp
        if not isinstance(value, list) or len(value) > 10:
            return "Use a list of at most 10 targets."
        names = set()
        for i, tg in enumerate(value):
            why = commands.share_target_reason(tg)
            if why:
                return "Target %d: %s." % (i + 1, why)
            if tg["name"] in names:
                return "Target %d: the name %s is used twice." % (i + 1, tg["name"])
            names.add(tg["name"])
            if set(tg) - {"name", "kind", "path", "url", "branch", "default_level"}:
                return "Target %d: unknown fields %s." % (i + 1, ", ".join(sorted(set(tg) - {"name", "kind", "path", "url",
                                                                                            "branch", "default_level"})))
            if tg["kind"] == "folder" and not sp.share_folder_roots({"share": {"targets": [tg]}}):
                return "Target %d: %s isn't an existing folder (a full path)." % (i + 1, tg.get("path"))
            if tg.get("default_level") not in (None, "team", "public"):
                return "Target %d: the level must be team or public." % (i + 1)
        return None
    if t == "envs_narrow":
        cur = current if isinstance(current, list) else []
        if not isinstance(value, list) or not value:
            return "Keep at least one environment."
        extra = [e for e in value if e not in cur]
        return None if not extra else "Adding %s takes /cg:setup." % ", ".join(extra)
    if t == "db_scope":
        from . import dbaccess
        errs = dbaccess.scope_errors(value)
        return (errs[0][0].upper() + errs[0][1:] + ".") if errs else None
    if t == "db_arguments":
        from . import dbconnector
        errs = dbconnector.mapping_errors(value)
        return (errs[0][0].upper() + errs[0][1:] + ".") if errs else None
    return "This setting can't be changed here."


def check_locked(key, value, cfg=None):
    """Validate a value for a setting the dashboard only shows (editable False), for /cg:setup and config checks:
    None or the problem. validate() still refuses every such key from the dashboard."""
    it = _by_key(cfg if cfg is not None else common.load_config()).get(key)
    if not it:
        return "This setting doesn't exist."
    return check(it, value, configstore.get(cfg or {}, key))


def _locked_summary(type_, value):
    """One line for a structured setting the page only shows (the scope, the argument mapping)."""
    if not isinstance(value, dict) or not value:
        return ""
    if type_ == "db_scope":
        conns = value.get("connections") if isinstance(value.get("connections"), dict) else {}
        parts = ["%s (%s)" % (c, (e or {}).get("env") or "env unknown") for c, e in sorted(conns.items())
                 if isinstance(e, dict) or e is None]
        repos = value.get("repos") if isinstance(value.get("repos"), list) else []
        return "%s · %d repositor%s" % (", ".join(parts) or "no connections", len(repos), "y" if len(repos) == 1 else "ies")
    return ", ".join("%s → %s" % (k, v or "(none)") for k, v in sorted(value.items()))


def describe(cfg=None):
    """The schema with each item's current value, grouped for the page."""
    raw, _ = configstore.read_raw()
    eff = _defaults()
    groups = {g: {"id": g, "label": lbl, "items": []} for g, lbl in GROUPS}
    for it in schema(eff):
        row = {k: v for k, v in it.items() if k != "default_value"}
        if "fixed" in it:
            row["value"] = it["fixed"]
        elif it.get("repo"):
            row["value"] = _effective_repo(eff, it)
            row["set"] = configstore.get(raw or {}, it["key"]) is not None
        else:
            row["value"] = configstore.get(eff, it["key"], it.get("default_value"))
            row["set"] = configstore.get(raw or {}, it["key"]) is not None  # changed from the default
            if it["type"] in ("db_scope", "db_arguments"):
                row["value"] = _locked_summary(it["type"], row["value"])
        if it.get("needs") == "slack":
            row["available"] = bool(configstore.get(eff, "jira_worklog.slack.self_user_id"))
        elif it.get("needs") == "database":
            row["available"] = bool(configstore.get(eff, "mcp.database.server"))
        groups[it["group"]]["items"].append(row)
    return {"groups": [g for g in groups.values() if g["items"]], "history": configstore.history(20)}


def validate(changes, cfg=None):
    eff = cfg if cfg is not None else _defaults()
    items = _by_key(eff)
    clean, errors = {}, {}
    if not isinstance(changes, dict) or not changes:
        return {}, {"": "Nothing to change."}
    if len(changes) > 40:
        return {}, {"": "Too many changes at once."}
    for k, v in changes.items():
        it = items.get(k)
        if not it:
            errors[k] = "This setting doesn't exist."
        elif not it.get("editable"):
            errors[k] = "This setting can only be changed with /cg:setup."
        else:
            err = check(it, v, configstore.get(eff, k))
            if err:
                errors[k] = err
            else:
                clean[k] = v
    return clean, errors


def apply(changes, source="dashboard"):
    """Validate and write. Raises ValueError with {key: error} when anything is refused (nothing is written)."""
    clean, errors = validate(changes)
    if errors:
        raise ValueError(errors)
    from . import relations
    for k in clean:
        if k.endswith(".relations"):
            clean[k] = relations.normalize(clean[k])[0]  # stored as the engine reads it: "to" always a list
    return configstore.update(clean, source)


def matches(pattern, name):
    return fnmatch.fnmatchcase(name, pattern)
