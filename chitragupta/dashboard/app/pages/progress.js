// My progress: one day's authored commits, committed changes, PRs raised and merged and completed tickets, from the
// recorded evidence only (progress.py via /api/progress*), each with what it counts, its source status and the evidence
// behind it; then next steps, your tickets, weekly wins, your own reflection, a month calendar and personal records.
// The numbers describe observed work in your profile's scope: never quality, effort or a ranking. The day lives in the
// address: #/progress?date=YYYY-MM-DD.
import { useEffect, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, setParams, runAction, toast, fmtDay, fmtTs, monthName, pad, plural, addDays, parseDay, isHttp } from '../lib/core.js';
import { Icon, Status, Empty, Load, PageHead, TicketLink, Scroll, CopyButton, Markdown } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
// status -> [Status kind, words]; the words always say it, colour never alone
const ST = {
  known: ['ok', 'Recorded'], zero: ['off', 'None recorded'], partial: ['warn', 'Partial'],
  stale: ['warn', 'Not current'], unknown: ['off', 'Unknown'],
};
const KIND = { workday: 'Workday', half_day: 'Half day', leave: 'Leave', holiday: 'Holiday', weekend: 'Weekend', extra_workday: 'Extra workday' };
const EXPLAIN = {
  commits: 'Distinct non-merge commits whose author email is one of your confirmed emails, counted on the day of their '
    + 'author time in your profile’s time zone. A change seen on several branches, rebased or cherry-picked counts once. '
    + 'Merge commits are left out so integration isn’t counted twice; commits you only co-authored are listed apart.',
  lines: 'Lines added and deleted in the commits you authored without co-authors, after the exclusions in your profile '
    + '(lockfiles, vendored, generated and build output). It is committed change volume, tests and docs included: not '
    + 'quality, effort or productivity. Editing the same lines again counts again, and uncommitted work isn’t included. '
    + 'Commits with co-authors are shown as shared change volume, never split per person.',
  prs_raised: 'Pull requests your confirmed Git host account opened, by creation time. Drafts count and are labelled; '
    + 'reopening a PR or marking a draft ready doesn’t make it new. Someone else’s PR, such as a shared integration PR, '
    + 'never counts.',
  prs_merged: 'Your pull requests merged that day, by merge time, whoever clicked merge. Kept apart from PRs raised.',
  tickets_completed: 'Distinct tickets moved into a done status that day while they were yours (assigned to your '
    + 'confirmed Jira account, or included by you), from Jira’s status history as the evening check reads it for the '
    + 'tickets you worked on. A ticket reopened and completed again the same day counts once.',
};

function thisMonth(day) { return (day || '').slice(0, 7); }
function shiftMonth(ym, n) { const p = ym.split('-').map(Number); const d = new Date(p[0], p[1] - 1 + n, 1); return d.getFullYear() + '-' + pad(d.getMonth() + 1); }
function val(v) { return v == null ? '—' : String(v); }

function StatusLine({ m }) {
  const s = ST[m.status] || ST.unknown;
  return html`<p class="small ptile-st"><${Status} kind=${s[0]} quiet>${s[1]}</${Status}>${m.why ? html`<span class="muted"> · ${m.why}</span>` : null}</p>`;
}

function Tile({ id, label, value, sr, m, children }) {
  return html`<section class="ptile" aria-labelledby=${'pt-' + id}>
    <h3 id=${'pt-' + id}>${label}</h3>
    <p class="ptile-v"><span aria-hidden=${sr ? 'true' : null}>${value}</span>${sr ? html`<span class="sr">${sr}</span>` : null}</p>
    <${StatusLine} m=${m} />
    ${children}
    <details class="more"><summary>What this counts</summary><p class="small">${EXPLAIN[id]}</p></details>
  </section>`;
}

