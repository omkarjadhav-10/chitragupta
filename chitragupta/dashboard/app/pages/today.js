// Today (and any past day at #/day/<date>): progress toward the day's target, the day lane by lane, delivery alerts
// and tonight's Jira entries with a copy button per field.
import { useEffect, useMemo, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, fmtMin, fmtDay, fmtTs, mins, pad, addDays, slotFor, copyText, toast, plural, go, isoDay, runAction } from '../lib/core.js';
import { Icon, Status, CopyButton, Empty, Load, PageHead, TicketLink, Scroll, Dialog } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

const SAVED_WORD = { sent: 'Sent to Slack', nudged: 'Reminder sent', failed: 'Send failed', composed: 'Composed, not sent yet', none: 'Nothing to send' };

function hhmm(ts) { return ts && ts.length >= 16 ? ts.slice(11, 16) : ts || ''; }

function Lanes({ lanes, commits, slots, entries, isToday }) {
  const [selected, setSelected] = useState(null);
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    if (!isToday) return undefined;
    const timer = setInterval(() => setNow(new Date()), 60000);
    return () => clearInterval(timer);
  }, [isToday]);
  const all = lanes.flatMap((l) => l.spans || []);
  if (!all.length && lanes.some((l) => l.minutes > 0)) {  // totals in the day file, but no activity left to place them in the day
    return html`<p class="muted small">The times of day aren't available for this day, only the totals.</p>
      <ul class="other-list">${lanes.map((l) => html`<li key=${l.ticket || 'other'}><span>${l.ticket ? html`<${TicketLink} k=${l.ticket} /> ${l.title || ''}` : 'Other work'}</span><span>${fmtMin(l.minutes)}</span></li>`)}</ul>`;
  }
  if (!all.length) return html`<${Empty} title="No time recorded for this day" icon="today">Work in a repo with Claude Code and it appears here within a few minutes.</${Empty}>`;
  let lo = Math.min(...all.map((s) => mins(hhmm(s[0])))), hi = Math.max(...all.map((s) => mins(hhmm(s[1]))));
  lo = Math.min(8 * 60, Math.floor(lo / 60) * 60); hi = Math.max(18 * 60, Math.ceil(hi / 60) * 60);
  const span = Math.max(60, hi - lo);
  const x = (t) => Math.max(0, Math.min(100, ((mins(hhmm(t)) - lo) / span) * 100));
  const ticks = [];
  for (let m = lo; m <= hi; m += Math.max(60, Math.round(span / 6 / 60) * 60)) ticks.push(html`<span key=${m} style=${{ left: ((m - lo) / span) * 100 + '%' }}>${pad(Math.floor(m / 60))}:00</span>`);
  const nowMinutes = now.getHours() * 60 + now.getMinutes();
  return html`<div class="dayline">
    <div class="dayline-axis" aria-hidden="true">${ticks}</div>
    <div class="dayline-row"><span class="dayline-label">Worked</span><div class="dayline-track">
      ${lanes.map((l) => (l.spans || []).map((s, i) => html`<button type="button" key=${(l.ticket || 'other') + i} class=${'dayline-span ' + slots[l.ticket || '']}
        style=${{ left: x(s[0]) + '%', width: Math.max(0.4, x(s[1]) - x(s[0])) + '%' }}
        aria-label=${(l.ticket || 'Other work') + ', ' + hhmm(s[0]) + ' to ' + hhmm(s[1])}
        onClick=${() => setSelected({ ticket: l.ticket, title: l.title, from: hhmm(s[0]), to: hhmm(s[1]) })}>
        <span>${l.ticket || 'Other'}</span></button>`))}
      ${commits.map((c, i) => html`<span key=${i} class="dayline-commit" style=${{ left: x(c.time) + '%' }} title=${'Commit ' + c.time + ': ' + (c.subject || '')}></span>`)}
      ${isToday && nowMinutes >= lo && nowMinutes <= hi ? html`<span class="dayline-now" style=${{ left: ((nowMinutes - lo) / span) * 100 + '%' }}><span>now</span></span>` : null}
    </div></div>
    <div class="dayline-row"><span class="dayline-label">Jira</span><div class="dayline-track jira-track">
      ${(entries || []).map((e, i) => html`<span key=${i} class="jira-span" style=${{ left: x(e.start) + '%', width: Math.max(0.4, Math.min(100 - x(e.start), (e.minutes || 0) / span * 100)) + '%' }}
        title=${e.ticket + ': ' + e.start + ', ' + e.duration}><span>${e.ticket}</span></span>`)}
    </div></div>
  </div>
  ${selected ? html`<div class="dayline-detail" role="status"><span>${selected.from}–${selected.to}</span>
    <span>${selected.ticket ? html`<${TicketLink} k=${selected.ticket} />` : 'Other work'} ${selected.title || ''}</span>
    <button type="button" class="btn quiet" onClick=${() => setSelected(null)}>Close</button></div>` : null}
  <div class="legend"><span><i class="tick"></i>Commit</span><span>Choose a stretch to see the work. Jira shows the rounded entries below.</span></div>
  <details class="more"><summary>Show the day as a table</summary>
    <${Scroll} label="The day as a table"><table><caption class="sr">Time per ticket with the times worked</caption>
      <thead><tr><th scope="col">Ticket</th><th scope="col">Worked</th><th scope="col" class="num">Time</th></tr></thead>
      <tbody>${lanes.map((l) => html`<tr key=${l.ticket || 'other'}><td>${l.ticket || 'Other work'}</td><td>${(l.spans || []).map((s) => hhmm(s[0]) + '–' + hhmm(s[1])).join(', ')}</td><td class="num">${fmtMin(l.minutes)}</td></tr>`)}</tbody>
    </table></${Scroll}></details>`;
}

