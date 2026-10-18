// Today (and any past day at #/day/<date>): progress toward the day's target, the day lane by lane, delivery alerts
// and tonight's Jira entries with a copy button per field.
import { useEffect, useMemo } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, fmtMin, clock, fmtDay, fmtTs, mins, pad, addDays, slotFor, copyText, toast, plural, go, isoDay } from '../lib/core.js';
import { Icon, Status, CopyButton, Empty, Load, PageHead, TicketLink, Scroll } from '../lib/ui.js';
import { Desk, pageKeys, paletteOpen } from '../lib/shell.js';

const SAVED_WORD = { sent: 'Sent to Slack', nudged: 'Reminder sent', failed: 'Send failed', composed: 'Composed, not sent yet', none: 'Nothing to send' };

function Ring({ value, target }) {
  const R = 76, C = 2 * Math.PI * R, pct = Math.min(1, target ? value / target : 0);
  return html`<div class="ring" role="img" aria-label=${fmtMin(value) + ' tracked of ' + fmtMin(target) + ' target'}>
    <svg viewBox="0 0 172 172" aria-hidden="true"><circle cx="86" cy="86" r=${R} fill="none" stroke="var(--sunk)" stroke-width="12" />
      <circle class="arc" cx="86" cy="86" r=${R} fill="none" stroke="var(--accent)" stroke-width="12" stroke-linecap="round" stroke-dasharray=${C * pct + ' ' + C} /></svg>
    <div class="c" aria-hidden="true"><div class="v">${clock(value)}</div><div class="of">tracked · target ${fmtMin(target).replace(' 00m', '')}</div></div></div>`;
}

function hhmm(ts) { return ts && ts.length >= 16 ? ts.slice(11, 16) : ts || ''; }

function Lanes({ lanes, commits, slots }) {
  const all = lanes.flatMap((l) => l.spans || []);
  if (!all.length) return html`<${Empty} title="No time recorded for this day" icon="today">Work in a repo with Claude Code and it appears here within a few minutes.</${Empty}>`;
  let lo = Math.min(...all.map((s) => mins(hhmm(s[0])))), hi = Math.max(...all.map((s) => mins(hhmm(s[1]))));
  lo = Math.min(8 * 60, Math.floor(lo / 60) * 60); hi = Math.max(18 * 60, Math.ceil(hi / 60) * 60);
  const span = Math.max(60, hi - lo);
  const x = (t) => Math.max(0, Math.min(100, ((mins(hhmm(t)) - lo) / span) * 100));
  const ticks = [];
  for (let m = lo; m <= hi; m += Math.max(60, Math.round(span / 6 / 60) * 60)) ticks.push(html`<span key=${m}>${pad(Math.floor(m / 60))}:00</span>`);
  return html`<div class="lanes">
    <div class="hours" aria-hidden="true"><span></span><span class="ticks">${ticks}</span><span></span></div>
    ${lanes.map((l) => html`<div class=${'lane ' + slots[l.ticket || '']} key=${l.ticket || 'other'}>
      <div class="who">${l.ticket ? html`<${TicketLink} k=${l.ticket} />` : html`<span class="tkey">Other work</span>`}<span class="t">${l.ticket ? l.title || '' : 'No ticket'}</span></div>
      <div class="track" aria-hidden="true">
        ${(l.spans || []).map((s, i) => html`<span key=${i} class="seg" style=${{ left: x(s[0]) + '%', width: Math.max(0.4, x(s[1]) - x(s[0])) + '%' }} title=${hhmm(s[0]) + '–' + hhmm(s[1])}></span>`)}
        ${commits.filter((c) => (c.ticket || '') === (l.ticket || '')).map((c, i) => html`<span key=${'c' + i} class="tick" style=${{ left: x(c.time) + '%' }} title=${'Commit ' + c.time + ': ' + (c.subject || '')}></span>`)}
      </div>
      <div class="m">${fmtMin(l.minutes)}</div></div>`)}
  </div>
  <div class="legend"><span><i class="tick"></i>Commit</span><span>Each row is a ticket; bars are the stretches you worked on it.</span></div>
  <details class="more"><summary>Show the day as a table</summary>
    <${Scroll} label="The day as a table"><table><caption class="sr">Time per ticket with the times worked</caption>
      <thead><tr><th scope="col">Ticket</th><th scope="col">Worked</th><th scope="col" class="num">Time</th></tr></thead>
      <tbody>${lanes.map((l) => html`<tr key=${l.ticket || 'other'}><td>${l.ticket || 'Other work'}</td><td>${(l.spans || []).map((s) => hhmm(s[0]) + '–' + hhmm(s[1])).join(', ')}</td><td class="num">${fmtMin(l.minutes)}</td></tr>`)}</tbody>
    </table></${Scroll}></details>`;
}

