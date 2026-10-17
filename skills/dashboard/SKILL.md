---
name: dashboard
description: Open Chitragupta's local dashboard in the browser, starting it first if it isn't running.
disable-model-invocation: true
argument-hint: "[day YYYY-MM-DD | ticket KEY | page]"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" dashboard open *)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" dashboard open *)
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" dashboard open)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" dashboard open)
---

# /cg:dashboard

Run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" dashboard open [ROUTE]` once. It starts the server in the background when it isn't running (or replaces an older copy) and opens the page in the default browser.

ROUTE comes from `$ARGUMENTS`: `day/<YYYY-MM-DD>` for a day, `tickets/<KEY>` for a ticket, or a page name (`timesheet`, `knowledge`, `search`, `health`, `setup`, `settings`). Leave it out for Today. Use only those shapes; never pass anything else.

Give the address from the output on its own line (it is `http://127.0.0.1:<port>/…`, local only). If the output says another program holds the port, say so and suggest `/cg:doctor`. If the command failed because this version has no dashboard, say that in one line and suggest `/cg:today` or `/cg:ticket` instead. Don't start anything else.
