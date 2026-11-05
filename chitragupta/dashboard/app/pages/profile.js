// Profile: whose work Chitragupta tracks. Your identity (emails and account ids masked unless you show them), the
// confirmed commit emails and provider accounts with each provider's status, suggestions with where they came from
// (nothing is yours until you confirm it), the scope, planning context, time zone and working week, the history window,
// collaborative tickets and preferences, and how current the tracking is. Every change shows its effect first
// (/api/profile/preview, nothing written) and is then saved through the Settings write path (PATCH /api/settings).
import { useEffect, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { api, useApi, write, runAction, toast, fmtTs, fmtDay, plural } from '../lib/core.js';
import { Icon, Status, Empty, Load, PageHead, Dialog, Scroll, TicketLink } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

const PROVIDERS = [['jira', 'Jira'], ['github', 'GitHub'], ['gitlab', 'GitLab'], ['azure', 'Azure DevOps'], ['bitbucket', 'Bitbucket']];
const PNAME = Object.fromEntries(PROVIDERS);
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const STATE = { ok: ['ok', 'Connected'], unconfigured: ['off', 'Not set up'], partial: ['warn', 'Partly set up'], error: ['bad', 'Problem'] };
const EMAIL = /^[^\s@]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/;
const TICKET = /^[A-Z][A-Z0-9]{1,9}-\d{1,7}$/;
const LABEL = {
  'git_author_emails': 'Confirmed commit emails', 'profile.accounts': 'Confirmed accounts', 'profile.display_name': 'Display name',
  'profile.email': 'Primary work email', 'profile.repos': 'Repositories in scope', 'profile.jira_projects': 'Jira projects',
  'profile.focus': 'Current focus', 'profile.role': 'Role', 'profile.technologies': 'Technologies', 'profile.timezone': 'Time zone',
  'jira_worklog.workdays': 'Working days', 'profile.tracking_start': 'Tracking starts', 'profile.backfill_days': 'Import history (days)',
  'profile.include_tickets': 'Collaborative tickets', 'profile.celebrations': 'Milestone messages', 'profile.animations': 'Animations',
  'profile.reflection': 'Daily reflection', 'progress.exclude_globs': 'Left out of line counts', 'progress.done_statuses': 'Statuses that mean done',
};

function words(s) { return String(s || '').split(',').map((x) => x.trim()).filter(Boolean); }
function lines(s) { return String(s || '').split(/\r?\n/).map((x) => x.trim()).filter(Boolean); }
function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
// The unmasked profile, fetched only to build a change (the page itself shows masked values unless you ask).
function real() { return api('/api/profile?reveal=1'); }

function StateBadge({ s }) {
  const x = STATE[s] || STATE.unconfigured;
  return html`<${Status} kind=${x[0]} quiet>${x[1]}</${Status}>`;
}

function Sec({ id, title, sub, children }) {
  return html`<section class="sec pfsec" aria-labelledby=${'pf-' + id} id=${'pfs-' + id}><div class="sec-head"><h2 id=${'pf-' + id} tabindex="-1">${title}</h2>
    ${sub ? html`<p class="sub">${sub}</p>` : null}</div>${children}</section>`;
}

function Row({ id, label, help, err, children }) {
  return html`<div class="setrow"><div class="setlab"><label for=${id}>${label}</label>${help ? html`<p class="muted small" id=${id + '-help'}>${help}</p>` : null}</div>
    <div class="setctl">${children}${err ? html`<p class="field-err" role="alert"><${Icon} name="bad" size=${14} />${err}</p>` : null}</div></div>`;
}

// ------------------------------------------------------------------ the review step: effect first, then save
function Effect({ p }) {
  const out = [];
  const add = (cond, text) => { if (cond) out.push(text); };
  add(p.aliases.added.length, 'Commit emails added: ' + p.aliases.added.join(', '));
  add(p.aliases.removed.length, 'Commit emails removed: ' + p.aliases.removed.join(', '));
  add(p.accounts.added.length, 'Accounts added: ' + p.accounts.added.map((a) => (PNAME[a.provider] || a.provider) + ' ' + (a.instance || '') + ' ' + (a.login || a.id)).join('; '));
  add(p.accounts.removed.length, 'Accounts removed: ' + p.accounts.removed.map((a) => (PNAME[a.provider] || a.provider) + ' ' + (a.instance || '') + ' ' + (a.login || a.id)).join('; '));
  add(p.repos.added.length, 'Repositories entering your scope: ' + p.repos.added.join(', '));
  add(p.repos.removed.length, 'Repositories leaving your scope: ' + p.repos.removed.join(', '));
  add(p.tickets.entering.length, 'Tickets that become yours: ' + p.tickets.entering.join(', '));
  add(p.tickets.leaving.length, 'Tickets no longer yours: ' + p.tickets.leaving.join(', '));
  add(p.leaving.commits || p.leaving.prs, 'Counted now and leaving: ' + plural(p.leaving.commits, 'commit') + ', ' + plural(p.leaving.prs, 'PR') + '.');
  return html`<ul class="pfeffect">${out.map((t, i) => html`<li key=${'e' + i}>${t}</li>`)}${p.notes.map((t, i) => html`<li key=${'n' + i}>${t}</li>`)}
    ${!out.length && !p.notes.length ? html`<li>Nothing that counts as yours changes.</li>` : null}</ul>`;
}

function Review({ pending, onClose, onSaved, setErrs }) {
  const [prev, setPrev] = useState(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setPrev(null); setErr('');
    if (!pending) return;
    api('/api/profile/preview?changes=' + encodeURIComponent(JSON.stringify(pending.changes))).then(setPrev, (e) => setErr(e.message));
  }, [pending]);
  const save = () => {
    setBusy(true);
    write('PATCH', '/api/settings', { changes: pending.changes }).then((r) => {
      toast('Saved: ' + pending.title.toLowerCase(), 'ok');
      setErrs({});
      onSaved(r);
    }, (e) => {
      if (e.fields) { setErrs(e.fields); setPrev({ ok: false, fields: e.fields }); }
      toast(e.message, 'bad');
    }).finally(() => setBusy(false));
  };
  const fields = prev && prev.ok === false ? prev.fields || {} : null;
  return html`<${Dialog} open=${!!pending} title=${pending ? pending.title : ''} onClose=${onClose}>
    ${pending ? html`<div class="pfreview">
      <p class="small muted">${Object.keys(pending.changes).map((k) => LABEL[k] || k).join(', ')}</p>
      ${err ? html`<p class="field-err" role="alert"><${Icon} name="bad" size=${14} />${err}</p>`
        : !prev ? html`<p class="muted" aria-live="polite">Working out what this changes…</p>`
          : fields ? html`<div role="alert"><p><strong>Not saved: some values aren't allowed.</strong></p>
            <ul class="pfeffect">${Object.keys(fields).map((k) => html`<li key=${k}>${LABEL[k] || k}: ${fields[k]}</li>`)}</ul></div>`
            : html`<h3 class="small">What this changes</h3><${Effect} p=${prev} />`}
      <div class="btnline"><button type="button" class="btn primary" disabled=${busy || !prev || !!fields || !!err} onClick=${save}>${busy ? 'Saving…' : 'Save'}</button>
        <button type="button" class="btn" onClick=${onClose}>Cancel</button></div></div>` : null}
  </${Dialog}>`;
}

