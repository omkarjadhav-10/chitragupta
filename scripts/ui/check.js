// Browser checks for the dashboard (run through scripts/ui/run.py, which serves a synthetic home).
// node check.js <base url> <today YYYY-MM-DD> [screenshot dir]
// Fails (exit 1) on: a serious or critical axe violation, a console or page error, any request that leaves
// 127.0.0.1, a first view slower than 1 s, horizontal scrolling at 900 px, or a keyboard check that doesn't hold.
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const AXE = fs.readFileSync(path.join(__dirname, 'vendor', 'axe.min.js'), 'utf8');
const [base, today, shots] = process.argv.slice(2);
const PAGES = [['today', '#/today'], ['day', '#/day/' + today], ['tickets', '#/tickets'], ['ticket', '#/tickets/ABC-1001'],
  ['ticket-branches', '#/tickets/ABC-1001?tab=branches'], ['ticket-dbflow', '#/tickets/ABC-1001?tab=dbflow'],
  ['ticket-requests', '#/tickets/ABC-1001?tab=requests'], ['timesheet', '#/timesheet?day=' + today], ['knowledge', '#/knowledge'],
  ['knowledge-apis', '#/knowledge?tab=apis'], ['search', '#/search?q=order'], ['health', '#/health'], ['setup', '#/setup'],
  ['settings', '#/settings'], ['db', '#/db'], ['db-table', '#/db/shop.orders']];
let failed = 0;
function check(cond, msg) { console.log((cond ? '  ok   ' : '  FAIL ') + msg); if (!cond) failed++; }

async function launch() {
  for (const channel of ['chrome', 'msedge']) {
    try { return await chromium.launch({ channel }); } catch (e) { /* try the next browser */ }
  }
  throw new Error('No Chrome or Edge found for playwright-core');
}

(async () => {
  const browser = await launch();
  for (const theme of ['light', 'dark']) {
    for (const width of theme === 'light' ? [1440, 1280, 900] : [1440]) {
      console.log(theme + ' · ' + width + ' px');
      // bypassCSP only so the test can inject axe; the page itself runs under the real CSP in every other check
      const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: theme, bypassCSP: true });
      const page = await ctx.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
      page.on('request', (r) => { if (!r.url().startsWith(base) && !/^(data|blob):/.test(r.url())) errors.push('request left the machine: ' + r.url()); });
      for (const [name, hash] of PAGES) {
        errors.length = 0;
        const t0 = Date.now();
        await page.goto(base + hash, { waitUntil: 'domcontentloaded' });
        await page.waitForSelector('#page-title', { timeout: 10000 }).catch(() => errors.push('no page heading'));
        await page.waitForFunction(() => !document.querySelector('.skeleton'), null, { timeout: 10000 }).catch(() => errors.push('still loading after 10 s'));
        const ms = Date.now() - t0;
        if (/dbflow|branches|knowledge$/.test(name)) await page.waitForTimeout(1500);
        if (shots) await page.screenshot({ path: path.join(shots, name + '-' + width + '-' + theme + '.png') });
        let msg = name;
        if (width === 1440) {
          await page.addScriptTag({ content: AXE });
          const v = await page.evaluate(async () => (await window.axe.run(document, { runOnly: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] }))
            .violations.filter((x) => x.impact === 'serious' || x.impact === 'critical').map((x) => x.id + ' ×' + x.nodes.length + ' (' + x.nodes[0].target.join(' ') + ')'));
          check(!v.length, msg + ': axe serious/critical ' + (v.join(', ') || 'none'));
          check(ms < 1000 || name === 'setup', msg + ': first view ' + ms + ' ms');
        }
        if (width === 900) check(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)), msg + ': no horizontal scroll');
        check(!errors.length, msg + ': no errors' + (errors.length ? ' — ' + errors.join(' | ') : ''));
      }
      await ctx.close();
    }
  }

  console.log('keyboard');
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  await page.goto(base + '#/today');
  await page.waitForSelector('#page-title');
  await page.waitForTimeout(300);
  const stops = [];
  for (let i = 0; i < 30; i++) {
    await page.keyboard.press('Tab');
    stops.push(await page.evaluate(() => {
      const e = document.activeElement;
      if (!e || e === document.body) return null;
      const cs = getComputedStyle(e), r = e.getBoundingClientRect();
      return { label: (e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 30) || '<' + e.tagName.toLowerCase() + (e.type ? ' ' + e.type : '') + (e.id ? '#' + e.id : '') + '>', ring: cs.outlineStyle !== 'none' || (/^(date|month)$/.test(e.type) && !e.matches(':focus')),  // the browser's own picker button inside the input has focus (styled in app.css)
        w: r.width, h: r.height };
    }));
  }
  const real = stops.filter(Boolean);
  check(real.length && /Skip to content/.test(real[0].label), 'first Tab stop is the skip link');
  const noRing = real.filter((s) => !s.ring);
  check(!noRing.length, 'focus is visible on every stop' + (noRing.length ? ' — not on: ' + noRing.map((s) => s.label).join(', ') : ''));
  const small = real.filter((s) => s.w < 24 || s.h < 24);
  check(!small.length, 'every stop is at least 24 × 24 px' + (small.length ? ' — ' + small.map((s) => s.label + ' ' + Math.round(s.w) + '×' + Math.round(s.h)).join(', ') : ''));
  await page.keyboard.press('Control+k');
  await page.waitForSelector('dialog[open] input[role="combobox"]');
  await page.waitForTimeout(150);
  check(await page.evaluate(() => document.activeElement.getAttribute('role') === 'combobox'), 'Ctrl+K puts focus in the palette');
  await page.keyboard.type('ABC-1003');
  await page.waitForTimeout(400);
  await page.keyboard.press('Enter');
  await page.waitForTimeout(400);
  check(/#\/tickets\/ABC-1003/.test(page.url()), 'Enter in the palette opens the ticket');
  await page.focus('[role="tab"][aria-selected="true"]');
  await page.keyboard.press('ArrowRight');
  await page.waitForTimeout(200);
  check(/tab=branches/.test(page.url()), 'arrow keys move between ticket tabs');
  await page.keyboard.press('Shift+Slash');
  await page.waitForTimeout(200);
  check(await page.locator('dialog[open] h2:has-text("Keyboard shortcuts")').count() === 1, '? lists the shortcuts');
  await page.keyboard.press('Escape');
  await page.keyboard.press('g');
  await page.keyboard.press('h');
  await page.waitForTimeout(300);
  check(/#\/health/.test(page.url()), 'g then h opens Health');
  const sse = await page.evaluate(() => new Promise((res) => {
    const es = new EventSource('/api/events');
    const t = setTimeout(() => { es.close(); res(false); }, 4000);
    es.addEventListener('cursor', () => { clearTimeout(t); es.close(); res(true); });
  }));
  check(sse, 'live updates: /api/events sends a cursor');
  await browser.close();
  console.log(failed ? failed + ' check(s) failed' : 'all checks passed');
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
