// Profile: whose work Chitragupta tracks, in three groups. Identity: your name and primary email, the confirmed commit
// emails and provider accounts (several per host are fine), each provider's identity and connection state, and the
// suggestions found in git config and signed-in tools beside them (nothing is yours until you confirm it). Scope: the
// repositories, Jira projects and collaborative tickets. Preferences and tracking: planning context, time zone and
// history, what the progress page shows, and how current collection is. Every change shows its effect first
// (/api/profile/preview, nothing written) and is then saved through the Settings write path (PATCH /api/settings).
// Unsaved edits are drafts kept by the page itself (and for this tab in sessionStorage): showing emails and ids, a
// live refresh, a discovery or another section's save never resets them; only saving or discarding a group does.
import { useEffect, useRef, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { api, useApi, write, runAction, toast, fmtTs, fmtDay, plural } from '../lib/core.js';
import { Icon, Status, Empty, Load, PageHead, Dialog, Scroll, TicketLink } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

const PROVIDERS = [['jira', 'Jira'], ['github', 'GitHub'], ['gitlab', 'GitLab'], ['azure', 'Azure DevOps'], ['bitbucket', 'Bitbucket']];
const PNAME = Object.fromEntries(PROVIDERS);
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const IDENT = { ok: ['ok', 'Identity confirmed'], unconfigured: ['off', 'Not set up'], partial: ['warn', 'Partly set up'], error: ['bad', 'Problem'] };
const CONN = { ok: ['ok', 'Last read worked'], partial: ['warn', 'Partly read'], stale: ['warn', 'Not read recently'],
  failing: ['bad', 'Last read failed'], never: ['off', 'Not read yet'], off: ['off', 'Switched off'] };
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
// the drafts each group owns: saving or discarding a group clears exactly these
const GROUP_KEYS = {
  identity: ['name', 'email', 'addEmail', 'acct'],
  scope: ['only', 'sel', 'proj', 'collabKey', 'collabWhy'],
  prefs: ['focus', 'role', 'tech', 'tz', 'days', 'start', 'back', 'c', 'a', 'r', 'g', 's'],
};
const DRAFT_KEY = 'cg-profile-draft';

function words(s) { return String(s || '').split(',').map((x) => x.trim()).filter(Boolean); }
function lines(s) { return String(s || '').split(/\r?\n/).map((x) => x.trim()).filter(Boolean); }
function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
// The unmasked profile, fetched only to build a change (the page itself shows masked values unless you ask).
function real() { return api('/api/profile?reveal=1'); }
// the same identity profile.account_key uses: several accounts may share an instance, the same one twice may not
function akey(a) { return [a.provider, (a.instance || '').toLowerCase(), a.id || 'login:' + String(a.login || '').toLowerCase()].join('|'); }

function readDraft() {
  try { const v = JSON.parse(sessionStorage.getItem(DRAFT_KEY) || '{}'); return v && typeof v === 'object' ? v : {}; } catch (e) { return {}; }
}
function keepDraft(d) {
  try { if (Object.keys(d).length) sessionStorage.setItem(DRAFT_KEY, JSON.stringify(d)); else sessionStorage.removeItem(DRAFT_KEY); } catch (e) { /* private window */ }
}

function Badge({ map, s }) {
  const x = map[s] || map.unconfigured || map.never;
  return html`<${Status} kind=${x[0]} quiet>${x[1]}</${Status}>`;
}

function Group({ id, title, sub, dirty, children, onDiscard }) {
  return html`<section class="pfgroup" aria-labelledby=${'pf-' + id} id=${'pfs-' + id}>
    <div class="pfgroup-head"><h2 id=${'pf-' + id} tabindex="-1">${title}</h2>
      ${dirty ? html`<span class="pfdirty" role="status"><${Icon} name="wait" size=${14} />Unsaved changes
        <button type="button" class="btn quiet small" onClick=${onDiscard}>Discard</button></span>` : null}</div>
    ${sub ? html`<p class="sub">${sub}</p>` : null}${children}</section>`;
}

function Sub({ id, title, sub, children }) {
  return html`<section class="pfsub" id=${'pfs-' + id} aria-labelledby=${'pfh-' + id}><h3 id=${'pfh-' + id}>${title}</h3>${sub ? html`<p class="muted small">${sub}</p>` : null}${children}</section>`;
}

function Row({ id, label, help, err, children }) {
  return html`<div class="setrow"><div class="setlab"><label for=${id}>${label}</label>${help ? html`<p class="muted small" id=${id + '-help'}>${help}</p>` : null}</div>
    <div class="setctl">${children}${err ? html`<p class="field-err" role="alert"><${Icon} name="bad" size=${14} />${err}</p>` : null}</div></div>`;
}

function Err({ e }) { return e ? html`<p class="field-err" role="alert"><${Icon} name="bad" size=${14} />${e}</p>` : null; }

// ------------------------------------------------------------------ the review step: effect first, then save
function Effect({ p }) {
  const out = [];
  const add = (cond, text) => { if (cond) out.push(text); };
  const acct = (a) => (PNAME[a.provider] || a.provider) + ' ' + (a.instance || '') + ' ' + (a.login || a.id);
  add(p.aliases.added.length, 'Commit emails added: ' + p.aliases.added.join(', '));
  add(p.aliases.removed.length, 'Commit emails removed: ' + p.aliases.removed.join(', '));
  add(p.accounts.added.length, 'Accounts added: ' + p.accounts.added.map(acct).join('; '));
  add(p.accounts.removed.length, 'Accounts removed: ' + p.accounts.removed.map(acct).join('; '));
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
      onSaved(pending, r);
    }, (e) => {
      if (e.fields) { setErrs(e.fields); setPrev({ ok: false, fields: e.fields }); }
      toast('Not saved: ' + e.message, 'bad');
    }).finally(() => setBusy(false));
  };
  const fields = prev && prev.ok === false ? prev.fields || {} : null;
  return html`<${Dialog} open=${!!pending} title=${pending ? pending.title : ''} onClose=${onClose}>
    ${pending ? html`<div class="pfreview">
      <p class="small muted">${Object.keys(pending.changes).map((k) => LABEL[k] || k).join(', ')}</p>
      ${err ? html`<p class="field-err" role="alert"><${Icon} name="bad" size=${14} />${err}</p>`
        : !prev ? html`<p class="muted" aria-live="polite">Working out what this changes…</p>`
          : fields ? html`<div role="alert"><p><strong>Not saved: some values aren't allowed.</strong> Your edits are kept; fix them and review again.</p>
            <ul class="pfeffect">${Object.keys(fields).map((k) => html`<li key=${k}>${LABEL[k] || k}: ${fields[k]}</li>`)}</ul></div>`
            : html`<h3 class="small">What this changes</h3><${Effect} p=${prev} />`}
      <div class="btnline"><button type="button" class="btn primary" disabled=${busy || !prev || !!fields || !!err} onClick=${save}>${busy ? 'Saving…' : 'Save these changes'}</button>
        <button type="button" class="btn" onClick=${onClose}>Back to editing</button></div></div>` : null}
  </${Dialog}>`;
}

