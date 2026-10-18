import { callBridge, type FastBridgeContext } from "../../desktop-bridge.cjs";

/** The backend owns Desktop choices; CLI use retains the original local preference path. */
export class DesktopFastPreference {
  private connected = false;
  private warned = false;

  get isConnected(): boolean { return this.connected; }

  async read(context: FastBridgeContext): Promise<boolean | undefined> {
    try {
      const state = await callBridge({ method: "read", context });
      this.connected = true;
      this.warned = false;
      return state.desiredActive;
    } catch (error) {
      if (this.connected) throw error;
      return undefined;
    }
  }

  async set(context: FastBridgeContext, desiredActive: boolean): Promise<void> {
    if (!this.connected) return;
    await callBridge({ method: "set", context, desiredActive });
  }

  shouldWarn(): boolean {
    if (this.warned) return false;
    this.warned = true;
    return true;
  }
}
