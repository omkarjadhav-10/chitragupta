// Session-local UI state and the one shared refresh coordinator (NFR-01, A06, A27, A53).
// Nothing here is workflow truth: durable state stays in the Python backend and is only read through
// the bridge. Reads happen only while an owned pane is open (and placed), at most once per 15 s unless the
// person acts or the profile generation changes; a reply from a superseded request or another profile
// generation is dropped, never drawn.
import { BridgeError, read } from './bridge.js';
import { MODULES, PANES, contextKey, freshLocal } from './panes.js';
import { ACCENTS, block, clean, dbArg, mark, ticketArg } from './text.js';

export const MIN_INTERVAL = 15000;
const COPY_PAGES = 4;

function blank() {
  return {
    generation: null, snapshot: null, problem: null, notice: '',
    reads: new Map(), errors: new Map(),
    views: new Map(), local: new Map(),
    select: null, db: null, dbDraft: '', dbNotice: '',
    token: 0, inflight: 0, lastReadAt: -Infinity, timer: null, retry: false,
    dropped: 0, revoked: 0, override: {},
  };
}

export const S = { internal: false, interactive: true, options: {}, ...blank() };

export function configure(options) {
  S.options = options && typeof options === 'object' ? options : {};
}

/** MOD-06 preferences: the manifest's userConfig (defaults off) plus this session's appearance overrides. */
export function prefs() {
  const o = S.options || {};
  const m = mark(o.mark);
  return {
    privacy: S.override.privacy ?? o.privacy === true,
    density: S.override.density ?? (o.density === 'comfortable' ? 'comfortable' : 'compact'),
    accent: ACCENTS.includes(o.accent) ? o.accent : 'suggestion',
    mark: m.text, markValid: m.valid,
    band: o.prompt_band === true,
    autoOpen: o.auto_open === true,
  };
}

function redraw($) {
  $.ui.invalidate('ui.render');
}

const anyOpen = () => [...S.views.values()].some(v => v.open);
const anyPlaced = () => [...S.views.values()].some(v => v.open && v.placed);

// ---------------------------------------------------------------- views and their local state

/** The context a view reads for: the companion follows the snapshot's selection, temporary panes their binding. */
export function context(view) {
  if (view.kind === 'companion') {
    const c = (S.snapshot && S.snapshot.context) || {};
    return { workflow: c.workflow || null, ticket: c.ticket || null };
  }
  if (view.kind === 'database') return S.db || {};
  return view.context || {};
}

const localKey = view => view.id + '|' + contextKey(view.kind, context(view));

export function localOf(view) {
  const key = localKey(view);
  let l = S.local.get(key);
  if (!l) {
    l = freshLocal(MODULES[view.kind]);
    S.local.set(key, l);
  }
  return l;
}

/** Render's read-only access: never creates or changes state. */
export function peekLocal(view) {
  return S.local.get(localKey(view)) || freshLocal(MODULES[view.kind]);
}

export function readKey(req) {
  return [req.operation, req.workflow, req.group, req.cursor, req.flow, req.investigation,
    (req.pinned || []).join(',')].map(x => x || '').join('|');
}

export const cachedRead = req => S.reads.get(readKey(req)) || null;
export const readError = req => S.errors.get(readKey(req)) || null;

/** The module's reads for a view, as the coordinator issues them (pure; render uses it for cache keys). */
export function viewReads(view) {
  return MODULES[view.kind].reads(context(view), peekLocal(view), prefs());
}

function paneReads() {
  const out = [];
  const seen = new Set();
  for (const view of S.views.values()) {
    if (!view.open) continue;
    for (const req of viewReads(view)) {
      const key = readKey(req);
      if (!seen.has(key)) {
        seen.add(key);
        out.push(req);
      }
    }
  }
  return out;
}

function snapshotArgs() {
  const a = { operation: 'snapshot' };
  if (S.select && S.select.workflow) a.workflow = S.select.workflow;
  else if (S.select && S.select.ticket) a.ticket = S.select.ticket;
  if (S.db && S.db.flow) a.flow = S.db.flow;
  if (S.db && S.db.investigation) a.investigation = S.db.investigation;
  if (prefs().privacy) a.privacy = true;
  return a;
}

// ---------------------------------------------------------------- revocation and failures

/** Generation change or revoked access: remove old visible state first, then reconcile (ARCH-04, UX-03). */
function revoke(notice) {
  S.reads.clear();
  S.errors.clear();
  S.select = null;
  S.db = null;
  S.dbDraft = '';
  for (const l of S.local.values()) Object.assign(l, { draft: '', cursor: null, page: 0, expanded: {}, packet: '', notice: '', pinned: [] });
  for (const v of S.views.values()) if (v.kind === 'review' || v.kind === 'pr') v.context = {};
  S.notice = notice;
  S.revoked += 1;
}

