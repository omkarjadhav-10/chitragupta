// History: looking back rather than at today. Weekly wins (a factual summary with links, copied with its coverage
// qualifications), the activity calendar and personal records, each with its definition, coverage and ties; a record
// is only held by a completely collected day, and a partly collected day that shows more is listed with it.
// #/history?date=YYYY-MM-DD picks the week; ?month=YYYY-MM the calendar.
import { useEffect } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, setParams, fmtDay, monthName, pad, plural, parseDay } from '../lib/core.js';
import { Icon, Load, PageHead, CopyButton, Markdown } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
const KIND = { workday: 'Workday', half_day: 'Half day', leave: 'Leave', holiday: 'Holiday', weekend: 'Weekend', extra_workday: 'Extra workday' };
const WDS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function shiftMonth(ym, n) { const p = ym.split('-').map(Number); const d = new Date(p[0], p[1] - 1 + n, 1); return d.getFullYear() + '-' + pad(d.getMonth() + 1); }

function Wins({ day }) {
  const res = useApi('/api/progress/wins' + (day ? '?date=' + day : ''));
  return html`<section class="sec" aria-labelledby="sec-wins"><div class="sec-head"><h2 id="sec-wins">Weekly wins</h2>
      <p class="sub">${res.data ? fmtDay(res.data.start, true) + ' to ' + fmtDay(res.data.end, true) + ' · facts with links, no model' : 'This week'}</p>
      <span class="grow"></span>${res.data ? html`<${CopyButton} text=${res.data.copy_text} label="Copy the summary" what="the weekly wins (emails and ids left out)" />` : null}</div>
    <${Load} res=${res} lines=${4}>${(w) => html`<div class="pwins"><${Markdown} text=${w.markdown} /></div>`}</${Load}></section>`;
}

function cellLabel(c) {
  const head = fmtDay(c.date) + (c.kind && c.kind !== 'workday' ? ', ' + (KIND[c.kind] || c.kind) : '');
  if (c.activity == null) return head + ': not known (outside the collected history or no source)';
  if (!c.activity) return head + ': no commits or PRs recorded';
  const parts = [];
  if (c.commits) parts.push(plural(c.commits, 'commit'));
  if (c.prs_raised) parts.push(plural(c.prs_raised, 'PR') + ' raised');
  if (c.prs_merged) parts.push(plural(c.prs_merged, 'PR') + ' merged');
  if (c.tickets_completed) parts.push(plural(c.tickets_completed, 'ticket') + ' completed');
  return head + ': ' + plural(c.activity, 'activity event') + ' (' + parts.join(', ') + ')' + (c.status === 'partial' ? ', partly collected' : '');
}

function Calendar({ ym }) {
  const res = useApi('/api/progress/calendar?month=' + ym);
  const prev = shiftMonth(ym, -1), next = shiftMonth(ym, 1);
  return html`<section class="sec" aria-labelledby="sec-cal"><div class="sec-head"><h2 id="sec-cal">Activity calendar</h2>
      <p class="sub">${monthName(ym)} · distinct commits, PRs and completed tickets per day; a count, not a score</p>
      <span class="grow"></span><div class="month-actions">
        <button type="button" class="btn quiet" onClick=${() => setParams({ month: prev })} aria-label=${'Previous month: ' + monthName(prev)}><${Icon} name="left" size=${16} />${monthName(prev).split(' ')[0]}</button>
        <button type="button" class="btn quiet" onClick=${() => setParams({ month: next })} aria-label=${'Next month: ' + monthName(next)}>${monthName(next).split(' ')[0]}<${Icon} name="right" size=${16} /></button></div></div>
    <${Load} res=${res} lines=${5}>${(cal) => html`<div class="heat pcal" role="grid" aria-label=${'Activity per day in ' + monthName(cal.month)}>
      <div role="row" style="display:contents"><span role="columnheader" class="wd"><span class="sr">Week</span></span>${WDS.map((w) => html`<span role="columnheader" class="wd" key=${w}>${w}</span>`)}</div>
      ${cal.weeks.map((wk, i) => html`<div role="row" style="display:contents" key=${i}><span role="rowheader" class="wk"><span class="sr">Week ${i + 1}</span></span>
        ${wk.map((c, j) => !c ? html`<span role="gridcell" class="dcell out" key=${'x' + j}></span>`
          : html`<span role="gridcell" key=${c.date}><a class=${'dcell' + (c.level ? ' h' + c.level : '') + (c.activity == null ? ' unknown' : '') + (c.kind === 'weekend' || c.kind === 'leave' || c.kind === 'holiday' ? ' weekend' : '')}
            href=${'#/progress?date=' + c.date} aria-label=${cellLabel(c) + '. Open that day'}>
            <span class="dn">${parseDay(c.date).getDate()}</span>
            ${c.kind && ['leave', 'holiday', 'half_day'].indexOf(c.kind) >= 0 ? html`<span class="dk">${KIND[c.kind]}</span>` : null}
            <span class="dh">${c.activity == null ? '?' : String(c.activity) + (c.status === 'partial' ? '+' : '')}</span></a></span>`)}</div>`)}
    </div>
    <p class="heatkey">Each day shows its count of distinct activity events; “?” is not known (before the collected history, or a source that isn’t set up) and “+” partly collected (the count may be higher). Days off are striped; nothing is expected on them.</p>`}</${Load}></section>`;
}

