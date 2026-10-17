// Shared components: icons, status badges, copy buttons, tabs, dialogs, toasts, empty and loading states, a sortable
// (and virtual-scrolling) table, code blocks, Markdown and Mermaid diagrams with zoom and pan.
import { useEffect, useRef, useState, useMemo, useLayoutEffect } from '/vendor/hooks.module.js';
import { html } from './html.js';
import { copyText, toast, toasts, useStore, EXPORT, store } from './core.js';

// ------------------------------------------------------------------ icons (hand-drawn, 24×24, stroke = currentColor)
const P = {
  today: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
  tickets: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 9h8M8 13h5"/>',
  timesheet: '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 5v15"/>',
  knowledge: '<path d="M5 5h6v14H5zM13 5h6v14h-6z"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4-4"/>',
  health: '<path d="M3 12h4l2-5 4 10 2-5h6"/>',
  setup: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="m3.5 6 1.2 1.2L7 5M3.5 12l1.2 1.2L7 11M3.5 18l1.2 1.2L7 17"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/>',
  ok: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  bad: '<circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5v.5"/>',
  warn: '<path d="M12 3.5 2.5 20h19z"/><path d="M12 10v4.5M12 17.2v.3"/>',
  off: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  wait: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  left: '<path d="m14.5 6-6 6 6 6"/>', right: '<path d="m9.5 6 6 6-6 6"/>',
  down: '<path d="m6 9.5 6 6 6-6"/>', up: '<path d="m6 14.5 6-6 6 6"/>',
  download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  moon: '<path d="M19.5 14.5A7.5 7.5 0 0 1 9.5 4.5a7.5 7.5 0 1 0 10 10z"/>',
  system: '<rect x="3.5" y="5" width="17" height="11" rx="1.5"/><path d="M9 20h6M12 16v4"/>',
  rail: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M9 4.5v15"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.5M9.5 10h.5M13 10h.5M16.5 10h1M7 14h10"/>',
  zoomin: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4-4M8.5 11h5M11 8.5v5"/>',
  zoomout: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4-4M8.5 11h5"/>',
  reset: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3M4.5 4.5v4h4"/>',
  branch: '<circle cx="6" cy="5.5" r="2"/><circle cx="6" cy="18.5" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7.5v9M18 10c0 4-6 3-11.2 6.7"/>',
  db: '<ellipse cx="12" cy="6" rx="7" ry="2.5"/><path d="M5 6v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5"/>',
  api: '<path d="M8 7 3 12l5 5M16 7l5 5-5 5M13.5 5l-3 14"/>',
  plug: '<path d="M9 3v5M15 3v5M6.5 8h11v3a5.5 5.5 0 0 1-11 0zM12 16.5V21"/>',
  capture: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3" fill="currentColor"/>',
  chat: '<path d="M4.5 5.5h15v10h-9l-4.5 3.5v-3.5h-1.5z"/><path d="M8.5 10.5h7"/>',
  dash: '<path d="M7 12h10"/>',
  alert: '<path d="M12 6.5v7M12 17.5v.5"/>',
  file: '<path d="M6 3.5h8l4 4V20.5H6z"/><path d="M14 3.5v4h4"/>',
  day: '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M8.5 3v4M15.5 3v4"/>',
};
export function Icon({ name, size, label }) {
  const s = size || 18;
  return html`<svg class="ic" width=${s} height=${s} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden=${label ? null : 'true'} role=${label ? 'img' : null} aria-label=${label || null}
    dangerouslySetInnerHTML=${{ __html: P[name] || P.info }}></svg>`;
}

// ------------------------------------------------------------------ status: icon + words, never colour alone
const KIND_ICON = { ok: 'ok', warn: 'warn', bad: 'bad', off: 'off', info: 'info', wait: 'wait' };
export function Status({ kind, children, quiet }) {
  const k = KIND_ICON[kind] ? kind : 'info';
  return html`<span class=${'status ' + k + (quiet ? ' quiet' : '')}><${Icon} name=${KIND_ICON[k]} size=${15} />${children}</span>`;
}

