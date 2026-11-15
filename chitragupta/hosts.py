"""Pull-request data for a work repo, without a model and without storing a token.

Sources, best first: the host's own CLI, signed in by the user (gh / glab / az repos), then the host's read-only MCP
server (hostmcp.py: Azure DevOps, GitHub), then the PR merge commits on the base branches (git only). Nothing here
writes to the repo or the host.

A PR record: {number, title, source, target, state (draft|open|merged|closed), review, created_at, merged_at,
merge_commit, head (current or final head sha), head_at_merge, url, src (gh|glab|az|mcp:<provider>|merge-commit)},
plus `ci` (check runs per head commit) on your own open GitHub PRs: see "CI checks" below.
"""
import hashlib
import json
import os
import re
import shutil
import time
from urllib.parse import quote, unquote

from . import common
from . import gitro
from .redact import redact
from .safety import commands

SAFE_ARG = re.compile(r"^[A-Za-z0-9._/@+=-]+$")  # branch names passed to a CLI (az is a .cmd file on Windows)
SAFE_NAME = re.compile(r"^[A-Za-z0-9._ @+=-]+$")  # org / project / repo names: spaces allowed, no shell characters


def detect(url):
    """Remote URL -> {"host": github|gitlab|azure|bitbucket|other, ...ids}. Credentials in the URL are dropped."""
    u = re.sub(r"//[^/@]+@", "//", (url or "").strip())
    m = re.search(r"(?:ssh\.dev\.azure\.com:v3/|vs-ssh\.visualstudio\.com:v3/)([^/]+)/([^/]+)/([^/\s]+?)(?:\.git)?$", u)
    if m:
        return {"host": "azure", "org": unquote(m.group(1)), "project": unquote(m.group(2)), "repo": unquote(m.group(3))}
    m = re.search(r"dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/\s?#]+)", u) or \
        re.search(r"([A-Za-z0-9-]+)\.visualstudio\.com/(?:DefaultCollection/)?([^/]+)/_git/([^/\s?#]+)", u)
    if m:
        return {"host": "azure", "org": unquote(m.group(1)), "project": unquote(m.group(2)), "repo": unquote(m.group(3))}
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", u)
    if m:
        return {"host": "github", "path": "%s/%s" % (m.group(1), m.group(2))}
    m = re.search(r"gitlab[^:/]*[:/](.+?)(?:\.git)?/?$", u)
    if m:
        return {"host": "gitlab", "path": re.sub(r"^\d+/", "", m.group(1).split("gitlab.com/")[-1])}
    if "bitbucket" in u:
        return {"host": "bitbucket"}
    return {"host": "other"}


def remote_info(cfg, repo, remote):
    url = gitro.ok(cfg, repo, ["remote", "get-url", remote]).strip()
    info = detect(url)
    info["remote"] = remote
    return info


def cli_path(cfg, name):
    p = ((cfg.get("paths") or {}).get(name) or "").strip()
    if p and os.path.exists(p):
        return p
    return shutil.which(name)


PR_SOURCES = ("auto", "cli", "mcp", "git")  # branching.repos.<repo>.pr_source ("" / absent = auto)


def pref_of(pref):
    """Configured pr_source -> auto | cli | mcp | git (older spellings of git-only kept)."""
    p = str(pref or "").strip().lower()
    if p in ("git", "merge-commit", "none"):
        return "git"
    return p if p in PR_SOURCES else "auto"


def source_for(cfg, info, pref=None):
    """Which PR source applies -> gh | glab | az | mcp:<provider> | merge-commit.
    auto: the host CLI if installed, else the host's MCP server if setup recorded one, else merge commits;
    cli / mcp: only that one, else merge commits; git: merge commits."""
    p = pref_of(pref)
    if p == "git":
        return "merge-commit"
    if p in ("auto", "cli"):
        cli = {"github": "gh", "gitlab": "glab", "azure": "az"}.get(info.get("host"))
        if cli and cli_path(cfg, cli):
            return cli
    if p in ("auto", "mcp"):
        from . import hostmcp
        provider, srv = hostmcp.server_for(cfg, info)
        if srv:
            return "mcp:" + provider
    return "merge-commit"


