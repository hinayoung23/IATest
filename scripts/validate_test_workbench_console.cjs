const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');

(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.IATEST_SMOKE_BROWSER});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const base=process.env.IATEST_SMOKE_URL,lab=process.env.IATEST_LAB_URL;
  const url=tab=>`${base}/testing?project=console-smoke&lang=zh&tab=${tab}`;
  async function visit(tab){const r=await page.goto(url(tab));assert.equal(r.status(),200);}
  async function fill(form,name,value){await form.locator(`[name="${name}"]`).fill(value);}
  async function save(form){await Promise.all([page.waitForURL(u=>u.pathname==='/testing'),form.getByRole('button',{name:'保存计划',exact:true}).click()]);}
  async function create(kind,id,path){
    await visit(kind);const form=page.locator('form[action="/actions/testing-build"]');
    await fill(form,'id',id);
    if(kind!=='workflow'){await fill(form,'url',lab+path);await fill(form,'origins',lab);}
    return form;
  }
  async function run(id){
    const article=page.locator('.testing-plan').filter({has:page.getByRole('heading',{name:id,exact:true})});
    await article.getByRole('button',{name:'运行测试',exact:true}).click();
    const result=page.locator('section[data-run-id]').filter({has:page.getByRole('heading',{name:id,exact:true})}).first();
    await result.locator('[data-verdict]').waitFor({timeout:30000});
    assert.equal(await result.locator('[data-verdict]').getAttribute('data-verdict'),'passed',await result.innerText());
  }
  try{
    let f=await create('web','console-web','/ui');
    assert.deepEqual(await f.locator('[name=browser] option').evaluateAll(xs=>xs.map(x=>x.value)),['chromium','firefox','webkit']);
    await f.locator('[name=op]').selectOption('fill');await fill(f,'selector','#username');await fill(f,'value','Console');await fill(f,'expected_selector','#username');
    // Text inputs have no textContent; use a meaningful visible postcondition with a click plan instead.
    await f.locator('[name=op]').selectOption('click');await fill(f,'selector','#login');await fill(f,'expected_selector','#status');await fill(f,'expected','Hello');
    assert.equal(await f.locator('[name=value]').isVisible(),false);
    await save(f);await run('console-web');
    const image=page.locator('.testing-evidence').first();await image.waitFor();
    await page.waitForFunction(()=>{const i=document.querySelector('.testing-evidence');return i&&i.complete&&i.naturalWidth>0;});
    f=await create('performance','console-performance','/health');await fill(f,'duration','0.3');await fill(f,'rate','10');await save(f);await run('console-performance');
    f=await create('performance','console-users','/health');await f.locator('[name=mode]').selectOption('users');await fill(f,'duration','0.2');await save(f);await run('console-users');
    f=await create('security','console-security','/health');await f.locator('[name=rule]').selectOption('response_headers');await save(f);await run('console-security');
    f=await create('api','console-api','/order');await save(f);await run('console-api');
    f=await create('workflow','console-workflow','');await fill(f,'chain','console-api\nconsole-web\nconsole-performance\nconsole-security');await fill(f,'cleanup','console-api');await fill(f,'max_parallel','2');await f.locator('[name=independent_branches]').check();await save(f);await run('console-workflow');
    for(const plan of [
      {id:'console-data',kind:'data',spec:{records:[{id:1},{id:2}],checks:[{id:'unique',rule:'unique',path:'$.id'}]}},
      {id:'console-protocol',kind:'protocol',spec:{allowed_origins:[lab],checks:[{id:'headers',request:{url:lab+'/health'},headers:{'x-content-type-options':'nosniff'}}]}},
      {id:'console-accessibility',kind:'accessibility',spec:{allowed_origins:[lab],start_url:lab+'/ui',checks:[{id:'name',rule:'accessible_name',target:{id:'login'}}]}},
      {id:'console-security-extensions',kind:'security',spec:{allowed_origins:[lab],checks:[{id:'exposure',rule:'sensitive_exposure',request:{url:lab+'/health'},options:{forbidden_paths:['$.password']}}]}},
    ]){
      await visit(plan.kind);f=page.locator('form[action="/actions/testing-save"]').last();
      const disclosure=f.locator('xpath=ancestor::details[1]');if(await disclosure.count()&&await disclosure.getAttribute('open')===null)await disclosure.locator('summary').first().click();
      await fill(f,'content',JSON.stringify(plan));await save(f);await run(plan.id);
    }
    await visit('desktop');f=page.locator('form[action="/actions/testing-save"]').last();
    await fill(f,'content',JSON.stringify({id:'console-desktop-schema',kind:'desktop',spec:{platform:'macos',process_id:1234,app_id:'com.example.App',steps:[{op:'expect',target:{identifier:'status'},attribute:'exists',expected:true}]}}));
    await save(f);assert.ok(await page.getByRole('heading',{name:'console-desktop-schema',exact:true}).count());
    for(const [tab,action,content] of [
      ['security','security-library',{id:'console-library',version:'1',source:'explicit console fixture',rules:[{id:'control-chain',title:'Controlled finding',severity:'high',chain:[{request:{url:'{{url}}'},control:{status:[403]}},{request:{url:'{{url}}'},finding:{text_contains:'fixture-marker'}}]}]}],
      ['accessibility','accessibility-review',{id:'console-review',start_url:lab+'/ui',build_sha256:'a'.repeat(64),reviews:[{criterion:'1.1.1',status:'passed',reviewer:'explicit fixture reviewer',evidence_sha256:'b'.repeat(64),notes:'Manually reviewed this controlled fixture only.'}]}],
    ]) {
      await visit(tab);f=page.locator(`form[action="/actions/testing-${action}"]`);await fill(f,'content',JSON.stringify(content));
      await Promise.all([page.waitForURL(u=>u.pathname==='/testing'),f.getByRole('button').click()]);
      assert.equal(await page.locator('[role=alert]').count(),0);
      assert.ok((await page.locator('main').textContent()).includes(content.id));
    }
    const close=page.getByRole('button',{name:'关闭',exact:true});if(await close.count()&&await close.first().isVisible())await close.first().click();
    await page.screenshot({path:'/private/tmp/iatest-multitype-console-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});await visit('web');
    await page.screenshot({path:'/private/tmp/iatest-multitype-console-phone.png',fullPage:true});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth-window.innerWidth)<=2,'390px viewport overflow');
    await page.setViewportSize({width:1440,height:1000});
    await visit('web');f=page.locator('form[action="/actions/testing-inspect"]');await fill(f,'url',lab+'/ui');await fill(f,'origins',lab);await fill(f,'selector','#login');await f.getByRole('button',{name:'查找目标',exact:true}).click();
    await page.locator('section[data-run-id]').filter({has:page.getByRole('heading',{name:'web-inspection',exact:true})}).locator('[data-verdict=not_evaluated]').waitFor({timeout:30000});
    for(const locale of ['en','ja','fr','ru']){
      const localized=new URL(url('security'));localized.searchParams.set('lang',locale);
      const r=await page.goto(localized.href);assert.equal(r.status(),200);
      assert.equal(await page.locator('html').getAttribute('lang'),locale);
    }
    for(const path of ['/','/interfaces','/cases','/reports','/context','/config','/mobile']){
      const r=await page.goto(`${base}${path}?project=console-smoke&lang=zh`);assert.equal(r.status(),200);
      assert.ok(await page.getByRole('link',{name:'多类型测试',exact:true}).count()>0);
    }
    await visit('nodes');
    async function authorityForm(action,spec) {
      const f=page.locator(`form[action="/actions/testing-${action}"]`);
      if(spec!==undefined)await f.locator('[name=content]').fill(JSON.stringify(spec));
      await Promise.all([page.waitForURL(u=>u.pathname==='/testing'),f.getByRole('button').click()]);
      assert.equal(await page.locator('[role=alert]').count(),0);
    }
    await authorityForm('coordinator-start',{host:'127.0.0.1',port:0,identities:[{id:'console',token_env:'IATEST_CONSOLE_RESOURCE',resources:['lab:shared']}]});
    const section=page.locator('section').filter({has:page.locator('form[action="/actions/testing-coordinator-start"]')});
    const status=JSON.parse(await section.locator('pre').first().textContent());
    await authorityForm('coordinator-config',{url:`http://127.0.0.1:${status.port}`,id:'console',token_env:'IATEST_CONSOLE_RESOURCE',ttl:3});
    assert.ok((await page.locator('main').textContent()).includes('leases'));
    await authorityForm('coordinator-config',{});
    await authorityForm('coordinator-stop');
    assert.deepEqual(errors,[]);
    const report={passed:true,engines:'Real native CDP and HTTP engines; no device/engine stubs',flows:['existing plan forms','data/protocol/accessibility JSON plans','extensible security rules','parallel workflow and cleanup','background execution','web screenshot retrieval','performance arrival and users modes','security header control','read-only target inspection','390px responsive layout','locale routes','legacy navigation'],page_errors:errors};
    report.flows.push('shared resource authority start/configure/query/stop');
    report.flows.push('browser backend selector','desktop typed-plan save without native App execution','versioned security-library import','immutable manual WCAG review import');
    fs.writeFileSync('/private/tmp/iatest-multitype-console-acceptance.json',JSON.stringify(report,null,2));
    process.stdout.write(JSON.stringify(report)+'\n');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
