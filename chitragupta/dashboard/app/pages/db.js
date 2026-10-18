// DB map: databases → schemas → tables in the side list; one table's columns, links (verified, likely or not verified
// against the QA link map), who reads and writes it, its tickets and saved queries, and a 1–2 hop neighbourhood
// diagram with a table as its text alternative. The whole map is drawn only on request.
import { useEffect, useMemo, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, setParams, href, fmtDay, plural } from '../lib/core.js';
import { Icon, Status, Empty, Load, PageHead, TicketLink, DataTable, Diagram, Scroll, CodeBlock } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

function same(a, b) { return a === b || a.endsWith('.' + b) || b.endsWith('.' + a); }
function short(k) { return String(k || '').split('.').slice(-1)[0]; }
function col(ref) { return short(ref); }
function lbl(s) { return '"' + String(s).replace(/"/g, '#quot;') + '"'; }
const ARROW = { verified: ' ==>|', likely: ' -->|', unverified: ' -.->|' };

function graphSource(nodes, edges, focus) {
  const ids = {};
  const id = (k) => { const hit = Object.keys(ids).find((x) => same(x, k)); if (hit) return ids[hit]; ids[k] = 'n' + Object.keys(ids).length; return ids[k]; };
  if (focus) id(focus);
  nodes.forEach(id);
  const lines = ['flowchart LR'];
  Object.keys(ids).forEach((k) => lines.push('  ' + ids[k] + '[' + lbl(k) + ']'));
  edges.forEach((e) => lines.push('  ' + id(e.from) + (ARROW[e.status] || ARROW.unverified) + lbl(col(e.from_col) + ' → ' + col(e.to_col)) + '| ' + id(e.to)));
  if (focus) lines.push('  style ' + ids[Object.keys(ids).find((x) => same(x, focus))] + ' stroke-width:3px');
  return lines.join('\n');
}

function LinkStatus({ l }) {
  if (l.status === 'verified') return html`<${Status} kind="ok">Verified ${Math.round(l.ratio * 100)}%</${Status}>`;
  if (l.status === 'likely') return html`<${Status} kind="warn">Likely ${Math.round(l.ratio * 100)}%</${Status}>`;
  return html`<${Status} kind="off">${l.ratio != null ? 'Weak ' + Math.round(l.ratio * 100) + '%' : 'Not verified'}</${Status}>`;
}

function TableRef({ k, keys }) {
  const hit = keys.find((x) => same(x, k));
  return hit ? html`<a class="mono small" href=${href('db', hit)}>${k}</a>` : html`<span class="mono small">${k}</span>`;
}

function LinksTable({ links, keys, focus }) {
  if (!links.length) return html`<${Empty} title="No links recorded for this table yet" icon="db">Links appear when the QA link map runs or when a session or code shows two tables joined.</${Empty}>`;
  return html`<${Scroll} label="Links"><table><caption class="sr">Links of ${focus}</caption>
    <thead><tr><th scope="col">From</th><th scope="col">To</th><th scope="col">Checked in QA</th><th scope="col">Tickets</th><th scope="col" class="hide-sm">Evidence</th></tr></thead>
    <tbody>${links.map((l, i) => html`<tr key=${i}>
      <td><${TableRef} k=${l.from} keys=${keys} /><div class="muted small mono">${col(l.from_col)}</div></td>
      <td><${TableRef} k=${l.to} keys=${keys} /><div class="muted small mono">${col(l.to_col)}</div></td>
      <td><${LinkStatus} l=${l} />${l.sampled ? html`<div class="muted small">${l.matched} of ${l.sampled} sampled rows matched</div>` : html`<div class="muted small">seen in code or a session</div>`}</td>
      <td>${(l.tickets || []).map((k) => html`<${TicketLink} key=${k} k=${k} /> `)}</td><td class="hide-sm small">${l.evidence || ''}</td></tr>`)}</tbody></table></${Scroll}>`;
}

const COLS = [
  { id: 'name', label: 'Column', value: (c) => c.name, render: (c) => html`<span class="mono">${c.name}</span>` },
  { id: 'type', label: 'Type', value: (c) => c.type || '', render: (c) => html`<span class="mono small">${c.type || '—'}</span>` },
  { id: 'flags', label: 'Key', render: (c) => [c.pk ? 'primary key' : '', c.fk ? 'foreign key' : '', c.phi ? 'personal data' : ''].filter(Boolean).join(', ') || '—' },
  { id: 'null', label: 'Nullable', hideSm: true, render: (c) => (c.nullable == null ? '—' : c.nullable ? 'yes' : 'no') },
];

function TablePanel({ k, hops, keys }) {
  const res = useApi('/api/db/table?key=' + encodeURIComponent(k) + (hops === 2 ? '&hops=2' : ''));
  return html`<${Load} res=${res} lines=${8}>${(t) => html`
    <div class="sec-head"><h2 class="mono">${t.name}</h2><span class="muted small">${t.database} › ${t.schema}</span><span class="grow"></span>
      <a class="btn quiet" href=${href('db')}><${Icon} name="left" size=${15} />All databases</a></div>
    ${t.purpose ? html`<p class="lede">${t.purpose}</p>` : null}
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
      <p class="muted small">Thick arrow: verified in QA (80% or more of sampled rows matched) · plain: likely (30% or more) · dotted: not verified.</p>`
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
      : html`<p class="muted">No code seen reading or writing it yet.</p>`}
    <h3 style="margin:16px 0 8px">Saved queries</h3>
    ${t.queries.length ? t.queries.map((q) => html`<div key=${q.ticket + q.id} style="margin-bottom:12px"><p class="small"><${TicketLink} k=${q.ticket} />
        ${q.purpose ? ' · ' + q.purpose : ''}${q.date ? html`<span class="muted"> · ${fmtDay(q.date, true)}</span>` : null}</p>
        <${CodeBlock} code=${q.text} label=${q.dialect || 'sql'} what="the query" /></div>`)
      : html`<p class="muted">No saved queries use this table.</p>`}`}</${Load}>`;
}

function Overview({ m, full }) {
  const all = useApi(full ? '/api/db/graph' : null);
  return html`<p class="lede">${plural(m.counts.tables, 'table')} in ${plural(m.databases.length, 'group')}, ${plural(m.counts.links, 'link')}; ${m.counts.verified} verified against QA${m.linkmap ? ' (link map from ' + (m.linkmap.env || 'QA') + (m.linkmap.fetched_at ? ', ' + fmtDay(String(m.linkmap.fetched_at).slice(0, 10), true) : '') + ')' : ''}. Pick a table in the side list.</p>
    <div class="grid">${m.databases.map((d) => html`<section class="card" key=${d.name} aria-label=${d.name}><h2><span class="dbname"><${Icon} name="db" size=${18} />${d.name}</span></h2>
      <ul class="dblist">${d.schemas.map((s) => html`<li key=${s.name}><span class="mono">${s.name}</span> <span class="muted small">· ${plural(s.tables.length, 'table')}</span></li>`)}</ul></section>`)}</div>
    <div class="sec-head" style="margin-top:16px"><h2>Whole map</h2><span class="grow"></span>
      ${full ? html`<button type="button" class="btn quiet" onClick=${() => setParams({ full: '' })}>Hide</button>`
        : html`<button type="button" class="btn" onClick=${() => setParams({ full: '1' })}>Draw every link</button>`}</div>
    ${full ? html`<${Load} res=${all} lines=${4}>${(g) => html`<${Diagram} code=${graphSource(g.graph.nodes, g.graph.edges, null)} label="Every recorded link"
        alt=${html`<${LinksTable} links=${g.graph.edges} keys=${g.graph.nodes} focus="all tables" />`} />`}</${Load}>`
      : html`<p class="muted">Large maps are slow to draw and hard to read; a table's neighbourhood is usually clearer.</p>`}`;
}

export function DbPage({ route }) {
  const res = useApi('/api/db');
  const p = route.params;
  const hops = p.hops === '2' ? 2 : 1;
  const [tq, setTq] = useState('');
  useEffect(() => { document.title = (route.arg ? route.arg + ' · ' : '') + 'DB map · Chitragupta'; pageKeys.set({}); }, [route.arg]);
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
          <a href=${href('db', t.key)} aria-current=${route.arg === t.key ? 'page' : null}><span class="sw" style=${{ background: t.verified ? 'var(--ok)' : 'var(--rule)' }}></span>
            <span><span class="k mono">${t.name}</span><span class="t">${t.columns ? 'columns known' : 'no schema card'}</span></span>
            <span class="m" title=${t.verified + ' of ' + t.links + ' links verified'}>${t.links ? t.verified + '/' + t.links : ''}</span></a></li>`)}</ul></div>`)}</details>`;
    })}` : null;
  return html`<${Desk} rail=${rail} railLabel="Tables">
    <${PageHead} title="DB map" sub=${m ? plural(m.counts.tables, 'table') + ' · ' + plural(m.counts.links, 'link') : ''} />
    <${Load} res=${res} lines=${8}>${(m) => !m.counts.tables
      ? html`<${Empty} title="No tables recorded yet" icon="db">Tables, columns and links appear as you investigate with the database MCP or read schema code.</${Empty}>`
      : route.arg ? html`<${TablePanel} key=${route.arg} k=${route.arg} hops=${hops} keys=${keys} />` : html`<${Overview} m=${m} full=${p.full === '1'} />`}</${Load}>
  </${Desk}>`;
}
