import assert from 'node:assert/strict';
import { test, after } from 'node:test';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { parseFastCommandArgs } from '../extension/src/fast-command-parser.ts';
import { executeFastCommand } from '../extension/src/fast-command.ts';
import { FastStateEngine } from '../extension/src/fast-state-engine.ts';
import { FastController } from '../renderer/fast-controller.mjs';
import { FastControl, onLoad, onUnload } from '../renderer/index.mjs';
import { contextFromDesktopState, loadDesktopStore } from '../renderer/desktop-context.mjs';
import { DesktopFastPreference } from '../extension/src/desktop-fast-preference.ts';
import {
  setupTestWorkspace,
  packageRootPosix,
  loadHostRunner,
  loadRendererBundle,
} from './desktop-fixture.mjs';

const workspace = setupTestWorkspace();
const scratch = workspace.scratchDir;
after(() => workspace.cleanup());
const require = createRequire(import.meta.url);
const { createPreferenceStore, callBridge } = require('../desktop-bridge.cjs');
const backend = require('../main.cjs');
const sol = { provider: 'custom', id: 'gpt-6.1-sol' };
const context = { ready: true, sessionId: 'fast-control-unit-session', modelKey: 'custom/gpt-6.1-sol', cwd: process.cwd() };
const policyContext = { ...context, supported: true, defaultDesired: false };
const fakeReact = {
  createElement: (type, props, ...children) => ({ type, props: props ?? {}, children }),
  useSyncExternalStore: (_subscribe, snapshot) => snapshot(),
};
const button = controller => FastControl({ React: fakeReact, controller }).children[0];

function commandDependencies() {
  const writes = [];
  const notices = [];
  return { writes, notices, dependencies: {
    stateEngine: new FastStateEngine({ desiredActive: false, supportedModels: [context.modelKey], currentModel: sol }),
    config: { persistState: false }, currentModel: sol,
    saveDesiredActive: async desired => { writes.push(['save', desired]); return true; },
    writeFastDesiredHandoff: desired => writes.push(['handoff', desired]),
    notify: (message, type) => notices.push({ message, type }),
  } };
}

test('Explicit commands are idempotent, status is read-only, and success is visible', async () => {
  assert.deepEqual(parseFastCommandArgs(' ON '), { kind: 'set', desiredActive: true });
  assert.deepEqual(parseFastCommandArgs('off'), { kind: 'set', desiredActive: false });
  assert.equal(parseFastCommandArgs('status').kind, 'status');
  const { writes, notices, dependencies } = commandDependencies();
  await executeFastCommand('on', dependencies);
  await executeFastCommand('on', dependencies);
  assert.equal(dependencies.stateEngine.snapshot().active, true);
  const before = writes.length;
  const state = dependencies.stateEngine.snapshot();
  assert.equal((await executeFastCommand('status', dependencies)).kind, 'status');
  assert.deepEqual(dependencies.stateEngine.snapshot(), state);
  assert.equal(writes.length, before);
  assert.match(notices.at(-1).message, /已开启/);
  await executeFastCommand('off', dependencies);
  await executeFastCommand('off', dependencies);
  assert.equal(dependencies.stateEngine.snapshot().active, false);
  assert.match(notices.at(-1).message, /已关闭/);
  await executeFastCommand('', dependencies);
  assert.equal(dependencies.stateEngine.snapshot().active, true);
  await executeFastCommand('invalid args', dependencies);
  assert.equal(dependencies.stateEngine.snapshot().active, true);
});

test('A failed shared-state update does not toggle, persist, or report success', async () => {
  const { dependencies, writes, notices } = commandDependencies();
  dependencies.applyDesiredActive = async () => { throw new Error('Disconnected'); };
  const result = await executeFastCommand('on', dependencies);
  assert.equal(result.kind, 'error');
  assert.equal(result.state.active, false);
  assert.deepEqual(writes, []);
  assert.match(notices.at(-1).message, /切换失败/);
});

test('Memory preferences isolate sessions and models and reset without disk state', () => {
  const store = createPreferenceStore();
  assert.equal(store.read(policyContext).active, false);
  store.set(policyContext, true);
  assert.equal(store.read(policyContext).active, true);
  assert.equal(store.read({ ...policyContext, sessionId: 'other' }).active, false);
  const luna = { ...policyContext, modelKey: 'custom/gpt-6-luna', defaultDesired: true };
  assert.equal(store.read(luna).active, true);
  store.set(luna, false);
  assert.equal(store.read(luna).active, false);
  assert.equal(store.read(policyContext).active, true);
  assert.throws(() => store.set({ ...policyContext, supported: false }, true), /支持/);
  store.clear();
  assert.equal(store.read(policyContext).active, false);
  assert.equal(store.read(luna).active, true);
});

