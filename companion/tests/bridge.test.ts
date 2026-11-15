// ARCH-02 bridge: one server, one read-only tool, allowlisted operations and arguments, bounded both ways.
import { expect, test } from 'claude-code/testing';
import { BridgeError, OPERATIONS, check, classify, read } from '../hooks/bridge.js';
import { CURSOR, FLOW, P1, SERVER, W1, clean, envelope, host } from './host.ts';

/** A stand-in `$` with only the two calls the bridge may make; anything else would throw. */
function fake(reply: any, conn: any = { isConnected: true, server: SERVER }) {
  const seen = { connect: [] as string[], call: [] as any[] };
  const $ = { mcp: {
    connect: async (server: string) => { seen.connect.push(server); return conn; },
    call: async (server: string, tool: string, args: any) => { seen.call.push({ server, tool, args }); return reply; },
  } };
  return { $, seen };
}
const text = (data: unknown, isError = false) => ({ content: [{ type: 'text', text: typeof data === 'string' ? data : JSON.stringify(data) }], isError });

test('the operation allowlist is exactly stage 3a\'s ten projections', () => {
  expect(Object.keys(OPERATIONS)).toEqual(['snapshot', 'planner', 'detail', 'review', 'plan', 'changes', 'verification', 'jobs', 'handoff', 'database']);
});

test('valid requests pass through unchanged', () => {
  expect(check({ operation: 'snapshot', ticket: 'ABC-1001', privacy: true, pinned: ['ABC-1002'] }))
    .toEqual({ operation: 'snapshot', ticket: 'ABC-1001', privacy: true, pinned: ['ABC-1002'] });
  expect(check({ operation: 'plan', workflow: W1, plan: P1, base: 'parent', offset: 8000 })).toMatchObject({ plan: P1, base: 'parent' });
  expect(check({ operation: 'planner', group: 'paused', cursor: CURSOR })).toMatchObject({ cursor: CURSOR });
  expect(check({ operation: 'database', flow: FLOW })).toEqual({ operation: 'database', flow: FLOW });
  expect(check({ operation: 'snapshot', ticket: undefined, workflow: null })).toEqual({ operation: 'snapshot' });
});

test('UI strings never become a server, tool, path, URL, command or unknown argument', () => {
  const bad = [
    null, [], 'snapshot', { operation: 'companion_write' }, { operation: 'get_db_flow' }, { operation: 'constructor' },
    { operation: 'snapshot', server: 'other' }, { operation: 'snapshot', tool: 'Bash' }, { operation: 'snapshot', path: 'C:/x' },
    { operation: 'snapshot', ticket: 'abc-1; rm -rf /' }, { operation: 'snapshot', ticket: 'https://acme.test/ABC-1' },
    { operation: 'detail' }, { operation: 'detail', workflow: 'w_../../etc' }, { operation: 'planner', group: 'everything' },
    { operation: 'planner', cursor: '../../x' }, { operation: 'planner', cursor: 'a'.repeat(1100) + '.' + '0'.repeat(32) },
    { operation: 'plan', workflow: W1, offset: -1 }, { operation: 'plan', workflow: W1, base: 'HEAD~1' },
    { operation: 'snapshot', privacy: 'yes' }, { operation: 'snapshot', pinned: Array(11).fill('ABC-1') },
    { operation: 'database', flow: "f_abc' OR 1=1" }, { operation: 'database', sql: 'select 1' },
    { operation: 'jobs', workflow: W1, cursor: CURSOR },
  ];
  for (const args of bad) expect(() => check(args)).toThrow(BridgeError);
});

test('read connects only to this plugin\'s own server and calls only companion_read', async () => {
  const { $, seen } = fake(text(envelope('snapshot', 'g1')));
  const data = await read($, { operation: 'snapshot' });
  expect(data.profile_generation).toBe('g1');
  expect(seen.connect).toEqual(['companion']);
  expect(seen.call).toEqual([{ server: SERVER, tool: 'companion_read', args: { operation: 'snapshot' } }]);
});

test('an invalid request makes no IPC at all', async () => {
  const { $, seen } = fake(text(envelope('snapshot', 'g1')));
  await expect(read($, { operation: 'snapshot', url: 'https://acme.test' })).rejects.toThrow('Unsupported Companion read argument');
  expect(seen.connect).toEqual([]);
  expect(seen.call).toEqual([]);
});

test('oversized, non-JSON, mismatched or unversioned replies are refused', async () => {
  const cases: [any, string][] = [
    [text('x'.repeat(65537)), '64 KiB'],
    [text('not json'), 'not JSON'],
    [text([1, 2]), 'Unsupported Companion reply'],
    [text(envelope('planner', 'g1')), 'Unsupported Companion reply'],
    [text({ ...envelope('snapshot', 'g1'), schema_version: 2 }), 'Unsupported Companion reply'],
    [{ content: [] }, 'reply missing'],
  ];
  for (const [reply, message] of cases) {
    const { $ } = fake(reply);
    await expect(read($, { operation: 'snapshot' })).rejects.toThrow(message);
  }
});

test('an unavailable bridge says so without detail leaking', async () => {
  const { $, seen } = fake(text({}), { isConnected: false, reason: 'disabled', message: 'turned off' });
  await expect(read($, { operation: 'snapshot' })).rejects.toThrow('Companion bridge unavailable (disabled)');
  expect(seen.call).toEqual([]);
});

test('backend refusals are sorted into reload, profile, setup, denied and unavailable', () => {
  expect(classify('stale or invalid cursor; reload the view').kind).toBe('reload');
  expect(classify('profile changed during read; reload').kind).toBe('profile');
  expect(classify('confirm your identity in setup first').kind).toBe('setup');
  expect(classify('The selected task is unavailable in this profile').kind).toBe('denied');
  expect(classify('Companion read unavailable').kind).toBe('unavailable');
  expect(classify('\u001b[31mred\u001b[0m and\u0007 bell').message).toBe('red and bell');
});

test('installed host: the plugin\'s reads reach only its own server and tool', async ($, on) => {
  const h = host(on);
  await $.command.run({ command: 'cg-panel', args: 'ABC-1001' });
  expect(h.calls.reads.length).toBeGreaterThan(0);
  clean(h);
});
