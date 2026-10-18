"""The one builder for internal `claude -p` calls.

Every call gets: no built-in tools (`--tools ""`), an explicit `--allowedTools` (empty for extraction and
consolidation), every write tool on `--disallowedTools`, `--permission-mode dontAsk` + `--permission-prompts none`
(anything not pre-approved is denied, nobody is asked), only the MCP servers the job needs (`--strict-mcp-config`),
the sandbox folder as working directory, and settings whose PreToolUse hook runs the safety guard for the job's
profile with `"onFailure": "block"` — if the guard errors or times out, the tool call is denied.
It never emits --dangerously-skip-permissions or a bypass permission mode; the command guard re-checks all of this.
"""
import json

from .. import common
from .commands import BUILTIN_WRITES

PROFILES = ("none", "mcp", "slack-send", "slack-discover")


def guard_settings(profile, cfg=None):
    """Inline --settings JSON: the fail-closed PreToolUse guard for this profile."""
    if profile not in PROFILES:
        raise ValueError("unknown guard profile %r" % profile)
    py, launcher = common.self_command("safety", cfg=cfg)[:2]
    hook = {"type": "command", "command": py, "args": [launcher, "safety", "internal", profile], "timeout": 15,
            "onFailure": "block"}
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}}, separators=(",", ":"))


def build(exe, prompt, profile, model, system_prompt_file=None, allowed=(), disallowed=(), mcp_config=None,
          strict=True, json_schema=None, output="json", max_turns=None, cfg=None):
    """argv for one internal call. `exe` is resolve_claude()'s list (claude.exe, or node + cli.js)."""
    args = list(exe) + ["-p", prompt, "--model", model]
    if system_prompt_file:
        args += ["--system-prompt-file", system_prompt_file]
    args += ["--tools", "", "--setting-sources", "project", "--settings", guard_settings(profile, cfg),
             "--permission-mode", "dontAsk", "--permission-prompts", "none",
             "--no-session-persistence", "--disable-slash-commands", "--output-format", output]
    if output == "stream-json":
        args.append("--verbose")
    if strict:
        args.append("--strict-mcp-config")
        if mcp_config:
            args += ["--mcp-config", mcp_config]
    allowed = [t for t in allowed if t]
    args += ["--allowedTools", ",".join(allowed)]  # always explicit; "" = nothing pre-approved
    args += ["--disallowedTools", ",".join(list(BUILTIN_WRITES) + [t for t in disallowed if t and t not in allowed])]
    if max_turns:
        args += ["--max-turns", str(max_turns)]
    if json_schema is not None:
        args += ["--json-schema", json_schema if isinstance(json_schema, str) else
                 json.dumps(json_schema, separators=(",", ":"))]
    return args
