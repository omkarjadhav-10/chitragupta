// Pure drawing (ARCH-02, NFR-01): each function reads the session-local cache and returns a tree built from
// the surface's own element table. No bridge read, timer, write or other `$` call happens here; buttons
// carry handlers (`act`) that run later, on the person's press. Status is always words, never color alone.
import { MODULES } from './panes.js';
import { S, cachedRead, peekLocal, prefs, readError, viewReads } from './session.js';
import { HIDDEN, clean, count, measure, scrub, when } from './text.js';

const DASH = '/cg:dashboard';
const TAB = { focus: 'Focus', evidence: 'Evidence', attention: 'Attention' };
const ROUTES = {
  companion: 'Details: the Evidence tab, or /cg:dashboard then Companion',
  planner: 'Details: /cg:dashboard then Companion · text: /cg:plan',
  review: 'Details and editing: /cg:dashboard then Companion, Jev Review',
  database: 'Details: /cg:dashboard then Database (#/db)',
  pr: 'Details: /cg:ticket <TICKET>, or /cg:dashboard then the ticket\'s Delivery tab',
};
const IMPROVE = 'Improve Plan: run /cg:dashboard, then Companion, Jev Review, Improve Plan. It saves a new candidate '
  + 'version that you select there; nothing runs from this pane.';
const DB_LINK = /^#\/db(?:\?[\w=&-]{0,120})?$/;
const TYPING = new Set(['terminal', 'desktop', 'vscode']); // surfaces whose element table has Input

function kit(el, cols, p) {
  const gap = p.density === 'comfortable' ? 1 : 0;
  const width = Math.max(8, cols - 4);
  const keep = list => list.filter(Boolean);
  const text = (s, style) => el.Text({ wrap: 'wrap', ...(style || {}), children: [s] });
  const line = (key, s, style) => el.Box({ key, flexDirection: 'column', children: [text(s, style)] });
  const head = (key, s) => line(key, s, p.accent === 'none' ? { bold: true } : { bold: true, color: p.accent });
  return {
    text, line, head,
    dim: (key, s) => line(key, s, { dimColor: true }),
    button: (key, label, onPress, extra) => el.Button({ key, label: clean(label, width), onPress, ...(extra || {}) }),
    row: (key, children) => el.Box({ key, flexDirection: 'row', flexWrap: 'wrap', columnGap: 1, children: keep(children) }),
    col: (key, children) => el.Box({ key, flexDirection: 'column', rowGap: gap, children: keep(children) }),
  };
}

const masked = (p, data) => p.privacy || Boolean(data && data.privacy);
const title = (p, data, value) => (masked(p, data) ? HIDDEN : scrub(value, 200) || 'Untitled');

function status() {
  const snap = S.snapshot;
  if (!snap) return S.inflight > 0 ? 'Loading…' : 'Not read yet';
  const at = when(snap.generated_at);
  if (S.problem) return 'Stale: last read ' + at;
  return (S.inflight > 0 ? 'Reading… last read ' : 'Updated ') + at;
}

function errorText(err) {
  if (!err) return '';
  if (err.kind === 'setup') return 'Identity not confirmed: ' + err.text + '. Open profile setup with /cg:dashboard, then Profile.';
  if (err.kind === 'denied') return 'Not available in this profile: ' + err.text + '. Nothing from an earlier read is shown.';
  if (err.kind === 'reload') return err.text;
  if (err.kind === 'shape') return 'Unsupported reply: ' + err.text + '.';
  return 'Cannot read this now: ' + err.text + '. Showing the last read, if any; retry with Read cached updates or run /cg:doctor.';
}

// ---------------------------------------------------------------- shared parts

function header(k, el, mod, p) {
  const bits = [status()];
  if (p.privacy) bits.push('Privacy mode on');
  return el.Box({ key: 'header', flexDirection: 'row', flexWrap: 'wrap', columnGap: 1, children: [
    k.text(p.mark + ' · ' + mod.title, p.accent === 'none' ? { bold: true } : { bold: true, color: p.accent }),
    k.text('— ' + bits.join(' · '), { dimColor: true }),
  ] });
}

function tabRow(k, mod, view, local, act) {
  const attention = S.snapshot && S.snapshot.attention_summary ? S.snapshot.attention_summary.total || 0 : 0;
  const buttons = mod.tabs.map(t => k.button('tab-' + t, (local.tab === t ? '› ' : '') + TAB[t]
    + (t === 'attention' && mod.kind === 'companion' && attention ? ' ' + attention : ''), act.tab(view, t)));
  if (mod.kind === 'companion') buttons.push(k.button('settings', (local.menu ? '› ' : '') + 'Settings', act.menu(view)));
  return k.row('tabs', buttons);
}

function common(k, act) {
  const out = [];
  if (S.notice) out.push(k.row('notice', [k.text(clean(S.notice, 200)), k.button('dismiss', 'Dismiss', act.dismiss())]));
  if (S.problem) out.push(k.line('problem', errorText(S.problem)));
  if (!S.snapshot && !S.problem) {
    out.push(k.line('loading', S.inflight > 0 ? 'Loading cached Chitragupta state…' : 'Nothing read yet. Press Read cached updates.'));
  }
  return out;
}

