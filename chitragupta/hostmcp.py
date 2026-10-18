"""Pull-request data through a Git host's read-only MCP server, when the host's CLI isn't installed.

Source order per repo (branching.repos.<repo>.pr_source, see hosts.source_for): host CLI -> host MCP -> merge commits.
Providers: Azure DevOps (microsoft/azure-devops-mcp) and GitHub (github/github-mcp-server). /cg:setup detects a
configured server, records it as mcp.git_host.<provider> = {"server", "enabled"} and files it in
.worklog/mcp/allow.json with "role": "git-host" and the classified read tools (safety.mcp.GIT_HOST_READ); every other
tool of that server is a write or unknown, and blocked.

One refresh of a repo = ONE job through the read-only harness (mcpjobs.run: strict MCP config with that server only,
the classified reads on --allowedTools, the fail-closed guard, the daily MCP budget with the 20:00 reserve): all due
branches' list calls in one turn. The PR records are built here from the raw tool results in the stream events,
never from the model's answer. Records have the CLI path's shape (hosts.py) with src = "mcp:<provider>".
"""
import json
import re
from urllib.parse import quote

from . import common, mcpjobs
from .hosts import SAFE_ARG, SAFE_NAME  # the same name rules as the CLI path
from .safety.mcp import GIT_HOST_READ

PROVIDERS = ("azure-devops", "github")
PROVIDER_OF_HOST = {"azure": "azure-devops", "github": "github"}
LABEL = {"azure-devops": "Azure DevOps", "github": "GitHub"}
SAFE_OWNER = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_CALLS = 16  # tool calls in one job (list calls for due branches first, then gets of known PRs)
MAX_BRANCHES = 10

# Every write tool the official servers publish (blocked anyway: not a classified read); listed so --disallowedTools
# names them too.
GIT_HOST_WRITE = {
    "azure-devops": ["repo_pull_request_write", "repo_pull_request_thread_write", "repo_file_write", "repo_create_branch",
                     "repo_create_pull_request", "repo_update_pull_request", "repo_update_pull_request_status",
                     "repo_update_pull_request_reviewers", "repo_create_pull_request_thread", "repo_reply_to_comment",
                     "repo_resolve_comment"],
    "github": ["create_pull_request", "update_pull_request", "merge_pull_request", "update_pull_request_branch",
               "pull_request_review_write", "add_comment_to_pending_review", "add_reply_to_pull_request_comment",
               "create_pending_pull_request_review", "submit_pending_pull_request_review",
               "delete_pending_pull_request_review", "create_and_submit_pull_request_review",
               "add_pull_request_review_comment_to_pending_review", "request_copilot_review", "create_pull_request_review",
               "create_branch", "create_or_update_file", "delete_file", "push_files"],
}


class Deferred(mcpjobs.JobError):
    """Skipped before any model call (not configured, over budget): no back-off needed."""

    def __init__(self, msg):
        super().__init__(msg, skip=True)


# --------------------------------------------------------------------------- classification (setup)

def detect_provider(name, sdef):
    """A Claude Code server definition -> "azure-devops" | "github" | None (by its package, image or URL)."""
    s = sdef if isinstance(sdef, dict) else {}
    text = " ".join([str(s.get("command") or ""), " ".join(str(a) for a in s.get("args") or []), str(s.get("url") or "")])
    text = text.lower()
    if "@azure-devops/mcp" in text or "azure-devops-mcp" in text or "mcp.dev.azure.com" in text:
        return "azure-devops"
    if "github-mcp-server" in text or "api.githubcopilot.com/mcp" in text or "server-github" in text:
        return "github"
    n = (name or "").lower()
    if n in ("ado", "azure-devops", "azuredevops", "azure_devops"):
        return "azure-devops"
    if n == "github":
        return "github"
    return None


def candidates(servers):
    """User-scope servers that look like a Git host's MCP: [{"server", "provider"}], sorted."""
    out = []
    for name, s in sorted((servers or {}).items()):
        p = detect_provider(name, s)
        if p:
            out.append({"server": name, "provider": p})
    return out


