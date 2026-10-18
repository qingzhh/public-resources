# PI Desktop Fast 适配插件

基于社区 Fast 开关机制的 PI-Desktop 适配源码，插件 ID 与版本以 `manifest.json` 为准，保留 MIT 许可证。

- `main.cjs`、`desktop-bridge.cjs`、`desktop-policy.cjs`：宿主桥接和策略。
- `extension/`：请求层、状态管理及开关行为。
- `renderer/`：面板界面。
- `tests/`：Node.js 离线检查。

```sh
node --test tests/*.test.mjs
```

使用与清单匹配的 PI-Desktop 插件开发、校验和打包能力安装此源码。这里不附带旧安装包、模型凭据、私有提供商配置、缓存或会话。兼容性限于原实现支持的宿主接口，不保证任意未来版本通用。