function footer(k, mod, view, act) {
  return k.col('footer', [
    k.row('footer-actions', [
      mod.kind !== 'companion' ? k.button('to-companion', 'Back to Chitragupta', act.open('companion')) : null,
      mod.kind === 'companion' ? k.button('open-planner', 'Daily Planner', act.open('planner')) : null,
      k.button('refresh', 'Read cached updates', act.refresh()),
      k.button('close', 'Close', act.close(view)),
    ]),
    k.dim('fallback', 'Without this pane: ' + mod.fallback),
  ]);
}

function noticeBlock(k, view, local, act) {
  return k.col('local-notice', [
    k.text(clean(local.notice, 500)),
    local.packet ? k.text(local.packet) : null,
    k.row('local-notice-actions', [
      /\/cg:dashboard/.test(local.notice) ? k.button('copy-dashboard', 'Copy ' + DASH, act.copyCommand(view)) : null,
      k.button('clear-notice', 'OK', act.clearNotice(view)),
    ]),
  ]);
}

// ---------------------------------------------------------------- next action routing (MOD-07 semantics)

function route(na) {
  const label = clean(na.label, 60);
  const wf = na.workflow || null;
  const t = na.ticket ? clean(na.ticket, 30) : null;
  const dash = where => (act, view) => act.route(view, label + ': run ' + DASH + ', then ' + where + '. That page holds the protected action.');
  const tab = (act, view) => act.tab(view, 'evidence');
  switch (na.label) {
    case 'Choose a task': return { label: 'Open Daily Planner', press: act => act.open('planner') };
    case 'Open profile setup': return { label, press: dash('Profile, Identity') };
    case 'Open setup': return { label, press: dash('Setup') };
    case 'Generate Plan': return { label: 'Open plan editor', press: dash('Companion, ' + (t || 'the task') + ', Generate Plan or Write a plan manually') };
    case 'Review saved plan': return wf ? { label: 'Open Jev Review', press: act => act.open('review', { workflow: wf, ticket: t }) } : { label, press: dash('Companion, Jev Review') };
    case 'Copy approved plan': return wf ? { label, press: (act, view) => act.copyHandoff(view, wf) } : { label, press: dash('Companion, Handoff') };
    case 'Open PR Review': return wf ? { label, press: act => act.open('pr', { workflow: wf, ticket: t }) } : { label, press: dash('the ticket\'s Delivery tab') };
    case 'Verify Requirements': case 'View completion evidence': return { label: 'View evidence', press: tab };
    case 'Review Changes': return { label: 'View changes', press: tab };
    case 'Review continuation': return { label: 'View continuation', press: tab };
    default:
      if (na.kind === 'planner') {
        return wf ? { label: 'Resume ' + (t || 'task'), press: act => act.selectWorkflow(wf, t) }
          : { label: 'Open plan editor', press: dash('Companion, choose ' + (t || 'the task')) };
      }
      if (na.kind === 'delivery') return { label, press: dash('the ticket\'s Delivery tab (#/tickets/' + (t || '') + '?tab=delivery)') };
      return { label, press: dash('Companion, ' + (t || 'the task') + ', Resume') };
  }
}

function nextBlock(k, na, view, act) {
  if (!na) {
    return k.col('next-action', [k.head('next-head', 'Next action'),
      k.text('Nothing ranked yet. Choose a task in the Daily Planner.'),
      k.button('next', 'Open Daily Planner', act.open('planner'), { variant: 'primary' })]);
  }
  const r = route(na);
  return k.col('next-action', [k.head('next-head', 'Next action'),
    k.text(clean(na.label, 80) + (na.text ? ': ' + scrub(na.text, 300) : '')),
    k.button('next', r.label, r.press(act, view), { variant: 'primary' })]);
}

// ---------------------------------------------------------------- companion: Focus, Evidence, Attention

function detailFor(view) {
  const req = viewReads(view).find(r => r.operation === 'detail');
  return req ? { data: cachedRead(req), error: readError(req) } : { data: null, error: null };
}

function contextBlock(k, ctx, act) {
  if (ctx.state === 'explicit') return k.dim('context', 'Context: selected by you for this session');
  if (ctx.state === 'evidenced') {
    return k.row('context', [k.text('Suggested, not chosen: ' + scrub(ctx.evidence, 160)),
      k.button('use-suggested', 'Use this task', act.selectWorkflow(ctx.workflow, ctx.ticket))]);
  }
  if (ctx.state === 'ambiguous') {
    const cands = Array.isArray(ctx.candidates) ? ctx.candidates.slice(0, 5) : [];
    return k.col('context', [k.text(scrub(ctx.reason, 160) || 'Several tasks match; choose one'),
      ...cands.map((c, i) => k.row('candidate-' + i, [
        k.button('choose-' + i, clean(c.ticket, 30) + ' · ' + clean(c.repository || 'repository unknown', 30) + ' · ' + clean(c.stage, 24),
          act.selectWorkflow(c.workflow, c.ticket)),
        k.text(scrub(c.evidence, 120), { dimColor: true })]))]);
  }
  return k.dim('context', (clean(ctx.reason, 160) || 'No task selected') + '. Choose one in the Daily Planner or type a ticket below.');
}