function Entries({ d }) {
  const saved = d.worklog_saved, pv = d.worklog_preview || {};
  const useSaved = saved && saved.entries && saved.entries.length;
  const entries = useSaved ? saved.entries : (pv.entries || []);
  const text = useSaved ? saved.text : (pv.text || '');
  const total = entries.reduce((a, e) => a + (e.minutes || 0), 0);
  let state;
  if (useSaved) state = html`<${Status} kind=${saved.status === 'sent' || saved.status === 'nudged' ? 'ok' : saved.status === 'failed' ? 'bad' : 'info'}>${SAVED_WORD[saved.status] || saved.status}${saved.sent_at ? ' at ' + fmtTs(saved.sent_at) : ''}</${Status}>`;
  else if (pv.kind === 'message') state = html`<${Status} kind="wait">Preview · composed at ${pv.send_at || '20:00'}</${Status}>`;
  else state = null;
  let body;
  if (!entries.length) {
    const why = pv.kind === 'nudge' ? (d.is_today === false ? 'Nothing was tracked that day.' : 'Nothing tracked yet today.') : pv.kind === 'error' ? pv.reason : pv.reason ? 'No Jira worklog for this day: ' + pv.reason + '.' : 'No Jira worklog yet.';
    body = html`<${Empty} title=${why} icon="timesheet">Leave, holidays and weekends have no entries. Log time by hand with /cg:log.</${Empty}>`;
  } else {
    body = html`<div class="entries">${entries.map((e, i) => {
      const what = 'for entry ' + (i + 1) + ' (' + e.ticket + ')';
      return html`<div class="entry" key=${i}>
        <div class="fld"><span class="lab">Start</span><div class="val"><span>${e.start}</span><${CopyButton} text=${e.start} quiet what=${'start ' + what} /></div></div>
        <div class="fld"><span class="lab">Duration</span><div class="val"><span>${e.duration}</span><${CopyButton} text=${e.duration} quiet what=${'duration ' + what} /></div></div>
        <div class="fld desc"><span class="lab"><${TicketLink} k=${e.ticket} />${e.manual ? ' · logged by hand' : ''} · description</span>
          <div class="val txt"><span>${e.description}</span><${CopyButton} text=${e.description} quiet what=${'description ' + what} /></div></div></div>`;
    })}</div>
    <div class="entry-foot"><span class="muted small">${useSaved ? 'Version ' + (saved.version || 1) + '. ' : 'Descriptions are the recorded lines, before the evening condensing. '}Copy each value into Jira; nothing is sent from this page.</span>
      ${text ? html`<${CopyButton} text=${text} label="Copy all" what="the whole Jira message" primary kbd="c" />` : null}</div>
    ${text ? html`<details class="more"><summary>Show the full message</summary><pre tabindex="0">${text}</pre></details>` : null}`;
  }
  body = html`${body}<${SendHistory} saved=${saved} />`;
  return html`<section class="sec" aria-labelledby="sec-jira"><div class="sec-head"><h2 id="sec-jira">${d.is_today === false ? 'Jira entries' : "Tonight's Jira entries"}</h2>
    <p class="sub">${entries.length ? fmtMin(total) + ' in ' + plural(entries.length, 'entry', 'entries') : ''}${pv.target ? ' · target ' + fmtMin(pv.target) : ''}</p><span class="grow"></span>${state}</div>${body}</section>`;
}

function DayEvents({ events }) {
  if (!events || !events.length) return null;
  return html`<section class="sec" aria-labelledby="sec-git"><div class="sec-head"><h2 id="sec-git">Branches and PRs that day</h2><p class="sub">${plural(events.length, 'event')}</p></div>
    <ul class="other-list">${events.map((e, i) => html`<li key=${i}><span><span class="mono small">${e.time || '··:··'}</span> ${e.ticket ? html`<${TicketLink} k=${e.ticket} /> ` : null}${e.text}
      ${e.repo ? html` <span class="muted">(${e.repo})</span>` : null}</span><span class="muted small">${e.kind === 'pr' ? 'PR' : 'branch'}</span></li>`)}</ul></section>`;
}

