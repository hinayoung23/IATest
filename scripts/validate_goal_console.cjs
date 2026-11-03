const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.IATEST_CHROMIUM_PATH});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const url=tab=>`${process.env.IATEST_SMOKE_URL}/testing?project=console-smoke&lang=zh&tab=${tab}`;
  try{
    await page.goto(url('exploration'));
    const form=page.locator('form[action="/actions/testing-explore"]'),context=JSON.parse(process.env.IATEST_GOAL_CONTEXT);
    await form.locator('[name=goal]').fill(process.env.IATEST_GOAL);
    await form.locator('[name=expected_result]').fill(process.env.IATEST_EXPECTED);
    await form.locator('[name=url]').fill(context.start_url);
    await form.locator('details').locator('summary').click();
    await form.locator('[name=context]').fill(JSON.stringify(context));
    await form.getByRole('button').click();
    await page.waitForFunction(()=>[...document.querySelectorAll('[data-case-id]')].some(e=>e.dataset.caseId!=='seed'&&e.querySelector('[data-case-state="reproducible_passed"]')),{},{timeout:60000});
    const item=page.locator('[data-case-id]:not([data-case-id="seed"])').filter({has:page.locator('[data-case-state="reproducible_passed"]')}).first();
    await item.locator('a').click();await page.waitForURL(u=>u.pathname==='/assets');
    assert.ok(await page.getByRole('heading',{name:'关联资产',exact:true}).count());
    assert.ok(await page.locator('form[action="/actions/testing-run"]').count());
    await page.screenshot({path:'/private/tmp/iatest-goal-asset-detail.png',fullPage:true});
    await page.goto(`${process.env.IATEST_SMOKE_URL}/assets?project=console-smoke&lang=zh`);
    assert.equal(await page.locator('[data-asset-category]').count(),7);
    await page.locator('[data-asset-category="performance"]').click();
    await page.locator('[data-asset-key="engine-plan:console-perf"] a').click();
    await page.getByRole('heading',{name:'性能门禁',exact:true}).waitFor();
    await page.goto(url('exploration'));
    await page.screenshot({path:'/private/tmp/iatest-goal-console-desktop.png',fullPage:true});
    if(process.env.IATEST_SKIP_ANALYSIS!=='1'){
    await page.goto(url('runs'));
    const run=page.locator(`[data-run-id="${process.env.IATEST_ANALYSIS_RUN}"]`);
    await run.locator('form[action="/actions/testing-analyze"]').getByRole('button').click();
    await page.locator('[data-analysis-id]').waitFor({timeout:180000});
    assert.ok(await page.getByRole('heading',{name:'AI 解释与假设',exact:true}).count());
    assert.ok(await page.getByRole('heading',{name:'建议与复测方法',exact:true}).count());
    await page.screenshot({path:'/private/tmp/iatest-goal-console-desktop.png',fullPage:true});
    }
    await page.setViewportSize({width:390,height:844});
    for(const tab of ['exploration','analysis']){
      await page.goto(url(tab));
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth)<=2,`${tab} overflow`);
    }
    await page.screenshot({path:'/private/tmp/iatest-goal-console-phone.png',fullPage:true});
    for(const locale of ['en','ja','fr','ru']){
      await page.goto(url('exploration').replace('lang=zh',`lang=${locale}`));
      assert.equal(await page.locator('html').getAttribute('lang'),locale);
    }
    assert.deepEqual(errors,[]);
    const report={passed:true,flows:['goal-first form and background cached-case replay with actual Chromium','linked UI case and compiled-plan assets','unified asset categories and readable performance configuration','390px layout','five locales'],analysis:process.env.IATEST_SKIP_ANALYSIS==='1'?'not requested':'real-model evidence analysis passed',page_errors:errors};
    fs.writeFileSync('/private/tmp/iatest-goal-console-acceptance.json',JSON.stringify(report,null,2));
    process.stdout.write(JSON.stringify(report)+'\n');
  }catch(error){await page.screenshot({path:'/private/tmp/iatest-goal-console-error.png',fullPage:true});throw error;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
