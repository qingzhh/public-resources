# Profile: codex-workflow

## 适用场景

- Codex 多 Agent 工作流、上下文管理、Vault 记忆和长期项目协作。

## 工作原则

- planner 可管理流程，worker 执行，reviewer 只读审查。
- fresh context > long history。
- 保留 checkpoint，不保留长日志。
- 优先复用 profile，不复制重型项目规则。

## 常用验证

- 检查改动范围是否符合约束。
- 检查 AGENTS、profiles、projects 是否分层清晰。
- 检查是否记录了 changed files、verification、risks。

## 禁止事项

- 不把单个项目 SOP 写入全局 AGENTS。
- 不为每个项目创建重型 AGENTS.md。
- 不让 subagent 长期携带旧上下文。

## 交付格式

- workflow
- checkpoint
- verification
- next step
