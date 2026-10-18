"""Headless Claude extraction / consolidation, plus the deterministic fallback.

The claude call runs in an empty sandbox folder with hooks disabled, no tools, no MCP,
no user settings, no session persistence, and the internal-process marker set (recursion guard).
"""
import glob
import json
import os
import re
import shutil

from . import common
from .redact import redact

DIGEST_CAP = 120_000
PER_EVENT = {"prompt": 4000, "message": 6000, "command": 600, "http": 3000, "content": 2500, "mcp": 2000}

# --------------------------------------------------------------------------- claude executable


_CC_BIN = ("node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")


def claude_candidates():
    """Places a claude.exe lives that don't depend on the current shell (the scheduled task has no
    per-session PATH): the native install, then Node version managers' stable links, then npm global."""
    appdata = os.environ.get("APPDATA", "")
    cands = [os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe"),
             os.path.join(appdata, "fnm", "aliases", "default", *_CC_BIN)]  # fnm default alias
    if os.environ.get("FNM_DIR"):
        cands.append(os.path.join(os.environ["FNM_DIR"], "aliases", "default", *_CC_BIN))
    if os.environ.get("NVM_SYMLINK"):  # nvm-windows' active-version link
        cands.append(os.path.join(os.environ["NVM_SYMLINK"], *_CC_BIN))
    cands.append(os.path.join(appdata, "npm", *_CC_BIN))
    return cands


def stable_path(p):
    """fnm puts a per-shell folder on PATH (fnm_multishells\\<pid>_…) that is gone once the shell exits;
    resolve it to the real Node version folder, which outlives the shell."""
    if "fnm_multishells" in p.replace("/", "\\").lower():
        real = os.path.realpath(p)
        if os.path.isfile(real) and "fnm_multishells" not in real.lower():
            return real
    return p


def unstable(p):
    """True for a path that only exists inside one shell session."""
    return bool(p) and "fnm_multishells" in p.replace("/", "\\").lower()


