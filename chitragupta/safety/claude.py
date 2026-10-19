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
# This plugin (name `cg`, marketplace `chitragupta`) is switched off inside every internal call as well, so its
# read-only MCP server never starts there (--strict-mcp-config already leaves plugin servers out). The server itself
# also lists no tools when CHITRAGUPTA_INTERNAL=1.
OWN_PLUGIN = "cg@chitragupta"


def guard_settings(profile, cfg=None):
    """Inline --settings JSON: the fail-closed PreToolUse guard for this profile."""
    if profile not in PROFILES:
        raise ValueError("unknown guard profile %r" % profile)
    # The launcher of *this* package (plugin hooks/run.py, or the runtime copy's run.py), never the stable launcher:
    # the settings are inline and per call, and an older runtime copy may not know this guard yet.
    from .. import plugin
    py = common.python_exe(cfg)
    hook = {"type": "command", "command": py, "args": [plugin.own_launcher(), "safety", "internal", profile],
            "timeout": 15, "onFailure": "block"}
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}, "enabledPlugins": {OWN_PLUGIN: False}},
                      separators=(",", ":"))


def build(exe, prompt, profile, model, system_prompt_file=None, allowed=(), disallowed=(), mcp_config=None,
          json_schema=None, output="json", max_turns=None, cfg=None):
    """argv for one internal call. `exe` is resolve_claude()'s list (claude.exe, or node + cli.js). Always
    --strict-mcp-config: only the servers in `mcp_config` (none without it) load."""
    args = list(exe) + ["-p", prompt, "--model", model]
    if system_prompt_file:
        args += ["--system-prompt-file", system_prompt_file]
    args += ["--tools", "", "--setting-sources", "project", "--settings", guard_settings(profile, cfg),
             "--permission-mode", "dontAsk", "--permission-prompts", "none",
             "--no-session-persistence", "--disable-slash-commands", "--output-format", output]
    if output == "stream-json":
        args.append("--verbose")
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
