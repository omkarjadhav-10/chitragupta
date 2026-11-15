// Database workspace walkthroughs (DB-20/21/23, DBT-33/35/37 render half), loaded by check.js when run.py merged the
// database scenario (dbhome.py) into the synthetic home. The seven PRD walkthroughs at 390/768/900/1280/1440 px in
// light and dark, then a keyboard-only pass of Explain flow and Record journey. Fails on: root horizontal scroll, a
// serious/critical axe violation (1440 px), a console or page error, a request leaving 127.0.0.1, a request carrying the
// record key in its URL, any transport / mcpjobs.run / process call while rendering (counted by the fake transport in
// the server process; the trace itself runs only through that fake), a lost input after an error or a refresh, a
// cached detail first view slower than 1 s, and a revoked connection or profile whose data is still reachable.
const fs = require('fs');
const path = require('path');

const AXE = fs.readFileSync(path.join(__dirname, 'vendor', 'axe.min.js'), 'utf8');
const WIDTHS = [1440, 1280, 900, 768, 390];
const KEY = { tenant_id: '7', id: '1001' };
const LIGHT_BG = 'rgb(250, 250, 248)', DARK_BG = 'rgb(15, 18, 22)';

function readJson(p) { return JSON.parse(fs.readFileSync(p, 'utf8')); }
function snip(text) { return process.env.CG_UI_DB_VERBOSE ? ' [' + String(text).replace(/\s+/g, ' ').slice(0, 700) + ']' : ''; }

class Walk {
  constructor(browser, base, check, shots, info) {
    Object.assign(this, { browser, base, check, shots, info, timings: [] });
    this.assets = shots ? path.dirname(path.resolve(shots)) : null;
  }

  counts() { const c = readJson(this.info.calls); return { transport: c.transport || 0, model: c.mcpjobs_run || 0, processes: c.processes || 0 }; }

  // rendering must not move any counter; a trace run may only add transport calls (never mcpjobs.run or a process)
  same(before, what, allowTransport) {
    const now = this.counts();
    this.check(now.model === before.model && now.processes === before.processes && (allowTransport || now.transport === before.transport),
      what + ': no ' + (allowTransport ? 'model call or process' : 'transport, model or process call') + ' (' + JSON.stringify(before) + ' -> ' + JSON.stringify(now) + ')');
    return now;
  }

  async open(theme, width) {
    this.ctx = await this.browser.newContext({ viewport: { width, height: 900 }, colorScheme: theme, bypassCSP: true, reducedMotion: 'reduce' });
    const page = this.page = await this.ctx.newPage();
    this.theme = theme; this.width = width; this.errors = [];
    page.on('pageerror', (e) => this.errors.push(e.message));
    page.on('console', (m) => { if (m.type() === 'error') this.errors.push(m.text()); });
    page.on('request', (r) => {
      const u = r.url();
      if (!u.startsWith(this.base) && !/^(data|blob):/.test(u)) this.errors.push('request left the machine: ' + u);
      const rest = decodeURIComponent(u.slice(this.base.length)).replace(/ABC-1\d{3}/g, '');
      if (/(^|[^0-9a-z])1001([^0-9a-z]|$)/i.test(rest) || /tenant_id=/.test(rest)) this.errors.push('a record key value in a request URL: ' + u);
    });
    return page;
  }

  async close() { await this.ctx.close(); }

  async go(hash) {
    const t0 = Date.now();
    this.nav = (this.nav || 0) + 1;  // a new document each time: a hash-only change would keep component state
    await this.page.goto(this.base + '?theme=' + this.theme + '&n=' + this.nav + hash, { waitUntil: 'domcontentloaded' });
    await this.page.waitForSelector('#page-title', { timeout: 10000 });
    await this.idle();
    return Date.now() - t0;
  }

  async idle() { await this.page.waitForFunction(() => !document.querySelector('.skeleton'), null, { timeout: 10000 }).catch(() => this.errors.push('still loading after 10 s')); }

