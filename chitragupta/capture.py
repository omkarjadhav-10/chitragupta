"""Hook-side capture helpers for the replay / DB-flow / artifact features. Loaded only when needed,
so hooks for ordinary events stay as light as before. Everything returned here is redacted."""
import os
import re

CONTENT_MAX = 200_000
# Write/Edit content is kept for requests, payloads and artifact-type text files
CONTENT_EXT = (".bru", ".http", ".rest", ".json", ".md", ".markdown", ".html", ".htm", ".mmd", ".mermaid", ".puml",
               ".plantuml", ".svg", ".sql", ".yaml", ".yml", ".txt", ".csv", ".drawio", ".xml")
SCRIPT_EXT = (".py", ".ps1", ".sh", ".js", ".mjs", ".ts")
SKIP_DIRS = ("/node_modules/", "/target/", "/bin/", "/obj/", "/dist/", "/build/", "/.idea/", "/.vs/", "/.git/")
LINK_RX = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+")


def want_content(path, repo_root):
    p = (path or "").replace("\\", "/")
    pl = p.lower()
    if any(d in pl for d in SKIP_DIRS):
        return False
    if pl.endswith(CONTENT_EXT):
        return True
    # investigation scripts: only outside git repos (scratchpads, temp folders)
    return pl.endswith(SCRIPT_EXT) and (not repo_root or "/temp/" in pl or "/scratchpad/" in pl)


def file_content(tool, ti, redact, truncate):
    if tool == "Write":
        c = ti.get("content")
    elif tool == "Edit":
        c = ti.get("new_string")
    elif tool == "MultiEdit":
        c = "\n…\n".join(str(e.get("new_string", "")) for e in ti.get("edits") or [] if isinstance(e, dict))
    else:
        return None
    if not isinstance(c, str) or not c:
        return None
    return redact(truncate(c, CONTENT_MAX))


BODY_MAX = 50_000  # qa_data.max_response_kb


def http_capture(cmd, tool_response, cwd, redact, keep_body=False):
    """Outcome of an HTTP-like command: statuses + key skeleton (never values), plus body files it sent.
    keep_body (QA calls only): the response text too (<= 50 KB, secrets masked) as the expected result for replay."""
    from . import apicalls
    files = {}
    for m in re.finditer(r"(?:--data-binary|--data|-d|--json|--data-raw|-T|--upload-file)[ =]+[\"']?@([^\s\"']+)", cmd or ""):
        p = m.group(1)
        txt = apicalls._read_file(p, cwd)
        if txt is not None:
            files[p] = redact(txt)
    reqs = apicalls.parse_command(cmd, cwd, files)
    meta = apicalls.outcome_meta(cmd, tool_response, reqs)
    out = {"statuses": meta["statuses"], "response_keys": meta["response_keys"], "token_path": meta["token_path"],
           "transport_error": meta["transport_error"], "requests": len(reqs)}
    if files:
        out["files"] = files
    if keep_body:
        body = apicalls.response_text(tool_response) or ""
        if body and len(body.encode("utf-8")) <= BODY_MAX:
            out["body"] = redact(body)
    return out


def artifact_links(obj):
    import json
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(obj)
    return sorted(set(LINK_RX.findall(s or "")))
