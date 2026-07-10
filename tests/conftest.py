import pytest

from app.llm.registry import ModelRegistry
from app.llm.schemas import ModelConfig


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
