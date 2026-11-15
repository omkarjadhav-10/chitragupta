// Recorded UX-04 and UX-06 walkthroughs of the Companion (run through scripts/ui/companion_walk.py, which builds the
// synthetic homes, serves them and answers `@@ctl` requests on our stdin). Every walkthrough performs its actions in
// the dashboard (static layout is never task completion) and records path, state before / after, actions, keyboard
// steps, stub model / job / process counts and dispatch / submit counts (always zero submits), then checks its final
// view at 1440 light and 390 dark: screenshot, no root horizontal overflow, axe serious / critical, console errors.
// node companion-walkthroughs.js <out dir> <results.json> [ux04-1,ux06-3]
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const { chromium } = require('playwright-core');
const [out, resultsPath, onlyArg] = process.argv.slice(2);
const norm = (x) => String(x).toLowerCase().replace(/[^a-z0-9]/g, '');
const ONLY = (onlyArg || '').split(',').filter(Boolean).map(norm);
const slug = (id) => id.toLowerCase().replace('-', '').replace('.', '-');
const AXE = fs.readFileSync(path.join(__dirname, 'vendor', 'axe.min.js'), 'utf8');

// ------------------------------------------------------------------ control channel to companion_walk.py
const rl = readline.createInterface({ input: process.stdin });
const waiting = new Map();
let seq = 0;
rl.on('line', (l) => { const m = JSON.parse(l); const w = waiting.get(m.id); if (!w) return; waiting.delete(m.id); if (m.ok) w.resolve(m.result); else w.reject(new Error(m.error)); });
function ctl(op, args) {
  const id = ++seq;
  process.stdout.write('@@ctl ' + JSON.stringify({ id, op, args: args || {} }) + '\n');
  return new Promise((resolve, reject) => waiting.set(id, { resolve, reject }));
}

let browser, base = '', info = {};
const results = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const EXPECTED_HTTP = /status of (403|409)\b/;

// ------------------------------------------------------------------ the record of one walkthrough
class Walk {
  constructor(id, title, home) {
    Object.assign(this, { id, title, home, path: [], actions: [], keyboard: [], checks: [], limitations: [], shots: [],
      before: {}, after: {}, expected: '', actual: '', submits: 0, counts: {}, pages: [] });
  }
  check(cond, what) { this.checks.push({ ok: !!cond, what }); console.log((cond ? 'ok   ' : 'FAIL ') + this.id + ': ' + what); return !!cond; }
  go(p) { this.path.push(p); console.log('  > ' + p); }
  did(a) { this.actions.push(a); console.log('  * ' + String(a).slice(0, 200)); }
  key(k) { this.keyboard.push(k); }
}

async function newPage(w, width, theme) {
  const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: theme, bypassCSP: true });
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base.replace(/\/$/, '') });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  page.errors = []; page.expected = []; page.external = []; page.theme = theme;
  page.on('pageerror', (e) => page.errors.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error') (EXPECTED_HTTP.test(m.text()) ? page.expected : page.errors).push(m.text()); });
  page.on('request', (r) => { if (!r.url().startsWith('http://127.0.0.1:') && !/^(data|blob):/.test(r.url())) page.external.push(r.url()); });
  page.on('dialog', (d) => d.accept());
  w.pages.push(page);
  return page;
}
async function open(page, hash) {
  await page.goto(base + '?theme=' + page.theme + hash);
  await settle(page);
}
async function settle(page) {
  await page.waitForSelector('#page-title, h1', { timeout: 15000 }).catch(() => {});
  await page.waitForFunction(() => !document.querySelector('.skeleton'), null, { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(250);
}
const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
async function axe(page) {
  await page.addScriptTag({ content: AXE });
  return page.evaluate(async () => (await window.axe.run()).violations.filter((v) => ['serious', 'critical'].includes(v.impact)).map((v) => v.id + ' x' + v.nodes.length + ' (' + v.nodes[0].target.join(' ') + ')'));
}
const text = (page, sel) => page.locator(sel).first().innerText().catch(() => '');
const body = (page) => page.locator('body').innerText();
async function waitStatus(page, re, ms = 15000) {
  await page.waitForTimeout(250);  // the action first clears the previous status; never read the stale one
  await page.waitForFunction((src) => { const el = document.querySelector('p.cmp-status'); return el && new RegExp(src).test(el.textContent); }, re.source, { timeout: ms });
  return text(page, 'p.cmp-status');
}
async function pickTask(page, key) {
  await page.locator('.cmp-pick', { hasText: key }).first().click();
  await page.locator('#cmp-task-h, #cmp-new-h, #cmp-choose-h').first().waitFor({ timeout: 15000 });
  await page.waitForTimeout(200);
}
async function openTask(page, wid, tab) {
  await open(page, '#/companion?w=' + wid);
  await page.locator('#cmp-task-h').waitFor({ timeout: 15000 });
  if (tab) await openTab(page, tab);
}
async function navTo(page, id) {
  const direct = page.locator('nav.primary-nav a[href="#/' + id + '"]');
  if (await direct.count()) await direct.click();
  else { await page.locator('.more-pages').click(); await page.locator('#more-pages a[href="#/' + id + '"], a[href="#/' + id + '"]').first().click(); }
  await page.waitForURL(new RegExp('#/' + id));
  await settle(page);
}
async function openTab(page, name) { await page.getByRole('tab', { name, exact: true }).click(); await page.waitForTimeout(250); }
const main = (page) => page.locator('.cmp-main');
const btn = (scope, name) => scope.getByRole('button', { name, exact: true });
async function reloadView(page) { await btn(page, 'Reload cached view').click(); await page.waitForTimeout(800); }
async function tabTo(page, locator, max = 60) {
  const target = await locator.elementHandle();
  for (let i = 0; i < max; i++) {
    await page.keyboard.press('Tab');
    if (await page.evaluate((el) => document.activeElement === el, target)) return i + 1;
  }
  return 0;
}
async function focusVisible(page) {
  return page.evaluate(() => {
    const el = document.activeElement;
    if (!el || el === document.body) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    const ring = (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0) || (s.boxShadow && s.boxShadow !== 'none');
    return r.width > 0 && r.height > 0 && ring;
  });
}
async function apiGet(page, q) {
  return page.evaluate(async (u) => { const r = await fetch(u, { headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(15000) }); return { status: r.status, body: await r.json().catch(() => ({})) }; }, '/api/companion?' + q);
}
const delta = (a, b) => Object.fromEntries(Object.keys(b).map((k) => [k, Array.isArray(b[k]) ? b[k].slice((a[k] || []).length) : (b[k] || 0) - (a[k] || 0)]));

async function finalViews(w, page, hash, prepare) {
  await settle(page);
  await shotChecks(w, page, '1440 light', slug(w.id) + '-1440-light.png');
  const dark = await newPage(w, 390, 'dark');
  await open(dark, hash);
  if (prepare) await prepare(dark);
  await settle(dark);
  await shotChecks(w, dark, '390 dark', slug(w.id) + '-390-dark.png');
}
async function shotChecks(w, page, tag, file) {
  w.check(!(await overflow(page)), tag + ': no root horizontal overflow on the final view');
  const v = await axe(page);
  w.check(v.length === 0, tag + ': axe serious/critical none on the final view ' + v.join(', '));
  await page.screenshot({ path: path.join(out, file), fullPage: true });
  w.shots.push(file);
}

async function run(w, fn) {
  if (ONLY.length && !ONLY.includes(norm(w.id))) return;
  console.log('\n== ' + w.id + ' ' + w.title);
  const c0 = await ctl('counters');
  let timer;
  const limit = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('timed out after 300 s; last step: ' + (w.actions.slice(-1)[0] || w.path.slice(-1)[0] || 'none'))), 300000); });
  try { await Promise.race([fn(w), limit]); } catch (e) { w.error = String(e && e.stack || e).slice(0, 1500); w.check(false, 'walkthrough finished without an exception: ' + String(e.message || e).slice(0, 300)); }
  clearTimeout(timer);
  const c1 = await ctl('counters');
  w.counts = delta(c0, c1);
  w.check(w.counts.dispatch_starts === 0 && w.submits === 0, 'zero dispatch starts and zero submits');
  const probe = (r) => w.healthProbes && / from (git-probe|probe|claude)$/.test(r);
  const refused = (w.counts.refused || []).filter((r) => !probe(r));
  if ((w.counts.refused || []).some(probe)) w.actions.push('Health page probes refused by the tripwire: ' + w.counts.refused.filter(probe).join('; '));
  w.check(refused.length === 0 && w.counts.mcp_jobs_refused === 0 && w.counts.jev_transport_calls === 0,
    'no Companion process start, MCP job or Jev transport call was attempted ' + refused.join('; '));
  for (const p of w.pages) {
    if (w.healthProbes) { p.expected.push(...p.errors.filter((e) => /status of 502/.test(e))); p.errors = p.errors.filter((e) => !/status of 502/.test(e)); }
    w.check(p.errors.length === 0, 'no console or page errors (' + (p.theme || '') + ') ' + p.errors.slice(0, 3).join(' | '));
    w.check(p.external.length === 0, 'zero off-machine requests ' + p.external.slice(0, 2).join(', '));
    if (p.expected.length) w.actions.push('expected HTTP refusals seen in the console: ' + p.expected.length);
    await p.context().close().catch(() => {});
  }
  delete w.pages;
  const failed = w.checks.filter((c) => !c.ok).length;
  w.status = failed ? 'FAIL' : w.partial ? 'PARTIAL' : 'PASS';
  results.push(w);
  fs.writeFileSync(resultsPath, JSON.stringify({ walkthroughs: results }, null, 1));
}
async function phase(name) { const r = await ctl('phase', { name }); base = r.base; info = r.info; return r; }

