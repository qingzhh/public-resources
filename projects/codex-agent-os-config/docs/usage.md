# Usage

## 日常任务模板

```text
请按我的 Codex Agent OS 执行本任务。

使用：
- Profile: <YOUR_VAULT_ROOT>/profiles/codex-workflow.md
- Project State: <YOUR_VAULT_ROOT>/projects/project-template.md

要求：
- planner 作为 workflow manager 判断复杂度
- planner/reviewer 只读，禁止改文件
- worker 严格按 plan 最小改动
- reviewer 检查 correctness / regression / edge cases
- subagent 完成即 disposable
- fresh context > long history
- 完成前必须验证
- 有长期价值时再更新 Vault
- 输出中文、简洁
```

## 让 Codex 自动配置

把下面内容复制给 Codex：

```text
请根据 https://github.com/qingzhh/public-resources/tree/main/projects/codex-agent-os-config 配置我的 Codex Agent OS。

先备份我本机已有的 ~/.codex/AGENTS.md、~/.codex/config.toml 和 ~/.codex/agents/。
安装 codex/AGENTS.md 和 codex/agents/*.toml。
config.toml 只参考 codex/config.example.toml 合并，不要直接覆盖。
任何本机路径、project trust、高权限 sandbox、MCP 本地命令都必须先让我确认。
不要复制 auth.json、日志、session、sqlite、cache 或凭据。
完成后验证 TOML 可解析，并报告 changed files / verification / risks。
```

## Profile 选择

- 字幕任务：`<YOUR_VAULT_ROOT>/profiles/subtitle-workflow.md`
- 前端 React：`<YOUR_VAULT_ROOT>/profiles/frontend-react.md`
- Python 研究：`<YOUR_VAULT_ROOT>/profiles/python-research.md`
- MCP 开发：`<YOUR_VAULT_ROOT>/profiles/mcp-dev.md`
- Codex 工作流：`<YOUR_VAULT_ROOT>/profiles/codex-workflow.md`

## 记忆写入规则

- 只记录长期有价值的信息。
- 优先追加 checkpoint，不重写全文。
- 不写入长日志、临时推理、大段 diff、构建产物或未验证结论。
- `projects/` 只保存项目状态、风险和下一步。
- `profiles/` 只保存跨项目能力模板。