// ------------------------------------------------------------------ identity
function IdentityGroup({ d, show, v, put, propose, errs, dirty, discard, onFound }) {
  const st = d.status;
  const name = v('name', d.values['profile.display_name'] || ''), email = v('email', ''), add = v('addEmail', '');
  const f = v('acct', { provider: 'github', instance: '', id: '', login: '' });
  const submit = (e) => {
    e.preventDefault();
    const ch = {};
    if (name.trim() !== (d.values['profile.display_name'] || '')) ch['profile.display_name'] = name.trim();
    if (email.trim()) ch['profile.email'] = email.trim();
    if (!Object.keys(ch).length) { toast('Nothing changed in your name or primary email.', 'info'); return; }
    propose('Update your name and primary email', ch, ['name', 'email']);
  };
  const removeAlias = (i) => propose('Remove a commit email', real().then((r) => ({ git_author_emails: r.aliases.filter((_, j) => j !== i) })), []);
  const addAlias = (e) => {
    e.preventDefault();
    const x = add.trim();
    if (!EMAIL.test(x)) { toast('Use an email address such as developer@acme.test.', 'bad'); return; }
    propose('Add a commit email', real().then((r) => ({ git_author_emails: r.aliases.concat([x]) })), ['addEmail']);
  };
  const removeAcct = (a) => propose('Remove an account', real().then((r) => {
    const raw = (r.accounts_raw || []);
    // the row shown (its id may be masked) -> the same row unmasked -> exactly that account; others on the host stay
    const m = r.accounts.find((x) => x.provider === a.provider && x.instance === a.instance && (x.login || '') === (a.login || '')
      && x.source === a.source && x.confirmed_at === a.confirmed_at);
    const target = m ? raw.find((x) => akey(x) === akey(m)) : null;
    if (!target) throw new Error('That account changed; reload the page.');
    return { 'profile.accounts': raw.filter((x) => akey(x) !== akey(target)) };
  }), []);
  const addAcct = (e) => {
    e.preventDefault();
    const n = { provider: f.provider, instance: f.instance.trim().toLowerCase(), id: f.id.trim(), login: f.login.trim(), source: 'entered on the Profile page', confirmed_at: new Date().toISOString() };
    if (!n.instance || !(n.id || n.login)) { toast('Give the instance and the account id or login.', 'bad'); return; }
    // added next to your other accounts on that instance; only the very same account is replaced
    propose('Add an account', real().then((r) => ({ 'profile.accounts': (r.accounts_raw || []).filter((x) => akey(x) !== akey(n)).concat([n]) })), ['acct']);
  };
  const accts = (prov, inst) => show.accounts.filter((a) => a.provider === prov && (prov === 'jira' || a.instance === inst));
  const rows = [{ key: 'git', name: 'Git commits', s: st.git, who: plural(st.git.aliases || 0, 'confirmed email'), list: [] },
    { key: 'jira', name: 'Jira', s: st.jira, list: accts('jira') }]
    .concat((st.hosts || []).map((h) => ({ key: h.provider + h.instance, name: (PNAME[h.provider] || h.provider) + ' · ' + h.instance, s: h, list: accts(h.provider, h.instance) })));
  return html`<${Group} id="identity" title="Identity" sub="Who you are, and the emails and accounts whose work is yours" dirty=${dirty} onDiscard=${discard}>
    <${Sub} id="you" title="You" sub="Shown in greetings and summaries; never used to match your work">
      <form onSubmit=${submit}>
        <${Row} id="pf-name" label="Display name" err=${errs['profile.display_name']}>
          <input id="pf-name" type="text" maxlength="80" value=${name} onInput=${(e) => put('name', e.currentTarget.value)} /></${Row}>
        <${Row} id="pf-email" label="Primary work email" err=${errs['profile.email']}
          help=${'Now: ' + (show.email || 'not set') + '. The Jira lookup uses it. It is not proof that a commit is yours: commit emails are confirmed below.'}>
          <input id="pf-email" type="email" autocomplete="off" spellcheck="false" placeholder=${d.email ? 'Type a new address to change it' : 'developer@acme.test'}
            value=${email} onInput=${(e) => put('email', e.currentTarget.value)} />
          ${d.email ? html`<button type="button" class="btn quiet" onClick=${() => propose('Remove the primary email', { 'profile.email': '' }, [])}>Remove the primary email</button>` : null}</${Row}>
        <div class="btnline"><button type="submit" class="btn">Review name and email</button></div></form></${Sub}>
    <${Sub} id="emails" title="Confirmed commit emails" sub="A commit is yours only when its author email is exactly one of these">
      ${show.aliases.length ? html`<ul class="other-list pflist">${show.aliases.map((e, i) => html`<li key=${i}><span class="mono wrap">${e}</span>
        <button type="button" class="btn quiet" onClick=${() => removeAlias(i)} aria-label=${'Remove commit email ' + e}>Remove</button></li>`)}</ul>`
        : html`<p class="note">No commit email is confirmed, so no commit is counted as yours. Confirm a suggestion below or add one.</p>`}
      <${Err} e=${errs.git_author_emails} />
      <form class="btnline pfadd" onSubmit=${addAlias}><label class="sr" for="pf-add-email">Add a commit email</label>
        <input id="pf-add-email" type="email" autocomplete="off" spellcheck="false" placeholder="Add an email you commit with" value=${add} onInput=${(e) => put('addEmail', e.currentTarget.value)} />
        <button type="submit" class="btn">Review adding it</button></form>
      <p class="muted small">Look-alike addresses never match: no dot or plus stripping, no matching by name or domain. A bot or service address can't be added.</p></${Sub}>
    <${Sub} id="accounts" title="Accounts and connections" sub="Identity says what is confirmed; connection says whether the last read worked. One provider's gap never blocks another">
      <ul class="acctlist">${rows.map((r) => { const c = r.s.connection || {}; return html`<li key=${r.key}>
        <div class="acct-h"><strong>${r.name}</strong><span class="acct-b"><${Badge} map=${IDENT} s=${r.s.state} /><${Badge} map=${CONN} s=${c.state} /></span></div>
        ${r.who ? html`<p class="small">${r.who}</p>` : r.list.length ? html`<ul class="acct-ids">${r.list.map((a, i) => html`<li key=${i}><span class="mono wrap">${a.login || a.id}</span>
            <span class="muted small">${a.legacy ? 'from the older mcp.jira.account_id setting' : a.source || ''}</span>
            ${a.legacy ? null : html`<button type="button" class="btn quiet" onClick=${() => removeAcct(a)} aria-label=${'Remove the ' + r.name + ' account ' + (a.login || a.id)}>Remove</button>`}</li>`)}</ul>`
          : html`<p class="small muted">None confirmed.</p>`}
        <p class="small muted">${r.s.why}${c.why && r.s.why.indexOf(c.why.replace(/\.$/, '')) < 0 ? ' ' + c.why : ''}</p></li>`; })}</ul>
      <${Err} e=${errs['profile.accounts']} />
      <details class="more" open=${!!(f.instance || f.id || f.login)}><summary>Add an account by hand</summary>
        <form class="pfacct" onSubmit=${addAcct}>
          <label class="field">Provider<select value=${f.provider} onChange=${(e) => put('acct', Object.assign({}, f, { provider: e.currentTarget.value }))}>
            ${PROVIDERS.map((p) => html`<option key=${p[0]} value=${p[0]}>${p[1]}</option>`)}</select></label>
          <label class="field">Instance<input type="text" spellcheck="false" placeholder="github.com or acme.atlassian.net" value=${f.instance} onInput=${(e) => put('acct', Object.assign({}, f, { instance: e.currentTarget.value }))} /></label>
          <label class="field">Account id (preferred)<input type="text" spellcheck="false" value=${f.id} onInput=${(e) => put('acct', Object.assign({}, f, { id: e.currentTarget.value }))} /></label>
          <label class="field">Login or sign-in name<input type="text" spellcheck="false" value=${f.login} onInput=${(e) => put('acct', Object.assign({}, f, { login: e.currentTarget.value }))} /></label>
          <div class="btnline"><button type="submit" class="btn">Review adding the account</button></div></form>
        <p class="muted small">It is added next to any account you already have on that instance. Stable ids beat logins. Tokens and passwords never go here: sign-ins stay with each tool.</p></details></${Sub}>
    <${Suggestions} d=${d} show=${show} propose=${propose} onFound=${onFound} />
  </${Group}>`;
}

