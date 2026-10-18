import assert from 'node:assert/strict';
import { test, after } from 'node:test';
import { createServer } from 'node:http';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { ModelFastPreference } from '../extension/src/model-fast-preference.ts';
import { installLunaRequestDefault } from '../extension/src/luna-request-default.ts';

import {
  setupTestWorkspace,
  extensionEntry,
  loadHostRunner,
  loadSubagentRunner,
} from './desktop-fixture.mjs';

const workspace = setupTestWorkspace();
const scratch = workspace.scratchDir;
after(() => workspace.cleanup());

const entry = process.env.FAST_TEST_EXTENSION_ENTRY ?? extensionEntry;
const luna = { provider: 'custom', id: 'gpt-6-luna', api: 'openai-responses' };
const sol = { provider: 'custom', id: 'gpt-6.1-sol', api: 'openai-responses' };

async function desktopSession(TrustedExtensionRunner, initialModel, cwd = process.cwd()) {
  let model = initialModel;
  const ui = [];
  const runner = new TrustedExtensionRunner({
    specs: [{ id: 'luna-policy-test', entry, pluginId: 'imported.pi-openai-fast', label: 'Fast' }],
    bridge: {
      sessionId: 'fast-policy-test', cwd,
      getModel: () => model, getThinkingLevel: () => 'off', isIdle: () => true,
      getContextUsage: () => ({ tokens: 0, contextWindow: 128000, percent: 0 }),
      getSystemPrompt: () => '', getActiveTools: () => [], getAllTools: () => [],
      hasPendingMessages: () => false, getSessionName: () => '',
      publishCommands: () => {}, publishDiagnostics: () => {},
      requestUi: async (_spec, request) => { ui.push(request); return {}; },
    },
  });
  const reports = await runner.load();
  assert.equal(reports[0].state, 'loaded');
  return {
    runner, ui,
    select: next => { model = next; },
    tier: async () => {
      const payload = { model: model.id, input: [] };
      const result = await runner.emit('before_provider_request', { type: 'before_provider_request', payload }, (_p, n) => n);
      return (result ?? payload).service_tier ?? null;
    },
  };
}

test('Luna defaults on while model-specific manual choices stay isolated', () => {
  const policy = new ModelFastPreference(false);
  assert.equal(policy.desiredFor(luna), true);
  assert.equal(policy.desiredFor(sol), false);
  policy.remember(luna, false);
  assert.equal(policy.desiredFor(luna), false);
  policy.remember(sol, true);
  assert.equal(policy.desiredFor(sol), true);
  assert.equal(new ModelFastPreference(false).desiredFor(luna), true);
});

test('Desktop main session handles silent model switches and remembers manual Luna off', async (t) => {
  const TrustedExtensionRunner = await loadHostRunner(t);
  if (!TrustedExtensionRunner) return;
  const previous = process.env.PI_OPENAI_FAST_DESIRED;
  process.env.PI_OPENAI_FAST_DESIRED = '0';
  const session = await desktopSession(TrustedExtensionRunner, sol);
  try {
    assert.equal(await session.tier(), null);
    session.select(luna);
    assert.equal(await session.tier(), 'priority');
    assert.ok(session.ui.some(r => r.kind === 'setStatus' && r.text === 'fast'));
    await session.runner.runCommand('fast', '');
    assert.equal(await session.tier(), 'default');
    session.select(sol);
    assert.equal(await session.tier(), null);
    await session.runner.runCommand('fast', '');
    assert.equal(await session.tier(), 'priority');
    session.select(luna);
    assert.equal(await session.tier(), 'default');
    await session.runner.runCommand('fast', '');
    assert.equal(await session.tier(), 'priority');
    session.select(sol);
    assert.equal(await session.tier(), 'priority');
    const failures = session.runner.getDiagnostics().filter(d => /error|timeout/.test(d.kind));
    assert.deepEqual(failures, []);
  } finally {
    await session.runner.dispose();
    if (previous === undefined) delete process.env.PI_OPENAI_FAST_DESIRED;
    else process.env.PI_OPENAI_FAST_DESIRED = previous;
  }
});

test('Fresh Luna session starts on even with inherited off and a different workspace', async (t) => {
  const TrustedExtensionRunner = await loadHostRunner(t);
  if (!TrustedExtensionRunner) return;
  const cwd = join(scratch, 'luna-other-workspace');
  await mkdir(join(cwd, '.pi/extensions'), { recursive: true });
  await writeFile(join(cwd, '.pi/extensions/pi-openai-fast.json'), JSON.stringify({
    persistState: false, desiredActive: false, supportedModels: [], footer: { mode: 'status' },
  }));
  const previous = process.env.PI_OPENAI_FAST_DESIRED;
  process.env.PI_OPENAI_FAST_DESIRED = '0';
  const session = await desktopSession(TrustedExtensionRunner, luna, cwd);
  try { assert.equal(await session.tier(), 'priority'); }
  finally {
    await session.runner.dispose();
    if (previous === undefined) delete process.env.PI_OPENAI_FAST_DESIRED;
    else process.env.PI_OPENAI_FAST_DESIRED = previous;
  }
});

