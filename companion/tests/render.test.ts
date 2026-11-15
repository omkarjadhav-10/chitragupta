// NFR-01: render is pure computation over the cache. This drives the render module directly (its own module
// instance, not the loaded plugin's) with a full cache and a stand-in element table, and measures it.
import { expect, test } from 'claude-code/testing';
import { MODULES } from '../hooks/panes.js';
import { band, pane } from '../hooks/render.js';
import { S, readKey } from '../hooks/session.js';
import { W1, backend } from './host.ts';

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
