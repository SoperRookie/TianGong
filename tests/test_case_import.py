"""历史用例入库测试（F-7-2）：三格式解析、导出→导入回环、优先级归一、入库检索。"""

import pytest

from app.exporters import export_csv, export_excel, export_xmind
from app.knowledge import KnowledgeService, KnowledgeStore
from app.knowledge.importers import (
    CaseImportError,
    normalize_priority,
    parse_cases_file,
    render_case_chunk,
)
from app.templates import TestCase
from tests.stubs import make_case
from tests.test_knowledge import StubEmbedder, _VOCAB


def _cases() -> list[TestCase]:
    return [
        TestCase.model_validate(make_case()),
        TestCase.model_validate(
            make_case(
                case_id="TC-投注-001",
                module="投注",
                title="验证赔率结算正确",
                priority="P0",
                precondition="已进入牌桌",
                steps=[
                    {"action": "下注一对玩法", "expected": "扣款成功"},
                    {"action": "开牌为一对", "expected": "按4.8赔率派彩"},
                ],
            )
        ),
    ]


# ---- 导出 → 导入回环：与导出器互为镜像 ----


@pytest.mark.parametrize("fmt", ["xlsx", "csv", "xmind"])
def test_roundtrip_with_exporters(tmp_path, fmt):
    exporter = {"xlsx": export_excel, "csv": export_csv, "xmind": export_xmind}[fmt]
    path = exporter(_cases(), tmp_path / f"用例.{fmt}")
    parsed = parse_cases_file(path)
    assert len(parsed) == 2
    bet = next(c for c in parsed if c["title"] == "验证赔率结算正确")
    assert bet["priority"] == "P0"
    assert bet["precondition"] == "已进入牌桌"
    assert len(bet["steps"]) == 2
    assert bet["steps"][1]["action"] == "开牌为一对"
    assert bet["steps"][1]["expected"] == "按4.8赔率派彩"
    assert "投注" in bet["module"]


def test_priority_normalization():
    assert normalize_priority("p4") == "P3"
    assert normalize_priority("P5") == "P3"
    assert normalize_priority("p1") == "P1"
    assert normalize_priority("高") == "高"  # 自定义枚举原样保留（F-4-1）


def test_render_case_chunk_contains_key_fields():
    text = render_case_chunk({
        "case_id": "TC-1", "module": "投注", "title": "验证限红", "priority": "P2",
        "precondition": "已入座", "steps": [{"action": "超限下注", "expected": "提示超出限红"}],
        "remark": "",
    })
    assert "【历史用例】验证限红" in text
    assert "优先级：P2" in text
    assert "超限下注 → 预期：提示超出限红" in text


