// Workflow panes, rendering states, sizes, surfaces, privacy, branding, band and coexistence
// (MOD-02..06, FLOW-11 native, PLAN-03, EXEC-01 copy; A23, A25, A26, A41, A53, A62-A64).
import { expect, test } from 'claude-code/testing';
import { CURSOR, FLOW, LOCATION, PLUGIN, START, W1, W3, clean, drawn, envelope, host, mountPane, paneProps } from './host.ts';

const BAND = { hasSurvey: false, isWorking: false, maxRows: 6, scroll: { offset: 0, bodyRows: 5 }, view: {} };
const band = ($: any, cols = 120, plugin = PLUGIN, extra = {}) => $.ui.mount({ plugin, component: 'AbovePrompt', surface: 'terminal',
  viewport: { columns: cols, rows: 40 }, props: { ...BAND, bodyColumns: cols - 5, ...extra } });

test('Daily Planner: groups with counts, why, attempt chooser, paging and a protected plan route', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-plan' });
  expect(h.calls.opens[0]).toMatchObject({ id: 'cg-planner', title: 'Daily Planner', focus: true, closeOnEscape: true });
  const ui = await mountPane($, 'cg-planner');
  expect((await ui.find({ key: 'card-0' }))?.text).toContain('Why: Resumable: review incomplete');
  expect((await ui.find({ key: 'group-paused' }))?.props.label).toBe('Paused (1)');
  await ui.press({ key: 'more' });
  expect(h.calls.reads.at(-1).args).toMatchObject({ operation: 'planner', cursor: CURSOR });
  expect((await ui.find({ key: 'attempt-0-1' }))?.props.label).toBe('Attempt in shop-web · in_development');
  await ui.press({ key: 'first' });
  await ui.press({ key: 'group-paused' });
  expect(h.calls.reads.at(-1).args).toEqual({ operation: 'planner', group: 'paused' });
  await ui.press({ key: 'group-all' });
  await ui.press({ key: 'plan-1' });
  expect((await ui.find({ key: 'local-notice' }))?.text).toContain('Generate Plan or Write a plan manually');
  await ui.press({ key: 'copy-dashboard' });
  expect(h.calls.copies).toEqual(['/cg:dashboard']);
  await ui.press({ key: 'resume-0' });
  expect(h.calls.opens.at(-1).id).toBe('cg-companion');
  expect(h.calls.reads.find(r => r.args.operation === 'snapshot' && r.args.workflow === W1)).toBeDefined();
  await ui.unmount();
  clean(h);
});

test('choosing one attempt of several selects exactly that workflow (never the first by default)', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-plan', args: '' });
  const ui = await mountPane($, 'cg-planner');
  await ui.press({ key: 'more' });
  await ui.press({ key: 'attempt-0-1' });
  expect(h.calls.reads.filter(r => r.args.operation === 'snapshot').at(-1).args).toEqual({ operation: 'snapshot', workflow: W3 });
  await ui.unmount();
  clean(h);
});

test('PLAN-03: Jev Review shows Improve Plan and Execute Plan, exactly one recommended in words, evaluation honest', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const companion = await mountPane($, 'cg-companion');
  await companion.press({ key: 'next' }); // "Open Jev Review"
  expect(h.calls.opens.at(-1)).toMatchObject({ id: 'cg-review', title: 'Jev Review', closeOnEscape: true });
  expect(h.ops().slice(-2)).toEqual(['review', 'jobs']);
  const ui = await mountPane($, 'cg-review');
  const tree = await drawn(ui);
  expect((await ui.find({ key: 'choice-improve' }))?.text).toContain('Improve Plan — Recommended.');
  expect((await ui.find({ key: 'choice-execute' }))?.text).toContain('Execute Plan');
  expect(tree.split('Recommended.').length - 1).toBe(1);
  expect((await ui.find({ key: 'improve' }))?.props.variant).toBe('primary');
  expect((await ui.find({ key: 'execute' }))?.props.variant).toBeUndefined();
  expect((await ui.find({ key: 'evaluation' }))?.text).toContain('Evaluation unavailable');
  expect(tree).not.toMatch(/\b0%/);
  await ui.press({ key: 'execute' });
  const exec = (await ui.find({ key: 'execute-detail' }))?.text || '';
  expect(exec).toContain('I reviewed this exact version');
  expect(exec).toContain('Prepared; not submitted');
  expect(exec).toContain('never starts execution');
  expect(exec).toContain('repository shop-api');
  expect(await drawn(ui)).not.toContain('Users\\\\dev'); // the absolute location is never drawn
  await ui.press({ key: 'improve' });
  expect((await ui.find({ key: 'local-notice' }))?.text).toContain('Improve Plan: run /cg:dashboard');
  await ui.unmount();
  await companion.unmount();
  clean(h);
});

