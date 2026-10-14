"""Agent 调用上下文：当前项目画像（随 AI 操作上下文设置，拆解 / 生成 / 评审 / 查漏等 Agent 统一取用）。

用 ContextVar 而非层层传参：后台任务由 asyncio.create_task 创建时自动复制上下文，和调用日志的 ai_context 一致。
"""

from __future__ import annotations

from contextvars import ContextVar

project_profile: ContextVar[str | None] = ContextVar("project_profile", default=None)


def set_profile(text: str | None) -> None:
    project_profile.set(text or None)


def profile_block() -> str:
    """当前项目画像的注入块（无画像时为空串，直接拼到用户消息末尾即可）。"""
    text = project_profile.get()
    if not text:
        return ""
    from app.agents.prompts import wrap_data
    from app.prompts import prompt_text

    return prompt_text("profile_block").format(profile=wrap_data("项目画像", text))
