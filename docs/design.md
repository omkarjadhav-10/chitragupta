# Dashboard design

The dashboard's look lives in one stylesheet, `chitragupta/dashboard/app/app.css` (tokens first, then components), and
a handful of Preact components in `app/lib/ui.js`. A new page reuses these; it adds a pattern only when none fits, and
then documents it here. Everything here also renders inside a ticket export (the same CSS and modules, inlined).

## Tokens (`:root` in app.css)

| Group | Tokens | Notes |
|---|---|---|
| Surfaces | `--canvas` (page), `--surface` (panes, cards), `--sunk` (wells, chips, code) | |
| Ink | `--ink`, `--ink-2` (secondary), `--ink-3` (muted, still AA on every surface) | |
| Rules | `--rule`, `--rule-strong` | 1 px lines and hover borders |
| Accent | `--accent`, `--accent-ink` (links, the selected item), `--accent-soft` (highlights), `--on-accent` | neutral paper and ink blue; Night ink in dark mode |
| Status | `--ok`, `--warn`, `--bad`, `--info`, each with a `-soft` background | never the only signal (see below) |
| Ticket hues | `--t1` … `--t6`, `--to` (other) | timelines and lanes |
| Heat | `--heat-0` … `--heat-4` | the timesheet calendar |
| Focus | `--focus` | the 2 px focus ring |
| Type | `--serif` (headings), `--sans` (text), `--mono` (code, keys, paths); scale `--fs-xs` 12 · `--fs-s` 14 · `--fs-m` 16 · `--fs-l` 21 · `--fs-xl` 28 · `--fs-2xl` 40 · `--fs-3xl` 56 | numbers are tabular |
| Space | `--s1` 4 · `--s2` 8 · `--s3` 12 · `--s4` 16 · `--s5` 20 · `--s6` 28 · `--s7` 40 | |
| Radius | `--r-row` < `--r-control` < `--r-card` < `--r-pane` < `--r-dialog` | by hierarchy |
| Motion | `--dur-1`, `--dur-2`, `--ease` | 0 under reduced motion |

**Dark mode** is the default when no browser preference is saved (`:root:not([data-theme])`). Explicit light,
dark and system preferences remain respected; system uses `prefers-color-scheme`. The local Source Serif 4 and
IBM Plex Sans files supply headings and text, including standalone exports with their license notices. Pages use
tokens for both themes. `data-density="compact"` tightens spacing and `data-motion="reduce"` stops transitions.

## Components

| Class / component | Use |
|---|---|
| `.pane`, `.rail`, `.primary-nav`, `.more-nav`, `.desk` | the shell: top navigation, the side list, the page |
| `PageHead`, `.pagehead`, `.pagehead-act` | a page's `h1` (focused on navigation) and its actions |
| `.tkhead`, `.tkbar` | the ticket header and the slim bar with its tabs that stays in view |
| `Tabs` / `TabPanel` | WAI-ARIA tabs: arrow keys, Home and End move; automatic activation |
| `Status` (`.status.ok\|warn\|bad\|info\|wait\|off`) | an icon plus words in a soft pill; `.quiet` drops the pill |
| `Chip` (`.chip`) | a small neutral fact: repo, issue type, time |
| `.card`, `.grid` | a bordered box; an auto-fill grid of them |
| `.kv` | two-column key/value list |
| `.lede` | a lead paragraph (at most 80ch) |
| `.alert` (`.bad`) | a banner with an icon and text; `role="alert"` only when it needs attention now |
| `.findings` (`.t`, `.d`, `.m`) | a list of items: title, detail, meta line |
| `.kindgroup`, `.finding-row` | a heading and count over findings; each finding has its evidence in an aside that stacks on narrow screens |
| `.step` | a card for one request or query |
| `details.more` | anything kept one click away |
| `.two` | a 1.25fr / 1fr two-column layout that stacks below 1040 px |
| `DataTable`, `Scroll` | sortable tables; wide tables scroll inside a labelled region |
| `Empty`, `Skeleton`, `ErrorBox`, `Load` | empty, loading and error states of a request |
| `CopyButton`, `CodeBlock`, `Markdown`, `Diagram`, `Dialog`, `Toasts` | copying, code, the README and DB-flow Markdown (escaped, no raw HTML), Mermaid, modal dialogs, the one polite live region |

## The ticket brief (FX-20)

