// Tickets: a filterable, sortable list; and one ticket with a sticky header and tabs (Overview · Branches & PRs ·
// DB flow · Requests · Queries · Artifacts · Timeline · Notes), plus the export links.
import { useEffect, useMemo, useRef, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, fmtMin, fmtTs, fmtDay, setParams, href, plural, isHttp, EXPORT, parseDay, isoDay } from '../lib/core.js';
import { Icon, Status, CopyButton, CodeBlock, Empty, Load, PageHead, TicketLink, Chip, Tabs, TabPanel, DataTable, Markdown, Diagram, Scroll } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';
import { ShareButton } from '../lib/share.js';

// ------------------------------------------------------------------ list
const SINCE = [['', 'Any time'], ['7', '7 days'], ['30', '30 days'], ['90', '90 days']];
function sinceDay(n) { const d = new Date(); d.setDate(d.getDate() - n); return isoDay(d); }
function filterTickets(list, p) {
  const q = (p.q || '').trim().toLowerCase();
  return list.filter((t) => (!q || (t.key + ' ' + t.title + ' ' + t.tldr).toLowerCase().indexOf(q) >= 0)
    && (!p.status || (t.status || 'No status') === p.status)
    && (!p.repo || (t.repos || []).indexOf(p.repo) >= 0)
    && (!p.alerts || (t.alerts || []).length)
    && (!p.since || (t.last || '') >= sinceDay(+p.since)));
}
function counts(list, f) {
  const m = {};
  list.forEach((t) => [].concat(f(t)).forEach((v) => { m[v] = (m[v] || 0) + 1; }));
  return Object.keys(m).sort((a, b) => m[b] - m[a] || a.localeCompare(b)).map((k) => [k, m[k]]);
}
function TicketFilters({ list, p }) {
  const statuses = counts(list, (t) => t.status || 'No status');
  const repos = counts(list, (t) => t.repos || []);
  const active = p.q || p.status || p.repo || p.alerts || p.since;
  return html`<div class="ticket-filters">
      <label class="sr" for="tk-q">Filter tickets</label>
      <input id="tk-q" type="search" placeholder="Filter by key, title or summary" value=${p.q || ''} autocomplete="off"
        onInput=${(e) => setParams({ q: e.currentTarget.value })} />
      <label>Last active<select value=${p.since || ''} onChange=${(e) => setParams({ since: e.currentTarget.value })}>${SINCE.map(([v, l]) => html`<option key=${v} value=${v}>${l}</option>`)}</select></label>
      <label>Status<select value=${p.status || ''} onChange=${(e) => setParams({ status: e.currentTarget.value })}><option value="">Any status</option>${statuses.map(([v, n]) => html`<option key=${v} value=${v}>${v} (${n})</option>`)}</select></label>
      <label>Repo<select value=${p.repo || ''} onChange=${(e) => setParams({ repo: e.currentTarget.value })}><option value="">Any repo</option>${repos.map(([v, n]) => html`<option key=${v} value=${v}>${v} (${n})</option>`)}</select></label>
      <label class="check"><input type="checkbox" checked=${!!p.alerts} onChange=${(e) => setParams({ alerts: e.currentTarget.checked ? '1' : '' })} />Only those that need you</label>
      ${active ? html`<button type="button" class="btn quiet" onClick=${() => setParams({ q: '', status: '', repo: '', alerts: '', since: '' })}><${Icon} name="close" size=${15} />Clear filters</button>` : null}
    </div>`;
}

