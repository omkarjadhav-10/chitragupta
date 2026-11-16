// Lifecycle and refresh discipline of the persistent companion (MOD-01, MOD-02A, NFR-01; A02, A06, A27, A41, A53, A78).
import { expect, test } from 'claude-code/testing';
import { CURSOR, PLUGIN, START, W1, clean, drawn, host, mountPane } from './host.ts';

test('A53: no pane opens at startup; only the three plain commands are registered', async ($, on) => {
  const h = host(on);
  await $.session.start(START);
  await h.clock.advance(60_000);
  expect(h.calls.opens).toEqual([]);
  expect(h.calls.reads).toEqual([]);
  expect(h.calls.registered).toEqual(['cg-panel', 'cg-plan', 'cg-resume']);
  clean(h);
});

test('A01: /cg-panel opens the companion, reads cached state once and draws ticket, next action and details route', async ($, on) => {
  const h = host(on);
  await $.session.start(START);
  const answer = await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  expect(answer.text).toBeUndefined();
  expect(h.calls.opens).toHaveLength(1);
  expect(h.calls.opens[0]).toMatchObject({ id: 'cg-companion', title: 'Chitragupta', focus: true });
  expect(h.ops()).toEqual(['snapshot', 'detail', 'graph_summary']);
  expect(h.calls.reads[0].args).toEqual({ operation: 'snapshot', ticket: 'ABC-1001' });
  const ui = await mountPane($, 'cg-companion');
  expect((await ui.find({ key: 'ticket' }))?.text).toBe('ABC-1001 · Correct discount calculation');
  expect((await ui.find({ key: 'next-action' }))?.text).toContain('Review saved plan');
  expect(await ui.find({ key: 'details-route' })).toBeDefined();
  expect((await ui.find({ key: 'resume' }))?.text).toContain('Pause (Task pause only; capture keeps running)');
  expect((await ui.find({ key: 'header' }))?.text).toContain('CG · Chitragupta');
  await ui.unmount();
  clean(h);
});

test('render purity: tab switches, redraws and resizes make zero bridge calls', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const before = h.calls.reads.length;
  const ui = await mountPane($, 'cg-companion');
  for (let i = 0; i < 5; i += 1) {
    await ui.press({ key: 'tab-evidence' });
    await ui.press({ key: 'tab-attention' });
    await ui.press({ key: 'tab-focus' });
    await ui.press({ key: 'settings' });
    await ui.press({ key: 'settings' });
  }
  for (const cols of [80, 100, 120, 160, 60, 200]) {
    await ui.redraw({ title: 'Chitragupta', isFocused: false, bodyColumns: cols - 4, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} });
  }
  expect(h.calls.reads.length).toBe(before);
  expect(h.calls.connect).toBe(before);
  await ui.unmount();
  clean(h);
});

test('A53: ten reopenings keep one view and the typed draft, with no extra reads', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel' });
  const reads = h.calls.reads.length;
  const ui = await mountPane($, 'cg-companion');
  await ui.input({ key: 'choose-ticket', text: 'abc-10', kind: 'change' });
  for (let i = 0; i < 10; i += 1) await $.command.run({ command: 'cg-panel' });
  expect(h.calls.opens).toHaveLength(11);
  expect(new Set(h.calls.opens.map(o => o.id))).toEqual(new Set(['cg-companion']));
  expect(h.open.size).toBe(1);
  expect(h.calls.reads.length).toBe(reads);
  expect((await ui.find({ key: 'choose-ticket' }))?.props.value).toBe('abc-10');
  await ui.unmount();
  clean(h);
});

test('NFR-01: periodic reads wait 15 s, an explicit read does not, and closing the last pane stops polling', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel' });
  expect(h.ops()).toEqual(['snapshot']);
  await h.clock.advance(14_000);
  expect(h.ops()).toEqual(['snapshot']);
  await h.clock.advance(1_000);
  expect(h.ops()).toEqual(['snapshot', 'snapshot']);
  const ui = await mountPane($, 'cg-companion');
  await h.clock.advance(5_000);
  await ui.press({ key: 'refresh' });
  expect(h.ops()).toHaveLength(3);
  await h.clock.advance(10_000); // the 30 s tick lands 10 s after the explicit read: skipped
  expect(h.ops()).toHaveLength(3);
  await h.clock.advance(15_000);
  expect(h.ops()).toHaveLength(4);
  await ui.press({ key: 'close' });
  expect(h.calls.closes).toEqual([{ id: 'cg-companion', origin: { kind: 'plugin' } }]);
  const reads = h.calls.reads.length;
  const panes = h.calls.panes;
  await h.clock.advance(120_000);
  expect(h.calls.reads.length).toBe(reads);
  expect(h.calls.panes).toBe(panes); // the timer itself is gone
  expect(h.calls.opens).toHaveLength(1); // periodic reads never reopen or refocus a pane
  await ui.unmount();
  clean(h);
});