const kindOf = err => (err instanceof BridgeError ? err.kind : 'unavailable');
const textOf = err => clean(err && err.message, 200) || 'Companion read unavailable';

function failSnapshot(err) {
  const kind = kindOf(err);
  if (kind === 'profile' || kind === 'reload') {
    revoke('Profile or scope changed; earlier details were removed.');
    S.generation = null;
    S.retry = true;
  } else if (kind === 'setup' || kind === 'denied') {
    revoke('');
    S.snapshot = null;
    S.generation = null;
    S.problem = { kind, text: textOf(err) };
  } else {
    S.problem = { kind, text: textOf(err) }; // keep the last safe cached state (ARCH-02)
  }
}

function failRead(err, key, req) {
  const kind = kindOf(err);
  if (kind === 'profile') {
    revoke('Profile or scope changed; earlier details were removed.');
    S.generation = null;
    S.retry = true;
  } else if (kind === 'reload') { // a cursor from an older listing: start again without it
    for (const l of S.local.values()) {
      if (req.cursor && l.cursor === req.cursor) Object.assign(l, { cursor: null, page: 0, notice: 'The list changed; showing the first page again.' });
    }
    S.reads.delete(key);
    S.retry = true;
  } else if (kind === 'denied' || kind === 'setup') {
    S.reads.delete(key); // content removed immediately
    S.errors.set(key, { kind, text: textOf(err) });
  } else {
    S.errors.set(key, { kind, text: textOf(err) }); // the cached read stays, drawn as stale
  }
}

// ---------------------------------------------------------------- the coordinator

async function cycle($, token) {
  let snap;
  try {
    snap = await read($, snapshotArgs());
  } catch (err) {
    if (token === S.token) failSnapshot(err);
    else S.dropped += 1;
    return;
  }
  if (token !== S.token) { // A06: a newer request superseded this one
    S.dropped += 1;
    return;
  }
  if (S.generation !== null && snap.profile_generation !== S.generation) {
    // Revoke first, then reconcile: this reply was asked for with the old selection, so read again without it.
    revoke('Profile or scope changed; earlier details were removed.');
    S.generation = snap.profile_generation;
    S.snapshot = null;
    S.retry = true;
    return;
  }
  S.generation = snap.profile_generation;
  S.snapshot = snap;
  S.problem = null;
  for (const req of paneReads()) {
    const key = readKey(req);
    try {
      const data = await read($, req);
      if (token !== S.token) {
        S.dropped += 1;
        return;
      }
      if (data.profile_generation !== S.generation) { // never restore another generation's content
        S.dropped += 1;
        S.retry = true;
        return;
      }
      S.reads.set(key, data);
      S.errors.delete(key);
    } catch (err) {
      if (token !== S.token) return;
      failRead(err, key, req);
      if (S.retry) return;
    }
  }
}

/** One bounded read of the open views. `periodic` waits out the 15 s minimum; explicit reasons do not. */
export async function refresh($, reason, depth = 0) {
  if (!anyOpen()) return;
  const now = await $.clock.now();
  if (reason === 'periodic' && (S.inflight > 0 || now - S.lastReadAt < MIN_INTERVAL)) return;
  const token = ++S.token;
  S.lastReadAt = now;
  S.inflight += 1;
  S.retry = false;
  redraw($);
  try {
    await cycle($, token);
  } finally {
    S.inflight -= 1;
    redraw($);
  }
  if (S.retry && token === S.token && depth < 1) {
    S.retry = false;
    await refresh($, 'generation', depth + 1);
  }
}

function ensureTimer($) {
  if (!S.timer && anyOpen()) S.timer = $.clock.every(MIN_INTERVAL, () => {
    tick($).catch(() => {});
  });
}

function stopIfIdle() {
  if (S.timer && !anyOpen()) {
    S.timer.cancel();
    S.timer = null;
  }
}

/** The engine's record of this plugin's own panes wins over ours (a person's close, an unload, a reload). */
function reconcile(panes) {
  const open = new Map(panes.filter(p => p && PANES.has(p.id)).map(p => [p.id, p]));
  for (const view of S.views.values()) {
    const p = open.get(view.id);
    view.open = Boolean(p);
    view.placed = Boolean(p && p.isPlaced);
  }
  for (const [id, p] of open) {
    if (!S.views.has(id)) {
      const mod = PANES.get(id);
      S.views.set(id, { id, kind: mod.kind, open: true, placed: Boolean(p.isPlaced), context: {} });
    }
  }
}

