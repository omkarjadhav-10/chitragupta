# Dashboard

The dashboard is a page served from your own machine. It shows what Chitragupta has recorded: today, your tickets, your timesheet, the knowledge it has gathered and its own health. It lives at `http://127.0.0.1:7419/` by default, works offline and only reads your data.

![The Today page with synthetic data](images/dashboard-today.png)

## Open it

| How | What happens |
|---|---|
| `/cg:dashboard [day YYYY-MM-DD \| ticket KEY \| page]` | Starts the dashboard if it isn't running and opens it in your browser |
| Start menu | Setup adds a **Chitragupta** shortcut to the Start menu. `… dashboard shortcut --desktop` adds one to the Desktop too. |
| As an app | In Chrome or Edge, use *Install Chitragupta* in the address bar (or *Apps → Install this site as an app*). It then opens in its own window and appears in the Start menu with Today, Tickets, Timesheet and Health as jump-list shortcuts. |
| Automatically | The `Chitragupta` scheduled task (at logon and every 5 minutes) and the SessionStart maintenance step (at most every 10 minutes) start it if it's down, or replace an older version. Turn this off with `dashboard.autostart: false` or `CHITRAGUPTA_DASHBOARD=0`. |

The command line, where `…` is the stable launcher `py -3 "$env:USERPROFILE\.chitragupta\runtime\run.py"`:

| Command | Does |
|---|---|
| `… dashboard open [ROUTE] [--no-browser]` | Starts it when needed (waits up to 6 s) and opens the page, for example `dashboard open tickets/ABC-1001` |
| `… dashboard ensure [--json]` | Starts it when needed and prints `Dashboard started: http://127.0.0.1:7419/` |
| `… dashboard stop [--json]` | Stops *our* server on the port. A foreign app on that port is left alone. |
| `… dashboard status [--json]` | Who answers on the port: `down`; `ours` (with version, pid and data home); or `foreign` |
| `… dashboard url [ROUTE]` | Prints a link |
| `… dashboard shortcut [--desktop]` | Writes the Start menu shortcut again (and one on the Desktop) |
| `… dashboard serve [--port N]` | Runs it in the foreground, for debugging |

The server runs detached and windowless. Its log is `<home>\.worklog\logs\dashboard.log`, cleared on each start. **One server per port:** the port is the lock. `/api/health` answers `{"app": "chitragupta", "version", "pid", "home"}`, and a newer plugin version replaces an older server. A server for a different data home, or another app on the port, is never touched; `ensure` says the port is taken, and you pick another with `dashboard.port`.

The installed app window and the shortcuts only work on this machine: the server listens on `127.0.0.1` and nowhere else. There is no offline copy of your data: when the server isn't running, the window says so instead of showing old numbers.

## Layout

- **Top bar:** the command field (<kbd>Ctrl</kbd> <kbd>K</kbd>), status for capture, Slack and each MCP server (each an icon and words, linking to Health), and the theme switch.
- **Navigation** down the left: Today, Tickets, Timesheet, Knowledge, Search, Health, and at the bottom Setup and Settings. *Show labels* (or <kbd>[</kbd>) widens it; the choice is remembered in this browser.
- **Side list:** each page's own list or filters (the day's tickets, ticket filters, months, tables, result types). Below 1040 px it folds into a *Show …* button above the page.
- **The page.**

## Pages

