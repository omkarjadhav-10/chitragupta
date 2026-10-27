// Focused browser regressions; also included in the full browser check.
const cases = {
  async summary(browser, base, check) {
    const ctx = await browser.newContext();
    try {
      const page = await ctx.newPage();
      await page.route('**/api/tickets/ABC-1001', async route => {
        const response = await route.fetch(), ticket = await response.json();
        const verified = ticket.brief.summary.sections.find(s => s.id === 'verified');
        verified.claims = [{ text: 'Synthetic test passed.', cites: ['test:proof'] }];
        verified.empty = false; verified.text = null;
        ticket.brief.evidence['test:proof'] = { kind: 'test', title: 'Synthetic proof passed' };
        await route.fulfill({ json: ticket });
      });
      await page.goto(base + '#/tickets/ABC-1001');
      for (const id of ['problem', 'root_cause', 'changed', 'verified', 'unknowns', 'next_action']) {
        await page.locator('#s-' + id).waitFor();
        check(await page.locator('#s-' + id).isVisible(), 'summary question is visible: ' + id);
      }
      check(await page.locator('.full-brief').getAttribute('open') === null, 'Full brief starts collapsed');
      await page.locator('#s-verified').locator('..').locator('a.cite').click();
      await page.locator('#e-test-proof').waitFor({ state: 'visible' });
      check(await page.getByText('Synthetic proof passed', { exact: true }).isVisible(), 'verification cite opens its actual proof');
      await page.locator('.full-brief > summary').click();
      check(await page.locator('#b-root_cause').isVisible(), 'one click opens the preserved full brief');
    } finally { await ctx.close(); }
  },
  async relevance(browser, base, check) {
    const ctx = await browser.newContext();
    try {
      const page = await ctx.newPage(), loads = [];
      const core = { id: 'core', text: 'select state from shop.orders', purpose: 'Synthetic core query', revealed: 'Reserved', sent: true, relevance: 'core', role: 'cause' };
      const context = { ...core, id: 'context', purpose: 'Synthetic context query', role: 'context' };
      const noise = { id: 'noise', text: 'select * from information_schema.tables', purpose: 'Synthetic catalog query', sent: true, relevance: 'noise', hidden_reason: 'catalog' };
      const review = { id: 'review', text: 'select id from shop.sessions', purpose: 'Synthetic reviewed query', sent: true, relevance: 'supporting', review: { why: 'wrong ticket' } };
      await page.route('**/api/tickets/ABC-1001*', async route => {
        const url = new URL(route.request().url()), section = url.searchParams.get('section');
        if (section) loads.push(section);
        if (section === 'notes') return route.fulfill({ json: { key: 'ABC-1001', section, readme: 'Synthetic lazy owner note' } });
        if (section === 'queries') return route.fulfill({ json: { key: 'ABC-1001', section, queries: [context, noise, core, review] } });
        const response = await route.fetch(), ticket = await response.json();
        if (!section) Object.assign(ticket, { focused: true, lazy: true, queries: [core, context], core_ranked: { queries: true }, readme: '',
          core: { tables: [{ id: 'shop.orders', name: 'shop.orders' }], apis: [] }, record_counts: { queries: 4, items: ticket.findings.length, tables: 1, apis: 0, files: 0 } });
        await route.fulfill({ json: ticket });
      });
      await page.goto(base + '#/tickets/ABC-1001');
      await page.getByRole('heading', { name: 'Recorded tables (not yet ranked)', exact: true }).waitFor();
      check(loads.length === 0, 'focused Overview does not fetch heavy evidence or hub text');
      await page.getByRole('tab', { name: /^Queries/ }).click();
      await page.locator('#q-core').waitFor();
      check(await page.getByRole('heading', { name: 'Also used', exact: true }).isVisible(), 'context queries have their own default section');
      check(await page.locator('#q-context').isVisible(), 'context pick stays uncollapsed');
      check(await page.locator('#q-review').count() === 0 && await page.locator('#q-noise').count() === 0, 'noise and unresolved reviews stay out of the default query list');
      await page.getByText('All recorded (4)', { exact: true }).click();
      await page.locator('#q-review').waitFor();
      check((await page.locator('[id^=q-]').evaluateAll(es => es.map(e => e.id))).slice(0, 2).join(',') === 'q-core,q-context', 'loading all evidence preserves key then context order');
      check(loads.includes('queries') && await page.locator('#q-noise').isVisible(), 'All recorded fetches and exposes retained query evidence');
      await page.getByRole('tab', { name: /^Notes/ }).click();
      await page.getByText('Synthetic lazy owner note', { exact: true }).waitFor();
      check(loads.includes('notes'), 'hub text is fetched when My notes opens');
    } finally { await ctx.close(); }
  },
  async reviews(browser, base, check) {
    const ctx = await browser.newContext();
    try {
      const page = await ctx.newPage();
      await page.route('**/api/tickets/ABC-1001', async route => {
        const response = await route.fetch(), ticket = await response.json();
        ticket.record_reviews = [{ id: 'GET /sessions', kind: 'api', title: 'Synthetic session lookup', detail: '/sessions',
          review: { why: 'names ABC-1002', suggest: 'ABC-1002' } }];
        await route.fulfill({ json: ticket });
      });
      await page.goto(base + '#/tickets/ABC-1001');
      await page.locator('#all-findings > summary').click();
      const review = page.locator('details').filter({ has: page.locator('summary', { hasText: /^Needs review/ }) }).last();
      await review.locator('summary').first().click();
      const text = await review.innerText();
      check(text.includes('ABC-1001: keep GET /sessions'), 'record review includes the complete copyable keep key');
      check(text.includes('ABC-1001: move GET /sessions ABC-1002'), 'record review includes additive move command');
    } finally { await ctx.close(); }
  },
  async dayline(browser, base, check) {
    const ctx = await browser.newContext();
    try {
      const page = await ctx.newPage();
      await page.route('**/api/today', async route => {
        const response = await route.fetch(), day = await response.json();
        day.lanes = [{ ticket: 'ABC-1001', minutes: 60, spans: [['09:00', '10:00']] },
                     { ticket: 'ABC-1002', minutes: 60, spans: [['09:00', '10:00']] }];
        day.worklog_saved = null;
        day.worklog_preview = { ...day.worklog_preview, entries: [{ ticket: 'ABC-1001', start: '', minutes: 60, duration: '1h' }] };
        await route.fulfill({ json: day });
      });
      await page.goto(base + '#/today');
      await page.locator('.dayline-span').first().waitFor();
      const boxes = await page.locator('.dayline-span').evaluateAll(elements => elements.map(e => { const r = e.getBoundingClientRect(); return { top: r.top, bottom: r.bottom }; }));
      check(boxes.length === 2 && (boxes[0].bottom <= boxes[1].top || boxes[1].bottom <= boxes[0].top), 'parallel ticket stretches occupy separate Day Line tracks');
      check(await page.locator('.jira-span').count() === 0, 'Jira entries without a start are not placed at an invented time');
      check((await page.locator('.dayline').innerText()).includes('no recorded start'), 'unplaced Jira entries have an explanation');
    } finally { await ctx.close(); }
  },
  async media(browser, base, check) {
    const ctx = await browser.newContext();
    try {
      await ctx.addInitScript(() => {
        Object.defineProperty(MediaQueryList.prototype, 'addEventListener', { value: undefined });
        try { localStorage.setItem('cg-density', 'compact'); localStorage.setItem('cg-motion', 'reduce'); } catch (_) {}
      });
      const page = await ctx.newPage(), errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.goto(base + '#/today');
      await page.locator('#page-title').waitFor();
      check(!errors.length, 'legacy media queries do not stop appearance setup');
      check(await page.evaluate(() => document.documentElement.dataset.density === 'compact' && document.documentElement.dataset.motion === 'reduce'), 'legacy media queries preserve density and motion choices');
    } finally { await ctx.close(); }
  },
  async accessibility(browser, base, check) {
    const ctx = await browser.newContext();
    try {
      const page = await ctx.newPage();
      await page.goto(base + '#/tickets/ABC-1001');
      await page.locator('#page-title').waitFor();
      const target = await page.locator('.more-pages').getAttribute('aria-controls');
      check(!target || await page.locator('[id="' + target + '"]').count() === 1, 'closed More menu has no dangling controls target');
      await page.locator('#all-findings > summary').click();
      check(await page.getByRole('complementary', { name: 'Recorded evidence' }).count() === 0, 'finding evidence does not create complementary landmarks');
      await page.keyboard.press('j');
      check(await page.evaluate(() => document.activeElement.matches('.rlist a') && document.activeElement.getClientRects().length > 0), 'j focuses a visible ticket link');
      await page.keyboard.press('k');
      check(await page.evaluate(() => document.activeElement.matches('.rlist a') && document.activeElement.getClientRects().length > 0), 'k keeps focus on visible ticket links');
    } finally { await ctx.close(); }
  },
  async print(browser, base, check) {
    const ctx = await browser.newContext({ colorScheme: 'dark' });
    try {
      const page = await ctx.newPage();
      await page.goto(base + '?theme=dark#/tickets/ABC-1001');
      await page.locator('#page-title').waitFor();
      await page.emulateMedia({ media: 'print' });
      check(await page.evaluate(() => getComputedStyle(document.body).color) === 'rgb(21, 25, 46)', 'dark ticket prints with light-theme ink');
      check(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme) === 'light', 'printing forces light color scheme');
      await page.evaluate(() => document.documentElement.removeAttribute('data-theme'));
      check(await page.evaluate(() => getComputedStyle(document.body).color) === 'rgb(21, 25, 46)', 'printing without an appearance attribute uses light ink');
      const response = await page.request.get(base + 'api/tickets/ABC-1001/export?mode=full');
      const exported = await response.text();
      await page.goto('about:blank');
      await page.setContent(exported, { waitUntil: 'domcontentloaded' });
      await page.locator('#page-title').waitFor();
      check(await page.evaluate(() => getComputedStyle(document.body).color) === 'rgb(21, 25, 46)', 'standalone export prints dark text');
    } finally { await ctx.close(); }
  },
  async alerts(browser, base, check) {
    const ctx = await browser.newContext();
    try {
      const page = await ctx.newPage();
      await page.route('**/api/tickets/ABC-1001', async route => {
        const response = await route.fetch();
        const ticket = await response.json();
        ticket.git = { ...ticket.git, alerts: [{ kind: 'missing', text: 'Synthetic change has not reached QA.' }] };
        await route.fulfill({ json: ticket });
      });
      await page.goto(base + '#/tickets/ABC-1001');
      await page.locator('#git-alerts').first().waitFor();
      check(await page.locator('#git-alerts').count() === 1, 'delivery alerts render exactly once on Overview');
      check(await page.locator('#git-alerts[role="alert"]').count() === 1, 'one delivery alert region is announced');
    } finally { await ctx.close(); }
  }
};
exports.run = async (browser, base, check, focus) => {
  for (const [name, run] of Object.entries(cases)) if (!focus || focus === name) await run(browser, base, check);
};
