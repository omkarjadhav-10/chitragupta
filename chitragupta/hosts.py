"""Pull-request data for a work repo, without a model and without storing a token.

Sources, best first: the host's own CLI, signed in by the user (gh / glab / az repos), then the PR merge commits
on the base branches (git only). Nothing here writes to the repo or the host.

A PR record: {number, title, source, target, state (draft|open|merged|closed), review, merged_at, merge_commit,
head (current or final head sha), head_at_merge, url, src (gh|glab|az|merge-commit)}.
"""
import json
import os
import re
import shutil
from urllib.parse import quote, unquote

from . import common
from . import gitro

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


def source_for(cfg, info, pref=None):
    """Which PR source applies: the configured one, else the host CLI if installed, else merge-commit."""
    if pref in ("git", "merge-commit", "none"):
        return "merge-commit"
    cli = {"github": "gh", "gitlab": "glab", "azure": "az"}.get(info.get("host"))
    if cli and cli_path(cfg, cli):
        return cli
    return "merge-commit"


def _run(cmd, timeout=60):
    return common.run(cmd, timeout=timeout, env_extra={"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GLAB_NO_PROMPT": "1",
                                                        "AZURE_CORE_NO_COLOR": "1", "AZURE_CORE_ONLY_SHOW_ERRORS": "1"})


class SourceError(Exception):
    pass


# --------------------------------------------------------------------------- GitHub (gh)

GH_FIELDS = "number,title,state,baseRefName,headRefName,headRefOid,mergedAt,mergeCommit,isDraft,reviewDecision,url,createdAt"


def gh_prs(cfg, info, branch, run=_run):
    if not SAFE_ARG.match(branch or ""):
        return []
    rc, so, se = run([cli_path(cfg, "gh"), "pr", "list", "--repo", info["path"], "--head", branch, "--state", "all",
                      "--limit", "20", "--json", GH_FIELDS])
    if rc != 0:
        raise SourceError("gh: " + (se or so).strip()[:200])
    out = []
    for p in json.loads(so or "[]"):
        st = (p.get("state") or "").upper()
        state = "merged" if st == "MERGED" else "closed" if st == "CLOSED" else ("draft" if p.get("isDraft") else "open")
        rd = (p.get("reviewDecision") or "").lower()
        out.append({"number": p.get("number"), "title": p.get("title") or "", "source": p.get("headRefName"),
                    "target": p.get("baseRefName"), "state": state, "review": rd, "created_at": p.get("createdAt"),
                    "merged_at": p.get("mergedAt"), "merge_commit": (p.get("mergeCommit") or {}).get("oid"),
                    "head": p.get("headRefOid"), "head_at_merge": p.get("headRefOid") if state == "merged" else None,
                    "url": p.get("url"), "src": "gh"})
    return out


# --------------------------------------------------------------------------- GitLab (glab)

def glab_prs(cfg, info, branch, run=_run):
    if not SAFE_ARG.match(branch or ""):
        return []
    rc, so, se = run([cli_path(cfg, "glab"), "mr", "list", "--repo", info["path"], "--source-branch", branch, "--all",
                      "--output", "json"])
    if rc != 0:
        raise SourceError("glab: " + (se or so).strip()[:200])
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
                    "url": p.get("web_url"), "src": "glab"})
    return out


# --------------------------------------------------------------------------- Azure DevOps (az repos)

_VOTE = {10: "approved", 5: "approved", 0: "", -5: "changes_requested", -10: "changes_requested"}


def az_prs(cfg, info, branch, run=_run):
    if not SAFE_ARG.match(branch or ""):
        return []
    if not all(SAFE_NAME.match(info.get(k) or "") for k in ("org", "project", "repo")):
        raise SourceError("az: unexpected characters in the org / project / repo name")
    org = "https://dev.azure.com/" + info["org"]
    rc, so, se = run([cli_path(cfg, "az"), "repos", "pr", "list", "--org", org, "--project", info["project"],
                      "--repository", info["repo"], "--source-branch", branch, "--status", "all", "--top", "20",
                      "--detect", "false", "--output", "json"], timeout=90)
    if rc != 0:
        raise SourceError("az: " + (se or so).strip()[:200])
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
                    "src": "az"})
    return out


def _short(ref):
    return re.sub(r"^refs/heads/", "", ref or "")


CLI = {"gh": gh_prs, "glab": glab_prs, "az": az_prs}


def prs_for_branch(cfg, info, source, branch, run=_run):
    """PRs whose source is `branch` from the host CLI; [] for merge-commit mode. Raises SourceError."""
    fn = CLI.get(source)
    return fn(cfg, info, branch, run=run) if fn else []


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
