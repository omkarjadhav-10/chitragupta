// Releases: your tickets by planned release date for one month (release.month_overview via /api/releases), then the
// overdue ones and those whose release date is unresolved. Each ticket keeps its per-repo state, so one finished repo
// never hides another that isn't. The month lives in the address: #/releases?month=YYYY-MM.
import { useEffect } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, setParams, monthName, pad, fmtDay, fmtTs, plural } from '../lib/core.js';
import { Icon, Status, Empty, Load, PageHead, TicketLink, Scroll } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';
import { ROLES, StageMark, PrRef, ReadyStatus, Reminders, MissingList, Freshness, UatFacts, CodeText, since } from '../lib/release.js';

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
function thisMonth() { const d = new Date(); return d.getFullYear() + '-' + pad(d.getMonth() + 1); }
function shiftMonth(ym, n) { const p = ym.split('-').map(Number); const d = new Date(p[0], p[1] - 1 + n, 1); return d.getFullYear() + '-' + pad(d.getMonth() + 1); }

function StageCell({ s, prs }) {
  return html`<${StageMark} s=${s} />${s && s.detail ? html`<div class="stg-d">${s.detail}</div>` : null}
    ${prs.length ? html`<div class="prs">${prs.map((p) => html`<${PrRef} key=${(p.shared ? 's' : '') + p.number} p=${p} />`)}</div>` : null}`;
}

function RepoTable({ r }) {
  return html`<${Scroll} label=${'Repositories of ' + r.key}><table class="reltable">
    <caption class="sr">${r.key}: each repository's release branch, stages and next step</caption>
    <thead><tr><th scope="col">Repo</th><th scope="col">Release branch</th>${ROLES.map(([k, l]) => html`<th scope="col" key=${k}>${l}</th>`)}<th scope="col">Next step</th></tr></thead>
    <tbody>${r.repos.map((x) => { const f = x.freshness || {}; return html`<tr key=${x.repo}>
      <th scope="row" class="relrepo-name">${x.repo}${f.stale ? html`<div class="stg-d"><${Status} kind="warn" quiet>refs stale</${Status}> ${f.why_stale || ''}</div>` : null}
        ${(f.errors || []).map((e, i) => html`<div class="stg-d" key=${i}>${e}</div>`)}</th>
      <td class="rb">${x.branch ? html`<span class="mono">${x.branch}</span>` : html`<${Status} kind="warn" quiet>not confirmed</${Status}>`}${x.branch_state ? html`<div class="stg-d">${x.branch_state}</div>` : null}</td>
      ${ROLES.map(([k]) => html`<td key=${k}><${StageCell} s=${x[k]} prs=${(x.prs || []).filter((p) => p.role === k)} /></td>`)}
      <td class="relnext-cell">${(x.flags || []).map((fl, i) => html`<div key=${i} class="relflag"><${Status} kind="warn" quiet>Flag:</${Status}> ${fl.text}</div>`)}
        ${x.next ? html`<${CodeText} text=${x.next} />` : html`<span class="muted">—</span>`}</td></tr>`; })}</tbody></table></${Scroll}>`;
}

function TicketRow({ r }) {
  const started = !!(r.computed_at || (r.repos || []).length);
  const id = 'rr-' + r.key;
  const req = ((r.uat || {}).requirement || {}).state;
  return html`<article class="relrow" aria-labelledby=${id}>
    <div class="relrow-head"><h3 id=${id}><${TicketLink} k=${r.key} />${r.title ? html`<span class="t">${r.title}</span>` : null}</h3>
      <${ReadyStatus} ready=${r.ready} started=${started} /></div>
    <p class="relmeta">${r.release ? html`<span>Release <strong>${r.release}</strong></span>` : html`<span class="muted">No release chosen</span>`}
      ${r.planned_date ? html`<span>planned ${fmtDay(r.planned_date, true)}${r.date_source ? html` <span class="muted">(${r.date_source})</span>` : null}</span>`
        : r.unresolved_why ? html`<span><${Status} kind="warn" quiet>Date unresolved:</${Status}> ${r.unresolved_why}</span>` : null}
      ${r.why ? html`<span>Yours: ${r.why}</span>` : null}
      ${req ? html`<span>UAT <${UatFacts} uat=${r.uat} compact /></span>` : null}</p>
    ${(r.repos || []).length ? html`<${RepoTable} r=${r} />` : html`<p class="muted small">No repository work recorded yet.</p>`}
    <${Reminders} list=${r.reminders} />
    <${MissingList} ready=${r.ready} missing=${r.missing} />
    <${Freshness} computed=${r.computed_at} jira=${r.jira} repos=${(r.repos || []).map((x) => [x.repo, x.freshness])} />
  </article>`;
}

