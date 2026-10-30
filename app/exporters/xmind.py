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


def _case_topic(case: TestCase, compact: bool = False) -> dict:
    """用例节点。默认：标题 → 每步操作 → 预期结果 三层子主题（XMind 里每层都计为一个主题）；
    compact：只到用例一层，步骤与预期写进备注，节点数 = 用例数 + 模块数 + 1。"""
    if compact:
        lines = [f"前置条件：{case.precondition}"] if case.precondition else []
        lines += [f"{i}. {s.action}\n   预期：{s.expected}" for i, s in enumerate(case.steps, 1)]
        return _topic(f"{case.case_id} {case.title}", labels=[case.priority], notes="\n".join(lines))
    node = _topic(case.title, labels=[case.priority], notes=case.precondition)
    for step in case.steps:
        step_node = _attach(node, _topic(step.action))
        _attach(step_node, _topic(step.expected))
    return node


def export_xmind(cases: list[TestCase], path: str | Path, root_title: str = "测试用例", compact: bool = False) -> Path:
    """生成 .xmind 文件；用例按 module 分组为一级子节点，组内保持原有顺序。compact 见 _case_topic。"""
    path = Path(path)

    root = _topic(root_title)
    root["structureClass"] = "org.xmind.ui.map.unbalanced"
    module_nodes: dict[str, dict] = {}
    for case in cases:
        if case.module not in module_nodes:
            module_nodes[case.module] = _attach(root, _topic(case.module))
        _attach(module_nodes[case.module], _case_topic(case, compact))

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


_POINT_SOURCE = {"ai": "AI 拆解", "gap": "查漏新增", "supplement": "AI 补充", "manual": "人工", "ai_fix": "修改新增",
                 "blind_spot": "盲区转入", "derived": "经验延伸", "combined": "组合延伸", "e2e": "端到端场景"}
_POINT_STATUS = {"pending": "待审核", "approved": "已通过", "rejected": "已驳回"}


def export_points_xmind(modules: list[dict], path: str | Path, root_title: str = "测试点", approved_only: bool = False,
                        case_links: dict[str, list[str]] | None = None) -> Path:
    """测试点 → .xmind（与页面「脑图」同一结构）：需求 → 模块（条数）→ 测试点「编号 描述」（labels=维度 / 来源 / 状态）→
    子节点：需求来源、依据（经验延伸 / 组合延伸的 basis，组合点带来源编号）、驳回意见、AI 评审意见、覆盖用例编号；
    备注里再写一遍 ref / 依据 / 审核意见，方便只看一层的人。approved_only 只导已通过的；空模块不出现。"""
    from app.terminals import label_of, point_terminal

    path = Path(path)
    case_links = case_links or {}
    root = _topic(root_title)
    root["structureClass"] = "org.xmind.ui.map.unbalanced"
    for entry in modules:
        pts = []
        for p in entry.get("points") or []:
            if isinstance(p, str):
                p = {"point": p}
            if approved_only and p.get("status") != "approved":
                continue
            pts.append(p)
        if not pts:
            continue
        mod_terms = {point_terminal(p) for p in pts}
        mod = _attach(root, _topic(f"{entry.get('module', '')}（{len(pts)}）",
                                   labels=[label_of(t) for t in sorted(mod_terms)] if mod_terms else None))
        for p in pts:
            tp = str(p.get("tp_id") or "")
            src = str(p.get("source") or "ai")
            labels = [x for x in (str(p.get("dimension") or ""),
                                  _POINT_SOURCE.get(src, src) if src not in ("ai",) else "",
                                  _POINT_STATUS.get(str(p.get("status") or "pending"), "")) if x]
            notes = []
            if p.get("ref"):
                notes.append(f"需求来源：{p['ref']}")
            if p.get("basis"):
                notes.append(("组合依据" if src == "combined" else "依据") + f"：{p['basis']}"
                             + (f"（由 {'、'.join(p['combo_from'])} 组合）" if p.get("combo_from") else ""))
            if p.get("status") == "rejected" and (p.get("comment") or p.get("fix_request")):
                notes.append(f"驳回：{p.get('comment') or ''}" + (f"｜要求：{p['fix_request']}" if p.get("fix_request") else ""))
            elif p.get("comment"):
                notes.append(f"审核意见：{p['comment']}")
            rv = p.get("ai_review") or {}
            if rv.get("verdict"):
                notes.append(f"AI 评审·{rv['verdict']}：{rv.get('fix') or rv.get('note') or ''}")
            linked = case_links.get(tp) or []
            if linked:
                notes.append(f"覆盖用例：{'、'.join(linked)}")
            node = _attach(mod, _topic(f"{tp} {p.get('point', '')}".strip(), labels=labels, notes="\n".join(notes)))
            for line in notes:
                _attach(node, _topic(line))
    sheet = {"id": uuid.uuid4().hex, "class": "sheet", "title": "测试点", "rootTopic": root, "topicPositioning": "fixed"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps([sheet], ensure_ascii=False))
        zf.writestr("metadata.json", "{}")
        zf.writestr("manifest.json", json.dumps(_MANIFEST))
    return path
