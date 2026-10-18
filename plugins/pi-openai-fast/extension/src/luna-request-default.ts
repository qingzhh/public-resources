import { DEFAULT_FAST_MODEL_ID } from "./model-fast-preference.ts";

const BRIDGE_KEY = Symbol.for("pi-openai-fast.luna-fetch-default.v1");
type FetchBridge = { users: number; original: typeof fetch; wrapped: typeof fetch };
type BridgeGlobal = typeof globalThis & { [BRIDGE_KEY]?: FetchBridge };

function priorityBody(body: string): string | undefined {
  try {
    const payload = JSON.parse(body);
    if (payload?.model !== DEFAULT_FAST_MODEL_ID || payload.service_tier != null) return undefined;
    return JSON.stringify({ ...payload, service_tier: "priority" });
  } catch {
    return undefined;
  }
}

/** Desktop's built-in Task runners share fetch but bypass extension provider hooks. */
export function installLunaRequestDefault(): () => void {
  const host = globalThis as BridgeGlobal;
  let bridge = host[BRIDGE_KEY];
  if (!bridge) {
    const original = globalThis.fetch;
    bridge = { users: 0, original, wrapped: original };
    const state = bridge;
    bridge.wrapped = async (input, init) => {
      if (state.users > 0) {
        let nextInput = input;
        let nextInit = init;
        try {
          const request = input instanceof Request ? input : undefined;
          const url = new URL(request?.url ?? String(input));
          const method = (init?.method ?? request?.method ?? "GET").toUpperCase();
          const isOpenAIEndpoint = /\/(responses|chat\/completions)\/?$/.test(url.pathname);
          if (method === "POST" && isOpenAIEndpoint) {
            const body = typeof init?.body === "string" ? init.body :
              init?.body == null && request ? await request.clone().text() : undefined;
            const modified = body === undefined ? undefined : priorityBody(body);
            if (modified !== undefined) {
              const headers = new Headers(init?.headers ?? request?.headers);
              headers.delete("content-length");
              nextInit = { ...init, headers, body: modified };
              // Construct a new Request to retain URL, method, signal and request options.
              if (request) {
                nextInput = new Request(request, nextInit);
                nextInit = undefined;
              }
            }
          }
        } catch {
          // Non-JSON/unsupported request forms retain their normal fetch behavior.
          nextInput = input;
          nextInit = init;
        }
        return state.original(nextInput, nextInit);
      }
      return state.original(input, init);
    };
    host[BRIDGE_KEY] = bridge;
    globalThis.fetch = bridge.wrapped;
  }
  bridge.users += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    bridge!.users -= 1;
    if (bridge!.users === 0) {
      if (globalThis.fetch === bridge!.wrapped) globalThis.fetch = bridge!.original;
      if (host[BRIDGE_KEY] === bridge) delete host[BRIDGE_KEY];
    }
  };
}
