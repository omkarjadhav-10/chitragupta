"""Claude Code hook: append one redacted JSON line per event to .worklog/raw/<work-day>/<session_id>.jsonl.

Usage (exec form, from ~/.claude/settings.json, through the launcher):
    python.exe  run.py hook session-start      (sync; may print SessionStart context)
    pythonw.exe run.py hook user-prompt|post-tool|stop|session-end   (async)

Rules: never call claude, never print except SessionStart output, always exit 0.
Until setup has run (plugin.is_set_up), SessionStart prints one "not set up" line and every other event exits
without writing anything. Once set up, SessionStart also starts the detached maintenance process (plugin.py).
"""
import json
import os
import re
import sys
import time

_T0 = time.perf_counter()

LIMITS = {"prompt": 4000, "message": 12000, "command": 2000, "http_command": 20000, "mcp": 4000}


_STDIN = []


def _read_stdin():
    """The hook input (read once; later calls get the same dict)."""
    if _STDIN:
        return _STDIN[0]
    stream = sys.stdin
    data = {}
    if stream is not None:
        try:
            raw = stream.buffer.read() if hasattr(stream, "buffer") else stream.read().encode("utf-8")
            data = json.loads(raw.decode("utf-8-sig") or "{}")
        except Exception:
            data = {}
    _STDIN.append(data if isinstance(data, dict) else {})
    return _STDIN[0]


def _cg_mark(event):
    """Safety: mark the session while a /cg: command runs (UserPromptSubmit), clear it at Stop. Independent of
    pause and capture scope; the in-session guard (chitragupta.safety.hook) reads it."""
    try:
        from .safety import hook as safety_hook
        inp = _read_stdin()
        if event == "user-prompt":
            safety_hook.mark(inp.get("session_id"), str(inp.get("prompt") or inp.get("user_prompt") or ""))
        elif event == "stop":
            safety_hook.clear(inp.get("session_id"))
    except Exception:  # noqa: BLE001 — a hook must never fail
        pass


