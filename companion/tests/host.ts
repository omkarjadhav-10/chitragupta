// Shared synthetic host for the companion's kit tests: a fake read-only backend behind the plugin's own MCP
// server, the pane/clipboard host ops, and tripwires for everything the mod must never do. Synthetic data only.
import { expect, mock } from 'claude-code/testing';

export const PLUGIN = 'cg-companion';
export const SERVER = 'plugin:cg-companion:companion';
export const W1 = 'w_' + '1'.repeat(64);
export const W2 = 'w_' + '2'.repeat(64);
export const W3 = 'w_' + '3'.repeat(64);
export const P1 = 'p_' + 'a'.repeat(64);
export const FLOW = 'f_abcdef12';
export const LOCATION = 'C:\\Users\\dev\\src\\shop-api';
export const CURSOR = 'eyJwYWdlIjoxfQ.' + '0123456789abcdef'.repeat(2);
export const OPS = ['snapshot', 'planner', 'detail', 'review', 'plan', 'changes', 'verification', 'jobs', 'handoff', 'database'];
const AT = '2026-11-09T10:42:00Z';

export function envelope(operation: string, gen: string, extra: Record<string, unknown> = {}) {
  return { schema_version: 1, operation, profile_id: 'pr_0001', profile_generation: gen, scope_generation: 'sc1',
    snapshot_id: 's_0001', generated_at: AT, timezone: 'UTC', privacy: false, ...extra };
}

const TITLE = 'Correct discount calculation';
const masked = (args: any, title: string) => (args.privacy ? 'Title hidden (privacy mode)' : title);

function dbContext(args: any) {
  if (!args.flow && !args.investigation) return { available: false, reason: 'No database flow or investigation selected' };
  return { available: true, schema_version: 1, flow_id: args.flow || null, investigation_id: args.investigation || null,
    operation_label: 'Cancel order', environment: 'QA', last_evidenced_step: 'Cancellation recorded in Orders DB',
    largest_gap: 'Refund not observed in this query scope', next_action: 'Inspect consumer evidence',
    coverage: 'partial', freshness: 'stale', updated_at: AT,
    observations: [{ step: 'Order cancelled', status: 'observed', status_label: 'Observed at 10:42', observed_at: AT },
      { step: 'Refund issued', status: 'not_checked', status_label: 'Not checked', observed_at: null }],
    links: { flow: '#/db?flow=' + args.flow, evidence: '#/db?flow=' + args.flow + '&tab=evidence',
      journey: '#/db?flow=' + args.flow + '&tab=journey' } };
}

const CARDS = [
  { id: 'card:ABC-1001', ticket: 'ABC-1001', title: TITLE, group: 'review_incomplete', group_label: 'Review incomplete',
    stage: 'review_incomplete', workflow: W1, attempts: [{ workflow: W1, stage: 'review_incomplete', repository: 'shop-api' }],
    action: 'Resume Task', why: 'Resumable: review incomplete · Priority High' },
  { id: 'card:ABC-1002', ticket: 'ABC-1002', title: 'Add refund webhook', group: 'new', group_label: 'New', stage: 'new',
    workflow: null, attempts: [], action: 'Generate Plan', why: 'Assigned or included in your profile' },
  { id: 'card:ABC-1003', ticket: 'ABC-1003', title: 'Split invoice export', group: 'paused', group_label: 'Paused',
    stage: 'paused', workflow: W2, attempts: [{ workflow: W2, stage: 'paused', repository: 'shop-api' },
      { workflow: W3, stage: 'in_development', repository: 'shop-web' }], action: 'Resume Task', why: 'Resumable: paused' },
];

