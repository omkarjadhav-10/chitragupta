// Settings: every setting from the server's schema, grouped; edits collect into a review (old → new) before "Save
// changes"; saves go through the same write path as /cg:setup and are recorded, and the last one can be undone.
// Appearance is per browser. Locked items (safety rules, MCP tools, secrets, data folder, GitHub repo) are only shown.
import { useEffect, useMemo, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useStore, useApi, write, runAction, toast, fmtTs } from '../lib/core.js';
import { Icon, Status, PageHead, Dialog, Load, Empty, themeMode, Scroll } from '../lib/ui.js';
import { Desk, pageKeys, appearance } from '../lib/shell.js';
import { McpStatusPanel } from '../lib/mcpstatus.js';
import { SchedulerPanel } from '../lib/scheduler.js';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const TIME = /^([01]\d|2[0-3]):[0-5]\d$/;
const PREFIX = /^[A-Z][A-Z0-9]{1,9}$/;
const BRANCH = /^[A-Za-z0-9*][A-Za-z0-9._\/*-]{0,99}$/;
const REL = { cut_from: 'is cut from', merges_into: 'merges into', syncs_from: 'syncs from' };
function words(s) { return String(s || '').split(',').map((x) => x.trim()).filter(Boolean); }

// The same rules the server applies (chitragupta/settings.py check); the server stays the judge.
function problem(it, v) {
  switch (it.type) {
    case 'bool': return typeof v === 'boolean' ? null : 'Choose on or off.';
    case 'int': case 'number':
      if (typeof v !== 'number' || isNaN(v) || (it.type === 'int' && !Number.isInteger(v))) return it.type === 'int' ? 'Enter a whole number.' : 'Enter a number.';
      return v < it.min || v > it.max ? 'Use a value from ' + it.min + ' to ' + it.max + '.' : null;
    case 'time':
      if (!TIME.test(v || '')) return 'Use 24-hour time, for example 20:00.';
      return (it.min && v < it.min) || (it.max && v > it.max) ? 'Use a time from ' + it.min + ' to ' + it.max + '.' : null;
    case 'choice': return it.choices.some((c) => c[0] === v) ? null : 'Pick one of the listed options.';
    case 'days': return Array.isArray(v) && v.length ? null : 'Pick at least one day.';
    case 'prefixes': return (v || []).every((p) => PREFIX.test(p)) ? null : 'Use uppercase project keys of 2 to 10 letters or digits, starting with a letter.';
    case 'paths': return (v || []).every((p) => /^([A-Za-z]:[\\/]|\/)/.test(p)) ? null : 'Use full paths, one per line.';
    case 'envs_narrow': return (v || []).length ? null : 'Keep at least one environment.';
    case 'name': return typeof v === 'string' && v.length <= 80 && !/[\x00-\x1f]/.test(v) ? null : 'Use one line of at most 80 characters.';
    case 'share_targets': {
      const names = new Set();
      for (let i = 0; i < (v || []).length; i++) {
        const t = v[i];
        if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/.test(t.name || '')) return 'Target ' + (i + 1) + ': a name of letters, digits, - or _.';
        if (names.has(t.name)) return 'Target ' + (i + 1) + ': the name ' + t.name + ' is used twice.';
        names.add(t.name);
        if (t.kind === 'folder' && !/^([A-Za-z]:[\\/]|\/)/.test(t.path || '')) return 'Target ' + (i + 1) + ': a full folder path.';
        if (t.kind === 'git' && !(t.url || '').trim()) return 'Target ' + (i + 1) + ': the repo URL.';
        if (/slack/i.test((t.url || '') + (t.path || ''))) return 'Target ' + (i + 1) + ': sharing never posts to Slack.';
      }
      return null;
    }
    case 'bases': {
      const bad = Object.keys(v || {}).find((r) => !(v[r] || []).length || v[r].some((p) => !BRANCH.test(p)));
      return bad ? bad + ': use branch names or patterns (letters, digits, . _ / * -), separated by commas.' : null;
    }
    case 'relations': {
      if ((v || []).length > 30) return 'Use at most 30 relations.';
      for (let i = 0; i < (v || []).length; i++) {
        const r = v[i];
        if (!r.from || !BRANCH.test(r.from)) return 'Relation ' + (i + 1) + ': give the branch or pattern it starts from.';
        if (!(r.to || []).length || r.to.some((t) => !BRANCH.test(t))) return 'Relation ' + (i + 1) + ': give the branch or branches it relates to.';
        if (r.to.indexOf(r.from) >= 0) return 'Relation ' + (i + 1) + ': ' + r.from + " can't relate to itself.";
      }
      return null;
    }
    default: return null;
  }
}
function show(v) {
  if (v === null || v === undefined || v === '') return '(not set)';
  if (typeof v === 'boolean') return v ? 'On' : 'Off';
  if (Array.isArray(v) && v.length && typeof v[0] === 'object') return v.map((r) => r.from + ' ' + (REL[r.rel] || r.rel) + ' ' + (r.to || []).join(', ')).join('; ');
  if (Array.isArray(v)) return v.length ? v.join(', ') : '(none)';
  if (typeof v === 'object') return Object.keys(v).map((k) => k + ': ' + [].concat(v[k]).join(', ')).join('; ');
  return String(v);
}
function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }
function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// A comma-separated list in a text field that keeps what you type (a trailing comma, spaces) while it reports the list.
function ListInput({ value, onChange, ...rest }) {
  const [txt, setTxt] = useState((value || []).join(', '));
  useEffect(() => { if (!same(words(txt), value || [])) setTxt((value || []).join(', ')); }, [JSON.stringify(value || [])]);
  return html`<input type="text" spellcheck="false" autocomplete="off" ...${rest} value=${txt}
    onInput=${(e) => { setTxt(e.currentTarget.value); onChange(words(e.currentTarget.value)); }} />`;
}

function BasesEditor({ it, value, onChange, common }) {
  const v = value || {};
  return html`<fieldset class="baseedit" ...${common}><legend class="sr">${it.label}</legend>
    ${it.roles.map((r) => html`<label key=${r} class="baserow"><span class="small">${r}</span>
      <${ListInput} class="mono" value=${[].concat(v[r] || [])} aria-label=${it.repo + ' ' + r + ' branch names'}
        onChange=${(list) => onChange(Object.assign({}, v, { [r]: list }))} /></label>`)}</fieldset>`;
}

function ShareTargetsEditor({ it, value, onChange, common }) {
  const rows = value || [];
  const put = (i, patch) => onChange(rows.map((r, j) => {
    if (j !== i) return r;
    const n = Object.assign({}, r, patch);
    if (n.kind === 'folder') { delete n.url; delete n.branch; } else { delete n.path; }
    Object.keys(n).forEach((k) => { if (n[k] === '' && k !== 'name') delete n[k]; });
    return n;
  }));
  const lab = (i, f) => 'Share target ' + (i + 1) + ': ' + f;
  return html`<fieldset class="reledit" ...${common}><legend class="sr">${it.label}</legend>
    ${rows.length ? html`<ol class="targetlist">${rows.map((r, i) => html`<li key=${i}>
      <input type="text" class="mono" aria-label=${lab(i, 'name')} placeholder="name" value=${r.name || ''} onInput=${(e) => put(i, { name: e.currentTarget.value.trim() })} />
      <select aria-label=${lab(i, 'kind')} value=${r.kind} onChange=${(e) => put(i, { kind: e.currentTarget.value })}>
        <option value="folder">Folder</option><option value="git">Git repo</option></select>
      ${r.kind === 'git'
        ? html`<input type="text" class="mono" aria-label=${lab(i, 'repo URL')} placeholder="repo URL" value=${r.url || ''} onInput=${(e) => put(i, { url: e.currentTarget.value.trim() })} />
          <input type="text" class="mono" aria-label=${lab(i, 'branch')} placeholder="main" value=${r.branch || ''} onInput=${(e) => put(i, { branch: e.currentTarget.value.trim() })} />`
        : html`<input type="text" class="mono wide" aria-label=${lab(i, 'folder')} placeholder="full folder path" value=${r.path || ''} onInput=${(e) => put(i, { path: e.currentTarget.value.trim() })} />`}
      <button type="button" class="btn quiet icon" aria-label=${'Remove share target ' + (i + 1)} onClick=${() => onChange(rows.filter((_, j) => j !== i))}><${Icon} name="close" size=${15} /></button></li>`)}</ol>`
      : html`<p class="muted small">None yet: /cg:share needs one.</p>`}
    <div class="btnline"><button type="button" class="btn" onClick=${() => onChange(rows.concat([{ name: rows.length ? 'target' + (rows.length + 1) : 'team', kind: 'folder', path: '' }]))}>
      <${Icon} name="plus" size=${15} />Add a target</button></div></fieldset>`;
}

function RelationsEditor({ it, value, onChange, common }) {
  const rows = value || [];
  const [found, setFound] = useState(null);
  const [busy, setBusy] = useState(false);
  const put = (i, patch) => onChange(rows.map((r, j) => (j === i ? Object.assign({}, r, patch) : r)));
  const detect = () => {
    setBusy(true);
    runAction('relations-detect', { repo: it.repo }).then((r) => setFound(r.proposals), (e) => toast(e.message, 'bad')).finally(() => setBusy(false));
  };
  const n = (i) => it.repo + ' relation ' + (i + 1);
  return html`<fieldset class="reledit" ...${common}><legend class="sr">${it.label}</legend>
    ${rows.length ? html`<ol class="rellist">${rows.map((r, i) => html`<li key=${i}>
      <${ListInput} class="mono" value=${r.from ? [r.from] : []} aria-label=${n(i) + ': from'} onChange=${(l) => put(i, { from: l.join(',') })} />
      <select aria-label=${n(i) + ': kind'} value=${r.rel} onChange=${(e) => put(i, { rel: e.currentTarget.value })}>
        ${it.kinds.map((k) => html`<option key=${k[0]} value=${k[0]}>${k[1]}</option>`)}</select>
      <${ListInput} class="mono" value=${r.to || []} aria-label=${n(i) + ': to'} onChange=${(l) => put(i, { to: l })} />
      <button type="button" class="btn quiet icon" aria-label=${'Remove ' + n(i)} onClick=${() => onChange(rows.filter((_, j) => j !== i))}><${Icon} name="close" size=${15} /></button></li>`)}</ol>`
      : html`<p class="muted small">None: the graph forks every base from the first one.</p>`}
    <div class="btnline"><button type="button" class="btn" onClick=${() => onChange(rows.concat([{ from: '', rel: 'merges_into', to: [] }]))}><${Icon} name="plus" size=${15} />Add a relation</button>
      <button type="button" class="btn" disabled=${busy} onClick=${detect}><${Icon} name="search" size=${15} />${busy ? 'Reading the repo…' : 'Detect from the repo'}</button></div>
    ${found ? html`<div class="note" role="status"><p><strong>${found.length ? 'Detected from local branches (read-only, no fetch):' : 'Nothing could be detected from the local branches.'}</strong></p>
      ${found.length ? html`<ul>${found.map((p, i) => html`<li key=${i}><span class="mono">${p.from}</span> ${REL[p.rel] || p.rel} <span class="mono">${[].concat(p.to).join(', ')}</span>
        <span class="muted small"> · ${p.evidence}</span></li>`)}</ul>
        <button type="button" class="btn" onClick=${() => { onChange(found.map((p) => ({ from: p.from, rel: p.rel, to: [].concat(p.to) }))); setFound(null); }}>Use these (review before saving)</button>` : null}</div>` : null}
  </fieldset>`;
}

function Control({ it, value, onChange, err }) {
  const id = 'set-' + it.key.replace(/[^a-z0-9]/gi, '-');
  const descr = (it.help ? id + '-help ' : '') + (err ? id + '-err' : '');
  const common = { id, 'aria-describedby': descr.trim() || null, 'aria-invalid': err ? 'true' : null };
  if (!it.editable) {
    return html`<span class="locked"><${Icon} name="off" size=${15} />${show(value)}</span>`;
  }
  const off = it.available === false;
  switch (it.type) {
    case 'bool':
      return html`<button type="button" role="switch" class=${'switch' + (value ? ' on' : '')} aria-checked=${value ? 'true' : 'false'}
        disabled=${off} ...${common} onClick=${() => onChange(!value)}><span class="knob" aria-hidden="true"></span>${value ? 'On' : 'Off'}</button>`;
    case 'int': case 'number':
      return html`<input type="number" ...${common} min=${it.min} max=${it.max} step=${it.type === 'int' ? 1 : 0.5} value=${value == null ? '' : value}
        onInput=${(e) => onChange(e.currentTarget.value === '' ? null : Number(e.currentTarget.value))} />`;
    case 'time':
      return html`<input type="time" ...${common} value=${value || ''} onInput=${(e) => onChange(e.currentTarget.value)} />`;
    case 'choice':
      return html`<select ...${common} value=${value == null ? '' : value} onChange=${(e) => onChange(e.currentTarget.value)}>
        ${it.choices.map((c) => html`<option key=${c[0]} value=${c[0]}>${c[1]}</option>`)}</select>`;
    case 'days':
      return html`<fieldset class="daypick" ...${common}><legend class="sr">${it.label}</legend>${DAYS.map((d) => html`<label key=${d} class="check">
        <input type="checkbox" checked=${(value || []).indexOf(d) >= 0} onChange=${(e) => onChange(DAYS.filter((x) => (x === d ? e.currentTarget.checked : (value || []).indexOf(x) >= 0)))} />${d}</label>`)}</fieldset>`;
    case 'envs_narrow':
      return html`<fieldset class="daypick" ...${common}><legend class="sr">${it.label}</legend>${(it.value || []).map((env) => html`<label key=${env} class="check">
        <input type="checkbox" checked=${(value || []).indexOf(env) >= 0} onChange=${(e) => onChange((it.value || []).filter((x) => (x === env ? e.currentTarget.checked : (value || []).indexOf(x) >= 0)))} />${env}</label>`)}</fieldset>`;
    case 'bases': return html`<${BasesEditor} it=${it} value=${value} onChange=${onChange} common=${common} />`;
    case 'share_targets': return html`<${ShareTargetsEditor} it=${it} value=${value} onChange=${onChange} common=${common} />`;
    case 'name':
      return html`<input type="text" ...${common} maxlength="80" value=${value || ''} onInput=${(e) => onChange(e.currentTarget.value)} />`;
    case 'relations': return html`<${RelationsEditor} it=${it} value=${value} onChange=${onChange} common=${common} />`;
    case 'prefixes': case 'paths':
      return html`<textarea ...${common} rows=${Math.min(6, Math.max(2, (value || []).length + 1))} spellcheck="false"
        onInput=${(e) => onChange(e.currentTarget.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean))}>${(value || []).join('\n')}</textarea>`;
    default:
      return html`<span>${show(value)}</span>`;
  }
}

function Item({ it, value, onChange, err }) {
  const id = 'set-' + it.key.replace(/[^a-z0-9]/gi, '-');
  const changed = !same(value, it.value);
  return html`<div class=${'setrow' + (changed ? ' changed' : '') + (it.type === 'relations' || it.type === 'share_targets' ? ' stack' : '')}>
    <div class="setlab"><label for=${['days', 'envs_narrow', 'bases', 'relations', 'share_targets'].indexOf(it.type) >= 0 || !it.editable ? null : id} id=${id + '-lab'}>${it.repo ? cap(it.label.slice(it.repo.length + 2)) : it.label}</label>
      ${it.help ? html`<p class="muted small" id=${id + '-help'}>${it.help}</p>` : null}
      ${it.available === false ? html`<p class="small"><${Status} kind="off" quiet>Set this up with /cg:setup first</${Status}></p>` : null}
      ${!it.editable ? html`<p class="small muted">Change with /cg:setup.</p>` : null}</div>
    <div class="setctl"><${Control} it=${it} value=${value} onChange=${onChange} err=${err} />
      ${changed ? html`<span class="small muted">was ${show(it.value)}</span>` : null}
      ${err ? html`<p class="field-err" id=${id + '-err'} role="alert"><${Icon} name="bad" size=${14} />${err}</p>` : null}</div>
  </div>`;
}

function Choice({ legend, value, options, onChange, hint }) {
  return html`<div class="setrow"><div class="setlab"><span id=${'ap-' + legend}>${legend}</span>${hint ? html`<p class="muted small">${hint}</p>` : null}</div>
    <div class="setctl"><div class="seg" role="radiogroup" aria-labelledby=${'ap-' + legend} onKeyDown=${(e) => {
      const i = options.findIndex(([v]) => v === value);
      const d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const n = (i + d + options.length) % options.length;
      onChange(options[n][0]);
      const btns = e.currentTarget.querySelectorAll('[role="radio"]');
      setTimeout(() => btns[n] && btns[n].focus(), 0);
    }}>${options.map(([v, l]) => html`<button key=${v} type="button" role="radio" aria-checked=${value === v ? 'true' : 'false'}
      tabindex=${value === v ? 0 : -1} onClick=${() => onChange(v)}>${l}</button>`)}</div></div></div>`;
}

function Appearance() {
  const theme = useStore(themeMode);
  const density = document.documentElement.getAttribute('data-density') || 'comfortable';
  const motion = document.documentElement.getAttribute('data-motion') || 'system';
  const rerender = () => themeMode.set(themeMode.get());
  return html`<section class="sec setgroup" id="g-appearance" aria-labelledby="h-appearance"><h2 id="h-appearance">Appearance</h2>
    <p class="muted small">Saved in this browser only; takes effect at once.</p>
    <${Choice} legend="Theme" value=${theme} options=${[['system', 'System'], ['light', 'Light'], ['dark', 'Dark']]} onChange=${appearance.setTheme} />
    <${Choice} legend="Density" value=${density} options=${[['comfortable', 'Comfortable'], ['compact', 'Compact']]} onChange=${(v) => { appearance.setDensity(v); rerender(); }} />
    <${Choice} legend="Motion" value=${motion} hint="Reduce turns off the ring animation and loading shimmer."
      options=${[['system', 'System'], ['reduce', 'Reduce'], ['full', 'Full']]} onChange=${(v) => { appearance.setMotion(v); rerender(); }} /></section>`;
}

function History({ rows, onUndo, busy }) {
  const mine = rows.filter((r) => r.source === 'dashboard' && !r.undo_of && r.kind !== 'action');
  return html`<section class="sec setgroup" id="g-history" aria-labelledby="h-history"><div class="sec-head"><h2 id="h-history">Change history</h2><span class="grow"></span>
    <button type="button" class="btn" disabled=${!mine.length || busy} onClick=${onUndo}><${Icon} name="reset" size=${16} />Undo last change</button></div>
    <p class="muted small">Every change from this page, from /cg:setup and from the engine is recorded in settings-history.jsonl in your Chitragupta folder.</p>
    ${rows.length ? html`<${Scroll} label="Change history"><table><caption class="sr">Recent changes</caption>
      <thead><tr><th scope="col">When</th><th scope="col">By</th><th scope="col">Setting</th><th scope="col">Change</th></tr></thead>
      <tbody>${rows.map((r, i) => html`<tr key=${i}><td class="nowrap">${fmtTs(r.at)}</td><td>${r.undo_of ? 'undo' : r.source}</td>
        <td class="mono small">${r.key}</td><td>${r.kind === 'action' ? r.new : show(r.old) + ' → ' + show(r.new)}</td></tr>`)}</tbody></table></${Scroll}>`
      : html`<${Empty} title="No changes recorded yet" icon="settings" />`}</section>`;
}

export function SettingsPage() {
  const res = useApi('/api/settings');
  const st = useApi('/api/status');
  const [pending, setPending] = useState({});
  const [errors, setErrors] = useState({});
  const [review, setReview] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { document.title = 'Settings · Chitragupta'; pageKeys.set({}); }, []);
  const d = res.data;
  const items = useMemo(() => {
    const m = {};
    ((d && d.groups) || []).forEach((g) => g.items.forEach((it) => { m[it.key] = it; }));
    return m;
  }, [d]);
  const keys = Object.keys(pending).filter((k) => items[k] && !same(pending[k], items[k].value));
  const set = (k, v) => {
    setPending((p) => Object.assign({}, p, { [k]: v }));
    setErrors((e) => Object.assign({}, e, { [k]: problem(items[k], v) }));
  };
  const local = keys.map((k) => [k, problem(items[k], pending[k])]).filter((x) => x[1]);
  const save = () => {
    setBusy(true);
    const changes = {};
    keys.forEach((k) => { changes[k] = pending[k] === '' ? null : pending[k]; });
    write('PATCH', '/api/settings', { changes }).then((r) => {
      setPending({}); setErrors({}); setReview(false);
      toast('Saved ' + r.changes.length + ' change' + (r.changes.length === 1 ? '' : 's'), 'ok');
      res.reload();
    }, (e) => {
      setReview(false);
      if (e.fields) setErrors(Object.assign({}, errors, e.fields));
      toast(e.message, 'bad');
    }).finally(() => setBusy(false));
  };
  const undo = () => {
    setBusy(true);
    write('POST', '/api/settings/undo', {}).then(() => { toast('Undid the last change', 'ok'); res.reload(); },
      (e) => toast(e.message, 'bad')).finally(() => setBusy(false));
  };
  const rail = d ? html`<h2>Groups</h2><ul class="rlist">${d.groups.concat([{ id: 'mcp', label: 'MCP status' }, { id: 'scheduler', label: 'Background job' }, { id: 'appearance', label: 'Appearance' }, { id: 'history', label: 'Change history' }]).map((g) => html`<li key=${g.id}>
    <a href=${'#g-' + g.id} onClick=${(e) => { e.preventDefault(); const el = document.getElementById('g-' + g.id); if (el) { el.scrollIntoView({ block: 'start' }); el.querySelector('h2').focus(); } }}>
      <span class="sw" style=${{ background: g.items && g.items.some((it) => keys.indexOf(it.key) >= 0) ? 'var(--accent)' : 'var(--rule)' }}></span>
      <span class="k" style="font-weight:500">${g.label}</span><span class="m"></span></a></li>`)}</ul>` : null;
  return html`<${Desk} rail=${rail} railLabel="Setting groups">
    <${PageHead} title="Settings" sub="Choose how your work is recorded and your daily worklog is prepared." />
    <p class="sr" aria-live="polite">${keys.length ? keys.length + ' unsaved changes' : ''}</p>
    <${Load} res=${res} lines=${12}>${(d) => html`
      ${d.groups.map((g) => html`<section class="sec setgroup" key=${g.id} id=${'g-' + g.id} aria-labelledby=${'h-' + g.id}>
        <h2 id=${'h-' + g.id} tabindex="-1">${g.label}</h2>
        ${g.items.filter((it) => !it.repo).map((it) => html`<${Item} key=${it.key} it=${it} value=${it.key in pending ? pending[it.key] : it.value}
          onChange=${(v) => set(it.key, v)} err=${errors[it.key]} />`)}
        ${[...new Set(g.items.filter((it) => it.repo).map((it) => it.repo))].map((repo) => html`<div class="repobox" key=${repo} role="group" aria-labelledby=${'repo-' + repo}>
          <h3 id=${'repo-' + repo} class="mono">${repo}</h3>
          ${g.items.filter((it) => it.repo === repo).map((it) => html`<${Item} key=${it.key} it=${it} value=${it.key in pending ? pending[it.key] : it.value}
            onChange=${(v) => set(it.key, v)} err=${errors[it.key]} />`)}</div>`)}</section>`)}
      <section class="sec setgroup" id="g-mcp" aria-labelledby="h-mcp"><h2 id="h-mcp" tabindex="-1">MCP status</h2>
        ${st.data ? html`<${McpStatusPanel} mc=${st.data.mcp || {}} onChecked=${st.reload} headingLevel=${3} />` : null}</section>
      <section class="sec setgroup" id="g-scheduler" aria-labelledby="h-scheduler"><h2 id="h-scheduler" tabindex="-1">Background job</h2>
        <p class="muted small">Status and Repair only: setup registers it, uninstall removes it.</p><${SchedulerPanel} /></section>
      <${Appearance} />
      <${History} rows=${d.history || []} onUndo=${undo} busy=${busy} />`}</${Load}>
    ${keys.length ? html`<div class="settings-save" role="region" aria-label="Unsaved changes"><div><strong>${keys.length} unsaved change${keys.length === 1 ? '' : 's'}</strong>
      <p class="muted small">Review your changes before saving.</p></div><span class="grow"></span>
      <button type="button" class="btn quiet" onClick=${() => { setPending({}); setErrors({}); }}>Discard</button>
      <button type="button" class="btn primary" disabled=${!!local.length} onClick=${() => setReview(true)}>Review ${keys.length} change${keys.length === 1 ? '' : 's'}</button></div>` : null}
    <${Dialog} open=${review} title="Review changes" onClose=${() => setReview(false)} wide>
      <p>These settings change when you save. Nothing is saved until then.</p>
      <${Scroll} label="Changes to save"><table><caption class="sr">Changes to save</caption><thead><tr><th scope="col">Setting</th><th scope="col">Now</th><th scope="col">After saving</th></tr></thead>
        <tbody>${keys.map((k) => html`<tr key=${k}><td>${items[k].label}</td><td>${show(items[k].value)}</td><td><strong>${show(pending[k])}</strong></td></tr>`)}</tbody></table></${Scroll}>
      <div class="pagehead-act" style="margin-top:16px;justify-content:flex-end">
        <button type="button" class="btn" onClick=${() => setReview(false)}>Keep editing</button>
        <button type="button" class="btn primary" disabled=${busy} onClick=${save}>${busy ? 'Saving…' : 'Save changes'}</button></div>
    </${Dialog}>
  </${Desk}>`;
}
