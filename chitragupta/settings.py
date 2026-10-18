"""The settings the dashboard can show and change: one schema, one validator, one write path.

    describe()                 -> {"groups": [{"id","label","items":[{key,label,help,type,…,value,default,editable}]}]}
    validate(changes, cfg)     -> (clean dict, {key: error}) — the same rules the page shows next to each field
    apply(changes, source)     validate, then write through configstore (lock, backup, history); -> configstore result

Everything not in the schema, and every item with `editable: False`, is refused here whatever the request says: the
safety rules, MCP tool lists and their read/write classification, secrets, the data folder, the GitHub repo, paths,
the Slack recipient, push paths and redaction patterns are only ever shown. /cg:setup is how those change.
"""
import fnmatch
import os
import re

from . import common
from . import configstore

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MODELS = ["haiku", "sonnet", "opus"]
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PREFIX = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")


def _defaults():
    from . import gitcmd, timesheet
    d = common.load_config()
    d["jira_worklog"] = timesheet.settings(d)
    b = gitcmd.settings(d)
    d["branching"] = dict(d.get("branching") or {}, guard_mode=b["guard_mode"], stale_pr_days=b["stale_pr_days"],
                          fetch=b["fetch"])
    return d


# --------------------------------------------------------------------------- schema
# type: bool | int | number | time | choice | days | prefixes | paths | envs_narrow | text
def _i(key, label, help_, type_, group, **kw):
    return dict(key=key, label=label, help=help_, type=type_, group=group, editable=kw.pop("editable", True), **kw)


GROUPS = [("capture", "Capture"), ("worklog", "Daily worklog & Slack"), ("jira", "Jira"), ("database", "Database MCP"),
          ("branching", "Branches & PRs"), ("artifacts", "Requests & artifacts"), ("push", "GitHub daily push"),
          ("models", "Models & budget"), ("data", "Data"), ("dashboard", "Dashboard"), ("safety", "Locked")]

STATIC = [
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
    _i("mcp.jira.enabled", "Use the Jira MCP", "Ticket cards and the evening facts the worklog subtracts.", "bool", "jira",
       affects="Background Jira reads (within the daily budget)"),
    _i("mcp.database.enabled", "Use the database MCP", "Schema cards, the QA trace and the link map.", "bool", "database",
       needs="database"),
    _i("qa_data.allowed_envs", "Environments it may read", "Only narrowing is possible here; adding an environment "
       "takes /cg:setup, which checks it isn't production.", "envs_narrow", "database"),
    _i("branching.guard_mode", "Branch guard", "What happens when a new branch or PR doesn't fit the repo's strategy.",
       "choice", "branching", choices=[["warn", "Warn"], ["ask", "Ask before"], ["off", "Off"]]),
    _i("branching.fetch.enabled", "Fetch watched repos", "Refresh remote branches so merge status is exact. Fetch only "
       "updates remote-tracking refs.", "bool", "branching"),
    _i("branching.stale_pr_days", "Stale PR after (days)", "An open PR older than this raises an alert.", "int",
       "branching", min=1, max=60),
    _i("artifacts.watch_dirs", "Watched folders", "New files here are copied in when they name a ticket.", "paths",
       "artifacts", must_exist=True),
    _i("push_interval_minutes", "Push at most every (minutes)", "Minimum gap between pushes of the daily files.", "int",
       "push", min=5, max=1440),
    _i("models.extract", "Model: session extraction", "", "choice", "models", choices=[[m, m] for m in MODELS]),
    _i("models.consolidate", "Model: day summary", "", "choice", "models", choices=[[m, m] for m in MODELS]),
    _i("models.curate", "Model: request curation", "", "choice", "models", choices=[[m, m] for m in MODELS]),
    _i("models.dbflow", "Model: DB flow", "", "choice", "models", choices=[[m, m] for m in MODELS]),
    _i("models.worklog_condense", "Model: worklog condensing", "", "choice", "models", choices=[[m, m] for m in MODELS],
       default_value="sonnet"),
    _i("mcp.daily_call_budget", "Daily MCP job budget", "Background MCP jobs per day. Runs you ask for count separately.",
       "int", "models", min=0, max=100),
    _i("raw_retention_days", "Keep raw events (days)", "Raw capture files older than this are pruned once processed.",
       "int", "data", min=7, max=365),
    _i("reminders.mcp", "Weekly connect reminder", "A line at session start when Slack or Jira isn't connected.", "bool",
       "data"),
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
    return out


def schema(cfg=None):
    cfg = cfg if cfg is not None else common.load_config()
    return STATIC + _repo_items(cfg)


def _by_key(cfg):
    out = {}
    for it in schema(cfg):
        out[it["key"]] = it
    return out


# --------------------------------------------------------------------------- validation (mirrored by the page)

def check(it, value, current):
    """None when `value` is acceptable for item `it`, else a sentence saying what's wrong and what is allowed."""
    t = it["type"]
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
    if t == "days":
        if not isinstance(value, list) or not value or any(d not in DAYS for d in value) or len(set(value)) != len(value):
            return "Pick at least one day."
        return None
    if t == "prefixes":
        if not isinstance(value, list) or any(not isinstance(p, str) or not PREFIX.match(p) for p in value):
            return "Use uppercase project keys of 2 to 10 letters or digits, starting with a letter."
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
    if t == "envs_narrow":
        cur = current if isinstance(current, list) else []
        if not isinstance(value, list) or not value:
            return "Keep at least one environment."
        extra = [e for e in value if e not in cur]
        return None if not extra else "Adding %s takes /cg:setup." % ", ".join(extra)
    return "This setting can't be changed here."


def describe(cfg=None):
    """The schema with each item's current value, grouped for the page."""
    raw, _ = configstore.read_raw()
    eff = _defaults()
    groups = {g: {"id": g, "label": lbl, "items": []} for g, lbl in GROUPS}
    for it in schema(eff):
        row = {k: v for k, v in it.items() if k != "default_value"}
        if "fixed" in it:
            row["value"] = it["fixed"]
        else:
            row["value"] = configstore.get(eff, it["key"], it.get("default_value"))
            row["set"] = configstore.get(raw or {}, it["key"]) is not None  # changed from the default
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
    return configstore.update(clean, source)


def matches(pattern, name):
    return fnmatch.fnmatchcase(name, pattern)
