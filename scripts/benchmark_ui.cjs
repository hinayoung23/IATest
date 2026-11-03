// Development-only mature-tool baseline. The framework has no Playwright dependency.
const {chromium}=require('playwright');
const fs=require('node:fs');
(async()=>{
  const input=JSON.parse(fs.readFileSync(0,'utf8')),evidence=[];
  const began=performance.now(),cpu=process.cpuUsage();
  const browser=await chromium.launch({executablePath:input.browser,headless:true,args:['--window-size=1280,800']});
  process.on('SIGINT',async()=>{await browser.close();process.exit(130);});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:800}});
    await page.goto(input.plan.spec.start_url);
    function locator(target){
      let root=page;
      for(const frame of target.frame||[])root=root.frameLocator(frame.selector);
      for(const host of target.shadow||[])root=root.locator(host);
      if(target.scope)root=root.locator(target.scope);
      if(target.id)return root.locator('#'+CSSescape(target.id));
      if(target.test_id)return root.getByTestId(target.test_id);
      if(target.role)return root.getByRole(target.role,{name:target.name,exact:true});
      if(target.label)return root.getByLabel(target.label,{exact:true});
      if(target.text)return root.getByText(target.text,{exact:true});
      return root.locator(target.selector);
    }
    function CSSescape(text){return String(text).replace(/[^a-zA-Z0-9_-]/g,c=>'\\'+c);}
    for(const step of input.plan.spec.steps){
      const node=locator(step.target);
      if(await node.count()!==1)throw Error('Target missing or ambiguous');
      evidence.push(await node.ariaSnapshot());
      if(step.op==='fill')await node.fill(step.value);
      else if(step.op==='click')await node.click();
      else if(step.op==='expect'||step.op==='wait'){
        const deadline=Date.now()+2000;
        let passed=false;
        do{
          const actual=step.assert==='value'?await node.inputValue():step.assert==='enabled'?await node.isEnabled():(await node.textContent()).trim();
          passed=actual===step.expected;
          if(!passed)await page.waitForTimeout(30);
        }while(!passed&&Date.now()<deadline);
        if(!passed)throw Error('Business postcondition mismatch');
      }else throw Error('Benchmark manifest uses an unsupported baseline operation');
    }
    const spent=process.cpuUsage(cpu);
    process.stdout.write(JSON.stringify({passed:true,wall_ms:performance.now()-began,host_cpu_seconds:(spent.user+spent.system)/1e6,evidence,model_calls:0,protocol_bytes:null}));
  }finally{await browser.close();}
})().catch(error=>{process.stderr.write(error.message);process.exitCode=1;});
