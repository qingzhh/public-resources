# Octopus 智能故障切换与健康控制补丁

基于 [Hureru/octopus](https://github.com/Hureru/octopus)，保留上游许可证。

- 基线提交：`0e1a7ee062c8a24a9c8b95e7d1778f265182a304`。
- 补丁来源提交：`44ecfb20dfd447ea75c9a42e3f8cef054feb5585`；只发布 `internal/` 与 `web/` 的源码增量。
- 功能包括故障切换、健康探测、限额、路由排序及相关控制界面与测试。
- `changes.patch` 是机器生成的 Git 差异；`overlay/` 提供同一组修改后的完整文件，可覆盖到严格匹配的基线源码。

## 使用

先取得上游并检出上述基线。在干净工作副本应用 `changes.patch` 或复制 `overlay/` 后，按上游文档安装依赖、执行测试和构建。不要把补丁直接覆盖到未知版本。

```sh
git clone https://github.com/Hureru/octopus.git octopus-source
cd octopus-source
git checkout 0e1a7ee062c8a24a9c8b95e7d1778f265182a304
git apply /path/to/changes.patch
go test ./internal/grouphealth ./internal/relay/balancer ./internal/task ./internal/webdav ./internal/update
```

这里不包含部署地址、账号、模型凭据、数据库、Trellis 任务、会话记录或原应用二进制。`upstream.json` 列出补丁文件及基线，便于比对。源码发布检查与功能测试结果以归集库发布清单为准；不表示已在任意新版上游验证。
