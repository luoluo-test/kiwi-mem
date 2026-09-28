#!/usr/bin/env node
// Execute the real inline module with a minimal DOM and mocked HTTP responses.
// This checks UI behavior, not real browser layout or PostgreSQL deletion.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';

const html = await fs.readFile(process.argv[2] || new URL('../admin-panel/characters.html', import.meta.url), 'utf8');
const source = html.match(/<script type="module">([\s\S]*?)<\/script>/)?.[1];
assert.ok(source, 'character manager inline module missing');

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attributes = new Map();
    this.disabled = false; this.dataset = {}; this._text = ''; this.href = '';
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(v => typeof v === 'string' ? v : v.textContent).join(''); }
  set innerHTML(_) { throw new Error('role names must never be inserted as HTML'); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ''; this.children = nodes; }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  removeAttribute(key) { this.attributes.delete(key); if (key === 'href') this.href = ''; }
  querySelectorAll(selector) {
    return this.children.flatMap(child => typeof child === 'string' ? [] : [
      ...(selector.split(',').map(s => s.trim()).includes(child.tagName) ? [child] : []),
      ...child.querySelectorAll(selector),
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  reset() { this.fields = {}; this.wasReset = true; }
}
const fixture = (id, state = 'active', name = id) => ({ id, state, name });
const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const invalidJSON = (status = 502) => ({ ok: status < 400, status, json: async () => { throw new SyntaxError('Unexpected token <'); } });
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }
const tick = () => new Promise(resolve => setImmediate(resolve));

async function boot(initial = [fixture('default'), fixture('A'), fixture('B')]) {
  const nodes = { '#status': new Element('p'), '#roles': new Element('section'), '#create': new Element('form') };
  nodes['#create'].append(new Element('button'));
  const app = { nodes, calls: [], prompts: [], answers: [], rows: initial, handler: null };
  const context = vm.createContext({
    document: { querySelector: selector => nodes[selector], createElement: tag => new Element(tag) },
    fetch: async (path, options) => {
      const call = { path, options }; app.calls.push(call);
      if (app.handler) { const result = await app.handler(call); if (result !== undefined) return result; }
      if (path === '/characters' && !options?.method) return response({ characters: app.rows });
      return response({ status: 'ok' });
    },
    prompt: (...args) => { app.prompts.push(args); return app.answers.shift() ?? null; },
    FormData: class { constructor(form) { return Object.entries(form.fields || {}); } },
    console,
  });
  vm.runInContext(source, context, { filename: 'admin-panel/characters.html' });
  await tick();
  app.cards = () => nodes['#roles'].querySelectorAll('article');
  app.card = id => app.cards().find(card => card.querySelector('strong')?.textContent.includes(` · ${id} · `));
  app.button = (id, text = '永久删除') => app.card(id)?.querySelectorAll('button').find(button => button.textContent.includes(text));
  app.purgeCalls = () => app.calls.filter(call => call.path.endsWith('/purge'));
  app.refresh = () => vm.runInContext('refresh()', context);
  return app;
}
function deleteButton(app, id = 'A', text) {
  const button = app.button(id, text);
  assert.ok(button, `missing permanent-delete action for ${id}`);
  return button;
}
const cases = [];
const test = (name, run) => cases.push({ name, run });

test('active and disabled roles are deletable; default is protected; deleted is hidden', async () => {
  const app = await boot([fixture('default'), fixture('A'), fixture('off', 'disabled'), fixture('gone', 'deleted'), fixture('new', 'provisioning')]);
  deleteButton(app); deleteButton(app, 'off');
  assert.equal(app.button('default'), undefined);
  assert.equal(app.button('new'), undefined);
  assert.equal(app.card('gone'), undefined);
  assert.match(app.card('default').textContent, /不可删除/);
});
test('deleting roles expose retry without opening or rename', async () => {
  const app = await boot([{ ...fixture('A', 'deleting'), deletion_error: 'character_purge_failed' }]);
  deleteButton(app, 'A', '重试');
  assert.equal(app.card('A').querySelector('a'), null);
  assert.equal(app.button('A', '改名'), undefined);
});
test('cancel confirmation sends no purge request', async () => {
  const app = await boot(); app.answers.push(null);
  await deleteButton(app).onclick();
  assert.equal(app.purgeCalls().length, 0);
});
test('wrong ID, whitespace, and case mismatch send no purge request', async () => {
  const app = await boot();
  for (const answer of ['', 'a', ' A ', 'B']) { app.answers.push(answer); await deleteButton(app).onclick(); }
  assert.equal(app.purgeCalls().length, 0);
  assert.match(app.nodes['#status'].textContent, /不匹配|不一致/);
});
test('confirmation gives complete scope and names are always text', async () => {
  const name = '<img src=x onerror=alert(1)>';
  const app = await boot([fixture('A', 'active', name)]);
  assert.match(app.card('A').textContent, /<img src=x onerror=alert\(1\)>/);
  app.answers.push(null); await deleteButton(app).onclick();
  const message = app.prompts[0][0];
  for (const text of [name, 'A', '记忆', '向量', '聊天', '项目', '画像', '日历', 'Dream', '配置', '不可恢复', '源码', '备份']) assert.ok(message.includes(text), `confirmation missing ${text}`);
  assert.match(message, /外部备份.*不/);
});
test('pending purge locks only its card and sends the exact request once', async () => {
  const app = await boot(); const pending = deferred();
  app.handler = call => call.path.endsWith('/purge') ? pending.promise : undefined;
  const original = deleteButton(app); const oldLink = app.card('A').querySelector('a'); app.answers.push('A', 'A');
  const operation = original.onclick(); await tick();
  assert.ok(app.card('A'), 'card disappeared before server confirmed deletion');
  assert.ok(app.card('A').querySelectorAll('button').every(button => button.disabled));
  const link = app.card('A').querySelector('a');
  assert.ok(!link || !link.href, 'pending role panel link must not navigate');
  let prevented = false; oldLink.onclick({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true, 'an old panel link handler must also honor the pending lock');
  assert.equal(deleteButton(app, 'B').disabled, false);
  await original.onclick();
  assert.equal(app.purgeCalls().length, 1);
  const call = app.purgeCalls()[0];
  assert.equal(call.path, '/characters/A/purge');
  assert.equal(call.options.method, 'POST');
  assert.equal(call.options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(call.options.body), { confirm_character_id: 'A' });
  app.rows = app.rows.filter(row => row.id !== 'A');
  pending.resolve(response({ status: 'deleted', data_retained: false }));
  await operation;
  assert.equal(app.card('A'), undefined); assert.ok(app.card('B'));
  assert.match(app.nodes['#status'].textContent, /已永久删除/);
});
test('refresh during purge preserves pending lock', async () => {
  const app = await boot(); const pending = deferred();
  app.handler = call => call.path.endsWith('/purge') ? pending.promise : undefined;
  app.answers.push('A'); const operation = deleteButton(app).onclick(); await tick();
  await app.refresh();
  assert.ok(app.card('A').querySelectorAll('button').every(button => button.disabled));
  pending.resolve(response({ code: 'character_purge_failed' }, 503)); await operation;
  assert.equal(deleteButton(app).disabled, false);
});
test('failed purge refreshes actual deleting state and retry succeeds', async () => {
  const app = await boot();
  app.handler = call => {
    if (!call.path.endsWith('/purge')) return;
    app.rows = [fixture('default'), { ...fixture('A', 'deleting'), deletion_error: 'character_purge_failed' }];
    return response({ code: 'character_purge_failed' }, 503);
  };
  app.answers.push('A'); await deleteButton(app).onclick();
  assert.match(app.nodes['#status'].textContent, /删除.*失败|未完成/);
  assert.ok(app.card('A')); const retry = deleteButton(app, 'A', '重试'); assert.equal(retry.disabled, false);
  app.handler = call => {
    if (!call.path.endsWith('/purge')) return;
    app.rows = [fixture('default')]; return response({ status: 'deleted', data_retained: false });
  };
  app.answers.push('A'); await retry.onclick();
  assert.equal(app.purgeCalls().length, 2); assert.equal(app.card('A'), undefined);
});
test('network failure leaves a retryable card with a human readable message', async () => {
  const app = await boot();
  app.handler = call => { if (call.path.endsWith('/purge')) throw new TypeError('Failed to fetch'); };
  app.answers.push('A'); await deleteButton(app).onclick();
  assert.equal(deleteButton(app).disabled, false);
  assert.match(app.nodes['#status'].textContent, /网络|连接/);
});
test('non-JSON failure reports HTTP status and never removes a role', async () => {
  const app = await boot(); app.handler = call => call.path.endsWith('/purge') ? invalidJSON() : undefined;
  app.answers.push('A'); await deleteButton(app).onclick();
  assert.ok(app.card('A')); assert.match(app.nodes['#status'].textContent, /502/);
  assert.doesNotMatch(app.nodes['#status'].textContent, /Unexpected token/);
});
test('malformed success cannot be mistaken for completed deletion', async () => {
  const app = await boot(); app.handler = call => call.path.endsWith('/purge') ? response({ status: 'disabled', data_retained: true }) : undefined;
  app.answers.push('A'); await deleteButton(app).onclick();
  assert.ok(app.card('A')); assert.doesNotMatch(app.nodes['#status'].textContent, /已永久删除/);
});
test('confirmed deletion stays removed when follow-up refresh fails', async () => {
  const app = await boot();
  app.handler = call => call.path.endsWith('/purge') ? response({ status: 'deleted', data_retained: false }) : invalidJSON();
  app.answers.push('A'); await deleteButton(app).onclick();
  assert.equal(app.card('A'), undefined);
  assert.match(app.nodes['#status'].textContent, /已永久删除/);
  assert.match(app.nodes['#status'].textContent, /列表|刷新/);
});
test('failure plus failed refresh retains card and unlocks retry', async () => {
  const app = await boot(); app.handler = () => invalidJSON();
  app.answers.push('A'); await deleteButton(app).onclick();
  assert.equal(deleteButton(app).disabled, false);
  assert.match(app.nodes['#status'].textContent, /刷新/);
});
test('create keeps its existing request contract', async () => {
  const app = await boot(); const form = app.nodes['#create']; form.fields = { id: 'new_A', name: '新角色' };
  await form.onsubmit({ preventDefault() {}, target: form });
  const call = app.calls.find(call => call.options?.method === 'POST');
  assert.equal(call.path, '/characters'); assert.deepEqual(JSON.parse(call.options.body), { id: 'new_A', name: '新角色' });
  assert.equal(form.wasReset, true); assert.equal(form.querySelector('button').disabled, false);
});
test('rename keeps its existing request contract and blocks a stale handler during purge', async () => {
  const app = await boot(); const rename = app.button('A', '改名'); app.answers.push('新名字'); await rename.onclick();
  const call = app.calls.find(call => call.options?.method === 'PATCH');
  assert.equal(call.path, '/characters/A'); assert.deepEqual(JSON.parse(call.options.body), { name: '新名字' });
  const pending = deferred(); app.handler = call => call.path.endsWith('/purge') ? pending.promise : undefined;
  app.answers.push('A', '不可改名'); const operation = deleteButton(app).onclick(); await tick(); await rename.onclick();
  assert.equal(app.calls.filter(call => call.options?.method === 'PATCH').length, 1);
  pending.resolve(response({ code: 'character_purge_failed' }, 503)); await operation;
});
test('older list response cannot overwrite a newer refresh', async () => {
  const app = await boot(); const first = deferred(); let gets = 0;
  app.handler = call => call.path === '/characters' && !call.options?.method ? (++gets === 1 ? first.promise : response({ characters: [fixture('B')] })) : undefined;
  const old = app.refresh(); await tick(); await app.refresh();
  first.resolve(response({ characters: [fixture('A')] })); await old;
  assert.equal(app.card('A'), undefined); assert.ok(app.card('B'));
});

let failed = 0;
for (const { name, run } of cases) {
  try { await run(); console.log(`PASS ${name}`); }
  catch (error) { failed++; console.error(`FAIL ${name}: ${error.message}`); }
}
console.log(`${cases.length - failed}/${cases.length} character manager DOM guards passed (mock HTTP; no live database)`);
process.exitCode = failed ? 1 : 0;