function Tiles({ m }) {
  const ln = m.lines;
  const lineText = ln.added == null ? '—' : '+' + ln.added + ' / −' + ln.deleted;
  const lineSr = ln.added == null ? 'unknown' : ln.added + ' lines added, ' + ln.deleted + ' lines deleted';
  const ex = m.excluded || {};
  return html`<div class="ptiles">
    <${Tile} id="commits" label="Authored commits" value=${val(m.commits.value)} sr=${m.commits.value == null ? 'unknown' : null} m=${m.commits}>
      ${m.coauthored && m.coauthored.commits ? html`<p class="small muted">Co-authored only: ${plural(m.coauthored.commits, 'commit')}, listed apart.</p>` : null}</${Tile}>
    <${Tile} id="lines" label="Committed changes" value=${lineText} sr=${lineSr} m=${ln}>
      ${m.shared && m.shared.commits ? html`<p class="small muted">Shared with co-authors: ${plural(m.shared.commits, 'commit')}, +${m.shared.added} / −${m.shared.deleted} (not split).</p>` : null}
      ${ex.files ? html`<p class="small muted">Left out: +${ex.added} / −${ex.deleted} in ${plural(ex.files, 'excluded file')}.</p>` : null}
      ${m.binary_files ? html`<p class="small muted">${plural(m.binary_files, 'binary file')}: no line counts.</p>` : null}</${Tile}>
    <${Tile} id="prs_raised" label="PRs raised" value=${val(m.prs_raised.value)} sr=${m.prs_raised.value == null ? 'unknown' : null} m=${m.prs_raised}>
      ${m.prs_raised.drafts ? html`<p class="small muted">${plural(m.prs_raised.drafts, 'draft')} included.</p>` : null}</${Tile}>
    <${Tile} id="prs_merged" label="PRs merged" value=${val(m.prs_merged.value)} sr=${m.prs_merged.value == null ? 'unknown' : null} m=${m.prs_merged} />
    <${Tile} id="tickets_completed" label="Tickets completed" value=${val(m.tickets_completed.value)} sr=${m.tickets_completed.value == null ? 'unknown' : null} m=${m.tickets_completed} />
  </div>`;
}

function PrCell({ p }) {
  const t = '#' + p.number;
  return isHttp(p.url) ? html`<a href=${p.url} rel="noreferrer noopener" target="_blank">${t}<span class="sr"> (opens the Git host)</span></a>` : html`<span>${t}</span>`;
}

function Evidence({ m }) {
  const ev = m.evidence || {};
  const commits = ev.commits || [], skipped = ev.not_counted || [], raised = ev.prs_raised || [], merged = ev.prs_merged || [];
  const done = ev.tickets_completed || [], unresolved = m.unresolved || [];
  const n = commits.length + raised.length + merged.length + done.length;
  if (!n && !skipped.length && !unresolved.length) return null;
  return html`<details class="more pevidence"><summary>The evidence: ${plural(commits.length, 'commit')}, ${plural(raised.length + merged.length, 'PR')}${done.length ? ', ' + plural(done.length, 'ticket') : ''}${skipped.length ? ', ' + skipped.length + ' not counted' : ''}</summary>
    ${commits.length ? html`<${Scroll} label="Commits counted"><table><caption>Commits</caption>
      <thead><tr><th scope="col">Repo</th><th scope="col">Commit</th><th scope="col">Subject</th><th scope="col" class="num">Lines</th><th scope="col">Notes</th></tr></thead>
      <tbody>${commits.map((c) => html`<tr key=${c.repo + c.sha}><td>${c.repo}</td><td class="mono">${c.sha}</td>
        <td>${c.subject}${c.ticket ? html` <${TicketLink} k=${c.ticket} />` : null}</td>
        <td class="num">+${c.added} / −${c.deleted}</td>
        <td class="small">${[c.role === 'coauthor' ? 'co-authored only' : c.shared ? 'shared with co-authors' : '',
          c.excluded && c.excluded.files ? plural(c.excluded.files, 'excluded file') : '', c.binary_files ? plural(c.binary_files, 'binary file') : ''].filter(Boolean).join(' · ') || '—'}</td></tr>`)}</tbody></table></${Scroll}>` : null}
    ${skipped.length ? html`<${Scroll} label="Commits not counted"><table><caption>Not counted</caption>
      <thead><tr><th scope="col">Repo</th><th scope="col">Commit</th><th scope="col">Subject</th><th scope="col">Why</th></tr></thead>
      <tbody>${skipped.map((c) => html`<tr key=${c.repo + c.sha}><td>${c.repo}</td><td class="mono">${c.sha}</td><td>${c.subject}</td><td class="small">${c.why}</td></tr>`)}</tbody></table></${Scroll}>` : null}
    ${raised.length || merged.length ? html`<${Scroll} label="Pull requests"><table><caption>Pull requests</caption>
      <thead><tr><th scope="col">Repo</th><th scope="col">PR</th><th scope="col">Title</th><th scope="col">What happened</th></tr></thead>
      <tbody>${raised.map((p) => html`<tr key=${'r' + p.repo + p.number}><td>${p.repo}</td><td><${PrCell} p=${p} /></td><td>${p.title}${p.ticket ? html` <${TicketLink} k=${p.ticket} />` : null}</td>
          <td class="small">Raised ${fmtTs(p.created_at)}${p.draft ? ' · draft' : ''}</td></tr>`)}
        ${merged.map((p) => html`<tr key=${'m' + p.repo + p.number}><td>${p.repo}</td><td><${PrCell} p=${p} /></td><td>${p.title}${p.ticket ? html` <${TicketLink} k=${p.ticket} />` : null}</td>
          <td class="small">Merged ${fmtTs(p.merged_at)}</td></tr>`)}</tbody></table></${Scroll}>` : null}
    ${done.length ? html`<p class="small">Completed: ${done.map((t, i) => html`<span key=${t.key}>${i ? ', ' : ''}<${TicketLink} k=${t.key} /> (${t.to}, ${fmtTs(t.at)})</span>`)}</p>` : null}
    ${unresolved.length ? html`<p class="small"><${Status} kind="warn" quiet>Not counted on any day:</${Status}> ${unresolved.map((u) => u.repo + ' ' + u.what + ' (' + u.why + ')').join('; ')}</p>` : null}
  </details>`;
}

