// Database workspace pieces shared by pages/db.js (kept apart so db.js stays readable): the persistent environment
// bar, the focus header, and the contextual Known impacts / Compare metadata panels. Every read here is a GET of a
// cached projection (/api/db/impact, /api/db/compare, /api/db/context): no query, no model, no ingestion. Computing an
// impact or a comparison is an explicit protected action (db-impact-compute, db-compare-compute) over cached metadata.
import { useState } from '/vendor/hooks.module.js';
import { html } from './html.js';
import { useApi, runAction, fmtTs } from './core.js';
import { Status, Dialog, Scroll, Chip } from './ui.js';

// One polite live region per page: short announcements of what changed (computed, planned, failed, revoked).
let announceEl = null;
export function announce(text) {
  if (!announceEl) announceEl = document.getElementById('dbx-live');
  if (!announceEl) return;
  announceEl.textContent = '';
  setTimeout(() => { if (announceEl) announceEl.textContent = text; }, 30);
}
export function LiveRegion() {
  return html`<p id="dbx-live" class="sr" role="status" aria-live="polite"></p>`;
}

const STATE_KIND = { permission_denied: 'bad', disconnected_source: 'bad', blocked_comparison: 'bad', scope_changed: 'warn' };
// A state from a view envelope: its title, what it means here and the specific next step (never a bare "No data").
export function StateList({ states, label }) {
  if (!states || !states.length) return null;
  return html`<ul class="dbx-states" aria-label=${label || 'What needs attention'}>${states.map((s) => html`<li key=${s.id + s.text}>
    <${Status} kind=${STATE_KIND[s.id] || 'warn'}>${s.title}</${Status}>
    <span>${s.text !== s.title ? String(s.text).replace(/[.]$/, '') + '. ' : ''}<strong>Next:</strong> ${s.next}</span></li>`)}</ul>`;
}

// The persistent header of every database view: environment label of the approved scope, Change (explains the scope;
// editing it lives in Settings / setup), and the coverage / freshness line.
export function EnvBar({ ws, onChange }) {
  const d = ws.data;
  return html`<div class="dbx-head" role="group" aria-label="Environment and coverage">
    <${Chip} icon="db" title="Environment of the approved scope">${d.environment}</${Chip}>
    <button type="button" class="btn quiet" onClick=${onChange} aria-haspopup="dialog">Change</button>
    <span class="muted small dbx-cov">${d.coverage_line}</span></div>`;
}

const GAP_ORDER = ['unknown_boundary', 'runtime_events_unavailable', 'mapping_unresolved', 'permission_denied', 'definition_unavailable', 'dynamic_sql', 'deployed_revision_unknown'];
function largestGap(gaps) {
  const g = (gaps || []).slice().sort((a, b) => (GAP_ORDER.indexOf(a.kind) + 99) % 99 - (GAP_ORDER.indexOf(b.kind) + 99) % 99)[0];
  return g || null;
}

// Last observed / unresolved / next action of a flow: from your newest investigation of it (its database_context,
// a cached read) or, before any trace, from the flow's own evidence and gaps.
export function FocusHeader({ f, onEvidence, onPlan }) {
  const mine = useApi('/api/db/investigations', { live: false });
  const inv = mine.data ? (mine.data.items || []).find((x) => x.flow === f.data.title || x.flow_id === f.id) : null;
  const ctx = useApi(inv ? '/api/db/context?investigation=' + encodeURIComponent(inv.id) : null, { live: false });
  const c = ctx.data && ctx.data.data ? ctx.data.data.database_context : null;
  const gap = largestGap(f.data.gaps);
  const cited = f.items.filter((s) => s.basis !== 'inferred' && s.stage != null);
  const last = c && c.last_evidenced_step ? c.last_evidenced_step
    : 'No record traced yet' + (cited.length ? '; last cited step: ' + cited[cited.length - 1].label : '');
  const unresolved = c && c.largest_gap ? c.largest_gap : gap ? gap.label + ': ' + gap.text : 'Nothing unresolved is recorded on this page of steps.';
  const next = c && c.next_action ? c.next_action : gap ? gap.next : 'Review a trace plan to observe one record.';
  return html`<section class="dbx-focus" aria-label="Focus">
    <p><strong>Last observed:</strong> <span class="wrapname">${last}</span>${c && c.updated_at ? html` <span class="muted small">(${fmtTs(c.updated_at)})</span>` : null}</p>
    <p><strong>Unresolved:</strong> <span class="wrapname">${unresolved}</span></p>
    <p><strong>Next:</strong> <span class="wrapname">${next}</span></p>
    <div class="dbx-actions"><button type="button" class="btn" onClick=${onEvidence}>View evidence</button>
      <button type="button" class="btn" onClick=${onPlan}>Review trace plan</button></div></section>`;
}