test('A63: Copy approved plan copies the exact prepared text and says Prepared; not submitted', async ($, on) => {
  const h = host(on);
  const review = h.state.data.review;
  h.state.data.review = (args: any, gen: string) => ({ ...review(args, gen), stage: 'approved', approved_plan: 'p_' + 'a'.repeat(64) });
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const companion = await mountPane($, 'cg-companion');
  await companion.press({ key: 'open-review' });
  const ui = await mountPane($, 'cg-review');
  await ui.press({ key: 'copy-plan' });
  await ui.press({ key: 'copy-plan' }); // a double press copies twice; it never submits or records anything
  expect(h.calls.copies).toEqual(['APPROVED PLAN p_aaaa\n1. Fix rounding\n2. Add tests', 'APPROVED PLAN p_aaaa\n1. Fix rounding\n2. Add tests']);
  const note = (await ui.find({ key: 'local-notice' }))?.text || '';
  expect(note).toContain('Prepared; not submitted');
  expect(note).not.toMatch(/started|executing|submitted to/i);
  expect(h.ops().filter(o => o === 'handoff')).toHaveLength(2);
  await ui.unmount();
  await companion.unmount();
  clean(h);
});

test('A64: where the host cannot copy, the text is shown with how to copy it; no handoff means no copy', async ($, on) => {
  const h = host(on, { copy: false });
  const review = h.state.data.review;
  h.state.data.review = (args: any, gen: string) => ({ ...review(args, gen), stage: 'approved' });
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const companion = await mountPane($, 'cg-companion');
  await companion.press({ key: 'open-review' });
  const ui = await mountPane($, 'cg-review');
  await ui.press({ key: 'copy-plan' });
  const note = (await ui.find({ key: 'local-notice' }))?.text || '';
  expect(note).toContain('Copy is unavailable here (no-clipboard)');
  expect(note).toContain('1. Fix rounding');
  h.state.data.handoff = (args: any, gen: string) => envelope('handoff', gen, { state: 'unavailable', label: 'No current approved handoff',
    reason: 'The approval expired; review it again', text: null });
  await ui.press({ key: 'copy-plan' });
  expect((await ui.find({ key: 'local-notice' }))?.text).toContain('No approved plan is prepared to copy: The approval expired');
  expect(h.calls.copies).toEqual(['APPROVED PLAN p_aaaa\n1. Fix rounding\n2. Add tests']);
  await ui.unmount();
  await companion.unmount();
  clean(h);
});