def _key_input(tool, ti, common, redact, repo_root=None):
    """The part of a tool call that says what was done. Tool output is never stored; for HTTP commands only
    the status codes and a key-only skeleton of the response are kept (see _outcome)."""
    ti = ti if isinstance(ti, dict) else {}
    t = common.truncate
    if tool in ("Bash", "PowerShell"):
        cmd = str(ti.get("command", ""))
        lim = LIMITS["http_command"] if _is_http(cmd) else LIMITS["command"]
        return {"command": redact(t(cmd, lim)),
                "description": redact(t(str(ti.get("description", "") or ""), 200))}
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read"):
        out = {"file_path": ti.get("file_path") or ti.get("notebook_path") or ""}
        if tool == "Read" and ti.get("offset"):
            out["offset"] = ti.get("offset")
        if tool in ("Write", "Edit", "MultiEdit"):
            from . import capture
            if capture.want_content(out["file_path"], repo_root):
                c = capture.file_content(tool, ti, redact, t)
                if c:
                    out["content"] = c
                    out["content_mode"] = "full" if tool == "Write" else "edit"
        return out
    if tool == "WebFetch":
        return {"url": redact(str(ti.get("url", "")))}
    if tool == "Artifact":
        return {"file_path": ti.get("file_path") or "", "url": ti.get("url") or "", "title": redact(str(ti.get("title") or "")),
                "action": ti.get("action") or "publish"}
    if tool.startswith("mcp__"):
        try:
            s = json.dumps(ti, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(ti)
        return {"arguments": redact(t(s, LIMITS["mcp"]))}
    return {}


def _time_only(event, inp, cfg, common):
    """Sessions inside the worklog folder: time only (timestamp + ticket mentioned), never content."""
    if event not in ("user-prompt", "post-tool", "stop"):
        return
    ticket = None
    if event == "user-prompt":
        found = common.find_tickets(str(inp.get("user_prompt") or inp.get("prompt") or ""), cfg.get("ticket_prefixes") or [])
        ticket = found[0] if found else None
    sid = str(inp.get("session_id") or "unknown").replace("/", "_").replace("\\", "_")
    ev = {"ts": common.now_iso(), "event": event, "session_id": sid, "source": "time-only", "ticket": ticket}
    common.append_line(common.wl("raw", common.workday(common.now(), cfg), "time", sid + ".jsonl"), json.dumps(ev))


def _qa_env(tool, ti, cfg, common):
    """'qa' (or another allowed env) when this call only touches allowed-environment hosts or the worklog bruno/ folder."""
    ti = ti if isinstance(ti, dict) else {}
    from . import envmap
    if tool in ("Bash", "PowerShell"):
        cmd = str(ti.get("command", ""))
        env = envmap.env_of_text(cmd) if _is_http(cmd) else None
    elif tool in ("Write", "Edit", "MultiEdit"):
        fp = str(ti.get("file_path") or "")
        if fp and common.path_within(fp, common.hp("bruno")) and not fp.replace("\\", "/").lower().endswith("/.env"):
            return "qa"
        env = envmap.env_of_text(str(ti.get("content") or ti.get("new_string") or "")[:20000])
    else:
        return None
    return env if env and envmap.allowed(env, cfg) else None


def _is_http(cmd):
    if not cmd or not any(w in cmd for w in ("curl", "Invoke-", "irm ", "iwr ", "wget", "http ", "https ")):
        return False
    from . import apicalls
    return apicalls.is_http_command(cmd)


def _outcome(tool, ti, resp, cwd, redact):
    """Outcome facts kept from tool_response: HTTP status + response key skeleton, artifact share links."""
    ti = ti if isinstance(ti, dict) else {}
    from . import capture
    if tool in ("Bash", "PowerShell") and _is_http(str(ti.get("command", ""))):
        return {"http": capture.http_capture(str(ti.get("command", "")), resp, cwd, redact,
                                             keep_body=getattr(redact, "keywords", {}).get("mode") == "secrets")}
    if tool == "Artifact":
        links = capture.artifact_links(resp)
        return {"artifact_links": links} if links else {}
    if tool in ("Bash", "PowerShell") and _PR_CREATE.search(str(ti.get("command", ""))):
        from . import gittrack  # PR link only: nothing else from the output is kept
        r = resp if isinstance(resp, dict) else {"stdout": str(resp or "")}
        urls = gittrack.pr_urls("%s\n%s" % (r.get("stdout") or "", r.get("stderr") or ""))
        return {"pr_urls": urls[:3]} if urls else {}
    return {}


_PR_CREATE = re.compile(r"\b(?:gh\s+pr\s+create|glab\s+mr\s+create|az\s+repos\s+pr\s+create)\b")


def _session_start_output(cfg, common, ev, inp):
    parts = []
    health = common.read_json(common.wl("health.json"))
    warn = _health_warning(health, common)
    ticket = ev.get("ticket")
    if ticket:
        ctx = common.read_text(common.wl("cache", "context", ticket + ".md")).strip()
        if ctx:
            parts.append(ctx)
    if warn:
        parts.append(warn)
    try:
        from . import plugin
        reminder = plugin.mcp_reminder()
    except Exception:
        reminder = ""
    if reminder:
        parts.append(reminder)
    out = {"hookEventName": "SessionStart"}
    if parts:
        out["additionalContext"] = common.truncate("\n\n".join(parts), 9500)
    if ticket and not inp.get("session_title"):
        repo = os.path.basename(ev.get("repo_root") or "") or os.path.basename(ev.get("cwd") or "")
        out["sessionTitle"] = "%s · %s" % (ticket, repo) if repo else ticket
    if len(out) > 1:
        sys.stdout.write(json.dumps({"hookSpecificOutput": out}, ensure_ascii=True))
        sys.stdout.flush()


def _health_warning(health, common):
    """One factual line, only when something is wrong."""
    msgs = list(health.get("warnings") or [])
    last_ok = common.parse_ts(health.get("last_tick_ok"))
    if last_ok and (common.now() - last_ok).total_seconds() > 2 * 3600:
        msgs.append("Worklog recorder status: no successful background run since %s."
                    % last_ok.strftime("%Y-%m-%d %H:%M"))
    return " ".join(msgs[:3])


def main():
    from . import common
    if common.internal():
        return
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    from . import plugin
    if not plugin.is_set_up():
        if event == "session-start":
            plugin.write_not_set_up()
        return
    if event == "pre-tool":  # the branch guard: never records anything
        from . import guard
        guard.main(_read_stdin())
        if common.env("HOOK_TIMING"):
            common.log("hook-timing", "%s %.1fms" % (event, (time.perf_counter() - _T0) * 1000))
        return
    if event in ("user-prompt", "stop"):
        _cg_mark(event)
    try:
        _capture(event, common)
    finally:
        if event == "session-start":
            plugin.after_session_start()


def _capture(event, common):
    if os.path.exists(common.wl("PAUSED")):
        return
    inp = _read_stdin()
    cwd = inp.get("cwd") or os.getcwd()
    cfg = common.load_config()
    if common.path_within(cwd, common.home()):
        _time_only(event, inp, cfg, common)
        return
    if not common.scope_allows(cwd, cfg):
        return
    repo_root = common.find_repo_root(cwd)
    if common.path_ignored(cwd) or (repo_root and common.path_within(repo_root, common.home())):
        return
    from .redact import redact

    sid = str(inp.get("session_id") or "unknown").replace("/", "_").replace("\\", "_")
    branch = common.read_branch(repo_root)
    prefixes = cfg.get("ticket_prefixes") or []
    ticket = common.ticket_from_branch(branch, prefixes)

    payload = {}
    if event == "session-start":
        payload = {"source": inp.get("source"), "model": inp.get("model"),
                   "session_title": inp.get("session_title")}
    elif event == "user-prompt":
        prompt = inp.get("user_prompt") or inp.get("prompt") or ""
        payload = {"prompt": redact(common.truncate(str(prompt), LIMITS["prompt"]))}
        if not ticket:
            found = common.find_tickets(str(prompt), prefixes)
            ticket = found[0] if found else None
    elif event == "post-tool":
        tool = str(inp.get("tool_name") or "")
        env = _qa_env(tool, inp.get("tool_input"), cfg, common)
        if env:  # QA data may stay literal locally: secrets-only masking for this call
            import functools
            redact = functools.partial(redact, mode="secrets")
        payload = {"tool": tool, "input": _key_input(tool, inp.get("tool_input"), common, redact, repo_root)}
        if env:
            payload["env"] = env
        try:
            oc = _outcome(tool, inp.get("tool_input"), inp.get("tool_response"), cwd, redact)
        except Exception:
            oc = {}
        if oc:
            payload["outcome"] = oc
    elif event == "stop":
        msg = inp.get("last_assistant_message") or ""
        if isinstance(msg, (list, dict)):
            msg = json.dumps(msg, ensure_ascii=False)
        msg = str(msg)
        payload = {"message": redact(common.truncate(msg, LIMITS["message"]))}
        if "worklog-" in msg:
            from . import blocks
            found, total = [], 0
            for kind, raw, _ in blocks.find(msg):
                if total + len(raw) <= blocks.MAX_BLOCKS_CHARS:
                    found.append({"kind": kind, "text": redact(raw)})
                    total += len(raw)
            if found:  # complete blocks survive even when the message itself is truncated
                payload["blocks"] = found
    elif event == "session-end":
        payload = {"reason": inp.get("reason")}

    ts = common.now_iso()
    ev = {
        "ts": ts, "event": event, "session_id": sid, "source": "hook",
        "transcript_path": inp.get("transcript_path"), "cwd": cwd, "repo_root": repo_root,
        "branch": branch, "ticket": ticket, "agent_type": inp.get("agent_type"),
        "payload": payload,
    }
    day = common.workday(common.now(), cfg)
    common.append_line(common.wl("raw", day, sid + ".jsonl"), json.dumps(ev, ensure_ascii=False))

    if event == "session-start":
        _session_start_output(cfg, common, ev, inp)
    if common.env("HOOK_TIMING"):
        common.log("hook-timing", "%s %.1fms" % (event, (time.perf_counter() - _T0) * 1000))


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:  # noqa: never break the session
        try:
            import traceback
            from . import common as _common
            from . import plugin as _plugin
            if not _plugin.is_set_up():
                raise SystemExit  # nothing is written before setup
            from .safety import paths as _paths
            _paths.append_text(_common.wl("logs", "hook-errors.log"), "%s %s %s\n" % (
                time.strftime("%Y-%m-%dT%H:%M:%S"), sys.argv[1:], traceback.format_exc().replace("\n", " | ")))
        except BaseException:
            pass
    os._exit(0)
