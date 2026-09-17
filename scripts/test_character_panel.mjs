import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const text = readFileSync(new URL('../admin-panel/js/api.js', import.meta.url), 'utf8');
const prefix = text.slice(text.indexOf('const rolePrefix'), text.indexOf('export async function request'))
  .replace('export const API', 'const API');
const resolve = new Function('window', prefix + '; return API;');
const location = pathname => ({location:{origin:'https://kiwi.example',pathname}});
assert.equal(resolve(location('/characters/A/admin/')), 'https://kiwi.example/characters/A');
assert.equal(resolve(location('/characters/B/admin/')), 'https://kiwi.example/characters/B');
assert.equal(resolve(location('/admin/')), 'https://kiwi.example');
assert.equal(resolve(location('/characters/default/admin/')), 'https://kiwi.example/characters/default');
assert.equal(resolve(location('/characters/A/admin/')), 'https://kiwi.example/characters/A');
console.log('PASS: 5 character panel URL isolation guards');

const wizard = readFileSync(new URL('../admin-panel/js/wizard.js', import.meta.url), 'utf8');
const render = wizard.slice(wizard.indexOf('function renderStep3'));
for (const cid of ['A', 'B']) {
  const API = resolve(location(`/characters/${cid}/admin/`));
  let copied;
  const handlers = {};
  const mask = {innerHTML:'', querySelector: selector => ({addEventListener: (event, fn) => {handlers[selector]=fn;}})};
  new Function('API','location','shell','escHtml','navigator','toast', render + ';renderStep3(arguments[6]);')(
    API, {origin:'https://kiwi.example'}, (step, html) => html, x=>x,
    {clipboard:{writeText: async text => {copied=text;}}}, ()=>{}, mask);
  await handlers['[data-wiz="copy"]']();
  assert.equal(copied, API + '/v1');
  assert.ok(mask.innerHTML.includes(API + '/v1'));
}
const doneKey = wizard.match(/const DONE_KEY = (.*);/)[1];
assert.notEqual(new Function('API', 'return ' + doneKey)('https://kiwi.example/characters/A'),
                new Function('API', 'return ' + doneKey)('https://kiwi.example/characters/B'));
console.log('PASS: wizard copy/display and per-role completion state');
