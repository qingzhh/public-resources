#!/usr/bin/env bash
set -u

CONFIG_FILE="config.properties"
INITIALIZED_FLAG_FILE=".initialized"
STATE_DB_FILE="state.db"

TMP_DIR="$(mktemp -d /tmp/u2qb.XXXXXX)"
QB_COOKIE_JAR="$TMP_DIR/qb_cookie.txt"
QB_SESSION_LAST_LOGIN_TS="0"
QB_SESSION_REFRESH_SECONDS="1500"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANAGER_SCRIPT="$SCRIPT_DIR/u2_manager.py"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

log() {
  echo "[$(date '+%F %T')] $*"
}

fail() {
  log "错误：$*"
  exit 1
}

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

urlencode() {
  local string="$1"
  local length="${#string}"
  local i c out=""
  for (( i=0; i<length; i++ )); do
    c="${string:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      ' ') out+="%20" ;;
      *)
        printf -v hex '%%%02X' "'$c"
        out+="$hex"
        ;;
    esac
  done
  printf '%s' "$out"
}

load_cookie_from_file() {
  local cookie_file="$1"
  local cookie_header raw_line

  [ -f "$cookie_file" ] || fail "cookie_file 指定的文件不存在：$cookie_file"

  cookie_header="$(
    awk -F '\t' '
      /^[[:space:]]*$/ { next }
      /^#/ && $0 !~ /^#HttpOnly_/ { next }
      {
        domain = $1
        sub(/^#HttpOnly_/, "", domain)

        name = $6
        value = $7

        if (name == "" || value == "") {
          next
        }

        if (domain ~ /(^|[.])u2[.]dmhy[.]org$/) {
          if (out != "") {
            out = out "; "
          }
          out = out name "=" value
        }
      }
      END {
        print out
      }
    ' "$cookie_file"
  )"

  if [ -z "$cookie_header" ]; then
    raw_line="$(
      awk '
        /^[[:space:]]*$/ { next }
        /^[[:space:]]*#/ { next }
        {
          print
          exit
        }
      ' "$cookie_file"
    )"
    raw_line="$(trim "$raw_line")"

    if printf '%s' "$raw_line" | grep -q '='; then
      cookie_header="$raw_line"
    fi
  fi

  [ -n "$cookie_header" ] || fail "cookie_file 中没有找到 u2.dmhy.org 的有效 Cookie"
  printf '%s' "$cookie_header"
}

have_command() {
  command -v "$1" >/dev/null 2>&1
}

manager_python_bin() {
  if have_command python3; then
    printf '%s' "python3"
  elif have_command python; then
    printf '%s' "python"
  else
    fail "需要 python3 或 python 来运行 u2_manager.py"
  fi
}

manager_run() {
  local py_bin
  py_bin="$(manager_python_bin)"
  "$py_bin" "$MANAGER_SCRIPT" "$@"
}

current_epoch_seconds() {
  date +%s
}

qb_get_session_age_seconds() {
  local now

  if ! [[ "${QB_SESSION_LAST_LOGIN_TS:-0}" =~ ^[0-9]+$ ]] || [ "${QB_SESSION_LAST_LOGIN_TS:-0}" -le 0 ]; then
    printf '0'
    return 0
  fi

  now="$(current_epoch_seconds)"
  printf '%s' $((now - QB_SESSION_LAST_LOGIN_TS))
}

qb_ensure_session_ready() {
  local reason="$1"
  local age

  if [ ! -s "$QB_COOKIE_JAR" ]; then
    log "qB 会话不存在，准备重新登录：$reason"
    qb_login || return 1
    return 0
  fi

  if [ "${QB_SESSION_REFRESH_SECONDS:-0}" -le 0 ]; then
    return 0
  fi

  age="$(qb_get_session_age_seconds)"
  if [ "$age" -ge "$QB_SESSION_REFRESH_SECONDS" ]; then
    log "qB 会话已持续 ${age}s，主动刷新：$reason"
    qb_login || return 1
  fi
}