function Milestones({ list, animate }) {
  if (!list || !list.length) return null;
  return html`<div class=${'milestone' + (animate ? ' animate' : '')} role="status"><${Icon} name="ok" size=${18} />
    <div>${list.map((s, i) => html`<p key=${i}>${s}</p>`)}</div></div>`;
}

function NextSteps({ na }) {
  if (!na) return null;
  return html`<section class="sec" aria-labelledby="sec-next"><div class="sec-head"><h2 id="sec-next">Next steps</h2>
      <p class="sub">From your own PRs and tickets; nothing is inferred from a quiet day</p></div>
    ${na.day_off ? html`<p class="note">${na.note}</p>` : !na.actions.length ? html`<p class="muted small">${na.note}</p>` : html`
      <ul class="pnext">${na.actions.map((a, i) => html`<li key=${i}><span>${a.text}</span>
        <span class="small muted">${a.why}${a.ticket ? html` · <${TicketLink} k=${a.ticket} />` : null}${isHttp(a.url) ? html` · <a href=${a.url} rel="noreferrer noopener" target="_blank">Open the PR<span class="sr"> (opens the Git host)</span></a>` : null}</span></li>`)}</ul>
      ${na.more ? html`<p class="muted small">and ${na.more} more on the Tickets and Releases pages</p>` : null}`}</section>`;
}

function MyTickets({ t }) {
  if (!t) return null;
  return html`<section class="sec" aria-labelledby="sec-mine"><div class="sec-head"><h2 id="sec-mine">Your current tickets</h2>
      <p class="sub">${t.not_yours ? plural(t.not_yours, 'other ticket') + ' recorded but not yours now' : 'Why each one is yours'}</p></div>
    ${t.current.length ? html`<ul class="other-list">${t.current.map((r) => html`<li key=${r.key}><span><${TicketLink} k=${r.key} /> ${r.title}</span><span class="small muted">${r.reason}</span></li>`)}</ul>`
      : html`<p class="muted small">No ticket is confirmed as yours: Chitragupta needs your Jira account (on the <a href="#/profile">Profile</a> page) to see assignments, or a ticket you include yourself.</p>`}
    ${t.reassigned.length ? html`<details class="more"><summary>${plural(t.reassigned.length, 'ticket')} reassigned away from you (your earlier work on them is kept)</summary>
      <ul class="other-list">${t.reassigned.map((r) => html`<li key=${r.key}><span><${TicketLink} k=${r.key} /> ${r.title}</span><span class="small muted">${r.reason}</span></li>`)}</ul></details>` : null}
  </section>`;
}