/** The default synthetic projections, by operation, shaped as stage 3a documents them. */
export function backend(): Record<string, (args: any, gen: string) => any> {
  return {
    snapshot: (args, gen) => {
      const chosen = args.workflow === W1 || args.ticket === 'ABC-1001';
      return envelope('snapshot', gen, {
        state: 'ready', empty: null, privacy: Boolean(args.privacy),
        context: chosen ? { state: 'explicit', ticket: 'ABC-1001', workflow: W1, candidates: [] }
          : { state: 'absent', reason: 'No task selected', candidates: [] },
        focus: chosen ? { ticket: 'ABC-1001', title: masked(args, TITLE), repository: 'shop-api',
          facts: ['Release: November update', 'Stage: Review incomplete', '2 change(s) since your last review'],
          jira_status: 'In Progress', links: { ticket: '#/tickets/ABC-1001' } } : null,
        next_action: chosen
          ? { label: 'Review saved plan', text: 'The plan is saved; its review was interrupted', kind: 'workflow',
            evidence: ['checkpoint:r3'], ticket: 'ABC-1001', workflow: W1 }
          : { label: 'Resume Task ABC-1001', text: 'Resumable: review incomplete', kind: 'planner',
            evidence: ['card:ABC-1001'], ticket: 'ABC-1001', workflow: W1 },
        workflow_context: chosen ? { workflow: W1, revision: 3, stage: 'review_incomplete', situation: 'A',
          situation_label: 'Plan saved; review interrupted', unread: 2,
          recovery: [{ state: 'stale', label: 'Review changed requirements', blocking: false }], blocking: false } : null,
        attention_summary: { total: 2, more: false, left_out: 1, coverage: 'partial',
          semantics: 'Counts of recorded alerts; unread or unavailable sources may hide more',
          items: [{ id: 'release:ABC-1001:1', kind: 'release', ticket: 'ABC-1001', label: 'Resolve release mapping',
            text: 'No release branch is mapped for shop-api', source: 'release tracking' },
          { id: 'alert:ABC-1002:1', kind: 'branch', ticket: 'ABC-1002', label: 'Review branch alert',
            text: 'feature/abc-1002 is behind qa; owner dev@acme.test', source: 'branch tracking' }] },
        sources: [
          { id: 'git', label: 'Git commits', state: 'Connected', observed_at: AT, freshness: 'current', recovery: '' },
          { id: 'jira', label: 'Jira', state: 'Stale', observed_at: AT, freshness: 'stale', recovery: 'Open Health to see why the background run is behind' },
          { id: 'jev', label: 'Jev evaluation', state: 'Unavailable', observed_at: null, freshness: 'unknown', recovery: 'Jev evaluation is unavailable' }],
        capabilities: { mutations: { available: false, reason: 'Read-only projection' } },
        database_context: dbContext(args),
        items: CARDS.slice(0, 2), more: false, cursor: null,
      });
    },
    planner: (args, gen) => {
      const pool = (args.group && args.group !== 'all' ? CARDS.filter(c => c.group === args.group) : CARDS)
        .map(c => ({ ...c, title: masked(args, c.title) }));
      const first = !args.cursor;
      return envelope('planner', gen, { state: 'ready', empty: null, privacy: Boolean(args.privacy), group: args.group || 'all',
        groups: [{ id: 'new', label: 'New', count: 1 }, { id: 'review_incomplete', label: 'Review incomplete', count: 1 },
          { id: 'paused', label: 'Paused', count: 1 }, { id: 'completed', label: 'Completed', count: 1 }],
        completed: { collapsed: true, count: 1 }, total: pool.length,
        items: first ? pool.slice(0, 2) : pool.slice(2), more: first && pool.length > 2, cursor: first && pool.length > 2 ? CURSOR : null });
    },
    detail: (args, gen) => envelope('detail', gen, { privacy: Boolean(args.privacy), workflow: args.workflow, ticket: 'ABC-1001',
      title: masked(args, TITLE), repository: 'shop-api', stage: 'review_incomplete', situation: 'A',
      situation_label: 'Plan saved; review interrupted', next_action: { label: 'Review saved plan', text: 'Review it' },
      actions: [{ name: 'Pause', available: true, reason: 'Task pause only; capture keeps running' },
        { name: 'Verify Requirements', available: true, reason: '' }, { name: 'Complete', available: false, reason: 'Gated' }],
      measures: { plan_coverage: { state: 'unavailable', label: 'Evaluation unavailable', percent: null },
        implementation_progress: { numerator: 2, denominator: 3, percent: 67 },
        functional_verification: { numerator: 1, denominator: 4, percent: 25, gaps: [] } },
      changes: { total: 2, first_use: false, label: null, items: [
        { id: 'commit:1', label: 'Commit abc1234 in shop-api', observed_at: AT },
        { id: 'test:1', label: 'Tests passed in shop-api', observed_at: AT }] },
      verification: { rows: [
        { criterion: 'AC-1', status: 'Verified', applicability: 'Verified by current applicable test evidence' },
        { criterion: 'AC-2', status: 'Missing', applicability: 'Missing: no current applicable evidence' },
        { criterion: 'AC-3', status: 'Unverified', applicability: 'Unverified: no current applicable evidence' },
        { criterion: 'AC-4', status: 'Partial', applicability: 'Partial: earlier evidence no longer applies' }] },
      delivery: { release: 'November update', reminders: [{ kind: 'mapping', label: 'Resolve release mapping', text: 'Map shop-api' }],
        guidance: 'Open the ticket\'s delivery details for repository-specific base, PR target, QA and UAT' },
      prs: [{ id: 'pr:shop-api:41', outcome: 'open', review: 'changes_requested', head: 'abc1234def567',
        branch: 'feature/abc-1001-discount', label: 'Fix discount rounding' }],
      decisions: [{ text: 'Round half up, per the finance rule' }],
      continuation: { requested_changes: ['pr:shop-api:41'], head_gaps: [], ci: 'CI checks unavailable',
        scope: 'Resume Execution only for explicitly selected requested work' },
      context: { branch: 'feature/abc-1001-discount', reason: '' },
      links: { ticket: '#/tickets/ABC-1001' }, database_context: dbContext(args) }),
    review: (args, gen) => envelope('review', gen, { workflow: args.workflow, ticket: 'ABC-1001', stage: 'review_incomplete',
      selected_plan: P1, approved_plan: null, versions: [{ id: P1, source: 'claude', selected: true, number: 1 }],
      evaluation: { state: 'unavailable', label: 'Evaluation unavailable', percent: null,
        reason: 'Jev evaluation is unavailable under the current safety policy' },
      assessment: null, assessment_label: 'Developer assessment (yours; never a Jev score)',
      choices: [
        { action: 'Improve Plan', available: true, reason: '', recommended: true, why: '2 criteria Missing, Partial or Unverified' },
        { action: 'Execute Plan', available: true, reason: '', recommended: false,
          why: 'Advisory coverage is never required to execute; acknowledge remaining risks' }],
      execute: { plan: P1, digest: 'd'.repeat(64), baseline: 'b_0001', baseline_completeness: 'partial',
        target: { repository: 'shop-api', location: LOCATION, session: 'A separate ordinary Claude Code turn you start (copy fallback)' },
        findings: { evaluation: 'Evaluation unavailable', open_criteria: ['AC-2', 'AC-3'] }, risks: ['Rounding edge cases'],
        acknowledgement: 'I reviewed this exact version and accept the unresolved risks, the unavailable Jev evaluation and the unknown working-tree state.',
        outcome: 'Prepared; not submitted' },
      candidates: [], drafts: [], criteria: [] }),
    jobs: (args, gen) => envelope('jobs', gen, { workflow: args.workflow, active: [], jobs: [], poll_after_s: 15 }),
    handoff: (args, gen) => envelope('handoff', gen, { workflow: args.workflow, state: 'prepared_not_submitted',
      label: 'Prepared; not submitted', text: 'APPROVED PLAN p_aaaa\n1. Fix rounding\n2. Add tests', cursor: null, plan: P1 }),
    database: (args, gen) => envelope('database', gen, { database_context: dbContext(args) }),
  };
}

