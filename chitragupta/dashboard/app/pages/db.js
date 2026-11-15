// Database investigation: one workspace over the cached database-flow records (no query on any view). A start
// (ticket, operation, symbol, file or table) resolves to visible candidates; a flow shows as an ordered step list with
// basis labels, gaps and next evidence, an on-request graph of the same edges (with an equivalent keyboard table), and
// a detail panel with citations. Tabs: Explain flow · Record journey (key form -> reviewed trace plan -> explicit run
// through the protected db-* actions; one job poller that stops at a terminal state; per-step observations, notes and
// the local handoff draft; key values live in component state and request bodies only, never in the URL) ·
// Evidence (passports, sources, applicability, unresolved claims). The table rail and #/db/<table> pages of the DB map
// stay: one table's columns, sampled links (observed in a QA sample, never "verified"), readers and writers, tickets
// and saved queries, with a 1–2 hop neighbourhood diagram whose text alternative is a table.
import { useEffect, useMemo, useRef, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, setParams, href, fmtDay, fmtTs, plural, api, runAction, toast } from '../lib/core.js';
import { Icon, Status, Empty, Load, PageHead, TicketLink, DataTable, Diagram, Scroll, CodeBlock, Tabs, TabPanel, Dialog, Chip, CopyButton } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';
import { EnvBar, FocusHeader, ContextActions, objRef, LiveRegion, announce } from '../lib/dbx.js';