function Reflection({ day, rec, on }) {
  const [text, setText] = useState((rec && rec.text) || '');
  const [busy, setBusy] = useState(false);
  useEffect(() => { setText((rec && rec.text) || ''); }, [day, rec && rec.updated_at]);
  if (!on) return null;
  const save = (e) => {
    e.preventDefault();
    setBusy(true);
    runAction('reflection-save', { date: day, text }).then(() => toast('Saved your note for ' + fmtDay(day, true), 'ok'),
      (err) => toast(err.message, 'bad')).finally(() => setBusy(false));
  };
  return html`<section class="sec" aria-labelledby="sec-refl"><div class="sec-head"><h2 id="sec-refl">Reflection</h2>
      <p class="sub">Self-reported, kept on this machine, shown apart from the recorded evidence</p></div>
    <form class="preflect" onSubmit=${save}><label for="refl-text">What moved forward ${day ? 'on ' + fmtDay(day, true) : 'today'}?</label>
      <p class="muted small" id="refl-help">Design, debugging, reviews, learning and helping teammates often leave no commit. A line or two is enough.</p>
      <textarea id="refl-text" aria-describedby="refl-help" rows="3" maxlength="1000" value=${text} onInput=${(e) => setText(e.currentTarget.value)}></textarea>
      <div class="btnline"><button type="submit" class="btn" disabled=${busy}>${busy ? 'Saving…' : 'Save note'}</button>
        ${rec && rec.updated_at ? html`<span class="muted small">Saved ${fmtTs(rec.updated_at)}</span>` : null}</div></form></section>`;
}

function Wins({ day }) {
  const res = useApi('/api/progress/wins' + (day ? '?date=' + day : ''));
  return html`<section class="sec" aria-labelledby="sec-wins"><div class="sec-head"><h2 id="sec-wins">Weekly wins</h2>
      <p class="sub">${res.data ? fmtDay(res.data.start, true) + ' to ' + fmtDay(res.data.end, true) + ' · facts with links, no model' : 'This week'}</p>
      <span class="grow"></span>${res.data ? html`<${CopyButton} text=${res.data.copy_text} label="Copy" what="the weekly wins" />` : null}</div>
    <${Load} res=${res} lines=${4}>${(w) => html`<div class="pwins"><${Markdown} text=${w.markdown} /></div>`}</${Load}></section>`;
}

const WDS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
function cellLabel(c) {
  const head = fmtDay(c.date) + (c.kind && c.kind !== 'workday' ? ', ' + (KIND[c.kind] || c.kind) : '');
  if (c.activity == null) return head + ': not known (outside the collected history or no source)';
  if (!c.activity) return head + ': no commits or PRs recorded';
  const parts = [];
  if (c.commits) parts.push(plural(c.commits, 'commit'));
  if (c.prs_raised) parts.push(plural(c.prs_raised, 'PR') + ' raised');
  if (c.prs_merged) parts.push(plural(c.prs_merged, 'PR') + ' merged');
  if (c.tickets_completed) parts.push(plural(c.tickets_completed, 'ticket') + ' completed');
  return head + ': ' + plural(c.activity, 'activity event') + ' (' + parts.join(', ') + ')';
}