const CHANGES = [['', 'Any change'], ['rename', 'Rename'], ['type', 'Type change'], ['nullability', 'Nullability change'],
  ['default', 'Default change'], ['semantic', 'Meaning change'], ['application_only', 'Application-only change']];

function ImpactItem({ it }) {
  const flags = [].concat(it.stale ? ['Stale: ' + it.stale_reasons.join('; ')] : [], it.unavailable ? ['Unavailable: ' + it.unavailable_reasons.join('; ')] : []);
  return html`<li class="dbx-imp">
    <div class="dbx-imp-h"><${Status} kind=${it.basis === 'direct' ? 'ok' : 'off'}>${it.basis_label}</${Status}>
      <span class="dbx-kind">${it.relation_label}${(it.also || []).map((a) => ' + ' + a.relation_label.toLowerCase()).join('')}</span>
      <span class="wrapname mono small" title=${it.label}>${it.label}</span></div>
    ${it.evidence_label || it.cite ? html`<div class="muted small wrapname">${it.evidence_label || ''}${it.cite ? ' · ' + it.cite : ''}</div>` : null}
    <div class="small"><strong>Why:</strong> ${it.why}</div>
    <div class="small"><strong>Suggested check:</strong> ${it.validation.text} <span class="muted">(${it.validation.rationale})</span></div>
    ${flags.length ? html`<div class="small"><${Status} kind="warn">Check the source</${Status}> ${flags.join(' · ')}</div>` : null}</li>`;
}

// Known impacts of a change to one object or column: the cached projection (direct evidence apart from inferred
// association, limits, unknown coverage, suggested checks as text). Computing it is an explicit protected action
// over cached metadata; nothing is ever run.
export function ImpactPanel({ target, label }) {
  const [change, setChange] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const path = '/api/db/impact?target=' + encodeURIComponent(target) + (change ? '&change=' + change : '');
  const res = useApi(path, { live: false });
  const compute = () => {
    setBusy(true); setErr('');
    runAction('db-impact-compute', { target, change: change || undefined }).then(() => { setBusy(false); res.reload(); announce('Impact computed from cached metadata.'); },
      (x) => { setBusy(false); setErr(x.message); announce('Impact not computed: ' + x.message); });
  };
  const e = res.data;
  const d = e ? e.data : null;
  const stale = e && (e.state === 'stale_result' || e.state === 'scope_changed');
  return html`<div class="dbx-panel">
    <p class="small muted">Known impact of a change to <span class="mono wrapname">${label}</span>: what recorded evidence links to it. Suggestions only; nothing is run.</p>
    <label for="dbx-change">Kind of change</label>
    <select id="dbx-change" value=${change} onChange=${(x) => setChange(x.currentTarget.value)}>${CHANGES.map(([v, l]) => html`<option key=${v} value=${v}>${l}</option>`)}</select>
    ${res.error ? html`<p role="alert"><${Status} kind="bad">Not available</${Status}> ${res.error.message}</p>` : null}
    ${!e ? (res.error ? null : html`<p class="muted small" aria-busy="true">Reading the cached result…</p>`) : html`<div aria-live="polite" data-change=${(d && d.change && d.change.kind) || "any"}>
      <${StateList} states=${e.states} label="Impact state" />
      ${e.state === 'not_computed' || stale ? html`<p><button type="button" class="btn primary" disabled=${busy} onClick=${compute}>${busy ? 'Computing…' : (stale ? 'Recompute' : 'Compute') + ' from cached metadata'}</button>
        <span class="muted small"> No query, no model: the recorded graph only.</span></p>` : null}
      ${err ? html`<p role="alert"><${Status} kind="bad">Not computed</${Status}> ${err}</p>` : null}
      ${e.state !== 'not_computed' && d.headline ? html`<p class="dbx-headline"><strong>${d.headline}</strong>${d.computed_at ? html` <span class="muted small">· computed ${fmtTs(d.computed_at)}</span>` : null}</p>
        ${d.note ? html`<p class="small">${d.note}</p>` : null}
        ${e.items.length ? html`<h4>Affected (${e.page.total})</h4><ul class="dbx-imps">${e.items.map((it) => html`<${ImpactItem} key=${it.id || it.key} it=${it} />`)}</ul>` : null}
        ${e.more_available ? html`<p class="muted small">More items: cli db impact ${d.target.id} --cursor …</p>` : null}
        ${d.excluded && d.excluded.length ? html`<details class="small"><summary>Not affected by this kind of change (${d.excluded.length})</summary><ul>${d.excluded.map((x, i) => html`<li key=${i}><span class="wrapname">${x.relation_label} ${x.label}</span>: ${x.reason}</li>`)}</ul></details>` : null}
        <h4>Coverage and limits</h4><ul class="small">${[d.limits && d.limits.text].concat(d.coverage || []).filter(Boolean).map((c, i) => html`<li key=${i} class="wrapname">${c}</li>`)}</ul>
        <p class="muted small">${d.suggest_note}</p>` : null}</div>`}</div>`;
}

