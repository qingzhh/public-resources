# Sub2API · Antigravity Priority 恢复包

将原 Antigravity Priority v1.2.18 引擎及完整管理页面接入 Sub2API 0.2.16。包含 OAuth 宿主适配、额度查询、学习/冷却/调度反馈、管理员鉴权、原生优先级映射与负优先级编辑修复。

## 是否依赖 CPA

完成账户和插件状态迁移后，Sub2API 版本的插件使用自己的数据库、令牌刷新、HTTP 传输和持久缓存，运行时不需要 CPA 或 CPA 内的插件。停用 CPA 会使原 CPA 地址及其监控失效；这不代表 Sub2API 故障。账户被撤销、刷新令牌失效或删除 Sub2API 自身持久数据仍会影响运行。

本包没有账号、密钥、令牌、Cookie、运行缓存、身份映射、服务器配置或数据库。保留服务器本地的 PostgreSQL、Redis 和 `/app/data/antigravity-priority/`，其中学习状态和历史无法从公开源码重新生成。

## 一条命令恢复

适用环境：Linux amd64、Python 3.11+、Docker Compose v2；部署目录默认 `/opt/sub2api`，应用服务名默认 `sub2api`。服务应能正常启动，且仍保留插件的本地状态目录。

```sh
curl -fsSL https://raw.githubusercontent.com/qingzhh/public-resources/main/docker/sub2api-antigravity-priority/restore.sh | sudo bash -s -- --directory /opt/sub2api
```

只读检查可在末尾加 `--check`；其他服务名/文件名可加 `--service NAME --compose-file FILE`。已安装同一验证程序时会检查并直接退出，不重复重建。下载失败或校验不符不会切换服务。

脚本会核对当前程序版本，校验压缩包和程序的 SHA-256，在本地保存原 Compose、环境文件、数据库归档和插件状态，保留原镜像，然后只重建应用。健康检查、程序摘要及插件页面不通过时自动回退；不会用旧数据库覆盖当前数据。原 Compose 的私有值和占位引用原样保留；恢复结果不会打印它们。

## 版本边界

当前发布：`0.2.16-agpriority.1.2.18.sync.1`，对应官方 **Sub2API 0.2.16**，Antigravity Priority **v1.2.18**。恢复包替换的是包含插件的完整应用程序，并非官方原生的 `.s2plugin`。

官方升级可能替换该定制程序，管理入口随之消失；学习/配置仍在本地持久目录中。**任意未来版本不能保证直接恢复本包**：如果实际运行版本不是 0.2.16，脚本会在改动前停止，不降级，不回滚数据库，不覆盖新版本代码。更新前先确认本目录已有匹配版本的适配发布；以后每个新版本需重新适配、测试并发布对应恢复程序。本发布仍可用于同版本官方镜像覆盖后的恢复。

## 备份和回退

私有备份保存于部署目录 `priority-restore-backups/<时间>/`，目录 0700、文件 0600。不要上传这些文件。

若需要撤回刚完成的恢复，使用输出中的备份路径：

```sh
sudo python3 /opt/sub2api/priority-restore-backups/<时间>/restore.py --rollback /opt/sub2api/priority-restore-backups/<时间>
```

回退仅切换配置和已保存的原镜像；数据库和插件状态保留。Compose 在恢复后又被其他操作修改时会拒绝覆盖，需人工合并。

## 源码重建

源码以 `engine/` 和 `overlay/` 保存；`source-manifest.json` 记录上游原文件及修改文件的摘要。重建工具先校验固定上游提交和全部覆盖文件，拒绝将覆盖文件用于未知版本。

```sh
python3 prepare_source.py /tmp/sub2api-priority-source
cd /tmp/sub2api-priority-source
GO=/usr/local/go/bin/go GOLANGCI_LINT=/path/to/golangci-lint bash build.sh
```

构建使用 Go 1.27.2、pnpm 9.15.9、golangci-lint 2.12.2；需自行预装 Go、Node、lint 和网络依赖。`build.sh` 执行原引擎 lint/测试、宿主/网关回归、前端 i18n/类型检查/构建和嵌入前端的后端构建。输出 `build/sub2api`，公开 Release 的程序是这一固定源码版本在 Linux amd64 上验证的结果。

Sub2API 上游和许可证见 `sources.json`、`LICENSE.sub2api`；Antigravity Priority 引擎保留 MIT 许可证 `engine/LICENSE`。修改版代码遵守各自上游许可证。通用恢复脚本可在仓库公开查看，程序校验值见 `release.json` 与 Release 附件 `SHA256SUMS.txt`。