function Calendar({ ym, sel, onMonth }) {
  const res = useApi('/api/progress/calendar?month=' + ym);
  const prev = shiftMonth(ym, -1), next = shiftMonth(ym, 1);
  return html`<section class="sec" aria-labelledby="sec-cal"><div class="sec-head"><h2 id="sec-cal">Activity calendar</h2>
      <p class="sub">${monthName(ym)} · distinct commits, PRs and completed tickets per day; a count, not a score</p>
      <span class="grow"></span><div class="month-actions">
        <button type="button" class="btn quiet" onClick=${() => onMonth(prev)} aria-label=${'Previous month: ' + monthName(prev)}><${Icon} name="left" size=${16} />${monthName(prev).split(' ')[0]}</button>
        <button type="button" class="btn quiet" onClick=${() => onMonth(next)} aria-label=${'Next month: ' + monthName(next)}>${monthName(next).split(' ')[0]}<${Icon} name="right" size=${16} /></button></div></div>
    <${Load} res=${res} lines=${5}>${(cal) => html`<div class="heat pcal" role="grid" aria-label=${'Activity per day in ' + monthName(cal.month)}>
      <div role="row" style="display:contents"><span role="columnheader" class="wd"><span class="sr">Week</span></span>${WDS.map((w) => html`<span role="columnheader" class="wd" key=${w}>${w}</span>`)}</div>
      ${cal.weeks.map((wk, i) => html`<div role="row" style="display:contents" key=${i}><span role="rowheader" class="wk"><span class="sr">Week ${i + 1}</span></span>
        ${wk.map((c, j) => !c ? html`<span role="gridcell" class="dcell out" key=${'x' + j}></span>`
          : html`<span role="gridcell" key=${c.date}><button type="button" class=${'dcell' + (c.level ? ' h' + c.level : '') + (c.activity == null ? ' unknown' : '') + (c.kind === 'weekend' || c.kind === 'leave' || c.kind === 'holiday' ? ' weekend' : '')}
            aria-pressed=${sel === c.date ? 'true' : 'false'} aria-label=${cellLabel(c)} onClick=${() => setParams({ date: c.date })}>
            <span class="dn">${parseDay(c.date).getDate()}</span>
            ${c.kind && ['leave', 'holiday', 'half_day'].indexOf(c.kind) >= 0 ? html`<span class="dk">${KIND[c.kind]}</span>` : null}
            <span class="dh">${c.activity == null ? '?' : String(c.activity)}</span></button></span>`)}</div>`)}
    </div>
    <p class="heatkey">Each day shows its count of distinct activity events; “?” means not known (before the collected history, or a source that isn’t set up). Days off are striped; nothing is expected on them.</p>`}</${Load}></section>`;
}

function Records({ day }) {
  const res = useApi('/api/progress/records');
  return html`<section class="sec" aria-labelledby="sec-rec"><div class="sec-head"><h2 id="sec-rec">Personal records</h2>
      <p class="sub">${res.data ? 'Your best days ' + res.data.coverage.label + '; the earliest day wins a tie' : 'Your best days'}</p></div>
    <${Load} res=${res} lines=${4}>${(r) => html`<ul class="precords">${r.records.map((x) => html`<li key=${x.id}>
      <h3>${x.label}</h3>
      ${x.value ? html`<p class="ptile-v">${x.value}</p>
        <p class="small"><a href=${'#/progress?date=' + x.date} aria-current=${x.date === day ? 'true' : null}>${fmtDay(x.date, true)}</a>${x.ties ? ' · tied with ' + plural(x.ties, 'other day') : ''}</p>
        ${x.breakdown ? html`<p class="small muted">${plural(x.breakdown.commits || 0, 'commit')} · ${x.breakdown.prs_raised || 0} raised · ${x.breakdown.prs_merged || 0} merged${x.breakdown.tickets_completed ? ' · ' + plural(x.breakdown.tickets_completed, 'ticket') + ' completed' : ''}</p>` : null}`
        : html`<p class="small muted">${x.why || 'None yet'}</p>`}
      <p class="small muted">${x.coverage}</p>
      ${x.definition ? html`<details class="more"><summary>How this is counted</summary><p class="small">${x.definition}</p></details>` : null}</li>`)}</ul>`}</${Load}></section>`;
}