test('A27: a close the engine records (person or unload) stops polling at the next tick; reopening reads once', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel' });
  h.open.delete('cg-companion'); // the person closed it: the engine's record no longer lists it
  await h.clock.advance(15_000); // the tick reconciles with $.ui.panes() and stops
  const reads = h.calls.reads.length;
  await h.clock.advance(90_000);
  expect(h.calls.reads.length).toBe(reads);
  await $.command.run({ command: 'cg-panel' });
  expect(h.calls.reads.length).toBe(reads + 1);
  clean(h);
});

test('A06: an out-of-order reply from an older request never replaces newer content', async ($, on) => {
  const h = host(on);
  h.state.late.snapshot = [5_000];
  const first = $.command.run({ command: 'cg-panel', args: 'ABC-1001' }); // its snapshot reply is held back
  await h.clock.settle();
  const ui = await mountPane($, 'cg-companion');
  const snap = h.state.data.snapshot;
  h.state.data.snapshot = (args: any, gen: string) => {
    const s = snap(args, gen);
    return { ...s, focus: { ...s.focus, title: 'Newer title' } };
  };
  await ui.press({ key: 'refresh' });
  expect((await ui.find({ key: 'ticket' }))?.text).toBe('ABC-1001 · Newer title');
  await h.clock.advance(5_000); // the held reply (older request, older title) arrives now
  await first;
  expect((await ui.find({ key: 'ticket' }))?.text).toBe('ABC-1001 · Newer title');
  expect(await drawn(ui)).not.toContain('Correct discount calculation');
  await ui.unmount();
  clean(h);
});

test('A06/UX-03: a profile generation change revokes old details and drafts before reconciling', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  const ui = await mountPane($, 'cg-companion');
  await ui.input({ key: 'choose-ticket', text: 'ABC-77', kind: 'change' });
  expect(await drawn(ui)).toContain('Correct discount calculation');
  h.state.gen = 'g2';
  await ui.press({ key: 'refresh' });
  const tree = await drawn(ui);
  expect(tree).not.toContain('Correct discount calculation');
  expect(tree).toContain('Profile or scope changed; earlier details were removed.');
  expect((await ui.find({ key: 'choose-ticket' }))?.props.value).toBe('');
  expect((await ui.find({ key: 'ticket' }))?.text).toBe('No task selected');
  await ui.unmount();
  clean(h);
});

test('a detail reply from another generation is dropped and the view re-read once', async ($, on) => {
  const h = host(on);
  const detail = h.state.data.detail;
  let calls = 0;
  h.state.data.detail = (args: any, gen: string) => (calls++ === 0 ? detail(args, 'g0') : detail(args, gen));
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  expect(h.ops()).toEqual(['snapshot', 'detail', 'snapshot', 'detail', 'graph_summary']); // the dropped cycle stops before the graph read
  const ui = await mountPane($, 'cg-companion');
  expect((await ui.find({ key: 'resume' }))?.text).toContain('Plan saved; review interrupted');
  await ui.unmount();
  clean(h);
});

test('a stale cursor ("reload") starts the planner again without it', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-plan' });
  const ui = await mountPane($, 'cg-planner');
  await ui.press({ key: 'more' });
  expect(h.calls.reads.at(-1).args).toMatchObject({ operation: 'planner', cursor: CURSOR });
  h.state.errors.planner = ['stale or invalid cursor; reload the view'];
  await h.clock.advance(15_000);
  const last = h.calls.reads.filter(r => r.args.operation === 'planner').at(-1).args;
  expect(last.cursor).toBeUndefined();
  expect(await drawn(ui)).toContain('The list changed; showing the first page again.');
  await ui.unmount();
  clean(h);
});

