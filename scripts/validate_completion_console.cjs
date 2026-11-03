const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.IATEST_CHROMIUM_PATH});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const base = process.env.IATEST_SMOKE_URL, project = process.env.IATEST_CONSOLE_PROJECT;
  const url = (tab, lang = 'zh') => `${base}/testing?project=${encodeURIComponent(project)}&tab=${tab}&lang=${lang}`;
  try {
    await page.goto(url('protocol'));
    const compiler = page.locator('form[action="/actions/testing-proto-compile"]');
    await compiler.getByRole('button').click();
    await page.locator('details').filter({hasText: 'Protobuf protobuf-'}).first().waitFor();
    const descriptor = JSON.parse(await page.locator('details').filter({hasText: 'Protobuf protobuf-'}).first().locator('textarea').inputValue());
    assert.match(descriptor.sha256, /^[a-f0-9]{64}$/);
    await page.locator('form[action="/actions/testing-proto-compile"] button').click();
    assert.equal(await page.locator('details').filter({hasText: 'Protobuf protobuf-'}).count(), 1);
    for (const plan of JSON.parse(process.env.IATEST_CONSOLE_PLANS)) {
      if (['ios', 'android'].includes(plan.kind)) {
        const mobile = tab => `${base}/mobile?project=${encodeURIComponent(project)}&tab=${tab}&lang=zh`;
        await page.goto(mobile('runs'));
        const oldRuns = await page.locator('[data-mobile-run-id]').evaluateAll(rows => rows.map(row => row.dataset.mobileRunId));
        await page.goto(mobile('plans'));
        const form = page.locator('form[action="/actions/mobile-plan"]').last();
        await form.locator('xpath=ancestor::details[1]').locator('summary').first().click();
        await form.locator('[name=content]').fill(JSON.stringify(plan));
        await form.getByRole('button').click();
        await page.locator('form[action="/actions/mobile-run"]').filter({has: page.locator(`input[name=plan_id][value="${plan.id}"]`)}).getByRole('button').click();
        await page.waitForFunction(({id, oldRuns}) => [...document.querySelectorAll('[data-mobile-run-id]')].some(e => e.textContent.includes(id) && !oldRuns.includes(e.dataset.mobileRunId)), {id: plan.id, oldRuns}, {timeout: 240000});
        const latest = page.locator('[data-mobile-run-id]').filter({hasText: plan.id}).first();
        assert.equal(await latest.locator('[data-verdict]').getAttribute('data-verdict'), 'passed', await latest.innerText());
        continue;
      }
      await page.goto(url('runs'));
      const oldRuns = await page.locator('[data-run-id]').evaluateAll(rows => rows.map(row => row.dataset.runId));
      await page.goto(url(plan.kind));
      const form = page.locator('form[action="/actions/testing-save"]').last();
      const disclosure = form.locator('xpath=ancestor::details[1]');
      if (await disclosure.count()) await disclosure.locator('summary').first().click();
      await form.locator('[name=content]').fill(JSON.stringify(plan));
      await form.getByRole('button').click();
      const section = page.locator('article.testing-plan').filter({has: page.getByRole('heading', {name: plan.title || plan.id, exact: true})}).first();
      await section.locator('form[action="/actions/testing-run"] button').click();
      await page.waitForFunction(({id, oldRuns}) => [...document.querySelectorAll('[data-run-id]')].some(e => e.textContent.includes(id) && !oldRuns.includes(e.dataset.runId)), {id: plan.id, oldRuns}, {timeout: 240000});
      const latest = page.locator('[data-run-id]').filter({hasText: plan.id}).first();
      assert.equal(await latest.locator('[data-verdict]').getAttribute('data-verdict'), 'passed', await latest.innerText());
    }
    await page.setViewportSize({width: 390, height: 844});
    for (const lang of ['zh', 'en', 'ja', 'fr', 'ru']) for (const tab of ['protocol', 'data', 'desktop', 'apps', 'client_performance', 'exploration']) {
      await page.goto(url(tab, lang));
      assert.equal(await page.locator('html').getAttribute('lang'), lang === 'zh' ? 'zh-CN' : lang);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth) <= 2, `${lang}/${tab} overflow`);
    }
    await page.screenshot({path: process.env.IATEST_CONSOLE_REPORT + '.png', fullPage: true});
    assert.deepEqual(errors, []);
    fs.writeFileSync(process.env.IATEST_CONSOLE_REPORT, JSON.stringify({passed: true, page_errors: errors, locales: 5, viewport_width: 390, compiled_proto: true, immutable_compile: true}, null, 2));
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
