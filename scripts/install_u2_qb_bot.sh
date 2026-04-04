#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="${REPO_OWNER:-qingzhh}"
REPO_NAME="${REPO_NAME:-u2-auto-follow-v2}"
RELEASE_TAG="${RELEASE_TAG:-v2.1.0}"
ASSET_NAME="${ASSET_NAME:-u2-qb-bot-share-v2.1.zip}"
DOWNLOAD_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}/releases/download/${RELEASE_TAG}/${ASSET_NAME}"

INSTALL_DIR="/opt/u2-qb-bot"
SERVICE_NAME="u2-qb-bot"
WEB_PORT="18081"
WEB_USER="admin"
WEB_PASS=""
QB_URL=""
QB_USER=""
QB_PASS=""
QB_CATEGORY="pt-u2"
U2_PASSKEY=""
COOKIECLOUD_URL="https://cocl.bigq.top:10100"
COOKIECLOUD_KEY=""
COOKIECLOUD_PASSWORD=""

usage() {
  cat <<'EOF'
Usage:
  curl -fsSL https://raw.githubusercontent.com/qingzhh/u2-auto-follow-v2/main/scripts/install_u2_qb_bot.sh | bash

Optional arguments:
  --install-dir PATH
  --service-name NAME
  --web-port PORT
  --web-user USER
  --web-pass PASS
  --qb-url URL
  --qb-user USER
  --qb-pass PASS
  --qb-category NAME
  --u2-passkey PASSKEY
  --cookiecloud-url URL
  --cookiecloud-key KEY
  --cookiecloud-password PASSWORD
  --release-tag TAG
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 1; }
}

prompt_value() {
  local var_name="$1"
  local label="$2"
  local current="$3"
  local required="${4:-0}"
  local value=""
  while true; do
    if [ -n "$current" ]; then
      read -r -p "$label [$current]: " value
      value="${value:-$current}"
    else
      read -r -p "$label: " value
    fi
    if [ "$required" = "1" ] && [ -z "$value" ]; then
      echo "$label is required." >&2
      continue
    fi
    printf -v "$var_name" '%s' "$value"
    return 0
  done
}

prompt_secret() {
  local var_name="$1"
  local label="$2"
  local current="$3"
  local required="${4:-0}"
  local value=""
  while true; do
    if [ -n "$current" ]; then
      read -r -s -p "$label [hidden, press Enter to keep current]: " value
      echo
      value="${value:-$current}"
    else
      read -r -s -p "$label: " value
      echo
    fi
    if [ "$required" = "1" ] && [ -z "$value" ]; then
      echo "$label is required." >&2
      continue
    fi
    printf -v "$var_name" '%s' "$value"
    return 0
  done
}

extract_zip() {
  local zip_path="$1"
  local out_dir="$2"
  if command -v unzip >/dev/null 2>&1; then
    unzip -q "$zip_path" -d "$out_dir"
  else
    python3 - "$zip_path" "$out_dir" <<'PY'
import sys, zipfile
zip_path, out_dir = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zip_path, 'r') as zf:
    zf.extractall(out_dir)
PY
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --service-name) SERVICE_NAME="$2"; shift 2 ;;
    --web-port) WEB_PORT="$2"; shift 2 ;;
    --web-user) WEB_USER="$2"; shift 2 ;;
    --web-pass) WEB_PASS="$2"; shift 2 ;;
    --qb-url) QB_URL="$2"; shift 2 ;;
    --qb-user) QB_USER="$2"; shift 2 ;;
    --qb-pass) QB_PASS="$2"; shift 2 ;;
    --qb-category) QB_CATEGORY="$2"; shift 2 ;;
    --u2-passkey) U2_PASSKEY="$2"; shift 2 ;;
    --cookiecloud-url) COOKIECLOUD_URL="$2"; shift 2 ;;
    --cookiecloud-key) COOKIECLOUD_KEY="$2"; shift 2 ;;
    --cookiecloud-password) COOKIECLOUD_PASSWORD="$2"; shift 2 ;;
    --release-tag) RELEASE_TAG="$2"; DOWNLOAD_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}/releases/download/${RELEASE_TAG}/${ASSET_NAME}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

require_command bash
require_command curl
require_command python3
require_command systemctl

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "Please run as root." >&2
  exit 1
fi

