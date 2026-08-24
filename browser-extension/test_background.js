'use strict';

const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const fetchCalls = [];
let configuredToken = '';
let sessionToken = '';
let sessionResponseToken = 'session-secret';
let apiResponse = () => ({ ok: true, status: 200, async json() { return { status: 'ok' }; } });
const context = {
  console,
  URL,
  fetch: async (url, options = {}) => {
    fetchCalls.push({ url, options });
    if (url.endsWith('/auth/session')) {
      return { ok: true, status: 200, async json() { return { token: sessionResponseToken }; } };
    }
    return apiResponse(url, options);
  },
  chrome: {
    runtime: { id: 'abcdefghijklmnopabcdefghijklmnop', onMessage: { addListener() {} } },
    storage: {
      local: { async get() { return configuredToken ? { singReactorApiToken: configuredToken } : {}; } },
      session: {
        async get() { return sessionToken ? { singReactorApiToken: sessionToken } : {}; },
        async set(value) { sessionToken = value.singReactorApiToken; },
        async remove() { sessionToken = ''; }
      }
    }
  },
  importScripts(file) {
    if (file === 'config.js') {
      context.BLSConfig = Object.freeze({ API_BASE: 'https://api.example.test/service' });
      return;
    }
    const source = fs.readFileSync(`browser-extension/${file}`, 'utf8');
    vm.runInContext(source, context);
  }
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync('browser-extension/background.js', 'utf8'), context);

(async () => {
  const logic = context.BLSBackgroundLogic;
  assert.deepStrictEqual({ ...logic.buildLocalHeaders('') }, { 'Content-Type': 'application/json' });
  assert.deepStrictEqual({ ...logic.buildLocalHeaders('  secret-token  ') }, {
    'Content-Type': 'application/json',
    'X-Sing-Reactor-Token': 'secret-token'
  });

  await logic.localApi('/transcribe/jobs/example', { method: 'GET' });
  assert.strictEqual(fetchCalls[0].url, 'https://api.example.test/service/auth/session');
  assert.strictEqual(fetchCalls[0].options.cache, 'no-store');
  assert.strictEqual(fetchCalls[0].options.headers['X-Sing-Reactor-Extension'], 'abcdefghijklmnopabcdefghijklmnop');
  assert.strictEqual(fetchCalls[1].options.headers['X-Sing-Reactor-Token'], 'session-secret');
  assert.strictEqual(sessionToken, 'session-secret');

  fetchCalls.length = 0;
  sessionResponseToken = 'refreshed-session-secret';
  apiResponse = (url, options) => options.headers['X-Sing-Reactor-Token'] === 'session-secret'
    ? { ok: false, status: 401, async json() { return { detail: 'expired session' }; } }
    : { ok: true, status: 200, async json() { return { status: 'retried' }; } };
  const retried = await logic.localApi('/transcribe/jobs/example', { method: 'GET' });
  assert.strictEqual(retried.status, 'retried');
  assert.strictEqual(fetchCalls.length, 3);
  assert.strictEqual(fetchCalls[0].options.headers['X-Sing-Reactor-Token'], 'session-secret');
  assert.strictEqual(fetchCalls[1].url, 'https://api.example.test/service/auth/session');
  assert.strictEqual(fetchCalls[2].options.headers['X-Sing-Reactor-Token'], 'refreshed-session-secret');
  assert.strictEqual(sessionToken, 'refreshed-session-secret');

  configuredToken = 'configured-secret';
  fetchCalls.length = 0;
  apiResponse = () => ({ ok: false, status: 401, async json() { return { detail: 'configured token rejected' }; } });
  await assert.rejects(
    logic.localApi('/transcribe/jobs/example', { method: 'GET' }),
    /configured token rejected/
  );
  assert.strictEqual(fetchCalls.length, 1);
  assert.strictEqual(fetchCalls[0].options.headers['X-Sing-Reactor-Token'], 'configured-secret');
  assert.strictEqual(sessionToken, 'refreshed-session-secret');

  assert.strictEqual(logic.nextPollDelay(1, 100, 1000), 100);
  assert.strictEqual(logic.nextPollDelay(4, 100, 500), 500);
  assert.strictEqual(logic.shouldRetryJobOperation({ ok: false }, 4, 5), true);
  assert.strictEqual(logic.shouldRetryJobOperation({ ok: false }, 5, 5), false);
  assert.strictEqual(logic.shouldRetryJobOperation({ ok: true }, 1, 5), false);

  const contentSource = fs.readFileSync('browser-extension/content.js', 'utf8');
  const parseTimeSource = contentSource.match(/function parseTime\(raw, allowFraction = true\) \{[\s\S]*?\n  \}/)?.[0];
  assert.ok(parseTimeSource, 'parseTime function should exist');
  const parseContext = {};
  vm.createContext(parseContext);
  vm.runInContext(`${parseTimeSource}; this.parseTime = parseTime;`, parseContext);
  assert.strictEqual(parseContext.parseTime('01:02.50'), 62.5);
  assert.strictEqual(parseContext.parseTime('01:02', false), 62);
  assert.strictEqual(parseContext.parseTime('01:02.5', false), null);
  assert.strictEqual(parseContext.parseTime('01:60'), null);
  assert.ok(!/row\.innerHTML/.test(contentSource));
  assert.ok(/textEl\.textContent = line\.text/.test(contentSource));

  console.log('background and content logic tests passed');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
