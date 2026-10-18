import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { readFile, writeFile, rm } from 'node:fs/promises';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

export const packageRoot = resolve(__dirname, '..');
export const packageRootPosix = packageRoot.replaceAll('\\', '/');
export const extensionEntry = join(packageRoot, 'extension', 'index.ts');
export const extensionEntryPosix = extensionEntry.replaceAll('\\', '/');

/**
 * Initializes an isolated test workspace to prevent tests from reading/writing
 * user preferences or the project repository root.
 */
export function setupTestWorkspace() {
  const originalCwd = process.cwd();
  const testDir = mkdtempSync(join(tmpdir(), 'pi-fast-test-'));
  const scratchDir = process.env.PI_SCRATCH_DIR || join(testDir, 'scratch');
  mkdirSync(scratchDir, { recursive: true });

  // Create isolated .pi/extensions configuration inside the test workspace
  const extensionDir = join(testDir, '.pi', 'extensions');
  mkdirSync(extensionDir, { recursive: true });
  writeFileSync(
    join(extensionDir, 'pi-openai-fast.json'),
    JSON.stringify(
      {
        persistState: false,
        desiredActive: false,
        supportedModels: ['custom/gpt-6.1-sol'],
        footer: { mode: 'status', vars: {} },
      },
      null,
      2,
    ),
  );

  process.chdir(testDir);

  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    try {
      process.chdir(originalCwd);
    } catch {}
    try {
      rmSync(testDir, { recursive: true, force: true });
    } catch {}
  };

  process.on('exit', cleanup);

  return {
    testDir,
    scratchDir,
    originalCwd,
    cleanup,
  };
}

/**
 * Loads the host TrustedExtensionRunner if PI_DESKTOP_TEST_RUNNER is configured.
 * If not configured, marks test as skipped via t.skip().
 * If configured but invalid, throws to trigger real test failure.
 */
export async function loadHostRunner(t) {
  const runnerPath = process.env.PI_DESKTOP_TEST_RUNNER;
  if (!runnerPath) {
    if (t && typeof t.skip === 'function') {
      t.skip('Skipped: PI_DESKTOP_TEST_RUNNER not configured');
    }
    return null;
  }
  const resolved = resolve(runnerPath);
  const mod = await import(pathToFileURL(resolved).href);
  if (typeof mod.TrustedExtensionRunner !== 'function') {
    throw new Error(`Host runner at ${resolved} does not export TrustedExtensionRunner`);
  }
  return mod.TrustedExtensionRunner;
}

/**
 * Loads the Desktop renderer bundle if PI_DESKTOP_RENDERER_BUNDLE is configured.
 * If not configured, marks test as skipped via t.skip().
 * If configured but missing, throws ENOENT to trigger real test failure.
 */
export async function loadRendererBundle(t) {
  const bundlePath = process.env.PI_DESKTOP_RENDERER_BUNDLE;
  if (!bundlePath) {
    if (t && typeof t.skip === 'function') {
      t.skip('Skipped: PI_DESKTOP_RENDERER_BUNDLE not configured');
    }
    return null;
  }
  const resolved = resolve(bundlePath);
  return await readFile(resolved, 'utf8');
}

/**
 * Loads DesktopSubagentRunner from PI_DESKTOP_TEST_RUNTIME_ROOT/sidecar.js.
 * If not configured, marks test as skipped via t.skip().
 * If configured but invalid, throws to trigger real test failure.
 */
export async function loadSubagentRunner(t, scratchDir) {
  const runtimeRoot = process.env.PI_DESKTOP_TEST_RUNTIME_ROOT;
  if (!runtimeRoot) {
    if (t && typeof t.skip === 'function') {
      t.skip('Skipped: PI_DESKTOP_TEST_RUNTIME_ROOT not configured');
    }
    return null;
  }
  const sidecarPath = join(runtimeRoot, 'sidecar.js');
  const source = await readFile(sidecarPath, 'utf8');

  const stdinMatch = source.match(/([a-zA-Z0-9_$]+)\(process\.stdin,\s*async/);
  const classMatch = source.match(
    /var\s+([a-zA-Z0-9_$]+)=class\{agent;opts;currentAssistant;lastReportText/,
  );
  if (!stdinMatch || !classMatch) {
    throw new Error(
      `Incompatible sidecar.js in ${runtimeRoot}: could not locate subagent runner or stdin hook`,
    );
  }

  const stop = stdinMatch.index;
  const className = classMatch[1];
  const chunksUrl = pathToFileURL(join(runtimeRoot, 'chunks')).href + '/';
  const testSource =
    source.slice(0, stop).replaceAll('./chunks/', chunksUrl) +
    `\nexport { ${className} as DesktopSubagentRunner };\n`;

  const scratch = scratchDir || process.env.PI_SCRATCH_DIR || tmpdir();
  const modulePath = join(scratch, `desktop-subagent-test-export-${Date.now()}-${Math.random().toString(36).slice(2)}.mjs`);
  await writeFile(modulePath, testSource, 'utf8');

  try {
    const mod = await import(pathToFileURL(modulePath).href);
    if (typeof mod.DesktopSubagentRunner !== 'function') {
      throw new Error(`Exported DesktopSubagentRunner is not a function from ${sidecarPath}`);
    }
    return {
      DesktopSubagentRunner: mod.DesktopSubagentRunner,
      cleanup: async () => {
        await rm(modulePath, { force: true }).catch(() => {});
      },
    };
  } catch (err) {
    await rm(modulePath, { force: true }).catch(() => {});
    throw err;
  }
}
