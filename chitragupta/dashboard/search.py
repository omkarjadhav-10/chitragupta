"""The dashboard's search: a thin wrapper over chitragupta/searchidx.py (the one persistent index, shared with
`/cg:search` and the MCP server). The dashboard only reads it: when the file is missing or behind the data home,
searchidx answers from an up-to-date copy in memory and nothing is written."""
from ..searchidx import (  # noqa: F401 — re-exported for the server and the tests
    LABELS, MAX_QUERY, MAX_TERMS, PER_GROUP, SNIPPET, TITLE_WEIGHT, TYPE_ORDER, TYPES, default_engine, fold,
    fts5_available, fts_expr, invalidate, parse_query, reset, search, snippet, stats, tokens,
)
