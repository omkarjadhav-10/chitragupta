// Layout and shared UI state used by every page: navigation, the Desk (nav · rail · pane), appearance and the stores
// the palette and shortcuts read. Kept apart from main.js so pages never import the entry module.
import { useState } from '/vendor/hooks.module.js';
import { html } from './html.js';
import { route, useStore, store, prefs } from './core.js';
import { Icon, themeMode } from './ui.js';

export const NAV = [
  { id: 'today', label: 'Today', icon: 'today', key: 't' },
  { id: 'tickets', label: 'Tickets', icon: 'tickets', key: 'k' },
  { id: 'timesheet', label: 'Timesheet', icon: 'timesheet', key: 's' },
  { id: 'knowledge', label: 'Knowledge', icon: 'knowledge', key: 'n' },
  { id: 'search', label: 'Search', icon: 'search', key: 'f' },
  { id: 'health', label: 'Health', icon: 'health', key: 'h' },
];
export const NAV_END = [
  { id: 'setup', label: 'Setup', icon: 'setup', key: 'u' },
  { id: 'settings', label: 'Settings', icon: 'settings', key: 'e' },
];

// ------------------------------------------------------------------ shared UI state
export const paletteOpen = store(false);
export const keysOpen = store(false);
export const navLabels = store(prefs.get('nav', 'icons') === 'labels');
// Page-level shortcuts: pages register {key: {label, run}} while mounted.
export const pageKeys = store({});

export function setTheme(t) {
  const root = document.documentElement;
  if (t === 'light' || t === 'dark') root.setAttribute('data-theme', t); else root.removeAttribute('data-theme');
  prefs.set('theme', t === 'light' || t === 'dark' ? t : null);
  themeMode.set(t === 'light' || t === 'dark' ? t : 'system');
}
export const appearance = {
  setTheme,
  setDensity(d) { if (d === 'compact') document.documentElement.setAttribute('data-density', d); else document.documentElement.removeAttribute('data-density'); prefs.set('density', d === 'compact' ? d : null); },
  setMotion(m) { if (m === 'reduce' || m === 'full') document.documentElement.setAttribute('data-motion', m); else document.documentElement.removeAttribute('data-motion'); prefs.set('motion', m === 'reduce' || m === 'full' ? m : null); },
};

// ------------------------------------------------------------------ Desk: nav · rail · pane
export function Desk({ rail, railLabel, children }) {
  const labels = useStore(navLabels);
  const r = useStore(route);
  const [railOpen, setRailOpen] = useState(false);
  const item = (n) => html`<a key=${n.id} href=${'#/' + n.id} aria-current=${(r.name === n.id || (r.name === 'day' && n.id === 'today')) ? 'page' : null} title=${labels ? null : n.label}>
    <${Icon} name=${n.icon} size=${20} /><span class="lbl">${n.label}</span></a>`;
  return html`<div class=${'desk' + (labels ? ' labels' : '') + (rail ? '' : ' no-rail')}>
    <nav class="nav" aria-label="Pages">
      ${NAV.map(item)}
      <div class="grow"></div>
      ${NAV_END.map(item)}
      <button type="button" class="navbtn" onClick=${() => { navLabels.set(!labels); prefs.set('nav', labels ? 'icons' : 'labels'); }}
        aria-pressed=${labels ? 'true' : 'false'} title=${labels ? null : 'Show labels'}><${Icon} name="rail" size=${20} /><span class="lbl">${labels ? 'Hide labels' : 'Show labels'}</span></button>
    </nav>
    ${rail ? html`<aside class=${'rail' + (railOpen ? '' : ' collapsed')} aria-label=${railLabel || 'Page list'}>
      <button type="button" class="btn rail-toggle" aria-expanded=${railOpen ? 'true' : 'false'} onClick=${() => setRailOpen(!railOpen)}>
        <${Icon} name=${railOpen ? 'up' : 'down'} size=${16} />${railOpen ? 'Hide ' : 'Show '}${(railLabel || 'list').toLowerCase()}</button>
      ${rail}</aside>` : null}
    <main class="pane" id="main" tabindex="-1">${children}
      <p class="foot">Local and private: this page reads your worklog on this machine and sends nothing anywhere.</p></main>
  </div>`;
}

