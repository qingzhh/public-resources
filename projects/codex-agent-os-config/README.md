# Codex Agent OS Config

这是一个脱敏后的 Codex 全局配置与工作流模板仓库。它适合迁移、分享和让别人的 Codex 直接按说明完成基础配置。

## 包含内容

- `codex/AGENTS.md`：脱敏后的全局 Codex 工作流守则。
- `codex/config.example.toml`：带备注的脱敏配置示例。
- `codex/agents/`：planner、worker、reviewer 三个 subagent 模板。
- `vault/AGENTS.md`：Vault 长期记忆规则。
- `vault/profiles/`：跨项目能力模板。
- `vault/projects/`：项目状态模板和 Codex OS 示例状态。
- `docs/`：使用、恢复和脱敏说明。

## 不包含内容

- 认证文件、token、key、cookie。
- Codex 日志、session、SQLite 状态库、本地缓存。
- 本机 runtime 路径、插件缓存路径、项目 trust 快照。
- 本地项目代码、私有项目路径、个人项目状态。
- 构建产物、临时输出、大段 diff。

## 直接复制给 Codex

把下面这段发给另一台机器上的 Codex，它就可以按本仓库完成配置。执行前请先确认你愿意让 Codex 修改本机 `~/.codex` 和可选的 Vault 目录。

```text
请根据这个公开仓库配置我的 Codex Agent OS：

https://github.com/qingzhh/public-resources/tree/main/projects/codex-agent-os-config

要求：
1. 先只读检查我本机是否已有 ~/.codex/AGENTS.md、~/.codex/config.toml、~/.codex/agents/。
2. 在修改前备份已有文件，备份文件名带当前日期时间。
3. 将仓库中的 codex/AGENTS.md 合并或写入 ~/.codex/AGENTS.md。
4. 将仓库中的 codex/agents/planner.toml、worker.toml、reviewer.toml 安装到 ~/.codex/agents/。
5. 不要直接覆盖我的 config.toml；请读取 codex/config.example.toml，只合并 model、agents、plugins、features、memories 等通用配置。
6. 本机路径、project trust、MCP 本地命令和高权限 sandbox 配置必须让我确认后再写入。
7. 不要复制 auth.json、日志、session、sqlite、cache 或任何凭据。
8. 如果我要使用 Vault，请询问我的 Vault 根目录，再把 vault/ 下的模板合并到该目录。
9. 完成后验证 TOML 可解析、agent role 文件含顶层 developer_instructions，并汇报 changed files / verification / risks。
```

## 手动恢复

1. 备份本机现有 `~/.codex`。
2. 将 `codex/AGENTS.md` 合并到 `~/.codex/AGENTS.md`。
3. 将 `codex/agents/*.toml` 放入 `~/.codex/agents/`。
4. 参考 `codex/config.example.toml` 手动合并 `~/.codex/config.toml`，不要整文件覆盖。
5. 如果使用 Vault，把 `vault/` 中的模板合并到自己的长期记忆目录。

更多说明见 [docs/usage.md](docs/usage.md)、[docs/restore.md](docs/restore.md) 和 [docs/sanitization.md](docs/sanitization.md)。