test('FLOW-11: the Database Investigation pane maps cached context and never queries or traces', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const companion = await mountPane($, 'cg-companion');
  await companion.press({ key: 'open-database' });
  const ui = await mountPane($, 'cg-database');
  expect((await ui.find({ key: 'db-empty' }))?.text).toContain('No database flow or investigation selected');
  const before = h.calls.reads.length;
  await ui.input({ key: 'choose-db', text: "f_zz'; drop table orders" });
  expect(h.calls.reads.length).toBe(before);
  expect((await ui.find({ key: 'db-notice' }))?.text).toContain('flow ID (f_…)');
  await ui.input({ key: 'choose-db', text: FLOW });
  expect(h.calls.reads.find(r => r.args.operation === 'database')?.args).toEqual({ operation: 'database', flow: FLOW });
  expect(h.calls.reads.filter(r => r.args.operation === 'snapshot').at(-1).args).toEqual({ operation: 'snapshot', ticket: 'ABC-1001', flow: FLOW });
  expect((await ui.find({ key: 'db-focus' }))?.text).toContain('Cancel order · QA');
  expect((await ui.find({ key: 'db-focus' }))?.text).toContain('Unresolved: Refund not observed in this query scope');
  expect((await ui.find({ key: 'db-ticket' }))?.text).toBe('Ticket context unchanged: ABC-1001');
  await ui.press({ key: 'tab-evidence' });
  const ev = (await ui.find({ key: 'db-evidence' }))?.text || '';
  expect(ev).toContain('Refund issued · Not checked · time unknown');
  expect(ev).toContain('#/db?flow=' + FLOW + '&tab=journey');
  await ui.press({ key: 'tab-attention' });
  expect((await ui.find({ key: 'db-attention' }))?.text).toContain('(requires revalidation)');
  expect((await ui.find({ key: 'db-attention' }))?.text).toContain('Not observed is not failed');
  expect((await companion.find({ key: 'db-focus' }))?.text).toContain('Cancel order · QA');
  await ui.press({ key: 'leave-db' });
  expect(h.calls.reads.filter(r => r.args.operation === 'snapshot').at(-1).args).toEqual({ operation: 'snapshot', ticket: 'ABC-1001' });
  expect(new Set(h.ops())).toEqual(new Set(['snapshot', 'detail', 'database']));
  await ui.unmount();
  await companion.unmount();
  clean(h);
});

test('PR Review shows recorded pull request facts only and never proposes creating one', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const companion = await mountPane($, 'cg-companion');
  await companion.press({ key: 'open-pr' });
  const ui = await mountPane($, 'cg-pr');
  expect((await ui.find({ key: 'pr-0' }))?.text)
    .toBe('shop-api #41 · open · review: changes requested · branch feature/abc-1001-discount · head abc1234def56 · Fix discount rounding');
  expect((await ui.find({ key: 'ci' }))?.text).toBe('CI checks unavailable');
  expect((await ui.find({ key: 'pr-continuation' }))?.text).toContain('Changes requested on: pr:shop-api:41');
  expect(await drawn(ui)).not.toMatch(/(create|open) (a |new )?(pull request|PR)\b/i);
  await ui.unmount();
  await companion.unmount();
  clean(h);
});

test('A23: widths 80/100/120/160 x rows 24/40 keep ticket, next action and details route, with long values', { timeoutMs: 20_000 }, async ($, on) => {
  const h = host(on);
  const snap = h.state.data.snapshot;
  const long = 'Correct the discount calculation for stacked vouchers across '.repeat(5).trim();
  h.state.data.snapshot = (args: any, gen: string) => {
    const s = snap(args, gen);
    return { ...s, focus: { ...s.focus, title: long, repository: 'shop-api-' + 'x'.repeat(80) } };
  };
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const reads = h.calls.reads.length;
  for (const cols of [80, 100, 120, 160]) {
    for (const rows of [24, 40]) {
      const ui = await mountPane($, 'cg-companion', 'terminal', cols, rows);
      expect((await ui.find({ key: 'ticket' }))?.text).toContain('ABC-1001 · Correct the discount');
      expect((await ui.find({ key: 'next-action' }))?.text).toContain('Review saved plan');
      expect(await ui.find({ key: 'details-route' })).toBeDefined();
      for (const b of await ui.findAll({ type: 'Button' })) expect(String(b.props.label).length).toBeLessThanOrEqual(cols - 8);
      for (const tab of ['tab-evidence', 'tab-attention', 'tab-focus']) await ui.press({ key: tab });
      await ui.unmount();
    }
  }
  expect(h.calls.reads.length).toBe(reads);
  clean(h);
});

