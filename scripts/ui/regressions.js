// Focused browser regressions; also included in the full browser check.
const cases = {
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