  tag(name) { return name + ' @ ' + this.width + ' ' + this.theme; }

  // layout, theme and errors after each walkthrough; axe at 1440 px; screenshots (and the representative copies)
  async settle(name, label) {
    const page = this.page, t = this.tag(name);
    await page.waitForTimeout(150);
    this.check(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)), t + ': no horizontal scroll');
    const theme = await page.evaluate(() => [document.documentElement.getAttribute('data-theme'), getComputedStyle(document.body).backgroundColor]);
    this.check(theme[0] === this.theme && theme[1] === (this.theme === 'light' ? LIGHT_BG : DARK_BG), t + ': renders in ' + this.theme + ' (' + theme.join(', ') + ')');
    if (this.width === 1440) {
      await page.addScriptTag({ content: AXE });
      const v = await page.evaluate(async () => (await window.axe.run(document, { runOnly: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] }))
        .violations.filter((x) => x.impact === 'serious' || x.impact === 'critical').map((x) => x.id + ' ×' + x.nodes.length + ' (' + x.nodes[0].target.join(' ') + ')'));
      this.check(!v.length, t + ': axe serious/critical ' + (v.join(', ') || 'none'));
    }
    this.check(!this.errors.length, t + ': no errors' + (this.errors.length ? ' — ' + this.errors.join(' | ') : ''));
    this.errors.length = 0;
    if (this.shots) {
      const file = path.join(this.shots, 'db-' + name + '-' + this.width + '-' + this.theme + '.png');
      await page.screenshot({ path: file });
      if (this.assets && ((this.width === 1440 && this.theme === 'light') || (this.width === 390 && this.theme === 'dark'))) {
        fs.copyFileSync(file, path.join(this.assets, label + '-' + this.width + '-' + this.theme + '.png'));
      }
    }
  }

  // an extra screenshot with one element scrolled into view (no checks: settle() does those)
  async shot(name, label, sel) {
    if (!this.shots) return;
    if (sel) await this.page.locator(sel).first().scrollIntoViewIfNeeded().catch(() => {});
    await this.page.waitForTimeout(150);
    const file = path.join(this.shots, 'db-' + name + '-' + this.width + '-' + this.theme + '.png');
    await this.page.screenshot({ path: file });
    if (this.assets && ((this.width === 1440 && this.theme === 'light') || (this.width === 390 && this.theme === 'dark'))) {
      fs.copyFileSync(file, path.join(this.assets, label + '-' + this.width + '-' + this.theme + '.png'));
    }
  }

  async tab(name) {
    await this.page.getByRole('tab', { name, exact: true }).click();
    await this.idle();
  }

  async dialogClose() { await this.page.keyboard.press('Escape'); await this.page.waitForSelector('dialog[open]', { state: 'detached', timeout: 3000 }).catch(() => {}); }
}

module.exports.Walk = Walk;
module.exports.KEY = KEY;
module.exports.WIDTHS = WIDTHS;

