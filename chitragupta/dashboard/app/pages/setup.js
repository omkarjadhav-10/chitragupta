// Setup: the first-run and repair checklist, mirroring /cg:setup and /cg:doctor. Each step says what's wrong and what to
// run; steps only Claude Code can do (MCP sign-in) show as instructions to copy.
import { useEffect } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, fmtTs, plural } from '../lib/core.js';
import { Status, CopyButton, Load, PageHead } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

const WORD = { ok: 'Done', warn: 'Needs a look', bad: 'Needs fixing', off: 'Off' };

export function SetupPage() {
  const res = useApi('/api/setup');
  useEffect(() => { document.title = 'Setup · Chitragupta'; pageKeys.set({}); }, []);
  const d = res.data;
  const n = d ? d.items.filter((i) => i.status === 'bad' || i.status === 'warn').length : 0;
  const rail = d ? html`<h2>Progress</h2><ul class="rlist">${d.items.map((i) => html`<li key=${i.id}><a href=${'#step-' + i.id} onClick=${(e) => { e.preventDefault(); const el = document.getElementById('step-' + i.id); if (el) { el.scrollIntoView({ block: 'start' }); el.focus(); } }}>
      <span class="sw" style=${{ background: i.status === 'ok' ? 'var(--ok)' : i.status === 'bad' ? 'var(--bad)' : i.status === 'warn' ? 'var(--warn)' : 'var(--rule)' }}></span>
      <span><span class="k" style="font-weight:500">${i.label}</span><span class="t">${WORD[i.status] || i.status}</span></span><span class="m"></span></a></li>`)}</ul>` : null;
  return html`<${Desk} rail=${rail} railLabel="Steps">
    <${PageHead} title="Setup" sub=${d ? (n ? plural(n, 'step') + ' to look at' : 'Everything is set up') + ' · checked ' + fmtTs(d.checked_at) : ''} />
    <p class="lede">The same checks as /cg:doctor. Buttons that fix things from this page arrive with the Settings update; until then, copy the step and run it in Claude Code.</p>
    <${Load} res=${res} lines=${8}>${(d) => html`<ol class="findings" style="list-style:none">${d.items.map((i) => html`<li key=${i.id} id=${'step-' + i.id} tabindex="-1">
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><strong style="flex:1">${i.label}</strong><${Status} kind=${i.status}>${WORD[i.status] || i.status}</${Status}></div>
      ${i.detail ? html`<div class="d">${i.detail}</div>` : null}
      ${i.fix && i.fix.text && i.status !== 'ok' ? html`<div style="display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap">
        <span class="muted small">${i.fix.kind === 'claude' ? 'In Claude Code:' : 'Run:'}</span><code>${i.fix.text}</code><${CopyButton} text=${i.fix.text} what=${'the fix for ' + i.label} /></div>` : null}</li>`)}</ol>`}</${Load}>
  </${Desk}>`;
}
