// ARCH-02: the mod's only IPC. One server (this plugin's own `companion`, from its .mcp.json), one read-only
// tool, allowlisted operations and argument names, sizes and shapes checked both ways. No UI string ever
// becomes a server, tool, path, URL or command; nothing here writes, spawns, fetches or calls a model.
// Verified against the installed host's kit: $.mcp.connect(server) -> { isConnected, server } | { reason },
// $.mcp.call(server, tool, args) -> { content, isError }.
import { clean } from './text.js';

const SERVER = 'companion';
const TOOL = 'companion_read';
export const MAX_REPLY = 65536;
export const MAX_ARGS = 8192;

const ID = {
  workflow: /^w_[a-f0-9]{64}$/,
  plan: /^p_[a-f0-9]{64}$/,
  ticket: /^[A-Z][A-Z0-9]*-\d+$/,
  flow: /^f_[0-9a-f]{8,40}$/,
  investigation: /^i_[0-9a-f]{8,40}$/,
};
const CURSOR = /^[A-Za-z0-9_-]+\.[0-9a-f]{32}$/;
export const GROUPS = ['all', 'new', 'planned', 'review_incomplete', 'in_development', 'paused',
  'awaiting_verification', 'pr_review', 'completed'];

/** Stage 3a's operations and, per operation, the only argument names the mod ever sends. */
export const OPERATIONS = Object.freeze({
  snapshot: ['ticket', 'workflow', 'flow', 'investigation', 'privacy', 'pinned'],
  planner: ['group', 'cursor', 'pinned', 'privacy'],
  detail: ['workflow', 'privacy'],
  review: ['workflow', 'privacy'],
  plan: ['workflow', 'plan', 'cursor', 'offset', 'base'],
  changes: ['workflow', 'cursor'],
  verification: ['workflow', 'cursor'],
  jobs: ['workflow'],
  handoff: ['workflow', 'cursor'],
  database: ['flow', 'investigation', 'privacy'],
  graph_summary: ['ticket', 'cursor', 'privacy'], // the Evidence Graph's requirement rows for one ticket
});
const NEEDS_WORKFLOW = new Set(['detail', 'review', 'plan', 'changes', 'verification', 'jobs', 'handoff']);

export class BridgeError extends Error {
  /** kind: invalid | unavailable | shape | reload | profile | setup | denied */
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

const bytes = s => new TextEncoder().encode(s).length;

function value(name, v) {
  if (Object.hasOwn(ID, name)) {
    if (typeof v === 'string' && ID[name].test(v)) return v;
  } else if (name === 'base') {
    if (v === 'parent' || (typeof v === 'string' && ID.plan.test(v))) return v;
  } else if (name === 'cursor') {
    if (typeof v === 'string' && v.length <= 1024 && CURSOR.test(v)) return v;
  } else if (name === 'offset') {
    if (Number.isInteger(v) && v >= 0 && v <= 1e7) return v;
  } else if (name === 'privacy') {
    if (typeof v === 'boolean') return v;
  } else if (name === 'pinned') {
    if (Array.isArray(v) && v.length <= 10 && v.every(p => typeof p === 'string' && ID.ticket.test(p))) return [...v];
  } else if (name === 'group') {
    if (GROUPS.includes(v)) return v;
  }
  throw new BridgeError('invalid', 'Invalid Companion read argument: ' + clean(name, 20));
}

/** The request exactly as it will be sent, or a refusal before any IPC. */
export function check(args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) throw new BridgeError('invalid', 'Unsupported Companion read');
  const op = args.operation;
  if (typeof op !== 'string' || !Object.hasOwn(OPERATIONS, op)) throw new BridgeError('invalid', 'Unsupported Companion read');
  const out = { operation: op };
  for (const [name, v] of Object.entries(args)) {
    if (name === 'operation' || v === undefined || v === null) continue;
    if (!OPERATIONS[op].includes(name)) throw new BridgeError('invalid', 'Unsupported Companion read argument');
    out[name] = value(name, v);
  }
  if (NEEDS_WORKFLOW.has(op) && !out.workflow) throw new BridgeError('invalid', 'This read needs a task');
  if (bytes(JSON.stringify(out)) > MAX_ARGS) throw new BridgeError('invalid', 'Companion read arguments exceed 8 KiB');
  return out;
}

/** A refusal from the backend, sanitized and sorted into what the coordinator does next. */
export function classify(text) {
  const t = clean(text, 200) || 'Companion read unavailable';
  if (/profile changed/i.test(t)) return new BridgeError('profile', t);
  if (/reload|start again|stale or invalid cursor/i.test(t)) return new BridgeError('reload', t);
  if (/identity|setup/i.test(t)) return new BridgeError('setup', t);
  if (/unavailable in this profile|not found|denied|permission|hidden|unknown workflow/i.test(t)) return new BridgeError('denied', t);
  return new BridgeError('unavailable', t);
}

/** One bounded, read-only projection read through this plugin's own server. */
export async function read($, args) {
  const req = check(args);
  let conn;
  try {
    conn = await $.mcp.connect(SERVER);
  } catch (_) {
    throw new BridgeError('unavailable', 'Companion bridge unavailable');
  }
  if (!conn || conn.isConnected !== true || typeof conn.server !== 'string') {
    throw new BridgeError('unavailable', 'Companion bridge unavailable' + (conn && conn.reason ? ' (' + clean(conn.reason, 40) + ')' : ''));
  }
  let result;
  try {
    result = await $.mcp.call(conn.server, TOOL, req);
  } catch (_) {
    throw new BridgeError('unavailable', 'Companion read failed');
  }
  const blocks = result && Array.isArray(result.content) ? result.content : [];
  const raw = blocks.find(b => b && b.type === 'text' && typeof b.text === 'string')?.text;
  if (typeof raw !== 'string') throw new BridgeError('shape', 'Companion reply missing');
  if (bytes(raw) > MAX_REPLY) throw new BridgeError('shape', 'Companion reply larger than 64 KiB');
  if (result.isError) throw classify(raw);
  let data;
  try {
    data = JSON.parse(raw);
  } catch (_) {
    throw new BridgeError('shape', 'Companion reply is not JSON');
  }
  if (!data || typeof data !== 'object' || Array.isArray(data) || data.schema_version !== 1
      || data.operation !== req.operation || typeof data.profile_generation !== 'string') {
    throw new BridgeError('shape', 'Unsupported Companion reply; update the companion or use /cg:dashboard');
  }
  return data;
}