export function TicketsPage({ route }) {
  const res = useApi('/api/tickets');
  const p = route.params;
  useEffect(() => { document.title = 'Tickets · Chitragupta'; pageKeys.set({}); }, []);
  const all = (res.data && res.data.tickets) || [];
  const shown = useMemo(() => filterTickets(all, p), [all, p.q, p.status, p.repo, p.alerts, p.since]);
  const cols = [
    { id: 'key', label: 'Ticket', value: (t) => t.key, render: (t) => html`<${TicketLink} k=${t.key} />${(t.alerts || []).length ? html` <${Icon} name="warn" size=${15} label=${plural(t.alerts.length, 'delivery alert')} />` : null}` },
    { id: 'title', label: 'Title', cls: 'wide', value: (t) => (t.title || '').toLowerCase(), render: (t) => html`<div>${t.title || '—'}</div>${t.tldr ? html`<div class="muted small">${t.tldr.length > 120 ? t.tldr.slice(0, 117) + '…' : t.tldr}</div>` : null}` },
    { id: 'status', label: 'Status and repos', hideSm: true, value: (t) => t.status || '', render: (t) => html`${t.status ? html`<${Chip}>${t.status}</${Chip}>` : html`<span class="muted">No status</span>`}
      ${(t.repos || []).length ? html`<div class="muted small" style="margin-top:4px;overflow-wrap:anywhere">${t.repos.join(', ')}</div>` : null}` },
    { id: 'last', label: 'Last active', hideSm: true, value: (t) => t.last || '', render: (t) => html`<span class="nowrap">${t.last ? fmtDay(t.last, true) : '—'}</span>` },
    { id: 'minutes', label: 'Time', num: true, value: (t) => t.minutes, render: (t) => fmtMin(t.minutes) },
    { id: 'findings', label: 'Findings', num: true, hideSm: true, value: (t) => (t.counts || {}).findings || 0, render: (t) => (t.counts || {}).findings || 0 },
    { id: 'requests', label: 'Requests', num: true, hideSm: true, value: (t) => (t.counts || {}).requests || 0, render: (t) => (t.counts || {}).requests || 0 },
  ];
  return html`<${Desk} className="tickets-desk">
    <${PageHead} title="Tickets" sub=${res.data ? (shown.length === all.length ? plural(all.length, 'ticket') : shown.length + ' of ' + plural(all.length, 'ticket')) + ', newest activity first' : ''} />
    <${TicketFilters} list=${all} p=${p} />
    <${Load} res=${res} lines=${10}>${() => (all.length
      ? html`<p class="sr" aria-live="polite">${shown.length} tickets shown</p>
        <${DataTable} columns=${cols} rows=${shown} rowKey=${(t) => t.key} caption="Tickets" virtual sort=${{ id: 'last', dir: 'desc' }}
          empty=${html`<${Empty} title="No ticket matches these filters">Clear the filters above, or search everything with Ctrl K.</${Empty}>`} />`
      : html`<${Empty} title="No tickets recorded yet" icon="tickets">Work on a branch or mention a ticket key in Claude Code, and the ticket appears here.</${Empty}>`)}</${Load}>
  </${Desk}>`;
}

