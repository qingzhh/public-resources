'use strict';
const { createServer, createConnection } = require('node:net');
const { createHash } = require('node:crypto');
const { resolve, join } = require('node:path');
const { tmpdir } = require('node:os');

function bridgeAddress(root = __dirname) {
  const normalized = resolve(root).replaceAll('\\', '/');
  const hash = createHash('sha256').update(process.platform === 'win32' ? normalized.toLowerCase() : normalized).digest('hex').slice(0, 24);
  return process.platform === 'win32' ? `\\\\.\\pipe\\pi-openai-fast-${hash}` : join(tmpdir(), `pi-openai-fast-${hash}.sock`);
}

function validateContext(context) {
  if (!context || typeof context !== 'object' || Array.isArray(context)) throw new Error('Fast context is missing');
  for (const [key, maximum] of [['sessionId', 256], ['modelKey', 512]]) {
    if (typeof context[key] !== 'string' || !context[key].trim() || context[key].length > maximum || /[\x00-\x1f]/.test(context[key])) throw new Error(`Invalid Fast ${key}`);
  }
  if (!context.modelKey.includes('/')) throw new Error('Fast model key must name a provider and model');
}

function createPreferenceStore() {
  const choices = new Map();
  let revision = 0;
  const keyFor = context => JSON.stringify([context.sessionId, context.modelKey]);
  function read(context) {
    validateContext(context);
    const choice = choices.get(keyFor(context));
    const desiredActive = choice ?? context.defaultDesired === true;
    const supported = context.supported === true;
    return { sessionId: context.sessionId, modelKey: context.modelKey, desiredActive, active: desiredActive && supported,
      supported, source: choice === undefined ? 'default' : 'manual', revision };
  }
  return {
    read,
    set(context, desiredActive) {
      validateContext(context);
      if (typeof desiredActive !== 'boolean') throw new Error('Fast requires an explicit on/off choice');
      if (desiredActive && context.supported !== true) throw new Error('当前模型未配置 Fast 支持');
      const key = keyFor(context);
      if (choices.get(key) !== desiredActive) { choices.set(key, desiredActive); revision += 1; }
      return read(context);
    },
    clear() { choices.clear(); revision += 1; },
  };
}

async function startBridge(store, address = bridgeAddress()) {
  const sockets = new Set();
  const server = createServer(socket => {
    sockets.add(socket);
    socket.setEncoding('utf8');
    socket.setTimeout(1000, () => socket.destroy());
    socket.on('error', () => {});
    socket.on('close', () => sockets.delete(socket));
    let text = '';
    let answered = false;
    socket.on('data', chunk => {
      if (answered) return;
      text += chunk;
      if (Buffer.byteLength(text) > 8192) { socket.destroy(); return; }
      if (!text.includes('\n')) return;
      answered = true;
      try {
        const request = JSON.parse(text.slice(0, text.indexOf('\n')));
        const value = request.method === 'read' ? store.read(request.context) : request.method === 'set' ? store.set(request.context, request.desiredActive) : (() => { throw new Error('Unknown Fast bridge method'); })();
        socket.end(JSON.stringify({ ok: true, value }) + '\n');
      } catch (error) { socket.end(JSON.stringify({ ok: false, message: error.message }) + '\n'); }
    });
  });
  await new Promise((accept, reject) => {
    server.once('error', reject);
    server.listen(address, () => { server.removeListener('error', reject); accept(); });
  });
  server.on('error', () => {});
  server.unref();
  return async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise((accept, reject) => server.close(error => error ? reject(error) : accept()));
    store.clear();
  };
}

function callBridge(request, address = bridgeAddress(), timeout = 350) {
  return new Promise((accept, reject) => {
    const socket = createConnection(address);
    socket.setEncoding('utf8');
    let text = '', settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true; clearTimeout(timer); socket.destroy();
      error ? reject(error) : accept(value);
    };
    const timer = setTimeout(() => finish(new Error('Fast 状态服务响应超时')), timeout);
    socket.on('error', error => finish(error));
    socket.on('connect', () => socket.write(JSON.stringify(request) + '\n'));
    socket.on('data', chunk => {
      text += chunk;
      if (Buffer.byteLength(text) > 8192) { finish(new Error('Invalid Fast bridge response')); return; }
      if (!text.includes('\n')) return;
      try {
        const reply = JSON.parse(text.slice(0, text.indexOf('\n')));
        reply.ok ? finish(null, reply.value) : finish(new Error(reply.message ?? 'Fast state update failed'));
      } catch (error) { finish(error); }
    });
    socket.on('end', () => { if (!settled) finish(new Error('Fast 状态服务未返回结果')); });
  });
}

module.exports = { bridgeAddress, validateContext, createPreferenceStore, startBridge, callBridge };
