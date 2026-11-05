// The action-first band shared by Today and My progress (/api/progress): one line about the sources behind it, the
// next useful action (or setting up, or recovering a source, or a checked "nothing needs you"), a compact row of the
// day's numbers, and your active tickets. Each number opens its own evidence: what it counts, the events that make it
// up, what was left out and why, and how current its sources are. Uncertainty stays on the number it affects.
import { useState } from '/vendor/hooks.module.js';
import { html } from './html.js';
import { fmtTs, plural, isHttp } from './core.js';
import { Icon, Status, Dialog, TicketLink, Scroll } from './ui.js';
import { CodeText } from './release.js';

// metric status -> [Status kind, words]; a complete count shows no label at all (the status line covers it)
export const QUALIFIER = {
  partial: ['warn', 'partly collected'], stale: ['warn', 'not current'], unknown: ['off', 'unknown'],
};
export const DEFINITION = {
  commits: 'Distinct non-merge commits whose author email is one of your confirmed emails, counted on the day of their '
    + 'author time in your profile’s time zone. A change seen on several branches, clones, rebased or cherry-picked '
    + 'counts once. Merge commits are left out so integration isn’t counted twice; commits you only co-authored are '
    + 'listed apart and not in this number.',
  lines: 'Lines added and deleted in the commits you authored without co-authors, after the exclusions in your profile '
    + '(lockfiles, vendored, generated and build output). It is committed change volume, tests and docs included: not '
    + 'quality, effort or productivity. Uncommitted work isn’t included; commits with co-authors are shared change volume, '
    + 'shown apart and never split per person.',
  prs_raised: 'Pull requests your confirmed Git host accounts opened, by creation time. Drafts count and are labelled; '
    + 'reopening a PR or marking a draft ready doesn’t make it new. Someone else’s PR, such as a shared integration PR, '
    + 'never counts.',
  prs_merged: 'Your pull requests merged that day, by merge time, whoever clicked merge. Kept apart from PRs raised.',
  tickets_completed: 'Distinct tickets moved into a done status that day while they were yours (assigned to your '
    + 'confirmed Jira account, or included by you), from Jira’s status history as the evening check reads it. A ticket '
    + 'reopened and completed again the same day counts once.',
};
export const LABEL = { commits: 'Authored commits', lines: 'Committed changes', prs_raised: 'PRs raised',
  prs_merged: 'PRs merged', tickets_completed: 'Tickets completed' };
const ORDER = ['commits', 'lines', 'prs_raised', 'prs_merged', 'tickets_completed'];

function metricOf(m, id) { return id === 'lines' ? m.lines : m[id]; }
function valueText(m, id) {
  if (id === 'lines') return m.lines.added == null ? '—' : '+' + m.lines.added + ' / −' + m.lines.deleted;
  const v = m[id].value;
  return v == null ? '—' : String(v);
}
function valueSr(m, id) {
  if (id === 'lines') return m.lines.added == null ? 'unknown' : m.lines.added + ' lines added, ' + m.lines.deleted + ' lines deleted';
  return m[id].value == null ? 'unknown' : String(m[id].value);
}

function PrLink({ p }) {
  const t = p.repo + ' #' + p.number;
  return isHttp(p.url) ? html`<a href=${p.url} rel="noreferrer noopener" target="_blank">${t}<span class="sr"> (opens the Git host)</span></a>` : html`<span>${t}</span>`;
}

// The events of one metric: counted ones first, then those that are related but not in this number, with why.
function events(m, id) {
  const ev = m.evidence || {};
  const commits = ev.commits || [];
  if (id === 'commits') {
    return { counted: commits.filter((c) => c.role === 'author'), apart: commits.filter((c) => c.role === 'coauthor')
      .map((c) => Object.assign({}, c, { why: 'you are only a co-author' })).concat(ev.not_counted || []), kind: 'commit' };
  }
  if (id === 'lines') {
    return { counted: commits.filter((c) => c.role === 'author' && !c.shared), apart: commits.filter((c) => c.shared || c.role === 'coauthor')
      .map((c) => Object.assign({}, c, { why: c.shared ? 'shared with co-authors: shown apart, not split' : 'you are only a co-author' })), kind: 'commit' };
  }
  if (id === 'prs_raised') return { counted: ev.prs_raised || [], apart: [], kind: 'pr', at: 'created_at' };
  if (id === 'prs_merged') return { counted: ev.prs_merged || [], apart: [], kind: 'pr', at: 'merged_at' };
  return { counted: ev.tickets_completed || [], apart: [], kind: 'ticket' };
}