// ------------------------------------------------------------------ copy
export function CopyButton({ text, label, what, quiet, primary, kbd }) {
  const [done, setDone] = useState(false);
  const timer = useRef(0);
  useEffect(() => () => clearTimeout(timer.current), []);
  const go = (e) => {
    e.preventDefault();
    copyText(text == null ? '' : String(text)).then(() => {
      setDone(true);
      toast('Copied' + (what ? ' ' + what : '') + ' to the clipboard', 'ok');
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setDone(false), 1600);
    }, () => toast("Couldn't copy. Select the text and press Ctrl+C.", 'bad'));
  };
  const word = done ? 'Copied' : (label || 'Copy');
  return html`<button type="button" class=${'btn copy' + (quiet ? ' quiet' : '') + (primary ? ' primary' : '') + (done ? ' done' : '')}
    onClick=${go} aria-label=${quiet ? word + (what ? ' ' + what : '') : null}>
    <${Icon} name=${done ? 'check' : 'copy'} size=${15} />${quiet ? null : html`<span>${word}</span>`}${kbd ? html`<kbd>${kbd}</kbd>` : null}</button>`;
}

export function CodeBlock({ code, label, what }) {
  return html`<div class="code"><div class="code-head"><span class="muted small">${label || ''}</span><${CopyButton} text=${code} what=${what || label || 'code'} /></div>
    <pre tabindex="0" aria-label=${label || 'Code'}><code>${code}</code></pre></div>`;
}

// ------------------------------------------------------------------ toasts (one polite live region, outside the views)
export function Toasts() {
  const list = useStore(toasts);
  return html`<div class="toasts" role="status" aria-live="polite" aria-atomic="false">
    ${list.map((t) => html`<div key=${t.id} class=${'toast ' + t.kind}><${Icon} name=${t.kind === 'bad' ? 'bad' : t.kind === 'ok' ? 'check' : 'info'} size=${16} />${t.text}</div>`)}</div>`;
}

// ------------------------------------------------------------------ empty / loading / error
export function Empty({ title, children, icon }) {
  return html`<div class="empty"><${Icon} name=${icon || 'info'} size=${22} /><div><p class="empty-t">${title}</p>${children ? html`<p class="muted">${children}</p>` : null}</div></div>`;
}
export function Skeleton({ lines }) {
  return html`<div class="skeleton" aria-busy="true" aria-label="Loading">${Array.from({ length: lines || 4 }, (_, i) => html`<span key=${i} style=${{ width: (92 - i * 13) + '%' }}></span>`)}</div>`;
}
export function ErrorBox({ error }) {
  return html`<div class="errorbox" role="alert"><${Icon} name="bad" size=${20} /><div><p><strong>${error.status === 404 ? 'Not found' : "This page couldn't load"}</strong></p><p>${error.message || String(error)}</p></div></div>`;
}
// Renders loading / error / data states of a useApi() result.
export function Load({ res, lines, children }) {
  if (res.error && !res.data) return html`<${ErrorBox} error=${res.error} />`;
  if (!res.data) return html`<${Skeleton} lines=${lines} />`;
  return children(res.data);
}

// ------------------------------------------------------------------ tabs (WAI-ARIA tabs, automatic activation)
export function Tabs({ tabs, active, onChange, label, idBase }) {
  const refs = useRef({});
  const keydown = (e) => {
    const ids = tabs.map((t) => t.id);
    let i = ids.indexOf(active);
    if (e.key === 'ArrowRight') i = (i + 1) % ids.length;
    else if (e.key === 'ArrowLeft') i = (i - 1 + ids.length) % ids.length;
    else if (e.key === 'Home') i = 0;
    else if (e.key === 'End') i = ids.length - 1;
    else return;
    e.preventDefault();
    onChange(ids[i]);
    setTimeout(() => refs.current[ids[i]] && refs.current[ids[i]].focus(), 0);
  };
  return html`<div class="tabs" role="tablist" aria-label=${label} onKeyDown=${keydown}>
    ${tabs.map((t) => html`<button key=${t.id} type="button" role="tab" id=${idBase + '-tab-' + t.id} aria-controls=${idBase + '-panel'}
      aria-selected=${t.id === active ? 'true' : 'false'} tabindex=${t.id === active ? 0 : -1} ref=${(el) => { refs.current[t.id] = el; }}
      onClick=${() => onChange(t.id)}>${t.label}${t.count != null ? html`<span class="tabcount">${t.count}</span>` : null}${t.alert ? html`<${Icon} name="warn" size=${14} label="needs attention" />` : null}</button>`)}
  </div>`;
}
export function TabPanel({ idBase, active, children }) {
  return html`<div class="tabpanel" role="tabpanel" id=${idBase + '-panel'} aria-labelledby=${idBase + '-tab-' + active} tabindex="0">${children}</div>`;
}