// 1. Discover one table: a shared name lists both candidates (nothing chosen), then the passport of the chosen one.
Walk.prototype.discover = async function () {
  const p = this.page, t = this.tag('discover one table'), before = this.counts();
  await this.go('#/db');
  await p.fill('#dbx-q', 'shop.orders');
  await p.selectOption('#dbx-kind', 'table');
  await p.getByRole('button', { name: 'Find', exact: true }).click();
  await p.waitForSelector('.dbx-cands .rlist button', { timeout: 10000 }).catch(async (e) => { throw new Error(e.message.split('\n')[0] + ' url=' + p.url() + ' q=' + await p.inputValue('#dbx-q') + ' kind=' + await p.inputValue('#dbx-kind')); });
  const cands = await p.locator('.dbx-cands .rlist button').allInnerTexts();
  this.check(cands.length === 2 && cands.some((c) => /qa-main/.test(c)) && cands.some((c) => /dev-main/.test(c)) &&
    /nothing is chosen for you/.test(await p.locator('.dbx-cands').innerText()), t + ': both environments’ shop.orders listed, none chosen');
  await p.locator('.dbx-cands .rlist button', { hasText: 'shop.orders (qa-main)' }).click();
  await p.waitForSelector('[role=tab]', { timeout: 10000 });
  await this.tab('Evidence');
  const t0 = Date.now();
  await p.locator('.tabpanel .rlist button', { hasText: 'shop.orders (qa-main)' }).first().click();
  await p.waitForSelector('section.dbx-passport table', { timeout: 10000 });
  const ms = Date.now() - t0;
  const pass = await p.locator('section.dbx-passport').innerText();
  this.check(/Columns \(5\)/.test(pass) && /Writers/.test(pass) && /Static analysis/i.test(pass), t + ': passport with columns, sources and its limits');
  if (this.width === 1440) { this.timings.push(['passport (cached detail)', this.theme, ms]); this.check(ms < 1000, t + ': passport first view ' + ms + ' ms'); }
  this.same(before, t);
  await p.locator('section.dbx-passport').scrollIntoViewIfNeeded();
  await this.settle('1-discover', 'walkthrough-1-discover-one-table');
};

// 2. Explain one operation: focus header, cited steps, a step's detail, the graph and its equivalent table.
Walk.prototype.explain = async function () {
  const p = this.page, t = this.tag('explain one operation'), before = this.counts();
  await this.go('#/db');
  await p.fill('#dbx-q', this.info.ticket);
  await p.getByRole('button', { name: 'Find', exact: true }).click();
  await p.locator('.dbx-cands .rlist button', { hasText: this.info.ticket }).click();
  await p.waitForSelector('.dbx-stepbtn', { timeout: 10000 });
  const focus = await p.locator('section.dbx-focus').innerText();
  this.check(/Last observed:/.test(focus) && /Unresolved:/.test(focus) && /Next:/.test(focus), t + ': focus header (last observed, unresolved, next)');
  await p.locator('.dbx-stepbtn', { hasText: 'insert billing.refunds (qa-billing)' }).click();
  await p.waitForSelector('#dbx-detail', { timeout: 5000 });
  this.check((await p.locator('#dbx-detail').innerText()).includes('billing-worker/refunds/consumer.py:7-8'), t + ': step detail cites the consumer');
  await p.getByRole('button', { name: 'Expand relevant graph', exact: true }).click();
  await p.waitForSelector('figure.diagram .diagram-canvas svg, .diagram-fail', { timeout: 15000 });
  this.check(await p.locator('.diagram-fail').count() === 0, t + ': the graph draws');
  const clusters = await p.locator('figure.diagram .diagram-canvas svg g.cluster').count();
  if (!clusters) console.log('    svg classes: ' + (await p.evaluate(() => Array.from(new Set(Array.from(document.querySelectorAll('figure.diagram .diagram-canvas svg g')).map((g) => g.getAttribute('class')))).join(' / '))).slice(0, 600));
  this.check(clusters >= 3, t + ': database / service boundaries drawn as boxes (' + clusters + ')');
  await this.shot('2-graph', 'walkthrough-2-explain-graph', 'figure.diagram');
  await p.locator('details.diagram-alt > summary').click();
  const rows = await p.locator('details.diagram-alt tbody tr').allInnerTexts();
  this.check(rows.length >= 10 && rows.some((r) => /unknown boundary/.test(r)) && rows.some((r) => /\(async\)/.test(r)) && rows.some((r) => /→ qa-billing \(QA\)/.test(r)),
    t + ': the table lists the same edges with direction, basis, async and boundaries (' + rows.length + ' rows)');
  await p.locator('details.diagram-alt tbody button').first().click();
  await p.waitForTimeout(100);
  this.check(await p.evaluate(() => document.activeElement && document.activeElement.id === 'dbx-detail'), t + ': a table row opens and focuses the step detail');
  this.same(before, t);
  await this.settle('2-explain', 'walkthrough-2-explain-one-operation');
};

