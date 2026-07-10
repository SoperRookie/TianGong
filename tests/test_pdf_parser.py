import fitz
import pytest

from app.parsers import ScannedPDFError, parse_file


def _make_pdf(path, pages):
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for text, size in lines:
            page.insert_text((72, y), text, fontsize=size)
            y += size + 12
    doc.save(str(path))
    doc.close()


def test_pdf解析标题与正文(tmp_path):
    path = tmp_path / "req.pdf"
    _make_pdf(
        path,
        [
            [
                ("Login Requirements", 20),
                ("User can login with account and password.", 11),
                ("Error Handling", 16),
                ("Show error message when password is wrong.", 11),
            ]
        ],
    )
    doc = parse_file(path)

    assert doc.doc_type == "pdf"
    titles = [(s.level, s.title) for s in doc.sections if s.title]
    assert (1, "Login Requirements") in titles
    assert (2, "Error Handling") in titles
    assert "password is wrong" in doc.full_text


def test_扫描版pdf报错(tmp_path):
    path = tmp_path / "scan.pdf"
    doc = fitz.open()
    doc.new_page()  # 空白页无文本层，模拟扫描件
    doc.save(str(path))
    doc.close()

    with pytest.raises(ScannedPDFError, match="扫描"):
        parse_file(path)