function Group({ id, title, sub, rows }) {
  return html`<section class="relgroup" aria-labelledby=${id}>
    <div class="sec-head"><h2 id=${id}>${title}</h2><p class="sub">${sub}</p></div>
    ${rows.map((r) => html`<${TicketRow} key=${r.key} r=${r} />`)}</section>`;
}

function counts(rows) {
  const ready = rows.filter((r) => r.ready === true).length;
  return plural(rows.length, 'ticket') + (ready ? ' · ' + ready + ' ready' : '');
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
        <button type="button" class="btn quiet" onClick=${() => to(prev)} aria-label=${'Previous month: ' + monthName(prev)}><${Icon} name="left" />${monthName(prev)}</button>
        <label class="field"><span class="sr">Show a month</span>
          <input type="month" value=${ym} onChange=${(e) => to(e.currentTarget.value)} /></label>
        <button type="button" class="btn quiet" onClick=${() => to(next)} aria-label=${'Next month: ' + monthName(next)}>${monthName(next)}<${Icon} name="right" /></button>
        ${ym !== now ? html`<button type="button" class="btn" onClick=${() => to(now)}>This month</button>` : null}
      </div>
    </${PageHead}>
    <${Load} res=${res} lines=${8}>${(ov) => {
      const groups = ov.groups || [], overdue = ov.overdue || [], unresolved = ov.unresolved || [];
      const sc = ov.scope || {};
      const total = groups.reduce((a, g) => a + g.tickets.length, 0);
      return html`
        ${!ov.enabled ? html`<div class="alert quietalert" role="status"><${Icon} name="info" size=${18} /><div class="grow">
          <strong>Release tracking is off.</strong> Turn it on with <code>releases.enabled</code> in config.json (see docs/releases.md);
          until then nothing new is computed and this page shows only records kept from earlier.</div></div>` : null}
        <p class="relscope">${total ? plural(total, 'ticket') + ' planned in ' + monthName(ov.month) : 'Nothing planned in ' + monthName(ov.month)}${overdue.length ? ' · ' + overdue.length + ' overdue' : ''}${unresolved.length ? ' · ' + unresolved.length + ' without a release date' : ''}
          <span class="muted"> · Jira scope search: ${sc.fetched_at ? 'read ' + fmtTs(sc.fetched_at) + (since(sc.fetched_at) ? ' (' + since(sc.fetched_at) + ')' : '') : 'not run yet'}</span>
          ${sc.error ? html` <${Status} kind="warn" quiet>last search failed:</${Status}> ${String(sc.error).slice(0, 200)}` : null}</p>
        ${groups.map((g, i) => html`<${Group} key=${g.release} id=${'rg-' + i} title=${g.release || 'Release'} sub=${'Planned ' + fmtDay(g.planned_date) + ' · ' + counts(g.tickets)} rows=${g.tickets} />`)}
        ${overdue.length ? html`<${Group} id="rg-overdue" title="Overdue" sub=${'Planned before ' + fmtDay(ov.today, true) + ' and not complete · ' + counts(overdue)} rows=${overdue} />` : null}
        ${unresolved.length ? html`<${Group} id="rg-unresolved" title="Release date unresolved" sub=${'No single release or no planned date yet · ' + plural(unresolved.length, 'ticket')} rows=${unresolved} />` : null}
        ${!(groups.length || overdue.length || unresolved.length) ? html`<${Empty} title=${'No tickets of yours are planned for ' + monthName(ov.month)} icon="release">
          Tickets appear here once Jira gives them a fix version with a release date and they are yours: assigned to you, on your
          own development branch, or worked on in Claude Code. ${ov.enabled ? '' : 'Release tracking is off.'}</${Empty}>` : null}`;
    }}</${Load}>
  </${Desk}>`;
}
