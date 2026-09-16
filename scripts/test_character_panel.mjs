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
