"""PDF 解析（F-2-1）：PyMuPDF 提取文本与章节结构。

扫描版 PDF（无文本层）当前抛出 ScannedPDFError，OCR 兜底链路（PaddleOCR）
待多模态/OCR 方案落地后接入（POC-R5 结论出来前不锁定实现）。
"""

from pathlib import Path

import fitz  # PyMuPDF

from app.parsers.base import EmbeddedImage, ParsedDocument, Section


class ScannedPDFError(ValueError):
    pass


# 正文主流字号的倍数超过该值视为标题
_HEADING_SIZE_RATIO = 1.15


class PdfParser:
    suffixes = (".pdf",)

    def parse(self, path: Path) -> ParsedDocument:
        pages: list[tuple[list[tuple[str, float]], list[tuple[bytes, str]]]] = []
        from app.config import get_settings
        from app.parsers.base import UnsafeFileError

        with fitz.open(str(path)) as doc:
            max_pages = get_settings().max_pdf_pages
            if doc.page_count > max_pages:
                raise UnsafeFileError(f"{path.name} 共 {doc.page_count} 页，超过解析上限 {max_pages} 页，请拆分后上传")
            seen_xrefs: set[int] = set()  # 页眉 logo 等重复图片只取一次
            for page in doc:
                pages.append(
                    (self._page_spans(page), self._page_images(doc, page, seen_xrefs))
                )
        all_spans = [s for spans, _ in pages for s in spans]
        if not all_spans:
            raise ScannedPDFError(
                f"{path.name} 未提取到文本，疑似扫描版 PDF；OCR 兜底链路尚未接入，"
                "请先转换为文本版 PDF 或直接粘贴需求文本"
            )
        sections, images = self._build(pages, all_spans)
        return ParsedDocument(
            source=path.name, doc_type="pdf", sections=sections, embedded_images=images
        )

    @staticmethod
    def _page_spans(page: "fitz.Page") -> list[tuple[str, float]]:
        """提取单页 (文本, 字号) 序列，按阅读顺序。"""
        spans: list[tuple[str, float]] = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(s["text"] for s in line["spans"]).strip()
                if not text:
                    continue
                size = max(s["size"] for s in line["spans"])
                spans.append((text, size))
        return spans

    @staticmethod
    def _page_images(
        doc: "fitz.Document", page: "fitz.Page", seen_xrefs: set[int]
    ) -> list[tuple[bytes, str]]:
        images: list[tuple[bytes, str]] = []
        for info in page.get_images(full=True):
            xref = info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            extracted = doc.extract_image(xref)
            images.append((extracted["image"], f"image/{extracted['ext']}"))
        return images

    @staticmethod
    def _build(
        pages: list[tuple[list[tuple[str, float]], list[tuple[bytes, str]]]],
        all_spans: list[tuple[str, float]],
    ) -> tuple[list[Section], list[EmbeddedImage]]:
        # 以全文出现最多的字号为正文基准，显著更大的行视作标题
        sizes = [round(size, 1) for _, size in all_spans]
        body_size = max(set(sizes), key=sizes.count)
        heading_sizes = sorted(
            {s for s in sizes if s > body_size * _HEADING_SIZE_RATIO}, reverse=True
        )

        sections: list[Section] = []
        images: list[EmbeddedImage] = []
        buffer: list[str] = []

        def flush() -> None:
            content = "\n".join(buffer).strip()
            if content:
                sections.append(Section(level=0, content=content))
            buffer.clear()

        for spans, page_images in pages:
            for text, size in spans:
                rounded = round(size, 1)
                if rounded in heading_sizes:
                    flush()
                    level = heading_sizes.index(rounded) + 1
                    sections.append(Section(level=level, title=text))
                else:
                    buffer.append(text)
            flush()
            # 图片位置精确到页：占位符附加在该页文本之后
            for data, mime in page_images:
                placeholder = f"[[图片:{len(images) + 1}]]"
                sections.append(Section(level=0, content=placeholder))
                images.append(EmbeddedImage(placeholder=placeholder, data=data, mime=mime))
        return sections, images
