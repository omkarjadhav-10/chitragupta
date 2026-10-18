// The background job (Task Scheduler, launchd, systemd timer or cron): what is registered, whether it is what setup
// would write now, and Repair (the same as `setup scheduler --repair`). Used on Health and Settings.
import { useState } from '/vendor/hooks.module.js';
import { html } from './html.js';
import { useApi, runAction, toast } from './core.js';
import { Icon, Status } from './ui.js';

const METHOD = { task: 'Task Scheduler', startup: 'Startup-folder launcher', launchd: 'launchd LaunchAgent',
  systemd: 'systemd user timer', cron: 'cron', none: 'nothing registered', skipped: 'skipped' };

export function schedulerState(s) {
  if (!s) return ['wait', 'Checking'];
  if (!s.installed) return ['bad', 'Not registered'];
  if (s.current === false) return ['warn', 'Out of date'];
  return ['ok', 'Registered'];
}

export function SchedulerPanel({ headingLevel, card }) {
  const res = useApi('/api/scheduler');
  const [busy, setBusy] = useState(false);
  const s = res.data;
  const st = schedulerState(s);
  const repair = () => {
    setBusy(true);
    runAction('scheduler-repair').then((r) => { toast(r.changed ? 'Registered again' : 'Already registered and current', 'ok'); res.reload(); },
      (e) => toast(e.message, 'bad')).finally(() => setBusy(false));
  };
  const H = headingLevel === 3 ? 'h3' : 'h2';
  const body = html`<dl class="kv">
      <dt>How</dt><dd>${s ? METHOD[s.method] || s.method : '…'}${s && s.os ? ' (' + s.os + ')' : ''}</dd>
      ${s && s.status ? html`<dt>State</dt><dd>${s.status}${s.last_result ? ', last result ' + s.last_result : ''}</dd>` : null}
      ${s && s.next_run ? html`<dt>Next run</dt><dd>${s.next_run}</dd>` : null}
      <dt>Detail</dt><dd>${s ? s.detail : 'Checking…'}</dd></dl>
    ${s && (!s.installed || s.current === false) ? html`<p><button type="button" class="btn" disabled=${busy} onClick=${repair}>
      <${Icon} name="reset" size=${15} />${busy ? 'Repairing…' : 'Repair'}</button>
      <span class="muted small"> Registers Chitragupta's own job again; nothing else is touched.</span></p>` : null}`;
  if (card) {
    return html`<section class="card" aria-labelledby="h-task"><${H} id="h-task">Background job<${Status} kind=${st[0]}>${st[1]}</${Status}></${H}>${body}</section>`;
  }
  return html`<div><p><${Status} kind=${st[0]}>${st[1]}</${Status}></p>${body}</div>`;
}
