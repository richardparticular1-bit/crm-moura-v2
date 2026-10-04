/* Cores da clínica: sem cache global para evitar compartilhar o tema entre contas. */
(function(global){
  const defaults={themePrimary:'#0f766e',themeSidebar:'#134e4a',themeBackground:'#f1f5f4'};
  const presets={
    moura:{themePrimary:'#29a8f5',themeSidebar:'#16344b',themeBackground:'#f3f8fc'},
    suave:{themePrimary:'#83b9d6',themeSidebar:'#326d8c',themeBackground:'#f5f8fa'},
    original:defaults
  };
  const valid=c=>typeof c==='string'&&/^#[0-9a-f]{6}$/i.test(c);
  const rgb=c=>[1,3,5].map(i=>parseInt(c.slice(i,i+2),16));
  const hex=a=>'#'+a.map(v=>Math.round(v).toString(16).padStart(2,'0')).join('');
  const mix=(a,b,t)=>hex(rgb(a).map((v,i)=>v*(1-t)+rgb(b)[i]*t));
  function luminance(c){return rgb(c).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);}
  function contrast(a,b){const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);}
  const textOn=c=>contrast(c,'#ffffff')>=contrast(c,'#000000')?'#ffffff':'#000000';
  function normalize(settings={}){return Object.fromEntries(Object.entries(defaults).map(([k,v])=>[k,valid(settings[k])?settings[k].toLowerCase():v]));}
  function tokens(settings){
    const s=normalize(settings),p=s.themePrimary,n=s.themeSidebar,b=s.themeBackground;
    let link=p;
    for(let i=0;contrast(link,'#ffffff')<4.5&&i<100;i++)link=mix(link,'#000000',.05);
    const on=textOn(n),buttonOn=textOn(p),ink=textOn(b);
    const muted=(fg,bg)=>{const c=mix(fg,bg,.15);return contrast(c,bg)>=4.5?c:fg;};
    return {'--brand-primary':p,'--brand-on-primary':buttonOn,'--brand-sidebar':n,'--brand-on-sidebar':on,
      '--brand-sidebar-muted':muted(on,n),'--brand-page-ink':ink,'--brand-page-muted':muted(ink,b),
      '--teal-950':mix(n,'#000000',.45),'--teal-900':n,'--teal-800':link,'--teal-700':link,
      '--teal-300':mix(p,'#ffffff',.65),'--teal-100':mix(p,'#ffffff',.9),'--bg':b};
  }
  function apply(settings={}){
    const s=normalize(settings),root=document.documentElement;
    for(const [k,v] of Object.entries(tokens(s)))root.style.setProperty(k,v);
    const meta=document.querySelector('meta[name="theme-color"]');if(meta)meta.content=s.themeSidebar;
  }
  const api={defaults,presets,valid,normalize,tokens,contrast,textOn,apply};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else global.ClinicAppearance=api;
})(globalThis);