def _run(cmd, timeout=60):
    """A Git-host CLI call through the command guard (purpose `host`: list / view only)."""
    return commands.run(cmd, "host", timeout=timeout, env_extra={"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GLAB_NO_PROMPT": "1",
                                                        "AZURE_CORE_NO_COLOR": "1", "AZURE_CORE_ONLY_SHOW_ERRORS": "1"})


class SourceError(Exception):
    pass


def author_of(src, a):
    """A PR's author as the host reports it -> {"id", "node_id", "login", "is_bot"}, or None. Only what tells the
    author apart is kept (no display name, no email). gh's `author.id` is a GraphQL node id, so it goes to node_id;
    REST and GitLab ids are numeric ids; Azure DevOps has an id (GUID) and a uniqueName (the sign-in name)."""
    if not isinstance(a, dict):
        return None
    if src == "gh":
        out = {"id": "", "node_id": str(a.get("id") or ""), "login": str(a.get("login") or ""), "is_bot": bool(a.get("is_bot"))}
    elif src in ("az", "mcp:azure-devops"):
        out = {"id": str(a.get("id") or ""), "node_id": "", "login": str(a.get("uniqueName") or ""), "is_bot": False}
    else:  # glab, GitHub REST (mcp:github)
        out = {"id": str(a.get("id") or ""), "node_id": str(a.get("node_id") or ""),
               "login": str(a.get("username") or a.get("login") or ""),
               "is_bot": bool(a.get("bot")) or str(a.get("type") or "").lower() == "bot"}
    return out if (out["id"] or out["node_id"] or out["login"]) else None


# --------------------------------------------------------------------------- GitHub (gh)

GH_FIELDS = "number,title,state,baseRefName,headRefName,headRefOid,mergedAt,mergeCommit,isDraft,reviewDecision,url,createdAt,author"


def gh_prs(cfg, info, branch, run=_run):
    if branch is not None and not SAFE_ARG.match(branch or ""):
        return []
    rc, so, se = run([cli_path(cfg, "gh"), "pr", "list", "--repo", info["path"]] + (["--head", branch] if branch is not None else []) + ["--state", "all",
                      "--limit", "20" if branch is not None else "1000", "--json", GH_FIELDS])
    if rc != 0:
        raise SourceError("gh: " + (se or so).strip()[:200])
    return _gh_rows(so)


def _gh_rows(so):
    out = []
    for p in json.loads(so or "[]"):
        st = (p.get("state") or "").upper()
        state = "merged" if st == "MERGED" else "closed" if st == "CLOSED" else ("draft" if p.get("isDraft") else "open")
        rd = (p.get("reviewDecision") or "").lower()
        out.append({"number": p.get("number"), "title": p.get("title") or "", "source": p.get("headRefName"),
                    "target": p.get("baseRefName"), "state": state, "review": rd, "created_at": p.get("createdAt"),
                    "merged_at": p.get("mergedAt"), "merge_commit": (p.get("mergeCommit") or {}).get("oid"),
                    "head": p.get("headRefOid"), "head_at_merge": p.get("headRefOid") if state == "merged" else None,
                    "url": p.get("url"), "src": "gh", "author": author_of("gh", p.get("author"))})
    return out


# --------------------------------------------------------------------------- GitLab (glab)

def glab_prs(cfg, info, branch, run=_run):
    if branch is not None and not SAFE_ARG.match(branch or ""):
        return []
    rc, so, se = run([cli_path(cfg, "glab"), "mr", "list", "--repo", info["path"]] + (["--source-branch", branch] if branch is not None else ["--per-page", "100"]) + ["--all",
                      "--output", "json"])
    if rc != 0:
        raise SourceError("glab: " + (se or so).strip()[:200])
    return _glab_rows(so)


def _glab_rows(so):
    out = []
    for p in json.loads(so or "[]"):
        st = (p.get("state") or "").lower()
        state = "merged" if st == "merged" else "closed" if st in ("closed", "locked") else (
            "draft" if p.get("draft") or p.get("work_in_progress") else "open")
        out.append({"number": p.get("iid"), "title": p.get("title") or "", "source": p.get("source_branch"),
                    "target": p.get("target_branch"), "state": state, "review": "", "created_at": p.get("created_at"),
                    "merged_at": p.get("merged_at"),
                    "merge_commit": p.get("merge_commit_sha") or p.get("squash_commit_sha"),
                    "head": p.get("sha"), "head_at_merge": p.get("sha") if state == "merged" else None,
                    "url": p.get("web_url"), "src": "glab", "author": author_of("glab", p.get("author"))})
    return out


