export interface FastBridgeContext {
  sessionId: string;
  modelKey: string;
  supported: boolean;
  defaultDesired: boolean;
}
export interface FastBridgeState {
  sessionId: string;
  modelKey: string;
  desiredActive: boolean;
  active: boolean;
  supported: boolean;
  source: "manual" | "default";
  revision: number;
}
export function callBridge(request: { method: "read" | "set"; context: FastBridgeContext; desiredActive?: boolean }, address?: string, timeout?: number): Promise<FastBridgeState>;