function same(a, b) { return a === b || a.endsWith('.' + b) || b.endsWith('.' + a); }
function short(k) { return String(k || '').split('.').slice(-1)[0]; }
function col(ref) { return short(ref); }
// One pass to Mermaid entity codes (cut first, so no code is split); labels render as plain SVG text (strict, no HTML).
function lbl(s) { return '"' + String(s).replace(/[\u0000-\u001f\u007f]/g, ' ').slice(0, 80).replace(/["\[\]{}<>|#;]/g, (c) => '#' + c.charCodeAt(0) + ';') + '"'; }
// line style as well as colour: thick = sample matched, plain = partly matched, dotted = weak or not sampled
const ARROW = { sample_matched: ' ==>|', sample_partial: ' -->|', sample_weak: ' -.->|', not_sampled: ' -.->|' };

function graphSource(nodes, edges, focus) {
  const ids = {};
  const id = (k) => { const hit = Object.keys(ids).find((x) => same(x, k)); if (hit) return ids[hit]; ids[k] = 'n' + Object.keys(ids).length; return ids[k]; };
  if (focus) id(focus);
  nodes.forEach(id);
  const lines = ['flowchart LR'];
  Object.keys(ids).forEach((k) => lines.push('  ' + ids[k] + '[' + lbl(k) + ']'));
  edges.forEach((e) => lines.push('  ' + id(e.from) + (ARROW[e.status] || ARROW.not_sampled) + lbl(col(e.from_col) + ' → ' + col(e.to_col)) + '| ' + id(e.to)));
  if (focus) lines.push('  style ' + ids[Object.keys(ids).find((x) => same(x, focus))] + ' stroke-width:3px');
  return lines.join('\n');
}

function LinkStatus({ l }) {
  const kind = { sample_matched: 'ok', sample_partial: 'warn' }[l.status] || 'off';
  const word = l.ratio != null ? 'Sample matched ' + Math.round(l.ratio * 100) + '%' : 'Not sampled';
  return html`<${Status} kind=${kind}>${word}</${Status}>`;
}

function TableRef({ k, keys }) {
  const hit = keys.find((x) => same(x, k));
  return hit ? html`<a class="mono small wrapname" title=${k} href=${href('db', hit)}>${k}</a>` : html`<span class="mono small wrapname" title=${k}>${k}</span>`;
}

function LinksTable({ links, keys, focus }) {
  if (!links.length) return html`<${Empty} title="No links recorded for this table yet" icon="db">Links appear when the QA link map samples them or when a session or code shows two tables joined.</${Empty}>`;
  return html`<${Scroll} label="Links"><table><caption class="sr">Links of ${focus}</caption>
    <thead><tr><th scope="col">From</th><th scope="col">To</th><th scope="col">QA sample</th><th scope="col">Tickets</th><th scope="col" class="hide-sm">Evidence</th></tr></thead>
    <tbody>${links.map((l, i) => html`<tr key=${i}>
      <td><${TableRef} k=${l.from} keys=${keys} /><div class="muted small mono">${col(l.from_col)}</div></td>
      <td><${TableRef} k=${l.to} keys=${keys} /><div class="muted small mono">${col(l.to_col)}</div></td>
      <td><${LinkStatus} l=${l} /><div class="muted small">${l.label || ''} · ${l.basis_label || ''}</div></td>
      <td>${(l.tickets || []).map((k) => html`<${TicketLink} key=${k} k=${k} /> `)}</td><td class="hide-sm small">${l.evidence || ''}</td></tr>`)}</tbody></table></${Scroll}>`;
}

const COLS = [
  { id: 'name', label: 'Column', value: (c) => c.name, render: (c) => html`<span class="mono wrapname" title=${c.name}>${c.name}</span>` },
  { id: 'type', label: 'Type', value: (c) => c.type || '', render: (c) => html`<span class="mono small">${c.type || '—'}</span>` },
  { id: 'flags', label: 'Key', render: (c) => [c.pk || c.primary_key ? 'primary key' : '', c.fk || c.foreign_key ? 'foreign key' : '', c.phi || c.sensitive ? 'personal data' : ''].filter(Boolean).join(', ') || '—' },
  { id: 'null', label: 'Nullable', hideSm: true, render: (c) => (c.nullable == null ? '—' : c.nullable ? 'yes' : 'no') },
];

function TablePanel({ k, hops, keys }) {
  const res = useApi('/api/db/table?key=' + encodeURIComponent(k) + (hops === 2 ? '&hops=2' : ''));
  return html`<${Load} res=${res} lines=${8}>${(t) => html`
    <div class="sec-head"><h2 class="mono wrapname" title=${t.key}>${t.name}</h2><span class="muted small">${t.database} › ${t.schema}</span><span class="grow"></span>
      <a class="btn quiet" href=${href('db')}><${Icon} name="left" size=${15} />Database investigation</a></div>
    ${t.purpose ? html`<p class="lede">${t.purpose} <span class="muted small">(Unconfirmed meaning: a session summary, not a citation)</span></p>` : null}
    <dl class="kv" style="margin-bottom:16px">
      ${t.connection ? html`<dt>Schema card</dt><dd>${t.connection}${t.fetched_at ? ', read ' + fmtDay(String(t.fetched_at).slice(0, 10), true) : ''}</dd>` : null}
      ${t.key_columns.length ? html`<dt>Key columns</dt><dd>${t.key_columns.map((c) => html`<code key=${c}>${c}</code> `)}</dd>` : null}
      ${t.tickets.length ? html`<dt>Tickets</dt><dd>${t.tickets.map((x) => html`<${TicketLink} key=${x} k=${x} /> `)}</dd>` : null}
    </dl>
    <div class="sec-head"><h3>Neighbourhood</h3><span class="grow"></span>
      <div role="group" aria-label="How far to draw" class="seg">${[1, 2].map((n) => html`<button key=${n} type="button" aria-pressed=${hops === n ? 'true' : 'false'}
        onClick=${() => setParams({ hops: n === 2 ? '2' : '' })}>${n === 1 ? 'Direct links' : 'Two hops'}</button>`)}</div></div>
    ${t.graph.edges.length ? html`<${Diagram} code=${graphSource(t.graph.nodes, t.graph.edges, t.key)} label=${'Links around ' + t.key}
        alt=${html`<${LinksTable} links=${t.graph.edges} keys=${keys} focus=${t.key} />`} />
      <p class="muted small">Thick arrow: a QA sample matched 80% or more of its values · plain: 30% or more · dotted: weak or not sampled. A sample is observed evidence, not a declared constraint.</p>`
      : html`<p class="muted">No links to draw.</p>`}
    <h3 style="margin:16px 0 8px">Links</h3><${LinksTable} links=${t.links} keys=${keys} focus=${t.key} />
    <h3 style="margin:16px 0 8px">Columns</h3>
    ${t.columns.length ? html`<${DataTable} columns=${COLS} rows=${t.columns} rowKey=${(c) => c.name} caption=${'Columns of ' + t.key} />`
      : html`<${Empty} title="No schema card for this table" icon="db">Columns appear after the database MCP describes the table.</${Empty}>`}
    <h3 style="margin:16px 0 8px">Who reads and writes it</h3>
    ${t.writers.writes.length || t.writers.reads.length ? html`<${Scroll} label="Readers and writers"><table><caption class="sr">Readers and writers of ${t.key}</caption>
      <thead><tr><th scope="col">Component</th><th scope="col">Does</th><th scope="col">Column</th><th scope="col">Tickets</th></tr></thead>
      <tbody>${t.writers.writes.map((w, i) => html`<tr key=${'w' + i}><td class="mono small">${w.component || '—'}</td><td>writes${w.change ? ' (' + w.change + ')' : ''}</td><td class="mono small">${w.column || '—'}</td><td>${w.tickets.map((x) => html`<${TicketLink} key=${x} k=${x} /> `)}</td></tr>`)}
        ${t.writers.reads.map((w, i) => html`<tr key=${'r' + i}><td class="mono small">${w.component || '—'}</td><td>reads</td><td>—</td><td>${w.tickets.map((x) => html`<${TicketLink} key=${x} k=${x} /> `)}</td></tr>`)}</tbody></table></${Scroll}>`
      : html`<p class="muted">No code seen reading or writing it yet. Static analysis may miss dynamic SQL.</p>`}
    <h3 style="margin:16px 0 8px">Saved queries</h3>
    ${t.queries.length ? t.queries.map((q) => html`<div key=${q.ticket + q.id} style="margin-bottom:12px"><p class="small"><${TicketLink} k=${q.ticket} />
        ${q.purpose ? ' · ' + q.purpose : ''}${q.date ? html`<span class="muted"> · ${fmtDay(q.date, true)}</span>` : null}</p>
        <${CodeBlock} code=${q.text} label=${q.dialect || 'sql'} what="the query" /></div>`)
      : html`<p class="muted">No saved queries use this table.</p>`}`}</${Load}>`;
}

// ------------------------------------------------------------------ the investigation workspace

const BASIS_KIND = { declared_metadata: 'ok', code_supported: 'ok', user_confirmed: 'info', observed_sample: 'info', inferred: 'off' };
function Basis({ b, label }) {
  return html`<span class=${'basis b-' + (b || 'inferred')}><${Status} kind=${BASIS_KIND[b] || 'off'} quiet=${true}>${label || 'Inferred'}</${Status}></span>`;
}

function States({ states }) {
  if (!states || !states.length) return null;
  return html`<ul class="dbx-states" aria-label="What needs attention">${states.map((s) => html`<li key=${s.id + s.text}>
    <${Status} kind=${s.id === 'permission_denied' || s.id === 'disconnected_source' ? 'bad' : 'warn'}>${s.title}</${Status}>
    <span>${s.text !== s.title ? String(s.text).replace(/[.]$/, '') + '. ' : ''}<strong>Next:</strong> ${s.next}</span></li>`)}</ul>`;
}

function ScopeDialog({ open, onClose, ws }) {
  return html`<${Dialog} open=${open} title="Environment and scope" onClose=${onClose}>
    <p>Views read only the approved scope: connections with an explicit environment that the safety guard allows. ${ws.scope_note}</p>
    ${ws.connections.length ? html`<${Scroll} label="Approved connections"><table><caption class="sr">Approved connections</caption>
      <thead><tr><th scope="col">Connection</th><th scope="col">Environment</th><th scope="col">Engine</th><th scope="col">Schemas</th><th scope="col">Access</th></tr></thead>
      <tbody>${ws.connections.map((c) => html`<tr key=${c.id}><td class="mono">${c.id}</td><td>${c.env.toUpperCase()}</td><td>${c.engine || 'unknown'}</td>
        <td class="mono small wrapname">${c.schemas.join(', ') || 'unknown'}</td><td class="small">${c.authorization}</td></tr>`)}</tbody></table></${Scroll}>`
      : html`<p class="muted">No connection is approved.</p>`}
    ${ws.blocked.length ? html`<h3>Not in scope</h3><ul>${ws.blocked.map((b) => html`<li key=${b.id}><span class="mono">${b.id}</span>: ${b.reason}</li>`)}</ul>` : null}
    <p class="muted small">Scope comes from ${ws.scope_provenance}. Repositories: ${ws.repos.join(', ') || 'none selected'}.</p>
    <p><a class="btn" href=${href('settings')}>Open settings</a></p></${Dialog}>`;
}

function StartForm({ p }) {
  const [text, setText] = useState(p.q || '');
  const [kind, setKind] = useState(p.kind || '');
  const mounted = useRef(false);  // follow later URL changes only: what you type right after the page opens is kept
  useEffect(() => { if (!mounted.current) { mounted.current = true; return; } setText(p.q || ''); setKind(p.kind || ''); }, [p.q, p.kind]);
  const res = useApi(p.q ? '/api/db/resolve?q=' + encodeURIComponent(p.q) + (p.kind ? '&kind=' + p.kind : '') : null, { live: false });
  const pick = (c) => setParams(c.kind === 'ticket' ? { start: 'ticket:' + c.key, flow: '', q: '', tab: '' } : { start: 'node:' + c.id, flow: '', q: '', tab: '' });
  return html`<form class="dbx-start" role="search" onSubmit=${(e) => { e.preventDefault(); setParams({ q: text.trim(), kind }); }}>
      <label for="dbx-q">Start from a ticket, operation, code symbol or file, table, or words</label>
      <div class="dbx-row"><input id="dbx-q" type="search" value=${text} onInput=${(e) => setText(e.currentTarget.value)} placeholder="ABC-1001, POST /orders, shop.orders" autocomplete="off" />
      <label class="sr" for="dbx-kind">Kind</label>
      <select id="dbx-kind" value=${kind} onChange=${(e) => setKind(e.currentTarget.value)}>
        <option value="">Any</option><option value="ticket">Ticket</option><option value="api">API operation</option>
        <option value="symbol">Code symbol or file</option><option value="table">Table</option><option value="text">Words</option></select>
      <button type="submit" class="btn primary">Find</button></div></form>
    ${p.q ? html`<${Load} res=${res} lines=${2}>${(r) => html`<div class="dbx-cands" aria-live="polite">
      <${States} states=${r.states} />
      ${r.items.length ? html`<p class="small muted">${r.data.state === 'resolved' ? 'One match:' : plural(r.page.total, 'candidate') + ': pick one (nothing is chosen for you).'}</p>
        <ul class="rlist">${r.items.map((c) => html`<li key=${c.id || c.key}><button type="button" class="btn quiet wrapname" title=${c.label} onClick=${() => pick(c)}>
          <span class="mono">${c.label}</span> <span class="muted small">${c.detail || ''}</span></button></li>`)}</ul>` : html`<p class="muted">${r.data.reason || 'Nothing recorded matches.'}</p>`}</div>`}</${Load}>` : null}`;
}

// A step's place: its stage within its group (main path, a separate path restarting at 1, or unordered). Steps that
// share a stage have no evidenced order between them; the list index is never shown as a sequence.
function stageWord(s) {
  if (s.stage == null) return 'Unordered: ' + s.order_basis;
  return 'Stage ' + s.stage + ' of ' + (s.group_label || 'the flow') + (s.stage_shared ? ' (same stage, no order between them)' : '') + ' · ' + s.order_basis;
}
function edgeStage(e) { return e.stage == null ? 'unordered' : (e.group_label && e.group_label !== 'Main path' ? 'separate path, ' : '') + 'stage ' + e.stage; }

// The graph's rows: exactly the page's step edges (DBT-33) plus one "not recorded" row per unknown boundary step, each
// with its meaning, direction, basis, async flag and the database / service boundary it crosses.
const ASYNC_KINDS = { publishes: 1, consumes: 1 };
function graphRows(g, steps) {
  const node = (id) => g.nodes.find((n) => n.id === id) || { id, label: id, kind: '' };
  const side = (n) => (n.connection ? n.connection + ' (' + String(n.environment || '').toUpperCase() + ')' : n.kind === 'event' ? 'event' : 'service or code');
  const rows = g.edges.map((e) => {
    const s = steps.find((x) => x.id === e.step) || {};
    const a = node(e.source), b = node(e.target);
    const async = !!(s.async || ASYNC_KINDS[e.kind] || /event/i.test(e.transport || ''));
    const boundary = side(a) === side(b) ? 'within ' + side(a) : side(a) + ' → ' + side(b);
    return { id: e.id, step: e.step, stage: edgeStage(e), from: a, to: b, label: e.label, basis: e.basis, basis_label: e.basis_label || 'Inferred', async, boundary };
  });
  steps.filter((s) => s.kind === 'unknown_boundary' && !s.edge).forEach((s) => {
    const a = node((s.nodes || [])[0]);
    rows.push({ id: 'u-' + s.id, step: s.id, stage: 'unknown', from: a, to: { id: 'u-' + s.id, label: '? (not recorded)', kind: 'unknown' }, label: 'what happens next is not recorded', basis: 'inferred', basis_label: s.basis_label || 'Inferred', async: false, boundary: 'unknown boundary', unknown: true });
  });
  return rows;
}

// Mermaid source: nodes grouped by database connection / services / events (boundaries as boxes), line style as well
// as colour and a text label on every edge: solid = cited, thick = cited and asynchronous, dotted = inferred or unknown.
function stepGraph(g, rows) {
  const ids = {}, back = {};
  g.nodes.forEach((n, i) => { ids[n.id] = 'n' + i; back['n' + i] = n.id; });
  const short = (n) => String(n.label).replace(/\s*\([^)]*\)\s*$/, '');
  const shape = (n) => (n.kind === 'table' || n.kind === 'view' ? '[(' + lbl(short(n)) + ')]' : n.kind === 'event' ? '>' + lbl(n.label) + ']' : n.kind === 'api_operation' ? '([' + lbl(n.label) + '])' : '[' + lbl(n.label) + ']');
  const groups = {};
  g.nodes.forEach((n) => { const k = n.connection ? n.connection + ' · ' + String(n.environment || '').toUpperCase() + ' database' : n.kind === 'event' ? 'Events (asynchronous)' : 'Services and code'; (groups[k] = groups[k] || []).push(n); });
  const lines = ['flowchart LR'];
  Object.keys(groups).forEach((k, i) => { lines.push('  subgraph b' + i + '[' + lbl(k) + ']'); groups[k].forEach((n) => lines.push('    ' + ids[n.id] + shape(n))); lines.push('  end'); });
  rows.forEach((r, i) => {
    if (!ids[r.from.id]) return;
    if (r.unknown) { lines.push('  u' + i + '{{"?"}}'); lines.push('  ' + ids[r.from.id] + ' -.->|' + lbl('unknown: not recorded') + '| u' + i); return; }
    if (!ids[r.to.id]) return;
    const arrow = r.basis === 'inferred' ? ' -.->|' : r.async ? ' ==>|' : ' -->|';
    lines.push('  ' + ids[r.from.id] + arrow + lbl(r.stage + ': ' + r.label + ' · ' + (r.basis === 'inferred' ? 'inferred' : r.basis_label) + (r.async ? ' · async' : '')) + '| ' + ids[r.to.id]);
  });
  return { code: lines.join('\n'), back };
}

function GraphTable({ rows, onPick }) {
  return html`<${Scroll} label="Graph edges"><table><caption class="sr">The same edges as the step list, with direction, basis and boundary</caption>
    <thead><tr><th scope="col">Stage</th><th scope="col">From</th><th scope="col">Edge (opens its detail)</th><th scope="col">To</th><th scope="col">Basis</th><th scope="col">Boundary</th></tr></thead>
    <tbody>${rows.map((r) => html`<tr key=${r.id}><td class="small">${r.stage}</td><td class="wrapname" title=${r.from.label}>${r.from.label}</td>
      <td><button type="button" class="btn quiet wrapname" onClick=${() => onPick(r.step)}>${r.label}${r.async ? ' (async)' : ''}</button></td>
      <td class="wrapname" title=${r.to.label}>${r.to.label}</td><td>${r.basis === 'inferred' ? html`<em>${r.basis_label}</em>` : r.basis_label}</td>
      <td class="small wrapname">${r.boundary}</td></tr>`)}</tbody></table></${Scroll}>`;
}

function StepDetail({ s, gaps, onClose, nodes, conns, focus }) {
  const ref = useRef(null);
  useEffect(() => { if (s && focus && ref.current) ref.current.focus(); }, [s && s.id, focus]);
  if (!s) return null;
  const mine = gaps.filter((g) => (s.gaps || []).indexOf(g.id) >= 0);
  const objs = (s.nodes || []).map((id) => (nodes || []).find((n) => n.id === id)).filter((n) => n && /^o_/.test(n.id) && n.connection);
  return html`<aside class="dbx-detail" aria-label=${'Step detail: ' + s.label} tabindex="-1" id="dbx-detail" ref=${ref}>
    <div class="sec-head"><h3>${s.kind_label}</h3><span class="grow"></span><button type="button" class="btn quiet" onClick=${onClose}>Close</button></div>
    <p class="wrapname">${s.label}</p>
    ${objs.map((n) => html`<div key=${n.id} class="dbx-actions"><span class="mono small wrapname" title=${n.label}>${n.label}</span>
      <${ContextActions} obj=${objRef(n)} conns=${conns} /></div>`)}
    <dl class="kv"><dt>Basis</dt><dd><${Basis} b=${s.basis} label=${s.basis_label} /></dd>
      <dt>Order</dt><dd>${stageWord(s)}</dd>
      <dt>Path</dt><dd>${s.path_known ? s.path : 'not distinguished (main path assumed)'}${s.async ? ' · asynchronous' : ''}</dd>
      ${s.columns && s.columns.length ? html`<dt>Columns</dt><dd class="mono small">${s.columns.join(', ')}</dd>` : null}
      ${s.edge ? html`<dt>Edge</dt><dd class="mono small">${s.edge}</dd>` : null}</dl>
    <h4>Citations</h4>
    ${s.evidence.length ? html`<ul class="dbx-ev">${s.evidence.map((v) => html`<li key=${v.id}><span class="wrapname">${v.text}</span>
      <div class="muted small">${v.basis_label} · ${v.validation === 'validated' ? 'references checked' : 'not checked'}${v.content_hash ? ' · file ' + v.content_hash : ''}</div></li>`)}</ul>`
      : html`<p class="muted">No citation: this step is inferred.</p>`}
    ${s.lineage.length ? html`<h4>Column lineage</h4><ul>${s.lineage.map((l, i) => html`<li key=${i} class="small"><span class="mono">${l.from.join(', ')}</span> → <span class="mono">${l.to.join(', ')}</span>: ${l.transform} (${l.transport}, ${l.basis_label})</li>`)}</ul>` : null}
    ${mine.length ? html`<h4>Gaps</h4><ul>${mine.map((g) => html`<li key=${g.id} class="small"><strong>${g.label}</strong>: ${g.text} <em>Next:</em> ${g.next}</li>`)}</ul>` : null}
  </aside>`;
}

function groupsOf(steps) {
  const out = [];
  steps.forEach((s) => { const g = s.group_label || 'Steps'; if (!out.length || out[out.length - 1].label !== g) out.push({ label: g, steps: [] }); out[out.length - 1].steps.push(s); });
  return out;
}

function ExplainFlow({ f, sel, setSel, conns, setTab }) {
  const [graph, setGraph] = useState(false);
  const [jump, setJump] = useState(0);  // bumps when the graph or its table opens a step: focus moves to its detail
  const steps = f.items;
  const d = f.data;
  const cur = steps.find((s) => s.id === sel);
  const rows = useMemo(() => graphRows(d.graph, steps), [f]);
  const gsrc = useMemo(() => stepGraph(d.graph, rows), [rows]);
  const pick = (id) => { setSel(id); setJump((n) => n + 1); };
  const nodeClick = (ev) => {  // a click on a drawn node opens the first step that stands on it
    const g = ev.target.closest && ev.target.closest('g.node');
    const m = g && /(?:^|-)(n\d+)(?:-|$)/.exec(g.id || '');
    const nid = m && gsrc.back[m[1]];
    const s = nid && steps.find((x) => (x.nodes || []).indexOf(nid) >= 0);
    if (s) pick(s.id);
  };
  return html`<div class="dbx-flow">
    <${FocusHeader} f=${f} onEvidence=${() => setTab('evidence')} onPlan=${() => setTab('journey')} />
    <p class="dbx-scope"><strong>Scope:</strong> ${d.scope_line} · <span class="muted">${d.label}</span></p>
    <${States} states=${f.states} />
    <p class="muted small">Numbers are stages ordered by evidence: steps sharing a number have no order between them, and a separate path starts again at 1.</p>
    ${groupsOf(steps).map((g) => html`<section key=${g.label} aria-label=${g.label}><h3 class="small">${g.label}</h3>
    <ol class="dbx-steps" aria-label=${'Steps: ' + g.label}>${g.steps.map((s) => html`<li key=${s.id} class=${'dbx-step k-' + s.kind + (s.id === sel ? ' on' : '')}>
      <button type="button" class="dbx-stepbtn" aria-expanded=${s.id === sel ? 'true' : 'false'} aria-controls="dbx-detail" onClick=${() => setSel(s.id === sel ? '' : s.id)}>
        <span class="dbx-n" title=${stageWord(s)}>${s.stage != null ? s.stage + '.' : '–'}</span>
        <span class="dbx-what"><span class="dbx-kind">${s.kind_label}</span><span class="wrapname" title=${s.label}>${s.label}</span>
          ${s.async ? html`<${Chip}>async</${Chip}>` : null}${s.path_known ? html`<${Chip}>${s.path}</${Chip}>` : null}${s.stage_shared ? html`<${Chip}>same stage</${Chip}>` : null}${s.stage == null ? html`<${Chip}>unordered</${Chip}>` : null}</span>
        <${Basis} b=${s.basis} label=${s.basis_label} /></button></li>`)}</ol></section>`)}
    ${f.more_available ? html`<p class="muted small">More steps are available: open the flow with the CLI (cli db flow) or narrow the start.</p>` : null}
    <${StepDetail} s=${cur} gaps=${d.gaps} onClose=${() => setSel('')} nodes=${d.graph.nodes} conns=${conns} focus=${jump} />
    <h3>Gaps and next evidence</h3>
    ${d.gaps.length ? html`<ul class="dbx-gaps">${d.gaps.map((g) => html`<li key=${g.id}><strong>${g.label}</strong>: ${g.text} <span class="muted"><strong>Next:</strong> ${g.next}</span></li>`)}</ul>` : html`<p class="muted">No gap recorded for these steps; static analysis may still miss dynamic SQL.</p>`}
    <div class="sec-head"><h3>Graph</h3><span class="grow"></span>
      <button type="button" class="btn" aria-expanded=${graph ? 'true' : 'false'} aria-controls="dbx-graph" onClick=${() => setGraph(!graph)}>${graph ? 'Hide graph' : 'Expand relevant graph'}</button></div>
    <div id="dbx-graph">${graph ? (rows.length ? html`<div onClick=${nodeClick}><${Diagram} code=${gsrc.code} label="The flow's objects and edges, grouped by database connection and service"
        alt=${html`<${GraphTable} rows=${rows} onPick=${pick} />`} /></div>
      <p class="muted small">Boxes group objects by database connection, services and events. Solid arrow: cited (catalog, code, sample or your confirmation) · thick: cited and asynchronous (an event) · dotted: inferred or not recorded (?). Every arrow is labelled with its stage, meaning and basis; "Show as text" lists the same edges as a table whose buttons open each step's detail. Clicking a box opens its first step.</p>`
      : html`<p class="muted">This page of steps stands on no recorded edge (entry points and unknown boundaries only): the step list above is the whole recorded story. Next: open the Evidence tab or widen the start.</p>`) : null}</div></div>`;
}

// ------------------------------------------------------------------ record journey (D2)

const OBS_KIND = { observed: 'ok', truncated: 'warn', not_observed_in_scope: 'info', mapping_unresolved: 'warn', not_queried: 'off', permission_denied: 'bad', failed: 'bad' };
const JOB_KIND = { completed: 'ok', partial: 'warn', cancelled: 'off', timed_out: 'warn', revoked: 'bad', failed: 'bad' };
// The specific next step of each job state and observation status (DB-23): shown when the record names none.
const JOB_NEXT = {
  queued: 'Wait for the runner to pick it up; this page only reads the job record. You can cancel it.',
  running: 'Wait: reads go out within the reviewed plan only. Cancel stops further reads.',
  cancel_requested: 'Wait for the runner to stop; a read already sent may still finish at the provider.',
  cancelled: 'Answered steps are kept. Review a new plan to trace again.',
  timed_out: 'The overall deadline passed; the provider query may still have finished. Review a new plan, with fewer steps if needed.',
  completed: 'Read the observations below; steps not observed are not data loss.',
  partial: 'Some steps were answered; see each step for why the others were not.',
  failed: 'Nothing was answered; see the reason, then review a new plan.',
  revoked: 'The access scope changed during the run: nothing was published. Check the scope, then review a new plan.',
};
const OBS_NEXT = {
  observed: 'A bounded observation (at most the plan\'s row limit): compare it with the other steps and their times.',
  not_observed_in_scope: 'No matching record within this query scope and time: check again later (eventual consistency) or inspect the consumer\'s evidence.',
  mapping_unresolved: 'The mapping to this step is incomplete: confirm the column mapping, then review a new plan.',
  not_queried: 'Not part of what ran: review a new plan for this step.',
  permission_denied: 'Ask for read access on this connection; it is checked again at the next refresh.',
  failed: 'The read failed; review a new plan to try again.',
  truncated: 'More rows matched than the limit: narrow the key.',
};

// The key form: environment (approved connections only, blocked ones with their reason), start object, every key
// field. Typed values stay in component state through errors and refreshes; they go only into the POST body.
function TraceForm({ src, onPlan, formFocus }) {
  const spec = useApi('/api/db/plan?' + (src.flow ? 'flow=' + encodeURIComponent(src.flow) : 'start=' + encodeURIComponent(src.start)), { live: false });
  useEffect(() => {  // "Review trace plan" from the focus header: focus the form's first control once it is there
    if (!formFocus || !spec.data) return;
    const el = document.getElementById('dbx-env') || document.getElementById('dbx-obj');
    if (el) el.focus();
  }, [formFocus, !!spec.data]);
  const [env, setEnv] = useState('');
  const [obj, setObj] = useState('');
  const [keyVals, setKeyVals] = useState({});
  const [objective, setObjective] = useState('');
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  return html`<${Load} res=${spec} lines=${3}>${(e) => {
    const d = e.data;
    const envs = d.environments;
    const curEnv = env || (envs.length === 1 ? envs[0].env : '');
    const objs = d.objects.filter((o) => !curEnv || o.environment === curEnv);
    const cur = objs.find((o) => o.id === obj) || (objs.length === 1 ? objs[0] : null);
    const review = (ev) => {
      ev.preventDefault();
      if (!cur || !curEnv) { setErr({ error: 'Pick the environment and the start object.' }); return; }
      setBusy(true); setErr(null);
      const body = { env: curEnv, object: cur.id, key: keyVals, objective: objective || undefined };
      if (src.flow) body.flow = src.flow; else body.start = src.start;
      runAction('db-plan', body).then((plan) => { setBusy(false); onPlan(plan); announce('Trace plan ready for review: nothing has been queried.'); }, (x) => {
        setBusy(false);
        setErr({ error: x.message, field: x.fields || null });
        announce('Not planned: ' + x.message);
        const m = x.message.match(/missing (\w+)|^(\w+) is a /) || [];
        const el = (m[1] || m[2]) && document.getElementById('dbx-key-' + (m[1] || m[2]));
        if (el) el.focus();  // the typed values stay as they are: nothing in the form is reset by an error
      });
    };
    return html`<form class="dbx-start" onSubmit=${review} aria-describedby="dbx-trace-note">
      <p id="dbx-trace-note" class="muted small">${d.history_note} ${d.capabilities.text}</p>
      ${envs.length ? html`<label for="dbx-env">Environment</label>
        <select id="dbx-env" value=${curEnv} onChange=${(x) => { setEnv(x.currentTarget.value); setObj(''); }}>
          ${envs.length > 1 ? html`<option value="">Choose…</option>` : null}
          ${envs.map((x) => html`<option key=${x.env} value=${x.env}>${x.env.toUpperCase()} (${x.connections.join(', ')})</option>`)}</select>`
        : html`<p><${Status} kind="warn">No approved connection</${Status}> This flow touches no connection you may read.</p>`}
      ${d.blocked.length ? html`<details class="small"><summary>Not available (${d.blocked.length})</summary><ul>${d.blocked.map((b) => html`<li key=${b.id}><span class="mono">${b.id}</span>: ${b.reason}</li>`)}</ul></details>` : null}
      <label for="dbx-obj">Start object</label>
      <select id="dbx-obj" value=${cur ? cur.id : ''} onChange=${(x) => setObj(x.currentTarget.value)}>
        ${objs.length !== 1 ? html`<option value="">Choose…</option>` : null}
        ${objs.map((o) => html`<option key=${o.id} value=${o.id} disabled=${!o.fields.length}>${o.label}${o.fields.length ? '' : ' (' + o.reason + ')'}</option>`)}</select>
      ${cur ? html`<fieldset><legend>Key of ${cur.label} (every field, tenant included)</legend>
        ${cur.fields.map((fl) => html`<div key=${fl.name} class="dbx-row"><label for=${'dbx-key-' + fl.name}>${fl.name} <span class="muted small">${fl.type}</span></label>
          <input type="text" id=${'dbx-key-' + fl.name} autocomplete="off" spellcheck="false" value=${keyVals[fl.name] || ''}
            onInput=${(x) => { const v = x.currentTarget.value; setKeyVals((k) => ({ ...k, [fl.name]: v })); }} /></div>`)}</fieldset>` : null}
      <label for="dbx-objective">Objective (optional)</label>
      <input type="text" id="dbx-objective" value=${objective} onInput=${(x) => setObjective(x.currentTarget.value)} placeholder="What should this trace answer?" />
      ${err ? html`<p role="alert"><${Status} kind="bad">Not planned</${Status}> ${err.error}</p>` : null}
      <p><button type="submit" class="btn primary" disabled=${busy || !cur}>${busy ? 'Reviewing…' : 'Review trace plan'}</button></p></form>`;
  }}</${Load}>`;
}

// The reviewed plan: targets, columns, predicates (values masked), limits, coverage, data handling, transport.
function PlanReview({ plan, onRun, busy }) {
  const d = plan.data;
  const l = d.limits;
  return html`<section class="card" aria-labelledby="dbx-plan-h"><h3 id="dbx-plan-h">Review trace plan</h3>
    <p>${d.flow.title} · ${plan.environment} · ${d.connections.join(', ')} · start ${d.start.object_label} by ${d.start.fields.join(', ')} (values not shown)</p>
    <${Scroll} label="Planned reads"><table><caption class="sr">Planned reads</caption>
      <thead><tr><th scope="col">#</th><th scope="col">Target</th><th scope="col">Columns</th><th scope="col">Where</th><th scope="col">Limit</th><th scope="col">Status</th></tr></thead>
      <tbody>${plan.items.map((s) => html`<tr key=${s.id}><td>${s.n}</td><td class="wrapname">${s.object_label}<div class="muted small">${s.mapping || ''}${s.basis_label ? ' · ' + s.basis_label : ''}</div></td>
        <td class="mono small wrapname">${(s.columns || []).join(', ') || '—'}</td><td class="mono small">${s.predicate_shape || '—'}</td><td>${s.limit || '—'}</td>
        <td><${Status} kind=${s.status === 'planned' ? 'ok' : 'warn'}>${s.status_label}</${Status}>${s.reason ? html`<div class="small">${s.reason}</div>` : null}</td></tr>`)}</tbody></table></${Scroll}>
    <dl class="kv"><dt>Limits</dt><dd>${l.rows_per_step} rows per step · ${l.queries} queries · ${l.concurrent} at a time · ${l.statement_timeout_s} s per statement · ${l.deadline_s} s overall · ${Math.round(l.value_bytes / 1024)} KiB of values</dd>
      <dt>Coverage estimate</dt><dd>${d.coverage_estimate}</dd>
      <dt>Provider limits</dt><dd>${d.capabilities.text}</dd>
      <dt>Data handling</dt><dd>${d.data_handling.text}${d.data_handling.retained.length ? ' Kept: ' + d.data_handling.retained.map((x) => x.step + ' (' + x.columns.join(', ') + ')').join('; ') + '.' : ''}</dd>
      <dt>Retention</dt><dd>${d.retention} ${d.key_note}</dd>
      <dt>Transport</dt><dd>${d.transport_disclosure}</dd>
      <dt>Fingerprint</dt><dd class="mono small">${d.fingerprint}</dd></dl>
    ${d.runnable ? html`<p><button type="button" class="btn primary" disabled=${busy} onClick=${() => onRun(plan)}>${busy ? 'Starting…' : 'Run trace'}</button>
        <span class="muted small"> Runs exactly this plan (${plural(d.queries_planned, 'read')}); a changed plan needs a new review.</span></p>`
      : html`<p class="muted">${d.key_kept ? 'Nothing in this plan can be queried; see each step.' : 'The key is no longer kept: review the plan again with the key.'}</p>`}</section>`;
}

// One poller per job: reads /api/db/job (a read: it never enqueues) until the job reaches a terminal state.
function JobProgress({ jobId, onDone }) {
  const [job, setJob] = useState(null);
  const [cancelling, setCancelling] = useState(false);
  useEffect(() => {
    let stop = false, timer = 0;
    const tick = () => api('/api/db/job?id=' + encodeURIComponent(jobId)).then((j) => {
      if (stop) return;
      setJob(j);
      if (j.data.terminal) onDone(j); else timer = setTimeout(tick, 1500);
    }, () => { if (!stop) timer = setTimeout(tick, 4000); });
    tick();
    return () => { stop = true; clearTimeout(timer); };
  }, [jobId]);
  const cancel = () => {
    setCancelling(true);
    runAction('db-trace-cancel', { job: jobId }).then((j) => setJob(j), (x) => { setCancelling(false); toast(x.message, 'bad'); });
  };
  if (!job) return html`<p aria-live="polite">Starting the trace…</p>`;
  const d = job.data;
  return html`<div class="card" aria-live="polite"><p><${Status} kind=${d.terminal ? (JOB_KIND[d.state] || 'off') : 'info'}>${d.state_label}</${Status}>${d.text !== d.state_label ? ' ' + d.text : ''}</p>
    <p class="small muted">${d.budget.queries_used} of ${d.budget.queries_allowed} reads sent${d.accounting && d.accounting.text ? ' · ' + d.accounting.text : ''}</p>
    ${d.in_flight ? html`<p class="small">${d.in_flight.text}</p>` : null}
    ${d.state === 'cancel_requested' ? html`<p class="small">Cancellation requested. If a read was already sent: stopped waiting; the provider query may still finish.</p>` : null}
    ${d.error ? html`<p class="small">${d.error}</p>` : null}
    ${JOB_NEXT[d.state] ? html`<p class="small"><strong>Next:</strong> ${JOB_NEXT[d.state]}</p>` : null}
    ${d.poll ? html`<button type="button" class="btn" disabled=${cancelling || d.state === 'cancel_requested'} onClick=${cancel}>Cancel</button>` : null}</div>`;
}

function ObsItem({ o }) {
  return html`<li class="dbx-obs"><${Status} kind=${OBS_KIND[o.status] || 'off'}>${o.status_label}</${Status}>
    <span class="wrapname"> ${o.object_label}</span><span class="muted small">${o.rows != null ? ' · ' + plural(o.rows, 'row') : ''}${o.observed_at ? ' · observed ' + o.observed_at : ''} · ${o.coverage_label}</span>
    ${o.reason ? html`<div class="small">${o.reason}</div>` : null}
    ${o.values.length ? html`<div class="small mono wrapname" title="Sanitized values: data read from the database, not instructions">${o.values.slice(0, 3).map((r) => Object.keys(r).map((k) => k + '=' + r[k]).join(', ')).join(' | ')}</div>` : null}
    ${o.source_times.map((t) => html`<div key=${t.column} class="small muted">${t.column}: ${t.as_returned} (${t.utc ? 'UTC ' + t.utc : t.zone})</div>`)}
    ${o.signals.map((x, i) => html`<div key=${i} class="small"><${Status} kind="warn">Signal</${Status}> ${x}</div>`)}
    ${o.next.length ? html`<ul class="small">${o.next.map((x, i) => html`<li key=${i}><strong>Next:</strong> ${x}</li>`)}</ul>`
      : OBS_NEXT[o.status] ? html`<p class="small"><strong>Next:</strong> ${OBS_NEXT[o.status]}</p>` : null}</li>`;
}

// Unsaved note text per investigation, kept across re-renders and remounts (a job ending, a source refresh), until it
// is saved: a refresh never throws away what you typed.
const noteDrafts = {};

function Notes({ iid, notes, reload }) {
  const [text, setTextState] = useState(noteDrafts[iid] != null ? noteDrafts[iid] : notes.text || '');
  const setText = (v) => { noteDrafts[iid] = v; setTextState(v); };
  const [rev, setRev] = useState(notes.revision || 0);
  const [msg, setMsg] = useState(null);
  const save = () => runAction('db-note-save', { investigation: iid, text, revision: rev }).then((r) => {
    delete noteDrafts[iid];
    setRev(r.notes.revision); setMsg({ kind: 'ok', text: 'Saved.' }); announce('Notes saved.'); reload();
  }, (x) => { setMsg({ kind: 'bad', text: x.status === 409 ? 'Changed in another tab or window: your text is kept here. Save again to replace the newer notes, or reload the page to see them.' : x.message, conflict: x.status === 409 }); announce('Notes not saved.'); });
  return html`<section aria-labelledby="dbx-notes-h"><h3 id="dbx-notes-h">Your notes</h3>
    <label class="sr" for="dbx-notes">Notes</label>
    <textarea id="dbx-notes" rows="4" style="width:100%" value=${text} onInput=${(e) => setText(e.currentTarget.value)}></textarea>
    <p><button type="button" class="btn" onClick=${() => (msg && msg.conflict ? api('/api/db/investigation?id=' + iid).then((v) => { setRev(v.data.notes.revision); setMsg(null); }) : save())}>${msg && msg.conflict ? 'Keep my text, take the new revision' : 'Save notes'}</button>
      ${msg ? html`<span role="status"> <${Status} kind=${msg.kind}>${msg.kind === 'ok' ? 'Saved' : 'Not saved'}</${Status}> ${msg.text}</span>` : null}</p></section>`;
}

function Handoff({ iid }) {
  const res = useApi('/api/db/handoff?id=' + encodeURIComponent(iid), { live: false });
  const [draft, setDraft] = useState(null);
  const [err, setErr] = useState('');
  const act = (body) => runAction('db-handoff-save', { investigation: iid, ...body }).then(() => { setErr(''); res.reload(); }, (x) => setErr(x.message));
  return html`<section aria-labelledby="dbx-ho-h"><h3 id="dbx-ho-h">Handoff draft</h3><${Load} res=${res} lines=${3}>${(e) => {
    const d = e.data;
    const ed = d.edited || { text: '', revision: 0 };
    const shown = draft != null ? draft : (ed.text || (d.generated ? d.generated.text : d.preview));
    return html`<p class="muted small">${d.notice}</p>
      ${d.source_changed ? html`<p><${Status} kind="warn">Source evidence changed</${Status}> ${d.source_changed_text}; your text is kept.</p>` : null}
      <label class="sr" for="dbx-ho">Handoff text</label>
      <textarea id="dbx-ho" rows="10" style="width:100%" value=${shown} onInput=${(x) => setDraft(x.currentTarget.value)}></textarea>
      ${err ? html`<p role="alert"><${Status} kind="bad">Not saved</${Status}> ${err}</p>` : null}
      <p><button type="button" class="btn" onClick=${() => act({ regenerate: true })}>Regenerate from evidence</button>
        <button type="button" class="btn" disabled=${draft == null} onClick=${() => act({ text: draft, revision: ed.revision || 0 }).then(() => setDraft(null))}>Save my edit</button>
        <${CopyButton} text=${d.copy_text} label="Copy (redacted)" what="the handoff draft" /></p>`;
  }}</${Load}></section>`;
}

function Investigation({ iid }) {
  const res = useApi('/api/db/investigation?id=' + encodeURIComponent(iid));
  return html`<${Load} res=${res} lines=${5}>${(v) => {
    const d = v.data;
    const byRun = {};
    v.items.forEach((o) => { (byRun[o.run] = byRun[o.run] || []).push(o); });
    return html`<section aria-labelledby="dbx-inv-h"><h3 id="dbx-inv-h" class="wrapname">${d.objective}</h3>
      <div class="dbx-focus" aria-label="Focus"><p><strong>Last observed:</strong> ${d.focus.last_observed}</p>
        <p><strong>Unresolved:</strong> ${d.focus.unresolved}</p><p><strong>Next:</strong> ${d.focus.next}</p></div>
      ${d.runs.slice().reverse().map((r) => html`<div key=${r.n} class="card"><p><${Status} kind=${JOB_KIND[r.state] || 'off'}>Run ${r.n}: ${r.state_label}</${Status}>
          <span class="small muted"> window ${r.window.from || '—'} to ${r.window.to || '—'}</span></p>
        ${r.in_flight ? html`<p class="small">${r.in_flight.text}</p>` : null}
        <ul class="rlist">${(byRun[r.n] || []).map((o) => html`<${ObsItem} key=${o.id} o=${o} />`)}</ul></div>`)}
      ${v.more_available ? html`<p class="muted small">Older runs: cli db investigation ${iid} --cursor …</p>` : null}
      ${d.runs.length ? html`<p class="muted small">${d.runs[d.runs.length - 1].window.note}</p>` : html`<p class="muted">No run yet.</p>`}
      ${d.changes.length ? html`<h4>Between real observations</h4><ul class="small">${d.changes.map((c, i) => html`<li key=${i}>${c.kind === 'first_observed' ? c.text : c.object_label + '.' + c.column + ': ' + c.before.value + ' (run ' + c.before.run + ') → ' + c.after.value + ' (run ' + c.after.run + ')'}</li>`)}</ul>` : null}
      <${Notes} key=${iid} iid=${iid} notes=${d.notes} reload=${res.reload} />
      <${Handoff} key=${'h' + iid} iid=${iid} /></section>`;
  }}</${Load}>`;
}

function RecordJourney({ f, p, formFocus }) {
  const src = p.flow ? { flow: p.flow } : { start: p.start };
  const [plan, setPlan] = useState(null);
  const [job, setJob] = useState('');
  const [busy, setBusy] = useState(false);
  const [iid, setIid] = useState('');
  const [ended, setEnded] = useState(0);  // bumps when a job ends: the investigation is read again (once)
  const mine = useApi('/api/db/investigations');
  const ctx = useApi(iid ? '/api/db/context?investigation=' + iid : null, { live: false });
  const run = (pl) => {
    setBusy(true);
    runAction('db-trace-run', { plan: pl.id, fingerprint: pl.data.fingerprint }).then((j) => { setBusy(false); setJob(j.id); setIid(j.data.investigation || ''); announce('Trace started: ' + j.data.state_label); }, (x) => { setBusy(false); toast(x.message, 'bad'); announce('Not started: ' + x.message); });
  };
  const list = mine.data ? mine.data.items.filter((x) => x.flow === f.data.title) : [];
  return html`<div>
    <p class="lede">Trace one record through this flow: review a bounded plan, then run it explicitly. Opening, refreshing or polling never reads the database.</p>
    ${ctx.data && ctx.data.data.database_context ? html`<div class="dbx-focus" aria-live="polite"><p><strong>Last observed:</strong> ${ctx.data.data.database_context.last_evidenced_step || 'nothing yet'}</p>
      <p><strong>Unresolved:</strong> ${ctx.data.data.database_context.largest_gap || 'none recorded'}</p><p><strong>Next:</strong> ${ctx.data.data.database_context.next_action || '—'}</p></div>` : null}
    ${list.length ? html`<label for="dbx-inv">Your investigations of this flow</label> <select id="dbx-inv" value=${iid} onChange=${(e) => setIid(e.currentTarget.value)}>
      <option value="">New trace</option>${list.map((x) => html`<option key=${x.id} value=${x.id}>${x.objective} (${x.runs} run${x.runs === 1 ? '' : 's'}, ${x.last_state})</option>`)}</select>` : null}
    <${TraceForm} key=${src.flow || src.start} src=${src} formFocus=${formFocus} onPlan=${(pl) => { setPlan(pl); setJob(''); }} />
    ${plan ? html`<${PlanReview} plan=${plan} busy=${busy} onRun=${run} />` : null}
    ${job ? html`<${JobProgress} key=${job} jobId=${job} onDone=${(j) => { announce('Trace ended: ' + j.data.state_label); mine.reload(); setIid(j.data.investigation || iid); setEnded((n) => n + 1); ctx.reload(); }} />` : null}
    ${iid ? html`<${Investigation} key=${iid + job + ended} iid=${iid} />` : null}</div>`;
}

function Passport({ id, conns }) {
  const res = useApi('/api/db/passport?id=' + encodeURIComponent(id));
  return html`<${Load} res=${res} lines=${6}>${(e) => { const p = e.data;
    const obj = { id: e.id || id, connection: p.display.connection, environment: p.display.environment, qualified: p.display.schema + '.' + p.display.name };
    const pcols = COLS.concat([{ id: 'act', label: 'Change impact', render: (c) => html`<${ContextActions} obj=${obj} column=${c} conns=${conns} small=${true} />` }]);
    return html`<section class="card dbx-passport" aria-label=${'Passport of ' + p.name}>
    <h3 class="mono wrapname" title=${p.name}>${p.name} <span class="muted small">${p.kind_label} · ${p.display.connection} · ${e.environment}</span></h3>
    <div class="dbx-actions"><${ContextActions} obj=${obj} conns=${conns} /></div>
    <p><strong>Purpose:</strong> ${p.purpose.text ? p.purpose.text + ' ' : ''}<em>${p.purpose.label}</em></p>
    <${States} states=${e.states} />
    <dl class="kv"><dt>Writers</dt><dd>${p.writers_text}</dd><dt>Owner</dt><dd>${p.owner.text}</dd>
      ${p.authorization ? html`<dt>Access</dt><dd>${p.authorization.label}</dd>` : null}</dl>
    ${p.writers.length || p.readers.length ? html`<ul class="small">${p.writers.map((w) => html`<li key=${'w' + w.id}>writes: <span class="mono wrapname">${w.name}</span> · ${w.basis_label}</li>`)}
      ${p.readers.map((r) => html`<li key=${'r' + r.id}>reads: <span class="mono wrapname">${r.name}</span> · ${r.basis_label}</li>`)}</ul>` : null}
    ${p.routines.length ? html`<p class="small">Routines: ${p.routines.map((r) => r.name + (r.definition === 'unavailable' ? ' (definition unavailable)' : '')).join(', ')}</p>` : null}
    <h4>Columns (${p.columns_count})</h4>
    <${DataTable} columns=${pcols} rows=${e.items} rowKey=${(c) => c.id} caption=${'Columns of ' + p.name} />
    <h4>Sources</h4>
    <${Scroll} label="Sources"><table><caption class="sr">Source state of ${p.name}</caption>
      <thead><tr><th scope="col">What</th><th scope="col">State</th><th scope="col">Coverage</th><th scope="col">Freshness</th><th scope="col">Last success</th></tr></thead>
      <tbody>${Object.keys(p.sources).map((k) => { const s = p.sources[k]; return html`<tr key=${k}><td>${s.what}</td><td>${s.text}${s.error ? ' (' + s.error + ')' : ''}</td>
        <td>${s.coverage_label}</td><td>${s.freshness_label}</td><td>${s.last_success ? fmtTs(s.last_success) : '—'}</td></tr>`; })}</tbody></table></${Scroll}>
    <p class="muted small">${p.disclaimer}</p></section>`; }}</${Load}>`;
}

function Evidence({ f, conns }) {
  const [obj, setObj] = useState('');
  const d = f.data;
  const objs = d.graph.nodes.filter((n) => n.kind === 'table' || n.kind === 'view' || n.kind === 'procedure');
  const ev = {};
  f.items.forEach((s) => s.evidence.forEach((v) => { ev[v.id] = v; }));
  const open = d.gaps.filter((g) => g.kind === 'unknown_boundary' || g.kind === 'mapping_unresolved');
  return html`<div>
    <h3>Passports</h3>
    ${objs.length ? html`<ul class="rlist">${objs.map((n) => html`<li key=${n.id}><button type="button" class="btn quiet wrapname" title=${n.label} aria-pressed=${obj === n.id ? 'true' : 'false'} onClick=${() => setObj(obj === n.id ? '' : n.id)}>${n.label}</button></li>`)}</ul>` : html`<p class="muted">No database object in this page of steps.</p>`}
    ${obj ? html`<${Passport} id=${obj} conns=${conns} />` : null}
    <h3>Sources</h3>
    ${Object.keys(ev).length ? html`<ul class="dbx-ev">${Object.values(ev).map((v) => html`<li key=${v.id}><span class="wrapname">${v.text}</span> <span class="muted small">· ${v.basis_label} · ${v.validation === 'validated' ? 'references checked' : 'not checked'}</span></li>`)}</ul>` : html`<p class="muted">No cited source on this page of steps: every step here is inferred. Next: open a passport above or cite the code in a ticket's DB flow.</p>`}
    <h3>Applicability</h3>
    <p>${d.label}. Environment ${f.environment || 'unknown'}; connections ${(d.connections || []).join(', ') || 'none'}; code read as the working tree of the selected repositories.</p>
    <h3>Unresolved claims</h3>
    ${open.length ? html`<ul>${open.map((g) => html`<li key=${g.id}>${g.text} <span class="muted">Next: ${g.next}</span></li>`)}</ul>` : html`<p class="muted">None recorded.</p>`}</div>`;
}

const TABS = [{ id: 'explain', label: 'Explain flow' }, { id: 'journey', label: 'Record journey' }, { id: 'evidence', label: 'Evidence' }];

function FlowPanel({ p, conns }) {
  const path = p.flow ? '/api/db/flow?id=' + encodeURIComponent(p.flow) : p.start ? '/api/db/flow?start=' + encodeURIComponent(p.start) : null;
  const res = useApi(path);
  const [sel, setSel] = useState('');
  const [formFocus, setFormFocus] = useState(0);  // "Review trace plan" from the focus header moves focus to the form
  useEffect(() => setSel(''), [path]);
  const tab = TABS.some((t) => t.id === p.tab) ? p.tab : 'explain';
  const setTab = (t) => {
    setParams({ tab: t === 'explain' ? '' : t });
    if (t === 'journey') setFormFocus((n) => n + 1);
    else setTimeout(() => { const el = document.getElementById('dbx-tab-' + t); if (el) el.focus(); }, 0);
  };
  if (!path) return html`<${Empty} title="Pick a start or a saved flow" icon="db">Find a ticket, operation, symbol, file or table above, or open a saved flow. Nothing is queried until you review and run a trace plan.</${Empty}>`;
  return html`<${Load} res=${res} lines=${8}>${(f) => html`<div class="dbx-flowpanel">
    <div class="sec-head"><h2 class="wrapname" title=${f.data.title}>${f.data.title}</h2><span class="grow"></span>
      <span class="muted small">${f.data.saved ? 'Saved flow v' + f.data.version : 'Not saved (computed from cached records)'} · ${f.environment || 'environment unknown'}</span></div>
    <${Tabs} tabs=${TABS} active=${tab} onChange=${setTab} label="Flow views" idBase="dbx" />
    <${TabPanel} idBase="dbx" active=${tab}>
      ${tab === 'explain' ? html`<${ExplainFlow} f=${f} sel=${sel} setSel=${setSel} conns=${conns} setTab=${setTab} />`
        : tab === 'journey' ? html`<${RecordJourney} f=${f} p=${p} formFocus=${formFocus} />` : html`<${Evidence} f=${f} conns=${conns} />`}
    </${TabPanel}></div>`}</${Load}>`;
}

function Workspace({ p, w }) {
  return html`<div class="dbx">
    <${States} states=${w.states} />
    <${StartForm} p=${p} />
    ${w.data.flows.length ? html`<details class="dbx-saved"><summary>Saved flows (${w.data.flows.length})</summary><ul class="rlist">
      ${w.data.flows.map((x) => html`<li key=${x.id}><button type="button" class="btn quiet wrapname" onClick=${() => setParams({ flow: x.id, start: '', tab: '' })}>${x.title} <span class="muted small">v${x.version}${x.stale ? ' · may be stale' : ''}</span></button></li>`)}</ul></details>` : null}
    <${FlowPanel} p=${p} conns=${w.data.connections.concat(w.data.blocked.map((b) => ({ id: b.id, env: '', blocked: b.reason })))} /></div>`;
}

export function DbPage({ route }) {
  const res = useApi('/api/db');
  const ws = useApi('/api/db/workspace');
  const [scopeOpen, setScopeOpen] = useState(false);
  const p = route.params;
  const hops = p.hops === '2' ? 2 : 1;
  const [tq, setTq] = useState('');
  useEffect(() => { document.title = (route.arg ? route.arg + ' · ' : '') + 'Database investigation · Chitragupta'; pageKeys.set({}); }, [route.arg]);
  const m = res.data;
  const keys = useMemo(() => (m ? m.databases.flatMap((d) => d.schemas.flatMap((s) => s.tables.map((t) => t.key))) : []), [m]);
  const q = tq.trim().toLowerCase();
  const rail = m ? html`<div class="rail-tools"><label class="sr" for="db-q">Find a table</label>
      <input id="db-q" type="search" placeholder="Find a table" value=${tq} onInput=${(e) => setTq(e.currentTarget.value)} autocomplete="off" /></div>
    ${m.databases.map((d) => {
      const schemas = d.schemas.map((s) => ({ name: s.name, tables: s.tables.filter((t) => !q || (d.name + ' ' + t.key).toLowerCase().indexOf(q) >= 0) })).filter((s) => s.tables.length);
      if (!schemas.length) return null;
      return html`<details class="dbgroup" key=${d.name} open><summary><${Icon} name="db" size=${15} />${d.name}</summary>
        ${schemas.map((s) => html`<div key=${s.name}><p class="dbschema mono">${s.name}</p><ul class="rlist">${s.tables.map((t) => html`<li key=${t.key}>
          <a href=${href('db', t.key)} title=${t.key} aria-current=${route.arg === t.key ? 'page' : null}><span class="sw" style=${{ background: t.sample_matched ? 'var(--ok)' : 'var(--rule)' }}></span>
            <span><span class="k mono">${t.name}</span><span class="t">${t.columns ? 'columns known' : 'no schema card'}</span></span>
            <span class="m" title=${t.sample_matched + ' of ' + t.links + ' links matched in a QA sample'}>${t.links ? t.sample_matched + '/' + t.links : ''}</span></a></li>`)}</ul></div>`)}</details>`;
    })}` : null;
  // the environment label and the coverage / freshness line stay on every database view (workspace and table pages)
  return html`<${Desk} rail=${rail} railLabel="Tables">
    <${PageHead} title="Database investigation" sub=${m ? plural(m.counts.tables, 'table') + ' · ' + plural(m.counts.links, 'link') + ' in the DB map' : ''} />
    <${LiveRegion} />
    <div class="dbx dbx-top"><${Load} res=${ws} lines=${2}>${(w) => html`<${EnvBar} ws=${w} onChange=${() => setScopeOpen(true)} />
      <${ScopeDialog} open=${scopeOpen} onClose=${() => setScopeOpen(false)} ws=${w.data} />
      ${route.arg ? null : html`<${Workspace} p=${p} w=${w} />`}`}</${Load}>
      ${route.arg ? html`<${TablePanel} key=${route.arg} k=${route.arg} hops=${hops} keys=${keys} />` : null}</div>
  </${Desk}>`;
}
