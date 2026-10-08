# Docker 项目

每个项目使用独立子目录。当前可用项目：

| 项目 | 用途 | 镜像获取方式 |
| --- | --- | --- |
| [tg-ed2k-reporter](tg-ed2k-reporter/) | Telegram / TXT ED2K 采集、清洗、永久去重及 MS HASH 上报 | 按下文使用 Compose 本地构建 |

## tg-ed2k-reporter

独立运行的 Python / Docker 服务，通过已有 MS 做媒体识别并向云端提交 HASH。公开 Telegram 页面和 TXT 提供 ED2K 材料，无需影片本体；新服务使用自己的设置、私有认证和 SQLite 数据，不挂载原 MS 配置或 Docker socket。

- 默认采集 `regeng115`，首次回填最近 7 天，然后每 5 分钟增量检查；初次历史边界和游标持久保留。
- 按 MD4＋文件大小永久去重，并保留来源。文件名、大小和完整 MD4 有效时自动补齐 `|/` 结束符、去除扩展尾字段与复制日志的报错尾部；缺失材料不猜测。
- 支持 UTF-8 和 UTF-8 BOM TXT，单文件上限 16 MiB。运行时由同一工作进程导入 `inbox/*.txt`，避免锁争用。
- 云端已存在、创建成功和更新成功分别保存回执。暂时性错误有限退避，认证失败暂停；写请求中断或结果不确定时先回查。
- 每周期最多处理 20 条、请求间隔 5 秒。当前决策协议和旧资源行协议均有兼容处理，服务端要求未知 HASH 材料时保留错误。

### 准备私有配置

需要 Docker Compose、可用的 HTTP/HTTPS 代理，以及可访问的 MS 本地 API 和有效的云端认证。Compose 默认使用 bridge 网络，`MS_HOST` 与 `PROXY_HOST` 必须能从该网络访问。

进入源码目录，准备运行目录：

```sh
git clone --depth 1 https://github.com/qingzhh/public-resources.git
cd public-resources/docker/tg-ed2k-reporter
mkdir -p secrets data inbox
```

在此目录自行创建 `.env`，替换下面的占位符。认证代理可在私有 URL 中包含账号信息，文件应只留在部署机器：

```dotenv
TG_PROXY=http://PROXY_HOST:7890
MS_NO_PROXY=localhost,127.0.0.1,MS_HOST
```

创建 `secrets/report.json`，替换所有占位符。`ms_api_key` 用于本地 MS 识别；`email`、`slogan` 用于云端认证；`driver_name` 与实际 MS 驱动名称保持一致：

```json
{
  "ms_url": "http://MS_HOST:8888",
  "ms_api_key": "YOUR_MS_API_KEY",
  "email": "YOUR_MS_EMAIL",
  "slogan": "YOUR_MS_SLOGAN",
  "driver_name": "115 Open"
}
```

可选字段 `cloud_url` 用于兼容部署的云端入口，默认使用程序内置入口。修改 `settings.json` 可调整频道、轮询间隔、首次历史范围与重试配置；已有状态初始化后，改变 `initial_days` 不会扩大原历史边界。

容器以 UID/GID `65532:65532` 运行。Linux/NAS 上设置数据和私有文件权限，并让当前 NAS 用户可以写入 inbox：

```sh
chmod 0600 .env
sudo chown 65532:65532 secrets/report.json data
sudo chmod 0400 secrets/report.json
sudo chmod 0700 data
sudo chown "$(id -u):65532" inbox
sudo chmod 2775 inbox
```

`.gitignore` 排除 `.env`、`secrets/`、`data/` 和 `inbox/`；镜像构建上下文使用白名单。公开仓库不包含任何部署凭据、代理认证、SQLite、TXT 输入或运行日志。

### 构建与启动

完成私有配置后校验、构建并启动：

```sh
docker compose config --quiet
docker compose build
docker compose up -d reporter
docker exec tg-ed2k-reporter python /app/app.py health
docker exec tg-ed2k-reporter python /app/app.py status
```

此项目当前使用本地构建镜像 `tg-ed2k-reporter:1.0.0`。Compose 限制资源和日志，使用非 root、只读根、无发布端口、丢弃 capabilities、禁止新增权限与 `unless-stopped`。

新容器的外部采集与云端请求使用上述代理，本地 MS 识别请求显式绕过代理。原 MS、原插件和其他容器沿用原配置。

### TXT 与日常操作

将 UTF-8 TXT 放入本目录的 `inbox/`，下一周期自动导入。规范链接输出到 `data/normalized.txt`，永久状态保存在 `data/state.sqlite3`。重复导入同一 MD4＋大小不会新增上报项；缺少 HASH 或大小的输入会记录解析错误。

```sh
docker exec tg-ed2k-reporter python /app/app.py status
docker logs --tail 20 tg-ed2k-reporter
docker stop tg-ed2k-reporter
docker start tg-ed2k-reporter
```

停止服务保留持久数据。需要显式重试失败项时，先停止常驻进程，再运行独立命令并恢复服务：

```sh
docker stop tg-ed2k-reporter
docker compose run --rm --no-deps reporter retry-failed
docker start tg-ed2k-reporter
```

`pending/retry/uncertain` 表示仍需处理；`reported` 表示创建或更新收到成功回执；`existing` 表示云端已有。健康状态只反映进程心跳，需同时检查队列和 `report_pause`。保留 SQLite 才能保留永久去重和中断回查状态。

### 验证

源码内包含 84 项回归，覆盖格式修复、TXT/BOM、去重、分页失败、可信空增量页、固定历史边界、严格回执、媒体识别、认证暂停、有限重试、中断回查、CLI 和本地 HTTP 模拟。

在源码目录执行：

```sh
python -m unittest discover -p 'test_*.py'
docker run --rm --entrypoint python tg-ed2k-reporter:1.0.0 -m unittest discover -s /app -p 'test_*.py'
```

已在 Windows Python 3.14 和 Linux Python 3.11 验证；独立 NAS 部署还通过真实创建/更新回执与云端回查、TXT 重复导入、隔离代理故障恢复、重启持久化以及原 MS 保持核验。测试里的 Key、邮箱和口令均为无效测试数据。
