# Surge DNS：国内默认 + 日港代理例外

版本：2026-10-12-r2。此版本是配置设计，不是已完成的逐站 IP 归属或用户网络测速报告。未增加定时任务。

## 模块订阅地址

```text
https://raw.githubusercontent.com/qingzhh/public-resources/main/network/surge/dns-balanced.sgmodule
https://raw.githubusercontent.com/qingzhh/public-resources/main/network/surge/general-balanced.sgmodule
https://raw.githubusercontent.com/qingzhh/public-resources/main/network/pt-dns/pt-dns.sgmodule
```

停用旧 QingRex DNS、通用模块和重复的 u2-only 模块，只启用一套。其他会覆盖 DNS、Host 或拦截 DoH 的模块需另行检查。模块优先于主配置；自定义代理策略和引用它的规则必须写在主配置。

## 主配置一次性修改

以下是分段修改点，不是可替换整个主配置的完整文件。保留节点、订阅、Tailnet、Emby 和证书。不要公开完整个人配置。

### General：替换同名字段

```ini
use-local-host-item-for-proxy = false
dns-server = 223.5.5.5, 119.29.29.29
encrypted-dns-server = https://dns.alidns.com/dns-query, https://doh.pub/dns-query
encrypted-dns-follow-outbound-mode = true
encrypted-dns-skip-cert-verification = false
hijack-dns = *:53
allow-dns-svcb = false
```

两家国内 DoH 并发，不是主备。传统 DNS 仍可能用于 DoH 域名引导和连通性测试；私有名称保留明确的 system 例外。不承诺零明文 DNS。保留原 ipv6=true、ipv6-vif=auto 及局域网/CarPlay 接管设置。

### Proxy Group：新增一行

```ini
DNS-境外 = select, "🇯🇵 日本", "🇭🇰 香港", include-all-proxies=0
```

复用主配置已有国家组。默认日本，香港可手动切换；没有 DIRECT 选项。这是 select，不是自动故障转移。

**必要前提：**最终选中的代理节点应以实际 IP 作为服务器地址。Surge 原生加密 DNS 命中域名形式的代理服务器时，可能为避免解析回环而回退 DIRECT。仅仅从策略组里去掉 DIRECT 不能阻止这种内部回退。链式代理也需检查依赖；不要随便固定 IP 或关闭 TLS/SNI 校验。不能满足此前提时，不应声称已保证境外 DNS 必经代理。

### Rule：前三条放在现有规则最前面

```ini
DOMAIN,dns.alidns.com,DIRECT
DOMAIN,doh.pub,DIRECT
DOMAIN,cloudflare-dns.com,DNS-境外
```

已有 DOMAIN,cloudflare-dns.com,Proxy 时替换，不要重复。原 DOMAIN,u2.dmhy.org,DIRECT 保留；其他 PT、Tracker 和业务流量策略保持原样。DNS 走代理不意味着网站或下载流量走代理。

在所有业务规则之后，将唯一的 FINAL 改为：

```ini
FINAL,DIRECT
```

去掉旧 dns-failed；不能解析的直连域名不会因此自动恢复。使用规则模式。原漏网之鱼策略组可以保留但不再引用。

给原来没有 no-resolve 的混合 RULE-SET 行追加 ,no-resolve，特别是两条 PT 规则；已有的不重复添加。这只避免为了匹配 IP 规则提前解析，不省略 DIRECT 连接必需的 DNS。

### Host

注释/删除 u2.dmhy.org = 104.25.27.31 和历史 doh.pub = 1.12.12.12, 120.53.53.53。阿里引导映射改为：

```ini
dns.alidns.com = 223.5.5.5, 223.6.6.6
```

保留其他私有设备映射，不固定普通 PT 网站 IP。阿里引导用 IPv4 不等于禁用网站 AAAA。

### MITM 与兼容列表

在 MITM 段设置 skip-server-cert-verify = false；不要改 CA 字段。通用模块会覆盖 skip-proxy/always-real-ip，主配置也可同步复制模块中这两行。它们不是直连规则，也不是 DNS 上游选择。负向 MITM hostname 会停用对应主机依赖解密的脚本。

## PT 怎样区分国内/境外 DNS

默认未列入 PT DNS 模块的域名使用国内双 DoH；保留原 34 项境外 DNS 例外。保留原名单是兼容性选择，不代表已经证明这 34 项全部污染或服务器都在境外。

主配置的 PrivateTracker 远程表含 248 条规则，混有公开 Tracker、IP 和 torrent/announce 等宽泛关键词，不能整表当成境外 DNS 名单。例如 hdhome.org、hdsky.me、ourbits.club、pthome.net、hddolby.com、hdtime.org、m-team.cc 未列入原 34 项，按本方案默认国内解析；这是策略安排，不是服务器位于中国大陆的结论。

网站与 Tracker 分开看：主表里的 tracker.qingwapt.org 不被 qingwapt.com 的 Host 后缀映射覆盖。

IP 所在地只能辅助判断。Cloudflare 等 CDN 可能返回 Anycast 边缘 IP，不是源站地址；A/AAAA、API/Tracker 子域以及不同网络的答案可能不同。国内 DoH 正常解析到海外 IP 不必改境外；国内失败而日港代理 DoH 正常时才有依据新增例外。换 DNS 不能修复网站 DIRECT 路由本身不通。

本次未能取得有效的实时 A/AAAA 批量结果，也未在用户 Wi-Fi/蜂窝及实际代理节点上测试，因此没有按未经验证的 IP 归属将原 34 项强行重新分配。

## 单独把已验证站点改为国内 DNS

编辑 network/pt-dns/domains.json，保留 domain/scope，给已验证条目增加 resolver=default，例如下面仅作格式演示：

```json
{"domain":"example.com","scope":"suffix","resolver":"default"}
```

生成器只取消该域名的 DNS 覆盖，让其继承全局国内双 DoH，同时保留 pt.list 的流量规则。不要为了换 DNS 删除整个条目，否则会连带删除自有 PT 分流规则。也支持单独指定一个 HTTPS DoH URL，需要同步考虑其出站规则。

```sh
python scripts/build_pt_dns.py
python scripts/build_pt_dns.py --check
```

仓库已有 Generate PT DNS 工作流会在 JSON/脚本修改后重新生成；本次不改变该工作流，不新增计划任务。不要直接修改生成后的 sgmodule。

## 验收与回退

清理相关缓存后，核对真正的原生 DNS 请求 cloudflare-dns.com:443 命中 DNS-境外和日本/香港实际节点，没有 DIRECT 回退；再独立核对 U2 网站命中 DIRECT。分别测试 Wi-Fi 和蜂窝。国家组名称不等于 Cloudflare 实际 Anycast 节点所在地。

本地静态检查和生成器测试不等于 Surge 真机导入或线路测试。回退需同时恢复主配置备份和旧模块状态；只停用模块不撤销手工改动。

## 参考

参考 QingRex/LoonKissSurge DNS/通用模块重新整理；原始模块作者 VirgilClyne / GetSomeFries。

- https://manual.nssurge.com/dns/encrypted-dns.html
- https://manual.nssurge.com/dns/local-dns-mapping.html
- https://manual.nssurge.com/profile/module.html
- https://manual.nssurge.com/rules/final.html
- https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/
- https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/
