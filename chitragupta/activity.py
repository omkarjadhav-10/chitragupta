"""Your own commits and pull requests per repository, for personal progress: data/activity/<repo_key>.json.

Written by the tick step "activity" (after "profile"), read by progress.py. Only what profile.Policy says is yours is
kept: the provider narrows first (git `--author` / `--grep`, the host's `--author` / `--creator`), then every result is
rechecked exactly before it is stored.

  commits   role author: authored by a confirmed email; role coauthor: someone else's commit listing a confirmed
            email as an exact `Co-authored-by` trailer. Merge commits never enter (`--no-merges`). Per-commit
            `--numstat` with `--root` (a root commit counts its whole tree) and rename detection; binary files count
            0 lines; paths matching progress.exclude_globs are tallied apart, never dropped silently.
            state: counted | equivalent (same patch-id and author time as a counted copy: a rebase or cherry-pick) |
            integration (the squash commit of one of your merged PRs whose head is a counted commit) |
            unreachable (not on any ref after a successful full scan: amended, rewritten, deleted) |
            future (author time more than an hour ahead: never moved into today)
  prs       authored by your confirmed account on the repo's host instance; one identity per (instance, repo,
            number), so a reopen or a draft turning ready is the same PR. MCP-only hosts give only the PRs of tracked
            branches (partial); merge commits carry no author (unavailable, never zero).

The ledger carries profile.signature: after an identity or scope change it is rebuilt from scratch, and readers
ignore a ledger whose signature differs. History is bounded by profile.window_start. Re-running a scan rewrites the
same items (idempotent); a failed scan keeps the last good items and records the error. Committer emails and other
people's identities are never stored.
"""
import datetime as _dt
import fnmatch
import json
import os
import re

from . import common
from . import gitro
from . import profile
from .redact import redact

V = 1
INCREMENTAL_S = 15 * 60  # new commits
FULL_S = 3600            # a full-window rescan: what is still reachable
PR_S = 3600              # your PR list
FUTURE_SLACK = _dt.timedelta(hours=1)
_RS, _US, _TS = "\x1e", "\x1f", "\x1d"
_FMT = ("--format=" + _RS + "%H" + _US + "%ae" + _US + "%aI" + _US + "%cI" + _US + "%S" + _US +
        "%(trailers:key=Co-authored-by,valueonly,separator=%x1d)" + _US + "%s" + _US)
_ADDR = re.compile(r"<\s*([^<>\s]+@[^<>\s]+)\s*>")


def ledger_path(key):
    return common.hp("data", "activity", key + ".json")


def new_ledger(repo=None):
    return {"v": V, "repo": repo or {}, "profile_sig": "", "window_start": "",
            "commits": {"status": "unconfigured", "why": "", "scanned_at": "", "full_at": "", "since": "", "error": "",
                        "items": {}},
            "prs": {"status": "unconfigured", "why": "", "source": "", "account": "", "fetched_at": "", "error": "",
                    "capped": False, "items": {}}}


def load(path_or_key):
    """The ledger of a repo (path or repo key), or None."""
    from . import gitcmd
    key = gitcmd.repo_key(path_or_key) if ("/" in path_or_key or "\\" in path_or_key) else path_or_key
    led = common.read_json(ledger_path(key), None)
    return led if isinstance(led, dict) and led.get("v") == V else None


# --------------------------------------------------------------------------- repository identity

def canonical_id(info, path):
    """Stable across clones and folder names: the host's own identity of the repository, else the folder."""
    h = (info or {}).get("host")
    if h == "github" and info.get("path"):
        return "github:github.com/" + info["path"].lower()
    if h == "azure" and info.get("org"):
        return "azure:dev.azure.com/%s/%s/%s" % (info["org"], info.get("project", ""), info.get("repo", ""))
    if h == "gitlab" and info.get("path"):
        return "gitlab:%s/%s" % (info.get("instance") or "gitlab.com", info["path"].lower())
    return "path:" + common.norm_path(os.path.realpath(path))