// 3. Trace a record across two databases: plan (an error keeps the input), run through the fake transport only,
// observations; an unsaved note survives the refresh the job's end causes.
Walk.prototype.trace = async function () {
  const p = this.page, t = this.tag('trace a record'), before = this.counts();
  const ms = await this.go('#/db?flow=' + this.info.flow);
  await p.waitForSelector('.dbx-stepbtn', { timeout: 10000 });
  if (this.width === 1440) { this.timings.push(['saved flow (cached detail)', this.theme, ms]); this.check(ms < 1000, t + ': saved flow first view ' + ms + ' ms'); }
  await p.locator('section.dbx-focus').getByRole('button', { name: 'Review trace plan', exact: true }).click();
  await p.waitForSelector('#dbx-obj', { timeout: 10000 });
  await p.waitForTimeout(150);
  this.check(await p.evaluate(() => ['dbx-env', 'dbx-obj'].indexOf(document.activeElement.id) >= 0), t + ': Review trace plan moves focus to the form');
  if (await p.locator('#dbx-inv').count()) {
    await p.selectOption('#dbx-inv', this.info.investigation);
    await p.waitForSelector('#dbx-notes', { timeout: 10000 });
    await p.fill('#dbx-notes', 'Unsaved draft: the refund looks delayed');
  }
  await p.selectOption('#dbx-obj', this.info.orders);
  await p.fill('#dbx-key-tenant_id', KEY.tenant_id);
  await p.fill('#dbx-key-id', 'abc');
  await p.locator('form').getByRole('button', { name: 'Review trace plan', exact: true }).click();
  await p.waitForSelector('form [role=alert]', { timeout: 10000 });
  this.errors = this.errors.filter((e) => !/status of 422/.test(e));  // the refused key's answer, asked for on purpose
  const kept = [await p.inputValue('#dbx-key-tenant_id'), await p.inputValue('#dbx-key-id')];
  this.check(kept[0] === KEY.tenant_id && kept[1] === 'abc' && await p.evaluate(() => document.activeElement.id === 'dbx-key-id'),
    t + ': a refused key keeps every typed value and focuses the field (' + kept.join(', ') + ')');
  await p.fill('#dbx-key-id', KEY.id);
  await p.locator('form').getByRole('button', { name: 'Review trace plan', exact: true }).click();
  await p.waitForSelector('#dbx-plan-h', { timeout: 10000 });
  const plan = await p.locator('section[aria-labelledby="dbx-plan-h"]').innerText();
  this.check(/values not shown/.test(plan) && !/1001/.test(plan) && /qa-billing/.test(plan), t + ': the plan names both connections and masks the key');
  const planned = this.same(before, t + ' (plan reviewed)');
  await p.getByRole('button', { name: 'Run trace', exact: true }).click();
  await p.waitForFunction(() => /Trace ended/.test((document.getElementById('dbx-live') || {}).textContent || ''), null, { timeout: 30000 });
  await this.idle();
  const after = this.same(planned, t + ' (run)', true);
  this.check(after.transport > planned.transport && after.transport - planned.transport <= 10, t + ': the run sent ' + (after.transport - planned.transport) + ' read(s) through the fake transport (at most 10)');
  await p.waitForSelector('li.dbx-obs', { timeout: 10000 });
  this.check(/flow=/.test(p.url()), t + ': the selected flow stays selected');
  if (await p.locator('#dbx-notes').count()) this.check((await p.inputValue('#dbx-notes')).includes('Unsaved draft'), t + ': the unsaved note survives the refresh');
  // the sticky header and table rail on a long page: scrolled to the end, the header sits at the viewport's top and the
  // rail inside the viewport (a full-page capture draws them mid-page; that is the capture, not the layout)
  const sticky = await p.evaluate(() => { window.scrollTo(0, document.documentElement.scrollHeight);
    const top = document.querySelector('.top').getBoundingClientRect(), rail = document.querySelector('.rail');
    const r = rail ? rail.getBoundingClientRect() : null;
    return { y: Math.round(scrollY), top: Math.round(top.top), rail: r ? (getComputedStyle(rail).position === 'static' || (r.top >= top.bottom - 1 && r.top < innerHeight)) : true }; });
  this.check(sticky.y > 0 && sticky.top === 0 && sticky.rail, t + ': scrolled ' + sticky.y + ' px, the header stays at the top and the rail in view');
  await p.evaluate(() => window.scrollTo(0, 0));
  await this.settle('3-trace', 'walkthrough-3-trace-across-two-databases');
};

