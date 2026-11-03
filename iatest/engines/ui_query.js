// Runs only in a named isolated CDP world. Page content is data, never code.
async function queryTarget(target, options) {
  const started = performance.now();
  const cap = options.cap || 5, maxNodes = 2000;
  let complete = true, visited = 0;
  const selectors = {button:'button,input[type=button],input[type=submit],[role="button"]',
    link:'a[href],[role="link"]',textbox:'input:not([type=hidden]),textarea,[role="textbox"]',
    checkbox:'input[type=checkbox],[role="checkbox"]',dialog:'dialog,[role="dialog"]',
    heading:'h1,h2,h3,h4,h5,h6,[role="heading"]',row:'tr,[role="row"]'};
  globalThis.__iatestHit = (x,y) => {
    let hit=document.elementFromPoint(x,y);
    for(let n=0;hit&&n<8;n++){const root=hit.shadowRoot||globalThis.__iatestClosedRoots?.get(hit);if(!root)break;const next=root.elementFromPoint(x,y);if(!next||next===hit)break;hit=next;}
    return hit;
  };
  let scope = document;
  for(const selector of (target.shadow || [])){
    const hosts=scope.querySelectorAll(selector);
    const root=hosts.length===1&&(hosts[0].shadowRoot||globalThis.__iatestClosedRoots?.get(hosts[0]));
    if(!root)return {search_complete:false,match_count:0,error:'Shadow host must uniquely expose a native-resolved root',candidates:[]};
    scope=root;
  }
  if (target.scope) {
    const roots = scope.querySelectorAll(target.scope);
    if (roots.length !== 1) return {search_complete:false, error:'Scope must resolve exactly once',candidates:[]};
    scope = roots[0];
  }
  if (target.anchor) {
    const anchors = scope.querySelectorAll(target.anchor.selector),matches=[];
    let examined=0;
    for(const e of anchors){
      if(++examined>maxNodes||performance.now()-started>100)return {search_complete:false,error:'Anchor query incomplete',candidates:[]};
      if(e.textContent.trim()===target.anchor.text)matches.push(e);
      if(matches.length>1)break;
    }
    if (matches.length !== 1) return {search_complete:false,error:'Anchor must resolve exactly once',candidates:[]};
    const container = matches[0].closest(target.anchor.container || 'tr,[role="row"],article');
    if (!container || !scope.contains(container)) return {search_complete:false,error:'Anchor has no valid container',candidates:[]};
    scope = container;
  }
  const escape = v => CSS.escape(v);
  let selector = target.selector || (target.test_id ? `[data-testid="${escape(target.test_id)}"]` :
    target.id ? '#' + escape(target.id) : target.role ? (selectors[target.role] || `[role="${escape(target.role)}"]`) : null);
  let elements;
  if (target.label) {
    const labelled=scope.querySelectorAll(`label,[aria-label="${escape(target.label)}"]`);
    elements=[];
    let examined=0;
    for(const e of labelled){
      if(++examined>maxNodes||performance.now()-started>100){complete=false;break;}
      const control=e.tagName==='LABEL'&&e.textContent.trim()===target.label?e.control:
        e.getAttribute('aria-label')===target.label?e:null;
      if(control&&scope.contains(control)&&!elements.includes(control)) elements.push(control);
    }
    selector='native-label-query';
  } else if (selector) {
    // Cache only raw id/test-id matches inside an explicitly resolved region.
    // Visibility, text and geometry are recomputed; action dispatch always bypasses.
    const eligible=options.cache!==false && !!target.scope && !target.anchor &&
      !target.selector && !target.role && !!(target.id||target.test_id);
    const now=performance.now();
    const registry=globalThis.__iatestRegions ||= new Map();
    for(const [root,entry] of registry){
      if(!root.isConnected||now-entry.used>5000){entry.observer.disconnect();registry.delete(root);}
    }
    let entry=eligible?registry.get(scope):null;
    if(eligible&&!entry){
      while(registry.size>=16){const first=registry.keys().next().value;registry.get(first).observer.disconnect();registry.delete(first);}
      entry={used:now,items:new Map()};
      entry.observer=new MutationObserver(()=>entry.items.clear());
      entry.observer.observe(scope,{subtree:true,childList:true,attributes:true,characterData:true});
      registry.set(scope,entry);
    }
    if(entry){
      if(entry.observer.takeRecords().length)entry.items.clear();
      entry.used=now;registry.delete(scope);registry.set(scope,entry);
      const cached=entry.items.get(selector);
      if(cached&&now-cached.at<=1000&&cached.nodes.every(e=>e.isConnected&&scope.contains(e))){
        elements=cached.nodes;options.cache_hit=true;
      } else {
        elements=scope.querySelectorAll(selector);
        if(elements.length<=32){
          while(entry.items.size>=4)entry.items.delete(entry.items.keys().next().value);
          entry.items.set(selector,{at:now,nodes:Array.from(elements)});
        }
      }
    } else elements=scope.querySelectorAll(selector);
  }
  else {
    elements = [];
    const walker = document.createTreeWalker(scope,NodeFilter.SHOW_ELEMENT);
    let node;
    while ((node = walker.nextNode())) {
      visited++;
      if (visited > maxNodes || performance.now() - started > 100) { complete=false; break; }
      if (target.label) {
        if (node.getAttribute('aria-label') === target.label ||
            (node.labels && Array.from(node.labels).some(l => l.textContent.trim() === target.label))) elements.push(node);
      } else if (target.text && node.children.length === 0 && node.textContent.trim() === target.text) elements.push(node);
      if (visited % 256 === 0) await new Promise(resolve => setTimeout(resolve,0));
    }
  }
  const candidates = [];
  let count = 0, matched = 0;
  for (const element of elements) {
    if (++count > maxNodes || performance.now() - started > 100) {complete=false;break;}
    if (target.text && element.textContent.trim() !== target.text) continue;
    const r=element.getBoundingClientRect(), style=getComputedStyle(element);
    const visible=!!(r.width && r.height && style.visibility!=='hidden' && style.display!=='none');
    if (target.visible !== false && !visible) continue;
    matched++;
    if (candidates.length < cap) candidates.push(element);
  }
  globalThis.__iatestNodes = candidates;
  return {search_complete:complete, match_count:matched, has_more:matched>cap,
    // Browser's internal selector scan cost is unknown; only own walk is counted.
    visited_nodes:selector ? null : visited, selector_internal_cost:'unknown',
    cache_hit:!!options.cache_hit, duration_ms:performance.now()-started, candidates:candidates.map((e,i)=>{
      const r=e.getBoundingClientRect();
      return {index:i,tag:e.tagName.toLowerCase(),identifier:e.id||e.getAttribute('data-testid')||'',
        text:e.type==='password'?'[REDACTED]':e.textContent.trim().slice(0,1000),
        truncated:e.textContent.trim().length>1000,enabled:!e.disabled&&e.getAttribute('aria-disabled')!=='true',
        rect:{x:r.x,y:r.y,width:r.width,height:r.height}};
    })};
}