def resolve_claude(cfg=None):
    """A real executable (never a .cmd shim): configured path, native claude.exe, or None."""
    cfg = cfg or common.load_config()
    p = (cfg.get("paths") or {}).get("claude")
    if p and os.path.isfile(p) and not p.lower().endswith((".cmd", ".bat")):
        return [p]
    for c in claude_candidates():
        if os.path.isfile(c):
            return [c]
    which = shutil.which("claude.exe")
    if which:
        return [stable_path(which)]
    # npm install without native binary: node + cli script
    node = shutil.which("node.exe")
    for js in glob.glob(os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules", "@anthropic-ai", "claude-code", "cli*.js")):
        if node:
            return [node, js]
    return None


# --------------------------------------------------------------------------- digest

def _outcome_text(p):
    oc = (p.get("outcome") or {}).get("http")
    if not oc:
        links = (p.get("outcome") or {}).get("artifact_links")
        return ("  → published " + ", ".join(links)) if links else ""
    bits = []
    if oc.get("statuses"):
        bits.append("HTTP " + ", ".join(str(x) for x in oc["statuses"]))
    elif oc.get("transport_error"):
        bits.append("connection failed")
    if oc.get("response_keys") is not None:
        bits.append("response keys " + json.dumps(oc["response_keys"], ensure_ascii=False)[:600])
    if oc.get("token_path"):
        bits.append("token at " + oc["token_path"])
    return ("  → " + " · ".join(bits)) if bits else ""


CONTENT_IN_DIGEST = (".bru", ".http", ".rest", ".json", ".sql", ".mmd", ".puml")


def _line(ev):
    t = common.hhmm(ev.get("ts")) or "--:--"
    p = ev.get("payload") or {}
    e = ev.get("event")
    if e == "user-prompt":
        return "[%s] PROMPT: %s" % (t, common.truncate(p.get("prompt", ""), PER_EVENT["prompt"]))
    if e == "stop":
        from . import blocks
        line = "[%s] CLAUDE: %s" % (t, common.truncate(blocks.strip(p.get("message", "")), PER_EVENT["message"]))
        for b in p.get("blocks") or []:
            marker = "[worklog-%s block: ingested separately]" % b.get("kind")
            if marker not in line:
                line += "\n" + marker
        return line
    if e == "post-tool":
        tool, ki = p.get("tool", ""), p.get("input") or {}
        if "command" in ki:
            lim = PER_EVENT["http"] if (p.get("outcome") or {}).get("http") else PER_EVENT["command"]
            return "[%s] TOOL %s: %s%s" % (t, tool, common.truncate(ki["command"], lim), _outcome_text(p))
        if "file_path" in ki:
            line = "[%s] TOOL %s: %s%s" % (t, tool, ki["file_path"], _outcome_text(p))
            if ki.get("content") and ki["file_path"].lower().endswith(CONTENT_IN_DIGEST):
                line += "\n    content (%s): %s" % (ki.get("content_mode", "full"), common.truncate(ki["content"], PER_EVENT["content"]))
            elif ki.get("content"):
                line += "  (%d chars written)" % len(ki["content"])
            return line
        if "url" in ki:
            return "[%s] TOOL %s: %s" % (t, tool, ki["url"])
        if "arguments" in ki:
            return "[%s] TOOL %s: %s" % (t, tool, common.truncate(ki["arguments"], PER_EVENT["mcp"]))
        return "[%s] TOOL %s" % (t, tool)
    if e == "session-start":
        return "[%s] SESSION START (%s) branch=%s" % (t, p.get("source") or "", ev.get("branch") or "-")
    if e == "session-end":
        return "[%s] SESSION END (%s)" % (t, p.get("reason") or "")
    if e == "enrich":
        return "[%s] CLAUDE (intermediate): %s" % (t, common.truncate(p.get("message", ""), 3000))
    return ""


def event_chars(ev):
    p = ev.get("payload") or {}
    ki = p.get("input") or {}
    n = len(p.get("prompt", "")) + len(p.get("message", ""))
    if ki.get("content"):
        ki = dict(ki, content=ki["content"][:PER_EVENT["content"]])
    return n + (len(json.dumps(ki, ensure_ascii=False)) if ki else 0) + (300 if p.get("outcome") else 0)


def build_digests(events, meta, candidates, already):
    """Chronological digest(s) of one session's pending events, chunked at ~DIGEST_CAP chars."""
    header = [
        "SESSION: %s   WORK DAY: %s" % (meta.get("session", "")[:8], meta.get("day", "")),
        "REPO: %s   BRANCH: %s" % (os.path.basename((meta.get("repo") or "").rstrip("/\\")) or "-", meta.get("branch") or "-"),
        "CANDIDATE TICKETS: %s" % (", ".join(candidates) if candidates else "(none detected)"),
        "ALREADY_RECORDED: " + json.dumps(already, ensure_ascii=False)[:20000],
        "CURRENT_WORKLOG_LINES: " + json.dumps(meta.get("lines") or {}, ensure_ascii=False),
        "",
        "=== ACTIVITY (chronological; tool outputs are never recorded) ===",
    ]
    head = redact("\n".join(header)) + "\n"
    lines = [redact(l) for l in (_line(e) for e in events) if l]
    chunks, cur = [], head
    for l in lines:
        if len(cur) + len(l) + 1 > DIGEST_CAP and cur != head:
            chunks.append(cur)
            cur = head + "(continued)\n"
        cur += l + "\n"
    if cur != head:
        chunks.append(cur)
    return chunks


def enrich_events(transcript_path, events, max_chars=20000):
    """Intermediate assistant text from the transcript between the first and last pending event.
    Best effort: any failure returns []."""
    try:
        if not transcript_path or not os.path.isfile(transcript_path) or os.path.getsize(transcript_path) > 25_000_000:
            return []
        from . import sources
        tev, _, _ = sources.transcript_events(transcript_path, 0)
        ts = [common.parse_ts(e.get("ts")) for e in events if e.get("ts")]
        ts = [t for t in ts if t]
        if not ts:
            return []
        lo, hi = min(ts), max(ts)
        known = {(e.get("payload") or {}).get("message", "")[:200] for e in events if e.get("event") == "stop"}
        out, total = [], 0
        for e in tev:
            if e["event"] != "stop":
                continue
            t = common.parse_ts(e.get("ts"))
            msg = e["payload"].get("message", "")
            if not t or t < lo or t > hi or msg[:200] in known or len(msg) < 80:
                continue
            total += len(msg)
            if total > max_chars:
                break
            out.append({"ts": t.isoformat(timespec="seconds"), "event": "enrich", "payload": {"message": msg}})
        return out
    except Exception:
        return []


# --------------------------------------------------------------------------- schema validation (subset)

def validate(obj, schema, path="$"):
    """Minimal JSON Schema check for the subset our schemas use. Returns list of errors."""
    errs = []
    if "anyOf" in schema:
        branches = [validate(obj, b, path) for b in schema["anyOf"]]
        return [] if any(not b for b in branches) else branches[0]
    t = schema.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    pymap = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "number": (int, float), "null": type(None)}
    if types and not any(isinstance(obj, pymap[x]) and not (x in ("integer", "number") and isinstance(obj, bool)) for x in types):
        return ["%s: expected %s" % (path, types)]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append("%s: %r not in enum" % (path, obj))
    if isinstance(obj, dict):
        for r in schema.get("required", []):
            if r not in obj:
                errs.append("%s: missing %s" % (path, r))
        for k, v in obj.items():
            if k in schema.get("properties", {}):
                errs += validate(v, schema["properties"][k], path + "." + k)
    if isinstance(obj, list) and "items" in schema:
        for i, v in enumerate(obj):
            errs += validate(v, schema["items"], "%s[%d]" % (path, i))
    return errs


