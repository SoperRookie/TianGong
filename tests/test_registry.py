import pytest

from app.config import get_settings
from app.llm.registry import ModelRegistry, NoVisionModelError, UnknownModelError
from app.llm.schemas import MissingAPIKeyError, ModelConfig


def test_从项目配置文件加载():
    registry = ModelRegistry.from_yaml(get_settings().models_config_path)
    assert registry.default_model == "deepseek-chat"
    cfg = registry.get()
    assert cfg.provider == "deepseek"
    assert cfg.supports_vision is False


def test_任务级模型切换(registry_with_vision):
    assert registry_with_vision.get().name == "deepseek-chat"
    assert registry_with_vision.get("qwen-vl").name == "qwen-vl"
    with pytest.raises(UnknownModelError):
        registry_with_vision.get("不存在的模型")


def test_vision自动路由(registry_with_vision):
    # 默认模型不支持 Vision，图片需求应自动路由至 qwen-vl
    assert registry_with_vision.resolve_vision().name == "qwen-vl"
    assert registry_with_vision.resolve_vision("qwen-vl").name == "qwen-vl"


def test_无vision模型时报错():
    registry = ModelRegistry(
        default_model="m1",
        models=[ModelConfig(name="m1", provider="p", base_url="http://x", model="m1")],
    )
    with pytest.raises(NoVisionModelError):
        registry.resolve_vision()


def test_模型名重复校验():
    cfg = dict(provider="p", base_url="http://x", model="m")
    with pytest.raises(ValueError, match="重复"):
        ModelRegistry(
            default_model="a",
            models=[ModelConfig(name="a", **cfg), ModelConfig(name="a", **cfg)],
        )


def test_默认模型必须在清单中():
    with pytest.raises(ValueError, match="default_model"):
        ModelRegistry(
            default_model="不存在",
            models=[ModelConfig(name="a", provider="p", base_url="http://x", model="m")],
        )


def test_密钥从环境变量解析(monkeypatch):
    cfg = ModelConfig(name="a", provider="p", base_url="http://x", api_key_env="TEST_KEY", model="m")
    monkeypatch.delenv("TEST_KEY", raising=False)
    with pytest.raises(MissingAPIKeyError):
        cfg.resolve_api_key()
    monkeypatch.setenv("TEST_KEY", "sk-xxx")
    assert cfg.resolve_api_key() == "sk-xxx"


def test_内网服务可无密钥():
    cfg = ModelConfig(name="a", provider="ollama", base_url="http://内网:11434/v1", model="m")
    assert cfg.resolve_api_key() == "EMPTY"


def test_对外视图不泄露密钥信息(registry_with_vision):
    for item in registry_with_vision.list_public():
        assert "api_key_env" not in item
        assert "base_url" not in item
