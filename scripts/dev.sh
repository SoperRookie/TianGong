#!/usr/bin/env bash
# 本地一键启动：先确保开发 MySQL 在运行，再启动服务（参数原样透传给 uvicorn）。
#   bash scripts/dev.sh                # 默认 --reload，8000 端口
#   bash scripts/dev.sh --port 8001    # 自定义 uvicorn 参数
# 若 TIANGONG_DB_URL 指向非本机数据库，则跳过 MySQL 拉起。
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

[ -f .env ] && set -a && . ./.env && set +a
DB_URL="${TIANGONG_DB_URL:-mysql+pymysql://root@127.0.0.1:3306/tiangong}"

case "$DB_URL" in
  *127.0.0.1*|*localhost*) bash scripts/mysql_dev.sh ;;
  *) echo "TIANGONG_DB_URL 指向外部数据库，跳过本地 MySQL 启动" ;;
esac

PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY=python
if [ $# -eq 0 ]; then set -- --reload; fi
exec "$PY" -m uvicorn app.main:app "$@"
