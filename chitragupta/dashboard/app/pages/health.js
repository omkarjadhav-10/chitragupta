// Health: capture, background runs, the daily push, Slack, MCP servers (the cached `claude mcp list` check and what the
// last jobs saw), the scheduled task, safety violations and recent errors, with "Copy diagnostics".
import { useEffect } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, fmtTs, fmtDay, ago, plural } from '../lib/core.js';
import { Icon, Status, CopyButton, Empty, Load, PageHead, Scroll } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';

function Card({ title, status, big, lines, id }) {
  return html`<section class="card" aria-labelledby=${id}><h2 id=${id}>${title}${status}</h2>${big != null ? html`<div class="big">${big}</div>` : null}
    ${(lines || []).filter(Boolean).map((l, i) => html`<p key=${i}>${l}</p>`)}</section>`;
}

function slackState(sl) {
  const last = (sl.recent || [])[0];
  if (sl.enabled === false) return ['off', 'Off'];
  if (sl.status === 'not_connected' || (!sl.configured && !last)) return ['off', 'Not connected'];
  if (sl.send_warning) return ['bad', 'Failing'];
  if (last && last.status === 'failed') return ['warn', 'Retrying'];
  if (last && (last.status === 'sent' || last.status === 'nudged')) return ['ok', 'Sent'];
  return ['wait', 'Waiting'];
}

function diagnostics(s, setup) {
  const L = [];
  const ds = s.dashboard || {};
  L.push('Chitragupta ' + (ds.version || '?') + ' · dashboard pid ' + (ds.pid || '?') + ' port ' + (ds.port || '?'));
  L.push('Checked ' + s.now + (s.paused ? ' · capture PAUSED' : ''));
  L.push('Hooks: last event ' + (s.hooks.last_event || 'never') + ' (' + ago(s.hooks.minutes_ago) + ')');
  L.push('Tick: last ' + (s.tick.last_tick || 'never') + ', last good ' + (s.tick.last_tick_ok || 'never'));
  L.push('Push: ' + (s.push.remote_set ? 'last ' + (s.push.last_push || 'never') + (s.push.error ? ' ERROR ' + s.push.error : '') : 'no remote'));
  L.push('Slack: enabled=' + s.slack.enabled + ' status=' + (s.slack.status || '-') + (s.slack.send_warning ? ' warning=' + s.slack.send_warning : ''));
  const mc = s.mcp || {};
  (mc.configured || []).forEach((c) => L.push('MCP ' + c.role + ': ' + (c.enabled ? 'on' : 'off') + ' (' + (c.server || '-') + ')'));
  ((mc.live && (mc.live.servers || mc.live.rows)) || []).forEach((x) => L.push('  live ' + (x.name || x.server) + ': ' + x.status));
  L.push('MCP today: ' + (mc.calls_today || 0) + ' jobs' + (mc.budget ? ' of ' + mc.budget : '') + ', tokens in ' + (mc.tokens_in_today || 0) + ' out ' + (mc.tokens_out_today || 0));
  if (setup) setup.items.forEach((i) => L.push('Setup ' + i.label + ': ' + i.status + (i.detail ? ' (' + i.detail + ')' : '')));
  L.push('Errors all time: ' + (s.errors_total || 0));
  (s.errors || []).forEach((e) => L.push('  [' + e.source + '] ' + e.text));
  return L.join('\n');
}

