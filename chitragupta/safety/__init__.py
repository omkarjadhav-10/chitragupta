"""Chitragupta's safety layer: the only code that starts a process, changes a file or opens a network connection.

  commands  run(argv, purpose) / spawn() — the command guard (git, gh / glab / az, claude, self, schtasks)
  paths     write_text / append_text / replace / copy / delete … — the path guard (own write roots only)
  query     sql_reason / mongo_reason — the query guard
  mcp       per-profile tool decisions — the MCP tool guard
  claude    build() — every internal `claude -p` call
  hook      PreToolUse entry points (internal calls; /cg: commands in your sessions)
  net       GET-only HTTP (GitHub privacy check, the local dashboard)
  log       security.log — every block, shown on Health and by /cg:doctor

There is no setting, flag or environment variable that turns any of it off. Kept import-light: hooks load it.
"""
from .log import Blocked  # noqa: F401
