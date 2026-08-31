"""回收站（完整需求 14.4）：测试点/用例默认逻辑删除。

删除操作把完整快照（含恢复所需上下文）移入 recycle_bin 表并记录 deleted_by / deleted_at；
恢复把快照放回任务并回到待评审；只有管理员永久删除才真正抹掉快照（版本历史仍留档）。
"""

import json
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.db import get_engine, recycle_bin


def add_to_bin(task_id: str, kind: str, entity_id: str, label: str,
               payload: dict, by: str | None = None) -> int:
    with get_engine().begin() as conn:
        result = conn.execute(recycle_bin.insert().values(
            task_id=task_id, kind=kind, entity_id=str(entity_id), label=(label or "")[:500],
            payload=json.dumps(payload, ensure_ascii=False), deleted_by=by,
            deleted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ))
    return int(result.inserted_primary_key[0])


def list_bin(task_id: str) -> list[dict]:
    with get_engine().begin() as conn:
        rows = conn.execute(
            select(recycle_bin.c.id, recycle_bin.c.kind, recycle_bin.c.entity_id,
                   recycle_bin.c.label, recycle_bin.c.deleted_by, recycle_bin.c.deleted_at)
            .where(recycle_bin.c.task_id == task_id)
            .order_by(recycle_bin.c.id.desc())
        ).mappings().all()
    return [dict(r) for r in rows]


def list_bin_for_tasks(task_ids: list[str]) -> list[dict]:
    """跨任务聚合（项目回收站视角）：项目下全部任务的回收站条目，新删的在前。"""
    if not task_ids:
        return []
    with get_engine().begin() as conn:
        rows = conn.execute(
            select(recycle_bin.c.id, recycle_bin.c.task_id, recycle_bin.c.kind,
                   recycle_bin.c.entity_id, recycle_bin.c.label,
                   recycle_bin.c.deleted_by, recycle_bin.c.deleted_at)
            .where(recycle_bin.c.task_id.in_(task_ids))
            .order_by(recycle_bin.c.id.desc())
        ).mappings().all()
    return [dict(r) for r in rows]


def get_item(task_id: str, item_id: int) -> dict | None:
    with get_engine().begin() as conn:
        row = conn.execute(
            select(recycle_bin).where(
                recycle_bin.c.id == item_id, recycle_bin.c.task_id == task_id)
        ).mappings().first()
    if row is None:
        return None
    return {**dict(row), "payload": json.loads(row["payload"])}


def purge(task_id: str, item_id: int) -> bool:
    """永久删除（仅管理员调用方可达）：从回收站抹掉快照。"""
    with get_engine().begin() as conn:
        result = conn.execute(delete(recycle_bin).where(
            recycle_bin.c.id == item_id, recycle_bin.c.task_id == task_id))
    return result.rowcount > 0
