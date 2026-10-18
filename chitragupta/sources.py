"""Non-LLM sources: my git commits, Bruno requests, SQL script folders, inbox.md,
and the transcript safety net (Claude Code sessions the hooks missed)."""
import glob
import hashlib
import json
import os
import re

from . import common
from . import gitro
from .redact import redact

# --------------------------------------------------------------------------- git commits

_SEP_REC, _SEP_FLD = "\x1e", "\x1f"


def scan_commits(cfg, state, since_dt):
    """My commits in all watched repos since `since_dt`. Dedupe by sha via state['commits_seen']."""
    seen = state.setdefault("commits_seen", {})
    emails = [e for e in cfg.get("git_author_emails") or [] if e]
    if not emails:
        return []
    prefixes = cfg.get("ticket_prefixes") or []
    out = []
    for repo in cfg.get("watched_repos") or []:
        if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(os.path.join(repo, ".git")):
            continue
        args = ["log", "--all", "--source", "--no-merges",
                "--since=" + since_dt.isoformat(timespec="seconds"), "--name-status",
                "--format=" + _SEP_REC + "%H" + _SEP_FLD + "%aI" + _SEP_FLD + "%S" + _SEP_FLD + "%s" + _SEP_FLD + "%b" + _SEP_FLD]
        args += ["--author=" + re.escape(e) for e in emails]
        rc, so, se = gitro.run(cfg, repo, args, timeout=60)
        if rc != 0:
            common.log("agent", "git log failed for %s: %s" % (repo, se.strip()[:300]))
            continue
        for rec in so.split(_SEP_REC):
            if not rec.strip():
                continue
            parts = rec.split(_SEP_FLD)
            if len(parts) < 6:
                continue
            sha, adate, ref, subject, body, files_blob = parts[0].strip(), parts[1], parts[2], parts[3], parts[4], parts[5]
            if sha in seen:
                continue
            ref = re.sub(r"^refs/(heads|remotes/[^/]+)/", "", ref.strip())
            files = []
            for line in files_blob.strip().splitlines():
                bits = line.split("\t")
                if len(bits) >= 2:
                    files.append({"status": bits[0][:1], "path": bits[-1]})
            msg_tickets = common.find_tickets(subject + "\n" + body, prefixes)
            ticket = msg_tickets[0] if msg_tickets else common.ticket_from_branch(ref, prefixes)
            out.append({
                "sha": sha, "short": sha[:8], "repo": repo, "repo_name": os.path.basename(repo.rstrip("/\\")),
                "date": adate, "ref": ref, "subject": redact(subject.strip())[:200],
                "files": files[:200], "ticket": ticket,
            })
    return out


def mark_commits_seen(state, commits):
    seen = state.setdefault("commits_seen", {})
    for c in commits:
        seen[c["sha"]] = c["date"]
    # bound the set: forget commits older than 30 days
    cutoff = (common.now().timestamp() - 30 * 86400)
    for sha, d in list(seen.items()):
        dt = common.parse_ts(d)
        if dt and dt.timestamp() < cutoff:
            del seen[sha]


# --------------------------------------------------------------------------- Bruno

_BRU_METHOD = re.compile(r"(?m)^(get|post|put|delete|patch|options|head|connect|trace)\s*\{\s*$")
_BRU_URL = re.compile(r"(?m)^\s*url:\s*(.+?)\s*$")
_BRU_NAME = re.compile(r"(?ms)^meta\s*\{.*?^\s*name:\s*(.+?)\s*$")


def _strip_query_values(url):
    if "?" not in url:
        return url
    base, q = url.split("?", 1)
    keys = [kv.split("=", 1)[0] for kv in q.split("&") if kv]
    return base + "?" + "&".join(k + "=" for k in keys)


def parse_bru(path):
    txt = common.read_text(path)
    m = _BRU_METHOD.search(txt)
    if not m:
        return None
    block = txt[m.end(): txt.find("}", m.end()) if txt.find("}", m.end()) > 0 else len(txt)]
    u = _BRU_URL.search(block)
    n = _BRU_NAME.search(txt)
    return {"method": m.group(1).upper(),
            "url": redact(_strip_query_values(u.group(1))) if u else "",
            "name": (n.group(1) if n else os.path.splitext(os.path.basename(path))[0])[:120]}


def _collection_name(root):
    try:
        with open(os.path.join(root, "bruno.json"), encoding="utf-8") as f:
            return json.load(f).get("name") or os.path.basename(root)
    except (OSError, ValueError):
        return os.path.basename(root)