def read_tools(provider):
    return sorted(GIT_HOST_READ.get(provider) or {})


def allow_entry(provider, existing=None):
    """allow.json entry for a Git-host server: the classified reads (only), the known writes plus any listed before."""
    e = dict(existing or {})
    e.update(role="git-host", provider=provider, read=read_tools(provider))
    e["write"] = sorted(set(e.get("write") or []) | set(GIT_HOST_WRITE.get(provider) or []))
    for k in ("query_tools", "connections"):
        e.pop(k, None)
    return e


def configured(cfg):
    """{provider: server name} for every enabled mcp.git_host.<provider> entry with a server."""
    gh = ((cfg or {}).get("mcp") or {}).get("git_host") or {}
    out = {}
    for p in PROVIDERS:
        c = gh.get(p) if isinstance(gh.get(p), dict) else {}
        if c.get("enabled", True) and str(c.get("server") or "").strip():
            out[p] = str(c["server"]).strip()
    return out


def server_for(cfg, info):
    """(provider, server) the MCP source would use for this remote, or (provider, None)."""
    p = PROVIDER_OF_HOST.get((info or {}).get("host"))
    if not p:
        return None, None
    srv = configured(cfg).get(p)
    if srv and srv in (mcpjobs.claude_json().get("mcpServers") or {}):
        return p, srv
    return p, None


# --------------------------------------------------------------------------- adapters (raw tool result -> PR record)

def payload(result):
    """A tool result's text -> list of dicts. Handles JSON, wrapped JSON ({"result": ...}, text blocks), a list under a
    key (pull_requests / items / value), and text around the JSON (Azure DevOps marks external content)."""
    x = mcpjobs.unwrap(result)
    if isinstance(x, str):
        dec, i, found = json.JSONDecoder(), 0, None
        while found is None:
            m = re.compile(r"[\[{]").search(x, i)
            if not m:
                break
            try:
                found, _ = dec.raw_decode(x, m.start())
            except ValueError:
                i = m.start() + 1
        x = found
    if isinstance(x, dict):
        for k in ("pull_requests", "pullRequests", "items", "value", "results"):
            if isinstance(x.get(k), list):
                x = x[k]
                break
    if isinstance(x, dict):
        x = [x]
    return [p for p in x if isinstance(p, dict)] if isinstance(x, list) else []


def _short(ref):
    return re.sub(r"^refs/heads/", "", ref or "")


_ADO_STATUS = {1: "active", 2: "abandoned", 3: "completed"}


def ado_pr(p, info):
    """Azure DevOps pull request (list output, trimmed; or the full object from get) -> PR record."""
    n = p.get("pullRequestId")
    if n is None:
        return None
    st = p.get("statusName") or p.get("status")
    st = _ADO_STATUS.get(st, "") if isinstance(st, int) else str(st or "").lower()
    state = "merged" if st == "completed" else "closed" if st == "abandoned" else ("draft" if p.get("isDraft") else "open")
    votes = [int(r.get("vote") or 0) for r in p.get("reviewers") or [] if isinstance(r, dict) and
             (r.get("isRequired") or r.get("vote"))]
    review = "changes_requested" if any(v < 0 for v in votes) else "approved" if any(v > 0 for v in votes) else ""
    org, project = info.get("org") or "", info.get("project") or ""
    repo = info.get("repo") or ""
    head = (p.get("lastMergeSourceCommit") or {}).get("commitId")
    return {"number": int(n), "title": p.get("title") or "", "source": _short(p.get("sourceRefName")),
            "target": _short(p.get("targetRefName")), "state": state, "review": review,
            "created_at": p.get("creationDate"), "merged_at": p.get("closedDate") if state == "merged" else None,
            "merge_commit": (p.get("lastMergeCommit") or {}).get("commitId") if state == "merged" else None,
            "head": head, "head_at_merge": head if state == "merged" else None,
            "url": "https://dev.azure.com/%s/%s/_git/%s/pullrequest/%s" % (quote(org), quote(project), quote(repo), n),
            "src": "mcp:azure-devops"}


