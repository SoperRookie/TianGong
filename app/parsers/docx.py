"""Word 解析（F-2-2）：提取正文、标题层级、表格、列表，保留章节结构。"""

import re
from pathlib import Path

from docx import Document

from app.parsers.base import ParsedDocument, Section

# 兼容英文样式 "Heading 1" 与中文样式 "标题 1"
_HEADING_STYLE_RE = re.compile(r"^(?:heading|标题)\s*(\d)", re.IGNORECASE)
_LIST_STYLE_RE = re.compile(r"list|列表|项目符号|编号", re.IGNORECASE)


class DocxParser:
    suffixes = (".docx",)

    def parse(self, path: Path) -> ParsedDocument:
        document = Document(str(path))
        sections: list[Section] = []
        buffer: list[str] = []

        def flush() -> None:
            content = "\n".join(buffer).strip()
            if content:
                sections.append(Section(level=0, content=content))
            buffer.clear()

        for para in document.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            style_name = para.style.name if para.style else ""
            heading = _HEADING_STYLE_RE.match(style_name)
            if heading:
                flush()
                sections.append(Section(level=int(heading.group(1)), title=text))
            elif _LIST_STYLE_RE.search(style_name):
                buffer.append(f"- {text}")
            else:
                buffer.append(text)
        flush()

        tables = [
            [[cell.text.strip() for cell in row.cells] for row in table.rows]
            for table in document.tables
        ]
        return ParsedDocument(source=path.name, doc_type="docx", sections=sections, tables=tables)
