@echo off
rem Chitragupta's read-only MCP server (stdio), started by Claude Code from the plugin's .mcp.json.
rem Windows: the py launcher (py -3), else python. Nothing but JSON-RPC goes to stdout.
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0..\hooks\run.py" mcp %*
  exit /b
)
python "%~dp0..\hooks\run.py" mcp %*