// 4. Inspect a missing downstream observation: "not observed within this query scope", never loss or failure.
Walk.prototype.missing = async function () {
  const p = this.page, t = this.tag('missing downstream observation'), before = this.counts();
  const obs = p.locator('li.dbx-obs', { hasText: 'billing.refunds (qa-billing)' }).first();
  const text = await obs.innerText();
  await obs.scrollIntoViewIfNeeded();
  await this.shot('4-observation', 'walkthrough-4-missing-observation-in-the-run', null);
  const label = await obs.locator('.status').first().innerText();
  this.check(/not observed within this query scope/i.test(label) && /does not show the record is absent everywhere, deleted, lost/i.test(text) && /Next:/i.test(text),
    t + ': the refund is "not observed in scope" (not loss, deletion or failure) with a next step' + snip(label + ' || ' + text));
  await this.tab('Explain flow');
  await p.waitForSelector('section.dbx-focus', { timeout: 10000 });
  await p.waitForFunction(() => /billing\.refunds/.test(document.querySelector('section.dbx-focus').innerText), null, { timeout: 5000 }).catch(() => {});
  this.check(/billing\.refunds/.test(await p.locator('section.dbx-focus').innerText()), t + ': the focus header names the unresolved refund');
  await p.locator('section.dbx-focus').getByRole('button', { name: 'View evidence', exact: true }).click();
  await this.idle();
  this.check((await p.locator('.tabpanel').innerText()).includes('billing-worker/refunds/consumer.py'), t + ': the evidence lists the consumer’s code');
  this.same(before, t);
  await this.settle('4-missing', 'walkthrough-4-missing-downstream-observation');
};

// 5. Analyze a column change: Known impacts of billing.refunds.order_ref (a rename), computed from cached metadata.
Walk.prototype.impact = async function () {
  const p = this.page, t = this.tag('analyze a column change'), before = this.counts();
  await this.go('#/db?flow=' + this.info.flow + '&tab=evidence');
  await p.locator('.tabpanel .rlist button', { hasText: 'billing.refunds (qa-billing)' }).first().click();
  await p.waitForSelector('section.dbx-passport table', { timeout: 10000 });
  await p.getByRole('button', { name: 'Known impacts of billing.refunds.order_ref', exact: true }).click();
  await p.waitForSelector('dialog[open] #dbx-change', { timeout: 5000 });
  await p.selectOption('dialog[open] #dbx-change', 'rename');
  await p.waitForSelector('dialog[open] [data-change=rename] .dbx-headline, dialog[open] [data-change=rename] button.primary', { timeout: 10000 });
  const compute = p.locator('dialog[open]').getByRole('button', { name: /from cached metadata/ });
  if (await compute.count()) {
    await compute.click();
    await p.waitForSelector('dialog[open] .dbx-headline', { timeout: 10000 });
  }
  const text = await p.locator('dialog[open]').innerText();
  this.check(/3 affected: 2 recorded impact links/.test(text) && /Affected \(3\)/.test(text), t + ': the headline counts what is listed');
  this.check(/Implements the mapping \(writer\)/i.test(text) && /refunds\.on_order_cancelled/.test(text) && /consumer\.py:7-8/.test(text) && /Direct evidence/i.test(text),
    t + ': the consumer implementing the mapping is listed with its citation' + snip(text));
  this.check(/nothing was run/.test(text) && /Coverage and limits/.test(text), t + ': suggestions only, limits shown');
  this.same(before, t);
  await this.settle('5-impact', 'walkthrough-5-analyze-a-column-change');
  await this.dialogClose();
};

