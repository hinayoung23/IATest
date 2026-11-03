const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.IATEST_SMOKE_BROWSER});
  const page = await browser.newPage({viewport:{width:1360,height:960}});
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  const url=(tab,lang='zh')=>`${process.env.IATEST_SMOKE_URL}/testing?project=${process.env.IATEST_PERF_PROJECT}&lang=${lang}&tab=${tab}`;
  const report={physical_device:true,synthetic_device:false,project:process.env.IATEST_PERF_PROJECT,page_errors:errors,checks:{}};
  async function submit(form) {
    await Promise.all([page.waitForNavigation({waitUntil:'domcontentloaded'}),form.locator('button').click()]);
  }
  async function awaitElement(selector, tab, timeout=240000) {
    const deadline=Date.now()+timeout;
    while(Date.now()<deadline) {
      await page.goto(url(tab));
      if(await page.locator(selector).count()) return page.locator(selector).first();
      await page.waitForTimeout(2000);
    }
    throw new Error('Console did not produce expected evidence: '+selector);
  }
  async function runPlan(id, verdict, timeout=240000) {
    const form=page.locator('form[action="/actions/testing-run"]').filter({has:page.locator(`[name=plan_id][value="${id}"]`)});
    await submit(form);
    const parent=await awaitElement(`section[data-plan-id="${id}"]`, 'runs', timeout);
    assert.equal(await parent.locator('[data-verdict]').getAttribute('data-verdict'),verdict,await parent.innerText());
  }
  try {
    assert.equal((await page.goto(url('client_performance'))).status(),200);
    assert.ok((await page.locator('body').innerText()).includes('iOS 启动并采集示例'));
    if(process.env.IATEST_PERF_FRAMES==='1') {
      const isolated=process.env.IATEST_PERF_ISOLATED==='1';
      const editor=page.locator(isolated?'[data-ios-isolated-recording] form':'[data-ios-frame-recording] form');
      await editor.locator('textarea').fill(process.env.IATEST_PERF_SPEC);
      await submit(editor);
      await runPlan('ios-console-frame-journey','passed');
      report.checks.actual_frame_recording_ui_and_gate=true;
      if(isolated) report.checks.explicit_isolated_attach_form=true;
      const measurement=await awaitElement('section[data-ios-measurement]', 'client_performance', 30000);
      report.frame_measurement_id=await measurement.getAttribute('data-ios-measurement');
      for(const [id,threshold,verdict] of [['ios-console-frame-gate',5000,'passed'],['ios-console-frame-reject',0,'failed']]) {
        const asset=await awaitElement(`section[data-ios-measurement="${report.frame_measurement_id}"]`, 'client_performance', 30000);
        const gate=asset.locator('form[action="/actions/testing-ios-gate-create"]');
        await gate.locator('[name=plan_id]').fill(id);
        await gate.locator('[name=thresholds]').fill(JSON.stringify({max_frame_lifetime_ms:threshold}));
        await submit(gate);
        await runPlan(id,verdict,30000);
      }
      report.checks.frozen_frame_gate_passed=true;
      report.checks.zero_frame_threshold_rejected=true;
    } else {
    const reused=process.env.IATEST_PERF_CALIBRATION;
    let calibrationId=reused;
    if(!reused) {
      const calibrate=page.locator('form[action="/actions/testing-ios-calibrate"]');
      await calibrate.locator('textarea').fill(process.env.IATEST_PERF_SPEC);
      await submit(calibrate);
      const calibration=await awaitElement('section[data-ios-calibration]', 'client_performance');
      calibrationId=await calibration.getAttribute('data-ios-calibration');
      assert.ok((await calibration.locator('h2').innerText()).includes('usable'));
      report.checks.real_positive_negative_calibration=true;
      await submit(calibration.locator('form[action="/actions/testing-ios-measure"]'));
    } else {
      report.reused_real_calibration_id=reused;
      const editor=page.locator('form[action="/actions/testing-ios-measure"]').nth(1);
      await editor.locator('textarea').fill(JSON.stringify({...JSON.parse(process.env.IATEST_PERF_SPEC),calibration_id:reused}));
      await submit(editor);
      report.checks.explicit_historical_control_reused=true;
    }
    const asset=await awaitElement(`section[data-ios-measurement]:has-text("${calibrationId}")`, 'client_performance');
    report.checks.real_measurement_uses_scoped_calibration=true;
    const gate=asset.locator('form[action="/actions/testing-ios-gate-create"]');
    await gate.locator('[name=plan_id]').fill('ios-console-hitch-gate');
    await gate.locator('[name=thresholds]').fill('{"max_hitch_ratio":0.05}');
    await submit(gate);
    await runPlan('ios-console-hitch-gate','passed',30000);
    report.checks.frozen_offline_gate_passed=true;
    }
    await page.setViewportSize({width:390,height:844});
    for(const lang of ['zh','en','ja','fr','ru']) {
      assert.equal((await page.goto(url('client_performance',lang))).status(),200);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth-window.innerWidth)<=2);
      assert.equal(JSON.parse(await page.locator('[data-ios-frame-recording] textarea').inputValue()).spec.ios_capture.template,'frame_lifetimes');
    }
    report.checks.viewport_390=true;
    report.checks.five_languages=true;
    assert.deepEqual(errors,[]);
    report.passed=true;
  } catch(error) {
    report.passed=false; report.error=error.message; throw error;
  } finally {
    fs.writeFileSync(process.env.IATEST_PERF_REPORT, JSON.stringify(report,null,2));
    process.stdout.write(JSON.stringify(report)+'\n');
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
