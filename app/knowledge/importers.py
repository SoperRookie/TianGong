"""历史用例入库（F-7-2）：Excel/CSV/XMind 存量用例 → 测试用例库。

与导出器（app/exporters）互为镜像：表格列名复用模板识别的启发式映射
（app/templates/custom.py），XMind 按团队模板层级解析（优先级在节点 labels、
前置条件在用例节点 notes）。每条用例渲染为一个独立检索切片。
"""

import csv
import io
import json
import re
import zipfile
from pathlib import Path

from openpyxl import load_workbook

from app.templates.custom import _map_canonical

_PRIORITY_RE = re.compile(r"^[Pp]([0-9])$")

# 步骤/预期列中的行前编号："1. xxx" / "1、xxx" / "1) xxx"
_STEP_NO_RE = re.compile(r"^\s*\d+\s*[.、)．]\s*")


class CaseImportError(ValueError):
    pass


def normalize_priority(value: str) -> str:
    """优先级归一：大写；P4 及以上并入 P3（2026-07-10 四级决策）；其余原样保留。"""
    value = str(value or "").strip().upper()
    m = _PRIORITY_RE.match(value)
    if m and int(m.group(1)) > 3:
        return "P3"
    return value


def parse_cases_file(path: str | Path) -> list[dict]:
    """按扩展名解析存量用例文件，返回规范化用例字典列表。"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return _parse_xlsx(path)
    if suffix == ".csv":
        return _parse_csv(path)
    if suffix == ".xmind":
        return _parse_xmind(path)
    raise CaseImportError(f"不支持的用例文件格式 {suffix}，当前支持: .xlsx / .csv / .xmind")


def render_case_chunk(case: dict) -> str:
    """将一条用例渲染为知识切片文本（检索与注入的最小单元）。"""
    head_parts = [f"模块：{case.get('module', '')}"]
    if case.get("priority"):
        head_parts.append(f"优先级：{case['priority']}")
    if case.get("case_id"):
        head_parts.append(f"编号：{case['case_id']}")
    lines = [f"【历史用例】{case.get('title', '')}", " | ".join(head_parts)]
    if case.get("precondition"):
        lines.append(f"前置条件：{case['precondition']}")
    steps = case.get("steps") or []
    if steps:
        lines.append("步骤：")
        lines.extend(
            f"{i}. {s.get('action', '')} → 预期：{s.get('expected', '')}"
            for i, s in enumerate(steps, 1)
        )
    if case.get("keywords"):
        lines.append(f"关键词：{case['keywords']}")
    if case.get("remark"):
        lines.append(f"备注：{case['remark']}")
    return "\n".join(lines)


# ---- 表格（Excel / CSV）----


def _parse_xlsx(path: Path) -> list[dict]:
    ws = load_workbook(path, read_only=True, data_only=True).active
    rows = [[("" if c is None else str(c)) for c in row] for row in ws.iter_rows(values_only=True)]
    return _rows_to_cases(rows, path.name)


def _parse_csv(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig")
    rows = [list(row) for row in csv.reader(io.StringIO(text))]
    return _rows_to_cases(rows, path.name)


def _rows_to_cases(rows: list[list[str]], filename: str) -> list[dict]:
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        raise CaseImportError(f"{filename} 内容为空")
    headers = [str(h).strip() for h in rows[0]]
    mapping = {i: _map_canonical(h) for i, h in enumerate(headers) if h}
    if "title" not in mapping.values():
        raise CaseImportError(
            f"{filename} 表头未识别到用例标题列（首行表头: {[h for h in headers if h]}）"
        )
    cases = []
    for row in rows[1:]:
        record: dict = {}
        for i, canonical in mapping.items():
            if canonical == "custom" or i >= len(row):
                continue
            record.setdefault(canonical, str(row[i] or "").strip())
        if not record.get("title"):
            continue
        record["priority"] = normalize_priority(record.get("priority", ""))
        record["steps"] = _pair_steps(record.pop("steps", ""), record.pop("expected", ""))
        cases.append(record)
    if not cases:
        raise CaseImportError(f"{filename} 未解析到任何用例数据行")
    return cases


def _pair_steps(steps_text: str, expected_text: str) -> list[dict]:
    """把「1. 动作」与「1. 预期」两列多行文本还原为步骤对（数量不齐时补空）。"""
    actions = [_STEP_NO_RE.sub("", s).strip() for s in steps_text.splitlines() if s.strip()]
    expecteds = [_STEP_NO_RE.sub("", s).strip() for s in expected_text.splitlines() if s.strip()]
    length = max(len(actions), len(expecteds))
    return [
        {
            "action": actions[i] if i < len(actions) else "",
            "expected": expecteds[i] if i < len(expecteds) else "",
        }
        for i in range(length)
    ]


# ---- XMind（ZEN 格式：zip + content.json）----


def _parse_xmind(path: Path) -> list[dict]:
    try:
        with zipfile.ZipFile(path) as zf:
            sheets = json.loads(zf.read("content.json"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
        raise CaseImportError(f"{path.name} 不是有效的 XMind 2020+ 文件（ZEN 格式）: {e}")
    cases: list[dict] = []
    for sheet in sheets:
        root = sheet.get("rootTopic") or {}
        for child in _children(root):
            _walk_topic(child, module_path=[], cases=cases)
    if not cases:
        raise CaseImportError(f"{path.name} 未解析到任何用例节点（用例节点需带优先级标签或备注）")
    return cases


def _children(topic: dict) -> list[dict]:
    return (topic.get("children") or {}).get("attached") or []


def _notes(topic: dict) -> str:
    return (((topic.get("notes") or {}).get("plain") or {}).get("content") or "").strip()


def _priority_label(topic: dict) -> str | None:
    for label in topic.get("labels") or []:
        if _PRIORITY_RE.match(str(label).strip()):
            return normalize_priority(label)
    return None


def _walk_topic(topic: dict, module_path: list[str], cases: list[dict]) -> None:
    """递归下钻：带优先级标签或备注的节点为用例；其余带子节点的为模块层级。

    团队模板约定（docs/architecture/测试用例模版.xmind）：优先级在 labels、
    前置条件在用例节点 notes；用例下为「步骤 → 预期结果（步骤子节点）」。
    无标签无备注的叶子节点视为纯测试点（仅标题）。
    """
    title = str(topic.get("title", "")).strip()
    priority = _priority_label(topic)
    notes = _notes(topic)
    children = _children(topic)
    if priority is not None or notes or not children:
        steps = [
            {
                "action": str(step.get("title", "")).strip(),
                "expected": str(_children(step)[0].get("title", "")).strip() if _children(step) else "",
            }
            for step in children
        ]
        cases.append(
            {
                "case_id": "",
                "module": "/".join(module_path) or "未分组",
                "title": title,
                "priority": priority or "",
                "precondition": notes,
                "steps": steps,
                "remark": "",
            }
        )
        return
    for child in children:
        _walk_topic(child, module_path + [title], cases)
