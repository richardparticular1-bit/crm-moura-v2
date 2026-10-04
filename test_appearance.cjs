const assert=require('assert/strict');
const theme=require('./frontend/appearance.js');
for(const color of ['#000000','#ffffff','#29a8f5','#83b9d6','#808080','#ffff00','#ff0000','#00ff00','#0000ff']){
  const t=theme.tokens({themePrimary:color,themeSidebar:color,themeBackground:color});
  for(const [fg,bg] of [['--brand-on-primary','--brand-primary'],['--brand-on-sidebar','--brand-sidebar'],['--brand-sidebar-muted','--brand-sidebar'],['--brand-page-ink','--bg'],['--brand-page-muted','--bg']])
    assert(theme.contrast(t[fg],t[bg])>=4.5,`${color}: ${fg}`);
  assert(theme.contrast(t['--teal-700'],'#ffffff')>=4.5);
}
assert.deepEqual(theme.normalize({themePrimary:'red;display:none',themeSidebar:'url(evil)',themeBackground:null}),theme.defaults);
const properties=new Map();
const meta={content:''};
global.document={documentElement:{style:{setProperty:(k,v)=>properties.set(k,v)}},querySelector:()=>meta};
theme.apply(theme.presets.moura);
assert.equal(properties.get('--brand-primary'),'#29a8f5');
theme.apply({themePrimary:'#ff0000',themeSidebar:'#000000',themeBackground:'#ffffff'});
assert.equal(properties.get('--brand-primary'),'#ff0000');
assert.equal(meta.content,'#000000');
theme.apply();
assert.equal(properties.get('--brand-primary'),theme.defaults.themePrimary);
console.log('Aparência: contraste, cores inválidas, aplicação e troca de clínica passaram.');
