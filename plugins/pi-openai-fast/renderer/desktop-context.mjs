// PI-Desktop 0.16 composerControl supplies position only. Keep its read-only
// store compatibility adapter here so a future public context API can replace it.
export function contextFromDesktopState(state) {
  if (!state || !Array.isArray(state.sessions) || !Array.isArray(state.providers)) throw new Error('Fast 无法读取会话上下文');
  const sessionId = state.activeSessionId;
  if (!sessionId) return { ready: false, reason: '请先新建会话，再选择 Fast；无需先发送消息。' };
  if (state.selectingSessionId) return { ready: false, reason: '正在切换会话…' };
  const session = state.sessions.find(candidate => candidate.id === sessionId);
  if (!session) return { ready: false, reason: '正在读取当前会话…' };
  if (session.source === 'remote' || session.source === 'pi-native') return { ready: false, reason: '此会话使用独立运行环境，Fast 插件无法控制。' };
  const providerId = session.providerId ?? state.settings?.defaultProviderId;
  const provider = state.providers.find(candidate => candidate.id === providerId);
  const modelId = session.modelId ?? state.settings?.defaultModelId ?? provider?.defaultModelId ?? provider?.models?.[0]?.id;
  const cwd = session.projectPath ?? state.workspace?.path;
  if (!provider || !modelId) return { ready: false, reason: '请先选择模型。' };
  if (!cwd) return { ready: false, reason: '请先为会话选择工作目录。' };
  return { ready: true, sessionId, modelKey: `${provider.vendorKey || 'custom'}/${modelId}`, cwd };
}

export async function loadDesktopStore(documentRef = document, importModule = url => import(url)) {
  const scripts = [...documentRef.querySelectorAll('script[type="module"][src]')];
  const source = scripts.map(script => new URL(script.src, documentRef.baseURI)).find(url =>
    /\/assets\/index-[\w-]+\.js$/.test(url.pathname) && url.origin === new URL(documentRef.baseURI).origin);
  if (!source) throw new Error('Fast 未找到兼容的桌面会话接口，请更新插件。');
  const module = await importModule(source.href);
  const candidates = [...new Set(Object.values(module))].filter(value =>
    typeof value?.getState === 'function' && typeof value?.subscribe === 'function');
  const store = candidates.find(value => {
    const state = value.getState();
    return Array.isArray(state?.sessions) && Array.isArray(state?.providers) && Object.hasOwn(state, 'activeSessionId');
  });
  if (!store) throw new Error('Fast 当前版本无法读取桌面会话，请更新插件。');
  return store;
}
