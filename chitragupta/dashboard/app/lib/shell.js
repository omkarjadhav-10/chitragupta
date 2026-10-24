// Layout and shared UI state used by every page: navigation, the Desk (page rail and content), appearance and the stores
// the palette and shortcuts read. Kept apart from main.js so pages never import the entry module.
import { useState } from '/vendor/hooks.module.js';
import { html } from './html.js';
import { store, prefs } from './core.js';
import { Icon, themeMode, effectiveDark } from './ui.js';

export const NAV = [
  { id: 'today', label: 'Today', icon: 'today', key: 't' },
  { id: 'tickets', label: 'Tickets', icon: 'tickets', key: 'k' },
  { id: 'timesheet', label: 'Timesheet', icon: 'timesheet', key: 's' },
  { id: 'knowledge', label: 'Knowledge', icon: 'knowledge', key: 'n' },
  { id: 'db', label: 'DB map', icon: 'db', key: 'b' },
  { id: 'search', label: 'Search', icon: 'search', key: 'f' },
  { id: 'team', label: 'Team notes', icon: 'chat', key: 'm' },
  { id: 'health', label: 'Health', icon: 'health', key: 'h' },
];
export const NAV_END = [
  { id: 'setup', label: 'Setup', icon: 'setup', key: 'u' },
  { id: 'settings', label: 'Settings', icon: 'settings', key: 'e' },
];

// ------------------------------------------------------------------ shared UI state
export const paletteOpen = store(false);
export const keysOpen = store(false);
export const morePagesOpen = store(false); // More pages menu; also opened with the existing [ shortcut.
// Page-level shortcuts: pages register {key: {label, run}} while mounted.
export const pageKeys = store({});

export function setTheme(t) {
  const root = document.documentElement;
  t = t === 'light' || t === 'dark' ? t : 'system';
  root.setAttribute('data-theme', t);
  prefs.set('theme', t);
  themeMode.set(t === 'light' || t === 'dark' ? t : 'system');
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', effectiveDark() ? '#0F1216' : '#FAFAF8');
}
export const appearance = {
  setTheme,
  setDensity(d) { if (d === 'compact') document.documentElement.setAttribute('data-density', d); else document.documentElement.removeAttribute('data-density'); prefs.set('density', d === 'compact' ? d : null); },
  setMotion(m) { if (m === 'reduce' || m === 'full') document.documentElement.setAttribute('data-motion', m); else document.documentElement.removeAttribute('data-motion'); prefs.set('motion', m === 'reduce' || m === 'full' ? m : null); },
};

// ------------------------------------------------------------------ Desk: page rail and content
export function Desk({ rail, railLabel, children, className = '' }) {
  const [railOpen, setRailOpen] = useState(false);
  return html`<div class=${'desk ' + className + (rail ? '' : ' no-rail')}>
    ${rail ? html`<aside class=${'rail' + (railOpen ? '' : ' collapsed')} aria-label=${railLabel || 'Page list'}>
      <button type="button" class="btn rail-toggle" aria-expanded=${railOpen ? 'true' : 'false'} onClick=${() => setRailOpen(!railOpen)}>
        <${Icon} name=${railOpen ? 'up' : 'down'} size=${16} />${railOpen ? 'Hide ' : 'Show '}${(railLabel || 'list').toLowerCase()}</button>
      ${rail}</aside>` : null}
    <main class="pane" id="main" tabindex="-1">${children}
      <p class="foot">Local and private: this page reads your worklog on this machine and sends nothing anywhere.</p></main>
  </div>`;
}

