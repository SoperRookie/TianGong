"""清空全部项目及其相关数据（保留账号、模板、提示词、模型配置）。

用法（在项目根目录，先停服务：bash scripts/server.sh stop）：
    .venv/bin/python scripts/reset_data.py              # 预览：只统计将删除的内容，不动库
    .venv/bin/python scripts/reset_data.py --yes        # 真正执行
选项：
    --keep-knowledge   保留知识库（知识文档 + 向量库）
    --with-audit       连操作审计日志一起清空（默认保留）
    --keep-files       只清数据库，不删 outputs/ 下的任务产物与附件

数据库地址取自 .env 的 TIANGONG_DB_URL（与应用一致），执行前请先备份：
    mysqldump -h <host> -u <user> -p tiangong | gzip > backups/tiangong-$(date +%Y%m%d-%H%M%S).sql.gz
"""

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select  # noqa: E402

from app.audit import audit_log  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import DocStore, entity_versions, get_engine, kv_docs, recycle_bin  # noqa: E402
from app.llm.calllog import ai_calls  # noqa: E402

# 项目及其派生数据所在的 kv_docs 命名空间；auth / user_prefs / memories / templates / prompts 保留
PROJECT_STORES = ["projects", "requirements", "tasks", "plans", "modules", "dependencies", "versions"]
KNOWLEDGE_STORES = ["knowledge_docs"]


def _assert_service_stopped(settings) -> None:
    """应用为内存态 + 整表回写，运行中清库会被回写覆盖，必须先停。"""
    import fcntl

    lock = settings.outputs_dir / ".instance.lock"
    if not lock.exists():
        return
    handle = open(lock, "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        sys.exit("服务正在运行，请先停止：bash scripts/server.sh stop")
    finally:
        handle.close()


def _count_store(conn, store: str) -> int:
    return conn.execute(select(func.count()).where(kv_docs.c.store == store)).scalar_one()


def _count_table(conn, table) -> int:
    return conn.execute(select(func.count()).select_from(table)).scalar_one()


def _output_targets(settings) -> list[Path]:
    """outputs/ 下的任务产物、需求附件、上传缓存；实例锁与非数据文件不动。"""
    out = settings.outputs_dir
    if not out.exists():
        return []
    def skip(p: Path) -> bool:
        return p.name == ".instance.lock" or (p.suffix == ".json" and p.name != "tasks.json")

    return sorted(p for p in out.iterdir() if not skip(p))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="真正执行删除（默认仅预览）")
    ap.add_argument("--keep-knowledge", action="store_true")
    ap.add_argument("--with-audit", action="store_true")
    ap.add_argument("--keep-files", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    _assert_service_stopped(settings)
    engine = get_engine()

    stores = PROJECT_STORES + ([] if args.keep_knowledge else KNOWLEDGE_STORES)
    tables = [("entity_versions（测试点/用例版本）", entity_versions), ("recycle_bin（回收站）", recycle_bin),
              ("ai_calls（AI 调用日志）", ai_calls)]
    if args.with_audit:
        tables.append(("audit_log（操作审计日志）", audit_log))

    print(f"数据库：{engine.url.render_as_string(hide_password=True)}")
    print("将清空的数据：")
    with engine.begin() as conn:
        for s in stores:
            print(f"  kv_docs/{s:<16} {_count_store(conn, s):>6} 条")
        for label, t in tables:
            print(f"  {label:<28} {_count_table(conn, t):>6} 条")
    files = [] if args.keep_files else _output_targets(settings)
    if files:
        print(f"  outputs/ 下 {len(files)} 项（任务产物、附件、上传缓存）")
    kdir = settings.knowledge_dir
    if not args.keep_knowledge and kdir.exists():
        print(f"  向量库目录 {kdir}")
    print("保留：账号与权限、用户偏好（清空最近/收藏项目）、模板、提示词、模型配置（config/models.yaml）"
          + ("" if args.with_audit else "、操作审计日志"))

    if not args.yes:
        print("\n以上为预览。确认无误且已备份后加 --yes 执行。")
        return

    with engine.begin() as conn:
        for s in stores:
            conn.execute(delete(kv_docs).where(kv_docs.c.store == s))
        for _, t in tables:
            conn.execute(delete(t))
    # 用户偏好里的最近/收藏项目已失效，清空；项目级偏好记忆一并移除
    prefs = DocStore("user_prefs")
    for k, doc in prefs.load_all().items():
        doc["favorites"], doc["recent"] = [], []
        prefs.put(k, doc)
    mem = DocStore("memories")
    for k, doc in mem.load_all().items():
        items = doc.get("memories") or []
        kept = [m for m in items if not m.get("project")]
        if len(kept) != len(items):
            doc["memories"] = kept
            mem.put(k, doc)
    for p in files:
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    if not args.keep_knowledge and kdir.exists():
        shutil.rmtree(kdir, ignore_errors=True)
    print("\n已清空。启动服务后为空项目状态：bash scripts/server.sh start")


if __name__ == "__main__":
    main()
