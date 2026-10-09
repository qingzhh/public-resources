# PTSkit

用于 PTS 保种任务的中文网页管理工具。支持多个 qBittorrent / Transmission 实例、站端与本地统计、API / RSS 候选、持久补量、原生 qB→TR 转种、精确标签管理、观察式清理与数字日志。

首页先读取后台已有站端统计缓存，再异步读取本地库存和完整候选。后台遵守保存的站端查询周期；统计跨重启保留，缓存过期或查询失败会标为旧记录。无法确认的数据显示未知，可信空记录才显示 0。

公开包包含通用代码、离线测试和占位配置。真实连接、凭据、历史任务、操作记录与个人 NAS 部署工具未包含。自动补量、自动转种和清理执行首次启动均关闭；在网页核对连接和规则后自行启用。

## 从源码启动

需要 Linux Docker 与 Docker Compose v2，以及可从容器访问的 qB Web API、Transmission RPC 和 PTS API。API 须支持 `/api/v1/seedkeep/refill`。网页登录使用已配置的 Transmission 用户名和密码。

```sh
git clone --depth 1 https://github.com/qingzhh/public-resources.git
cd public-resources/docker/ptskit
mkdir -p data
cp .env.example .env
cp examples/docker_settings.example.json data/docker_settings.json
cp examples/source.example.json data/source.json
cp examples/tr_credentials.example.json data/tr_credentials.json
```

启动前修改以下文件：

| 文件 | 需要填写 |
| --- | --- |
| `.env` | `WEB_BIND`、`WEB_PORT`；默认只绑定本机 `127.0.0.1`。需要局域网访问时填写 NAS 的局域网地址 |
| `data/source.json` | PTS 地址和 Token、qB 地址及账号密码、qB 容器内下载目录 |
| `data/tr_credentials.json` | Transmission 用户名和密码，也用于网页登录 |
| `data/docker_settings.json` | TR RPC 地址、维持目标、筛选参数和周期 |

Compose 使用 bridge 网络。示例中的 `qbittorrent`、`transmission` 名称需要在同一 Docker 网络中可解析；也可替换为容器可达的 NAS 地址。容器里的 `127.0.0.1` 指 PTSkit 自身。下载目录填写下载器看到的路径，例如 `/downloads`。

服务以 UID 0 运行，根文件系统只读，丢弃全部 capabilities，仅保留 `DAC_READ_SEARCH`；不挂载 Docker socket。将私有目录交给该 UID 并收紧权限：

```sh
sudo chown -R 0:0 data
sudo chmod 0700 data
sudo chmod 0600 data/*.json .env
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

访问 `.env` 对应地址的 `8786` 端口（或自己设置的 `WEB_PORT`），以 Transmission 账号登录。在“设置”和“下载器”核对参数及实例，再选择是否启用自动化。管理页优先放在可信局域网；远程访问可通过现有 VPN 或 HTTPS 反向代理。

## 可选的只读文件检查

普通统计不要求挂载下载内容。原文件检查或需要原文件证据的清理规则，应另外添加窄范围只读挂载。将 `PTS_INSPECT_PATH` 写入私有 `.env`，值为宿主机需要检查的下载子目录，然后使用：

```sh
docker compose -f compose.yaml -f compose.inspect.example.yaml config --quiet
docker compose -f compose.yaml -f compose.inspect.example.yaml up -d --build
```

在网页的“原文件检查映射”中，选实际实例，填写下载器内对应目录和 `/inspect/downloads`。该目录为只读；检查失败、证据不全或映射缺失显示未知，不提供自动删除资格。文件检查不会挂载 NAS 根目录。

## 发布镜像

仓库根目录的 `.github/workflows/ptskit-publish.yml` 仅支持手动触发。提交代码本身不会发布镜像。

1. 打开 `qingzhh/public-resources` 的 **Actions**，选择 **Publish PTSkit image**。
2. 点击 **Run workflow**，选择 `main`，输入版本号，例如 `1.0.0`。
3. 工作流先运行前端检查及离线 Python 回归，随后构建并推送 `ghcr.io/qingzhh/ptskit:1.0.0`；稳定版同时更新 `latest`，预览版只发布自己的版本标签。
4. 首次发布后，在账号 **Packages → ptskit → Package settings → Change visibility** 将包设为 **Public**。GHCR 包的可见性需要单独核对。
5. 在未登录 GHCR 的环境执行 `docker pull ghcr.io/qingzhh/ptskit:1.0.0`，确认其他人可以直接拉取。

该工作流使用仓库的 `GITHUB_TOKEN`，无需把个人 Token 存到源码。它发布 `linux/amd64` 镜像；ARM 设备可先从源码在本机构建。每个正式版本使用新的版本号，便于回退。

源码上传、镜像发布和部署是独立步骤。首次镜像发布成功之前使用前面的源码构建方式；本文中的 `1.0.0` 是发布示例。

使用已发布镜像时，把 `.env` 的 `PTS_IMAGE` 改为自己的正式版本：

```dotenv
PTS_IMAGE=ghcr.io/qingzhh/ptskit:1.0.0
```

保留私有配置后执行：

```sh
docker compose pull ptskit
docker compose up -d --no-build ptskit
```

## 升级与回退

升级前备份整个 `data/`、私有 `.env`、Compose 和当前镜像版本；其中包含永久去重、批次、恢复、操作记录、调度、设置及站端缓存。正在补量或转种时先等作业完成并在网页暂停自动化，再更新服务。升级后核对开关和运行状态，按原配置恢复。

源码构建：更新源码，运行 `docker compose up -d --build`。镜像部署：改为新版本后运行 `docker compose pull ptskit` 与 `docker compose up -d --no-build ptskit`。两者都保留原 `data/`。

回退时选回旧镜像版本，或使用备份中的旧源码重新构建。任务运行资料继续保留；只有经核对确实需要恢复时，才在停止服务后恢复对应数据备份。

## 验证与边界

```sh
python run_tests.py
node --check web/app.js
node test_dashboard_startup.mjs
docker build --target test -t ptskit:test .
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,size=64m ptskit:test
```

统计显示与执行预算分离；标签采用精确匹配，身份永久去重。站端独立刷新只读 API，本地独立刷新只查库存。未知、陈旧、连接失败和读取失败不当作 0。转种或增删请求结果不确定时先回读，自动清理须明确保存并启用。

`.gitignore` 排除私有配置与数据；Docker 构建使用白名单。维护发布时仅提交源码、测试、说明及 `*.example.json`。现有 README、Token 文件、日志、HexHub 导出和私人迁移脚本不要加入公开目录。

官方参考：[GHCR 容器仓库](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)、[GitHub Actions 发布镜像](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)、[Docker Compose build](https://docs.docker.com/reference/cli/docker/compose/build/)。
