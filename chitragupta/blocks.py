"""`worklog-requests` / `worklog-dbflow` fenced blocks that Claude emits in a reply when the user asks to save
requests or map a ticket's DB flow. Parsed by the agent before extraction, validated, ingested exactly,
and stripped from the digest so extraction doesn't count them twice."""
import json
import os
import re

BLOCK_RX = re.compile(r"```[ \t]*worklog-(requests|dbflow)[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
MAX_BLOCKS_CHARS = 60_000


def find(text):
    """[(kind, raw_json_text, span)]"""
    return [(m.group(1), m.group(2), m.span()) for m in BLOCK_RX.finditer(text or "")]


def strip(text, note=True):
    def repl(m):
        return "[worklog-%s block: ingested separately]" % m.group(1) if note else ""
    return BLOCK_RX.sub(repl, text or "")


def _schema(kind):
    from . import common
    p = common.prompt_path("schemas/block_%s.json" % kind)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def parse(kind, raw):
    """(obj, errors). Strict JSON; validated against prompts/schemas/block_<kind>.json."""
    try:
        obj = json.loads(raw)
    except ValueError as e:
        return None, ["invalid JSON: %s" % e]
    if not isinstance(obj, dict):
        return None, ["top level must be an object"]
    sch = _schema(kind)
    if sch:
        from . import extract
        errs = extract.validate(obj, sch)
        if errs:
            return None, errs[:5]
    return obj, []
