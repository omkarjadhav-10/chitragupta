// Settings. Appearance is per browser and works now. Every other group becomes editable here once the safety layer
// lands (each change shown as old → new, recorded and undoable); until then /cg:setup changes them.
import { useEffect } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useStore, prefs } from '../lib/core.js';
import { PageHead, themeMode } from '../lib/ui.js';
import { Desk, pageKeys, appearance } from '../lib/shell.js';

function Choice({ name, legend, value, options, onChange, hint }) {
  return html`<fieldset class="card" style="margin:0 0 16px"><legend style="font:600 15.5px var(--serif);padding:0 4px">${legend}</legend>
    ${hint ? html`<p style="margin:0 0 8px">${hint}</p>` : null}
    <div class="seg" role="radiogroup" aria-label=${legend} onKeyDown=${(e) => {  // radio pattern: arrows move and select
      const i = options.findIndex(([v]) => v === value);
      const d = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      const n = (i + d + options.length) % options.length;
      onChange(options[n][0]);
      const btns = e.currentTarget.querySelectorAll('[role="radio"]');
      setTimeout(() => btns[n] && btns[n].focus(), 0);
    }}>${options.map(([v, l]) => html`<button key=${v} type="button" role="radio" aria-checked=${value === v ? 'true' : 'false'}
      tabindex=${value === v ? 0 : -1} onClick=${() => onChange(v)}>${l}</button>`)}</div></fieldset>`;
}

const LATER = ['Capture: pause, repos included and excluded, ticket prefixes', 'Daily worklog and Slack: the 8 PM message, send time, workdays, lunch, hours per day, leave and holidays',
  'Jira, Database MCP, Branches and PRs, Requests and artifacts', 'GitHub daily push, models and the daily MCP budget, data retention'];

export function SettingsPage() {
  const theme = useStore(themeMode);
  const density = document.documentElement.getAttribute('data-density') || 'comfortable';
  const motion = document.documentElement.getAttribute('data-motion') || 'system';
  useEffect(() => { document.title = 'Settings · Chitragupta'; pageKeys.set({}); }, []);
  const rerender = () => themeMode.set(themeMode.get());
  return html`<${Desk}>
    <${PageHead} title="Settings" sub="Appearance is saved in this browser only" />
    <div style="max-width:720px">
      <${Choice} legend="Theme" value=${theme} options=${[['system', 'System'], ['light', 'Light'], ['dark', 'Dark']]} onChange=${appearance.setTheme} />
      <${Choice} legend="Density" value=${density} options=${[['comfortable', 'Comfortable'], ['compact', 'Compact']]} onChange=${(v) => { appearance.setDensity(v); rerender(); }} />
      <${Choice} legend="Motion" value=${motion} hint="Reduce turns off the ring animation and loading shimmer. System follows your operating system's setting."
        options=${[['system', 'System'], ['reduce', 'Reduce'], ['full', 'Full']]} onChange=${(v) => { appearance.setMotion(v); rerender(); }} />
      <section class="card" aria-labelledby="set-later"><h2 id="set-later">Coming next</h2>
        <p>These become editable here, each change shown before it's saved and recorded so you can undo it:</p>
        <ul class="small" style="margin:8px 0;color:var(--ink-2)">${LATER.map((l) => html`<li key=${l}>${l}</li>`)}</ul>
        <p>For now, change them with <code>/cg:setup</code> in Claude Code. Safety rules, MCP tool lists and secrets are never editable here.</p></section>
    </div>
  </${Desk}>`;
}
