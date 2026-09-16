"""AI 调用上下文与调用日志（完整需求 15 章 / 核心规则 22、23）。

- ai_context：当前 AI 操作的业务上下文（项目 / 任务 / 需求 / 发起人 / 用途），路由层进入 AI 操作前设置，
  asyncio 子任务自动继承（后台任务在 submit 前设置即可）；
- used_prompts：本次调用前取用过的 Prompt（key → 版本号），由 app.prompts.prompt_text 登记；
- ai_calls 表：每次模型调用一行——服务商/模型/用途/Prompt 版本/输入输出对象（预览）/发起人/项目/耗时/token/状态。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, Index, Integer, String, Table, Text, delete, func, insert, select
from sqlalchemy.dialects.mysql import LONGTEXT

from app.db import get_engine, metadata

ai_context: ContextVar[dict] = ContextVar("ai_context", default={})
used_prompts: ContextVar[dict] = ContextVar("used_prompts", default={})

PREVIEW_CHARS = 3000

ai_calls = Table(
    "ai_calls",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", String(32), nullable=False),
    Column("purpose", String(64), nullable=False, default=""),       # 用途（Prompt 名 / 场景）
    Column("prompt_key", String(64), nullable=False, default=""),
    Column("prompt_version", Integer, nullable=True),
    Column("prompt_versions", String(500), nullable=False, default="{}"),  # 本次用到的全部 Prompt 版本
    Column("provider", String(32), nullable=False, default=""),
    Column("model", String(64), nullable=False, default=""),
    Column("project", String(191), nullable=True),
    Column("task_id", String(32), nullable=True),
    Column("requirement_id", String(32), nullable=True),
    Column("by", String(64), nullable=True),
    Column("status", String(16), nullable=False, default="ok"),      # ok / error
    Column("error", String(1000), nullable=False, default=""),
    Column("finish_reason", String(32), nullable=True),
    Column("attempts", Integer, nullable=False, default=1),
    Column("elapsed_ms", Integer, nullable=False, default=0),
    Column("prompt_tokens", Integer, nullable=False, default=0),
    Column("completion_tokens", Integer, nullable=False, default=0),
    Column("input_chars", Integer, nullable=False, default=0),
    Column("output_chars", Integer, nullable=False, default=0),
    Column("input_preview", Text().with_variant(LONGTEXT, "mysql"), nullable=False, default=""),
    Column("output_preview", Text().with_variant(LONGTEXT, "mysql"), nullable=False, default=""),
    Index("ix_ai_calls_task", "task_id"),
    Index("ix_ai_calls_project_at", "project", "at"),
    Index("ix_ai_calls_at", "at"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def ai_scope(**ctx):
    """设置 AI 业务上下文（with 块内的模型调用都带上），退出恢复。"""
    merged = {**ai_context.get(), **{k: v for k, v in ctx.items() if v is not None}}
    token = ai_context.set(merged)
    try:
        yield
    finally:
        ai_context.reset(token)


def set_ai_context(**ctx) -> None:
    """无 with 场景（后台任务 submit 前）直接设置当前上下文。"""
    ai_context.set({**ai_context.get(), **{k: v for k, v in ctx.items() if v is not None}})


def note_prompt(key: str, version_no: int, purpose: str, kind: str = "system") -> None:
    """登记本次调用将使用的 Prompt（由 app.prompts.prompt_text 调用）；注入块不决定用途。"""
    cur = dict(used_prompts.get())
    cur[key] = version_no
    if kind != "block":
        cur.setdefault("__purpose__", purpose)
        cur.setdefault("__key__", key)
    used_prompts.set(cur)


def _messages_text(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):  # 多模态：只保留文本片段
            content = " ".join(str(c.get("text", "")) if isinstance(c, dict) and c.get("type") == "text"
                               else "[图片]" for c in content)
        parts.append(f"[{m.get('role', '')}] {content}")
    return "\n".join(parts)


def record_call(messages: list[dict], result: Any = None, error: Exception | None = None,
                model_name: str = "", provider: str = "", elapsed_ms: int = 0, attempts: int = 1) -> None:
    """写一行调用日志；任何异常都不影响主流程。"""
    try:
        ctx = ai_context.get()
        prompts = dict(used_prompts.get())
        used_prompts.set({})
        purpose = prompts.pop("__purpose__", None) or ctx.get("purpose") or "其他"
        key = prompts.pop("__key__", "") or ""
        text_in = _messages_text(messages)
        content = getattr(result, "content", "") if result is not None else ""
        usage = getattr(result, "usage", None)
        row = {
            "at": _now(), "purpose": purpose, "prompt_key": key,
            "prompt_version": prompts.get(key), "prompt_versions": json.dumps(prompts, ensure_ascii=False),
            "provider": provider or getattr(result, "provider", "") or "",
            "model": model_name or getattr(result, "model_name", "") or "",
            "project": ctx.get("project"), "task_id": ctx.get("task_id"),
            "requirement_id": ctx.get("requirement_id"), "by": ctx.get("by"),
            "status": "error" if error else "ok", "error": (str(error) if error else "")[:1000],
            "finish_reason": getattr(result, "finish_reason", None) if result is not None else None,
            "attempts": getattr(result, "attempts", attempts) if result is not None else attempts,
            "elapsed_ms": getattr(result, "elapsed_ms", elapsed_ms) if result is not None else elapsed_ms,
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "input_chars": len(text_in), "output_chars": len(content),
            "input_preview": text_in[:PREVIEW_CHARS], "output_preview": content[:PREVIEW_CHARS],
        }
        from app.db import persist_async

        def _write(r=row):
            with get_engine().begin() as conn:
                conn.execute(insert(ai_calls).values(**r))

        persist_async(_write)
    except Exception as e:  # pragma: no cover - 日志写入失败不影响业务
        from loguru import logger

        logger.warning("AI 调用日志写入失败：{}", e)


def list_calls(project: str | None = None, task_id: str | None = None, requirement_id: str | None = None,
               purpose: str | None = None, model: str | None = None, status: str | None = None,
               by: str | None = None, since: str | None = None, visible: set[str] | None = None,
               page: int = 1, page_size: int = 50) -> dict:
    conds = []
    if project:
        conds.append(ai_calls.c.project == project)
    if task_id:
        conds.append(ai_calls.c.task_id == task_id)
    if requirement_id:
        conds.append(ai_calls.c.requirement_id == requirement_id)
    if purpose:
        conds.append(ai_calls.c.purpose == purpose)
    if model:
        conds.append(ai_calls.c.model == model)
    if status:
        conds.append(ai_calls.c.status == status)
    if by:
        conds.append(ai_calls.c.by == by)
    if since:
        conds.append(ai_calls.c.at >= since)
    if visible is not None:
        conds.append(ai_calls.c.project.in_(sorted(visible)) if visible else ai_calls.c.project == "__none__")
    cols = [c for c in ai_calls.c if c.name not in ("input_preview", "output_preview")]
    with get_engine().begin() as conn:
        total = conn.execute(select(func.count()).select_from(ai_calls).where(*conds)).scalar_one()
        rows = conn.execute(
            select(*cols).where(*conds).order_by(ai_calls.c.id.desc())
            .offset((page - 1) * page_size).limit(page_size)
        ).mappings().all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [{**dict(r), "prompt_versions": json.loads(r["prompt_versions"] or "{}")} for r in rows]}


def get_call(call_id: int) -> dict | None:
    with get_engine().begin() as conn:
        row = conn.execute(select(ai_calls).where(ai_calls.c.id == call_id)).mappings().first()
    if row is None:
        return None
    return {**dict(row), "prompt_versions": json.loads(row["prompt_versions"] or "{}")}


def prompt_versions_for_task(task_id: str) -> dict[str, int]:
    """任务用到的 Prompt 版本汇总（核心规则 23：AI 任务记录 Prompt 版本）。"""
    from app.db import wait_persist

    wait_persist()  # 调用日志走后台写入，先等它落库再汇总
    out: dict[str, int] = {}
    with get_engine().begin() as conn:
        rows = conn.execute(select(ai_calls.c.prompt_versions).where(ai_calls.c.task_id == task_id)).all()
    for (pv,) in rows:
        for k, v in (json.loads(pv or "{}") or {}).items():
            out.setdefault(k, v)
    return out


def stats(since: str | None = None, visible: set[str] | None = None, project: str | None = None) -> dict:
    conds = []
    if since:
        conds.append(ai_calls.c.at >= since)
    if project:
        conds.append(ai_calls.c.project == project)
    elif visible is not None:
        conds.append(ai_calls.c.project.in_(sorted(visible)) if visible else ai_calls.c.project == "__none__")
    with get_engine().begin() as conn:
        by_model = conn.execute(
            select(ai_calls.c.model, func.count(), func.sum(ai_calls.c.prompt_tokens + ai_calls.c.completion_tokens),
                   func.sum(ai_calls.c.elapsed_ms))
            .where(*conds).group_by(ai_calls.c.model)
        ).all()
        by_purpose = conn.execute(
            select(ai_calls.c.purpose, ai_calls.c.status, func.count()).where(*conds)
            .group_by(ai_calls.c.purpose, ai_calls.c.status)
        ).all()
    purposes: dict[str, dict] = {}
    for purpose, status, n in by_purpose:
        p = purposes.setdefault(purpose, {"purpose": purpose, "calls": 0, "errors": 0})
        p["calls"] += n
        if status == "error":
            p["errors"] += n
    return {
        "by_model": [{"model": m, "calls": n, "tokens": int(t or 0), "elapsed_ms": int(e or 0)} for m, n, t, e in by_model],
        "by_purpose": sorted(purposes.values(), key=lambda x: -x["calls"]),
    }


def rename_project(old: str, new: str) -> int:
    from sqlalchemy import update

    with get_engine().begin() as conn:
        return conn.execute(update(ai_calls).where(ai_calls.c.project == old).values(project=new)).rowcount


def purge_before(at: str) -> int:
    with get_engine().begin() as conn:
        return conn.execute(delete(ai_calls).where(ai_calls.c.at < at)).rowcount