# --------------------------------------------------------------------------- Azure DevOps (az repos)

_VOTE = {10: "approved", 5: "approved", 0: "", -5: "changes_requested", -10: "changes_requested"}


def az_prs(cfg, info, branch, run=_run):
    if branch is not None and not SAFE_ARG.match(branch or ""):
        return []
    if not all(SAFE_NAME.match(info.get(k) or "") for k in ("org", "project", "repo")):
        raise SourceError("az: unexpected characters in the org / project / repo name")
    org = "https://dev.azure.com/" + info["org"]
    rc, so, se = run([cli_path(cfg, "az"), "repos", "pr", "list", "--org", org, "--project", info["project"],
                      "--repository", info["repo"]] + (["--source-branch", branch] if branch is not None else []) + ["--status", "all", "--top", "20" if branch is not None else "1000",
                      "--detect", "false", "--output", "json"], timeout=90)
    if rc != 0:
        raise SourceError("az: " + (se or so).strip()[:200])
    return _az_rows(so, org, info)


def _az_rows(so, org, info):
    out = []
    for p in json.loads(so or "[]"):
        st = (p.get("status") or "").lower()
        state = "merged" if st == "completed" else "closed" if st == "abandoned" else ("draft" if p.get("isDraft") else "open")
        votes = [int(r.get("vote") or 0) for r in p.get("reviewers") or [] if r.get("isRequired") or r.get("vote")]
        review = "changes_requested" if any(v < 0 for v in votes) else "approved" if votes and any(v > 0 for v in votes) else ""
        n = p.get("pullRequestId")
        out.append({"number": n, "title": p.get("title") or "", "source": _short(p.get("sourceRefName")),
                    "target": _short(p.get("targetRefName")), "state": state, "review": review,
                    "created_at": p.get("creationDate"), "merged_at": p.get("closedDate") if state == "merged" else None,
                    "merge_commit": (p.get("lastMergeCommit") or {}).get("commitId") if state == "merged" else None,
                    "head": (p.get("lastMergeSourceCommit") or {}).get("commitId"),
                    "head_at_merge": (p.get("lastMergeSourceCommit") or {}).get("commitId") if state == "merged" else None,
                    "url": "%s/%s/_git/%s/pullrequest/%s" % (org, quote(info["project"]), quote(info["repo"]), n),
                    "src": "az", "author": author_of("az", p.get("createdBy"))})
    return out


def _short(ref):
    return re.sub(r"^refs/heads/", "", ref or "")


CLI = {"gh": gh_prs, "glab": glab_prs, "az": az_prs}


def prs_for_branch(cfg, info, source, branch, run=_run):
    """PRs whose source is `branch` from the host CLI; [] for merge-commit mode. Raises SourceError."""
    fn = CLI.get(source)
    return fn(cfg, info, branch, run=run) if fn else []


def prs_for_repo(cfg, info, source, run=_run):
    """One bounded repository list; callers filter sources locally. Never declare a capped result complete."""
    fn = CLI.get(source)
    rows = fn(cfg, info, None, run=run) if fn else []
    limit = 100 if source == "glab" else 1000
    if len(rows) >= limit:
        raise SourceError("%s: repository PR list reached %d rows; previous PR checks retained" % (source,limit))
    return rows


# --------------------------------------------------------------------------- your own PRs (personal progress)