def gh_pr(p, info):
    """GitHub pull request (the server's minimal object, or the REST object) -> PR record."""
    n = p.get("number")
    if n is None:
        return None
    head, base = p.get("head") or {}, p.get("base") or {}
    st = str(p.get("state") or "").lower()
    merged = bool(p.get("merged") or p.get("merged_at") or p.get("mergedAt"))
    state = "merged" if merged else "closed" if st == "closed" else ("draft" if p.get("draft") else "open")
    rd = str(p.get("review_decision") or p.get("reviewDecision") or "").lower()
    sha = head.get("sha") if isinstance(head, dict) else None
    return {"number": int(n), "title": p.get("title") or "",
            "source": head.get("ref") if isinstance(head, dict) else None,
            "target": base.get("ref") if isinstance(base, dict) else None, "state": state, "review": rd,
            "created_at": p.get("created_at"), "merged_at": p.get("merged_at") if merged else None,
            "merge_commit": p.get("merge_commit_sha") if merged else None,
            "head": sha, "head_at_merge": sha if merged else None, "url": p.get("html_url") or p.get("url"),
            "src": "mcp:github"}


ADAPTER = {"azure-devops": ado_pr, "github": gh_pr}


def records(provider, res, info):
    """PR records from a job's stream: every successful call of a classified read, newest data last (a get after a
    list fills in what the trimmed list lacks)."""
    reads = GIT_HOST_READ.get(provider) or {}
    fn = ADAPTER[provider]
    by = {}
    for c in res.get("calls") or []:
        tool = str(c.get("name") or "").split("__")[-1]
        if tool not in reads or c.get("is_error") or c.get("result") is None:
            continue
        for p in payload(c["result"]):
            rec = fn(p, info)
            if not rec:
                continue
            old = by.get(rec["number"])
            if old is None:
                by[rec["number"]] = rec
            else:
                old.update({k: v for k, v in rec.items() if v not in (None, "")})
    return list(by.values())


# --------------------------------------------------------------------------- the job

DONE = {"type": "object", "properties": {"done": {"type": "boolean"}}, "required": ["done"], "additionalProperties": False}


def _ado_calls(info, branches, known):
    proj, repo = info["project"], info["repo"]
    calls = ["repo_pull_request action=list project=%s repositoryId=%s sourceRefName=%s status=All top=20" % (
        json.dumps(proj), json.dumps(repo), json.dumps("refs/heads/" + b)) for b in branches]
    calls += ["repo_pull_request action=get project=%s repositoryId=%s pullRequestId=%d" % (
        json.dumps(proj), json.dumps(repo), n) for n in known]
    note = ("If this server has no repo_pull_request tool, it is an older version: use "
            "repo_list_pull_requests_by_repo_or_project instead of action=list and repo_get_pull_request_by_id instead "
            "of action=get, with the same other arguments. ")
    turn2 = ("Then, in ONE more turn, call the get variant once for every pull request the list calls returned whose "
             "pullRequestId is not in the list above (at most %d), in parallel. " % max(0, MAX_CALLS - len(calls)))
    return calls, note, turn2


def _gh_calls(info, branches, known):
    owner, repo = info["path"].split("/", 1)
    calls = ["list_pull_requests owner=%s repo=%s state=\"all\" head=%s perPage=20" % (
        json.dumps(owner), json.dumps(repo), json.dumps("%s:%s" % (owner, b))) for b in branches]
    calls += ["pull_request_read method=\"get\" owner=%s repo=%s pullNumber=%d" % (json.dumps(owner), json.dumps(repo), n)
              for n in known]
    note = ("If this server has no pull_request_read tool, it is an older version: use get_pull_request with the same "
            "arguments minus method. ")
    return calls, note, ""


def _valid(provider, info):
    if provider == "azure-devops":
        return all(SAFE_NAME.match(info.get(k) or "") for k in ("org", "project", "repo"))
    parts = (info.get("path") or "").split("/")
    return len(parts) == 2 and all(SAFE_OWNER.match(x) for x in parts)