test('Read-only context adapter selects the model vendor key and rejects incomplete or remote contexts', async () => {
  const state = { activeSessionId: context.sessionId, sessions: [{ id: context.sessionId, providerId: 'provider-uuid', modelId: sol.id, projectPath: context.cwd }],
    providers: [{ id: 'provider-uuid', vendorKey: 'custom' }], settings: {} };
  assert.deepEqual(contextFromDesktopState(state), context);
  assert.equal(contextFromDesktopState({ ...state, activeSessionId: undefined }).ready, false);
  assert.equal(contextFromDesktopState({ ...state, selectingSessionId: context.sessionId }).ready, false);
  assert.equal(contextFromDesktopState({ ...state, sessions: [{ ...state.sessions[0], source: 'remote' }] }).ready, false);
  const store = { getState: () => state, subscribe: () => () => {} };
  const documentRef = { baseURI: 'file:///C:/desktop/index.html', querySelectorAll: () => [{ src: 'file:///C:/desktop/assets/index-fixture.js' }] };
  assert.equal(await loadDesktopStore(documentRef, async () => ({ store })), store);
  await assert.rejects(loadDesktopStore({ ...documentRef, querySelectorAll: () => [] }), /接口/);
});

test('Button confirms a change only after success, prevents double clicks, and exposes errors', async () => {
  let resolveSet, failSet = false;
  const store = createPreferenceStore();
  const dispatch = async (_action, payload) => {
    if (payload.method === 'fast.read') return store.read(policyContext);
    if (failSet) throw new Error('LOCAL_FAIL');
    await new Promise(resolve => { resolveSet = resolve; });
    return store.set(policyContext, payload.args.desiredActive);
  };
  const controller = new FastController(dispatch);
  await controller.setContext(context);
  assert.equal(button(controller).props['aria-pressed'], false);
  const pending = controller.toggle();
  assert.equal(button(controller).props['aria-busy'], true);
  assert.equal(button(controller).props.disabled, true);
  assert.equal(button(controller).props['aria-pressed'], false);
  await controller.toggle();
  resolveSet(); await pending;
  assert.equal(button(controller).props['aria-pressed'], true);
  failSet = true; await controller.toggle();
  assert.equal(button(controller).props['aria-pressed'], false);
  assert.match(controller.snapshot().error, /LOCAL_FAIL/);
  assert.equal(FastControl({ React: fakeReact, controller }).children[1].props.role, 'alert');
  controller.dispose();
});

test('Late replies cannot overwrite a new session, and disposal stops updates', async () => {
  let finishOld;
  const store = createPreferenceStore();
  const controller = new FastController(async (_action, payload) => {
    if (payload.args.context.sessionId === context.sessionId) return new Promise(resolve => { finishOld = resolve; });
    return store.read({ ...policyContext, sessionId: 'new-session' });
  });
  const old = controller.setContext(context);
  await controller.setContext({ ...context, sessionId: 'new-session' });
  finishOld(store.set(policyContext, true)); await old;
  assert.equal(controller.snapshot().data.sessionId, 'new-session');
  assert.equal(controller.snapshot().data.active, false);
  controller.dispose();
  const snapshot = controller.snapshot();
  await controller.refresh();
  assert.equal(controller.snapshot(), snapshot);
});

