import pytest
from docx import Document

from app.parsers import UnsupportedFormatError, parse_file, parse_text


def test_纯文本解析保留标题层级():
    doc = parse_text("# 登录模块\n用户输入账号密码\n## 异常场景\n密码错误提示")
    assert doc.doc_type == "txt"
    titles = [(s.level, s.title) for s in doc.sections if s.title]
    assert (1, "登录模块") in titles
    assert (2, "异常场景") in titles
    assert "密码错误提示" in doc.full_text


def test_txt文件解析(tmp_path):
    f = tmp_path / "需求.txt"
    f.write_text("充值功能：支持微信与支付宝", encoding="utf-8")
    doc = parse_file(f)
    assert doc.source == "需求.txt"
    assert "充值功能" in doc.full_text


def test_docx解析标题正文与表格(tmp_path):
    path = tmp_path / "需求.docx"
    d = Document()
    d.add_heading("活动系统需求", level=1)
    d.add_paragraph("活动期间每日登录可领取积分。")
    d.add_heading("积分规则", level=2)
    d.add_paragraph("积分上限为 1000 分。")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "等级"
    table.cell(0, 1).text = "积分倍率"
    table.cell(1, 0).text = "VIP1"
    table.cell(1, 1).text = "1.5"
    d.save(str(path))

    doc = parse_file(path)

    assert doc.doc_type == "docx"
    titles = [(s.level, s.title) for s in doc.sections if s.title]
    assert (1, "活动系统需求") in titles
    assert (2, "积分规则") in titles
    assert "积分上限为 1000 分。" in doc.full_text
    assert doc.tables == [[["等级", "积分倍率"], ["VIP1", "1.5"]]]


def test_不支持的格式报错(tmp_path):
    f = tmp_path / "需求.xyz"
    f.write_text("x")
    with pytest.raises(UnsupportedFormatError, match="不支持"):
        parse_file(f)
