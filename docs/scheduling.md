# Scheduling: the background run on each OS

Chitragupta's background run (`agent tick`) turns captured events into daily files, ticket notes and the 20:00 message. It runs every 5 minutes and at login, through one backend per OS. `chitragupta/scheduler.py` is the one interface: `/cg:setup` registers it, `setup scheduler --repair` (and the dashboard's Settings) re-register it when it is missing or differs, `/cg:doctor` reports it, and uninstall removes it.

| OS | Backend | Files | Runs |
|---|---|---|---|
| Windows | Task Scheduler task `Chitragupta` | the task (XML in `.worklog\cache\`); if Task Scheduler refuses it, one hidden launcher `Chitragupta.vbs` in your Startup folder | every 5 minutes and at logon |
| macOS | launchd LaunchAgent `dev.chitragupta.tick` | `~/Library/LaunchAgents/dev.chitragupta.tick.plist` | every 5 minutes (`StartInterval` 300) and at login (`RunAtLoad`), as a background process |
| Linux | systemd user timer | `~/.config/systemd/user/chitragupta.service` and `chitragupta.timer` | every 5 minutes on the clock, 1 minute after login, and a run missed while asleep or off is caught up (`Persistent=true`) |
| Linux without a systemd user session | cron | one line in your crontab, marked `# chitragupta` | every 5 minutes |

Every backend runs the same command: the Python that set it up, Chitragupta's stable launcher (`~/.chitragupta/current/hooks/run.py`), `agent tick --from-task`, in the data home. launchd, systemd and cron start with a short `PATH`, so the job sets one that includes the Python's folder, `~/.local/bin` (Claude Code's native install), `/opt/homebrew/bin` and `/usr/local/bin`.

## Commands

```
python -m chitragupta setup scheduler            # status: registered, up to date, last result
python -m chitragupta setup scheduler --repair   # register again if missing or different
```

The status says *current* when the job is exactly what setup would write now (for example after the runtime moved), and *differs: repair* otherwise.

## Run records

Every background run appends one line to `.worklog\logs\ticks.jsonl`: `{"at", "outcome", "ms"}`, where `outcome` is `ok`, `error`, `skipped-busy` (another run held the agent lock) or `skipped-upgrade` (an upgrade was in progress). A skipped run can't write `health.json` (the run holding the lock owns it), so this file is where skips show; the run that holds the lock keeps it to the last 50 lines.

When Claude Code hooks recorded events in the last hour but no background run has succeeded for over 30 minutes, SessionStart, `/cg:doctor` (the *last tick* row), the Health page and the MCP server's `get_alerts` show one warning line, with how the runs since the last good one ended (for example `skipped-busy x6`). A new install gets 30 minutes before it counts.

## What Chitragupta may run here

Only its own job, enforced by the command and path guards ([safety.md](safety.md)):

- `launchctl print | bootout gui/<uid>/dev.chitragupta.tick` and `launchctl bootstrap gui/<uid> <its plist>`; nothing else.
- `systemctl --user` with `daemon-reload`, or `enable | start | stop | disable | is-enabled | is-active | status` of `chitragupta.service` / `chitragupta.timer`; never system units, never another unit.
- `crontab -l`, and `crontab -` only with a table whose other lines are exactly your current ones (the guard reads `crontab -l` itself and compares) and at most one `# chitragupta` line.
- Files: the plist and the two unit files, and creating their folders when they don't exist yet.

## Defaults on macOS and Linux

The data home is `~/Chitragupta` and the runtime `~/.chitragupta`, as on Windows. The background job uses the Python 3 that ran setup.

## Limits

- **Hooks still start with `py -3`.** The plugin's hooks and `/cg:` commands use the Windows Python launcher. The `bin/cg-hook` launcher (python3, then python; `py -3` on Windows) is in the plugin, but switching `hooks.json` and the skills to it waits for a check in a live Claude Code session on each OS, because the `/cg:` safety guard is a fail-closed hook. Until then the scheduler backends are ready on macOS and Linux, but the hooks there won't start.
- macOS and Linux are covered by tests with a fake `launchctl` / `systemctl` / `crontab`; they have not been run on a real Mac or Linux machine yet.