AUTHORED_LIMIT = 200  # one bounded list per repository; a full page means "partial", never "complete"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def authored_prs(cfg, info, source, account, since, run=_run):
    """The PRs `account` (your confirmed account on this host) authored, asked of the host itself: gh `--author
    <login>` with `updated:<since>..*`, glab `--author <username>`, az `--creator <id | sign-in name>`. Callers still
    recheck each author. -> (rows, capped). Raises SourceError; ValueError when the account lacks what the CLI needs."""
    login, aid = str(account.get("login") or ""), str(account.get("id") or "")
    if since and not _DAY.match(since):
        raise ValueError("since must be YYYY-MM-DD")
    if source == "gh":
        if not login or not SAFE_ARG.match(login):
            raise ValueError("gh filters by login: confirm your GitHub login in the profile")
        rc, so, se = run([cli_path(cfg, "gh"), "pr", "list", "--repo", info["path"], "--author", login, "--state", "all",
                          "--limit", str(AUTHORED_LIMIT)] + (["--search", "updated:%s..*" % since] if since else []) +
                         ["--json", GH_FIELDS])
        if rc != 0:
            raise SourceError("gh: " + (se or so).strip()[:200])
        rows = _gh_rows(so)
        return rows, len(rows) >= AUTHORED_LIMIT
    if source == "glab":
        if not login or not SAFE_ARG.match(login):
            raise ValueError("glab filters by username: confirm your GitLab username in the profile")
        rc, so, se = run([cli_path(cfg, "glab"), "mr", "list", "--repo", info["path"], "--author", login, "--all",
                          "--per-page", "100", "--output", "json"])
        if rc != 0:
            raise SourceError("glab: " + (se or so).strip()[:200])
        rows = _glab_rows(so)
        return rows, len(rows) >= 100
    if source == "az":
        who = aid or login
        if not who or not SAFE_ARG.match(who):
            raise ValueError("az filters by creator: confirm your Azure DevOps id or sign-in name in the profile")
        if not all(SAFE_NAME.match(info.get(k) or "") for k in ("org", "project", "repo")):
            raise SourceError("az: unexpected characters in the org / project / repo name")
        org = "https://dev.azure.com/" + info["org"]
        rc, so, se = run([cli_path(cfg, "az"), "repos", "pr", "list", "--org", org, "--project", info["project"],
                          "--repository", info["repo"], "--creator", who, "--status", "all", "--top",
                          str(AUTHORED_LIMIT), "--detect", "false", "--output", "json"], timeout=90)
        if rc != 0:
            raise SourceError("az: " + (se or so).strip()[:200])
        rows = _az_rows(so, org, info)
        return rows, len(rows) >= AUTHORED_LIMIT
    raise ValueError("no author-filtered list for %s" % source)


# --------------------------------------------------------------------------- merge commits (git only)

MERGE_RX = [
    re.compile(r"^Merged PR (\d+): (.*)$"),                                   # Azure DevOps
    re.compile(r"^Merge pull request #?(\d+) from (?:[^/\s]+/)?(\S+?)(?: into (\S+))?$"),  # GitHub / Azure (older)
    re.compile(r"^Merge branch '([^']+)' into '([^']+)'$"),                   # GitLab (with "See merge request !N" in the body)
]
_GL_MR = re.compile(r"See merge request [^!\s]*!(\d+)")


def merge_commits(cfg, repo, target_ref, target, since):
    """PR merges (merge or squash commits) on the first-parent history of target_ref since `since` (iso): [{number, title, source?, target,
    merge_commit, head_at_merge, merged_at, state: merged, src: merge-commit}] newest first."""
    so = gitro.ok(cfg, repo, ["log", target_ref, "--first-parent", "--since=" + since, "--grep=^Merge", "--grep=^Merged PR",
                              "--format=%H%x1f%P%x1f%cI%x1f%s%x1f%b%x1e"], timeout=90)
    out = []
    for rec in so.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 5:
            continue
        sha, parents, when, subj, body = parts[0].strip(), parts[1].split(), parts[2], parts[3], parts[4]
        num, title, source = None, subj, None
        m = MERGE_RX[0].match(subj)
        if m:
            num, title = int(m.group(1)), m.group(2)
        else:
            m = MERGE_RX[1].match(subj)
            if m:
                num, source, title = int(m.group(1)), m.group(2), (body.strip().splitlines() or [subj])[0]
            else:
                m = MERGE_RX[2].match(subj)
                gl = _GL_MR.search(body or "")
                if m and gl:
                    num, source, title = int(gl.group(1)), m.group(1), (body.strip().splitlines() or [subj])[0]
        if num is None:
            continue
        out.append({"number": num, "title": title.strip(), "source": source, "target": target, "state": "merged",
                    "review": "", "merged_at": when, "merge_commit": sha,
                    "head": parents[1] if len(parents) > 1 else None,  # one parent: a squash merge, head unknown
                    "head_at_merge": parents[1] if len(parents) > 1 else None, "url": None, "src": "merge-commit"})
    return out