// ------------------------------------------------------------------ branches & PRs (tk.git from branch tracking)
const DSYM = { in: '✓', same: '✓', squash: '✓', open: '…', missing: '✗', reverted: '↩', check: '?' };
const DLAB = { in: 'in target', same: 'same change in target', squash: 'via squash PR', open: 'PR open', missing: 'missing', reverted: 'reverted', check: 'check by hand' };
const DCLS = { in: 'good', same: 'good', squash: 'good', open: 'open', missing: 'bad', reverted: 'bad', check: 'check' };
const AKIND = { lost: 'Lost commits', 'only-copy': 'Only copy', gap: 'Gap', check: 'Check by hand', 'wrong-parent': 'Wrong parent', 'pr-base': 'PR base', unpushed: 'Not pushed', 'pr-stale': 'Stale PR' };
const BSTATE = { active: ['off', 'active'], pr_open: ['warn', 'PR open'], merged: ['ok', 'merged'], deleted_remote: ['off', 'deleted on remote'], abandoned: ['off', 'abandoned'] };
const PSTATE = { merged: 'ok', open: 'warn', draft: 'off', closed: 'off' };
function prSourceText(src) {
  return { gh: 'PR info from GitHub (gh)', glab: 'PR info from GitLab (glab)', az: 'PR info from Azure DevOps (az)',
    'merge-commit': 'PR info from merge commits (open PRs and reviews unavailable)', test: 'PR info from test data' }[src || ''] || 'PR info unavailable';
}
function shas(list) { return html`<span class="shas">${(list || []).map((c, i) => html`<code key=${i}>${String(c).slice(0, 9)}</code>`)}</span>`; }
export function GitAlerts({ g }) {
  let al = (g && g.alerts) || [];
  if (!al.length) return null;
  const rank = { lost: 0, 'only-copy': 1 };
  al = al.slice().sort((a, b) => (a.kind in rank ? rank[a.kind] : 9) - (b.kind in rank ? rank[b.kind] : 9));
  const bad = al.some((a) => a.kind === 'lost' || a.kind === 'only-copy');
  return html`<div class=${'alert' + (bad ? ' bad' : '')} role="alert" id="git-alerts"><${Icon} name=${bad ? 'bad' : 'warn'} size=${18} />
    <div class="grow"><strong>Branches and delivery need attention</strong><ul>${al.map((a, i) => html`<li key=${i}>
      <${Status} kind=${a.kind === 'lost' || a.kind === 'only-copy' ? 'bad' : 'warn'} quiet>${AKIND[a.kind] || a.kind}</${Status}>
      ${a.repo ? html` <span class="muted">${a.repo} ·</span>` : null} ${a.text} ${a.commits && a.commits.length ? shas(a.commits) : null}</li>`)}</ul></div></div>`;
}
function cellTitle(c) { return [c.why, c.pr ? 'PR #' + c.pr : '', c.via ? 'via ' + c.via : '', c.ref ? 'checked in ' + c.ref : ''].filter(Boolean).join(' · '); }
function GitMatrix({ e }) {
  const ts = e.targets || [], rows = e.rows || [];
  if (!ts.length || !rows.length) return e.strategy ? html`<p class="muted small">No commits for this ticket in this repo yet.</p>` : null;
  return html`<p class="gitsub">Delivery</p><${Scroll} label="Delivery matrix"><table><caption class="sr">Which commits reached which target branch</caption>
    <thead><tr><th scope="col">Commit</th><th scope="col">Subject</th>${ts.map((t) => html`<th scope="col" key=${t.role} title=${t.how || ''}>${t.name || t.role}${t.name !== t.role ? html` <span class="muted">${t.role}</span>` : null}</th>`)}</tr></thead>
    <tbody>${rows.map((r, i) => html`<tr key=${i}><td class="mono nowrap">${(r.shas || []).map((s, j) => html`<div key=${j}>${s}</div>`)}${(r.loose || []).length ? html`<div class="muted" title="cherry-picked with changes">~${r.loose.join(' ~')}</div>` : null}</td>
      <td>${r.subject}<div class="muted small">${fmtTs(r.date)}${(r.branches || []).length ? html` · <span class="mono">${r.branches.join(', ')}</span>` : null}</div></td>
      ${ts.map((t) => { const c = (r.cells || {})[t.role] || { s: 'missing' }; return html`<td key=${t.role} class=${'dv ' + (DCLS[c.s] || 'check')} title=${cellTitle(c)}>${(DSYM[c.s] || '?') + ' ' + (DLAB[c.s] || c.s)}</td>`; })}</tr>`)}</tbody>
  </table></${Scroll}>${e.rows_cut ? html`<p class="muted small">${e.rows_cut} older commits not shown.</p>` : null}`;
}
function GitBranches({ e }) {
  const bs = e.branches || [];
  if (!bs.length) return null;
  return html`<p class="gitsub">Branches</p><${Scroll} label="Branches"><table><caption class="sr">Branches linked to this ticket</caption>
    <thead><tr><th scope="col">Branch</th><th scope="col">Parent</th><th scope="col">Status</th><th scope="col">PRs</th><th scope="col" class="num">Commits</th><th scope="col" class="num">Unpushed</th><th scope="col" class="num">Ahead / behind</th><th scope="col">Linked by</th></tr></thead>
    <tbody>${bs.map((b) => {
      const st = BSTATE[b.status] || ['off', b.status || '—'];
      const inferred = b.parent_source === 'inferred' || b.role_guessed;
      const sup = b.suggested ? /superseded by (.+?)\)?$/.exec(b.suggested) : null;
      const where = [b.local ? 'local' : '', b.on_remote ? 'remote' : ''].filter(Boolean).join(' · ');
      return html`<tr key=${b.branch}><td><span class="mono">${b.branch}</span>${b.created_at ? html`<div class="muted small">created ${fmtTs(b.created_at)}</div>` : null}</td>
        <td><span class="mono">${b.parent || '—'}</span>${inferred ? html` <span class="muted">(inferred)</span>` : b.parent_source ? html` <${Chip}>${b.parent_source}</${Chip}>` : null}</td>
        <td><${Status} kind=${st[0]}>${st[1]}</${Status}>${b.suggested ? html`<div class="muted small" title=${b.suggested}>abandoned? (suggested)${sup ? html` · superseded by <span class="mono">${sup[1]}</span>` : null}</div>` : null}${where ? html`<div class="muted small">${where}</div>` : null}</td>
        <td class="nowrap">${(b.prs || []).length ? b.prs.map((n) => '#' + n).join(', ') : html`<span class="muted">—</span>`}</td>
        <td class="num">${b.own != null ? b.own : ''}</td><td class="num">${b.unpushed ? html`<strong>${b.unpushed}</strong>` : html`<span class="muted">0</span>`}</td>
        <td class="num">${b.ahead != null || b.behind != null ? '↑' + (b.ahead == null ? '?' : b.ahead) + ' ↓' + (b.behind == null ? '?' : b.behind) : html`<span class="muted">—</span>`}</td>
        <td>${b.link || '—'}</td></tr>`;
    })}</tbody></table></${Scroll}>`;
}
function GitPrs({ e }) {
  const ps = e.prs || [];
  if (!ps.length) return null;
  return html`<p class="gitsub">Pull requests</p><${Scroll} label="Pull requests"><table><caption class="sr">Pull requests for this ticket</caption>
    <thead><tr><th scope="col">PR</th><th scope="col">Title</th><th scope="col">Source to target</th><th scope="col">State</th><th scope="col">Review</th><th scope="col">Created</th><th scope="col">Merged</th></tr></thead>
    <tbody>${ps.map((p) => html`<tr key=${p.number}><td class="nowrap">${isHttp(p.url) && !EXPORT ? html`<a href=${p.url} rel="noopener noreferrer" target="_blank">#${p.number}<span class="sr"> (opens in a new tab)</span></a>` : '#' + p.number}</td>
      <td>${p.title || ''}</td><td class="mono small">${(p.source || '?') + ' → ' + (p.target || '?')}</td>
      <td><${Status} kind=${PSTATE[p.state] || 'off'}>${p.state || '—'}</${Status}></td><td>${p.review || '—'}</td>
      <td class="nowrap">${fmtTs(p.created_at)}</td><td class="nowrap">${p.merged_at ? fmtTs(p.merged_at) : html`<span class="muted">—</span>`}</td></tr>`)}</tbody></table></${Scroll}>`;
}
function GitTab({ g }) {
  const names = Object.keys((g && g.repos) || {}).sort();
  if (!names.length) return html`<${Empty} title="No branches tracked for this ticket" icon="branch">Branch tracking links branches and PRs once you commit on a branch named after the ticket. See docs/branch-tracking.md.</${Empty}>`;
  return html`<${GitAlerts} g=${g} />
    ${g.summary ? html`<p><strong>${g.summary}</strong>${g.checked_at ? html` <span class="muted">· checked ${fmtTs(g.checked_at)}</span>` : null}</p>` : null}
    ${names.map((n) => { const e = g.repos[n]; const lg = e.graph_legend || {}, ks = Object.keys(lg); return html`<div class="gitrepo" key=${n}>
      <h2><${Icon} name="branch" size=${18} />${n}</h2>
      <p class="small">${e.strategy ? html`Strategy <strong>${e.strategy}</strong>${e.strategy_how ? html` <span class="muted">(${e.strategy_how})</span>` : null}` : html`<span class="muted">${e.note || 'No branching strategy set for this repo'}</span>`} · ${prSourceText(e.pr_source)}</p>
      ${e.graph ? html`<${Diagram} code=${e.graph} label=${'Branch graph for ' + n} quiet="The branch graph couldn't be drawn here; the tables below have the same facts." />
        <p class="muted small">Highlighted dots start the base branches (their own history isn't drawn); crossed commits are missing from a target; tags mark merged PRs and cherry-picked copies.${ks.length ? ' Renamed in the graph: ' + ks.map((k) => k + ' = ' + lg[k]).join(', ') + '.' : ''}</p>`
        : e.graph_why ? html`<p class="muted small">No graph (${e.graph_why}); the tables below have everything.</p>` : null}
      <${GitMatrix} e=${e} /><${GitBranches} e=${e} /><${GitPrs} e=${e} /></div>`; })}`;
}

