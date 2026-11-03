/* Owned local preview only. Saves fixture profiles/plans; device detection is read-only. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const base = new URL(process.argv[2] || 'http://127.0.0.1:8874/?project=showcase');
  assert.ok(['localhost', '127.0.0.1', '[::1]'].includes(base.hostname), 'Use an owned local preview');
  const project = base.searchParams.get('project') || 'showcase';
  const output = path.resolve(process.argv[3] || '/private/tmp/iatest-console-workspace');
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true, executablePath: process.env.IATEST_SMOKE_BROWSER});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
  await context.addCookies([{name: 'iatest_lang', value: 'zh', url: base.origin}]);
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const report = {passed: false, checks: [], errors: []};
  page.on('pageerror', e => report.errors.push(e.message));
  const url = (p, extra = {}) => base.origin + p + '?' + new URLSearchParams({project, lang: 'zh', ...extra});
  const noOverflow = async () => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Overflow: ' + page.url());
  try {
    await page.goto(url('/testing'));
    assert.equal(await page.locator('.scenario-card').count(), 14);
    assert.equal(await page.locator('.site-nav a').count(), 7);
    assert.equal(await page.locator('textarea:visible').count(), 0);
    await page.screenshot({path: path.join(output, 'test-catalog.png'), fullPage: true});
    await page.getByRole('link', {name: '专项质量', exact: true}).click();
    assert.equal(await page.locator('.scenario-card').count(), 4);
    report.checks.push('scenario catalog and four task-oriented groups');

    await page.goto(url('/testing', {group: 'interaction'}));
    await page.locator('[data-goal-workspace]').waitFor();
    assert.equal(await page.locator('form[action="/actions/testing-explore"] [name=expected_result]').count(), 1);
    for (const platform of ['web', 'android', 'ios', 'desktop']) {
      await page.goto(url('/testing', {tab: 'exploration', platform}));
      assert.equal(await page.locator('[data-goal-workspace]').count(), 1);
      assert.equal(await page.locator('textarea[name=content]:visible').count(), 0);
      await noOverflow();
    }
    await page.goto(url('/testing', {tab: 'web'}));
    await page.locator('.goal-alternatives a[href*="method=manual"]').click();
    await page.locator('form[action="/actions/testing-build"]').waitFor();
    assert.equal(new URL(page.url()).searchParams.get('method'), 'manual');
    report.checks.push('AI goal and outcome are the default for all UI platforms; manual creation remains an explicit choice');

    await page.goto(url('/assets'));
    assert.equal(await page.locator('[data-asset-category]').count(), 7);
    await page.locator('[data-asset-category="ui"]').click();
    await page.locator('.asset-filters [name=q]').fill('no-such-asset-in-ui-check');
    await page.locator('.asset-filters button').click();
    await page.locator('.asset-inventory .empty-state').waitFor();
    await page.locator('.asset-filters a').click();
    await page.waitForTimeout(550);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({path: path.join(output, 'unified-assets.png'), fullPage: true});
    report.checks.push('equal asset categories, search, empty states and reset filters');

    for (const tab of ['data', 'protocol', 'desktop', 'accessibility', 'apps', 'nodes', 'remote', 'client_performance', 'campaigns']) {
      await page.goto(url('/testing', {tab}));
      assert.equal(await page.locator('textarea[name=content]:visible').count(), 0, tab + ' exposes raw plan JSON');
      await noOverflow();
    }
    report.checks.push('guided forms and collapsed full-contract editors across nine scenarios');

    await page.goto(url('/devices'));
    await page.locator('form[action="/actions/mobile-doctor"] button').click();
    await page.locator('.device-discovery').waitFor({timeout: 30000});
    report.devices = await page.locator('.device-discovery tbody tr').allTextContents();
    assert.ok(!await page.getByRole('heading', {name: '操作失败', exact: true}).count());
    const useDevice = page.locator('[data-use-device]').first();
    if (await useDevice.count()) {
      const id = await useDevice.getAttribute('data-use-device');
      await useDevice.click();
      assert.equal(await page.locator('form[action="/actions/mobile-profile"] [name=device_id]').inputValue(), id);
    }
    await page.screenshot({path: path.join(output, 'device-discovery.png'), fullPage: true});
    report.checks.push('live read-only discovery, actionable device list and profile prefill');

    const suffix = Date.now();
    const profile = 'ui-fixture-' + suffix;
    const profileForm = page.locator('form[action="/actions/mobile-profile"]');
    await profileForm.locator('[name=id]').fill(profile);
    await profileForm.locator('[name=platform]').selectOption('android');
    await profileForm.locator('[name=device_id]').fill('ui-fixture-no-device');
    await profileForm.locator('[name=app_id]').fill('dev.iatest.uifixture');
    await profileForm.getByRole('button', {name: '保存', exact: true}).click();
    await page.waitForURL(u => u.pathname === '/mobile');
    await page.goto(url('/mobile', {tab: 'plans', method: 'manual'}));
    const builder = page.locator('.mobile-plan-builder');
    await builder.locator('[name=profile]').selectOption(profile);
    await builder.locator('[name=id]').fill('ui-plan-' + suffix);
    await builder.locator('[name=op]').selectOption('text');
    await builder.locator('[name=identifier]').fill('nickname');
    await builder.locator('[name=value]').fill('测试昵称');
    await builder.locator('[data-add-mobile-step]').click();
    await builder.locator('[name=step1_identifier]').fill('save');
    await builder.locator('[data-add-mobile-step]').click();
    await builder.locator('[name=step2_identifier]').fill('unused');
    await builder.locator('[data-remove-mobile-step]').last().click();
    assert.equal(await builder.locator('[name=step_count]').inputValue(), '2');
    await builder.locator('[name=expected_identifier]').fill('status');
    await builder.locator('[name=expected_value]').fill('已保存');
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({path: path.join(output, 'mobile-plan-builder.png'), fullPage: true});
    await builder.getByRole('button', {name: '保存移动测试计划', exact: true}).click();
    await page.waitForURL(u => u.pathname === '/mobile' && u.searchParams.get('tab') === 'plans');
    const saved = page.locator('.testing-plan').filter({has: page.getByRole('heading', {name: 'ui-plan-' + suffix, exact: true})});
    assert.equal(await saved.locator('.mobile-steps li').count(), 3);
    assert.match(await saved.innerText(), /测试昵称/);
    assert.match(await saved.innerText(), /已保存/);
    report.checks.push('multi-action mobile plan compiled and saved with an independent outcome; no phone execution');

    await page.goto(url('/assets', {category: 'ui', asset: 'mobile-plan:ui-plan-' + suffix}));
    await page.locator('form[action="/actions/mobile-run"]').waitFor();
    assert.equal(await page.locator('form[action="/actions/testing-run"]').count(), 0);
    assert.equal(await page.locator('.mobile-steps li').count(), 3);
    report.checks.push('mobile plans are managed from their actual asset details with the correct run action');

    await page.goto(url('/testing', {tab: 'workflow'}));
    const workflow = page.locator('form[action="/actions/testing-build"]');
    await workflow.locator('[name=id]').fill('ui-workflow-' + suffix);
    await workflow.locator('[name=selected_plans][value="mobile:ui-plan-' + suffix + '"]').check();
    assert.equal(await workflow.locator('[name=cleanup]').getAttribute('required'), null);
    await workflow.getByRole('button', {name: '保存计划', exact: true}).click();
    await page.waitForURL(u => u.pathname === '/testing' && u.searchParams.get('tab') === 'workflow');
    await page.locator('.testing-plan').filter({has: page.getByRole('heading', {name: 'ui-workflow-' + suffix, exact: true})}).waitFor();

    await page.goto(url('/testing', {tab: 'client_performance'}));
    const measured = page.locator('form[action="/actions/testing-build"]');
    await measured.locator('[name=id]').fill('ui-measured-' + suffix);
    await measured.locator('[name=plan_id]').selectOption('mobile:ui-plan-' + suffix);
    await measured.locator('[name=metric]').selectOption('max_cpu_percent');
    await measured.locator('[name=threshold]').fill('80');
    await measured.getByRole('button', {name: '保存配置', exact: true}).click();
    await page.waitForURL(u => u.pathname === '/testing' && u.searchParams.get('tab') === 'client_performance');
    await page.getByRole('heading', {name: 'ui-measured-' + suffix, exact: true}).waitFor();
    report.checks.push('saved mobile plans selected in workflow and client-performance forms without JSON or phone execution');

    await page.setViewportSize({width: 390, height: 844});
    for (const [p, q] of [['/testing', {}], ['/mobile', {tab: 'plans'}], ['/devices', {}], ['/assets', {}], ['/results', {}]]) {
      await page.goto(url(p, q)); await noOverflow();
    }
    await page.screenshot({path: path.join(output, 'results-mobile.png'), fullPage: true});
    report.checks.push('390px layout across catalog, mobile builder, devices, assets and reports');
    assert.deepEqual(report.errors, []);
    report.passed = true;
  } catch (e) {report.error = e.stack;}
  finally {
    await browser.close();
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
  }
  if (!report.passed) process.exitCode = 1;
})();
