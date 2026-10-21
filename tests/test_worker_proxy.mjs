import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

// Cloudflare's TCP module is unavailable in Node; these tests exercise HTTP only.
const source = (await readFile(new URL('../cloudflare-worker/src/index.js', import.meta.url), 'utf8'))
  .replace('import { connect } from "cloudflare:sockets";', '');
const { default: worker } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
let called = 0;
globalThis.fetch = async (url, init) => {
  called++;
  assert.equal(url, 'https://openrouter.ai/api/v1/chat/completions');
  assert.equal(init.headers.get('Authorization'), 'Bearer test-key');
  assert.equal(init.headers.has('X-Proxy-Token'), false);
  assert.equal(await new Response(init.body).text(), '{"model":"openrouter/free"}');
  return new Response('{"choices":[]}', { status: 200 });
};
const response = await worker.fetch(new Request('https://worker.example/openrouter/api/v1/chat/completions', {
  method: 'POST', headers: { 'X-Proxy-Token': 'secret', Authorization: 'Bearer test-key' },
  body: '{"model":"openrouter/free"}',
}), { PROXY_TOKEN: 'secret' });
assert.equal(response.status, 200);
const denied = await worker.fetch(new Request('https://worker.example/openrouter/api/v1/chat/completions'), { PROXY_TOKEN: 'secret' });
assert.equal(denied.status, 403);
assert.equal(called, 1);
console.log('Worker OpenRouter routing and authorization: OK');