// 6. Compare two permitted environments: qa-main vs dev-main (pair confirmed explicitly), then a blocked side.
Walk.prototype.compare = async function () {
  const p = this.page, t = this.tag('compare two environments'), before = this.counts();
  await this.go('#/db?flow=' + this.info.flow + '&tab=evidence');
  await p.locator('.tabpanel .rlist button', { hasText: 'shop.orders (qa-main)' }).first().click();
  await p.waitForSelector('section.dbx-passport table', { timeout: 10000 });
  await p.locator('section.dbx-passport').getByRole('button', { name: 'Compare metadata', exact: true }).click();
  await p.waitForSelector('dialog[open] #dbx-right', { timeout: 5000 });
  this.check(await p.inputValue('dialog[open] #dbx-right') === 'dev-main', t + ': the other permitted environment is offered first');
  const dlg = p.locator('dialog[open]');
  await p.waitForSelector('dialog[open] .dbx-panel > div[aria-live]', { timeout: 10000 });
  if (await dlg.getByRole('button', { name: 'Compare cached metadata', exact: true }).count()) {
    await dlg.getByRole('button', { name: 'Compare cached metadata', exact: true }).click();
    await p.waitForSelector('dialog[open] .dbx-cats', { timeout: 10000 });
  }
  const confirm = dlg.getByRole('button', { name: /^Confirm shop\.orders/ });
  if (await confirm.count()) {
    await confirm.click();
    await p.waitForFunction(() => /varchar\(20\)/.test(document.querySelector('dialog[open]').innerText), null, { timeout: 10000 });
  }
  const text = await dlg.innerText();
  this.check((text.match(/metadata as of|snapshot /g) || []).length >= 2 && await p.locator('dialog[open] .dbx-cats li').count() === 6,
    t + ': both sides’ times and the six categories');
  this.check(/varchar\(32\)/.test(text) && /varchar\(20\)/.test(text) && /Matching schema says nothing about data or deployed code\./.test(text),
    t + ': the type difference, and what a schema match does not say');
  this.same(before, t);
  await this.settle('6-compare', 'walkthrough-6-compare-two-environments');
  await p.selectOption('dialog[open] #dbx-right', 'uat-main');
  await p.waitForFunction(() => /not permitted/.test(document.querySelector('dialog[open] .dbx-panel').innerText) && /Blocked/.test(document.querySelector('dialog[open] .dbx-panel').innerText), null, { timeout: 10000 }).catch(() => {});
  const blocked = await dlg.innerText();
  this.check(/Blocked/.test(blocked) && /uat is not in qa_data\.allowed_envs/.test(blocked) && !(await dlg.getByRole('button', { name: 'Compare cached metadata' }).count()),
    t + ': a side that is not permitted shows its reason and offers nothing to widen it');
  this.same(before, t + ' (blocked side)');
  await this.dialogClose();
};

