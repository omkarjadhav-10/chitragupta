// Release tracking, shared by the Releases page and a ticket's Branches & PRs tab: stage states, PRs, reminders,
// freshness and the per-ticket release plan. A state is always a symbol plus words, never colour alone, and "Ready"
// is shown only when the record's readiness says so. Everything here only reads (release.view / month_overview).
import { html } from './html.js';
import { isHttp, EXPORT, parseTs, fmtTs, fmtDay, ago, plural } from './core.js';
import { Icon, Status, Chip, Scroll, CopyButton } from './ui.js';

// stage state -> [symbol, words, tone]
export const STAGE = {
  present: ['✓', 'present', 'ok'], partial: ['◐', 'partial', 'warn'], missing: ['✗', 'missing', 'bad'],
  unknown: ['?', 'unknown', 'info'], not_started: ['○', 'not started', 'off'], not_applicable: ['–', 'not applicable', 'off'],
};
export const ROLES = [['release', 'Release'], ['qa', 'QA'], ['uat', 'UAT']];
const BASE = { ok: ['✓', 'from the release branch', 'ok'], wrong: ['✗', 'wrong base', 'bad'], unproven: ['?', 'unproven', 'info'], unknown: ['?', 'unknown', 'off'] };
const BRANCH = { confirmed: 'ok', linked: 'ok', suggested: 'warn', 'mapped-unverified': 'warn', 'mapped-missing': 'bad', unresolved: 'warn' };
const PSTATE = { merged: 'ok', open: 'warn', draft: 'off', closed: 'off' };
const SEVERITY = { alert: ['bad', 'Action'], warn: ['warn', 'Check'], info: ['info', 'Note'] };
const UAT_REQ = { required: 'required', not_required: 'not required', unknown: 'unknown' };

// "3 h ago" for a past timestamp; '' for a missing or future one (the browser's clock may be behind the record's)
export function since(ts) {
  const d = parseTs(ts);
  if (!d) return '';
  const m = Math.round((Date.now() - d.getTime()) / 60000);
  return m < 0 ? '' : ago(m);
}
function when(ts) { const s = since(ts); return fmtTs(ts) + (s ? ' (' + s + ')' : ''); }

// Text from the engine marks commands with backticks: they become <code>; unbalanced backticks stay as text.
export function CodeText({ text }) {
  const parts = String(text || '').split('`');
  if (parts.length % 2 === 0) return html`<span>${text}</span>`;
  return html`<span>${parts.map((p, i) => (i % 2 ? html`<code key=${i}>${p}</code>` : p))}</span>`;
}

export function StageMark({ s }) {
  const st = (s && s.state) || 'unknown';
  const [sym, word, tone] = STAGE[st] || STAGE.unknown;
  return html`<span class=${'stg ' + tone}><span aria-hidden="true">${sym}</span> ${word}</span>`;
}

// Ready only when the record's readiness says so; a ticket with no record yet is "Not started".
export function ReadyStatus({ ready, started }) {
  if (ready === true) return html`<${Status} kind="ok">Ready</${Status}>`;
  if (started === false) return html`<${Status} kind="off">Not started</${Status}>`;
  return html`<${Status} kind="warn">In progress</${Status}>`;
}

export function PrRef({ p }) {
  const n = '#' + p.number;
  const link = isHttp(p.url) && !EXPORT
    ? html`<a href=${p.url} rel="noopener noreferrer" target="_blank">${n}<span class="sr"> (opens in a new tab)</span></a>` : html`<span>${n}</span>`;
  return html`<span class="prref">${link} <span class="muted">${p.state || ''}</span>${p.shared ? html` <span class="lab">shared</span>` : null}${p.wrong_target ? html` <span class="lab bad">wrong target</span>` : null}</span>`;
}

export function Reminders({ list, title }) {
  if (!(list || []).length) return null;
  return html`<div class="relrem"><p class="lab">${title || plural(list.length, 'reminder')}</p><ul>${list.map((r) => {
    const [kind, word] = SEVERITY[r.severity] || SEVERITY.info;
    return html`<li key=${r.id || r.text}><${Status} kind=${kind} quiet>${word}:</${Status}> <${CodeText} text=${r.text} /></li>`;
  })}</ul></div>`;
}

