// Evidence graph: one ticket's requirements read from Jira, each with its three separate statuses (plan coverage,
// implementation, verification), the source line and the one next action (graphviews.summary via /api/graph).
// Read-only: api() only, never fetch. A 403 clears the page (the profile no longer includes the ticket).
// #/graph?ticket=<KEY>&req=<gr_…>
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
  return html`<div class="egx-list">
    <${Group} title="Requirements" rows=${live} sel=${sel} pick=${pick} open=${true} />
    <${Group} title="Not confirmed in a task baseline" rows=${unconfirmed} sel=${sel} pick=${pick} open=${!live.length} />
    <${Group} title="Removed from Jira" rows=${removed} sel=${sel} pick=${pick} open=${false} />
    ${more.next ? html`<p><button type="button" class="btn" disabled=${more.busy} onClick=${loadMore}>
      ${more.busy ? 'Loading…' : 'Load more'}</button> <span class="muted small">${rows.length} of ${env.total} shown</span></p>` : null}
    ${more.error ? html`<${Status} kind="bad">${more.error}</${Status}>` : null}
  </div>`;
}

function Summary({ env, ticket, sel, pick }) {
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
    <section class="sec egx-trace" aria-labelledby="egx-trace-h" data-egx-trace="placeholder">
      <h2 id="egx-trace-h">Trace</h2>
      ${/* The layered trace panel renders here (the next dashboard step). */ ''}
      <p class="muted">${sel ? 'The trace of the selected requirement is also available in Claude Code: /cg:graph ' + ticket + ' ' + sel
        : 'Select a requirement to see what supports it.'}</p>
    </section>`;
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
  const revoked = res.error && res.error.status === 403;
  const state = revoked ? 'revoked' : res.data && res.data.graph ? res.data.graph.state : '';
  const body = !ticket ? html`<${Empty} title="Choose a ticket">Pick one of your tickets to see its requirements and what supports them.</${Empty}>`
    : STATE_TEXT[state] ? html`<div class=${'egx-state egx-' + state}><${Empty} title=${STATE_TEXT[state][0]} icon=${state === 'revoked' ? 'off' : 'wait'}>${STATE_TEXT[state][1]}</${Empty}></div>`
      : html`<${Load} res=${res} lines=${8}>${(env) => html`<${Summary} env=${env} ticket=${ticket} sel=${sel} pick=${pick} />`}</${Load}>`;
  return html`<${Desk} className="egx-desk">
    <${PageHead} title="Evidence graph" sub=${ticket ? html`<${TicketLink} k=${ticket} />` : 'Requirements traced to plan, code, tests, pull requests and release'}>
      <${Picker} ticket=${ticket} />
    </${PageHead}>
    ${body}
  </${Desk}>`;
}
