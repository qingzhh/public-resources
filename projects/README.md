# 公开项目

2026-10-08 将账号原有的六个非空公开仓库归集到本目录。每个项目保留原目录内容、Git 提交历史和许可证；根目录的 PT DNS 资源与工作流继续使用原路径。

| 目录 | 类型 | 上游或来源 |
| --- | --- | --- |
| [Auto-Seedbox-PT](Auto-Seedbox-PT/) | Fork 资源 | `yimouleng/Auto-Seedbox-PT` |
| [clash](clash/) | Fork 配置 | `liandu2024/clash` |
| [NextChat](NextChat/) | Fork 应用 | `ChatGPTNextWeb/NextChat` |
| [codex-agent-os-config](codex-agent-os-config/) | 自有配置 | 原 `qingzhh/codex-agent-os-config` |
| [pt-tracker-manager](pt-tracker-manager/) | 自有工具 | 原 `qingzhh/pt-tracker-manager` |
| [u2-auto-follow-v2](u2-auto-follow-v2/) | 自有工具 | 原 `qingzhh/u2-auto-follow-v2` |

## 本地使用

```bash
git clone https://github.com/qingzhh/public-resources.git
cd public-resources/projects/<项目目录>
```

进入项目目录后按该项目 README 安装依赖、构建或运行。原 Fork 的 `.github/workflows/` 保存在各自目录内，作为来源记录；它们不会作为本仓库根目录工作流执行。各应用的部署配置、外部服务及已安装客户端需要单独维护。

## 历史与版本

归集合并提交将六个原仓库的主分支提交作为父提交，原有提交仍可在本仓库 Git 历史中访问。原有标签放在 `source/<项目名>/<原标签>` 下，以区分不同项目的版本。

`u2-auto-follow-v2` 的原 `v2.1.0` 对应历史标签为 `source/u2-auto-follow-v2/v2.1.0`，迁移后的 Release 使用 `u2-auto-follow-v2-v2.1.0`，发布附件保留为 `u2-qb-bot-share-v2.1.zip`，可在本仓库 Releases 下载。这两个标签均指向原项目的版本提交，其源码归档保持原项目根目录结构。

未来发布建议用 `<项目名>-v<版本>` 命名新标签和 Release，并在发布说明中注明项目目录。

## 更新来源

Fork 项目以表中上游为来源。归集后按项目目录更新代码和配置，检查上游变更后再提交；GitHub 原仓库的 Fork 关系不会随目录合并转移。

许可证和版权声明以项目原文件为准。本仓库未给这些第三方或自有项目统一附加新的许可证。
