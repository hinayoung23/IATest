const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.IATEST_SMOKE_BROWSER});
  const page = await browser.newPage({viewport:{width:1360,height:960}});
  const failures=[];
  page.on('pageerror', error => failures.push(error.message));
  const base=process.env.IATEST_SMOKE_URL;
  const url=(path,extra='')=>`${base}${path}?project=console-smoke&lang=zh${extra}`;
  async function visit(path,extra='') {
    const response=await page.goto(url(path,extra));assert.equal(response.status(),200);
  }
  async function post(form,button='保存') {
    await Promise.all([page.waitForURL(u=>u.pathname==='/mobile'),form.getByRole('button',{name:button,exact:true}).click()]);
    await page.waitForLoadState('domcontentloaded');
  }
  try {
    await visit('/');
    await page.getByRole('link',{name:'移动端测试',exact:true}).first().click();
    let form=page.locator('form[action="/actions/mobile-profile"]');
    await form.locator('[name=id]').fill('smoke-phone');
    await form.locator('[name=device_id]').fill('console-fixture');
    await form.locator('[name=app_id]').fill('com.example.shop');
    assert.equal(await form.locator('[name=advanced_profile]').count(),1);
    await form.locator('[name=advanced_profile]').locator('xpath=ancestor::details[1]').locator('summary').click();
    await form.locator('[name=advanced_profile]').fill(JSON.stringify({allowed_apps:['com.example.login'],allowed_permissions:[],allow_clipboard:false,allow_webview:false,video_capture:'screenshot'}));
    await post(form);
    assert.equal(await page.locator('td').getByText('smoke-phone',{exact:true}).count(),1);
    await page.getByRole('button',{name:'打开会话',exact:true}).click();
    await page.locator('form[action="/actions/mobile-query"]').waitFor({timeout:15000});
    await post(page.locator('form[action="/actions/mobile-outline"]'),'页面控件概览');
    assert.equal(await page.locator('.mobile-candidate').count(),1);
    await page.getByRole('button',{name:'人工接管',exact:true}).click();
    form=page.locator('form[action="/actions/mobile-query"]');
    assert.equal(await form.locator('[name=allow_cache]').count(),1);
    assert.equal(await page.locator('[name=media_codec] option[value=h264]').count(),1);
    await form.locator('[name=identifier]').fill('com.example.shop:id/login');
    await post(form,'查找元素');
    assert.equal(await page.locator('.mobile-candidate').count(),1);
    await post(page.locator('form[action="/actions/mobile-cache-clear"]'),'清空节点缓存');
    form=page.locator('form[action="/actions/mobile-query"]');
    await form.locator('[name=identifier]').fill('com.example.shop:id/login');
    await post(form,'查找元素');
    assert.equal(await page.locator('form[action="/actions/mobile-visual-find"]').count(),1);
    assert.equal(await page.locator('[name=visual_crop]').count(),1);
    form=page.locator('form[action="/actions/mobile-action"]');
    assert.equal(await form.locator('[name=tap_backend] option[value=accessibility]').count(),1);
    await form.locator('[name=tap_backend]').selectOption('accessibility');
    await post(form,'执行');
    await page.getByText('injection_call_returned',{exact:true}).waitFor();
    await page.getByRole('button',{name:'获取画面',exact:true}).click();
    await page.waitForFunction(()=>{const image=document.querySelector('[data-mobile-frame]');return image && !image.hidden && image.complete && image.naturalWidth>0;});
    await page.getByRole('button',{name:'短时预览',exact:true}).click();
    await page.getByRole('button',{name:'■',exact:true}).click();
    await page.locator('[name=media_codec]').selectOption('h264-native');
    await page.getByRole('button',{name:'短时预览',exact:true}).click();
    await page.waitForFunction(()=>{const c=document.querySelector('[data-mobile-video]');return c&&!c.hidden&&c.width===96&&c.height===64&&c.getContext('2d').getImageData(0,0,96,64).data.some(v=>v>0);});
    await page.getByRole('button',{name:'■',exact:true}).click();
    await page.getByRole('button',{name:'关闭会话',exact:true}).click();
    await visit('/mobile','&tab=plans&method=manual');
    for (const name of ['android-multitouch.json','android-ime.json']) {
      const plan=JSON.parse(fs.readFileSync(require('node:path').join(__dirname,'../examples/plans',name),'utf8'));
      plan.spec.profile='smoke-phone';
      const editor=page.locator('form[action="/actions/mobile-plan"]').last();
      await editor.evaluate(form=>{form.closest('details').open=true;});
      await editor.locator('[name=content]').fill(JSON.stringify(plan));
      await post(editor);
      await page.getByText(plan.id,{exact:true}).first().waitFor();
      assert.equal(await page.locator('.mobile-workspace [role=alert]').count(),0);
    }
    form=page.locator('form[action="/actions/mobile-build-plan"]');
    await form.locator('[name=id]').fill('smoke-plan');
    await form.locator('[name=identifier]').fill('com.example.shop:id/login');
    await form.locator('[name=expected_identifier]').fill('com.example.shop:id/status');
    await form.locator('[name=expected_value]').fill('成功');
    await post(form);
    await page.locator('form[action="/actions/mobile-run"]').filter({has:page.locator('input[name=plan_id][value=smoke-plan]')}).getByRole('button',{name:'运行测试',exact:true}).click();
    await page.getByText('passed',{exact:true}).waitFor({timeout:15000});
    assert.equal(await page.locator('.mobile-workspace [role=alert]').count(),0);
    await visit('/mobile','&tab=plans&method=manual');
    await page.screenshot({path:'/private/tmp/iatest-mobile-console-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'/private/tmp/iatest-mobile-console-phone.png',fullPage:true});
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth-window.innerWidth);
    assert.ok(overflow<=2,`Viewport overflows by ${overflow}px`);
    await page.setViewportSize({width:1360,height:960});
    for(const path of ['/','/interfaces','/cases','/reports','/context','/config']) {
      await visit(path);assert.ok(await page.getByRole('link',{name:'移动端测试',exact:true}).count()>0);
    }
    assert.deepEqual(failures,[]);
    const report={browser:'Chromium',device:'explicit synthetic boundary; no phone operation',passed:true,
      flows:['device profile','session startup','bounded control overview','bounded query','cache clear','vision crop and codec controls','explicit accessibility tap selection','manual action','PNG capture','bounded preview','session teardown','gesture and IME JSON plan saves','plan builder','background run','result view','legacy navigation','390px responsive layout'],page_errors:failures};
    report.flows.push('real H.264 WebCodecs decode to canvas with synthetic native boundary');
    fs.writeFileSync('/private/tmp/iatest-mobile-console-acceptance.json',JSON.stringify(report,null,2));
    process.stdout.write(JSON.stringify(report)+'\n');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
