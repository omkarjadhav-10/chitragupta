@echo off
rem Chitragupta launcher for hooks and /cg: commands on Windows: py -3, else python.
rem Hooks fail open: without a Python, `cg-hook hook <event>` exits 0 silently.
where /q py && goto py
where /q python && goto python
if /i "%~1"=="hook" exit /b 0
echo Chitragupta: Python 3.10+ not found (install it from python.org) 1>&2
exit /b 9009
:py
py -3 "%~dp0..\hooks\run.py" %*
exit /b %errorlevel%
:python
python "%~dp0..\hooks\run.py" %*
exit /b %errorlevel%
