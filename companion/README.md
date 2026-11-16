# Chitragupta Companion (optional native mod)

An optional Claude Code mod that shows Chitragupta's cached, profile-scoped projections in native panes. It only
reads. Planning, approval, execution handoff and every other protected action stay in the dashboard
(`/cg:dashboard`, Companion page) and the `/cg:` commands. The core `cg` plugin works the same without it.

## What it adds

| Command | What it does |
|---|---|
| `/cg-panel [TICKET \| close]` | Opens or focuses the **Chitragupta** pane (Focus, Evidence, Attention). With a ticket, that ticket becomes this session's explicit context. `close` closes the pane. |
| `/cg-plan [TICKET]` | Opens the **Daily Planner**. A ticket is pinned to the top for this session. It never starts coding. |
| `/cg-resume [TICKET]` | Opens the resume view for a ticket, or the planner so you can choose a task. It reads only. Capture is unaffected; `/cg:pause resume` stays the global capture control. |

The **Evidence** tab starts with a **Requirement trace** for the ticket: one requirement at a time with its plan,
implementation and verification status, the commit, run and pull request behind them, the top gap and the dashboard
address of the full trace (`#/graph?ticket=…&req=…`). It comes from one `graph_summary` read of the
[Evidence Graph](../docs/evidence-graph.md). **Previous** and **Next** read nothing.

The companion opens temporary panes from its Focus tab: **Jev Review**, **PR Review** and **Database
Investigation**. Each opens only when you ask. Opening one again focuses the existing view.

Without the pane, the same information is available through `/cg:resume`, `/cg:plan`, `cli companion …` and the
dashboard's Companion page.

## Activation and minimum version

- Tested with Claude Code **2.1.302** (`claude --version`). The offline kit also passes under **2.1.306**. Earlier
  versions are not tested.
- The mod runs `server.py`, which imports the adjacent checkout's `chitragupta` package. Keep this folder inside the
  Chitragupta checkout.
- To try it for one session, run `claude --plugin-dir <checkout>/companion`.
- To load it in sessions you cannot pass a flag to, name the same folder in `CLAUDE_CODE_PLUGIN_DIRS`.
- Nothing opens at startup. Every option defaults to off, and the companion never edits your Claude settings.

## Options (`/config`, under cg-companion)

| Option | Default | Effect |
|---|---|---|
| `auto_open` | off | Opens the Chitragupta pane when a session starts, without taking focus. A narrow terminal holds it until it is wide enough. It never opens the other panes. |
| `prompt_band` | off | Shows one summary line above the prompt, from the last cached read. |
| `privacy` | off | Masks ticket and pull request titles in the panes and the band. It does not redact your transcript. |
| `density`, `accent` | compact, suggestion | Spacing and the heading accent. The accent follows your Claude Code theme. |
| `mark` | `CG` | The header mark, at most 12 plain characters. Anything else falls back to `CG`. |

**Settings** in the pane can switch privacy and density for the session. **Reset appearance** resets density only.

**Branding scope:** the mark changes only the companion's own header. Claude Code's logo and messages stay
unchanged. Replacing the Claude logo is not supported.

## Disabling only the companion

Disable `cg-companion` in `/plugin`, or stop passing its `--plugin-dir`. Capture hooks, the scheduled tick, the
dashboard and the `/cg:` commands are not affected, and no records are removed.

## Safety boundaries

- **One read-only tool.** The mod talks only to this plugin's own `companion` MCP server, and calls only its read-only
  `companion_read` tool with allowlisted operations and arguments. Requests are capped at 8 KiB and replies at 64 KiB.
  The allowlist has the ten Companion projections and `graph_summary` (a ticket, a cursor and privacy only).
- **Pure drawing.** Drawing reads the session-local cache only.
- **Bounded reads.** Reads run only while a Chitragupta pane is open, at most once every 15 s unless you act. Closing
  the last pane stops polling.
- **Never:**
  - submitting prompts or editing your prompt input;
  - running commands;
  - permission or tool-check hooks;
  - model calls, processes, network access, file writes or settings writes.
- **Copy, not submit.** **Copy approved plan** puts the prepared text on the clipboard and labels it *Prepared; not
  submitted*. You paste it into a separate ordinary turn yourself.

## Tests

From the checkout, run:

```
claude plugin validate companion
claude plugin test companion
```

The kit tests the hooks and the trees they return on each surface. It cannot prove that a pane is actually painted
on screen.