test('surfaces: desktop and vscode draw the same companion; mobile, which has no Input, gets a command route', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  for (const surface of ['desktop', 'vscode', 'mobile'] as const) {
    const ui = await mountPane($, 'cg-companion', surface, 100, 40);
    expect((await ui.find({ key: 'ticket' }))?.text).toContain('ABC-1001');
    expect((await ui.find({ key: 'next-action' }))?.text).toContain('Review saved plan');
    if (surface === 'mobile') expect((await ui.find({ key: 'chooser' }))?.text).toContain('/cg-panel ABC-1001');
    else expect(await ui.find({ key: 'choose-ticket' })).toBeDefined();
    await ui.unmount();
  }
  clean(h);
});

test('A26: privacy mode from settings masks titles at once, with no read; the mark is validated; Reset appearance', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const ui = await mountPane($, 'cg-companion');
  expect((await ui.find({ key: 'ticket' }))?.text).toContain('Correct discount calculation');
  const reads = h.calls.reads.length;
  await ui.press({ key: 'settings' });
  expect((await ui.find({ key: 'branding' }))?.text).toContain('replacing the Claude logo is not supported');
  await ui.press({ key: 'privacy' });
  expect((await ui.find({ key: 'ticket' }))?.text).toBe('ABC-1001 · Title hidden (privacy mode)');
  expect((await ui.find({ key: 'header' }))?.text).toContain('Privacy mode on');
  expect(h.calls.reads.length).toBe(reads);
  await ui.press({ key: 'density' });
  expect((await ui.drawn()).props?.rowGap).toBe(1);
  await ui.press({ key: 'reset-appearance' });
  expect((await ui.drawn()).props?.rowGap).toBe(0);
  expect((await ui.find({ key: 'header' }))?.text).toContain('Privacy mode on'); // reset keeps privacy
  await ui.press({ key: 'tab-attention' });
  const tree = await drawn(ui);
  expect(tree).toContain('owner [email]');
  expect(tree).not.toContain('dev@acme.test');
  await ui.unmount();
  clean(h);
});

test('A26: configured privacy is sent to the projection; an unsafe custom mark falls back to CG', { options: { privacy: true, mark: '\u001b[2JX' } }, async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  expect(h.calls.reads[0].args).toEqual({ operation: 'snapshot', ticket: 'ABC-1001', privacy: true });
  expect(h.calls.reads[1].args).toEqual({ operation: 'detail', workflow: W1, privacy: true });
  const ui = await mountPane($, 'cg-companion');
  expect((await ui.find({ key: 'header' }))?.text).toContain('CG · Chitragupta');
  await ui.press({ key: 'settings' });
  expect((await ui.find({ key: 'settings-menu' }))?.text).toContain('the custom mark was rejected');
  expect(await drawn(ui)).not.toContain('Correct discount calculation');
  await ui.unmount();
  clean(h);
});

test('MOD-06: a short custom mark replaces the header mark only', { options: { mark: 'ACME', density: 'comfortable', accent: 'none' } }, async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel' });
  const ui = await mountPane($, 'cg-companion');
  expect((await ui.find({ key: 'header' }))?.text).toContain('ACME · Chitragupta');
  expect((await ui.drawn()).props?.rowGap).toBe(1);
  await ui.unmount();
  clean(h);
});