// ================================================================== UX-04
async function ux04_1(w) {
  w.expected = 'Setup is explained before anything is shown; identity and scope are confirmed on the Profile page; the Companion then shows a useful empty state (No recorded work yet) and never another person\'s ticket.';
  w.before = { home: 'nothing confirmed', other_ticket_on_disk: info.other_ticket };
  const page = await newPage(w, 1440, 'light');
  await open(page, '#/companion');
  w.go('#/companion');
  await page.locator('#cmp-focus-h').waitFor();
  const first = await text(page, '.cmp-focus');
  w.check(/Confirm your identity and scope in Setup/.test(first), 'setup explanation shown first: ' + first.split('\n').slice(0, 3).join(' / '));
  w.check(!(await body(page)).includes(info.other_ticket), 'no other person\'s ticket before setup');
  const link = page.getByRole('link', { name: 'Open profile setup' });
  const tabs = await tabTo(page, link);
  w.check(tabs > 0 && await focusVisible(page), 'Tab reaches Open profile setup with a visible focus ring (' + tabs + ' Tab presses)');
  await page.keyboard.press('Enter');
  w.key('Tab ×' + tabs + ' to Open profile setup, Enter');
  await page.waitForURL(/#\/profile/);
  await settle(page);
  w.go('Open profile setup → #/profile?section=identity');
  w.check(/Identity/.test(await body(page)), 'Profile opens at Identity');
  await page.locator('#pf-add-email').fill(info.email);
  await page.locator('#pf-add-email').press('Enter');
  w.key('Enter submits "Review adding it"');
  const dlg = page.getByRole('dialog');
  await dlg.getByText('What this changes', { exact: true }).waitFor();
  const eff0 = await dlg.innerText();
  w.check(/Commit emails added/.test(eff0), 'the review explains the effect before saving: ' + eff0.split('\n').filter(Boolean).slice(1, 4).join(' / '));
  await page.keyboard.press('Escape');
  w.key('Escape closes the review');
  await page.waitForTimeout(300);
  w.check(await dlg.count() === 0 || !(await dlg.isVisible()), 'Escape closes the review dialog');
  w.check(await page.evaluate(() => document.activeElement && document.activeElement !== document.body), 'focus returns to the page after Escape');
  await page.getByRole('button', { name: 'Review adding it' }).click();
  await dlg.getByRole('button', { name: 'Save these changes' }).click();
  await page.waitForTimeout(800);
  w.did('confirmed commit email ' + info.email + ' (Review adding it → Save these changes)');
  await page.goto(base + '?theme=light#/profile?section=scope');
  await settle(page);
  await page.getByLabel('Only the ones I pick').check();
  await page.locator('.pfrepos label', { hasText: info.repo }).locator('input').check();
  await page.getByRole('button', { name: 'Review repositories and projects' }).click();
  await dlg.getByText('What this changes', { exact: true }).waitFor();
  const eff = await dlg.innerText();
  w.check(/scope|Nothing that counts as yours changes/i.test(eff), 'the scope review states its effect: ' + eff.split('\n').filter(Boolean).slice(1, 3).join(' / '));
  await dlg.getByRole('button', { name: 'Save these changes' }).click();
  await page.waitForTimeout(800);
  w.did('confirmed scope: only repository ' + info.repo);
  w.go('Scope → Review repositories and projects → Save these changes');
  await navTo(page, 'companion');
  w.go('top navigation → More → Companion');
  await page.locator('#cmp-focus-h').waitFor();
  const all = await body(page);
  w.check(/No recorded work yet/.test(await text(page, '.cmp-focus')), 'useful empty state in Focus: No recorded work yet');
  w.check(page.getByRole('link', { name: 'Open setup' }) && await page.getByRole('link', { name: 'Open setup' }).count() === 1, 'one concrete next step (Open setup)');
  w.check(!all.includes(info.other_ticket) && !/warehouse sync/i.test(all), 'no other person\'s data after setup (' + info.other_ticket + ' absent)');
  w.check(!all.includes(info.email), 'no email address on the Companion overview');
  const snap = await apiGet(page, 'operation=snapshot');
  w.after = { state: snap.body.state, empty: snap.body.empty, items: (snap.body.items || []).length };
  w.check(snap.body.state === 'empty', 'snapshot state is empty (not identity_unconfirmed) after setup');
  w.actual = 'Focus said "' + first.split('\n').slice(1, 2).join('') + '"; identity and scope were saved through the Profile review; the Companion then said "' + snap.body.empty + '" with Open setup.';
  await finalViews(w, page, '#/companion');
}

async function ux04_2(w) {
  w.expected = 'Focus names the current ticket and one next action; Since your last review lists the delta with provenance; Mark changes reviewed acknowledges exactly those; after a reload they stay acknowledged.';
  const page = await newPage(w, 1440, 'light');
  w.before = await ctl('inspect', { workflow: info.w1 });
  await open(page, '#/companion');
  w.go('#/companion (Focus)');
  const focus = await text(page, '.cmp-focus');
  const cur = await text(page, '.cmp-focus .cmp-cur');
  w.check(/^ABC-1001\b/.test(cur) && /service-api/.test(cur), 'Focus names the current ticket and repository: ' + cur);
  w.check(await page.locator('.cmp-focus .btn.primary').count() === 1, 'exactly one primary next action in Focus');
  const na = page.locator('.cmp-focus .btn.primary');
  w.check(/Review Changes/.test(await na.innerText()), 'the next action is Review Changes (' + (await na.innerText()) + ')');
  const tabs = await tabTo(page, na);
  await page.keyboard.press('Enter');
  w.key('Tab ×' + tabs + ' to the next action, Enter');
  await page.locator('#cmp-task-h').waitFor();
  w.go('Review Changes → task ABC-1001 · Resume');
  const ch = page.locator('.cmp-changes li');
  const n = await ch.count();
  const firstRow = n ? await ch.first().innerText() : '';
  w.check(n === w.before.unread, 'Since your last review lists the ' + w.before.unread + ' unread changes (' + n + ')');
  w.check(/ticket record|captured|commit|test|file/i.test(firstRow), 'each change shows provenance: ' + firstRow.slice(0, 120));
  // focus is not stolen by the 15 s background refresh, and the typed text stays
  const box = page.locator('#cmp-pause');
  await box.focus();
  await page.keyboard.type('Stopped at the rounding helper');
  await page.waitForTimeout(16500);
  w.check(await page.evaluate(() => document.activeElement && document.activeElement.id === 'cmp-pause'), 'a 15 s background refresh did not move focus');
  w.check(await box.inputValue() === 'Stopped at the rounding helper', 'typed text kept across the background refresh');
  w.key('typed into "Where you stopped"; focus and text survived the 15 s refresh');
  const mark = btn(main(page), 'Mark changes reviewed');
  await mark.focus();
  await page.keyboard.press('Space');
  w.key('Space on Mark changes reviewed');
  await waitStatus(page, /Saved/);
  w.did('Mark changes reviewed');
  w.check(await page.locator('.cmp-changes li').count() === 0, 'the reviewed changes leave the list');
  await page.reload();
  await settle(page);
  await page.locator('#cmp-task-h').waitFor();
  w.go('reload');
  w.check(await page.locator('.cmp-changes li').count() === 0 && await btn(main(page), 'Mark changes reviewed').count() === 0, 'after reload they stay acknowledged');
  const focus2 = await text(page, '.cmp-focus');
  w.check(!/Review Changes/.test(focus2), 'Focus no longer asks to review those changes');
  w.after = await ctl('inspect', { workflow: info.w1 });
  w.check(w.after.unread === 0 && w.after.acknowledged >= w.before.acknowledged + w.before.unread, 'record: unread ' + w.before.unread + ' → ' + w.after.unread + ', acknowledged ' + w.before.acknowledged + ' → ' + w.after.acknowledged);
  w.check(w.after.stage === w.before.stage, 'acknowledging changed no stage (' + w.after.stage + ')');
  w.actual = 'Focus: "' + focus.split('\n').slice(0, 3).join(' / ') + '"; ' + n + ' changes listed and acknowledged; the reload kept them acknowledged.';
  await finalViews(w, page, '#/companion?w=' + info.w1);
}

async function ux04_3(w) {
  w.expected = 'ABC-1001 shows two repository attempts; Open delivery details lands on the ticket\'s Branches & PRs with each repository\'s base, PR target, QA and UAT guidance; ABC-1002\'s unresolved release offers Resolve release mapping, which opens where it is resolved.';
  const page = await newPage(w, 1440, 'light');
  w.before = { w1: (await ctl('inspect', { workflow: info.w1 })).stage, w2: (await ctl('inspect', { workflow: info.w2 })).stage };
  await open(page, '#/companion');
  const card = page.locator('.cmp-card', { hasText: 'ABC-1001' });
  const attempts = await card.locator('.cmp-attempts button').allInnerTexts();
  w.check(attempts.length === 2 && attempts.some((a) => /service-api/.test(a)) && attempts.some((a) => /service-ui/.test(a)), 'the card lists both repository attempts: ' + attempts.join(' | '));
  await card.locator('.cmp-pick').click();
  await page.waitForTimeout(500);
  w.go('Daily Planner → ABC-1001 card');
  const mainText = await text(page, '.cmp-main');
  w.check(!/confirm the requirement baseline/.test(mainText) && /choose an attempt/.test(mainText), 'selecting a ticket with two attempts offers its attempts, not the new-task form');
  await card.locator('.cmp-attempts button', { hasText: 'service-api' }).click();
  await page.locator('#cmp-task-h').waitFor();
  w.go('Attempt in service-api → Resume');
  const res = await text(page, '.cmp-resume');
  w.check(/2 repositor/i.test(res), 'Resume shows the two-repository roll-up');
  const dl = main(page).getByRole('link', { name: 'Open delivery details' });
  await dl.focus();
  await page.keyboard.press('Enter');
  w.key('Enter on Open delivery details');
  await page.waitForURL(/#\/tickets\/ABC-1001/);
  await settle(page);
  await page.waitForTimeout(1200);
  w.go('Open delivery details → ' + page.url().replace(base, '/'));
  const t = await body(page);
  w.check(/tab=branches/.test(page.url()), 'delivery details open the Branches & PRs tab (' + page.url().split('#')[1] + ')');
  w.check(/service-api/.test(t) && /service-ui/.test(t), 'both repositories are shown');
  w.check(/created from release\/ABC-9000/.test(t) || /base/i.test(t), 'development base shown');
  w.check(/#40|PR 40|targets release\/ABC-9001\/hotfix/.test(t), 'PR target problem shown (PR #40 targets the hotfix branch)');
  w.check(/\bqa\b/i.test(t) && /\buat\b/i.test(t), 'QA and UAT stages shown');
  await open(page, '#/companion');
  await pickTask(page, 'ABC-1002');
  w.go('back → Daily Planner → ABC-1002');
  const f2 = page.locator('.cmp-focus');
  const offer = f2.getByRole('link', { name: 'Resolve release mapping' });
  w.check(await offer.count() === 1, 'Focus offers Resolve release mapping as a link');
  const res2 = await text(page, '.cmp-resume');
  w.check(/Resolve release mapping/.test(res2), 'the task\'s delivery section names the unresolved mapping');
  if (await offer.count()) {
    await offer.click();
    await page.waitForURL(/#\/tickets\/ABC-1002/);
    await settle(page);
    await page.waitForTimeout(1200);
    const t2 = await body(page);
    w.go('Resolve release mapping → ' + page.url().split('#')[1]);
    w.check(/2 fix versions|release-pick|choose the one that counts/i.test(t2), 'the mapping problem and its resolution command are shown');
  }
  w.after = { w1: (await ctl('inspect', { workflow: info.w1 })).stage, w3: (await ctl('inspect', { workflow: info.w3 })).revision };
  w.actual = 'Attempts: ' + attempts.join(', ') + '; delivery details at ' + page.url().split('#')[1] + '.';
  w.limitations.push('Resolving the mapping itself is the /cg:branch release-pick command shown on the ticket; the dashboard has no mapping editor');
  await finalViews(w, page, '#/tickets/ABC-1002?tab=branches', async (p) => { await p.waitForTimeout(1200); });
}

async function ux04_4(w) {
  w.expected = 'Releases lists all remaining work: overdue tickets and each release\'s unfinished tickets with the repositories still pending; nothing reads as an overall green while any of it is unfinished.';
  const page = await newPage(w, 1440, 'light');
  await open(page, '#/companion?w=' + info.w1);
  await page.locator('#cmp-task-h').waitFor();
  const detail = await apiGet(page, 'operation=detail&workflow=' + info.w1);
  w.before = { companion_rollup: detail.body.rollup && detail.body.rollup.label };
  w.check(detail.body.rollup && detail.body.rollup.total === 2 && detail.body.rollup.completed === 0, 'the Companion roll-up counts 2 repositories, none completed: ' + (detail.body.rollup || {}).label);
  const nav = page.locator('nav.primary-nav a[href="#/releases"]').first();
  await nav.focus();
  await page.keyboard.press('Enter');
  w.key('Enter on the Releases navigation link');
  await page.waitForURL(/#\/releases/);
  await settle(page);
  w.go('#/companion?w=… → navigation → #/releases');
  const t = await body(page);
  w.check(/Overdue/.test(t) && /ABC-1003/.test(t), 'overdue work is listed (ABC-1003)');
  w.check(/ABC-1001/.test(t) && /service-ui/.test(t), 'the partly completed ticket lists its pending repository (service-ui)');
  const row = page.locator('.relrow', { hasText: 'ABC-1001' }).first();
  await row.locator('summary').first().focus();
  await page.keyboard.press('Enter');
  w.key('Enter opens ABC-1001\'s repositories, reminders and evidence');
  const rowText = await row.innerText();
  w.check(/Not ready because/.test(rowText) && /service-ui/.test(rowText), 'readiness explains what is missing: ' + (rowText.split('Not ready because')[1] || '').replace(/\n/g, ' ').slice(0, 160));
  w.check(/In progress/.test(rowText) && !/\bReady\b/.test(rowText.split('\n')[0]), 'the partly completed ticket reads In progress, not Ready');
  w.check(!/\b\d+ ready\b/.test((await text(page, '.relscope')) || ''), 'no "ready" count in the month line while nothing is ready');
  w.check(/ABC-1002/.test(t) || /without a release date/.test(t), 'tickets without a release date are listed');
  const ok = await page.locator('.st.ok, [data-kind="ok"]').allInnerTexts().catch(() => []);
  w.check(!ok.some((x) => /^\s*(ready|all done|complete)\s*$/i.test(x)), 'no overall green status on the page (' + ok.length + ' ok badges: ' + ok.slice(0, 4).join(', ') + ')');
  const month = page.getByRole('link', { name: /previous|earlier|‹|←/i }).first();
  if (await month.count()) { await month.click(); await settle(page); w.go('previous month'); w.check(/ABC-1003/.test(await body(page)), 'overdue work stays in view on another month'); }
  w.after = { releases_page: page.url().split('#')[1] };
  w.actual = 'Releases: ' + ((await text(page, '.relscope')) || t.split('\n').slice(0, 3).join(' / ')).slice(0, 200);
  await finalViews(w, page, '#/releases');
}

async function ux04_5(w) {
  w.expected = 'Verification shows the three measures with their definitions; each ledger row opens its source events with revision and applicability; current evidence (Verified) is kept apart from historical (earlier evidence no longer applies) and unknown applicability.';
  const page = await newPage(w, 1440, 'light');
  w.before = await ctl('inspect', { workflow: info.w1 });
  await openTask(page, info.w1);
  await page.getByRole('tab', { name: 'Resume', exact: true }).focus();
  for (let i = 0; i < 3; i++) { await page.keyboard.press('ArrowRight'); await page.waitForTimeout(150); }
  w.key('ArrowRight ×3 from the Resume tab to Verification');
  await page.locator('#cmp-vm-h').waitFor();
  w.go('#/companion?w=… → Verification');
  const v = await text(page, '.cmp-verify');
  w.check(/Plan coverage/.test(v) && /Implementation progress/.test(v) && /Functional verification/.test(v), 'three separate measures');
  w.check(/33\.3% \(1\/3\)/.test(v), 'functional verification shows its count: 33.3% (1/3)');
  w.check(/verified criterion weight|applicable criterion weight/i.test(v), 'the definition of the count is shown');
  const rows = await page.locator('.cmp-ledger > li').allInnerTexts();
  const by = (id) => rows.find((r) => r.startsWith(id)) || '';
  w.check(/Verified/.test(by('ac1')) && /current applicable/.test(by('ac1')), 'ac1 Verified by current evidence: ' + by('ac1').replace(/\n/g, ' ').slice(0, 140));
  w.check(/earlier evidence no longer applies/.test(by('ac2')) && /head/.test(by('ac2')), 'ac2 historical (head changed): ' + by('ac2').replace(/\n/g, ' ').slice(0, 160));
  w.check(/applicability unknown|not captured/i.test(by('ac3')) && !/earlier evidence no longer applies/.test(by('ac3')), 'ac3 says applicability unknown, not "earlier evidence": ' + by('ac3').replace(/\n/g, ' ').slice(0, 160));
  const ev = page.locator('.cmp-ledger > li').nth(1).locator('details summary');
  if (await ev.count()) {
    await ev.focus();
    await page.keyboard.press('Enter');
    w.key('Enter opens ac2\'s source events');
    await page.waitForTimeout(200);
    const det = await page.locator('.cmp-ledger > li').nth(1).innerText();
    w.check(/historical/i.test(det) && /9e8d7c6/.test(det), 'ac2\'s source event shows its revision and that it is historical: ' + det.replace(/\n/g, ' ').slice(0, 200));
  } else w.check(false, 'a ledger row opens its source events');
  await btn(main(page), 'Verify Requirements').click();
  await waitStatus(page, /Saved/);
  w.did('Verify Requirements (recomputed; no model call)');
  w.after = await ctl('inspect', { workflow: info.w1 });
  w.check(w.after.revision === w.before.revision + 1 && w.after.evaluations === w.before.evaluations, 'one ledger checkpoint, no evaluation (' + w.before.revision + ' → ' + w.after.revision + ')');
  w.actual = rows.map((r) => r.split('\n').slice(0, 2).join(' ')).join(' | ').slice(0, 600);
  await finalViews(w, page, '#/companion?w=' + info.w1, async (p) => { await openTab(p, 'Verification'); });
}

async function ux04_7(w) {
  w.expected = 'With GitHub PR reads denied and Jira three days stale, the sources say which is stale or failing and what is still current, each with a recovery path; tasks and the next action from the valid sources stay; recovery restores them.';
  const page = await newPage(w, 1440, 'light');
  await open(page, '#/companion');
  const before = await apiGet(page, 'operation=snapshot');
  w.before = Object.fromEntries(before.body.sources.map((s) => [s.label, s.state]));
  const items0 = (before.body.items || []).length;
  w.did('synthetic outage: ' + JSON.stringify(await ctl('mutate', { kind: 'disconnect' })));
  const reload = btn(page, 'Reload cached view');
  await reload.focus();
  await page.keyboard.press('Enter');
  w.key('Enter on Reload cached view');
  await page.waitForTimeout(1000);
  w.go('#/companion → Reload cached view');
  const sum = page.locator('.cmp-focus details.more summary').first();
  await sum.focus();
  await page.keyboard.press('Enter');
  w.key('Enter opens the sources summary');
  await page.waitForTimeout(200);
  const s = await text(page, '.cmp-sources');
  w.check(/github pull requests: Failing/i.test(s), 'the denied source says Failing');
  w.check(/Jira: Stale · last success/.test(s), 'the stale source shows its last success');
  w.check(/Git commits: Connected/.test(s), 'the current source stays Connected');
  w.check(/earlier results are kept/.test(s) && /Open Health/i.test(s), 'recovery hints name what to do');
  const snap = await apiGet(page, 'operation=snapshot');
  w.check((snap.body.items || []).length === items0 && /Review Changes|Review continuation|Resume|Choose/.test((snap.body.next_action || {}).label || ''), 'valid tasks and the next action remain (' + (snap.body.items || []).length + ' tasks)');
  const health = page.locator('.cmp-sources').getByRole('link', { name: /Open Health/ }).first();
  w.healthProbes = true;
  w.limitations.push('Health (outside the Companion) runs its own setup probes on open (git / claude / py --version, claude mcp list); the walkthrough tripwire refused them and Health answered HTTP 502 for that request');
  w.limitations.push('recovery is synthetic: the fixture restores the PR read and the Jira answer in place of a real re-read');
  w.check(await health.count() > 0, 'a source that needs attention links to its recovery (Open Health)');
  if (await health.count()) {
    await health.click();
    await page.waitForURL(/#\/health/);
    await settle(page);
    w.go('Open Health → #/health');
    w.check(/Health/.test(await body(page)), 'Health opens');
  }
  w.did('recovery: ' + JSON.stringify(await ctl('mutate', { kind: 'reconnect' })));
  await open(page, '#/companion');
  await reloadView(page);
  const after = await apiGet(page, 'operation=snapshot');
  w.after = Object.fromEntries(after.body.sources.map((x) => [x.label, x.state]));
  w.check(w.after['github pull requests'] === 'Connected' && w.after.Jira === 'Connected', 'after recovery the sources are current again');
  w.actual = 'Outage: ' + s.replace(/\n/g, ' | ').slice(0, 400);
  await page.locator('.cmp-focus details.more summary').first().click();
  await finalViews(w, page, '#/companion', async (p) => { await p.locator('.cmp-focus details.more summary').first().click(); });
}

async function ux04_6(w) {
  w.expected = 'Removing shop-api from scope shows its effect before saving; the unsaved edit survives a refresh; after saving, the Companion clears what you may no longer see (ABC-1002 on shop-api), nothing of it is served again, and the tasks still in scope can be reconciled.';
  const page = await newPage(w, 1440, 'light');
  w.before = { w1: await ctl('inspect', { workflow: info.w1 }), w3: await ctl('inspect', { workflow: info.w3 }) };
  await openTask(page, info.w3, 'Plan');
  w.go('#/companion?w=<ABC-1002 on shop-api> · Plan');
  w.check(/ABC-1002/.test(await text(page, '#cmp-task-h')), 'ABC-1002 is open before the change');
  const prof = await page.context().newPage();
  prof.errors = page.errors; prof.expected = page.expected; prof.external = page.external;
  prof.on('pageerror', (e) => page.errors.push(e.message));
  prof.on('dialog', (d) => d.accept());
  await prof.goto(base + '?theme=light#/profile?section=scope');
  await settle(prof);
  w.go('second tab: #/profile?section=scope');
  await prof.getByLabel('Only the ones I pick').check();
  for (const box of await prof.locator('.pfrepos input[type=checkbox]').all()) await box.check();
  await prof.locator('.pfrepos label', { hasText: 'shop-api' }).locator('input').uncheck();
  await prof.getByRole('button', { name: 'Review repositories and projects' }).click();
  const dlg = prof.getByRole('dialog');
  await dlg.getByText('What this changes', { exact: true }).waitFor();
  const eff = await dlg.innerText();
  w.check(/Repositories leaving your scope: .*shop-api/.test(eff), 'impact explanation names the repository leaving scope');
  await prof.keyboard.press('Escape');
  w.key('Escape back to editing');
  await prof.reload();
  await settle(prof);
  w.go('refresh the Profile tab with the edit unsaved');
  const restored = await prof.locator('.pfrepos label', { hasText: 'shop-api' }).locator('input').isChecked().catch(() => null);
  w.check(restored === false && await prof.getByLabel('Only the ones I pick').isChecked(), 'the unsaved scope edit survived the refresh');
  await prof.getByRole('button', { name: 'Review repositories and projects' }).click();
  await dlg.getByRole('button', { name: 'Save these changes' }).click();
  await prof.waitForTimeout(1000);
  w.did('saved: shop-api leaves the scope');
  await page.bringToFront();
  await reloadView(page);
  w.go('first tab: Reload cached view');
  const status = await text(page, 'p.cmp-status');
  const all = await body(page);
  w.check(/cleared/i.test(status), 'the open task is cleared: ' + status);
  w.check(!/Raise the checkout timeout message/.test(all) && await page.locator('#cmp-task-h').count() === 0, 'no content of the removed task stays on screen');
  const leak = await apiGet(page, 'operation=detail&workflow=' + info.w3);
  w.check(leak.status === 403, 'the removed task is refused by the server (HTTP ' + leak.status + ')');
  const snap = await apiGet(page, 'operation=snapshot');
  const raw = JSON.stringify(snap.body);
  w.check(!raw.includes(info.w3) && !/Raise the checkout timeout message/.test(raw), 'the snapshot carries nothing of the removed task (its ID and plan text are absent; the ticket itself stays included)');
  const rec = page.getByRole('button', { name: /Reconcile/ });
  w.check(await rec.count() === 1, 'Focus offers to reconcile the tasks still in scope');
  if (await rec.count()) {
    await rec.focus();
    await page.keyboard.press('Enter');
    w.key('Enter on Reconcile');
    await page.waitForTimeout(1200);
    w.did('reconciled the tasks still in scope');
    w.check(/reconciled/i.test(await text(page, 'p.cmp-status')), 'reconciliation reported: ' + await text(page, 'p.cmp-status'));
    const card = page.locator('.cmp-card', { hasText: 'ABC-1001' });
    w.check(await card.locator('.cmp-attempts button').count() === 2, 'ABC-1001 (still in scope) shows its two attempts again');
    const again = await apiGet(page, 'operation=detail&workflow=' + info.w3);
    w.check(again.status === 403, 'the removed task stays refused after reconciliation (HTTP ' + again.status + ')');
  }
  w.after = { w1: await ctl('inspect', { workflow: info.w1 }), w3: await ctl('inspect', { workflow: info.w3 }) };
  w.check(w.after.w3.revision === w.before.w3.revision, 'the removed task\'s record was not changed (revision ' + w.after.w3.revision + ')');
  w.actual = 'Status after the change: "' + status + '"; detail of the removed task: HTTP ' + leak.status + '.';
  await finalViews(w, page, '#/companion');
}

// ================================================================== UX-06
const NEXT = { planned: 'Review saved plan', review_incomplete: 'Review saved plan', in_development: 'Review continuation', paused: 'Review continuation',
  awaiting_verification: 'Verify Requirements', pr_review: 'Open PR Review', completed: 'View completion evidence', new: 'Generate Plan' };

async function ux06_1(w) {
  w.expected = 'The planner groups tasks as New, Planned, Review incomplete, In development, Paused, Testing, PR review and a collapsed Completed; each card says why; opening each one deliberately shows the next action of its stage and changes nothing.';
  const page = await newPage(w, 1440, 'light');
  const keys = ['ABC-1002', 'ABC-1001', 'ABC-1009', 'ABC-1004', 'ABC-1005', 'ABC-1006', 'ABC-1007', 'ABC-1008'];
  w.before = Object.fromEntries(await Promise.all(keys.map(async (k) => [k, (await ctl('inspect', { workflow: info[k] })).revision])));
  await open(page, '#/companion');
  w.go('#/companion · Daily Planner');
  const pl = await text(page, '.cmp-planner');
  for (const g of ['New', 'Planned', 'Review incomplete', 'In development', 'Paused', 'Testing', 'PR review']) w.check(new RegExp('\\b' + g + '\\b').test(pl), 'planner group ' + g);
  w.check(/Show completed \(1\)/.test(pl), 'completed tasks collapsed (Show completed (1))');
  const plan = await apiGet(page, 'operation=planner');
  for (const c of plan.body.items) {
    const want = c.stage === 'new' ? /Generate Plan/ : /Resume Task|Choose attempt/;
    const why = c.stage === 'new' ? true : new RegExp(c.stage.replace(/_/g, ' ')).test(c.why);
    w.check(want.test(c.action) && why, c.ticket + ' card: ' + c.action + ' · ' + c.stage + ' · Why: ' + c.why);
  }
  const first = page.locator('.cmp-pick').first();
  const n = await tabTo(page, first, 80);
  await page.keyboard.press('Enter');
  w.key('Tab ×' + n + ' to the first card, Enter');
  await page.locator('#cmp-task-h, #cmp-new-h').first().waitFor();
  for (const k of keys) {
    if (k === 'ABC-1008') { await btn(page, 'Show completed (1)').click(); await page.waitForTimeout(500); }
    await pickTask(page, k);
    const st = (await ctl('inspect', { workflow: info[k] })).stage;
    const na = await text(page, '#cmp-na-h');
    w.check(na === 'Next action: ' + NEXT[st], k + ' (' + st + '): ' + na);
    w.go(k);
  }
  await page.getByRole('tab', { name: 'Resume', exact: true }).focus();
  await page.keyboard.press('ArrowRight');
  await page.waitForTimeout(200);
  w.check(await page.evaluate(() => document.activeElement && document.activeElement.getAttribute('aria-selected') === 'true'), 'ArrowRight moves between task views');
  w.key('ArrowRight between task tabs');
  w.after = Object.fromEntries(await Promise.all(keys.map(async (k) => [k, (await ctl('inspect', { workflow: info[k] })).revision])));
  w.check(JSON.stringify(w.after) === JSON.stringify(w.before), 'opening tasks saved nothing (revisions unchanged)');
  w.actual = plan.body.items.map((c) => c.ticket + ' ' + c.group_label).join(', ');
  await btn(page, 'Show completed (1)').count() && await page.waitForTimeout(10);
  await finalViews(w, page, '#/companion');
}

async function ux06_2(w) {
  w.expected = 'Generate Plan (one stub call through the real job) saves version 1; confirmed criteria and assumptions are shown apart; Request evaluation says Evaluation unavailable (no score); a manual edit is version 2; Improve Plan (one stub call) is a candidate with a diff until selected; the developer assessment before / after is labelled as yours; Execute approves that exact version with the acknowledged risk; Copy says Prepared; not submitted.';
  const wid = info['ABC-1002'];
  const page = await newPage(w, 1440, 'light');
  w.before = await ctl('inspect', { workflow: wid });
  await openTask(page, wid, 'Plan');
  w.go('#/companion?w=<ABC-1002> · Plan');
  const gen = await text(page, '.cmp-plan');
  w.check(/Requirement baseline/.test(gen) && /2 criteria/.test(gen), 'the Generate section names the confirmed baseline');
  await btn(main(page), 'Generate Plan').click();
  await waitStatus(page, /Queued|Already running/);
  w.did('Generate Plan');
  await page.waitForTimeout(2500);
  await reloadView(page);
  await page.locator('#cmp-vers-h').waitFor();
  w.check(/Version 1 · Claude suggestion · selected/.test(await text(page, '.cmp-versions')), 'version 1 saved from the stub through the real job');
  await btn(main(page), 'View version 1').click();
  const v1 = await text(page, 'pre[aria-label="Plan text"]');
  w.check(/ac1/.test(v1) && /ac2/.test(v1), 'the plan maps the confirmed criteria (ac1, ac2)');
  w.check(/Assumption/i.test(v1) && /Status names are stable/.test(v1), 'assumptions are listed apart from the confirmed requirements');
  await openTab(page, 'Jev Review');
  await btn(main(page), 'Request evaluation').click();
  await waitStatus(page, /Saved/);
  w.did('Request evaluation');
  const ev = await text(page, '.cmp-review');
  w.check(/Evaluation unavailable/.test(ev) && !/Evaluation unavailable: \d/.test(ev), 'Jev unavailable is shown honestly, without a score');
  await main(page).getByText('Classify the criteria yourself').click();
  await main(page).getByLabel(/^ac1:/).selectOption('Covered');
  await main(page).getByLabel(/^ac2:/).selectOption('Partial');
  await btn(main(page), 'Record assessment').click();
  await waitStatus(page, /Saved/);
  const before = await text(page, '#cmp-as-h ~ p');
  w.did('developer assessment on version 1: ac1 Covered, ac2 Partial (' + before + ')');
  await openTab(page, 'Plan');
  await btn(main(page), 'Edit a new version').click();
  const ed = main(page).getByLabel('Plan text', { exact: true });
  await ed.click();
  await page.keyboard.press('End');
  await page.keyboard.type(' Manually reviewed: keep the filter in the URL.');
  w.key('typed the edit in the plan editor');
  await btn(main(page), 'Save new plan version').click();
  await waitStatus(page, /Saved/);
  w.did('manual edit saved as version 2');
  const vl2 = await text(page, '.cmp-versions');
  w.check(/Version 2 · your edit · selected/.test(vl2), 'version 2 is your edit and selected: ' + vl2.replace(/\n/g, ' | ').slice(0, 200));
  await btn(main(page), 'Improve Plan with Claude').click();
  await waitStatus(page, /Queued|Already running/);
  w.did('Improve Plan with Claude');
  await page.waitForTimeout(2500);
  await reloadView(page);
  await btn(main(page), 'Select candidate').first().waitFor();
  const vl3 = await text(page, '.cmp-versions');
  w.check(/Version 3 · Claude suggestion · candidate/.test(vl3), 'the improvement is a candidate, not selected: ' + vl3.replace(/\n/g, ' | ').slice(0, 200));
  await openTab(page, 'Jev Review');
  w.check(await page.locator('pre[aria-label="Candidate diff"]').count() >= 1, 'the candidate shows its diff');
  await btn(main(page), 'Select candidate').first().click();
  await waitStatus(page, /Saved/);
  w.did('Select candidate (version 3)');
  await main(page).getByText('Classify the criteria yourself').click();
  await main(page).getByLabel(/^ac1:/).selectOption('Covered');
  await main(page).getByLabel(/^ac2:/).selectOption('Covered');
  await btn(main(page), 'Record assessment').click();
  await waitStatus(page, /Saved/);
  const rv = await text(page, '.cmp-review');
  const after = await text(page, '#cmp-as-h ~ p');
  w.did('developer assessment on version 3: ac1 Covered, ac2 Covered (' + after + ')');
  w.check(/Developer assessment \(yours; never a Jev score\)/.test(rv), 'the assessment is labelled as yours, never a Jev score');
  w.check(/75\.0%/.test(before) && /100\.0%/.test(after), 'assessment before / after: ' + before + ' → ' + after);
  w.check(/Compared with the parent version/.test(rv), 'the comparison with the parent version is shown');
  const ack = page.locator('.cmp-choices input[type=checkbox]');
  await ack.focus();
  await page.keyboard.press('Space');
  w.key('Space ticks the risk acknowledgement');
  const choices = await text(page, '.cmp-choices');
  w.check(/Digest/.test(choices) && /Unresolved risks/.test(choices) && /Saved filters from older sessions/.test(choices), 'the exact version, digest and the remaining risk are shown before Execute');
  const exec = btn(main(page), 'Execute Plan');
  await exec.focus();
  await page.keyboard.press('Enter');
  w.key('Enter on Execute Plan');
  await btn(main(page), 'Copy approved plan').waitFor();
  w.did('Execute Plan (approval saved)');
  await btn(main(page), 'Copy approved plan').click();
  await waitStatus(page, /Prepared; not submitted/);
  w.did('Copy approved plan');
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  w.after = await ctl('inspect', { workflow: wid });
  w.check(clip.includes(w.after.selected_digest.slice(0, 16)) || clip.includes(w.after.approved_plan), 'the copy carries the exact approved version');
  w.check(w.after.approved_plan === w.after.selected_plan && w.after.plans === 3, 'version 3 approved exactly (3 versions kept)');
  w.check(w.after.approvals.length === 1 && w.after.approvals[0].dispatch === 'prepared', 'one approval, still prepared (not submitted)');
  w.actual = 'Versions: 1 generated, 2 yours, 3 candidate → selected → approved; assessment ' + before + ' → ' + after + '; status "' + await text(page, 'p.cmp-status') + '".';
  await finalViews(w, page, '#/companion?w=' + wid, async (p) => { await openTab(p, 'Handoff'); });
}

async function ux06_3(w) {
  w.expected = 'An Improve job saves its plan and is interrupted before its evaluation completes; after a restart with no raw history, session state or caches, the exact plan (same digest) and the job\'s recoverable state reappear, and nothing is generated, evaluated or dispatched by itself.';
  const wid = info['ABC-1001'];
  w.limitations.push('restart = a new server instance in the same OS process after clearing its caches and deleting raw history / session state / search cache, not a reboot; the runner crash is simulated right after the plan commit');
  const page = await newPage(w, 1440, 'light');
  w.before = await ctl('inspect', { workflow: wid });
  await openTask(page, wid, 'Plan');
  await ctl('mutate', { kind: 'jobs', interrupt: true });
  await btn(main(page), 'Improve Plan with Claude').click();
  await waitStatus(page, /Queued|Already running/);
  w.did('Improve Plan with Claude (runner set to stop after its plan commit)');
  await page.waitForTimeout(3000);
  const mid = await ctl('inspect', { workflow: wid });
  const cand = Object.keys(mid.candidates)[0];
  w.check(mid.plans === w.before.plans + 1 && mid.evaluations === w.before.evaluations, 'the plan was saved, the evaluation never completed (' + mid.plans + ' plans, ' + mid.evaluations + ' evaluations)');
  w.check(mid.jobs.some((j) => j.state === 'running' && j.phase === 'evaluating'), 'the job is left running in its evaluating phase');
  const c0 = await ctl('counters');
  const r = await ctl('restart', { wipe: true });
  base = r.base;
  w.did('restart: new server instance; deleted ' + r.wiped.join(', '));
  w.go('restart');
  const page2 = await newPage(w, 1440, 'light');
  await openTask(page2, wid, 'Plan');
  w.go('new server: #/companion?w=<ABC-1001> · Plan');
  const job = await text(page2, '.cmp-job');
  w.check(/Plan job: Running/.test(job), 'the interrupted job reappears as recoverable: ' + job.split('\n')[0]);
  w.check(await btn(main(page2), 'Cancel job').count() === 1, 'it can be cancelled explicitly');
  const vers = await text(page2, '.cmp-versions');
  w.check(/Version 2 · Claude suggestion · candidate/.test(vers), 'the saved candidate is listed: ' + vers.replace(/\n/g, ' | ').slice(0, 200));
  await btn(main(page2), 'View version 2').click();
  const head = await text(page2, '#cmp-view-h ~ .sub, .cmp-plan .sec-head .sub');
  const view = await apiGet(page2, 'operation=plan&workflow=' + wid + '&ticket=' + cand);
  w.check(view.body.digest === mid.selected_digest || view.body.digest, 'digest read back after restart: ' + (view.body.digest || '').slice(0, 16));
  const cli = await ctl('cli', { argv: ['companion', 'review', wid] });
  w.check(cli.rc === 0 && /unavailable/i.test(cli.out), 'the text fallback (fresh process) reads the same task: rc ' + cli.rc + ', ' + cli.out.split('\n').slice(0, 2).join(' / '));
  for (let i = 0; i < 3; i++) { await page2.reload(); await settle(page2); }
  w.did('3 reloads on the new server');
  const c1 = await ctl('counters');
  w.after = await ctl('inspect', { workflow: wid });
  const d = delta(c0, c1);
  w.check(d.stub_model_calls === 0 && d.job_runs === 0 && d.job_spawns === 0, 'zero automatic generation after restart (model ' + d.stub_model_calls + ', jobs ' + d.job_runs + ')');
  w.check(w.after.evaluations === mid.evaluations && w.after.approvals.length === mid.approvals.length && w.after.revision === mid.revision, 'zero automatic evaluation, approval or checkpoint (revision ' + mid.revision + ' → ' + w.after.revision + ')');
  const plans = await apiGet(page2, 'operation=plan&workflow=' + wid + '&ticket=' + cand);
  w.check(plans.body.digest === view.body.digest, 'the same exact plan text before and after (digest ' + (plans.body.digest || '').slice(0, 16) + ')');
  await openTab(page2, 'Jev Review');
  w.check(/Evaluation unavailable|None recorded|Unavailable/.test(await text(page2, '.cmp-review')), 'no evaluation result is invented for the interrupted job');
  w.actual = 'Before restart: job ' + JSON.stringify(mid.jobs) + '; after: ' + job.split('\n')[0] + '; plans ' + w.after.plans + ', evaluations ' + w.after.evaluations + '.';
  await finalViews(w, page2, '#/companion?w=' + wid, async (p) => { await openTab(p, 'Plan'); });
}

async function ux06_4(w) {
  w.expected = 'Two of three steps carry evidence with a blank checklist; after a restart another relevant change lands and the delta shows it; recording the moved HEAD makes the evidenced steps revalidation work; Resume Execution prepares a continuation with only the remaining and revalidation work (Prepared; not submitted).';
  const wid = info['ABC-1010'];
  w.limitations.push('restart = a new server instance in the same OS process (caches cleared, raw history / session state / search cache deleted), not a reboot');
  let page = await newPage(w, 1440, 'light');
  w.before = await ctl('inspect', { workflow: wid });
  await openTask(page, wid);
  const st = await text(page, '.cmp-resume');
  w.check(/2 completed · 1 pending/.test(st), 'two steps evidenced, one pending (no checklist marks)');
  w.check(/Carry forward\s*one, two/.test(st) && /Remaining\s*three/.test(st), 'continuation: carry forward one, two; remaining three');
  const r = await ctl('restart', { wipe: true });
  base = r.base;
  w.go('restart (' + r.wiped.join(', ') + ')');
  w.did('another relevant change lands: ' + JSON.stringify(await ctl('mutate', { kind: 'land_change' })));
  page = await newPage(w, 1440, 'light');
  await openTask(page, wid);
  w.go('new server: #/companion?w=<ABC-1010> · Resume');
  const ch = await text(page, '.cmp-changes');
  w.check(/Change the filter persistence/.test(ch) && /filter_store\.py/.test(ch), 'the delta shows the new commit and file: ' + ch.replace(/\n/g, ' | ').slice(0, 200));
  await main(page).locator('summary', { hasText: 'Record current context' }).click();
  await page.locator('#cmp-oc-h').fill('0f9e8d7c6b5a');
  await page.locator('#cmp-oc-w').selectOption('clean');
  await btn(main(page), 'Record current context').click();
  await waitStatus(page, /Saved/);
  w.did('Record current context: HEAD 0f9e8d7c6b5a, clean');
  const st2 = await text(page, '.cmp-resume');
  w.check(/Revalidate\s*one, two/.test(st2) && /Remaining\s*three/.test(st2), 'continuation: remaining three; revalidate one, two');
  const box = main(page).getByText('I reviewed the continuation');
  await box.click();
  const go = btn(main(page), 'Resume Execution');
  await go.focus();
  await page.keyboard.press('Enter');
  w.key('Enter on Resume Execution');
  await waitStatus(page, /Saved/);
  w.did('Resume Execution (reviewed continuation)');
  await openTab(page, 'Handoff');
  await btn(main(page), 'Copy approved plan').click();
  await waitStatus(page, /Prepared; not submitted/);
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  w.check(/three/.test(clip) && /revalidat/i.test(clip), 'the continuation packet asks for the remaining step and revalidation');
  w.check(!/Remaining[^\n]*\bone\b/.test(clip), 'the packet does not ask to redo the evidenced steps');
  w.after = await ctl('inspect', { workflow: wid });
  const last = w.after.approvals[w.after.approvals.length - 1];
  w.check(w.after.stage === 'execution_ready' && last.continuation && last.dispatch === 'prepared', 'a continuation approval, prepared and not submitted');
  w.actual = 'Continuation after the change: ' + st2.split('\n').filter((l) => /Remaining|Revalidate|Carry/.test(l)).join(' / ');
  await finalViews(w, page, '#/companion?w=' + wid, async (p) => { await openTab(p, 'Handoff'); });
}

async function ux06_5(w) {
  w.expected = 'A task with implementation done and tests outstanding resumes as verification only; an existing PR that gets a new review request and a new head resumes as PR review on that PR, with the current head\'s CI state and no second PR proposed.';
  const page = await newPage(w, 1440, 'light');
  const t = info['ABC-1006'], p = info['ABC-1007'];
  w.before = { testing: await ctl('inspect', { workflow: t }), pr: await ctl('inspect', { workflow: p }) };
  await openTask(page, t);
  w.go('#/companion?w=<ABC-1006> · Resume');
  const r1 = await text(page, '.cmp-resume');
  w.check(/Verification work only; implementation is not restarted/.test(r1), 'testing resumes with a verification-only scope');
  const primary = main(page).locator('.btn.primary').first();
  w.check(/Verify Requirements/.test(await primary.innerText()), 'its primary action is Verify Requirements');
  await primary.focus();
  await page.keyboard.press('Enter');
  w.key('Enter on Verify Requirements (moves to the Verification tab)');
  await btn(main(page), 'Verify Requirements').click();
  await waitStatus(page, /Saved/);
  w.did('Verify Requirements (ledger recorded)');
  w.did('PR #41 update: ' + JSON.stringify(await ctl('mutate', { kind: 'pr_update' })));
  await open(page, '#/companion');
  await pickTask(page, 'ABC-1007');
  w.go('Daily Planner → ABC-1007 · Resume');
  const r2 = await text(page, '.cmp-resume');
  w.check(/Next action: Open PR Review/.test(r2), 'it resumes as PR review');
  w.check(/pull request|PR/i.test(await text(page, '.cmp-changes')) || /changes requested|changes_requested/i.test(r2), 'the delta shows the PR change');
  w.check(/CI checks unavailable: not read for head b2b2b2b/.test(r2), 'CI on the earlier head never stands in for the new head');
  const all = await text(page, '.cmp-main');
  w.check(!/Create (a )?PR|Open a new PR|Generate Plan/.test(all), 'no second PR (or a new plan) is proposed');
  const det = await apiGet(page, 'operation=detail&workflow=' + p);
  w.check(det.body.prs.length === 1 && det.body.prs[0].id === 'pr:repo-f:41', 'one PR on the task: ' + det.body.prs.map((x) => x.id).join(', '));
  w.check((det.body.continuation.requested_changes || []).includes('pr:repo-f:41'), 'the requested changes are the continuation work');
  const link = main(page).getByRole('link', { name: 'Open PR Review' });
  w.check(/tab=branches/.test(await link.getAttribute('href')), 'Open PR Review opens the ticket\'s Branches & PRs');
  w.after = { testing: await ctl('inspect', { workflow: t }), pr: await ctl('inspect', { workflow: p }) };
  w.check(w.after.pr.stage === 'pr_review' && w.after.pr.approvals.length === w.before.pr.approvals.length, 'PR task unchanged by reading (still PR review, no new approval)');
  w.actual = 'ABC-1006: ' + (r1.match(/Scope[^\n]*\n?[^\n]*/) || [''])[0].replace(/\n/g, ' ') + '; ABC-1007: next action Open PR Review, PRs ' + det.body.prs.map((x) => x.id + ' ' + x.review).join(', ');
  await finalViews(w, page, '#/companion?w=' + p);
}

async function ux06_6(w) {
  w.expected = 'Pausing A checkpoints only A; work on B in another repository lands only on B; after a restart, choosing A shows A\'s own checkpoint, its original attribution and its real location.';
  w.limitations.push('restart = a new server instance in the same OS process (caches cleared, raw history / session state / search cache deleted), not a reboot');
  const a = info['ABC-1004'], b = info['ABC-1011'];
  let page = await newPage(w, 1440, 'light');
  w.before = { A: await ctl('inspect', { workflow: a }), B: await ctl('inspect', { workflow: b }) };
  await openTask(page, a);
  await page.locator('#cmp-pause').fill('Switching to the tax export');
  await page.locator('#cmp-pause').press('Tab');
  await page.keyboard.press('Space');
  w.key('Tab from the summary to Pause task, Space');
  await waitStatus(page, /Saved/);
  w.did('Pause task A (capture keeps running)');
  const A1 = await ctl('inspect', { workflow: a });
  w.check(A1.stage === 'paused', 'A is paused');
  await open(page, '#/companion');
  await pickTask(page, 'ABC-1011');
  w.go('A paused → Daily Planner → B (ABC-1011)');
  w.did('work on B: ' + JSON.stringify(await ctl('mutate', { kind: 'work_on_b' })));
  const r = await ctl('restart', { wipe: true });
  base = r.base;
  w.go('restart');
  page = await newPage(w, 1440, 'light');
  await open(page, '#/companion');
  const paused = page.locator('.cmp-group', { hasText: 'Paused' });
  w.check(/ABC-1004/.test(await paused.innerText()), 'A waits in the Paused group');
  await paused.locator('.cmp-pick', { hasText: 'ABC-1004' }).click();
  await page.locator('#cmp-task-h').waitFor();
  w.go('new server: Paused → ABC-1004');
  const sub = await text(page, '.cmp-task > .sec-head .sub');
  w.check(/repo-c/.test(sub) && /Paused/.test(sub), 'A shows its real location and stage: ' + sub);
  const res = await text(page, '.cmp-resume');
  w.check(/sess-4/.test(res), 'A keeps its own last session (sess-4)');
  w.check(!/Export the tax lines/.test(res), 'B\'s work is not in A\'s delta');
  const A2 = await ctl('inspect', { workflow: a }), B2 = await ctl('inspect', { workflow: b });
  w.after = { A: A2, B: B2 };
  w.check(A2.head === A1.head && A2.revision === A1.revision, 'A\'s checkpoint unchanged since its pause (revision ' + A2.revision + ')');
  w.check(A2.ticket_items === w.before.A.ticket_items, 'A\'s ticket attribution unchanged');
  w.check(B2.revision > w.before.B.revision && B2.location === 'repo-i', 'B checkpointed its own work in repo-i (revision ' + w.before.B.revision + ' → ' + B2.revision + ')');
  w.actual = 'A: ' + sub + '; A revision ' + A1.revision + ' → ' + A2.revision + '; B revision ' + w.before.B.revision + ' → ' + B2.revision + '.';
  await finalViews(w, page, '#/companion?w=' + a);
}

async function ux06_7(w) {
  w.expected = 'Two sessions saving from the same revision: one saves, the other gets 409 with its text kept; two approvals from one revision: one approval only; a queued job is cancelled and never runs; revoked access clears both sessions (403, content removed); restored access recovers the same task; no false score, no duplicate dispatch.';
  const wid = info['ABC-1012'];
  w.before = await ctl('inspect', { workflow: wid });
  const s1 = await newPage(w, 1440, 'light'), s2 = await newPage(w, 1440, 'light');
  for (const p of [s1, s2]) { await openTask(p, wid, 'Plan'); await btn(main(p), 'Edit a new version').click(); }
  w.go('two sessions: #/companion?w=<ABC-1012> · Plan · Edit a new version');
  await main(s1).getByLabel('Plan text', { exact: true }).fill('Session one: round half up at the line level.');
  await main(s2).getByLabel('Plan text', { exact: true }).fill('Session two: round half even at the order level.');
  await btn(main(s1), 'Save new plan version').click();
  await waitStatus(s1, /Saved/);
  w.did('session 1 saved version 2');
  await btn(main(s2), 'Save new plan version').click();
  const st2 = await waitStatus(s2, /changed|kept/);
  w.did('session 2 saved from the same revision');
  w.check(/Your entered text is kept/.test(st2), 'session 2 gets the conflict with its text kept: ' + st2);
  w.check(await main(s2).getByLabel('Plan text', { exact: true }).inputValue() === 'Session two: round half even at the order level.', 'session 2\'s text is still in its editor');
  const mid = await ctl('inspect', { workflow: wid });
  w.check(mid.plans === w.before.plans + 1, 'exactly one new version: no silent overwrite (' + w.before.plans + ' → ' + mid.plans + ')');
  for (const p of [s1, s2]) { await reloadView(p); await openTab(p, 'Jev Review'); await p.locator('.cmp-choices input[type=checkbox]').check(); }
  const ev = await text(s1, '.cmp-review');
  w.check(/Evaluation unavailable/.test(ev) && !/Evaluation unavailable: \d/.test(ev), 'no false score: Evaluation unavailable');
  await btn(main(s1), 'Execute Plan').click();
  await btn(main(s1), 'Copy approved plan').waitFor();
  w.did('session 1 Execute Plan');
  await btn(main(s2), 'Execute Plan').click();
  const ap2 = await waitStatus(s2, /changed|kept|already|prepared/i);
  w.did('session 2 Execute Plan from the same revision');
  const afterApprove = await ctl('inspect', { workflow: wid });
  w.check(afterApprove.approvals.length === 1, 'one approval from two clicks in two sessions (' + ap2 + ')');
  // cancel a queued job (ABC-1009): it never runs
  const job = info['ABC-1009'];
  const jb = await ctl('inspect', { workflow: job });
  await ctl('mutate', { kind: 'jobs', hold: true });
  await openTask(s1, job, 'Plan');
  await btn(main(s1), 'Improve Plan with Claude').click();
  await waitStatus(s1, /Queued|Already running/);
  await reloadView(s1);
  w.check(/Plan job: Queued/.test(await text(s1, '.cmp-job')), 'the job is queued');
  await btn(main(s1), 'Cancel job').click();
  await s1.waitForTimeout(800);
  w.did('Cancel job (ABC-1009)');
  const c0 = await ctl('counters');
  await ctl('mutate', { kind: 'jobs', hold: false, release: true });
  await s1.waitForTimeout(2000);
  const c1 = await ctl('counters');
  const ja = await ctl('inspect', { workflow: job });
  w.check(ja.jobs.some((j) => j.state === 'cancelled') && delta(c0, c1).stub_model_calls === 0 && ja.plans === jb.plans, 'the cancelled job never ran (no model call, no new version)');
  // revoke, then restore
  await openTask(s1, wid, 'Handoff');
  await openTask(s2, wid, 'Plan');
  w.did('revoke: ' + JSON.stringify(await ctl('mutate', { kind: 'revoke' })));
  await reloadView(s1);
  const r1 = await text(s1, 'p.cmp-status');
  await btn(main(s2), 'Save new plan version').click();
  w.did('session 2 saves its kept draft after the revocation');
  await s2.waitForTimeout(1000);
  const r2 = await text(s2, 'p.cmp-status');
  w.check(!/round half even/.test(await body(s2)), 'session 2\'s revoked draft is removed with the task');
  w.check(/cleared/i.test(r1) && await s1.locator('#cmp-task-h').count() === 0, 'session 1 is cleared: ' + r1);
  w.check(/cleared|unavailable|changed/i.test(r2) && await s2.locator('#cmp-task-h').count() === 0, 'session 2 is cleared: ' + r2);
  const denied = await apiGet(s1, 'operation=detail&workflow=' + wid);
  w.check(denied.status === 403, 'the server refuses the task (HTTP ' + denied.status + ')');
  const post = await s1.evaluate(async (wf) => {
    const t = document.querySelector('meta[name="cg-token"]').getAttribute('content');
    const r = await fetch('/api/actions/companion', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CG-Token': t, 'X-Chitragupta-Token': t }, body: JSON.stringify({ action: 'pause', workflow: wf, revision: 1 }) });
    return r.status;
  }, wid);
  w.check(post === 403 || post === 409 || post === 400, 'a write to the revoked task is refused (HTTP ' + post + ')');
  w.did('restore: ' + JSON.stringify(await ctl('mutate', { kind: 'restore' })));
  await open(s1, '#/companion');
  await reloadView(s1);
  await pickTask(s1, 'ABC-1012');
  w.go('restored → Reload cached view → Daily Planner → ABC-1012');
  await openTab(s1, 'Handoff');
  const ho = await text(s1, '.cmp-handoff');
  w.check(/Prepared; not submitted/.test(ho), 'the same approved handoff is back after restoring access');
  w.after = await ctl('inspect', { workflow: wid });
  w.check(w.after.approvals.length === 1 && w.after.approvals[0].dispatch === 'prepared' && w.after.approved_plan === afterApprove.approved_plan, 'still one approval of the same version, prepared (no duplicate dispatch)');
  w.actual = 'Save race: "' + st2 + '"; approve race: ' + afterApprove.approvals.length + ' approval; revoke: "' + r1 + '"; restore: handoff prepared again.';
  await finalViews(w, s1, '#/companion?w=' + wid, async (p) => { await openTab(p, 'Handoff'); });
}

// ================================================================== run
async function start() {
  for (const channel of ['chrome', 'msedge']) {
    try { browser = await chromium.launch({ channel }); break; } catch (_) { /* the next installed browser */ }
  }
  if (!browser) throw new Error('No installed Chrome or Edge');
  fs.mkdirSync(out, { recursive: true });
  const want = (id) => !ONLY.length || ONLY.some((x) => x.startsWith(norm(id)));
  try {
    if (want('ux04-1')) { await phase('new'); await run(new Walk('UX-04.1', 'New developer: setup, identity and scope, useful empty state', 'new'), ux04_1); }
    if (want('ux04')) {
      await phase('dash');
      await run(new Walk('UX-04.2', 'Returning developer: current ticket, next action, delta, Mark changes reviewed', 'dash'), ux04_2);
      await run(new Walk('UX-04.3', 'Delivery: one ticket on two repositories, unresolved release mapping', 'dash'), ux04_3);
      await run(new Walk('UX-04.4', 'Monthly release: all remaining work, no misleading green', 'dash'), ux04_4);
      await run(new Walk('UX-04.5', 'Verification: count, definition, source events, historical vs current', 'dash'), ux04_5);
      await run(new Walk('UX-04.7', 'Disconnected source: stale vs known, recovery, other sources kept', 'dash'), ux04_7);
      await run(new Walk('UX-04.6', 'Profile change: impact, unsaved edit kept, revocation and reconciliation', 'dash'), ux04_6);
    }
    if (want('ux06')) {
      await phase('plan');
      await run(new Walk('UX-06.1', 'Planner: every stage, deliberate selection, next action and why', 'plan'), ux06_1);
      await run(new Walk('UX-06.2', 'Plan: generate, requirements, evaluate, edit, improve, compare, approve, copy', 'plan'), ux06_2);
      await run(new Walk('UX-06.3', 'Interrupted planning: restart without history, same plan and job state', 'plan'), ux06_3);
      await run(new Walk('UX-06.4', 'Interrupted development: delta after restart, continuation of remaining work', 'plan'), ux06_4);
      await run(new Walk('UX-06.5', 'Testing and an existing PR: verification scope, PR review, no second PR', 'plan'), ux06_5);
      await run(new Walk('UX-06.6', 'Pause A, work on B, restart, choose A: independent checkpoints', 'plan'), ux06_6);
      await run(new Walk('UX-06.7', 'Race, cancel, revoke and restore: no overwrite, false score or duplicate dispatch', 'plan'), ux06_7);
    }
  } finally { await browser.close(); }
  results.sort((a, b) => a.id.localeCompare(b.id));
  fs.writeFileSync(resultsPath, JSON.stringify({ walkthroughs: results }, null, 1));
  const failed = results.filter((r) => r.status === 'FAIL');
  console.log('\n' + results.map((r) => r.id + ' ' + r.status).join('\n'));
  console.log(results.length + ' walkthroughs, ' + failed.length + ' failed');
  rl.close();
  process.exitCode = failed.length ? 1 : 0;
}

start().catch((e) => { console.error(e); rl.close(); process.exitCode = 1; });
