"""数据库持久化层（所有业务数据入库，MySQL 为准）。

统一「文档表」模型 kv_docs：store（命名空间）+ k（业务键）+ payload（JSON 文本）。
- 大表（tasks）按记录一行，保存即单行写入；
- 小表沿用原 JSON 文档整体读写语义（replace_all / 单文档 put），行为与旧文件落盘一致；
- 首次启动如命名空间为空且存在旧 JSON 文件，自动迁移入库（旧文件保留、不再写入）。

连接经 TIANGONG_DB_URL 配置：生产 MySQL（utf8mb4），测试指向 SQLite 隔离。
后续 M3 的版本历史 / 回收站等结构化表在此模块追加。
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Callable

from loguru import logger
from sqlalchemy import (
    Column, Index, Integer, MetaData, String, Table, Text, create_engine, delete, insert, select,
)
from sqlalchemy.dialects.mysql import LONGTEXT

from app.config import get_settings

metadata = MetaData()

kv_docs = Table(
    "kv_docs",
    metadata,
    Column("store", String(64), primary_key=True),
    Column("k", String(191), primary_key=True),
    Column("payload", Text().with_variant(LONGTEXT, "mysql"), nullable=False),
)

# 测试点/用例版本历史（完整需求 10 章）：内容快照按版本追加，恢复即基于历史版本再生一版
entity_versions = Table(
    "entity_versions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(32), nullable=False),
    Column("kind", String(16), nullable=False),       # point / case
    Column("entity_id", String(64), nullable=False),  # 测试点 tp_id / 用例 uid
    Column("version_no", Integer, nullable=False),
    Column("source", String(16), nullable=False),     # ai_original / manual / ai_fix / final / import
    Column("reason", String(500), nullable=False, default=""),
    Column("created_by", String(64)),
    Column("created_at", String(32), nullable=False),
    Column("payload", Text().with_variant(LONGTEXT, "mysql"), nullable=False),
    Index("ix_ev_entity", "task_id", "kind", "entity_id"),
)

# 回收站（完整需求 14.4）：核心数据默认逻辑删除——删除即移入此表，可恢复；管理员永久删除才落地
recycle_bin = Table(
    "recycle_bin",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(32), nullable=False),
    Column("kind", String(16), nullable=False),       # point / case
    Column("entity_id", String(64), nullable=False),  # tp_id / uid
    Column("label", String(500), nullable=False, default=""),
    Column("payload", Text().with_variant(LONGTEXT, "mysql"), nullable=False),
    Column("deleted_by", String(64)),
    Column("deleted_at", String(32), nullable=False),
    Index("ix_rb_task", "task_id"),
)


@lru_cache
def get_engine():
    url = get_settings().db_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    metadata.create_all(engine)
    logger.info("数据库已连接：{}", engine.url.render_as_string(hide_password=True))
    return engine


def reset_engine_cache() -> None:
    """配置变更/测试隔离后重建连接。"""
    get_engine.cache_clear()


class DocStore:
    """一个命名空间的文档集合：{key: dict}，payload 以 JSON 文本存储。"""

    def __init__(self, name: str):
        self._name = name

    def load_all(self) -> dict[str, dict]:
        with get_engine().begin() as conn:
            rows = conn.execute(
                select(kv_docs.c.k, kv_docs.c.payload).where(kv_docs.c.store == self._name)
            ).all()
        return {k: json.loads(p) for k, p in rows}

    def put(self, key: str, doc: dict) -> None:
        payload = json.dumps(doc, ensure_ascii=False)
        with get_engine().begin() as conn:
            conn.execute(delete(kv_docs).where(kv_docs.c.store == self._name, kv_docs.c.k == key))
            conn.execute(insert(kv_docs).values(store=self._name, k=key, payload=payload))

    def remove(self, key: str) -> None:
        with get_engine().begin() as conn:
            conn.execute(delete(kv_docs).where(kv_docs.c.store == self._name, kv_docs.c.k == key))

    def replace_all(self, docs: dict[str, dict]) -> None:
        with get_engine().begin() as conn:
            conn.execute(delete(kv_docs).where(kv_docs.c.store == self._name))
            if docs:
                conn.execute(
                    insert(kv_docs),
                    [
                        {"store": self._name, "k": k, "payload": json.dumps(v, ensure_ascii=False)}
                        for k, v in docs.items()
                    ],
                )


def load_with_migration(
    doc: DocStore, legacy_path: Path | None, loader: Callable[[dict | list], dict[str, dict]]
) -> dict[str, dict]:
    """读取命名空间；为空且旧 JSON 文件存在时执行一次性迁移（loader 把文件内容转为 {key: doc}）。"""
    docs = doc.load_all()
    if docs or legacy_path is None or not legacy_path.exists():
        return docs
    try:
        raw = json.loads(legacy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    docs = loader(raw) or {}
    if docs:
        doc.replace_all(docs)
        logger.info("已将旧文件 {} 迁移入库（{} 条 → {}）", legacy_path.name, len(docs), doc._name)
    return docs
