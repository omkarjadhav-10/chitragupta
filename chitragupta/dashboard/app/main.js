// The shell: top bar (command field, status pills, theme), the Desk layout (nav · rail · pane), routing, the command
// palette (Ctrl+K), keyboard shortcuts (?) and live updates.
import { render } from '/vendor/preact.module.js';
import { useEffect, useState, useRef, useMemo } from '/vendor/hooks.module.js';
import { html } from './lib/html.js';
import { route, useStore, useApi, store, prefs, go, href, startLive, connection, api, EXPORT, toast, copyText } from './lib/core.js';
import { Icon, Toasts, Dialog, themeMode } from './lib/ui.js';
import { NAV, NAV_END, paletteOpen, keysOpen, navLabels, pageKeys, setTheme, Desk } from './lib/shell.js';
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

// ------------------------------------------------------------------ top bar
// Top-bar status: one pill per service, its own icon plus a small state badge (✓ on · – off · ! needs a look), so the
// state never depends on colour alone. The words are the tooltip and the accessible name.
function statusPills(s) {
  if (!s) return [];
  const pills = [];
  const hk = s.hooks && s.hooks.minutes_ago;
  pills.push(Object.assign({ svc: 'capture' }, s.paused ? { kind: 'warn', text: 'Capture paused' }
    : hk != null && hk <= 24 * 60 ? { kind: 'ok', text: 'Capture on' } : { kind: 'warn', text: 'Capture quiet' }));
  const sl = s.slack || {};
  pills.push(Object.assign({ svc: 'chat' }, sl.enabled === false || sl.status === 'off' ? { kind: 'off', text: 'Slack off' }
    : sl.status === 'not_connected' || !sl.configured ? { kind: 'off', text: 'Slack not connected' }
      : sl.send_warning ? { kind: 'bad', text: 'Slack failing' } : { kind: 'ok', text: 'Slack on' }));
  const mc = s.mcp || {};
  const live = (mc.live && (mc.live.servers || mc.live.rows)) || [];
  (mc.configured || []).forEach((c) => {
    const name = c.role === 'database' ? 'Database MCP' : 'Jira MCP';
    const svc = c.role === 'database' ? 'db' : 'tickets';
    if (!c.enabled) { pills.push({ svc, kind: 'off', text: name + ' off' }); return; }
    const hit = live.find((x) => (x.name || x.server) === c.server) || (mc.servers || []).find((x) => x.server === c.server);
    const st = hit ? (hit.status || '') : '';
    pills.push(st === 'connected' ? { svc, kind: 'ok', text: name + ' connected' } : { svc, kind: 'bad', text: name + (st ? ' ' + st : ' unknown') });
  });
  return pills;
}
const BADGE = { ok: 'check', off: 'dash', warn: 'alert', bad: 'alert' };
function Pill({ p }) {
  return html`<a class=${'pill ' + p.kind} href="#/health" title=${p.text} aria-label=${p.text}>
    <span class="pill-ic" aria-hidden="true"><${Icon} name=${p.svc} size=${18} /><span class="pill-badge"><${Icon} name=${BADGE[p.kind] || 'alert'} size=${8} /></span></span>
    <span class="lbl" aria-hidden="true">${p.text}</span></a>`;
}
function TopBar() {
  const st = useApi(EXPORT ? null : '/api/status');
  const conn = useStore(connection);
  const theme = useStore(themeMode);
  const next = { system: 'light', light: 'dark', dark: 'system' }[theme] || 'light';
  const pills = statusPills(st.data);
  return html`<header class="top">
    <a class="brand" href="#/today"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>Chitragupta</a>
    <button type="button" class="cmd" onClick=${() => paletteOpen.set(true)} aria-haspopup="dialog" aria-keyshortcuts="Control+K">
      <${Icon} name="search" size=${16} /><span>Type a ticket, a table, an API, or a command</span><kbd>Ctrl K</kbd></button>
    <div class="top-right">
      ${conn === 'offline' ? html`<${Pill} p=${{ svc: 'system', kind: 'bad', text: 'Dashboard offline' }} />` : null}
      ${pills.map((p) => html`<${Pill} key=${p.text} p=${p} />`)}
      <button type="button" class="btn quiet icon" title=${'Theme: ' + theme} onClick=${() => setTheme(next)} aria-label=${'Theme: ' + theme + '. Switch to ' + next}>
        <${Icon} name=${theme === 'dark' ? 'moon' : theme === 'light' ? 'sun' : 'system'} size=${16} /></button>
    </div>
  </header>`;
}

// ------------------------------------------------------------------ command palette (combobox + listbox)
function words(s) { return String(s || '').toLowerCase(); }
function matches(q, text) { const t = words(text); return words(q).split(/\s+/).filter(Boolean).every((w) => t.indexOf(w) >= 0); }
const ACTIONS = [
  { id: 'theme-system', title: 'Use the system theme', sub: 'Appearance', run: () => setTheme('system') },
  { id: 'theme-light', title: 'Use the light theme', sub: 'Appearance', run: () => setTheme('light') },
  { id: 'theme-dark', title: 'Use the dark theme', sub: 'Appearance', run: () => setTheme('dark') },
  { id: 'labels', title: 'Show or hide navigation labels', sub: 'Shortcut [', run: () => { const v = !navLabels.get(); navLabels.set(v); prefs.set('nav', v ? 'labels' : 'icons'); } },
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
  ['j / k', 'Next / previous item in the side list'], ['[', 'Show or hide navigation labels'], ['Esc', 'Close a dialog'],
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
  const links = Array.from(document.querySelectorAll('.rail .rlist a'));
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
      if (e.key === '[') { const v = !navLabels.get(); navLabels.set(v); prefs.set('nav', v ? 'labels' : 'icons'); return; }
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