function SendHistory({ saved }) {
  const h = (saved && saved.history) || [];
  if (!h.length) return null;
  return html`<details class="more" style="margin-top:12px"><summary>Send history (${plural(h.length, 'version')})</summary>
    <${Scroll} label="Send history"><table><caption class="sr">Every version of this day's message</caption>
      <thead><tr><th scope="col">Version</th><th scope="col">When</th><th scope="col">Why</th><th scope="col">Slack</th></tr></thead>
      <tbody>${h.map((x) => html`<tr key=${x.v}><td>${x.v}</td><td class="nowrap">${fmtTs(x.at)}</td><td>${x.why || x.kind || 'first'}</td>
        <td>${x.sent ? html`<${Status} kind="ok" quiet>Sent</${Status}>` : x.error ? html`<${Status} kind="bad" quiet>${x.error}</${Status}>` : html`<${Status} kind="off" quiet>Not sent</${Status}>`}</td></tr>`)}</tbody>
    </table></${Scroll}></details>`;
}

function Alerts({ tickets }) {
  const rows = [];
  (tickets || []).forEach((t) => (t.alerts || []).forEach((a) => rows.push({ key: t.key, a })));
  if (!rows.length) return html`<p class="muted small" style="margin:0 0 20px"><${Icon} name="ok" size=${15} /> No delivery alerts: every tracked commit is where its branches should be.</p>`;
  const bad = rows.some((r) => r.a.kind === 'lost' || r.a.kind === 'only-copy');
  return html`<div class=${'alert' + (bad ? ' bad' : '')} role="status"><${Icon} name=${bad ? 'bad' : 'warn'} size=${18} />
    <div class="grow"><strong>${plural(rows.length, 'delivery alert')}</strong>
      <ul>${rows.slice(0, 6).map((r, i) => html`<li key=${i}><${TicketLink} k=${r.key} />: ${r.a.text}${r.a.repo ? html` <span class="muted">(${r.a.repo})</span>` : null}
        · <a href=${'#/tickets/' + encodeURIComponent(r.key) + '?tab=branches'}>Review branches</a></li>`)}
        ${rows.length > 6 ? html`<li class="muted">and ${rows.length - 6} more on the Tickets page</li>` : null}</ul></div></div>`;
}

function dayAlerts(list) {  // the day's tickets' alerts, in the shape the strip takes
  const by = {};
  (list || []).forEach((a) => { (by[a.ticket] = by[a.ticket] || { key: a.ticket, alerts: [] }).alerts.push(a); });
  return Object.values(by);
}