test('Actual Desktop slot registry accepts the renderer and renderer/backend state is shared through IPC', async (t) => {
  const source = await loadRendererBundle(t);
  if (!source) return;
  const validation = source.slice(source.indexOf('const DH='), source.indexOf('let N0=0;'));
  const registrySource = source.slice(source.indexOf('function g9('), source.indexOf('const b9=new y9'));
  const registry = runInNewContext(validation + registrySource + ';new y9()', {});
  assert.match(source.slice(source.lastIndexOf('export{')), /ee as P/);
  const testRoot = join(scratch, 'fast-renderer-fixture');
  await mkdir(join(testRoot, 'assets'), { recursive: true });
  const fixture = join(testRoot, 'assets/index-fast-fixture.js');
  await writeFile(fixture, 'export const store = globalThis.__FAST_TEST_STORE__;');
  const listeners = new Set();
  const state = { activeSessionId: context.sessionId, sessions: [{ id: context.sessionId, providerId: 'uuid', modelId: sol.id, projectPath: context.cwd }], providers: [{ id: 'uuid', vendorKey: 'custom' }] };
  globalThis.__FAST_TEST_STORE__ = { getState: () => state, subscribe: listener => { listeners.add(listener); return () => listeners.delete(listener); } };
  globalThis.document = { baseURI: pathToFileURL(join(testRoot, 'index.html')).href, querySelectorAll: () => [{ src: pathToFileURL(fixture).href }] };
  globalThis.__PI_RENDERER_HOST__ = { react: fakeReact };
  const styles = new Set();
  await backend.onLoad();
  try {
    await onLoad({ ui: { injectStyle: style => { styles.add(style); return () => styles.delete(style); } },
      slots: { register: registration => registry.register('imported.pi-openai-fast', registration, []) },
      dispatch: async (_action, payload) => backend.onRendererCall(payload.method, payload.args),
    });
    const entry = registry.entriesForSide('composerControl', 'right')[0];
    assert.ok(entry);
    assert.equal(registry.entriesForSide('composerControl', 'left').length, 0);
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(entry.component().children[0].props['aria-pressed'], false);
    await entry.component().children[0].props.onClick();
    assert.equal(entry.component().children[0].props['aria-pressed'], true);
    assert.equal((await callBridge({ method: 'read', context: policyContext })).active, true);
    const client = new DesktopFastPreference();
    assert.equal(await client.read(policyContext), true);
    await client.set(policyContext, false);
    assert.equal((await backend.onRendererCall('fast.read', { context })).active, false);
    const configBefore = await readFile(resolve('.pi/extensions/pi-openai-fast.json'), 'utf8');
    await backend.onRendererCall('fast.set', { context, desiredActive: true });
    assert.equal(await readFile(resolve('.pi/extensions/pi-openai-fast.json'), 'utf8'), configBefore);
    onUnload();
    assert.equal(registry.entriesFor('composerControl').length, 0);
    assert.equal(styles.size, 0);
    assert.equal(listeners.size, 0);
    await backend.onUnload();
    await assert.rejects(client.read(policyContext));
    await backend.onLoad();
    assert.equal((await backend.onRendererCall('fast.read', { context })).active, false);
  } finally {
    onUnload(); await backend.onUnload();
    delete globalThis.__FAST_TEST_STORE__; delete globalThis.__PI_RENDERER_HOST__; delete globalThis.document;
  }
});

test('Command-side changes clear outdated button feedback and success feedback expires', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const store = createPreferenceStore();
  const controller = new FastController(async (_action, payload) => payload.method === 'fast.read'
    ? store.read(policyContext) : store.set(policyContext, payload.args.desiredActive));
  try {
    await controller.setContext(context);
    await controller.toggle();
    assert.match(controller.snapshot().message, /已开启/);
    store.set(policyContext, false);
    await controller.refresh();
    assert.equal(button(controller).props['aria-pressed'], false);
    assert.equal(controller.snapshot().message, '');
    await controller.toggle();
    t.mock.timers.tick(3000);
    assert.equal(controller.snapshot().message, '');
    assert.equal(button(controller).props['aria-pressed'], true);
  } finally { controller.dispose(); }
});

test('Unloading during asynchronous store discovery leaves no subscriptions or controls', async () => {
  const fixtureRoot = join(scratch, 'fast-unload-fixture');
  await mkdir(join(fixtureRoot, 'assets'), { recursive: true });
  const fixture = join(fixtureRoot, 'assets/index-unload-fixture.js');
  await writeFile(fixture, 'export const store = await globalThis.__FAST_DELAYED_STORE__;');
  let resolveStore, subscriptions = 0, calls = 0;
  globalThis.__FAST_DELAYED_STORE__ = new Promise(resolve => { resolveStore = resolve; });
  globalThis.document = { baseURI: pathToFileURL(join(fixtureRoot, 'index.html')).href, querySelectorAll: () => [{ src: pathToFileURL(fixture).href }] };
  globalThis.__PI_RENDERER_HOST__ = { react: fakeReact };
  const styles = new Set(), slots = new Set();
  try {
    const loading = onLoad({
      ui: { injectStyle: style => { styles.add(style); return () => styles.delete(style); } },
      slots: { register: slot => { slots.add(slot); return () => slots.delete(slot); } },
      dispatch: async () => { calls += 1; },
    });
    onUnload();
    resolveStore({ getState: () => ({ sessions: [], providers: [], activeSessionId: null }), subscribe: () => { subscriptions += 1; return () => { subscriptions -= 1; }; } });
    await loading;
    assert.equal(styles.size, 0);
    assert.equal(slots.size, 0);
    assert.equal(subscriptions, 0);
    assert.equal(calls, 0);
  } finally {
    onUnload();
    delete globalThis.__FAST_DELAYED_STORE__; delete globalThis.document; delete globalThis.__PI_RENDERER_HOST__;
  }
});

