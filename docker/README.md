# Docker 项目

每个项目使用独立子目录。当前可用项目：

| 项目 | 用途 | 镜像获取方式 |
| --- | --- | --- |
| [tg-ed2k-reporter](tg-ed2k-reporter/) | Telegram / TXT ED2K 采集、清洗、永久去重、MS HASH 上报及中文网页管理 | Compose 本地构建 |
| [ptskit](ptskit/) | PTS 保种统计、API / RSS 候选、多实例 qB/TR 管理、持久补量、原生转种及观察式清理 | Compose 本地构建；手动 Actions 可发布 GHCR 镜像 |

## tg-ed2k-reporter

独立 Python / Docker 服务，支持直接 HASH 上报和 MS 原生插件读取共享 TXT 两种后端。公开 Telegram 页面和 TXT 提供 ED2K 材料，无需影片本体。服务使用自己的设置、私有认证和 SQLite 数据，不挂载原 MS 配置或 Docker socket。

默认采集 `regeng115`，首次回填最近 7 天，然后每 5 分钟增量检查。首次历史边界和游标持久保留；修改 `initial_days` 不会扩大已有边界。每周期最多处理 20 条；直接上报请求间隔 5 秒，插件模式默认每 15 秒检查日志与云端结果。

文件名、大小和完整 MD4 有效时，可以补齐 `|/` 结束符并去除复制日志报错尾部；缺失材料不猜测。按 MD4＋大小永久去重，同时保留各个来源。云端已有、创建成功和更新成功分别保存回执；暂时性错误有限退避，认证失败暂停，写请求中断后先回查。

### 中文管理页面

页面显示上报成功、云端已有、等待处理、失败记录，以及采集来源、最后同步时间、下次运行时间和近期活动。支持名称或 MD4 搜索、状态筛选、分页、完整链接详情与复制。

可以粘贴 ED2K 文本或导入 UTF-8/BOM TXT，先预览新增、重复、修复和非法项，再确认提交；网页单次上限 4 MiB。可下载全部规范链接、立即采集、暂停/恢复上报、重试单条或全部失败项，以及修改独立网页登录密码。手动暂停会跨重启保留，频道采集继续运行。认证暂停不会被“恢复上报”跳过，核对认证后可显式重试失败项。成功项和不确定请求不会被失败重试按钮重新提交。

HTTP 线程读取数据库快照，只把写操作提交给同一工作进程。网页会话最多 12 小时，重启或改密码后重新登录；密码仅保存 scrypt 摘要，修改后的摘要位于 `data/.web-auth.json`。所有写操作校验 CSRF，登录有失败次数限制，页面只接受私有配置内的 Host。Compose 默认只在指定 NAS 地址发布管理端口；下面的配置用于可信局域网 HTTP 访问。

### 准备私有配置

需要 Docker Compose、可用的 HTTP/HTTPS 代理、可访问的 MS 本地 API 和有效的云端认证。Compose 使用 bridge 网络，`MS_HOST` 和 `PROXY_HOST` 必须能从容器访问。

```sh
git clone --depth 1 https://github.com/qingzhh/public-resources.git
cd public-resources/docker/tg-ed2k-reporter
mkdir -p secrets data inbox
```

创建私有 `.env` 并替换占位符。`WEB_BIND` 填 NAS 局域网地址，管理页面在 `WEB_PORT` 访问；代理值只配置到该容器。

```dotenv
TG_PROXY=http://PROXY_HOST:7890
MS_NO_PROXY=localhost,127.0.0.1,MS_HOST
WEB_BIND=NAS_LAN_IP
WEB_PORT=8890
```

创建 `secrets/report.json`：

```json
{
  "ms_url": "http://MS_HOST:8888",
  "ms_api_key": "YOUR_MS_API_KEY",
  "email": "YOUR_MS_EMAIL",
  "slogan": "YOUR_MS_SLOGAN",
  "driver_name": "115 Open"
}
```

`ms_api_key` 用于本地 MS 识别，`email`、`slogan` 用于云端认证，驱动名称应与 MS 保持一致。可选 `cloud_url` 默认使用程序内置云端入口。频道、间隔、历史范围和重试策略在 `settings.json` 设置。

先构建镜像，再以交互方式生成独立网页登录配置。用户名是 `admin`；下面的命令读取密码时不会回显，NAS 地址或主机名应与浏览器访问地址一致。

```sh
docker compose build reporter
docker run --rm -it --user 0:0 -v "$PWD/secrets:/output" --entrypoint python tg-ed2k-reporter:1.2.0 -c 'import getpass,json,os; from pathlib import Path; from dashboard import password_record; host=input("NAS IP or hostname: ").strip(); record=password_record("admin",getpass.getpass("Web password (12-128 characters): ")); record.update(allowed_hosts=[host],secure_cookie=False); path=Path("/output/web.json"); f=path.open("x",encoding="utf-8"); os.chmod(path,0o600); json.dump(record,f); f.close()'
```

容器使用 UID/GID `65532:65532`。设置数据及私有文件权限，同时保留当前 NAS 用户写入 inbox 的能力：

```sh
chmod 0600 .env
sudo chown 65532:65532 secrets/report.json secrets/web.json data
sudo chmod 0400 secrets/report.json secrets/web.json
sudo chmod 0700 data
sudo chown "$(id -u):65532" inbox
sudo chmod 2775 inbox
```

`.gitignore` 排除 `.env`、`secrets/`、`data/`、`inbox/`、`queue/` 和私有升级备份；镜像构建上下文使用白名单。公开仓库不包含部署凭据、代理认证、SQLite、TXT 输入和日志。

### 启动与升级

