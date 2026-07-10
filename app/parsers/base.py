"""需求解析抽象：各格式解析器统一输出 ParsedDocument（供需求分析 Agent 消费）。"""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


class Section(BaseModel):
    """带层级的章节：level=0 表示正文段落，1/2/3... 对应标题层级。"""

    level: int = 0
    title: str = ""
    content: str = ""


class ParsedDocument(BaseModel):
    source: str = Field(description="来源文件名或 'text'")
    doc_type: str = Field(description="txt / docx / pdf / image ...")
    sections: list[Section] = Field(default_factory=list)
    tables: list[list[list[str]]] = Field(default_factory=list, description="表格：表 -> 行 -> 单元格")

    @property
    def full_text(self) -> str:
        """按章节顺序拼接的纯文本，供直接注入 LLM 上下文。"""
        parts: list[str] = []
        for sec in self.sections:
            if sec.title:
                parts.append(f"{'#' * max(sec.level, 1)} {sec.title}")
            if sec.content:
                parts.append(sec.content)
        for i, table in enumerate(self.tables, 1):
            parts.append(f"[表格{i}]")
            parts.extend(" | ".join(row) for row in table)
        return "\n".join(parts)


class Parser(Protocol):
    suffixes: tuple[str, ...]

    def parse(self, path: Path) -> ParsedDocument: ...


class UnsupportedFormatError(ValueError):
    pass


def _registry() -> dict[str, Parser]:
    from app.parsers.docx import DocxParser
    from app.parsers.pdf import PdfParser
    from app.parsers.text import TextParser

    mapping: dict[str, Parser] = {}
    for parser in (TextParser(), DocxParser(), PdfParser()):
        for suffix in parser.suffixes:
            mapping[suffix] = parser
    return mapping


def parse_file(path: str | Path) -> ParsedDocument:
    """按扩展名分发解析器（格式白名单校验，F-2-8 的一部分）。"""
    path = Path(path)
    parser = _registry().get(path.suffix.lower())
    if parser is None:
        supported = ", ".join(sorted(_registry()))
        raise UnsupportedFormatError(f"不支持的文件格式 {path.suffix}，当前支持: {supported}")
    return parser.parse(path)


def parse_text(text: str) -> ParsedDocument:
    """纯文本直接输入（F-2-4）：对话框粘贴需求。"""
    from app.parsers.text import TextParser

    return TextParser().parse_string(text)
