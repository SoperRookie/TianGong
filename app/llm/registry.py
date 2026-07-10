"""模型注册表：加载 models.yaml，提供任务级模型解析与 Vision 路由（F-1-3/4/5）。"""

from pathlib import Path

import yaml

from app.llm.schemas import ModelConfig


class UnknownModelError(KeyError):
    pass


class NoVisionModelError(RuntimeError):
    pass


class ModelRegistry:
    def __init__(self, default_model: str, models: list[ModelConfig]):
        names = [m.name for m in models]
        if len(names) != len(set(names)):
            dup = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"模型配置 name 重复: {dup}")
        self._models = {m.name: m for m in models}
        if default_model not in self._models:
            raise ValueError(f"default_model={default_model} 不在模型清单中")
        self.default_model = default_model

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ModelRegistry":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"模型配置文件不存在: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        models = [ModelConfig.model_validate(item) for item in data.get("models", [])]
        if not models:
            raise ValueError(f"模型配置文件 {path} 中未定义任何模型")
        return cls(default_model=data.get("default_model", models[0].name), models=models)

    def get(self, name: str | None = None) -> ModelConfig:
        """任务级模型切换：name 为空时返回默认模型（F-1-4）。"""
        target = name or self.default_model
        if target not in self._models:
            raise UnknownModelError(f"未注册的模型: {target}，可用: {sorted(self._models)}")
        return self._models[target]

    def resolve_vision(self, name: str | None = None) -> ModelConfig:
        """图片需求路由：所选模型不支持 Vision 时自动改用 Vision 模型（F-1-5）。"""
        chosen = self.get(name)
        if chosen.supports_vision:
            return chosen
        candidates = [m for m in self._models.values() if m.supports_vision]
        if not candidates:
            raise NoVisionModelError(
                "当前配置中没有支持 Vision 的模型，无法处理图片类需求；"
                "请在 config/models.yaml 中添加 supports_vision: true 的模型"
            )
        return candidates[0]

    def list_public(self) -> list[dict]:
        return [m.public_view(is_default=(m.name == self.default_model)) for m in self._models.values()]