function chooser(k, el, view, local, act) {
  if (!el.typing) return k.dim('chooser', 'Choose a ticket with /cg-panel ABC-1001');
  return k.col('chooser', [el.Input({ key: 'choose-ticket', label: 'Choose a ticket', placeholder: 'ABC-1001',
    value: local.draft, submitLabel: 'select', onInput: act.draft(view), onSubmit: act.submitTicket(view) })]);
}

function resumeBlock(k, d) {
  const avail = (Array.isArray(d.actions) ? d.actions : []).filter(a => a && a.available).slice(0, 8);
  return k.col('resume', [
    k.head('resume-head', 'Where it stands'),
    k.text(clean(d.situation_label, 80) + (d.next_action ? ' · next: ' + clean(d.next_action.label, 60) : '')),
    avail.length ? k.text('Task actions for this stage (protected; take them in ' + DASH + ', Companion, Resume): '
      + avail.map(a => clean(a.name, 40) + (a.reason ? ' (' + clean(a.reason, 90) + ')' : '')).join('; ')) : null,
    k.dim('resume-note', 'Resuming never starts coding; capture keeps running.'),
  ]);
}

function dbFocus(k, db, key) {
  return k.col(key, [
    k.text(scrub(db.operation_label, 120) + ' · ' + clean(db.environment || 'environment unknown', 40), { bold: true }),
    k.text('Last observed: ' + scrub(db.last_evidenced_step || 'nothing observed yet', 200)),
    k.text('Unresolved: ' + scrub(db.largest_gap || 'no gap recorded', 240)),
    k.text('Next: ' + scrub(db.next_action || 'open the flow in the dashboard', 300)),
  ]);
}

function focusTab(k, el, view, local, p, act) {
  const snap = S.snapshot;
  if (!snap) return [];
  if (snap.state === 'identity_unconfirmed' || snap.state === 'empty') {
    return [k.line('ticket', scrub(snap.empty, 300) || 'Nothing to show yet.'), nextBlock(k, snap.next_action, view, act)];
  }
  const ctx = snap.context || {};
  const f = snap.focus;
  const out = [];
  out.push(f ? k.line('ticket', clean(f.ticket, 40) + ' · ' + title(p, snap, f.title), { bold: true })
    : k.line('ticket', ctx.state === 'ambiguous' ? 'Several tasks match; choose one below' : 'No task selected'));
  const where = [];
  if (f && f.repository) where.push('Repository ' + scrub(f.repository, 80));
  if (snap.workflow_context) where.push('Stage: ' + clean(snap.workflow_context.situation_label, 60));
  if (f) where.push('Jira: ' + clean(f.jira_status, 60));
  if (where.length) out.push(k.dim('where', where.join(' · ')));
  out.push(nextBlock(k, snap.next_action, view, act));
  out.push(contextBlock(k, ctx, act));
  const facts = (f && Array.isArray(f.facts) ? f.facts : []).slice(0, 3);
  if (facts.length) out.push(k.col('facts', facts.map(x => k.text('• ' + scrub(x, 160)))));
  const task = ctx.workflow ? { workflow: ctx.workflow, ticket: ctx.ticket } : null;
  out.push(k.row('workflow-panes', [ // MOD-02A: temporary panes, each opened only on request
    task ? k.button('open-review', 'Jev Review', act.open('review', task)) : null,
    task ? k.button('open-pr', 'PR Review', act.open('pr', task)) : null,
    k.button('open-database', 'Database Investigation', act.open('database')),
  ]));
  const { data: d, error } = detailFor(view);
  if (d) out.push(resumeBlock(k, d));
  else if (error) out.push(k.line('detail-error', errorText(error)));
  const db = snap.database_context;
  if (db && db.available) out.push(dbFocus(k, db, 'db-focus'));
  out.push(chooser(k, el, view, local, act));
  return out;
}

function prLine(pr, p) {
  const m = /^pr:([^:]+):(\d+)$/.exec(String(pr.id || ''));
  const name = m ? clean(m[1], 40) + ' #' + m[2] : clean(pr.id, 60);
  return [name, clean(pr.outcome || 'state unknown', 20),
    pr.review ? 'review: ' + clean(pr.review, 30).replace(/_/g, ' ') : null,
    pr.branch ? 'branch ' + clean(pr.branch, 80) : null,
    pr.head ? 'head ' + clean(pr.head, 40).slice(0, 12) : null,
    pr.label ? (p.privacy ? HIDDEN : scrub(pr.label, 120)) : null].filter(Boolean).join(' · ');
}

