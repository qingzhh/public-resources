# public-resources

公开配置、工具及项目的统一入口。当前包含 PT DNS 模块；Docker 和安卓目录为未来项目预留，并不包含已经实现的应用。

| 路径 | 内容 |
| --- | --- |
| `network/pt-dns/` | 域名源清单、Surge DNS 模块及分流规则 |
| `scripts/` | 自动生成脚本 |
| `docker/` | Dockerfile、Compose 示例和镜像使用说明 |
| `android/` | 安卓项目源码及版本说明 |
| `docs/` | 公共使用文档 |

## PT DNS 的维护方式

只维护 `network/pt-dns/domains.json`。`scope: exact` 只匹配指定主机；`scope: suffix` 匹配根域名及所有子域名。

初始清单来自用户提供的 33 个后缀规则，加上已经验证 DNS 覆盖有效的 `u2.dmhy.org`（精确匹配），共 34 项。其他站点尚未逐项验证 DoH 效果；可先用 `u2-only.sgmodule`，再启用完整模块。

本仓库清单只是已有站点集合，不是完整 PT 目录，也不声称所有域名都存在 DNS 污染。

网页编辑 JSON 并提交到 main 后，GitHub Actions 会生成并提交两个文件：

- `pt-dns.sgmodule`：仅 `[Host]`，使用 Cloudflare DoH，不改变出站策略。
- `pt.list`：不带策略名称的规则集，可在自己的主配置里绑定 `PT站点`。

若 main 禁止机器人直接推送，改为在本地执行下面的命令并随源码提交生成文件；不要为了这个工作流关闭分支保护。

```bash
python scripts/build_pt_dns.py
python scripts/build_pt_dns.py --check
```

## iPhone Surge 使用方法

在 Surge 的模块管理中添加以下远程模块 URL：

```text
https://raw.githubusercontent.com/qingzhh/public-resources/main/network/pt-dns/pt-dns.sgmodule
```

先启用模块，清除 Surge DNS 缓存，再分别验证 Wi-Fi、蜂窝网络访问，以及实际 DNS 和连接日志。现有 `[General]` 和 PT 策略保持不变。不需要添加 MITM 或固定网站 IP。

不要同时启用完整模块和 u2-only 模块。完整模块确认有效后，可删除主配置中重复的 U2 `[Host]` 项，让远程模块成为唯一维护点。

如果暂时只验证 U2，使用：

```text
https://raw.githubusercontent.com/qingzhh/public-resources/main/network/pt-dns/u2-only.sgmodule
```

清单更新后，在 Surge 中更新该远程模块；GitHub 发布更新不等于设备已经更新。

### 可选：统一已有分流规则

验证模块后，可将原先手写的 33 条 DOMAIN-SUFFIX 规则替换为下面一条，放在原来的 blackmatrix7 规则之前：

```ini
[Rule]
RULE-SET,https://raw.githubusercontent.com/qingzhh/public-resources/main/network/pt-dns/pt.list,PT站点
RULE-SET,https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Surge/PrivateTracker/PrivateTracker.list,PT站点
# 原有 blackmatrix7 PrivateTracker RULE-SET 继续保留
# FINAL 仍然放在末尾
```

`pt.list` 只覆盖本仓库域名；保留 blackmatrix7 能维持原有其他站点的分流，但这些额外站点不会自动获得本模块的 DoH 覆盖。

需要为额外站点覆盖 DNS 时，将经过核对的域名添加到 JSON。暂不自动复制第三方清单，以避免未知域名、授权和上游变更直接扩大 DNS 覆盖范围。

### 兼容性与故障处理

Surge 官方的 `[Host]` 远程 DOMAIN-SET/RULE-SET 引用章节标注 Mac 5.10.0+，本方案不依赖该语法，使用普通 Host 行的模块兼容当前 iPhone 用法。

根域名与 `*.域名` 分别生成，避免 `*example.com` 意外匹配 `fakeexample.com`。

DNS 正确不代表直连线路一定畅通。Cloudflare DoH 在不同网络上的可达性需要测试；U2 当前已验证可用。如果完整模块启用后其他站点变慢，先禁用它并退回 U2 单域模块，再缩小清单。

DoH 加密传输不能保证解析器返回内容正确。此次“仅更换 U2 解析器，DIRECT 即恢复”的结果强烈支持解析路径问题，但不能单凭这一点确定具体污染来源或广告页由谁注入。

## Docker / 安卓发布约定

- Docker：仓库保存 Dockerfile 和 Compose 示例；构建后的镜像推送到 `ghcr.io/qingzhh/<应用名>`。GHCR 包的可见性独立于仓库，发布后需确认设为 Public。
- Android：仓库保存源码，签名 APK/AAB 作为 Releases 附件，发布标签建议使用 `android-<应用名>-v1.0.0`。
- 大型独立应用成熟后可拆到独立仓库，本仓库 README 保留导航链接。
- 公共仓库只放可公开的配置和代码。不要上传 PT passkey、订阅链接、节点密码、Android 签名密钥或构建私密配置。
- 第三方内容需按各自授权处理；当前骨架没有默认附加开源许可证，正式发布源码时请明确选择许可证。

## 维护仓库

仓库地址：https://github.com/qingzhh/public-resources

最简单的维护方式：在 GitHub 网页中编辑 `network/pt-dns/domains.json`，提交到 main，然后在 Actions 查看 Generate PT DNS 的结果。工作流成功后，在 Surge 更新远程模块。

本地维护可先 clone，再编辑清单并生成文件：

```bash
git clone https://github.com/qingzhh/public-resources.git
cd public-resources
python scripts/build_pt_dns.py
python scripts/build_pt_dns.py --check
```

提交清单和生成文件后推送即可。此仓库的远程配置会公开影响订阅者，新增域名请核对站点来源和匹配范围。

## 官方参考

- https://manual.nssurge.com/dns/local-dns-mapping.html
- https://manual.nssurge.com/profile/module.html
- https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry
- https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases
