// The Share dialog on a ticket: pick sections, level and target, Preview (writes nothing: what each section loses,
// what is never included, the scan), then Share (the share-ticket action; written only when the scan passes).
import { useEffect, useState } from '/vendor/hooks.module.js';
import { html } from './html.js';
import { api, runAction, toast } from './core.js';
import { Icon, Status, Dialog, Markdown, Empty, CopyButton } from './ui.js';

export function ShareButton({ k }) {
  const [open, setOpen] = useState(false);
  return html`<button type="button" class="btn quiet" onClick=${() => setOpen(true)} title="Share picked sections with your team, redacted">
      <${Icon} name="external" size=${16} />Share…</button>
    <${Dialog} open=${open} title=${'Share ' + k + ' with your team'} onClose=${() => setOpen(false)} wide>
      ${open ? html`<${ShareForm} k=${k} onDone=${() => setOpen(false)} />` : null}</${Dialog}>`;
}

function ShareForm({ k, onDone }) {
  const [info, setInfo] = useState(null);
  const [err, setErr] = useState(null);
  const [picked, setPicked] = useState([]);
  const [level, setLevel] = useState('team');
  const [target, setTarget] = useState('');
  const [pv, setPv] = useState(null);
  const [busy, setBusy] = useState('');
  useEffect(() => {
    api('/api/share').then((d) => {
      setInfo(d);
      setPicked(d.sections.filter((s) => s.default).map((s) => s.id));
      setLevel(d.default_level);
      const ok = d.targets.find((t) => !t.problem);
      setTarget(ok ? ok.name : '');
    }, setErr);
  }, [k]);
  const opts = JSON.stringify([picked, level, target]);
  useEffect(() => { setPv(null); }, [opts]);
  if (err) return html`<p class="field-err">${err.message}</p>`;
  if (!info) return html`<p class="muted">Loading…</p>`;
  const usable = info.targets.filter((t) => !t.problem);
  if (!usable.length) {
    return html`<${Empty} title="No share target yet" icon="info">Add one in Settings → Team sharing (an existing folder your team syncs, or a team Git repo).
      ${info.targets.length ? html`<br />${info.targets.map((t) => html`<span key=${t.name}>${t.name}: ${t.problem}. </span>`)}` : null}</${Empty}>`;
  }
  const preview = () => {
    setBusy('preview');
    api('/api/share/preview?key=' + encodeURIComponent(k) + '&sections=' + encodeURIComponent(picked.join(',')) + '&level=' + level)
      .then((d) => setPv(Object.assign({ opts }, d)), (e) => toast(e.message, 'bad')).finally(() => setBusy(''));
  };
  const share = () => {
    setBusy('share');
    runAction('share-ticket', { key: k, sections: picked, level, target }).then((r) => {
      if (r.ok) { toast('Shared to ' + r.target + (r.committed ? (r.pushed ? ' and pushed' : '; the push failed: ' + r.push_error) : ''), r.pushed === false && r.committed ? 'info' : 'ok'); onDone(); }
      else setPv(Object.assign({}, pv, { blocked: true, hits: r.hits || [], problems: r.problems || [] }));
    }, (e) => toast(e.message, 'bad')).finally(() => setBusy(''));
  };
  const toggle = (id) => setPicked(picked.indexOf(id) >= 0 ? picked.filter((x) => x !== id) : picked.concat([id]));
  return html`<div class="shareform">
    <fieldset class="daypick"><legend>Sections</legend>${info.sections.map((s) => html`<label key=${s.id} class="check">
      <input type="checkbox" checked=${picked.indexOf(s.id) >= 0} onChange=${() => toggle(s.id)} />${s.label}</label>`)}</fieldset>
    <div class="btnline">
      <label class="filt"><span class="small">Level</span><select value=${level} onChange=${(e) => setLevel(e.currentTarget.value)}>
        ${info.levels.map((l) => html`<option key=${l} value=${l}>${l === 'team' ? 'team (SQL, hosts, internal names stay)' : 'public (those removed too)'}</option>`)}</select></label>
      <label class="filt"><span class="small">Target</span><select value=${target} onChange=${(e) => setTarget(e.currentTarget.value)}>
        ${usable.map((t) => html`<option key=${t.name} value=${t.name}>${t.name} (${t.kind === 'git' ? 'Git repo' : 'folder'})</option>`)}</select></label></div>
    <p class="muted small">Never included: ${info.never.join(', ')}. Sharing never posts to Slack.</p>
    <div class="btnline"><button type="button" class="btn" disabled=${!picked.length || !!busy} onClick=${preview}>${busy === 'preview' ? 'Building…' : 'Preview'}</button>
      <button type="button" class="btn primary" disabled=${!pv || pv.opts !== opts || pv.blocked || !!busy} onClick=${share}>${busy === 'share' ? 'Sharing…' : 'Share'}</button>
      ${!pv ? html`<span class="muted small">Preview first; nothing is written until you press Share.</span>` : null}</div>
    ${pv ? html`<div class="sharepv" aria-live="polite">
      ${pv.blocked ? html`<div class="field-err" role="alert"><p><strong>Blocked: nothing will be written.</strong></p><ul>
          ${(pv.problems || []).map((p, i) => html`<li key=${'p' + i}>${p}</li>`)}
          ${(pv.hits || []).map((h, i) => html`<li key=${'h' + i}>${h.section}, ${h.file} line ${h.line}: ${h.kind} (${h.rule})</li>`)}</ul>
          <p>Leave that section out, or change the text in the ticket's notes, and preview again.</p></div>`
        : html`<p><${Status} kind="ok">The scan passed</${Status}></p>`}
      <h3>Removed</h3>
      <ul>${Object.keys(pv.removed_text || {}).filter((s) => pv.removed_text[s]).map((s) => html`<li key=${s}><strong>${s}:</strong> ${pv.removed_text[s]}</li>`)}
        ${!Object.keys(pv.removed_text || {}).some((s) => pv.removed_text[s]) ? html`<li class="muted">Nothing needed removing.</li>` : null}</ul>
      <details class="more"><summary>The README as it will be shared</summary><${Markdown} text=${pv.markdown.replace(/^<!-- cg-share .*?-->\n/, '')} keepTitle /></details></div>` : null}
  </div>`;
}

// Team notes: one bundle's README, with the path of its ticket.html to open.
export function TeamNote({ target, dir }) {
  const [st, setSt] = useState({ data: null, error: null });
  useEffect(() => {
    if (!dir) return;
    setSt({ data: null, error: null });
    api('/api/team-notes/note?target=' + encodeURIComponent(target) + '&dir=' + encodeURIComponent(dir))
      .then((data) => setSt({ data, error: null }), (error) => setSt({ data: null, error }));
  }, [target, dir]);
  if (st.error) return html`<p class="field-err">${st.error.message}</p>`;
  if (!st.data) return html`<p class="muted">Loading…</p>`;
  return html`<div>${st.data.html_file ? html`<p class="small" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">The full page: <code>${st.data.html_file}</code>
      <${CopyButton} text=${st.data.html_file} what="the file path" quiet /></p>` : null}
    <${Markdown} text=${st.data.markdown.replace(/^<!-- cg-share .*?-->\n/, '')} keepTitle /></div>`;
}
