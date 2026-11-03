const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.IATEST_CHROMIUM_PATH});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const base=process.env.IATEST_SMOKE_URL;
  const url=(tab,lang='zh')=>`${base}/testing?project=business-smoke&lang=${lang}&tab=${tab}`;
  async function submitJSON(action,content){const f=page.locator(`form[action="/actions/testing-${action}"]`).first();await f.locator('[name=content]').fill(JSON.stringify(content));await f.getByRole('button').click();}
  try {
    await page.goto(url('assets'));
    for(const fmt of ['csv','json','xlsx']){
      const response=await page.request.get(`${base}/testing-template?format=${fmt}&mode=goals`);
      assert.equal(response.status(),200);assert.ok((await response.body()).length>100);
    }
    const plan=JSON.parse(process.env.IATEST_CASE_PLAN);
    const content={cases:[{case_id:'browser-bulk',title:'Bulk heading',plan}]};
    await page.locator('[data-case-upload]').setInputFiles({name:'cases.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(content))});
    await page.locator('form[action="/actions/testing-import-preview"] button').click();
    await page.locator('[data-import-id]').first().waitFor();
    await page.locator('form[action="/actions/testing-import-commit"]').first().getByRole('button').click();
    await page.locator('[data-case-id="browser-bulk"]').waitFor();
    await page.locator('form[action="/actions/testing-import-process"]').first().getByRole('button').click();
    await page.waitForFunction(()=>document.querySelector('[data-case-id="browser-bulk"]')?.textContent.includes('verified'),{},{timeout:60000});
    await page.goto(url('client_performance'));
    await submitJSON('save',{...JSON.parse(process.env.IATEST_CLIENT_PLAN),id:'browser-client',spec:{...JSON.parse(process.env.IATEST_CLIENT_PLAN).spec,iterations:1,warmup:0}});
    await page.locator('section').filter({has:page.getByRole('heading',{name:'browser-client',exact:true})}).locator('form[action="/actions/testing-run"]').getByRole('button').click();
    await page.waitForFunction(()=>[...document.querySelectorAll('[data-run-id]')].some(e=>e.textContent.includes('browser-client')&&e.querySelector('[data-verdict="passed"]')),{},{timeout:60000});
    await page.goto(url('campaigns'));
    await submitJSON('campaign-create',{id:'console-release',build:{service_version:'build-2'},nodes:[{id:'api',plan_id:'api-smoke'},{id:'client',plan_id:'browser-client',depends_on:['api']}]});
    await page.locator('form[action="/actions/testing-campaign-run"]').getByRole('button').click();
    await page.waitForFunction(()=>[...document.querySelectorAll('[data-campaign-run]')].some(e=>e.textContent.includes('completed')&&e.textContent.includes('passed')),{},{timeout:60000});
    const app=JSON.parse(process.env.IATEST_APP_SPEC);
    if(app){
      await page.goto(url('apps'));
      await submitJSON('app-fetch',app);
      await page.waitForFunction(()=>[...document.querySelectorAll('section h2')].some(e=>e.textContent==='actual-probe'),{},{timeout:60000});
    }
    await page.goto(url('nodes'));
    assert.equal(await page.locator('[data-node-id]').count(),2);
    await submitJSON('node-enroll',{id:'console-node',token_env:'IATEST_EXISTING_TOKEN',allowed_origins:['http://localhost:8000'],allowed_kinds:['api','performance']});
    await page.locator('[data-node-id="console-node"]').waitFor();
    await submitJSON('controller-start',{host:'127.0.0.1',port:0});
    await page.locator('form[action="/actions/testing-controller-stop"]').getByRole('button').click();
    await page.goto(url('campaigns'));
    await page.screenshot({path:'/private/tmp/iatest-business-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    for(const tab of ['assets','apps','client_performance','nodes','remote','campaigns','runs']){
      await page.goto(url(tab));
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth)<=2,`${tab} overflow`);
    }
    await page.screenshot({path:'/private/tmp/iatest-business-phone.png',fullPage:true});
    for(const lang of ['zh','en','ja','fr','ru']){
      for(const tab of ['assets','apps','client_performance','nodes','remote','campaigns']){
        await page.goto(url(tab,lang));assert.equal(await page.locator('html').getAttribute('lang'),lang==='zh'?'zh-CN':lang);
      }
    }
    assert.deepEqual(errors,[]);
    const report={passed:true,flows:['CSV/JSON/XLSX template download','upload,preview,commit,batch native UI replay','client performance save/background run','version campaign snapshot/background run','actual APK fetch and inspection without installation','node enrollment/controller start-stop','390px layout and five locales'],page_errors:errors};
    fs.writeFileSync('/private/tmp/iatest-business-console.json',JSON.stringify(report,null,2));
    process.stdout.write(JSON.stringify(report)+'\n');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