```sh
docker compose config --quiet
docker compose up -d --no-build reporter
docker exec tg-ed2k-reporter python /app/app.py health
```

浏览器打开 `http://NAS_LAN_IP:8890/`（替换成私有 `.env` 中的地址和端口），使用独立账号登录。镜像名为 `tg-ed2k-reporter:1.2.0`。容器使用非 root、只读根、丢弃 capabilities、禁止新增权限、受限资源和日志，并按 `unless-stopped` 自动重启。健康检查同时验证工作进程心跳和启用的网页服务。具体上报结果以页面状态和回执为准。

升级已有 1.0 服务时先备份 Compose、私有配置及 SQLite，保留原 data 和 inbox；补充 `WEB_BIND/WEB_PORT` 与 `secrets/web.json`，更新源码并重新构建，最后执行 `docker compose up -d reporter`。不删除运行数据，修改后的网页密码也会保留。回退时可恢复原 Compose、原源码和 1.0 镜像入口；只停止新服务也不会影响原 MS。

### MS 插件读取待上报 TXT

在已有 MS 的 `/downloads` bind 挂载对应主机目录内创建 `.tg-ed2k-queue/normalized.txt`，保持 MS 配置目录独立。将主机队列目录写入私有 `.env` 的 `QUEUE_DIR`，Compose 会把同一目录挂到采集容器的 `/queue`。队列目录由 UID/GID `65532:65532` 写入，文件保持 `0600`；MS 运行用户需要读取权限。初次接入使用空文件。

在 MS 新建专用“ED2K 链接 HASH 上报”实例：驱动与私有认证一致，列表文件填写 `/downloads/.tg-ed2k-queue/normalized.txt`，直接输入留空、Cron 留空、并发为 1，并开启消息推送。保留已有插件实例。将下面字段并入 `settings.json`；示例 ID `1` 需要替换为新实例的真实数字 ID。

```json
{
  "report_backend": "ms_plugin",
  "ms_plugin": {
    "instance_id": 1,
    "queue_file": "/queue/normalized.txt",
    "check_seconds": 15,
    "timeout_seconds": 900
  }
}
```

单一工作进程先回查永久去重记录，再原子发布批次并触发专用实例。只把本批需要上传的链接写为有效行；其它未确认项以 `# 状态 链接` 注释保留。批次运行时 TXT 保持不变，后续采集继续记入 SQLite。读取专用实例的“ED2K HASH 上报完成”日志后逐条回查云端，最终确认的项从共享 TXT 移出，网页历史永久保留。插件确认记录不区分新增或更新。

失败项保留并有限退避；触发请求超时、服务重启或缺少批次结束日志时先回查，保存日志边界与 TXT 校验和，避免盲目再次触发。手动暂停停止新批次，已触发批次继续确认。共享文件被外部改写会暂停交接。MS 消息推送提供批次汇总，逐条状态以网页回执为准。

`data/normalized.txt` 和网页下载保留全部规范链接，插件使用 `/queue/normalized.txt` 作为待上报队列。默认后端仍为 `direct`；切换后端前应完成活动批次的确认。云端 HASH 确认与媒体搜索可见性需要分别核对。

### TXT 与命令行

将 UTF-8/BOM TXT 放入新服务的 `inbox/`，下一周期自动导入，单文件上限 16 MiB。规范链接输出到 `data/normalized.txt`，永久状态在 `data/state.sqlite3`。保留数据库才能保留去重、历史边界和中断回查状态。

```sh
docker exec tg-ed2k-reporter python /app/app.py status
docker logs --tail 20 tg-ed2k-reporter
docker stop tg-ed2k-reporter
docker start tg-ed2k-reporter
```

网页重试无需停止服务。若使用独立 CLI 写命令，先停止常驻进程，再运行并恢复服务：

```sh
docker stop tg-ed2k-reporter
docker compose run --rm --no-deps reporter retry-failed
docker start tg-ed2k-reporter
```

默认镜像命令仍为 `run`；Compose 通过 `run --web` 启用页面。`collect`、`once`、`import`、`normalize`、`status`、`health` 等命令保持可用。外部采集和云端请求走新服务的私有代理，本地 MS 请求显式绕过代理。

### 验证

包含 153 项 Python 回归，覆盖原格式修复、TXT/BOM、永久去重、采集分页、可信空增量、固定历史边界、严格回执、有限重试、中断回查、网页登录与管理，以及插件批次冻结、原子写入、失败注释保留、日志边界、触发超时、文件变更保护和崩溃恢复。

```sh
python -m pip install -r requirements.txt
python -m unittest discover -p 'test_*.py'
docker run --rm --network none --tmpfs /tmp:rw -v "$PWD/settings.json:/app/settings.json:ro" --entrypoint python tg-ed2k-reporter:1.2.0 -m unittest discover -s /app -p 'test_*.py'
```

测试中的 Key、邮箱和密码均为无效测试数据。网页使用原生 HTML/CSS/JavaScript，无外部资源和构建步骤；浏览器验收覆盖登录、搜索分页、导入、任务轮询、暂停/恢复、失败重试、密码修改及桌面/手机布局。

## PTSkit

中文六页管理工具，首页优先显示后台站端统计缓存，本地库存与完整候选异步读取。保留永久身份去重、精确标签、独立刷新及中断恢复；自动化首次启动关闭，清理规则须明确保存并启用。

首次启动、私有占位配置、只读文件检查映射、升级回退与镜像发布步骤见 [PTSkit 说明](ptskit/README.md)。仓库根目录的 **Publish PTSkit image** 工作流仅手动触发；首次发布后须把 GHCR 包可见性设为 Public，当前发布平台为 linux/amd64。