def _scan_files(roots, ext, state_key, baseline_key, state):
    """Yield (root, path, mtime) for files changed since last seen; first sight of a root is baseline only."""
    mt = state.setdefault(state_key, {})
    base = state.setdefault(baseline_key, [])
    for root in roots:
        if not os.path.isdir(root):
            continue
        first = common.norm_path(root) not in base
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git", "environments")]
            for fn in filenames:
                if not fn.lower().endswith(ext):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                key = common.norm_path(p)
                old = mt.get(key)
                mt[key] = m
                if first or (old is not None and m <= old):
                    continue
                if old is None and not first:
                    yield root, p, m  # new file
                elif old is not None:
                    yield root, p, m
        if first:
            base.append(common.norm_path(root))


def scan_bruno(cfg, state):
    out = []
    import datetime as dt
    for root, p, m in _scan_files(cfg.get("bruno_collections") or [], ".bru", "bruno_mtimes", "bruno_baselined", state):
        info = parse_bru(p)
        if not info:
            continue
        rel = os.path.relpath(p, root).replace("\\", "/")
        info.update({"collection": _collection_name(root), "path": rel,
                     "ts": dt.datetime.fromtimestamp(m).astimezone().isoformat(timespec="seconds")})
        info["ticket"] = (common.find_tickets(rel + " " + info["name"], cfg.get("ticket_prefixes")) or [None])[0]
        out.append(info)
    return out


# --------------------------------------------------------------------------- SQL script folders

def sql_roots(cfg):
    roots = list(cfg.get("sql_script_dirs") or [])
    for g in cfg.get("sql_script_globs") or []:
        roots.extend(sorted(glob.glob(g)))
    return roots


def guess_dialect(text):
    t = text.lower()
    if re.search(r"\bdb\.\w+\.(find|aggregate|update|insert|delete)", t):
        return "mongodb"
    if re.search(r"\btop\s+\d+|\bnolock\b|\bgetdate\(\)|\[dbo\]|\bnvarchar\b|\bisnull\(", t):
        return "sqlserver"
    if re.search(r"::\w+|\bilike\b|\bjsonb\b|\bnow\(\)|\blimit\s+\d+|\bserial\b|returning\b", t):
        return "postgresql"
    return "other"


def scan_sql(cfg, state):
    out = []
    hashes = state.setdefault("sql_hashes", {})
    import datetime as dt
    for root, p, m in _scan_files(sql_roots(cfg), ".sql", "sql_mtimes", "sql_baselined", state):
        text = common.read_text(p).strip()
        if not text:
            continue
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        key = common.norm_path(p)
        if hashes.get(key) == h:
            continue
        hashes[key] = h
        red = redact(common.truncate(text, 4000))
        out.append({"path": p.replace("\\", "/"), "name": os.path.basename(p), "text": red,
                    "dialect": guess_dialect(text),
                    "ts": dt.datetime.fromtimestamp(m).astimezone().isoformat(timespec="seconds"),
                    "ticket": (common.find_tickets(p.replace("\\", "/") + "\n" + text[:2000], cfg.get("ticket_prefixes")) or [None])[0]})
    return out


# --------------------------------------------------------------------------- inbox.md