function Suggestions({ d, show, propose, onFound }) {
  const c = show.candidates || { candidates: [], errors: {} };
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
    return { 'profile.accounts': (r.accounts_raw || []).filter((a) => akey(a) !== akey(n)).concat([n]) };
  }), []);
  const errs = Object.keys(c.errors || {});
  return html`<${Sub} id="suggestions" title="Suggestions" sub="Found in your git config and your signed-in tools, with where each came from; nothing is yours until you confirm it">
    <div class="btnline">
      <button type="button" class="btn" disabled=${!!busy} onClick=${() => find('git', {})}><${Icon} name="reset" size=${15} />${busy === 'git' ? 'Looking…' : 'Check git config again'}</button>
      <button type="button" class="btn" disabled=${!!busy} onClick=${() => find('hosts', { hosts: true })}><${Icon} name="search" size=${15} />${busy === 'hosts' ? 'Asking the Git host tools…' : 'Find my Git host accounts'}</button>
      <button type="button" class="btn" disabled=${!!busy} onClick=${() => find('jira', { jira: true })}><${Icon} name="search" size=${15} />${busy === 'jira' ? 'Asking Jira…' : 'Look up my Jira account'}</button></div>
    <p class="muted small">Git host accounts: one read of the account gh, glab or az is signed in as. Jira: one read-only lookup for your primary email, within the daily MCP budget.${c.at ? ' Last looked: ' + fmtTs(c.at) + '.' : ''}</p>
    ${c.candidates.length ? html`<ul class="acctlist cands">${c.candidates.map((x) => html`<li key=${x.cid}>
        <div class="acct-h"><span>${x.kind === 'email' ? html`<span class="mono wrap">${x.value}</span>` : html`${PNAME[x.provider] || x.provider} · ${x.instance} · <span class="mono wrap">${x.login || x.id}</span>${x.handle ? html` <span class="muted small">(${x.handle})</span>` : null}`}</span>
          <span>${x.confirmed ? html`<${Status} kind="ok" quiet>Confirmed</${Status}>` : x.bot ? html`<${Status} kind="off" quiet>Bot or service identity: can't be yours</${Status}>`
            : html`<button type="button" class="btn" onClick=${() => confirm(x)} aria-label=${'Review confirming ' + (x.kind === 'email' ? x.value : (x.login || x.id) + ' on ' + x.instance)}>Review and confirm</button>`}</span></div>
        <p class="small muted">From ${x.source}</p></li>`)}</ul>`
      : html`<${Empty} title="No suggestions yet" icon="search">Check git config, or find your Git host accounts. You can also add emails and accounts by hand above.</${Empty}>`}
    ${errs.length ? html`<p class="small"><${Status} kind="warn" quiet>Couldn't look:</${Status}> ${errs.map((k) => k + ': ' + c.errors[k]).join('; ')}</p>` : null}</${Sub}>`;
}

// ------------------------------------------------------------------ scope
function ScopeGroup({ d, v, put, propose, errs, dirty, discard }) {
  const sv = d.values;
  const only = v('only', (sv['profile.repos'] || []).length > 0);
  const sel = v('sel', d.watched_repos.filter((r) => r.selected).map((r) => r.path));
  const proj = v('proj', (sv['profile.jira_projects'] || []).join(', '));
  const cur = sv['profile.include_tickets'] || {};
  const ck = v('collabKey', ''), cw = v('collabWhy', '');
  const submit = (e) => {
    e.preventDefault();
    const ch = {};
    const repos = only ? sel : [];
    if (!same(repos, sv['profile.repos'] || [])) ch['profile.repos'] = repos;
    const p = words(proj).map((x) => x.toUpperCase());
    if (!same(p, sv['profile.jira_projects'] || [])) ch['profile.jira_projects'] = p;
    if (only && !repos.length) { toast('Pick at least one repository, or track every watched one.', 'bad'); return; }
    if (!Object.keys(ch).length) { toast('Nothing changed in your repositories or projects.', 'info'); return; }
    propose('Change your repositories and projects', ch, ['only', 'sel', 'proj']);
  };
  const include = (e) => {
    e.preventDefault();
    const k = ck.trim().toUpperCase();
    if (!TICKET.test(k) || !cw.trim()) { toast('Give a ticket key such as ABC-1001 and why it counts as yours.', 'bad'); return; }
    propose('Include a collaborative ticket', { 'profile.include_tickets': Object.assign({}, cur, { [k]: cw.trim() }) }, ['collabKey', 'collabWhy']);
  };
  const stop = (k) => { const n = Object.assign({}, cur); delete n[k]; propose('Stop including ' + k, { 'profile.include_tickets': n }, []); };
  const keys = Object.keys(cur).sort();
  return html`<${Group} id="scope" title="Scope" sub="The repositories, Jira projects and collaborative tickets whose work is yours to track" dirty=${dirty} onDiscard=${discard}>
    <form onSubmit=${submit}>
      <fieldset class="pfscope"><legend>Repositories</legend>
        <label class="check"><input type="radio" name="pf-scope" checked=${!only} onChange=${() => put('only', false)} />Every watched repository (${d.watched_repos.length})</label>
        <label class="check"><input type="radio" name="pf-scope" checked=${only} onChange=${() => put('only', true)} />Only the ones I pick</label>
        ${only ? html`<div class="pfrepos">${d.watched_repos.map((r) => html`<label class="check" key=${r.path}><input type="checkbox" checked=${sel.indexOf(r.path) >= 0}
          onChange=${(e) => put('sel', e.currentTarget.checked ? sel.concat([r.path]) : sel.filter((x) => x !== r.path))} /><span>${r.name} <span class="muted small mono wrap">${r.path}</span></span></label>`)}</div>` : null}
        <${Err} e=${errs['profile.repos']} /></fieldset>
      <${Row} id="pf-proj" label="Jira projects" help=${'Comma-separated keys. Empty: your ticket prefixes (' + (d.ticket_prefixes.join(', ') || 'none') + '). Tickets of other projects are never searched or kept as yours.'} err=${errs['profile.jira_projects']}>
        <input id="pf-proj" type="text" spellcheck="false" value=${proj} onInput=${(e) => put('proj', e.currentTarget.value)} /></${Row}>
      <div class="btnline"><button type="submit" class="btn">Review repositories and projects</button></div></form>
    <${Sub} id="collab" title="Collaborative tickets" sub="Tickets assigned to someone else that you count as yours; only your own activity on them counts">
      ${keys.length ? html`<ul class="other-list pflist">${keys.map((k) => html`<li key=${k}><span><${TicketLink} k=${k} /> <span class="muted small">${cur[k]}</span></span>
        <button type="button" class="btn quiet" onClick=${() => stop(k)} aria-label=${'Stop including ' + k}>Remove</button></li>`)}</ul>` : html`<p class="muted small">None. Tickets assigned to someone else are not yours just because they are on the same board.</p>`}
      <${Err} e=${errs['profile.include_tickets']} />
      <form class="pfacct" onSubmit=${include}>
        <label class="field">Ticket<input type="text" spellcheck="false" placeholder="ABC-1001" value=${ck} onInput=${(e) => put('collabKey', e.currentTarget.value)} /></label>
        <label class="field">Why it counts as yours<input type="text" maxlength="120" value=${cw} onInput=${(e) => put('collabWhy', e.currentTarget.value)} /></label>
        <div class="btnline"><button type="submit" class="btn">Review including it</button></div></form></${Sub}>
  </${Group}>`;
}

// ------------------------------------------------------------------ preferences and tracking
function PrefsGroup({ d, v, put, propose, errs, dirty, discard }) {
  const sv = d.values;
  const focus = v('focus', sv['profile.focus'] || ''), role = v('role', sv['profile.role'] || ''), tech = v('tech', (sv['profile.technologies'] || []).join(', '));
  const tz = v('tz', sv['profile.timezone'] || ''), days = v('days', sv['jira_worklog.workdays'] || []);
  const start = v('start', sv['profile.tracking_start'] || ''), back = v('back', String(sv['profile.backfill_days'] || 90));
  const c = v('c', sv['profile.celebrations']), a = v('a', sv['profile.animations']), r = v('r', sv['profile.reflection']);
  const g = v('g', (sv['progress.exclude_globs'] || []).join('\n')), s = v('s', (sv['progress.done_statuses'] || []).join(', '));
  const submit = (e) => {
    e.preventDefault();
    const ch = {};
    if (focus !== (sv['profile.focus'] || '')) ch['profile.focus'] = focus;
    if (role.trim() !== (sv['profile.role'] || '')) ch['profile.role'] = role.trim();
    if (!same(words(tech), sv['profile.technologies'] || [])) ch['profile.technologies'] = words(tech);
    if (tz.trim() !== (sv['profile.timezone'] || '')) ch['profile.timezone'] = tz.trim();
    if (!same(days, sv['jira_worklog.workdays'] || [])) ch['jira_worklog.workdays'] = days;
    if (start !== (sv['profile.tracking_start'] || '')) ch['profile.tracking_start'] = start;
    if (Number(back) !== sv['profile.backfill_days']) ch['profile.backfill_days'] = Number(back);
    if (c !== sv['profile.celebrations']) ch['profile.celebrations'] = c;
    if (a !== sv['profile.animations']) ch['profile.animations'] = a;
    if (r !== sv['profile.reflection']) ch['profile.reflection'] = r;
    if (!same(lines(g), sv['progress.exclude_globs'] || [])) ch['progress.exclude_globs'] = lines(g);
    if (!same(words(s), sv['progress.done_statuses'] || [])) ch['progress.done_statuses'] = words(s);
    if (!Object.keys(ch).length) { toast('Nothing changed in your preferences.', 'info'); return; }
    propose('Change preferences and tracking', ch, GROUP_KEYS.prefs);
  };
  return html`<${Group} id="prefs" title="Preferences and tracking" sub="Planning context, days and history, and what the progress page shows; tracking keeps running whatever you choose" dirty=${dirty} onDiscard=${discard}>
    <form onSubmit=${submit}>
      <${Sub} id="context" title="Planning context" sub="Given to Claude in a short profile line; never evidence of what is yours">
        <${Row} id="pf-focus" label="Current focus" help="Projects, responsibilities or what you are driving now (400 characters)." err=${errs['profile.focus']}>
          <textarea id="pf-focus" rows="3" maxlength="400" value=${focus} onInput=${(e) => put('focus', e.currentTarget.value)}></textarea></${Row}>
        <${Row} id="pf-role" label="Role" help="Optional." err=${errs['profile.role']}>
          <input id="pf-role" type="text" maxlength="80" value=${role} onInput=${(e) => put('role', e.currentTarget.value)} /></${Row}>
        <${Row} id="pf-tech" label="Preferred technologies" help="Optional, comma-separated." err=${errs['profile.technologies']}>
          <input id="pf-tech" type="text" value=${tech} onInput=${(e) => put('tech', e.currentTarget.value)} /></${Row}></${Sub}>
      <${Sub} id="time" title="Time zone, working week and history" sub="Days, months, overdue releases, reminders and records all follow these">
        <${Row} id="pf-tz" label="Time zone" help="Empty: this machine's zone. Or an offset such as +05:30, or a zone name this machine has data for." err=${errs['profile.timezone']}>
          <input id="pf-tz" type="text" spellcheck="false" placeholder="this machine's zone" value=${tz} onInput=${(e) => put('tz', e.currentTarget.value)} /></${Row}>
        <div class="setrow"><div class="setlab"><span id="pf-days-l">Working days</span><p class="muted small">Next steps stay quiet on other days; nothing counts a streak.</p></div>
          <div class="setctl"><fieldset class="daypick" aria-labelledby="pf-days-l"><legend class="sr">Working days</legend>${DAYS.map((x) => html`<label key=${x} class="check">
            <input type="checkbox" checked=${days.indexOf(x) >= 0} onChange=${(e) => put('days', DAYS.filter((y) => (y === x ? e.currentTarget.checked : days.indexOf(y) >= 0)))} />${x}</label>`)}</fieldset>
            <${Err} e=${errs['jira_worklog.workdays']} /></div></div>
        <${Row} id="pf-start" label="Tracking starts" help="History before this day isn't collected. Empty: the day Chitragupta first ran." err=${errs['profile.tracking_start']}>
          <input id="pf-start" type="date" value=${start} onInput=${(e) => put('start', e.currentTarget.value)} /></${Row}>
        <${Row} id="pf-back" label="Import history (days)" help="At most this many days back, whatever the start (1 to 365)." err=${errs['profile.backfill_days']}>
          <input id="pf-back" type="number" min="1" max="365" value=${back} onInput=${(e) => put('back', e.currentTarget.value)} /></${Row}></${Sub}>
      <${Sub} id="display" title="On the progress pages">
        <fieldset class="pfchecks"><legend class="sr">On the progress pages</legend>
          <label class="check"><input type="checkbox" checked=${c} onChange=${(e) => put('c', e.currentTarget.checked)} />Milestone messages (short, factual)</label>
          <label class="check"><input type="checkbox" checked=${a} onChange=${(e) => put('a', e.currentTarget.checked)} />Animations (reduced motion is always respected)</label>
          <label class="check"><input type="checkbox" checked=${r} onChange=${(e) => put('r', e.currentTarget.checked)} />Daily reflection box</label></fieldset>
        <details class="more" open=${dirty && ('g' in (d._draft || {}) || 's' in (d._draft || {}))}><summary>Line-count exclusions and done statuses</summary>
          <${Row} id="pf-globs" label="Left out of line counts" help="Glob patterns, one per line: lockfiles, vendored, generated and build output. Shown separately, never hidden." err=${errs['progress.exclude_globs']}>
            <textarea id="pf-globs" rows="5" spellcheck="false" value=${g} onInput=${(e) => put('g', e.currentTarget.value)}></textarea></${Row}>
          <${Row} id="pf-done" label="Jira statuses that mean done" help="Comma-separated, any case. A move into one counts as completing the ticket that day." err=${errs['progress.done_statuses']}>
            <input id="pf-done" type="text" value=${s} onInput=${(e) => put('s', e.currentTarget.value)} /></${Row}></details></${Sub}>
      <div class="btnline"><button type="submit" class="btn">Review preferences</button></div></form>
    <${Tracking} d=${d} />
  </${Group}>`;
}

function Tracking({ d }) {
  const st = d.status;
  return html`<details class="more pftracking"><summary>Tracking: what is collected and how current it is</summary>
    <dl class="kv">
      <dt>History</dt><dd>${st.coverage.label}</dd>
      <dt>Time zone</dt><dd>${st.timezone.state === 'ok' ? st.timezone.why : html`<${Status} kind="warn" quiet>${st.timezone.why}</${Status}>`}</dd>
      <dt>Commits read</dt><dd>${d.last.commits ? fmtTs(d.last.commits) : 'not yet'}</dd>
      <dt>PRs read</dt><dd>${d.last.prs ? fmtTs(d.last.prs) : 'not yet'}</dd>
      ${d.reconciled ? html`<dt>Last profile change</dt><dd>${fmtTs(d.reconciled.at)}: ${plural(d.reconciled.archived || 0, 'commit record')} archived, ${d.reconciled.restored || 0} restored${d.reconciled.unverified ? ', ' + d.reconciled.unverified + ' whose author couldn\'t be verified kept apart' : ''}</dd>` : null}
    </dl>
    ${d.collected.length ? html`<ul class="acctlist">${d.collected.map((r) => html`<li key=${r.repo}><div class="acct-h"><strong>${r.repo}</strong><span class="small">${r.state}${r.since ? ', since ' + fmtDay(r.since, true) : ''}</span></div>
      <p class="small">Commits: ${r.commits || '—'}${r.commits_why ? html` <span class="muted">· ${r.commits_why}</span>` : null}</p>
      <p class="small">Pull requests: ${r.prs || '—'}${r.prs_why ? html` <span class="muted">· ${r.prs_why}</span>` : null}</p></li>`)}</ul>` : null}
    <ul class="small pfrules">
      <li>A commit: its author email is exactly one of your confirmed emails (never the committer, never a name or domain). Merge commits are left out.</li>
      <li>A pull request: one of your confirmed accounts on that Git host opened it. Reviewing or merging someone else's PR doesn't make it yours.</li>
      <li>A ticket: Jira assigns it to one of your confirmed Jira accounts (in your Jira projects), or you included it with a reason. A reassigned ticket stops being yours; your earlier work on it is kept. A ticket with none of your work on it isn't shown, searched or exported as yours.</li>
      <li>Bot and service identities are never yours. With nothing confirmed for a provider, nothing from it counts: never everyone's work.</li></ul></details>`;
}

