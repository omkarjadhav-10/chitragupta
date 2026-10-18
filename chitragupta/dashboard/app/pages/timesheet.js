// Timesheet: a month heatmap (hours against each day's target, with leave, half days and holidays marked in words),
// weekly bars, the selected day's tickets, and hours per ticket by week or month with a CSV export.
import { useEffect, useMemo, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, fmtMin, fmtHours, fmtDay, setParams, isoDay, parseDay, monthName, pad, plural, addDays, runAction, toast } from '../lib/core.js';
import { Icon, Status, Empty, Load, PageHead, TicketLink, Scroll } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

const KINDW = { leave: 'Leave', half_day: 'Half day', holiday: 'Holiday', extra_workday: 'Extra workday', weekend: 'Weekend', workday: 'Workday' };
const KINDI = { leave: 'off', half_day: 'wait', holiday: 'off', extra_workday: 'info' };
const WDS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function monthRange(ym) {
  const p = ym.split('-').map(Number);
  const first = new Date(p[0], p[1] - 1, 1), last = new Date(p[0], p[1], 0);
  return [isoDay(first), isoDay(last)];
}
function shiftMonth(ym, n) { const p = ym.split('-').map(Number); const d = new Date(p[0], p[1] - 1 + n, 1); return d.getFullYear() + '-' + pad(d.getMonth() + 1); }
function isoWeek(s) {
  const d = parseDay(s); const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7; t.setUTCDate(t.getUTCDate() + 4 - day);
  const y0 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return t.getUTCFullYear() + '-W' + pad(Math.ceil(((t - y0) / 86400000 + 1) / 7));
}
function heat(d) {
  if (!d.minutes) return '';
  const r = d.minutes / (d.target || 480);
  return r < 0.25 ? ' h1' : r < 0.6 ? ' h2' : r < 1 ? ' h3' : ' h4';
}

function Heatmap({ ym, days, sel }) {
  const [from, to] = monthRange(ym);
  const byDate = {};
  days.forEach((d) => { byDate[d.date] = d; });
  const start = parseDay(from);
  const lead = (start.getDay() + 6) % 7;  // Monday first
  const cells = [];
  let cur = addDays(from, -lead);
  const end = addDays(to, (7 - ((parseDay(to).getDay() + 6) % 7) - 1));
  while (cur <= end) { cells.push(cur); cur = addDays(cur, 1); }
  const weeks = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
  return html`<div class="heat" role="grid" aria-label=${'Hours per day in ' + monthName(ym)}>
    <div role="row" style="display:contents"><span role="columnheader" class="wd"><span class="sr">Week</span></span>${WDS.map((w) => html`<span role="columnheader" class="wd" key=${w}>${w}</span>`)}</div>
    ${weeks.map((wk) => html`<div role="row" style="display:contents" key=${wk[0]}><span role="rowheader" class="wk">${isoWeek(wk[0]).slice(6)}</span>
      ${wk.map((s) => {
        const inMonth = s >= from && s <= to;
        const d = byDate[s] || { date: s, minutes: 0, kind: '', target: 0 };
        if (!inMonth) return html`<span role="gridcell" class="dcell out" key=${s}></span>`;
        const kind = d.kind && d.kind !== 'workday' ? d.kind : '';
        const label = fmtDay(s) + ': ' + (d.minutes ? fmtMin(d.minutes) : 'nothing recorded') + (kind ? ', ' + KINDW[kind] : '') + (d.note ? ' (' + d.note + ')' : '');
        return html`<span role="gridcell" key=${s}><button type="button" class=${'dcell' + heat(d) + (kind === 'weekend' && !d.minutes ? ' weekend' : '')} aria-pressed=${sel === s ? 'true' : 'false'}
          aria-label=${label} onClick=${() => setParams({ day: sel === s ? '' : s })}>
          <span class="dn">${parseDay(s).getDate()}</span>
          ${kind && kind !== 'weekend' ? html`<span class="dk"><${Icon} name=${KINDI[kind] || 'info'} size=${12} />${KINDW[kind]}</span>` : null}
          <span class="dh">${d.minutes ? fmtHours(d.minutes) + 'h' : ''}</span></button></span>`;
      })}</div>`)}
  </div>
  <div class="heatkey" aria-hidden="true">Less <i style="background:var(--heat-0)"></i><i style="background:var(--heat-1)"></i><i style="background:var(--heat-2)"></i><i style="background:var(--heat-3)"></i><i style="background:var(--heat-4)"></i> At or over the day's target</div>`;
}

function WeekBars({ days }) {
  const weeks = {};
  days.forEach((d) => { const w = isoWeek(d.date); const x = weeks[w] || (weeks[w] = { m: 0, t: 0 }); x.m += d.minutes || 0; x.t += d.target || 0; });
  const ks = Object.keys(weeks).sort();
  if (!ks.length) return null;
  const max = Math.max(...ks.map((k) => Math.max(weeks[k].m, weeks[k].t)), 60);
  return html`<div class="bars" role="list" aria-label="Hours per week against the target">${ks.map((k) => html`<div class="bar-row" role="listitem" key=${k}>
    <span>${k.replace('-W', ' week ')}</span>
    <span class="bar-track" aria-hidden="true"><span class="fill" style=${{ width: (weeks[k].m / max) * 100 + '%' }}></span>${weeks[k].t ? html`<span class="target" style=${{ left: (weeks[k].t / max) * 100 + '%' }}></span>` : null}</span>
    <span class="num">${fmtHours(weeks[k].m)}h <span class="muted">/ ${fmtHours(weeks[k].t)}h</span></span></div>`)}</div>`;
}

