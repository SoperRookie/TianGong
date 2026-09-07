#!/usr/bin/env bash
# 本地开发 MySQL：首次自动初始化 data/mysql 数据目录，之后直接启动并建库。
# 生产环境自行部署 MySQL 8，应用经 TIANGONG_DB_URL 指向即可。
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATADIR="$DIR/data/mysql"
SOCK="/tmp/tiangong-mysql.sock"
PORT=3306

if mysqladmin --socket="$SOCK" -u root ping >/dev/null 2>&1; then
  echo "MySQL 已在运行（${SOCK}）"
else
  if [ ! -d "$DATADIR/mysql" ]; then
    echo "初始化 MySQL 数据目录：$DATADIR"
    mysqld --initialize-insecure --datadir="$DATADIR" >/dev/null 2>&1
  fi
  mysqld --datadir="$DATADIR" --socket="$SOCK" --port=$PORT \
    --bind-address=127.0.0.1 --mysqlx=OFF >"$DIR/logs/mysql.log" 2>&1 &
  echo "MySQL 启动中（pid $!，日志 logs/mysql.log）…"
  for _ in $(seq 1 30); do
    mysqladmin --socket="$SOCK" -u root ping >/dev/null 2>&1 && break
    sleep 1
  done
  mysqladmin --socket="$SOCK" -u root ping >/dev/null 2>&1 || { echo "MySQL 启动失败，见 logs/mysql.log"; exit 1; }
fi

mysql --socket="$SOCK" -u root -e \
  "CREATE DATABASE IF NOT EXISTS tiangong CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
echo "MySQL 就绪：mysql+pymysql://root@127.0.0.1:$PORT/tiangong"
