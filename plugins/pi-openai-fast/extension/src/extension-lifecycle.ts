import type {
  BeforeProviderRequestEvent,
  ExtensionAPI,
  ExtensionCommandContext,
  ExtensionContext,
  ExtensionEvent,
} from "@earendil-works/pi-coding-agent";
import { FAST_COMMAND } from "./capabilities.ts";
import { ConfigLoadCoordinator } from "./config-load-coordination.ts";
import { executeFastCommand, FAST_COMMAND_SAVE_FAILED_MESSAGE } from "./fast-command.ts";
import {
  DEFAULT_FAST_CONFIG,
  FastConfigStore,
  type FastConfig,
  type FastConfigRepository,
} from "./fast-config-store.ts";
import { readFastDesiredHandoff, writeFastDesiredHandoff } from "./fast-desired-handoff.ts";
import { FastStateEngine, type FastStateSnapshot } from "./fast-state-engine.ts";
import { createFastWarningCollector, deliverFastWarnings, hasConfigWriteFailureWarning } from "./fast-warnings.ts";
import { FooterFeedback } from "./footer-feedback.ts";
import { ServiceTierInjector } from "./service-tier-injector.ts";
import { isStartupFastOverrideRequested, registerStartupFastOverrideFlag } from "./startup-fast-override.ts";
import { resolveStartupDesiredActive } from "./startup-fast-resolution.ts";
import { isDefaultFastModel, ModelFastPreference } from "./model-fast-preference.ts";
import { installLunaRequestDefault } from "./luna-request-default.ts";
import { fastModelKey, type FastModelIdentity } from "./supported-models.ts";
import { DesktopFastPreference } from "./desktop-fast-preference.ts";

type ModelSelectEvent = Extract<ExtensionEvent, { type: "model_select" }>;

export interface PiOpenAIFastRuntimeOptions {
  configStore?: FastConfigRepository;
  stateEngine?: FastStateEngine;
  serviceTierInjector?: ServiceTierInjector;
  footerFeedback?: FooterFeedback;
}

function cloneDefaultConfig(): FastConfig {
  return {
    persistState: DEFAULT_FAST_CONFIG.persistState,
    desiredActive: DEFAULT_FAST_CONFIG.desiredActive,
    supportedModels: [...DEFAULT_FAST_CONFIG.supportedModels],
    footer: { ...DEFAULT_FAST_CONFIG.footer, vars: { ...DEFAULT_FAST_CONFIG.footer.vars } },
  };
}

/**
 * Package-level registration boundary for the focused Fast Mode extension.
 *
 * This lifecycle module wires Pi command and provider-request hooks while
 * leaving command parsing, state derivation, config persistence, and payload
 * mutation in their own modules.
 */