prompt_value INSTALL_DIR "Install directory" "$INSTALL_DIR" 1
prompt_value SERVICE_NAME "systemd service name" "$SERVICE_NAME" 1
prompt_value WEB_PORT "Web port" "$WEB_PORT" 1
prompt_value WEB_USER "Web login username" "$WEB_USER" 1
prompt_secret WEB_PASS "Web login password" "$WEB_PASS" 1
prompt_value QB_URL "qB WebUI URL" "$QB_URL" 1
prompt_value QB_USER "qB username" "$QB_USER" 1
prompt_secret QB_PASS "qB password" "$QB_PASS" 1
prompt_value QB_CATEGORY "qB category" "$QB_CATEGORY" 1
prompt_value U2_PASSKEY "U2 passkey" "$U2_PASSKEY" 1
prompt_value COOKIECLOUD_URL "CookieCloud URL" "$COOKIECLOUD_URL" 1
prompt_value COOKIECLOUD_KEY "CookieCloud key" "$COOKIECLOUD_KEY" 1
prompt_secret COOKIECLOUD_PASSWORD "CookieCloud password" "$COOKIECLOUD_PASSWORD" 1

TMP_DIR="$(mktemp -d /tmp/u2-qb-bot-install.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

ZIP_PATH="$TMP_DIR/$ASSET_NAME"
EXTRACT_DIR="$TMP_DIR/extract"
mkdir -p "$EXTRACT_DIR"

echo "Downloading release asset..."
curl -fsSL "$DOWNLOAD_URL" -o "$ZIP_PATH"

echo "Extracting package..."
extract_zip "$ZIP_PATH" "$EXTRACT_DIR"

PACKAGE_ROOT="$(find "$EXTRACT_DIR" -maxdepth 1 -mindepth 1 -type d | head -n 1)"
if [ -z "$PACKAGE_ROOT" ]; then
  echo "Failed to locate extracted package directory." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR"
cp -rf "$PACKAGE_ROOT"/* "$INSTALL_DIR"/

if [ -f "$INSTALL_DIR/config.properties" ]; then
  cp "$INSTALL_DIR/config.properties" "$INSTALL_DIR/config.properties.bak.$(date +%Y%m%d_%H%M%S)"
fi

cat > "$INSTALL_DIR/config.properties" <<EOF
# ===== U2 =====
cookiecloud.url=$COOKIECLOUD_URL
cookiecloud.key=$COOKIECLOUD_KEY
cookiecloud.password=$COOKIECLOUD_PASSWORD

cookie=
cookie_file=
passkey=$U2_PASSKEY

# ===== QB =====
qb.url=$QB_URL
qb.user=$QB_USER
qb.pass=$QB_PASS

# ===== QB Torrent Settings =====
qb.category=$QB_CATEGORY
qb.up_limit_mb=50
qb.max_downloading=3
qb.min_free_space_gb=20
seed.readd_cooldown_minutes=1440
shout.max_age_minutes=120
magic.up_rates=1.00,2.00,2.33
magic.use_thresholds=0
magic.min_up_rate=1.00
magic.max_down_rate=0.00
dynamic.rule1.enabled=0
dynamic.rule1.free_space_le_gb=0
dynamic.rule1.min_size_gb=0
dynamic.rule1.max_size_gb=0
dynamic.rule1.min_up_rate=0
dynamic.rule1.max_down_rate=999
dynamic.rule2.enabled=0
dynamic.rule2.free_space_le_gb=0
dynamic.rule2.min_size_gb=0
dynamic.rule2.max_size_gb=0
dynamic.rule2.min_up_rate=0
dynamic.rule2.max_down_rate=999
qb.session_refresh_seconds=1500

# ===== Web UI =====
web.host=0.0.0.0
web.port=$WEB_PORT
web.username=$WEB_USER
web.password=$WEB_PASS
web.session_hours=12
web.qb_refresh_seconds=5
log.archive_interval_minutes=60
log.archive_keep_count=20
web.token=

# ===== Telegram =====
telegram.bot_token=
telegram.chat_id=
telegram.poll_seconds=3

poll_interval=60
EOF

chmod +x "$INSTALL_DIR/u2_qb_bot.sh" "$INSTALL_DIR/u2_service.sh" "$INSTALL_DIR/install_systemd.sh"

echo "Installing systemd services..."
bash "$INSTALL_DIR/install_systemd.sh" "$INSTALL_DIR" "$SERVICE_NAME"

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP="${HOST_IP:-<your-server-ip>}"

echo
echo "Install completed."
echo "Install dir: $INSTALL_DIR"
echo "Web URL: http://$HOST_IP:$WEB_PORT"
echo "Web username: $WEB_USER"
echo "Web password: [hidden]"