// ------------------------------------------------------------------ ticket tabs
const KIND = { root_cause: 'Root cause', finding: 'Findings', how_it_works: 'How it works', fix: 'Fix', decision: 'Decisions', config: 'Config',
  repro_test: 'Repro and tests', reference: 'References', blocker: 'Blockers', question: 'Open questions', todo: 'To do' };
const KIND_ICON = { root_cause: 'warn', blocker: 'bad', fix: 'ok', decision: 'check', question: 'info', todo: 'wait' };
function Finding({ i }) {
  return html`<li class="finding-row"><div class="finding-copy"><div class="t">${i.title}</div>${i.detail ? html`<div class="d">${i.detail}</div>` : null}</div>
    <aside class="finding-evidence" aria-label="Recorded evidence"><div>${[i.date, i.time].filter(Boolean).join(' ')}</div>
      ${i.confidence ? html`<div>${i.confidence} confidence</div>` : null}
      ${[].concat(i.evidence || []).map((e, n) => html`<p key=${n}>${e}</p>`)}</aside></li>`;
}
function Overview({ t }) {
  const groups = {};
  t.findings.forEach((i) => { (groups[i.kind] = groups[i.kind] || []).push(i); });
  const order = Object.keys(KIND).filter((k) => groups[k]).concat(Object.keys(groups).filter((k) => !KIND[k]));
  return html`${t.problem ? html`<p class="lede">${t.problem}</p>` : null}
    ${order.length ? order.map((k) => html`<section class="kindgroup" key=${k} aria-labelledby=${'kg-' + k}>
      <h2 id=${'kg-' + k}><${Icon} name=${KIND_ICON[k] || 'info'} size=${17} />${KIND[k] || k}<span class="tabcount">${groups[k].length}</span></h2>
      <ul class="findings">${groups[k].map((i) => html`<${Finding} key=${i.id} i=${i} />`)}</ul></section>`)
      : html`<${Empty} title="Nothing recorded for this ticket yet">Findings, root causes and decisions appear here as you work on it with Claude Code.</${Empty}>`}`;
}
function Requests({ rq }) {
  if (!rq.steps.length) return html`<${Empty} title=${'No saved requests' + (rq.captured ? ' (' + rq.captured + ' captured calls, not curated yet)' : '')} icon="api">Ask Claude to save the essential requests for this ticket to the worklog.</${Empty}>`;
  return html`${rq.setup ? html`<p class="muted small">Run these in Git Bash from the data home; the first block loads base URLs and secrets from bruno/.env.</p><${CodeBlock} code=${rq.setup} label="Setup" what="the setup block" />` : null}
    ${rq.steps.map((s) => html`<div class="step" key=${s.n}><div class="step-head"><strong>${s.n}. ${s.name}</strong>${s.method ? html`<span class="method">${s.method}</span>` : null}
      ${s.url ? html`<span class="url">${s.url}</span>` : null}<span class="grow"></span>${s.curl ? html`<${CopyButton} text=${s.curl} label="Copy as curl" what=${'for step ' + s.n} />` : null}</div>
      <div class="small muted">Expected: ${s.expected || '2xx'}${s.owner === 'user' ? ' · yours' : ''}${s.executed === false ? ' · not yet executed' : ''}${s.critical_fields && s.critical_fields.length ? ' · critical: ' + s.critical_fields.join(', ') : ''}</div>
      ${s.curl ? html`<details class="more"><summary>Show the curl command</summary><pre tabindex="0"><code>${s.curl}</code></pre></details>` : null}</div>`)}
    <p class="small muted">${rq.dropped ? rq.dropped + ' other captured calls left out. ' : ''}${rq.bruno_folder ? html`Bruno folder: <span class="mono">${rq.bruno_folder}</span> <${CopyButton} text=${rq.bruno_folder} label="Copy path" what="Bruno folder path" />` : null}</p>`;
}
function Queries({ qs }) {
  if (!qs.length) return html`<${Empty} title="No queries recorded" icon="db">Queries you run while working on the ticket are kept here.</${Empty}>`;
  return qs.map((q) => html`<div class="step" key=${q.id}><div class="step-head"><strong>${q.purpose || 'Query'}</strong><span class="muted small">${[q.date, q.time].filter(Boolean).join(' ')} · ${[q.dialect, q.kind].filter(Boolean).join(' · ')}</span><span class="grow"></span><${CopyButton} text=${q.text} what="query" /></div>
    ${q.tables && q.tables.length ? html`<div class="small muted">Tables: ${q.tables.map((x, i) => html`<code key=${i}>${x}</code> `)}</div>` : null}
    ${q.revealed ? html`<div class="small">Revealed: ${q.revealed}</div>` : null}<pre tabindex="0" style="margin-top:8px"><code>${q.text}</code></pre></div>`);
}
function Artifacts({ list }) {
  const cols = [
    { id: 'date', label: 'Date', value: (a) => a.last || a.date || '', render: (a) => html`<span class="nowrap">${a.last || a.date || ''}</span>` },
    { id: 'name', label: 'Artifact', value: (a) => (a.name || '').toLowerCase(), render: (a) => html`<div>${a.name}</div>${a.path ? html`<${CopyButton} text=${a.path} label="Copy path" what=${a.name} />` : null}` },
    { id: 'kind', label: 'Kind', hideSm: true, value: (a) => a.kind || '', render: (a) => a.kind || '' },
    { id: 'purpose', label: 'Purpose', render: (a) => a.purpose || '' },
    { id: 'versions', label: 'Versions', num: true, hideSm: true, value: (a) => +a.versions || 1, render: (a) => a.versions || 1 },
  ];
  return html`<${DataTable} columns=${cols} rows=${list} rowKey=${(a) => a.id} caption="Artifacts" sort=${{ id: 'date', dir: 'desc' }}
    empty=${html`<${Empty} title="No artifacts filed" icon="file">Drop a file whose name starts with the ticket key into _drop, or ask Claude to save an analysis.</${Empty}>`} />`;
}
function Timeline({ days }) {
  if (!days.length) return html`<${Empty} title="No days recorded yet" icon="day" />`;
  return html`<${Scroll} label="Timeline"><table><caption class="sr">Days worked on this ticket</caption><thead><tr><th scope="col">Day</th><th scope="col" class="num">Time</th><th scope="col">What happened</th></tr></thead>
    <tbody>${days.map((d) => html`<tr key=${d.day}><td class="nowrap">${EXPORT ? fmtDay(d.day, true) : html`<a href=${'#/day/' + d.day}>${fmtDay(d.day, true)}</a>`}</td><td class="num">${fmtMin(d.minutes)}</td><td>${d.line}</td></tr>`)}</tbody></table></${Scroll}>`;
}
function Notes({ t }) {
  const paths = Object.keys(t.paths || {});
  return html`${t.readme ? html`<${Markdown} text=${t.readme} />` : html`<${Empty} title="No notes file yet" icon="file">The ticket's README.md is written by the background tick.</${Empty}>`}
    ${paths.length ? html`<h2 style="margin:24px 0 8px">Files in the data home</h2><ul class="findings">${paths.map((k) => html`<li key=${k} style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <${Chip}>${k}</${Chip}><span class="mono small" style="flex:1;min-width:0;overflow-wrap:anywhere">${t.paths[k]}</span><${CopyButton} text=${t.paths[k]} label="Copy path" what=${k} /></li>`)}</ul>` : null}
    ${(t.files || []).length ? html`<details class="more"><summary>Code files touched (${t.files.length})</summary><ul class="findings">${t.files.map((f, i) => html`<li key=${i} class="mono small">${f.path || f.file || JSON.stringify(f)}</li>`)}</ul></details>` : null}`;
}
function DbFlow({ df }) {
  if (!df.markdown) return html`<${Empty} title="No DB flow traced yet" icon="db">Ask Claude to map this ticket's DB flow, or run /cg:trace for a QA worked example.</${Empty}>`;
  return html`${df.qa_trace ? html`<p class="small muted">QA worked example from ${df.qa_trace.env || 'QA'}${df.qa_trace.fetched_at ? ', fetched ' + fmtTs(df.qa_trace.fetched_at) : ''}.</p>` : null}<${Markdown} text=${df.markdown} />`;
}

