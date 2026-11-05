// Releases: your tickets by planned release date for one month (release.month_overview via /api/releases). What needs
// attention comes first (overdue, release date unresolved), then each release of the month with its unfinished
// tickets; ready tickets fold away. A ticket is one compact row: the repositories still pending and the next action;
// its repositories' stages, PRs and evidence open on request, one stacked block per repository, so nothing needs a
// wide table on a phone. One repository finished never hides another that isn't. #/releases?month=YYYY-MM.
import { useEffect } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, setParams, monthName, pad, fmtDay, fmtTs, plural } from '../lib/core.js';
import { Icon, Status, Empty, Load, PageHead, TicketLink } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';
import { ROLES, StageMark, PrRef, ReadyStatus, Reminders, Freshness, UatFacts, CodeText, since } from '../lib/release.js';

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
function thisMonth() { const d = new Date(); return d.getFullYear() + '-' + pad(d.getMonth() + 1); }
function shiftMonth(ym, n) { const p = ym.split('-').map(Number); const d = new Date(p[0], p[1] - 1 + n, 1); return d.getFullYear() + '-' + pad(d.getMonth() + 1); }

// The first stage of a repository that isn't done, in the order the work goes: release, QA, then UAT when it applies.
function pending(x) {
  for (const [k, l] of ROLES) {
    const st = (x[k] || {}).state;
    if (st && st !== 'present' && st !== 'not_applicable') return { role: k, label: l, state: st };
  }
  return null;
}

function RepoBlock({ x, uatReq }) {
  const f = x.freshness || {};
  return html`<section class="relrepo-b" aria-label=${'Repository ' + x.repo}>
    <h4>${x.repo}${f.stale ? html` <${Status} kind="warn" quiet>refs stale</${Status}>` : null}</h4>
    <dl class="kv relkv">
      <dt>Release branch</dt><dd>${x.branch ? html`<span class="mono wrap">${x.branch}</span>` : html`<${Status} kind="warn" quiet>not confirmed</${Status}>`}${x.branch_state && !x.branch ? html` <span class="muted small">${x.branch_state}</span>` : null}</dd>
    </dl>
    <ul class="relstages rb-stages" aria-label=${'Stages in ' + x.repo}>${ROLES.map(([k, label]) => html`<li key=${k}><span class="lab">${label}</span>
      <span class="stg-cell"><${StageMark} s=${x[k]} />${(x.prs || []).filter((p) => p.role === k).map((p) => html` <${PrRef} key=${(p.shared ? 's' : '') + p.number} p=${p} />`)}
        ${x[k] && x[k].detail && x[k].state !== 'present' ? html`<span class="muted small stg-detail">${x[k].detail}</span>` : null}</span></li>`)}</ul>
    ${(x.flags || []).map((fl, i) => html`<p key=${i} class="relflag small"><${Status} kind="warn" quiet>Flag:</${Status}> ${fl.text}</p>`)}
    ${x.next ? html`<p class="relnext small"><span class="lab">Next:</span> <${CodeText} text=${x.next} /></p>` : null}
    ${(f.errors || []).length ? html`<p class="small muted">${f.errors.join('; ')}</p>` : null}
  </section>`;
}