def fetch_prs(cfg, info, provider, branches, known, health, day=None):
    """ONE read-only job for one repo: PRs of `branches` (their list calls) and fresh copies of `known` PR numbers.
    -> {"prs": {branch: [PR]}, "branches": [branches covered], "calls": n, "server": name, "tools": [...]}.
    Raises Deferred (no model call made) or mcpjobs.JobError."""
    p, srv = server_for(cfg, info)
    if p != provider or not srv:
        raise Deferred("no %s MCP server configured (mcp.git_host.%s)" % (LABEL.get(provider, provider), provider))
    if common.env("NO_MCP_JOBS"):
        raise Deferred("MCP jobs are off (NO_MCP_JOBS)")
    if not _valid(provider, info):
        raise Deferred("unexpected characters in the remote's names")
    day = day or common.workday(common.now(), cfg)
    if common.env("MCP_MANUAL") != "1":
        from .kbjobs import EVENING_RESERVE
        if mcpjobs.budget_left(health, cfg, day) <= EVENING_RESERVE:
            raise Deferred("daily MCP budget left is kept for the 20:00 Jira call")
    branches = [b for b in branches if SAFE_ARG.match(b or "")][:MAX_BRANCHES]
    if not branches:
        raise Deferred("no branch to check")
    known = [int(n) for n in known][:max(0, MAX_CALLS - len(branches))]
    allow = common.read_json(common.wl("mcp", "allow.json"), {}) or {}
    if ((allow.get("servers") or {}).get(srv) or {}).get("role") != "git-host":
        mcpjobs.ensure_files(cfg)  # setup recorded the server after today's guard files were written
    calls, note, turn2 = (_ado_calls if provider == "azure-devops" else _gh_calls)(info, branches, known)
    task = ("Pull-request data, read only. Make ALL of these calls in a single turn (parallel), with exactly these "
            "arguments:\n%s\n%s%sDo not make any other call, do not retry, do not read saved outputs. Then return "
            "{\"done\": true}." % ("\n".join("%d. %s" % (i + 1, c) for i, c in enumerate(calls)), note, turn2))
    reads = read_tools(provider)
    res = mcpjobs.run("git-host-prs-" + provider, {srv: reads}, [mcpjobs.mcp_tool(srv, t) for t in reads],
                      [mcpjobs.mcp_tool(srv, t) for t in GIT_HOST_WRITE[provider]], task, DONE, cfg, health, day=day,
                      max_turns=4 if turn2 else 3, timeout=400)
    done = covered(provider, res, info, branches)
    if not done:
        err = next((c.get("result") for c in res.get("calls") or [] if c.get("is_error")), res.get("result"))
        raise mcpjobs.JobError("%s MCP returned nothing usable: %s" % (LABEL[provider], str(err)[:200]), skip=True)
    out = {b: [] for b in done}
    for r in records(provider, res, info):
        if r.get("source") in out:
            out[r["source"]].append(r)  # another branch's PR (someone else's work) is not kept
    return {"prs": out, "branches": done, "calls": len(res.get("calls") or []), "server": srv,
            "tools": sorted({str(c.get("name") or "").split("__")[-1] for c in res.get("calls") or []})}


def covered(provider, res, info, branches):
    """Branches whose list call came back without an error (the others stay due for the next refresh)."""
    reads = GIT_HOST_READ.get(provider) or {}
    got = set()
    for c in res.get("calls") or []:
        tool = str(c.get("name") or "").split("__")[-1]
        if tool not in reads or c.get("is_error") or c.get("result") is None:
            continue
        inp = c.get("input") if isinstance(c.get("input"), dict) else {}
        if provider == "azure-devops":
            b = _short(str(inp.get("sourceRefName") or ""))
        else:
            b = str(inp.get("head") or "").split(":", 1)[-1]
        if b:
            got.add(b)
    return [b for b in branches if b in got]