async function tick($) {
  try {
    const panes = await $.ui.panes();
    if (Array.isArray(panes)) reconcile(panes);
  } catch (_) {
    // keep our own record
  }
  if (!anyOpen()) return stopIfIdle();
  if (anyPlaced()) await refresh($, 'periodic');
}

// ---------------------------------------------------------------- pane lifecycle (MOD-02A)

/** Open or focus one module's pane. Reopening a fresh, unchanged view focuses it with no read (A53). */
export async function open($, kind, ctx = {}, how = {}) {
  const mod = MODULES[kind];
  if (!mod) return { opened: false, reason: 'Unknown view' };
  const target = kind === 'review' || kind === 'pr' ? { workflow: ctx.workflow || null, ticket: ctx.ticket || null } : {};
  const verdict = mod.applicable(kind === 'database' ? S.db || {} : target);
  if (!verdict.ok) return { opened: false, reason: verdict.reason };
  let view = S.views.get(mod.id);
  const wasOpen = Boolean(view && view.open);
  const before = view ? contextKey(kind, context(view)) : null;
  if (!view) {
    view = { id: mod.id, kind, open: false, placed: false, context: {} };
    S.views.set(mod.id, view);
  }
  if (kind === 'review' || kind === 'pr') view.context = target;
  const retargeted = before !== null && before !== contextKey(kind, context(view));
  const local = localOf(view);
  if (how.pinned) local.pinned = how.pinned;
  if (how.tab) local.tab = how.tab;
  if (how.notice !== undefined) local.notice = how.notice;
  const pane = { id: mod.id, title: mod.title };
  if (how.asked !== false) pane.focus = true;
  if (!mod.persistent && how.asked !== false) pane.closeOnEscape = true;
  let res;
  try {
    res = await $.ui.open(pane);
  } catch (err) {
    return { opened: false, reason: clean(err && err.message, 120) || 'The host refused to open the pane' };
  }
  view.open = true;
  view.placed = Boolean(res && res.isPlaced === true);
  ensureTimer($);
  const now = await $.clock.now();
  const fresh = wasOpen && !retargeted && !how.reread && S.snapshot && now - S.lastReadAt < MIN_INTERVAL;
  if (view.placed && !fresh) await refresh($, 'open');
  else redraw($);
  return { opened: true, placed: view.placed, reason: res && res.reason ? clean(res.reason, 160) : '' };
}

/** Our own bookkeeping for a close: the plugin's own `$.ui.close` does not raise its own `ui.close` hook. */
export function closed(id) {
  const view = S.views.get(id);
  if (view) {
    view.open = false;
    view.placed = false;
  }
  stopIfIdle();
}

export async function close($, id) {
  if (!PANES.has(id)) return;
  closed(id);
  try {
    await $.ui.close({ id });
    const panes = await $.ui.panes();
    if (Array.isArray(panes) && panes.some(p => p && p.id === id)) reconcile(panes); // a hook kept it open
    ensureTimer($);
  } catch (_) {
    // a refused close leaves our record closed; reopening reads again
  }
  redraw($);
}

/** session.start: panes that survived a hot reload come back; auto-open applies to the companion only. */
export async function startup($, e) {
  S.interactive = Boolean(e && e.isInteractive === true && e.surface);
  let panes = [];
  try {
    panes = await $.ui.panes();
  } catch (_) {
    panes = [];
  }
  if (Array.isArray(panes) && panes.some(p => p && PANES.has(p.id))) {
    reconcile(panes);
    ensureTimer($);
    $.clock.after(0, () => {
      refresh($, 'open').catch(() => {});
    });
  }
  if (!prefs().autoOpen || !S.interactive || (S.views.get(MODULES.companion.id) || {}).open) return;
  let res;
  try {
    res = await $.ui.open({ id: MODULES.companion.id, title: MODULES.companion.title }); // unasked: no focus
  } catch (_) {
    return;
  }
  const view = { id: MODULES.companion.id, kind: 'companion', open: true, placed: Boolean(res && res.isPlaced), context: {} };
  S.views.set(view.id, view);
  localOf(view);
  ensureTimer($);
  if (view.placed) {
    $.clock.after(0, () => {
      refresh($, 'open').catch(() => {});
    });
  }
}

