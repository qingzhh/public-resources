# 开源改进镜像发布回执

发布批次：`archive-20261018`。以下三个镜像均已通过 GitHub Actions 构建、推送、认证回拉摘要检查及匿名 manifest 读取检查。

源码提交：[4c1a2a0](https://github.com/qingzhh/public-resources/commit/4c1a2a0bdb6f601d9358b88a67696ae6c0a71f80)。

构建记录：[Publish derivative containers #39147466511](https://github.com/qingzhh/public-resources/actions/runs/39147466511)。

| 镜像（统一前缀 `ghcr.io/qingzhh/`） | 标签 | 完整摘要 | 访问 |
| --- | --- | --- | --- |
| `octopus-smart-routing` | `archive-20261018` | `sha256:727d1a5db79633998b6e69903f117b960df718a93b4f0109babb1d00131fdfe9` | 公开 |
| `sub2api-antigravity-priority` | `archive-20261018` | `sha256:8a67e9fda584b2c504d2036f13a913726190846f594878093846b8f049196fe1` | 公开 |
| `workbuddy-hub-ops` | `archive-20261018` | `sha256:bb11e5c2ffaac86cd816cabbf807290ded96b843eae537666dfcf192b6cdf09a` | 公开 |

例如：

```sh
docker pull ghcr.io/qingzhh/octopus-smart-routing:archive-20261018
```

需要固定不可变版本时，使用 `ghcr.io/qingzhh/<项目名>@sha256:<完整摘要>`。本批使用独立快照标签，不变更共享仓库的默认 Latest Release。

项目来源、许可证、基线、补丁与构建入口见 [分类索引](pi-codex-projects.md)。镜像构建及回拉通过不代表已部署到用户设备，也不代表每个服务的真实外部接口已通过运行验收。运行配置、认证及数据应由使用者在自己的环境提供。