test('UX-03: unconfirmed identity, no recorded work, denied detail and an unavailable bridge are honest states', async ($, on) => {
  const h = host(on);
  const snap = h.state.data.snapshot;
  h.state.data.snapshot = (args: any, gen: string) => ({ ...snap(args, gen), state: 'identity_unconfirmed', focus: null,
    empty: 'Confirm your identity and scope in Setup. Nothing of yours is shown until then.',
    next_action: { label: 'Open profile setup', text: 'Confirm your commit email and accounts', kind: 'setup', target: '#/profile?section=identity' } });
  await $.command.run({ command: 'cg-panel' });
  const ui = await mountPane($, 'cg-companion');
  expect((await ui.find({ key: 'ticket' }))?.text).toContain('Confirm your identity');
  await ui.press({ key: 'next' });
  expect((await ui.find({ key: 'local-notice' }))?.text).toContain('Open profile setup: run /cg:dashboard, then Profile, Identity');
  h.state.data.snapshot = (args: any, gen: string) => ({ ...snap(args, gen), state: 'empty', focus: null,
    empty: 'No recorded work yet. Work in a repository in your scope with Claude Code.' });
  await ui.press({ key: 'refresh' });
  expect((await ui.find({ key: 'ticket' }))?.text).toContain('No recorded work yet');
  h.state.data.snapshot = snap;
  await ui.press({ key: 'refresh' });
  h.state.errors.detail = ['The selected task is unavailable in this profile'];
  await ui.input({ key: 'choose-ticket', text: 'ABC-1001' });
  expect((await ui.find({ key: 'detail-error' }))?.text).toContain('Not available in this profile');
  expect(await ui.find({ key: 'resume' })).toBeUndefined();
  h.state.errors.snapshot = ['Companion read unavailable'];
  await ui.press({ key: 'refresh' });
  expect((await ui.find({ key: 'problem' }))?.text).toContain('Cannot read this now');
  expect((await ui.find({ key: 'header' }))?.text).toContain('Stale: last read');
  expect((await ui.find({ key: 'ticket' }))?.text).toContain('ABC-1001'); // the last safe read stays
  await ui.unmount();
  clean(h);
});

test('unsupported surface: an explicit open that cannot be placed answers with the fallback and reads nothing', async ($, on) => {
  const h = host(on, { placed: false });
  const answer = await $.command.run({ command: 'cg-panel' });
  expect(answer.text).toContain('not shown here (no-surface)');
  expect(answer.text).toContain('/cg:dashboard');
  await h.clock.advance(60_000);
  expect(h.calls.reads).toEqual([]);
  clean(h);
});

test('MOD-05: the prompt band is off by default and passes the engine\'s band through unchanged', async ($, on) => {
  const h = host(on);
  const ui = await band($);
  expect(await ui.drawn()).toMatchObject({ type: 'Box', props: { key: 'engine-band' } });
  expect(await ui.find({ key: 'cg-band' })).toBeUndefined();
  await ui.unmount();
  clean(h);
});

test('MOD-05: the opt-in band is one cached line that composes with the band beneath and shortens when narrow', { options: { prompt_band: true } }, async ($, on) => {
  const h = host(on);
  const empty = await band($);
  expect((await empty.find({ key: 'cg-band' }))?.text).toBe('CG · Chitragupta · /cg-panel opens the companion');
  expect(await empty.find({ key: 'engine-band' })).toBeDefined();
  await empty.unmount();
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const reads = h.calls.reads.length;
  const wide = await band($, 160);
  expect((await wide.find({ key: 'cg-band' }))?.text).toBe('CG · ABC-1001 · next: Review saved plan · 2 attention items · as of 2026-11-09 10:42 UTC');
  await wide.unmount();
  const narrow = await band($, 50);
  expect((await narrow.find({ key: 'cg-band' }))?.text).toBe('CG · ABC-1001 · 2 items · /cg-panel');
  await narrow.unmount();
  const survey = await band($, 120, PLUGIN, { hasSurvey: true });
  expect(await survey.find({ key: 'cg-band' })).toBeUndefined();
  await survey.unmount();
  expect(h.calls.reads.length).toBe(reads);
  clean(h);
});