# --------------------------------------------------------------------------- CI checks (FLOW-03, read-only)
#
# One provider: GitHub check runs through `gh api --method GET` (the guard allows api GET only). For an open PR you
# raised whose head commit is known, and only right after the tick read that PR (same interval, rotation, pre-send
# check):  GET repos/<owner>/<repo>/commits/<head>/check-runs?filter=all&per_page=100&page=<n>, n <= CI_PAGES.
# filter=all keeps every attempt (a re-run is a new check run with its own id). Stored per head on the PR fact
# (prs.json `ci`): minimal sanitized fields; never logs, output text or URLs; never a rerun, cancel or any write.

CI_PAGE = 100     # check runs per page (GitHub's maximum)
CI_PAGES = 3      # the first page and at most 2 more: 300 check runs per commit, then "pagination cut"
CI_HEADS = 3      # check sets kept per PR: its current head and the two before it (history, never current)
CI_PRS = 5        # PRs read per repository refresh, least recently read first
CI_TIMEOUT = 30   # seconds per page
CI_BUDGET = 60    # seconds for every CI page of one repository refresh
CI_OUTCOME = {"success": "passed", "failure": "failed", "startup_failure": "failed", "cancelled": "interrupted",
              "timed_out": "interrupted", "action_required": "action_required", "neutral": "neutral",
              "skipped": "skipped", "stale": "stale"}
CI_RUNNING = {"queued", "in_progress", "waiting", "requested", "pending"}
CI_ATTENTION = {"failed", "interrupted", "action_required"}
CI_MATRIX = {  # provider -> what is read (the supported-provider matrix in the docs)
    "github": "check runs of the PR head commit through gh api (GET, filter=all, 100 per page, at most 3 pages)",
    "gitlab": "not implemented for this host", "azure": "not implemented for this host",
    "bitbucket": "no PR source", "other": "no PR source"}
NOT_YOURS = "not a PR you raised; checks are read only for your own PRs"
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_OWNER_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_WHEN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_CI_TEXT = {"failing": "CI checks need attention on head {h}: {n} of {m}",
            "running": "CI checks still running on head {h}: {n} of {m}",
            "passed": "CI checks passed on head {h} ({m} checks)",
            "neutral": "CI checks finished without a pass or a failure on head {h}",
            "partial": "CI checks partly read on head {h}: {r}",
            "missing": "No CI checks reported for head {h}",
            "unknown": "CI check results unclear on head {h}"}


def ci_capability(source, head=None, mine=None):
    """Can CI checks be read for a PR from `source`? Pure. -> {"state": available|unavailable, "provider", "reason",
    "text"}. GitLab, Azure DevOps: not implemented for this host. GitHub (gh): only your own PR (`mine` True, as
    profile.Policy.pr_mine decides: stable ids or the exact login, never a name) with a known head commit."""
    src = str(source or "")
    provider = {"gh": "github", "glab": "gitlab", "az": "azure"}.get(src) or (src[4:] if src.startswith("mcp:") else "")
    if src == "gh":
        reason = "no confirmed GitHub account in your profile, or the PR has no author" if mine is None else \
            NOT_YOURS if mine is False else \
            "the PR's head commit is not known" if not _SHA.match(str(head or "").lower()) else None
    elif src in ("glab", "az"):
        reason = "not implemented for this host"
    elif src.startswith("mcp:"):
        reason = "not implemented for PRs read through the host's MCP server"
    elif src == "merge-commit":
        reason = "no host CLI: PRs are known only from merge commits"
    else:
        reason = "no PR source"
    if reason:
        return {"state": "unavailable", "provider": provider, "reason": reason, "text": "CI checks unavailable: " + reason}
    return {"state": "available", "provider": "github", "reason": "", "text": "GitHub check runs"}


def _clean(value, limit):
    return re.sub(r"[\x00-\x1f\x7f]", " ", redact(str(value or "")))[:limit]


def _when(value):
    return value if isinstance(value, str) and _WHEN.match(value) else None