function evidenceTab(k, view, local, p, act) {
  const snap = S.snapshot;
  if (!snap) return [];
  const out = [];
  const { data: d, error } = detailFor(view);
  if (!d) {
    const wf = snap.context && snap.context.workflow;
    out.push(k.line('evidence-empty', error ? errorText(error) : wf ? 'Reading this task\'s evidence…'
      : 'Choose a task to see its verification, delivery, pull requests and decisions.'));
  } else {
    const m = d.measures || {};
    const ver = d.verification || {};
    const rows = Array.isArray(ver.rows) ? ver.rows : [];
    const shown = local.expanded.ledger ? rows.slice(0, 20) : rows.slice(0, 3);
    out.push(k.col('verification', [
      k.head('verification-head', 'Verification: three separate measures'),
      k.text('Plan coverage: ' + measure(m.plan_coverage, 'Evaluation unavailable') + ' (advisory; never verification)'),
      k.text('Implementation progress: ' + measure(m.implementation_progress, 'No approved steps evidenced yet')),
      k.text('Functional verification: ' + measure(m.functional_verification, 'Not verified yet')),
      ...shown.map(r => k.text('• ' + clean(r.criterion, 20) + ' · ' + clean(r.status, 30) + ' · ' + scrub(r.applicability, 200))),
      rows.length > 3 ? k.button('ledger', local.expanded.ledger ? 'Show fewer criteria' : 'Show all criteria (' + rows.length + ')', act.toggle(view, 'ledger')) : null,
    ]));
    const ch = d.changes || {};
    const items = Array.isArray(ch.items) ? ch.items : [];
    out.push(k.col('changes', [
      k.head('changes-head', 'Since your last review'),
      k.text(ch.label ? clean(ch.label, 80) : count(ch.total || 0, 'change')),
      ...(local.expanded.changes ? items.slice(0, 10) : items.slice(0, 3)).map(c => k.text('• ' + scrub(c.label || c.id, 160)
        + (c.observed_at ? ' · ' + when(c.observed_at) : ''))),
      items.length > 3 ? k.button('changes-more', local.expanded.changes ? 'Show fewer changes' : 'More changes', act.toggle(view, 'changes')) : null,
      ch.total ? k.dim('changes-note', 'Reading never marks changes reviewed; Mark changes reviewed is in ' + DASH + ', Companion, Resume.') : null,
    ]));
    const dl = d.delivery || {};
    const rem = Array.isArray(dl.reminders) ? dl.reminders : [];
    out.push(k.col('delivery', [
      k.head('delivery-head', 'Delivery'),
      k.text('Release: ' + (dl.release ? scrub(dl.release, 160) : 'not resolved')),
      ...rem.map(r => k.text('• ' + clean(r.label, 60) + ': ' + scrub(r.text, 200))),
      k.dim('delivery-guidance', scrub(dl.guidance, 200)),
    ]));
    const prs = Array.isArray(d.prs) ? d.prs : [];
    out.push(k.col('prs', [
      k.head('prs-head', 'Pull requests and CI'),
      ...(prs.length ? prs.slice(0, 5).map(x => k.text('• ' + prLine(x, p))) : [k.text('No pull request recorded.')]),
      ...(Array.isArray(d.ci) && d.ci.length ? d.ci.slice(0, 5).map(c => k.text(scrub(c.text, 200)))
        : [k.text('CI checks unavailable')]),
    ]));
    const dec = Array.isArray(d.decisions) ? d.decisions : [];
    if (dec.length) out.push(k.col('decisions', [k.head('decisions-head', 'Decisions'), ...dec.slice(-3).map(x => k.text('• ' + scrub(x.text, 200)))]));
  }
  const db = snap.database_context;
  if (db && db.available && Array.isArray(db.observations)) {
    out.push(k.col('db-evidence', [k.head('db-evidence-head', 'Database evidence (cached)'),
      ...db.observations.slice(0, 3).map(o => k.text('• ' + scrub(o.step, 120) + ' · ' + clean(o.status_label || o.status, 40) + ' · ' + when(o.observed_at)))]));
  }
  const src = Array.isArray(snap.sources) ? snap.sources : [];
  const current = src.filter(s => s.freshness === 'current').length;
  out.push(k.col('sources', [k.head('sources-head', 'Sources: ' + current + ' current, ' + (src.length - current) + ' need attention'),
    ...src.slice(0, 8).map(s => k.text('• ' + clean(s.label, 40) + ': ' + clean(s.state, 30) + (s.observed_at ? ' · ' + when(s.observed_at) : '')))]));
  return out;
}

