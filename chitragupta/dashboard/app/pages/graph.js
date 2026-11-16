// Evidence graph: one ticket's requirements read from Jira, each with its three separate statuses (plan coverage,
// implementation, verification), the source line and the one next action (graphviews.summary via /api/graph).
// Selecting a requirement opens its trace: one column per layer (plan steps, code, commits, tests, pull requests,
// release), each card saying how it is linked (observed / declared / inferred), its details opening in place; the same
// trace as a nested list under "Show as text"; the ticket's other evidence; and what changed.
// Read-only: api() only, never fetch. A 403 clears the page (the profile no longer includes the ticket).
// #/graph?ticket=<KEY>&req=<gr_…>&basis=observed|declared|inferred&hist=1&gaps=1
import { useEffect, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { api, useApi, setParams, plural } from '../lib/core.js';
import { Status, Empty, Load, PageHead, DataTable, TicketLink } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

const KEY_RE = /^[A-Z][A-Z0-9]*-\d+$/;
// Every state has an icon and a word; colour is never the only signal.
const KIND = {
  plan_coverage: { Covered: 'ok', Partial: 'warn', Missing: 'bad', Unverified: 'info' },
  implementation: { Linked: 'ok', Partial: 'warn', 'Unverified association': 'warn', None: 'off' },
  verification: { 'Verified within recorded scope': 'ok', Partial: 'warn', Failed: 'bad', 'Historical only': 'warn',
    Unverified: 'info' },
};
const LABEL = { plan_coverage: 'Plan', implementation: 'Implementation', verification: 'Verification' };
const STATE_TEXT = {
  not_built: ['Not built yet', 'The background run builds the evidence graph from your cached records. Check back after the next run.'],
  rebuilding: ['Being rebuilt', 'Your profile changed, so the graph is being rebuilt for it. Nothing from before the change is shown.'],
  revoked: ['Not available', 'This ticket is outside your current profile, so nothing about it is shown.'],
};

export function StatusChip({ dim, st }) {
  const s = st || {};
  return html`<span class="egx-status" title=${(LABEL[dim] || dim) + ': ' + (s.reason || s.state || '')}>
    <${Status} kind=${(KIND[dim] || {})[s.state] || 'info'}>${s.state || 'Unknown'}</${Status}></span>`;
}

function SourceLine({ env }) {
  const g = env.graph || {}, s = env.snapshot || {};
  const src = s.state === 'current' ? 'Jira card read' : s.state === 'partial' ? 'Jira card read in part'
    : s.state === 'inaccessible' ? 'Jira card unavailable' : 'Jira card state unknown';
  return html`<p class="egx-source">
    <${Status} kind=${s.state === 'current' ? 'ok' : s.state === 'partial' ? 'warn' : 'off'} quiet=${true}>${src}${s.completeness ? ' (' + s.completeness + ')' : ''}</${Status}>
    <span>Graph snapshot ${g.snapshot == null ? '—' : g.snapshot}${g.built_at ? ', built ' + String(g.built_at).replace('T', ' ').slice(0, 16) : ''}</span>
    ${g.stale ? html`<${Status} kind="warn">Behind: the inputs changed since this was built; the next background run updates it</${Status}>` : null}
    ${(s.gaps || []).length ? html`<span class="muted">${plural(s.gaps.length, 'source gap')}: ${s.gaps[0]}</span>` : null}
  </p>`;
}

function columns(sel, pick) {
  return [
    { id: 'alias', label: 'Requirement', value: (r) => r.ordinal == null ? 1e9 : r.ordinal,
      render: (r) => html`<button type="button" class="egx-req" aria-pressed=${sel === r.id ? 'true' : 'false'} onClick=${() => pick(r.id)}>
        ${r.critical ? html`<span class="egx-critical" title="Needs attention first"><${Status} kind="bad">Critical</${Status}></span>` : null}
        <strong>${r.alias || r.id}</strong></button>` },
    { id: 'text', label: 'Wording', render: (r) => html`<span class="egx-text">${r.text}</span>` },
    { id: 'plan', label: 'Plan', render: (r) => html`<${StatusChip} dim="plan_coverage" st=${r.statuses.combined.plan_coverage} />` },
    { id: 'impl', label: 'Implementation', render: (r) => html`<${StatusChip} dim="implementation" st=${r.statuses.combined.implementation} />` },
    { id: 'ver', label: 'Verification', render: (r) => html`<${StatusChip} dim="verification" st=${r.statuses.combined.verification} />` },
  ];
}

function Group({ title, rows, sel, pick, open }) {
  if (!rows.length) return null;
  const table = html`<${DataTable} caption=${title} columns=${columns(sel, pick)} rows=${rows} rowKey=${(r) => r.id}
    rowClass=${(r) => (sel === r.id ? 'egx-selected' : null)} />`;
  return open ? html`<section class="sec egx-group"><h2>${title}</h2>${table}</section>`
    : html`<details class="more egx-group"><summary>${title} (${rows.length})</summary>${table}</details>`;
}

// Up / Down move between the requirement buttons (Enter selects: they are buttons).
function listKeys(e) {
  if ((e.key !== 'ArrowDown' && e.key !== 'ArrowUp') || !e.target.classList || !e.target.classList.contains('egx-req')) return;
  const all = Array.from(document.querySelectorAll('.egx-list .egx-req'));
  const next = all[all.indexOf(e.target) + (e.key === 'ArrowDown' ? 1 : -1)];
  if (next) { e.preventDefault(); next.focus(); }
}

function Requirements({ env, ticket, sel, pick }) {
  const [more, setMore] = useState({ rows: [], next: env.next_cursor, busy: false, error: '' });
  useEffect(() => setMore({ rows: [], next: env.next_cursor, busy: false, error: '' }), [env.snapshot_id]);
  const critical = new Set((env.critical || {}).ids || []);
  const rows = env.rows.concat(more.rows).map((r) => Object.assign({}, r, { critical: critical.has(r.id) }));
  const loadMore = () => {
    setMore((m) => Object.assign({}, m, { busy: true }));
    api('/api/graph?view=summary&ticket=' + encodeURIComponent(ticket) + '&cursor=' + encodeURIComponent(more.next)).then(
      (d) => setMore((m) => ({ rows: m.rows.concat(d.rows || []), next: d.next_cursor, busy: false, error: '' })),
      (e) => setMore((m) => Object.assign({}, m, { busy: false, error: e.message })));
  };
  const live = rows.filter((r) => r.status === 'current');
  const unconfirmed = rows.filter((r) => r.status === 'unconfirmed');
  const removed = rows.filter((r) => r.status === 'removed');
  return html`<div class="egx-list" onKeyDown=${listKeys}>
    <${Group} title="Requirements" rows=${live} sel=${sel} pick=${pick} open=${true} />
    <${Group} title="Not confirmed in a task baseline" rows=${unconfirmed} sel=${sel} pick=${pick} open=${!live.length} />
    <${Group} title="Removed from Jira" rows=${removed} sel=${sel} pick=${pick} open=${false} />
    ${more.next ? html`<p><button type="button" class="btn" disabled=${more.busy} onClick=${loadMore}>
      ${more.busy ? 'Loading…' : 'Load more'}</button> <span class="muted small">${rows.length} of ${env.total} shown</span></p>` : null}
    ${more.error ? html`<${Status} kind="bad">${more.error}</${Status}>` : null}
  </div>`;
}

// ------------------------------------------------------------------ the trace
const LAYERS = [['plan steps', 'Plan steps'], ['code changes', 'Code'], ['commits', 'Commits'], ['tests and runs', 'Tests'],
  ['pull requests', 'Pull requests'], ['release evidence', 'Release']];
const LINK = { ADDRESSED_BY: 'plan step for it', IMPLEMENTED_BY: 'implements it', COVERED_BY_TEST: 'test for it',
  SUPPORTED_BY: 'evidence for it', PART_OF_COMMIT: 'part of commit', EXECUTED_IN: 'run of the test', INCLUDED_IN_PR: 'in pull request',
  CHECKED_BY: 'CI check of the PR', HAS_RELEASE_EVIDENCE: 'release evidence', EQUIVALENT_TO: 'same patch as',
  CHERRY_PICKED_FROM: 'cherry-picked from', SQUASHED_INTO: 'squashed into' };
const BASES = [['', 'All links'], ['observed', 'Observed'], ['declared', 'Declared'], ['inferred', 'Inferred']];

function basisLine(e) {
  if (!e) return 'ticket-level; not linked to this requirement';
  return e.basis + (e.basis === 'inferred' && e.confidence != null ? ' · ' + e.confidence : '') + ' · ' + (LINK[e.type] || e.type);
}
// A gap: something that does not (or no longer) count as it stands.
function isGap(i) {
  const e = i.edge || {};
  return i.node.lifecycle !== 'current' || e.basis === 'inferred' || (!!e.applicability && e.applicability !== 'current');
}

function Card({ item, idx, open, toggle }) {
  const n = item.node, e = item.edge, id = 'egx-d-' + idx;
  const facts = [['Type', n.type.replace(/_/g, ' ')], ['Lifecycle', n.lifecycle.replace(/_/g, ' ')], ['Source', n.source_ref]];
  if (e) facts.push(['Link', e.type + ' (' + e.basis + (e.confidence != null ? ', confidence ' + e.confidence : '') + ')'],
    ['Evidence', e.evidence_ref], ['Applies', e.applicability ? e.applicability + (e.reason ? ' (' + e.reason + ')' : '') : ''],
    ['Declared by', (e.declarations || []).map((d) => d.by + (d.at ? ' at ' + String(d.at).replace('T', ' ').slice(0, 16) : '')).join('; ')]);
  Object.keys(n.attrs || {}).forEach((k) => {
    const v = n.attrs[k];
    if (v != null && v !== '' && typeof v !== 'object') facts.push([k.replace(/_/g, ' '), String(v)]);
    else if (v && typeof v === 'object' && v.state) facts.push([k.replace(/_/g, ' '), v.state + (v.text ? ': ' + v.text : '')]);
  });
  return html`<li class=${'egx-card' + (e && e.basis === 'inferred' ? ' egx-inferred' : '') + (open ? ' open' : '')}>
    <button type="button" class="egx-card-btn" aria-expanded=${open ? 'true' : 'false'} aria-controls=${id} onClick=${toggle}>
      <span class="egx-card-label">${n.label}</span>
      <span class="egx-basis">${basisLine(e)}</span>
      ${e && e.basis === 'inferred' ? html`<${Status} kind="warn">Inferred: a suggestion, not counted</${Status}>` : null}
      ${n.lifecycle !== 'current' ? html`<${Status} kind="warn">${n.lifecycle.replace(/_/g, ' ')}</${Status}>` : null}
    </button>
    <dl id=${id} class="kv egx-detail" hidden=${!open}>${facts.filter((x) => x[1]).map((x) => [html`<dt key=${'t' + x[0]}>${x[0]}</dt>`, html`<dd key=${'d' + x[0]}>${x[1]}</dd>`])}</dl>
  </li>`;
}

function Filters({ f, setF }) {
  return html`<div class="egx-filters" role="group" aria-label="Trace filters">
    <div class="seg" role="group" aria-label="Link basis">${BASES.map(([v, label]) => html`<button key=${v || 'all'} type="button"
      class="btn quiet" aria-pressed=${(f.basis || '') === v ? 'true' : 'false'} onClick=${() => setF({ basis: v })}>${label}</button>`)}</div>
    <label class="egx-check"><input type="checkbox" checked=${!!f.hist} onChange=${(e) => setF({ hist: e.currentTarget.checked ? '1' : '' })} /> Include historical</label>
    <label class="egx-check"><input type="checkbox" checked=${!!f.gaps} onChange=${(e) => setF({ gaps: e.currentTarget.checked ? '1' : '' })} /> Only gaps</label>
  </div>`;
}

function TextTrace({ req, layers, other }) {
  return html`<ul class="egx-textlist">
    <li><strong>Requirement ${req.alias || req.id}</strong> (${req.status}): ${req.text}</li>
    ${layers.map(([layer, title, items]) => html`<li key=${layer}>${title}: ${items.length ? '' : 'nothing recorded'}
      ${items.length ? html`<ul>${items.map((i) => html`<li key=${i.node.id}>${i.node.label} — ${basisLine(i.edge)}${i.node.lifecycle !== 'current' ? ' (' + i.node.lifecycle.replace(/_/g, ' ') + ')' : ''}</li>`)}</ul>` : null}</li>`)}
    <li>Other evidence on this ticket: ${other.length ? '' : 'none'}${other.length ? html`<ul>${other.map((i) => html`<li key=${i.node.id}>${i.node.label} (${i.node.type.replace(/_/g, ' ')})</li>`)}</ul>` : null}</li>
  </ul>`;
}

function Trace({ ticket, req, f, setF }) {
  const path = '/api/graph?view=trace&ticket=' + encodeURIComponent(ticket) + '&requirement=' + encodeURIComponent(req)
    + (f.basis ? '&basis=' + f.basis : '') + (f.hist ? '&history=1' : '');
  const res = useApi(path);
  const [more, setMore] = useState({ items: [], next: null, busy: false });
  const [open, setOpen] = useState('');
  const [asText, setAsText] = useState(false);
  const env = res.data;
  useEffect(() => { setMore({ items: [], next: null, busy: false }); setOpen(''); }, [path, env && env.snapshot_id]);
  const close = (e) => {   // Escape closes the open card and returns focus to it
    if (e.key !== 'Escape' || !open) return;
    const btn = document.querySelector('[aria-controls="' + open + '"]');
    setOpen('');
    if (btn) btn.focus();
  };
  const loadMore = () => {
    const cursor = more.next || env.next_cursor;
    setMore((m) => Object.assign({}, m, { busy: true }));
    api(path + '&cursor=' + encodeURIComponent(cursor)).then(
      (d) => setMore((m) => ({ items: m.items.concat(d.items || []), next: d.next_cursor, busy: false })),
      () => setMore((m) => Object.assign({}, m, { busy: false })));
  };
  const body = (d) => {
    const all = d.items.concat(more.items);
    const linked = all.filter((i) => i.layer !== 'ticket evidence');
    const other = all.filter((i) => i.layer === 'ticket evidence');
    const shown = f.gaps ? linked.filter(isGap) : linked;
    const layers = LAYERS.map(([layer, title]) => [layer, title, shown.filter((i) => i.layer === layer)]);
    const next = more.items.length ? more.next : d.next_cursor;
    const r = d.requirement;
    let idx = 0;
    const card = (i) => { const id = 'egx-d-' + idx; idx += 1; return html`<${Card} key=${i.node.id} item=${i} idx=${idx - 1} open=${open === id} toggle=${() => setOpen(open === id ? '' : id)} />`; };
    return html`<div class="egx-columns" onKeyDown=${close}>
        <section class="egx-col egx-col-req" aria-label="Requirement"><h3>Requirement</h3>
          <p><strong>${r.alias || r.id}</strong> · ${r.status}</p><p class="egx-text">${r.text}</p>
          <p class="muted small">${plural((r.revisions || []).length, 'wording')} shown${(r.supersedes || []).length ? ', reworded ' + plural(r.supersedes.length, 'time') : ''}</p></section>
        ${layers.map(([layer, title, items]) => html`<section key=${layer} class="egx-col" aria-label=${title}>
          <h3>${title} <span class="muted small">${(d.totals || {})[layer] || 0}</span></h3>
          ${items.length ? html`<ul class="egx-cards">${items.map(card)}</ul>` : html`<p class="muted small">Nothing recorded</p>`}</section>`)}
      </div>
      ${next ? html`<p><button type="button" class="btn" disabled=${more.busy} onClick=${loadMore}>${more.busy ? 'Loading…' : 'Load more of the trace'}</button>
        <span class="muted small">${all.length} of ${d.total} shown</span></p>` : null}
      <p><button type="button" class="btn quiet" aria-expanded=${asText ? 'true' : 'false'} aria-controls="egx-text-trace" onClick=${() => setAsText(!asText)}>Show as text</button></p>
      <div id="egx-text-trace" hidden=${!asText}><${TextTrace} req=${r} layers=${layers} other=${other} /></div>
      <details class="more egx-other" onKeyDown=${close}><summary>Other evidence on this ticket (${other.length})</summary>
        ${other.length ? html`<ul class="egx-cards">${other.map(card)}</ul>` : html`<p class="muted small">None recorded beyond what is linked above.</p>`}</details>`;
  };
  return html`<${Filters} f=${f} setF=${setF} />
    ${res.error && res.error.status !== 403 ? html`<${Status} kind="bad">${res.error.message}</${Status}>` : html`<${Load} res=${res} lines=${5}>${body}</${Load}>`}`;
}

function Changes({ ticket }) {
  const [since, setSince] = useState('');
  const [asked, setAsked] = useState('');
  const res = useApi('/api/graph?view=changes&ticket=' + encodeURIComponent(ticket) + (asked ? '&since=' + asked : ''));
  return html`<details class="more egx-changes"><summary>What changed</summary>
    <form class="egx-since" onSubmit=${(e) => { e.preventDefault(); setAsked(/^\d+$/.test(since) ? since : ''); }}>
      <label for="egx-since">Since journal position</label>
      <input id="egx-since" inputmode="numeric" value=${since} placeholder="latest change" onInput=${(e) => setSince(e.currentTarget.value)} />
      <button type="submit" class="btn quiet">Show</button></form>
    <${Load} res=${res} lines=${3}>${(d) => d.baseline !== 'ok'
      ? html`<${Status} kind="warn">${'Changes unavailable: ' + (d.reason || 'unknown')}</${Status}>`
      : html`<p class="muted small">After ${d.from} up to ${d.to} (${plural(d.entries, 'entry', 'entries')})</p>
        ${d.items.length ? html`<ul class="egx-changelist">${d.items.map((i, n) => html`<li key=${n}><strong>${i.category}</strong>: ${i.kind || ''}
          ${i.label || (i.id ? String(i.id).slice(0, 12) : '')}${i.from !== undefined ? ' ' + (i.from || '—') + ' → ' + (i.to || '—') : i.state ? ' (' + i.state + ')' : ''}</li>`)}</ul>`
        : html`<p class="muted small">Nothing changed in this window.</p>`}`}</${Load}>
  </details>`;
}

function Summary({ env, ticket, sel, pick, f, setF }) {
  const r = env.requirements || {};
  const ver = (env.counts || {}).verification || {};
  const counted = r.total - r.removed - r.excluded;
  const allVerified = counted > 0 && (ver['Verified within recorded scope'] || 0) === counted;
  return html`
    <${SourceLine} env=${env} />
    <p class="egx-next"><strong>Next action:</strong> ${env.next_action}</p>
    ${!r.total ? html`<${Empty} title="No requirements yet">${'No requirements were read from the Jira card yet (' + ((env.snapshot || {}).state || 'unknown') + '). They appear after the card is read and the background run builds the graph.'}</${Empty}>`
      : html`
        ${allVerified ? html`<${Status} kind="ok">Every requirement is verified within its recorded scope</${Status}>` : null}
        <p class="muted small">${plural(r.total, 'requirement')}: ${r.current} confirmed in a task, ${r.unconfirmed} not confirmed, ${r.removed} removed, ${r.excluded} excluded.
          Plan coverage, implementation and verification are separate measures.</p>
        <${Requirements} env=${env} ticket=${ticket} sel=${sel} pick=${pick} />`}
    <section class="sec egx-trace" aria-labelledby="egx-trace-h">
      <h2 id="egx-trace-h">Trace</h2>
      ${sel ? html`<${Trace} key=${sel} ticket=${ticket} req=${sel} f=${f} setF=${setF} />`
        : html`<p class="muted">Select a requirement to see what supports it, layer by layer.</p>`}
    </section>
    <${Changes} ticket=${ticket} />`;
}

function Picker({ ticket }) {
  const res = useApi('/api/tickets');
  const list = ((res.data && res.data.tickets) || []).slice(0, 200);
  return html`<label class="egx-picker">Ticket
    <select value=${ticket} onChange=${(e) => setParams({ ticket: e.currentTarget.value, req: '' })}>
      <option value="">Choose a ticket</option>
      ${list.map((t) => html`<option key=${t.key} value=${t.key}>${t.key}${t.title ? ' · ' + t.title : ''}</option>`)}
    </select></label>`;
}

export function GraphPage({ route }) {
  const ticket = KEY_RE.test(route.params.ticket || '') ? route.params.ticket : '';
  const sel = route.params.req || '';
  const res = useApi(ticket ? '/api/graph?view=summary&ticket=' + encodeURIComponent(ticket) : null);
  useEffect(() => { document.title = 'Evidence graph · Chitragupta'; }, []);
  useEffect(() => () => pageKeys.set({}), []);
  const pick = (id) => setParams({ req: id === sel ? '' : id });
  const f = { basis: ['observed', 'declared', 'inferred'].indexOf(route.params.basis) >= 0 ? route.params.basis : '',
    hist: route.params.hist === '1', gaps: route.params.gaps === '1' };
  const setF = (patch) => setParams(patch);   // in the URL: a filter never loses the selection or reloads the page
  const revoked = res.error && res.error.status === 403;
  const state = revoked ? 'revoked' : res.data && res.data.graph ? res.data.graph.state : '';
  const body = !ticket ? html`<${Empty} title="Choose a ticket">Pick one of your tickets to see its requirements and what supports them.</${Empty}>`
    : STATE_TEXT[state] ? html`<div class=${'egx-state egx-' + state}><${Empty} title=${STATE_TEXT[state][0]} icon=${state === 'revoked' ? 'off' : 'wait'}>${STATE_TEXT[state][1]}</${Empty}></div>`
      : html`<${Load} res=${res} lines=${8}>${(env) => html`<${Summary} env=${env} ticket=${ticket} sel=${sel} pick=${pick} f=${f} setF=${setF} />`}</${Load}>`;
  return html`<${Desk} className="egx-desk">
    <${PageHead} title="Evidence graph" sub=${ticket ? html`<${TicketLink} k=${ticket} />` : 'Requirements traced to plan, code, tests, pull requests and release'}>
      <${Picker} ticket=${ticket} />
    </${PageHead}>
    ${body}
  </${Desk}>`;
}