def _ci_run(c, head):
    """One check run -> its stored fields; None when it is not a check run of `head`."""
    if not isinstance(c, dict) or not isinstance(c.get("id"), int) or isinstance(c.get("id"), bool) \
            or str(c.get("head_sha") or "").lower() != head:
        return None
    status, conclusion = str(c.get("status") or "").lower(), str(c.get("conclusion") or "").lower()
    outcome = "running" if status in CI_RUNNING else CI_OUTCOME.get(conclusion, "unknown") if status == "completed" \
        else "unknown"
    app = c.get("app") if isinstance(c.get("app"), dict) else {}
    run = {"id": c["id"], "name": _clean(c.get("name"), 200),
           "status": status if status in CI_RUNNING or status == "completed" else "unknown",
           "conclusion": conclusion if conclusion in CI_OUTCOME else None, "outcome": outcome,
           "started_at": _when(c.get("started_at")), "completed_at": _when(c.get("completed_at")), "head_sha": head,
           "app": _clean(app.get("slug"), 100)}
    att = c.get("run_attempt")
    if isinstance(att, int) and not isinstance(att, bool) and att >= 1:
        run["attempt"], run["attempt_basis"] = att, "provider"
    return run


def _attempts(runs):
    """run_attempt when the provider states it, else the order of the check runs with the same app and name on the
    commit (ids only grow, so a re-run is attempt 2). -> runs by id."""
    seen = {}
    for x in sorted(runs, key=lambda x: x["id"]):
        k = (x.get("app"), x.get("name"))
        seen[k] = seen.get(k, 0) + 1
        if x.get("attempt_basis") != "provider":
            x["attempt"], x["attempt_basis"] = seen[k], "order"
    return sorted(runs, key=lambda x: x["id"])


def _ci_failure(rc, so, se):
    """A failed page -> rate-limited | permission denied | not found | timeout | error. No host text is kept."""
    text = "%s %s" % (se or "", so or "")
    if re.search(r"(?i)rate limit|HTTP 429", text):
        return "rate-limited"
    if re.search(r"HTTP 40[13]\b", text):
        return "permission denied"
    if re.search(r"HTTP 404\b", text):
        return "not found"
    return "timeout" if rc == 124 else "error"


def _ci_read(runs, total, reason):
    rows = sorted(runs.values(), key=lambda x: x["id"])
    return {"runs": rows, "total": total, "reason": reason,
            "coverage": "complete" if not reason else "partial" if rows else "unavailable"}


def gh_check_runs(cfg, info, head, run=_run, deadline=None):
    """One bounded read of a commit's check runs (GET only, at most CI_PAGES pages). -> {"runs", "total",
    "coverage": complete|partial|unavailable, "reason"}: host errors, the guard and the page cap become a reason,
    never an exception and never an empty "no checks"."""
    path, head = str(info.get("path") or ""), str(head or "").lower()
    if not _OWNER_REPO.match(path) or not _SHA.match(head):
        return _ci_read({}, None, "repository or head commit not recognised")
    exe = cli_path(cfg, "gh")
    if not exe:
        return _ci_read({}, None, "gh is not installed")
    runs, total, dropped = {}, None, 0
    for page in range(1, CI_PAGES + 1):
        left = CI_TIMEOUT if deadline is None else min(CI_TIMEOUT, deadline - time.monotonic())
        if left < 1:
            return _ci_read(runs, total, "time budget reached")
        argv = [exe, "api", "--method", "GET", "repos/%s/commits/%s/check-runs?filter=all&per_page=%d&page=%d"
                % (path, head, CI_PAGE, page)]
        try:
            rc, so, se = run(argv, timeout=int(left))
        except PermissionError:  # the command guard refused it (and logged why)
            return _ci_read(runs, total, "refused by the command guard")
        except OSError:
            return _ci_read(runs, total, "error")
        if rc != 0:
            return _ci_read(runs, total, _ci_failure(rc, so, se))
        try:
            body = json.loads(so or "")
        except ValueError:
            body = None
        rows = body.get("check_runs") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not isinstance(body.get("total_count"), int):
            return _ci_read(runs, total, "unreadable response")
        total = body["total_count"]
        for c in rows:
            x = _ci_run(c, head)
            if x:
                runs[x["id"]] = x
            else:
                dropped += 1
        if len(rows) < CI_PAGE or page * CI_PAGE >= total:
            return _ci_read(runs, total, "unrecognised check runs skipped" if dropped else "")
    return _ci_read(runs, total, "pagination cut")