qb_request() {
  local resp_file="$1"
  local request_label="$2"
  local log_success="$3"
  local endpoint="$4"
  shift 4

  local code resp age retried=0
  local -a curl_args

  curl_args=(
    -sS
    -o "$resp_file"
    -w '%{http_code}'
    -b "$QB_COOKIE_JAR"
    -c "$QB_COOKIE_JAR"
    -H "Referer: $QB_URL"
    -H "Origin: $QB_URL"
    "$@"
    "$QB_URL$endpoint"
  )

  qb_ensure_session_ready "$request_label" || return 1

  code="$(curl "${curl_args[@]}")" || return 1
  resp="$(cat "$resp_file" 2>/dev/null || true)"

  if [ "$code" = "403" ]; then
    age="$(qb_get_session_age_seconds)"
    log "$request_label：HTTP=$code，返回内容=$resp"
    log "qB 请求返回 403，准备重新登录后重试：$request_label，会话年龄=${age}s"
    qb_login || return 1

    retried=1
    code="$(curl "${curl_args[@]}")" || return 1
    resp="$(cat "$resp_file" 2>/dev/null || true)"
  fi

  if [ "$retried" = "1" ]; then
    log "$request_label（重试后）：HTTP=$code，返回内容=$resp"
  elif [ "$log_success" = "1" ] || [ "$code" != "200" ]; then
    log "$request_label：HTTP=$code，返回内容=$resp"
  fi

  [ "$code" = "200" ]
}

has_cookiecloud_config() {
  [ -n "${COOKIECLOUD_URL:-}" ] && [ -n "${COOKIECLOUD_KEY:-}" ] && [ -n "${COOKIECLOUD_PASSWORD:-}" ]
}

extract_cookie_from_cookiecloud_json() {
  local json_file="$1"
  local py_bin=""

  if have_command python3; then
    py_bin="python3"
  elif have_command python; then
    py_bin="python"
  else
    fail "使用 CookieCloud 需要 python3 或 python 来解析 JSON"
  fi

  "$py_bin" - "$json_file" <<'PY'
import io
import json
import sys

path = sys.argv[1]
with io.open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)

cookie_data = data.get("cookie_data")
if not isinstance(cookie_data, dict):
    sys.exit(1)

pairs = {}
for domain, items in cookie_data.items():
    if not isinstance(items, list):
        continue
    for item in items:
        if not isinstance(item, dict):
            continue

        item_domain = str(item.get("domain") or domain or "")
        normalized_domain = item_domain.lstrip(".").lower()
        if normalized_domain != "u2.dmhy.org" and not normalized_domain.endswith(".u2.dmhy.org"):
            continue

        name = item.get("name")
        value = item.get("value")
        if not name or value is None:
            continue

        pairs[str(name)] = str(value)

if not pairs:
    sys.exit(1)

parts = []
for name, value in pairs.items():
    parts.append("%s=%s" % (name, value))
sys.stdout.write("; ".join(parts))
PY
}

load_cookie_from_cookiecloud() {
  local resp_file code resp cookie_url cookie_header

  cookie_url="${COOKIECLOUD_URL%/}/get/$(urlencode "$COOKIECLOUD_KEY")"
  resp_file="$TMP_DIR/cookiecloud_u2.json"

  code="$(
    curl -sS -o "$resp_file" -w '%{http_code}' \
      -H 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8' \
      --data "password=$(urlencode "$COOKIECLOUD_PASSWORD")" \
      "$cookie_url"
  )" || return 1

  if [ "$code" != "200" ]; then
    resp="$(cat "$resp_file" 2>/dev/null || true)"
    log "CookieCloud 请求失败：HTTP=$code，返回内容：$resp"
    return 1
  fi

  cookie_header="$(extract_cookie_from_cookiecloud_json "$resp_file")" || {
    resp="$(head -c 300 "$resp_file" 2>/dev/null || true)"
    log "CookieCloud 返回内容无法解析，前 300 字符：$resp"
    return 1
  }

  printf '%s' "$cookie_header"
}

refresh_u2_cookie() {
  local previous_cookie new_cookie

  previous_cookie="${COOKIE:-}"

  if has_cookiecloud_config; then
    new_cookie="$(load_cookie_from_cookiecloud)" || {
      if [ -n "$previous_cookie" ]; then
        log "CookieCloud 刷新失败，继续使用上一轮的 U2 Cookie"
        COOKIE="$previous_cookie"
        return 0
      fi
      return 1
    }
    COOKIE="$new_cookie"
  elif [ -n "${COOKIE_FILE:-}" ]; then
    new_cookie="$(load_cookie_from_file "$COOKIE_FILE")" || {
      if [ -n "$previous_cookie" ]; then
        log "cookie_file 刷新失败，继续使用上一轮的 U2 Cookie"
        COOKIE="$previous_cookie"
        return 0
      fi
      return 1
    }
    COOKIE="$new_cookie"
  fi

  [ -n "${COOKIE:-}" ]
}