// ------------------------------------------------------------------ sections
function Identity({ d, propose, errs }) {
  const [name, setName] = useState(d.values['profile.display_name'] || '');
  const [email, setEmail] = useState('');
  useEffect(() => { setName(d.values['profile.display_name'] || ''); }, [d.values['profile.display_name']]);
  const submit = (e) => {
    e.preventDefault();
    const ch = {};
    if (name.trim() !== (d.values['profile.display_name'] || '')) ch['profile.display_name'] = name.trim();
    if (email.trim()) ch['profile.email'] = email.trim();
    if (!Object.keys(ch).length) { toast('Nothing changed.', 'info'); return; }
    propose('Update your identity', ch);
  };
  return html`<${Sec} id="identity" title="Identity" sub="How summaries greet you; never used to match your work">
    <form onSubmit=${submit}>
      <${Row} id="pf-name" label="Display name" help="Shown in greetings and summaries." err=${errs['profile.display_name']}>
        <input id="pf-name" type="text" maxlength="80" value=${name} onInput=${(e) => setName(e.currentTarget.value)} /></${Row}>
      <${Row} id="pf-email" label="Primary work email" err=${errs['profile.email']}
        help=${'Now: ' + (d.email || 'not set') + '. Shown to you only; the Jira lookup uses it. It is not proof that a commit is yours: commit emails are confirmed below.'}>
        <input id="pf-email" type="email" autocomplete="off" spellcheck="false" placeholder=${d.email ? 'Type a new address to change it' : 'developer@acme.test'}
          value=${email} onInput=${(e) => setEmail(e.currentTarget.value)} />
        ${d.email ? html`<button type="button" class="btn quiet" onClick=${() => propose('Remove the primary email', { 'profile.email': '' })}>Remove the primary email</button>` : null}</${Row}>
      <div class="btnline"><button type="submit" class="btn">Review and save</button></div></form></${Sec}>`;
}

