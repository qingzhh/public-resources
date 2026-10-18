import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import ts from 'typescript';
import { fileURLToPath } from 'node:url';

const dirname = path.dirname(fileURLToPath(import.meta.url));

function loadWebDAVAction() {
    const sourcePath = path.join(dirname, '..', 'src', 'components', 'modules', 'setting', 'webdav-action.ts');
    const source = readFileSync(sourcePath, 'utf8');
    const output = ts.transpileModule(source, {
        compilerOptions: {
            module: ts.ModuleKind.CommonJS,
            target: ts.ScriptTarget.ES2022,
        },
    }).outputText;
    const loadedModule = { exports: {} };
    new Function('exports', 'module', output)(loadedModule.exports, loadedModule);
    return loadedModule.exports;
}

test('WebDAV action waits for every pending setting before sending its request', async () => {
    const { runWebDAVAction } = loadWebDAVAction();
    const calls = [];

    const completed = await runWebDAVAction(
        [
            { save: async () => { calls.push('url'); return true; } },
            { save: async () => { calls.push('password'); return true; } },
            { save: async () => { calls.push('path'); return true; } },
        ],
        async () => { calls.push('action'); }
    );

    assert.equal(completed, true);
    assert.deepEqual(calls, ['url', 'password', 'path', 'action']);
});

test('WebDAV action does not run when a pending setting fails to save', async () => {
    const { runWebDAVAction } = loadWebDAVAction();
    const calls = [];

    const completed = await runWebDAVAction(
        [
            { save: async () => { calls.push('url'); return true; } },
            { save: async () => { calls.push('password'); return false; } },
            { save: async () => { calls.push('path'); return true; } },
        ],
        async () => { calls.push('action'); }
    );

    assert.equal(completed, false);
    assert.deepEqual(calls, ['url', 'password']);
});