def ci_sig(ci):
    """Changes only when what the checks say changes, never on the read times alone (no ticket churn)."""
    keep = [ci.get("head"), (ci.get("capability") or {}).get("reason")] + [
        [h.get("head"), h.get("coverage"), h.get("reason"), h.get("stale"), h.get("total"), h.get("runs")]
        for h in ci.get("heads") or [] if isinstance(h, dict)]
    return hashlib.sha1(json.dumps(keep, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _heads(pr):
    ci = pr.get("ci") if isinstance(pr.get("ci"), dict) else {}
    return [h for h in ci.get("heads") or [] if isinstance(h, dict) and h.get("head")]


def ci_record(pr, info, read, at):
    """Store one read on the PR fact for its current head. A failed or cut read keeps the runs already stored for
    that head (stale / partial), never "no checks"; distinct attempts stay distinct; the same response read again
    changes nothing but the read times. Earlier heads keep their runs as history (at most CI_HEADS heads)."""
    head, heads = str(pr.get("head") or "").lower(), _heads(pr)
    old = next((h for h in heads if h["head"] == head), None) or {}
    fresh = {x["id"]: x for x in read["runs"]}
    complete = read["coverage"] == "complete"
    runs = {} if complete else {x["id"]: x for x in old.get("runs") or [] if isinstance(x, dict)
                                and isinstance(x.get("id"), int) and not isinstance(x.get("id"), bool)}
    kept = set(runs) - set(fresh)
    runs.update(fresh)
    block = {"head": head, "observed_at": at if complete or fresh else old.get("observed_at"), "attempted_at": at,
             "coverage": "complete" if complete else "partial" if runs else "unavailable", "reason": read["reason"],
             "stale": bool(kept), "total": read["total"] if read["total"] is not None else old.get("total"),
             "runs": _attempts([dict(x) for x in runs.values()])}
    out = {"provider": "github", "repo": str(info.get("path") or ""), "head": head,
           "capability": ci_capability("gh", head, True),
           "heads": ([block] + [h for h in heads if h["head"] != head])[:CI_HEADS]}
    out["sig"] = ci_sig(out)
    pr["ci"] = out
    return out


def ci_skip(pr, info, cap):
    """A GitHub PR whose checks are not read: why, and nothing kept of someone else's checks."""
    out = {"provider": "github", "repo": str(info.get("path") or ""), "head": str(pr.get("head") or "").lower() or None,
           "capability": cap, "heads": [] if cap["reason"] == NOT_YOURS else _heads(pr)}
    out["sig"] = ci_sig(out)
    pr["ci"] = out
    return out


def _last_read(pr):
    head = str(pr.get("head") or "").lower()
    return next((str(h.get("attempted_at") or "") for h in _heads(pr) if h["head"] == head), "")


def ci_refresh(cfg, info, source, prs, run=_run, now_iso=None):
    """From the tick, right after the PR read of these PRs' branches (so its interval, rotation and pre-send check):
    GitHub check runs of your own open PRs, at most CI_PRS PRs and CI_BUDGET seconds per repository. Other hosts are
    a capability state only (ci_capability). Never raises for one PR. -> number of PRs read."""
    if source != "gh":
        return 0
    from . import profile
    pol = profile.Policy(cfg)
    prov, inst = profile.host_instance({"host": "github"})
    at, deadline, todo = now_iso or common.now_iso(), time.monotonic() + CI_BUDGET, []
    for p in prs:
        if p.get("state") not in ("open", "draft"):
            continue
        cap = ci_capability(source, p.get("head"), pol.pr_mine(p.get("author"), prov, inst))
        if cap["state"] == "available":
            todo.append(p)
        else:
            ci_skip(p, info, cap)
    todo.sort(key=lambda p: (_last_read(p), str(p.get("number"))))
    n = 0
    for p in todo[:CI_PRS]:
        if time.monotonic() >= deadline:
            break
        try:
            ci_record(p, info, gh_check_runs(cfg, info, p["head"], run=run, deadline=deadline), at)
            n += 1
        except Exception as e:  # noqa: BLE001 - one PR's checks never stop the others or the PR refresh
            common.log("agent", "CI checks for PR #%s: %s" % (p.get("number"), e))
    return n


def _verdict(block):
    """(state, counts by outcome, checks) from the latest attempt of each check on one head."""
    latest = {}
    for x in block.get("runs") or []:
        if isinstance(x, dict):
            k = (x.get("app"), x.get("name"))
            if k not in latest or (x.get("attempt") or 0, x.get("id") or 0) > \
                    (latest[k].get("attempt") or 0, latest[k].get("id") or 0):
                latest[k] = x
    counts = {}
    for x in latest.values():
        o = x.get("outcome") or "unknown"
        counts[o] = counts.get(o, 0) + 1
    complete = block.get("coverage") == "complete"
    if not latest:
        state = "missing" if complete else "unavailable"
    elif set(counts) & CI_ATTENTION:
        state = "failing"
    elif "running" in counts:
        state = "running"
    elif not complete:
        state = "partial"
    elif set(counts) <= {"passed", "neutral", "skipped"}:
        state = "passed" if "passed" in counts else "neutral"
    else:
        state = "unknown"
    return state, counts, len(latest)


def ci_state(pr):
    """The CI state of a PR's CURRENT head. Pure; for views, resume and evidence. Runs on any other head never count
    (A14: an earlier head's success cannot clear a missing or failing check now); they are listed as history.
    -> {"state": failing|running|passed|neutral|partial|missing|unavailable|unknown, "text", "head", "coverage",
    "reason", "stale", "counts", "checks", "observed_at", "absent", "history"}. Unread or unreadable is never "passed";
    a check the previous head had and a complete read of this head lacks makes it "missing" (`absent`)."""
    pr = pr if isinstance(pr, dict) else {}
    ci = pr.get("ci") if isinstance(pr.get("ci"), dict) else {}
    head = str(pr.get("head") or ci.get("head") or "").lower()
    heads = _heads(pr)
    out = {"head": head or None, "coverage": None, "reason": "", "stale": False, "counts": {}, "checks": 0,
           "observed_at": None, "absent": [], "history": [{"head": h["head"], "state": _verdict(h)[0], "observed_at": h.get("observed_at")}
                                            for h in heads if h["head"] != head]}
    cap = ci.get("capability") if isinstance(ci.get("capability"), dict) else None
    if cap is None:
        cap = ci_capability(pr.get("src"), head, None)
        if pr.get("src") == "gh":
            why = "not read yet" if pr.get("state") in ("open", "draft") else "checks are read only while a PR is open"
            cap = dict(cap, reason=why, text="CI checks unavailable: " + why)
    block = next((h for h in heads if h["head"] == head), None) if cap.get("state") == "available" else None
    if cap.get("state") != "available" or not block:
        why = cap.get("reason") if cap.get("state") != "available" else "not read for head %s yet" % head[:7]
        return dict(out, state="unavailable", reason=why, text="CI checks unavailable: " + why)
    state, counts, checks = _verdict(block)
    # checks the previous head had but a complete read of this head lacks: missing now, whatever they said before
    prev = next((h for h in heads if h["head"] != head), None)
    have = {(x.get("app"), x.get("name")) for x in block.get("runs") or [] if isinstance(x, dict)}
    absent = sorted({str(x.get("name") or "") for x in (prev or {}).get("runs") or [] if isinstance(x, dict)
                     and (x.get("app"), x.get("name")) not in have}) if block.get("coverage") == "complete" else []
    if absent and state in ("passed", "neutral"):
        state = "missing"
    n = sum(counts.get(o, 0) for o in CI_ATTENTION) if state == "failing" else counts.get("running", 0)
    reason = block.get("reason") or ""
    if state == "unavailable":
        text = "CI checks unavailable: " + (reason or "no runs read")
    elif state == "missing" and absent:
        text = "CI checks missing on head %s: %d reported on an earlier head (%s)" % (
            head[:7], len(absent), ", ".join(absent[:5]) + (", …" if len(absent) > 5 else ""))
    else:
        text = _CI_TEXT[state].format(h=head[:7], n=n, m=checks, r=reason or "partial")
    if block.get("stale"):
        text += " (last read: %s; earlier runs kept)" % (reason or "incomplete")
    return dict(out, state=state, text=text, coverage=block.get("coverage"), reason=reason, stale=bool(block.get("stale")),
                counts=counts, checks=checks, observed_at=block.get("observed_at"), absent=absent[:20])
