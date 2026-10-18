import { loadDesktopStore, contextFromDesktopState } from './desktop-context.mjs';
import { FastController } from './fast-controller.mjs';

const STYLE = `
.fast-control { position: relative; display: inline-flex; align-items: center; }
.fast-button { height: 28px; display: inline-flex; align-items: center; gap: 5px; padding: 0 8px; border: 1px solid color-mix(in srgb, currentColor 17%, transparent); border-radius: 8px; background: transparent; color: var(--text-secondary, #8c8c8c); font: inherit; font-size: 12px; font-weight: 600; cursor: pointer; transition: color .15s, background .15s, border-color .15s; }
.fast-button:hover:not(:disabled) { background: color-mix(in srgb, currentColor 9%, transparent); }
.fast-button.is-on { color: #c18b16; background: #c18b1618; border-color: #c18b1666; }
.fast-button.is-error { color: #d06060; border-color: #d0606066; }
.fast-button:disabled { cursor: default; opacity: .55; }
.fast-button:focus-visible { outline: 2px solid currentColor; outline-offset: 3px; }
.fast-button svg { width: 13px; height: 13px; flex: none; }
.fast-button-state { font-size: 10px; opacity: .8; }
.fast-feedback { position: absolute; right: 0; bottom: calc(100% + 9px); width: max-content; max-width: 280px; padding: 8px 10px; border: 1px solid color-mix(in srgb, currentColor 18%, transparent); border-radius: 8px; background: var(--bg-secondary, #282828); color: var(--text-primary, #efefef); font-size: 12px; font-weight: 400; line-height: 1.5; white-space: normal; box-shadow: 0 4px 16px #0002; pointer-events: none; }
.fast-feedback.is-error { color: #d06060; }
`;
let controller;
let cleanup = [];

export function FastControl({ React, controller: stateController }) {
  const state = React.useSyncExternalStore(stateController.subscribe, stateController.snapshot, stateController.snapshot);
  const active = state.data?.active === true && !state.error;
  const disabled = state.pending || !state.context?.ready || !state.data?.supported;
  const status = state.pending ? '…' : state.error ? '!' : !state.data ? '—' : !state.data.supported ? '不可用' : active ? 'ON' : 'OFF';
  const title = state.error || state.context?.reason || (!state.data ? '正在读取 Fast 状态…' : !state.data.supported ? '当前模型未配置 Fast 支持。' : `${state.context.modelKey} · ${active ? '点击关闭' : '点击开启'} Fast；影响后续请求。`);
  const feedback = state.error || state.message;
  return React.createElement('span', { className: 'fast-control' },
    React.createElement('button', {
      type: 'button', className: `fast-button${active ? ' is-on' : ''}${state.error ? ' is-error' : ''}`,
      title, disabled, 'aria-label': `Fast ${status}`, 'aria-pressed': active, 'aria-busy': state.pending,
      onClick: () => stateController.toggle(),
    }, React.createElement('svg', { viewBox: '0 0 16 16', fill: 'none', 'aria-hidden': true },
      React.createElement('path', { d: 'M9.5 1.5 3 9h4.5L6.5 14.5 13 7H8.5l1-5.5Z', stroke: 'currentColor', strokeWidth: 1.5, strokeLinejoin: 'round' })),
    'Fast', React.createElement('span', { className: 'fast-button-state' }, status)),
    feedback ? React.createElement('span', { className: `fast-feedback${state.error ? ' is-error' : ''}`, role: state.error ? 'alert' : 'status', 'aria-live': 'polite' }, feedback) : null);
}

export async function onLoad(pi) {
  onUnload();
  const React = globalThis.__PI_RENDERER_HOST__?.react;
  if (!React?.createElement || !React?.useSyncExternalStore) throw new Error('Fast 缺少桌面 React 接口');
  const stateController = new FastController(pi.dispatch);
  controller = stateController;
  const disposers = cleanup;
  const isCurrent = () => controller === stateController && !stateController.disposed;
  disposers.push(pi.ui.injectStyle(STYLE));
  disposers.push(pi.slots.register({ slot: 'composerControl', positions: ['right'], component: () => FastControl({ React, controller: stateController }) }));
  try {
    const store = await loadDesktopStore();
    if (!isCurrent()) return;
    const update = () => { if (!isCurrent()) return; try { stateController.setContext(contextFromDesktopState(store.getState())); } catch (error) { stateController.fail(error); } };
    disposers.push(store.subscribe(update));
    update();
    const timer = setInterval(() => stateController.refresh(), 1200);
    disposers.push(() => clearInterval(timer));
  } catch (error) { if (isCurrent()) stateController.fail(error); }
}

export function onUnload() {
  controller?.dispose();
  for (const dispose of cleanup.splice(0).reverse()) dispose();
  controller = undefined;
}
