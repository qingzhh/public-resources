# 测试说明 (Testing Guide)

本项目测试套件针对 PI-Desktop Fast Mode 插件编写，包含独立单元测试与可选宿主集成测试。

## 1. Node.js 环境要求

测试直接以 ESM 方式导入 TypeScript 源文件（如 `extension/index.ts`、`*.ts`），无需预编译构建步骤。
- **推荐 Node.js 版本**：Node.js 22.18+ 或 Node.js 24+（原生支持 TypeScript 类型剥离 Type Stripping）。
- 低于此版本的 Node.js 需要额外 loader（如 `--experimental-strip-types` 或 tsx）。

## 2. 默认单元测试运行方式

在插件根目录下直接执行：

```bash
node --test tests/fast-controls.test.mjs tests/luna-default.test.mjs
```

在未配置宿主环境变量时：
- 测试将使用独立的临时工作区（`os.tmpdir()` + `mkdtemp`），自动隔离 cwd 与存储，不会读取或污染真实用户目录（`~/.pi`）与项目 `.pi/` 目录。
- **10 项纯单元测试** 将全部正常通过。
- **5 项依赖 Desktop 宿主环境的集成测试** 将自动显式跳过（`skip`），不会因缺失宿主环境而在顶层 import 崩溃。

## 3. 可选宿主环境配置

如需在本地运行完整的 Desktop 宿主集成测试，可通过以下环境变量指定已安装的 PI-Desktop 资源路径：

| 环境变量 | 说明 | 示例 |
| --- | --- | --- |
| `PI_DESKTOP_TEST_RUNNER` | 指定 Desktop 宿主扩展执行器 (`runner-*.js`) 路径 | `C:/Program Files/PI-Desktop/resources/agent-runtime/chunks/runner-LJTPDIGW.js` |
| `PI_DESKTOP_TEST_RUNTIME_ROOT` | 指定 Desktop agent-runtime 根目录（包含 `sidecar.js` 与 `chunks/`） | `C:/Program Files/PI-Desktop/resources/agent-runtime` |
| `PI_DESKTOP_RENDERER_BUNDLE` | 指定匹配被断言 SlotClass 锚点的 Desktop Renderer 构建代码 | `C:/path/to/inspect-index-*.js` |
| `PI_SCRATCH_DIR` | （可选）指定测试临时文件生成目录；缺省时回退至系统临时目录 | `C:/temp/scratch` |

### 运行宿主集成测试示例（PowerShell）：

```powershell
# 动态定位当前安装的 runner-*.js
$runner = (Get-ChildItem "C:\Program Files\PI-Desktop\resources\agent-runtime\chunks\runner-*.js").FullName
$env:PI_DESKTOP_TEST_RUNNER = $runner
$env:PI_DESKTOP_TEST_RUNTIME_ROOT = "C:\Program Files\PI-Desktop\resources\agent-runtime"

node --test tests/fast-controls.test.mjs tests/luna-default.test.mjs
```

## 4. 跳过与失败行为约定 (Skip & Fail Policy)

- **未配置变量时**：
  - 未配置 `PI_DESKTOP_TEST_RUNNER` 时，依赖 `TrustedExtensionRunner` 的 4 项测试跳过。
  - 未配置 `PI_DESKTOP_TEST_RUNTIME_ROOT` 时，依赖 `sidecar.js` Subagent 的 1 项测试跳过。
  - 未配置 `PI_DESKTOP_RENDERER_BUNDLE` 时，依赖 Renderer 槽位注册表的 1 项测试跳过。
- **显式配置但无效时（真实失败）**：
  - 若指定了 `PI_DESKTOP_TEST_RUNNER` 但文件不存在或未导出 `TrustedExtensionRunner`，测试将真实报错失败（不会静默 skip）。
  - 若指定了 `PI_DESKTOP_TEST_RUNTIME_ROOT` 但 `sidecar.js` 缺失或结构不兼容，测试将真实报错失败。
  - 若指定了 `PI_DESKTOP_RENDERER_BUNDLE` 但文件不存在，测试将真实报错失败。