def coerce(obj, schema):
    """Repair small deviations (missing keys, nulls for arrays) so partially valid output is usable."""
    if "anyOf" in schema:
        if obj is None and any(b.get("type") == "null" for b in schema["anyOf"]):
            return None
        return coerce(obj, next((b for b in schema["anyOf"] if b.get("type") == "object"), schema["anyOf"][0]))
    t = schema.get("type")
    if t == "object":
        obj = obj if isinstance(obj, dict) else {}
        out = {}
        for k, sub in schema.get("properties", {}).items():
            st = sub.get("type")
            if k in obj:
                out[k] = coerce(obj[k], sub)
            elif st == "array":
                out[k] = []
            elif (isinstance(st, list) and "null" in st) or "anyOf" in sub:
                out[k] = None
            elif st == "string":
                out[k] = ""
            elif st == "boolean":
                out[k] = False
            elif st == "object":
                out[k] = coerce({}, sub)
        return out
    if t == "array":
        if not isinstance(obj, list):
            return []
        return [coerce(x, schema["items"]) for x in obj if (schema["items"].get("type") != "object" or isinstance(x, dict))]
    if t == "string":
        if "enum" in schema and obj not in schema["enum"]:
            return schema["enum"][0]
        return obj if isinstance(obj, str) else ("" if obj is None else str(obj))
    return obj


# --------------------------------------------------------------------------- the call

class ExtractError(Exception):
    def __init__(self, msg, limit=False):
        super().__init__(msg)
        self.limit = limit


