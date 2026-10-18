// Team notes: every bundle in your share targets, yours and your teammates', newest first; one opens in the pane.
// Read-only: a git target is shown as its clone is now (the next share brings in the team's new commits).
import { useEffect, useState } from '/vendor/hooks.module.js';
import { html } from '../lib/html.js';
import { useApi, setParams, fmtTs, plural } from '../lib/core.js';
import { Empty, Load, PageHead, TicketLink } from '../lib/ui.js';
import { Desk, pageKeys } from '../lib/shell.js';
import { TeamNote } from '../lib/share.js';

export function TeamPage({ route }) {
  const res = useApi('/api/team-notes');
  const p = route.params;
  const [q, setQ] = useState('');
  useEffect(() => { document.title = 'Team notes · Chitragupta'; pageKeys.set({}); }, []);
  const d = res.data;
  const notes = d ? d.notes.filter((n) => !q || (n.key + ' ' + n.title + ' ' + n.by).toLowerCase().indexOf(q.toLowerCase()) >= 0) : [];
  const cur = d && d.notes.find((n) => n.target === p.target && n.dir === p.dir);
  const rail = d && d.notes.length ? html`<div class="rail-tools"><label class="sr" for="tn-q">Find a note</label>
      <input id="tn-q" type="search" placeholder="Find a note" value=${q} onInput=${(e) => setQ(e.currentTarget.value)} autocomplete="off" /></div>
    <ul class="rlist">${notes.map((n) => html`<li key=${n.target + n.dir}><a href=${'#/team?target=' + encodeURIComponent(n.target) + '&dir=' + encodeURIComponent(n.dir)}
        aria-current=${cur === n ? 'page' : null} onClick=${(e) => { e.preventDefault(); setParams({ target: n.target, dir: n.dir }); }}>
      <span class="sw" style=${{ background: n.mine ? 'var(--accent)' : 'var(--rule)' }}></span>
      <span><span class="k">${n.key}</span><span class="t">${n.title || '—'}</span></span>
      <span class="m small">${n.by || ''}</span></a></li>`)}</ul>` : null;
  return html`<${Desk} rail=${rail} railLabel="Notes">
    <${PageHead} title="Team notes" sub=${d ? plural(d.notes.length, 'shared note') + (d.targets.length ? ' · from ' + d.targets.join(', ') : '') : ''} />
    <${Load} res=${res} lines=${6}>${(d) => !d.targets.length
      ? html`<${Empty} title="No share target configured" icon="info">Add one in Settings → Team sharing; then /cg:share or a ticket's Share… button writes bundles there.</${Empty}>`
      : !d.notes.length ? html`<${Empty} title="Nothing shared yet" icon="info">Use Share… on a ticket, or /cg:share, to share one.</${Empty}>`
        : !cur ? html`<p class="lede">Pick a note in the side list. Yours are marked in the accent colour.</p>`
          : html`<div class="sec-head"><h2><${TicketLink} k=${cur.key} /> ${cur.title}</h2></div>
            <p class="muted small">Shared ${fmtTs(cur.at)}${cur.by ? ' by ' + cur.by : ''} · level ${cur.level} · ${cur.target}</p>
            <${TeamNote} target=${cur.target} dir=${cur.dir} />`}</${Load}>
  </${Desk}>`;
}