function Aliases({ d, propose, errs }) {
  const [add, setAdd] = useState('');
  const remove = (i) => propose('Remove a commit email', real().then((r) => ({ git_author_emails: r.aliases.filter((_, j) => j !== i) })));
  const submit = (e) => {
    e.preventDefault();
    const v = add.trim();
    if (!EMAIL.test(v)) { toast('Use an email address such as developer@acme.test.', 'bad'); return; }
    propose('Add a commit email', real().then((r) => ({ git_author_emails: r.aliases.concat([v]) })));
  };
  return html`<${Sec} id="emails" title="Confirmed commit emails" sub="A commit is yours only when its author email is exactly one of these">
    ${d.aliases.length ? html`<ul class="other-list pflist">${d.aliases.map((e, i) => html`<li key=${i}><span class="mono">${e}</span>
      <button type="button" class="btn quiet" onClick=${() => remove(i)} aria-label=${'Remove commit email ' + e}>Remove</button></li>`)}</ul>`
      : html`<p class="note">No commit email is confirmed, so no commit is counted as yours. Confirm a suggestion below or add one.</p>`}
    ${errs.git_author_emails ? html`<p class="field-err" role="alert"><${Icon} name="bad" size=${14} />${errs.git_author_emails}</p>` : null}
    <form class="btnline pfadd" onSubmit=${submit}><label class="sr" for="pf-add-email">Add a commit email</label>
      <input id="pf-add-email" type="email" autocomplete="off" spellcheck="false" placeholder="Add an email you commit with" value=${add} onInput=${(e) => setAdd(e.currentTarget.value)} />
      <button type="submit" class="btn">Add</button></form>
    <p class="muted small">Look-alike addresses never match: no dot or plus stripping, no matching by name or domain. A bot or service address can't be added.</p></${Sec}>`;
}

function accountFor(d, prov, inst) {
  return d.accounts.find((a) => a.provider === prov && (prov === 'jira' || a.instance === inst));
}

function Accounts({ d, propose, errs }) {
  const st = d.status;
  const [f, setF] = useState({ provider: 'github', instance: '', id: '', login: '' });
  const remove = (a) => propose('Remove an account', real().then((r) => ({
    'profile.accounts': (r.accounts_raw || []).filter((x) => !(x.provider === a.provider && (x.instance || '') === a.instance)) })));
  const submit = (e) => {
    e.preventDefault();
    const n = { provider: f.provider, instance: f.instance.trim().toLowerCase(), id: f.id.trim(), login: f.login.trim(), source: 'entered on the Profile page', confirmed_at: new Date().toISOString() };
    if (!n.instance || !(n.id || n.login)) { toast('Give the instance and the account id or login.', 'bad'); return; }
    propose('Add an account', real().then((r) => ({
      'profile.accounts': (r.accounts_raw || []).filter((x) => !(x.provider === n.provider && (x.instance || '') === n.instance)).concat([n]) })));
  };
  const rows = [{ key: 'git', name: 'Git commits', s: st.git, who: plural(st.git.aliases || 0, 'confirmed email') }]
    .concat([{ key: 'jira', name: 'Jira', s: st.jira, acct: accountFor(d, 'jira') }])
    .concat((st.hosts || []).map((h) => ({ key: h.provider + h.instance, name: (PNAME[h.provider] || h.provider) + ' · ' + h.instance, s: h, acct: accountFor(d, h.provider, h.instance) })));
  return html`<${Sec} id="accounts" title="Connected accounts" sub="One provider's gap never blocks another">
    <${Scroll} label="Accounts and their status"><table><caption class="sr">Each provider: your confirmed account and what is tracked</caption>
      <thead><tr><th scope="col">Provider</th><th scope="col">Your account</th><th scope="col">Status</th><th scope="col">What it means</th><th scope="col"><span class="sr">Actions</span></th></tr></thead>
      <tbody>${rows.map((r) => html`<tr key=${r.key}><th scope="row">${r.name}</th>
        <td>${r.who || (r.acct ? html`<span class="mono">${r.acct.login || r.acct.id}</span>${r.acct.legacy ? html`<div class="small muted">from the older mcp.jira.account_id setting</div>` : r.acct.source ? html`<div class="small muted">${r.acct.source}</div>` : null}` : html`<span class="muted">none confirmed</span>`)}</td>
        <td><${StateBadge} s=${r.s.state} /></td><td class="small">${r.s.why}</td>
        <td>${r.acct && !r.acct.legacy ? html`<button type="button" class="btn quiet" onClick=${() => remove(r.acct)} aria-label=${'Remove the ' + r.name + ' account'}>Remove</button>` : null}</td></tr>`)}</tbody></table></${Scroll}>
    ${errs['profile.accounts'] ? html`<p class="field-err" role="alert"><${Icon} name="bad" size=${14} />${errs['profile.accounts']}</p>` : null}
    <details class="more"><summary>Add an account by hand</summary>
      <form class="pfacct" onSubmit=${submit}>
        <label class="field">Provider<select value=${f.provider} onChange=${(e) => setF(Object.assign({}, f, { provider: e.currentTarget.value }))}>
          ${PROVIDERS.map((p) => html`<option key=${p[0]} value=${p[0]}>${p[1]}</option>`)}</select></label>
        <label class="field">Instance<input type="text" spellcheck="false" placeholder="github.com or acme.atlassian.net" value=${f.instance} onInput=${(e) => setF(Object.assign({}, f, { instance: e.currentTarget.value }))} /></label>
        <label class="field">Account id (preferred)<input type="text" spellcheck="false" value=${f.id} onInput=${(e) => setF(Object.assign({}, f, { id: e.currentTarget.value }))} /></label>
        <label class="field">Login or sign-in name<input type="text" spellcheck="false" value=${f.login} onInput=${(e) => setF(Object.assign({}, f, { login: e.currentTarget.value }))} /></label>
        <div class="btnline"><button type="submit" class="btn">Review and add</button></div></form>
      <p class="muted small">Stable ids beat logins. Tokens and passwords never go here: sign-ins stay with each tool.</p></details></${Sec}>`;
}

