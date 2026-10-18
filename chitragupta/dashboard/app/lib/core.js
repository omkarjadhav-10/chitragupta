// Data, live updates, routing, formatting and small stores shared by every page. No framework state library: a few
// tiny observable stores and hooks on top of Preact's.
import { useEffect, useState, useRef, useCallback } from '/vendor/hooks.module.js';

// ------------------------------------------------------------------ export mode
// An exported ticket carries its data in <script id="cg-export">; the page then makes no requests at all.
export const EXPORT = (() => {
  try { const el = document.getElementById('cg-export'); return el ? JSON.parse(el.textContent) : null; } catch (e) { return null; }
})();

// ------------------------------------------------------------------ tiny stores
export function store(initial) {
  let value = initial;
  const subs = new Set();
  return {
    get: () => value,
    set(v) { value = typeof v === 'function' ? v(value) : v; subs.forEach((f) => f(value)); },
    sub(f) { subs.add(f); return () => subs.delete(f); },
  };
}
export function useStore(s) {
  const [v, setV] = useState(s.get());
  useEffect(() => s.sub(setV), [s]);
  return v;
}

// ------------------------------------------------------------------ preferences (per browser)
export const prefs = {
  get(k, d) { try { const v = localStorage.getItem('cg-' + k); return v == null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { if (v == null) localStorage.removeItem('cg-' + k); else localStorage.setItem('cg-' + k, v); } catch (e) { /* private window */ } },
};

// ------------------------------------------------------------------ API
export class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}
export function api(path) {
  if (EXPORT) {
    if (path.indexOf('/api/tickets/') === 0) return Promise.resolve(EXPORT);
    return Promise.reject(new ApiError('Not part of this export', 0));
  }
  return fetch(path, { headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin' }).then(
    (r) => r.json().catch(() => ({})).then((j) => {
      if (!r.ok) throw new ApiError(j.error || 'The dashboard answered ' + r.status, r.status);
      return j;
    }),
    () => { throw new ApiError("The dashboard server isn't answering. Start it with /cg:dashboard.", 0); });
}

// Writes: JSON only, with the per-start token the server put in the page (see dashboard/writes.py).
function pageToken() { const m = document.querySelector('meta[name="cg-token"]'); return m ? m.getAttribute('content') : ''; }
export function write(method, path, body) {
  if (EXPORT) return Promise.reject(new ApiError('An exported file is read-only', 0));
  return fetch(path, {
    method, cache: 'no-store', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-Chitragupta-Token': pageToken() },
    body: JSON.stringify(body || {}),
  }).then((r) => r.json().catch(() => ({})).then((j) => {
    if (!r.ok) {
      const e = new ApiError(j.error || 'The dashboard answered ' + r.status, r.status);
      e.fields = j.fields || null;
      throw e;
    }
    return j;
  }), () => { throw new ApiError("The dashboard server isn't answering. Start it with /cg:dashboard.", 0); });
}
export function runAction(name, body) { return write('POST', '/api/actions/' + name, body); }

// ------------------------------------------------------------------ live updates (SSE, polling fallback)
// One cursor for the whole page: it moves when any file the dashboard reads changes. Pages refetch when it moves.
export const cursor = store(null);
export const connection = store(EXPORT ? 'export' : 'connecting');  // live | polling | offline | export
let es = null, pollTimer = null, backoff = 3000;

function setCursor(c) { if (c && c !== cursor.get()) cursor.set(c); }
function poll() {
  clearTimeout(pollTimer);
  if (document.hidden) return;
  api('/api/changes').then((j) => { setCursor(j.cursor); connection.set('polling'); }, () => connection.set('offline'))
    .finally(() => { pollTimer = setTimeout(poll, 10000); });
}
function openStream() {
  if (EXPORT || es || document.hidden) return;
  if (!window.EventSource) { poll(); return; }
  es = new EventSource('/api/events');
  es.addEventListener('cursor', (ev) => {
    backoff = 3000;
    clearTimeout(pollTimer);
    connection.set('live');
    try { setCursor(JSON.parse(ev.data).cursor); } catch (e) { /* ignore a bad event */ }
  });
  es.onerror = () => {
    if (es && es.readyState === EventSource.CLOSED) {  // refused (e.g. 503) or the server went away: poll, retry later
      es = null;
      poll();
      setTimeout(openStream, backoff);
      backoff = Math.min(backoff * 2, 60000);
    } else {
      connection.set('polling');
    }
  };
}
function closeStream() { if (es) { es.close(); es = null; } clearTimeout(pollTimer); }
export function startLive() {
  if (EXPORT) return;
  document.addEventListener('visibilitychange', () => (document.hidden ? closeStream() : openStream()));
  openStream();
}

// useApi(path): data for a page; refetches quietly whenever the live cursor moves, keeping the old data on screen.
export function useApi(path, opts) {
  const live = !(opts && opts.live === false);
  const [st, setSt] = useState({ data: null, error: null, loading: true });
  const seq = useRef(0);
  const load = useCallback((quiet) => {
    if (!path) return;
    const n = ++seq.current;
    if (!quiet) setSt((s) => ({ data: s.path === path ? s.data : null, error: null, loading: true, path }));
    api(path).then((data) => { if (n === seq.current) setSt({ data, error: null, loading: false, path }); },
      (error) => { if (n === seq.current) setSt((s) => ({ data: quiet ? s.data : null, error, loading: false, path })); });
  }, [path]);
  useEffect(() => { load(false); }, [load]);
  useEffect(() => (live ? cursor.sub(() => load(true)) : undefined), [load, live]);
  return { ...st, reload: () => load(false) };
}

// ------------------------------------------------------------------ routing (hash URLs)
export const PAGES = ['today', 'tickets', 'timesheet', 'knowledge', 'search', 'health', 'setup', 'settings', 'day'];
export function parseRoute(hash) {
  if (EXPORT) return { name: 'tickets', arg: String(EXPORT.key || ''), params: {} };
  const raw = (hash == null ? location.hash : hash).replace(/^#\/?/, '');
  const qi = raw.indexOf('?');
  const path = qi >= 0 ? raw.slice(0, qi) : raw;
  const params = {};
  new URLSearchParams(qi >= 0 ? raw.slice(qi + 1) : '').forEach((v, k) => { params[k] = v; });
  const parts = path.split('/').filter(Boolean).map((p) => { try { return decodeURIComponent(p); } catch (e) { return p; } });
  let name = parts[0] || 'today';
  if (name === 'ticket') name = 'tickets';
  if (PAGES.indexOf(name) < 0) name = 'missing';
  const arg = parts[1] ? (name === 'tickets' ? parts[1].toUpperCase() : parts[1]) : '';
  return { name, arg, params, path };
}
export const route = store(parseRoute());
window.addEventListener('hashchange', () => route.set(parseRoute()));
export function href(name, arg, params) {
  let h = '#/' + name + (arg ? '/' + encodeURIComponent(arg) : '');
  const q = new URLSearchParams();
  Object.keys(params || {}).forEach((k) => { if (params[k] !== '' && params[k] != null && params[k] !== false) q.set(k, params[k]); });
  const s = q.toString();
  return h + (s ? '?' + s : '');
}
export function go(h) { if (location.hash !== h) location.hash = h; }
// Change query params without a history entry or a scroll jump (filters, tabs, search text).
export function setParams(patch) {
  const r = route.get();
  const h = href(r.name, r.arg, Object.assign({}, r.params, patch));
  try { history.replaceState(null, '', h); } catch (e) { /* file:// export */ }
  route.set(parseRoute(h));
}

// ------------------------------------------------------------------ toasts (announced in a polite live region)
export const toasts = store([]);
let toastSeq = 0;
export function toast(text, kind) {
  const id = ++toastSeq;
  toasts.set((l) => l.concat([{ id, text, kind: kind || 'info' }]).slice(-4));
  setTimeout(() => toasts.set((l) => l.filter((t) => t.id !== id)), kind === 'bad' ? 8000 : 3500);
}

// ------------------------------------------------------------------ clipboard
export function copyText(text) {
  const fallback = () => new Promise((res, rej) => {
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.top = '-1000px';
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    ok ? res() : rej(new Error('copy failed'));
  });
  return navigator.clipboard && navigator.clipboard.writeText ? navigator.clipboard.writeText(text).catch(fallback) : fallback();
}

// ------------------------------------------------------------------ formatting
export function pad(n) { return (n < 10 ? '0' : '') + n; }
export function fmtMin(m) {
  m = Math.round(m || 0);
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60), r = m % 60;
  return h + 'h ' + pad(r) + 'm';
}
export function clock(m) { m = Math.round(m || 0); return Math.floor(m / 60) + ':' + pad(m % 60); }
export function fmtHours(m) { return ((m || 0) / 60).toFixed(1).replace(/\.0$/, ''); }
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const MONTH = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const WD = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
export function parseDay(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ''));  // a full timestamp counts as its date
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
}
export function isoDay(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
export function addDays(s, n) { const d = parseDay(s); d.setDate(d.getDate() + n); return isoDay(d); }
export function fmtDay(s, short) {
  const d = parseDay(s);
  if (!d) return s || '—';
  return short ? WD[d.getDay()].slice(0, 3) + ' ' + d.getDate() + ' ' + MON[d.getMonth()] : WD[d.getDay()] + ' ' + d.getDate() + ' ' + MONTH[d.getMonth()];
}
export function monthName(ym) { const p = ym.split('-'); return MONTH[+p[1] - 1] + ' ' + p[0]; }
export function parseTs(s) { if (!s) return null; const d = new Date(s); return isNaN(d) ? null : d; }
export function fmtTs(s) {
  const d = parseTs(s);
  if (!d) return s || '—';
  const t = pad(d.getHours()) + ':' + pad(d.getMinutes());
  return d.toDateString() === new Date().toDateString() ? t : d.getDate() + ' ' + MON[d.getMonth()] + ' ' + t;
}
export function ago(min) {
  if (min == null) return 'never';
  if (min < 1) return 'just now';
  if (min < 60) return min + ' min ago';
  if (min < 48 * 60) return Math.round(min / 60) + ' h ago';
  return Math.round(min / 1440) + ' days ago';
}
export function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }
export function mins(hhmm) { const p = String(hhmm || '0:0').split(':'); return +p[0] * 60 + +p[1]; }
export function isHttp(u) { return typeof u === 'string' && /^https?:\/\/[^\s"'<>]+$/i.test(u); }

// Stable colour slot per ticket (t1…t6), so a ticket keeps its colour across the page.
export function slotFor(keys) {
  const map = {};
  keys.forEach((k, i) => { map[k] = k ? 't' + ((i % 6) + 1) : 'to'; });
  return map;
}
