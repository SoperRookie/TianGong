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


def test_不限制格式_未知后缀按文本读取_二进制才报不支持(tmp_path):
    f = tmp_path / "需求.xyz"
    f.write_text("# 登录\n输入账号密码登录", encoding="utf-8")
    doc = parse_file(f)
    assert doc.doc_type == "xyz" and "输入账号密码登录" in doc.full_text and doc.sections[0].title == "登录"
    g = tmp_path / "接口.json"
    g.write_text('{"login": "POST /api/login"}', encoding="utf-8")
    assert "POST /api/login" in parse_file(g).full_text
    b = tmp_path / "程序.exe"
    b.write_bytes(b"MZ\x00\x01\x90\x00" * 10)
    with pytest.raises(UnsupportedFormatError, match="不支持"):
        parse_file(b)


def test_excel与csv按表格解析(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "功能清单"
    ws.append(["模块", "功能", "说明"])
    ws.append(["登录", "账号密码登录", "错误 5 次锁定"])
    ws2 = wb.create_sheet("接口")
    ws2.append(["接口", "方法"])
    ws2.append(["/api/login", "POST"])
    x = tmp_path / "需求.xlsx"
    wb.save(x)
    doc = parse_file(x)
    text = doc.full_text
    assert doc.doc_type == "xlsx" and len(doc.tables) == 2
    assert "# 功能清单" in text and "登录 | 账号密码登录 | 错误 5 次锁定" in text and "# 接口" in text and "/api/login | POST" in text
    c = tmp_path / "需求.csv"
    c.write_text("模块,功能\n登录,找回密码\n", encoding="utf-8")
    doc = parse_file(c)
    assert doc.tables == [[["模块", "功能"], ["登录", "找回密码"]]] and "登录 | 找回密码" in doc.full_text


def test_pptx按页解析文字与图片占位(tmp_path):
    import zipfile

    slide = """<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
      xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:cSld><p:spTree>
      <p:sp><p:txBody><a:p><a:r><a:t>登录流程</a:t></a:r></a:p><a:p><a:r><a:t>输入账号</a:t></a:r><a:r><a:t>密码</a:t></a:r></a:p></p:txBody></p:sp>
      <p:pic><p:blipFill><a:blip r:embed="rId2"/></p:blipFill></p:pic></p:spTree></p:cSld></p:sld>"""
    rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.png"/></Relationships>"""
    from PIL import Image
    import io
    buf = io.BytesIO(); Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    f = tmp_path / "需求.pptx"
    with zipfile.ZipFile(f, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", slide)
        zf.writestr("ppt/slides/_rels/slide1.xml.rels", rels)
        zf.writestr("ppt/media/image1.png", buf.getvalue())
        zf.writestr("ppt/slides/slide2.xml", slide.replace("登录流程", "找回密码").replace('<p:pic><p:blipFill><a:blip r:embed="rId2"/></p:blipFill></p:pic>', ""))
    doc = parse_file(f)
    assert doc.doc_type == "pptx"
    assert doc.sections[0].title == "第 1 页：登录流程" and "输入账号密码" in doc.full_text
    assert len(doc.embedded_images) == 1 and "[[图片:1]]" in doc.full_text
    assert any(s.title == "第 2 页：找回密码" for s in doc.sections)