function Records() {
  const res = useApi('/api/progress/records');
  return html`<section class="sec" aria-labelledby="sec-rec"><div class="sec-head"><h2 id="sec-rec">Personal records</h2>
      <p class="sub">${res.data ? 'Your best completely collected days ' + res.data.coverage.label + '; the earliest day wins a tie' : 'Your best days'}</p></div>
    <${Load} res=${res} lines=${4}>${(r) => html`<ul class="precords">${r.records.map((x) => html`<li key=${x.id}>
      <h3>${x.label}</h3>
      ${x.value ? html`<p class="ptile-v">${x.value}</p>
        <p class="small"><a href=${'#/progress?date=' + x.date}>${fmtDay(x.date, true)}</a>${x.ties ? ' · tied with ' + plural(x.ties, 'other day') : ''}</p>
        ${x.breakdown ? html`<p class="small muted">${plural(x.breakdown.commits || 0, 'commit')} · ${x.breakdown.prs_raised || 0} raised · ${x.breakdown.prs_merged || 0} merged${x.breakdown.tickets_completed ? ' · ' + plural(x.breakdown.tickets_completed, 'ticket') + ' completed' : ''}</p>` : null}`
        : html`<p class="small muted">${x.why || 'None yet'}</p>`}
      ${(x.partial_days || []).length ? html`<p class="small"><span class="status warn quiet">Not a record:</span> ${x.partial_days.map((p) => fmtDay(p.date, true) + ' shows at least ' + p.value + ' but was partly collected').join('; ')}</p>` : null}
      <p class="small muted">${x.coverage}</p>
      ${x.definition ? html`<details class="more"><summary>How this is counted</summary><p class="small">${x.definition}</p></details>` : null}</li>`)}</ul>`}</${Load}></section>`;
}

export function HistoryPage({ route }) {
  const day = DAY_RE.test(route.params.date || '') ? route.params.date : '';
  const ym = MONTH_RE.test(route.params.month || '') ? route.params.month : (day ? day.slice(0, 7) : '');
  const now = useApi('/api/progress/calendar' + (ym ? '?month=' + ym : ''), { live: false });
  const month = ym || (now.data && now.data.month) || '';
  useEffect(() => { document.title = 'History · Chitragupta'; }, []);
  useEffect(() => {
    if (!month) return undefined;
    pageKeys.set({ p: { label: 'Previous month', run: () => setParams({ month: shiftMonth(month, -1) }) }, n: { label: 'Next month', run: () => setParams({ month: shiftMonth(month, 1) }) } });
    return () => pageKeys.set({});
  }, [month]);
  return html`<${Desk} className="history-desk">
    <${PageHead} title="History" sub="Your week and your best days, from the recorded evidence; today's next steps are on Today" />
    <${Wins} day=${day} />
    ${month ? html`<${Calendar} ym=${month} />` : null}
    <${Records} />
  </${Desk}>`;
}
