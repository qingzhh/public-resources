import { fastModelKey, type FastModelIdentity } from "./supported-models.ts";

export const DEFAULT_FAST_MODEL_ID = "gpt-6-luna";

export function isDefaultFastModel(model: FastModelIdentity | undefined): boolean {
  return model?.id === DEFAULT_FAST_MODEL_ID;
}

/** Preferences belong to a model in one session; Luna's default outranks inherited off. */
export class ModelFastPreference {
  private readonly overrides = new Map<string, boolean>();

  private readonly otherModelDefault: boolean;

  constructor(otherModelDefault: boolean) {
    this.otherModelDefault = otherModelDefault;
  }
  desiredFor(model: FastModelIdentity | undefined): boolean {
    const key = fastModelKey(model);
    return (key === undefined ? undefined : this.overrides.get(key)) ??
      (isDefaultFastModel(model) || this.otherModelDefault);
  }

  remember(model: FastModelIdentity | undefined, desired: boolean): void {
    const key = fastModelKey(model);
    if (key !== undefined) this.overrides.set(key, desired);
  }
}
