// The live MCP status panel (Health and Settings): the same `claude mcp list` check /cg:doctor runs (no model call),
// run on demand ("Check now") and automatically when the panel opens if the last check is over 5 minutes old, shown
// next to what the last background job saw.
import { useEffect, useState } from '/vendor/hooks.module.js';
import { html } from './html.js';
import { runAction, toast, fmtTs, parseTs } from './core.js';
import { Icon, Status, CopyButton } from './ui.js';

const AUTO_AFTER_MS = 5 * 60 * 1000;
let autoRanAt = 0;  // at most once every 5 minutes per page load, however often the panel is mounted

function liveState(c, lv) {
  if (!c.enabled) return ['off', 'Switched off'];
  if (!lv) return ['off', 'Missing'];
  if (lv.status === 'connected') return ['ok', 'Connected'];
  if (/sign|auth/i.test(lv.status)) return ['warn', 'Signed out'];
  if (/not listed|missing/i.test(lv.status)) return ['off', 'Missing'];
  if (/unreachable|offline|fail/i.test(lv.status)) return ['bad', 'Unreachable'];
  return ['bad', lv.status];
}

export function McpStatusPanel({ mc, onChecked, headingLevel }) {
  const [busy, setBusy] = useState(false);
  const live = mc.live || {};
  const servers = live.servers || [];
  const check = () => {
    if (busy) return;
    setBusy(true);
    autoRanAt = Date.now();
    runAction('mcp-check').then((r) => { toast('MCP check done in ' + r.seconds + ' s', 'ok'); if (onChecked) onChecked(); },
      (e) => toast(e.message, 'bad')).finally(() => setBusy(false));
  };
  useEffect(() => {
    const at = parseTs(live.check_at || live.checked_at);
    if ((!at || Date.now() - at.getTime() > AUTO_AFTER_MS) && Date.now() - autoRanAt > AUTO_AFTER_MS) check();
  }, []);
  const H = headingLevel === 3 ? 'h3' : 'h2';
  const slack = servers.find((x) => x.role === 'slack');
  const rows = (mc.configured || []).concat(slack ? [{ role: 'slack', enabled: true, server: slack.server }] : []);
  return html`<div class="mcp-panel">
    <div class="sec-head"><p class="sub" aria-live="polite">${busy ? 'Checking every server now…'
      : live.checked_at ? 'Live check ' + fmtTs(live.check_at || live.checked_at) + (live.seconds != null ? ' · took ' + live.seconds + ' s' : '') + ' · claude mcp list, no model call'
        : 'Not checked yet'}</p><span class="grow"></span>
      <button type="button" class="btn" onClick=${check} disabled=${busy}><${Icon} name="reset" size=${16} />${busy ? 'Checking…' : 'Check now'}</button></div>
    ${live.check_error ? html`<p class="field-err"><${Icon} name="bad" size=${14} />The last check couldn't run: ${live.check_error}</p>` : null}
    <div class="grid">${rows.map((c) => {
      const lv = servers.find((x) => x.role === c.role) || servers.find((x) => x.server === c.server);
      const job = (mc.servers || []).find((x) => x.server === c.server);
      const st = liveState(c, lv);
      const name = c.role === 'database' ? 'Database MCP' : c.role === 'slack' ? 'Slack' : 'Jira';
      return html`<section class="card" key=${c.role} aria-labelledby=${'mcp-' + c.role}><${H} id=${'mcp-' + c.role}>${name}<${Status} kind=${st[0]}>${st[1]}</${Status}></${H}>
        <dl class="kv"><dt>Server</dt><dd class="mono">${c.server || (lv && lv.server) || '—'}</dd>
          <dt>Live</dt><dd>${lv ? lv.status + (live.checked_at ? ', ' + fmtTs(live.checked_at) : '') : 'not seen by the last check'}</dd>
          <dt>Last job</dt><dd>${job ? job.job + ', ' + fmtTs(job.at) + ' (' + (job.state || job.status) + ')' : 'none yet'}</dd>
          ${job && job.error ? html`<dt>Job error</dt><dd>${job.error}${job.error_at ? ', ' + fmtTs(job.error_at) : ''}</dd>` : null}
          ${lv && st[0] !== 'ok' && (lv.error || lv.detail) ? html`<dt>Last error</dt><dd>${lv.error ? lv.error + (lv.error_at ? ', ' + fmtTs(lv.error_at) + ' · ' : ' · ') : ''}${lv.detail || ''}</dd>` : null}</dl>
        ${st[0] === 'warn' ? html`<p style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">Sign in again in Claude Code: <code>/mcp</code><${CopyButton} text="/mcp" what="the sign-in command" /></p>` : null}
        ${st[1] === 'Missing' && c.enabled ? html`<p>Add it with <code>/cg:setup</code>; only setup adds MCP servers and classifies their tools.</p>` : null}</section>`;
    })}</div>
    <p class="muted small">Per-server response times aren't available: <code>claude mcp list</code> reports one time for the whole check.</p></div>`;
}