export function MissingList({ ready, missing }) {
  if (ready === true || !(missing || []).length) return null;
  return html`<div class="relmiss"><p class="lab">Not ready because</p><ul>${missing.map((m, i) => html`<li key=${i}>${m}</li>`)}</ul></div>`;
}

// UAT required / deployed / accepted; `compact` leaves the sources out (the ticket's release plan has them).
export function UatFacts({ uat, compact }) {
  const u = uat || {}, req = u.requirement || {};
  const src = (s) => (s && !compact ? html` <span class="muted">(${s})</span>` : null);
  const fact = (label, f) => (f ? html`<span>${label} <strong>${f.state === 'yes' ? 'yes' : 'unverified'}</strong>${src(f.source)}</span>` : null);
  return html`<span class=${'uatfacts' + (compact ? ' compact' : '')}><span>${compact ? '' : 'Requirement '}<strong>${UAT_REQ[req.state] || 'unknown'}</strong>${src(req.source)}</span>
    ${req.state === 'required' ? html`${fact('deployed', u.deployed)}${fact('accepted', u.accepted)}` : null}</span>`;
}

// Computed time, stale refs and read errors of a record (or an overview row).
export function Freshness({ computed, jira, repos }) {
  const j = jira || {};
  const out = [computed ? 'Computed ' + when(computed) : 'Not computed yet'];
  if (j.error) out.push('Jira read failed: ' + String(j.error).slice(0, 200));
  else if (j.source === 'scope') out.push('From the Jira scope search');
  else if (!j.source) out.push('No Jira data read yet');
  else if (j.fetched_at) out.push('Jira read ' + when(j.fetched_at));
  (repos || []).forEach(([name, f]) => {
    if (!f) return;
    if (f.stale) out.push(name + ' refs ' + (f.why_stale || 'stale'));
    (f.errors || []).forEach((e) => out.push(name + ': ' + e));
  });
  const bad = !!j.error || (repos || []).some(([, f]) => f && (f.stale || (f.errors || []).length));
  return html`<p class="relfresh">${bad ? html`<${Icon} name="warn" size=${14} label="Some sources are stale or failed" /> ` : null}${out.join(' · ')}</p>`;
}

function selected(plan) { return (plan.releases || []).find((v) => v.key === plan.selected) || null; }

// The ticket header's one line: release, ready or in progress, and the reminder count.
export function ReleaseLine({ plan, onOpen, href }) {
  if (!plan) return null;
  const sel = selected(plan), n = (plan.reminders || []).length, rd = (plan.readiness || {}).ready;
  return html`<p class="relline"><span class="lab">Release:</span>
    <a href=${href} onClick=${(e) => { e.preventDefault(); onOpen(); }}>${sel ? sel.name : 'not resolved'}</a>
    ${sel && sel.planned_date ? html`<span class="muted">planned ${fmtDay(sel.planned_date, true)}</span>` : null}
    <${ReadyStatus} ready=${rd} />
    ${n ? html`<${Status} kind=${(plan.reminders || []).some((r) => r.severity === 'alert') ? 'bad' : 'warn'}>${plural(n, 'reminder')}</${Status}>` : null}</p>`;
}

function BaseLine({ br }) {
  const rb = br.state === 'confirmed' || br.state === 'linked' ? br.name : null;
  const tone = BRANCH[br.state] || 'warn';
  return html`${rb ? html`<span class="mono">${rb}</span> ` : null}<${Status} kind=${tone} quiet>${rb ? br.state : 'not certain'}</${Status}>
    ${rb ? html`<div class="muted small">${[br.source, br.confidence ? br.confidence + ' confidence' : '', br.verified_at ? 'verified ' + when(br.verified_at) : ''].filter(Boolean).join(' · ')}</div>`
      : br.name ? html`<div class="small">Mapped to <span class="mono">${br.name}</span>${br.source ? html` <span class="muted">(${br.source})</span>` : null}</div>` : null}
    ${br.missing ? html`<div class="small">${br.missing}</div>` : null}
    ${(br.candidates || []).length ? html`<div class="small">Suggestions to confirm:</div><ul class="relcands">${br.candidates.map((c) => html`<li key=${c.name}>
      <span class="mono">${c.name}</span> <span class="muted small">${c.why}${c.score != null ? ' · score ' + c.score : ''}</span></li>`)}</ul>` : null}`;
}

