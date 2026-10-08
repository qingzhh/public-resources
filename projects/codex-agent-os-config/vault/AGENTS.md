# Vault Memory Rules

本目录是 Codex 的跨项目长期记忆库。

目标：
- 保存长期有效的信息
- 降低 context rot
- 支持 Durable Threads
- 支持跨项目 workflow
- 让未来的 Codex 能快速接手工作

允许记录：
- 项目状态
- TODO
- decisions
- checkpoints
- workflows
- profiles
- lessons learned
- 风险
- blocker
- 下一步

禁止记录：
- 长日志
- 大段命令输出
- 临时推理
- 无意义聊天
- 重复内容
- 未验证结论
- 构建产物
- 大文件
- 敏感信息

更新原则：
- 有实质性新信息时再修改
- 优先追加 checkpoint，而不是重写全文
- 保持文件稳定、可读、低噪声
- 记录决策时写清日期、背景、方案、最终决定、影响范围
- 记录任务时写清状态、下一步、未决风险
- 不要让单个项目的规则污染整个 Vault

分层原则：
- ~/.codex/AGENTS.md 管全局人格
- <YOUR_VAULT_ROOT>/profiles/ 管能力模板
- <YOUR_VAULT_ROOT>/projects/ 管项目状态
- <YOUR_VAULT_ROOT>/checkpoints/ 管阶段结果
- <YOUR_VAULT_ROOT>/decisions/ 管长期决策
- <YOUR_VAULT_ROOT>/lessons/ 管踩坑经验
- <YOUR_VAULT_ROOT>/workflows/ 管可复用流程