read_config() {
  [ -f "$CONFIG_FILE" ] || fail "配置文件不存在：$CONFIG_FILE"

  COOKIE=""
  COOKIE_FILE=""
  COOKIE_SOURCE_DESC=""
  COOKIECLOUD_URL=""
  COOKIECLOUD_KEY=""
  COOKIECLOUD_PASSWORD=""
  PASSKEY=""
  QB_URL=""
  QB_USER=""
  QB_PASS=""
QB_CATEGORY=""
QB_UP_LIMIT_MB="0"
QB_MAX_DOWNLOADING="0"
QB_SESSION_REFRESH_SECONDS="1500"
MAGIC_UP_RATES="1.00,2.00,2.33"
MAGIC_USE_THRESHOLDS="0"
MAGIC_MIN_UP_RATE="1.00"
MAGIC_MAX_DOWN_RATE="0.00"
SHOUT_MAX_AGE_MINUTES="120"
POLL_INTERVAL="60"

  while IFS='=' read -r raw_key raw_value; do
    raw_key="$(trim "$raw_key")"
    raw_value="$(trim "$raw_value")"

    [ -z "$raw_key" ] && continue
    case "$raw_key" in
      \#*) continue ;;
    esac

    case "$raw_key" in
      cookie)
        if [ -z "$COOKIE_FILE" ]; then
          COOKIE="$raw_value"
        fi
        if [ -n "$COOKIE" ] && [ -z "$COOKIE_SOURCE_DESC" ]; then
          COOKIE_SOURCE_DESC="cookie"
        fi
        ;;
      cookie_file)
        COOKIE_FILE="$raw_value"
        if [ -n "$COOKIE_FILE" ]; then
          COOKIE="$(load_cookie_from_file "$COOKIE_FILE")"
          COOKIE_SOURCE_DESC="cookie_file=$COOKIE_FILE"
        fi
        ;;
      cookiecloud.url) COOKIECLOUD_URL="${raw_value%/}" ;;
      cookiecloud.key) COOKIECLOUD_KEY="$raw_value" ;;
      cookiecloud.password) COOKIECLOUD_PASSWORD="$raw_value" ;;
      passkey) PASSKEY="$raw_value" ;;
      qb.url) QB_URL="$raw_value" ;;
      qb.user) QB_USER="$raw_value" ;;
      qb.pass) QB_PASS="$raw_value" ;;
      qb.category) QB_CATEGORY="$raw_value" ;;
      qb.up_limit_mb) QB_UP_LIMIT_MB="$raw_value" ;;
      qb.max_downloading) QB_MAX_DOWNLOADING="$raw_value" ;;
      qb.session_refresh_seconds) QB_SESSION_REFRESH_SECONDS="$raw_value" ;;
      magic.up_rates) MAGIC_UP_RATES="$raw_value" ;;
      magic.use_thresholds) MAGIC_USE_THRESHOLDS="$raw_value" ;;
      magic.min_up_rate) MAGIC_MIN_UP_RATE="$raw_value" ;;
      magic.max_down_rate) MAGIC_MAX_DOWN_RATE="$raw_value" ;;
      shout.max_age_minutes) SHOUT_MAX_AGE_MINUTES="$raw_value" ;;
      poll_interval) POLL_INTERVAL="$raw_value" ;;
    esac
  done < "$CONFIG_FILE"

  [ -n "$COOKIE" ] || fail "缺少配置：cookie"
  [ -n "$PASSKEY" ] || fail "缺少配置：passkey"
  [ -n "$QB_URL" ] || fail "缺少配置：qb.url"
  [ -n "$QB_USER" ] || fail "缺少配置：qb.user"
  [ -n "$QB_PASS" ] || fail "缺少配置：qb.pass"

  QB_URL="${QB_URL%/}"

  if ! [[ "$QB_UP_LIMIT_MB" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    fail "qb.up_limit_mb 必须是数字，例如 0、1、1.5、49"
  fi

  if ! [[ "$QB_MAX_DOWNLOADING" =~ ^[0-9]+$ ]]; then
    fail "qb.max_downloading 必须是非负整数"
  fi

  if ! [[ "$QB_SESSION_REFRESH_SECONDS" =~ ^[0-9]+$ ]]; then
    fail "qb.session_refresh_seconds 必须是非负整数"
  fi

  if ! [[ "$POLL_INTERVAL" =~ ^[0-9]+$ ]] || [ "$POLL_INTERVAL" -le 0 ]; then
    fail "poll_interval 必须是正整数"
  fi

  # qB Web API 需要的是 bytes/s
  # 这里按 1 MB = 1024 * 1024 bytes 计算，更符合常见 Linux / qB 显示习惯
  QB_UP_LIMIT_BYTES="$(awk "BEGIN { printf \"%d\", ($QB_UP_LIMIT_MB * 1024 * 1024 + 0.5) }")"

  log "配置加载成功"
  log "qB 地址：$QB_URL"
  log "qB 分类：$QB_CATEGORY"
  log "qB 上传限速（MB/s）：$QB_UP_LIMIT_MB"
  log "qB 上传限速（bytes/s）：$QB_UP_LIMIT_BYTES"
  log "qB 最大当前下载数：$QB_MAX_DOWNLOADING"
  log "qB 会话主动刷新间隔（秒）：$QB_SESSION_REFRESH_SECONDS"
  log "允许魔法倍率：$MAGIC_UP_RATES"
  log "阈值识别：启用=$MAGIC_USE_THRESHOLDS，上传>=$MAGIC_MIN_UP_RATE，下载<=$MAGIC_MAX_DOWN_RATE"
  log "聊天消息最大年龄（分钟）：$SHOUT_MAX_AGE_MINUTES"
  log "轮询间隔（秒）：$POLL_INTERVAL"
}

read_config_v2() {
  [ -f "$CONFIG_FILE" ] || fail "配置文件不存在：$CONFIG_FILE"

  COOKIE=""
  COOKIE_FILE=""
  COOKIE_SOURCE_DESC=""
  COOKIECLOUD_URL=""
  COOKIECLOUD_KEY=""
  COOKIECLOUD_PASSWORD=""
  PASSKEY=""
  QB_URL=""
  QB_USER=""
  QB_PASS=""
  QB_CATEGORY=""
  QB_UP_LIMIT_MB="0"
  QB_MAX_DOWNLOADING="0"
  QB_SESSION_REFRESH_SECONDS="1500"
  MAGIC_UP_RATES="1.00,2.00,2.33"
  MAGIC_USE_THRESHOLDS="0"
  MAGIC_MIN_UP_RATE="1.00"
  MAGIC_MAX_DOWN_RATE="0.00"
  SHOUT_MAX_AGE_MINUTES="120"
  POLL_INTERVAL="60"

  while IFS='=' read -r raw_key raw_value; do
    raw_key="$(trim "$raw_key")"
    raw_value="$(trim "$raw_value")"

    [ -z "$raw_key" ] && continue
    case "$raw_key" in
      \#*) continue ;;
    esac

    case "$raw_key" in
      cookie) COOKIE="$raw_value" ;;
      cookie_file) COOKIE_FILE="$raw_value" ;;
      cookiecloud.url) COOKIECLOUD_URL="${raw_value%/}" ;;
      cookiecloud.key) COOKIECLOUD_KEY="$raw_value" ;;
      cookiecloud.password) COOKIECLOUD_PASSWORD="$raw_value" ;;
      passkey) PASSKEY="$raw_value" ;;
      qb.url) QB_URL="$raw_value" ;;
      qb.user) QB_USER="$raw_value" ;;
      qb.pass) QB_PASS="$raw_value" ;;
      qb.category) QB_CATEGORY="$raw_value" ;;
      qb.up_limit_mb) QB_UP_LIMIT_MB="$raw_value" ;;
      qb.max_downloading) QB_MAX_DOWNLOADING="$raw_value" ;;
      qb.session_refresh_seconds) QB_SESSION_REFRESH_SECONDS="$raw_value" ;;
      magic.up_rates) MAGIC_UP_RATES="$raw_value" ;;
      magic.use_thresholds) MAGIC_USE_THRESHOLDS="$raw_value" ;;
      magic.min_up_rate) MAGIC_MIN_UP_RATE="$raw_value" ;;
      magic.max_down_rate) MAGIC_MAX_DOWN_RATE="$raw_value" ;;
      shout.max_age_minutes) SHOUT_MAX_AGE_MINUTES="$raw_value" ;;
      poll_interval) POLL_INTERVAL="$raw_value" ;;
    esac
  done < "$CONFIG_FILE"

  if [ -n "$COOKIECLOUD_URL$COOKIECLOUD_KEY$COOKIECLOUD_PASSWORD" ]; then
    [ -n "$COOKIECLOUD_URL" ] || fail "缺少配置：cookiecloud.url"
    [ -n "$COOKIECLOUD_KEY" ] || fail "缺少配置：cookiecloud.key"
    [ -n "$COOKIECLOUD_PASSWORD" ] || fail "缺少配置：cookiecloud.password"
    COOKIE_SOURCE_DESC="cookiecloud"
  elif [ -n "$COOKIE_FILE" ]; then
    COOKIE_SOURCE_DESC="cookie_file=$COOKIE_FILE"
  elif [ -n "$COOKIE" ]; then
    COOKIE_SOURCE_DESC="cookie"
  else
    fail "缺少配置：cookie、cookie_file 或 CookieCloud"
  fi

  refresh_u2_cookie || fail "无法加载 U2 Cookie，请检查 cookie、cookie_file 或 CookieCloud 配置"

  [ -n "$PASSKEY" ] || fail "缺少配置：passkey"
  [ -n "$QB_URL" ] || fail "缺少配置：qb.url"
  [ -n "$QB_USER" ] || fail "缺少配置：qb.user"
  [ -n "$QB_PASS" ] || fail "缺少配置：qb.pass"

  QB_URL="${QB_URL%/}"

  if ! [[ "$QB_UP_LIMIT_MB" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    fail "qb.up_limit_mb 必须是数字，例如 0、1、1.5、50"
  fi

  if ! [[ "$QB_MAX_DOWNLOADING" =~ ^[0-9]+$ ]]; then
    fail "qb.max_downloading 必须是非负整数"
  fi

  if ! [[ "$QB_SESSION_REFRESH_SECONDS" =~ ^[0-9]+$ ]]; then
    fail "qb.session_refresh_seconds 必须是非负整数"
  fi

  if ! [[ "$POLL_INTERVAL" =~ ^[0-9]+$ ]] || [ "$POLL_INTERVAL" -le 0 ]; then
    fail "poll_interval 必须是正整数"
  fi

  QB_UP_LIMIT_BYTES="$(awk "BEGIN { printf \"%d\", ($QB_UP_LIMIT_MB * 1024 * 1024 + 0.5) }")"

  log "配置加载成功"
  log "U2 Cookie 来源：$COOKIE_SOURCE_DESC"
  log "qB 地址：$QB_URL"
  log "qB 分类：$QB_CATEGORY"
  log "qB 上传限速（MB/s）：$QB_UP_LIMIT_MB"
  log "qB 上传限速（bytes/s）：$QB_UP_LIMIT_BYTES"
  log "qB 最大当前下载数：$QB_MAX_DOWNLOADING"
  log "qB 会话主动刷新间隔（秒）：$QB_SESSION_REFRESH_SECONDS"
  log "允许魔法倍率：$MAGIC_UP_RATES"
  log "阈值识别：启用=$MAGIC_USE_THRESHOLDS，上传>=$MAGIC_MIN_UP_RATE，下载<=$MAGIC_MAX_DOWN_RATE"
  log "聊天消息最大年龄（分钟）：$SHOUT_MAX_AGE_MINUTES"
  log "轮询间隔（秒）：$POLL_INTERVAL"
}

reset_runtime_state() {
  rm -f "$INITIALIZED_FLAG_FILE" "$STATE_DB_FILE"
  log "已自动重置运行状态：删除 $INITIALIZED_FLAG_FILE 和 $STATE_DB_FILE"
}

qb_login() {
  local body resp_file code resp
  body="username=$(urlencode "$QB_USER")&password=$(urlencode "$QB_PASS")"

  resp_file="$TMP_DIR/qb_login_body.txt"
  code="$(
    curl -sS -o "$resp_file" -w '%{http_code}' \
      -b "$QB_COOKIE_JAR" \
      -c "$QB_COOKIE_JAR" \
      -H "Referer: $QB_URL" \
      -H "Origin: $QB_URL" \
      -H 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8' \
      --data "$body" \
      "$QB_URL/api/v2/auth/login"
  )" || return 1

  resp="$(cat "$resp_file" 2>/dev/null || true)"

  if [ "$code" != "200" ] || [ "$resp" != "Ok." ]; then
    log "qB 登录失败：HTTP=$code，返回内容=$resp"
    return 1
  fi

  QB_SESSION_LAST_LOGIN_TS="$(current_epoch_seconds)"
  log "qB 登录成功"
  return 0
}