/** /clear or /resume: a new conversation; the session-local selection resets, panes stay. */
export function newConversation($) {
  S.select = null;
  S.db = null;
  S.dbDraft = '';
  S.notice = 'New conversation: the task selection was reset.';
  if (anyOpen()) {
    $.clock.after(0, () => {
      refresh($, 'generation').catch(() => {});
    });
  }
}

export function reset() {
  if (S.timer) S.timer.cancel();
  Object.assign(S, blank());
}

// ---------------------------------------------------------------- explicit actions (handlers only)

export function dismiss($) {
  S.notice = '';
  redraw($);
}

export function setLocal($, view, patch) {
  Object.assign(localOf(view), patch);
  redraw($);
}

export function setDraft(view, value) {
  localOf(view).draft = String(value || '').slice(0, 80);
}

export async function selectTicket($, ticket) {
  S.select = { ticket };
  S.notice = '';
  await refresh($, 'action');
}

export async function selectWorkflow($, workflow, ticket) {
  S.select = { workflow, ticket: ticket || null };
  S.notice = '';
  return open($, 'companion', {}, { tab: 'focus', reread: true });
}

export async function submitTicket($, view, value) {
  const ticket = ticketArg(value);
  const local = localOf(view);
  if (!ticket) {
    local.notice = 'Type a ticket key such as ABC-1001.';
    redraw($);
    return;
  }
  local.draft = '';
  local.notice = '';
  await selectTicket($, ticket);
}

export async function chooseDb($, value) {
  const arg = dbArg(value);
  if (!arg) {
    S.dbDraft = String(value || '').slice(0, 60);
    S.dbNotice = 'Type a flow ID (f_…) or an investigation ID (i_…).';
    redraw($);
    return;
  }
  S.db = arg;
  S.dbDraft = '';
  S.dbNotice = '';
  await refresh($, 'action');
}

export async function leaveDb($) {
  S.db = null; // the ticket selection is untouched (FLOW-11)
  await refresh($, 'action');
}

export async function setPlanner($, view, patch) {
  Object.assign(localOf(view), patch);
  await refresh($, 'action');
}

/** Session appearance overrides; `null` is Reset appearance (density only: identity, records and privacy stay). */
export function appearance($, patch) {
  if (patch === null) delete S.override.density;
  else Object.assign(S.override, patch);
  redraw($);
}

export async function copyText($, view, text, surface, done) {
  let res;
  try {
    res = await $.ui.copy({ text, surface });
  } catch (_) {
    res = { isCopied: false, reason: 'refused' };
  }
  localOf(view).notice = res && res.isCopied ? done
    : 'Copy is unavailable here (' + clean(res && res.reason, 40) + '). Select the text above, or type ' + text + ' yourself.';
  redraw($);
}

/** Copy approved plan: bounded reads of the prepared packet, then the host clipboard. Never submits. */
export async function copyHandoff($, view, workflow, surface) {
  const local = localOf(view);
  let text = '';
  let cursor;
  let pages = 0;
  try {
    do {
      const d = await read($, { operation: 'handoff', workflow, cursor });
      if (d.profile_generation !== S.generation) throw new BridgeError('profile', 'profile changed; reload');
      if (d.state !== 'prepared_not_submitted' || typeof d.text !== 'string') {
        local.notice = 'No approved plan is prepared to copy: ' + clean(d.reason || d.label, 160)
          + '. Approve an exact version in /cg:dashboard, Companion, Jev Review.';
        local.packet = '';
        redraw($);
        return;
      }
      text += d.text;
      cursor = d.cursor || undefined;
      pages += 1;
    } while (cursor && pages < COPY_PAGES);
  } catch (err) {
    local.notice = 'Could not read the prepared plan: ' + textOf(err);
    redraw($);
    return;
  }
  if (cursor) {
    local.notice = 'The prepared plan is longer than this pane copies. Use /cg:dashboard, Companion, Handoff, Copy approved plan.';
    redraw($);
    return;
  }
  let res;
  try {
    res = await $.ui.copy({ text, surface });
  } catch (_) {
    res = { isCopied: false, reason: 'refused' };
  }
  if (res && res.isCopied) {
    local.packet = '';
    local.notice = 'Copied. Prepared; not submitted. Paste it into a separate ordinary Claude turn yourself; '
      + 'normal permissions apply. Record the outcome in /cg:dashboard, Companion, Handoff.';
  } else {
    local.packet = block(text, 2000);
    local.notice = 'Copy is unavailable here (' + clean(res && res.reason, 40) + '). Prepared; not submitted. '
      + 'Select the text below, or use /cg:dashboard, Companion, Handoff, Copy approved plan.';
  }
  redraw($);
}
