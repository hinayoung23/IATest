((region, context={}) => {
  let scope=document;
  for(const selector of context.shadow||[]){
    const hosts=scope.querySelectorAll(selector);
    if(hosts.length!==1||!hosts[0].shadowRoot)return {controls:[],complete:false,region_missing:true};
    scope=hosts[0].shadowRoot;
  }
  const roots=region?scope.querySelectorAll(region):null;
  if(roots&&roots.length!==1)return {controls:[],complete:false,region_missing:true};
  const root=roots?roots[0]:scope===document?document.body:scope;
  if (!root) return {controls: [], complete: true, region_missing: true};
  const nodes = [], contexts=[], deadline = performance.now() + 40;
  let visited = 0, complete = true;
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  let e = root;
  do {
    if (++visited > 1200 || performance.now() > deadline || nodes.length >= 24) {complete = false; break;}
    if(e.nodeType!==Node.ELEMENT_NODE)continue;
    const descriptor=e.id?'#'+CSS.escape(e.id):e.dataset.testid?'[data-testid="'+CSS.escape(e.dataset.testid)+'"]':null;
    if(descriptor&&contexts.length<8){
      if(e.matches('iframe,frame')&&e.src&&!e.src.startsWith('about:')){
        try{contexts.push({kind:'frame',context:{...context,frame:[...(context.frame||[]),{selector:descriptor,origin:new URL(e.src).origin}]}});}catch{}
      }
      if(e.shadowRoot)contexts.push({kind:'shadow',context:{...context,shadow:[...(context.shadow||[]),descriptor]}});
    }
    if (e.matches('input,textarea,select,button,a[href],h1,h2,nav,[role="button"],[role="tab"],[role="dialog"],[role="status"],[aria-live]') && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden') {
      const text = e.tagName === 'INPUT' ? '' : (e.innerText || '').trim().slice(0, 120);
      const label = e.getAttribute('aria-label') || e.labels?.[0]?.innerText?.trim().slice(0, 120) || '';
      const target = e.dataset.testid ? {test_id:e.dataset.testid} : e.id ? {id:e.id} : label ? {label} : text ? {text} : null;
      nodes.push({tag:e.tagName.toLowerCase(),role:e.getAttribute('role') || '',text,label,type:e.type || '',target:target?{...context,...target}:null});
    }
  } while ((e = walk.nextNode()));
  return {url:location.href,title:document.title.slice(0,160),controls:nodes,contexts,visited,complete,
    note:'Partial orientation only; use a targeted query before action or assertions'};
})