The ticket page opens on the brief (`app/pages/brief.js`). It reuses `.alert` (the *New findings since …* bar with
**Refresh brief**), `Status` (confidence, Jira status, inferred status, delivery stage, verified), `.card`,
`details.more` and `.findings`, and adds three patterns:

- **Cite chip** (`a.cite`, `.cite.static`). A small pill naming one piece of evidence: a file and line, "session, 14 Oct",
  a query, a table, a commit, a PR or a replay step. A chip that leads somewhere is a link: it switches to the tab
  that holds the evidence and scrolls to it, opening the collapsed *All findings* list when the evidence is an item,
  and briefly highlights it (`.hit`). Raw log lines and Jira keys are not links; they get a dashed border. Every chip
  carries a visually hidden "Evidence:" prefix and its id in the title. At least 24 px high.
- **Claim / evidence grid** (`.claims` > `.claim` with `.txt` and `.cites`). One row per claim: the text, then its
  chips in a right-hand margin column (120–220 px). `.claims.plain` drops the bullet. When the brief itself is
  narrower than 640 px (a container query on `.brief`, so it follows the pane, not the window), the chips move under
  the claim. Root cause uses `.rc`: the first claim set larger in the serif.
- **Before / after grid** (`.ba`). Two `.card`s side by side, "How it works today" and "How it works after the fix",
  each with an `h3`, an ordered list of steps, its chips below and, after the fix, a *Verified* / *Not verified*
  status. Stacked under 640 px.

Smaller parts: `.bmeta` (the header line under the title: issue type, *Jira:* and *Inferred:* statuses, delivery
stage, repo · branch · PR, *Updated*), `.bsec` (a brief section; its `h2` carries the confidence or the source label
`.src`, e.g. "from git and PRs"), `.lab` (claim labels: *Recorded:*, *Suggested:*, *Ruled out:*, *Rejected:*),
`.bempty` ("Not recorded yet"), `.fixes` and `.fsum` (the computed fixes list and its summary), `.alld` (the one quiet
*All details* line) and `.allfind` (the collapsed *All findings (n)* list below it).

Print: the navigation, side list, tab bar, buttons and the *All details* line are hidden, sections don't split across
pages, and *All findings* starts on a new page, so the brief is page one.

## Accessibility rules

- **Headings.** Every page has one `h1`; every section has a heading (`h2`, or `h3` inside a card). A grid whose
  cards have their own headings gets a visually hidden `h2` (`.sr`). Sections are `<section aria-labelledby>`.
- **Words, not colour.** A status is an icon plus words (`Status`); a label is a word (*Recorded:*, *Suggested:*,
  *Superseded*, *Needs review:*). Colour only repeats what the words say. Text meets WCAG AA contrast in both themes.
- **Keyboard.** Everything that acts is a `button` or a link; no click handlers on plain elements. Tabs follow the
  WAI-ARIA pattern; a chip or *All details* link moves focus to what it shows (`tabindex="-1"` targets). The focus
  ring is `--focus`, 2 px, never removed for keyboard users. Shortcuts are listed under <kbd>?</kbd>.
- **Targets** are at least 24 × 24 px. **Live updates** go through the one polite toast region; banners that need
  attention use `role="alert"`, informational bars `role="status"`.
- **Checks.** `scripts/ui/run.py` runs axe (WCAG 2.0–2.2 A/AA) on every page in light and dark at 1440, 1280 and
  900 px and fails on any serious or critical finding, horizontal scroll at 900 px or a failed keyboard check; see
  [development.md](development.md).

At phone widths (600 px and below), ticket header actions sit below the title and brief metadata so the heading retains the full reading width. The browser checks cover this at 390 px.

When a generated brief exists, it supplies the ticket summary; the legacy model TL;DR is shown in the header only as a fallback before a brief exists. This prevents stale prose competing with current cited claims.

Brief text wraps long method names and owner corrections inside its reading width, including at 390 px.

Ticket delivery alerts render once in the shared header (or once inside the Branches tab), including when the Overview is open. The browser regression supplies a ticket with delivery alerts.

Printing forces the light palette and light color scheme for both dashboard pages and standalone exports, including when Night ink or System dark is selected.

Evidence margins use plain elements rather than a complementary landmark per finding. The More trigger only references a rendered menu; j/k opens the ticket switcher and moves among visible links.