test('A02: a headless session answers commands with a text fallback and never opens, polls or draws', async ($, on) => {
  const h = host(on);
  await $.session.start({ cwd: 'C:/work/shop', surface: null, isInteractive: false });
  const answer = await $.command.run({ command: 'cg-panel' });
  expect(answer.text).toContain('/cg:dashboard');
  await h.clock.advance(120_000);
  expect(h.calls.opens).toEqual([]);
  expect(h.calls.reads).toEqual([]);
  clean(h);
});

test('MOD-01: an internal Chitragupta session registers nothing and stays silent', async ($, on) => {
  const h = host(on, { env: { CHITRAGUPTA_INTERNAL: '1' } });
  await $.session.start(START);
  expect(h.calls.registered).toEqual([]);
  expect(await $.command.run({ command: 'cg-panel' })).toEqual({});
  expect(h.calls.opens).toEqual([]);
  expect(h.calls.reads).toEqual([]);
  clean(h);
});

test('A78: /cg-resume and /cg-plan never reach the global capture commands, a prompt or a model', async ($, on) => {
  const h = host(on);
  await $.session.start(START);
  await $.command.run({ command: 'cg-resume', args: '' });
  await $.command.run({ command: 'cg-resume', args: 'ABC-1001' });
  await $.command.run({ command: 'cg-plan', args: 'abc-1002' });
  expect(h.calls.beneath).toEqual([]);
  expect(h.calls.opens.map(o => o.id)).toEqual(['cg-planner', 'cg-companion', 'cg-planner']);
  const plan = h.calls.reads.filter(r => r.args.operation === 'planner').at(-1).args;
  expect(plan.pinned).toEqual(['ABC-1002']);
  expect((await $.command.run({ command: 'cg-resume', args: 'rm -rf /' })).text).toContain('/cg-resume [TICKET]');
  clean(h);
});

test('session end: /clear resets the task selection; exit stops everything', async ($, on) => {
  const h = host(on);
  await $.session.start(START);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  await $.session.end({ reason: 'clear', sessionId: 's1', resume: {} as any });
  await h.clock.settle();
  expect(h.calls.reads.at(-1).args).toEqual({ operation: 'snapshot' });
  await $.session.end({ reason: 'prompt_input_exit', sessionId: 's1', resume: {} as any });
  const reads = h.calls.reads.length;
  await h.clock.advance(60_000);
  expect(h.calls.reads.length).toBe(reads);
  clean(h);
});

test('auto-open applies to the companion only, without focus, when the person enabled it', { options: { auto_open: true } }, async ($, on) => {
  const h = host(on);
  await $.session.start(START);
  await h.clock.settle();
  expect(h.calls.opens).toEqual([{ id: 'cg-companion', title: 'Chitragupta' }]);
  expect(h.ops()).toEqual(['snapshot']);
  clean(h);
});

test('a pane left open across a hot reload is reconciled from $.ui.panes() and read once', async ($, on) => {
  const h = host(on);
  h.open.add('cg-planner');
  await $.session.start(START);
  await h.clock.settle();
  expect(h.ops()).toEqual(['snapshot', 'planner']);
  const ui = await mountPane($, 'cg-planner');
  expect(await drawn(ui)).toContain('ABC-1002');
  await ui.unmount();
  clean(h);
});

test('W1 is a valid synthetic workflow id', () => {
  expect(W1).toMatch(/^w_[a-f0-9]{64}$/);
  expect(PLUGIN).toBe('cg-companion');
});

test('A25: an unrelated pane is drawn by its own hooks with zero IPC from this mod', async ($, on) => {
  const h = host(on);
  on('ui.render', { component: 'Pane' }, ($: any, e: any) => ({ type: 'Text', props: {}, children: ['Other mod: ' + e.requestId] }));
  await $.command.run({ command: 'cg-panel' });
  const reads = h.calls.reads.length;
  const ui = await $.ui.mount({ plugin: 'other', component: 'Pane', requestId: 'other-pane', surface: 'terminal',
    viewport: { columns: 80, rows: 24 }, props: { title: 'Other', isFocused: true, bodyColumns: 76, placement: 'inline',
      scroll: { offset: 0, bodyRows: 8 }, view: {} } });
  expect(await ui.find({ type: 'Text', text: 'Other mod: other-pane' })).toBeDefined();
  expect(h.calls.reads.length).toBe(reads);
  await ui.unmount();
  clean(h);
});
