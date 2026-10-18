// Settings: every setting from the server's schema, grouped; edits collect into a review (old → new) before "Save
// changes"; saves go through the same write path as /cg:setup and are recorded, and the last one can be undone.
// Appearance is per browser. Locked items (safety rules, MCP tools, secrets, data folder, GitHub repo) are only shown.
import { useEffect, useMemo, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useStore, useApi, write, toast, fmtTs } from '../lib/core.js';
import { Icon, Status, PageHead, Dialog, Load, Empty, themeMode, Scroll } from '../lib/ui.js';
import { Desk, pageKeys, appearance } from '../lib/shell.js';
import { McpStatusPanel } from '../lib/mcpstatus.js';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const TIME = /^([01]\d|2[0-3]):[0-5]\d$/;
const PREFIX = /^[A-Z][A-Z0-9]{1,9}$/;

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
    default: return null;
  }
}
function show(v) {
  if (v === null || v === undefined || v === '') return '(not set)';
  if (typeof v === 'boolean') return v ? 'On' : 'Off';
  if (Array.isArray(v)) return v.length ? v.join(', ') : '(none)';
  return String(v);
}
function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

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
  return html`<div class=${'setrow' + (changed ? ' changed' : '')}>
    <div class="setlab"><label for=${it.type === 'days' || it.type === 'envs_narrow' || !it.editable ? null : id} id=${id + '-lab'}>${it.label}</label>
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
  const rail = d ? html`<h2>Groups</h2><ul class="rlist">${d.groups.concat([{ id: 'mcp', label: 'MCP status' }, { id: 'appearance', label: 'Appearance' }, { id: 'history', label: 'Change history' }]).map((g) => html`<li key=${g.id}>
    <a href=${'#g-' + g.id} onClick=${(e) => { e.preventDefault(); const el = document.getElementById('g-' + g.id); if (el) { el.scrollIntoView({ block: 'start' }); el.querySelector('h2').focus(); } }}>
      <span class="sw" style=${{ background: g.items && g.items.some((it) => keys.indexOf(it.key) >= 0) ? 'var(--accent)' : 'var(--rule)' }}></span>
      <span class="k" style="font-weight:500">${g.label}</span><span class="m"></span></a></li>`)}</ul>` : null;
  return html`<${Desk} rail=${rail} railLabel="Setting groups">
    <${PageHead} title="Settings" sub="Changes go through the same checks and write path as /cg:setup, and each one is recorded">
      ${keys.length ? html`<button type="button" class="btn quiet" onClick=${() => { setPending({}); setErrors({}); }}>Discard</button>
        <button type="button" class="btn primary" disabled=${!!local.length} onClick=${() => setReview(true)}>Review ${keys.length} change${keys.length === 1 ? '' : 's'}</button>` : null}
    </${PageHead}>
    <p class="sr" aria-live="polite">${keys.length ? keys.length + ' unsaved changes' : ''}</p>
    <${Load} res=${res} lines=${12}>${(d) => html`
      ${d.groups.map((g) => html`<section class="sec setgroup" key=${g.id} id=${'g-' + g.id} aria-labelledby=${'h-' + g.id}>
        <h2 id=${'h-' + g.id} tabindex="-1">${g.label}</h2>
        ${g.items.map((it) => html`<${Item} key=${it.key} it=${it} value=${it.key in pending ? pending[it.key] : it.value}
          onChange=${(v) => set(it.key, v)} err=${errors[it.key]} />`)}</section>`)}
      <section class="sec setgroup" id="g-mcp" aria-labelledby="h-mcp"><h2 id="h-mcp" tabindex="-1">MCP status</h2>
        ${st.data ? html`<${McpStatusPanel} mc=${st.data.mcp || {}} onChecked=${st.reload} headingLevel=${3} />` : null}</section>
      <${Appearance} />
      <${History} rows=${d.history || []} onUndo=${undo} busy=${busy} />`}</${Load}>
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