function TicketRow({ r, showRelease }) {
  const started = !!(r.computed_at || (r.repos || []).length);
  const id = 'rr-' + r.key;
  const req = ((r.uat || {}).requirement || {}).state;
  const open = (r.repos || []).map((x) => [x, pending(x)]).filter(([, p]) => p);
  const next = (open[0] && open[0][0].next) || ((r.repos || [])[0] || {}).next || '';
  const flagged = (r.repos || []).reduce((a, x) => a + (x.flags || []).length, 0);
  return html`<article class="relrow" aria-labelledby=${id}>
    <div class="relrow-head"><h3 id=${id}><${TicketLink} k=${r.key} />${r.title ? html`<span class="t">${r.title}</span>` : null}</h3>
      <${ReadyStatus} ready=${r.ready} started=${started} /></div>
    <p class="relmeta">
      ${showRelease ? (r.release ? html`<span>${r.release}</span>` : html`<span class="muted">No release chosen</span>`) : null}
      ${showRelease && r.planned_date ? html`<span>planned ${fmtDay(r.planned_date, true)}</span>` : null}
      ${r.unresolved && r.unresolved_why ? html`<span><${Status} kind="warn" quiet>Date unresolved:</${Status}> ${r.unresolved_why}</span>` : null}
      ${(r.repos || []).length ? html`<span>${open.length ? 'Pending: ' + open.map(([x, p]) => x.repo + ' ' + p.label + ' ' + p.state.replace('_', ' ')).join(' · ')
        : plural(r.repos.length, 'repository', 'repositories') + ' done'}</span>` : null}
      ${req && req !== 'not_required' ? html`<span>UAT <${UatFacts} uat=${r.uat} compact /></span>` : null}
      ${flagged ? html`<span><${Status} kind="warn" quiet>${plural(flagged, 'flag')}</${Status}></span>` : null}
      ${r.scope_stale ? html`<span class="muted">not seen in the latest Jira search yet</span>` : null}</p>
    ${!r.ready && next ? html`<p class="relnext"><span class="lab">Next:</span> <${CodeText} text=${next} /></p>`
      : !r.ready && !started ? html`<p class="relnext muted small">Not started: no branches or worklog yet.</p>` : null}
    ${(r.repos || []).length || (r.reminders || []).length || (r.missing || []).length ? html`<details class="more reldetail">
      <summary>Repositories, reminders and evidence</summary>
      <div class="relrepos">${(r.repos || []).map((x) => html`<${RepoBlock} key=${x.repo} x=${x} uatReq=${req} />`)}</div>
      <${Reminders} list=${(r.reminders || []).filter((m) => !(r.repos || []).some((x) => (x.flags || []).some((f) => m.text.indexOf(f.text) >= 0)))} />
      ${!r.ready && (r.missing || []).length ? html`<div class="relmiss"><p class="lab">Not ready because</p><ul>${r.missing.map((m, i) => html`<li key=${i}>${m}</li>`)}</ul></div>` : null}
      <p class="small muted">Yours: ${r.why}${r.date_source ? ' · planned date from ' + r.date_source : ''}</p>
      <${Freshness} computed=${r.computed_at} jira=${r.jira} repos=${(r.repos || []).map((x) => [x.repo, x.freshness])} />
    </details>` : null}
  </article>`;
}

function Group({ id, title, sub, rows, showRelease, tone }) {
  const todo = rows.filter((r) => !r.ready), ready = rows.filter((r) => r.ready);
  return html`<section class=${'relgroup' + (tone ? ' ' + tone : '')} aria-labelledby=${id}>
    <div class="sec-head"><h2 id=${id}>${title}</h2><p class="sub">${sub}</p></div>
    ${todo.map((r) => html`<${TicketRow} key=${r.key} r=${r} showRelease=${showRelease} />`)}
    ${ready.length ? html`<details class="more relready"><summary>${plural(ready.length, 'ready ticket')}</summary>
      ${ready.map((r) => html`<${TicketRow} key=${r.key} r=${r} showRelease=${showRelease} />`)}</details>` : null}</section>`;
}

function ScopeLine({ sc }) {
  if (!sc) return null;
  if (sc.unconfigured) return html`<span><${Status} kind="info" quiet>Jira scope search not run:</${Status}> ${sc.unconfigured} (<a href="#/profile">Profile</a>)</span>`;
  if (!sc.fetched_at && !sc.error) return html`<span class="muted">Jira scope search: not run yet</span>`;
  const when = sc.fetched_at ? fmtTs(sc.fetched_at) + (since(sc.fetched_at) ? ' (' + since(sc.fetched_at) + ')' : '') : 'never';
  if (sc.error) return html`<span><${Status} kind="warn" quiet>Jira scope search failed:</${Status}> ${String(sc.error).slice(0, 160)}. Showing what was known at ${when}; nothing is treated as gone.</span>`;
  if (!sc.complete) return html`<span><${Status} kind="warn" quiet>Jira scope search partial:</${Status}> ${sc.partial_reason || 'not every page read yet'}; earlier tickets stay listed.</span>`;
  return html`<span class="muted">Jira scope search complete, read ${when}</span>`;
}

