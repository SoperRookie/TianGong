#!/usr/bin/env bash
# 服务器磁盘清理（磁盘满了升级不了时用）：
#   bash scripts/disk_cleanup.sh            # 看占用 + 清理安全项（日志、缓存、旧备份、临时文件）
#   bash scripts/disk_cleanup.sh --dry-run  # 只看会删什么，不动
#   bash scripts/disk_cleanup.sh --mysql    # 另外清 MySQL binlog（只留 1 天，并设置以后只留 3 天）
#   bash scripts/disk_cleanup.sh --docker   # 另外清 Docker 无用镜像 / 构建缓存
#
# 绝不触碰：outputs/（上传原件、导出、执行附件）、data/（向量库、本地库数据）、.env、config/
# 可以在任意目录执行；项目目录按脚本位置推断，也可用 TIANGONG_DIR 指定。
set -uo pipefail

DIR="${TIANGONG_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
DRY=0; DO_MYSQL=0; DO_DOCKER=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --mysql) DO_MYSQL=1 ;;
    --docker) DO_DOCKER=1 ;;
    *) echo "未知参数：$a"; exit 1 ;;
  esac
done

KEEP_BACKUPS="${KEEP_BACKUPS:-3}"         # backups/ 保留最近几份
LOG_KEEP_DAYS="${LOG_KEEP_DAYS:-7}"       # 应用日志保留天数
freed_total=0

size_kb() { du -sk "$1" 2>/dev/null | awk '{print $1}'; }
human() { awk -v k="$1" 'BEGIN{ if (k>=1048576) printf "%.1fG", k/1048576; else if (k>=1024) printf "%.1fM", k/1024; else printf "%dK", k }'; }
run() { if [ "$DRY" = 1 ]; then echo "    [dry-run] $*"; else eval "$@"; fi; }
# 删除前后算大小，累计释放量
clean_path() {  # clean_path <说明> <路径> [<rm 命令>]
  local label="$1" path="$2" cmd="${3:-}"
  [ -e "$path" ] || return 0
  local before; before="$(size_kb "$path")"; before="${before:-0}"
  [ "$before" -gt 0 ] || return 0
  echo "  - ${label}：$(human "$before")  (${path})"
  if [ -n "$cmd" ]; then run "$cmd"; else run "rm -rf -- '$path'"; fi
  local after; after="$( [ -e "$path" ] && size_kb "$path" || echo 0 )"; after="${after:-0}"
  freed_total=$((freed_total + before - after))
}

echo "=============== 磁盘状况（清理前） ==============="
df -h / | sed 's/^/  /'
echo
echo "---- 根目录一级占用（前 12）"
du -xh --max-depth=1 / 2>/dev/null | sort -h | tail -12 | sed 's/^/  /'
echo
echo "---- 项目目录占用：${DIR}"
du -xh --max-depth=1 "${DIR}" 2>/dev/null | sort -h | tail -10 | sed 's/^/  /'
echo
echo "---- 其他常见大户"
for p in /var/lib/mysql /var/log /var/lib/docker /root/.cache /tmp /var/cache/apt; do
  [ -d "$p" ] && printf "  %-20s %s\n" "$p" "$(du -sh "$p" 2>/dev/null | awk '{print $1}')"
done
command -v journalctl >/dev/null && echo "  journal              $(journalctl --disk-usage 2>/dev/null | grep -o '[0-9.]*[KMG]' | head -1)"
echo

echo "=============== 清理安全项 $( [ "$DRY" = 1 ] && echo '（dry-run，不实际删除）' ) ==============="
echo "[1] 应用日志 ${DIR}/logs"
if [ -d "${DIR}/logs" ]; then
  clean_path "旧的进程输出 server.out.1" "${DIR}/logs/server.out.1"
  # server.out 可能正被运行中的进程打开：清空而不是删除，否则空间不会释放
  if [ -f "${DIR}/logs/server.out" ]; then
    b="$(size_kb "${DIR}/logs/server.out")"
    if [ "${b:-0}" -gt 10240 ]; then
      echo "  - 进程输出 server.out：$(human "$b")（清空，不删除）"
      run ": > '${DIR}/logs/server.out'"; freed_total=$((freed_total + b))
    fi
  fi
  n=$(find "${DIR}/logs" -maxdepth 1 -name 'tiangong_*.log*' -mtime +"${LOG_KEEP_DAYS}" 2>/dev/null | wc -l)
  if [ "$n" -gt 0 ]; then
    b=$(find "${DIR}/logs" -maxdepth 1 -name 'tiangong_*.log*' -mtime +"${LOG_KEEP_DAYS}" -exec du -ck {} + 2>/dev/null | tail -1 | awk '{print $1}')
    echo "  - ${LOG_KEEP_DAYS} 天前的应用日志 ${n} 个：$(human "${b:-0}")"
    run "find '${DIR}/logs' -maxdepth 1 -name 'tiangong_*.log*' -mtime +${LOG_KEEP_DAYS} -delete"; freed_total=$((freed_total + ${b:-0}))
  fi
  clean_path "MySQL 开发实例日志 mysql.log" "${DIR}/logs/mysql.log" ": > '${DIR}/logs/mysql.log'"
fi