// ------------------------------------------------------------------ dialog (native <dialog>: modal, focus contained, Esc closes)
export function Dialog({ open, title, onClose, children, wide, className }) {
  const ref = useRef(null);
  const opener = useRef(null);
  useLayoutEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) { opener.current = document.activeElement; d.showModal(); }
    if (!open && d.open) d.close();
  }, [open]);
  useEffect(() => {
    const d = ref.current;
    const onclose = () => { onClose && onClose(); const o = opener.current; if (o && o.focus) setTimeout(() => o.focus(), 0); };
    d.addEventListener('close', onclose);
    return () => d.removeEventListener('close', onclose);
  }, [onClose]);
  const backdrop = (e) => { if (e.target === ref.current) ref.current.close(); };
  return html`<dialog ref=${ref} class=${'dlg' + (wide ? ' wide' : '') + (className ? ' ' + className : '')} aria-labelledby="dlg-title" onClick=${backdrop}>
    ${open ? html`<div class="dlg-in"><div class="dlg-head"><h2 id="dlg-title">${title}</h2>
      <button type="button" class="btn quiet icon" onClick=${() => ref.current.close()} aria-label="Close"><${Icon} name="close" /></button></div>${children}</div>` : null}
  </dialog>`;
}

// ------------------------------------------------------------------ sortable table with optional virtual scrolling
// columns: [{id, label, value(row) for sorting, render(row), num, cls, hideSm}]
const ROW_H = 44;
export function DataTable({ columns, rows, rowKey, sort, onSort, caption, empty, virtual, maxHeight, rowClass }) {
  const [own, setOwn] = useState(sort || null);
  const s = sort !== undefined && onSort ? sort : own;
  const setS = onSort || setOwn;
  const sorted = useMemo(() => {
    if (!s) return rows;
    const col = columns.find((c) => c.id === s.id);
    if (!col || !col.value) return rows;
    const dir = s.dir === 'desc' ? -1 : 1;
    return rows.slice().sort((a, b) => {
      const x = col.value(a), y = col.value(b);
      return (x < y ? -1 : x > y ? 1 : 0) * dir;
    });
  }, [rows, s && s.id, s && s.dir, columns]);
  const wrap = useRef(null);
  const [scroll, setScroll] = useState(0);
  const [viewH, setViewH] = useState(maxHeight || 640);
  const useVirtual = virtual && sorted.length > 120;
  useEffect(() => {
    if (!useVirtual || !wrap.current) return;
    setViewH(wrap.current.clientHeight || maxHeight || 640);
  }, [useVirtual]);
  let first = 0, last = sorted.length;
  if (useVirtual) {
    first = Math.max(0, Math.floor(scroll / ROW_H) - 10);
    last = Math.min(sorted.length, first + Math.ceil(viewH / ROW_H) + 20);
  }
  const head = columns.map((c) => {
    const on = s && s.id === c.id;
    const aria = on ? (s.dir === 'desc' ? 'descending' : 'ascending') : 'none';
    return html`<th key=${c.id} scope="col" class=${(c.num ? 'num ' : '') + (c.hideSm ? 'hide-sm ' : '') + (c.cls || '')} aria-sort=${c.value ? aria : null}>
      ${c.value ? html`<button type="button" class="sort" onClick=${() => setS({ id: c.id, dir: on && s.dir === 'asc' ? 'desc' : 'asc' })}>
        ${c.label}<${Icon} name=${on && s.dir === 'desc' ? 'down' : 'up'} size=${14} /></button>` : c.label}</th>`;
  });
  if (!rows.length) return empty || html`<${Empty} title="Nothing to show" />`;
  return html`<div class="tablewrap" ref=${wrap} tabindex=${useVirtual ? 0 : null} role=${useVirtual ? 'region' : null} aria-label=${useVirtual ? caption : null}
      style=${useVirtual ? { maxHeight: (maxHeight || 640) + 'px', overflowY: 'auto' } : null} onScroll=${useVirtual ? (e) => setScroll(e.currentTarget.scrollTop) : null}>
    <table class="dt">${caption ? html`<caption class="sr">${caption}</caption>` : null}
      <thead><tr>${head}</tr></thead>
      <tbody>
        ${useVirtual && first > 0 ? html`<tr class="spacer" aria-hidden="true" style=${{ height: first * ROW_H + 'px' }}></tr>` : null}
        ${sorted.slice(first, last).map((r) => html`<tr key=${rowKey(r)} class=${rowClass ? rowClass(r) : null}>${columns.map((c) => html`<td key=${c.id} class=${(c.num ? 'num ' : '') + (c.hideSm ? 'hide-sm ' : '') + (c.cls || '')}>${c.render ? c.render(r) : c.value(r)}</td>`)}</tr>`)}
        ${useVirtual && last < sorted.length ? html`<tr class="spacer" aria-hidden="true" style=${{ height: (sorted.length - last) * ROW_H + 'px' }}></tr>` : null}
      </tbody></table></div>`;
}