export function HealthPage() {
  const res = useApi('/api/status');
  const setup = useApi('/api/setup');
  useEffect(() => { document.title = 'Health · Chitragupta'; pageKeys.set({}); }, []);
  return html`<${Desk}>
    <${Load} res=${res} lines=${10}>${(s) => {
      const hk = s.hooks.minutes_ago, tk = s.tick.minutes_ago, pu = s.push, sl = s.slack, mc = s.mcp || {};
      const last = (sl.recent || [])[0];
      const ss = slackState(sl);
      const live = (mc.live && (mc.live.servers || mc.live.rows)) || [];
      const guard = (s.errors || []).filter((e) => e.source === 'mcp-guard');
      const task = setup.data && setup.data.items.find((i) => /task/i.test(i.id + ' ' + i.label));
      const budgetHit = mc.budget && mc.calls_today >= mc.budget;
      return html`
      <${PageHead} title="Health" sub=${fmtDay(s.day) + ' · checked ' + fmtTs(s.now)}>
        <${CopyButton} text=${diagnostics(s, setup.data)} label="Copy diagnostics" what="the diagnostics" /></${PageHead}>
      ${s.paused ? html`<div class="alert" role="status"><${Icon} name="warn" size=${18} /><div class="grow"><strong>Capture is paused.</strong> Nothing new is recorded until you resume with /cg:pause off.</div></div>` : null}
      <div class="grid" style="margin-bottom:28px">
        <${Card} id="h-cap" title="Capture" status=${s.paused ? html`<${Status} kind="warn">Paused</${Status}>` : hk == null ? html`<${Status} kind="bad">No events</${Status}>` : hk <= 24 * 60 ? html`<${Status} kind="ok">Recording</${Status}>` : html`<${Status} kind="warn">Quiet</${Status}>`}
          big=${hk == null ? '—' : ago(hk)} lines=${[s.hooks.last_event ? 'Last captured event ' + fmtTs(s.hooks.last_event) : 'No hook event captured yet. Start a Claude Code session in a repo.']} />
        <${Card} id="h-tick" title="Background runs" status=${tk == null ? html`<${Status} kind="bad">Never ran</${Status}>` : tk <= 15 ? html`<${Status} kind="ok">Running</${Status}>` : tk <= 60 ? html`<${Status} kind="warn">Late</${Status}>` : html`<${Status} kind="bad">Stopped</${Status}>`}
          big=${tk == null ? '—' : ago(tk)} lines=${[s.tick.last_tick ? 'Last run ' + fmtTs(s.tick.last_tick) : '', s.tick.last_tick_ok ? 'Last good run ' + fmtTs(s.tick.last_tick_ok) : '', tk != null && tk > 60 ? 'Check the scheduled task below, or run /cg:doctor.' : '']} />
        <${Card} id="h-push" title="Daily push" status=${!pu.remote_set ? html`<${Status} kind="off">Off</${Status}>` : pu.error ? html`<${Status} kind="bad">Failing</${Status}>` : pu.minutes_ago == null ? html`<${Status} kind="wait">Not yet</${Status}>` : html`<${Status} kind="ok">Working</${Status}>`}
          big=${pu.minutes_ago == null ? '—' : ago(pu.minutes_ago)} lines=${[!pu.remote_set ? 'No GitHub remote set; everything stays on this machine.' : '', pu.error || '', pu.last_commit ? 'Last commit ' + fmtTs(pu.last_commit) : '']} />
        <${Card} id="h-slack" title="Slack worklog" status=${html`<${Status} kind=${ss[0]}>${ss[1]}</${Status}>`} big=${last ? fmtDay(last.day, true) + ' · ' + (last.status || '—') : '—'}
          lines=${[sl.send_at ? 'Sends at ' + sl.send_at : '', last && last.sent_at ? 'Sent ' + fmtTs(last.sent_at) : '', sl.send_warning || '', last && last.last_error ? last.last_error : '',
            ss[1] === 'Not connected' ? "Slack isn't connected; the worklog is still written to the daily file. Connect it with /cg:setup." : '']} />
        <${Card} id="h-task" title="Scheduled task" status=${task ? html`<${Status} kind=${task.status}>${task.status === 'ok' ? 'Registered' : task.status === 'off' ? 'Off' : 'Needs attention'}</${Status}>` : html`<${Status} kind="wait">Checking</${Status}>`}
          lines=${[task ? task.detail : '', task && task.fix && task.fix.text && task.status !== 'ok' ? 'Fix: ' + task.fix.text : '']} />
        <${Card} id="h-safe" title="Safety" status=${guard.length ? html`<${Status} kind="warn">${plural(guard.length, 'blocked call')}</${Status}>` : html`<${Status} kind="ok">No violations</${Status}>`}
          lines=${[guard.length ? 'The read-only guard blocked these MCP calls; see the errors below.' : 'No MCP call was blocked recently.']} />
      </div>

      <section class="sec" aria-labelledby="h-mcp"><div class="sec-head"><h2 id="h-mcp">MCP servers</h2>
        <p class="sub">${mc.live && mc.live.checked_at ? 'Connection check from ' + fmtTs(mc.live.checked_at) + ' (claude mcp list, no model call)' : 'No connection check cached yet; /cg:doctor runs one'}</p></div>
        <div class="grid">${(mc.configured || []).map((c) => {
          const lv = live.find((x) => (x.name || x.server) === c.server);
          const job = (mc.servers || []).find((x) => x.server === c.server);
          const st = !c.enabled ? ['off', 'Switched off'] : lv ? (lv.status === 'connected' ? ['ok', 'Connected'] : /sign|auth/i.test(lv.status) ? ['warn', 'Signed out'] : ['bad', lv.status]) : ['off', 'Missing'];
          return html`<section class="card" key=${c.role} aria-labelledby=${'mc-' + c.role}><h2 id=${'mc-' + c.role}>${c.role === 'database' ? 'Database MCP' : 'Jira'}<${Status} kind=${st[0]}>${st[1]}</${Status}></h2>
            <dl class="kv"><dt>Server</dt><dd class="mono">${c.server || '—'}</dd>
              <dt>Last job</dt><dd>${job ? job.job + ', ' + fmtTs(job.at) + ' (' + job.status + ')' : 'none yet'}</dd></dl>
            ${st[0] === 'warn' ? html`<p>Sign in again: run <code>/mcp</code> in Claude Code and pick this server.</p>` : null}</section>`;
        })}
        <section class="card" aria-labelledby="mc-use"><h2 id="mc-use">Today's use${budgetHit ? html`<${Status} kind="warn">Budget reached</${Status}>` : html`<${Status} kind="ok">Within budget</${Status}>`}</h2>
          <div class="big">${mc.calls_today || 0}${mc.budget ? ' of ' + mc.budget : ''} jobs</div>
          <p>${(mc.tokens_in_today || 0).toLocaleString()} tokens in · ${(mc.tokens_out_today || 0).toLocaleString()} out${mc.cost_today ? ' · about $' + mc.cost_today : ''}</p>
          ${typeof mc.last_skip_reason === 'string' && mc.last_skip_reason.trim() ? html`<p>Last skip: ${mc.last_skip_reason}</p>` : null}</section></div>
        ${live.length ? html`<details class="more"><summary>All servers Claude Code knows (${live.length})</summary><${Scroll} label="All MCP servers"><table><caption class="sr">MCP servers</caption>
          <thead><tr><th scope="col">Server</th><th scope="col">Status</th></tr></thead><tbody>${live.map((x) => html`<tr key=${x.name || x.server}><td class="mono">${x.name || x.server}</td>
          <td><${Status} kind=${x.status === 'connected' ? 'ok' : 'bad'}>${x.status}</${Status}></td></tr>`)}</tbody></table></${Scroll}></details>` : null}
      </section>

      ${(s.warnings || []).length ? html`<section class="sec" aria-labelledby="h-warn"><div class="sec-head"><h2 id="h-warn">Warnings</h2></div>
        <ul class="findings">${s.warnings.map((w, i) => html`<li key=${i}><${Status} kind="warn" quiet>${w}</${Status}></li>`)}</ul></section>` : null}
      <section class="sec" aria-labelledby="h-err"><div class="sec-head"><h2 id="h-err">Recent errors</h2><p class="sub">${plural(s.errors_total || 0, 'error')} all time</p></div>
        ${(s.errors || []).length ? html`<ul class="findings">${s.errors.slice().reverse().map((e, i) => html`<li key=${i}><span class="chip">${e.source}</span> <span class="mono small" style="overflow-wrap:anywhere">${e.text}</span></li>`)}</ul>`
          : html`<${Empty} title="No recent errors" icon="ok" />`}</section>
      <p class="muted small">Dashboard ${(s.dashboard || {}).version || ''} · port ${(s.dashboard || {}).port || ''} · started ${fmtTs((s.dashboard || {}).started_at)} · data home <span class="mono">${s.home}</span></p>`;
    }}</${Load}></${Desk}>`;
}
