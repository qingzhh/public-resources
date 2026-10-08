# Project: codex-os

## 当前目标

- 建立长期可复用的 Codex Agent OS：全局人格 + Vault Profiles + Project State + Checkpoints。

## 当前状态

- Vault 基础结构已规划，等待持续使用中追加真实 checkpoint。

## 关键约束

- 不把项目规则写入全局 AGENTS。
- 不记录长日志、临时输出、大段 diff 或未验证结论。
- 只有出现长期价值信息时才修改 Vault。

## 最近 checkpoint

- 2026-05-23：创建 Vault Agent OS 初始结构。

## 下一步

- 按任务选择 profile，并在项目文件中维护状态、下一步和风险。

## 未决风险

- 需要在实际项目中持续保持低噪声，避免把 Vault 变成长日志仓库。
