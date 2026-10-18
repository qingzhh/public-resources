export type WebDAVSaveable = {
    save: () => Promise<boolean>;
};

export async function runWebDAVAction(
    fields: readonly WebDAVSaveable[],
    action: () => Promise<unknown>
): Promise<boolean> {
    for (const field of fields) {
        if (!await field.save()) return false;
    }
    await action();
    return true;
}
