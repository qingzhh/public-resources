# Sub2API · Antigravity Priority 恢复包

将 Antigravity Priority v1.2.18 原引擎与完整管理页面接入 Sub2API 0.2.16。当前版本 `0.2.16-agpriority.1.2.18.sync.3`，含额度查询、学习、冷却、调度反馈、管理员鉴权和优先级同步。

## 优先级和容量

插件优先级越大越优先，Sub2API 数值越小越优先。自 sync.2 起使用 **Sub2API = 1000 − 插件优先级**：999→1、100→900、99→901、冷却/降级−1→1001。插件紧迫度和算法保持原逻辑；原生同步数值默认折叠，日常主要看插件优先级。

sync.3 保存插件已经生效的优先级，每5秒检查一次原生值。自动应用开启、未暂停且处于允许时间窗口时，Sub2API 中的手工改值会恢复到插件保存值，数据库与调度缓存沿原生通知路径同步。暂停或关闭自动应用时不纠正漂移，恢复自动应用后再同步。额度探测和新优先级计算仍遵守原计划，默认15分钟；预览、预测值不会被当作已应用结果强制写入。

首次启用这项同步时采用数据库当时的有效值；以后插件成功写回或重置都会更新记录。记录持久保存在本地 `priority-authority.json`，重启后继续使用。重置后的缺失优先级和显式0仍分别保留。原生状态、认证错误、手动调度开关、凭据、extra和并发数不属于这项差异纠正的覆盖范围。

账户容量表示当前并发占用/本地并发上限。并发上限由管理员在账户设置中调整，不增加上游额度，插件和恢复工具不会覆盖该设置。

## 新增 Antigravity 账号

通过 Sub2API 新建或批量导入的 **Antigravity OAuth** 账号可动态进入插件，无需重启、编辑身份映射或重新迁移。新账号使用稳定的数据库ID身份，现有迁移账号的身份保持。API-key、upstream和其他平台账号不纳入。

添加后点击插件“同步”可立即出现在 Gemini 与 Claude/GPT 两组快照中；这个操作只更新列表和预览，不查额度。下一次自动运行会查询额度并参与主控组分配，也可手动“探测”后确认应用。页面的普通定时刷新只读取最近快照，因此新增后想立即查看应使用“同步”。原生手动停用、认证错误和手动暂停调度继续由 Sub2API 控制；停用账号仍可被插件读取，以验证和恢复插件自己的额度禁用。

新建 OAuth 未填写优先级时默认原生1000（插件逻辑0），首次计算前采用创建时的有效值；管理员明确填写的初值保留。账号需要有效的 OAuth 授权和 `project_id` 才能取得额度证据；缺少授权或额度查询失败时会显示失败，不会伪造额度或强行分配。

## 是否依赖 CPA

完成账户和状态迁移后，插件使用 Sub2API 自己的数据库、令牌刷新、HTTP 传输和缓存，运行时不需要 CPA。停 CPA 会影响原 CPA 入口及其监控；Sub2API 入口独立。

本包不包含账号、密钥、令牌、Cookie、缓存、身份映射、服务器配置或数据库。保留本地 PostgreSQL、Redis 和 `/app/data/antigravity-priority/`，特别是 `cache.json`、`identities.json`、`host-state.json`、`priority-mapping.json` 和 `priority-authority.json`。学习状态、历史及账号不能从公开源码重新生成。

## 一条命令恢复或更新适配

适用 Linux amd64、Python 3.11+、Docker Compose v2，默认部署目录 `/opt/sub2api`、服务名 `sub2api`。服务应能启动且保留本地状态目录。

```sh
curl -fsSL https://raw.githubusercontent.com/qingzhh/public-resources/main/docker/sub2api-antigravity-priority/restore.sh | sudo bash -s -- --directory /opt/sub2api
```

末尾加 `--check` 可只读预检；其他服务名/文件名可加 `--service NAME --compose-file FILE`。相同验证程序、映射和配置已就位时直接退出。脚本和程序均校验固定版本 SHA-256。

工具先私下保存 Compose、环境文件、可校验数据库归档和整个插件状态目录，保留原镜像；只停止/重建应用，保留 PG、Redis 等依赖。首次从 sync.1 更新时，在数据库事务中将现有 Antigravity OAuth 原生优先级加1000，记录映射版本并通知调度缓存刷新，不修改凭据、并发或学习状态。重复执行或从 sync.2 更新不会再次加1000。若同一组存在其他类型账户，首次转换会拒绝并要求检查相对优先级。

启动/校验失败自动回退原镜像和映射。回退仅逆向转换当前优先级，不回灌旧数据库，不覆盖后续凭据刷新或学习记录。请使用当前的一键入口；sync.1 旧程序不理解新的正数映射，不应直接覆盖 sync.2/3。

## 版本边界

本发布仅支持 **官方 Sub2API 0.2.16** 及已知 sync.1、sync.2、sync.3 适配。它替换包含插件的完整应用程序，并非官方 `.s2plugin`。官方升级可能替换定制程序，插件入口随之消失，但持久目录仍可保留。

未知新版会在改动前停止，不能用本包静默降级。升级 Sub2API 前先确认存在匹配版本的适配发布；以后每个新版本需要重新适配、测试和发布。未经评估，不应混用两个映射版本。

## 备份和回退

备份位于部署目录 `priority-restore-backups/<时间>/`，目录0700、文件0600，不要上传。

```sh
sudo python3 /opt/sub2api/priority-restore-backups/<时间>/restore.py --rollback /opt/sub2api/priority-restore-backups/<时间>
```

回退使用保存的原镜像，恢复先前映射。Compose 后来被其他操作修改时会拒绝覆盖，需要人工合并。数据库归档只供人工灾难恢复，本工具不会自动用旧归档覆盖当前数据。

若切换中断，重跑时会在当前 Compose 与备份候选一致的情况下提示具体回退命令。回退前先检查依赖身份；若 PG/Redis 后来已被重建，拒绝回退且不主动停止应用，需先核对当前部署。

## 源码重建

`engine/` 保留原引擎及 bridge，`overlay/` 保存宿主适配，`source-manifest.json` 校验固定上游和全部覆盖文件。

```sh
python3 prepare_source.py /tmp/sub2api-priority-source
cd /tmp/sub2api-priority-source
GO=/usr/local/go/bin/go GOLANGCI_LINT=/path/to/golangci-lint bash build.sh
```

需要 Go1.27.2、Node、pnpm9.15.9 和 golangci-lint2.12.2。引擎lint固定使用Go1.26.8，避免当前linter读取Go1.27编译导出格式失败；Go工具链机制会按需选择该版本。其余测试及后端构建使用Go1.27.2。完整构建执行引擎lint/测试、宿主与网关回归、前端i18n/类型检查/打包，再输出嵌入前端的 `build/sub2api`。已有部署应通过恢复工具完成映射迁移；直接启动新二进制但缺少映射标记会安全拒绝。

来源和许可证见 `sources.json`、`LICENSE.sub2api`、`engine/LICENSE`。发布摘要见 `release.json` 与 Release 的 `SHA256SUMS.txt`。本地源码、程序和恢复工具可公开；所有实际运行数据继续留在本机。
