# U2 qB Bot v2.1

公开项目：`https://github.com/qingzhh/public-resources/tree/main/projects/u2-auto-follow-v2`

## 快速安装

Linux VPS 上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/qingzhh/public-resources/main/projects/u2-auto-follow-v2/scripts/install_u2_qb_bot.sh | bash
```

脚本会交互式要求输入：

- Web 端口
- Web 登录账号
- Web 登录密码
- qB 地址 / 用户名 / 密码
- U2 passkey
- CookieCloud 地址 / key / 密码

也支持命令行参数：

```bash
curl -fsSL https://raw.githubusercontent.com/qingzhh/public-resources/main/projects/u2-auto-follow-v2/scripts/install_u2_qb_bot.sh | bash -s -- \
  --web-port 18081 \
  --web-user admin \
  --web-pass 'strong-pass' \
  --qb-url 'http://127.0.0.1:18080' \
  --qb-user 'admin' \
  --qb-pass 'adminadmin' \
  --u2-passkey 'your-passkey' \
  --cookiecloud-url 'https://your-cookiecloud.example' \
  --cookiecloud-key 'your-key' \
  --cookiecloud-password 'your-password'
```

## 手动部署

Windows 端仍可使用：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy_vps_v2.1.ps1 -ConfigFile .\deploy.jsonc
```

使用前：

1. 复制 `deploy.template.jsonc` 为 `deploy.jsonc`
2. 填入自己的 VPS、qB、U2、CookieCloud 信息

## 发布资产

GitHub Release 附件：

- `u2-qb-bot-share-v2.1.zip`

## Web 控制台

- 默认端口：`18081`
- 默认用户名：`admin`
- 登录密码由安装时输入

## 目录

- `scripts/install_u2_qb_bot.sh`
  Linux 一键安装脚本
- `deploy_vps_v2.1.ps1`
  Windows 一键上传部署
- `config.template.properties`
  配置模板
- `deploy.template.jsonc`
  部署模板
- `install_systemd.sh`
  systemd 安装脚本
