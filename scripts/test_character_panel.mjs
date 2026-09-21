import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const text = readFileSync(new URL('../admin-panel/js/api.js', import.meta.url), 'utf8');
// Evaluate the actual network module so every consumer uses the same tab identity.
const load = new Function('window', 'fetch', 'document', text.replace(/^export /gm, '') +
  '; return { API, CHARACTER_ID, request, download, sse };');
const location = pathname => ({location:{origin:'https://kiwi.example',pathname}});
const resolve = window => load(window).API;
assert.equal(resolve(location('/characters/A/admin/')), 'https://kiwi.example/characters/A');
assert.equal(resolve(location('/characters/B/admin/')), 'https://kiwi.example/characters/B');
assert.equal(resolve(location('/admin/')), 'https://kiwi.example');
assert.equal(resolve(location('/characters/default/admin/')), 'https://kiwi.example/characters/default');
assert.equal(resolve(location('/characters/A/admin/')), 'https://kiwi.example/characters/A');
console.log('PASS: 5 character panel URL isolation guards');

const encodedPanels = [
  ['/characters/%41/admin/', 'A'],
  ['/%63haracters/A/admin/', 'A'],
  ['/%63%68%61%72%61%63%74%65%72%73/%42/%61dmin/', 'B'],
  ['/characters/float%5Fchar%2D01/admin', 'float_char-01'],
];
for (const [path, cid] of encodedPanels) {
  const calls = [];
  const links = [];
  const api = load(location(path), async (url, opts) => {
    calls.push({url, opts});
    return {ok: true, body: {getReader: () => ({read: async () => ({done: true})})}};
  }, {
    createElement: () => ({click() {links.push(this.href);}, remove() {}}),
    body: {appendChild() {}},
  });
  const expected = `https://kiwi.example/characters/${cid}`;
  assert.equal(api.API, expected, path);
  assert.equal(api.CHARACTER_ID, cid, path);
  await api.request('/debug/memories', {method: 'POST', body: {content: 'sentinel'}});
  assert.equal(calls[0].url, expected + '/debug/memories', path);
  api.download('/sync/export');
  assert.deepEqual(links, [expected + '/sync/export'], path);
  await new Promise((resolve, reject) => {
    api.sse('/dream/start', {}, () => {}, {onDone: resolve, onError: reject});
  });
  assert.equal(calls[1].url, expected + '/dream/start', path);
}
for (const path of [
  '/characters/%/admin/', '/characters/%ZZ/admin/', '/characters/%FF/admin/',
  '/characters/%20/admin/', '/characters/%E8%A7%92%E8%89%B2/admin/',
  '/characters/A%2FB/admin/', '/characters/A%5CB/admin/',
  '/characters%2FA/admin/',
  '/characters/A%3FB/admin/', '/characters/A%23B/admin/',
  '/characters/%2541/admin/', '/%2563haracters/A/admin/',
  '/characters', '/characters/', '/characters//admin/',
  '/characters/admin/', '/characters/A', '/characters/A/',
  '/characters/' + 'A'.repeat(129) + '/admin/',
]) {
  let requests = 0;
  assert.throws(() => load(location(path), () => {requests++;}), /invalid_character_panel_path/, path);
  assert.equal(requests, 0, path + ' must not send a default request');
}
assert.equal(resolve(location('/admin')), 'https://kiwi.example');
assert.equal(resolve(location('/%61dmin/')), 'https://kiwi.example');
console.log('PASS: encoded roles retain request/download/SSE identity; invalid panel URLs fail closed');

const app = readFileSync(new URL('../admin-panel/js/app.js', import.meta.url), 'utf8');
const roleHeader = app.slice(app.indexOf('const activeCharacter'), app.indexOf('const REMOVED ='));
for (const path of ['/admin/', '/characters/A/admin/', '/characters/B/admin/', ...encodedPanels.map(([path]) => path)]) {
  const { CHARACTER_ID } = load(location(path));
  const subtitle = {textContent: 'Kiwi-Mem'};
  const links = [];
  const document = {
    title: 'Kiwi-Mem',
    querySelector: selector => selector === '.sidebar-head .sub' ? subtitle : {append: link => links.push(link)},
    createElement: () => ({}),
  };
  new Function('document', 'CHARACTER_ID', roleHeader)(document, CHARACTER_ID);
  assert.equal(document.title, CHARACTER_ID ? `Kiwi-Mem · ${CHARACTER_ID}` : 'Kiwi-Mem', path);
  assert.equal(subtitle.textContent, CHARACTER_ID ? `角色：${CHARACTER_ID}` : 'Kiwi-Mem', path);
  assert.deepEqual(links, CHARACTER_ID ? [{href: '/character-manager', textContent: '角色管理'}] : [], path);
}
console.log('PASS: panel title and management link share the normalized API identity');

const wizard = readFileSync(new URL('../admin-panel/js/wizard.js', import.meta.url), 'utf8');
const render = wizard.slice(wizard.indexOf('function renderStep3'));
for (const path of ['/characters/A/admin/', '/characters/B/admin/', '/characters/%41/admin/', '/%63haracters/%42/admin/']) {
  const API = resolve(location(path));
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
