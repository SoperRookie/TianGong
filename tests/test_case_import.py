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
