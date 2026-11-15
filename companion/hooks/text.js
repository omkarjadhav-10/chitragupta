// Pure display helpers for the native companion: no `$`, no I/O, no timers.
// Every string the backend or the person supplies passes through `clean` or `scrub` before it is drawn.

export const HIDDEN = 'Title hidden (privacy mode)';
export const MARK = 'CG';
export const MARK_MAX = 12;
export const ACCENTS = ['suggestion', 'claude', 'remember', 'merged', 'none'];
export const TICKET = /^[A-Z][A-Z0-9]*-\d+$/;
export const DB_ID = /^(?:f|i)_[0-9a-f]{8,40}$/;

const ANSI = /\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])/g;
const CONTROL = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f‎‏‪-‮⁦-⁩]/g;
const UNSAFE_MARK = /[\u0000-\u001f\u007f-\u009f‎‏‪-‮⁦-⁩]/;
const URL = /\b(?:https?|ftp|file):\/\/\S+/gi;
const EMAIL = /[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g;
const ABS = /(?<![\w.])(?:[A-Za-z]:[\\/]|\\\\[\w.$]|\/(?:home|Users|usr|etc|var|opt|tmp|mnt|root|srv)\/)\S*/g;

/** One display line: escape sequences and control/bidi characters removed, whitespace folded, bounded. */
export function clean(value, max = 300) {
  const s = String(value ?? '').replace(ANSI, '').replace(CONTROL, ' ').replace(/\s+/g, ' ').trim();
  return s.length > max ? s.slice(0, Math.max(1, max - 1)) + '…' : s;
}

/** A line for overview surfaces: also masks URLs, emails and absolute paths (UX-03). */
export function scrub(value, max = 300) {
  return clean(String(value ?? '').replace(URL, '[link]').replace(EMAIL, '[email]').replace(ABS, '[path]'), max);
}

/** A block whose line breaks matter (a prepared plan excerpt): control characters removed, newlines kept. */
export function block(value, max = 2000) {
  const s = String(value ?? '').replace(ANSI, '').replace(/\r\n?/g, '\n').replace(CONTROL, ' ');
  return s.length > max ? s.slice(0, max) + '\n…' : s;
}

/** MOD-06: the header mark is a short plain string; anything else falls back to the default, said so. */
export function mark(value) {
  if (value === undefined || value === null || value === '') return { text: MARK, valid: true };
  const raw = typeof value === 'string' ? value.trim() : '';
  const ok = raw.length > 0 && raw.length <= MARK_MAX && !UNSAFE_MARK.test(value) && !/\x1b/.test(value);
  return ok ? { text: raw, valid: true } : { text: MARK, valid: false };
}

/** '2026-11-09T10:42:07Z' -> '2026-11-09 10:42 UTC'; anything else is an honest unknown. */
export function when(iso) {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(String(iso || ''));
  return m ? m[1] + ' ' + m[2] + ' UTC' : 'time unknown';
}

export function count(n, one, many) {
  const k = Number.isFinite(n) ? n : 0;
  return k + ' ' + (k === 1 ? one : many || one + 's');
}

/** A measure as words: `3 of 4 (75%)`, or why there is no number. Never a bare 0 for unknown. */
export function measure(m, unknown) {
  if (!m || typeof m !== 'object') return unknown;
  const { numerator: n, denominator: d, percent: p } = m;
  if (Number.isFinite(n) && Number.isFinite(d) && d > 0) return n + ' of ' + d + (Number.isFinite(p) ? ' (' + p + '%)' : '');
  if (Number.isFinite(p)) return p + '%';
  return clean(m.label || m.reason || unknown, 120);
}

/** A ticket argument as typed: trimmed, upper-cased, validated; null when it is not a ticket key. */
export function ticketArg(text) {
  const t = String(text || '').trim().toUpperCase();
  return t.length <= 40 && TICKET.test(t) ? t : null;
}

/** A flow or investigation ID as typed: { flow } or { investigation }, or null. */
export function dbArg(text) {
  const t = String(text || '').trim().toLowerCase();
  if (!DB_ID.test(t)) return null;
  return t.startsWith('f_') ? { flow: t } : { investigation: t };
}
