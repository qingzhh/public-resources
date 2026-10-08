# Sanitization

本仓库是脱敏模板，不是完整 `~/.codex` 镜像。

## 已排除

- `auth.json`
- token、key、cookie、凭据
- sqlite 状态库
- logs、sessions、cache、tmp
- 本机 runtime 路径
- 本机 MCP 可执行文件路径和环境变量
- 私有项目代码
- 私有项目状态
- 真实 project trust 快照
- 长命令输出和大段 diff

## 已保留

- 全局工作流规则
- subagent 模板
- Vault 规则
- profile 模板
- 项目状态模板
- 脱敏配置示例

## 发布前检查

建议在提交前运行：

```powershell
rg -n "gho_|ghp_|sk-[A-Za-z0-9]|Bearer|api[_-]?key|token|cookie|auth|session|UserHome|AppData|\.sqlite|logs_2|state_5|session_index" .
```

如果有命中，确认是否属于说明文本；真实密钥、本机路径或状态文件必须删除。

还应检查：

```powershell
rg -n "real-user-name|real-project-path|TODO_SECRET|PASSWORD" .
```

`<YOUR_...>` 这类占位符可以保留，真实路径和凭据不能保留。
