// Search: everything in the worklog — tickets, findings, queries, requests, artifacts, branches, PRs, APIs, tables, DB
// links, integrations and daily files — grouped by type, with the matching words highlighted, narrowed by ticket, repo
// and dates (kept in the address). Up and Down move between results.
import { useEffect, useRef, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { api, setParams, fmtDay, plural } from '../lib/core.js';
import { Icon, Empty, Skeleton, ErrorBox, PageHead, Chip } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

const TYPE_ICON = { ticket: 'tickets', finding: 'info', query: 'db', request: 'api', api: 'api', table: 'db', dblink: 'db', integration: 'plug',
  artifact: 'file', branch: 'branch', pr: 'branch', day: 'day' };
const FILTERS = ['ticket', 'repo', 'from', 'to'];

function Filters({ p }) {
  const [open, setOpen] = useState(FILTERS.some((k) => p[k]));
  const on = FILTERS.filter((k) => p[k]).length;
  const field = (k, label, type, extra) => html`<label class="filt"><span class="small">${label}</span>
    <input type=${type} value=${p[k] || ''} ...${extra || {}} onChange=${(e) => setParams({ [k]: k === 'ticket' ? e.currentTarget.value.trim().toUpperCase() : e.currentTarget.value.trim() })} /></label>`;
  return html`<div class="filters"><button type="button" class="btn quiet" aria-expanded=${open ? 'true' : 'false'} aria-controls="search-filters" onClick=${() => setOpen(!open)}>
      <${Icon} name=${open ? 'up' : 'down'} size=${15} />Filters${on ? ' (' + on + ')' : ''}</button>
    ${on ? html`<button type="button" class="btn quiet" onClick=${() => setParams({ ticket: '', repo: '', from: '', to: '' })}>Clear filters</button>` : null}
    ${open ? html`<div id="search-filters" class="filtrow">
      ${field('ticket', 'Ticket', 'text', { placeholder: 'ABC-1001', autocomplete: 'off', spellcheck: 'false', style: 'text-transform:uppercase' })}
      ${field('repo', 'Repo', 'text', { autocomplete: 'off', spellcheck: 'false' })}
      ${field('from', 'From', 'date')}${field('to', 'To', 'date')}</div>` : null}</div>`;
}

export function Highlight({ text, hits }) {
  const s = String(text || '');
  const hs = (hits || []).slice().sort((a, b) => a[0] - b[0]);
  const out = [];
  let at = 0;
  hs.forEach(([start, len], i) => {
    if (start < at || start >= s.length) return;
    if (start > at) out.push(s.slice(at, start));
    out.push(html`<mark key=${i}>${s.slice(start, start + len)}</mark>`);
    at = start + len;
  });
  if (at < s.length) out.push(s.slice(at));
  return out;
}

export function SearchPage({ route }) {
  const q = route.params.q || '';
  const type = route.params.type || '';
  const p = route.params;
  const fq = FILTERS.filter((k) => p[k]).map((k) => '&' + k + '=' + encodeURIComponent(k === 'ticket' ? p[k].toUpperCase() : p[k])).join('');
  const [text, setText] = useState(q);
  const [st, setSt] = useState({ data: null, error: null, loading: false });
  const list = useRef(null);
  useEffect(() => { document.title = (q ? q + ' · ' : '') + 'Search · Chitragupta'; pageKeys.set({}); }, [q]);
  useEffect(() => { setText(q); }, [q]);
  useEffect(() => {
    if (q.trim().length < 2) { setSt({ data: null, error: null, loading: false }); return undefined; }
    let live = true;
    setSt((s) => ({ ...s, loading: true }));
    api('/api/search?limit=200&q=' + encodeURIComponent(q.trim()) + fq).then((data) => live && setSt({ data, error: null, loading: false }),
      (error) => live && setSt({ data: null, error, loading: false }));
    return () => { live = false; };
  }, [q, fq]);
  useEffect(() => {
    const t = setTimeout(() => { if (text !== q) setParams({ q: text }); }, 220);
    return () => clearTimeout(t);
  }, [text]);
  const d = st.data;
  const groups = d ? d.groups.filter((g) => !type || g.type === type) : [];
  const move = (e) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    const links = Array.from(list.current ? list.current.querySelectorAll('.results a') : []);
    if (!links.length) return;
    e.preventDefault();
    let i = links.indexOf(document.activeElement);
    i = e.key === 'ArrowDown' ? Math.min(links.length - 1, i + 1) : Math.max(-1, i - 1);
    if (i < 0) document.getElementById('search-q').focus(); else links[i].focus();
  };
  const rail = d ? html`<div class="facet-t">Show</div><ul class="rlist">
      <li><a href="#" aria-current=${!type ? 'true' : null} onClick=${(e) => { e.preventDefault(); setParams({ type: '' }); }}><span class="sw"></span><span class="k" style="font-weight:500">Everything</span><span class="m">${d.total}</span></a></li>
      ${d.groups.map((g) => html`<li key=${g.type}><a href="#" aria-current=${type === g.type ? 'true' : null} onClick=${(e) => { e.preventDefault(); setParams({ type: type === g.type ? '' : g.type }); }}>
        <span class="sw"></span><span class="k" style="font-weight:500"><${Icon} name=${TYPE_ICON[g.type] || 'info'} size=${15} />${g.label}</span><span class="m">${g.count}</span></a></li>`)}</ul>
    <p class="muted small" style="margin-top:16px">${d.engine === 'fts5' ? 'Full-text index' : 'Simple scan (this Python has no full-text search)'} · ${d.took_ms} ms</p>` : null;
  return html`<${Desk} rail=${rail} railLabel="Result types">
    <div onKeyDown=${move} ref=${list}>
    <${PageHead} title="Search" sub="Tickets, findings, queries, requests, branches, PRs, APIs, tables, DB links, artifacts and daily notes" />
    <div class="pagehead-act" style="margin-bottom:20px"><label class="sr" for="search-q">Search the worklog</label>
      <input id="search-q" type="search" style="flex:1;min-width:240px;font-size:16px" placeholder="Search everything you have worked on" value=${text}
        onInput=${(e) => setText(e.currentTarget.value)} autocomplete="off" spellcheck="false" />
    </div>
    <${Filters} p=${p} />
    <p class="sr" aria-live="polite">${d ? plural(d.total, 'result') + (type ? ', showing ' + type : '') : ''}</p>
    ${q.trim().length < 2 ? html`<${Empty} title="Type at least two letters" icon="search">Try a ticket key, a table name, an endpoint path or a word from a finding. Ctrl K searches from anywhere.</${Empty}>`
      : st.error ? html`<${ErrorBox} error=${st.error} />`
        : !d ? html`<${Skeleton} lines=${6} />`
          : !groups.length ? html`<${Empty} title=${'Nothing found for “' + q + '”'} icon="search">Check the spelling, use fewer words, or search for the start of a word.</${Empty}>`
            : groups.map((g) => html`<section class="sec" key=${g.type} aria-labelledby=${'sg-' + g.type}>
              <div class="sec-head"><h2 id=${'sg-' + g.type}><${Icon} name=${TYPE_ICON[g.type] || 'info'} size=${18} /> ${g.label}</h2><span class="tabcount">${g.count}</span></div>
              <ul class="results">${g.results.map((r, i) => html`<li key=${i}><a href=${r.href}>
                <span class="rt">${r.title}${r.ticket && r.type !== 'ticket' ? html`<${Chip}>${r.ticket}</${Chip}>` : null}${r.repo ? html`<${Chip} icon="branch">${r.repo}</${Chip}>` : null}</span>
                ${r.snippet ? html`<span class="rs" style="display:block"><${Highlight} text=${r.snippet} hits=${r.hits} /></span>` : null}
                ${r.date ? html`<span class="rm">${fmtDay(String(r.date).slice(0, 10), true)}</span>` : null}</a></li>`)}</ul></section>`)}
    </div>
  </${Desk}>`;
}