type Options = { env?: Record<string, string>; copy?: boolean; placed?: boolean };

/** Hooks beneath the plugin: the host's answers plus counters and tripwires. Call before the first `$` call. */
export function host(on: any, opts: Options = {}) {
  const calls = { connect: 0, reads: [] as any[], opens: [] as any[], closes: [] as any[], copies: [] as string[],
    panes: 0, registered: [] as string[], beneath: [] as string[], prompts: 0, models: 0, processes: 0, fetches: 0 };
  const open = new Set<string>();
  const state = { gen: 'g1', data: backend(), late: {} as Record<string, number[]>, errors: {} as Record<string, string[]> };
  const clock = mock.clock(on, { now: 1_000_000 });
  mock.env(on, opts.env ?? {});
  on('session.start', ($: any, e: any) => ({ cwd: e.cwd }));
  on('session.end', ($: any, e: any) => ({ sessionId: e.sessionId }));
  on('command.register', ($: any, e: any) => {
    calls.registered.push(e.name);
    return { value: { command: e.name } };
  });
  on('mcp.connect', () => {
    calls.connect += 1;
    return { value: { isConnected: true, server: SERVER } };
  });
  on('mcp.call', async ($: any, e: any) => {
    calls.reads.push({ server: e.server, tool: e.tool, args: e.args });
    const op = e.args && e.args.operation;
    const failure = (state.errors[op] || []).shift();
    const out = failure ? null : (state.data[op] ? state.data[op](e.args, state.gen) : envelope(op, state.gen));
    const delay = (state.late[op] || []).shift();
    if (delay) await clock.sleep(delay);
    if (failure) return { value: { content: [{ type: 'text', text: failure }], isError: true } };
    return { value: { content: [{ type: 'text', text: typeof out === 'string' ? out : JSON.stringify(out) }], isError: false } };
  });
  on('ui.open', ($: any, e: any) => {
    calls.opens.push({ ...e });
    open.add(e.id);
    return { value: opts.placed === false ? { isPlaced: false, reason: 'no-surface' } : { isPlaced: true } };
  });
  on('ui.close', ($: any, e: any) => {
    calls.closes.push({ ...e });
    open.delete(e.id);
    return { value: undefined };
  });
  on('ui.panes', () => {
    calls.panes += 1;
    return { value: [...open].map(id => ({ id, title: id, isShown: true, isFocused: false, isPlaced: opts.placed !== false })) };
  });
  on('ui.copy', ($: any, e: any) => {
    calls.copies.push(e.text);
    return { value: opts.copy === false ? { isCopied: false, reason: 'no-clipboard' } : { isCopied: true } };
  });
  on('ui.render', { component: 'AbovePrompt' }, ($: any, e: any) => {
    const { Box } = $.ui.resolve(e);
    return Box({ key: 'engine-band', children: [] });
  });
  // Tripwires: none of these may ever be reached from the mod.
  on('command.run', ($: any, e: any) => {
    calls.beneath.push(e.command);
    return { text: 'reached beneath the mod' };
  });
  on('prompt.submit', () => {
    calls.prompts += 1;
    return { drop: 'tripwire' };
  });
  on('model.complete', () => {
    calls.models += 1;
    return { value: { isAnswered: false, reason: 'aborted' } };
  });
  on('process.run', () => {
    calls.processes += 1;
    return { deny: 'tripwire' };
  });
  on('http.fetch', () => {
    calls.fetches += 1;
    return { deny: 'tripwire' };
  });
  return { calls, clock, open, state, ops: () => calls.reads.map(r => r.args.operation) };
}