function DevBranches({ list, name }) {
  if (!(list || []).length) return null;
  return html`<p class="gitsub">Development branches</p><${Scroll} label=${'Development branches in ' + name}><table class="reltab">
    <caption class="sr">Development branches of this ticket in ${name} and where they started</caption>
    <thead><tr><th scope="col">Branch</th><th scope="col">Started from</th><th scope="col">Base check</th><th scope="col">Note</th></tr></thead>
    <tbody>${list.map((d) => { const [sym, word, tone] = BASE[d.base] || BASE.unknown; return html`<tr key=${d.branch}>
      <td class="mono">${d.branch}</td><td><span class="mono">${d.parent || '—'}</span>${d.parent_source ? html` <span class="muted small">(${d.parent_source})</span>` : null}</td>
      <td><span class=${'stg ' + tone}><span aria-hidden="true">${sym}</span> ${word}</span></td><td class="small">${d.note || ''}</td></tr>`; })}</tbody></table></${Scroll}>`;
}

function prNote(p) {
  return [p.shared ? 'shared integration PR' + (p.carries != null ? ': carries ' + p.carries + ' of ' + p.of + ' of this ticket\'s commits' : '') : '',
    p.note || ''].filter(Boolean).join('; ');
}
function RepoPrs({ e, name }) {
  const by = e.prs || {};
  const list = ROLES.map(([r]) => r).concat(Object.keys(by).filter((r) => !ROLES.some(([x]) => x === r)))
    .reduce((a, r) => a.concat((by[r] || []).map((p) => Object.assign({ role: r }, p))), []).concat(e.shared || []);
  if (!list.length) return html`<p class="muted small">No pull requests for this ticket here yet.</p>`;
  return html`<p class="gitsub">Pull requests by destination</p><${Scroll} label=${'Pull requests in ' + name}><table class="reltab">
    <caption class="sr">Pull requests of this ticket in ${name}, by destination</caption>
    <thead><tr><th scope="col">PR</th><th scope="col">Destination</th><th scope="col">Source to target</th><th scope="col">State</th><th scope="col">Note</th></tr></thead>
    <tbody>${list.map((p) => html`<tr key=${(p.shared ? 's' : '') + p.number}>
      <td class="nowrap">${isHttp(p.url) && !EXPORT ? html`<a href=${p.url} rel="noopener noreferrer" target="_blank">#${p.number}<span class="sr"> (opens in a new tab)</span></a>` : '#' + p.number}</td>
      <td>${(ROLES.find(([r]) => r === p.role) || [0, p.role || 'other'])[1]}${p.shared ? html` <span class="lab">shared</span>` : null}</td>
      <td class="mono small">${(p.source || '?') + ' → ' + (p.target || '?')}</td>
      <td><${Status} kind=${PSTATE[p.state] || 'off'}>${p.state || '—'}</${Status}></td>
      <td class="small">${p.wrong_target ? html`<${Status} kind="bad" quiet>Wrong target</${Status}> ` : null}${prNote(p)}</td></tr>`)}</tbody></table></${Scroll}>`;
}

function Uncertainty({ f }) {
  if (!f) return null;
  const out = (f.stale ? ['refs ' + (f.why_stale || 'stale')] : []).concat(f.errors || [])
    .concat(f.open_prs_visible === false ? ['open PRs aren\'t visible (PR info from merge commits only)'] : []);
  return out.length ? html`<p class="small"><${Status} kind="warn" quiet>Uncertain:</${Status}> ${out.join('; ')}</p>` : null;
}

