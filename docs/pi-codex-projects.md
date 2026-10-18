# PI / Codex 开源改进项目

本页只介绍可公开的上游改进与通用适配代码。第三方版权、许可证和固定基线保留在各子目录，个人部署配置不属于源码归集范围。

| 项目 | 上游与改进 | 使用入口 |
| --- | --- | --- |
| [Octopus](../patches/octopus-smart-routing/) | Hureru/octopus；故障切换、健康探测与路由控制；补丁及覆盖文件可追溯到固定提交 | 先阅读 upstream.json；应用到匹配基线，或使用目录中的 Dockerfile 从源码构建 |
| [WorkBuddy Hub](../patches/workbuddy-hub-ops/) | ardeyouxipianyi/workbuddy2api-hub v1.7.2；运维增强模块、基线校验与回退 | apply.py --help；手动构建 Dockerfile 或对严格匹配的已有源码应用补丁 |
| [PI Desktop Fast](../plugins/pi-openai-fast/) | MIT 插件机制的桌面适配；开关、模型状态及请求策略 | manifest.json、README.md 与 TESTING.md |
| [Sub2API Priority](../docker/sub2api-antigravity-priority/) | Sub2API 与 Antigravity Priority 固定版本集成 | 既有 README、prepare_source.py、build.sh 和恢复工具 |

## 已发布的上游修复

- [Sub2API #8141](https://github.com/Wei-Shaw/sub2api/pull/8141)：Gemini 工具调用轮次的进度正文兼容。
- [Antigravity Priority #5](https://github.com/ygq-future/antigravity-priority/pull/5)：额度字段别名和百分数字符串解析。

这些修复已在对应 fork / PR 中公开，本次仅提供索引，不复制其 Git 历史或改动现有 PR。

## Docker 镜像

手动工作流为 `.github/workflows/publish-derivative-containers.yml`，支持三个项目及独立快照标签：

```text
ghcr.io/qingzhh/octopus-smart-routing:archive-20261018
ghcr.io/qingzhh/workbuddy-hub-ops:archive-20261018
ghcr.io/qingzhh/sub2api-antigravity-priority:archive-20261018
```

上述是发布目标；镜像是否已生成、实际摘要和匿名访问状态以对应 Actions 成功回执为准。工作流会记录源码提交、认证回拉和匿名 manifest 结果。源码公开不代表 GHCR 包自动公开；首次包需要核对可见性。标签不覆盖既有 Latest。

Octopus 从固定上游与源码覆盖文件构建前端及 Go 程序；WorkBuddy 从固定上游应用经摘要校验的源码包；Sub2API 复用固定工具链和原有 lint、回归、前端及嵌入式程序构建，不上传原部署机器上的程序文件。

## 检查范围与隐私边界

- 只包含可再分发源码、许可证、固定基线、通用测试及示例说明。
- 不包含私有账号、认证、真实请求记录、会话、运行数据库、队列、日志和部署备份。
- WorkBuddy 循环检测夹具为虚构文本，安装器已在隔离基线副本验证安装、幂等与回退。
- Fast 的测试需要 Node 支持直接加载 TypeScript；宿主集成测试有单独的显式环境入口，缺少宿主时标记跳过，不等同于验证通过。
- 补丁对未知上游版本的兼容性未承诺；使用前按各目录的基线要求核对。
