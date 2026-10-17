# Third-party notices

Chitragupta's own code is MIT-licensed (see LICENSE). This file lists anything included from elsewhere, with its license.

| Component | Where | License | Notes |
|---|---|---|---|
| Mermaid 11.17.2 (`dist/mermaid.min.js`, unmodified) | `third_party/mermaid/` | MIT, Copyright (c) 2014 - 2022 Knut Sveidqvist — see `third_party/mermaid/LICENSE` | Draws the DB-flow diagrams in the dashboard; served locally at `/vendor/mermaid.min.js`, never from a CDN. Source: npm package `mermaid@11.17.2` (jsDelivr), sha256 (base64) `WB7X10vZBI0OOpE2OSfXLvIpQtdyJUayf3zCnjU5Drg=`. |
| Preact 10.29.8 (`dist/preact.module.js`, `hooks/dist/hooks.module.js`) | `third_party/preact/` | MIT, Copyright (c) 2015-present Jason Miller — see `third_party/preact/LICENSE` | Renders the dashboard; served at `/vendor/preact.module.js` and `/vendor/hooks.module.js`. Source: npm package `preact@10.29.8`. Two changes: the hooks file imports `/vendor/preact.module.js` instead of the bare `preact`, and both files drop their `sourceMappingURL` comment. sha256 of the originals: `c30e721ebfdc6e2ad4c18c14d2dfb82667829c8aec27de1207774e3fc16858a8` (preact.module.js), `a6ee626f2d01570592dd569a792e3f050154aa02890eead8c223fa3ed5aa3d5a` (hooks.module.js). |
| axe-core 4.13.0 (`axe.min.js`, unmodified) — **development only** | `scripts/ui/vendor/` | MPL-2.0 — see `scripts/ui/vendor/axe-core.LICENSE` | The accessibility checks in `scripts/ui/check.js`. Never served by the dashboard and not part of the runtime. Source: npm package `axe-core@4.13.0`, sha256 `c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1`. |

Development tools installed with npm and not committed: `playwright-core` (Apache-2.0), pinned in `scripts/ui/package.json`.

Design and packaging patterns were studied from other open-source Claude Code plugins; no files or substantial snippets were copied. If that ever changes, the copied file keeps its original notice and is listed here.