qb_get_current_downloading_count() {
  local resp_file resp count

  resp_file="$TMP_DIR/qb_downloading.json"
  if ! qb_request "$resp_file" "查询当前下载数" "0" "/api/v2/torrents/info?filter=downloading"; then
    return 1
  fi

  resp="$(cat "$resp_file" 2>/dev/null || true)"
  count="$(printf '%s' "$resp" | grep -o '"hash":"' | wc -l | awk '{print $1}')"
  printf '%s' "$count"
}

qb_add_torrent_file() {
  local torrent_file="$1"
  local filename="$2"
  local resp_file

  resp_file="$TMP_DIR/qb_add_body.txt"

  qb_request \
    "$resp_file" \
    "qB 添加种子文件：文件名=$filename" \
    "1" \
    "/api/v2/torrents/add" \
    -F "category=$QB_CATEGORY" \
    -F "upLimit=$QB_UP_LIMIT_BYTES" \
    -F "torrents=@${torrent_file};filename=${filename};type=application/x-bittorrent"
}

fetch_main_page() {
  local out_file="$1"
  curl -sS -L \
    -H 'User-Agent: Mozilla/5.0' \
    -H "Cookie: $COOKIE" \
    -o "$out_file" \
    "https://u2.dmhy.org/index.php"
}

