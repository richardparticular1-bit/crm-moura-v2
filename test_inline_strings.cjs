const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const html = fs.readFileSync('frontend/index.html', 'utf8');
const script = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map(m=>m[1]).find(s=>s.trim());
new vm.Script(script);
const fn = script.match(/function escJS\(s\)\{[^\n]+\}/)[0];
for (const name of ["D'Ávila", "Teste');globalThis.prova=1;//", '<img src=x onerror=alert(1)>', '" & \\ \n 😃']) {
  const ctx = { result: null, capture: s => { ctx.result = s; } };
  vm.createContext(ctx);
  vm.runInContext(fn, ctx);
  const encoded = vm.runInContext(`escJS(${JSON.stringify(name)})`, ctx);
  assert(!/[<>&'"\n]/.test(encoded));
  vm.runInContext(`capture('${encoded}')`, ctx);
  assert.equal(ctx.result, name);
  assert.equal(ctx.prova, undefined);
}
console.log('Escape de eventos: quatro casos de regressão passaram.');