// Refresh from Jira / Preview exactly: each is one read-only call within the daily MCP budget, run on request.
function PreviewButtons({ d, pv, onDone }) {
  const [busy, setBusy] = useState('');
  if (!d.preview_buttons || pv.kind !== 'message' || !(d.jira_on || pv.pending_condense)) return null;
  const run = (name, done) => {
    setBusy(name);
    runAction(name, { day: d.day }).then((r) => { toast(r.ok ? done(r) : 'Not run: ' + r.skipped + '.', r.ok ? 'ok' : 'info'); onDone(); },
      (e) => toast(e.message, 'bad')).finally(() => setBusy(''));
  };
  return html`<div class="btnline" style="margin-top:12px">
    ${d.jira_on ? html`<button type="button" class="btn" disabled=${!!busy} onClick=${() => run('worklog-refresh', () => 'Jira data refreshed')}
      title="One read-only Jira call within the daily MCP budget"><${Icon} name="reset" size=${15} />${busy === 'worklog-refresh' ? 'Reading Jira…' : 'Refresh from Jira'}</button>` : null}
    ${pv.pending_condense ? html`<button type="button" class="btn" disabled=${!!busy} onClick=${() => run('worklog-exact', (r) => r.model_calls ? 'Descriptions condensed as at 20:00' : 'Already exact')}
      title="One condensing call within the daily MCP budget; the 20:00 message reuses its lines"><${Icon} name="check" size=${15} />${busy === 'worklog-exact' ? 'Condensing…' : 'Preview exactly'}</button>` : null}
    <span class="muted small">One call per press, within the daily MCP budget.</span></div>`;
}

function Entries({ d, onReload }) {
  const saved = d.worklog_saved, pv = d.worklog_preview || {};
  const useSaved = saved && saved.entries && saved.entries.length;
  const entries = useSaved ? saved.entries : (pv.entries || []);
  const text = useSaved ? saved.text : (pv.text || '');
  const total = entries.reduce((a, e) => a + (e.minutes || 0), 0);
  let state;
  if (useSaved) state = html`<${Status} kind=${saved.status === 'sent' || saved.status === 'nudged' ? 'ok' : saved.status === 'failed' ? 'bad' : 'info'}>${SAVED_WORD[saved.status] || saved.status}${saved.sent_at ? ' at ' + fmtTs(saved.sent_at) : ''}</${Status}>`;
  else if (pv.kind === 'message') state = html`<${Status} kind="wait">Preview · sends at ${pv.send_at || '20:00'}</${Status}>`;
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
        <div class="entry-ticket"><${TicketLink} k=${e.ticket} />${e.manual ? html`<span class="muted small">Logged by hand</span>` : null}</div>
        <div class="fld"><span class="lab">Duration</span><div class="val"><span>${e.duration}</span><${CopyButton} text=${e.duration} quiet what=${'duration ' + what} /></div></div>
        <div class="fld desc"><span class="lab sr">Description</span>
          <div class="val txt"><span>${e.description}${!useSaved && e.pending_condense ? html` <span class="chip small" title="This line is shortened by the model when the message is composed at 20:00">condensed at ${pv.send_at || '20:00'}</span>` : null}</span><${CopyButton} text=${e.description} quiet what=${'description ' + what} /></div></div></div>`;
    })}</div>
    <div class="entry-foot"><span class="muted small">${useSaved ? 'Version ' + (saved.version || 1) + '. '
      : 'The message the sender would send now' + (pv.facts_as_of ? ' · ' + pv.facts_as_of : ' · no Jira data yet, so time already in Jira isn\'t subtracted') + (pv.pending_condense ? ' · marked lines are condensed at ' + (pv.send_at || '20:00') : '') + '. '}Copy each value into Jira; nothing is sent from this page.</span>
      ${text ? html`<${CopyButton} text=${text} label="Copy all" what="the whole Jira message" primary kbd="c" />` : null}</div>
    ${text ? html`<details class="more"><summary>Show the full message</summary><pre tabindex="0">${text}</pre></details>` : null}`;
  }
  if (!useSaved) body = html`${body}<${PreviewButtons} d=${d} pv=${pv} onDone=${onReload} />`;
  body = html`${body}<${SendHistory} saved=${saved} day=${d.day} />`;
  return html`<section class="sec worklog-ledger" aria-labelledby="sec-jira"><div class="sec-head"><h2 id="sec-jira">${d.is_today === false ? 'Worklog' : "Tonight's worklog"}</h2>
    <p class="sub">${entries.length ? fmtMin(total) + ' in ' + plural(entries.length, 'entry', 'entries') : ''}${pv.target ? ' · target ' + fmtMin(pv.target) : ''}</p><span class="grow"></span>${state}</div>${body}</section>`;
}