def call_claude(kind, stdin_text, cfg, timeout=300):
    """kind: 'extract' | 'extract_delta' | 'consolidate' | 'curate'. Returns (structured_output, cost_usd)."""
    exe = resolve_claude(cfg)
    if not exe:
        raise ExtractError("claude executable not found")
    schema_path = common.prompt_path("schemas/%s.json" % kind)
    prompt_path = common.prompt_path(kind + ".md")
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)
    schema_arg = json.dumps({k: v for k, v in schema.items() if k != "$schema"}, separators=(",", ":"))
    models = cfg.get("models") or {}
    model = models.get(kind) or models.get(kind.split("_")[0]) or ("haiku" if kind.startswith("extract") else "sonnet")
    cache_dir = common.env("LLM_CACHE")  # optional (backfill preview -> real run): same input, same answer
    cache_file = None
    if cache_dir:
        import hashlib
        key = hashlib.sha1("\0".join([kind, model, common.read_text(prompt_path), schema_arg, stdin_text]).encode("utf-8")).hexdigest()
        cache_file = os.path.join(cache_dir, key + ".json")
        cached = common.read_json(cache_file, None)
        if cached:
            return cached["out"], 0.0
    sandbox = common.wl("sandbox")
    common.ensure_dir(sandbox)
    from .safety import claude as builder, commands
    args = builder.build(exe, "Process the worklog input provided on stdin and return the structured result.", "none",
                         model, system_prompt_file=prompt_path, json_schema=schema_arg, output="json", cfg=cfg)
    rc, so, se = commands.run(args, "claude", cwd=sandbox, input_text=stdin_text, timeout=timeout, cfg=cfg,
                              env_extra=dict(common.INTERNAL_ENV, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"))
    data = None
    try:
        data = json.loads(so.strip().splitlines()[-1]) if so.strip() else None
    except (ValueError, IndexError):
        try:
            data = json.loads(so)
        except ValueError:
            data = None
    blob = (so + " " + se).lower()
    limit = any(s in blob for s in ("usage limit", "rate limit", "rate_limit", "limit reached", "quota", "overloaded", "429"))
    if rc != 0 or not isinstance(data, dict):
        raise ExtractError("claude rc=%s: %s" % (rc, (se or so).strip()[:400]), limit=limit)
    cost = float(data.get("total_cost_usd") or 0)
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        raise ExtractError("claude error %s: %s" % (data.get("subtype"), str(data.get("result"))[:300]), limit=limit)
    out = data.get("structured_output")
    if out is None and isinstance(data.get("result"), str):
        try:
            out = json.loads(data["result"])
        except ValueError:
            out = None
    if not isinstance(out, dict):
        raise ExtractError("no structured_output")
    errs = validate(out, schema)
    if errs:
        out = coerce(out, schema)
        if validate(out, schema):
            raise ExtractError("invalid output: " + "; ".join(errs[:5]))
    if cache_file:
        common.write_json(cache_file, {"kind": kind, "out": out, "cost": cost})
    return out, cost


# --------------------------------------------------------------------------- deterministic fallback

_SQL_RX = re.compile(r"(?is)\b(select\s+.{3,}?\bfrom\b.+?|update\s+\S+\s+set\b.+?|insert\s+into\b.+?|delete\s+from\b.+?|"
                     r"alter\s+table\b.+?|create\s+(?:table|index|view)\b.+?)(?:;|\n\n|```|$)")


def fallback_summary(events):
    """Commands, files changed and SQL found by regex - rendered with 'AI summary pending'."""
    cmds, files, sql = [], [], []
    for e in events:
        p = e.get("payload") or {}
        if e.get("event") == "post-tool":
            ki = p.get("input") or {}
            if p.get("tool") in ("Edit", "Write", "MultiEdit", "NotebookEdit") and ki.get("file_path"):
                if ki["file_path"] not in files:
                    files.append(ki["file_path"])
            elif ki.get("command"):
                c = ki["command"].strip().splitlines()[0][:160]
                if c and c not in cmds:
                    cmds.append(c)
            text = ki.get("command", "") + "\n" + ki.get("arguments", "")
        else:
            text = p.get("prompt", "") + "\n" + p.get("message", "")
        for m in _SQL_RX.finditer(text or ""):
            q = re.sub(r"\s+", " ", m.group(1)).strip()[:400]
            if len(q) > 15 and q not in sql:
                sql.append(q)
    return {"commands": cmds[-15:], "files": files[:30], "sql": [redact(s) for s in sql[:10]]}
