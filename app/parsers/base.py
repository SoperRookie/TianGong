"""需求解析抽象：各格式解析器统一输出 ParsedDocument（供需求分析 Agent 消费）。"""

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


class Section(BaseModel):
    """带层级的章节：level=0 表示正文段落，1/2/3... 对应标题层级。"""

    level: int = 0
    title: str = ""
    content: str = ""


class EmbeddedImage(BaseModel):
    """混排文档中的内嵌图片：placeholder 对应正文中的占位 Section，理解后原位回填。"""

    placeholder: str
    data: bytes = Field(exclude=True, repr=False)
    mime: str = "image/png"


class ParsedDocument(BaseModel):
    source: str = Field(description="来源文件名或 'text'")
    doc_type: str = Field(description="txt / docx / pdf / image ...")
    sections: list[Section] = Field(default_factory=list)
    tables: list[list[list[str]]] = Field(default_factory=list, description="表格：表 -> 行 -> 单元格")
    embedded_images: list[EmbeddedImage] = Field(
        default_factory=list, exclude=True, description="待 Vision 理解的内嵌图片（不序列化）"
    )

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


ZIP_SUFFIXES = {".docx", ".xlsx", ".xmind", ".pptx"}


class UnsafeFileError(ValueError):
    """解压炸弹 / 超限文档：拒绝解析。"""


def check_zip_safety(path: str | Path, max_uncompressed_mb: int | None = None, max_ratio: int = 200) -> None:
    """zip 类文档解析前校验解压后总大小与压缩比，防解压炸弹（在读入任何内容之前）。"""
    import zipfile

    from app.config import get_settings

    path = Path(path)
    if path.suffix.lower() not in ZIP_SUFFIXES:
        return
    limit = (max_uncompressed_mb or get_settings().max_zip_uncompressed_mb) * 1024 * 1024
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as e:
        raise UnsafeFileError(f"{path.name} 不是有效的压缩文档: {e}")
    total = sum(i.file_size for i in infos)
    compressed = sum(i.compress_size for i in infos) or 1
    if total > limit:
        raise UnsafeFileError(f"{path.name} 解压后 {total // (1024 * 1024)}MB 超过上限 {limit // (1024 * 1024)}MB，拒绝解析")
    if total > 10 * 1024 * 1024 and total / compressed > max_ratio:
        raise UnsafeFileError(f"{path.name} 压缩比异常（{total // compressed}:1），疑似解压炸弹，拒绝解析")
    if len(infos) > 20000:
        raise UnsafeFileError(f"{path.name} 内含 {len(infos)} 个条目，超过上限，拒绝解析")


def read_text_any(path: str | Path) -> str:
    """文本文件按 utf-8-sig → gb18030 依次尝试（国内 Windows 导出的 txt/csv 多为 GBK）。"""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", raw[:16], 0, 1, f"{Path(path).name} 不是 UTF-8 或 GBK 编码的文本文件")


def parse_file(path: str | Path) -> ParsedDocument:
    """按扩展名分发解析器（格式白名单校验，F-2-8 的一部分）；zip 类先做安全校验。"""
    path = Path(path)
    parser = _registry().get(path.suffix.lower())
    if parser is None:
        supported = ", ".join(sorted(_registry()))
        raise UnsupportedFormatError(f"不支持的文件格式 {path.suffix}，当前支持: {supported}")
    check_zip_safety(path)
    return parser.parse(path)


def parse_text(text: str) -> ParsedDocument:
    """纯文本直接输入（F-2-4）：对话框粘贴需求。"""
    from app.parsers.text import TextParser

    return TextParser().parse_string(text)