function DayEvents({ events }) {
  if (!events || !events.length) return null;
  return html`<section class="sec" aria-labelledby="sec-git"><div class="sec-head"><h2 id="sec-git">Branches and PRs that day</h2><p class="sub">${plural(events.length, 'event')}</p></div>
    <ul class="other-list">${events.map((e, i) => html`<li key=${i}><span><span class="mono small">${e.time || '··:··'}</span> ${e.ticket ? html`<${TicketLink} k=${e.ticket} /> ` : null}${e.text}
      ${e.repo ? html` <span class="muted">(${e.repo})</span>` : null}</span><span class="muted small">${e.kind === 'pr' ? 'PR' : 'branch'}</span></li>`)}</ul></section>`;
}

const STATE_WORD = { not_connected: 'Slack not connected', off: 'Slack switched off' };
function rid() { try { return crypto.randomUUID(); } catch (e) { return Date.now().toString(36) + Math.random().toString(36).slice(2); } }

function SendAgain({ day }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [id, setId] = useState('');
  const send = () => {
    setBusy(true);
    runAction('worklog-resend', { day, request_id: id }).then((r) => { toast(r.duplicate ? 'Already asked; it goes out once' : 'Queued: the next background run sends it', 'ok'); setOpen(false); },
      (e) => toast(e.message, 'bad')).finally(() => setBusy(false));
  };
  return html`<button type="button" class="btn" onClick=${() => { setId(rid()); setOpen(true); }}><${Icon} name="chat" size=${15} />Send again</button>
    <${Dialog} open=${open} title="Send this day's message again?" onClose=${() => setOpen(false)}>
      <p>The next background run (within a few minutes) composes ${fmtDay(day)} again and sends it to you on Slack as a new version. Nothing goes to Jira.</p>
      <div class="pagehead-act" style="margin-top:16px;justify-content:flex-end"><button type="button" class="btn" onClick=${() => setOpen(false)}>Cancel</button>
        <button type="button" class="btn primary" disabled=${busy} onClick=${send}>${busy ? 'Queuing…' : 'Send again'}</button></div></${Dialog}>`;
}

