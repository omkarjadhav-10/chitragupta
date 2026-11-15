// Companion: Daily Planner, task resume, plan versions, Jev Review, verification and the copy handoff. Reads come from
// the shared bounded projections (/api/companion, the same envelopes the CLI, MCP bridge and native mod read); every
// change is one protected action (/api/actions/companion) under an expected revision. Navigation, refresh and polling
// never generate, evaluate, acknowledge or start anything. Drafts, focus and scroll survive refresh; a 403 or a
// profile-generation change revokes everything shown; a 409 keeps the draft. #/companion?w=<w_…>&ticket=&flow=
import { useEffect, useRef, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { api, runAction, copyText, useStore, route, setParams, fmtTs, plural } from '../lib/core.js';
import { PageHead, Status, Tabs, TabPanel, Empty, Skeleton } from '../lib/ui.js';
import { Desk } from '../lib/shell.js';

const POLL_MS = 15000;
const SECTIONS = ['approach', 'components', 'database', 'risks', 'testing', 'verification'];
const STATUSES = ['Covered', 'Partial', 'Missing', 'Unverified'];
const STAGE = { new: 'New', planning: 'Planning', planned: 'Planned', review_incomplete: 'Review incomplete', plan_reviewed: 'Plan reviewed',
  approved: 'Approved', execution_ready: 'Execution ready', in_development: 'In development', paused: 'Paused',
  awaiting_verification: 'Testing', pr_review: 'PR review', completed: 'Completed', legacy: 'Earlier work' };
const ACTIVE = ['in_development', 'awaiting_verification', 'pr_review'];
const EDITABLE = ['planned', 'review_incomplete', 'plan_reviewed', 'approved'];
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const stageLabel = (s) => STAGE[s] || String(s || '').replace(/_/g, ' ');
function url(operation, workflow, target, cursor) {
  const q = new URLSearchParams({ operation });
  if (workflow) q.set('workflow', workflow);
  if (target) q.set('ticket', target);
  if (cursor) q.set('cursor', cursor);
  return '/api/companion?' + q.toString();
}
const pct = (m) => (!m || m.percent == null ? (m && m.state === 'unavailable' ? 'Unavailable' : 'N/A') : m.percent.toFixed(1) + '% (' + m.numerator + '/' + m.denominator + ')');

// ------------------------------------------------------------------ small pieces
function Measures({ m }) {
  if (!m) return null;
  const impl = m.implementation_progress;
  return html`<dl class="kv cmp-measures">
    <dt>Plan coverage (advisory)</dt><dd>${pct(m.plan_coverage)}${m.plan_coverage && m.plan_coverage.reason ? html` <span class="muted small">${m.plan_coverage.reason}</span>` : null}</dd>
    <dt>Implementation progress</dt><dd>${impl ? impl.numerator + ' of ' + impl.denominator + ' evidenced steps · ' + impl.pending + ' pending · ' + impl.uncertain + ' uncertain' : 'No plan'}</dd>
    <dt>Functional verification</dt><dd>${pct(m.functional_verification)}</dd></dl>`;
}
function Field({ id, label, children, help }) {
  return html`<div class="fld cmp-fld"><label class="lab" for=${id}>${label}</label>${children}${help ? html`<p class="muted small">${help}</p>` : null}</div>`;
}
function ChoiceButton({ c, onClick, busy }) {
  return html`<div class=${'cmp-choice' + (c.recommended ? ' rec' : '')}>
    <button type="button" class=${'btn' + (c.recommended ? ' primary' : '')} disabled=${busy || !c.available} onClick=${onClick}>${c.action}</button>
    <p class="small">${c.recommended ? html`<strong>Recommended. </strong>` : null}${c.available ? (c.why || '') : 'Unavailable: ' + c.reason}</p></div>`;
}

// ------------------------------------------------------------------ attention (FLOW-07): presentation only
// Acknowledge hides an item until its evidence changes; Snooze hides it for a bounded time. Required items (delivery,
// task overlays, job outcomes) stay in the full list, labelled. Neither ever changes delivery or test state.
function Attention({ att, act, busy }) {
  const [all, setAll] = useState(false);
  const items = att.items || [], initial = att.initial || 3;
  const shown = all ? items : items.slice(0, initial);
  const hidden = att.hidden || { total: 0, acknowledged: 0, snoozed: 0 };
  const body = (it, extra) => Object.assign({ item: it.id, generation: it.generation }, extra || {});
  return html`<section class="cmp-att" aria-labelledby="cmp-att-h">
    <h3 id="cmp-att-h">Attention: ${att.total} to review</h3>
    ${hidden.total ? html`<p class="small muted">${hidden.total} hidden (${hidden.acknowledged} acknowledged, ${hidden.snoozed} snoozed). Required items stay listed; delivery and tests are unchanged.</p>` : null}
    ${att.left_out ? html`<p class="small muted">${att.left_out} assigned to others left out</p>` : null}
    ${items.length ? html`<ul class="cmp-att-list">${shown.map((a) => {
      const st = (a.presentation || {}).state || 'active';
      return html`<li key=${a.id} class=${'cmp-att-item' + (st !== 'active' ? ' quiet' : '')}>
        <p><strong>${a.label}</strong> · ${a.ticket}${a.required ? html` <span class="muted small">(required)</span>` : null}</p>
        <p class="small">${a.text}</p>
        ${a.presentation && a.presentation.changed && st === 'active' ? html`<p class="small"><${Status} kind="warn">${a.presentation.label}</${Status}></p>` : null}
        <div class="btnline">${st === 'active' ? html`
          <button type="button" class="btn quiet" disabled=${busy} onClick=${() => act('attention_ack', body(a))}>Acknowledge</button>
          <select class="cmp-snooze" aria-label=${'Snooze ' + a.label + ' for ' + a.ticket} value="" disabled=${busy}
            onChange=${(e) => { const v = e.currentTarget.value; e.currentTarget.value = ''; if (v) act('attention_snooze', body(a, { duration: v })); }}>
            <option value="">Snooze…</option>${(att.snooze || []).map((s) => html`<option key=${s.id} value=${s.id}>${s.label}</option>`)}</select>`
          : html`<button type="button" class="btn quiet" disabled=${busy} onClick=${() => act('attention_unsnooze', body(a))}>Show again</button>`}</div></li>`; })}</ul>`
      : html`<p class="small muted">No recorded attention items.</p>`}
    ${items.length > initial ? html`<button type="button" class="btn quiet" onClick=${() => setAll(!all)}>${all ? 'Show fewer' : 'Show all (' + items.length + ')'}</button>` : null}
    <p class="muted small">${att.semantics}</p></section>`;
}

// ------------------------------------------------------------------ focus: next action, sources, attention
// A collected source that needs attention links to where it is recovered: Setup when it is off, Health otherwise.
function recovery(s) {
  if (!(s.id === 'git' || s.id === 'jira' || s.id.startsWith('host:')) || s.state === 'Connected') return null;
  const off = s.state === 'Not configured';
  return html` <a href=${off ? '#/setup' : '#/health'} aria-label=${(off ? 'Open Setup for ' : 'Open Health for ') + s.label}>${off ? 'Open Setup' : 'Open Health'}</a>`;
}
function Focus({ snap, onPick, selected, act, busy }) {
  if (!snap) return html`<${Skeleton} lines=${3} />`;
  const n = snap.next_action;
  // One primary action per context (UX-01): with a task open, its own panel holds the primary action.
  const lead = n && !(selected && n.workflow === selected);
  const failing = (snap.sources || []).filter((s) => s.state !== 'Connected' && s.state !== 'Unavailable');
  const att = snap.attention_summary || { total: 0, items: [] };
  return html`<section class="sec cmp-focus" aria-labelledby="cmp-focus-h">
    <div class="sec-head"><h2 id="cmp-focus-h">Focus</h2><p class="sub">${snap.context.state === 'ambiguous' ? 'Several tasks are plausible; choose one' :
      snap.context.state === 'evidenced' ? snap.context.evidence : snap.context.state === 'explicit' ? 'Your selection' : 'No task selected'}</p></div>
    ${snap.empty ? html`<p>${snap.empty}${n && n.target ? null : html` <a href=${snap.state === 'identity_unconfirmed' ? '#/profile?section=identity' : '#/setup'}>${snap.state === 'identity_unconfirmed' ? 'Open profile setup' : 'Open setup'}</a>`}</p>` : null}
    ${snap.reconcile ? html`<div class="cmp-reconcile"><p><${Status} kind="warn">Profile changed</${Status}> ${snap.reconcile.label}. <span class="small muted">${snap.reconcile.note}.</span></p>
      <button type="button" class="btn" disabled=${busy} onClick=${() => act('reconcile_profile', {})}>Reconcile ${plural(snap.reconcile.count, 'task')} with your profile</button></div>` : null}
    ${snap.focus ? html`<p class="cmp-cur"><span class="tkey">${snap.focus.ticket}</span> ${snap.focus.title}${snap.focus.repository ? html` <span class="muted small">· ${snap.focus.repository}</span>` : null}</p>` : null}
    ${n ? html`<div class="cmp-next"><p class="small muted">Next action</p>
      ${!lead ? (n.target ? html`<a class="btn" href=${n.target}>${n.label}</a>` : html`<p><strong>${n.label}</strong></p>`)
        : n.target ? html`<a class="btn primary" href=${n.target}>${n.label}</a>`
        : html`<button type="button" class="btn primary" onClick=${() => onPick(n)}>${n.label}</button>`}
      ${n.text ? html`<p class="small">${n.text}</p>` : null}</div>` : null}
    ${snap.context.state === 'ambiguous' ? html`<ul class="cmp-cands">${snap.context.candidates.map((c) => html`<li key=${c.workflow}>
      <button type="button" class="btn" onClick=${() => onPick({ ticket: c.ticket, workflow: c.workflow })}>${c.ticket} · ${stageLabel(c.stage)} · ${c.repository}</button>
      <span class="muted small"> ${c.evidence}</span></li>`)}</ul>` : null}
    ${snap.focus ? html`<ul class="cmp-facts">${snap.focus.facts.map((f, i) => html`<li key=${i}>${f}</li>`)}</ul>` : null}
    <details class="more"><summary>Sources: ${(snap.sources || []).length - failing.length} current, ${failing.length} need attention</summary>
      <ul class="cmp-sources">${(snap.sources || []).map((s) => html`<li key=${s.id}><strong>${s.label}</strong>: ${s.state}${s.success_at ? ' · last success ' + fmtTs(s.success_at) : ''}${s.recovery ? html`<span class="muted small"> · ${s.recovery}</span>` : null}${recovery(s)}</li>`)}</ul></details>
    <${Attention} att=${att} act=${act} busy=${busy} />
  </section>`;
}

// ------------------------------------------------------------------ Daily Planner
function Planner({ data, sel, onSelect, onMore, onGroup, group, filter, setFilter, busy }) {
  if (!data) return html`<${Skeleton} lines=${6} />`;
  const f = filter.trim().toLowerCase();
  const items = data.items.filter((c) => !f || (c.ticket + ' ' + (c.title || '')).toLowerCase().includes(f));
  const groups = [];
  items.forEach((c) => { let g = groups.find((x) => x.id === c.group); if (!g) { g = { id: c.group, label: c.group_label, items: [] }; groups.push(g); } g.items.push(c); });
  return html`<section class="sec cmp-planner" aria-labelledby="cmp-planner-h">
    <div class="sec-head"><h2 id="cmp-planner-h">Daily Planner</h2><p class="sub">${data.scope}</p></div>
    <div class="cmp-filters">
      <${Field} id="cmp-group" label="Stage"><select id="cmp-group" value=${group} onChange=${(e) => onGroup(e.currentTarget.value)}>
        <option value="all">All open stages</option>${data.groups.map((g) => html`<option key=${g.id} value=${g.id}>${g.label} (${g.count})</option>`)}</select></${Field}>
      <${Field} id="cmp-filter" label="Filter"><input id="cmp-filter" type="search" value=${filter} onInput=${(e) => setFilter(e.currentTarget.value)} placeholder="Ticket or title" /></${Field}></div>
    ${data.empty ? html`<${Empty} title=${data.empty} />` : null}
    ${groups.map((g) => html`<section key=${g.id} class="cmp-group" aria-labelledby=${'cmp-g-' + g.id}><h3 id=${'cmp-g-' + g.id}>${g.label} <span class="muted small">(${g.items.length})</span></h3>
      <ul class="cmp-cards">${g.items.map((c) => html`<li key=${c.ticket} class=${'cmp-card' + (sel.ticket === c.ticket ? ' on' : '')}>
        <button type="button" class="btn quiet cmp-pick" aria-current=${sel.ticket === c.ticket ? 'true' : null} onClick=${() => onSelect(c)}>
          <span class="tkey">${c.ticket}</span> <span class="cmp-title">${c.title || 'Untitled task'}</span></button>
        <p class="small">${c.action} · ${stageLabel(c.stage)} · Priority ${c.priority} · Jira ${c.jira_status}</p>
        <p class="small muted">Why: ${c.why}</p>
        <p class="small muted">${c.criteria.label}${c.blockers.length ? ' · Blocked: ' + c.blockers[0] : ''}</p>
        ${c.attempts.length ? html`<ul class="cmp-attempts" aria-label=${'Attempts of ' + c.ticket}>${c.attempts.map((a) => html`<li key=${a.workflow}>
          <button type="button" class="btn" onClick=${() => onSelect(c, a.workflow)}>Attempt in ${a.repository} · ${stageLabel(a.stage)}</button></li>`)}</ul>` : null}
        ${c.reconstruction ? html`<p class="small">${c.reconstruction}</p>` : null}</li>`)}</ul></section>`)}
    <div class="btnline">
      ${data.more ? html`<button type="button" class="btn" disabled=${busy} onClick=${onMore}>More tasks</button>` : null}
      ${group !== 'completed' && data.completed.count ? html`<button type="button" class="btn quiet" onClick=${() => onGroup('completed')}>Show completed (${data.completed.count})</button>` : null}</div>
  </section>`;
}

// ------------------------------------------------------------------ a ticket with tasks in several repositories
// Selecting its card shows its attempts to choose from (never the new-task form); starting another attempt is a
// separate, explicit choice.
function Chooser({ ctx, onPick, onNew }) {
  return html`<section class="sec" aria-labelledby="cmp-choose-h"><h2 id="cmp-choose-h">${ctx.ticket}: choose an attempt</h2>
    <p class="small">This ticket has a task in more than one repository. Choosing one only changes what you look at.</p>
    <ul class="cmp-cands">${ctx.candidates.map((c) => html`<li key=${c.workflow}>
      <button type="button" class="btn" onClick=${() => onPick(c)}>Attempt in ${c.repository} · ${stageLabel(c.stage)}</button>
      <span class="muted small"> ${c.evidence}</span></li>`)}</ul>
    <button type="button" class="btn quiet" onClick=${onNew}>Start an attempt in another repository</button></section>`;
}

// ------------------------------------------------------------------ new task: proposed baseline -> confirm
function NewTask({ ticket, act, busy, revoke, gen }) {
  const [prop, setProp] = useState(null), [err, setErr] = useState('');
  const [inst, setInst] = useState(0), [repo, setRepo] = useState(''), [keep, setKeep] = useState({}), [extra, setExtra] = useState('');
  useEffect(() => {
    let live = true;
    api(url('proposal', '', ticket)).then((d) => {
      if (!live) return;
      if (gen.current && d.profile_generation !== gen.current) { revoke(); return; }
      setProp(d); setRepo(d.repositories[0] || ''); setKeep({});
    }, (e) => { if (live) { if (e.status === 403) revoke(); setErr(e.message); } });
    return () => { live = false; };
  }, [ticket]);
  if (err) return html`<p role="alert">${err}</p>`;
  if (!prop) return html`<${Skeleton} lines=${4} />`;
  const p = prop.proposals[inst] || { available: false, reason: 'No confirmed Jira account' };
  const rows = p.available ? p.confirmable.criteria : [];
  const chosen = rows.filter((r) => keep[r.id] !== false);
  const confirm = () => {
    let next = Math.max(0, ...rows.map((r) => +(/^ac(\d+)$/.exec(r.id) || [0, 0])[1])) + 1;
    const manual = extra.split('\n').map((s) => s.trim()).filter(Boolean).map((text) => ({ id: 'ac' + next++, text, source: 'local clarification' }));
    const dropped = chosen.length < rows.length;
    act('create', { ticket, location: repo, jira_instance: p.instance || '', criteria: chosen.concat(manual),
      completeness: dropped || !p.available ? 'partial' : p.confirmable.completeness, coverage: p.available ? p.confirmable.coverage : undefined });
  };
  return html`<section class="sec" aria-labelledby="cmp-new-h"><h2 id="cmp-new-h">${ticket}: confirm the requirement baseline</h2>
    <p class="small">Proposed from the cached Jira card. Nothing is a requirement until you confirm it here; this never changes Jira.</p>
    ${prop.proposals.length > 1 ? html`<${Field} id="cmp-inst" label="Jira instance"><select id="cmp-inst" value=${inst} onChange=${(e) => setInst(+e.currentTarget.value)}>
      ${prop.proposals.map((x, i) => html`<option key=${i} value=${i}>${x.instance || 'Configured account'}</option>`)}</select></${Field}>` : null}
    ${!p.available ? html`<p><${Status} kind="warn">${p.reason}</${Status}></p>` : null}
    ${p.state ? html`<p><${Status} kind="warn">${p.state}</${Status}></p>` : null}
    ${p.truncated ? html`<p><${Status} kind="warn">The cached description is cut at 3,000 characters: criteria beyond it are missing.</${Status}></p>` : null}
    ${rows.length ? html`<fieldset class="cmp-crit"><legend>Proposed criteria (confirm the ones that are requirements)</legend>
      ${rows.map((r) => html`<label key=${r.id} class="cmp-check"><input type="checkbox" checked=${keep[r.id] !== false} onChange=${(e) => setKeep(Object.assign({}, keep, { [r.id]: e.currentTarget.checked }))} />
        <span><strong>${r.id}</strong> ${r.text} <span class="muted small">(${r.source})</span></span></label>`)}</fieldset>` : html`<p>Coverage unavailable: no confirmed criteria in the cached card.</p>`}
    ${p.available && p.gaps.length ? html`<details class="more" open><summary>Missing or incomplete (${p.gaps.length})</summary><ul>${p.gaps.map((g, i) => html`<li key=${i}>${g}</li>`)}</ul></details>` : null}
    ${p.available && p.proposed.length ? html`<details class="more"><summary>Jira comments (${p.proposed.length}): proposals only</summary><ul>${p.proposed.map((c) => html`<li key=${c.id}>${c.text} <span class="muted small">${c.label}</span></li>`)}</ul></details>` : null}
    <${Field} id="cmp-extra" label="Add local clarifications (one per line)" help="Kept as local criteria; Jira is not changed."><textarea id="cmp-extra" rows="3" value=${extra} onInput=${(e) => setExtra(e.currentTarget.value)} /></${Field}>
    <${Field} id="cmp-repo" label="Repository"><select id="cmp-repo" value=${repo} onChange=${(e) => setRepo(e.currentTarget.value)}>
      ${prop.repositories.map((r) => html`<option key=${r} value=${r}>${r}</option>`)}</select></${Field}>
    <button type="button" class="btn primary" disabled=${busy || !repo || (!chosen.length && !extra.trim())} onClick=${confirm}>Confirm baseline</button>
  </section>`;
}

// ------------------------------------------------------------------ plan versions, generation, editor
function PlanTab({ d, jobs, rev, act, busy, draft, setDraft, revoke, gen, say }) {
  const [view, setView] = useState(null);
  const caps = rev ? rev.capabilities : null;
  const active = jobs && jobs.active && jobs.active[0];
  const editable = EDITABLE.includes(d.stage) || d.stage === 'new' || (d.stage === 'paused' && d.approved_plan);
  async function open(pid) {
    try {
      let page = await api(url('diff', d.workflow, pid));
      if (page.profile_generation !== gen.current) { revoke(); return; }
      let text = page.text, out = page;
      for (let i = 0; page.cursor && i < 20; i++) { page = await api(url('plan', d.workflow, pid, page.cursor)); text += page.text; }
      setView(Object.assign({}, out, { text }));
    } catch (e) { if (e.status === 403) revoke(); say(e.message); }
  }
  async function edit(pid) {
    if (draft.dirty) { setDraft(Object.assign({}, draft, { open: true })); return; }
    try {
      const data = await api(url('editor', d.workflow, pid || ''));
      if (data.profile_generation !== gen.current) { revoke(); return; }
      const p = data.plan;
      setDraft({ open: true, dirty: false, revision: data.revision, baseline: data.baseline,
        fields: p ? Object.assign({ text: p.text }, p.sections) : Object.fromEntries(['text'].concat(SECTIONS).map((k) => [k, ''])), steps: p ? p.steps : null });
    } catch (e) { if (e.status === 403) revoke(); say(e.message); }
  }
  function field(k, v) { setDraft(Object.assign({}, draft, { dirty: true, fields: Object.assign({}, draft.fields, { [k]: v }) })); }
  async function save() {
    const ids = ((draft.baseline || {}).criteria || []).map((c) => c.id);
    const steps = draft.steps || [{ id: 'implementation', text: draft.fields.approach || 'Implement the plan', criteria: ids },
      { id: 'verification', text: draft.fields.verification || 'Verify the criteria', kind: 'verification', depends_on: ['implementation'], criteria: ids }];
    const ok = await act('save_plan', { plan: Object.assign({}, draft.fields, { steps }) }, draft.revision);
    if (ok) setDraft({ open: false, dirty: false });
  }
  const noPlan = !d.selected_plan;
  return html`<div class="cmp-plan">
    ${noPlan ? html`<section class="sec" aria-labelledby="cmp-gen-h"><h3 id="cmp-gen-h">Generate Plan</h3>
      <dl class="kv"><dt>Ticket</dt><dd>${d.ticket}</dd><dt>Repository</dt><dd>${d.repository}</dd>
        <dt>Requirement baseline</dt><dd>${plural(d.baseline.criteria_total, 'criterion', 'criteria')} · ${d.baseline.label} (${d.baseline.completeness})</dd>
        <dt>Context scope</dt><dd>Recorded ticket evidence for ${d.repository}: files, decisions, failed approaches, branches and release reminders; omissions are listed with the job</dd>
        <dt>Budget</dt><dd>${jobs && jobs.budget ? (jobs.budget.plan_call_ceiling != null ? 'Up to $' + jobs.budget.plan_call_ceiling + ' for this call · $' + jobs.budget.remaining + ' left today' : jobs.budget.plan_reason) : '…'}</dd></dl>
      ${d.baseline.gaps.length ? html`<p class="small"><${Status} kind="warn">Requirements incomplete</${Status}> ${d.baseline.gaps[0]}</p>` : null}
      <div class="btnline"><button type="button" class="btn primary" disabled=${busy || !!active || !(caps && caps.generate_plan.available)} onClick=${() => act('generate_plan', {})}>Generate Plan</button>
        <button type="button" class="btn" disabled=${busy} onClick=${() => edit('')}>Write a plan manually</button></div>
      ${caps && !caps.generate_plan.available ? html`<p class="small muted">Generate Plan unavailable: ${caps.generate_plan.reason}</p>` : null}
    </section>` : null}
    ${active ? html`<section class="sec cmp-job" aria-labelledby="cmp-job-h"><h3 id="cmp-job-h">Plan job: ${active.label}</h3>
      <p class="small">${cap(active.kind)} · attempt ${active.attempt} · updated ${fmtTs(active.updated_at)}. Checks local job status every 15 seconds while this page is visible; it never restarts the job.</p>
      <button type="button" class="btn" disabled=${busy} onClick=${() => act('cancel_job', { job: active.id })}>Cancel job</button></section>` : null}
    ${jobs && jobs.jobs.length && !active ? html`<p class="small muted">Last job: ${jobs.jobs[0].label}${jobs.jobs[0].reason ? ' — ' + jobs.jobs[0].reason : ''}</p>` : null}
    ${!noPlan ? html`<section class="sec" aria-labelledby="cmp-vers-h"><div class="sec-head"><h3 id="cmp-vers-h">Plan versions</h3><p class="sub">${d.plan_versions_total} saved; versions never change</p></div>
      <ul class="cmp-versions">${d.plan_versions.slice().reverse().map((v) => html`<li key=${v.id}>
        <span>Version ${v.number} · ${v.source === 'claude' ? 'Claude suggestion' : 'your edit'}${v.selected ? ' · selected' : ''}${v.approved ? ' · approved' : ''}${v.candidate === 'candidate' ? ' · candidate' : ''}${v.baseline_current ? '' : ' · older baseline'}</span>
        <span class="btnline"><button type="button" class="btn quiet" onClick=${() => open(v.id)}>View version ${v.number}</button>
          ${v.candidate === 'candidate' && EDITABLE.includes(d.stage) && v.baseline_current ? html`<button type="button" class="btn" disabled=${busy} onClick=${() => act('select_candidate', { plan_id: v.id })}>Select candidate</button>` :
            !v.selected && v.baseline_current && EDITABLE.includes(d.stage) ? html`<button type="button" class="btn quiet" disabled=${busy} onClick=${() => act('select_plan', { plan_id: v.id })}>Select for review</button>` : null}</span></li>`)}</ul>
      <div class="btnline"><button type="button" class="btn" disabled=${busy || !editable} onClick=${() => edit(d.selected_plan)}>Edit a new version</button>
        <button type="button" class="btn" disabled=${busy || !!active || !(caps && caps.improve_plan.available) || !EDITABLE.includes(d.stage)} onClick=${() => act('improve_plan', {})}>Improve Plan with Claude</button></div>
      ${caps && !caps.improve_plan.available ? html`<p class="small muted">Improve Plan with Claude unavailable: ${caps.improve_plan.reason}</p>` : null}
    </section>` : null}
    ${view ? html`<section class="sec" aria-labelledby="cmp-view-h"><div class="sec-head"><h3 id="cmp-view-h">Plan ${view.plan.slice(0, 14)}</h3><p class="sub">Digest ${view.digest.slice(0, 16)} · exact saved text</p></div>
      <pre class="cmp-pre" tabindex="0" aria-label="Plan text">${view.text}</pre>
      ${view.diff ? html`<details class="more"><summary>Changes from its parent version</summary><pre class="cmp-pre" tabindex="0" aria-label="Plan diff">${view.diff.text || 'No text changes'}</pre></details>` : null}
      <button type="button" class="btn quiet" onClick=${() => setView(null)}>Close plan</button></section>` : null}
    ${draft.open ? html`<section class="sec cmp-editor" aria-labelledby="cmp-ed-h"><h3 id="cmp-ed-h">Plan editor</h3>
      <p class="small">Saving creates a new immutable version with its parent; the selected and approved versions are never overwritten.</p>
      ${['text'].concat(SECTIONS).map((k) => html`<${Field} key=${k} id=${'cmp-ed-' + k} label=${k === 'text' ? 'Plan text' : cap(k)}>
        <textarea id=${'cmp-ed-' + k} rows=${k === 'text' ? 8 : 3} value=${(draft.fields || {})[k] || ''} onInput=${(e) => field(k, e.currentTarget.value)} /></${Field}>`)}
      <div class="btnline"><button type="button" class="btn primary" disabled=${busy} onClick=${save}>Save new plan version</button>
        <button type="button" class="btn quiet" onClick=${() => setDraft(Object.assign({}, draft, { open: false }))}>Hide editor (keeps your draft)</button>
        ${draft.dirty ? html`<span class="muted small">Not saved yet</span>` : null}</div></section>` : null}
  </div>`;
}

// ------------------------------------------------------------------ Jev Review: evaluation, assessment, Improve vs Execute
function ReviewTab({ rev, act, busy, goTab, ack, setAck, assess, setAssess }) {
  if (!rev) return html`<${Skeleton} lines=${5} />`;
  const ev = rev.evaluation, ex = rev.execute;
  const improve = rev.choices.find((c) => c.action === 'Improve Plan'), execute = rev.choices.find((c) => c.action === 'Execute Plan');
  if (!rev.selected_plan) return html`<${Empty} title="No plan to review yet">Generate or write a plan first.</${Empty}>`;
  const marks = assess.marks || {};
  return html`<div class="cmp-review">
    <section class="sec" aria-labelledby="cmp-ev-h"><h3 id="cmp-ev-h">Evaluation</h3>
      <p><${Status} kind=${ev.state === 'unavailable' ? 'off' : ev.partial ? 'warn' : 'info'}>${ev.label}${ev.percent != null ? ': ' + pct(ev) : ''}</${Status}>
        <span class="muted small"> ${ev.origin}${ev.reason ? ' · ' + ev.reason : ''}${ev.stale ? ' · requirements changed since' : ''}</span></p>
      ${ev.answers && ev.answers.length ? html`<ul class="small">${ev.answers.map((a) => html`<li key=${a.criterion}>${a.criterion}: ${a.status}${a.confidence != null ? ' · confidence ' + a.confidence : ''} <span class="muted">(${a.origin})</span></li>`)}</ul>` : null}
      <button type="button" class="btn quiet" disabled=${busy || !EDITABLE.includes(rev.stage)} onClick=${() => act('evaluate_plan', {})}>Request evaluation</button>
      ${rev.comparison ? html`<p class="small">Compared with the parent version: ${rev.comparison.direction}${rev.comparison.reasons && rev.comparison.reasons.length ? ' (' + rev.comparison.reasons.join('; ') + ')' : ''}. ${rev.comparison.advisory}</p>` : null}
    </section>
    <section class="sec" aria-labelledby="cmp-as-h"><h3 id="cmp-as-h">${rev.assessment_label}</h3>
      ${rev.assessment ? html`<p class="small">Recorded: ${pct(rev.assessment)} · ${fmtTs(rev.assessment.at)}</p>` : html`<p class="small muted">None recorded.</p>`}
      <details class="more"><summary>Classify the criteria yourself</summary>
        ${rev.criteria.filter((c) => !c.excluded).map((c) => html`<${Field} key=${c.id} id=${'cmp-as-' + c.id} label=${c.id + ': ' + c.text}>
          <select id=${'cmp-as-' + c.id} value=${marks[c.id] || ''} onChange=${(e) => setAssess(Object.assign({}, assess, { marks: Object.assign({}, marks, { [c.id]: e.currentTarget.value }) }))}>
            <option value="">Not classified</option>${STATUSES.map((s) => html`<option key=${s} value=${s}>${s}</option>`)}</select></${Field}>`)}
        <${Field} id="cmp-as-note" label="Note"><textarea id="cmp-as-note" rows="2" value=${assess.note || ''} onInput=${(e) => setAssess(Object.assign({}, assess, { note: e.currentTarget.value }))} /></${Field}>
        <button type="button" class="btn" disabled=${busy || !Object.values(marks).some(Boolean)} onClick=${() => act('manual_assessment', { plan_id: rev.selected_plan,
          classifications: Object.fromEntries(Object.entries(marks).filter((kv) => kv[1])), note: assess.note || '' })}>Record assessment</button></details>
    </section>
    <section class="sec cmp-choices" aria-labelledby="cmp-ch-h"><h3 id="cmp-ch-h">Improve or execute</h3>
      ${rev.gaps.length ? html`<p class="small">Open criteria: ${rev.gaps.map((g) => g.criterion + ' ' + g.status).join(', ')}</p>` : null}
      <dl class="kv"><dt>Exact version</dt><dd class="mono">${ex.plan}</dd><dt>Digest</dt><dd class="mono">${ex.digest}</dd>
        <dt>Requirement baseline</dt><dd>${ex.baseline.slice(0, 14)} (${ex.baseline_completeness})</dd>
        <dt>Target</dt><dd>${ex.target.repository} · <span class="mono">${ex.target.location}</span> · ${ex.target.session}</dd>
        <dt>Evaluation</dt><dd>${ex.findings.evaluation}${ex.findings.reason ? ' — ' + ex.findings.reason : ''}</dd>
        <dt>Unresolved risks</dt><dd>${ex.risks.length ? html`<ul>${ex.risks.map((r, i) => html`<li key=${i}>${r}</li>`)}</ul>` : 'None recorded'}</dd></dl>
      <label class="cmp-check"><input type="checkbox" checked=${ack} onChange=${(e) => setAck(e.currentTarget.checked)} disabled=${!execute.available} /><span>${ex.acknowledgement}</span></label>
      <div class="cmp-two">
        <${ChoiceButton} c=${improve} busy=${busy} onClick=${() => goTab('plan')} />
        <${ChoiceButton} c=${Object.assign({}, execute, { available: execute.available && ack, reason: execute.available ? 'Tick the acknowledgement above first' : execute.reason })} busy=${busy}
          onClick=${() => act('approve', { plan_id: ex.plan, context_digest: ex.context_digest, acknowledged_risks: true, session: 'reviewed-copy' }).then((r) => { if (r) goTab('handoff'); })} />
      </div>
      <p class="small muted">Execute Plan saves an approval of exactly this version and prepares a copy: Prepared; not submitted. No code runs from this page.</p>
    </section>
    ${rev.candidates.length ? html`<section class="sec" aria-labelledby="cmp-cand-h"><h3 id="cmp-cand-h">Candidates</h3>${rev.candidates.map((c) => html`<div key=${c.plan}>
      <p class="small">${c.plan.slice(0, 14)} · ${c.state}${c.selectable ? '' : ' · not selectable'}</p>
      ${c.diff ? html`<pre class="cmp-pre" tabindex="0" aria-label="Candidate diff">${c.diff.text}</pre>` : null}
      ${c.selectable ? html`<button type="button" class="btn" disabled=${busy} onClick=${() => act('select_candidate', { plan_id: c.plan })}>Select candidate</button>` : null}</div>`)}</section>` : null}
    ${rev.drafts.length ? html`<p class="small">${rev.drafts.map((x) => x.label + ': ' + x.reason).join('; ')}</p>` : null}
  </div>`;
}

// ------------------------------------------------------------------ handoff: copy, outcome, association
function HandoffTab({ d, ho, act, busy, say, revoke, gen, dr, form, setForm, dbKey }) {
  const [shown, setShown] = useState('');
  const drafts = html`<${DraftsSection} d=${d} dr=${dr} act=${act} busy=${busy} say=${say} revoke=${revoke} gen=${gen} form=${form} setForm=${setForm} dbKey=${dbKey} />`;
  if (!ho) return html`<div class="cmp-handoff"><${Skeleton} lines=${3} />${drafts}</div>`;
  async function copy() {
    try {
      let page = ho, text = ho.text || '';
      for (let i = 0; page.cursor && i < 20; i++) { page = await api(url('handoff', d.workflow, '', page.cursor)); if (page.profile_generation !== gen.current) { revoke(); return; } text += page.text; }
      await copyText(text); setShown(text);
      say('Approved plan copied. Prepared; not submitted. Paste it into a separate ordinary Claude turn.');
    } catch (e) { if (e.status === 403) revoke(); say(e.message); }
  }
  const unsettled = ho.dispatch === 'dispatching' || ho.dispatch === 'unknown' || (d.handoff && (d.handoff.dispatch === 'unknown' || d.handoff.dispatch === 'dispatching'));
  return html`<div class="cmp-handoff">
    ${ho.state === 'prepared_not_submitted' ? html`<section class="sec" aria-labelledby="cmp-ho-h"><h3 id="cmp-ho-h">Prepared; not submitted</h3>
      <dl class="kv"><dt>Attempt</dt><dd class="mono">${ho.attempt}</dd><dt>Approved version</dt><dd class="mono">${ho.plan}</dd><dt>Digest</dt><dd class="mono">${ho.plan_digest}</dd>
        <dt>Expires</dt><dd>${fmtTs(ho.expires_at)}</dd></dl>
      <div class="btnline"><button type="button" class="btn primary" onClick=${copy}>Copy approved plan</button></div>
      <p class="small">${ho.note}</p>
      ${shown ? html`<details class="more" open><summary>Copied text</summary><pre class="cmp-pre" tabindex="0" aria-label="Approved plan packet">${shown}</pre></details>` : null}
    </section>` : html`<section class="sec"><h3>No current approved handoff</h3><p class="small">${ho.reason}</p></section>`}
    ${ho.attempt ? html`<section class="sec" aria-labelledby="cmp-out-h"><h3 id="cmp-out-h">Record handoff outcome</h3>
      <p class="small">Chitragupta never submits or resends anything; record what you did.</p>
      <div class="btnline">${ho.state === 'prepared_not_submitted' ? html`<button type="button" class="btn" disabled=${busy} onClick=${() => act('record_dispatch', { attempt: ho.attempt, outcome: 'copied' })}>I copied it</button>` : null}
        <button type="button" class="btn quiet" disabled=${busy} onClick=${() => act('record_dispatch', { attempt: ho.attempt, outcome: 'rejected' })}>Not submitted</button>
        <button type="button" class="btn quiet" disabled=${busy} onClick=${() => act('record_dispatch', { attempt: ho.attempt, outcome: 'unknown' })}>Not sure</button>
        <button type="button" class="btn quiet" disabled=${busy} onClick=${() => act('record_dispatch', { attempt: ho.attempt, outcome: 'cancelled' })}>Cancel handoff</button></div>
      ${unsettled ? html`<p><${Status} kind="warn">Execution status unknown: associate its captured session or mark it not submitted. Nothing is resent.</${Status}></p>` : null}
    </section>` : null}
    ${['approved', 'execution_ready'].includes(d.stage) ? html`<section class="sec" aria-labelledby="cmp-as-s"><h3 id="cmp-as-s">Associate captured session</h3>
      <p class="small">After you submit the copy and its activity is captured, link that session. This does not start or complete work.</p>
      ${d.sessions.length ? html`<ul>${d.sessions.map((s) => html`<li key=${s.id}><button type="button" class="btn" disabled=${busy} onClick=${() => act('associate_execution', { session: s.id })}>Associate session ${s.id.slice(0, 8)}</button> <span class="muted small">${fmtTs(s.last_event)}</span></li>`)}</ul>` : html`<p class="small muted">No captured session for this repository yet.</p>`}
      ${d.stage === 'approved' ? html`<button type="button" class="btn quiet" disabled=${busy} onClick=${() => act('review_again', {})}>Review approval again</button>` : null}
    </section>` : null}
    ${drafts}
  </div>`;
}

// ------------------------------------------------------------------ drafts (FLOW-04): local, deterministic, never sent
// Generate draft saves a new generated version (no model); your edit stays current and the regenerated version is
// shown beside it as a diff. Save new version keeps your text as its own version (409 keeps the text). Copy re-reads
// the drafts view (access re-checked, tokens / emails / absolute paths removed) and copies; nothing is sent anywhere.
function DraftsSection({ d, dr, act, busy, say, revoke, gen, form, setForm, dbKey }) {
  const f = form || {};
  const aud = f.audience || 'pr';
  const ed = (f.editors || {})[aud] || { open: false };
  const [copied, setCopied] = useState('');
  if (!dr) return html`<${Skeleton} lines=${3} />`;
  const a = dr.audiences.find((x) => x.id === aud) || dr.audiences[0];
  const cur = a.current, shown = cur ? cur.text : (a.preview ? a.preview.text : '');
  const setEd = (x) => setForm(Object.assign({}, f, { editors: Object.assign({}, f.editors || {}, { [aud]: x }) }));
  function edit(text, base) {
    if (ed.dirty && !window.confirm('Replace the unsaved draft text in the editor?')) return;
    setEd({ open: true, dirty: false, text, base: base || null, revision: dr.revision });
  }
  async function editVersion(vid) {
    try {
      const v = await api(url('draft_version', d.workflow, vid));
      if (v.profile_generation !== gen.current) { revoke(); return; }
      edit(v.version.text, v.version.id);
    } catch (e) { if (e.status === 403) revoke(); say(e.message); }
  }
  async function save() {
    const ok = await act('draft_save', { audience: aud, text: ed.text, base: ed.base || undefined }, ed.revision);
    if (ok) setEd({ open: false, dirty: false });
  }
  async function copy() {
    try {
      const fresh = await api(url('drafts', d.workflow, dbKey));
      if (fresh.profile_generation !== gen.current) { revoke(); return; }
      const x = fresh.audiences.find((y) => y.id === aud);
      const text = x.current ? x.current.text : x.preview.text;
      await copyText(text); setCopied(text);
      say(a.label + ' draft copied. Nothing was sent; paste it where you choose.');
    } catch (e) { if (e.status === 403) revoke(); say(e.message); }
  }
  const genBody = Object.assign({ audience: aud }, dbKey ? { [dbKey.startsWith('f_') ? 'flow' : 'investigation']: dbKey } : {});
  return html`<section class="sec cmp-drafts" aria-labelledby="cmp-dr-h"><div class="sec-head"><h3 id="cmp-dr-h">Drafts</h3>
      <p class="sub">Local text for a PR reviewer, a QA tester or your next session. Nothing is sent.</p></div>
    <${Field} id="cmp-dr-aud" label="Audience"><select id="cmp-dr-aud" value=${aud} onChange=${(e) => setForm(Object.assign({}, f, { audience: e.currentTarget.value }))}>
      ${dr.audiences.map((x) => html`<option key=${x.id} value=${x.id}>${x.label}</option>`)}</select></${Field}>
    <p class="small">${cur ? 'Version ' + cur.number + ' · ' + cur.origin_label + ' · ' + fmtTs(cur.at) : a.preview.label}</p>
    ${cur && cur.stale ? html`<p><${Status} kind="warn">${cur.stale_label}</${Status}> <span class="small">Your text is kept. Generate draft to compare it with current evidence.</span></p>` : null}
    <div class="btnline">
      <button type="button" class="btn primary" disabled=${busy || d.archived} onClick=${() => act('draft_generate', genBody)}>Generate draft</button>
      <button type="button" class="btn" disabled=${busy || d.archived} onClick=${() => edit(shown, cur ? cur.id : null)}>Edit draft</button>
      <button type="button" class="btn" disabled=${!shown} onClick=${copy}>Copy draft</button></div>
    ${a.regenerated ? html`<details class="more" open><summary>${a.regenerated.label}</summary>
      <pre class="cmp-pre" tabindex="0" aria-label="Draft diff">${a.regenerated.diff.text || 'No text changes'}</pre>
      <button type="button" class="btn quiet" disabled=${busy} onClick=${() => editVersion(a.regenerated.id)}>Edit from version ${a.regenerated.number}</button></details>` : null}
    ${ed.open ? html`<div class="cmp-editor">
      <${Field} id="cmp-dr-text" label="Draft text" help="No email addresses, tokens or local absolute paths: drafts are written to be shared."><textarea id="cmp-dr-text" rows="14" value=${ed.text || ''} onInput=${(e) => setEd(Object.assign({}, ed, { text: e.currentTarget.value, dirty: true }))} /></${Field}>
      <div class="btnline"><button type="button" class="btn primary" disabled=${busy || !(ed.text || '').trim()} onClick=${save}>Save new version</button>
        <button type="button" class="btn quiet" onClick=${() => setEd(Object.assign({}, ed, { open: false }))}>Hide editor (keeps your text)</button>
        ${ed.dirty ? html`<span class="muted small">Not saved yet</span>` : null}</div></div>`
      : html`<pre class="cmp-pre" tabindex="0" aria-label="Draft text">${shown}</pre>`}
    ${a.versions.length ? html`<details class="more"><summary>Versions (${a.versions_total})</summary><ul class="cmp-versions">${a.versions.map((v) => html`<li key=${v.id}>
      <span>Version ${v.number} · ${v.origin_label}${v.current ? ' · current' : ''} · ${fmtTs(v.at)}</span>
      <button type="button" class="btn quiet" disabled=${busy} onClick=${() => editVersion(v.id)}>Edit from version ${v.number}</button></li>`)}</ul></details>` : null}
    ${copied ? html`<p class="small muted">Copied ${copied.length} characters; nothing was sent.</p>` : null}
    <p class="small muted">${dr.note}</p></section>`;
}

// ------------------------------------------------------------------ verification ledger
function VerifyTab({ d, ver, act, busy, more, form, setForm }) {
  if (!ver) return html`<${Skeleton} lines=${4} />`;
  const can = ACTIVE.includes(d.stage) || ['paused', 'completed'].includes(d.stage);
  const f = form || {};
  return html`<div class="cmp-verify">
    <section class="sec" aria-labelledby="cmp-vm-h"><h3 id="cmp-vm-h">Three separate measures</h3><${Measures} m=${ver.measures} />
      <ul class="small muted">${Object.entries(ver.definitions).map((kv) => html`<li key=${kv[0]}>${kv[1]}</li>`)}</ul>
      <p class="small">${ver.note}${ver.recorded ? ' Last recorded ' + fmtTs(ver.recorded.at) + '.' : ''}</p>
      <button type="button" class="btn" disabled=${busy || !can} onClick=${() => act('verify', {})}>Verify Requirements</button>
      ${!can ? html`<span class="muted small"> Available after development starts</span>` : null}</section>
    <section class="sec" aria-labelledby="cmp-vl-h"><h3 id="cmp-vl-h">Requirement ledger</h3>
      <ul class="cmp-ledger">${ver.rows.map((r) => html`<li key=${r.criterion}><p><strong>${r.criterion}</strong> · <${Status} kind=${r.status === 'Verified' ? 'ok' : r.failing ? 'bad' : 'warn'}>${r.status}</${Status}> ${r.text}</p>
        <p class="small">${r.applicability}${r.plan_steps.length ? ' · steps ' + r.plan_steps.join(', ') : ''}</p>
        ${r.evidence_total ? html`<details class="more"><summary>${plural(r.evidence_total, 'source event')}</summary><ul class="small cmp-plain">${r.evidence.map((e) => html`<li key=${e.id}>
          ${cap(e.kind || 'evidence')} · ${e.outcome || 'no outcome'} · ${e.basis} · ${e.revision ? 'revision ' + String(e.revision).slice(0, 12) : 'revision not captured'} · ${e.observed_at ? fmtTs(e.observed_at) : 'time unknown'}
          · <strong>${e.applicability === 'current' ? 'current' : e.applicability === 'historical' ? 'historical' : 'applicability unknown'}</strong>${e.reason ? ' (' + e.reason + ')' : ''}</li>`)}
          ${r.evidence_total > r.evidence.length ? html`<li>${r.evidence_total - r.evidence.length} more not shown</li>` : null}</ul></details>` : null}</li>`)}</ul>
      ${ver.more ? html`<button type="button" class="btn" onClick=${more}>More criteria</button>` : null}</section>
    ${can ? html`<details class="more"><summary>Record an acceptance observation (self-reported)</summary>
      <${Field} id="cmp-ob-c" label="Criterion"><select id="cmp-ob-c" value=${f.criterion || ''} onChange=${(e) => setForm(Object.assign({}, f, { criterion: e.currentTarget.value }))}>
        <option value="">Choose</option>${ver.rows.map((r) => html`<option key=${r.criterion} value=${r.criterion}>${r.criterion}</option>`)}</select></${Field}>
      <${Field} id="cmp-ob-o" label="What you observed"><select id="cmp-ob-o" value=${f.outcome || 'passed'} onChange=${(e) => setForm(Object.assign({}, f, { outcome: e.currentTarget.value }))}><option value="passed">Passed</option><option value="failed">Failed</option></select></${Field}>
      <${Field} id="cmp-ob-r" label="Revision (commit ID)"><input id="cmp-ob-r" value=${f.revision || ''} onInput=${(e) => setForm(Object.assign({}, f, { revision: e.currentTarget.value }))} /></${Field}>
      <${Field} id="cmp-ob-t" label="Observation"><textarea id="cmp-ob-t" rows="2" value=${f.text || ''} onInput=${(e) => setForm(Object.assign({}, f, { text: e.currentTarget.value }))} /></${Field}>
      <${Field} id="cmp-ob-l" label="Limits"><input id="cmp-ob-l" value=${f.limits || ''} onInput=${(e) => setForm(Object.assign({}, f, { limits: e.currentTarget.value }))} /></${Field}>
      <button type="button" class="btn" disabled=${busy || !f.criterion} onClick=${() => act('record_evidence', { mapping: { criteria: [f.criterion], coverage: 'complete',
        manual: { kind: 'acceptance', outcome: f.outcome || 'passed', revision: f.revision || '', text: f.text || '', limits: f.limits || '' } } })}>Record observation</button></details>` : null}
  </div>`;
}

// ------------------------------------------------------------------ decisions (FLOW-05) and impact (FLOW-06)
function Decisions({ k }) {
  if (!k || !(k.current_total || k.superseded_total || k.rejected.length || k.conflicts.length || k.needs_review.length)) return null;
  return html`<section class="sec" aria-labelledby="cmp-dc-h"><div class="sec-head"><h3 id="cmp-dc-h">Decisions</h3><p class="sub">From the ticket's recorded knowledge</p></div>
    ${k.relevant.length ? html`<ul class="cmp-plain">${k.relevant.map((r) => html`<li key=${r.id}><strong>${r.status_label}</strong>: ${r.title}${r.date ? html` <span class="muted small">${r.date}</span>` : null}
      ${r.supersedes.length ? html`<span class="small"> · supersedes ${r.supersedes.map((s) => '"' + s.title + '"').join(', ')}</span>` : null}</li>`)}</ul>` : null}
    ${k.conflicts.map((c) => html`<p key=${c.subject} class="small"><${Status} kind="warn">${c.label}</${Status}> ${c.subject}: ${c.items.map((i) => '"' + i.title + '"').join(' vs ')}</p>`)}
    ${k.needs_review.map((r) => html`<p key=${r.id} class="small"><${Status} kind="warn">Needs review</${Status}> ${r.title}</p>`)}
    ${k.rejected_here.map((r) => html`<p key=${r.id} class="small">Rejected approach here: ${r.title} <span class="muted">(${r.applies.label})</span></p>`)}
    ${k.rejected_elsewhere ? html`<p class="small muted">${plural(k.rejected_elsewhere, 'failed approach', 'failed approaches')} recorded in another repository, environment or an unknown context: context only, never a prohibition here.</p>` : null}
    <p class="small muted">${k.superseded_total ? plural(k.superseded_total, 'earlier decision') + ' kept as history. ' : ''}<a href=${k.link}>Open the ticket</a></p></section>`;
}

function Impact({ im }) {
  if (!im) return null;
  const kind = { explicit: 'info', historical: 'off', inferred: 'warn' };
  return html`<section class="sec" aria-labelledby="cmp-im-h"><div class="sec-head"><h3 id="cmp-im-h">Impact</h3><p class="sub">${im.label}</p></div>
    ${im.note ? html`<p class="small">${im.note}</p>` : null}
    ${im.items.length ? html`<ul class="cmp-impact">${im.items.map((s) => html`<li key=${s.id}>
      <p><strong>${s.item}</strong> <${Status} kind=${kind[s.confidence] || 'off'}>${s.confidence_label}</${Status}>${s.stale ? html` <${Status} kind="warn">Stale cache</${Status}>` : null}</p>
      <p class="small">${s.why}</p>
      <p class="small">Proposed check (text only; nothing runs from here): ${s.validation.text}</p>
      <ul class="small muted cmp-plain">${s.links.map((l) => html`<li key=${l.ref}>${l.text} <span class="mono">${l.ref}</span></li>`)}</ul></li>`)}</ul>` : null}
    ${im.more ? html`<p class="small muted">${im.total - im.items.length} more suggestions are not shown here.</p>` : null}
    ${im.unlinked.length ? html`<p class="small">Not linked yet: ${im.unlinked.join('; ')}</p>` : null}
    <details class="more"><summary>Coverage limits (${im.coverage})</summary><ul class="small">${im.limits.map((l, i) => html`<li key=${i}>${l}</li>`)}</ul></details></section>`;
}

// ------------------------------------------------------------------ resume view by situation (A-E)
function ResumeTab({ d, changes, moreChanges, act, busy, goTab, form, setForm, db }) {
  const f = form || {};
  const set = (k, v) => setForm(Object.assign({}, f, { [k]: v }));
  const names = d.actions.map((a) => a.name);
  const blocking = d.recovery.filter((r) => r.blocking);
  const cont = d.continuation;
  const resumable = names.includes('Resume Execution');
  const primary = blocking.length ? null : d.situation === 'A' ? html`<button type="button" class="btn primary" onClick=${() => goTab('review')}>Review saved plan</button>`
    : d.situation === 'new' ? html`<button type="button" class="btn primary" onClick=${() => goTab('plan')}>Generate Plan</button>`
    : d.situation === 'handoff' ? html`<button type="button" class="btn primary" onClick=${() => goTab('handoff')}>Copy approved plan</button>`
    : d.situation === 'C' ? html`<button type="button" class="btn primary" onClick=${() => goTab('verify')}>Verify Requirements</button>`
    : d.situation === 'D' ? html`<a class="btn primary" href=${d.links.delivery}>Open PR Review</a>`
    : d.situation === 'completed' ? html`<button type="button" class="btn primary" onClick=${() => goTab('verify')}>View completion evidence</button>` : null;
  return html`<div class="cmp-resume">
    <section class="sec" aria-labelledby="cmp-na-h"><h3 id="cmp-na-h">Next action: ${d.next_action.label}</h3><p class="small">${d.next_action.text}</p>
      ${blocking.map((r) => html`<p key=${r.state}><${Status} kind="warn">${r.label}</${Status}> ${r.reason} <span class="small">${r.action}</span></p>`)}
      ${primary}</section>
    <section class="sec" aria-labelledby="cmp-st-h"><h3 id="cmp-st-h">Where it stands</h3>
      <dl class="kv"><dt>Approved plan</dt><dd>${d.approved_label}</dd><dt>Last session</dt><dd>${d.last_session || 'None recorded'}</dd>
        <dt>Last checkpoint</dt><dd>${fmtTs(d.last_checkpoint)} (revision ${d.revision})</dd>
        <dt>Working tree</dt><dd>${d.context.reason}${d.context.head ? ' · HEAD ' + d.context.head.slice(0, 12) : ''}${d.context.branch ? ' · ' + d.context.branch : ''}</dd>
        <dt>Steps</dt><dd>${(d.progress.completed || []).length} completed · ${(d.progress.pending || []).length} pending · ${(d.progress.uncertain || []).length} uncertain${(d.progress.revalidate || []).length ? ' · ' + d.progress.revalidate.length + ' to revalidate' : ''}</dd>
        ${(d.ci || []).length ? html`<dt>CI checks</dt><dd>${d.ci.map((c) => html`<span class="cmp-ci" key=${c.pr}>${c.text}</span>`)}</dd>` : null}</dl>
      <${Measures} m=${d.measures} />
      ${d.rollup && d.rollup.total > 1 ? html`<p class="small">${d.rollup.label}</p>` : null}</section>
    <section class="sec" aria-labelledby="cmp-cg-h"><div class="sec-head"><h3 id="cmp-cg-h">Since your last review</h3><p class="sub">${changes ? changes.label : ''}</p></div>
      ${changes && changes.items.length ? html`<ul class="cmp-changes">${changes.items.map((c) => html`<li key=${c.id}>${cap(c.change)} ${c.kind}: ${c.label} <span class="muted small">${c.provenance} · ${c.observed_at ? fmtTs(c.observed_at) : 'time unknown'}${c.state !== 'available' ? ' · ' + c.state : ''}</span></li>`)}</ul>` : null}
      <div class="btnline">${changes && changes.more ? html`<button type="button" class="btn quiet" onClick=${moreChanges}>More changes</button>` : null}
        ${changes && changes.unread.length ? html`<button type="button" class="btn" disabled=${busy} onClick=${() => act('acknowledge', { events: changes.unread })}>Mark changes reviewed</button>` : null}</div></section>
    ${cont ? html`<section class="sec" aria-labelledby="cmp-ct-h"><h3 id="cmp-ct-h">Continuation</h3>
      <dl class="kv">${cont.carry_forward && cont.carry_forward.length ? html`<dt>Carry forward</dt><dd>${cont.carry_forward.map((s) => s.step).join(', ')}</dd>` : null}
        ${cont.remaining && cont.remaining.length ? html`<dt>Remaining</dt><dd>${cont.remaining.join(', ')}</dd>` : null}
        ${cont.revalidate && cont.revalidate.length ? html`<dt>Revalidate</dt><dd>${cont.revalidate.join(', ')}</dd>` : null}
        ${cont.criteria && cont.criteria.length ? html`<dt>Unverified criteria</dt><dd>${cont.criteria.join(', ')}</dd>` : null}
        ${cont.scope ? html`<dt>Scope</dt><dd>${cont.scope}</dd>` : null}</dl>
      ${resumable ? html`<label class="cmp-check"><input type="checkbox" checked=${!!f.ack} onChange=${(e) => set('ack', e.currentTarget.checked)} /><span>I reviewed the continuation, repository differences and unresolved risks.</span></label>
        <button type="button" class="btn" disabled=${busy || !f.ack} onClick=${() => act('resume_execution', { candidate: d.resume_id, acknowledged_risks: true, session: 'reviewed-copy' })}>Resume Execution</button>
        <p class="small muted">Prepares a continuation copy (Prepared; not submitted); nothing runs from this page.</p>` : null}</section>` : null}
    <${Decisions} k=${d.knowledge} />
    <${Impact} im=${d.impact} />
    <section class="sec" aria-labelledby="cmp-ac-h"><h3 id="cmp-ac-h">Task actions</h3>
      ${names.includes('Pause') ? html`<${Field} id="cmp-pause" label="Where you stopped (optional)"><input id="cmp-pause" value=${f.summary || ''} onInput=${(e) => set('summary', e.currentTarget.value)} /></${Field}>
        <button type="button" class="btn" disabled=${busy} onClick=${() => act('pause', { summary: f.summary || d.summary || 'Paused' })}>Pause task (capture keeps running)</button>` : null}
      ${d.stage === 'in_development' ? html`<${Field} id="cmp-checks" label="Remaining checks, one per line"><textarea id="cmp-checks" rows="2" value=${f.checks || ''} onInput=${(e) => set('checks', e.currentTarget.value)} /></${Field}>
        <button type="button" class="btn" disabled=${busy || !(f.checks || '').trim()} onClick=${() => act('request_verification', { checks: f.checks.split('\n').map((s) => s.trim()).filter(Boolean) })}>Request verification</button>` : null}
      ${names.includes('Complete') ? html`<details class="more"><summary>Complete (gated by current evidence)</summary>
        <${Field} id="cmp-done" label="Completion decision"><textarea id="cmp-done" rows="2" value=${f.decision || ''} onInput=${(e) => set('decision', e.currentTarget.value)} /></${Field}>
        <${Field} id="cmp-nopr" label="Why no PR is required (only if none is)"><input id="cmp-nopr" value=${f.nopr || ''} onInput=${(e) => set('nopr', e.currentTarget.value)} /></${Field}>
        <button type="button" class="btn" disabled=${busy || !(f.decision || '').trim()} onClick=${() => act('complete', { decision: f.decision, pr_not_required: f.nopr || null })}>Complete task</button></details>` : null}
      ${names.includes('Reopen') ? html`<details class="more"><summary>Reopen</summary>
        <${Field} id="cmp-ro-t" label="Reopen to"><select id="cmp-ro-t" value=${f.target || 'awaiting_verification'} onChange=${(e) => set('target', e.currentTarget.value)}>
          <option value="awaiting_verification">Testing</option><option value="pr_review">PR review</option><option value="paused">Development needed (paused)</option></select></${Field}>
        <${Field} id="cmp-ro-e" label="Why it reopens"><textarea id="cmp-ro-e" rows="2" value=${f.explanation || ''} onInput=${(e) => set('explanation', e.currentTarget.value)} /></${Field}>
        <button type="button" class="btn" disabled=${busy || !(f.explanation || '').trim()} onClick=${() => act('reopen', { target: f.target || 'awaiting_verification', explanation: f.explanation })}>Reopen task</button></details>` : null}
      <details class="more"><summary>Record current context</summary>
        <p class="small muted">What you see in your working tree; Chitragupta inspects nothing.</p>
        <${Field} id="cmp-oc-h" label="HEAD commit"><input id="cmp-oc-h" value=${f.head || ''} onInput=${(e) => set('head', e.currentTarget.value)} /></${Field}>
        <${Field} id="cmp-oc-b" label="Branch"><input id="cmp-oc-b" value=${f.branch || ''} onInput=${(e) => set('branch', e.currentTarget.value)} /></${Field}>
        <${Field} id="cmp-oc-w" label="Working tree"><select id="cmp-oc-w" value=${f.worktree || 'unknown'} onChange=${(e) => set('worktree', e.currentTarget.value)}><option value="unknown">Unknown</option><option value="clean">Clean</option><option value="dirty">Dirty</option></select></${Field}>
        <button type="button" class="btn" disabled=${busy} onClick=${() => act('observe_context', { observation: Object.assign({ worktree: f.worktree || 'unknown' }, f.head ? { head: f.head } : {}, f.branch ? { branch: f.branch } : {}) })}>Record current context</button></details>
      ${names.includes('Cancel') ? html`<details class="more"><summary>Cancel current operation</summary><p class="small">Cancels a pending job or handoff and keeps every version; nothing is rolled back.</p>
        <button type="button" class="btn quiet" disabled=${busy} onClick=${() => act('cancel', { reason: 'cancelled from the dashboard' })}>Cancel operation</button></details>` : null}
    </section>
    <section class="sec" aria-labelledby="cmp-dl-h"><h3 id="cmp-dl-h">Delivery and database</h3>
      <p class="small">${d.delivery.release ? 'Release: ' + d.delivery.release + '. ' : ''}${d.delivery.guidance}. <a href=${d.links.delivery}>Open delivery details</a></p>
      ${d.delivery.reminders.length ? html`<ul class="small">${d.delivery.reminders.map((r, i) => html`<li key=${i}><strong>${r.label}</strong>: ${r.text}</li>`)}</ul>` : null}
      ${db && db.available ? html`<p class="small">${db.operation_label} · ${db.environment} · last evidenced: ${db.last_evidenced_step || 'none'} · gap: ${db.largest_gap || 'none'}</p>
        <p class="small"><a href=${db.links.flow || '#/db'}>Open the flow</a>${db.links.evidence ? html` · <a href=${db.links.evidence}>Evidence</a> · <a href=${db.links.journey}>Record journey</a>` : null}</p>`
        : html`<p class="small muted">${db ? db.reason : 'No database flow or investigation selected'}. <a href="#/db">Open the database workspace</a></p>`}</section>
  </div>`;
}

// ------------------------------------------------------------------ the page
export function CompanionPage() {
  const r = useStore(route);
  const [snap, setSnap] = useState(null), [plan, setPlan] = useState(null), [group, setGroup] = useState('all'), [filter, setFilter] = useState('');
  const [d, setD] = useState(null), [rev, setRev] = useState(null), [jobs, setJobs] = useState(null), [changes, setChanges] = useState(null);
  const [ver, setVer] = useState(null), [ho, setHo] = useState(null), [db, setDb] = useState(null), [dr, setDr] = useState(null);
  const [tab, setTab] = useState('resume'), [busy, setBusy] = useState(false), [msg, setMsg] = useState(''), [ack, setAck] = useState(false);
  const [drafts, setDrafts] = useState({}), [forms, setForms] = useState({}), [another, setAnother] = useState({});
  const gen = useRef(null), token = useRef(0), selRef = useRef({ ticket: '', workflow: '' });
  const planRef = useRef(null), groupRef = useRef('all'), dbRef = useRef({});
  const sel = { ticket: r.params.ticket || '', workflow: r.params.w || '' };
  selRef.current = sel;
  planRef.current = plan;
  groupRef.current = group;
  dbRef.current = { flow: r.params.flow || '', investigation: r.params.investigation || '' };
  const wid = sel.workflow;
  const draft = drafts[wid] || { open: false };
  const setDraft = (x) => setDrafts((all) => Object.assign({}, all, { [wid]: x }));
  const form = (k) => (forms[wid] || {})[k];
  const setForm = (k) => (v) => setForms((all) => Object.assign({}, all, { [wid]: Object.assign({}, all[wid] || {}, { [k]: v }) }));
  const say = (t) => setMsg(t);

  function revoke(text) {
    token.current += 1; gen.current = null;
    setSnap(null); setPlan(null); setD(null); setRev(null); setJobs(null); setChanges(null); setVer(null); setHo(null); setDb(null); setDr(null);
    setDrafts({}); setForms({}); setAck(false);
    setParams({ w: '', ticket: '' });
    setMsg(text || 'Access changed. Your view was cleared; select your task again.');
  }
  const fresh = (data) => {
    if (gen.current && data.profile_generation && data.profile_generation !== gen.current) { revoke('Profile changed. Your view was cleared; select your task again.'); return false; }
    if (data.profile_generation) gen.current = data.profile_generation;
    return true;
  };
  async function get(path) { const data = await api(path); if (!fresh(data)) throw Object.assign(new Error('revoked'), { revoked: true }); return data; }
  async function loadTask(w, n) {
    const dbSel = dbRef.current;
    const ws = await Promise.all([get(url('detail', w)), get(url('review', w)), get(url('jobs', w)), get(url('changes', w)),
      get(url('verification', w)), get(url('handoff', w)), get(url('drafts', w, dbSel.flow || dbSel.investigation))]);
    if (n !== token.current || selRef.current.workflow !== w) return;
    setD(ws[0]); setRev(ws[1]); setJobs(ws[2]); setChanges(ws[3]); setVer(ws[4]); setHo(ws[5]); setDr(ws[6]);
  }
  async function load(quiet) {
    const n = token.current, s = selRef.current;
    try {
      const sn = await get(url('snapshot', s.workflow, s.workflow ? '' : s.ticket));
      if (n !== token.current) return;
      setSnap(sn);
      const c = sn.context || {};
      if (!s.workflow && s.ticket && c.state === 'explicit' && c.ticket === s.ticket && c.workflow) {
        setParams({ w: c.workflow, ticket: '' });  // the ticket has exactly one task: open it, never a second attempt
        return;
      }
      const shown = planRef.current, g = groupRef.current;
      if (!shown || !shown.pages || shown.pages === 1) {  // a paged-in planner is kept until an explicit reload
        const pl = await get(url('planner', '', g === 'all' ? '' : g));
        if (n !== token.current) return;
        setPlan(Object.assign({}, pl, { pages: 1 }));
      }
      if (s.workflow) await loadTask(s.workflow, n);
      const dbSel = dbRef.current;
      if (dbSel.flow || dbSel.investigation) {
        const x = await get(url('database', '', dbSel.flow || dbSel.investigation));
        if (n === token.current) setDb(x.database_context);
      }
    } catch (e) {
      if (e.revoked || n !== token.current) return;
      if (e.status === 403) { revoke(); return; }
      if (!quiet) say(e.message);
    }
  }
  useEffect(() => { document.title = 'Companion · Chitragupta'; }, []);
  useEffect(() => { token.current += 1; load(false); }, [wid, sel.ticket, group]);
  useEffect(() => {
    const t = setInterval(() => { if (document.visibilityState === 'visible') load(true); }, POLL_MS);
    return () => { clearInterval(t); token.current += 1; };
  }, [wid, sel.ticket, group]);

  function select(card, attempt) {
    const w = attempt || card.workflow || '';
    if (drafts[wid] && drafts[wid].dirty && w !== wid && !window.confirm('Keep the unsaved plan draft for later and switch tasks? It stays until you reload the page.')) return;
    setD(null); setRev(null); setJobs(null); setChanges(null); setVer(null); setHo(null); setDr(null); setAck(false); setMsg('');
    setTab('resume');
    setParams({ w, ticket: w ? '' : card.ticket });
  }
  async function act(action, extra, revision) {
    if (busy) return null;
    const n = token.current;
    setBusy(true); setMsg('');
    try {
      const body = Object.assign({ action, workflow: wid || undefined, revision: revision != null ? revision : d ? d.revision : undefined }, extra || {});
      const res = await runAction('companion', body);
      if (n !== token.current) return null;
      if (res.workflow && res.workflow !== wid) setParams({ w: res.workflow, ticket: '' });
      else await load(true);
      setAck(false);
      say(res.notice || (res.coalesced ? 'Already running: showing the active job.' : res.job ? 'Queued: ' + res.job.label + '. It runs in the background.' : 'Saved.'));
      return res;
    } catch (e) {
      if (n !== token.current) return null;
      if (e.status === 403) { revoke(); return null; }
      say(e.message + (e.status === 409 ? ' Your entered text is kept; review the current state before retrying.' : ''));
      if (e.status === 409) load(true);
      return null;
    } finally { setBusy(false); }
  }
  async function more() {
    try {
      const pl = await get(url('planner', '', groupRef.current === 'all' ? '' : groupRef.current, plan.cursor));
      setPlan(Object.assign({}, pl, { items: plan.items.concat(pl.items), pages: (plan.pages || 1) + 1 }));
    } catch (e) { if (e.status === 403) revoke(); else say(e.message); }
  }
  async function moreChanges() {
    try { const c = await get(url('changes', wid, '', changes.cursor)); setChanges(Object.assign({}, c, { items: changes.items.concat(c.items), unread: changes.unread.concat(c.unread) })); }
    catch (e) { if (e.status === 403) revoke(); else say(e.message); }
  }
  async function moreLedger() {
    try { const v = await get(url('verification', wid, '', ver.cursor)); setVer(Object.assign({}, v, { rows: ver.rows.concat(v.rows) })); }
    catch (e) { if (e.status === 403) revoke(); else say(e.message); }
  }
  const pick = (n) => { if (n.workflow || n.ticket) select({ ticket: n.ticket, workflow: n.workflow }); };
  const tabs = [{ id: 'resume', label: 'Resume' }, { id: 'plan', label: 'Plan' }, { id: 'review', label: 'Jev Review' },
    { id: 'verify', label: 'Verification' }, { id: 'handoff', label: 'Handoff' }];
  const taskTitle = d ? d.ticket + ' · ' + d.title : '';
  // the new-task form waits for the snapshot of this ticket: a ticket with several attempts gets the chooser instead
  const ctx = snap && snap.context;
  const ctxReady = !!(ctx && (ctx.ticket === sel.ticket || (ctx.state === 'absent' && /unavailable/.test(ctx.reason || ''))));
  const choose = !wid && !!sel.ticket && ctxReady && ctx.state === 'ambiguous' && !another[sel.ticket];
  return html`<${Desk} className="companion-desk">
    <${PageHead} title="Companion" sub="Plan deliberately. Resume from evidence. You stay in control of execution.">
      <button type="button" class="btn" disabled=${busy} onClick=${() => load(false)}>Reload cached view</button></${PageHead}>
    <p class="cmp-status" role="status" aria-live="polite">${msg}</p>
    <div class="cmp-layout">
      <div class="cmp-side">
        <${Focus} snap=${snap} onPick=${pick} selected=${wid} act=${act} busy=${busy} />
        <${Planner} data=${plan} sel=${sel} onSelect=${select} onMore=${more} busy=${busy}
          onGroup=${(g) => { setPlan(null); setGroup(g); }} group=${group} filter=${filter} setFilter=${setFilter} />
      </div>
      <div class="cmp-main">
        ${choose ? html`<${Chooser} ctx=${snap.context} onPick=${(c) => select({ ticket: c.ticket }, c.workflow)}
          onNew=${() => setAnother(Object.assign({}, another, { [sel.ticket]: true }))} />` : null}
        ${!wid && sel.ticket && !choose && ctxReady && !ctx.workflow ? html`<${NewTask} key=${sel.ticket} ticket=${sel.ticket} act=${act} busy=${busy} revoke=${revoke} gen=${gen} />` : null}
        ${!wid && sel.ticket && (!ctxReady || (ctx.workflow && !choose)) ? html`<${Skeleton} lines=${4} />` : null}
        ${!wid && !sel.ticket ? html`<${Empty} title="Select a task from the Daily Planner">Selecting only changes what you look at; nothing is generated or started.</${Empty}>` : null}
        ${wid && !d ? html`<${Skeleton} lines=${6} />` : null}
        ${d ? html`<section class="sec cmp-task" aria-labelledby="cmp-task-h">
          <div class="sec-head"><h2 id="cmp-task-h">${taskTitle}</h2><p class="sub">${d.repository} · ${stageLabel(d.stage)} · ${d.situation_label}${d.archived ? ' · archived' : ''}</p></div>
          ${Object.keys(d.overlays).length ? html`<p>${Object.entries(d.overlays).map((kv) => html`<${Status} key=${kv[0]} kind="warn">${kv[1].reason}</${Status}>`)}</p>` : null}
          <${Tabs} tabs=${tabs} active=${tab} onChange=${setTab} label="Task views" idBase="cmp" />
          <${TabPanel} idBase="cmp" active=${tab}>
            ${tab === 'resume' ? html`<${ResumeTab} d=${d} changes=${changes} moreChanges=${moreChanges} act=${act} busy=${busy} goTab=${setTab} form=${form('resume')} setForm=${setForm('resume')} db=${db} />` : null}
            ${tab === 'plan' ? html`<${PlanTab} d=${d} jobs=${jobs} rev=${rev} act=${act} busy=${busy} draft=${draft} setDraft=${setDraft} revoke=${revoke} gen=${gen} say=${say} />` : null}
            ${tab === 'review' ? html`<${ReviewTab} rev=${rev} act=${act} busy=${busy} goTab=${setTab} ack=${ack} setAck=${setAck} assess=${form('assess') || {}} setAssess=${setForm('assess')} />` : null}
            ${tab === 'verify' ? html`<${VerifyTab} d=${d} ver=${ver} act=${act} busy=${busy} more=${moreLedger} form=${form('observe')} setForm=${setForm('observe')} />` : null}
            ${tab === 'handoff' ? html`<${HandoffTab} d=${d} ho=${ho} act=${act} busy=${busy} say=${say} revoke=${revoke} gen=${gen}
              dr=${dr} form=${form('drafts')} setForm=${setForm('drafts')} dbKey=${dbRef.current.flow || dbRef.current.investigation} />` : null}
          </${TabPanel}></section>` : null}
      </div>
    </div>
  </${Desk}>`;
}
