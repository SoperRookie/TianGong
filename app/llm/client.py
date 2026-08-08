"""OpenAI 兼容协议客户端（F-1-1/2）：所有厂商与私有化模型统一经此调用。

容错与降级（F-1-6）：单模型失败自动重试（次数由 models.yaml 的 max_retries 配置），
重试耗尽后按 fallbacks 链路降级到备用模型；全链路失败抛 AllModelsFailedError。
"""

import time
from typing import Any

import openai
from loguru import logger
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.llm.registry import ModelRegistry
from app.llm.schemas import ModelConfig, UsageInfo

# 可重试的暂时性错误：网络/超时/限流/服务端 5xx；鉴权与参数错误不重试
_RETRYABLE = (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)


class AllModelsFailedError(RuntimeError):
    pass


class ChatResult(BaseModel):
    content: str
    model_name: str  # 系统内标识（配置条目 name）
    provider: str
    usage: UsageInfo
    elapsed_ms: int
    attempts: int = 1  # 实际尝试次数（含重试与降级）


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
        """发起一次对话补全（含自动重试与降级）。

        model: 任务级指定的模型标识，空则用默认模型（F-1-4）。
        require_vision: 消息含图片时置 True，自动路由至 Vision 模型（F-1-5）。
        overrides: 覆盖 temperature / max_tokens 等单次参数。
        """
        chain = self.registry.call_chain(model, require_vision=require_vision)
        attempts = 0
        last_error: Exception | None = None
        for i, cfg in enumerate(chain):
            if i > 0:
                logger.warning("模型降级：{} → {}（原因: {}）", chain[i - 1].name, cfg.name, last_error)
            for _ in range(1 + self.registry.max_retries):
                attempts += 1
                try:
                    return await self._call_once(cfg, messages, attempts, **overrides)
                except _RETRYABLE as e:
                    last_error = e
                    logger.warning("模型调用失败（第 {} 次，{}）：{}", attempts, cfg.name, e)
        tried = " → ".join(c.name for c in chain)
        logger.error("模型全链路失败（{} 次，链路: {}）：{}", attempts, tried, last_error)
        raise AllModelsFailedError(
            f"模型调用失败（已尝试 {attempts} 次，链路: {tried}）: {last_error}"
        ) from last_error

    async def _call_once(
        self, cfg: ModelConfig, messages: list[dict[str, Any]], attempts: int, **overrides: Any
    ) -> ChatResult:
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
        logger.info(
            "LLM 调用完成：{} {}ms tokens={}（输入 {} / 输出 {}）",
            cfg.name, elapsed_ms, usage.total_tokens, usage.prompt_tokens, usage.completion_tokens,
        )
        return ChatResult(
            content=resp.choices[0].message.content or "",
            model_name=cfg.name,
            provider=cfg.provider,
            usage=usage,
            elapsed_ms=elapsed_ms,
            attempts=attempts,
        )
