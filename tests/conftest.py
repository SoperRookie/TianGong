import os

import pytest

from app.llm.registry import ModelRegistry
from app.llm.schemas import ModelConfig


@pytest.fixture(autouse=True, scope="session")
def _isolate_storage(tmp_path_factory):
    """存储隔离：API 测试经真实 lifespan 启动应用，落盘一律指向临时目录，
    避免污染仓库 data/（记忆、使用计数、模板）与 outputs/。"""
    from app.config import get_settings

    base = tmp_path_factory.mktemp("storage")
    os.environ["TIANGONG_OUTPUTS_DIR"] = str(base / "outputs")
    os.environ["TIANGONG_DATA_DIR"] = str(base / "data")
    os.environ["TIANGONG_KNOWLEDGE_DIR"] = str(base / "knowledge")
    get_settings.cache_clear()
    yield


@pytest.fixture
def registry_with_vision() -> ModelRegistry:
    return ModelRegistry(
        default_model="deepseek-chat",
        models=[
            ModelConfig(
                name="deepseek-chat",
                provider="deepseek",
                base_url="https://api.deepseek.com/v1",
                api_key_env="DEEPSEEK_API_KEY",
                model="deepseek-chat",
            ),
            ModelConfig(
                name="qwen-vl",
                provider="dashscope",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                api_key_env="DASHSCOPE_API_KEY",
                model="qwen-vl-max",
                supports_vision=True,
            ),
        ],
    )
