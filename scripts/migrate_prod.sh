#!/usr/bin/env bash
# 验收周：存量数据正式迁移前的全量备份 + 迁移检查。
#
# 迁移本身在应用启动时自动、幂等执行（app/main.py：M4 执行轮次→计划、M2 任务→需求实体），
# 本脚本负责「先备份、再启动、后核对」三步，不直接改库：
#   1. mysqldump 全库到 backups/tiangong-<时间>.sql.gz
#   2. 提示启动服务（启动日志会打印迁移条数）
#   3. 登录系统管理员后调用 /api/v1/admin/migration 核对完成度；
#      未归属项目的遗留任务在「项目管理 →（未指定）→ 概览」逐个「归属项目」，归属后需求与计划自动跟随。
#
# 用法：TIANGONG_DB_URL=mysql+pymysql://user:pass@host:3306/tiangong scripts/migrate_prod.sh [--check TOKEN [BASE_URL]]
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ "${1:-}" = "--check" ]; then
  token="${2:?需要系统管理员的登录 token}"; base="${3:-http://127.0.0.1:8000}"
  echo "迁移报告（$base）："
  curl -sS -H "Authorization: Bearer $token" "$base/api/v1/admin/migration" | python3 -c '
import json, sys
r = json.load(sys.stdin)
print(f"  任务 {r[\"tasks_total\"]} 个：已归属 {r[\"tasks_with_project\"]}，未归属 {r[\"tasks_unassigned\"]}")
print(f"  需求实体化 {r[\"requirements_migrated\"]} 条，待实体化 {len(r[\"tasks_without_requirement\"])}")
print(f"  执行轮次已迁入计划 {r[\"plans_migrated\"]} 个，待迁 {len(r[\"tasks_with_legacy_executions\"])}，挂在「未指定」的计划 {r[\"plans_unassigned\"]} 个")
print("  状态：", "✅ 迁移完成" if r["complete"] else "⚠️ 待处理（请在 项目管理 →（未指定）中逐个归属项目）")
for t in r["unassigned"]:
    print(f"    - {t[\"task_id\"]}  {\"、\".join(t[\"sources\"])}  创建人 {t[\"created_by\"] or \"-\"}  用例 {t[\"cases\"]} 条")
'
  exit 0
fi

URL="${TIANGONG_DB_URL:-$(grep -E '^TIANGONG_DB_URL=' "$DIR/.env" 2>/dev/null | cut -d= -f2- || true)}"
URL="${URL:-mysql+pymysql://root@127.0.0.1:3306/tiangong?charset=utf8mb4}"   # 与 app/config.py 默认一致

# 解析 mysql+pymysql://user:pass@host:port/db?charset=...
rest="${URL#*://}"
auth="${rest%%@*}"; hostpart="${rest#*@}"
user="${auth%%:*}"; pass=""; [ "$auth" != "$user" ] && pass="${auth#*:}"
hostport="${hostpart%%/*}"; db="${hostpart#*/}"; db="${db%%\?*}"
host="${hostport%%:*}"; port="3306"; [ "$hostport" != "$host" ] && port="${hostport#*:}"

mkdir -p "$DIR/backups"
out="$DIR/backups/${db}-$(date +%Y%m%d-%H%M%S).sql.gz"
echo "备份 $db@$host:$port → $out"
MYSQL_PWD="$pass" mysqldump -h "$host" -P "$port" -u "$user" --single-transaction --routines --triggers \
  --default-character-set=utf8mb4 "$db" | gzip > "$out"
echo "备份完成：$(du -h "$out" | cut -f1)"
echo
echo "下一步："
echo "  1. 启动服务（scripts/dev.sh 或生产进程），启动日志会输出「M4 执行迁移」「M2 需求迁移」条数；"
echo "  2. 以系统管理员登录，执行：scripts/migrate_prod.sh --check <token> [base_url] 核对完成度；"
echo "  3. 报告中未归属的遗留任务，在 项目管理 →（未指定）→ 概览 逐个「归属项目」。"
echo "回滚：gunzip -c $out | mysql -h $host -P $port -u $user -p $db"