const OTHER = {
  name: 'other-mod',
  register: (on: any) => {
    on('session.start', async ($: any, e: any, next: any) => {
      await $.command.register({ name: 'other-pane', description: 'A synthetic second mod' });
      return next(e);
    });
    on('command.run', { command: 'other-open' }, async ($: any) => {
      await $.ui.open({ id: 'other-pane', title: 'Other' });
      return { text: 'opened' };
    });
    on('command.run', { command: 'other-close' }, async ($: any, e: any) => {
      await $.ui.close({ id: e.args || 'other-pane' });
      return { text: 'closed' };
    });
    on('ui.render', { component: 'Pane' }, ($: any, e: any, next: any) => {
      if (e.requestId !== 'other-pane') return next(e);
      const { Box, Text } = $.ui.resolve(e);
      return Box({ key: 'other-root', children: [Text({ children: ['Other mod pane'] })] });
    });
    on('ui.render', { component: 'AbovePrompt' }, ($: any, e: any) => {
      const { Box, Text } = $.ui.resolve(e);
      return Box({ key: 'other-band', children: [Text({ children: ['Other mod band'] })] });
    });
  },
};

test('A25/A41: a second mod\'s pane, band and closes pass through; a close of an owned ID stops only our polling', { plugins: [OTHER], options: { prompt_band: true } }, async ($, on) => {
  const h = host(on);
  await $.session.start(START);
  expect(h.calls.registered).toEqual(['cg-panel', 'cg-plan', 'cg-resume', 'other-pane']);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  expect((await $.command.run({ command: 'other-open' })).text).toBe('opened');
  const other = await $.ui.mount({ plugin: 'other-mod', component: 'Pane', requestId: 'other-pane', surface: 'terminal',
    viewport: { columns: 100, rows: 40 }, props: paneProps(100, 40, 'Other') });
  expect(await drawn(other)).toContain('Other mod pane');
  expect(await drawn(other)).not.toContain('Chitragupta');
  const ours = await mountPane($, 'cg-companion');
  expect((await ours.find({ key: 'ticket' }))?.text).toContain('ABC-1001');
  const b = await band($, 160);
  expect(await b.find({ key: 'cg-band' })).toBeDefined();
  expect(await b.find({ key: 'other-band' })).toBeDefined();
  await b.unmount();
  expect((await $.command.run({ command: 'other-close', args: 'other-pane' })).text).toBe('closed');
  expect(h.calls.closes.map(c => c.id)).toEqual(['other-pane']);
  await h.clock.advance(15_000);
  const reads = h.calls.reads.length;
  expect(reads).toBeGreaterThan(2); // our pane is still open and polled
  await $.command.run({ command: 'other-close', args: 'cg-companion' }); // a close event for our ID reaching our hook
  await h.clock.advance(60_000);
  expect(h.calls.reads.length).toBe(reads);
  expect(h.calls.beneath).toEqual([]);
  await other.unmount();
  await ours.unmount();
  clean(h);
});

test('A64: a profile change between review and Copy approved plan refuses the copy (no stale handoff)', async ($, on) => {
  const h = host(on);
  const review = h.state.data.review;
  h.state.data.review = (args: any, gen: string) => ({ ...review(args, gen), stage: 'approved' });
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const companion = await mountPane($, 'cg-companion');
  await companion.press({ key: 'open-review' });
  const ui = await mountPane($, 'cg-review');
  h.state.gen = 'g2'; // the profile/scope changed after the review was drawn
  await ui.press({ key: 'copy-plan' });
  expect(h.calls.copies).toEqual([]);
  expect((await ui.find({ key: 'local-notice' }))?.text).toContain('Could not read the prepared plan');
  await ui.unmount();
  await companion.unmount();
  clean(h);
});