function EventRows({ list, kind, at }) {
  if (kind === 'commit') {
    return html`<${Scroll} label="Commits"><table><caption class="sr">Commits</caption>
      <thead><tr><th scope="col">Commit</th><th scope="col">Subject</th><th scope="col" class="num">Lines</th><th scope="col">Note</th></tr></thead>
      <tbody>${list.map((c) => html`<tr key=${c.repo + c.sha}><td class="mono small">${c.repo} ${c.sha}<div class="muted">${fmtTs(c.at)}</div></td>
        <td>${c.subject}${c.ticket ? html` <${TicketLink} k=${c.ticket} />` : null}</td>
        <td class="num nowrap">+${c.added} / −${c.deleted}</td>
        <td class="small">${[c.why, c.excluded && c.excluded.files ? plural(c.excluded.files, 'excluded file') + ' (+' + c.excluded.added + ' / −' + c.excluded.deleted + ' left out)' : '',
          c.binary_files ? plural(c.binary_files, 'binary file') + ', no line counts' : ''].filter(Boolean).join(' · ') || '—'}</td></tr>`)}</tbody></table></${Scroll}>`;
  }
  if (kind === 'pr') {
    return html`<ul class="other-list">${list.map((p) => html`<li key=${p.repo + p.number}><span><${PrLink} p=${p} /> ${p.title}${p.ticket ? html` <${TicketLink} k=${p.ticket} />` : null}</span>
      <span class="small muted">${fmtTs(p[at])}${p.draft ? ' · draft' : ''}</span></li>`)}</ul>`;
  }
  return html`<ul class="other-list">${list.map((t) => html`<li key=${t.key}><span><${TicketLink} k=${t.key} /> moved to ${t.to}</span><span class="small muted">${fmtTs(t.at)}</span></li>`)}</ul>`;
}

export function MetricDialog({ id, m, sources, onClose }) {
  if (!id) return null;
  const x = metricOf(m, id), ev = events(m, id), q = QUALIFIER[x.status];
  const ex = m.excluded || {};
  return html`<${Dialog} open=${!!id} title=${LABEL[id] + ': ' + valueText(m, id)} onClose=${onClose} wide>
    <div class="mdrill">
      <p class="small">${q ? html`<${Status} kind=${q[0]} quiet>${q[1][0].toUpperCase() + q[1].slice(1)}</${Status}> ${x.why}`
        : x.status === 'zero' ? 'Every source for this day was read: none recorded.' : 'Every source for this day was read.'}</p>
      <h3 class="small">What it counts</h3><p class="small">${DEFINITION[id]}</p>
      <h3 class="small">Counted (${ev.counted.length})</h3>
      ${ev.counted.length ? html`<${EventRows} list=${ev.counted} kind=${ev.kind} at=${ev.at} />` : html`<p class="muted small">Nothing counted.</p>`}
      ${id === 'lines' && ex.files ? html`<p class="small">Left out by your exclusions: +${ex.added} / −${ex.deleted} in ${plural(ex.files, 'file')}.</p>` : null}
      ${ev.apart.length ? html`<h3 class="small">Seen but not in this number (${ev.apart.length})</h3><${EventRows} list=${ev.apart} kind=${ev.kind} at=${ev.at} />` : null}
      ${id === 'prs_raised' && m.prs_raised.drafts ? html`<p class="small muted">${plural(m.prs_raised.drafts, 'draft')} included.</p>` : null}
      <${SourceTable} s=${sources} kind=${id === 'tickets_completed' ? 'jira' : id.indexOf('prs') === 0 ? 'prs' : 'commits'} />
    </div></${Dialog}>`;
}

function SourceTable({ s, kind }) {
  if (!s) return null;
  if (kind === 'jira') {
    const j = s.jira || {}, c = j.connection || {};
    return html`<p class="small"><strong>Source:</strong> Jira status history from the evening check · ${c.why || j.why || ''}</p>`;
  }
  const rows = (s.repos || []).map((r) => [r.name, r[kind]]);
  return html`<h3 class="small">Sources</h3><ul class="other-list">${rows.map(([n, x]) => html`<li key=${n}><span>${n}</span>
    <span class="small">${x.status}${x.at ? ', read ' + fmtTs(x.at) : ''}${x.why ? html` <span class="muted">· ${x.why}</span>` : null}</span></li>`)}</ul>
    <p class="small muted">History ${s.coverage.label}.</p>`;
}

export function MetricRow({ m, sources }) {
  const [open, setOpen] = useState('');
  return html`<div class="mrow" role="list" aria-label="This day's numbers">
      ${ORDER.map((id) => {
        const x = metricOf(m, id), q = QUALIFIER[x.status];
        return html`<div role="listitem" key=${id}><button type="button" class="mtile" aria-haspopup="dialog" onClick=${() => setOpen(id)}
          aria-label=${LABEL[id] + ': ' + valueSr(m, id) + (q ? ', ' + q[1] : '') + '. Show what it counts and the evidence'}>
          <span class="mtile-v" aria-hidden="true">${valueText(m, id)}</span>
          <span class="mtile-l" aria-hidden="true">${LABEL[id]}</span>
          ${q ? html`<span class=${'mtile-q ' + q[0]} aria-hidden="true">${q[1]}</span>` : null}</button></div>`;
      })}</div>
    <${MetricDialog} id=${open} m=${m} sources=${sources} onClose=${() => setOpen('')} />`;
}

const ST_KIND = { ok: 'ok', attention: 'warn', setup: 'info', collecting: 'wait' };
export function SourceLine({ s }) {
  if (!s) return null;
  return html`<p class=${'srcline ' + s.state} role=${s.state === 'attention' ? 'status' : null}>
    <${Status} kind=${ST_KIND[s.state] || 'info'} quiet>${s.text}</${Status}>
    ${s.href && s.state === 'attention' ? html` <a href=${s.href}>See what to do</a>` : null}</p>`;
}

