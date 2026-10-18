"""Compatibility name for the read-only MCP guard (`python -m chitragupta mcpguard`, older settings files).

The guard itself lives in chitragupta/safety/ (mcp.py, query.py, hook.py); this module only forwards to it, so an
old readonly-settings.json that still runs `… mcpguard` gets the same fail-closed decision.
"""
import sys

from .safety import hook, mcp, query

split_tool = mcp.split_tool
load_allow = mcp.load_allow


def sql_check(sql, max_rows=20, require_limit=True):
    return query.sql_reason(sql, max_rows, require_limit)


def mongo_check(inp, max_rows=20):
    return query.mongo_reason(inp)


def check(tool_name, tool_input, allow=None):
    """None (allowed) or the reason: the `mcp` profile of the safety guard."""
    if tool_name == "StructuredOutput":
        return None
    return mcp.mcp_reason(tool_name, tool_input, allow)


def main():
    return hook.internal("mcp")


if __name__ == "__main__":
    sys.exit(main())
