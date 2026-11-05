"""MHTML / 网页归档解析：Confluence「导出为 Word」、IE / Edge「另存为 .mht」、Word「单个文件网页」。

这类文件是 MIME multipart/related：第一部分是 HTML 正文（通常 quoted-printable），其余部分是 base64 内嵌图片，
用 Content-Location / Content-ID 与正文里的 <img src> 对应。Confluence 导出的 .doc 后缀文件实际就是它，
交给 LibreOffice 会被当网页加载并拉外链，交给纯 HTML 抽取会把 MIME 头与 base64 当正文。

输出与 Word 解析器同口径：标题层级 → Section(level, title)，段落 / 列表 → 正文行，表格 → tables，
内嵌图片按位置生成 [[图片:N]] 占位，由 enrich_images 送 Vision 理解后原位回填。
"""

import email
import re
from email import policy
from pathlib import Path
from posixpath import basename
from urllib.parse import unquote, urlparse

from app.parsers.base import EmbeddedImage, ParsedDocument, Section, UnsupportedFormatError

_HEADING_RE = re.compile(r"^h([1-6])$")
_SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg", "head", "title", "meta", "link", "xml", "o:p"}
_INLINE_TAGS = {"span", "a", "b", "strong", "i", "em", "u", "s", "sub", "sup", "small", "big", "font", "code", "kbd",
                "mark", "abbr", "cite", "q", "time", "label", "br", "wbr", "del", "ins", "tt", "var"}