const CAT_KIND = { added: 'info', removed: 'warn', changed: 'warn', unchanged: 'ok', not_comparable: 'off', unavailable: 'off' };
const SCHEMA_ONLY = 'Matching schema says nothing about data or deployed code.';

function sideText(s) {
  return s.ref + ' (' + String(s.env || '?').toUpperCase() + ', ' + (s.engine || 'engine unknown') + ') · ' + (s.source === 'snapshot' ? 'snapshot ' : 'metadata as of ') + (s.taken_at ? fmtTs(s.taken_at) : 'unknown');
}

// Compare metadata of one object between its connection and another permitted one (or two snapshots): both snapshot
// times, the six categories, a blocked side with its reason, pairs only through an explicit confirmation.
export function ComparePanel({ obj, conns }) {
  const left = obj.connection;
  const others = conns.filter((c) => c.id !== left);  // approved ones, then the blocked ones (they answer with why)
  const [right, setRight] = useState((others.find((c) => !c.blocked && c.env !== obj.environment) || others.find((c) => !c.blocked) || {}).id || '');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const name = obj.qualified;
  const res = useApi(right ? '/api/db/compare?left=' + encodeURIComponent(left) + '&right=' + encodeURIComponent(right) + '&objects=' + encodeURIComponent(name) : null, { live: false });
  const act = (confirm) => {
    setBusy(true); setErr('');
    runAction('db-compare-compute', { left, right, objects: [name], confirm: confirm ? [confirm] : undefined }).then(() => { setBusy(false); res.reload(); announce(confirm ? 'Pair confirmed; comparison computed.' : 'Comparison computed from cached metadata.'); },
      (x) => { setBusy(false); setErr(x.message); announce('Not compared: ' + x.message); });
  };
  const e = res.data;
  const d = e ? e.data : null;
  return html`<div class="dbx-panel">
    <p class="small muted">Compare the recorded metadata of <span class="mono wrapname">${name}</span> on ${left} with another permitted connection. ${SCHEMA_ONLY}</p>
    ${others.some((c) => !c.blocked) ? null : html`<p><${Status} kind="warn">No other permitted connection</${Status}> Comparing needs a second approved connection: approving one happens in /cg:setup or the config file (mcp.database.scope, qa_data.allowed_envs).</p>`}
    ${others.length ? html`<label for="dbx-right">Compare with</label>
      <select id="dbx-right" value=${right} onChange=${(x) => setRight(x.currentTarget.value)}>${right ? null : html`<option value="">Choose…</option>`}${others.map((c) => html`<option key=${c.id} value=${c.id}>${c.id} (${c.blocked ? 'not permitted' : c.env.toUpperCase()})</option>`)}</select>` : null}
    ${res.error ? html`<p role="alert"><${Status} kind="bad">Not available</${Status}> ${res.error.message}</p>` : null}
    ${e ? html`<div aria-live="polite">
      <${StateList} states=${e.states} label="Comparison state" />
      ${d.blocked ? html`<p><${Status} kind="bad">Blocked</${Status}> The ${d.blocked.side} side (${d.blocked.connection}) is not permitted: ${d.blocked.reason}. Nothing here widens or relabels it.</p>` : null}
      ${!d.blocked && (e.state === 'not_computed' || e.state === 'stale_result' || e.state === 'scope_changed') ? html`<p><button type="button" class="btn primary" disabled=${busy} onClick=${() => act(null)}>${busy ? 'Comparing…' : 'Compare cached metadata'}</button>
        <span class="muted small"> No query: the recorded metadata only.</span></p>` : null}
      ${err ? html`<p role="alert"><${Status} kind="bad">Not compared</${Status}> ${err}</p>` : null}
      ${d.categories ? html`<dl class="kv small"><dt>Left</dt><dd class="wrapname">${sideText(d.left)}</dd><dt>Right</dt><dd class="wrapname">${sideText(d.right)}</dd></dl>
        <ul class="dbx-cats" aria-label="Categories">${Object.keys(d.categories).map((k) => html`<li key=${k}><${Status} kind=${d.categories[k] ? CAT_KIND[k] : 'off'} quiet=${!d.categories[k]}>${k.replace('_', ' ')} ${d.categories[k]}</${Status}></li>`)}</ul>
        ${e.items.length ? html`<ul class="dbx-imps">${e.items.map((it) => html`<li key=${it.key} class="dbx-imp"><div class="dbx-imp-h"><${Status} kind=${CAT_KIND[it.category] || 'off'}>${it.category_label || it.category}</${Status}>
            <span class="mono small wrapname">${it.key}</span></div><div class="small">${it.reason || ''}</div>
          ${(it.differences || []).length ? html`<${Scroll} label="Differences"><table class="small"><caption class="sr">Differences of ${it.key}</caption><thead><tr><th scope="col">Column</th><th scope="col">What</th><th scope="col">${d.left.ref}</th><th scope="col">${d.right.ref}</th></tr></thead>
            <tbody>${it.differences.map((x, i) => html`<tr key=${i}><td class="mono">${x.column || '—'}</td><td>${x.field}</td><td class="mono">${String(x.left)}</td><td class="mono">${String(x.right)}</td></tr>`)}</tbody></table></${Scroll}>` : null}
          ${it.candidate ? html`<p class="small"><button type="button" class="btn" disabled=${busy} onClick=${() => act(it.key)}>Confirm ${it.key} is the same object on both</button> <span class="muted">(recorded as your confirmation, self-reported)</span></p>` : null}</li>`)}</ul>` : null}
        <ul class="small muted">${(d.notes || []).map((n, i) => html`<li key=${i}>${n}</li>`)}</ul>` : null}
      <p class="small"><strong>${SCHEMA_ONLY}</strong></p></div>` : null}</div>`;
}

