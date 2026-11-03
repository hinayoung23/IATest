(options) => {
 const started=performance.now(), findings=[], unknown=[];
 let visited=0,complete=true;
 const issue=(criterion,rule,e,detail)=>{if(findings.length<200)findings.push({criterion,rule,identifier:e?.id||'',tag:e?.tagName||'',detail});};
 if(!document.title.trim())issue('2.4.2','document_title',null,'Empty document title');
 try{new Intl.Locale(document.documentElement.lang);}catch{issue('3.1.1','document_language',document.documentElement,'Missing or invalid language');}
 const ids=new Set(),label=(e)=>e.getAttribute('aria-label')||((e.getAttribute('aria-labelledby')||'').split(/\s+/).map(id=>document.getElementById(id)?.textContent||'').join(' ').trim())||Array.from(e.labels||[]).map(x=>x.textContent).join(' ').trim()||e.alt||e.textContent?.trim()||'';
 const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_ELEMENT);
 const color=(s)=>{const n=s.match(/[\d.]+/g)?.map(Number);return n&&n.length>=3&&(n.length<4||n[3]===1)?n.slice(0,3):null;};
 const lum=(c)=>c.map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
 let e=walker.currentNode;
 while(e){
  if(++visited>options.max_nodes||performance.now()-started>250){complete=false;break;}
  const style=getComputedStyle(e),rect=e.getBoundingClientRect(),visible=style.display!=='none'&&style.visibility!=='hidden'&&rect.width>0&&rect.height>0;
  if(e.id){if(ids.has(e.id))unknown.push({criterion:'1.3.1',rule:'duplicate_id',identifier:e.id});ids.add(e.id);}
  if(visible){
   if(e.tagName==='IMG'&&!e.hasAttribute('alt'))issue('1.1.1','image_alt',e,'Image has no explicit alternative');
   if(e.matches('button,a[href],input:not([type=hidden]),select,textarea,[role=button],[role=link]')&&!label(e))issue('4.1.2','control_name',e,'Interactive element has no observable accessible name');
   if(e.matches('input:not([type=hidden]):not([type=button]):not([type=submit]),select,textarea')&&!label(e))issue('3.3.2','form_label',e,'Form field has no observable label');
   if(e.hasAttribute('aria-labelledby')&&e.getAttribute('aria-labelledby').split(/\s+/).some(id=>!document.getElementById(id)))issue('4.1.2','broken_label_reference',e,'Accessible label reference is missing');
   if(e.tabIndex>0)unknown.push({criterion:'2.4.3',rule:'positive_tabindex',identifier:e.id});
   if(e.matches('button,input,select,textarea,a[href]')&&(rect.width<24||rect.height<24))unknown.push({criterion:'2.5.8',rule:'target_size_exception_review',identifier:e.id});
   if(Array.from(e.childNodes).some(n=>n.nodeType===Node.TEXT_NODE&&n.textContent.trim())){
    const fg=color(style.color),bg=color(style.backgroundColor);
    if(fg&&bg){const a=lum(fg),b=lum(bg),ratio=(Math.max(a,b)+.05)/(Math.min(a,b)+.05),large=parseFloat(style.fontSize)>=24||(parseFloat(style.fontSize)>=18.66&&parseInt(style.fontWeight)>=700);
      if(ratio<(large?3:4.5))issue('1.4.3','text_contrast',e,'Observed opaque text/background contrast below threshold');
    }else unknown.push({criterion:'1.4.3',rule:'composited_contrast_review',identifier:e.id});
   }
  }
  e=walker.nextNode();
 }
 return {complete,visited_nodes:visited,findings,manual_hints:unknown.slice(0,200),document_url:location.href};
}
