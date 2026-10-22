// The ticket brief (FX-20): what the ticket page opens on. Rendered only from the API's `brief` (brief.view on the
// server): the header line, the "new findings" bar with Refresh, then Root cause, Helpful findings, How it works
// today / after the fix, Proposed fix, Fixes made, Possible cons and one quiet "All details" line. Every claim carries
// evidence chips that jump to the item, query, request or branch it came from. Labels are words, never colour alone.
import { useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { EXPORT, fmtTs, fmtDay, plural, runAction, toast } from '../lib/core.js';
import { Icon, Status, Chip, TicketLink } from '../lib/ui.js';

const ORDER = ['root_cause', 'findings', 'before', 'after', 'proposed', 'fixes_made', 'cons'];
const HEAD = { root_cause: 'Root cause', findings: 'Helpful findings', before: 'How it works today', after: 'How it works after the fix',
  proposed: 'Proposed fix', fixes_made: 'Fixes made', cons: 'Possible cons' };
const CONF = { confirmed: ['ok', 'Confirmed'], likely: ['info', 'Likely'], hypothesis: ['warn', 'Hypothesis'], none: ['off', 'Not known'] };
const LAB = { recorded: ['Recorded:', 'rec'], suggested: ['Suggested:', 'sug'], 'ruled-out': ['Ruled out:', 'ro'] };
const STAGE = { 'in release': 'ok', merged: 'info', 'pr open': 'wait', pushed: 'off', committed: 'off', 'not started': 'off' };
const ITEM_KIND = { root_cause: 'root cause', how_it_works: 'how it works', repro_test: 'test', todo: 'to do' };
const PREFIX = ['commit', 'pr', 'jira', 'query', 'table', 'replay', 'test', 'raw', 'note'];

// ------------------------------------------------------------------ evidence ("cite") chips
// A cite is an id ("c607fa7d13", "commit:a1b2c3d", "query:<id>", …) or {id, label, kind}.
export function citeOf(c, items) {
  const id = String(typeof c === 'string' ? c : (c && c.id) || '');
  const i = id.indexOf(':');
  const pre = i > 0 ? id.slice(0, i) : '';
  const kind = (c && typeof c === 'object' && c.kind) || (PREFIX.indexOf(pre) >= 0 ? pre : 'item');
  const rest = PREFIX.indexOf(pre) >= 0 ? id.slice(i + 1) : id;
  let label = c && typeof c === 'object' ? c.label : '';
  if (!label) {
    const it = (kind === 'item' || kind === 'note' || kind === 'test') && items ? items[rest] : null;
    label = it ? (ITEM_KIND[it.kind] || String(it.kind || 'item').replace(/_/g, ' ')) + (it.date ? ', ' + fmtDay(it.date, true).slice(4) : '')
      : { commit: rest.slice(0, 7), pr: 'PR #' + rest, jira: rest, query: 'query', table: rest, replay: 'replay step ' + rest, test: 'test',
        raw: 'session log', note: 'your note' }[kind] || 'item ' + rest.slice(0, 6);
  }
  return { id, kind, rest, label };
}
// Where a chip leads: [tab, element id] (null: shown, not a link: a raw log line or a Jira key)
function citeTarget(ci) {
  if (ci.kind === 'item' || ci.kind === 'note' || ci.kind === 'test') return ['overview', 'i-' + ci.rest];
  if (ci.kind === 'query') return ['queries', 'q-' + ci.rest];
  if (ci.kind === 'commit' || ci.kind === 'pr') return ['branches', null];
  if (ci.kind === 'table') return ['dbflow', null];
  if (ci.kind === 'replay') return ['requests', 'rq-' + ci.rest];
  return null;
}
function Cite({ c, ctx }) {
  const ci = citeOf(c, ctx.items);
  const tg = citeTarget(ci);
  if (!tg) return html`<span class="cite static" title=${'Evidence: ' + ci.id}><span class="sr">Evidence: </span>${ci.label}</span>`;
  return html`<a class="cite" href=${ctx.href(tg[0])} title=${'Evidence: ' + ci.id + ' (show it)'}
    onClick=${(e) => { e.preventDefault(); ctx.jump(tg[0], tg[1]); }}><span class="sr">Evidence: </span>${ci.label}</a>`;
}
function Cites({ list, ctx }) {
  const cs = list || [];
  return html`<span class="cites">${cs.map((c, i) => html`<${Cite} key=${i} c=${c} ctx=${ctx} />`)}</span>`;
}
function uniq(lists) {
  const seen = {}, out = [];
  lists.forEach((l) => (l || []).forEach((c) => { const k = typeof c === 'string' ? c : (c && c.id) || ''; if (!seen[k]) { seen[k] = 1; out.push(c); } }));
  return out;
}

// ------------------------------------------------------------------ the header line (in the page's ticket header)
export function BriefMeta({ t }) {
  const h = ((t.brief || {}).header) || {};
  const st = h.stage || (t.delivery ? { kind: t.delivery.kind, text: t.delivery.text } : null);
  const js = h.jira_status || t.jira_status, inf = h.status_inferred || t.status_inferred;
  const pr = h.pr;
  const where = [h.repo ? html`repo <span class="mono">${h.repo}</span>` : null, h.branch ? html`branch <span class="mono">${h.branch}</span>` : null,
    pr && pr.number != null ? html`PR #${pr.number}${pr.state ? ' ' + pr.state : ''}` : null].filter(Boolean);
  return html`<div class="bmeta">
    ${h.jira_type ? html`<${Chip} title="Jira issue type">${h.jira_type}</${Chip}>` : null}
    ${js ? html`<${Status} kind="info">Jira: ${js}</${Status}>` : null}
    ${inf ? html`<${Status} kind="off">Inferred: ${inf}</${Status}>` : null}
    ${!js && !inf && t.status ? html`<${Chip} title="Status">${t.status}</${Chip}>` : null}
    ${st ? html`<${Status} kind=${(t.delivery && (t.delivery.gaps || []).length) ? 'warn' : STAGE[st.kind] || 'off'}>Delivery: ${st.text || st.kind}</${Status}>` : null}
    ${where.length ? html`<span class="small">${where.map((w, i) => html`${i ? ' · ' : ''}${w}`)}</span>` : null}
    <span class="grow"></span>
    ${h.updated ? html`<span class="small muted">Updated ${fmtTs(h.updated)}</span>` : null}
  </div>`;
}

// ------------------------------------------------------------------ sections
function sectionsOf(b) {
  const s = b.sections || [];
  if (Array.isArray(s)) return s.filter((x) => x && x.id);
  const keys = ORDER.filter((k) => s[k]).concat(Object.keys(s).filter((k) => ORDER.indexOf(k) < 0));
  return keys.map((k) => Object.assign({ id: k }, s[k]));
}
function isEmpty(s) {
  return !((s.claims || []).length || (s.steps || []).length || (s.list || []).length || (s.summary && s.summary.text)
    || (s.rejected && s.rejected.text) || (s.verified && s.verified.text));
}
function NotYet({ s }) { return html`<p class="bempty">${(s && s.empty) || 'Not recorded yet'}</p>`; }
function Label({ c }) {
  const l = LAB[c.label];
  return l ? html`<span class=${'lab ' + l[1]}>${l[0]}</span> ` : null;
}
function Claims({ list, ctx, plain, big }) {
  return html`<ul class=${'claims' + (plain ? ' plain' : '')}>${(list || []).map((c, i) => html`<li class="claim" key=${i}>
    ${big && i === 0 ? html`<p class="rc">${c.text}</p>` : html`<span class="txt"><${Label} c=${c} />${c.text}</span>`}
    <${Cites} list=${c.cites} ctx=${ctx} /></li>`)}</ul>`;
}
function Sec({ s, children, extra }) {
  return html`<section class="bsec" aria-labelledby=${'b-' + s.id}>
    <h2 id=${'b-' + s.id}><span>${s.heading || HEAD[s.id] || s.id}${s.override ? html` <span class="src">your correction</span>` : null}</span>${extra || null}</h2>
    ${isEmpty(s) ? html`<${NotYet} s=${s} />` : children}</section>`;
}
function RootCause({ s, ctx }) {
  const c = CONF[s.confidence] || null;
  const old = Object.values(ctx.items).filter((i) => i.kind === 'root_cause' && i.status === 'superseded');
  return html`<${Sec} s=${s} extra=${c && !isEmpty(s) ? html`<${Status} kind=${c[0]}>${c[1]}</${Status}>` : null}>
    <${Claims} list=${s.claims} ctx=${ctx} plain big />
    ${old.length ? html`<details class="more"><summary>${plural(old.length, 'earlier root cause')}, superseded</summary>
      <ul class="findings">${old.map((i) => html`<li key=${i.id}><span class="t">Superseded:</span> ${i.title}${i.superseded_by && ctx.items[i.superseded_by]
        ? html` <span class="m">replaced by “${ctx.items[i.superseded_by].title}”</span>` : null}</li>`)}</ul></details>` : null}</${Sec}>`;
}
function StepsCard({ s, ctx }) {
  const v = s.verified;
  return html`<div class="card"><h3 id=${'b-' + s.id}>${s.heading || HEAD[s.id]}</h3>
    ${(s.steps || []).length ? html`<ol>${s.steps.map((x, i) => html`<li key=${i}>${x.text}</li>`)}</ol>` : html`<${NotYet} s=${s} />`}
    ${v && (v.text || v.state) ? html`<p class="ver"><${Status} kind=${v.state === 'verified' ? 'ok' : 'warn'}>${v.state === 'verified' ? 'Verified' : 'Not verified'}</${Status}> ${v.text || ''}</p>` : null}
    <${Cites} list=${uniq((s.steps || []).map((x) => x.cites).concat([v && v.cites]))} ctx=${ctx} /></div>`;
}
function BeforeAfter({ before, after, ctx }) {
  return html`<section class="bsec" aria-labelledby="b-ba"><h2 id="b-ba" class="sr">Before and after the fix</h2>
    <div class="ba">${before ? html`<${StepsCard} s=${before} ctx=${ctx} />` : null}${after ? html`<${StepsCard} s=${after} ctx=${ctx} />` : null}</div></section>`;
}
function Proposed({ s, ctx }) {
  return html`<${Sec} s=${s}><${Claims} list=${s.claims} ctx=${ctx} plain />
    ${s.rejected && s.rejected.text ? html`<ul class="claims plain"><li class="claim"><span class="txt"><span class="lab ro">Rejected:</span> ${s.rejected.text}</span>
      <${Cites} list=${s.rejected.cites} ctx=${ctx} /></li></ul>` : null}</${Sec}>`;
}
function FixesMade({ s, ctx }) {
  return html`<${Sec} s=${s} extra=${html`<span class="src">${s.source || 'from git and PRs'}</span>`}>
    ${(s.list || []).length ? html`<ul class="fixes">${s.list.map((f, i) => html`<li key=${i}>${f.text} <${Cites} list=${f.cites} ctx=${ctx} /></li>`)}</ul>` : null}
    ${s.summary && s.summary.text ? html`<p class="fsum">${s.summary.text} <${Cites} list=${s.summary.cites} ctx=${ctx} /></p>` : null}</${Sec}>`;
}
function Section({ s, ctx }) {
  if (s.id === 'root_cause') return html`<${RootCause} s=${s} ctx=${ctx} />`;
  if (s.id === 'proposed') return html`<${Proposed} s=${s} ctx=${ctx} />`;
  if (s.id === 'fixes_made') return html`<${FixesMade} s=${s} ctx=${ctx} />`;
  return html`<${Sec} s=${s}><${Claims} list=${s.claims} ctx=${ctx} plain=${s.id !== 'findings' && s.id !== 'cons'} /></${Sec}>`;
}

// ------------------------------------------------------------------ refresh, fallback, epic, all details
function RefreshButton({ k, label }) {
  const [busy, setBusy] = useState(false);
  if (EXPORT) return null;
  const go = () => {
    setBusy(true);
    runAction('brief-refresh', { key: k }).then((r) => toast(r && r.skipped ? 'Brief not refreshed: ' + r.skipped : 'Brief refreshed', r && r.skipped ? 'info' : 'ok'),
      (e) => toast(e.message, 'bad')).finally(() => setBusy(false));
  };
  return html`<button type="button" class="btn" disabled=${busy} onClick=${go}><${Icon} name="reset" size=${15} />${busy ? 'Refreshing…' : label || 'Refresh brief'}</button>`;
}
function Fallback({ t, b, ctx }) {
  const f = b.fallback || {};
  const rc = f.root_cause;
  const c = rc && CONF[rc.confidence];
  return html`<div class="alert quietalert" role="note"><${Icon} name="info" size=${18} />
      <p class="grow">No brief yet. It is written at day close${EXPORT ? '' : ', or now'}; until then this shows the current root cause and the TL;DR.</p>
      <${RefreshButton} k=${t.key} label="Write the brief" /></div>
    <section class="bsec" aria-labelledby="b-root_cause"><h2 id="b-root_cause"><span>Root cause</span>${c ? html`<${Status} kind=${c[0]}>${c[1]}</${Status}>` : null}</h2>
      ${rc && rc.text ? html`<ul class="claims plain"><li class="claim"><p class="rc">${rc.text}</p><${Cites} list=${rc.cites} ctx=${ctx} /></li></ul>` : html`<${NotYet} />`}</section>
    ${f.tldr || t.tldr ? html`<section class="bsec" aria-labelledby="b-tldr"><h2 id="b-tldr">In short</h2>
      <p class="lede">${(t.tldr_check && t.tldr_check.length ? t.tldr_check : [{ text: f.tldr || t.tldr }]).map((x, i) => x.outdated
        ? html`<span key=${i}><span class="muted small">${x.outdated}:</span> <s>${x.text}</s> </span>` : x.text + ' ')}</p>
      ${f.problem ? html`<p class="lede">${f.problem}</p>` : null}</section>` : null}`;
}
function Children({ b }) {
  const ch = b.children || [];
  return html`<section class="bsec" aria-labelledby="b-children"><h2 id="b-children">Child tickets</h2>
    ${ch.length ? html`<ul class="findings">${ch.map((c) => html`<li key=${c.key}><${TicketLink} k=${c.key} /> ${c.title || ''}
      ${c.status ? html` <${Chip} title="Status">${c.status}</${Chip}>` : null}${c.line ? html`<div class="d">${c.line}</div>` : null}</li>`)}</ul>`
      : html`<p class="bempty">No child tickets recorded yet</p>`}</section>`;
}
function AllDetails({ t, ctx }) {
  const n = (t.findings || []).length;
  const links = [['overview', 'Findings (' + n + ')', 'all-findings'], ['dbflow', 'DB flow'], ['requests', 'Requests'], ['queries', 'Queries'],
    ['artifacts', 'Artifacts'], ['branches', 'Branches & PRs'], ['timeline', 'Timeline']];
  return html`<nav class="alld" aria-label="All details"><span>All details:</span>
    ${links.map(([tab, label, id]) => html`<a key=${tab} href=${ctx.href(tab)} onClick=${(e) => { e.preventDefault(); ctx.jump(tab, id || null); }}>${label}</a>`)}</nav>`;
}

// The brief itself: `jump(tab, elementId)` switches tab and shows the element; `tabHref(tab)` is that tab's URL.
export function Brief({ t, jump, tabHref }) {
  const b = t.brief || {};
  const items = {};
  (t.findings || []).forEach((i) => { items[i.id] = i; });
  const ctx = { items, jump, href: tabHref };
  const secs = sectionsOf(b);
  const by = {};
  secs.forEach((s) => { by[s.id] = s; });
  let body;
  if (b.epic) body = html`<${Children} b=${b} />`;
  else if (!b.has_brief) body = html`<${Fallback} t=${t} b=${b} ctx=${ctx} />`;
  else {
    body = secs.map((s) => {
      if (s.id === 'after' && by.before) return null;
      if (s.id === 'before' || s.id === 'after') return html`<${BeforeAfter} key="ba" before=${by.before} after=${by.after} ctx=${ctx} />`;
      return html`<${Section} key=${s.id} s=${s} ctx=${ctx} />`;
    });
  }
  return html`<article class="brief" aria-label=${'Brief for ' + t.key}>
    ${b.stale && b.has_brief ? html`<div class="alert stale" role="status"><${Icon} name="info" size=${18} />
      <p class="grow">New findings since ${fmtTs(b.stale.since)}${b.stale.new ? ' (' + plural(b.stale.new, 'item') + ')' : ''}.</p>
      <${RefreshButton} k=${t.key} /></div>` : null}
    ${body}
    <${AllDetails} t=${t} ctx=${ctx} /></article>`;
}
