"""表格导出（F-5-2 Excel / F-5-3 CSV）。

列结构由模板决定（F-4-5：导出字段与模板 100% 一致），缺省用内置默认模板。
Excel：表头样式、列宽自适应、优先级条件着色。
CSV：UTF-8 with BOM，便于导入禅道 / TestLink / Jira。
"""

import csv
from pathlib import Path
from typing import Callable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.templates import CustomTemplate, TestCase, builtin_default_template

_PRIORITY_FILLS = {
    "P0": PatternFill("solid", fgColor="F4CCCC"),  # 红
    "P1": PatternFill("solid", fgColor="FCE5CD"),  # 橙
    "P2": PatternFill("solid", fgColor="FFF2CC"),  # 黄
    "P3": PatternFill("solid", fgColor="EFEFEF"),  # 灰
}
_HEADER_FILL = PatternFill("solid", fgColor="4472C4")
_MAX_COL_WIDTH = 60

_CANONICAL_GETTERS: dict[str, Callable[[TestCase], str]] = {
    "case_id": lambda c: c.case_id,
    "module": lambda c: c.module,
    "title": lambda c: c.title,
    "priority": lambda c: c.priority,
    "precondition": lambda c: c.precondition,
    "steps": lambda c: "\n".join(f"{i}. {s.action}" for i, s in enumerate(c.steps, 1)),
    "expected": lambda c: "\n".join(f"{i}. {s.expected}" for i, s in enumerate(c.steps, 1)),
    "keywords": lambda c: c.keywords,
    "remark": lambda c: c.remark,
}


def _headers(template: CustomTemplate) -> list[str]:
    return [col.name for col in template.columns]


_CONTROL_RE = __import__("re").compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def safe_cell(value) -> str:
    """导出单元格防护：去掉 openpyxl 拒绝的控制字符；以 = + - @ 或制表/回车开头的文本加前导单引号，
    防止 Excel/CSV 把用例文本当公式执行（CSV/公式注入）。"""
    text = _CONTROL_RE.sub("", "" if value is None else str(value))
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def _rows(cases: list[TestCase], template: CustomTemplate) -> list[list[str]]:
    rows = []
    for case in cases:
        row = []
        for col in template.columns:
            getter = _CANONICAL_GETTERS.get(col.maps_to)
            row.append(safe_cell(getter(case) if getter else case.extras.get(col.name, "")))
        rows.append(row)
    return rows


def export_excel(
    cases: list[TestCase], path: str | Path, template: CustomTemplate | None = None
) -> Path:
    template = template or builtin_default_template()
    headers = _headers(template)
    path = Path(path)
    wb = Workbook()
    ws = wb.active
    ws.title = "测试用例"

    ws.append([safe_cell(h) for h in headers])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row in _rows(cases, template):
        ws.append(row)
        for cell in ws[ws.max_row]:  # 强制字符串类型，杜绝被识别为公式
            if isinstance(cell.value, str):
                cell.data_type = "s"

    priority_col = next(
        (i + 1 for i, col in enumerate(template.columns) if col.maps_to == "priority"), None
    )
    for row_idx in range(2, ws.max_row + 1):
        if priority_col:
            cell = ws.cell(row=row_idx, column=priority_col)
            fill = _PRIORITY_FILLS.get(str(cell.value))
            if fill:
                cell.fill = fill
        for col_idx in range(1, len(headers) + 1):
            ws.cell(row=row_idx, column=col_idx).alignment = Alignment(
                vertical="top", wrap_text=True
            )

    # 列宽自适应：按内容最长行估算（中文按 2 个宽度计），设上限
    for col_idx in range(1, len(headers) + 1):
        max_len = 0
        for row_idx in range(1, ws.max_row + 1):
            value = ws.cell(row=row_idx, column=col_idx).value or ""
            for line in str(value).splitlines():
                width = sum(2 if ord(ch) > 127 else 1 for ch in line)
                max_len = max(max_len, width)
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 4, _MAX_COL_WIDTH)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return path


def export_csv(
    cases: list[TestCase], path: str | Path, template: CustomTemplate | None = None
) -> Path:
    template = template or builtin_default_template()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([safe_cell(h) for h in _headers(template)])
        writer.writerows(_rows(cases, template))
    return path


# ---- 测试点导出（xlsx）----

_POINT_HEADERS = ["编号", "模块", "测试点", "维度", "来源", "状态", "审核意见", "驳回类型", "已生成用例", "覆盖用例编号"]
_POINT_SOURCE = {"ai": "AI 拆解", "gap": "查漏新增", "supplement": "AI 补充", "manual": "人工", "ai_fix": "修改新增"}
_POINT_STATUS = {"pending": "待审核", "approved": "已通过", "rejected": "已驳回"}


def export_points_excel(modules: list[dict], path: str | Path, case_links: dict[str, list[str]] | None = None,
                        generated: set[str] | None = None, approved_only: bool = False) -> Path:
    """测试点 → Excel：编号 / 模块 / 测试点 / 维度 / 来源 / 状态 / 审核意见 / 驳回类型 / 是否已生成用例 / 覆盖的用例编号。"""
    path = Path(path)
    case_links = case_links or {}
    generated = generated or set()
    wb = Workbook()
    ws = wb.active
    ws.title = "测试点"
    ws.append(_POINT_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(vertical="center")
    for entry in modules:
        for p in entry.get("points") or []:
            if isinstance(p, str):
                p = {"point": p}
            if approved_only and p.get("status") != "approved":
                continue
            tp = str(p.get("tp_id") or "")
            linked = case_links.get(tp) or []
            row = [tp, str(entry.get("module", "")), str(p.get("point", "")), str(p.get("dimension", "")),
                   _POINT_SOURCE.get(p.get("source", "ai"), str(p.get("source", ""))),
                   _POINT_STATUS.get(p.get("status", "pending"), str(p.get("status", ""))),
                   str(p.get("comment") or ""), "、".join(p.get("reject_types") or []),
                   "是" if (tp in generated or linked) else "否", "、".join(linked)]
            ws.append([safe_cell(v) for v in row])
            for cell in ws[ws.max_row]:
                cell.data_type = "s"
                cell.alignment = Alignment(vertical="top", wrap_text=True)
    for col_idx in range(1, len(_POINT_HEADERS) + 1):
        max_len = 0
        for row_idx in range(1, ws.max_row + 1):
            value = ws.cell(row=row_idx, column=col_idx).value or ""
            for line in str(value).splitlines():
                max_len = max(max_len, sum(2 if ord(ch) > 127 else 1 for ch in line))
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 4, _MAX_COL_WIDTH)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return path
