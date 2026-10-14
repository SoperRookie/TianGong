"""项目画像提炼 Agent：素材 + 当前生效画像 → 更新后的画像草稿（JSON）。"""

from __future__ import annotations

import json

from app.agents.graph import _chat_json
from app.agents.prompts import wrap_data
from app.knowledge.profile import normalize_profile
from app.llm.client import LLMClient
from app.prompts import prompt_text


async def run_profile_extract(llm: LLMClient, material: str, current: dict | None, model: str | None) -> dict:
    """返回 {"profile": 规整后的画像, "model_name": 模型}。"""
    current_text = json.dumps(normalize_profile(current), ensure_ascii=False) if current else "（尚无生效画像）"
    user = (
        f"当前已生效的项目画像：\n{wrap_data('当前画像', current_text)}\n\n"
        f"项目素材：\n{wrap_data('项目素材', material)}\n\n"
        "请输出更新后的完整项目画像 JSON。"
    )
    data, result = await _chat_json(
        llm,
        [{"role": "system", "content": prompt_text("profile_extract")}, {"role": "user", "content": user}],
        model,
    )
    return {"profile": normalize_profile(data), "model_name": result.model_name}