function SendHistory({ saved, day }) {
  const h = (saved && saved.history) || [];
  if (!h.length) return null;
  return html`<div class="btnline" style="margin-top:12px"><${SendAgain} day=${day} /></div>
    <details class="more" style="margin-top:12px"><summary>Send history (${plural(h.length, 'version')})</summary>
    <${Scroll} label="Send history"><table><caption class="sr">Every version of this day's message</caption>
      <thead><tr><th scope="col">Version</th><th scope="col">When</th><th scope="col">Why</th><th scope="col">Slack</th></tr></thead>
      <tbody>${h.map((x) => html`<tr key=${x.v}><td>${x.v}</td><td class="nowrap">${fmtTs(x.at)}</td><td>${x.why || x.kind || 'first'}</td>
        <td>${x.sent ? html`<${Status} kind="ok" quiet>Sent</${Status}>` : x.error ? html`<${Status} kind="bad" quiet>${x.error}</${Status}>` : html`<${Status} kind="off" quiet>${STATE_WORD[x.state] || 'Not sent'}</${Status}>`}</td></tr>`)}</tbody>
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
  // Stretches come from activity blocks; minutes come from the day file, like the summary, the
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
  const dayKind = d && d.day_kind;
  const suppliedTarget = d && (d.target_minutes ?? (d.worklog_preview || {}).target);
  const target = suppliedTarget ?? (['weekend', 'holiday', 'leave'].includes(dayKind) ? 0 : 480);
  const isToday = d ? d.is_today !== false && !day : !day;
  const dayTools = html`<div class="day-tools"><div class="daynav">
        <a class="btn" href=${shown ? '#/day/' + addDays(shown, -1) : '#/today'}><${Icon} name="left" size=${16} />Previous day</a>
        ${day ? html`<a class="btn" href="#/today">Today</a>` : null}
        ${day ? html`<a class="btn" href=${'#/day/' + addDays(shown, 1)}>Next<${Icon} name="right" size=${16} /></a>` : null}
      </div>
      <label class="field"><span class="sr">Go to a day</span>
        <input type="date" value=${shown || ''} max=${isoDay(new Date())} onChange=${(e) => { const v = e.currentTarget.value; if (v) go('#/day/' + v); }} /></label></div>`;
  const ticketSummary = html`<section class="sec day-tickets"><h2>${isToday ? "Today's tickets" : 'Tickets that day'}</h2>
    <ul class="rlist">${lanes.length ? lanes.map((l) => html`<li key=${l.ticket || 'other'}>${l.ticket
      ? html`<a href=${'#/tickets/' + encodeURIComponent(l.ticket)} class=${slots[l.ticket]}><span class="sw"></span><span><span class="k">${l.ticket}</span><span class="t">${l.title || ''}</span></span><span class="m">${fmtMin(l.minutes)}</span></a>`
      : html`<div class="ritem to"><span class="sw"></span><span><span class="k">Other work</span><span class="t">No ticket</span></span><span class="m">${fmtMin(l.minutes)}</span></div>`}</li>`)
      : html`<li class="muted small" style="padding:8px">No tickets yet.</li>`}</ul></section>`;
  return html`<${Desk} className="today-desk">
    <${Load} res=${res} lines=${8}>${(d) => html`
      <${PageHead} title=${fmtDay(d.day)} sub=${plural(d.sessions || 0, 'session') + ' · ' + plural(d.commits || 0, 'commit') + (d.closed ? ' · day closed' : '')}>
        ${dayTools}
      </${PageHead}>
      <div class="day-total"><div><span class="tracked-total">${fmtMin(d.total_minutes)}</span><span class="total-target">${target ? 'of ' + fmtMin(target) : 'tracked'}</span></div>
        <p>${target ? d.total_minutes >= target ? 'Daily target reached.' : fmtMin(Math.max(0, target - d.total_minutes)) + ' to your daily target.' : ({ weekend: 'A weekend. No daily target.', holiday: 'A holiday. No daily target.', leave: 'On leave. No daily target.' }[dayKind] || 'No daily target.')}${d.updated_at ? html`<span class="updated-at">Updated ${fmtTs(d.updated_at)}</span>` : null}</p></div>
      ${d.summary ? html`<p class="lede">${d.summary}</p>` : null}
      ${!(d.tickets || []).length && !(d.other || []).length && !d.worklog_saved ? html`<${Empty} title=${'Nothing was recorded on ' + fmtDay(d.day)} icon="day">
        No Claude Code sessions, commits or Jira entries for this day. Use the date picker or previous and next to move; <a href="#/today">go to Today</a>.</${Empty}>` : html`
      <section class="sec dayline-section" aria-labelledby="sec-day"><div class="sec-head"><h2 id="sec-day">The day line</h2>
        <p class="sub">${fmtMin(d.total_minutes)} tracked${d.other && d.other.length ? ', ' + fmtMin(d.other.reduce((a, o) => a + o.minutes, 0)) + ' without a ticket' : ''}</p></div>
        <${Lanes} lanes=${lanes} commits=${d.commit_list || []} slots=${slots} entries=${(d.worklog_saved && d.worklog_saved.entries && d.worklog_saved.entries.length ? d.worklog_saved.entries : (d.worklog_preview || {}).entries) || []} isToday=${isToday} /></section>`}
      <div class="day-bottom"><div><${Entries} d=${Object.assign({ is_today: isToday }, d)} onReload=${res.reload} /></div>
        <aside class="day-margin" aria-label="Day notes">${ticketSummary}<h2>Needs you</h2><${Alerts} tickets=${day ? dayAlerts(d.alerts) : tks.data && tks.data.tickets} /></aside></div>
      <${DayEvents} events=${d.branch_events} />
      ${d.other && d.other.length ? html`<section class="sec" aria-labelledby="sec-other"><div class="sec-head"><h2 id="sec-other">Other work</h2><p class="sub">Repos and branches with no ticket</p></div>
        <ul class="other-list">${d.other.map((o) => html`<li key=${o.group}><span>${o.group}</span><span>${fmtMin(o.minutes)}</span></li>`)}</ul></section>` : null}
    `}</${Load}></${Desk}>`;
}