def test_tabular_without_title_column_rejected(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("字段A,字段B\n1,2\n", encoding="utf-8-sig")
    with pytest.raises(CaseImportError, match="标题"):
        parse_cases_file(path)


def test_unsupported_format_rejected(tmp_path):
    path = tmp_path / "cases.docx"
    path.write_text("x")
    with pytest.raises(CaseImportError, match="格式"):
        parse_cases_file(path)


def test_xmind_bare_test_point_becomes_title_only_case(tmp_path):
    # 团队脑图中常见：无标签无备注的叶子节点是纯测试点
    import json, zipfile

    sheet = [{
        "rootTopic": {
            "title": "项目",
            "children": {"attached": [{
                "title": "模块A",
                "children": {"attached": [{"title": "验证断线重连"}]},
            }]},
        }
    }]
    path = tmp_path / "点.xmind"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.json", json.dumps(sheet, ensure_ascii=False))
    parsed = parse_cases_file(path)
    assert parsed == [{
        "case_id": "", "module": "模块A", "title": "验证断线重连", "priority": "",
        "precondition": "", "steps": [], "remark": "",
    }]


async def test_ingest_cases_searchable(tmp_path):
    store = KnowledgeStore(":memory:", dimensions=len(_VOCAB))
    service = KnowledgeService(store, StubEmbedder())
    path = export_excel(_cases(), tmp_path / "历史用例.xlsx")
    doc = await service.ingest_cases(path, space="game1")
    assert doc.category == "test_cases"
    assert doc.chunk_count == 2
    hits = await service.search("赔率结算", category="test_cases")
    assert hits and "验证赔率结算正确" in hits[0].text


# ---- 禅道导出兼容 ----

_ZENTAO_HEADERS = ["用例编号", "所属产品", "所属模块", "相关需求", "用例标题", "前置条件", "步骤", "预期", "关键词", "用例类型", "优先级", "适用阶段", "用例状态"]


def _zentao_rows():
    return [
        ["1", "天工", "/账号/登录", "", "正确账号密码登录成功", "已注册账号",
         "1. 打开登录页\n2. 输入正确账号密码\n2.1 点击登录", "1. 显示登录表单\n2. 进入首页\n2.1 显示用户名", "登录,冒烟", "功能测试", "1", "功能测试阶段", "正常"],
        ["2", "天工", "/账号/登录", "", "密码错误提示", "",
         "1. 输入错误密码", "1. 提示用户名或密码错误", "", "功能测试", "3", "功能测试阶段", "正常"],
        ["3", "天工", "", "", "第四档并入P3", "", "1. 操作", "1. 结果", "", "", "4", "", ""],
    ]


def test_zentao_csv_数字优先级与模块路径(tmp_path):
    import csv
    path = tmp_path / "禅道导出.csv"
    with path.open("w", encoding="gb18030", newline="") as fh:  # 禅道/Windows 常见 GBK 编码
        w = csv.writer(fh)
        w.writerow(_ZENTAO_HEADERS)
        w.writerows(_zentao_rows())
    cases = parse_cases_file(path)
    assert [c["priority"] for c in cases] == ["P0", "P2", "P3"]
    assert cases[0]["module"] == "账号/登录" and cases[2].get("module", "") == ""
    assert [s["action"] for s in cases[0]["steps"]] == ["打开登录页", "输入正确账号密码", "点击登录"]
    assert cases[0]["steps"][2]["expected"] == "显示用户名"
    assert cases[0]["keywords"] == "登录,冒烟"


def test_zentao_xlsx_数字单元格与br换行(tmp_path):
    from openpyxl import Workbook
    path = tmp_path / "禅道导出.xlsx"
    wb = Workbook(); ws = wb.active
    ws.append(_ZENTAO_HEADERS)
    row = list(_zentao_rows()[1]); row[10] = 2  # 数字单元格
    row[6] = "1. 输入错误密码<br />2. 点击登录"; row[7] = "1. 无<br />2. 提示用户名或密码错误"
    ws.append(row); wb.save(path)
    cases = parse_cases_file(path)
    assert cases[0]["priority"] == "P1"
    assert [s["action"] for s in cases[0]["steps"]] == ["输入错误密码", "点击登录"]
    assert cases[0]["steps"][1]["expected"] == "提示用户名或密码错误"


def test_zentao_xls_其实是html表格(tmp_path):
    path = tmp_path / "禅道导出.xls"
    head = "<tr>" + "".join(f"<th>{h}</th>" for h in _ZENTAO_HEADERS) + "</tr>"
    body = "".join("<tr>" + "".join(f"<td>{c.replace(chr(10), '<br />')}</td>" for c in r) + "</tr>" for r in _zentao_rows())
    path.write_text(f"<html><body><table>{head}{body}</table></body></html>", encoding="utf-8")
    cases = parse_cases_file(path)
    assert len(cases) == 3 and cases[0]["title"] == "正确账号密码登录成功"
    assert cases[0]["steps"][1]["action"] == "输入正确账号密码"


def test_二进制xls给出明确提示(tmp_path):
    path = tmp_path / "old.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(CaseImportError, match="xlsx 或 csv"):
        parse_cases_file(path)