def repo_identity(cfg, path):
    from . import gitcmd, hosts
    remote = gitcmd.repo_conf(cfg, gitcmd.repo_key(path)).get("remote") or "origin"
    try:
        info = hosts.remote_info(cfg, path, remote)
    except Exception:  # noqa: BLE001 - no remote: the folder identity
        info = {"host": "other"}
    prov, inst = profile.host_instance(info)
    return {"id": canonical_id(info, path), "path": path.replace("\\", "/"), "name": os.path.basename(path.rstrip("/\\")),
            "key": gitcmd.repo_key(path), "provider": prov or "", "instance": inst or "",
            "remote": {k: v for k, v in info.items() if k in ("host", "org", "project", "repo", "path", "remote")}}


# --------------------------------------------------------------------------- numstat

def stat_path(p):
    """A numstat path in its final form: quotes removed, renames ("a/{x => y}/b", "x => y") resolved to the new path."""
    p = p.strip()
    if len(p) >= 2 and p[0] == p[-1] == '"':
        p = p[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    p = re.sub(r"\{([^{}]*) => ([^{}]*)\}", lambda m: m.group(2), p)
    if " => " in p:
        p = p.split(" => ", 1)[1]
    return re.sub(r"/{2,}", "/", p).strip("/")


def excluded(path, globs):
    """True when a path matches one of progress.exclude_globs: "dir/**" anywhere in the path, a pattern with a slash
    against the whole path, any other against the file name. Case-insensitive."""
    p = path.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    for g in globs or []:
        g = str(g).replace("\\", "/").lower().strip()
        if not g:
            continue
        if g.endswith("/**"):
            d = g[:-3].strip("/")
            if p.startswith(d + "/") or ("/" + d + "/") in ("/" + p):
                return True
        elif "/" in g:
            if fnmatch.fnmatchcase(p, g):
                return True
        elif fnmatch.fnmatchcase(base, g):
            return True
    return False


def numstat(blob, globs):
    """{added, deleted, files, binary_files, excluded: {added, deleted, files}} of one commit's numstat lines."""
    out = {"added": 0, "deleted": 0, "files": 0, "binary_files": 0, "excluded": {"added": 0, "deleted": 0, "files": 0}}
    for line in (blob or "").splitlines():
        bits = line.split("\t", 2)
        if len(bits) != 3:
            continue
        a, d, path = bits
        out["files"] += 1
        if a == "-" or d == "-":  # binary: no line counts, its size never becomes "lines"
            out["binary_files"] += 1
            continue
        try:
            a, d = int(a), int(d)
        except ValueError:
            continue
        if excluded(stat_path(path), globs):
            ex = out["excluded"]
            ex["added"] += a
            ex["deleted"] += d
            ex["files"] += 1
        else:
            out["added"] += a
            out["deleted"] += d
    return out


# --------------------------------------------------------------------------- commits

def _log(cfg, path, since_iso, extra, timeout=90):
    args = ["log", "--exclude=refs/stash", "--all", "--source", "--no-merges", "--root", "-M", "--numstat",
            "--since=" + since_iso, _FMT, "--regexp-ignore-case"] + extra
    return gitro.run(cfg, path, args, timeout=timeout)


def _parse(so):
    for rec in (so or "").split(_RS):
        parts = rec.split(_US)
        if len(parts) < 8 or not parts[0].strip():
            continue
        yield {"sha": parts[0].strip(), "ae": parts[1].strip().lower(), "adate": parts[2].strip(),
               "cdate": parts[3].strip(), "ref": parts[4].strip(), "coauthors": _coauthors(parts[5]),
               "subject": parts[6], "stat": parts[7]}


def _coauthors(blob):
    out = []
    for v in (blob or "").split(_TS):
        m = _ADDR.search(v)
        e = m.group(1) if m else v.strip()
        e = profile.norm_email(e)
        if e:
            out.append(e)
    return out


def scan(cfg, pol, path, since_iso, globs, prefixes):
    """-> (ok, {sha: item}, error). Two read-only walks: your authored commits, then commits naming you only as an
    exact Co-authored-by. Every result is rechecked against the confirmed emails."""
    from .sources import stash_commit
    emails = sorted(pol.emails)
    found = {}
    for role, extra in (("author", ["--author=" + re.escape(e) for e in emails]),
                        ("coauthor", ["--grep=" + re.escape(e) for e in emails])):
        rc, so, se = _log(cfg, path, since_iso, extra)
        if rc != 0:
            return False, {}, ("git log: " + (se or "failed").strip())[:300]
        for c in _parse(so):
            got = pol.commit_role(c["ae"], c["coauthors"])
            if got != role or c["sha"] in found:
                continue
            ref = re.sub(r"^refs/(heads|remotes/[^/]+)/", "", c["ref"])
            if stash_commit(c["ref"], c["subject"]):
                continue
            tickets = common.find_tickets(c["subject"], prefixes)
            st = numstat(c["stat"], globs)
            found[c["sha"]] = dict(st, sha=c["sha"], author_date=c["adate"], committed_at=c["cdate"], role=role,
                                   coauthored=role == "author" and any(not pol.email_mine(e) for e in c["coauthors"]),
                                   subject=redact(c["subject"].strip())[:200],
                                   ticket=tickets[0] if tickets else common.ticket_from_branch(ref, prefixes),
                                   reachable=True)
    return True, found, ""


def _merge(items, found, since_iso, full, nowiso=None):
    """Fold a scan into the items. A full scan also settles reachability: an item it didn't return is unreachable
    when it was committed inside the scanned range, and forgotten when it fell out of the history window."""
    out = {}
    since = common.parse_ts(since_iso)
    for sha, old in items.items():
        if sha in found:
            continue
        if full:
            when = common.parse_ts(old.get("committed_at"))
            if when is None or since is None or when < since:
                continue  # outside the window now
            old = dict(old, reachable=False)
        out[sha] = old
    for sha, it in found.items():
        prev = items.get(sha) or {}
        out[sha] = dict(it, patch_id=prev.get("patch_id", ""), first_seen=prev.get("first_seen") or nowiso or common.now_iso())
    return out


def settle(items, prs, now, patch_ids=None):
    """Recompute every item's state from scratch (so it is the same however often it runs): future, equivalent,
    integration, unreachable, else counted."""
    for it in items.values():
        it["state"], it["equivalent_to"], it["integration_of"] = "counted", "", ""
    limit = now + FUTURE_SLACK
    for it in items.values():
        when = common.parse_ts(it.get("author_date"))
        if when is None or when > limit:
            it["state"] = "future"
    if patch_ids:
        for sha, pid in patch_ids.items():
            if sha in items and pid:
                items[sha]["patch_id"] = pid
    # one counted copy per (role, patch-id, author time); the rest are its rebased / cherry-picked copies
    groups = {}
    for sha, it in items.items():
        if it["state"] == "future" or not it.get("patch_id"):
            continue
        groups.setdefault((it["role"], it["patch_id"], it.get("author_date")), []).append(sha)
    for shas in groups.values():
        live = sorted(s for s in shas if items[s].get("reachable", True))
        keep = live[0] if live else None
        for s in sorted(shas):
            if keep and s != keep:
                items[s]["state"], items[s]["equivalent_to"] = "equivalent", keep
    for it in items.values():
        if it["state"] == "counted" and not it.get("reachable", True):
            it["state"] = "unreachable"
    # a squash / rebase merge commit of your own merged PR whose head you already count: the same work once
    for p in (prs or {}).values():
        mc, head = p.get("merge_commit"), p.get("head")
        if p.get("state") != "merged" or not mc or not head or mc == head:
            continue
        it, h = items.get(mc), items.get(head)
        if it and h and it["state"] == "counted" and h["state"] in ("counted", "equivalent"):
            it["state"], it["integration_of"] = "integration", "PR #%s" % p.get("number")
    return items


def _patch_ids(cfg, path, items):
    """Patch-ids of reachable commits not known yet, through branch tracking's cached helper (patch-ids.json)."""
    from . import gittrack
    want = [s for s, it in items.items() if it.get("reachable", True) and not it.get("patch_id")]
    if not want:
        return {}
    r = gittrack.Repo(cfg, path)
    got = r.patch_ids(want)
    r.data.save()
    return got


# --------------------------------------------------------------------------- PRs

def _pr_item(p, prefixes):
    tickets = common.find_tickets("%s %s" % (p.get("source") or "", p.get("title") or ""), prefixes)
    return {"number": p.get("number"), "title": redact(str(p.get("title") or ""))[:200], "url": p.get("url") or "",
            "state": p.get("state") or "", "draft": p.get("state") == "draft", "review": p.get("review") or "",
            "created_at": p.get("created_at") or "", "merged_at": p.get("merged_at") or "", "target": p.get("target") or "",
            "branch": p.get("source") or "", "ticket": tickets[0] if tickets else None,
            "merge_commit": p.get("merge_commit") or "", "head": p.get("head") or ""}


def collect_prs(cfg, pol, repo, prs, window, run=None, now=None):
    """Refresh prs (the ledger section) in place from an author-filtered request; never widens to everyone's PRs."""
    from . import gitcmd, hosts
    prefixes = cfg.get("ticket_prefixes") or []
    prov, inst = repo.get("provider"), repo.get("instance")
    prs["fetched_at"] = (now or common.now()).isoformat(timespec="seconds")
    if not prov:
        prs.update(status="unavailable", why="The remote isn't on a supported Git host, so PR authors can't be read.",
                   error="")
        return
    acct = pol.account(prov, inst)
    if not acct:
        prs.update(status="unconfigured", why="No confirmed %s account for %s in your profile." % (prov, inst), error="",
                   account="")
        return
    prs["account"] = "%s:%s:%s" % (prov, inst, acct.get("id") or acct.get("login"))
    pref = gitcmd.repo_conf(cfg, repo.get("key") or gitcmd.repo_key(repo["path"])).get("pr_source")
    info = dict(repo.get("remote") or {})
    src = hosts.source_for(cfg, info, pref)
    prs["source"] = src
    capped, why = False, ""
    try:
        if src in ("gh", "glab", "az"):
            rows, capped = hosts.authored_prs(cfg, info, src, acct, window, run=run or hosts._run)
        elif src.startswith("mcp:"):
            from .gittrack import RepoData
            rows = [p for p in RepoData(repo.get("key") or gitcmd.repo_key(repo["path"])).prs.values() if isinstance(p, dict)]
            if not any(isinstance(p.get("author"), dict) for p in rows):
                prs.update(status="unavailable", why="The %s MCP source gave no PR authors." % src[4:], error="")
                return
            why = "Only PRs on tracked branches (the Git host's MCP server lists PRs per branch)."
        else:
            prs.update(status="unavailable", why="PRs come from merge commits here, which carry no author.", error="")
            return
    except ValueError as e:
        prs.update(status="unconfigured", why=str(e)[:200], error="")
        return
    except Exception as e:  # noqa: BLE001 - one repo's failure keeps its last good items
        prs.update(status="error", error=str(e)[:300], why="The last PR read failed; earlier PRs are kept.")
        return
    items = dict(prs.get("items") or {})
    for p in rows:
        if pol.pr_mine(p.get("author"), prov, inst) is not True or p.get("number") is None:
            continue  # rechecked: a PR the host returned that isn't by your confirmed account is never stored
        items[str(p["number"])] = _pr_item(p, prefixes)
    start = profile.day_bounds(window, cfg)[0] if window else None
    if start is not None:
        items = {k: v for k, v in items.items()
                 if any((common.parse_ts(v.get(f)) or start) >= start for f in ("created_at", "merged_at") if v.get(f))
                 or v.get("state") in ("open", "draft")}
    prs["items"] = items
    prs["capped"] = bool(capped)
    if capped:
        prs.update(status="partial", why="The host returned its maximum page; older PRs may be missing.", error="")
    elif why:
        prs.update(status="partial", why=why, error="")
    else:
        prs.update(status="ok", why="", error="")


# --------------------------------------------------------------------------- the step

def refresh_repo(cfg, pol, sig, window, path, now, run=None, force=False):
    """One repository's ledger. -> a short summary dict."""
    from . import gitcmd
    key = gitcmd.repo_key(path)
    p = ledger_path(key)
    led = common.read_json(p, None)
    led = led if isinstance(led, dict) and led.get("v") == V else new_ledger()
    before = json.dumps(led, sort_keys=True)
    reset = led.get("profile_sig") != sig
    if reset:  # who or what counts changed: nothing collected under the old rules is kept
        led = new_ledger(led.get("repo"))
        led["profile_sig"] = sig
    nowiso = now.isoformat(timespec="seconds")
    com, prs = led["commits"], led["prs"]
    full_due = force or reset or not com.get("full_at") or led.get("window_start") != window or \
        _age(com.get("full_at"), now) >= FULL_S
    inc_due = full_due or _age(com.get("scanned_at"), now) >= INCREMENTAL_S
    if full_due or not led.get("repo"):
        led["repo"] = repo_identity(cfg, path)
    res = {"repo": led["repo"].get("name"), "commits": None, "prs": None}
    if not pol.emails:
        com.update(status="unconfigured", why="No commit email is confirmed in your profile.", items={}, error="")
    elif inc_due:
        globs = profile.progress_settings(cfg)["exclude_globs"]
        prefixes = cfg.get("ticket_prefixes") or []
        if full_due:
            since = (profile.day_bounds(window, cfg)[0] - _dt.timedelta(days=1)).isoformat(timespec="seconds")
        else:
            last = common.parse_ts(com.get("scanned_at")) or now
            since = (last - _dt.timedelta(days=1)).isoformat(timespec="seconds")
        ok, found, err = scan(cfg, pol, path, since, globs, prefixes)
        if ok:
            com["items"] = _merge(com.get("items") or {}, found, since, full_due, nowiso)
            com.update(status="ok", why="", error="", scanned_at=nowiso)
            if full_due:
                com.update(full_at=nowiso, since=since)
                led["window_start"] = window
            res["commits"] = len(found)
        else:
            com.update(status="error", error=err, why="The last commit scan failed; earlier results are kept.")
    if reset or force or _age(prs.get("fetched_at"), now) >= PR_S:
        collect_prs(cfg, pol, led["repo"], prs, window, run=run, now=now)
        res["prs"] = prs["status"]
    items = com.get("items") or {}
    try:
        pids = _patch_ids(cfg, path, items) if items else {}
    except Exception as e:  # noqa: BLE001 - without patch-ids nothing is merged as equivalent
        common.log("agent", "activity patch-ids failed for %s: %s" % (path, e))
        pids = {}
    settle(items, prs.get("items") or {}, now, pids)
    if json.dumps(led, sort_keys=True) != before or not os.path.exists(p):  # unchanged: no write, the page's cursor stays
        common.write_json(p, led)
    return res


def _age(iso, now):
    t = common.parse_ts(iso)
    return (now - t).total_seconds() if t else float("inf")


def tick(store, cfg, state, now=None, run=None, force=False):
    """The background step: each in-scope repository's ledger, a repository's failure never blocking the others."""
    now = now or common.now()
    pol = profile.Policy(cfg)
    sig, window = profile.signature(cfg), profile.window_start(cfg, profile.day_of(now, cfg))
    out = {}
    for path in pol.p["repos"]:
        if not (os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(os.path.join(path, ".git"))):
            continue
        try:
            out[path] = refresh_repo(cfg, pol, sig, window, path, now, run=run, force=force)
        except Exception as e:  # noqa: BLE001
            common.log("agent", "activity failed for %s: %s" % (path, e))
    return out
