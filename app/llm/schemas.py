"""LLM 适配层数据模型。"""

import os

from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    """单个模型的接入配置（config/models.yaml 中的一个条目）。"""

    name: str = Field(description="系统内唯一标识，任务级切换时使用")
    provider: str = Field(description="厂商标识：deepseek / openai / dashscope / ollama / vllm 等")
    base_url: str
    api_key_env: str | None = Field(default=None, description="密钥所在环境变量名；内网无鉴权服务可为空")
    model: str = Field(description="厂商侧模型名")
    supports_vision: bool = False
    temperature: float = 0.3
    max_tokens: int = 8192
    timeout: float = 120.0
    fallbacks: list[str] = Field(default_factory=list, description="备用模型降级链路（F-1-6），按顺序尝试")

    def resolve_api_key(self) -> str:
        if not self.api_key_env:
            return "EMPTY"  # vLLM/Ollama 等内网服务约定占位
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise MissingAPIKeyError(f"模型 {self.name} 的密钥环境变量 {self.api_key_env} 未设置")
        return key

    def public_view(self, is_default: bool) -> dict:
        """对外接口展示用，不含密钥相关信息。"""
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "supports_vision": self.supports_vision,
            "is_default": is_default,
        }


class MissingAPIKeyError(RuntimeError):
    pass


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