test('Process compatibility bridge preserves other requests, explicit off, and lifecycle cleanup', async () => {
  const original = globalThis.fetch;
  const captured = [];
  globalThis.fetch = async (input, init) => {
    const request = input instanceof Request ? input : new Request(input, init);
    captured.push({ body: await request.text(), method: request.method, header: request.headers.get('x-test') });
    return new Response('OK');
  };
  const probeFetch = globalThis.fetch;
  const release1 = installLunaRequestDefault();
  const release2 = installLunaRequestDefault();
  try {
    await fetch('http://127.0.0.1/v1/responses', { method: 'POST', body: JSON.stringify({ model: luna.id }) });
    assert.equal(JSON.parse(captured.at(-1).body).service_tier, 'priority');
    await fetch(new Request('http://127.0.0.1/v1/chat/completions', { method: 'POST', headers: { 'x-test': 'retained' }, body: JSON.stringify({ model: luna.id }) }));
    assert.equal(JSON.parse(captured.at(-1).body).service_tier, 'priority');
    assert.equal(captured.at(-1).header, 'retained');
    for (const body of [{ model: sol.id }, { model: luna.id, service_tier: 'default' }, { model: luna.id, service_tier: 'flex' }]) {
      await fetch('http://127.0.0.1/v1/responses', { method: 'POST', body: JSON.stringify(body) });
      assert.deepEqual(JSON.parse(captured.at(-1).body), body);
    }
    await fetch('http://127.0.0.1/unrelated', { method: 'POST', body: JSON.stringify({ model: luna.id }) });
    assert.equal(JSON.parse(captured.at(-1).body).service_tier, undefined);
    release1();
    assert.notEqual(globalThis.fetch, probeFetch);
    release2();
    assert.equal(globalThis.fetch, probeFetch);
  } finally { release1(); release2(); globalThis.fetch = original; }
});

test('Actual Desktop built-in subagent sends Luna priority while Sol remains ordinary', { timeout: 30000 }, async (t) => {
  const TrustedExtensionRunner = await loadHostRunner(t);
  if (!TrustedExtensionRunner) return;
  const runnerHelper = await loadSubagentRunner(t, scratch);
  if (!runnerHelper) return;
  const { DesktopSubagentRunner, cleanup } = runnerHelper;
  const requests = [];
  const server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk.toString();
    const payload = JSON.parse(body);
    requests.push({ model: payload.model, tier: payload.service_tier ?? null });
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    const base = { id: 'local-child', object: 'chat.completion.chunk', created: 1, model: payload.model };
    for (const choice of [{ index: 0, delta: { role: 'assistant', content: 'CHILD_OK' }, finish_reason: null }, { index: 0, delta: {}, finish_reason: 'stop' }]) {
      res.write(`data: ${JSON.stringify({ ...base, choices: [choice] })}\n\n`);
    }
    res.end('data: [DONE]\n\n');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const session = await desktopSession(TrustedExtensionRunner, sol);
  try {
    for (const [modelId, expectedTier] of [[luna.id, 'priority'], [sol.id, null]]) {
      const runner = new DesktopSubagentRunner({
        provider: { id: 'local-probe', name: 'Local probe', vendorKey: 'custom', authKind: 'none',
          apiStyle: 'chat_completions', baseUrl: `http://127.0.0.1:${server.address().port}/v1`, modelId,
          modelConfig: { api: 'openai-completions', input: ['text'], reasoning: false,
            contextWindow: 128000, maxTokens: 128, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } } },
        thinkingLevel: 'off', definition: { name: 'test-child', maxTokens: 128 }, tools: [],
        task: 'Reply CHILD_OK.', systemPrompt: 'Reply CHILD_OK.', sessionId: 'local-child-' + modelId,
        turnId: 'local-turn', parentToolCallId: 'local-tool', onEvent: () => {}, signal: AbortSignal.timeout(10000),
      });
      const result = await runner.run();
      assert.equal(result.status, 'completed', JSON.stringify(result));
      assert.match(result.report, /CHILD_OK/);
      assert.deepEqual(requests.at(-1), { model: modelId, tier: expectedTier });
    }
  } finally {
    await session.runner.dispose();
    await new Promise(resolve => server.close(resolve));
    await cleanup();
  }
});
