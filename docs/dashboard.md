# Dashboard

The dashboard is a read-only page served from your machine. It shows what Chitragupta has recorded. It lives at `http://127.0.0.1:7419/` by default.

## Start, stop, address

| How | What happens |
|---|---|
| `/cg:dashboard` | Starts the dashboard if it isn't running and prints its address |
| Automatically | The `Chitragupta` scheduled task (at logon and every 5 minutes) and the SessionStart maintenance step (at most every 10 minutes) start it if it's down, or replace an older version. Turn this off with `dashboard.autostart: false` or `CHITRAGUPTA_DASHBOARD=0`. |
| `… dashboard ensure [--json]` | Same as `/cg:dashboard`. It waits up to 6 s and prints `Dashboard started: http://127.0.0.1:7419/` |
| `… dashboard stop [--json]` | Stops *our* server on the port. A foreign app on that port is left alone. |
| `… dashboard status [--json]` | Reports who answers on the port: `down`; `ours` (with version, pid and data home); or `foreign` |
| `… dashboard url [ROUTE]` | Prints a link, for example `dashboard url tickets/ABC-1001` |
| `… dashboard serve [--port N]` | Runs it in the foreground, for debugging |

`…` is the stable launcher, `py -3 "$env:USERPROFILE\.chitragupta\runtime\run.py"`.

The server runs detached and windowless. Its log is `<home>\.worklog\logs\dashboard.log`, and it is cleared on each start.

**One server per port.** The port acts as the lock. `/api/health` answers `{"app": "chitragupta", "version", "pid", "home"}`, and a newer plugin version replaces an older server. A server for a *different* data home, or another app on the port, is never touched. Instead, `ensure` reports that the port is taken; pick another with `dashboard.port`.

## Pages

| Route | Shows |
|---|---|
| `#/today` | Time per ticket so far today, and tonight's Jira-worklog entries with a copy button for each entry's start, duration and description, plus one for the whole message. Before 20:00 this is a live preview. It uses no model and no Jira and saves nothing, so its descriptions are uncondensed and time already in Jira isn't subtracted. Once the day is composed, it shows the saved message. |
| `#/tickets` | A searchable list of tickets with title, last activity, time and counts (`#/tickets?q=checkout`) |
| `#/tickets/ABC-1001` (or `#/ticket/ABC-1001`) | One ticket: findings, saved requests with *copy as curl*, the DB flow drawn with Mermaid (including the QA worked example when there is one), queries, artifacts, timeline and file paths |
| `#/timesheet` | Hours per ticket by ISO week (`?period=month` for months), leave and holidays, and a CSV export |
| `#/health` | Last hook event, last tick, last push, Slack send status, MCP servers seen by the last jobs, MCP calls and tokens today, recent errors, and whether capture is paused |

**Updates.** The page checks `/api/changes` every 10 seconds while the tab is visible. It redraws only when something changed, and it keeps your scroll position, focus and open sections.

**Themes.** Light and dark follow your system setting, and the toggle cycles *auto → light → dark*. The page works fully with the keyboard: there is a skip link, real links and buttons, and visible focus.

**Offline.** The page is one self-contained file with no CDN and no web fonts. Mermaid is vendored (see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)) and served at `/vendor/mermaid.min.js`. If that file is missing, diagrams show their source text instead.

## API

The page uses these endpoints. Every one of them is GET/HEAD and returns JSON unless noted.

| Path | Returns |
|---|---|
| `/` | The page |
| `/api/health` | App, version, pid and data home |
| `/api/today` | Today's tickets and minutes, the saved worklog or a preview |
| `/api/tickets?q=` | Ticket list |
| `/api/tickets/<KEY>` | One ticket. A malformed key gets 400; an unknown key gets 404. |
| `/api/timesheet?period=week\|month` | Hours table and calendar notes |
| `/api/timesheet.csv?period=` | CSV in long format (`period,ticket,title,minutes,hours`), UTF-8 with BOM. Values that look like spreadsheet formulas are escaped. |
| `/api/status` | Health page data |
| `/api/changes` | A cursor that changes when any file the page reads changes |

## Security model

The dashboard shows your worklog, so it is locked to your own browser on your own machine:

- **Bound to `127.0.0.1` only**, with exclusive address use, so it is never reachable from the network.
- **Host header check.** A request is answered only when its `Host` is exactly `127.0.0.1:<port>` or `localhost:<port>`. A missing Host is rejected. That blocks DNS-rebinding attacks. If an `Origin` header is present, it must match too.
- **Read-only.** Only GET and HEAD are accepted; any other method gets 405. There is no endpoint that writes data.
- **No CORS.** Other sites can't read responses.
- **Headers on every response, errors included:** a strict Content-Security-Policy, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Cross-Origin-Resource-Policy: same-origin`.
- **No file serving.** Only the page and the vendored Mermaid file are served, so a crafted path can't reach any other file.
- **Robust to bad input.** A malformed request gets an error status (400, 414 or 505), never a crash. Sockets time out after 15 s.
- **Shutdown is guarded.** `/api/shutdown` needs a random token that only your user account can read (`%USERPROFILE%\.chitragupta\dashboard\<port>.token`). `dashboard stop` uses it.
- **No outgoing requests.** The server doesn't call out, and the page doesn't either. Mermaid runs with `securityLevel: strict`.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `dashboard.port` (config) | `7419` | Port on `127.0.0.1` |
| `CHITRAGUPTA_DASHBOARD_PORT` (env) | — | Overrides the port |
| `dashboard.autostart` (config) | `true` | Start from the scheduled task and SessionStart maintenance |
| `CHITRAGUPTA_DASHBOARD` (env) | — | `0` / `off` turns autostart off |

## Known gaps in v0.1.0

- **MCP status is not live.** The Health page reports the servers the *last* background job saw; it doesn't run a live check. `/cg:doctor` does a live check.
- **The Today preview is not the final message.** It isn't condensed, and time already in Jira isn't subtracted, so it can differ from what is sent at 20:00.
- **Only today has a Today view.** There is no page for a past day: the `/cg:dashboard day <date>` deep link opens Today. Use `/cg:today <date>` for a past day.
- **The Markdown renderer is basic.** Links show as text.
- **No settings page.** The dashboard can't change configuration (see the [roadmap](roadmap.md)).