function Sources({ s }) {
  if (!s) return null;
  const rows = s.repos || [];
  return html`<details class="more psources"><summary>Where these numbers come from</summary>
    <p class="small">History: ${s.coverage.label}. Your identity and scope are on the <a href="#/profile">Profile</a> page, where you can correct them.</p>
    ${rows.length ? html`<${Scroll} label="Sources per repository"><table><caption class="sr">Sources per repository</caption>
      <thead><tr><th scope="col">Repository</th><th scope="col">Commits</th><th scope="col">Pull requests</th></tr></thead>
      <tbody>${rows.map((r) => html`<tr key=${r.name}><th scope="row">${r.name}</th>
        <td class="small">${r.commits.status}${r.commits.at ? ', read ' + fmtTs(r.commits.at) : ''}${r.commits.why ? html`<div class="muted">${r.commits.why}</div>` : null}</td>
        <td class="small">${r.prs.status}${r.prs.at ? ', read ' + fmtTs(r.prs.at) : ''}${r.prs.why ? html`<div class="muted">${r.prs.why}</div>` : null}</td></tr>`)}</tbody></table></${Scroll}>`
      : html`<p class="small muted">No repository is in your profile's scope yet.</p>`}
  </details>`;
}

export function ProgressPage({ route }) {
  const want = DAY_RE.test(route.params.date || '') ? route.params.date : '';
  const res = useApi('/api/progress' + (want ? '?date=' + want : ''));
  const d = res.data;
  const day = want || (d && d.date) || '';
  const [ym, setYm] = useState('');
  const month = MONTH_RE.test(ym) ? ym : thisMonth(day);
  useEffect(() => { document.title = 'My progress · Chitragupta'; }, []);
  useEffect(() => { setYm(''); }, [want]);
  useEffect(() => {
    if (!day) return undefined;
    pageKeys.set({ p: { label: 'Previous day', run: () => setParams({ date: addDays(day, -1) }) }, n: { label: 'Next day', run: () => setParams({ date: addDays(day, 1) }) } });
    return () => pageKeys.set({});
  }, [day]);
  const today = d && d.today;
  const name = d && d.profile && d.profile.display_name;
  const reduce = typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const tools = html`<div class="day-tools"><div class="daynav">
      <button type="button" class="btn" onClick=${() => day && setParams({ date: addDays(day, -1) })}><${Icon} name="left" size=${16} />Previous day</button>
      ${want ? html`<button type="button" class="btn" onClick=${() => setParams({ date: '' })}>Today</button>` : null}
      ${want ? html`<button type="button" class="btn" onClick=${() => setParams({ date: addDays(day, 1) })}>Next<${Icon} name="right" size=${16} /></button>` : null}</div>
    <label class="field"><span class="sr">Show a day</span><input type="date" value=${day} onChange=${(e) => { const v = e.currentTarget.value; if (DAY_RE.test(v)) setParams({ date: v }); }} /></label></div>`;
  return html`<${Desk} className="progress-desk">
    <${PageHead} title="My progress" sub=${(name ? 'Hello, ' + name + ' · ' : '') + (day ? fmtDay(day) : '') + (d && d.metrics.kind && d.metrics.kind !== 'workday' ? ' · ' + (KIND[d.metrics.kind] || d.metrics.kind) : '')}>${tools}</${PageHead}>
    <${Load} res=${res} lines=${8}>${(t) => html`
      <section class="sec" aria-labelledby="sec-day"><div class="sec-head"><h2 id="sec-day">${today ? 'Today' : fmtDay(t.date, true)}</h2>
        <p class="sub">Committed work in your profile's scope, days in ${t.profile.timezone || "this machine's time zone"}</p></div>
        <${Tiles} m=${t.metrics} />
        <${Milestones} list=${t.celebrations} animate=${t.profile.animations && !reduce} />
        ${t.note ? html`<p class="note">${t.note}</p>` : null}
        <${Evidence} m=${t.metrics} />
        <${Sources} s=${t.sources} /></section>
      <div class="two">
        <div><${NextSteps} na=${t.next_actions} /><${MyTickets} t=${t.tickets} /></div>
        <div><${Reflection} day=${t.date} rec=${t.reflection} on=${t.reflection_on} /></div>
      </div>
      <${Wins} day=${want} />
      <${Calendar} ym=${month} sel=${t.date} onMonth=${setYm} />
      <${Records} day=${t.date} />`}</${Load}>
  </${Desk}>`;
}