function attentionTab(k, view, local, p, act) {
  const snap = S.snapshot;
  if (!snap) return [];
  const a = snap.attention_summary || {};
  const items = Array.isArray(a.items) ? a.items : [];
  const out = [k.head('attention-head', 'Attention: ' + count(a.total || 0, 'recorded item'))];
  if (!items.length) out.push(k.text('No recorded attention items.'));
  items.slice(0, 10).forEach((it, i) => {
    const where = it.ticket ? '#/tickets/' + clean(it.ticket, 30) + (it.kind === 'release' ? '?tab=delivery' : '') : 'Companion';
    out.push(k.row('attention-' + i, [
      k.text(clean(it.label, 60) + (it.ticket ? ' · ' + clean(it.ticket, 30) : '') + ': ' + scrub(it.text, 240) + ' (' + clean(it.source, 40) + ')'),
      k.button('attention-go-' + i, clean(it.label, 40), act.route(view, clean(it.label, 60) + ': run ' + DASH + ', then ' + where
        + '. Acknowledging there never clears required work.')),
    ]));
  });
  if (a.more) out.push(k.dim('attention-more', 'More items in ' + DASH + ', Companion.'));
  out.push(k.dim('attention-semantics', scrub(a.semantics, 200) + ' · coverage: ' + clean(a.coverage || 'unknown', 20)));
  if (a.left_out) out.push(k.dim('attention-left-out', count(a.left_out, 'item') + ' outside your personal scope are not shown.'));
  const rec = snap.workflow_context && Array.isArray(snap.workflow_context.recovery) ? snap.workflow_context.recovery : [];
  if (rec.length) out.push(k.col('recovery', [k.head('recovery-head', 'This task needs'), ...rec.map(r => k.text('• ' + clean(r.label, 60) + (r.blocking ? ' (blocking)' : ' (advisory)')))]));
  const src = (Array.isArray(snap.sources) ? snap.sources : []).filter(s => s.freshness !== 'current');
  if (src.length) out.push(k.col('source-attention', [k.head('source-attention-head', 'Sources needing attention'),
    ...src.slice(0, 6).map(s => k.text('• ' + clean(s.label, 40) + ': ' + clean(s.state, 30) + (s.recovery ? ' · ' + scrub(s.recovery, 160) : '')))]));
  const db = snap.database_context;
  if (db && db.available && (db.freshness !== 'current' || db.coverage !== 'complete')) {
    out.push(k.dim('db-attention', 'Database: ' + clean(db.operation_label, 80) + ' · freshness ' + clean(db.freshness || 'unknown', 20)
      + ' · coverage ' + clean(db.coverage || 'unknown', 20) + '. Not observed is not failed; acknowledging never resolves a gap.'));
  }
  return out;
}

function settings(k, p, act) {
  return k.col('settings-menu', [
    k.head('settings-head', 'Settings and help'),
    k.row('privacy-row', [k.text('Privacy mode: ' + (p.privacy ? 'on (titles masked)' : 'off')),
      k.button('privacy', p.privacy ? 'Turn privacy mode off' : 'Turn privacy mode on', act.appearance({ privacy: !p.privacy }))]),
    k.row('density-row', [k.text('Density: ' + p.density),
      k.button('density', p.density === 'compact' ? 'Use comfortable density' : 'Use compact density',
        act.appearance({ density: p.density === 'compact' ? 'comfortable' : 'compact' }))]),
    k.text('Header mark: ' + p.mark + (p.markValid ? '' : ' (the custom mark was rejected: use at most 12 plain characters)')
      + ' · accent: ' + p.accent),
    k.text('Prompt band: ' + (p.band ? 'on' : 'off') + ' · open at start: ' + (p.autoOpen ? 'on' : 'off')
      + '. Both default off; change them in /config under cg-companion.'),
    k.button('reset-appearance', 'Reset appearance', act.appearance(null)),
    k.dim('branding', 'Branding scope: the mark changes only this companion\'s own header. Claude Code\'s logo and messages '
      + 'are unchanged; replacing the Claude logo is not supported.'),
    k.dim('help', 'Tested on Claude Code 2.1.302. Disable only the companion with /plugin (cg-companion, Disable) or by '
      + 'dropping its --plugin-dir; capture, the dashboard and the /cg: commands keep working.'),
  ]);
}

// ---------------------------------------------------------------- temporary panes