// 7. Revoke a connection, then a profile: the old data is gone from views, search and detail by ID; then restored.
Walk.prototype.revoke = async function () {
  const p = this.page, t = this.tag('revoke'), info = this.info, orig = fs.readFileSync(info.config, 'utf8');
  const get = async (u) => { const r = await p.request.get(this.base + u); return { status: r.status(), text: await r.text() }; };
  const dbHits = (s) => { try { return (JSON.parse(s).groups || []).filter((g) => /^db/.test(g.type || '')).flatMap((g) => g.results || []).map((r) => JSON.stringify(r)); } catch (e) { return ['unreadable: ' + s.slice(0, 80)]; } };
  const pre = dbHits((await get('api/search?q=refunds')).text);
  this.check((await get('api/db/investigation?id=' + info.investigation)).status === 200 && pre.some((h) => /refunds/.test(h)), t + ': before: the investigation and the database search results are there');
  try {
    const cfg = JSON.parse(orig);
    delete cfg.mcp.database.scope.connections['qa-billing'];
    fs.writeFileSync(info.config, JSON.stringify(cfg));
    await this.go('#/db?flow=' + info.flow);
    await p.waitForSelector('.dbx-stepbtn', { timeout: 10000 });
    const page = await p.locator('main').innerText();
    const steps = (await p.locator('.dbx-steps').allInnerTexts()).join(' | ');
    this.check(!/billing\.refunds/.test(steps) && /outside your access now/.test(page),
      t + ': the saved flow drops the revoked connection’s steps and says so' + snip(steps + ' || ' + page));
    this.check(!/qa-billing|refunds/.test(await p.locator('.rail').innerText()) && !/qa-billing/.test((await get('api/db')).text),
      t + ': the DB map rail no longer lists the revoked connection’s tables');
    this.check((await get('api/db/passport?id=' + info.refunds)).status === 404, t + ': the passport by ID is gone (404)');
    this.check((await get('api/db/investigation?id=' + info.investigation)).status === 404, t + ': the investigation by ID is gone (404)');
    this.check(!(await get('api/db/investigations')).text.includes(info.investigation), t + ': the investigation is not listed');
    const post = dbHits((await get('api/search?q=refunds')).text);
    this.check(!post.some((h) => /billing\.refunds|qa-billing/.test(h)), t + ': search has no result from the revoked connection (' + post.length + ' database results left)');
    await this.settle('7-revoke', 'walkthrough-7-revoke-connection-and-profile');
    const other = JSON.parse(orig);
    other.git_author_emails = ['bob@acme.test'];
    fs.writeFileSync(info.config, JSON.stringify(other));
    this.check((await get('api/db/investigation?id=' + info.investigation)).status === 404 && !(await get('api/db/investigations')).text.includes(info.investigation),
      t + ': another profile cannot open or list the investigation');
  } finally {
    fs.writeFileSync(info.config, orig);
  }
  this.check((await get('api/db/investigation?id=' + info.investigation)).status === 200, t + ': restoring the scope shows it again (nothing was deleted by the check)');
};

