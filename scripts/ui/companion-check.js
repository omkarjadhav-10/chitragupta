// Companion walkthrough on a synthetic home (scripts/ui/companion.py). Never submits a prompt or runs implementation:
// Generate / Improve run through the backend with a stub caller; the handoff is only copied ("Prepared; not
// submitted"). Every width and theme: planner stages, task views, no root overflow, axe serious/critical, no console
// errors, no request leaving 127.0.0.1. Light 1280: the full action walkthrough. Plus a keyboard pass.
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');
const [base, shots] = process.argv.slice(2);
let ok = 0, fail = 0;
function check(cond, what) { if (cond) { ok++; console.log('ok   ' + what); } else { fail++; console.log('FAIL ' + what); } }

async function overflow(page) { return page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1); }
async function axe(page) {
  await page.addScriptTag({ content: fs.readFileSync(path.join(__dirname, 'vendor', 'axe.min.js'), 'utf8') });
  return page.evaluate(async () => (await axe.run()).violations.filter((v) => ['serious', 'critical'].includes(v.impact)).map((v) => v.id + ' (' + v.nodes.length + ')'));
}
const status = (page) => page.locator('p.cmp-status');
async function waitStatus(page, re, ms = 15000) { await page.waitForFunction((src) => new RegExp(src).test(document.querySelector('p.cmp-status').textContent), re.source, { timeout: ms }); }
async function pickTask(page, key) {
  await page.locator('.cmp-pick', { hasText: key }).first().click();
  await page.locator('#cmp-task-h, #cmp-new-h').first().waitFor({ timeout: 15000 });
}
async function openTab(page, name) { await page.getByRole('tab', { name, exact: true }).click(); }

async function main() {
  let browser;
  for (const channel of ['chrome', 'msedge']) {
    try { browser = await chromium.launch({ channel }); break; } catch (_) { /* try the next installed browser */ }
  }
  if (!browser) throw new Error('No installed Chrome or Edge');
  fs.mkdirSync(shots, { recursive: true });
  const external = [];
  try {
    for (const theme of ['light', 'dark']) for (const width of [390, 768, 900, 1280, 1440]) {
      // bypassCSP only so the vendored axe script can be injected; the general check.js runs under the real CSP
      const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: theme, bypassCSP: true });
      await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base.replace(/\/$/, '') });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
      page.on('request', (r) => { if (!r.url().startsWith(base) && !/^(data|blob):/.test(r.url())) external.push(r.url()); });
      const tag = theme + ' ' + width;
      await page.goto(base + '#/companion');
      await page.locator('#cmp-planner-h').waitFor();
      await page.evaluate((t) => document.documentElement.setAttribute('data-theme', t), theme);
      const text = await page.locator('.cmp-planner').innerText();
      check(['New', 'Planned', 'In development', 'Paused', 'Testing', 'PR review'].every((g) => text.includes(g)), tag + ': planner shows every open stage group');
      check(/Show completed \(1\)/.test(text), tag + ': completed tasks are collapsed');
      check((await page.locator('.cmp-planner').innerText()).includes('Why:'), tag + ': each card says why it is suggested');
      check(await page.locator('.cmp-att-item').count() === 3 && await page.getByRole('button', { name: 'Show all (4)', exact: true }).count() === 1,
        tag + ': attention shows at most three items initially');
      await pickTask(page, 'ABC-1001');
      check(await page.getByRole('tab', { name: 'Jev Review' }).count() === 1, tag + ': task views open');
      await openTab(page, 'Jev Review');
      await page.locator('#cmp-ch-h').waitFor();
      const review = await page.locator('.cmp-review').innerText();
      check(review.includes('Evaluation unavailable'), tag + ': honest Evaluation unavailable state');
      check(await page.getByRole('button', { name: 'Improve Plan', exact: true }).count() === 1 && await page.getByRole('button', { name: 'Execute Plan', exact: true }).count() === 1, tag + ': Improve Plan and Execute Plan both visible');
      check(await page.locator('.cmp-choice.rec').count() === 1, tag + ': exactly one recommendation');
      check(!(await overflow(page)), tag + ': no root horizontal overflow');
      const v = await axe(page);
      check(v.length === 0, tag + ': no serious/critical axe violations ' + v.join(', '));
      await page.screenshot({ path: path.join(shots, 'companion-' + theme + '-' + width + '.png'), fullPage: true });
      if (theme === 'light' && width === 1280) await walkthrough(page, tag);
      if (theme === 'dark' && width === 390) await keyboard(page, tag);
      check(errors.length === 0, tag + ': no console errors ' + errors.slice(0, 3).join('; '));
      await context.close();
    }
  } finally { await browser.close(); }
  check(external.length === 0, 'zero off-machine requests ' + external.slice(0, 3).join(', '));
  console.log('\n' + ok + ' ok, ' + fail + ' failed');
  if (fail) process.exitCode = 1;
}

