from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.llm.client import LLMClient


def _fake_response(content: str = "好的"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _stub_openai(captured: dict):
    async def create(**params):
        captured.update(params)
        return _fake_response()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=create))))


async def test_默认模型调用与用量统计(registry_with_vision):
    client = LLMClient(registry_with_vision)
    captured: dict = {}
    client._clients["deepseek-chat"] = _stub_openai(captured)

    result = await client.chat([{"role": "user", "content": "你好"}])

    assert result.content == "好的"
    assert result.model_name == "deepseek-chat"
    assert result.usage.total_tokens == 15
    assert captured["model"] == "deepseek-chat"
    assert captured["temperature"] == 0.3


async def test_单次参数覆盖(registry_with_vision):
    client = LLMClient(registry_with_vision)
    captured: dict = {}
    client._clients["deepseek-chat"] = _stub_openai(captured)

    await client.chat([{"role": "user", "content": "hi"}], temperature=0.9, max_tokens=100)

    assert captured["temperature"] == 0.9
    assert captured["max_tokens"] == 100


async def test_图片需求自动路由至vision模型(registry_with_vision):
    client = LLMClient(registry_with_vision)
    captured: dict = {}
    client._clients["qwen-vl"] = _stub_openai(captured)

    result = await client.chat(
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:..."}}]}],
        require_vision=True,
    )

    assert result.model_name == "qwen-vl"
    assert captured["model"] == "qwen-vl-max"