function RepoPlan({ name, e, uatReq }) {
  const br = e.branch || {}, rb = br.state === 'confirmed' || br.state === 'linked' ? br.name : null;
  const u = e.uat_conf, st = e.stages || {};
  const id = 'rp-' + name.replace(/[^A-Za-z0-9_-]/g, '_');
  const devWrong = (e.dev || []).filter((d) => d.base === 'wrong');
  const stageDd = (s, keepDone) => html`<${StageMark} s=${s} /> <span class="muted small">${(s || {}).state === 'present' && !keepDone ? '' : (s || {}).detail || ''}</span>`;
  return html`<section class="relrepo" aria-labelledby=${id}>
    <h3 id=${id}><${Icon} name="branch" size=${17} />${name}${e.candidate ? html`<${Chip}>candidate: no branches yet</${Chip}>` : null}</h3>
    <dl class="kv relkv">
      <dt>Development base</dt><dd>${rb ? html`<span class="mono wrap">${rb}</span>` : html`<${Status} kind=${BRANCH[br.state] || 'warn'} quiet>not certain</${Status}> <span class="small">${br.missing || ''}</span>`}
        ${devWrong.length ? html`<div class="small"><${Status} kind="bad" quiet>Wrong base:</${Status}> ${devWrong.map((d) => d.branch + ' started from ' + d.parent).join('; ')}</div>` : null}</dd>
      <dt>Development PR target</dt><dd>${rb ? html`<span class="mono wrap">${rb}</span>` : html`<span class="muted">none until the release branch is confirmed</span>`}</dd>
      ${e.candidate ? null : html`<dt>Release</dt><dd>${stageDd(st.release)}</dd><dt>QA</dt><dd>${stageDd(st.qa)}</dd>
        <dt>UAT</dt><dd>${stageDd(st.uat, true)}
          ${uatReq === 'required' && u && u.branch ? html`<div class="muted small">into <span class="mono">${u.branch}</span> via ${(u.promotion || {}).kind || 'unspecified'}${(u.promotion || {}).from ? ' from ' + u.promotion.from : ''}</div>` : null}</dd>`}
    </dl>
    ${(e.flags || []).length ? html`<ul class="relflags">${e.flags.map((f, i) => html`<li key=${i}><${Status} kind="warn" quiet>Flag:</${Status}> ${f.text}</li>`)}</ul>` : null}
    ${e.next ? html`<p class="relnext"><span class="lab">Next step:</span> <${CodeText} text=${e.next} /></p>` : null}
    <${Uncertainty} f=${e.freshness} />
    <details class="more"><summary>Evidence for ${name}: branches, pull requests and how the base was found</summary>
      <dl class="kv relkv"><dt>Release branch</dt><dd><${BaseLine} br=${br} /></dd>
        ${e.strategy ? html`<dt>Strategy</dt><dd>${e.strategy}</dd>` : null}
        ${e.affected && e.affected.how ? html`<dt>Why this repository</dt><dd>${e.affected.how}</dd>` : null}</dl>
      ${e.candidate ? null : html`<${DevBranches} list=${e.dev} name=${name} /><${RepoPrs} e=${e} name=${name} />`}
    </details>
  </section>`;
}

// One list of what stands between the ticket and "ready": the reminders and the readiness gaps, each said once.
function Blockers({ plan }) {
  const rd = plan.readiness || {};
  if (rd.ready) return null;
  const seen = new Set(), items = [];
  // what each repository block already shows (its flags, its release / QA / UAT state) isn't said again here
  const shown = new Set(['wrong-base', 'old-release-base', 'wrong-target', 'old-release-pr', 'dev-into-integration']);
  (plan.reminders || []).forEach((r) => { if (!shown.has(r.kind) && !seen.has(r.text)) { seen.add(r.text); items.push(r); } });
  (rd.missing || []).forEach((m) => {
    if (/: \d+ flags?$/.test(m) || /^[^:]+: (release|qa|uat) [a-z ]+$/.test(m)) return;
    if (!seen.has(m)) { seen.add(m); items.push({ text: m, severity: 'info' }); }
  });
  if (!items.length) return null;
  const top = items.slice(0, 4), rest = items.slice(4);
  const line = (r, i) => { const [kind, word] = SEVERITY[r.severity] || SEVERITY.info; return html`<li key=${i}><${Status} kind=${kind} quiet>${word}:</${Status}> <${CodeText} text=${r.text} /></li>`; };
  return html`<div class="relrem"><p class="lab">Not ready yet</p><ul>${top.map(line)}</ul>
    ${rest.length ? html`<details class="more"><summary>${plural(rest.length, 'more item')}</summary><ul>${rest.map(line)}</ul></details>` : null}</div>`;
}

