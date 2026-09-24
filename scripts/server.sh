#!/usr/bin/env bash
# 服务器源码部署一键脚本（不依赖 systemd / Docker）：
#   bash scripts/server.sh start     # 后台启动，等待 /health 通过
#   bash scripts/server.sh stop      # 优雅停止（最多等 20 秒，超时强杀）
#   bash scripts/server.sh restart
#   bash scripts/server.sh status
#   bash scripts/server.sh logs      # 跟踪输出日志
#   bash scripts/server.sh update    # 拉取当前分支最新代码，依赖有变化时重装，然后重启
#
# 配置只改 .env：密钥、数据库连接串由应用自行读取（脚本不 source .env，密码含 # $ @ 等
# 特殊字符也不受影响）；监听地址与端口可在 .env 里加 TIANGONG_HOST / TIANGONG_PORT，
# 默认 0.0.0.0:8000。模型清单在页面「模型配置」维护，保存即生效，无需重启。
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${DIR}"

PY="${DIR}/.venv/bin/python"
LOG_DIR="${DIR}/logs"
PID_FILE="${LOG_DIR}/server.pid"
OUT_FILE="${LOG_DIR}/server.out"
APP="app.main:app"
mkdir -p "${LOG_DIR}"

# 从 .env 取单个键的值（只按 KEY= 前缀取，不执行任何 shell 展开）
env_get() {
  [ -f .env ] || return 0
  { grep -E "^$1=" .env || true; } | tail -n 1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' -e 's/[[:space:]]*$//'
}
HOST="${TIANGONG_HOST:-$(env_get TIANGONG_HOST)}"; HOST="${HOST:-0.0.0.0}"
PORT="${TIANGONG_PORT:-$(env_get TIANGONG_PORT)}"; PORT="${PORT:-8000}"

# 只匹配本项目 venv 起的进程，多套部署同机互不干扰
find_pid() { pgrep -f "^${PY} -m uvicorn ${APP}" || true; }

running_pid() {
  local pid=""
  [ -f "${PID_FILE}" ] && pid="$(cat "${PID_FILE}")"
  if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then echo "${pid}"; return; fi
  find_pid | head -n 1
}

health() { curl -sf -m 3 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; }

do_start() {
  [ -x "${PY}" ] || { echo "缺少虚拟环境 ${PY}：先执行 python3 -m venv .venv && .venv/bin/pip install -e ."; exit 1; }
  [ -f .env ] || echo "提示：未找到 .env（数据库连接串与模型密钥），将按默认配置启动"
  local pid; pid="$(running_pid)"
  if [ -n "${pid}" ]; then echo "已在运行（pid ${pid}，端口 ${PORT}）"; return 0; fi
  # 输出日志超过 50MB 轮转一次，避免无限增长（应用自身的按日日志在 logs/tiangong_<日期>.log）
  if [ -f "${OUT_FILE}" ] && [ "$(wc -c <"${OUT_FILE}")" -gt 52428800 ]; then mv -f "${OUT_FILE}" "${OUT_FILE}.1"; fi
  echo "启动中：${HOST}:${PORT}（输出 logs/server.out）"
  nohup "${PY}" -m uvicorn "${APP}" --host "${HOST}" --port "${PORT}" --timeout-graceful-shutdown 10 >>"${OUT_FILE}" 2>&1 &
  pid=$!
  echo "${pid}" >"${PID_FILE}"
  # 启动含建表 / 数据迁移，最多等 90 秒
  for _ in $(seq 1 90); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      echo "启动失败，最近日志："; tail -n 30 "${OUT_FILE}"; rm -f "${PID_FILE}"; exit 1
    fi
    health && { echo "已启动（pid ${pid}），健康检查通过：http://127.0.0.1:${PORT}/health"; return 0; }
    sleep 1
  done
  echo "进程在跑（pid ${pid}）但 90 秒内健康检查未通过，请看日志：bash scripts/server.sh logs"; exit 1
}

do_stop() {
  local pid; pid="$(running_pid)"
  if [ -z "${pid}" ]; then echo "未在运行"; rm -f "${PID_FILE}"; return 0; fi
  echo "停止中（pid ${pid}）…"
  kill -TERM "${pid}" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "${pid}" 2>/dev/null || { echo "已停止"; rm -f "${PID_FILE}"; return 0; }
    sleep 1
  done
  echo "20 秒未退出，强制结束"
  kill -KILL "${pid}" 2>/dev/null || true
  rm -f "${PID_FILE}"
}

do_status() {
  local pid; pid="$(running_pid)"
  if [ -z "${pid}" ]; then echo "状态：未运行（端口 ${PORT}）"; return 1; fi
  local elapsed; elapsed="$(ps -o etime= -p "${pid}" | tr -d ' ')"
  if health; then echo "状态：运行中  pid ${pid}  端口 ${PORT}  已运行 ${elapsed}  健康检查通过"
  else echo "状态：进程在跑（pid ${pid}，已运行 ${elapsed}）但健康检查未通过，请看日志"; return 1; fi
}

do_update() {
  command -v git >/dev/null || { echo "缺少 git"; exit 1; }
  local branch before after
  branch="$(git rev-parse --abbrev-ref HEAD)"
  before="$(git rev-parse HEAD)"
  echo "拉取分支 ${branch} 最新代码…（升级前建议先备份：数据库、outputs/、data/）"
  git pull --ff-only origin "${branch}"
  after="$(git rev-parse HEAD)"
  if [ "${before}" = "${after}" ]; then echo "代码已是最新（${after}）"; else echo "已更新：${before:0:7} → ${after:0:7}"; fi
  if [ "${before}" != "${after}" ] && ! git diff --quiet "${before}" "${after}" -- pyproject.toml; then
    echo "依赖声明有变化，重新安装…"
    "${DIR}/.venv/bin/pip" install -q -e .
  fi
  do_stop
  do_start
}

case "${1:-}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; do_start ;;
  status)  do_status ;;
  logs)    tail -n 100 -f "${OUT_FILE}" ;;
  update)  do_update ;;
  *)
    echo "用法：bash scripts/server.sh {start|stop|restart|status|logs|update}"
    echo "  配置改 .env（TIANGONG_HOST / TIANGONG_PORT 可选，默认 0.0.0.0:8000）；改完 restart 生效"
    exit 1 ;;
esac
