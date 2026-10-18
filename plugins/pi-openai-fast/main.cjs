'use strict';
const { createPreferenceStore, startBridge, validateContext } = require('./desktop-bridge.cjs');
const { loadDesktopPolicy } = require('./desktop-policy.cjs');
let preferences;
let closeBridge;

module.exports = {
  async onLoad() {
    preferences = createPreferenceStore();
    closeBridge = await startBridge(preferences);
  },
  async onRendererCall(method, args) {
    if (!preferences || !closeBridge) throw new Error('Fast 状态服务尚未加载');
    if (method !== 'fast.read' && method !== 'fast.set') throw new Error('Unknown Fast renderer method');
    validateContext(args.context);
    const { sessionId, modelKey, cwd } = args.context;
    const policy = await loadDesktopPolicy(cwd, modelKey);
    const context = { sessionId, modelKey, ...policy };
    return method === 'fast.read' ? preferences.read(context) : preferences.set(context, args.desiredActive);
  },
  async onUnload() {
    const close = closeBridge;
    closeBridge = undefined;
    preferences = undefined;
    await close?.();
  },
};
