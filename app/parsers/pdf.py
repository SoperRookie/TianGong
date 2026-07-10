"""PDF 解析（F-2-1）：PyMuPDF 提取文本与章节结构。

扫描版 PDF（无文本层）当前抛出 ScannedPDFError，OCR 兜底链路（PaddleOCR）
待多模态/OCR 方案落地后接入（POC-R5 结论出来前不锁定实现）。
"""

from pathlib import Path

import fitz  # PyMuPDF

from app.parsers.base import ParsedDocument, Section


class ScannedPDFError(ValueError):
    pass


# 正文主流字号的倍数超过该值视为标题
_HEADING_SIZE_RATIO = 1.15


class PdfParser:
    suffixes = (".pdf",)

    def parse(self, path: Path) -> ParsedDocument:
        with fitz.open(str(path)) as doc:
            spans = self._collect_spans(doc)
        if not spans:
            raise ScannedPDFError(
                f"{path.name} 未提取到文本，疑似扫描版 PDF；OCR 兜底链路尚未接入，"
                "请先转换为文本版 PDF 或直接粘贴需求文本"
            )
        return ParsedDocument(
            source=path.name, doc_type="pdf", sections=self._build_sections(spans)
        )

    @staticmethod
    def _collect_spans(doc: "fitz.Document") -> list[tuple[str, float]]:
        """提取 (文本, 字号) 序列，按阅读顺序。"""
        spans: list[tuple[str, float]] = []
        for page in doc:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    if not text:
                        continue
                    size = max(s["size"] for s in line["spans"])
                    spans.append((text, size))
        return spans

    @staticmethod
    def _build_sections(spans: list[tuple[str, float]]) -> list[Section]:
        # 以出现最多的字号为正文基准，显著更大的行视作标题
        sizes = [round(size, 1) for _, size in spans]
        body_size = max(set(sizes), key=sizes.count)
        heading_sizes = sorted(
            {s for s in sizes if s > body_size * _HEADING_SIZE_RATIO}, reverse=True
        )

        sections: list[Section] = []
        buffer: list[str] = []

        def flush() -> None:
            content = "\n".join(buffer).strip()
            if content:
                sections.append(Section(level=0, content=content))
            buffer.clear()

        for text, size in spans:
            rounded = round(size, 1)
            if rounded in heading_sizes:
                flush()
                level = heading_sizes.index(rounded) + 1
                sections.append(Section(level=level, title=text))
            else:
                buffer.append(text)
        flush()
        return sections