extract_shoutbox_url() {
  local main_file="$1"
  local html src

  html="$(tr '\n' ' ' < "$main_file")"

  src="$(printf '%s' "$html" \
    | grep -oiE '<iframe[^>]+(id|name)=["'"'"']?sbcontent["'"'"']?[^>]*src=["'"'"'][^"'"'"']+["'"'"']|<iframe[^>]+src=["'"'"'][^"'"'"']+["'"'"'][^>]*(id|name)=["'"'"']?sbcontent["'"'"']?[^>]*' \
    | head -n 1 \
    | sed -E 's/.*src=["'"'"']([^"'"'"']+)["'"'"'].*/\1/I')"

  if [ -z "$src" ]; then
    src="$(printf '%s' "$html" \
      | grep -oiE '<iframe[^>]+src=["'"'"'][^"'"'"']*(shoutbox|shbox|sb)[^"'"'"']*["'"'"'][^>]*' \
      | head -n 1 \
      | sed -E 's/.*src=["'"'"']([^"'"'"']+)["'"'"'].*/\1/I')"
  fi

  if [ -z "$src" ]; then
    src="$(printf '%s' "$html" \
      | grep -oiE '<iframe[^>]+src=["'"'"'][^"'"'"']+["'"'"'][^>]*' \
      | head -n 1 \
      | sed -E 's/.*src=["'"'"']([^"'"'"']+)["'"'"'].*/\1/I')"
  fi

  if [ -z "$src" ]; then
    return 1
  fi

  case "$src" in
    http*) printf '%s' "$src" ;;
    /*) printf 'https://u2.dmhy.org%s' "$src" ;;
    *) printf 'https://u2.dmhy.org/%s' "$src" ;;
  esac
}

fetch_shoutbox_page() {
  local url="$1"
  local out_file="$2"

  curl -sS -L \
    -H 'User-Agent: Mozilla/5.0' \
    -H "Cookie: $COOKIE" \
    -H 'Referer: https://u2.dmhy.org/index.php' \
    -o "$out_file" \
    "$url"
}

extract_candidate_entries() {
  local shout_file="$1"
  local py_bin=""

  if have_command python3; then
    py_bin="python3"
  elif have_command python; then
    py_bin="python"
  else
    fail "提取聊天区命中记录需要 python3 或 python"
  fi
  ALLOWED_UP_RATES="$MAGIC_UP_RATES" MAGIC_USE_THRESHOLDS="$MAGIC_USE_THRESHOLDS" MAGIC_MIN_UP_RATE="$MAGIC_MIN_UP_RATE" MAGIC_MAX_DOWN_RATE="$MAGIC_MAX_DOWN_RATE" SHOUT_MAX_AGE_MINUTES="$SHOUT_MAX_AGE_MINUTES" "$py_bin" - "$shout_file" <<'PY'
import hashlib
import html
import os
import re
import sys

path = sys.argv[1]
with open(path, "rb") as fh:
    raw_bytes = fh.read()

texts = []
seen_texts = set()
for encoding in ("utf-8", "gb18030", "big5", "latin-1"):
    try:
        text = raw_bytes.decode(encoding, errors="ignore")
    except LookupError:
        continue
    if text and text not in seen_texts:
        texts.append(text)
        seen_texts.add(text)

allowed_rates = []
allowed_rate_values = []
for part in os.environ.get("ALLOWED_UP_RATES", "1.00,2.00,2.33").split(","):
    part = part.strip()
    if not part:
        continue
    allowed_rate_values.append(part)
    allowed_rates.append(re.escape(part))

if not allowed_rates:
    allowed_rates = ["1\\.00", "2\\.00", "2\\.33"]
    allowed_rate_values = ["1.00", "2.00", "2.33"]

use_thresholds = os.environ.get("MAGIC_USE_THRESHOLDS", "0").strip().lower() in {"1", "true", "yes", "on"}
try:
    min_up_rate = float(os.environ.get("MAGIC_MIN_UP_RATE", "1.00").strip() or "1.00")
except ValueError:
    min_up_rate = 1.00
try:
    max_down_rate = float(os.environ.get("MAGIC_MAX_DOWN_RATE", "0.00").strip() or "0.00")
except ValueError:
    max_down_rate = 0.00
try:
    shout_max_age_minutes = int(float(os.environ.get("SHOUT_MAX_AGE_MINUTES", "120").strip() or "120"))
except ValueError:
    shout_max_age_minutes = 120

def parse_relative_seconds(text: str) -> int | None:
    if not text:
        return None
    compact = text.replace(" ", "")
    total = 0
    matched = False
    for pattern, multiplier in [
        (r"(\d+)天前", 86400),
        (r"(\d+)天", 86400),
        (r"(\d+)小时前", 3600),
        (r"(\d+)小时", 3600),
        (r"(\d+)分钟前", 60),
        (r"(\d+)分钟", 60),
        (r"(\d+)秒前", 1),
        (r"(\d+)秒", 1),
    ]:
        for m in re.finditer(pattern, compact):
            total += int(m.group(1)) * multiplier
            matched = True
    return total if matched else None

magic_pattern = re.compile(
    r"\u5b8c\u6210\u4e86\u4e00\u6b21"
    r"(?:\u4e0a\u50b3|\u4e0a\u4f20)"
    r"(?:" + "|".join(allowed_rates) + r")"
    r"(?:\u4e0b\u8f09|\u4e0b\u8f7d)"
    r"0\.00\u7684\u9b54\u6cd5"
)
seed_text_pattern = re.compile(
    r"(?:\u5bf9\u79cd\u5b50|\u5c0d\u7a2e\u5b50).*?(?<!user)details\.php\?id=(\d+)",
    re.I | re.S,
)
seed_href_pattern = re.compile(r"(?<!user)details\.php\?id=(\d+)", re.I)

def normalize_block(block: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", block)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\u00ad", "")
    return re.sub(r"\s+", " ", text).strip()

results = []
seen_occurrences = set()
for text in texts:
    for block in re.split(r"(?i)<div>", text):
        if "details.php?id=" not in block:
            continue

        plain = normalize_block(block)
        if not plain:
            continue

        time_match = re.match(r"^\[\s*([^\]]+?)\s*\]", plain)
        relative_seconds = parse_relative_seconds(time_match.group(1).strip()) if time_match else None
        if relative_seconds is not None and shout_max_age_minutes > 0 and relative_seconds > shout_max_age_minutes * 60:
            continue

        compact = re.sub(r"\s+", "", plain)
        if not magic_pattern.search(compact):
            continue

        rate_match = re.search(r"(?:\u4e0a\u50b3|\u4e0a\u4f20)([0-9.]+)(?:\u4e0b\u8f09|\u4e0b\u8f7d)([0-9.]+)", compact)
        if not rate_match:
            continue

        try:
            up_rate = float(rate_match.group(1))
            down_rate = float(rate_match.group(2))
        except ValueError:
            continue

        exact_match = any(abs(up_rate - float(rate)) < 1e-9 for rate in allowed_rate_values)
        threshold_match = use_thresholds and up_rate >= min_up_rate and down_rate <= max_down_rate
        if not exact_match and not threshold_match:
            continue

        match = seed_text_pattern.search(block)
        if not match:
            match = seed_href_pattern.search(block)
        if not match:
            continue

        torrent_id = match.group(1)
        occurrence_key = hashlib.sha1(
            f"{torrent_id}\n{block}".encode("utf-8", errors="ignore")
        ).hexdigest()
        if occurrence_key in seen_occurrences:
            continue

        seen_occurrences.add(occurrence_key)
        results.append((occurrence_key, torrent_id, plain.replace("\t", " ")))

for occurrence_key, torrent_id, plain in results:
    sys.stdout.write(f"{occurrence_key}\t{torrent_id}\t{plain}\n")
PY
}

download_torrent() {
  local id="$1"
  local out_file="$2"
  local url code

  url="https://u2.dmhy.org/download.php?id=${id}&passkey=${PASSKEY}"

  code="$(
    curl -sS -L -o "$out_file" -w '%{http_code}' \
      -H 'User-Agent: Mozilla/5.0' \
      "$url"
  )" || return 1

  if [ "$code" != "200" ]; then
    log "下载种子文件失败：种子ID=$id，HTTP=$code"
    return 1
  fi

  if [ ! -s "$out_file" ]; then
    log "下载到的种子文件为空：种子ID=$id"
    return 1
  fi

  if head -c 200 "$out_file" | grep -Eqi '<html|<!doctype|<body'; then
    log "下载到的内容不是种子文件：种子ID=$id"
    return 1
  fi

  return 0
}

initialize_current_entries() {
  local entries="$1"
  local count=0
  local occurrence_key id matched_text

  while IFS=$'\t' read -r occurrence_key id matched_text; do
    [ -n "$occurrence_key" ] || continue
    manager_run record-event \
      --db "$STATE_DB_FILE" \
      --occurrence-key "$occurrence_key" \
      --seed-id "$id" \
      --source "auto" \
      --matched-text "$matched_text" \
      --action "baseline" \
      --result "ignored_baseline" \
      --note "首次启动时聊天区已可见的消息，作为基线跳过" >/dev/null || true
    count=$((count + 1))
  done <<< "$entries"

  touch "$INITIALIZED_FLAG_FILE"
  log "首次读取完成，已记录当前聊天区基线数量：$count，本次不会推送到 qB"
}

process_candidate_entry() {
  local occurrence_key="$1"
  local id="$2"
  local matched_text="$3"

  log "发现聊天区候选种子：种子ID=$id"
  if [ -n "$matched_text" ]; then
    log "命中聊天文本：种子ID=$id，文本=$matched_text"
  fi

  if ! manager_run add-seed \
    --config "$CONFIG_FILE" \
    --db "$STATE_DB_FILE" \
    --log-file "$SCRIPT_DIR/u2.log" \
    --seed-id "$id" \
    --occurrence-key "$occurrence_key" \
    --source "auto" \
    --matched-text "$matched_text" >/dev/null; then
    log "管理器处理失败：种子ID=$id"
  fi
}

run_once() {
  local main_file shout_file shout_url entries occurrence_key id matched_text

  main_file="$TMP_DIR/main.html"
  shout_file="$TMP_DIR/shout.html"

  refresh_u2_cookie || {
    log "刷新 U2 Cookie 失败"
    return
  }

  fetch_main_page "$main_file" || {
    log "抓取主页失败"
    return
  }

  shout_url="$(extract_shoutbox_url "$main_file")" || {
    log "无法找到聊天区 iframe，不一定是 Cookie 失效"
    log "主页前 1000 个字符如下："
    head -c 1000 "$main_file"
    echo
    return
  }

  log "抓取聊天区：$shout_url"

  fetch_shoutbox_page "$shout_url" "$shout_file" || {
    log "抓取聊天区页面失败"
    return
  }

  entries="$(extract_candidate_entries "$shout_file")"

  if [ -z "$entries" ]; then
    log "本轮没有匹配到符合条件的种子"
    return
  fi

  if [ ! -f "$INITIALIZED_FLAG_FILE" ]; then
    initialize_current_entries "$entries"
    return
  fi

  while IFS=$'\t' read -r occurrence_key id matched_text; do
    [ -n "$occurrence_key" ] || continue
    if manager_run occurrence-exists --db "$STATE_DB_FILE" --occurrence-key "$occurrence_key" >/dev/null 2>&1; then
      continue
    fi
    process_candidate_entry "$occurrence_key" "$id" "$matched_text"
  done <<< "$entries"
}

main() {
  read_config_v2

  log "启动完成，开始轮询"

  while true; do
    run_once
    sleep "$POLL_INTERVAL"
  done
}

main "$@"
