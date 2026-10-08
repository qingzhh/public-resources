#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${1:-/opt/u2-qb-bot}"
SERVICE_NAME="${2:-u2-qb-bot}"
WEB_SERVICE_NAME="${SERVICE_NAME}-web"
TELEGRAM_SERVICE_NAME="${SERVICE_NAME}-telegram"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
WEB_SERVICE_FILE="/etc/systemd/system/${WEB_SERVICE_NAME}.service"
TELEGRAM_SERVICE_FILE="/etc/systemd/system/${TELEGRAM_SERVICE_NAME}.service"
LOGROTATE_FILE="/etc/logrotate.d/${SERVICE_NAME}"

if [ ! -d "$INSTALL_DIR" ]; then
  echo "安装目录不存在：$INSTALL_DIR" >&2
  exit 1
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=U2 qB Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/u2_service.sh start
ExecStop=$INSTALL_DIR/u2_service.sh stop
ExecReload=$INSTALL_DIR/u2_service.sh restart
PIDFile=$INSTALL_DIR/u2_qb_bot.pid
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat > "$WEB_SERVICE_FILE" <<EOF
[Unit]
Description=U2 qB Bot Web Console
After=network-online.target ${SERVICE_NAME}.service
Wants=network-online.target

[Service]
Type=forking
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/u2_service.sh web-start
ExecStop=$INSTALL_DIR/u2_service.sh web-stop
ExecReload=$INSTALL_DIR/u2_service.sh web-restart
PIDFile=$INSTALL_DIR/u2_web_ui.pid
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat > "$TELEGRAM_SERVICE_FILE" <<EOF
[Unit]
Description=U2 qB Bot Telegram
After=network-online.target ${SERVICE_NAME}.service
Wants=network-online.target

[Service]
Type=forking
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/u2_service.sh telegram-start
ExecStop=$INSTALL_DIR/u2_service.sh telegram-stop
ExecReload=$INSTALL_DIR/u2_service.sh telegram-restart
PIDFile=$INSTALL_DIR/u2_telegram_bot.pid
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat > "$LOGROTATE_FILE" <<EOF
$INSTALL_DIR/u2.log $INSTALL_DIR/u2_web.log $INSTALL_DIR/u2_telegram.log {
    size 20M
    rotate 5
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl enable --now "$WEB_SERVICE_NAME"
systemctl status --no-pager "$SERVICE_NAME"
systemctl status --no-pager "$WEB_SERVICE_NAME"
if grep -Eq '^telegram\.bot_token=.+$' "$INSTALL_DIR/config.properties" && grep -Eq '^telegram\.chat_id=.+$' "$INSTALL_DIR/config.properties"; then
  systemctl enable --now "$TELEGRAM_SERVICE_NAME"
  systemctl status --no-pager "$TELEGRAM_SERVICE_NAME"
else
  systemctl disable --now "$TELEGRAM_SERVICE_NAME" >/dev/null 2>&1 || true
  echo "Telegram 服务未启用：缺少 telegram.bot_token 或 telegram.chat_id"
fi
echo "logrotate 配置已写入：$LOGROTATE_FILE"
