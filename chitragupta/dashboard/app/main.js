// The shell: top navigation, command palette, theme, page rail and content, routing, the command
// palette (Ctrl+K), keyboard shortcuts (?) and live updates.
import { render } from '/vendor/preact.module.js';
import { useEffect, useState, useRef, useMemo } from '/vendor/hooks.module.js';
import { html } from './lib/html.js';
import { route, useStore, useApi, go, href, startLive, connection, api, EXPORT, toast, copyText } from './lib/core.js';
import { Icon, Toasts, Dialog, themeMode } from './lib/ui.js';
import { NAV, NAV_END, paletteOpen, keysOpen, morePagesOpen, pageKeys, setTheme, Desk } from './lib/shell.js';
import { TodayPage } from './pages/today.js';
import { TicketsPage, TicketPage } from './pages/tickets.js';
import { TimesheetPage } from './pages/timesheet.js';
import { KnowledgePage } from './pages/knowledge.js';
import { DbPage } from './pages/db.js';
import { TeamPage } from './pages/team.js';
import { SearchPage } from './pages/search.js';
import { HealthPage } from './pages/health.js';
import { SetupPage } from './pages/setup.js';
import { SettingsPage } from './pages/settings.js';
import { healthIssues } from './lib/health.js';

// ------------------------------------------------------------------ top bar
function TopBar() {
  const st = useApi(EXPORT ? null : '/api/status');
  const conn = useStore(connection);
  const theme = useStore(themeMode);
  const next = { system: 'light', light: 'dark', dark: 'system' }[theme] || 'light';
  const r = useStore(route);
  const more = useStore(morePagesOpen);
  const problems = healthIssues(st.data);
  const primary = NAV.concat(NAV_END).filter((n) => ['today', 'tickets', 'timesheet', 'knowledge', 'health', 'settings'].includes(n.id));
  const secondary = NAV.concat(NAV_END).filter((n) => !primary.includes(n));
  useEffect(() => {
    if (!more) return undefined;
    const close = (e) => { if (!e.target.closest('.top')) morePagesOpen.set(false); };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [more]);
  const item = (n, cls = '') => html`<a key=${n.id} class=${cls} href=${'#/' + n.id}
    onClick=${() => morePagesOpen.set(false)} aria-current=${r.name === n.id || (r.name === 'day' && n.id === 'today') ? 'page' : null}>
    ${n.label}${n.id === 'health' && problems.length ? html`<span class="health-count">${problems.length} ${problems.length === 1 ? 'problem' : 'problems'}</span>` : null}</a>`;
  return html`<header class="top" onKeyDown=${(e) => { if (more && e.key === 'Escape') { morePagesOpen.set(false); document.querySelector('.more-pages').focus(); } }}>
    <a class="brand" href="#/today">Chitragupta</a>
    <nav class="primary-nav" aria-label="Pages">${primary.map((n) => item(n))}</nav>
    <div class="top-right">
      ${conn === 'offline' ? html`<a class="offline-status" href="#/health">Offline</a>` : null}
      <button type="button" class="btn quiet more-pages" aria-expanded=${more ? 'true' : 'false'} aria-controls="more-pages"
        onClick=${() => morePagesOpen.set(!more)}><span class="desktop-more">More</span><span class="mobile-more">Pages</span><${Icon} name=${more ? 'up' : 'down'} size=${14} /></button>
      <button type="button" class="cmd" onClick=${() => paletteOpen.set(true)} aria-haspopup="dialog" aria-label="Go to a page, ticket, or command" aria-keyshortcuts="Control+K">
        <span>Go to</span><kbd>Ctrl K</kbd></button>
      <button type="button" class="btn quiet icon" title=${'Theme: ' + theme} onClick=${() => setTheme(next)} aria-label=${'Theme: ' + theme + '. Switch to ' + next}>
        <${Icon} name=${theme === 'dark' ? 'moon' : theme === 'light' ? 'sun' : 'system'} size=${16} /></button>
    </div>
    ${more ? html`<nav id="more-pages" class="more-nav" aria-label="More pages" onKeyDown=${(e) => { if (e.key === 'Escape') { morePagesOpen.set(false); document.querySelector('.more-pages').focus(); } }}>
      <div class="mobile-primary">${primary.map((n) => item(n))}</div>${secondary.map((n) => item(n))}</nav>` : null}
  </header>`;
}

// ------------------------------------------------------------------ command palette (combobox + listbox)
function words(s) { return String(s || '').toLowerCase(); }
function matches(q, text) { const t = words(text); return words(q).split(/\s+/).filter(Boolean).every((w) => t.indexOf(w) >= 0); }
const ACTIONS = [
  { id: 'theme-system', title: 'Use the system theme', sub: 'Appearance', run: () => setTheme('system') },
  { id: 'theme-light', title: 'Use the light theme', sub: 'Appearance', run: () => setTheme('light') },
  { id: 'theme-dark', title: 'Use the dark theme', sub: 'Appearance', run: () => setTheme('dark') },
  { id: 'labels', title: 'Show or hide more pages', sub: 'Shortcut [', run: () => morePagesOpen.set(!morePagesOpen.get()) },
  { id: 'copy-worklog', title: "Copy tonight's Jira message", sub: 'From the Today page', run: () => api('/api/today').then((d) => {
    const s = d.worklog_saved, p = d.worklog_preview || {};
    const text = (s && s.text) || p.text || '';
    if (!text) { toast('There is no Jira message for today yet.', 'info'); return; }
    return copyText(text).then(() => toast("Copied tonight's Jira message", 'ok'));
  }) },
  { id: 'csv', title: 'Download the timesheet as CSV', sub: 'Hours per ticket per week', run: () => { location.href = '/api/timesheet.csv?period=week'; } },
  { id: 'keys', title: 'Show keyboard shortcuts', sub: 'Shortcut ?', run: () => keysOpen.set(true) },
];
function Palette() {
  const open = useStore(paletteOpen);
  const [q, setQ] = useState('');
  const [active, setActive] = useState(0);
  const [tickets, setTickets] = useState([]);
  const [found, setFound] = useState(null);
  const input = useRef(null);
  useEffect(() => {
    if (!open) return;
    setQ(''); setActive(0); setFound(null);
    api('/api/tickets').then((d) => setTickets(d.tickets || []), () => setTickets([]));
    setTimeout(() => input.current && input.current.focus(), 0);
  }, [open]);
  useEffect(() => {
    if (!open || q.trim().length < 2) { setFound(null); return undefined; }
    const t = setTimeout(() => api('/api/search?limit=8&q=' + encodeURIComponent(q.trim())).then(setFound, () => setFound(null)), 150);
    return () => clearTimeout(t);
  }, [q, open]);
  const items = useMemo(() => {
    const out = [];
    const pages = NAV.concat(NAV_END).filter((n) => !q || matches(q, n.label + ' page go'));
    pages.forEach((n) => out.push({ group: 'Pages', id: 'p-' + n.id, title: 'Go to ' + n.label, sub: 'g then ' + n.key, icon: n.icon, run: () => go('#/' + n.id) }));
    tickets.filter((t) => !q || matches(q, t.key + ' ' + t.title)).slice(0, q ? 8 : 5)
      .forEach((t) => out.push({ group: 'Tickets', id: 't-' + t.key, title: t.key, sub: t.title, icon: 'tickets', run: () => go('#/tickets/' + encodeURIComponent(t.key)) }));
    ACTIONS.filter((a) => !q || matches(q, a.title + ' ' + a.sub)).forEach((a) => out.push({ group: 'Actions', id: 'a-' + a.id, title: a.title, sub: a.sub, icon: 'keyboard', run: a.run }));
    const seen = {};
    out.forEach((o) => { seen[o.id] = 1; });
    ((found && found.groups) || []).forEach((g) => g.results.forEach((r) => {
      if (r.type === 'ticket' && seen['t-' + r.ref]) return;
      out.push({ group: 'Found in your worklog', id: 's-' + r.type + '-' + r.ref, title: r.title, sub: g.label + (r.ticket && r.type !== 'ticket' ? ' · ' + r.ticket : ''), icon: 'search', run: () => go(r.href) });
    }));
    if (q.trim()) out.push({ group: 'Search', id: 'search-all', title: 'Search everything for “' + q.trim() + '”', sub: 'Open the Search page', icon: 'search', run: () => go(href('search', '', { q: q.trim() })) });
    return out;
  }, [q, tickets, found]);
  const pick = (it) => { paletteOpen.set(false); setTimeout(() => Promise.resolve(it.run()).catch((e) => toast(e.message || String(e), 'bad')), 0); };
  const key = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((a) => Math.min(items.length - 1, a + 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
    else if (e.key === 'Home' && e.ctrlKey) { e.preventDefault(); setActive(0); }
    else if (e.key === 'End' && e.ctrlKey) { e.preventDefault(); setActive(items.length - 1); }
    else if (e.key === 'Enter') { e.preventDefault(); if (items[active]) pick(items[active]); }
  };
  useEffect(() => { const el = document.getElementById('pal-' + active); if (el) el.scrollIntoView({ block: 'nearest' }); }, [active]);
  let lastGroup = null;
  const act = items[Math.min(active, items.length - 1)];
  return html`<${Dialog} open=${open} title="Go to or run" onClose=${() => paletteOpen.set(false)} className="palette">
    <div class="pal-in">
      <div class="pal-input"><${Icon} name="search" size=${18} />
        <input ref=${input} autofocus type="text" role="combobox" aria-expanded="true" aria-controls="pal-list" aria-autocomplete="list"
          aria-activedescendant=${act ? 'pal-' + Math.min(active, items.length - 1) : null} aria-label="Go to a page or ticket, run an action, or search"
          placeholder="Ticket, page, table, API, action…" value=${q} onInput=${(e) => { setQ(e.currentTarget.value); setActive(0); }} onKeyDown=${key} autocomplete="off" spellcheck="false" /></div>
      <ul class="pal-list" id="pal-list" role="listbox" aria-label="Results">
        ${items.map((it, i) => {
          const head = it.group !== lastGroup ? html`<li role="presentation" class="pal-group">${it.group}</li>` : null;
          lastGroup = it.group;
          return [head, html`<li id=${'pal-' + i} role="option" class="pal-opt" aria-selected=${i === active ? 'true' : 'false'}
            onClick=${() => pick(it)} onMouseMove=${() => setActive(i)}><${Icon} name=${it.icon} size=${16} /><span class="pt">${it.title}</span><span class="ps">${it.sub || ''}</span></li>`];
        })}
        ${!items.length ? html`<li role="presentation" class="pal-group">Nothing matches. Try fewer words.</li>` : null}
      </ul>
      <div class="pal-foot"><span><kbd>↑</kbd> <kbd>↓</kbd> move</span><span><kbd>Enter</kbd> open</span><span><kbd>Esc</kbd> close</span>
        ${found ? html`<span>Search: ${found.engine === 'fts5' ? 'full-text index' : 'simple scan'}, ${found.took_ms} ms</span>` : null}</div>
    </div></${Dialog}>`;
}

// ------------------------------------------------------------------ keyboard shortcuts
const GLOBAL_KEYS = [
  ['Ctrl K or /', 'Open the command palette'], ['?', 'Show this list'],
  ['g then t', 'Today'], ['g then k', 'Tickets'], ['g then s', 'Timesheet'], ['g then n', 'Knowledge'], ['g then b', 'DB map'], ['g then f', 'Search'], ['g then m', 'Team notes'],
  ['g then h', 'Health'], ['g then u', 'Setup'], ['g then e', 'Settings'],
  ['j / k', 'Next / previous item in the side list'], ['[', 'Show or hide more pages'], ['Esc', 'Close a dialog'],
];
function KeysDialog() {
  const open = useStore(keysOpen);
  const pk = useStore(pageKeys);
  const rows = GLOBAL_KEYS.concat(Object.keys(pk).map((k) => [k, pk[k].label]));
  return html`<${Dialog} open=${open} title="Keyboard shortcuts" onClose=${() => keysOpen.set(false)}>
    <table class="keys-table"><caption class="sr">Keyboard shortcuts</caption><thead><tr><th scope="col">Keys</th><th scope="col">Does</th></tr></thead>
    <tbody>${rows.map((r) => html`<tr key=${r[0]}><td>${r[0].split(' ').map((p) => (p === 'then' || p === 'or' || p === '/' && r[0] !== '/' ? html`<span class="muted"> ${p} </span>` : html`<kbd>${p}</kbd>`))}</td><td>${r[1]}</td></tr>`)}</tbody></table>
    <p class="muted small" style="margin-top:12px">Shortcuts are off while you type in a field. Ctrl K always works.</p></${Dialog}>`;
}
function typing(t) { return t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)); }
function railStep(dir) {
  const links = Array.from(document.querySelectorAll('.rlist a'));
  if (!links.length) return;
  let i = links.indexOf(document.activeElement);
  if (i < 0) i = links.findIndex((a) => a.getAttribute('aria-current'));
  i = Math.max(0, Math.min(links.length - 1, i + dir));
  links[i].focus();
}
function useShortcuts() {
  useEffect(() => {
    let pendingG = 0;
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); paletteOpen.set(!paletteOpen.get()); return; }
      if (e.ctrlKey || e.metaKey || e.altKey || typing(e.target) || document.querySelector('dialog[open]')) return;
      if (pendingG && Date.now() - pendingG < 1200) {
        pendingG = 0;
        const n = NAV.concat(NAV_END).find((x) => x.key === e.key);
        if (n) { e.preventDefault(); go('#/' + n.id); }
        return;
      }
      if (e.key === 'g') { pendingG = Date.now(); return; }
      if (e.key === '/') { e.preventDefault(); paletteOpen.set(true); return; }
      if (e.key === '?') { e.preventDefault(); keysOpen.set(true); return; }
      if (e.key === '[') { morePagesOpen.set(!morePagesOpen.get()); return; }
      if (e.key === 'j') { e.preventDefault(); railStep(1); return; }
      if (e.key === 'k') { e.preventDefault(); railStep(-1); return; }
      const pk = pageKeys.get()[e.key];
      if (pk) { e.preventDefault(); pk.run(); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);
}

