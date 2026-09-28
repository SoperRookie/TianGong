"""PowerPoint（.pptx）解析：不依赖 python-pptx，直接读 zip 内各页幻灯片 XML 的文本与图片。

每页一个章节（标题取页内第一段文字），页内图片作为内嵌图片占位，由 enrich_images 经 Vision 理解后回填。
"""

import re
import zipfile
from pathlib import Path

from app.parsers.base import EmbeddedImage, ParsedDocument, Section

_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
       "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
_IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}


class PptxParser:
    suffixes = (".pptx",)

    def parse(self, path: Path) -> ParsedDocument:
        from lxml import etree

        sections: list[Section] = []
        images: list[EmbeddedImage] = []
        with zipfile.ZipFile(path) as zf:
            names = sorted((int(m.group(1)), n) for n in zf.namelist() if (m := _SLIDE_RE.match(n)))
            if not names:
                raise ValueError(f"{path.name} 不含幻灯片")
            for no, name in names:
                root = etree.fromstring(zf.read(name))
                paras: list[str] = []
                for p in root.iter(f"{{{_NS['a']}}}p"):
                    text = "".join(t.text or "" for t in p.iter(f"{{{_NS['a']}}}t")).strip()
                    if text:
                        paras.append(text)
                title = paras[0] if paras else f"第 {no} 页"
                sections.append(Section(level=1, title=f"第 {no} 页：{title}"))
                if len(paras) > 1:
                    sections.append(Section(level=0, content="\n".join(paras[1:])))
                # 页内图片：通过 rels 找到媒体文件
                rel_name = f"ppt/slides/_rels/slide{no}.xml.rels"
                if rel_name in zf.namelist():
                    rels = etree.fromstring(zf.read(rel_name))
                    targets = {r.get("Id"): r.get("Target") for r in rels if r.get("Type", "").endswith("/image")}
                    for blip in root.iter(f"{{{_NS['a']}}}blip"):
                        rid = blip.get(f"{{{_NS['r']}}}embed")
                        target = targets.get(rid)
                        if not target:
                            continue
                        media = "ppt/" + target.replace("../", "")
                        if media not in zf.namelist():
                            continue
                        mime = _IMG_MIME.get(Path(media).suffix.lower())
                        if not mime:
                            continue
                        placeholder = f"[[图片:{len(images) + 1}]]"
                        sections.append(Section(level=0, content=placeholder))
                        images.append(EmbeddedImage(placeholder=placeholder, data=zf.read(media), mime=mime))
        return ParsedDocument(source=path.name, doc_type="pptx", sections=sections, embedded_images=images)