function plannerBody(k, view, local, p, act) {
  const req = viewReads(view)[0];
  const d = cachedRead(req);
  const err = readError(req);
  const out = [];
  if (err) out.push(k.line('planner-error', errorText(err)));
  if (!d) return out.concat([k.line('planner-loading', S.inflight > 0 ? 'Loading the planner…' : 'No planner read yet.')]);
  if (d.empty) out.push(k.line('planner-empty', scrub(d.empty, 240)));
  const current = local.group || 'all';
  const groups = (Array.isArray(d.groups) ? d.groups : []).filter(g => g && g.id !== 'completed');
  out.push(k.row('groups', [
    k.button('group-all', (current === 'all' ? '› ' : '') + 'All open stages', act.planner(view, { group: null, cursor: null, page: 0 })),
    ...groups.map(g => k.button('group-' + g.id, (current === g.id ? '› ' : '') + clean(g.label, 24) + ' (' + (g.count || 0) + ')',
      act.planner(view, { group: g.id, cursor: null, page: 0 }))),
  ]));
  (Array.isArray(d.items) ? d.items : []).forEach((c, i) => {
    const attempts = Array.isArray(c.attempts) ? c.attempts : [];
    const buttons = attempts.length > 1
      ? attempts.slice(0, 4).map((a, j) => k.button('attempt-' + i + '-' + j, 'Attempt in ' + clean(a.repository || 'repository unknown', 24)
        + ' · ' + clean(a.stage, 20), act.selectWorkflow(a.workflow, c.ticket)))
      : c.workflow ? [k.button('resume-' + i, 'Resume ' + clean(c.ticket, 30), act.selectWorkflow(c.workflow, c.ticket))]
        : [k.button('plan-' + i, 'Open plan editor', act.route(view, 'Plan ' + clean(c.ticket, 30) + ': run ' + DASH
          + ', then Companion, choose ' + clean(c.ticket, 30) + ', then Generate Plan or Write a plan manually.'))];
    out.push(k.col('card-' + i, [
      k.text(clean(c.ticket, 30) + ' · ' + title(p, d, c.title) + ' · ' + clean(c.group_label || c.stage, 30), { bold: true }),
      k.text('Why: ' + scrub(c.why, 240), { dimColor: true }),
      k.row('card-actions-' + i, buttons),
    ]));
  });
  out.push(k.row('planner-pages', [
    d.more && d.cursor ? k.button('more', 'More tasks', act.planner(view, { cursor: d.cursor, page: (local.page || 0) + 1 })) : null,
    local.cursor ? k.button('first', 'First page', act.planner(view, { cursor: null, page: 0 })) : null,
    d.completed && d.completed.count && current !== 'completed'
      ? k.button('completed', 'Show completed (' + d.completed.count + ')', act.planner(view, { group: 'completed', cursor: null, page: 0 })) : null,
  ]));
  out.push(k.dim('planner-total', count(d.total || 0, 'task') + ' in this view · page ' + ((local.page || 0) + 1)));
  return out;
}

function executeBlock(k, r) {
  const x = r.execute || {};
  const t = x.target || {};
  const f = x.findings || {};
  const risks = Array.isArray(x.risks) ? x.risks : [];
  return k.col('execute-detail', [
    k.head('execute-head', 'Before you approve (in the dashboard)'),
    k.text('Version ' + clean(x.plan, 16) + '… · digest ' + clean(x.digest, 14) + '… · baseline ' + clean(x.baseline, 16)
      + ' (' + clean(x.baseline_completeness || 'unknown', 20) + ')'),
    k.text('Target: repository ' + clean(t.repository || 'unknown', 60) + ' · ' + clean(t.session, 120)),
    k.text('Findings: ' + clean(f.evaluation || 'Evaluation unavailable', 60) + ' · open criteria: '
      + (Array.isArray(f.open_criteria) ? f.open_criteria.length : 0)),
    ...risks.slice(0, 5).map(x2 => k.text('Risk: ' + scrub(x2, 200))),
    k.text('Acknowledgement: ' + clean(x.acknowledgement, 300)),
    k.text('After approval: ' + clean(x.outcome || 'Prepared; not submitted', 40) + '. Approve the exact version in ' + DASH
      + ', Companion, Jev Review, Execute Plan. This pane never starts execution.'),
  ]);
}

function reviewBody(k, view, local, p, act) {
  const ctx = view.context || {};
  if (!ctx.workflow) return [k.line('review-empty', 'No task is bound to this review. Open it from the companion\'s next action or the Daily Planner.')];
  const reqs = viewReads(view);
  const r = cachedRead(reqs[0]);
  const err = readError(reqs[0]);
  const jobs = cachedRead(reqs[1]);
  const out = [];
  if (err) out.push(k.line('review-error', errorText(err)));
  if (!r) return out.concat([k.line('review-loading', S.inflight > 0 ? 'Loading Jev Review…' : 'No review read yet.')]);
  const versions = Array.isArray(r.versions) ? r.versions : [];
  const sel = versions.find(v => v && v.selected);
  out.push(k.line('review-ticket', clean(r.ticket, 30) + ' · ' + (sel ? 'plan version ' + sel.number + ' of ' + versions.length : 'no plan selected')
    + ' · stage ' + clean(r.stage, 30)));
  const ev = r.evaluation || {};
  out.push(k.col('evaluation', [
    k.head('evaluation-head', 'Evaluation'),
    k.text(clean(ev.label || 'Evaluation unavailable', 80) + (Number.isFinite(ev.percent) ? ' · ' + ev.percent + '%' : '')
      + (ev.reason ? ': ' + scrub(ev.reason, 240) : '')),
    k.text(clean(r.assessment_label || 'Developer assessment (yours; never a Jev score)', 80) + ': '
      + (r.assessment ? clean(r.assessment.label, 60) + (Number.isFinite(r.assessment.percent) ? ' · ' + r.assessment.percent + '%' : '') : 'not recorded')),
  ]));
  const choices = (Array.isArray(r.choices) ? r.choices : []).filter(c => c && (c.action === 'Improve Plan' || c.action === 'Execute Plan'));
  out.push(k.head('choices-head', 'Improve or execute'));
  for (const c of choices) {
    const improve = c.action === 'Improve Plan';
    const key = improve ? 'improve' : 'execute';
    out.push(k.col('choice-' + key, [
      k.text(c.action + (c.recommended ? ' — Recommended.' : '') + (c.available ? '' : ' — Unavailable: ' + scrub(c.reason, 200)), { bold: Boolean(c.recommended) }),
      c.why ? k.text(scrub(c.why, 200), { dimColor: true }) : null,
      k.button(key, improve ? 'Open plan editor (Improve Plan)' : (local.expanded.execute ? 'Hide approval review' : 'Review approval (Execute Plan)'),
        improve ? act.route(view, IMPROVE) : act.toggle(view, 'execute'), c.recommended ? { variant: 'primary' } : undefined),
    ]));
  }
  if (local.expanded.execute) out.push(executeBlock(k, r));
  if (r.stage === 'approved' || r.stage === 'execution_ready' || r.approved_plan) {
    out.push(k.row('handoff', [k.text('Approved handoff: Prepared; not submitted.'),
      k.button('copy-plan', 'Copy approved plan', act.copyHandoff(view, ctx.workflow))]));
  }
  const active = jobs && Array.isArray(jobs.active) ? jobs.active : [];
  if (active.length) out.push(k.col('jobs', active.map(j => k.text('Plan job: ' + clean(j.label || j.state, 40)))));
  const cands = Array.isArray(r.candidates) ? r.candidates : [];
  if (cands.length) out.push(k.dim('candidates', count(cands.length, 'candidate version') + '; select one in ' + DASH + ', Companion, Jev Review.'));
  return out;
}