echo "[2] 旧备份 ${DIR}/backups（保留最近 ${KEEP_BACKUPS} 份）"
if [ -d "${DIR}/backups" ]; then
  while read -r f; do
    [ -n "$f" ] && clean_path "备份 $(basename "$f")" "$f"
  done < <(ls -1t "${DIR}"/backups/*.sql* "${DIR}"/backups/*.tar* "${DIR}"/backups/*.gz 2>/dev/null | awk '!seen[$0]++' | tail -n +$((KEEP_BACKUPS + 1)))
fi

echo "[3] 缓存与临时文件"
clean_path "pip 缓存（venv）" "$(${DIR}/.venv/bin/pip cache dir 2>/dev/null || echo /nonexistent)"
clean_path "pip 缓存（root）" "/root/.cache/pip"
clean_path "uv / huggingface 等缓存" "/root/.cache/uv"
clean_path "apt 缓存" "/var/cache/apt/archives" "apt-get clean >/dev/null 2>&1 || true"
[ -d /var/lib/apt/lists ] && clean_path "apt 索引" "/var/lib/apt/lists" "rm -rf /var/lib/apt/lists/*"
if command -v journalctl >/dev/null; then
  b="$(journalctl --disk-usage 2>/dev/null | grep -o '[0-9.]*[KMG]' | head -1)"
  echo "  - systemd 日志：${b:-?}（收到 100M）"; run "journalctl --vacuum-size=100M >/dev/null 2>&1 || true"
fi
for f in /var/log/*.gz /var/log/*.[0-9] /var/log/*/*.gz; do [ -e "$f" ] && clean_path "轮转旧系统日志 $(basename "$f")" "$f"; done
for f in /var/log/syslog /var/log/kern.log /var/log/auth.log /var/log/btmp /var/log/wtmp; do
  if [ -f "$f" ] && [ "$(size_kb "$f")" -gt 102400 ]; then clean_path "系统日志 $f（清空）" "$f" ": > '$f'"; fi
done
while read -r f; do [ -n "$f" ] && clean_path "临时文件 $(basename "$f")" "$f"; done < <(find /tmp -maxdepth 1 \( -name 'tmp*' -o -name 'tiangong-*' -o -name 'pip-*' \) -mtime +1 2>/dev/null)
b=$(find "${DIR}/app" "${DIR}/tests" -name __pycache__ -type d -prune -exec du -ck {} + 2>/dev/null | tail -1 | awk '{print $1}')
if [ "${b:-0}" -gt 0 ]; then echo "  - 项目 __pycache__：$(human "$b")"; run "find '${DIR}/app' '${DIR}/tests' -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null"; freed_total=$((freed_total + b)); fi
if command -v git >/dev/null && [ -d "${DIR}/.git" ]; then
  b="$(size_kb "${DIR}/.git")"; echo "  - git 仓库 ${DIR}/.git：$(human "${b:-0}")（gc 整理，顺便清掉上次拉取失败的残片）"
  run "cd '${DIR}' && rm -f .git/objects/pack/tmp_pack_* .git/objects/pack/*.idx.tmp 2>/dev/null; git gc --prune=now -q 2>/dev/null || true"
fi

if [ "$DO_MYSQL" = 1 ]; then
  echo "[4] MySQL binlog"
  if command -v mysql >/dev/null; then
    MYSQL_CMD="mysql ${MYSQL_ARGS:-}"
    echo "  - 当前 binlog："; $MYSQL_CMD -e "SHOW BINARY LOGS;" 2>&1 | sed 's/^/      /' | tail -8
    run "$MYSQL_CMD -e \"PURGE BINARY LOGS BEFORE DATE_SUB(NOW(), INTERVAL 1 DAY); SET GLOBAL binlog_expire_logs_seconds = 259200;\""
    echo "  - 已清 1 天前的 binlog，并设置以后只保留 3 天（my.cnf 里加 binlog_expire_logs_seconds=259200 才能重启后生效）"
  else
    echo "  - 本机没有 mysql 客户端，跳过。可用 MYSQL_ARGS='-uroot -p密码' 指定连接参数"
  fi
fi

if [ "$DO_DOCKER" = 1 ] && command -v docker >/dev/null; then
  echo "[5] Docker 无用镜像 / 构建缓存 / 悬空卷"
  docker system df 2>/dev/null | sed 's/^/      /'
  run "docker image prune -af >/dev/null 2>&1; docker builder prune -af >/dev/null 2>&1; docker volume prune -f >/dev/null 2>&1"
fi

echo
echo "=============== 已删除但仍被进程占用的文件（空间要等进程重启才释放） ==============="
if command -v lsof >/dev/null; then
  lsof +L1 2>/dev/null | awk 'NR>1 && $7 > 10485760 {printf "  pid %s %s  %.0fM  %s\n", $2, $1, $7/1048576, $9}' | sort -k3 -n | tail -8
  echo "  （有的话：重启对应进程，天工用 bash scripts/server.sh restart）"
else
  echo "  未安装 lsof，跳过"
fi

echo
echo "=============== 清理后 ==============="
df -h / | sed 's/^/  /'
echo "  本次释放约：$(human "$freed_total")"
echo
echo "仍然不够时看「根目录一级占用」里最大的目录继续追：du -xh --max-depth=1 <目录> | sort -h | tail"
echo "不要删：${DIR}/outputs（上传与导出）、${DIR}/data（向量库 / 本地库）"