// Mark a day: one inbox line, the same as /cg:log; the next background run (within about 5 minutes) applies it.
function MarkDay({ d }) {
  const marked = ['leave', 'half_day', 'holiday', 'extra_workday'].indexOf(d.kind) >= 0;
  const weekend = d.kind === 'weekend' || d.kind === 'extra_workday';
  const opts = [['leave', 'Leave'], ['half-day', 'Half day'], ['holiday', 'Holiday']].concat(weekend ? [['workday', 'Extra workday']] : [])
    .concat(marked ? [['clear', 'Remove the mark']] : []);
  const [kind, setKind] = useState(marked ? 'clear' : 'leave');
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState('');
  const id = 'mark-' + d.date;
  const save = (e) => {
    e.preventDefault();
    setBusy(true);
    runAction('calendar-mark', { day: d.date, kind, name }).then((r) => { setDone(r.line); toast('Added to inbox.md: ' + r.line, 'ok'); },
      (err) => toast(err.message, 'bad')).finally(() => setBusy(false));
  };
  return html`<form class="markday" onSubmit=${save}><label for=${id}>Mark this day</label>
    <div class="btnline"><select id=${id} value=${kind} onChange=${(e) => setKind(e.currentTarget.value)}>${opts.map((o) => html`<option key=${o[0]} value=${o[0]}>${o[1]}</option>`)}</select>
      ${kind === 'holiday' ? html`<input type="text" aria-label="Holiday name (optional)" placeholder="Name (optional)" maxlength="60" value=${name} onInput=${(e) => setName(e.currentTarget.value)} />` : null}
      <button type="submit" class="btn" disabled=${busy}>${busy ? 'Saving…' : 'Save'}</button></div>
    <p class="muted small" aria-live="polite">${done ? 'Added “' + done + '”; it shows here after the next background run (within about 5 minutes).' : 'Adds one line to inbox.md, the same as /cg:log. A sent day gets a corrected message.'}</p></form>`;
}

function DayPanel({ d }) {
  if (!d) return html`<${Empty} title="Pick a day" icon="day">Select a day in the calendar to see its tickets.</${Empty}>`;
  const ks = Object.keys(d.tickets || {}).sort((a, b) => d.tickets[b] - d.tickets[a]);
  return html`<div class="card"><h3>${fmtDay(d.date)}${d.kind && d.kind !== 'workday' ? html`<${Status} kind=${KINDI[d.kind] || 'off'}>${KINDW[d.kind] || d.kind}</${Status}>` : null}</h3>
    <p>${fmtMin(d.minutes)} recorded${d.target ? ' of ' + fmtMin(d.target) + ' target' : ''}${d.note ? ' · ' + d.note : ''}</p>
    ${ks.length ? html`<ul class="other-list" style="margin-top:12px">${ks.map((k) => html`<li key=${k}><span>${k ? html`<${TicketLink} k=${k} />` : 'Other work'}</span><span>${fmtMin(d.tickets[k])}</span></li>`)}</ul>` : null}
    <p style="margin-top:12px"><a class="btn" href=${'#/day/' + d.date}><${Icon} name="day" size=${16} />Open this day</a></p>
    <${MarkDay} key=${d.date + d.kind} d=${d} /></div>`;
}

function PerTicket({ period }) {
  const res = useApi('/api/timesheet?period=' + period);
  return html`<section class="sec" aria-labelledby="sec-pt"><div class="sec-head"><h2 id="sec-pt">Hours per ticket</h2><span class="grow"></span>
    <div class="seg" role="group" aria-label="Group by">${[['week', 'By ISO week'], ['month', 'By month']].map(([v, l]) => html`<button key=${v} type="button" aria-pressed=${period === v ? 'true' : 'false'} onClick=${() => setParams({ period: v === 'week' ? '' : v })}>${l}</button>`)}</div>
    <a class="btn" href=${'/api/timesheet.csv?period=' + period} download><${Icon} name="download" size=${16} />Download CSV</a></div>
    <${Load} res=${res} lines=${6}>${(d) => {
      if (!d.periods.length) return html`<${Empty} title="No days recorded yet" icon="timesheet" />`;
      const ps = d.periods.slice().reverse();
      const grand = ps.reduce((a, p) => a + (d.totals[p] || 0), 0);
      return html`<${Scroll} label="Hours per ticket table"><table><caption class="sr">Hours per ticket per ${period}</caption>
        <thead><tr><th scope="col">Ticket</th><th scope="col" class="hide-sm">Title</th>${ps.map((p) => html`<th scope="col" class="num" key=${p}>${p}</th>`)}<th scope="col" class="num">Total</th></tr></thead>
        <tbody>${d.rows.map((r) => html`<tr key=${r.ticket || 'other'}><td>${r.ticket ? html`<${TicketLink} k=${r.ticket} />` : html`<span class="muted">No ticket</span>`}</td><td class="hide-sm">${r.title || ''}</td>
          ${ps.map((p) => html`<td class="num" key=${p}>${r.minutes[p] ? fmtMin(r.minutes[p]) : html`<span class="muted" aria-label="none">·</span>`}</td>`)}<td class="num"><strong>${fmtMin(r.total)}</strong></td></tr>`)}</tbody>
        <tfoot><tr><td>Total</td><td class="hide-sm"></td>${ps.map((p) => html`<td class="num" key=${p}>${fmtMin(d.totals[p] || 0)}</td>`)}<td class="num">${fmtMin(grand)}</td></tr></tfoot></table></${Scroll}>`;
    }}</${Load}></section>`;
}

