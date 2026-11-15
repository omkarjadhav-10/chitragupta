// MOD-02A: the registry of Chitragupta workflow modules. The mod opens, focuses, closes and draws only these
// namespaced pane IDs; any other pane or event belongs to another mod and passes through untouched.
// Each module declares its pane, title, tabs, applicability, the bounded projection reads it needs while
// open, and the fallback destination used where the native pane is unavailable.

const OK = Object.freeze({ ok: true });
const withPrivacy = (req, prefs) => (prefs.privacy ? { ...req, privacy: true } : req);

export const MODULES = Object.freeze({
  companion: {
    kind: 'companion', id: 'cg-companion', title: 'Chitragupta', persistent: true,
    tabs: ['focus', 'evidence', 'attention'],
    applicable: () => OK,
    reads: (ctx, local, prefs) => (ctx.workflow ? [withPrivacy({ operation: 'detail', workflow: ctx.workflow }, prefs)] : []),
    fallback: 'Text and dashboard: /cg:resume or /cg:plan, or /cg:dashboard then Companion',
  },
  planner: {
    kind: 'planner', id: 'cg-planner', title: 'Daily Planner', tabs: [],
    applicable: () => OK,
    reads: (ctx, local, prefs) => [withPrivacy({ operation: 'planner', group: local.group || undefined,
      cursor: local.cursor || undefined, pinned: local.pinned && local.pinned.length ? local.pinned : undefined }, prefs)],
    fallback: 'Text: /cg:plan or cli companion planner; dashboard: /cg:dashboard then Companion',
  },
  review: {
    kind: 'review', id: 'cg-review', title: 'Jev Review', tabs: [],
    applicable: ctx => (ctx.workflow ? OK : { ok: false, reason: 'Choose a task with a saved plan first' }),
    reads: (ctx, local, prefs) => [withPrivacy({ operation: 'review', workflow: ctx.workflow }, prefs),
      { operation: 'jobs', workflow: ctx.workflow }],
    fallback: 'Text: cli companion review <task>; dashboard: /cg:dashboard then Companion, Jev Review tab',
  },
  database: {
    kind: 'database', id: 'cg-database', title: 'Database Investigation', tabs: ['focus', 'evidence', 'attention'],
    applicable: () => OK,
    reads: (ctx, local, prefs) => (ctx.flow || ctx.investigation
      ? [withPrivacy({ operation: 'database', flow: ctx.flow, investigation: ctx.investigation }, prefs)] : []),
    fallback: 'Dashboard: /cg:dashboard then Database (#/db); text: /cg:trace <TICKET> shows the recorded flow',
  },
  pr: {
    kind: 'pr', id: 'cg-pr', title: 'PR Review', tabs: [],
    applicable: ctx => (ctx.workflow ? OK : { ok: false, reason: 'Choose a task first' }),
    reads: (ctx, local, prefs) => [withPrivacy({ operation: 'detail', workflow: ctx.workflow }, prefs)],
    fallback: 'Text: /cg:ticket <TICKET>; dashboard: /cg:dashboard then the ticket\'s Delivery tab',
  },
});

/** Own pane ID -> module. Nothing outside this map is ever opened, closed, focused or drawn. */
export const PANES = new Map(Object.values(MODULES).map(m => [m.id, m]));

export const own = id => PANES.has(id);

/** The session-local UI state of one view (never workflow truth, never persisted). */
export function freshLocal(mod) {
  return { tab: mod.tabs[0] || 'main', expanded: {}, draft: '', cursor: null, group: null, pinned: [],
    menu: false, notice: '', packet: '', page: 0 };
}

/** The narrow context reference a temporary module is bound to; the key of its UI state. */
export function contextKey(kind, ctx) {
  if (kind === 'review' || kind === 'pr') return ctx.workflow || '';
  if (kind === 'database') return ctx.flow || ctx.investigation || '';
  return '';
}
