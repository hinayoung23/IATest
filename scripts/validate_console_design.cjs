/* Development-only acceptance against an owned, disposable console project.
 * Usage: IATEST_SMOKE_BROWSER=/path/to/chromium node scripts/validate_console_design.cjs URL OUTPUT_DIR
 * The data-plan check writes and runs one harmless plan in the selected project.
 */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const base = new URL(process.argv[2] || 'http://127.0.0.1:8874/?project=showcase');
  const output = path.resolve(process.argv[3] || '/private/tmp/iatest-console-design');
  assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname), 'Use an owned local console');
  fs.mkdirSync(output, {recursive: true});
  const project = base.searchParams.get('project') || 'showcase';
  const browser = await chromium.launch({headless: true, executablePath: process.env.IATEST_SMOKE_BROWSER});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, colorScheme: 'light'});
  context.setDefaultTimeout(15000); context.setDefaultNavigationTimeout(20000);
  const report = {passed: false, checks: [], errors: []};
  const page = await context.newPage();
  page.on('pageerror', error => report.errors.push(error.message));
  page.on('response', response => { if (new URL(response.url()).pathname.startsWith('/static/') && !response.ok()) report.errors.push('Asset ' + response.status() + ' ' + response.url()); });
  const url = (pathname = '/', locale = 'zh', extra = {}) => {
    const u = new URL(pathname, base);u.search = new URLSearchParams({project, lang: locale, ...extra});return u.href;
  };
  async function noOverflow() { assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Unexpected page overflow: ' + page.url()); }
  async function openPreferences() { if ((await page.locator('.site-preferences').getAttribute('open')) === null) await page.locator('.site-preferences > summary').click(); }
  try {
    await page.addInitScript(() => window.addEventListener('pagereveal', event => {window.__iatestViewTransition = Boolean(event.viewTransition);}));
    await page.goto(url());await page.waitForTimeout(1000);await noOverflow();
    assert.equal(await page.locator('.sidebar').count(), 0);
    await page.screenshot({path: path.join(output, 'overview-desktop.png')});
    const before = await page.locator('[data-scroll-stage]').evaluate(e => getComputedStyle(e).getPropertyValue('--stage-shift'));
    await page.mouse.wheel(0, 360);await page.waitForTimeout(950);
    const after = await page.locator('[data-scroll-stage]').evaluate(e => getComputedStyle(e).getPropertyValue('--stage-shift'));
    assert.notEqual(before, after);
    await page.locator('[data-chapter="1"]').scrollIntoViewIfNeeded();await page.waitForTimeout(950);
    assert.equal(await page.locator('[data-journey]').getAttribute('data-active-chapter'), '1');
    assert.equal(await page.locator('[data-journey]').evaluate(e => getComputedStyle(e).backgroundColor), 'rgb(20, 20, 22)');
    await page.screenshot({path: path.join(output, 'journey-desktop.png')});
    console.log('Verified phase');report.checks.push('native scroll effects and sticky chapter tracking');

    await page.locator('.site-nav a[href*="/assets"]').click();await page.waitForURL(u => u.pathname === '/assets');await page.waitForTimeout(500);
    assert.equal(await page.locator('.site-nav [aria-current=page]').getAttribute('href'), '/assets?project=' + project);
    assert.equal(await page.evaluate(() => window.__iatestViewTransition), true);
    await page.goBack();await page.waitForTimeout(500);assert.equal(new URL(page.url()).pathname, '/');
    console.log('Verified phase');report.checks.push('cross-document View Transition and browser history');

    await openPreferences();await page.locator('[data-language-select]').selectOption('fr');await page.waitForFunction(() => document.documentElement.dataset.locale === 'fr');
    assert.equal(await page.locator('html').getAttribute('lang'), 'fr');
    await openPreferences();await page.locator('#theme-toggle').click();assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await page.reload();await page.waitForTimeout(1000);assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await page.keyboard.press('Escape');await noOverflow();await page.screenshot({path: path.join(output, 'overview-dark-fr.png')});
    await openPreferences();await page.locator('#theme-toggle').click();
    console.log('Verified phase');report.checks.push('locale and theme persistence');

    for (const locale of ['zh', 'en', 'ja', 'fr', 'ru']) {
      await page.goto(url('/', locale));await page.waitForTimeout(100);
      await noOverflow();assert.equal(await page.locator('.site-nav a').count(), 7);
    }
    for (const width of [1024, 1280]) {
      await page.setViewportSize({width, height: 900});await page.goto(url('/', 'fr'));await noOverflow();
    }
    await page.setViewportSize({width: 390, height: 844});await page.goto(url());await page.waitForTimeout(950);await noOverflow();
    await page.screenshot({path: path.join(output, 'overview-mobile.png')});
    const menu = page.locator('.site-menu-toggle');await menu.click();assert.equal(await menu.getAttribute('aria-expanded'), 'true');
    await page.keyboard.press('Escape');assert.equal(await menu.getAttribute('aria-expanded'), 'false');
    assert.equal(await menu.evaluate(e => document.activeElement === e), true);
    await menu.click();await page.locator('.site-nav a[href*="/devices"]').click();await page.waitForURL(u => u.pathname === '/devices');await page.waitForTimeout(700);
    await noOverflow();await page.screenshot({path: path.join(output, 'mobile-workspace.png')});
    console.log('Verified phase');report.checks.push('five locales, narrow layout and keyboard-accessible navigation');

    await page.setViewportSize({width: 1440, height: 1000});await page.emulateMedia({reducedMotion: 'reduce'});await page.goto(url());
    assert.equal(await page.locator('html').evaluate(e => e.classList.contains('reveal-enabled')), false);
    assert.equal(await page.locator('[data-scroll-stage]').evaluate(e => getComputedStyle(e.querySelector('.stage-browser')).transform), 'none');
    await page.goto(url('/testing', 'zh', {tab: 'data'}));
    const id = 'design-check-' + Date.now();
    const form = page.locator('form[action="/actions/testing-build"]');
    await form.locator('[name=id]').fill(id);
    await form.locator('[name=csv]').fill('id,name\n1,A\n2,B');
    await form.locator('[name=rule]').selectOption('unique');
    await form.locator('[name=column]').fill('id');
    await form.getByRole('button', {name: '保存测试计划', exact: true}).click();await page.waitForURL(u => u.pathname === '/testing');
    await page.locator('.testing-plan').filter({has: page.getByRole('heading', {name: id, exact: true})}).getByRole('button', {name: '运行测试', exact: true}).click();
    const result = page.locator('section[data-run-id]').filter({has: page.getByRole('heading', {name: id, exact: true})}).first();
    await result.locator('[data-verdict=passed]').waitFor({timeout: 30000});
    await page.screenshot({path: path.join(output, 'plan-result.png')});
    console.log('Verified phase');report.checks.push('reduced motion, CSRF form save and real background data-plan readback');

    const noScript = await browser.newContext({javaScriptEnabled: false, viewport: {width: 390, height: 844}});
    const plain = await noScript.newPage();await plain.goto(url());
    assert.equal(await plain.locator('.site-nav a').count(), 7);
    assert.equal(await plain.locator('.site-nav a').first().isVisible(), true);
    assert.equal(await plain.locator('.overview-hero').isVisible(), true);
    await noScript.close();console.log('Verified phase');report.checks.push('navigation and content without JavaScript');
    assert.deepEqual(report.errors, []);report.passed = true;
  } catch (error) { report.error = error.stack; }
  finally { await browser.close();fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2));console.log(JSON.stringify(report)); }
  if (!report.passed) process.exitCode = 1;
})();
