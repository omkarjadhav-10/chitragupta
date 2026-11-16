// NFR-01: render is pure computation over the cache. This drives the render module directly (its own module
// instance, not the loaded plugin's) with a full cache and a stand-in element table, and measures it.
import { expect, test } from 'claude-code/testing';
import { MODULES } from '../hooks/panes.js';
import { band, pane } from '../hooks/render.js';
import { S, readKey } from '../hooks/session.js';
import { CURSOR, W1, backend, clean, graphRow, graphSummary, host, mountPane } from './host.ts';

const VERIFIED = 'Verified within recorded scope';

const element = (type: string) => (props: any) => ({ type, props, children: props.children });
const TABLE: any = Object.fromEntries(['Box', 'Text', 'Button', 'Input', 'Select', 'Link', 'Code', 'Markdown'].map(t => [t, element(t)]));
const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());

function fill() {
  const data = backend();
  S.generation = 'g1';
  S.snapshot = data.snapshot({ operation: 'snapshot', ticket: 'ABC-1001' }, 'g1');
  S.reads.set(readKey({ operation: 'detail', workflow: W1 }), data.detail({ operation: 'detail', workflow: W1 }, 'g1'));
  S.reads.set(readKey({ operation: 'planner' }), data.planner({ operation: 'planner' }, 'g1'));
  S.views.set('cg-companion', { id: 'cg-companion', kind: 'companion', open: true, placed: true, context: {} });
  S.views.set('cg-planner', { id: 'cg-planner', kind: 'planner', open: true, placed: true, context: {} });
}

test('render touches no host call: the act table is never invoked while drawing', () => {
  fill();
  const act = new Proxy({}, { get: () => () => () => { throw new Error('a handler ran during render'); } });
  const e: any = { surface: 'terminal', component: 'Pane', requestId: 'cg-companion', viewport: { columns: 120, rows: 40 },
    props: { bodyColumns: 116, scroll: { offset: 0, bodyRows: 13 } } };
  for (const tab of ['focus', 'evidence', 'attention']) {
    S.local.set('cg-companion|', { tab, expanded: { ledger: true, changes: true }, draft: '', cursor: null, group: null, pinned: [], menu: true, notice: 'x /cg:dashboard', packet: '', page: 0 });
    expect(pane(TABLE, e, MODULES.companion, act)).toMatchObject({ type: 'Box' });
  }
  expect(pane(TABLE, { ...e, requestId: 'cg-planner' }, MODULES.planner, act)).toMatchObject({ type: 'Box' });
  expect(band(TABLE, { props: { bodyColumns: 115 } } as any, null)).toMatchObject({ type: 'Box' });
});

test('NFR-01: p95 render computation stays far below 50 ms', () => {
  fill();
  const act = new Proxy({}, { get: () => () => () => undefined });
  const times: number[] = [];
  for (let i = 0; i < 300; i += 1) {
    const cols = [80, 100, 120, 160][i % 4];
    const e: any = { surface: 'terminal', component: 'Pane', requestId: 'cg-companion', viewport: { columns: cols, rows: 40 },
      props: { bodyColumns: cols - 4, scroll: { offset: 0, bodyRows: 13 } } };
    S.local.set('cg-companion|', { tab: ['focus', 'evidence', 'attention'][i % 3], expanded: {}, draft: '', cursor: null, group: null, pinned: [], menu: false, notice: '', packet: '', page: 0 });
    const t0 = now();
    pane(TABLE, e, MODULES.companion, act);
    times.push(now() - t0);
  }
  times.sort((a, b) => a - b);
  const p50 = times[Math.floor(times.length * 0.5)];
  const p95 = times[Math.floor(times.length * 0.95)];
  console.log('render p50 ' + p50.toFixed(3) + ' ms, p95 ' + p95.toFixed(3) + ' ms over ' + times.length + ' renders');
  expect(p95).toBeLessThan(50);
});

// ---------------------------------------------------------------- the Evidence tab's Requirement trace (one graph_summary read)

