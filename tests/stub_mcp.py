"""Minimal stdio MCP server standing in for "the database MCP" in tests: a SQLite 'QA' database with a synthetic
`shop` schema (customers, orders) and a trigger that locks a customer whose role is set to NULL.

Line-delimited JSON-RPC 2.0 on stdin/stdout. Methods: initialize, tools/list, tools/call, ping (notifications without
an id are ignored; unknown methods -> -32601). Tools: whoami, list_connections, list_schemas, list_tables,
describe_table, run_query (executes whatever it is given - the read-only guard must stop writes before they get here),
create_artifact (a write tool). Every call is appended to the file named by $STUB_MCP_LOG so tests can prove what
reached the server. No network.

The response shapes follow a typical database MCP: describe_table returns one dict per column (dataType, nullable,
isPrimaryKey, isForeignKey, references, isPhiField), run_query returns "N row(s) in Xms" followed by JSON rows.
"""
import json
import os
import sqlite3
import sys

DB = sqlite3.connect(":memory:")
DB.executescript("""
CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, status TEXT, is_locked INTEGER, role TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), status TEXT);
CREATE TRIGGER trg_lock AFTER UPDATE OF role ON customers WHEN NEW.role IS NULL
  BEGIN UPDATE customers SET is_locked = 1 WHERE id = NEW.id; END;
INSERT INTO customers VALUES (1000417, 'dev.test1@acme.test', 'APPROVED', 1, NULL);
INSERT INTO customers VALUES (1000095, 'other@acme.test', 'INACTIVE', 1, NULL);
""")
for i in range(1, 21):
    DB.execute("INSERT INTO orders (customer_id, status) VALUES (?, ?)", (1000417 if i % 2 else 1000095, "LOCKED"))
DB.commit()

SCHEMAS = ("shop_audit.", "shop.")

TOOLS = [
    {"name": "whoami", "description": "Current user", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_connections", "description": "List database connections", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_schemas", "description": "List schemas", "inputSchema": {"type": "object", "properties": {
        "connectionId": {"type": "string"}}, "required": ["connectionId"]}},
    {"name": "list_tables", "description": "List tables", "inputSchema": {"type": "object", "properties": {
        "connectionId": {"type": "string"}, "schema": {"type": "string"}}, "required": ["connectionId"]}},
    {"name": "describe_table", "description": "Columns, keys, indexes and triggers of a table", "inputSchema": {
        "type": "object", "properties": {"connectionId": {"type": "string"}, "table": {"type": "string"}},
        "required": ["connectionId", "table"]}},
    {"name": "run_query", "description": "Run a SQL query", "inputSchema": {"type": "object", "properties": {
        "connectionId": {"type": "string"}, "sql": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["connectionId", "sql"]}},
    {"name": "create_artifact", "description": "Create a saved artifact (writes)", "inputSchema": {
        "type": "object", "properties": {"title": {"type": "string"}, "content": {"type": "string"}}, "required": ["title"]}},
]


def log(rec):
    p = os.environ.get("STUB_MCP_LOG")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


def call(name, a):
    log({"tool": name, "args": a})
    if name == "whoami":
        return {"user": "stub"}
    if name == "list_connections":
        return [{"id": "qa-main", "name": "Shop QA", "env": "QA", "type": "postgres"},
                {"id": "prod-main", "name": "Shop PROD", "env": "PROD", "type": "postgres"}]
    if name == "list_schemas":
        return ["shop"]
    if name == "list_tables":
        return ["shop.customers", "shop.orders"]
    if name == "describe_table":  # one dict per column, FKs carry `references`
        t = a.get("table", "").split(".")[-1]
        fks = {r[3]: "%s.%s" % (r[2], r[4]) for r in DB.execute("PRAGMA foreign_key_list(%s)" % t)}
        return [{"name": r[1], "dataType": r[2], "nullable": not r[3], "defaultValue": r[4], "isPrimaryKey": bool(r[5]),
                 "isForeignKey": r[1] in fks, "references": fks.get(r[1]), "isPhiField": r[1] == "email"}
                for r in DB.execute("PRAGMA table_info(%s)" % t)]
    if name == "run_query":  # "N row(s) in Xms" + JSON rows
        sql = a.get("sql", "")
        low = sql.lower()
        if "pg_trigger" in low:
            out = [{"table_name": r[0], "trigger_name": r[1], "trigger_def": r[2], "function_src": r[2]}
                   for r in DB.execute("SELECT tbl_name, name, sql FROM sqlite_master WHERE type = 'trigger'")]
        elif "pg_proc" in low:
            out = []
        else:
            for s in SCHEMAS:
                sql = sql.replace(s, "")
            cur = DB.execute(sql)
            cols = [d[0] for d in cur.description or []]
            out = [dict(zip(cols, r)) for r in cur.fetchall()]
        return "%d row(s) in 1ms\n\n%s" % (len(out), json.dumps(out, indent=2))
    if name == "create_artifact":
        return {"created": True}
    raise ValueError("unknown tool " + name)


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            res = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "stub-sqldb", "version": "1"}}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                out = call(p.get("name"), p.get("arguments") or {})
                res = {"content": [{"type": "text", "text": out if isinstance(out, str) else json.dumps(out)}], "isError": False}
            except Exception as e:  # noqa: BLE001
                res = {"content": [{"type": "text", "text": "error: %s" % e}], "isError": True}
        elif method == "ping":
            res = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no method"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
