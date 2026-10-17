// Knowledge: the DB map explorer (search a table, see its links and which the QA link map verified, zoom and pan),
// the API catalog with filters, and integrations.
import { useEffect, useMemo, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, setParams, fmtDay, plural } from '../lib/core.js';
import { Icon, Status, Empty, Load, PageHead, TicketLink, Chip, Tabs, TabPanel, DataTable, Diagram, Scroll } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

const TABS = [{ id: 'db', label: 'DB map' }, { id: 'apis', label: 'APIs' }, { id: 'integrations', label: 'Integrations' }];

function short(name) { return String(name || '').split('.').slice(-1)[0]; }
function sameTable(a, b) { return a === b || short(a) === short(b); }
function lbl(s) { return '"' + String(s).replace(/"/g, '#quot;') + '"'; }
function mapSource(tables, rels, focus) {
  const names = [];
  const id = (n) => { let i = names.findIndex((x) => sameTable(x, n)); if (i < 0) { names.push(n); i = names.length - 1; } return 'n' + i; };
  const lines = ['flowchart LR'];
  const use = focus ? rels.filter((r) => sameTable(r.from, focus) || sameTable(r.to, focus)) : rels;
  if (focus) id(focus);
  else tables.forEach((t) => id(t.name));
  use.forEach((r) => { lines.push('  ' + id(r.from) + (r.verified ? ' -->|' : ' -.->|') + lbl(r.kind || 'link') + '| ' + id(r.to)); });
  names.forEach((n, i) => lines.splice(1 + i, 0, '  n' + i + '[' + lbl(focus ? n : short(n)) + ']'));
  if (focus) lines.push('  style n0 stroke-width:3px');
  return lines.join('\n');
}

function RelTable({ rels, focus }) {
  if (!rels.length) return html`<${Empty} title="No links recorded for this table yet" icon="db" />`;
  return html`<${Scroll} label="Links"><table><caption class="sr">Links of ${focus || 'all tables'}</caption>
    <thead><tr><th scope="col">From</th><th scope="col">To</th><th scope="col">Kind</th><th scope="col">Checked</th><th scope="col" class="hide-sm">Evidence</th><th scope="col">Tickets</th></tr></thead>
    <tbody>${rels.map((r, i) => html`<tr key=${i}><td class="mono small">${r.from}</td><td class="mono small">${r.to}</td><td>${r.kind || '—'}</td>
      <td>${r.verified ? html`<${Status} kind="ok">Verified in QA</${Status}>` : html`<${Status} kind="off">Not verified</${Status}>`}</td>
      <td class="hide-sm small">${r.evidence || ''}</td><td>${(r.tickets || []).map((k) => html`<${TicketLink} key=${k} k=${k} /> `)}</td></tr>`)}</tbody></table></${Scroll}>`;
}

function DbTab({ k, table }) {
  const t = table ? k.tables.find((x) => x.name === table) : null;
  if (!k.tables.length) return html`<${Empty} title="No tables recorded yet" icon="db">Tables, columns and links appear as you investigate with the database MCP or read schema code.</${Empty}>`;
  const verified = k.relations.filter((r) => r.verified).length;
  if (!t) {
    return html`<p class="lede">${plural(k.tables.length, 'table')} and ${plural(k.relations.length, 'link')} from your investigations; ${verified} verified against QA${k.linkmap ? ' (link map from ' + (k.linkmap.env || 'QA') + (k.linkmap.fetched_at ? ', ' + fmtDay(String(k.linkmap.fetched_at).slice(0, 10), true) : '') + ')' : ''}. Pick a table in the side list to see its neighbourhood.</p>
      <${Diagram} code=${mapSource(k.tables, k.relations, null)} label="Map of all tables" alt=${html`<${RelTable} rels=${k.relations} />`} />`;
  }
  const rels = k.relations.filter((r) => sameTable(r.from, t.name) || sameTable(r.to, t.name));
  return html`<div class="sec-head"><h2>${t.name}</h2><span class="muted small">${t.db || ''}</span><span class="grow"></span><button type="button" class="btn quiet" onClick=${() => setParams({ table: '' })}><${Icon} name="left" size=${15} />Whole map</button></div>
    ${t.purpose ? html`<p class="lede">${t.purpose}</p>` : null}
    <dl class="kv" style="margin-bottom:16px">
      ${(t.key_columns || []).length ? html`<dt>Key columns</dt><dd>${t.key_columns.map((c) => html`<code key=${c}>${c}</code> `)}</dd>` : null}
      ${(t.columns || []).length ? html`<dt>Columns</dt><dd><div class="chips">${t.columns.map((c) => html`<${Chip} key=${c}>${c}</${Chip}>`)}</div></dd>` : null}
      ${(t.tickets || []).length ? html`<dt>Tickets</dt><dd>${t.tickets.map((x) => html`<${TicketLink} key=${x} k=${x} /> `)}</dd>` : null}
      ${t.last_seen ? html`<dt>Last seen</dt><dd>${fmtDay(t.last_seen, true)}</dd>` : null}
    </dl>
    ${rels.length ? html`<${Diagram} code=${mapSource(k.tables, k.relations, t.name)} label=${'Links of ' + t.name} alt=${html`<${RelTable} rels=${rels} focus=${t.name} />`} />` : null}
    <h3 style="margin:8px 0">Links</h3><${RelTable} rels=${rels} focus=${t.name} />`;
}

function ApisTab({ k, p }) {
  const [method, setMethod] = useState('');
  const [dir, setDir] = useState('');
  const q = (p.q || '').toLowerCase();
  const methods = [...new Set(k.apis.map((a) => a.method).filter(Boolean))].sort();
  const dirs = [...new Set(k.apis.map((a) => a.direction).filter(Boolean))].sort();
  const rows = k.apis.filter((a) => (!method || a.method === method) && (!dir || a.direction === dir)
    && (!q || [a.method, a.path, a.host, a.purpose].join(' ').toLowerCase().indexOf(q) >= 0));
  const cols = [
    { id: 'm', label: 'Method', value: (a) => a.method || '', render: (a) => html`<span class="method">${a.method}</span>` },
    { id: 'p', label: 'Path', value: (a) => a.path || '', render: (a) => html`<span class="url">${a.path}</span><div class="muted small">${a.host || ''}</div>` },
    { id: 'd', label: 'Direction', hideSm: true, value: (a) => a.direction || '', render: (a) => a.direction || '—' },
    { id: 'u', label: 'Purpose', render: (a) => html`${a.purpose || ''}${(a.request_fields || []).length ? html`<div class="muted small">Request: ${a.request_fields.join(', ')}</div>` : null}` },
    { id: 't', label: 'Tickets', render: (a) => (a.tickets || []).map((x) => html`<${TicketLink} key=${x} k=${x} /> `) },
    { id: 'l', label: 'Last seen', hideSm: true, value: (a) => a.last_seen || '', render: (a) => html`<span class="nowrap">${a.last_seen ? fmtDay(a.last_seen, true) : ''}</span>` },
  ];
  if (!k.apis.length) return html`<${Empty} title="No APIs recorded yet" icon="api">Endpoints you call or read about while working on tickets are catalogued here.</${Empty}>`;
  return html`<div class="pagehead-act" style="margin-bottom:12px">
      <label class="sr" for="api-q">Filter APIs</label><input id="api-q" type="search" placeholder="Filter by path, host or purpose" value=${p.q || ''} onInput=${(e) => setParams({ q: e.currentTarget.value })} autocomplete="off" />
      <label class="sr" for="api-m">Method</label><select id="api-m" value=${method} onChange=${(e) => setMethod(e.currentTarget.value)}><option value="">Any method</option>${methods.map((m) => html`<option key=${m} value=${m}>${m}</option>`)}</select>
      <label class="sr" for="api-d">Direction</label><select id="api-d" value=${dir} onChange=${(e) => setDir(e.currentTarget.value)}><option value="">Any direction</option>${dirs.map((m) => html`<option key=${m} value=${m}>${m}</option>`)}</select>
      <span class="muted small" aria-live="polite">${rows.length} of ${k.apis.length}</span></div>
    <${DataTable} columns=${cols} rows=${rows} rowKey=${(a) => (a.method || '') + ' ' + a.path + ' ' + (a.host || '')} caption="API catalog" virtual sort=${{ id: 'l', dir: 'desc' }}
      empty=${html`<${Empty} title="No API matches these filters" />`} />`;
}

function IntegrationsTab({ k }) {
  if (!k.integrations.length) return html`<${Empty} title="No integrations recorded yet" icon="plug" />`;
  return html`<div class="grid">${k.integrations.map((x, i) => html`<div class="card" key=${i}><h3>${x.system || 'Integration'}</h3>
    ${x.artifact ? html`<p>${x.artifact}</p>` : null}${x.notes ? html`<p>${x.notes}</p>` : null}
    <p>${(x.tickets || []).map((t) => html`<${TicketLink} key=${t} k=${t} /> `)}${x.last_seen ? html`<span class="muted"> · last seen ${fmtDay(x.last_seen, true)}</span>` : null}</p></div>`)}</div>`;
}

export function KnowledgePage({ route }) {
  const res = useApi('/api/knowledge');
  const p = route.params;
  const tab = TABS.some((t) => t.id === p.tab) ? p.tab : 'db';
  const [tq, setTq] = useState('');
  useEffect(() => { document.title = 'Knowledge · Chitragupta'; pageKeys.set({}); }, []);
  const k = res.data;
  const tables = useMemo(() => (k ? k.tables.slice().sort((a, b) => a.name.localeCompare(b.name)) : []), [k]);
  const rail = tab === 'db' && k ? html`<div class="rail-tools"><label class="sr" for="kn-q">Find a table</label>
      <input id="kn-q" type="search" placeholder="Find a table" value=${tq} onInput=${(e) => setTq(e.currentTarget.value)} autocomplete="off" /></div>
    <ul class="rlist">${tables.filter((t) => !tq || (t.name + ' ' + (t.db || '') + ' ' + (t.purpose || '')).toLowerCase().indexOf(tq.toLowerCase()) >= 0).map((t) => {
      const n = k.relations.filter((r) => sameTable(r.from, t.name) || sameTable(r.to, t.name));
      const v = n.filter((r) => r.verified).length;
      return html`<li key=${t.name}><a href=${'#/knowledge?tab=db&table=' + encodeURIComponent(t.name)} aria-current=${p.table === t.name ? 'page' : null}
        onClick=${(e) => { e.preventDefault(); setParams({ table: t.name }); }}><span class="sw" style=${{ background: v ? 'var(--ok)' : 'var(--rule)' }}></span>
        <span><span class="k" style="font-weight:500">${t.name}</span><span class="t">${t.db || ''}</span></span><span class="m" title=${v + ' of ' + n.length + ' links verified'}>${n.length ? v + '/' + n.length : ''}</span></a></li>`;
    })}</ul>` : null;
  return html`<${Desk} rail=${rail} railLabel="Tables">
    <${PageHead} title="Knowledge" sub=${k ? plural(k.tables.length, 'table') + ' · ' + plural(k.apis.length, 'API') + ' · ' + plural(k.integrations.length, 'integration') : ''} />
    <${Load} res=${res} lines=${8}>${(k) => html`
      <${Tabs} tabs=${TABS.map((t) => Object.assign({}, t, { count: t.id === 'db' ? k.tables.length : t.id === 'apis' ? k.apis.length : k.integrations.length }))} active=${tab}
        onChange=${(id) => setParams({ tab: id === 'db' ? '' : id, q: '', table: '' })} label="Knowledge sections" idBase="kn" />
      <${TabPanel} idBase="kn" active=${tab}>
        ${tab === 'db' ? html`<${DbTab} k=${k} table=${p.table} />` : tab === 'apis' ? html`<${ApisTab} k=${k} p=${p} />` : html`<${IntegrationsTab} k=${k} />`}
      </${TabPanel}>`}</${Load}>
  </${Desk}>`;
}