// The ticket's release plan at the top of Branches & PRs (release.view): the release once, then per repository the
// development base, PR target, QA, UAT and the next step; blockers once; evidence and provenance on request.
export function ReleasePlan({ plan, brief, k }) {
  if (!plan) return null;
  const sel = selected(plan), how = plan.selection || {}, rd = plan.readiness || {};
  const req = ((plan.uat || {}).requirement || {}).state;
  const names = Object.keys(plan.repos || {}).sort((a, b) => (!!plan.repos[a].candidate - !!plan.repos[b].candidate) || a.localeCompare(b));
  const hist = (plan.uat || {}).history || [];
  return html`<section class="relplan" id="release-plan" tabindex="-1" aria-labelledby="rp-title">
    <div class="sec-head"><h2 id="rp-title">Release plan</h2><${ReadyStatus} ready=${rd.ready} /><span class="grow"></span>
      ${(brief || []).length && !EXPORT ? html`<${CopyButton} text=${brief.join('\n')} label="Copy as text" what="the release plan" />` : null}</div>
    <dl class="kv relkv">
      <dt>Release</dt><dd>${sel ? html`<strong>${sel.name}</strong> · ${sel.planned_date ? 'planned ' + fmtDay(sel.planned_date) : 'planned date unknown'}${sel.date_source ? html` <span class="muted">(${sel.date_source})</span>` : null}`
        : html`<${Status} kind="warn" quiet>Not resolved:</${Status}> ${how.missing || how.how || 'no release'}`}</dd>
      ${!sel && (how.candidates || []).length ? html`<dt>Fix versions</dt><dd><ul class="relcands">${how.candidates.map((c) => html`<li key=${c}>${c}</li>`)}</ul>
        <div class="muted small">Choose the one that counts: <code>/cg:branch release-pick ${k} "&lt;release&gt;"</code></div></dd>` : null}
      <dt>UAT</dt><dd><${UatFacts} uat=${plan.uat} compact /></dd>
    </dl>
    <${Blockers} plan=${plan} />
    ${names.length ? names.map((n) => html`<${RepoPlan} key=${n} name=${n} e=${plan.repos[n]} uatReq=${req} />`)
      : html`<p class="muted">No affected repository known yet: one counts once the ticket has a branch or PR in it.</p>`}
    <details class="more"><summary>Provenance and history</summary>
      <dl class="kv relkv"><dt>Chosen by</dt><dd>${how.how || '—'}</dd>
        ${sel && sel.name_date && sel.name_date.text ? html`<dt>Name date</dt><dd>The name says ${sel.name_date.text}: shown only, never used as the planned date.</dd>` : null}
        ${(plan.history || []).length ? html`<dt>Moved from</dt><dd>${plan.history.slice().reverse().map((h, i) => html`<div key=${i}>${(h.releases || []).map((x) => x.name || '?').join(', ')} on ${fmtDay(h.at, true)}
          ${Object.keys(h.branches || {}).length ? html` <span class="muted small">(was ${Object.keys(h.branches).map((n) => n + ': ' + h.branches[n]).join(', ')})</span>` : null}</div>`)}</dd>` : null}
        <dt>UAT evidence</dt><dd><${UatFacts} uat=${plan.uat} />
          ${hist.length ? html`<div class="small muted">For earlier changes (history, not the changes in UAT now): ${hist.map((h) => h.kind + ' ' + fmtDay(h.at, true)).join('; ')}</div>` : null}</dd></dl>
      <${Freshness} computed=${plan.computed_at} jira=${plan.jira} repos=${names.filter((n) => !plan.repos[n].candidate).map((n) => [n, plan.repos[n].freshness])} />
    </details>
  </section>`;
}