const strings = (node: any): string[] => {
  if (node === null || node === undefined || node === false) return [];
  if (typeof node === 'string') return [node];
  if (Array.isArray(node)) return node.flatMap(strings);
  return [...(node.props && typeof node.props.label === 'string' ? [node.props.label] : []), ...strings(node.children)];
};
const GRAPH = { operation: 'graph_summary', ticket: 'ABC-1001' };
const PANE = (cols: number): any => ({ surface: 'terminal', component: 'Pane', requestId: 'cg-companion', viewport: { columns: cols, rows: 40 },
  props: { bodyColumns: cols - 4, scroll: { offset: 0, bodyRows: 40 } } });
const LOCAL = (trace: number | null) => ({ tab: 'evidence', expanded: {}, draft: '', cursor: null, group: null, pinned: [],
  menu: false, notice: '', packet: '', page: 0, trace });
const summary = (over: Record<string, unknown> = {}, args: any = GRAPH) => graphSummary(args, 'g1', over);

/** The Evidence tab's lines at `cols`, drawn from the cache only; `made` records the handlers created (none run). */
function evidence(data: any, trace: number | null = null, cols = 80) {
  fill();
  S.override = {};
  if (data) S.reads.set(readKey(GRAPH), data);
  else S.reads.delete(readKey(GRAPH)); // not read yet
  S.local.set('cg-companion|', LOCAL(trace));
  const made: any[] = [];
  const act = new Proxy({}, { get: (_t, name) => (...args: any[]) => {
    made.push([name, args[1]]);
    return () => { throw new Error('a handler ran during render'); };
  } });
  const lines = strings(pane(TABLE, PANE(cols), MODULES.companion, act));
  return { lines, made, find: (start: string) => lines.find(l => l.startsWith(start)) };
}

test('Requirement trace: the critical requirement first, with ticket, three statuses, commit, PR, gap and the dashboard link', () => {
  const { lines, find } = evidence(summary());
  expect(find('Requirement trace')).toBe('Requirement trace · ABC-1001 · ac2 of 4');
  expect(lines).toContain('Orders over limit 2 are rejected with a clear message');
  expect(lines).toContain('Plan: Covered · Plan v3');
  expect(lines).toContain('Implementation: Linked · commit a12bc3d');
  expect(lines).toContain('Verification: Failed · passed; observed at 9f2e1c0');
  expect(lines).toContain('PR #214 open · CI running');
  expect(lines).toContain('Gap: Current-revision verification has not been established');
  expect(find('Open full evidence')).toBe('Open full evidence: /cg:dashboard then #/graph?ticket=ABC-1001&req=' + graphRow(2).id);
  for (const l of [find('Requirement trace'), find('Plan:'), find('Implementation:'), find('Verification:'), find('PR #')]) {
    expect((l as string).length).toBeLessThanOrEqual(76); // 80 columns less the pane frame; longer text wraps
  }
});

test('Requirement trace: Previous and Next only change the session selection; the first not verified is next in line', () => {
  const first = evidence(summary());
  expect(first.made.filter(m => m[0] === 'trace')).toEqual([['trace', 0], ['trace', 2]]);
  expect(first.lines).toContain('Previous');
  const last = evidence(summary(), 3);
  expect(last.find('Requirement trace')).toBe('Requirement trace · ABC-1001 · ac4 of 4');
  expect(last.lines).not.toContain('Next');
  expect(last.made.filter(m => m[0] === 'trace')).toEqual([['trace', 2]]);
  const rows = [graphRow(1, VERIFIED), graphRow(2), graphRow(3)];
  expect(evidence(summary({ rows, total: 3, critical: { total: 0, ids: [] } })).find('Requirement trace'))
    .toBe('Requirement trace · ABC-1001 · ac2 of 3');
  expect(evidence(summary(), 99).find('Requirement trace')).toBe('Requirement trace · ABC-1001 · ac2 of 4'); // out of range: the default
});