// ------------------------------------------------------------------ the page
function groupDirty(draft, g) { return GROUP_KEYS[g].some((k) => k in draft); }

export function ProfilePage({ route }) {
  // the page's data is always the masked profile: showing emails and ids reads a second copy and never reloads this one
  const res = useApi('/api/profile');
  const [reveal, setReveal] = useState(false);
  const shown = useApi(reveal ? '/api/profile?reveal=1' : null);
  const [draft, setDraftState] = useState(readDraft);
  const restored = useRef(Object.keys(readDraft()).length > 0);
  const [pending, setPending] = useState(null);
  const [errs, setErrs] = useState({});
  useEffect(() => { document.title = 'Profile · Chitragupta'; pageKeys.set({}); }, []);
  useEffect(() => { if (restored.current) { toast('Your unsaved profile edits are back.', 'info'); restored.current = false; } }, []);
  useEffect(() => keepDraft(draft), [draft]);
  useEffect(() => {
    const warn = (e) => { if (Object.keys(draft).length) { e.preventDefault(); e.returnValue = ''; } };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [draft]);
  useEffect(() => {
    const g = route && route.params && route.params.section;
    if (!g || !res.data) return;
    const h = document.getElementById('pf-' + g);
    if (h) setTimeout(() => { h.focus(); h.scrollIntoView({ block: 'start' }); }, 80);
  }, [route && route.params && route.params.section, !!res.data]);
  const put = (k, val) => setDraftState((d) => Object.assign({}, d, { [k]: val }));
  const v = (k, saved) => (k in draft ? draft[k] : saved);
  const clear = (keys) => setDraftState((d) => { const n = Object.assign({}, d); keys.forEach((k) => { delete n[k]; }); return n; });
  // a change: its title, the settings it sets (or a promise of them, when it needs the unmasked values) and the drafts
  // it settles once saved
  const propose = (title, changes, keys) => Promise.resolve(changes).then((c) => { setPending({ title, changes: c, keys: keys || [] }); },
    (e) => { toast(e.message || String(e), 'bad'); });
  const d = res.data;
  const view = (dd) => {
    const s = reveal && shown.data ? shown.data : dd;
    return { email: s.email, aliases: s.aliases, accounts: s.accounts, candidates: s.candidates };
  };
  return html`<${Desk} className="profile-desk">
    <${PageHead} title="Profile" sub=${(d && d.display_name ? d.display_name + ' · ' : '') + 'whose work Chitragupta tracks, and why'}>
      <button type="button" class="btn" aria-pressed=${reveal ? 'true' : 'false'} onClick=${() => setReveal(!reveal)}>
        <${Icon} name=${reveal ? 'off' : 'search'} size=${15} />${reveal ? 'Hide emails and ids' : 'Show emails and ids'}</button></${PageHead}>
    <${Load} res=${res} lines=${10}>${(dd) => html`
      <p class="lede">Commits, pull requests and tickets count as yours only through the identities you confirm here.
        Emails and account ids stay hidden on screen unless you show them; they never appear in progress summaries, copies or exports.</p>
      <nav class="pfjump" aria-label="Profile sections"><a href="#/profile?section=identity">Identity</a><a href="#/profile?section=scope">Scope</a><a href="#/profile?section=prefs">Preferences and tracking</a></nav>
      <${IdentityGroup} d=${dd} show=${view(dd)} v=${v} put=${put} propose=${propose} errs=${errs} dirty=${groupDirty(draft, 'identity')} discard=${() => clear(GROUP_KEYS.identity)} onFound=${() => { res.reload(); if (reveal) shown.reload(); }} />
      <${ScopeGroup} d=${dd} v=${v} put=${put} propose=${propose} errs=${errs} dirty=${groupDirty(draft, 'scope')} discard=${() => clear(GROUP_KEYS.scope)} />
      <${PrefsGroup} d=${Object.assign({ _draft: draft }, dd)} v=${v} put=${put} propose=${propose} errs=${errs} dirty=${groupDirty(draft, 'prefs')} discard=${() => clear(GROUP_KEYS.prefs)} />`}</${Load}>
    <${Review} pending=${pending} onClose=${() => setPending(null)} setErrs=${setErrs}
      onSaved=${(p) => { clear(p.keys || []); setPending(null); res.reload(); if (reveal) shown.reload(); }} />
  </${Desk}>`;
}
