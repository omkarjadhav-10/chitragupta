// Chitragupta native companion (optional mod). Hooks are wiring only: commands open owned panes, the
// coordinator reads cached projections through the bridge, and `ui.render` draws from memory.
// Never here: $.prompt.submit or any prompt edit, $.command.run, permission/authorize/tool.check hooks,
// model calls, processes, network, files or settings. Every other mod's panes and events pass through.
import { MODULES, PANES } from './panes.js';
import { band, pane } from './render.js';
import {
  S, appearance, chooseDb, close, closed, configure, copyHandoff, copyText, dismiss, leaveDb, newConversation, open,
  peekLocal, prefs, refresh, reset, selectWorkflow, setDraft, setLocal, setPlanner, startup, submitTicket,
} from './session.js';
import { clean, ticketArg } from './text.js';

// Plain names: `$.command.register` takes letters, digits, `_` and `-`, so the core plugin's `/cg:` skills
// (including the global capture control `/cg:pause resume`) can never collide with these.
const ABOUT = {
  'cg-panel': 'Open or focus the Chitragupta companion (Focus, Evidence, Attention); reads only',
  'cg-plan': 'Open the Chitragupta Daily Planner; reads only and never starts coding',
  'cg-resume': 'Choose a saved Chitragupta task to resume; reads only, capture is unaffected',
};

const HEADLESS = {
  'cg-panel': 'The Chitragupta companion pane needs a terminal or Desktop surface. Use /cg:resume, /cg:plan or /cg:dashboard.',
  'cg-plan': 'The Daily Planner pane needs a terminal or Desktop surface. Use /cg:plan, cli companion planner or /cg:dashboard.',
  'cg-resume': 'The resume pane needs a terminal or Desktop surface. Use /cg:resume <TICKET> or /cg:dashboard.',
};

const truthy = v => typeof v === 'string' && /^(1|true|yes|on)$/i.test(v.trim());

/** Chitragupta's own internal sessions (and the legacy name) get no commands, panes, polling or band. */
async function internal($) {
  try {
    if (truthy(await $.env.get('CHITRAGUPTA_INTERNAL'))) return true;
    return truthy(await $.env.get('WORKLOG_INTERNAL'));
  } catch (_) {
    return false; // unreadable environment: an ordinary session
  }
}

/** Button and input handlers. Each is created while drawing and runs only on the person's own act. */
function actions($) {
  const notice = text => {
    S.notice = text;
    $.ui.invalidate('ui.render');
  };
  return {
    tab: (view, tab) => () => setLocal($, view, { tab, menu: false }),
    menu: view => () => setLocal($, view, { menu: !peekLocal(view).menu }),
    toggle: (view, name) => () => setLocal($, view, { expanded: { ...peekLocal(view).expanded, [name]: !peekLocal(view).expanded[name] } }),
    trace: (view, index) => () => setLocal($, view, { trace: index }), // Previous / Next: session state only, no read
    route: (view, text) => () => setLocal($, view, { notice: text, packet: '' }),
    clearNotice: view => () => setLocal($, view, { notice: '', packet: '' }),
    dismiss: () => () => dismiss($),
    open: (kind, ctx) => async () => {
      const r = await open($, kind, ctx || {});
      if (!r.opened) notice(clean(r.reason, 120) + '. ' + MODULES[kind].fallback + '.');
    },
    close: view => () => close($, view.id),
    refresh: () => () => {
      S.notice = '';
      return refresh($, 'action');
    },
    copyCommand: view => press => copyText($, view, '/cg:dashboard', press && press.surface,
      'Copied /cg:dashboard. Paste it at the prompt yourself when you are ready.'),
    copyHandoff: (view, workflow) => press => copyHandoff($, view, workflow, press && press.surface),
    selectWorkflow: (workflow, ticket) => async () => {
      const r = await selectWorkflow($, workflow, ticket);
      if (r && !r.opened) notice(clean(r.reason, 120));
    },
    draft: view => value => setDraft(view, value),
    submitTicket: view => value => submitTicket($, view, value),
    dbDraft: () => value => {
      S.dbDraft = String(value || '').slice(0, 60);
    },
    chooseDb: () => value => chooseDb($, value),
    leaveDb: () => () => leaveDb($),
    planner: (view, patch) => () => setPlanner($, view, patch),
    appearance: patch => () => appearance($, patch),
  };
}