test('Requirement trace: all verified, no requirements, not built, rebuilding and more than 50 each say so', () => {
  const all = [1, 2, 3, 4].map(n => graphRow(n, VERIFIED));
  const v = evidence(summary({ rows: all, total: 4, critical: { total: 0, ids: [] } }));
  expect(v.lines).toContain('All 4 requirements verified within recorded scope');
  expect(v.find('Requirement trace')).toBe('Requirement trace · ABC-1001 · ac1 of 4');
  expect(v.find('Gap:')).toBeUndefined();
  expect(evidence(summary({ rows: [], total: 0, critical: { total: 0, ids: [] } })).lines)
    .toContain('No requirements read from Jira yet (source: complete)');
  const notBuilt = evidence(summary({ graph: { state: 'not_built' } }));
  expect(notBuilt.lines).toContain('Evidence graph not built yet');
  expect(notBuilt.find('Plan:')).toBeUndefined();
  expect(evidence(summary({ graph: { state: 'rebuilding' } })).lines).toContain('Evidence graph rebuilding for the current profile');
  const many = Array.from({ length: 50 }, (_, i) => graphRow(i + 1));
  expect(evidence(summary({ rows: many, total: 60, next_cursor: CURSOR })).lines).toContain('Showing 50 of 60; open full evidence');
  expect(evidence(null).lines).toContain('Reading the evidence graph…');
});

test('Requirement trace: privacy hides the wording and the plan name; every value is cleaned', () => {
  fill();
  S.override = { privacy: true };
  S.reads.set(readKey(GRAPH), summary({}, { ...GRAPH, privacy: true }));
  S.local.set('cg-companion|', LOCAL(null));
  const hidden = strings(pane(TABLE, PANE(80), MODULES.companion, new Proxy({}, { get: () => () => () => undefined })));
  S.override = {};
  expect(hidden).toContain('Requirement text hidden (privacy mode)');
  expect(hidden).toContain('Plan: Covered');
  expect(hidden.join('\n')).not.toContain('Orders over limit');
  const evil = graphRow(2, 'Failed\u001b[31m', {
    text: 'Reject \u001b[2J see https://evil.example/x and C:\\Users\\dev\\secret.txt', alias: 'ac2\u202e',
    compact: { plan: 'Plan v3 file:///etc/passwd', commit: 'a12bc3d\u0007', commits: 1,
      pr: { number: 214, state: 'open\nrm -rf', ci: 'running' }, run: 'passed', gap: 'Gap with javascript:alert(1) and dev@acme.test' } });
  const out = evidence(summary({ rows: [graphRow(1, VERIFIED), evil], total: 2 })).lines.join('\n');
  for (const bad of ['\u001b', '\u0007', '\u202e', 'https://evil', 'C:\\Users', 'dev@acme.test', 'open\nrm']) expect(out).not.toContain(bad);
});

test('Requirement trace in the pane: Previous and Next redraw from the cache with no read; time under 15 s reads nothing', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  expect(h.calls.reads.find(r => r.args.operation === 'graph_summary')?.args).toEqual(GRAPH);
  const reads = h.calls.reads.length;
  const ui = await mountPane($, 'cg-companion');
  await ui.press({ key: 'tab-evidence' });
  expect((await ui.find({ key: 'trace-head' }))?.text).toBe('Requirement trace · ABC-1001 · ac2 of 4');
  await ui.press({ key: 'trace-next' });
  expect((await ui.find({ key: 'trace-head' }))?.text).toBe('Requirement trace · ABC-1001 · ac3 of 4');
  await ui.press({ key: 'trace-prev' });
  await ui.press({ key: 'trace-prev' });
  expect((await ui.find({ key: 'trace-head' }))?.text).toBe('Requirement trace · ABC-1001 · ac1 of 4');
  await h.clock.advance(10_000);
  expect(h.calls.reads.length).toBe(reads);
  await ui.unmount();
  clean(h);
});
