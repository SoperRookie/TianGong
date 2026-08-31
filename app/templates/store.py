"""模板库管理（F-4-4）：入库持久化（kv_docs），旧 templates.json 首启自动迁移。"""

from pathlib import Path

from app.templates.custom import PRIORITY_ENUM, CustomTemplate, TemplateField

_DEFAULT_COLUMNS = [
    TemplateField(name="用例编号", maps_to="case_id", required=True),
    TemplateField(name="所属模块", maps_to="module", required=True),
    TemplateField(name="用例标题", maps_to="title", required=True),
    TemplateField(name="优先级", maps_to="priority", required=True, enum_values=list(PRIORITY_ENUM)),
    TemplateField(name="前置条件", maps_to="precondition"),
    TemplateField(name="测试步骤", maps_to="steps", required=True),
    TemplateField(name="预期结果", maps_to="expected", required=True),
    TemplateField(name="关键词", maps_to="keywords"),
    TemplateField(name="备注", maps_to="remark"),
]


def builtin_default_template() -> CustomTemplate:
    """内置默认模板（F-4-2），字段与 PRD 定义一致。"""
    return CustomTemplate(template_id="builtin-default", name="内置默认模板", columns=_DEFAULT_COLUMNS)


class TemplateStore:
    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self._templates: dict[str, CustomTemplate] = {}
        self.default_id = "builtin-default"
        self._load()
        # 内置模板始终以代码定义为准（列结构升级后旧持久化文件自动刷新）
        self._templates["builtin-default"] = builtin_default_template()
        self._persist()

    def _load(self) -> None:
        from app.db import DocStore, load_with_migration

        self._doc = DocStore("templates")
        raw = load_with_migration(self._doc, self.storage_path, lambda data: {"doc": data})
        data = raw.get("doc") or {}
        self._templates = {
            t["template_id"]: CustomTemplate.model_validate(t) for t in data.get("templates", [])
        }
        self.default_id = data.get("default_id", "builtin-default")

    def _persist(self) -> None:
        self._doc.put("doc", {
            "default_id": self.default_id,
            "templates": [t.model_dump() for t in self._templates.values()],
        })

    def save(self, template: CustomTemplate) -> CustomTemplate:
        self._templates[template.template_id] = template
        self._persist()
        return template

    def get(self, template_id: str | None = None) -> CustomTemplate | None:
        return self._templates.get(template_id or self.default_id)

    def list(self) -> list[dict]:
        return [
            {
                "template_id": t.template_id,
                "name": t.name,
                "columns": len(t.columns),
                "is_default": t.template_id == self.default_id,
            }
            for t in self._templates.values()
        ]

    def delete(self, template_id: str) -> bool:
        if template_id == "builtin-default":
            raise ValueError("内置默认模板不可删除")
        existed = self._templates.pop(template_id, None) is not None
        if existed:
            if self.default_id == template_id:
                self.default_id = "builtin-default"
            self._persist()
        return existed

    def set_default(self, template_id: str) -> None:
        if template_id not in self._templates:
            raise KeyError(f"模板不存在: {template_id}")
        self.default_id = template_id
        self._persist()