function Candidates({ d, propose, onFound }) {
  const c = d.candidates || { candidates: [], errors: {} };
  const [busy, setBusy] = useState('');
  const find = (what, body) => {
    setBusy(what);
    runAction('profile-discover', body).then((r) => {
      if (!r.ok) toast('Not looked up: ' + r.skipped, 'info');
      else toast(plural(r.candidates, 'suggestion') + (Object.keys(r.errors || {}).length ? '; some sources failed' : ''), 'ok');
      onFound();
    }, (e) => toast(e.message, 'bad')).finally(() => setBusy(''));
  };
  const confirm = (x) => propose(x.kind === 'email' ? 'Confirm a commit email' : 'Confirm an account', real().then((r) => {
    const got = ((r.candidates || {}).candidates || []).find((y) => y.cid === x.cid);
    if (!got) throw new Error('That suggestion is gone; look again.');
    if (got.kind === 'email') return { git_author_emails: r.aliases.concat([got.value]) };
    const n = { provider: got.provider, instance: got.instance, id: got.id || '', login: got.login || '', source: got.source || '', confirmed_at: new Date().toISOString() };
    if (got.node_id) n.node_id = got.node_id;
    return { 'profile.accounts': (r.accounts_raw || []).filter((a) => !(a.provider === n.provider && (a.instance || '') === n.instance)).concat([n]) };
  }));
  const errs = Object.keys(c.errors || {});
  return html`<${Sec} id="suggestions" title="Suggestions" sub="Found in your git config and your signed-in tools; nothing is yours until you confirm it">
    <div class="btnline">
      <button type="button" class="btn" disabled=${!!busy} onClick=${() => find('git', {})}><${Icon} name="reset" size=${15} />${busy === 'git' ? 'Looking…' : 'Check git config again'}</button>
      <button type="button" class="btn" disabled=${!!busy} onClick=${() => find('hosts', { hosts: true })}><${Icon} name="search" size=${15} />${busy === 'hosts' ? 'Asking the Git host tools…' : 'Find my Git host accounts'}</button>
      <button type="button" class="btn" disabled=${!!busy} onClick=${() => find('jira', { jira: true })}><${Icon} name="search" size=${15} />${busy === 'jira' ? 'Asking Jira…' : 'Look up my Jira account'}</button></div>
    <p class="muted small">Git host accounts: one read of the account gh, glab or az is signed in as. Jira: one read-only lookup for your primary email, within the daily MCP budget.${c.at ? ' Last looked: ' + fmtTs(c.at) + '.' : ''}</p>
    ${c.candidates.length ? html`<${Scroll} label="Suggestions"><table><caption class="sr">Identity suggestions and where each came from</caption>
      <thead><tr><th scope="col">Suggestion</th><th scope="col">Found in</th><th scope="col">State</th></tr></thead>
      <tbody>${c.candidates.map((x) => html`<tr key=${x.cid}>
        <td>${x.kind === 'email' ? html`<span class="mono">${x.value}</span>` : html`${PNAME[x.provider] || x.provider} · ${x.instance} · <span class="mono">${x.login || x.id}</span>${x.handle ? html` <span class="muted small">(${x.handle})</span>` : null}`}</td>
        <td class="small">${x.source}</td>
        <td>${x.confirmed ? html`<${Status} kind="ok" quiet>Confirmed</${Status}>` : x.bot ? html`<${Status} kind="off" quiet>Bot or service identity: can't be yours</${Status}>`
          : html`<button type="button" class="btn" onClick=${() => confirm(x)} aria-label=${'Confirm ' + (x.kind === 'email' ? x.value : (x.login || x.id) + ' on ' + x.instance)}>Confirm</button>`}</td></tr>`)}</tbody></table></${Scroll}>`
      : html`<${Empty} title="No suggestions yet" icon="search">Check git config, or find your Git host accounts. You can also add emails and accounts by hand above.</${Empty}>`}
    ${errs.length ? html`<p class="small"><${Status} kind="warn" quiet>Couldn't look:</${Status}> ${errs.map((k) => k + ': ' + c.errors[k]).join('; ')}</p>` : null}</${Sec}>`;
}

function Scope({ d, propose, errs }) {
  const v = d.values;
  const [only, setOnly] = useState((v['profile.repos'] || []).length > 0);
  const [sel, setSel] = useState(d.watched_repos.filter((r) => r.selected).map((r) => r.path));
  const [proj, setProj] = useState((v['profile.jira_projects'] || []).join(', '));
  const submit = (e) => {
    e.preventDefault();
    const ch = {};
    const repos = only ? sel : [];
    if (!same(repos, v['profile.repos'] || [])) ch['profile.repos'] = repos;
    const p = words(proj).map((x) => x.toUpperCase());
    if (!same(p, v['profile.jira_projects'] || [])) ch['profile.jira_projects'] = p;
    if (!Object.keys(ch).length) { toast('Nothing changed.', 'info'); return; }
    if (only && !repos.length) { toast('Pick at least one repository, or track every watched one.', 'bad'); return; }
    propose('Change your scope', ch);
  };
  return html`<${Sec} id="scope" title="Scope" sub="The repositories and Jira projects whose work is yours to track">
    <form onSubmit=${submit}>
      <fieldset class="pfscope"><legend>Repositories</legend>
        <label class="check"><input type="radio" name="pf-scope" checked=${!only} onChange=${() => setOnly(false)} />Every watched repository (${d.watched_repos.length})</label>
        <label class="check"><input type="radio" name="pf-scope" checked=${only} onChange=${() => setOnly(true)} />Only the ones I pick</label>
        ${only ? html`<div class="pfrepos">${d.watched_repos.map((r) => html`<label class="check" key=${r.path}><input type="checkbox" checked=${sel.indexOf(r.path) >= 0}
          onChange=${(e) => setSel(e.currentTarget.checked ? sel.concat([r.path]) : sel.filter((x) => x !== r.path))} /><span>${r.name} <span class="muted small mono">${r.path}</span></span></label>`)}</div>` : null}
        ${errs['profile.repos'] ? html`<p class="field-err" role="alert"><${Icon} name="bad" size=${14} />${errs['profile.repos']}</p>` : null}</fieldset>
      <${Row} id="pf-proj" label="Jira projects" help=${'Comma-separated keys. Empty: your ticket prefixes (' + (d.ticket_prefixes.join(', ') || 'none') + ').'} err=${errs['profile.jira_projects']}>
        <input id="pf-proj" type="text" spellcheck="false" value=${proj} onInput=${(e) => setProj(e.currentTarget.value)} /></${Row}>
      <div class="btnline"><button type="submit" class="btn">Review and save</button></div></form></${Sec}>`;
}

function Context({ d, propose, errs }) {
  const v = d.values;
  const [focus, setFocus] = useState(v['profile.focus'] || '');
  const [role, setRole] = useState(v['profile.role'] || '');
  const [tech, setTech] = useState((v['profile.technologies'] || []).join(', '));
  const submit = (e) => {
    e.preventDefault();
    const ch = {};
    if (focus !== (v['profile.focus'] || '')) ch['profile.focus'] = focus;
    if (role.trim() !== (v['profile.role'] || '')) ch['profile.role'] = role.trim();
    if (!same(words(tech), v['profile.technologies'] || [])) ch['profile.technologies'] = words(tech);
    if (!Object.keys(ch).length) { toast('Nothing changed.', 'info'); return; }
    propose('Update your planning context', ch);
  };
  return html`<${Sec} id="context" title="Planning context" sub="Given to Claude in a short profile line; never evidence of what is yours">
    <form onSubmit=${submit}>
      <${Row} id="pf-focus" label="Current focus" help="Projects, responsibilities or what you are driving now (400 characters)." err=${errs['profile.focus']}>
        <textarea id="pf-focus" rows="3" maxlength="400" value=${focus} onInput=${(e) => setFocus(e.currentTarget.value)}></textarea></${Row}>
      <${Row} id="pf-role" label="Role" help="Optional." err=${errs['profile.role']}>
        <input id="pf-role" type="text" maxlength="80" value=${role} onInput=${(e) => setRole(e.currentTarget.value)} /></${Row}>
      <${Row} id="pf-tech" label="Preferred technologies" help="Optional, comma-separated." err=${errs['profile.technologies']}>
        <input id="pf-tech" type="text" value=${tech} onInput=${(e) => setTech(e.currentTarget.value)} /></${Row}>
      <div class="btnline"><button type="submit" class="btn">Review and save</button></div></form></${Sec}>`;
}

function Time({ d, propose, errs }) {
  const v = d.values;
  const [tz, setTz] = useState(v['profile.timezone'] || '');
  const [days, setDays] = useState(v['jira_worklog.workdays'] || []);
  const [start, setStart] = useState(v['profile.tracking_start'] || '');
  const [back, setBack] = useState(String(v['profile.backfill_days'] || 90));
  const submit = (e) => {
    e.preventDefault();
    const ch = {};
    if (tz.trim() !== (v['profile.timezone'] || '')) ch['profile.timezone'] = tz.trim();
    if (!same(days, v['jira_worklog.workdays'] || [])) ch['jira_worklog.workdays'] = days;
    if (start !== (v['profile.tracking_start'] || '')) ch['profile.tracking_start'] = start;
    if (Number(back) !== v['profile.backfill_days']) ch['profile.backfill_days'] = Number(back);
    if (!Object.keys(ch).length) { toast('Nothing changed.', 'info'); return; }
    propose('Change time and history', ch);
  };
  return html`<${Sec} id="time" title="Time zone, working week and history" sub="Days, reminders and records follow these">
    <form onSubmit=${submit}>
      <${Row} id="pf-tz" label="Time zone" help="Empty: this machine's zone. Or an offset such as +05:30, or a zone name this machine has data for." err=${errs['profile.timezone']}>
        <input id="pf-tz" type="text" spellcheck="false" placeholder="this machine's zone" value=${tz} onInput=${(e) => setTz(e.currentTarget.value)} /></${Row}>
      <div class="setrow"><div class="setlab"><span id="pf-days-l">Working days</span><p class="muted small">Next steps stay quiet on other days; nothing counts a streak.</p></div>
        <div class="setctl"><fieldset class="daypick" aria-labelledby="pf-days-l"><legend class="sr">Working days</legend>${DAYS.map((x) => html`<label key=${x} class="check">
          <input type="checkbox" checked=${days.indexOf(x) >= 0} onChange=${(e) => setDays(DAYS.filter((y) => (y === x ? e.currentTarget.checked : days.indexOf(y) >= 0)))} />${x}</label>`)}</fieldset>
          ${errs['jira_worklog.workdays'] ? html`<p class="field-err" role="alert"><${Icon} name="bad" size=${14} />${errs['jira_worklog.workdays']}</p>` : null}</div></div>
      <${Row} id="pf-start" label="Tracking starts" help="History before this day isn't collected. Empty: the day Chitragupta first ran." err=${errs['profile.tracking_start']}>
        <input id="pf-start" type="date" value=${start} onInput=${(e) => setStart(e.currentTarget.value)} /></${Row}>
      <${Row} id="pf-back" label="Import history (days)" help="At most this many days back, whatever the start (1 to 365)." err=${errs['profile.backfill_days']}>
        <input id="pf-back" type="number" min="1" max="365" value=${back} onInput=${(e) => setBack(e.currentTarget.value)} /></${Row}>
      <div class="btnline"><button type="submit" class="btn">Review and save</button></div></form></${Sec}>`;
}

function Collab({ d, propose, errs }) {
  const cur = d.values['profile.include_tickets'] || {};
  const [key, setKey] = useState('');
  const [why, setWhy] = useState('');
  const keys = Object.keys(cur).sort();
  const submit = (e) => {
    e.preventDefault();
    const k = key.trim().toUpperCase();
    if (!TICKET.test(k) || !why.trim()) { toast('Give a ticket key such as ABC-1001 and why it counts as yours.', 'bad'); return; }
    propose('Include a collaborative ticket', { 'profile.include_tickets': Object.assign({}, cur, { [k]: why.trim() }) });
  };
  const remove = (k) => { const n = Object.assign({}, cur); delete n[k]; propose('Stop including ' + k, { 'profile.include_tickets': n }); };
  return html`<${Sec} id="collab" title="Collaborative tickets" sub="Tickets assigned to someone else that you count as yours; only your own activity on them counts">
    ${keys.length ? html`<ul class="other-list pflist">${keys.map((k) => html`<li key=${k}><span><${TicketLink} k=${k} /> <span class="muted small">${cur[k]}</span></span>
      <button type="button" class="btn quiet" onClick=${() => remove(k)} aria-label=${'Stop including ' + k}>Remove</button></li>`)}</ul>` : html`<p class="muted small">None. Tickets assigned to someone else are not yours just because they are on the same board.</p>`}
    ${errs['profile.include_tickets'] ? html`<p class="field-err" role="alert"><${Icon} name="bad" size=${14} />${errs['profile.include_tickets']}</p>` : null}
    <form class="pfacct" onSubmit=${submit}>
      <label class="field">Ticket<input type="text" spellcheck="false" placeholder="ABC-1001" value=${key} onInput=${(e) => setKey(e.currentTarget.value)} /></label>
      <label class="field">Why it counts as yours<input type="text" maxlength="120" value=${why} onInput=${(e) => setWhy(e.currentTarget.value)} /></label>
      <div class="btnline"><button type="submit" class="btn">Review and include</button></div></form></${Sec}>`;
}

function Prefs({ d, propose, errs }) {
  const v = d.values;
  const [f, setF] = useState({ c: v['profile.celebrations'], a: v['profile.animations'], r: v['profile.reflection'],
    g: (v['progress.exclude_globs'] || []).join('\n'), s: (v['progress.done_statuses'] || []).join(', ') });
  const put = (k, x) => setF(Object.assign({}, f, { [k]: x }));
  const submit = (e) => {
    e.preventDefault();
    const ch = {};
    if (f.c !== v['profile.celebrations']) ch['profile.celebrations'] = f.c;
    if (f.a !== v['profile.animations']) ch['profile.animations'] = f.a;
    if (f.r !== v['profile.reflection']) ch['profile.reflection'] = f.r;
    if (!same(lines(f.g), v['progress.exclude_globs'] || [])) ch['progress.exclude_globs'] = lines(f.g);
    if (!same(words(f.s), v['progress.done_statuses'] || [])) ch['progress.done_statuses'] = words(f.s);
    if (!Object.keys(ch).length) { toast('Nothing changed.', 'info'); return; }
    propose('Change preferences', ch);
  };
  return html`<${Sec} id="prefs" title="Preferences" sub="Tracking keeps running whatever you choose here">
    <form onSubmit=${submit}>
      <div class="setrow"><div class="setlab"><span id="pf-prefs-l">On the progress page</span></div><div class="setctl"><fieldset class="pfchecks" aria-labelledby="pf-prefs-l"><legend class="sr">On the progress page</legend>
        <label class="check"><input type="checkbox" checked=${f.c} onChange=${(e) => put('c', e.currentTarget.checked)} />Milestone messages (short, factual)</label>
        <label class="check"><input type="checkbox" checked=${f.a} onChange=${(e) => put('a', e.currentTarget.checked)} />Animations (reduced motion is always respected)</label>
        <label class="check"><input type="checkbox" checked=${f.r} onChange=${(e) => put('r', e.currentTarget.checked)} />Daily reflection box</label></fieldset></div></div>
      <${Row} id="pf-globs" label="Left out of line counts" help="Glob patterns, one per line: lockfiles, vendored, generated and build output. Shown separately, never hidden." err=${errs['progress.exclude_globs']}>
        <textarea id="pf-globs" rows="5" spellcheck="false" value=${f.g} onInput=${(e) => put('g', e.currentTarget.value)}></textarea></${Row}>
      <${Row} id="pf-done" label="Jira statuses that mean done" help="Comma-separated, any case. A move into one counts as completing the ticket that day." err=${errs['progress.done_statuses']}>
        <input id="pf-done" type="text" value=${f.s} onInput=${(e) => put('s', e.currentTarget.value)} /></${Row}>
      <div class="btnline"><button type="submit" class="btn">Review and save</button></div></form></${Sec}>`;
}

function Transparency({ d }) {
  const st = d.status;
  return html`<${Sec} id="tracking" title="Tracking" sub="What is collected, how current it is, and why something is yours">
    <dl class="kv">
      <dt>History</dt><dd>${st.coverage.label}</dd>
      <dt>Time zone</dt><dd>${st.timezone.state === 'ok' ? st.timezone.why : html`<${Status} kind="warn" quiet>${st.timezone.why}</${Status}>`}</dd>
      <dt>Commits read</dt><dd>${d.last.commits ? fmtTs(d.last.commits) : 'not yet'}</dd>
      <dt>PRs read</dt><dd>${d.last.prs ? fmtTs(d.last.prs) : 'not yet'}</dd>
      ${d.reconciled ? html`<dt>Last profile change</dt><dd>${fmtTs(d.reconciled.at)}: ${plural(d.reconciled.archived || 0, 'commit record')} archived, ${d.reconciled.restored || 0} restored${d.reconciled.unverified ? ', ' + d.reconciled.unverified + ' older ones not verifiable (left as they were)' : ''}</dd>` : null}
    </dl>
    ${d.collected.length ? html`<${Scroll} label="Collection per repository"><table><caption class="sr">Collection per repository</caption>
      <thead><tr><th scope="col">Repository</th><th scope="col">State</th><th scope="col">Commits</th><th scope="col">Pull requests</th></tr></thead>
      <tbody>${d.collected.map((r) => html`<tr key=${r.repo}><th scope="row">${r.repo}</th><td class="small">${r.state}${r.since ? ', since ' + fmtDay(r.since, true) : ''}</td>
        <td class="small">${r.commits || '—'}${r.commits_why ? html`<div class="muted">${r.commits_why}</div>` : null}</td>
        <td class="small">${r.prs || '—'}${r.prs_why ? html`<div class="muted">${r.prs_why}</div>` : null}</td></tr>`)}</tbody></table></${Scroll}>` : null}
    <details class="more"><summary>What makes something yours</summary><ul class="small pfrules">
      <li>A commit: its author email is exactly one of your confirmed emails (never the committer, never a name or domain). Merge commits are left out.</li>
      <li>A pull request: your confirmed account on that Git host opened it. Reviewing or merging someone else's PR doesn't make it yours.</li>
      <li>A ticket: Jira assigns it to your confirmed account, or you included it with a reason. A reassigned ticket stops being yours; your earlier work on it is kept.</li>
      <li>Bot and service identities are never yours. With nothing confirmed for a provider, nothing from it counts: never everyone's work.</li></ul></details></${Sec}>`;
}

