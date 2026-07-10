"""OpenAI 兼容协议客户端（F-1-1/2）：所有厂商与私有化模型统一经此调用。"""

import time
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.llm.registry import ModelRegistry
from app.llm.schemas import ModelConfig, UsageInfo


class ChatResult(BaseModel):
    content: str
    model_name: str  # 系统内标识（配置条目 name）
    provider: str
    usage: UsageInfo
    elapsed_ms: int


class LLMClient:
    def __init__(self, registry: ModelRegistry):
        self.registry = registry
        self._clients: dict[str, AsyncOpenAI] = {}

    def _client_for(self, cfg: ModelConfig) -> AsyncOpenAI:
        if cfg.name not in self._clients:
            self._clients[cfg.name] = AsyncOpenAI(
                base_url=cfg.base_url,
                api_key=cfg.resolve_api_key(),
                timeout=cfg.timeout,
            )
        return self._clients[cfg.name]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        require_vision: bool = False,
        **overrides: Any,
    ) -> ChatResult:
        """发起一次对话补全。

        model: 任务级指定的模型标识，空则用默认模型（F-1-4）。
        require_vision: 消息含图片时置 True，自动路由至 Vision 模型（F-1-5）。
        overrides: 覆盖 temperature / max_tokens 等单次参数。
        """
        cfg = self.registry.resolve_vision(model) if require_vision else self.registry.get(model)
        client = self._client_for(cfg)

        params: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
        }
        params.update(overrides)

        start = time.monotonic()
        resp = await client.chat.completions.create(**params)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        usage = UsageInfo()
        if resp.usage:
            usage = UsageInfo(
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
                total_tokens=resp.usage.total_tokens,
            )
        return ChatResult(
            content=resp.choices[0].message.content or "",
            model_name=cfg.name,
            provider=cfg.provider,
            usage=usage,
            elapsed_ms=elapsed_ms,
        )