// The contextual actions of a flow step, a passport or a column: Known impacts and Compare metadata open in a dialog
// over the cached projections (never home-page cards).
export function ContextActions({ obj, column, conns, small }) {
  const [open, setOpen] = useState('');
  const what = column ? obj.qualified + '.' + column.name : obj.qualified;
  return html`<span class="dbx-ctx">
    <button type="button" class=${'btn' + (small ? ' quiet' : '')} aria-haspopup="dialog" onClick=${() => setOpen('impact')}>Known impacts${small ? html`<span class="sr"> of ${what}</span>` : null}</button>
    ${column ? null : html`<button type="button" class="btn" aria-haspopup="dialog" onClick=${() => setOpen('compare')}>Compare metadata</button>`}
    <${Dialog} open=${!!open} title=${(open === 'compare' ? 'Compare metadata · ' : 'Known impacts · ') + what} onClose=${() => setOpen('')} wide=${true}>
      ${open === 'impact' ? html`<${ImpactPanel} target=${column ? column.id : obj.id} label=${what} />` : null}
      ${open === 'compare' ? html`<${ComparePanel} obj=${obj} conns=${conns || []} />` : null}</${Dialog}></span>`;
}

// {id, connection, environment, qualified} of a graph / passport object (its label carries "(connection)").
export function objRef(n) {
  return { id: n.id, connection: n.connection || '', environment: n.environment || '', qualified: String(n.label || '').replace(/\s*\([^)]*\)\s*$/, '') };
}