// A horizontally scrollable region that keyboard users can reach (axe: scrollable-region-focusable).
export function Scroll({ label, children }) {
  return html`<div class="scroll" tabindex="0" role="region" aria-label=${label}>${children}</div>`;
}

// ------------------------------------------------------------------ Markdown (db-flow.md, README): escaped, no raw HTML
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function inline(s) {
  const codes = [];
  s = String(s).replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return '\u0000' + (codes.length - 1) + '\u0000'; });
  s = esc(s);
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[\s(])_([^_]+)_(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, t, u) => '<span class="ref" title="' + u + '">' + t + '</span>');
  s = s.replace(/&lt;br\s*\/?&gt;/g, '<br>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => '<code>' + esc(codes[+i]) + '</code>');
}
function splitRow(line) {
  let t = line.trim();
  if (t[0] === '|') t = t.slice(1);
  if (t[t.length - 1] === '|' && t[t.length - 2] !== '\\') t = t.slice(0, -1);
  const cells = [];
  let cur = '';
  for (let i = 0; i < t.length; i++) {
    if (t[i] === '\\' && t[i + 1] === '|') { cur += '|'; i++; continue; }
    if (t[i] === '|') { cells.push(cur.trim()); cur = ''; continue; }
    cur += t[i];
  }
  cells.push(cur.trim());
  return cells;
}
// -> [{html}, {mermaid: code}, {code, lang}] segments
export function mdSegments(text, keepTitle) {
  const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
  const segs = [];
  let out = [], para = [], i = 0, inComment = false, tables = 0;
  const flush = () => { if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; } };
  const push = (seg) => { flush(); if (out.length) segs.push({ html: out.join('\n') }); out = []; segs.push(seg); };
  while (i < lines.length) {
    const ln = lines[i];
    if (inComment) { if (ln.indexOf('-->') >= 0) inComment = false; i++; continue; }
    if (/^\s*<!--/.test(ln)) { flush(); if (ln.indexOf('-->') < 0) inComment = true; i++; continue; }
    const f = /^\s*(```|~~~)\s*([\w-]*)/.exec(ln);
    if (f) {
      const buf = [];
      i++;
      while (i < lines.length && lines[i].trim().indexOf(f[1]) !== 0) { buf.push(lines[i]); i++; }
      i++;
      push(f[2] === 'mermaid' ? { mermaid: buf.join('\n') } : { code: buf.join('\n'), lang: f[2] });
      continue;
    }
    const hm = /^(#{1,6})\s+(.*)$/.exec(ln);
    if (hm) {
      flush();
      if (hm[1].length === 1 && !keepTitle) { i++; continue; }
      const lvl = Math.min(6, hm[1].length + (keepTitle ? 1 : 0));
      out.push('<h' + lvl + '>' + inline(hm[2]) + '</h' + lvl + '>');
      i++; continue;
    }
    if (/^\s*\|/.test(ln) && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
      flush();
      const head = splitRow(ln), rows = [];
      i += 2;
      while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(splitRow(lines[i])); i++; }
      tables++;
      out.push('<div class="scroll" tabindex="0" role="region" aria-label="Table ' + tables + '"><table><thead><tr>' + head.map((c) => '<th>' + inline(c) + '</th>').join('') +
        '</tr></thead><tbody>' + rows.map((r) => '<tr>' + r.map((c) => '<td>' + inline(c) + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>');
      continue;
    }
    const li = /^\s*([-*]|\d+\.)\s+(.*)$/.exec(ln);
    if (li) {
      flush();
      const ordered = /\d/.test(li[1]), items = [];
      while (i < lines.length) {
        const m2 = /^\s*([-*]|\d+\.)\s+(.*)$/.exec(lines[i]);
        if (!m2) break;
        items.push(m2[2].replace(/^\[( |x)\]\s*/i, (_, x) => (x.trim() ? '[x] ' : '[ ] ')));
        i++;
      }
      out.push((ordered ? '<ol>' : '<ul>') + items.map((t) => '<li>' + inline(t) + '</li>').join('') + (ordered ? '</ol>' : '</ul>'));
      continue;
    }
    if (/^\s*(---|\*\*\*)\s*$/.test(ln) || !ln.trim()) { flush(); i++; continue; }
    para.push(ln.trim());
    i++;
  }
  flush();
  if (out.length) segs.push({ html: out.join('\n') });
  return segs;
}
export function Markdown({ text, keepTitle }) {
  const segs = useMemo(() => mdSegments(text, keepTitle), [text]);
  return html`<div class="md">${segs.map((s, i) => s.mermaid != null ? html`<${Diagram} key=${i} code=${s.mermaid} label=${'Diagram ' + (i + 1)} />`
    : s.code != null ? html`<${CodeBlock} key=${i} code=${s.code} label=${s.lang || ''} what="code" />`
      : html`<div key=${i} dangerouslySetInnerHTML=${{ __html: s.html }}></div>`)}</div>`;
}

// ------------------------------------------------------------------ Mermaid: loaded on first use, themed, zoom + pan
export const themeMode = store(document.documentElement.getAttribute('data-theme') || 'system');
export function effectiveDark() {
  const t = themeMode.get();
  if (t === 'dark') return true;
  if (t === 'light') return false;
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
}
let mmdLoad = null, mmdTheme = null, mmdSeq = 0, mmdQueue = Promise.resolve();
const mmdCache = new Map();
function loadMermaid() {
  if (window.mermaid) return Promise.resolve(window.mermaid);
  if (EXPORT) return Promise.reject(new Error('Mermaid is not included in this export'));
  if (!mmdLoad) {
    mmdLoad = new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = '/vendor/mermaid.min.js';
      s.onload = () => (window.mermaid ? res(window.mermaid) : rej(new Error('Mermaid did not load')));
      s.onerror = () => { mmdLoad = null; rej(new Error('Mermaid is not available')); };
      document.head.appendChild(s);
    });
  }
  return mmdLoad;
}
function renderSvg(code, dark) {
  const theme = dark ? 'dark' : 'neutral';
  const key = theme + '\u0001' + code;
  if (mmdCache.has(key)) return Promise.resolve(mmdCache.get(key));
  // one render at a time: Mermaid keeps global state
  const job = mmdQueue.then(() => loadMermaid()).then((mermaid) => {
    if (mmdTheme !== theme) {
      mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme, fontFamily: 'inherit', flowchart: { htmlLabels: false },
        themeVariables: dark ? { edgeLabelBackground: '#2D323B', tertiaryTextColor: '#E8EAED' } : { edgeLabelBackground: '#F3F5F7' } });
      mmdTheme = theme;
    }
    return mermaid.render('mmd' + (++mmdSeq), code).then((r) => { mmdCache.set(key, r.svg); return r.svg; }, (e) => {
      document.querySelectorAll('body > [id^="dmmd"], body > svg[id^="mmd"]').forEach((x) => x.remove());
      throw e;
    });
  });
  mmdQueue = job.catch(() => null);
  return job;
}
export function Diagram({ code, label, quiet, alt }) {
  const [state, setState] = useState({ svg: null, error: null });
  const theme = useStore(themeMode);
  const [view, setView] = useState({ k: 1, x: 0, y: 0 });
  const box = useRef(null);
  const drag = useRef(null);
  useEffect(() => {
    let live = true;
    renderSvg(code, effectiveDark()).then((svg) => live && setState({ svg, error: null }), (e) => live && setState({ svg: null, error: e }));
    return () => { live = false; };
  }, [code, theme]);
  const zoom = (f) => setView((v) => ({ ...v, k: Math.max(0.4, Math.min(4, v.k * f)) }));
  const key = (e) => {
    const step = 40;
    if (e.key === '+' || e.key === '=') zoom(1.2);
    else if (e.key === '-') zoom(1 / 1.2);
    else if (e.key === '0') setView({ k: 1, x: 0, y: 0 });
    else if (e.key === 'ArrowLeft') setView((v) => ({ ...v, x: v.x + step }));
    else if (e.key === 'ArrowRight') setView((v) => ({ ...v, x: v.x - step }));
    else if (e.key === 'ArrowUp') setView((v) => ({ ...v, y: v.y + step }));
    else if (e.key === 'ArrowDown') setView((v) => ({ ...v, y: v.y - step }));
    else return;
    e.preventDefault();
  };
  const down = (e) => { if (e.button !== 0) return; drag.current = { x: e.clientX - view.x, y: e.clientY - view.y }; e.currentTarget.setPointerCapture(e.pointerId); };
  const move = (e) => { if (drag.current) setView((v) => ({ ...v, x: e.clientX - drag.current.x, y: e.clientY - drag.current.y })); };
  const up = () => { drag.current = null; };
  const wheel = (e) => { if (!e.ctrlKey) return; e.preventDefault(); zoom(e.deltaY < 0 ? 1.1 : 1 / 1.1); };
  if (state.error) {
    return quiet ? html`<p class="note">${quiet}</p>`
      : html`<div class="diagram-fail"><p class="note">This diagram couldn't be drawn (${String(state.error.message || state.error).split('\n')[0].slice(0, 160)}). Its source:</p><pre tabindex="0">${code}</pre></div>`;
  }
  return html`<figure class="diagram">
    <div class="diagram-tools" role="toolbar" aria-label=${(label || 'Diagram') + ' controls'}>
      <button type="button" class="btn quiet icon" onClick=${() => zoom(1.2)} aria-label="Zoom in"><${Icon} name="zoomin" /></button>
      <button type="button" class="btn quiet icon" onClick=${() => zoom(1 / 1.2)} aria-label="Zoom out"><${Icon} name="zoomout" /></button>
      <button type="button" class="btn quiet icon" onClick=${() => setView({ k: 1, x: 0, y: 0 })} aria-label="Reset zoom"><${Icon} name="reset" /></button>
      <span class="muted small">${Math.round(view.k * 100)}% · drag to pan, Ctrl+wheel or + and − to zoom</span></div>
    <div class="diagram-view" ref=${box} tabindex="0" role="img" aria-label=${(label || 'Diagram') + '. The source text below describes the same thing.'}
      onKeyDown=${key} onPointerDown=${down} onPointerMove=${move} onPointerUp=${up} onPointerCancel=${up} onWheel=${wheel}>
      ${state.svg ? html`<div class="diagram-canvas" style=${{ transform: 'translate(' + view.x + 'px,' + view.y + 'px) scale(' + view.k + ')' }}
        dangerouslySetInnerHTML=${{ __html: state.svg }}></div>` : html`<${Skeleton} lines=${3} />`}
    </div>
    <details class="diagram-alt"><summary>${alt ? 'Show as text' : 'Diagram source'}</summary>${alt || html`<pre tabindex="0">${code}</pre>`}</details>
  </figure>`;
}

// ------------------------------------------------------------------ misc
export function TicketLink({ k, children }) {
  if (EXPORT) return html`<span class="tkey">${children || k}</span>`;
  return html`<a class="tkey" href=${'#/tickets/' + encodeURIComponent(k)}>${children || k}</a>`;
}
export function Chip({ icon, children, title }) {
  return html`<span class="chip" title=${title || null}>${icon ? html`<${Icon} name=${icon} size=${14} />` : null}${children}</span>`;
}
export function Meter({ value, max, label }) {
  const pct = Math.max(0, Math.min(100, max ? (value / max) * 100 : 0));
  return html`<span class="meter" role="img" aria-label=${label}><span style=${{ width: pct + '%' }}></span></span>`;
}
export function PageHead({ title, sub, children }) {
  return html`<div class="pagehead"><div><h1 id="page-title" tabindex="-1">${title}</h1>${sub ? html`<p class="sub">${sub}</p>` : null}</div>${children ? html`<div class="pagehead-act">${children}</div>` : null}</div>`;
}