export function TimesheetPage({ route }) {
  const now = new Date();
  const ym = /^\d{4}-\d{2}$/.test(route.params.month || '') ? route.params.month : now.getFullYear() + '-' + pad(now.getMonth() + 1);
  const [from, to] = monthRange(ym);
  const res = useApi('/api/calendar?from=' + from + '&to=' + to);
  const sel = route.params.day || '';
  useEffect(() => { document.title = 'Timesheet · Chitragupta'; }, []);
  useEffect(() => {
    pageKeys.set({ p: { label: 'Previous month', run: () => setParams({ month: shiftMonth(ym, -1), day: '' }) }, n: { label: 'Next month', run: () => setParams({ month: shiftMonth(ym, 1), day: '' }) } });
    return () => pageKeys.set({});
  }, [ym]);
  const days = (res.data && res.data.days) || [];
  const tot = useMemo(() => {
    const t = { m: 0, target: 0, worked: 0, off: [] };
    days.forEach((d) => { t.m += d.minutes || 0; t.target += d.target || 0; if (d.minutes) t.worked++; if (['leave', 'half_day', 'holiday', 'extra_workday'].indexOf(d.kind) >= 0) t.off.push(d); });
    return t;
  }, [days]);
  const rail = html`<div class="daynav" style="margin:0 0 12px"><button type="button" class="btn icon" onClick=${() => setParams({ month: shiftMonth(ym, -1), day: '' })} aria-label="Previous month"><${Icon} name="left" /></button>
      <h2 style="margin:0" aria-live="polite">${monthName(ym)}</h2>
      <button type="button" class="btn icon" onClick=${() => setParams({ month: shiftMonth(ym, 1), day: '' })} aria-label="Next month"><${Icon} name="right" /></button></div>
    <dl class="kv"><dt>Recorded</dt><dd>${fmtMin(tot.m)}</dd><dt>Target</dt><dd>${fmtMin(tot.target)}</dd><dt>Days with work</dt><dd>${tot.worked}</dd>
      <dt>Hours per day</dt><dd>${res.data ? res.data.hours_per_day : '—'}</dd></dl>
    <h2 style="margin-top:20px">Days off and extra days</h2>
    ${tot.off.length ? html`<ul class="rlist">${tot.off.map((d) => html`<li key=${d.date}><a href=${'#/timesheet?month=' + ym + '&day=' + d.date} aria-current=${sel === d.date ? 'true' : null}
      onClick=${(e) => { e.preventDefault(); setParams({ day: d.date }); }}><span class="sw"></span><span><span class="k">${fmtDay(d.date, true)}</span><span class="t">${KINDW[d.kind]}${d.note ? ' · ' + d.note : ''}</span></span><span class="m"></span></a></li>`)}</ul>`
      : html`<p class="muted small">None this month.</p>`}
    <p class="muted small" style="margin-top:16px">To mark leave, a half day or a holiday, tell Claude (“I was on leave on …”) or use /cg:log. Marking from this page comes with the Settings update.</p>`;
  return html`<${Desk} rail=${rail} railLabel="Month">
    <${PageHead} title="Timesheet" sub=${monthName(ym) + ' · ' + fmtMin(tot.m) + ' recorded of ' + fmtMin(tot.target) + ' target'} />
    <${Load} res=${res} lines=${8}>${() => html`<div class="two">
      <section class="sec" aria-labelledby="sec-heat"><div class="sec-head"><h2 id="sec-heat">Month</h2><p class="sub">Darker means closer to the day's target</p></div><${Heatmap} ym=${ym} days=${days} sel=${sel} /></section>
      <div><section class="sec" aria-labelledby="sec-sel"><h2 id="sec-sel" class="sr">Selected day</h2><${DayPanel} d=${days.find((d) => d.date === sel)} /></section>
        <section class="sec" aria-labelledby="sec-wk"><div class="sec-head"><h2 id="sec-wk">Weeks</h2><p class="sub">Bar is recorded time; the line is the target</p></div><${WeekBars} days=${days} /></section></div></div>`}</${Load}>
    <${PerTicket} period=${route.params.period === 'month' ? 'month' : 'week'} />
  </${Desk}>`;
}