export function registerPiOpenAIFast(pi: ExtensionAPI, options: PiOpenAIFastRuntimeOptions = {}): void {
  const configStore = options.configStore ?? new FastConfigStore();
  const stateEngine =
    options.stateEngine ??
    new FastStateEngine({
      desiredActive: DEFAULT_FAST_CONFIG.desiredActive,
      supportedModels: DEFAULT_FAST_CONFIG.supportedModels,
    });
  const serviceTierInjector = options.serviceTierInjector ?? new ServiceTierInjector();
  const footerFeedback = options.footerFeedback ?? new FooterFeedback();

  registerStartupFastOverrideFlag(pi);

  const configLoadCoordinator = new ConfigLoadCoordinator(cloneDefaultConfig(), configStore);
  let modelPreference = new ModelFastPreference(false);
  let releaseLunaRequestDefault: (() => void) | undefined;
  const desktopPreference = new DesktopFastPreference();
  let modelSyncSequence = 0;

  function desktopContext(ctx: ExtensionContext, model: FastModelIdentity | undefined = ctx.model) {
    const modelKey = fastModelKey(model);
    const sessionId = ctx.sessionManager.getSessionId();
    if (!modelKey || !sessionId) return undefined;
    return {
      sessionId,
      modelKey,
      supported: isDefaultFastModel(model) || configLoadCoordinator.current.supportedModels.includes(modelKey),
      defaultDesired: isDefaultFastModel(model) || (configLoadCoordinator.current.persistState && configLoadCoordinator.current.desiredActive),
    };
  }

  async function syncModelPreference(ctx: ExtensionContext, model: FastModelIdentity | undefined = ctx.model): Promise<{ synchronized: boolean; current: boolean; state: FastStateSnapshot }> {
    const sequence = ++modelSyncSequence;
    const key = fastModelKey(model);
    const supportedModels = [...configLoadCoordinator.current.supportedModels];
    if (isDefaultFastModel(model) && key !== undefined) supportedModels.push(key);
    let desiredActive = modelPreference.desiredFor(model);
    let synchronized = true;
    const context = desktopContext(ctx, model);
    if (context) {
      try {
        const sharedDesired = await desktopPreference.read(context);
        if (sharedDesired !== undefined) desiredActive = sharedDesired;
      } catch {
        synchronized = false;
        desiredActive = false;
        if (sequence === modelSyncSequence && desktopPreference.shouldWarn()) notifyUi(ctx.ui, "Fast 状态同步失败，本次使用普通模式；请重载 Fast 插件。", "error");
      }
    }
    if (sequence !== modelSyncSequence) {
      // A request retains its own resolved state without overwriting newer model/UI state.
      const state = new FastStateEngine({ currentModel: model, desiredActive, supportedModels }).snapshot();
      return { synchronized, current: false, state };
    }
    const transition = stateEngine.transition({ currentModel: model, desiredActive, supportedModels });
    footerFeedback.notifyForTransition(transition, getNotifier(ctx.ui));
    syncFooter(ctx, model as ExtensionContext["model"]);
    return { synchronized, current: true, state: transition.current };
  }

  function notifyUi(ui: ExtensionContext["ui"] | undefined, message: string, type: "info" | "warning" | "error"): void {
    try {
      ui?.notify?.(message, type);
    } catch {
      // Notification sinks must not make lifecycle or command handling fail.
    }
  }

  function getUiForFooterFeedback(ui: ExtensionContext["ui"] | undefined): {
    notify(message: string, type?: "info" | "warning" | "error"): void;
    setFooter: ExtensionContext["ui"]["setFooter"] | undefined;
    setStatus: ExtensionContext["ui"]["setStatus"] | undefined;
  } {
    return {
      notify(message, type) {
        notifyUi(ui, message, type ?? "info");
      },
      setFooter: typeof ui?.setFooter === "function" ? ui.setFooter.bind(ui) : undefined,
      setStatus: typeof ui?.setStatus === "function" ? ui.setStatus.bind(ui) : undefined,
    };
  }

  function getNotifier(ui: ExtensionContext["ui"] | undefined):
    | { notify(message: string, type: "info" | "warning" | "error"): void }
    | undefined
  {
    if (typeof ui?.notify === "function") {
      return { notify: (message, type) => notifyUi(ui, message, type) };
    }

    return undefined;
  }

  function syncFooter(ctx: ExtensionContext, currentModel = ctx.model): void {
    const currentConfig = configLoadCoordinator.current;

    footerFeedback.syncFooterMode(currentConfig.footer.mode, getUiForFooterFeedback(ctx.ui), {
      context: {
        model: currentModel,
        sessionManager: ctx.sessionManager,
        modelRegistry: ctx.modelRegistry,
        getContextUsage: () => ctx.getContextUsage(),
      },
      isFastActive: () => stateEngine.snapshot().active,
      getThinkingLevel: () => pi.getThinkingLevel(),
      fastLabelColors: {
        dark: currentConfig.footer.darkFastColor,
        light: currentConfig.footer.lightFastColor,
        vars: { ...currentConfig.footer.vars },
      },
    });
  }

  async function loadConfig(
    ctx: ExtensionContext,
    currentModel = ctx.model,
  ): Promise<FastConfig> {
    const loadedConfig = await configLoadCoordinator.load(ctx.cwd);
    const currentConfig = loadedConfig.config;
    const warningCollector = createFastWarningCollector();
    const startupFastOverride = isStartupFastOverrideRequested(pi);
    const fastDesiredHandoff = readFastDesiredHandoff();
    if (fastDesiredHandoff.kind === "invalid") {
      warningCollector.collect(fastDesiredHandoff.warning);
    }
    deliverFastWarnings([...loadedConfig.warnings, ...warningCollector.warnings], ctx.ui);
    if (startupFastOverride) {
      writeFastDesiredHandoff(true);
    }
    modelPreference = new ModelFastPreference(resolveStartupDesiredActive({
      config: currentConfig,
      startupFastOverride,
      fastDesiredHandoff,
    }));
    await syncModelPreference(ctx, currentModel);
    releaseLunaRequestDefault ??= installLunaRequestDefault();
    return currentConfig;
  }

  async function ensureConfig(ctx: ExtensionContext): Promise<FastConfig> {
    if (!configLoadCoordinator.isLoaded) {
      return await loadConfig(ctx);
    }

    return configLoadCoordinator.current;
  }

  pi.registerCommand(FAST_COMMAND, {
    description: "Fast Mode：切换，或使用 on / off / status",
    handler: async (args: string, ctx: ExtensionCommandContext) => {
      const model = ctx.model;
      const config = await ensureConfig(ctx);
      const synchronization = await syncModelPreference(ctx, model);
      if (!synchronization.synchronized) return;
      if (!synchronization.current) {
        notifyUi(ctx.ui, "模型状态正在切换，请重新执行 Fast 命令。", "warning");
        return;
      }
      const warningCollector = createFastWarningCollector();
      const result = await executeFastCommand(args, {
        stateEngine,
        config: desktopPreference.isConnected ? { ...config, persistState: false } : config,
        currentModel: model,
        saveDesiredActive: (desiredActive) =>
          configStore.writeDesiredActive(ctx.cwd, desiredActive, { warn: warningCollector.collect }),
        applyDesiredActive: async (desiredActive) => {
          const context = desktopContext(ctx, model);
          if (context) await desktopPreference.set(context, desiredActive);
        },
        writeFastDesiredHandoff: (desiredActive) => {
          if (!desktopPreference.isConnected) writeFastDesiredHandoff(desiredActive);
        },
        notify: (message, type) => {
          if (
            type === "warning" &&
            message === FAST_COMMAND_SAVE_FAILED_MESSAGE &&
            hasConfigWriteFailureWarning(warningCollector.warnings)
          ) {
            return;
          }
          notifyUi(ctx.ui, message, type);
        },
      });
      deliverFastWarnings(warningCollector.warnings, ctx.ui);

      if (result.kind === "toggled") {
        modelPreference.remember(model, result.state.desiredActive);
        configLoadCoordinator.updateCurrent({
          ...configLoadCoordinator.current,
          desiredActive: result.state.desiredActive,
        });
        footerFeedback.notifyForTransition(result.transition, getNotifier(ctx.ui));
        syncFooter(ctx, model);
      }
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    await loadConfig(ctx);
  });

  pi.on("session_shutdown", async (_event, ctx: ExtensionContext) => {
    modelSyncSequence += 1;
    footerFeedback.cleanup(getUiForFooterFeedback(ctx.ui));
    releaseLunaRequestDefault?.();
    releaseLunaRequestDefault = undefined;
  });

  pi.on("model_select", async (event: ModelSelectEvent, ctx: ExtensionContext) => {
    if (!configLoadCoordinator.isLoaded) {
      await loadConfig(ctx, event.model);
      return;
    }

    await syncModelPreference(ctx, event.model);
  });

  pi.on("thinking_level_select", (_event, ctx: ExtensionContext) => {
    if (configLoadCoordinator.isLoaded) {
      syncFooter(ctx);
    }
  });

  pi.on("before_provider_request", async (event: BeforeProviderRequestEvent, ctx: ExtensionContext) => {
    const model = ctx.model;
    await ensureConfig(ctx);
    const { state } = await syncModelPreference(ctx, model);
    const nextPayload = serviceTierInjector.inject(event.payload, state);
    // An explicit default tier preserves a manual Luna off choice across the Task compatibility bridge.
    if (isDefaultFastModel(model) && !state.active && typeof nextPayload === "object" && nextPayload !== null && !Array.isArray(nextPayload)) {
      return { ...nextPayload, service_tier: "default" };
    }
    return nextPayload === event.payload ? undefined : nextPayload;
  });
}