_INBOX_TICKET = re.compile(r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?([A-Z][A-Z0-9]{1,9}-\d+)\s*:\s*(.+)$")
# action lines: every copy of an identical line is one more request (data lines are read once however often they appear)
# counted per copy: sending again, and calendar marks (mark, clear, mark again must work; re-applying one is harmless)
INBOX_ACTION = re.compile(r"^\s*(?:[-*]\s*)?(?:resend|leave|half-day|holiday|workday|clear)\s*:", re.I)


def _inbox_lines(text):
    """(stripped line, hash) for every non-template line of inbox.md, in file order."""
    in_comment = False
    for line in text.splitlines():
        s = line.strip()
        if in_comment:
            if "-->" in s:
                in_comment = False
            continue
        if s.startswith("<!--"):
            in_comment = "-->" not in s
            continue
        if not s or s.startswith("#"):
            continue
        yield s, hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def inbox_counts_from_legacy(hashes, text):
    """The old state (a list of line hashes seen) -> {hash: copies already read}. The old scan read a line once and
    ignored its copies, so every copy in the file today counts as read: nothing is replayed after the change."""
    occ = {}
    for _, h in _inbox_lines(text or ""):
        occ[h] = occ.get(h, 0) + 1
    return {h: max(1, occ.get(h, 0)) for h in hashes if isinstance(h, str)}


def scan_inbox(cfg, state):
    """New non-template lines of inbox.md. state["inbox_hashes"] = {line hash: copies read}. A data line (a note,
    time: ...) is read once however often it appears; an action line (resend:, and the calendar marks leave:,
    half-day:, holiday:, workday:, clear:) is counted, so each new copy of an identical line is read once more. Counts never go down (a file
    caught mid-save can't replay old lines). The file itself is never rewritten."""
    path = common.hp("inbox.md")
    text = common.read_text(path)
    seen = state.get("inbox_hashes")
    if isinstance(seen, list):  # not migrated yet (migrate.py "inbox-counts" does this with a backup)
        seen = inbox_counts_from_legacy(seen, text)
    seen = {h: int(n) for h, n in (seen or {}).items() if isinstance(n, (int, float))}
    now_counts = {}
    out = []
    for s, h in _inbox_lines(text):
        n = now_counts[h] = now_counts.get(h, 0) + 1
        if n <= seen.get(h, 0) or (n > 1 and not INBOX_ACTION.match(s)):
            continue
        seen[h] = n
        m = _INBOX_TICKET.match(s)
        ticket, text_ = (m.group(1).upper(), m.group(2)) if m else (None, re.sub(r"^[-*]\s*", "", s))
        out.append({"hash": h, "ts": common.now_iso(), "ticket": ticket, "text": redact(text_)[:1000]})
    state["inbox_hashes"] = seen
    return out


# --------------------------------------------------------------------------- transcript safety net

def transcripts_root():
    if common.env("TRANSCRIPTS_DIR"):
        return common.env("TRANSCRIPTS_DIR")
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tool_key_input(name, ti):
    ti = ti if isinstance(ti, dict) else {}
    if name in ("Bash", "PowerShell"):
        return {"command": redact(common.truncate(str(ti.get("command", "")), 2000))}
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read"):
        return {"file_path": ti.get("file_path") or ti.get("notebook_path") or ""}
    if name == "WebFetch":
        return {"url": redact(str(ti.get("url", "")))}
    if name.startswith("mcp__"):
        return {"arguments": redact(common.truncate(json.dumps(ti, ensure_ascii=False), 4000))}
    return None


def transcript_events(path, offset):
    """Parse new complete lines of a transcript from `offset`. Returns (events, new_offset, meta)."""
    events, meta = [], {}
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return events, offset, meta
    end = data.rfind(b"\n")
    if end < 0:
        return events, offset, meta
    chunk, new_offset = data[: end + 1], offset + end + 1
    for raw in chunk.splitlines():
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("cwd") and not meta.get("cwd"):
            meta["cwd"] = rec.get("cwd")
        if rec.get("gitBranch"):
            meta["branch"] = rec.get("gitBranch")
        if rec.get("sessionId"):
            meta["session_id"] = rec.get("sessionId")
        t = rec.get("type")
        ts = rec.get("timestamp")
        if rec.get("isMeta") or rec.get("isSidechain"):
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content")
        if t == "user":
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            txt = _text_of(content).strip()
            if not txt or txt.startswith(("<local-command", "<command-", "Caveat:", "<system-reminder")):
                continue
            events.append({"ts": ts, "event": "user-prompt", "branch": rec.get("gitBranch"), "cwd": rec.get("cwd"),
                           "payload": {"prompt": redact(common.truncate(txt, 4000))}})
        elif t == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    events.append({"ts": ts, "event": "stop", "branch": rec.get("gitBranch"), "cwd": rec.get("cwd"),
                                   "payload": {"message": redact(common.truncate(b["text"], 12000))}})
                elif b.get("type") == "tool_use":
                    ki = _tool_key_input(str(b.get("name", "")), b.get("input"))
                    if ki is not None:
                        events.append({"ts": ts, "event": "post-tool", "branch": rec.get("gitBranch"),
                                       "cwd": rec.get("cwd"), "payload": {"tool": b.get("name"), "input": ki}})
    return events, new_offset, meta


def list_recent_transcripts(hours=48):
    root = transcripts_root()
    cutoff = common.now().timestamp() - hours * 3600
    out = []
    try:
        projects = os.listdir(root)
    except OSError:
        return out
    for proj in projects:
        d = os.path.join(root, proj)
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(d, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                out.append((p, st.st_size, st.st_mtime))
    return out