export const START = { cwd: 'C:/work/shop', surface: 'terminal' as const, isInteractive: true };

export function paneProps(cols = 80, rows = 24, title = 'Chitragupta') {
  return { title, isFocused: true, bodyColumns: Math.max(20, cols - 4), placement: 'inline' as const,
    scroll: { offset: 0, bodyRows: Math.max(4, Math.floor(rows / 3)) }, view: {} };
}

export function mountPane($: any, id: string, surface: any = 'terminal', cols = 80, rows = 24) {
  return $.ui.mount({ plugin: PLUGIN, component: 'Pane', requestId: id, surface, viewport: { columns: cols, rows },
    props: paneProps(cols, rows) });
}

export async function drawn(ui: any) {
  return JSON.stringify(await ui.drawn());
}

/** The invariants every test ends on: only allowlisted reads of the plugin's own server, nothing else. */
export function clean(h: ReturnType<typeof host>) {
  for (const r of h.calls.reads) {
    expect(r.server).toBe(SERVER);
    expect(r.tool).toBe('companion_read');
    expect(OPS).toContain(r.args.operation);
  }
  expect(h.calls.prompts).toBe(0);
  expect(h.calls.models).toBe(0);
  expect(h.calls.processes).toBe(0);
  expect(h.calls.fetches).toBe(0);
  expect(h.calls.beneath).toEqual([]);
}
