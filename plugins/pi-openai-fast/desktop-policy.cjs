'use strict';
const { readFile } = require('node:fs/promises');
const { homedir } = require('node:os');
const { join, isAbsolute } = require('node:path');
const DEFAULT_SUPPORTED = ['openai/gpt-5.4', 'openai/gpt-5.5', 'openai-codex/gpt-5.4', 'openai-codex/gpt-5.5'];

// Read the same two preference files as FastConfigStore, without creating or updating either file.
async function loadDesktopPolicy(cwd, modelKey, home = homedir()) {
  if (typeof cwd !== 'string' || !isAbsolute(cwd)) throw new Error('Fast 无法确认当前工作目录');
  let persistState = false, desiredActive = false, supportedModels = [...DEFAULT_SUPPORTED];
  for (const file of [join(home, '.pi', 'agent', 'extensions', 'pi-openai-fast.json'), join(cwd, '.pi', 'extensions', 'pi-openai-fast.json')]) {
    let source;
    try { source = JSON.parse(await readFile(file, 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') continue; throw new Error('Fast 配置读取失败，请检查 pi-openai-fast.json'); }
    if (!source || typeof source !== 'object' || Array.isArray(source)) throw new Error('Fast 配置必须是 JSON 对象');
    if (typeof source.persistState === 'boolean') persistState = source.persistState;
    if (typeof source.desiredActive === 'boolean') desiredActive = source.desiredActive;
    else if (!Object.hasOwn(source, 'desiredActive') && typeof source.active === 'boolean') desiredActive = source.active;
    if (Array.isArray(source.supportedModels)) {
      supportedModels = source.supportedModels.filter(value => typeof value === 'string').map(value => value.trim()).filter(value => {
        const slash = value.indexOf('/');
        return slash > 0 && slash < value.length - 1 && !/\s|[\*\[\]\(\)\{\}\?\+\|\^\$\\]/.test(value);
      });
    }
  }
  const luna = modelKey.slice(modelKey.indexOf('/') + 1) === 'gpt-6-luna';
  return { supported: luna || supportedModels.includes(modelKey), defaultDesired: luna || (persistState && desiredActive) };
}
module.exports = { loadDesktopPolicy };