| Route | Shows |
|---|---|
| `#/today` | A ring of time tracked against the day's target; delivery alerts across all tickets; **the day, ticket by ticket**: one lane per ticket with the stretches you worked and a tick for each commit (also as a table); **tonight's Jira entries** with a copy button for each start, duration and description and *Copy all* for the whole message. Before 20:00 this is a live preview: it uses no model and no Jira and saves nothing, so descriptions are uncondensed and time already in Jira isn't subtracted. Once the day is composed, it shows the saved message and whether it was sent. |
| `#/day/YYYY-MM-DD` | The same for any past day (previous and next day buttons, <kbd>p</kbd> / <kbd>n</kbd>) |
| `#/tickets` | Every ticket, sortable by key, title, status, last activity, time, findings and requests. Filter by text, status, repo, *only with delivery alerts* and last active; the filters live in the address, so a filtered list can be bookmarked. Long lists scroll without slowing down. |
| `#/tickets/ABC-1001` | One ticket with a header that stays in view (title, Jira status, repo and branch chips, time, alerts) and tabs: **Overview** (in short, the problem, findings grouped by kind: root cause, fix, decisions, open questions…), **Branches & PRs** (the branch graph, delivery matrix, branches and PRs; see [branch-tracking.md](branch-tracking.md)), **DB flow** (with the QA worked example), **Requests** (copy as curl), **Queries**, **Artifacts**, **Timeline** (each day links to that day) and **Notes** (the ticket's README and its files). *Export* and *Export to share* download the page as one HTML file. |
| `#/timesheet` | A month calendar shaded by hours against each day's target, with leave, half days, holidays and extra workdays named in each cell; click a day to see its tickets. Weekly bars against the target, and hours per ticket by ISO week or month with *Download CSV*. |
| `#/knowledge` | **DB map:** the map of every table and link, zoom and pan; pick a table to see its columns, its neighbourhood and which links the QA link map verified. **APIs:** the catalog with method, path, host, direction, purpose and tickets, filterable. **Integrations.** |
| `#/search?q=` | Everything at once: tickets, findings, queries, requests, artifacts, branches, PRs, APIs, tables, DB links, integrations and daily notes, grouped by type with the matching words marked. <kbd>↑</kbd> <kbd>↓</kbd> move through the results. See [Search](#search). |
| `#/health` | Capture, background runs, the daily push, Slack, the scheduled task, **Safety** (enforced or not, and every action the safety layer blocked in the last 24 h and 7 days, from `security.log`; see [safety.md](safety.md)), each configured MCP server (connected, signed out, missing or switched off, from the last cached `claude mcp list`), today's MCP jobs and tokens against the budget, warnings and recent errors. *Copy diagnostics* copies all of it as text. |
| `#/setup` | The `/cg:doctor` checks as a checklist, each with what to run to fix it |
| `#/settings` | Appearance: theme, density and motion, saved in this browser |

## Search

One index serves the Search page, the command palette, `/cg:search` and the MCP server: `.worklog\cache\search.sqlite`, a local cache the background tick brings up to date after every render (only the files that changed are re-read). The dashboard only reads it: when the file is missing or a tick hasn't caught up yet, it answers from an up-to-date copy in memory and writes nothing. Results come grouped by type, best first, and a query that is exactly a ticket key puts that ticket first. Filter with `type`, `ticket`, `repo` and a date range (`from`, `to`) on `/api/search`. What is indexed, the ranking and rebuilding are in [search.md](search.md).

## Command palette

<kbd>Ctrl</kbd> <kbd>K</kbd> (or <kbd>/</kbd>) opens it from anywhere. Type part of a page, ticket, table, API or word: it lists matching pages, tickets and actions, then what search finds in your worklog. <kbd>↑</kbd> <kbd>↓</kbd> move, <kbd>Enter</kbd> opens, <kbd>Esc</kbd> closes. Actions: switch theme, show or hide navigation labels, copy tonight's Jira message, download the timesheet CSV, show the keyboard shortcuts.

## Keyboard shortcuts

<kbd>?</kbd> lists them on any page. Shortcuts are off while you type in a field; <kbd>Ctrl</kbd> <kbd>K</kbd> always works.

| Keys | Does |
|---|---|
| <kbd>Ctrl</kbd> <kbd>K</kbd> or <kbd>/</kbd> | Command palette |
| <kbd>?</kbd> | Show the shortcuts |
| <kbd>g</kbd> then <kbd>t</kbd> · <kbd>k</kbd> · <kbd>s</kbd> · <kbd>n</kbd> · <kbd>f</kbd> · <kbd>h</kbd> · <kbd>u</kbd> · <kbd>e</kbd> | Today · Tickets · Timesheet · Knowledge · Search · Health · Setup · Settings |
| <kbd>j</kbd> / <kbd>k</kbd> | Next / previous item in the side list |
| <kbd>[</kbd> | Show or hide navigation labels |
| <kbd>c</kbd> | Today: copy the whole Jira message |
| <kbd>p</kbd> / <kbd>n</kbd> | Today and Timesheet: previous / next day or month |
| <kbd>1</kbd> … <kbd>8</kbd> | Ticket: switch tab |
| <kbd>←</kbd> <kbd>→</kbd> <kbd>Home</kbd> <kbd>End</kbd> | Within tabs and option groups |
| <kbd>+</kbd> <kbd>−</kbd> <kbd>0</kbd> and arrows | Within a focused diagram: zoom, reset, pan |
| <kbd>Esc</kbd> | Close a dialog |

## Live updates

The page keeps one Server-Sent Events connection (`/api/events`) open while the tab is visible. The server sends a new change cursor as soon as any file the page reads changes, and the page fetches again. It keeps your scroll position, focus, typed filters and open sections. If the stream can't connect, the page checks `/api/changes` every 10 seconds instead and keeps retrying the stream.

## Appearance and accessibility

- **Themes:** system, light or dark; the switch is in the top bar and on Settings. Density (comfortable or compact) and motion (system, reduce or full) are on Settings. All three are remembered in this browser only.
- **Fonts:** the system's own (Segoe UI Variable and Sitka on Windows), so nothing is downloaded.
- **Keyboard:** a skip link, landmarks and one heading per page; every control can be reached and used from the keyboard with a visible focus ring; dialogs keep focus inside and give it back when closed; moving to a new page moves focus to its heading.
- **Screen readers:** tabs, dialogs, the palette (combobox and listbox) and option groups follow the WAI-ARIA patterns; copy confirmations and updates are announced in a polite live region; charts and diagrams have a table or text version next to them.
- **Never colour alone:** every status has an icon and a word.
- **Checked automatically:** every page in both themes against WCAG 2.2 AA with axe, at 1440, 1280 and 900 px, plus a keyboard walk (see [development.md](development.md#ui-checks)).

## API

The page uses these endpoints. Every one is GET/HEAD and returns JSON unless noted.

| Path | Returns |
|---|---|
| `/` | The page (`app/index.html`) |
| `/app/<file>` | The page's own scripts, styles and icons. Only files that exist under `chitragupta/dashboard/app/` with an allowed extension (`.js .css .svg .png .woff2 .webmanifest .json`) are served; anything else gets 404. |
| `/manifest.webmanifest` | The web app manifest |
| `/vendor/preact.module.js`, `/vendor/hooks.module.js`, `/vendor/mermaid.min.js` | Vendored libraries (see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)) |
| `/api/health` | App, version, pid and data home |
| `/api/today` | Today's tickets and minutes, the saved worklog or a preview, plus `lanes` (activity spans per ticket), `commit_list`, `sessions`, `last_activity`, `target_minutes` and `day_kind` |
| `/api/day/<YYYY-MM-DD>` | The same for any day. A past day's preview is composed for the whole day; a saved worklog still wins. A malformed date gets 400. |
| `/api/calendar?from=&to=` | Minutes per day and per ticket, each day's kind (`workday`, `weekend`, `leave`, `half_day`, `holiday`, `extra_workday`), its note and target. Default: the 365 days ending today; at most 400 days. |
| `/api/knowledge` | Tables (with columns from the QA schema cards), relations (`verified` when the QA link map confirmed them), the code that writes each table, APIs and integrations |
| `/api/search?q=&type=&ticket=&repo=&from=&to=&limit=` | Full-text search over every indexed type (see [search.md](search.md)), grouped by type. `type` is a comma-separated list; `ticket`, `repo` and the inclusive `from` / `to` dates (`YYYY-MM-DD`) narrow it; a malformed date gets 400. Each result has `type`, `ref`, `title`, `ticket`, `repo`, `date`, `href` (the page to open), `path` (the data-home file to read), a plain-text `snippet` and `hits` (offsets). |
| `/api/setup` | The setup checklist: each check's status (`ok`, `warn`, `bad`, `off`), detail and the fix to run. Built from `doctor --fast` and cached for 60 s. |
| `/api/events` | Server-Sent Events: a `cursor` event when the stream opens and whenever a file the page reads changes, and a keep-alive comment every 25 s. At most 16 open streams; more get 503 with `Retry-After: 5`. |
| `/api/tickets?q=` | Ticket list, with each ticket's branch alerts and first linked branches |
| `/api/tickets/<KEY>` | One ticket. A malformed key gets 400; an unknown key gets 404. Includes `git` (branches, PRs, delivery, alerts, and a Mermaid graph per repo) and `readme` (the ticket's `README.md`). |
| `/api/tickets/<KEY>/export?mode=full\|shareable` | The whole ticket page as one self-contained HTML file (attachment), built in memory: nothing is written. `shareable` applies the public redaction pass and drops QA values, PR and internal URLs, hosts and file paths. |
| `/api/timesheet?period=week\|month` | Hours table and calendar notes |
| `/api/timesheet.csv?period=` | CSV in long format (`period,ticket,title,minutes,hours`), UTF-8 with BOM. Values that look like spreadsheet formulas are escaped. |
| `/api/status` | Health page data, including `mcp.live` (the last `claude mcp list` check that setup, doctor or maintenance cached; the dashboard never runs it) and `search` (engine, units, sources, last build and update with their times, and whether this process reads the file or a copy in memory) |
| `/api/changes` | A cursor that changes when any file the page reads changes |

## Security model

The dashboard shows your worklog, so it is locked to your own browser on your own machine:

- **Bound to `127.0.0.1` only**, with exclusive address use, so it is never reachable from the network.
- **Host header check.** A request is answered only when its `Host` is exactly `127.0.0.1:<port>` or `localhost:<port>`. A missing Host is rejected. That blocks DNS-rebinding attacks. If an `Origin` header is present, it must match too.
- **Read-only.** Only GET and HEAD are accepted; any other method gets 405. There is no endpoint that writes data.
- **No CORS.** Other sites can't read responses.
- **Headers on every response, errors included:** a strict Content-Security-Policy, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Cross-Origin-Resource-Policy: same-origin`.
- **Scripts only from the server itself.** The Content-Security-Policy allows no inline script, no `eval`, no workers, no plugins and no external origins. Preact escapes every value it renders; the only raw HTML is the page's own Markdown renderer, which escapes its input first, and Mermaid's SVG (`securityLevel: strict`).
- **No file serving.** Only the page, the files under `app/` with an allowed extension and the vendored libraries are served. A request path is looked up as an exact key in a list built by walking `app/`; it is never joined onto a folder, so a crafted path can't reach any other file.
- **Search only reads.** The dashboard opens the search index read-only; when it is missing or behind, it answers from a copy in memory. Only the background tick and `/cg:search --rebuild` write it, through the safety layer's path guard. User input is always quoted, so it can't use FTS syntax.
- **Robust to bad input.** A malformed request gets an error status (400, 414 or 505), never a crash. Sockets time out after 15 s.
- **Shutdown is guarded.** `/api/shutdown` needs a random token that only your user account can read (`%USERPROFILE%\.chitragupta\dashboard\<port>.token`). `dashboard stop` uses it.
- **No outgoing requests.** The server doesn't call out, and the page doesn't either.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `dashboard.port` (config) | `7419` | Port on `127.0.0.1` |
| `CHITRAGUPTA_DASHBOARD_PORT` (env) | — | Overrides the port |
| `dashboard.autostart` (config) | `true` | Start from the scheduled task and SessionStart maintenance |
| `CHITRAGUPTA_DASHBOARD` (env) | — | `0` / `off` turns autostart off |

## Ticket export

The same file comes from `/cg:ticket <KEY> --export [--mode shareable]`, which saves it under `tickets\<KEY>\exports\` (local only, never pushed; the nightly backup zip includes it). The file is the dashboard's own page with its styles and scripts inlined, the ticket's data, and Mermaid only when the ticket has a diagram (about 4 MB with it). It opens offline in any modern browser, has the same tabs, makes no requests (a Content-Security-Policy inside the file blocks them) and shows *Exported <date> · full|shareable*.

## Known gaps

- **Read only for now.** Settings, marking leave and holidays, pausing capture, sending a test message and the Fix buttons on Setup arrive in the next update, together with the write protections they need. Until then use `/cg:setup`, `/cg:log` and `/cg:pause`.
- **MCP status is the last cached check.** Health shows the last `claude mcp list` result that setup, doctor or maintenance (daily) cached, and what the last background job saw. `/cg:doctor` runs a fresh check.
- **The Today preview is not the final message.** It isn't condensed, and time already in Jira isn't subtracted, so it can differ from what is sent at 20:00.
- **The Markdown renderer is basic.** Links show as text.