function databaseBody(k, el, view, local, p, act) {
  const out = [];
  out.push(el.typing
    ? k.col('db-chooser', [el.Input({ key: 'choose-db', label: 'Flow or investigation', placeholder: 'f_… or i_…', value: S.dbDraft,
      submitLabel: 'open', onInput: act.dbDraft(), onSubmit: act.chooseDb() })])
    : k.dim('db-chooser', 'Choose a saved flow in ' + DASH + ', Database (#/db).'));
  if (S.dbNotice) out.push(k.line('db-notice', S.dbNotice));
  const ticket = S.snapshot && S.snapshot.context ? S.snapshot.context.ticket : null;
  out.push(k.dim('db-ticket', 'Ticket context unchanged: ' + (ticket ? clean(ticket, 30) : 'none selected')));
  if (!S.db) {
    out.push(k.line('db-empty', 'No database flow or investigation selected.'));
  } else {
    const req = viewReads(view)[0];
    const d = req ? cachedRead(req) : null;
    const err = req ? readError(req) : null;
    const ctx = d && d.database_context;
    if (err) out.push(k.line('db-error', errorText(err)));
    if (!d) out.push(k.line('db-loading', S.inflight > 0 ? 'Loading cached database context…' : 'No database read yet.'));
    else if (!ctx || !ctx.available) out.push(k.line('db-unavailable', 'Database context unavailable: ' + scrub(ctx && ctx.reason, 200)));
    else if (local.tab === 'evidence') {
      const links = ctx.links || {};
      const link = s => (DB_LINK.test(String(s || '')) ? s : '#/db');
      out.push(k.col('db-evidence', [k.head('db-evidence-head', 'Observed sequence (cached)'),
        ...(Array.isArray(ctx.observations) ? ctx.observations : []).slice(0, 3).map(o => k.text('• ' + scrub(o.step, 120) + ' · '
          + clean(o.status_label || o.status, 40) + ' · ' + when(o.observed_at))),
        k.text('Open the flow: ' + DASH + ' then ' + link(links.flow || links.workspace)),
        k.text('Evidence: ' + link(links.evidence || links.workspace) + ' · Record journey: ' + link(links.journey || links.workspace))]));
    } else if (local.tab === 'attention') {
      out.push(k.col('db-attention', [k.head('db-attention-head', 'Database attention'),
        k.text('Freshness: ' + clean(ctx.freshness || 'unknown', 30) + (ctx.freshness === 'stale' ? ' (requires revalidation)' : '')
          + ' · coverage: ' + clean(ctx.coverage || 'unknown', 30) + ' · updated ' + when(ctx.updated_at)),
        k.text('Not observed is not failed; acknowledging an item never resolves a gap or changes its evidence.')]));
    } else {
      out.push(dbFocus(k, ctx, 'db-focus'));
    }
  }
  out.push(k.row('db-actions', [S.db ? k.button('leave-db', 'Leave investigation', act.leaveDb()) : null]));
  out.push(k.dim('db-never', 'Reads cached evidence only. This pane never queries a database or starts a trace; a trace needs '
    + 'a reviewed plan in ' + DASH + ', Database, Record journey.'));
  return out;
}