// ------------------------------------------------------------------ router
function Missing() {
  return html`<${Desk}><h1 id="page-title" tabindex="-1">Page not found</h1><p>There's no page at this address. <a href="#/today">Go to Today</a> or press <kbd>Ctrl K</kbd> to find something.</p></${Desk}>`;
}
function Page() {
  const r = useStore(route);
  const last = useRef(null);
  useEffect(() => {  // move focus to the new page's heading so screen readers announce it; not for filter/tab changes
    const k = r.name + '/' + r.arg;
    if (last.current !== null && last.current !== k) {
      window.scrollTo({ top: 0, behavior: 'instant' });
      setTimeout(() => { const h = document.getElementById('page-title') || document.getElementById('main'); if (h) h.focus({ preventScroll: true }); }, 60);
    }
    last.current = k;
  }, [r.name, r.arg]);
  switch (r.name) {
    case 'today': return html`<${TodayPage} route=${r} />`;
    case 'day': return html`<${TodayPage} route=${r} />`;
    case 'tickets': return r.arg ? html`<${TicketPage} key=${'t-' + r.arg} route=${r} />` : html`<${TicketsPage} route=${r} />`;
    case 'timesheet': return html`<${TimesheetPage} route=${r} />`;
    case 'knowledge': return html`<${KnowledgePage} route=${r} />`;
    case 'db': return html`<${DbPage} route=${r} />`;
    case 'team': return html`<${TeamPage} route=${r} />`;
    case 'search': return html`<${SearchPage} route=${r} />`;
    case 'health': return html`<${HealthPage} route=${r} />`;
    case 'setup': return html`<${SetupPage} route=${r} />`;
    case 'settings': return html`<${SettingsPage} route=${r} />`;
    default: return html`<${Missing} />`;
  }
}

function App() {
  useShortcuts();
  if (EXPORT) {
    return html`<div class="desk no-rail exported"><main class="pane" id="main" tabindex="-1" style="grid-column:1/-1">
      <${TicketPage} route=${{ name: 'tickets', arg: EXPORT.key, params: {} }} />
      <p class="foot">Exported ${String((EXPORT.export || {}).at || '').replace('T', ' ').slice(0, 16)} · ${(EXPORT.export || {}).mode || 'full'} · a snapshot of the local worklog; this file makes no requests.</p></main></div><${Toasts} />`;
  }
  return html`<${TopBar} /><${Page} /><${Palette} /><${KeysDialog} /><${Toasts} />`;
}

if (!EXPORT && !location.hash) { try { history.replaceState(null, '', '#/today'); } catch (e) { /* ignore */ } }
startLive();
render(html`<${App} />`, document.getElementById('app'));
