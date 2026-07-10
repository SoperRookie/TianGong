"""XMind 导出（F-5-1）：按 XMind ZEN 文件规范（zip + content.json）自研生成。

层级结构对齐团队模板（docs/architecture/测试用例模版.xmind）：
    需求名称 → 功能模块 → 用例标题（labels=优先级，notes=前置条件）→ 测试步骤 → 预期结果
兼容 XMind 2020+；与 Excel/CSV 导出内容保持一致（F-5-4）。
"""

import json
import uuid
import zipfile
from pathlib import Path

from app.templates import TestCase

_MANIFEST = {"file-entries": {"content.json": {}, "metadata.json": {}}}


def _topic(title: str, *, labels: list[str] | None = None, notes: str = "") -> dict:
    topic: dict = {"id": uuid.uuid4().hex, "title": title}
    if labels:
        topic["labels"] = labels
    if notes:
        topic["notes"] = {
            "plain": {"content": notes},
            "html": {"content": {"paragraphs": [{"spans": [{"text": notes}]}]}},
        }
    return topic


def _attach(parent: dict, child: dict) -> dict:
    parent.setdefault("children", {}).setdefault("attached", []).append(child)
    return child


def _case_topic(case: TestCase) -> dict:
    node = _topic(case.title, labels=[case.priority], notes=case.precondition)
    for step in case.steps:
        step_node = _attach(node, _topic(step.action))
        _attach(step_node, _topic(step.expected))
    return node


def export_xmind(cases: list[TestCase], path: str | Path, root_title: str = "测试用例") -> Path:
    """生成 .xmind 文件；用例按 module 分组为一级子节点，组内保持原有顺序。"""
    path = Path(path)

    root = _topic(root_title)
    root["structureClass"] = "org.xmind.ui.map.unbalanced"
    module_nodes: dict[str, dict] = {}
    for case in cases:
        if case.module not in module_nodes:
            module_nodes[case.module] = _attach(root, _topic(case.module))
        _attach(module_nodes[case.module], _case_topic(case))

    sheet = {
        "id": uuid.uuid4().hex,
        "class": "sheet",
        "title": "画布 1",
        "rootTopic": root,
        "topicPositioning": "fixed",
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps([sheet], ensure_ascii=False))
        zf.writestr("metadata.json", "{}")
        zf.writestr("manifest.json", json.dumps(_MANIFEST))
    return path
