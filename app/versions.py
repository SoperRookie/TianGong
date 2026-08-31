"""测试点/用例版本历史（完整需求 10 章）。

四类版本来源：AI 原始生成（ai_original）/ 人工修改（manual）/ AI 驳回修改（ai_fix）/
评审通过终稿（final）；M4b 接入后追加 import（Excel 导入/人工新增）。
每版存完整内容快照；恢复不覆盖历史——基于所选版本追加一条 manual 新版并回到待评审。
存量任务（入库前生成）在首次被触碰时以当前内容补记首版（ensure_versions）。
"""

import json
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db import entity_versions, get_engine

VERSION_SOURCES = {
    "ai_original": "AI 原始生成",
    "manual": "人工修改",
    "ai_fix": "AI 驳回修改",
    "final": "评审通过终稿",
    "import": "导入/人工新增",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def point_entities(modules: list[dict]) -> dict[str, dict]:
    """测试点集合 → {tp_id: 内容快照}（含所属模块，供版本展示）。"""
    from app.tasks.points import iter_points

    return {
        str(p.get("tp_id")): dict(p, module=entry.get("module", ""))
        for entry, p in iter_points(modules) if p.get("tp_id")
    }


def case_entities(cases: list[dict]) -> dict[str, dict]:
    """用例集合 → {uid: 内容快照}（uid 稳定，不受编号重排影响）。"""
    return {str(c.get("uid")): dict(c) for c in cases if c.get("uid")}


def record_version(
    task_id: str, kind: str, entity_id: str, source: str, content: dict,
    by: str | None = None, reason: str = "",
) -> int:
    """追加一条版本记录，返回版本号（同实体内自增）。"""
    with get_engine().begin() as conn:
        current = conn.execute(
            select(func.max(entity_versions.c.version_no)).where(
                entity_versions.c.task_id == task_id,
                entity_versions.c.kind == kind,
                entity_versions.c.entity_id == entity_id,
            )
        ).scalar()
        version_no = (current or 0) + 1
        conn.execute(entity_versions.insert().values(
            task_id=task_id, kind=kind, entity_id=str(entity_id), version_no=version_no,
            source=source, reason=(reason or "")[:500], created_by=by, created_at=_now(),
            payload=json.dumps(content, ensure_ascii=False),
        ))
    return version_no


def ensure_versions(
    task_id: str, kind: str, entities: dict[str, dict],
    by: str | None = None, source: str = "ai_original", reason: str = "",
) -> int:
    """只为尚无任何版本记录的实体补记首版（幂等），返回补记条数。

    覆盖两类场景：生成完成时的 AI 原始版本；存量任务首次被触碰时的打底版本。
    """
    if not entities:
        return 0
    with get_engine().begin() as conn:
        known = {
            row[0] for row in conn.execute(
                select(entity_versions.c.entity_id.distinct()).where(
                    entity_versions.c.task_id == task_id, entity_versions.c.kind == kind,
                )
            )
        }
    added = 0
    for entity_id, content in entities.items():
        if str(entity_id) in known or not entity_id:
            continue
        record_version(task_id, kind, entity_id, source, content, by=by, reason=reason)
        added += 1
    return added


def latest_version_no(task_id: str, kind: str, entity_id: str) -> int:
    """实体当前最新版本号（无版本记录时为 0）；测试计划快照以此建立版本引用。"""
    with get_engine().begin() as conn:
        current = conn.execute(
            select(func.max(entity_versions.c.version_no)).where(
                entity_versions.c.task_id == task_id,
                entity_versions.c.kind == kind,
                entity_versions.c.entity_id == str(entity_id),
            )
        ).scalar()
    return int(current or 0)


def list_versions(task_id: str, kind: str, entity_id: str) -> list[dict]:
    """某实体的完整版本链（升序），附相邻版本差异。"""
    with get_engine().begin() as conn:
        rows = conn.execute(
            select(entity_versions).where(
                entity_versions.c.task_id == task_id,
                entity_versions.c.kind == kind,
                entity_versions.c.entity_id == str(entity_id),
            ).order_by(entity_versions.c.version_no)
        ).mappings().all()
    out: list[dict] = []
    prev: dict | None = None
    for row in rows:
        content = json.loads(row["payload"])
        out.append({
            "version_no": row["version_no"],
            "source": row["source"],
            "source_label": VERSION_SOURCES.get(row["source"], row["source"]),
            "reason": row["reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "content": content,
            "changes": _diff(kind, prev, content),
        })
        prev = content
    return out


def get_version(task_id: str, kind: str, entity_id: str, version_no: int) -> dict | None:
    with get_engine().begin() as conn:
        row = conn.execute(
            select(entity_versions.c.payload).where(
                entity_versions.c.task_id == task_id,
                entity_versions.c.kind == kind,
                entity_versions.c.entity_id == str(entity_id),
                entity_versions.c.version_no == version_no,
            )
        ).scalar()
    return json.loads(row) if row else None


def _diff(kind: str, before: dict | None, after: dict) -> list[dict]:
    """相邻版本字段差异（需求 10.1：版本、修改人、时间、原因、字段差异）。"""
    if before is None:
        return []
    if kind == "point":
        changes = []
        for field in ("point", "dimension"):
            b, a = str(before.get(field, "") or ""), str(after.get(field, "") or "")
            if b != a:
                changes.append({"field": field, "before": b, "after": a})
        return changes
    from app.agents.quality import _case_field_changes

    return _case_field_changes(before, after)
