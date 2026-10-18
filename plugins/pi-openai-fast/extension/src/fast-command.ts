import { parseFastCommandArgs } from "./fast-command-parser.ts";
import type { FastConfig } from "./fast-config-store.ts";
import type { FastStateEngine, FastStateSnapshot, FastStateTransition } from "./fast-state-engine.ts";
import type { FastModelIdentity } from "./supported-models.ts";

export const FAST_COMMAND_USAGE = "用法：/fast [on | off | status]；不带参数时切换开关。";
export const FAST_COMMAND_SAVE_FAILED_MESSAGE =
  "Could not save Fast Mode preference; the config update was not saved.";

export type FastCommandNoticeType = "info" | "warning" | "error";

export interface FastCommandDependencies {
  stateEngine: FastStateEngine;
  config: Pick<FastConfig, "persistState">;
  currentModel?: FastModelIdentity | undefined;
  saveDesiredActive(desiredActive: boolean): Promise<boolean>;
  applyDesiredActive?(desiredActive: boolean): Promise<void>;
  writeFastDesiredHandoff(desiredActive: boolean): void;
  notify(message: string, type: FastCommandNoticeType): void;
}

export type FastCommandResult =
  | { kind: "usage" | "status" | "error"; state: FastStateSnapshot; persisted: false }
  | { kind: "toggled"; state: FastStateSnapshot; transition: FastStateTransition; persisted: boolean };

export function fastStatusMessage(state: FastStateSnapshot): string {
  const model = state.currentModelKey ?? "未选择模型";
  if (state.active) return `⚡ Fast 已开启 · ${model} · 后续请求将申请 priority。`;
  if (state.desiredActive) return `Fast 未生效 · ${model} · 当前模型未配置 Fast 支持。`;
  return `Fast 已关闭 · ${model} · 后续请求使用普通模式。`;
}

export async function executeFastCommand(args: string, dependencies: FastCommandDependencies): Promise<FastCommandResult> {
  const parsed = parseFastCommandArgs(args);
  if (parsed.kind === "usage") {
    dependencies.notify(FAST_COMMAND_USAGE, "error");
    return { kind: "usage", state: dependencies.stateEngine.snapshot(), persisted: false };
  }
  if (parsed.kind === "status") {
    const state = dependencies.stateEngine.snapshot();
    dependencies.notify(fastStatusMessage(state), "info");
    return { kind: "status", state, persisted: false };
  }
  const previous = dependencies.stateEngine.snapshot();
  const nextDesiredActive = parsed.kind === "set" ? parsed.desiredActive : !previous.desiredActive;
  try {
    await dependencies.applyDesiredActive?.(nextDesiredActive);
  } catch (error) {
    dependencies.notify(`Fast 切换失败：${error instanceof Error ? error.message : String(error)}`, "error");
    return { kind: "error", state: previous, persisted: false };
  }
  const transition = dependencies.stateEngine.transition({ currentModel: dependencies.currentModel, desiredActive: nextDesiredActive });
  const state = transition.current;
  dependencies.writeFastDesiredHandoff(state.desiredActive);
  let persisted = false;
  if (dependencies.config.persistState) {
    persisted = await dependencies.saveDesiredActive(state.desiredActive);
    if (!persisted) dependencies.notify(FAST_COMMAND_SAVE_FAILED_MESSAGE, "warning");
  }
  dependencies.notify(fastStatusMessage(state), state.desiredActive && !state.active ? "warning" : "info");
  return { kind: "toggled", state, transition, persisted };
}
