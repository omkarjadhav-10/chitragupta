"""Read-only Companion bridge, using the core JSON-RPC server protocol.

A separate optional server, so the shared core MCP registration stays unchanged. It exposes exactly the
companionviews operations (cached, profile-scoped projections): no mutation, model tool, job, query or
arbitrary tool forwarding. Requests are capped at 8 KiB and every result is a bounded envelope (≤ 64 KiB).
"""
import json
import sys

from . import companionviews, mcpserver

MAX_LINE = 8192
TOOL = {"name": "companion_read",
        "description": "Read cached, profile-scoped Chitragupta Companion projections (snapshot, planner, detail, "
                       "review, plan, changes, verification, jobs, handoff, database). Read-only.",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["operation"], "properties": {
            "operation": {"type": "string", "enum": list(companionviews.OPERATIONS)},
            "workflow": {"type": "string", "pattern": "^w_[a-f0-9]{64}$"},
            "ticket": {"type": "string", "maxLength": 40}, "plan": {"type": "string", "pattern": "^p_[a-f0-9]{64}$"},
            "base": {"type": "string", "maxLength": 80}, "offset": {"type": "integer", "minimum": 0},
            "cursor": {"type": "string", "maxLength": 1024}, "flow": {"type": "string", "maxLength": 50},
            "investigation": {"type": "string", "maxLength": 50}, "privacy": {"type": "boolean"},
            "pinned": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 40}},
            "group": {"type": "string", "maxLength": 40}}}}


class Server(mcpserver.Server):
    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "id" not in msg:
            return super().handle(msg)
        method = msg.get("method")
        if method not in {"tools/list", "tools/call"}:
            return super().handle(msg)
        result = {"tools": [] if self.internal else [TOOL]}
        if method == "tools/call":
            params = msg.get("params")
            if not isinstance(params, dict) or self.internal or params.get("name") != TOOL["name"]:
                return mcpserver._error(msg["id"], -32602, "Unknown tool")
            try:
                data = companionviews.read(params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                          "structuredContent": data, "isError": False}
            except (ValueError, PermissionError) as exc:
                result = {"content": [{"type": "text", "text": str(exc)[:300]}], "isError": True}
            except Exception:  # noqa: BLE001 - never leak internals through the bridge
                result = {"content": [{"type": "text", "text": "Companion read unavailable"}], "isError": True}
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


def handle_line(server, line):
    """One request line -> reply (None for notifications). Oversized input is refused before parsing."""
    if len(line.encode("utf-8")) > MAX_LINE:
        return mcpserver._error(None, -32600, "Request too large")
    return server.handle_line(line)


def serve():
    server = Server()
    for line in sys.stdin:
        reply = handle_line(server, line)
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    serve()
