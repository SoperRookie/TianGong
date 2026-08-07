"""Embedding 适配层（F-7-10）：与 LLM 同构的多厂商配置，统一 OpenAI 兼容 /embeddings 协议。

私有化（Ollama/vLLM）与商用 API 均以配置条目接入，选型切换只改 models.yaml。
"""

import asyncio

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.llm.schemas import MissingAPIKeyError  # noqa: F401  复用密钥缺失异常语义

_RETRYABLE = (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)

# 单次请求的批量上限：兼顾商用 API 的 batch 限制与内网服务吞吐
_BATCH_SIZE = 32


class EmbeddingConfig(BaseModel):
    """models.yaml 中 embeddings 段的一个条目。"""

    name: str = Field(description="系统内唯一标识")
    provider: str
    base_url: str
    api_key_env: str | None = None
    model: str
    dimensions: int = Field(description="向量维度，建库时用于校验集合 schema")
    timeout: float = 60.0

    def resolve_api_key(self) -> str:
        import os

        if not self.api_key_env:
            return "EMPTY"
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise MissingAPIKeyError(f"Embedding 模型 {self.name} 的密钥环境变量 {self.api_key_env} 未设置")
        return key


class UnknownEmbeddingError(KeyError):
    pass


class EmbeddingRegistry:
    """加载 models.yaml 的 embeddings 段，提供按名解析与默认模型。"""

    def __init__(self, default_embedding: str, configs: list[EmbeddingConfig]):
        names = [c.name for c in configs]
        if len(names) != len(set(names)):
            dup = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"Embedding 配置 name 重复: {dup}")
        self._configs = {c.name: c for c in configs}
        if default_embedding not in self._configs:
            raise ValueError(f"default_embedding={default_embedding} 不在 embeddings 清单中")
        self.default_embedding = default_embedding

    @classmethod
    def from_yaml(cls, path) -> "EmbeddingRegistry":
        from pathlib import Path

        import yaml

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"模型配置文件不存在: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        configs = [EmbeddingConfig.model_validate(item) for item in data.get("embeddings", [])]
        if not configs:
            raise ValueError(f"{path} 中未定义任何 Embedding 模型（embeddings 段为空）")
        return cls(
            default_embedding=data.get("default_embedding", configs[0].name),
            configs=configs,
        )

    def get(self, name: str | None = None) -> EmbeddingConfig:
        target = name or self.default_embedding
        if target not in self._configs:
            raise UnknownEmbeddingError(f"未注册的 Embedding 模型: {target}，可用: {sorted(self._configs)}")
        return self._configs[target]

    def list_public(self) -> list[dict]:
        return [
            {
                "name": c.name,
                "provider": c.provider,
                "model": c.model,
                "dimensions": c.dimensions,
                "is_default": c.name == self.default_embedding,
            }
            for c in self._configs.values()
        ]


class EmbeddingClient:
    """批量向量化客户端：自动分批、暂时性错误重试。"""

    def __init__(self, registry: EmbeddingRegistry, max_retries: int = 1):
        self.registry = registry
        self.max_retries = max_retries
        self._clients: dict[str, AsyncOpenAI] = {}

    def _client_for(self, cfg: EmbeddingConfig) -> AsyncOpenAI:
        if cfg.name not in self._clients:
            self._clients[cfg.name] = AsyncOpenAI(
                base_url=cfg.base_url,
                api_key=cfg.resolve_api_key(),
                timeout=cfg.timeout,
            )
        return self._clients[cfg.name]

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """将文本列表向量化，返回与输入等长的向量列表。"""
        if not texts:
            return []
        cfg = self.registry.get(model)
        vectors: list[list[float]] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i : i + _BATCH_SIZE]
            vectors.extend(await self._embed_batch(cfg, batch))
        return vectors

    async def _embed_batch(self, cfg: EmbeddingConfig, batch: list[str]) -> list[list[float]]:
        client = self._client_for(cfg)
        last_error: Exception | None = None
        for attempt in range(1 + self.max_retries):
            try:
                resp = await client.embeddings.create(model=cfg.model, input=batch)
                # 按 index 还原顺序，协议不保证返回有序
                ordered = sorted(resp.data, key=lambda d: d.index)
                return [d.embedding for d in ordered]
            except _RETRYABLE as e:
                last_error = e
                if attempt < self.max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise last_error
