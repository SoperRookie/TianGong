"""模型连通性测试只测指定模型：密钥缺失直接报原因，不能被降级链路的备用模型「测通」。"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from app.llm.client import LLMClient
from app.llm.registry import ModelRegistry
from app.llm.schemas import ModelConfig
from app.main import app


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def _registry(monkeypatch) -> ModelRegistry:
    monkeypatch.setenv("BACKUP_TEST_API_KEY", "sk-backup")
    backup = ModelConfig(name="backup", provider="mock", base_url="http://127.0.0.1:9",
                         api_key_env="BACKUP_TEST_API_KEY", model="backup")
    zhipu = ModelConfig(name="glm-4-plus", provider="zhipu", base_url="https://open.bigmodel.cn/api/paas/v4",
                        api_key_env="ZHIPU_TEST_API_KEY", model="glm-4-plus", fallbacks=["backup"])
    return ModelRegistry(default_model="backup", models=[backup, zhipu])


async def test_密钥未设置直接报原因不降级(client, monkeypatch):
    monkeypatch.delenv("ZHIPU_TEST_API_KEY", raising=False)
    app.state.registry = _registry(monkeypatch)
    calls: list[str] = []

    async def fake_call_once(self, cfg, messages, attempts, **overrides):
        calls.append(cfg.name)
        raise AssertionError("密钥缺失时不应发起任何模型调用")

    monkeypatch.setattr(LLMClient, "_call_once", fake_call_once)
    app.state.llm = LLMClient(app.state.registry)
    r = (await client.post("/api/v1/models/test", json={"name": "glm-4-plus"})).json()
    assert r["ok"] is False and "ZHIPU_TEST_API_KEY" in r["error"] and "重启" in r["error"]
    assert calls == []


async def test_指定模型失败时不用备用模型冒充成功(client, monkeypatch):
    monkeypatch.setenv("ZHIPU_TEST_API_KEY", "sk-wrong")
    app.state.registry = _registry(monkeypatch)
    calls: list[str] = []

    async def fake_call_once(self, cfg, messages, attempts, **overrides):
        import openai
        calls.append(cfg.name)
        if cfg.name == "glm-4-plus":
            raise openai.AuthenticationError("401 invalid api key", response=httpx.Response(401, request=httpx.Request("POST", cfg.base_url)), body=None)
        from app.llm.client import ChatResult
        from app.llm.schemas import UsageInfo
        return ChatResult(content="pong", model_name=cfg.name, provider=cfg.provider, usage=UsageInfo(),
                          elapsed_ms=1, attempts=attempts)

    monkeypatch.setattr(LLMClient, "_call_once", fake_call_once)
    app.state.llm = LLMClient(app.state.registry)
    r = (await client.post("/api/v1/models/test", json={"name": "glm-4-plus"})).json()
    assert r["ok"] is False and "glm-4-plus" in r["model"]
    assert calls == ["glm-4-plus"], "不应降级到 backup"
    # 正常路径：备用模型仍可用降级（业务调用不受影响）
    res = await app.state.llm.chat([{"role": "user", "content": "hi"}], model="glm-4-plus")
    assert res.model_name == "backup"
