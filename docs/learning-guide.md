# Learning guide

This guide maps the main ideas in Chitragupta to the code and documentation that explain them. Start with [Development](development.md) for the contributor contract and local workflow.

## Follow a captured event

Hooks record redacted session events quickly and without model or network calls. The capture boundary and its safety checks are described in [Safety](safety.md) and [Privacy and data](privacy.md). For development, use an isolated data home as described in [Development](development.md); tests use synthetic records and tripwires to prevent real service calls.

## Follow knowledge into a ticket

The agent ingests raw records, extracts structured findings, merges stable item identifiers, and renders ticket pages and hubs. See the extraction and rebuild code in `chitragupta/`, the [Architecture](architecture.md) overview, and [Search](search.md) for the local index. Ticket briefs summarize current evidence; their claims are validated against recorded item and delivery evidence. The release notes describe the brief fields and supported evidence identifiers.

## Understand time and delivery

Time is estimated from turn intervals and deduplicated across sessions; it is an activity estimate rather than a timesheet clock. Branch and pull-request delivery checks are based on repository evidence. Read [Branch tracking](branch-tracking.md) for the tracked branches and delivery rules, and [Scheduling](scheduling.md) for background processing.

## Explore the interface

The dashboard is plain JavaScript modules with no frontend build step. Its components are under `chitragupta/dashboard/app/`; the design tokens and brief components are documented in [Dashboard design](design.md). The [Dashboard](dashboard.md) guide describes the pages and interactions. Browser checks run against synthetic data in both themes and at several viewport widths; see the UI checks section in [Development](development.md).

## Make a change safely

1. Read [CLAUDE.md](../CLAUDE.md), then [Development](development.md).
2. Work against an isolated home and synthetic test data.
3. Keep runtime imports in the Python standard library and route side effects through `chitragupta/safety/`.
4. Add focused `unittest` coverage. For dashboard changes, run the browser checks.
5. Run the secret and deny-list scan, version check when relevant, and plugin validation before sharing changes.

For user setup, see [Install](install.md); for configuration, see [Configuration](configuration.md).