export function ProfilePage() {
  const [reveal, setReveal] = useState(false);
  const res = useApi('/api/profile' + (reveal ? '?reveal=1' : ''));
  const [pending, setPending] = useState(null);
  const [errs, setErrs] = useState({});
  useEffect(() => { document.title = 'Profile · Chitragupta'; pageKeys.set({}); }, []);
  // a change: its title and the settings it sets (or a promise of them, when it needs the unmasked values)
  const propose = (title, changes) => Promise.resolve(changes).then((c) => { setPending({ title, changes: c }); },
    (e) => { toast(e.message || String(e), 'bad'); });
  const d = res.data;
  return html`<${Desk} className="profile-desk">
    <${PageHead} title="Profile" sub=${(d && d.display_name ? d.display_name + ' · ' : '') + 'whose work Chitragupta tracks, and why'}>
      <button type="button" class="btn" aria-pressed=${reveal ? 'true' : 'false'} onClick=${() => setReveal(!reveal)}>
        <${Icon} name=${reveal ? 'off' : 'search'} size=${15} />${reveal ? 'Hide emails and ids' : 'Show emails and ids'}</button></${PageHead}>
    <${Load} res=${res} lines=${10}>${(d) => html`
      <p class="lede">Chitragupta counts commits, pull requests and tickets as yours only through the identities you confirm here.
        Emails and account ids stay hidden on screen unless you show them; they never appear in progress summaries, copies or exports.</p>
      <${Identity} key=${'i' + d.email + d.display_name} d=${d} propose=${propose} errs=${errs} />
      <${Aliases} key=${'a' + d.aliases.join()} d=${d} propose=${propose} errs=${errs} />
      <${Accounts} d=${d} propose=${propose} errs=${errs} />
      <${Candidates} d=${d} propose=${propose} onFound=${res.reload} />
      <${Scope} key=${'s' + JSON.stringify(d.values['profile.repos']) + d.values['profile.jira_projects'].join()} d=${d} propose=${propose} errs=${errs} />
      <${Context} key=${'c' + d.values['profile.focus'] + d.values['profile.role']} d=${d} propose=${propose} errs=${errs} />
      <${Time} key=${'t' + d.values['profile.timezone'] + d.values['profile.tracking_start'] + d.values['profile.backfill_days'] + d.values['jira_worklog.workdays'].join()} d=${d} propose=${propose} errs=${errs} />
      <${Collab} key=${'k' + JSON.stringify(d.values['profile.include_tickets'])} d=${d} propose=${propose} errs=${errs} />
      <${Prefs} key=${'p' + JSON.stringify([d.values['profile.celebrations'], d.values['profile.animations'], d.values['profile.reflection'], d.values['progress.exclude_globs'], d.values['progress.done_statuses']])} d=${d} propose=${propose} errs=${errs} />
      <${Transparency} d=${d} />`}</${Load}>
    <${Review} pending=${pending} onClose=${() => setPending(null)} setErrs=${setErrs} onSaved=${() => { setPending(null); res.reload(); }} />
  </${Desk}>`;
}