export function ReleasesPage({ route }) {
  const want = MONTH_RE.test(route.params.month || '') ? route.params.month : '';
  const res = useApi('/api/releases' + (want ? '?month=' + want : ''));
  const d = res.data;
  const ym = want || (d && d.month) || thisMonth();
  const now = d && d.today ? d.today.slice(0, 7) : thisMonth();
  const to = (m) => { if (MONTH_RE.test(m || '')) setParams({ month: m }); };
  useEffect(() => { document.title = 'Releases · Chitragupta'; }, []);
  useEffect(() => {
    pageKeys.set({ p: { label: 'Previous month', run: () => to(shiftMonth(ym, -1)) }, n: { label: 'Next month', run: () => to(shiftMonth(ym, 1)) } });
    return () => pageKeys.set({});
  }, [ym]);
  const prev = shiftMonth(ym, -1), next = shiftMonth(ym, 1);
  return html`<${Desk} className="releases-desk">
    <${PageHead} title="Releases" sub=${monthName(ym) + ' · your tickets by planned release date'}>
      <div class="month-actions">
        <button type="button" class="btn quiet" onClick=${() => to(prev)} aria-label=${'Previous month: ' + monthName(prev)}><${Icon} name="left" /><span class="mlabel">${monthName(prev)}</span></button>
        <label class="field"><span class="sr">Show a month</span>
          <input type="month" value=${ym} onChange=${(e) => to(e.currentTarget.value)} /></label>
        <button type="button" class="btn quiet" onClick=${() => to(next)} aria-label=${'Next month: ' + monthName(next)}><span class="mlabel">${monthName(next)}</span><${Icon} name="right" /></button>
        ${ym !== now ? html`<button type="button" class="btn" onClick=${() => to(now)}>This month</button>` : null}
      </div>
    </${PageHead}>
    <${Load} res=${res} lines=${8}>${(ov) => {
      const groups = ov.groups || [], overdue = ov.overdue || [], unresolved = ov.unresolved || [];
      const total = groups.reduce((a, g) => a + g.tickets.length, 0);
      const ready = groups.reduce((a, g) => a + g.tickets.filter((r) => r.ready).length, 0);
      return html`
        ${!ov.enabled ? html`<div class="alert quietalert" role="status"><${Icon} name="info" size=${18} /><div class="grow">
          <strong>Release tracking is off.</strong> Turn it on with <code>releases.enabled</code> in config.json (see docs/releases.md);
          until then nothing new is computed and this page shows only records kept from earlier.</div></div>` : null}
        <p class="relscope">${total ? plural(total, 'ticket') + ' planned in ' + monthName(ov.month) + (ready ? ' · ' + ready + ' ready' : '') : 'Nothing planned in ' + monthName(ov.month)}${overdue.length ? ' · ' + overdue.length + ' overdue' : ''}${unresolved.length ? ' · ' + unresolved.length + ' without a release date' : ''}
          <br /><${ScopeLine} sc=${ov.scope} /></p>
        ${overdue.length ? html`<${Group} id="rg-overdue" tone="attn" title="Overdue" sub=${'Planned before ' + fmtDay(ov.today, true) + ' and not complete · ' + plural(overdue.length, 'ticket')} rows=${overdue} showRelease />` : null}
        ${unresolved.length ? html`<${Group} id="rg-unresolved" tone="attn" title="Release date unresolved" sub=${'No single release or no planned date yet · ' + plural(unresolved.length, 'ticket')} rows=${unresolved} showRelease />` : null}
        ${groups.map((g, i) => html`<${Group} key=${g.release} id=${'rg-' + i} title=${g.release || 'Release'} sub=${'Planned ' + fmtDay(g.planned_date) + ' · ' + plural(g.tickets.length, 'ticket') + (g.tickets.some((r) => r.ready) ? ', ' + g.tickets.filter((r) => r.ready).length + ' ready' : '')} rows=${g.tickets} />`)}
        ${!(groups.length || overdue.length || unresolved.length) ? html`<${Empty} title=${'No tickets of yours are planned for ' + monthName(ov.month)} icon="release">
          Tickets appear here once Jira gives them a fix version with a release date and they are yours: assigned to your confirmed
          Jira account, on your own development branch, or worked on in Claude Code. ${ov.enabled ? '' : 'Release tracking is off.'}</${Empty}>` : null}`;
    }}</${Load}>
  </${Desk}>`;
}
