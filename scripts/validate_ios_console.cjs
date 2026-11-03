const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.IATEST_CHROMIUM_PATH});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const url=(tab,lang='zh')=>`${process.env.IATEST_SMOKE_URL}/testing?project=ios-console&tab=${tab}&lang=${lang}`;
  try{
    await page.goto(url('client_performance'));
    const f=page.locator('form[action="/actions/testing-ios-metrics-import"]');
    await f.locator('[name=content]').fill(process.env.IATEST_IOS_IMPORT);
    await f.locator('button').click();
    await page.locator('[data-ios-measurement]').waitFor({timeout:60000});
    const checks=[['ios-offline-pass',{max_ios_launch_ms:1500,max_cpu_percent:50,max_memory_footprint_kb:50000},'passed'],['ios-offline-fail',{max_ios_launch_ms:1000},'failed'],['ios-offline-missing',{max_frame_duration_ms:20},'inconclusive']];
    let passRun='';
    for(const [id,thresholds,verdict] of checks){
      await page.goto(url('client_performance'));
      const gate=page.locator('form[action="/actions/testing-ios-gate-create"]');
      await gate.locator('[name=plan_id]').fill(id);await gate.locator('[name=thresholds]').fill(JSON.stringify(thresholds));await gate.locator('button').click();
      const section=page.locator('section').filter({has:page.getByRole('heading',{name:id,exact:true})});
      await section.locator('form[action="/actions/testing-run"] button').click();
      await page.waitForFunction(([id,verdict])=>[...document.querySelectorAll('[data-run-id]')].some(e=>e.querySelector('h2')?.textContent===id&&e.querySelector(`[data-verdict="${verdict}"]`)),[id,verdict],{timeout:60000});
      if(id==='ios-offline-pass'){
        const run=page.locator('[data-run-id]').filter({has:page.getByRole('heading',{name:id,exact:true})});passRun=await run.getAttribute('data-run-id');
        assert.ok((await run.locator('.testing-metrics').innerText()).includes('物理内存'));
      }
    }
    if(process.env.IATEST_REAL_MODEL==='1'){
      await page.goto(url('runs'));await page.locator(`[data-run-id="${passRun}"] form[action="/actions/testing-analyze"] button`).click();
      await page.locator('[data-analysis-id]').waitFor({timeout:180000});
    }
    await page.goto(url('nodes'));
    const bundle=page.locator('form[action="/actions/testing-windows-bundle"]');
    await bundle.locator('[name=content]').fill(process.env.IATEST_WINDOWS_BUNDLE);await bundle.locator('button').click();
    const download=page.locator('a[href^="/testing-node-bundle/"]').first();await download.waitFor();
    const response=await page.request.get(new URL(await download.getAttribute('href'),process.env.IATEST_SMOKE_URL).href);assert.equal(response.status(),200);assert.equal(response.headers()['content-type'],'application/zip');assert.ok((await response.body()).length>1000);
    await page.goto(url('runs'));await page.screenshot({path:'/private/tmp/iatest-ios-console-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    for(const tab of ['client_performance','nodes','runs','analysis']){await page.goto(url(tab));assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth)<=2,`${tab} overflow`);}
    await page.screenshot({path:'/private/tmp/iatest-ios-console-phone.png',fullPage:true});
    for(const locale of ['zh','en','ja','fr','ru']){await page.goto(url('client_performance',locale));assert.equal(await page.locator('html').getAttribute('lang'),locale==='zh'?'zh-CN':locale);}
    assert.deepEqual(errors,[]);
    process.stdout.write(JSON.stringify({passed:true,page_errors:errors})+'\n');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
