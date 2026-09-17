"""首次部署不预置 LLM 模型：缺 models.yaml 时启动生成空清单，页面添加第一个模型自动成为默认。"""

import httpx
import pytest
import yaml
from asgi_lifespan import LifespanManager

from app.config import get_settings
from app.llm.registry import ModelRegistry, UnknownModelError
from app.main import app


def test_空模型清单可加载_调用时提示去页面配置(tmp_path):
    cfg = tmp_path / "models.yaml"
    cfg.write_text("default_model: null\nmodels: []\n", encoding="utf-8")
    registry = ModelRegistry.from_yaml(cfg)
    assert registry.default_model is None and registry.list_public() == []
    with pytest.raises(UnknownModelError, match="尚未配置任何模型"):
        registry.get()


async def test_首启缺配置文件则生成空清单并保留_embeddings(tmp_path, monkeypatch):
    settings = get_settings()
    cfg = tmp_path / "models.yaml"
    monkeypatch.setattr(settings, "models_config_path", cfg)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert cfg.exists(), "启动应生成 models.yaml"
            data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
            assert data["models"] == [] and data["default_model"] is None
            assert data["embeddings"], "Embedding 段应从样例复制"
            listing = (await client.get("/api/v1/models")).json()
            assert listing["models"] == [] and listing["default_model"] is None

            # 页面添加第一个模型：未指定默认 → 自动成为默认；文件写回且无密钥值
            resp = await client.put("/api/v1/models/config", json={"default_model": None, "max_retries": 1, "models": [
                {"name": "glm-4-plus", "provider": "zhipu", "base_url": "https://open.bigmodel.cn/api/paas/v4",
                 "api_key_env": "ZHIPU_API_KEY", "model": "glm-4-plus"}]})
            assert resp.status_code == 200, resp.text
            assert resp.json()["default_model"] == "glm-4-plus"
            saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
            assert saved["default_model"] == "glm-4-plus" and saved["embeddings"]
            # 建任务时模型缺密钥等错误由既有链路处理；这里只确认清单已可用
            assert (await client.get("/api/v1/models")).json()["default_model"] == "glm-4-plus"
