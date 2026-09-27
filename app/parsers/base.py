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


# 有内容但不参与文本解析的类型：保留可下载，解析时给出明确说明而不是「不支持」
NON_TEXT_SUFFIXES: dict[str, str] = {
    **{s: "视频" for s in (".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v")},
    **{s: "字体" for s in (".ttf", ".otf", ".woff", ".woff2")},
    **{s: "设计源文件" for s in (".indd",)},
    **{s: "邮箱数据文件" for s in (".pst", ".ost")},
}


class NonTextParser:
    suffixes = tuple(NON_TEXT_SUFFIXES)

    def parse(self, path: Path) -> ParsedDocument:
        kind = NON_TEXT_SUFFIXES[path.suffix.lower()]
        raise UnsupportedFormatError(f"{path.name} 是{kind}文件，不参与文本解析，已保留可下载")


class HtmlParser:
    suffixes = (".html", ".htm", ".xhtml", ".mht")

    def parse(self, path: Path) -> ParsedDocument:
        from app.parsers.link import html_to_markdown
        from app.parsers.text import TextParser

        title, body = html_to_markdown(read_text_any(path))
        doc = TextParser().parse_string((f"# {title}\n\n" if title else "") + body)
        doc.source, doc.doc_type = path.name, "html"
        return doc


def _registry() -> dict[str, Parser]:
    from app.parsers.container import ArchiveParser, EmailParser
    from app.parsers.convert import OfficeConvertParser
    from app.parsers.docx import DocxParser
    from app.parsers.pdf import PdfParser
    from app.parsers.pptx import PptxParser
    from app.parsers.table import TableParser
    from app.parsers.text import TextParser

    mapping: dict[str, Parser] = {}
    for parser in (TextParser(), DocxParser(), PdfParser(), TableParser(), PptxParser(), OfficeConvertParser(),
                   HtmlParser(), ArchiveParser(), EmailParser(), NonTextParser()):
        for suffix in parser.suffixes:
            mapping[suffix] = parser
    return mapping


ZIP_SUFFIXES = {".docx", ".xlsx", ".xmind", ".pptx", ".docm", ".dotx", ".dotm", ".xlsm", ".xltx", ".xltm",
                ".pptm", ".ppsx", ".ppsm", ".potx", ".potm", ".sldx", ".sldm", ".odt", ".ods", ".odp"}


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


def parse_file(path: str | Path, _depth: int = 0) -> ParsedDocument:
    """按扩展名分发解析器；zip 类先做安全校验。_depth 为压缩包 / 邮件递归层级（内部使用）。

    不限制上传格式：未登记的后缀（.json / .xml / .html / .log / .sql / 无后缀…）先按文本读取，
    能按 UTF-8 / GBK 解码的就当纯文本解析；只有二进制且无解析器的文件才报不支持。
    """
    from app.parsers.text import TextParser

    path = Path(path)
    parser = _registry().get(path.suffix.lower())
    if parser is None:
        try:
            text = read_text_any(path)
        except UnicodeDecodeError:
            raise UnsupportedFormatError(
                f"{path.name} 是不支持自动读取的二进制格式（{path.suffix or '无后缀'}）；"
                "文件已保留可下载，可转成 PDF / Word / 图片 / 文本后重新上传"
            ) from None
        if "\x00" in text[:4096]:
            raise UnsupportedFormatError(f"{path.name} 是不支持自动读取的二进制格式（{path.suffix or '无后缀'}）")
        doc = TextParser().parse_string(text)
        doc.source = path.name
        doc.doc_type = path.suffix.lstrip(".").lower() or "txt"
        return doc
    check_zip_safety(path)
    if _depth and hasattr(parser, "suffixes") and parser.__class__.__name__ in ("ArchiveParser", "EmailParser"):
        return parser.parse(path, _depth=_depth)
    return parser.parse(path)


def parse_text(text: str) -> ParsedDocument:
    """纯文本直接输入（F-2-4）：对话框粘贴需求。"""
    from app.parsers.text import TextParser

    return TextParser().parse_string(text)