_IMAGE_MAGIC = ((b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"), (b"GIF8", "image/gif"),
                (b"BM", "image/bmp"), (b"RIFF", "image/webp"))


def _image_mime(data: bytes, declared: str) -> str | None:
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    return declared if declared.startswith("image/") else None


def _resource_keys(location: str | None, cid: str | None) -> set[str]:
    """一个资源可能被 src 用全路径、仅文件名、cid: 三种方式引用，全部登记。"""
    keys: set[str] = set()
    if location:
        loc = location.strip()
        keys.add(loc)
        keys.add(unquote(loc))
        name = basename(urlparse(loc).path) or basename(loc)
        if name:
            keys.add(name)
            keys.add(unquote(name))
    if cid:
        c = cid.strip().strip("<>")
        keys.update({c, f"cid:{c}"})
    return {k for k in keys if k}


def read_mhtml(raw: bytes) -> tuple[str, dict[str, tuple[bytes, str]]]:
    """→ (HTML 正文, {引用键: (图片字节, mime)})。"""
    msg = email.message_from_bytes(raw, policy=policy.default)
    html: str | None = None
    resources: dict[str, tuple[bytes, str]] = {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True) or b""
        if ctype == "text/html" and html is None:
            charset = part.get_content_charset() or "utf-8"
            try:
                html = payload.decode(charset, errors="replace")
            except LookupError:
                html = payload.decode("utf-8", errors="replace")
            continue
        mime = _image_mime(payload[:16], ctype)
        if mime is None:
            continue
        for key in _resource_keys(part.get("Content-Location"), part.get("Content-ID")):
            resources.setdefault(key, (payload, mime))
    if html is None:
        raise UnsupportedFormatError("MHTML 里没有 HTML 正文部分")
    return html, resources


def _lookup(resources: dict[str, tuple[bytes, str]], src: str) -> tuple[bytes, str] | None:
    src = src.strip()
    for key in (src, unquote(src), basename(urlparse(src).path), basename(src), src.removeprefix("cid:")):
        if key and key in resources:
            return resources[key]
    return None


def html_to_document(html: str, resources: dict[str, tuple[bytes, str]] | None = None) -> ParsedDocument:
    """HTML → ParsedDocument（章节 / 列表 / 表格 / 内嵌图片占位）。resources 为 src → 图片字节的映射（MHTML 内嵌）。"""
    import lxml.html

    resources = resources or {}
    try:
        root = lxml.html.fromstring(html)
    except Exception as e:
        raise UnsupportedFormatError(f"网页正文无法解析：{e}") from e
    for bad in root.xpath("|".join(f"//{t}" for t in ("script", "style", "noscript", "iframe", "svg"))):
        parent = bad.getparent()
        if parent is not None:
            parent.remove(bad)

    sections: list[Section] = []
    tables: list[list[list[str]]] = []
    images: list[EmbeddedImage] = []
    buffer: list[str] = []

    def flush() -> None:
        content = "\n".join(line for line in buffer if line is not None).strip()
        if content:
            sections.append(Section(level=0, content=content))
        buffer.clear()

    def add_image(el) -> None:
        found = _lookup(resources, el.get("src") or "")
        if found is None:
            alt = " ".join((el.get("alt") or "").split())
            if alt:
                buffer.append(f"[图片：{alt}]")
            return
        data, mime = found
        flush()
        placeholder = f"[[图片:{len(images) + 1}]]"
        sections.append(Section(level=0, content=placeholder))
        images.append(EmbeddedImage(placeholder=placeholder, data=data, mime=mime))

    def inline_text(el) -> list[str]:
        """只含行内元素的节点 → 文本行（<br> 断行，span 之间不加空格），遇到 <img> 就地处理。"""
        parts: list[str] = []

        def collect(node) -> None:
            if isinstance(node.tag, str):
                if node.tag == "br":
                    parts.append("\n")
                elif node.tag == "img":
                    parts.append("\x00IMG\x00")
                    pending_images.append(node)
                elif node.text:
                    parts.append(node.text)
            for child in node:
                if isinstance(child.tag, str) and child.tag not in _SKIP_TAGS:
                    collect(child)
                if child.tail:
                    parts.append(child.tail)

        pending_images: list = []
        collect(el)
        lines: list[str] = []
        for chunk in "".join(parts).split("\x00IMG\x00"):
            for line in chunk.split("\n"):
                line = " ".join(line.split())
                if line:
                    lines.append(line)
            if pending_images:
                lines.append(None)  # 占位：在 walk 里按顺序插图
        return lines, pending_images

    def has_block(el) -> bool:
        return any(isinstance(c.tag, str) and c.tag not in _INLINE_TAGS and c.tag != "img" for c in el.iterdescendants())

    def emit_inline(el, prefix: str = "", cont_prefix: str | None = None) -> None:
        """prefix 加在第一行（列表项的「- 」），cont_prefix 加在后续行（默认与 prefix 等宽缩进）。"""
        lines, imgs = inline_text(el)
        imgs = list(imgs)
        if cont_prefix is None:
            cont_prefix = " " * len(prefix) if prefix.strip() else prefix
        first = True
        for line in lines:
            if line is None:
                if imgs:
                    add_image(imgs.pop(0))
                continue
            buffer.append((prefix if first else cont_prefix) + line)
            first = False
        for im in imgs:
            add_image(im)

    def walk(el, list_prefix: str = "") -> None:
        if not isinstance(el.tag, str) or el.tag in _SKIP_TAGS:
            return
        tag = el.tag
        heading = _HEADING_RE.match(tag)
        if heading:
            text = " ".join(el.text_content().split())
            if text:
                flush()
                sections.append(Section(level=int(heading.group(1)), title=text))
            for im in el.iter("img"):
                add_image(im)
            return
        if tag == "img":
            add_image(el)
            return
        if tag == "table":
            flush()
            rows: list[list[str]] = []
            for tr in el.iter("tr"):
                cells = [" ".join(td.text_content().split()) for td in tr if isinstance(td.tag, str) and td.tag in ("td", "th")]
                if any(cells):
                    rows.append(cells)
            if rows:
                tables.append(rows)
            for im in el.iter("img"):
                add_image(im)
            return
        if tag == "li":
            # 自己的行内文字 + 只含行内内容的 p / div 合成一条列表项（Confluence 导出的 li 里套 p）；
            # 嵌套的 ul / ol / table 等块级子项再递归，缩进两格
            own = lxml.html.Element("span")
            own.text = el.text
            nested = []
            for child in el:
                if not isinstance(child.tag, str) or child.tag in _INLINE_TAGS or child.tag == "img":
                    own.append(_clone(child))
                elif child.tag in ("ul", "ol", "table") or has_block(child):
                    nested.append(child)
                else:
                    own.append(lxml.html.Element("br"))
                    own.append(_clone(child))
            emit_inline(own, prefix=f"{list_prefix}- ", cont_prefix=f"{list_prefix}  ")
            for child in nested:
                walk(child, list_prefix + "  ")
            return
        if tag in ("ul", "ol"):
            for child in el:
                walk(child, list_prefix)
            return
        if not has_block(el):
            emit_inline(el, prefix=list_prefix)
            if tag in ("p", "div", "pre", "blockquote", "section", "article"):
                buffer.append("")
            return
        if el.text and el.text.strip():
            buffer.append(list_prefix + " ".join(el.text.split()))
        for child in el:
            walk(child, list_prefix)
            if child.tail and child.tail.strip():
                buffer.append(list_prefix + " ".join(child.tail.split()))

    body = root.body if root.body is not None else root
    walk(body)
    flush()
    # 连续空行压成一个
    cleaned: list[Section] = []
    for sec in sections:
        if sec.content:
            sec.content = re.sub(r"\n{3,}", "\n\n", sec.content).strip()
        cleaned.append(sec)
    return ParsedDocument(source="mhtml", doc_type="mht", sections=cleaned, tables=tables, embedded_images=images)


def _clone(el):
    import copy

    c = copy.deepcopy(el)
    return c


class MhtmlParser:
    suffixes = (".mht", ".mhtml")

    def parse(self, path: Path) -> ParsedDocument:
        html, resources = read_mhtml(path.read_bytes())
        doc = html_to_document(html, resources)
        doc.source = path.name
        return doc
