---
name: dashboard
description: Start Chitragupta's local read-only dashboard if it isn't running and print its address.
disable-model-invocation: true
argument-hint: "[day YYYY-MM-DD | ticket KEY]"
allowed-tools:
  - Bash(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" dashboard ensure)
  - PowerShell(py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" dashboard ensure)
---

# /cg:dashboard

Run `py -3 "${CLAUDE_PLUGIN_ROOT}/hooks/run.py" dashboard ensure` once. It starts the server in the background when it isn't running (or replaces an older copy) and returns at once.

Give the address from the output on its own line (it is `http://127.0.0.1:<port>/`, local only). If they named a day or ticket (`$ARGUMENTS`), add the deep link: `#/day/<YYYY-MM-DD>` or `#/ticket/<KEY>` after the address.

If the output says another program holds the port, say so and suggest `/cg:doctor`. If the command failed because the dashboard isn't available in this version, say that in one line and suggest `/cg:today` or `/cg:ticket` instead. Don't start anything else.
