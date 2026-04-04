#!/usr/bin/env bash

BOT_SCRIPT="./u2_qb_bot.sh"
BOT_PID_FILE="./u2_qb_bot.pid"
BOT_LOG_FILE="./u2.log"

WEB_SCRIPT="./u2_manager.py"
WEB_PID_FILE="./u2_web_ui.pid"
WEB_LOG_FILE="./u2_web.log"
TELEGRAM_SCRIPT="./u2_manager.py"
TELEGRAM_PID_FILE="./u2_telegram_bot.pid"
TELEGRAM_LOG_FILE="./u2_telegram.log"

CONFIG_FILE="./config.properties"
STATE_DB_FILE="./state.db"
DEPLOY_FILE="./deploy.jsonc"
TELEGRAM_OFFSET_FILE="./telegram.offset"

is_pid_running() {
  local pid_file="$1"
  if [ -f "$pid_file" ]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

python_bin() {
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "python3"
  elif command -v python >/dev/null 2>&1; then
    printf '%s' "python"
  else
    echo "错误：未找到 python3 或 python"
    return 1
  fi
}

start_bot() {
  if [ ! -f "$BOT_SCRIPT" ]; then
    echo "错误：未找到脚本 $BOT_SCRIPT"
    return 1
  fi

  if is_pid_running "$BOT_PID_FILE"; then
    echo "程序已在运行，PID：$(cat "$BOT_PID_FILE")"
    return 0
  fi

  chmod +x "$BOT_SCRIPT"
  nohup "$BOT_SCRIPT" >> "$BOT_LOG_FILE" 2>&1 &
  local pid=$!

  echo "$pid" > "$BOT_PID_FILE"
  sleep 1

  if kill -0 "$pid" 2>/dev/null; then
    echo "启动成功，PID：$pid"
    echo "日志文件：$BOT_LOG_FILE"
    return 0
  fi

  echo "启动失败，请检查日志：$BOT_LOG_FILE"
  rm -f "$BOT_PID_FILE"
  return 1
}

stop_pid() {
  local pid_file="$1"
  local service_label="$2"

  if ! is_pid_running "$pid_file"; then
    echo "$service_label 未运行"
    rm -f "$pid_file"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file")"
  kill "$pid" 2>/dev/null

  for i in 1 2 3 4 5; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_file"
      echo "$service_label 停止成功"
      return 0
    fi
    sleep 1
  done

  echo "$service_label 进程未正常退出，尝试强制停止，PID：$pid"
  kill -9 "$pid" 2>/dev/null
  sleep 1

  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file"
    echo "$service_label 已强制停止"
    return 0
  fi

  echo "$service_label 强制停止失败"
  return 1
}

stop_bot() {
  stop_pid "$BOT_PID_FILE" "Bot"
}

restart_bot() {
  stop_bot || return 1
  sleep 1
  start_bot || return 1
}

status_bot() {
  if is_pid_running "$BOT_PID_FILE"; then
    echo "Bot 正在运行，PID：$(cat "$BOT_PID_FILE")"
  else
    echo "Bot 未运行"
  fi
}

tail_bot_log() {
  touch "$BOT_LOG_FILE"
  tail -f "$BOT_LOG_FILE"
}

count_added() {
  if [ ! -f "$BOT_LOG_FILE" ]; then
    echo "成功加入种子数量：0"
    return 0
  fi

  local count
  count="$(grep -c "已成功上传到 qB" "$BOT_LOG_FILE" 2>/dev/null || true)"
  count="${count:-0}"
  echo "成功加入种子数量：$count"
}

start_web() {
  local py_bin
  py_bin="$(python_bin)" || return 1

  if [ ! -f "$WEB_SCRIPT" ]; then
    echo "错误：未找到脚本 $WEB_SCRIPT"
    return 1
  fi

  if is_pid_running "$WEB_PID_FILE"; then
    echo "Web 控制台已在运行，PID：$(cat "$WEB_PID_FILE")"
    return 0
  fi

  nohup "$py_bin" "$WEB_SCRIPT" serve \
    --config "$CONFIG_FILE" \
    --db "$STATE_DB_FILE" \
    --log-file "$BOT_LOG_FILE" \
    --deploy-file "$DEPLOY_FILE" \
    --pid-file "$BOT_PID_FILE" \
    --service-script "./u2_service.sh" >> "$WEB_LOG_FILE" 2>&1 &
  local pid=$!

  echo "$pid" > "$WEB_PID_FILE"
  sleep 1

  if kill -0 "$pid" 2>/dev/null; then
    echo "Web 控制台启动成功，PID：$pid"
    echo "日志文件：$WEB_LOG_FILE"
    return 0
  fi

  echo "Web 控制台启动失败，请检查日志：$WEB_LOG_FILE"
  rm -f "$WEB_PID_FILE"
  return 1
}

stop_web() {
  stop_pid "$WEB_PID_FILE" "Web 控制台"
}

restart_web() {
  stop_web || return 1
  sleep 1
  start_web || return 1
}

status_web() {
  if is_pid_running "$WEB_PID_FILE"; then
    echo "Web 控制台正在运行，PID：$(cat "$WEB_PID_FILE")"
  else
    echo "Web 控制台未运行"
  fi
}

tail_web_log() {
  touch "$WEB_LOG_FILE"
  tail -f "$WEB_LOG_FILE"
}

start_telegram() {
  local py_bin
  py_bin="$(python_bin)" || return 1

  if [ ! -f "$TELEGRAM_SCRIPT" ]; then
    echo "错误：未找到脚本 $TELEGRAM_SCRIPT"
    return 1
  fi

  if ! grep -Eq '^telegram\.bot_token=.+$' "$CONFIG_FILE" 2>/dev/null; then
    echo "错误：config.properties 中缺少 telegram.bot_token"
    return 1
  fi

  if ! grep -Eq '^telegram\.chat_id=.+$' "$CONFIG_FILE" 2>/dev/null; then
    echo "错误：config.properties 中缺少 telegram.chat_id"
    return 1
  fi

  if is_pid_running "$TELEGRAM_PID_FILE"; then
    echo "Telegram Bot 已在运行，PID：$(cat "$TELEGRAM_PID_FILE")"
    return 0
  fi

  nohup "$py_bin" "$TELEGRAM_SCRIPT" telegram-bot \
    --config "$CONFIG_FILE" \
    --db "$STATE_DB_FILE" \
    --offset-file "$TELEGRAM_OFFSET_FILE" >> "$TELEGRAM_LOG_FILE" 2>&1 &
  local pid=$!

  echo "$pid" > "$TELEGRAM_PID_FILE"
  sleep 1

  if kill -0 "$pid" 2>/dev/null; then
    echo "Telegram Bot 启动成功，PID：$pid"
    echo "日志文件：$TELEGRAM_LOG_FILE"
    return 0
  fi

  echo "Telegram Bot 启动失败，请检查日志：$TELEGRAM_LOG_FILE"
  rm -f "$TELEGRAM_PID_FILE"
  return 1
}

stop_telegram() {
  stop_pid "$TELEGRAM_PID_FILE" "Telegram Bot"
}

restart_telegram() {
  stop_telegram || return 1
  sleep 1
  start_telegram || return 1
}

status_telegram() {
  if is_pid_running "$TELEGRAM_PID_FILE"; then
    echo "Telegram Bot 正在运行，PID：$(cat "$TELEGRAM_PID_FILE")"
  else
    echo "Telegram Bot 未运行"
  fi
}

tail_telegram_log() {
  touch "$TELEGRAM_LOG_FILE"
  tail -f "$TELEGRAM_LOG_FILE"
}

health_check() {
  local py_bin
  py_bin="$(python_bin)" || return 1

  "$py_bin" "$WEB_SCRIPT" health-check \
    --config "$CONFIG_FILE" \
    --db "$STATE_DB_FILE"
}

manual_add() {
  local seed_id="${1:-}"
  local force_flag="${2:-}"
  local py_bin
  py_bin="$(python_bin)" || return 1

  if [ -z "$seed_id" ]; then
    echo "用法：$0 add <seed_id> [--force]"
    return 1
  fi

  if ! [[ "$seed_id" =~ ^[0-9]+$ ]]; then
    echo "错误：seed_id 必须是纯数字"
    return 1
  fi

  "$py_bin" "$WEB_SCRIPT" add-seed \
    --config "$CONFIG_FILE" \
    --db "$STATE_DB_FILE" \
    --log-file "$BOT_LOG_FILE" \
    --seed-id "$seed_id" \
    --occurrence-key "manual:${seed_id}:$(date +%s)" \
    --source "manual" \
    --matched-text "命令行手工加入" \
    ${force_flag:+--force}
}

case "${1:-}" in
  start)
    start_bot
    ;;
  stop)
    stop_bot
    ;;
  restart)
    restart_bot
    ;;
  status)
    status_bot
    ;;
  log)
    tail_bot_log
    ;;
  count)
    count_added
    ;;
  add)
    shift
    manual_add "$@"
    ;;
  web-start)
    start_web
    ;;
  web-stop)
    stop_web
    ;;
  web-restart)
    restart_web
    ;;
  web-status)
    status_web
    ;;
  web-log)
    tail_web_log
    ;;
  telegram-start)
    start_telegram
    ;;
  telegram-stop)
    stop_telegram
    ;;
  telegram-restart)
    restart_telegram
    ;;
  telegram-status)
    status_telegram
    ;;
  telegram-log)
    tail_telegram_log
    ;;
  check)
    health_check
    ;;
  *)
    echo "用法：$0 {start|stop|restart|status|log|count|add <seed_id> [--force]|web-start|web-stop|web-restart|web-status|web-log}"
    exit 1
    ;;
esac

exit $?