/** One explicit command: open or focus an owned pane. Never forwards to another command or the prompt. */
async function command($, name, args) {
  if (S.internal) return {};
  if (!S.interactive) return { text: HEADLESS[name] };
  const raw = String(args || '').trim();
  const ticket = raw ? ticketArg(raw) : null;
  if (name === 'cg-panel' && /^close$/i.test(raw)) {
    await close($, MODULES.companion.id);
    return {};
  }
  if (raw && !ticket) return { text: 'Use /' + name + ' [TICKET], for example /' + name + ' ABC-1001.' };
  let result;
  if (name === 'cg-plan') {
    result = await open($, 'planner', {}, { pinned: ticket ? [ticket] : [], reread: Boolean(ticket),
      notice: ticket ? ticket + ' is pinned to the top for this session.' : undefined });
  } else if (ticket) {
    S.select = { ticket };
    S.notice = '';
    result = await open($, 'companion', {}, { tab: 'focus', reread: true });
  } else if (name === 'cg-resume') {
    result = await open($, 'planner', {}, { notice: 'Choose a task to resume. Resuming never starts coding; capture keeps running.' });
  } else {
    result = await open($, 'companion', {}, {});
  }
  if (!result.opened) return { text: clean(result.reason, 160) + '. ' + MODULES.companion.fallback + '.' };
  if (!result.placed) return { text: 'The pane is open but not shown here (' + (result.reason || 'no surface places it') + '). ' + MODULES.companion.fallback + '.' };
  return {};
}

export function register(on, options) {
  configure(options);

  on('session.start', async ($, e, next) => {
    S.internal = await internal($);
    if (!S.internal) {
      await $.command.register({ name: 'cg-panel', description: ABOUT['cg-panel'], argumentHint: '[TICKET | close]' });
      await $.command.register({ name: 'cg-plan', description: ABOUT['cg-plan'], argumentHint: '[TICKET]' });
      await $.command.register({ name: 'cg-resume', description: ABOUT['cg-resume'], argumentHint: '[TICKET]' });
      await startup($, e);
    }
    return next(e);
  }).catch(($, e, next) => next(e));

  on('session.end', ($, e, next) => {
    if (e.reason === 'clear' || e.reason === 'resume') newConversation($);
    else reset();
    return next(e);
  }).catch(($, e, next) => next(e));

  // Literal names, so `claude plugin validate` reads each as a command this module answers itself.
  on('command.run', { command: 'cg-panel' }, ($, e) => command($, 'cg-panel', e.args));
  on('command.run', { command: 'cg-plan' }, ($, e) => command($, 'cg-plan', e.args));
  on('command.run', { command: 'cg-resume' }, ($, e) => command($, 'cg-resume', e.args));

  // A person's close mark, Escape, or an unload: bookkeeping for our own IDs only; the close always goes on.
  on('ui.close', ($, e, next) => {
    if (PANES.has(e.id)) closed(e.id);
    return next(e);
  }).catch(($, e, next) => next(e));

  on('ui.render', { component: 'Pane' }, ($, e, next) => {
    const mod = PANES.get(e.requestId);
    if (!mod || S.internal) return next(e);
    return pane($.ui.resolve(e), e, mod, actions($));
  }).catch(($, e, next) => {
    if (next.called || !PANES.has(e.requestId)) return next(e);
    const { Box, Text } = $.ui.resolve(e); // a broken view never takes the pane or the session down
    return Box({ key: 'render-failed', children: [Text({ wrap: 'wrap', children: [
      'Chitragupta could not draw this view. Use /cg:dashboard, /cg:resume or /cg:plan meanwhile.'] })] });
  });

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (S.internal || !prefs().band || e.props.hasSurvey) return next(e);
    const below = await next(e);
    return band($.ui.resolve(e), e, below);
  });
}