function prBody(k, view, local, p) {
  const ctx = view.context || {};
  if (!ctx.workflow) return [k.line('pr-empty', 'No task is bound to this PR review. Open it from the companion.')];
  const req = viewReads(view)[0];
  const d = cachedRead(req);
  const err = readError(req);
  const out = [];
  if (err) out.push(k.line('pr-error', errorText(err)));
  if (!d) return out.concat([k.line('pr-loading', S.inflight > 0 ? 'Loading recorded pull requests…' : 'No read yet.')]);
  out.push(k.line('pr-ticket', clean(d.ticket, 30) + ' · ' + title(p, d, d.title)));
  const prs = Array.isArray(d.prs) ? d.prs : [];
  out.push(k.head('pr-head', 'Recorded pull requests'));
  if (!prs.length) out.push(k.line('pr-none', 'No pull request is recorded for this task yet.'));
  prs.slice(0, 10).forEach((x, i) => out.push(k.line('pr-' + i, prLine(x, p))));
  const ci = Array.isArray(d.ci) ? d.ci.slice(0, 5) : [];
  if (ci.length) ci.forEach((x, i) => out.push(k.line('ci-' + i, scrub(x.text, 200))));
  else out.push(k.line('ci', 'CI checks unavailable'));
  const c = d.continuation || {};
  const asked = Array.isArray(c.requested_changes) ? c.requested_changes : [];
  const gaps = Array.isArray(c.head_gaps) ? c.head_gaps : [];
  if (asked.length || gaps.length || c.scope) {
    out.push(k.col('pr-continuation', [k.head('pr-continuation-head', 'Continuation'),
      asked.length ? k.text('Changes requested on: ' + asked.map(x => clean(x, 60)).join(', ')) : null,
      gaps.length ? k.text('Open with a newer head: ' + gaps.map(x => clean(x, 60)).join(', ')) : null,
      c.scope ? k.text(scrub(c.scope, 200)) : null]));
  }
  return out;
}

// ---------------------------------------------------------------- entry points

/** One owned pane, drawn from the cache. */
export function pane(table, e, mod, act) {
  const el = { ...table, typing: TYPING.has(e.surface) };
  const view = S.views.get(mod.id) || { id: mod.id, kind: mod.kind, open: true, placed: true, context: {} };
  const p = prefs();
  const cols = Math.max(20, Number(e.props && e.props.bodyColumns) || (e.viewport && e.viewport.columns) || 80);
  const k = kit(el, cols, p);
  const local = peekLocal(view);
  const parts = [header(k, el, mod, p)];
  if (mod.tabs.length) parts.push(tabRow(k, mod, view, local, act));
  parts.push(k.dim('details-route', ROUTES[mod.kind]));
  parts.push(...common(k, act));
  if (mod.kind === 'companion' && local.menu) parts.push(settings(k, p, act));
  if (mod.kind === 'companion') {
    const tab = local.tab === 'evidence' ? evidenceTab : local.tab === 'attention' ? attentionTab : null;
    parts.push(...(tab ? tab(k, view, local, p, act) : focusTab(k, el, view, local, p, act)));
  } else if (mod.kind === 'planner') parts.push(...plannerBody(k, view, local, p, act));
  else if (mod.kind === 'review') parts.push(...reviewBody(k, view, local, p, act));
  else if (mod.kind === 'database') parts.push(...databaseBody(k, el, view, local, p, act));
  else if (mod.kind === 'pr') parts.push(...prBody(k, view, local, p));
  if (local.notice) parts.push(noticeBlock(k, view, local, act));
  parts.push(footer(k, mod, view, act));
  return k.col('root', parts);
}

/** MOD-05: one opt-in line above the prompt, from the cache, composed with whatever the mods beneath drew. */
export function band(el, e, below) {
  const p = prefs();
  const cols = Math.max(20, Number(e.props && e.props.bodyColumns) || 80);
  const snap = S.snapshot;
  let text;
  if (!snap) text = p.mark + ' · Chitragupta · /cg-panel opens the companion';
  else {
    const t = snap.focus ? clean(snap.focus.ticket, 30) : null;
    const na = snap.next_action ? clean(snap.next_action.label, 60) : null;
    const att = snap.attention_summary ? snap.attention_summary.total || 0 : 0;
    const db = snap.database_context && snap.database_context.available && !p.privacy
      ? clean(snap.database_context.operation_label, 40) + ' · ' + clean(snap.database_context.environment, 20) : null;
    text = cols < 60
      ? [p.mark, t || 'no task', att ? count(att, 'item') : null, '/cg-panel'].filter(Boolean).join(' · ')
      : [p.mark, t || 'No task selected', na ? 'next: ' + na : null, db, count(att, 'attention item'),
        'as of ' + when(snap.generated_at)].filter(Boolean).join(' · ');
  }
  return el.Box({ flexDirection: 'column', children: [
    el.Box({ key: 'cg-band', children: [el.Text({ dimColor: true, wrap: 'truncate-end', children: [clean(text, cols)] })] }),
    below,
  ].filter(Boolean) });
}

export { MODULES };
