# Profile: mcp-dev

## 适用场景

- MCP server、tools、resources、connectors 和本地集成开发。

## 工作原则

- 明确工具边界、输入输出 schema 和错误处理。
- 优先小步验证，再接入真实客户端。
- 保持权限、凭据和网络行为可控。

## 常用验证

- schema 校验
- 本地启动检查
- 单个 tool 调用测试
- 客户端连接测试

## 禁止事项

- 不把密钥、token、长日志写入 Vault。
- 不默认扩大工具权限。
- 不在未验证前声称客户端可用。

## 交付格式

- tools/resources 变更
- 配置路径
- 验证命令
- 风险和限制
