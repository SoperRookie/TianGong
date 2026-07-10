"""纯文本解析（F-2-4）：识别 Markdown 风格标题以保留层级。"""

import re
from pathlib import Path

from app.parsers.base import ParsedDocument, Section

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


class TextParser:
    suffixes = (".txt", ".md", ".markdown")

    def parse(self, path: Path) -> ParsedDocument:
        doc = self.parse_string(path.read_text(encoding="utf-8"))
        doc.source = path.name
        return doc

    def parse_string(self, text: str) -> ParsedDocument:
        sections: list[Section] = []
        buffer: list[str] = []

        def flush() -> None:
            content = "\n".join(buffer).strip()
            if content:
                sections.append(Section(level=0, content=content))
            buffer.clear()

        for line in text.splitlines():
            m = _HEADING_RE.match(line.strip())
            if m:
                flush()
                sections.append(Section(level=len(m.group(1)), title=m.group(2).strip()))
            else:
                buffer.append(line)
        flush()
        return ParsedDocument(source="text", doc_type="txt", sections=sections)