const TABS = ['overview', 'branches', 'dbflow', 'requests', 'queries', 'artifacts', 'timeline', 'notes'];
function Plain({ children }) { return children; }

// Only this slim bar stays in view: the tabs, plus the key and title once the page heading has scrolled away. The
// full header above it scrolls normally, so a small window keeps most of its height for the content.
function TicketBar({ t, tabs, tab, setTab }) {
  const [stuck, setStuck] = useState(false);
  const mark = useRef(null);
  const topH = () => (EXPORT ? 0 : parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--top-h')) || 60);
  useEffect(() => {
    const h1 = document.getElementById('page-title');
    if (!h1 || !window.IntersectionObserver) return undefined;
    const io = new IntersectionObserver(([e]) => setStuck(!e.isIntersecting), { rootMargin: '-' + topH() + 'px 0px 0px 0px' });
    io.observe(h1);
    return () => io.disconnect();
  }, [t.key]);
  const change = (id) => {
    setTab(id);
    // switching tabs further down the page starts the new tab at its top, right under the bar
    const m = mark.current;
    if (stuck && m) window.scrollTo({ top: m.getBoundingClientRect().top + window.scrollY - topH(), behavior: 'instant' });
  };
  return html`<div ref=${mark}></div><div class=${'tkbar' + (stuck ? ' stuck' : '')}>
    <span class="tkbar-id" aria-hidden="true"><strong>${t.key}</strong>${t.title ? html`<span>${t.title}</span>` : null}</span>
    <${Tabs} tabs=${tabs} active=${tab} onChange=${change} label=${t.key + ' sections'} idBase="tk" /></div>`;
}

export function TicketPage({ route }) {
  const key = route.arg;
  const res = useApi('/api/tickets/' + encodeURIComponent(key));
  const list = useApi(EXPORT ? null : '/api/tickets');
  const [railQ, setRailQ] = useState('');
  const [expTab, setExpTab] = useState('overview');  // an export file has no router: tabs live in component state
  const tab = EXPORT ? expTab : TABS.indexOf(route.params.tab) >= 0 ? route.params.tab : 'overview';
  const setTab = EXPORT ? setExpTab : (id) => setParams({ tab: id === 'overview' ? '' : id });
  useEffect(() => { document.title = key + ' · Chitragupta'; }, [key]);
  useEffect(() => {
    const k = {};
    TABS.forEach((id, i) => { k[String(i + 1)] = { label: 'Ticket tab ' + (i + 1), run: () => setTab(id) }; });
    pageKeys.set(k);
    return () => pageKeys.set({});
  }, [key]);
  const t = res.data;
  const g = (t && t.git) || {};
  const nb = Object.values(g.repos || {}).reduce((a, e) => a + (e.branches || []).length, 0);
  const tabs = t ? [
    { id: 'overview', label: 'Overview', count: t.findings.length || null },
    { id: 'branches', label: 'Branches & PRs', count: nb || null, alert: (g.alerts || []).length > 0 },
    { id: 'dbflow', label: 'DB flow', count: t.dbflow.markdown ? null : 0 },
    { id: 'requests', label: 'Requests', count: t.requests.steps.length },
    { id: 'queries', label: 'Queries', count: t.queries.length },
    { id: 'artifacts', label: 'Artifacts', count: t.artifacts.length },
    { id: 'timeline', label: 'Timeline', count: t.timeline.length },
    { id: 'notes', label: 'Notes' },
  ] : [];
  const all = (list.data && list.data.tickets) || [];
  const rq = railQ.trim().toLowerCase();
  const rail = EXPORT ? null : html`<div class="rail-tools"><label class="sr" for="tk-rail-q">Filter the ticket list</label>
      <input id="tk-rail-q" type="search" placeholder="Filter tickets" value=${railQ} onInput=${(e) => setRailQ(e.currentTarget.value)} autocomplete="off" /></div>
    <ul class="rlist">${all.filter((x) => !rq || (x.key + ' ' + x.title).toLowerCase().indexOf(rq) >= 0).map((x) => html`<li key=${x.key}>
      <a href=${'#/tickets/' + encodeURIComponent(x.key)} aria-current=${x.key === key ? 'page' : null}><span class="sw" style=${{ background: x.key === key ? 'var(--accent)' : 'var(--rule)' }}></span>
        <span><span class="k">${x.key}${(x.alerts || []).length ? html`<${Icon} name="warn" size=${14} label="has delivery alerts" />` : null}</span><span class="t">${x.title}</span></span><span class="m">${fmtMin(x.minutes)}</span></a></li>`)}</ul>`;
  const Wrap = EXPORT ? Plain : Desk;
  return html`<${Wrap} className="ticket-desk">
    <${Load} res=${res} lines=${10}>${(t) => html`
      <div class="tkhead"><div class="tkhead-row"><div>
        ${EXPORT ? null : html`<details class="ticket-switcher"><summary>Tickets / switch ticket</summary>${rail}</details>`}
        <h1 id="page-title" tabindex="-1"><span class="tk-key">${t.key}</span>${t.title ? html`<span class="title">${t.title}</span>` : null}</h1>
        <div class="chips">
          ${t.status ? html`<${Chip} title="Jira status">${t.status}</${Chip}>` : null}
          ${(t.repos || []).map((r) => html`<${Chip} key=${'r' + r} icon="branch" title="Repo">${r}</${Chip}>`)}
          ${(t.branches || []).slice(0, 2).map((b) => html`<${Chip} key=${'b' + b} title=${'Branch ' + b}><span class="mono">${b}</span></${Chip}>`)}
          ${(t.branches || []).length > 2 ? html`<${Chip} title=${t.branches.slice(2).join(', ')}>+${t.branches.length - 2} branches</${Chip}>` : null}
          <${Chip} icon="today" title="Time recorded">${fmtMin(t.minutes)}</${Chip}>
          <${Chip} title="Active">${(t.first || '—') + ' to ' + (t.last || '—')}</${Chip}>
          ${(g.alerts || []).length ? html`<${Status} kind="warn">${plural(g.alerts.length, 'delivery alert')}</${Status}>` : null}
        </div></div>
        ${EXPORT ? html`<p class="exportline">Exported ${String((t.export || {}).at || '').replace('T', ' ').slice(0, 16)} · ${(t.export || {}).mode || 'full'}</p>`
          : html`<div class="pagehead-act"><a class="btn" href=${'/api/tickets/' + encodeURIComponent(t.key) + '/export?mode=full'} download title="Everything as recorded, for your own use"><${Icon} name="download" size=${16} />Export</a>
            <a class="btn quiet" href=${'/api/tickets/' + encodeURIComponent(t.key) + '/export?mode=shareable'} download title="Redacted for sharing: no links, hosts, paths, payloads, query results or QA values">Export to share</a>
            <${ShareButton} k=${t.key} /></div>`}
        </div></div>
      ${t.tldr ? html`<p class="ticket-summary">${t.tldr}</p>` : null}
      ${tab === 'branches' ? null : html`<${GitAlerts} g=${t.git} />`}
      <${TicketBar} t=${t} tabs=${tabs} tab=${tab} setTab=${setTab} />
      <${TabPanel} idBase="tk" active=${tab}>
        ${tab === 'overview' ? html`<${Overview} t=${t} />` : null}
        ${tab === 'branches' ? html`<${GitTab} g=${t.git} />` : null}
        ${tab === 'dbflow' ? html`<${DbFlow} df=${t.dbflow} />` : null}
        ${tab === 'requests' ? html`<${Requests} rq=${t.requests} />` : null}
        ${tab === 'queries' ? html`<${Queries} qs=${t.queries} />` : null}
        ${tab === 'artifacts' ? html`<${Artifacts} list=${t.artifacts} />` : null}
        ${tab === 'timeline' ? html`<${Timeline} days=${t.timeline} />` : null}
        ${tab === 'notes' ? html`<${Notes} t=${t} />` : null}
      </${TabPanel}>`}</${Load}>
  </${Wrap}>`;
}