function ActionLinks({ a }) {
  const out = [];
  if (a.ticket) out.push(html`<${TicketLink} k=${a.ticket} />`);
  if (isHttp(a.url)) out.push(html`<a href=${a.url} rel="noreferrer noopener" target="_blank">${a.kind === 'check' ? 'Check the PR now' : 'Open the PR'}<span class="sr"> (opens the Git host)</span></a>`);
  else if (a.url && a.url.indexOf('#/') === 0) out.push(html`<a href=${a.url}>${a.kind === 'setup' ? 'Set it up' : 'Fix it'}</a>`);
  return out.length ? html`<span class="nact-links">${out.map((x, i) => html`<span key=${i}>${x}</span>`)}</span>` : null;
}

const KIND_WORD = { setup: 'Set up', recover: 'Recover a source', check: 'Check', pr: 'Your PR', release: 'Release', delivery: 'Delivery' };
// The next action first: setup, then recovery, then your work; or what was checked when nothing needs you.
export function NextAction({ na, status }) {
  if (!na) return null;
  if (na.day_off) return html`<div class="nact quiet"><p>${na.note}</p></div>`;
  const acts = na.actions || [];
  if (!acts.length) {
    return html`<div class=${'nact' + (na.verified ? ' quiet' : '')}><p class="nact-h">${na.verified ? html`<${Icon} name="ok" size=${16} /> ` : null}${na.note}</p>
      ${!na.verified && status && status.href ? html`<p class="small"><a href=${status.href}>See which sources are missing</a></p>` : null}</div>`;
  }
  const [first, ...rest] = acts;
  return html`<div class=${'nact ' + first.kind}>
    <p class="nact-k">${KIND_WORD[first.kind] || 'Next'}</p>
    <p class="nact-h"><${CodeText} text=${first.text} /></p>
    <p class="small muted">${first.why}${first.why ? ' · ' : ''}<${ActionLinks} a=${first} /></p>
    ${rest.length ? html`<ul class="nact-more">${rest.map((a, i) => html`<li key=${i}><${CodeText} text=${a.text} /> <${ActionLinks} a=${a} /></li>`)}</ul>` : null}
    ${na.more ? html`<p class="small muted">and ${na.more} more on the Tickets and Releases pages</p>` : null}</div>`;
}

export function ActiveTickets({ t, limit }) {
  if (!t) return null;
  const cur = (t.current || []).slice(0, limit || 6);
  return html`<section class="sec" aria-labelledby="sec-active"><div class="sec-head"><h2 id="sec-active">Your active tickets</h2>
      <p class="sub">${(t.current || []).length ? plural(t.current.length, 'ticket') + ' yours now' : 'Why each one is yours, and what is next'}</p></div>
    ${cur.length ? html`<ul class="tlist">${cur.map((r) => html`<li key=${r.key}>
        <span class="tl-main"><${TicketLink} k=${r.key} /> <span class="tl-t">${r.title}</span></span>
        <span class="tl-meta">${r.status ? html`<span class="tl-s">${r.status}</span>` : null}
          ${r.next ? html`<span class=${'tl-n' + (r.blocked ? ' blocked' : '')}>${r.blocked ? 'Blocked: ' : 'Next: '}${r.next}</span>` : html`<span class="muted">${r.reason}</span>`}</span></li>`)}</ul>`
      : html`<p class="muted small">No ticket is confirmed as yours: Chitragupta needs your Jira account (on the <a href="#/profile">Profile</a> page) to see assignments, or a ticket you include yourself.</p>`}
    ${(t.current || []).length > cur.length ? html`<p class="small"><a href="#/tickets?mine=1">All ${t.current.length} of your tickets</a></p>` : null}
    ${(t.reassigned || []).length ? html`<details class="more"><summary>${plural(t.reassigned.length, 'ticket')} reassigned away from you (your earlier work on them is kept)</summary>
      <ul class="other-list">${t.reassigned.map((r) => html`<li key=${r.key}><span><${TicketLink} k=${r.key} /> ${r.title}</span><span class="small muted">${r.reason}</span></li>`)}</ul></details>` : null}
  </section>`;
}

// The whole band: what Today and My progress open with.
export function FocusBand({ d, children }) {
  if (!d) return null;
  return html`<section class="focus" aria-labelledby="focus-h"><h2 id="focus-h" class="sr">What needs you</h2>
    <${SourceLine} s=${d.status} />
    <${NextAction} na=${d.next_actions} status=${d.status} />
    <${MetricRow} m=${d.metrics} sources=${d.sources} />
    ${(d.celebrations || []).length ? html`<p class="small milestone-line">${d.celebrations[0]}${d.celebrations.length > 1 ? html` <span class="muted">(+${d.celebrations.length - 1} more on My progress)</span>` : null}</p>` : null}
    ${d.note ? html`<p class="small muted">${d.note}</p>` : null}
    ${children}</section>`;
}