test('Out-of-order model reads preserve the latest model and each request uses its own state', async (t) => {
  const TrustedExtensionRunner = await loadHostRunner(t);
  if (!TrustedExtensionRunner) return;
  const fixture = join(scratch, 'fast-state-race-extension.ts');
  await writeFile(fixture, `
import { registerPiOpenAIFast } from ${JSON.stringify(packageRootPosix + '/extension/src/extension-lifecycle.ts')};
import { DesktopFastPreference } from ${JSON.stringify(packageRootPosix + '/extension/src/desktop-fast-preference.ts')};
import { FastStateEngine } from ${JSON.stringify(packageRootPosix + '/extension/src/fast-state-engine.ts')};
const probe = globalThis.__FAST_STATE_RACE__;
const originalRead = DesktopFastPreference.prototype.read;
DesktopFastPreference.prototype.read = context => probe.read(context);
const engine = new FastStateEngine({ desiredActive: false, supportedModels: ['custom/gpt-6.1-sol'] });
probe.snapshot = () => engine.snapshot();
probe.restore = () => { DesktopFastPreference.prototype.read = originalRead; };
export default pi => registerPiOpenAIFast(pi, { stateEngine: engine });
`);
  const luna = { provider: 'custom', id: 'gpt-6-luna', api: 'openai-responses' };
  let model = { ...sol, api: 'openai-responses' };
  const probe = globalThis.__FAST_STATE_RACE__ = { read: async () => false };
  const runner = new TrustedExtensionRunner({
    specs: [{ id: 'fast-state-race', entry: fixture, pluginId: 'imported.pi-openai-fast', label: 'Fast race' }],
    bridge: {
      sessionId: 'fast-state-race-local', cwd: process.cwd(),
      getModel: () => model, getThinkingLevel: () => 'off', isIdle: () => true,
      getContextUsage: () => ({ tokens: 0, contextWindow: 128000, percent: 0 }),
      getSystemPrompt: () => '', getActiveTools: () => [], getAllTools: () => [],
      hasPendingMessages: () => false, getSessionName: () => '',
      publishCommands: () => {}, publishDiagnostics: () => {}, requestUi: async () => ({}),
    },
  });
  let reads = new Map();
  const beginReads = () => {
    reads = new Map();
    probe.read = context => new Promise(resolve => reads.set(context.modelKey, resolve));
  };
  const awaitReads = async () => {
    for (let attempt = 0; attempt < 30 && reads.size < 2; attempt += 1) await Promise.resolve();
    assert.equal(reads.size, 2);
  };
  try {
    assert.equal((await runner.load())[0].state, 'loaded');
    const initial = { type: 'before_provider_request', payload: { model: sol.id, input: [] } };
    await runner.emit('before_provider_request', initial, (_previous, next) => next);
    beginReads();
    const oldSelection = runner.emit('model_select', { type: 'model_select', model });
    model = luna;
    const newSelection = runner.emit('model_select', { type: 'model_select', model });
    await awaitReads();
    reads.get('custom/gpt-6-luna')(true); await newSelection;
    reads.get('custom/gpt-6.1-sol')(false); await oldSelection;
    assert.deepEqual(probe.snapshot(), { currentModelKey: 'custom/gpt-6-luna', desiredActive: true, active: true });

    beginReads();
    model = { ...sol, api: 'openai-responses' };
    const solPayload = { model: sol.id, input: [] };
    const solRequest = runner.emit('before_provider_request', { type: 'before_provider_request', payload: solPayload }, (_previous, next) => next);
    model = luna;
    const lunaPayload = { model: luna.id, input: [] };
    const lunaRequest = runner.emit('before_provider_request', { type: 'before_provider_request', payload: lunaPayload }, (_previous, next) => next);
    await awaitReads();
    reads.get('custom/gpt-6.1-sol')(true);
    reads.get('custom/gpt-6-luna')(false);
    const [solResult, lunaResult] = await Promise.all([solRequest, lunaRequest]);
    assert.equal((solResult ?? solPayload).service_tier, 'priority');
    assert.equal((lunaResult ?? lunaPayload).service_tier, 'default');
    assert.deepEqual(probe.snapshot(), { currentModelKey: 'custom/gpt-6-luna', desiredActive: false, active: false });
  } finally {
    for (const finish of reads.values()) finish(false);
    await runner.dispose(); probe.restore?.(); delete globalThis.__FAST_STATE_RACE__;
  }
});
