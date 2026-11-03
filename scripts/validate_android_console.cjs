const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.IATEST_SMOKE_BROWSER});
  const page = await browser.newPage({viewport:{width:1360,height:960}});
  page.setDefaultTimeout(30000);
  const errors=[];
  page.on('pageerror', error => errors.push(error.message));
  const project=process.env.IATEST_ANDROID_CONSOLE_PROJECT;
  const profile=process.env.IATEST_ANDROID_CONSOLE_PROFILE;
  const reportPath=process.env.IATEST_ANDROID_CONSOLE_REPORT;
  const report={physical_device:true,synthetic_device:false,project,profile,page_errors:errors,checks:{}};
  const url=(tab)=>`${process.env.IATEST_SMOKE_URL}/mobile?project=${encodeURIComponent(project)}&lang=zh&tab=${tab}`;
  async function post(form,button) {
    await Promise.all([page.waitForNavigation({waitUntil:'domcontentloaded'}),form.getByRole('button',{name:button,exact:true}).click()]);
  }
  async function query(identifier) {
    const form=page.locator('form[action="/actions/mobile-query"]');
    await form.locator('[name=identifier]').fill('dev.iatest.mobile.lab:id/'+identifier);
    await post(form,'查找元素');
    assert.equal(await page.locator('.mobile-candidate').count(),1);
    return page.locator('.mobile-candidate').first().innerText();
  }
  async function action(op,value='') {
    const form=page.locator('form[action="/actions/mobile-action"]');
    await form.locator('[name=op]').selectOption(op);
    if(op==='text') {
      await form.locator('[name=mode]').selectOption('semantic');
      await form.locator('[name=value]').fill(value);
    }
    await post(form,'执行');
    await page.getByText('injection_call_returned',{exact:true}).waitFor();
  }
  try {
    assert.equal((await page.goto(url('devices'))).status(),200);
    await page.locator('tr').filter({hasText:profile}).getByRole('button',{name:'打开会话',exact:true}).click();
    await page.locator('form[action="/actions/mobile-query"]').waitFor({timeout:90000});
    assert.ok((await query('ready_label')).includes('Ready'));
    report.checks.native_session_and_query=true;
    await page.getByRole('button',{name:'人工接管',exact:true}).click();
    await query('reset_button');await action('tap');
    const nickname='iatest_web_'+Date.now().toString(36);
    await query('nickname_field');await action('text',nickname);
    assert.ok((await query('nickname_field')).includes(nickname));
    await query('save_button');await action('tap');
    assert.ok((await query('saved_status')).includes('Saved: '+nickname));
    report.checks.manual_input_save_and_native_readback=true;
    report.nickname=nickname;
    await page.getByRole('button',{name:'获取画面',exact:true}).click();
    await page.waitForFunction(()=>{const img=document.querySelector('[data-mobile-frame]');return img&&!img.hidden&&img.complete&&img.naturalWidth>0;});
    report.checks.physical_png=true;
    await page.locator('[name=media_codec]').selectOption('h264-native');
    await page.getByRole('button',{name:'短时预览',exact:true}).click();
    await page.waitForFunction(()=>{
      const c=document.querySelector('[data-mobile-video]');
      if(!c||c.hidden||c.width<100||c.height<100)return false;
      const p=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      for(let i=0;i<p.length;i+=4)if(p[i+2]>p[i]+30&&p[i+2]>p[i+1]+10)return true;
      return false;
    });
    report.checks.physical_h264_webcodecs_blue_pixels=true;
    await page.waitForTimeout(3000);
    assert.equal(await page.locator('[data-mobile-video]').isVisible(),true);
    assert.ok(!/unavailable|failed|error/i.test(await page.locator('[data-mobile-preview-status]').innerText()));
    report.checks.preview_survives_static_window=true;
    await page.getByRole('button',{name:'■',exact:true}).click();
    await page.getByRole('button',{name:'关闭会话',exact:true}).click();
    await page.goto(url('plans'));
    const plan=JSON.parse(fs.readFileSync(process.env.IATEST_ANDROID_CONSOLE_PLAN,'utf8'));
    plan.id='android-console-save-relaunch';
    plan.spec.profile=profile;
    const editor=page.locator('form[action="/actions/mobile-plan"]').last();
    await editor.evaluate(form=>{form.closest('details').open=true;});
    await editor.locator('[name=content]').fill(JSON.stringify(plan));
    await post(editor,'保存');
    const runner=page.locator('form[action="/actions/mobile-run"]').filter({has:page.locator('input[name=plan_id][value=android-console-save-relaunch]')});
    await runner.getByRole('button',{name:'运行测试',exact:true}).click();
    await page.locator('.mobile-workspace section').filter({hasText:'android-console-save-relaunch'}).filter({hasText:'passed'}).first().waitFor({timeout:120000});
    report.checks.web_plan_save_background_run_and_result=true;
    await page.setViewportSize({width:390,height:844});
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-window.innerWidth);
    assert.ok(overflow<=2,`Viewport overflows by ${overflow}px`);
    report.checks.viewport_390=true;
    for(const lang of ['zh','en','ja','fr','ru']) {
      assert.equal((await page.goto(url('plans').replace('lang=zh',`lang=${lang}`))).status(),200);
      assert.ok((await page.evaluate(()=>document.documentElement.scrollWidth-window.innerWidth))<=2);
    }
    report.checks.five_languages=true;
    assert.deepEqual(errors,[]);
    report.passed=true;
  } catch(error) {
    report.passed=false;report.error=error.message;
    throw error;
  } finally {
    fs.writeFileSync(reportPath,JSON.stringify(report,null,2));
    process.stdout.write(JSON.stringify(report)+'\n');
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