async function walkthrough(page, tag) {
  const m = page.locator('.cmp-main');
  const shot = (n) => page.screenshot({ path: path.join(shots, 'walk-' + n + '.png'), fullPage: true });
  // 1. new task: proposed baseline from cached Jira -> confirm
  await pickTask(page, 'ABC-1003');
  await page.locator('#cmp-new-h').waitFor();
  const prop = await page.locator('.cmp-main').innerText();
  check(/Proposed criteria/.test(prop) && /A badge shows the active filter/.test(prop), tag + ': new task shows the proposed Jira baseline');
  await shot('1-new-task');
  await m.getByRole('button', { name: 'Confirm baseline', exact: true }).click();
  await page.locator('#cmp-task-h').waitFor({ timeout: 15000 });
  check((await page.locator('#cmp-task-h').innerText()).includes('ABC-1003'), tag + ': confirming creates the task workflow');
  // 2. generate (stub runner through the real job code) and poll local job state
  await pickTask(page, 'ABC-1002');
  await openTab(page, 'Plan');
  await m.getByRole('button', { name: 'Generate Plan', exact: true }).click();
  await waitStatus(page, /Queued|Already running/);
  check(true, tag + ': Generate Plan queued a job');
  await shot('2-generate-queued');
  await page.waitForTimeout(2500);
  await page.getByRole('button', { name: 'Reload cached view', exact: true }).click();
  await page.locator('#cmp-vers-h').waitFor({ timeout: 15000 });
  check((await page.locator('.cmp-versions').innerText()).includes('Claude suggestion'), tag + ': generated plan saved as a version');
  // 3. manual edit -> new immutable version, survives reload
  await pickTask(page, 'ABC-1001');
  await openTab(page, 'Plan');
  await m.getByRole('button', { name: 'Edit a new version', exact: true }).click();
  await m.getByLabel('Plan text', { exact: true }).fill('Manually reviewed plan: preserve this exact text.');
  await m.getByRole('button', { name: 'Save new plan version', exact: true }).click();
  await waitStatus(page, /Saved/);
  await page.reload();
  await page.locator('#cmp-task-h').waitFor({ timeout: 15000 });
  await openTab(page, 'Plan');
  await m.getByRole('button', { name: /View version 2/ }).click();
  check((await page.locator('pre[aria-label="Plan text"]').innerText()).includes('preserve this exact text'), tag + ': manual version kept exactly after reload');
  await shot('3-manual-version');
  // 4. improve once with the stub, then select the candidate explicitly
  await m.getByRole('button', { name: 'Improve Plan with Claude', exact: true }).click();
  await waitStatus(page, /Queued|Already running/);
  await page.waitForTimeout(2500);
  await page.getByRole('button', { name: 'Reload cached view', exact: true }).click();
  await m.getByRole('button', { name: 'Select candidate', exact: true }).first().waitFor({ timeout: 15000 });
  check(true, tag + ': improvement is a candidate, not auto-selected');
  await m.getByRole('button', { name: 'Select candidate', exact: true }).first().click();
  await waitStatus(page, /Saved/);
  // 5. Jev unavailable + developer assessment, then Execute with acknowledgement -> copy, Prepared; not submitted
  await openTab(page, 'Jev Review');
  await m.getByRole('button', { name: 'Request evaluation', exact: true }).click();
  await waitStatus(page, /Saved/);
  check((await page.locator('.cmp-review').innerText()).includes('Evaluation unavailable'), tag + ': requested evaluation stays unavailable (no false score)');
  await m.getByText('Classify the criteria yourself').click();
  await m.getByLabel(/^ac1:/).selectOption('Covered');
  await m.getByRole('button', { name: 'Record assessment', exact: true }).click();
  await waitStatus(page, /Saved/);
  check((await page.locator('.cmp-review').innerText()).includes('Developer assessment'), tag + ': developer assessment labelled separately');
  const details = await page.locator('.cmp-choices').innerText();
  check(/Digest/.test(details) && /Target/.test(details) && /Unresolved risks/.test(details), tag + ': exact version, digest, target and risks shown before Execute');
  await page.locator('.cmp-choices input[type=checkbox]').check();
  await m.getByRole('button', { name: 'Execute Plan', exact: true }).click();
  await m.getByRole('button', { name: 'Copy approved plan', exact: true }).waitFor({ timeout: 15000 });
  await m.getByRole('button', { name: 'Copy approved plan', exact: true }).click();
  await waitStatus(page, /Prepared; not submitted/);
  check(true, tag + ': copy says Prepared; not submitted');
  await shot('5-handoff');
  // 6. pause (capture keeps running) and resume execution
  await pickTask(page, 'ABC-1004');
  await m.getByRole('button', { name: 'Pause task (capture keeps running)', exact: true }).click();
  await waitStatus(page, /Saved/);
  check((await page.locator('.cmp-task > .sec-head .sub').innerText()).includes('Paused'), tag + ': task paused');
  await m.getByText('I reviewed the continuation').click();
  await m.getByRole('button', { name: 'Resume Execution', exact: true }).click();
  await waitStatus(page, /Saved/);
  check((await page.locator('.cmp-task > .sec-head .sub').innerText()).includes('Execution ready'), tag + ': Resume Execution prepared a continuation');
  await shot('6-resume');
  // 7. verify requirements: ledger with three separate measures
  await pickTask(page, 'ABC-1006');
  await openTab(page, 'Verification');
  await m.getByRole('button', { name: 'Verify Requirements', exact: true }).click();
  await waitStatus(page, /Saved/);
  const ver = await page.locator('.cmp-verify').innerText();
  check(/Plan coverage/.test(ver) && /Implementation progress/.test(ver) && /Functional verification/.test(ver), tag + ': three separate measures');
  check(/no current applicable evidence|earlier evidence/.test(ver), tag + ': applicability stated in words');
  await shot('7-verify');
  // 8. drafts: generate (deterministic, no model), edit -> new version, regenerate keeps the edit (diff), copy
  await openTab(page, 'Handoff');
  await page.locator('#cmp-dr-h').waitFor({ timeout: 15000 });
  await m.getByLabel('Audience', { exact: true }).selectOption('qa');
  check((await page.locator('.cmp-drafts').innerText()).includes('Preview (not saved)'), tag + ': a draft starts as an unsaved preview');
  await m.getByRole('button', { name: 'Generate draft', exact: true }).click();
  await waitStatus(page, /Saved/);
  check(/Version 1 · Generated/.test(await page.locator('.cmp-drafts').innerText()), tag + ': Generate draft saved version 1');
  const drafted = await page.locator('pre[aria-label="Draft text"]').innerText();
  check(drafted.includes('## Missing information') && !/all tests pass|deployed|accepted/i.test(drafted), tag + ': draft has a missing-information section and no unevidenced claims');
  await m.getByRole('button', { name: 'Edit draft', exact: true }).click();
  const box = m.getByLabel('Draft text', { exact: true });
  await box.fill((await box.inputValue()) + '\nQA note (mine): check the rounding of shipping estimates.');
  await m.getByRole('button', { name: 'Save new version', exact: true }).click();
  await waitStatus(page, /Saved/);
  check(/Version 2 · Your edit/.test(await page.locator('.cmp-drafts').innerText()), tag + ': your edit is saved as its own version');
  await m.getByRole('button', { name: 'Generate draft', exact: true }).click();
  await waitStatus(page, /Your edit stays current/);
  check((await page.locator('pre[aria-label="Draft text"]').innerText()).includes('QA note (mine)'), tag + ': regenerating never overwrites your edit');
  check(await page.locator('pre[aria-label="Draft diff"]').count() === 1, tag + ': the regenerated version is shown beside the edit as a diff');
  await m.getByRole('button', { name: 'Copy draft', exact: true }).click();
  await waitStatus(page, /copied\. Nothing was sent/);
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  check(clip.includes('QA note (mine)') && !/@|[A-Za-z]:\\/.test(clip), tag + ': copy re-reads the current draft, redacted');
  await shot('8-drafts');
  // 9. attention: snooze a required delivery reminder (it stays in the full list), then show it again
  const att = page.locator('.cmp-att');
  await att.locator('li', { hasText: 'View UAT evidence' }).first().getByRole('combobox').selectOption('1d');
  await waitStatus(page, /Snoozed for 1 day/);
  check(/1 hidden \(0 acknowledged, 1 snoozed\)/.test(await att.innerText()), tag + ': a snooze is counted as hidden');
  await att.getByRole('button', { name: 'Show all (4)', exact: true }).click();
  check(/Snoozed until/.test(await att.innerText()), tag + ': a snoozed required item stays in the full list');
  await shot('9-attention');
  await att.locator('li', { hasText: 'View UAT evidence' }).first().getByRole('button', { name: 'Show again', exact: true }).click();
  await waitStatus(page, /Shown again/);
  check(!/hidden \(/.test(await att.innerText()), tag + ': Show again restores it');
  check(!(await overflow(page)), tag + ': no overflow after the walkthrough');
}

async function keyboard(page, tag) {
  await page.goto(base + '#/companion');
  await page.locator('.cmp-pick').first().waitFor();
  await page.locator('.cmp-pick').first().focus();
  await page.keyboard.press('Enter');
  await page.locator('#cmp-task-h, #cmp-new-h').first().waitFor({ timeout: 15000 });
  const tabs = page.getByRole('tab');
  if (await tabs.count()) {
    await tabs.first().focus();
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(200);  // Tabs moves focus on the next tick
    const sel =await page.evaluate(() => document.activeElement && document.activeElement.getAttribute('aria-selected'));
    check(sel === 'true', tag + ': arrow keys move between task views');
  }
  for (let i = 0; i < 8; i++) await page.keyboard.press('Tab');
  const visible = await page.evaluate(() => { const el = document.activeElement; if (!el || el === document.body) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  check(visible, tag + ': keyboard focus stays on a visible control');
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
