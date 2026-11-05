// My progress: one day from the recorded evidence (progress.py via /api/progress), action first: how current the
// sources are, the next useful action (or setting up / recovering a source), the day's numbers in one compact row
// (each opens its own definition, counted events, exclusions and sources), your active tickets, the day's factual
// milestones and your own reflection. Weekly wins, the calendar and personal records live on History. The numbers
// describe observed work in your profile's scope: never quality, effort or a ranking. #/progress?date=YYYY-MM-DD.
import { useEffect, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, setParams, runAction, toast, fmtDay, fmtTs, plural, addDays } from '../lib/core.js';
import { Icon, Load, PageHead, Scroll } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';
import { FocusBand, ActiveTickets } from '../lib/focus.js';

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
const KIND = { workday: 'Workday', half_day: 'Half day', leave: 'Leave', holiday: 'Holiday', weekend: 'Weekend', extra_workday: 'Extra workday' };

function Milestones({ list, animate }) {
  if (!list || !list.length) return null;
  return html`<section class="sec" aria-labelledby="sec-ms"><div class="sec-head"><h2 id="sec-ms">Milestones</h2>
      <p class="sub">Facts from the recorded evidence; switch them off on the Profile page</p></div>
    <ul class=${'mslist' + (animate ? ' animate' : '')}>${list.map((s, i) => html`<li key=${i}>${s}</li>`)}</ul></section>`;
}

// Reflection: optional and self-reported; its draft survives live refreshes of the page (only saving replaces it).
function Reflection({ day, rec, on }) {
  const [text, setText] = useState((rec && rec.text) || '');
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (!dirty) setText((rec && rec.text) || ''); }, [day, rec && rec.updated_at]);
  useEffect(() => { setDirty(false); setText((rec && rec.text) || ''); }, [day]);
  if (!on) return null;
  const save = (e) => {
    e.preventDefault();
    setBusy(true);
    runAction('reflection-save', { date: day, text }).then(() => { setDirty(false); toast('Saved your note for ' + fmtDay(day, true), 'ok'); },
      (err) => toast(err.message, 'bad')).finally(() => setBusy(false));
  };
  return html`<details class="more preflect-box" open=${!!(rec && rec.text) || dirty}><summary>Reflection${rec && rec.text ? ' (saved)' : ': what moved forward?'}</summary>
    <form class="preflect" onSubmit=${save}><label for="refl-text">What moved forward ${day ? 'on ' + fmtDay(day, true) : 'today'}? <span class="muted small">Self-reported, kept on this machine</span></label>
      <p class="muted small" id="refl-help">Design, debugging, reviews, learning and helping teammates often leave no commit. A line or two is enough.</p>
      <textarea id="refl-text" aria-describedby="refl-help" rows="3" maxlength="1000" value=${text} onInput=${(e) => { setText(e.currentTarget.value); setDirty(true); }}></textarea>
      <div class="btnline"><button type="submit" class="btn" disabled=${busy || !dirty}>${busy ? 'Saving…' : 'Save the note'}</button>
        ${dirty ? html`<span class="muted small">Not saved yet</span>` : rec && rec.updated_at ? html`<span class="muted small">Saved ${fmtTs(rec.updated_at)}</span>` : null}</div></form></details>`;
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
  useEffect(() => { document.title = 'My progress · Chitragupta'; }, []);
  useEffect(() => {
    if (!day) return undefined;
    pageKeys.set({ p: { label: 'Previous day', run: () => setParams({ date: addDays(day, -1) }) }, n: { label: 'Next day', run: () => setParams({ date: addDays(day, 1) }) } });
    return () => pageKeys.set({});
  }, [day]);
  const name = d && d.profile && d.profile.display_name;
  const reduce = typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const tools = html`<div class="day-tools"><div class="daynav">
      <button type="button" class="btn" onClick=${() => day && setParams({ date: addDays(day, -1) })}><${Icon} name="left" size=${16} />Previous day</button>
      ${want ? html`<button type="button" class="btn" onClick=${() => setParams({ date: '' })}>Today</button>` : null}
      ${want ? html`<button type="button" class="btn" onClick=${() => setParams({ date: addDays(day, 1) })}>Next<${Icon} name="right" size=${16} /></button>` : null}</div>
    <label class="field"><span class="sr">Show a day</span><input type="date" value=${day} onChange=${(e) => { const v = e.currentTarget.value; if (DAY_RE.test(v)) setParams({ date: v }); }} /></label></div>`;
  return html`<${Desk} className="progress-desk">
    <${PageHead} title="My progress" sub=${(name ? 'Hello, ' + name + ' · ' : '') + (day ? fmtDay(day) : '') + (d && d.metrics.kind && d.metrics.kind !== 'workday' ? ' · ' + (KIND[d.metrics.kind] || d.metrics.kind) : '')}>${tools}</${PageHead}>
    <${Load} res=${res} lines=${6}>${(t) => html`
      <${FocusBand} d=${t} />
      <div class="two">
        <div><${ActiveTickets} t=${t.tickets} /></div>
        <div><${Milestones} list=${t.celebrations} animate=${t.profile.animations && !reduce} />
          <${Reflection} day=${t.date} rec=${t.reflection} on=${t.reflection_on} /></div>
      </div>
      <${Sources} s=${t.sources} />
      <p class="small pagelink"><a href=${'#/history' + (want ? '?date=' + want : '')}>History: weekly wins, the activity calendar and personal records</a></p>`}</${Load}>
  </${Desk}>`;
}