// Keyboard only: Explain flow (step, graph, its table, the detail) and Record journey (form to a reviewed plan).
Walk.prototype.keyboard = async function () {
  const p = this.page, t = this.tag('keyboard'), before = this.counts();
  await this.go('#/db?flow=' + this.info.flow);
  await p.waitForSelector('.dbx-stepbtn', { timeout: 10000 });
  const stops = [];
  const tabTo = async (test, max, back) => {
    for (let i = 0; i < (max || 120); i++) {
      await p.keyboard.press(back ? 'Shift+Tab' : 'Tab');
      const s = await p.evaluate(() => { const e = document.activeElement; if (!e || e === document.body) return null;
        const cs = getComputedStyle(e); return { id: e.id, cls: String(e.className || ''), role: e.getAttribute('role'), tag: e.tagName, text: (e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 40), ring: cs.outlineStyle !== 'none' }; });
      if (s) stops.push(s);
      if (s && test(s)) return s;
    }
    return null;
  };
  this.check(!!(await tabTo((s) => /dbx-stepbtn/.test(s.cls))), t + ': Tab reaches the first step');
  await p.keyboard.press('Enter');
  this.check(/Citations/.test(await p.locator('#dbx-detail').innerText().catch(() => '')), t + ': Enter opens the step detail with citations');
  this.check(!!(await tabTo((s) => s.text === 'Expand relevant graph')), t + ': Tab reaches Expand relevant graph');
  await p.keyboard.press('Enter');
  await p.waitForSelector('figure.diagram .diagram-canvas svg, .diagram-fail', { timeout: 15000 });
  this.check(!!(await tabTo((s) => s.tag === 'SUMMARY')), t + ': Tab reaches the graph’s text alternative');
  await p.keyboard.press('Enter');
  this.check(!!(await tabTo((s) => s.tag === 'BUTTON' && /wrapname/.test(s.cls) && s.text.length > 0, 40)), t + ': Tab reaches an edge in the table');
  await p.keyboard.press('Enter');
  await p.waitForTimeout(100);
  this.check(await p.evaluate(() => document.activeElement.id === 'dbx-detail'), t + ': Enter on an edge focuses its detail');
  this.check(!!(await tabTo((s) => s.role === 'tab', 160, true)), t + ': Shift+Tab reaches the view tabs');
  await p.keyboard.press('ArrowRight');
  await p.waitForSelector('#dbx-obj', { timeout: 10000 });
  this.check(/tab=journey/.test(p.url()), t + ': ArrowRight opens Record journey');
  this.check(!!(await tabTo((s) => s.id === 'dbx-obj', 40)), t + ': Tab reaches the start object');
  await p.keyboard.type('shop.orders (qa');
  await p.waitForSelector('#dbx-key-tenant_id', { timeout: 5000 });
  this.check(!!(await tabTo((s) => s.id === 'dbx-key-tenant_id', 10)), t + ': Tab reaches the first key field');
  await p.keyboard.type(KEY.tenant_id);
  await p.keyboard.press('Tab');
  await p.keyboard.type(KEY.id);
  this.check(!!(await tabTo((s) => s.tag === 'BUTTON' && s.text === 'Review trace plan', 6)), t + ': Tab reaches Review trace plan');
  await p.keyboard.press('Enter');
  await p.waitForSelector('#dbx-plan-h', { timeout: 10000 }).catch(() => {});
  this.check(await p.locator('#dbx-plan-h').count() === 1, t + ': Enter reviews the plan');
  const noRing = stops.filter((s) => !s.ring);
  this.check(!noRing.length, t + ': focus is visible on every stop (' + stops.length + ')' + (noRing.length ? ' — not on: ' + noRing.map((s) => s.text || s.tag).join(', ') : ''));
  this.same(before, t);
  await this.settle('8-keyboard', 'keyboard-explain-and-journey');
};

exports.run = async (browser, base, check, shots, infoPath) => {
  const info = readJson(infoPath);
  const w = new Walk(browser, base, check, shots, info);
  const order = ['discover', 'explain', 'trace', 'missing', 'impact', 'compare', 'revoke'];
  const quick = process.env.CG_UI_DB_QUICK;  // dev iteration only: one theme and width (e.g. "light:1440")
  for (const theme of quick ? [quick.split(':')[0]] : ['light', 'dark']) {
    for (const width of quick ? [+quick.split(':')[1]] : WIDTHS) {
      console.log('database walkthroughs · ' + theme + ' · ' + width + ' px');
      await w.open(theme, width);
      try {
        for (const name of order.filter((n) => !process.env.CG_UI_DB_WALKS || process.env.CG_UI_DB_WALKS.split(',').indexOf(n) >= 0)) {
          try { await w[name](); } catch (e) {
            check(false, w.tag(name) + ': ' + String(e.message || e).split('\n')[0] + snip(await w.page.locator('main').innerText().catch(() => '')));
          }
        }
      } finally { await w.close(); }
    }
  }
  console.log('database keyboard pass');
  await w.open('light', 1280);
  try { await w.keyboard(); } catch (e) { check(false, 'keyboard: ' + String(e.message || e).split('\n')[0]); } finally { await w.close(); }
  console.log('cached detail first views (1440 px): ' + w.timings.map((x) => x.join(' ') + ' ms').join(' · '));
  const c = w.counts();
  check(c.model === 0, 'no mcpjobs.run (model / provider) call in the whole database run (' + c.model + ')');
};