test('A41: a temporary pane closes and reopens with its session-local context; only our own ID is closed', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const companion = await mountPane($, 'cg-companion');
  await companion.press({ key: 'open-review' });
  let ui = await mountPane($, 'cg-review');
  await ui.press({ key: 'execute' });
  expect(await ui.find({ key: 'execute-detail' })).toBeDefined();
  await ui.press({ key: 'close' });
  expect(h.calls.closes).toEqual([{ id: 'cg-review', origin: { kind: 'plugin' } }]);
  expect(h.open.has('cg-companion')).toBe(true);
  await ui.unmount();
  const reads = h.calls.reads.length;
  await companion.press({ key: 'open-review' });
  expect(h.ops().slice(reads)).toEqual(['snapshot', 'detail', 'review', 'jobs']); // one bounded read on reopen
  ui = await mountPane($, 'cg-review');
  expect(await ui.find({ key: 'execute-detail' })).toBeDefined(); // expanded state kept for this task
  expect(h.calls.opens.filter(o => o.id === 'cg-review')).toHaveLength(2);
  await ui.unmount();
  await companion.unmount();
  clean(h);
});

const SAME_NAME = {
  name: 'name-clash-mod',
  register: (on: any) => {
    on('session.start', async ($: any, e: any, next: any) => {
      await $.command.register({ name: 'cg-panel', description: 'A synthetic mod reusing the name' });
      return next(e);
    });
    on('command.run', { command: 'cg-panel' }, () => ({ text: 'the other mod answered' }));
  },
};

test('COMP-01: a second mod registering the same plain name does not take over /cg-panel in the kit\'s tier order', { plugins: [SAME_NAME] }, async ($, on) => {
  const h = host(on);
  await $.session.start(START);
  expect(h.calls.registered).toEqual(['cg-panel', 'cg-plan', 'cg-resume', 'cg-panel']);
  const answer = await $.command.run({ command: 'cg-panel' });
  expect(answer.text).toBeUndefined();
  expect(h.calls.opens.map(o => o.id)).toEqual(['cg-companion']);
  clean(h);
});

test('MOD-03: an ambiguous context shows a chooser with evidence and never picks for you; a suggestion is labelled', async ($, on) => {
  const h = host(on);
  const snap = h.state.data.snapshot;
  h.state.data.snapshot = (args: any, gen: string) => (args.workflow ? snap(args, gen) : { ...snap(args, gen), focus: null, workflow_context: null,
    context: { state: 'ambiguous', ticket: null, workflow: null, reason: 'Several tasks have observed execution activity; choose one',
      candidates: [{ workflow: W1, ticket: 'ABC-1001', stage: 'in_development', repository: 'shop-api', evidence: 'observed execution activity 10:40' },
        { workflow: W3, ticket: 'ABC-1003', stage: 'in_development', repository: 'shop-web', evidence: 'observed execution activity 10:41' }] },
    next_action: { label: 'Choose a task', text: 'Several tasks; choose one', kind: 'selection', evidence: [], ticket: null, workflow: null } });
  await $.command.run({ command: 'cg-panel' });
  expect(h.ops()).toEqual(['snapshot']); // nothing chosen, so no task detail is read
  const ui = await mountPane($, 'cg-companion');
  expect((await ui.find({ key: 'ticket' }))?.text).toBe('Several tasks match; choose one below');
  expect((await ui.find({ key: 'candidate-1' }))?.text).toContain('observed execution activity 10:41');
  await ui.press({ key: 'choose-1' });
  expect(h.calls.reads.filter(r => r.args.operation === 'snapshot').at(-1).args).toEqual({ operation: 'snapshot', workflow: W3 });
  h.state.data.snapshot = (args: any, gen: string) => ({ ...snap({ ...args, workflow: W1 }, gen),
    context: { state: 'evidenced', ticket: 'ABC-1001', workflow: W1, candidates: [], evidence: 'Observed execution activity associated by you (10:40)' } });
  await ui.press({ key: 'refresh' });
  expect((await ui.find({ key: 'context' }))?.text).toContain('Suggested, not chosen: Observed execution activity');
  await ui.press({ key: 'use-suggested' });
  expect(h.calls.reads.filter(r => r.args.operation === 'snapshot').at(-1).args).toEqual({ operation: 'snapshot', workflow: W1 });
  await ui.unmount();
  clean(h);
});