export function TodayPage({ route }) {
  const day = route.name === 'day' ? route.arg : '';
  const res = useApi(day ? '/api/day/' + encodeURIComponent(day) : '/api/today');
  const tks = useApi('/api/tickets');
  const d = res.data;
  // Bars come from the activity blocks (when you worked); minutes come from the day file, like the ring, the
  // Timesheet and the ticket totals, so every number on the page agrees.
  const lanes = useMemo(() => {
    if (!d) return [];
    const m = {};
    (d.tickets || []).forEach((t) => { m[t.key] = t.minutes; });
    m[''] = (d.other || []).reduce((a, o) => a + o.minutes, 0);
    const out = (d.lanes || []).map((l) => Object.assign({}, l, { minutes: m[l.ticket || ''] != null ? m[l.ticket || ''] : l.minutes }));
    (d.tickets || []).forEach((t) => { if (!out.some((l) => l.ticket === t.key)) out.push({ ticket: t.key, title: t.title, minutes: t.minutes, spans: [] }); });
    return out.filter((l) => l.minutes > 0 || (l.spans || []).length).sort((x, y) => y.minutes - x.minutes);
  }, [d]);
  const slots = useMemo(() => slotFor(lanes.map((l) => l.ticket || '')), [lanes.map((l) => l.ticket).join()]);
  const shown = d ? d.day : day;
  useEffect(() => { document.title = (day ? fmtDay(day) : 'Today') + ' · Chitragupta'; }, [day]);
  useEffect(() => {
    const text = d ? ((d.worklog_saved && d.worklog_saved.entries && d.worklog_saved.entries.length && d.worklog_saved.text) || (d.worklog_preview || {}).text || '') : '';
    const keys = {
      p: { label: 'Previous day', run: () => shown && go('#/day/' + addDays(shown, -1)) },
      n: { label: 'Next day', run: () => shown && go('#/day/' + addDays(shown, 1)) },
    };
    if (text) keys.c = { label: "Copy the whole Jira message", run: () => copyText(text).then(() => toast('Copied the whole Jira message', 'ok')) };
    pageKeys.set(keys);
    return () => pageKeys.set({});
  }, [d, shown]);
  const target = (d && (d.target_minutes || (d.worklog_preview || {}).target)) || 480;
  const isToday = d ? d.is_today !== false && !day : !day;
  const rail = html`<div class="now">
      ${d ? html`<${Ring} value=${d.total_minutes} target=${target} />` : html`<div class="ring"></div>`}
      <p class="on">${!d || !lanes.length ? 'Nothing tracked yet' : lanes[0].ticket ? html`Most time on <b>${lanes[0].ticket}</b>` : 'Most time on work without a ticket'}${d && d.last_activity ? ' · last activity ' + d.last_activity : ''}</p>
      <div class="daynav">
        <a class="btn" href=${shown ? '#/day/' + addDays(shown, -1) : '#/today'}><${Icon} name="left" size=${16} />Previous day</a>
        ${day ? html`<a class="btn" href="#/today">Today</a>` : null}
        ${day ? html`<a class="btn" href=${'#/day/' + addDays(shown, 1)}>Next<${Icon} name="right" size=${16} /></a>` : null}
      </div>
      <label class="field" style="margin-top:12px;width:100%">Go to a day
        <input type="date" value=${shown || ''} max=${isoDay(new Date())} onChange=${(e) => { const v = e.currentTarget.value; if (v) go('#/day/' + v); }} /></label></div>
    <h2>${isToday ? "Today's tickets" : 'Tickets that day'}</h2>
    <ul class="rlist">${lanes.length ? lanes.map((l) => html`<li key=${l.ticket || 'other'}>${l.ticket
      ? html`<a href=${'#/tickets/' + encodeURIComponent(l.ticket)} class=${slots[l.ticket]}><span class="sw"></span><span><span class="k">${l.ticket}</span><span class="t">${l.title || ''}</span></span><span class="m">${fmtMin(l.minutes)}</span></a>`
      : html`<div class="ritem to"><span class="sw"></span><span><span class="k">Other work</span><span class="t">No ticket</span></span><span class="m">${fmtMin(l.minutes)}</span></div>`}</li>`)
      : html`<li class="muted small" style="padding:8px">No tickets yet.</li>`}</ul>`;
  return html`<${Desk} rail=${rail} railLabel="Day summary">
    <${Load} res=${res} lines=${8}>${(d) => html`
      <${PageHead} title=${isToday ? 'Today' : fmtDay(d.day)} sub=${(isToday ? fmtDay(d.day) + ' · ' : '') + plural(d.sessions || 0, 'session') + ' · ' + plural(d.commits || 0, 'commit') + (d.closed ? ' · day closed' : '') + (d.updated_at ? ' · updated ' + fmtTs(d.updated_at) : '')}>
        <button type="button" class="btn" onClick=${() => paletteOpen.set(true)}><${Icon} name="tickets" size=${16} />Open a ticket<kbd>Ctrl K</kbd></button>
      </${PageHead}>
      ${d.summary ? html`<p class="lede">${d.summary}</p>` : null}
      <${Alerts} tickets=${day ? dayAlerts(d.alerts) : tks.data && tks.data.tickets} />
      ${!(d.tickets || []).length && !(d.other || []).length && !d.worklog_saved ? html`<${Empty} title=${'Nothing was recorded on ' + fmtDay(d.day)} icon="day">
        No Claude Code sessions, commits or Jira entries for this day. Use the date picker or previous and next to move; <a href="#/today">go to Today</a>.</${Empty}>` : html`
      <section class="sec" aria-labelledby="sec-day"><div class="sec-head"><h2 id="sec-day">The day, ticket by ticket</h2>
        <p class="sub">${fmtMin(d.total_minutes)} tracked${d.other && d.other.length ? ', ' + fmtMin(d.other.reduce((a, o) => a + o.minutes, 0)) + ' without a ticket' : ''}</p></div>
        <${Lanes} lanes=${lanes} commits=${d.commit_list || []} slots=${slots} /></section>
      <${Entries} d=${Object.assign({ is_today: isToday }, d)} />`}
      <${DayEvents} events=${d.branch_events} />
      ${d.other && d.other.length ? html`<section class="sec" aria-labelledby="sec-other"><div class="sec-head"><h2 id="sec-other">Other work</h2><p class="sub">Repos and branches with no ticket</p></div>
        <ul class="other-list">${d.other.map((o) => html`<li key=${o.group}><span>${o.group}</span><span>${fmtMin(o.minutes)}</span></li>`)}</ul></section>` : null}
    `}</${Load}></${Desk}>`;
}
